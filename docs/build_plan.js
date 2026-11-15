const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, WidthType, ShadingType,
        BorderStyle, AlignmentType, LevelFormat } = require("docx");

const F = "Arial", NAVY = "1F3A5F", LIGHT = "E8EEF6", GREY = "F4F4F4";
const W = 10466; // A4 width 11906 - 2*720 margins
const thin = { style: BorderStyle.SINGLE, size: 4, color: "B8C2CF" };
const borders = { top: thin, bottom: thin, left: thin, right: thin };
const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: none, bottom: none, left: none, right: none };

const run = (t, o = {}) => new TextRun({ text: t, font: F, size: o.size || 15, bold: o.bold, color: o.color });
const para = (children, o = {}) => new Paragraph({ children: Array.isArray(children) ? children : [children],
  spacing: { before: o.before || 0, after: o.after ?? 20, line: 200 }, alignment: o.align, keepNext: false });
const bullet = (parts) => new Paragraph({ numbering: { reference: "b", level: 0 },
  spacing: { before: 0, after: 10, line: 195 },
  children: parts.map(p => typeof p === "string" ? run(p, { size: 16 }) : run(p.t, { size: 16, bold: true })) });

const cell = (content, width, o = {}) => new TableCell({
  width: { size: width, type: WidthType.DXA }, borders: o.borders || borders,
  shading: o.fill ? { fill: o.fill, type: ShadingType.CLEAR, color: "auto" } : undefined,
  margins: { top: 25, bottom: 25, left: 60, right: 60 }, verticalAlign: o.v,
  children: (Array.isArray(content) ? content : [content]).map(c => typeof c === "string"
    ? para(run(c, { size: o.size || 17, bold: o.bold, color: o.color })) : c) });

const cols = [1150, 3105, 3105, 3106];
const head = new TableRow({ tableHeader: true, children: [
  cell("Department", cols[0], { fill: NAVY, bold: true, color: "FFFFFF", size: 15 }),
  cell("Day 30 – Foundation", cols[1], { fill: NAVY, bold: true, color: "FFFFFF", size: 15 }),
  cell("Day 60 – Scale", cols[2], { fill: NAVY, bold: true, color: "FFFFFF", size: 15 }),
  cell("Day 90 – Embed & prove", cols[3], { fill: NAVY, bold: true, color: "FFFFFF", size: 15 })] });

const rows = [
 ["Company-wide",
  "AI-use policy signed (PII, approved tools, human sign-off). Time audit of top-3 repetitive tasks per dept → ranked backlog of ≥27 use cases. A named champion in all 9 depts. Data assistant live (read-only) for MIS + Payments: ≥90% correct on a 30-question test set, 0 PII leaks.",
  "9/9 depts have ≥1 live use case. Data assistant open to all managers in Telegram. ≥60% of licensed staff active weekly. Weekly scorecard running; all champions trained.",
  "≥2 live use cases per dept (≥18). ≥75% weekly active. ≥150 hrs/month saved vs the Day-30 baseline. ≥90% of staff completed the 90-min training. 6-month roadmap with cost and ROI signed off."],
 ["MIS", "Inventory the ~20 recurring reports; plain-English data Q&A live for the MIS team.",
  "5 daily/weekly reports auto-sent to Telegram; test-set accuracy ≥90%.",
  "≥50% of ad-hoc data requests self-served by managers; typical turnaround from hours to <5 min."],
 ["Payments", "Daily PSP approval-rate alert live (flags a drop of >5 pp vs the previous day).",
  "Weekly failed-deposit analysis by PSP and method; AI-drafted PSP escalation emails.",
  "Approval rate +1 pp on the weakest PSP, or a documented routing change; reconciliation time −40%."],
 ["Risk", "Shared-IP and deposit-then-withdraw rules run daily; analyst reviews the list.",
  "AI-drafted case summaries; false-positive rate tracked for every rule.",
  "Review time per case −40%; each rule has a measured precision; no automatic action on players."],
 ["Customer support", "Top-50 FAQ intents cleaned into one knowledge base.",
  "AI-drafted replies for the top 10 intents; the agent approves every reply.",
  "First-response time −30%; ≥50% of tickets use a draft; CSAT at or above baseline."],
 ["Finance", "Month-end close mapped; manual reconciliations listed.",
  "Hold / GGR / PSP reconciliation pack built automatically from the warehouse.",
  "Monthly reporting effort −30%; variance commentary drafted by AI, signed off by Finance."],
 ["Marketing", "Brand and compliance prompt templates (responsible-gaming rules built in).",
  "AI copy variants for ≥3 campaigns, A/B tested; segment briefs from the data assistant.",
  "Content turnaround −40%; ≥1 test beats control on CTR or conversion."],
 ["Sales", "Standard templates for proposals and follow-ups (partners / affiliates).",
  "Meeting-note summaries and follow-up drafts used by the whole team.",
  "Proposal prep time −30%; follow-up sent within 24h in ≥90% of cases."],
 ["HR", "JD and interview-question templates; policy Q&A on approved documents.",
  "CV shortlist support (a human decides); onboarding FAQ assistant.",
  "Time-to-shortlist −40%; ≥50% of routine HR queries self-served."],
 ["Product", "Feedback, tickets and app reviews collected in one place.",
  "Weekly insight digest (support + data); AI-assisted spec drafts.",
  "≥1 shipped improvement traced to an AI insight; spec drafting time −30%."],
].map((r, i) => new TableRow({ cantSplit: true, children: r.map((t, j) =>
  cell(t, cols[j], { bold: j === 0, fill: i === 0 ? LIGHT : (j === 0 ? GREY : undefined), size: 17 })) }));

