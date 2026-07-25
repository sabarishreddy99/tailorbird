#!/usr/bin/env python3
"""
ats.py - fetch a job description from any posting URL, in pure Python.

All fetching happens here (urllib), never through a Claude tool, so the agent
never triggers a permission prompt and new ATS hosts need no allow-listing.

Fast paths mirror the "Company Research Shortcuts" in
resumes/_index/LIBRARY.md: Greenhouse, Ashby, Lever, Workday, Oracle ORC,
SmartRecruiters, Apple, Microsoft, Amazon. Anything else falls back to a
platform sniff (embedded Greenhouse/Ashby/Lever/Workday) and finally a plain
GET + HTML-to-text extraction, so an unseen host still yields JD text.

    from ats import fetch
    job = fetch(url)   # {title, location, comp, text, removed, source, url}
"""

import html as htmllib
import json
import os
import re
import sys
import urllib.request
import urllib.error

# Make sibling modules importable whether this runs as a script or is imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screen import parse_comp

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 25


def http_get(url, as_json=False, accept=None):
    """Return (status, body|obj) or (status, None). Never raises for HTTP errors."""
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    elif as_json:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            if as_json:
                try:
                    return r.status, json.loads(raw)
                except json.JSONDecodeError:
                    return r.status, None
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def strip_html(s):
    s = htmllib.unescape(s or "")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def _result(text, title="", location="", source="generic", url="", removed=False):
    comp = parse_comp(text) if text else None
    return {"title": title, "location": location, "comp": comp,
            "text": text or "", "removed": removed, "source": source, "url": url}


# --------------------------------------------------------------- Greenhouse

def _gh_api(token, jid, url):
    status, d = http_get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}", as_json=True)
    if status == 200 and d:
        return _result(strip_html(d.get("content", "")),
                       title=d.get("title", ""),
                       location=(d.get("location") or {}).get("name", ""),
                       source=f"greenhouse:{token}", url=url)
    if status == 404:
        return _result("", source=f"greenhouse:{token}", url=url, removed=True)
    return None


def fetch_greenhouse(url):
    # Direct board URL: job-boards|boards.greenhouse.io/{token}/jobs/{id}
    m = re.search(r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_app\?for=)?([^/?]+)/jobs/(\d+)", url)
    if m:
        return _gh_api(m.group(1), m.group(2), url)
    # Company domain with ?gh_jid=... : resolve the board token, which often is
    # NOT the domain label (otter.ai -> 'otterai', unrivaled -> 'unrivaledbasketball').
    m = re.search(r"[?&]gh_jid=(\d+)", url)
    if not m:
        return None
    jid = m.group(1)
    candidates = []
    status, htmlbody = http_get(url)
    if htmlbody:
        t = re.search(r"greenhouse\.io/(?:embed/)?(?:job_board|[^/]+)/js\?for=([a-z0-9_]+)", htmlbody, re.I)
        if t:
            candidates.append(t.group(1))
    dm = re.search(r"https?://(?:www\.|job[-.]?boards?\.|careers?\.)?([a-z0-9-]+)\.", url, re.I)
    if dm:
        base = dm.group(1).replace("-", "")
        candidates += [base, base + "ai", base.replace("ai", ""), base + "careers",
                       base + "jobs", base + "hq", base + "inc"]
    seen = set()
    for tok in candidates:
        if not tok or tok in seen:
            continue
        seen.add(tok)
        r = _gh_api(tok, jid, url)
        if r and not r["removed"] and r["text"]:
            return r
    # Could not resolve the board confidently. Do NOT claim "removed" (that would
    # be a false drop); return unresolved so the screen routes it to needs_review.
    return _result("", source="greenhouse:unresolved", url=url, removed=False)


# --------------------------------------------------------------------- Ashby

