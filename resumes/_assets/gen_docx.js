// gen_docx.js - generic resume DOCX generator. Called by build_resume.py.
// Usage: node gen_docx.js <content.json>   (JSON produced by build_resume.py)
const fs = require("fs");
const modPath = "/opt/homebrew/lib/node_modules/docx";
const { Document, Packer, Paragraph, TextRun, ExternalHyperlink, AlignmentType,
        LevelFormat, TabStopType, TabStopPosition, BorderStyle } = require(modPath);

const FONT = "Calibri";
const BODY = 20;   // 10pt
const SMALL = 19;  // 9.5pt

const doc0 = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

const link = (text, url, size = SMALL) => new ExternalHyperlink({
  children: [new TextRun({ text, font: FONT, size, color: "1155CC", underline: {} })],
  link: url,
});
const sectionHeader = (text) => new Paragraph({
  spacing: { before: 120, after: 50 },
  border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "404040", space: 1 } },
  children: [new TextRun({ text: text.toUpperCase(), font: FONT, size: 22, bold: true, color: "1F3864" })],
});
const runsToTextRuns = (runs, size = BODY) =>
  runs.map(r => new TextRun({ text: r.text, font: FONT, size, bold: !!r.bold, italics: !!r.italic }));
const bulletPara = (runs) => new Paragraph({
  numbering: { reference: "bullets", level: 0 },
  spacing: { after: 25 },
  children: runsToTextRuns(runs),
});
const techLine = (tech) => new Paragraph({
  spacing: { after: 30 },
  children: [new TextRun({ text: tech, font: FONT, size: SMALL, italics: true, color: "555555" })],
});

const children = [];
children.push(new Paragraph({
  alignment: AlignmentType.CENTER, spacing: { after: 30 },
  children: [new TextRun({ text: doc0.name, font: FONT, size: 34, bold: true, color: "1F3864" })],
}));
const contactRuns = [];
doc0.contact.forEach((c, i) => {
  if (i > 0) contactRuns.push(new TextRun({ text: "  |  ", font: FONT, size: SMALL }));
  if (c.url) contactRuns.push(link(c.text, c.url));
  else contactRuns.push(new TextRun({ text: c.text, font: FONT, size: SMALL }));
});
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 50 }, children: contactRuns }));

for (const sec of doc0.sections) {
  children.push(sectionHeader(sec.title));
  if (sec.type === "paragraph") {
    children.push(new Paragraph({ spacing: { after: 30 }, children: runsToTextRuns(sec.runs) }));
  } else if (sec.type === "experience") {
    for (const e of sec.entries) {
      children.push(new Paragraph({
        spacing: { before: 90, after: 0 },
        children: [new TextRun({ text: e.heading, font: FONT, size: 21, bold: true })],
      }));
      if (e.subline) children.push(new Paragraph({
        spacing: { after: 20 },
        tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
        children: [
          new TextRun({ text: e.subline.title, font: FONT, size: BODY, bold: true, italics: true }),
          new TextRun({ text: ` | ${e.subline.loc}`, font: FONT, size: BODY }),
          new TextRun({ text: `\t${e.subline.dates}`, font: FONT, size: BODY, bold: true }),
        ],
      }));
      if (e.tech) children.push(techLine(e.tech));
      e.bullet_runs.forEach(r => children.push(bulletPara(r)));
    }
  } else if (sec.type === "skills") {
    for (const s of sec.lines) children.push(new Paragraph({
      spacing: { after: 35 },
      children: [
        new TextRun({ text: `${s.cat}: `, font: FONT, size: BODY, bold: true }),
        new TextRun({ text: s.items, font: FONT, size: BODY }),
      ],
    }));
  } else if (sec.type === "projects") {
    for (const e of sec.entries) {
      const titleRuns = [new TextRun({ text: e.heading + "  ", font: FONT, size: 21, bold: true })];
      if (e.link_url) titleRuns.push(link("[link]", e.link_url, 19));
      children.push(new Paragraph({ spacing: { before: 50, after: 0 }, children: titleRuns }));
      if (e.tech) children.push(techLine(e.tech));
      e.bullet_runs.forEach(r => children.push(bulletPara(r)));
    }
  } else if (sec.type === "education") {
    sec.lines.forEach((l, idx) => {
      if (idx === 0 && l.includes(" | ")) {
        const parts = l.split(" | ").map(p => p.replace(/\*+/g, "").trim());
        children.push(new Paragraph({
          spacing: { before: 30, after: 0 },
          tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
          children: [
            new TextRun({ text: parts[0], font: FONT, size: 21, bold: true }),
            new TextRun({ text: ` | ${parts[1] || ""}`, font: FONT, size: BODY }),
            new TextRun({ text: `\t${parts[2] || ""}`, font: FONT, size: BODY, bold: true }),
          ],
        }));
      } else {
        children.push(new Paragraph({ spacing: { after: 0 }, children: runsToTextRuns(sec.line_runs[idx]) }));
      }
    });
  }
}

const docx = new Document({
  styles: { default: { document: { run: { font: FONT, size: BODY } } } },
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 300, hanging: 180 } } } }],
    }],
  },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 560, right: 700, bottom: 560, left: 700 } } },
    children,
  }],
});

Packer.toBuffer(docx).then(buf => {
  fs.writeFileSync(doc0._out, buf);
  console.log("DOCX written:", doc0._out);
});