const table = new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: cols, rows: [head, ...rows] });

const half = [W / 2 - 60, W / 2 + 60];
const box = (title, items, w) => cell([para(run(title, { size: 18, bold: true, color: NAVY }), { after: 30 }), ...items], w,
  { borders, fill: undefined });
const two = new Table({ width: { size: W, type: WidthType.DXA }, columnWidths: half, rows: [new TableRow({ children: [
  box("How I measure adoption (weekly one-page scorecard, every Monday)", [
    bullet([{ t: "Reach: " }, "weekly active users ÷ licences, per department (admin usage dashboard). Target 60% by Day 60, 75% by Day 90."]),
    bullet([{ t: "Depth: " }, "live use cases per dept (9 → 18). A use case is “live” only after 3 consecutive weeks of real use; stop any with no use after 4 weeks."]),
    bullet([{ t: "Value: " }, "hours saved = (baseline minutes − current minutes) × runs, checked by time sampling and a 3-question monthly pulse; cost per hour saved."]),
    bullet([{ t: "Quality & safety: " }, "accuracy on the test set ≥90%, PII leaks = 0, human-override rate, incidents; logged queries reviewed weekly."]),
  ], half[0]),
  box("Approach and guardrails (no in-house tech team)", [
    bullet([{ t: "Buy and configure, don’t build: " }, "a managed AI workspace plus the data assistant; one part-time contract engineer (~2 days/week) for connectors and monitoring."]),
    bullet([{ t: "Order: " }, "MIS → Payments → Risk → Finance first (data-ready, most repeat work), then the other five."]),
    bullet([{ t: "Safe by design: " }, "read-only data access, personal data excluded at the data layer, every query logged, a human approves anything touching players or money."]),
    bullet([{ t: "Capacity: " }, "champions give ~2 hrs/week; a monthly budget cap is agreed on Day 1 and reported monthly."]),
  ], half[1])] })] });

const doc = new Document({
  numbering: { config: [{ reference: "b", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•",
    alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 200, hanging: 140 } } } }] }] },
  sections: [{ properties: { page: { size: { width: 11906, height: 16838 },
      margin: { top: 600, bottom: 500, left: 720, right: 720 } } },
    children: [
      new Paragraph({ spacing: { after: 20 }, children: [run("90-Day Plan: AI Transformation across 9 Departments", { size: 30, bold: true, color: NAVY })] }),
      para(run("Goal: AI in the daily work of HR, customer support, payments, risk, marketing, MIS, sales, finance and product – measured, safe and low-cost. " +
        "Percentage targets are measured against baselines taken in the first 30 days; figures shown are starting targets to be confirmed with each department head.", { size: 15, color: "444444" }), { after: 70 }),
      table,
      para(run(""), { after: 50 }),
      two,
    ] }] });

Packer.toBuffer(doc).then(b => { fs.writeFileSync("90_day_plan.docx", b); console.log("ok"); });