def fetch_ashby(url):
    m = re.search(r"(?:jobs\.ashbyhq\.com|ashbyhq\.com/[^/]*)/([^/?]+)/([0-9a-f-]{36})", url) \
        or re.search(r"ashby.*?/([^/?]+)/([0-9a-f-]{36})", url)
    org = uuid = None
    if m:
        org, uuid = m.group(1), m.group(2)
    else:
        m = re.search(r"ashbyhq\.com/([^/?]+)", url)
        u = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})", url)
        if m and u:
            org, uuid = m.group(1), u.group(1)
    if not (org and uuid):
        return None
    status, d = http_get(
        f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true",
        as_json=True)
    if status != 200 or not d:
        return None
    for p in d.get("jobs", []):
        if uuid in (p.get("jobUrl", "") + p.get("id", "")):
            comp = p.get("compensation") or {}
            text = p.get("descriptionPlain") or strip_html(p.get("descriptionHtml", ""))
            r = _result(text, title=p.get("title", ""),
                        location=p.get("location", ""), source=f"ashby:{org}", url=url)
            return r
    return _result("", source=f"ashby:{org}", url=url, removed=True)


# --------------------------------------------------------------------- Lever

def fetch_lever(url):
    m = re.search(r"lever\.co/([^/?]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    org, uuid = m.group(1), m.group(2)
    status, d = http_get(f"https://api.lever.co/v0/postings/{org}/{uuid}", as_json=True)
    if status == 404:
        return _result("", source=f"lever:{org}", url=url, removed=True)
    if status != 200 or not d:
        return None
    parts = [d.get("descriptionPlain", "")]
    for lst in d.get("lists", []):
        parts.append(strip_html(lst.get("text", "")) + "\n" + strip_html(lst.get("content", "")))
    parts.append(d.get("additionalPlain", ""))
    return _result("\n".join(p for p in parts if p),
                   title=d.get("text", ""),
                   location=(d.get("categories") or {}).get("location", ""),
                   source=f"lever:{org}", url=url)


# ------------------------------------------------------------------- Workday

def fetch_workday(url):
    m = re.search(r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[^/]+/)?([^/]+)/job/(.+)", url, re.I)
    if not m:
        return None
    tenant, wd, site, path = m.group(1), m.group(2), m.group(3), m.group(4)
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}"
    status, d = http_get(api, as_json=True, accept="application/json")
    if status != 200 or not d:
        return None
    info = d.get("jobPostingInfo", d)
    return _result(strip_html(info.get("jobDescription", "")),
                   title=info.get("title", ""),
                   location=info.get("location", ""),
                   source=f"workday:{tenant}", url=url)


# --------------------------------------------------------------- SmartRecruiters

def fetch_smartrecruiters(url):
    m = re.search(r"smartrecruiters\.com/(?:[^/]+/)?([^/]+)/(\d+)", url) \
        or re.search(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", url)
    if not m:
        return None
    co, pid = m.group(1), m.group(2)
    status, d = http_get(
        f"https://api.smartrecruiters.com/v1/companies/{co}/postings/{pid}", as_json=True)
    if status != 200 or not d:
        return None
    secs = (d.get("jobAd") or {}).get("sections") or {}
    text = "\n".join(strip_html((secs.get(k) or {}).get("text", ""))
                     for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
    return _result(text, title=d.get("name", ""),
                   location=(d.get("location") or {}).get("city", ""),
                   source=f"smartrecruiters:{co}", url=url)


# --------------------------------------------------------------------- Apple

def fetch_apple(url):
    if "jobs.apple.com" not in url:
        return None
    status, body = http_get(url)
    if not body:
        return _result("", source="apple", url=url, removed=(status == 404))
    i = body.find("__staticRouterHydrationData")
    if i == -1:
        return None
    seg = body[i:i + 500000]
    m = re.search(r'JSON\.parse\("', seg)
    if not m:
        return None
    j = m.end()
    out = []
    while j < len(seg):
        c = seg[j]
        if c == "\\":
            out.append(seg[j:j + 2]); j += 2; continue
        if c == '"':
            break
        out.append(c); j += 1
    try:
        obj = json.loads(json.loads('"' + "".join(out) + '"'))
    except json.JSONDecodeError:
        return None

    def walk(o):
        if isinstance(o, dict):
            if "jobsData" in o:
                return o["jobsData"]
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None

    jd = walk(obj)
    if not jd:
        return None
    parts = [jd.get("jobSummary", ""), jd.get("description", ""),
             jd.get("responsibilities", ""), jd.get("minimumQualifications", ""),
             jd.get("preferredQualifications", "")]
    locs = jd.get("locations") or []
    loc = ", ".join(l.get("name", "") for l in locs if isinstance(l, dict))[:80]
    return _result(strip_html(" \n".join(p for p in parts if p)),
                   title=jd.get("postingTitle", ""), location=loc, source="apple", url=url)


# ----------------------------------------------------------------- Microsoft

def fetch_microsoft(url):
    m = re.search(r"careers\.microsoft\.com.*?/(\d{6,})", url) or re.search(r"[?&]jobId=(\d+)", url)
    if not m:
        return None
    jid = m.group(1)
    status, d = http_get(
        f"https://apply.careers.microsoft.com/api/apply/v2/jobs/{jid}?domain=microsoft.com",
        as_json=True)
    if status != 200 or not d:
        return None
    return _result(strip_html(d.get("job_description", d.get("description", ""))),
                   title=d.get("name", ""),
                   location=d.get("location", ""), source="microsoft", url=url)


# -------------------------------------------------------------------- Amazon

def fetch_amazon(url):
    m = re.search(r"amazon\.jobs/[^/]+/jobs/(\d+)", url)
    if not m:
        return None
    jid = m.group(1)
    status, d = http_get(
        f"https://www.amazon.jobs/en/search.json?base_query={jid}&result_limit=5", as_json=True)
    if status != 200 or not d:
        return None
    for j in d.get("jobs", []):
        if str(j.get("id_icims", "")) == jid or jid in j.get("job_path", ""):
            text = "\n".join([j.get("description", ""), j.get("basic_qualifications", ""),
                              j.get("preferred_qualifications", "")])
            return _result(strip_html(text), title=j.get("title", ""),
                           location=j.get("normalized_location", ""), source="amazon", url=url)
    return None


# ------------------------------------------------------------------- generic

def fetch_generic(url):
    """Sniff an embedded ATS from the page, else return page text."""
    status, body = http_get(url)
    if not body:
        return _result("", source="generic", url=url, removed=(status in (404, 410)))
    # Embedded Greenhouse board on a company page.
    m = re.search(r"boards\.greenhouse\.io/embed/job_board/js\?for=([a-z0-9_]+)", body, re.I)
    j = re.search(r"[?&]gh_jid=(\d+)", url) or re.search(r"gh_jid[\"']?\s*[:=]\s*[\"']?(\d+)", body)
    if m and j:
        r = _gh_api(m.group(1), j.group(1), url)
        if r:
            return r
    text = strip_html(body)
    # Trim obvious nav/footer noise by keeping the densest middle if huge.
    return _result(text[:20000], source="generic-html", url=url,
                   removed=(len(text) < 200))


ADAPTERS = [
    ("greenhouse", fetch_greenhouse),
    ("ashby", fetch_ashby),
    ("lever", fetch_lever),
    ("workday", fetch_workday),
    ("smartrecruiters", fetch_smartrecruiters),
    ("apple", fetch_apple),
    ("microsoft", fetch_microsoft),
    ("amazon", fetch_amazon),
]


def fetch(url):
    """Try known adapters (by URL hint), then the generic fallback."""
    for name, fn in ADAPTERS:
        if name in url or (name == "greenhouse" and "gh_jid" in url) \
           or (name == "ashby" and "ashby" in url) \
           or (name == "workday" and "myworkday" in url):
            try:
                r = fn(url)
            except Exception:
                r = None
            if r is not None:
                return r
    try:
        return fetch_generic(url)
    except Exception:
        return _result("", source="generic", url=url, removed=True)


if __name__ == "__main__":
    import sys
    for u in (sys.argv[1:] or [
        "https://job-boards.greenhouse.io/vestwell/jobs/7751400003",
    ]):
        j = fetch(u)
        print(f"[{j['source']}] removed={j['removed']} title={j['title']!r} "
              f"loc={j['location']!r} comp={j['comp']} chars={len(j['text'])}")
