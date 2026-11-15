// Text from the server is always inserted with textContent (never innerHTML),
// so an answer, SQL or log entry cannot inject HTML into the page.
const $ = (id) => document.getElementById(id);
const app = $("app"), main = $("main"), scroll = $("scroll"), thread = $("thread");
const form = $("composer"), input = $("question"), sendBtn = $("send");
let busy = false;

// One random id per chat. The server keeps the last few questions + SQL under this id, so
// follow-ups ("and last week?") work. "New chat" makes a new id, i.e. a clean memory.
function newConversationId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}
let conversationId = newConversationId();

// ---------------------------------------------------------------- helpers
function greet() {
  const h = new Date().getHours();
  $("greeting").textContent = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

function scrollDown() { scroll.scrollTo({ top: scroll.scrollHeight, behavior: "smooth" }); }

function setEmpty(empty) { main.classList.toggle("is-empty", empty); }

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
  sendBtn.disabled = busy || !input.value.trim();
}

/** Render plain text: lines starting with "- " become a list, other lines paragraphs. */
function renderText(el, text) {
  el.replaceChildren();
  let ul = null;
  for (const raw of String(text).split("\n")) {
    const line = raw.trim().replace(/\*\*/g, "");
    if (/^[-*•]\s+/.test(line)) {
      if (!ul) { ul = document.createElement("ul"); el.append(ul); }
      const li = document.createElement("li");
      li.textContent = line.replace(/^[-*•]\s+/, "");
      ul.append(li);
    } else {
      ul = null;
      if (line) { const p = document.createElement("p"); p.textContent = line; el.append(p); }
    }
  }
}

const NUM = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
/** AI cost of one question, e.g. "$0.0021" (tiny amounts shown as "<$0.0001"). */
const fmtUsd = (x) => (x > 0 && x < 0.0001 ? "<$0.0001" : "$" + x.toFixed(4));
const isId = (col) => /(^|_)(id|ip_address|ip)$/i.test(col);   // keep IDs/IPs exactly as stored

function cell(v, col) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number" && !isId(col)) return NUM.format(v);
  return String(v);
}

/** Show the SQL result rows exactly as returned (the AI never re-types them). */
function renderTable(box, columns, rows, truncated) {
  box.hidden = false;
  box.querySelector(".result-count").textContent =
    `${rows.length.toLocaleString()} row${rows.length === 1 ? "" : "s"}` +
    (truncated ? ` (first ${rows.length}, capped for safety)` : "");
  const headRow = document.createElement("tr");
  for (const c of columns) {
    const th = document.createElement("th");
    th.textContent = c;
    headRow.append(th);
  }
  box.querySelector("thead").replaceChildren(headRow);
  const body = box.querySelector("tbody");
  body.replaceChildren();
  for (const r of rows) {
    const tr = document.createElement("tr");
    r.forEach((v, i) => {
      const td = document.createElement("td");
      td.textContent = cell(v, columns[i]);
      if (typeof v === "number") td.className = "num";
      tr.append(td);
    });
    body.append(tr);
  }
  const csvEsc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [columns, ...rows].map((r) => r.map(csvEsc).join(",")).join("\n");
  const btn = box.querySelector(".csv");
  btn.dataset.label = "Copy as CSV";
  btn.addEventListener("click", () => copyText(btn, csv));
}

async function copyText(btn, text) {
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Copied";
  } catch (e) {
    btn.textContent = "Copy failed";
  }
  setTimeout(() => (btn.textContent = btn.dataset.label || "Copy"), 1500);
}

// ---------------------------------------------------------------- turns
function addUser(text) {
  const node = $("tpl-user").content.firstElementChild.cloneNode(true);
  node.querySelector(".bubble").textContent = text;
  thread.append(node);
}

function addBot(thinkingText) {
  const node = $("tpl-bot").content.firstElementChild.cloneNode(true);
  if (thinkingText) node.querySelector(".thinking em").textContent = thinkingText;
  thread.append(node);
  scrollDown();

  const q = (s) => node.querySelector(s);
  return {
    finish({ text, notice, noticeKind, sql, meta, summary, copyLabel, columns, rows, truncated }) {
      q(".thinking").remove();
      const content = q(".content");
      if (notice) {                           // created only when needed: never an empty label
        const n = document.createElement("div");
        n.className = "notice" + (noticeKind ? " " + noticeKind : "");
        n.textContent = notice;
        content.before(n);
      }
      if (rows && rows.length > 1) renderTable(q(".result"), columns, rows, truncated);
      if (summary) {
        const pre = document.createElement("pre");
        pre.className = "summary";
        pre.textContent = text;
        content.append(pre);
      } else {
        renderText(content, text);
      }
      const actions = q(".actions");
      actions.hidden = false;
      const copy = q(".copy");
      copy.dataset.label = copyLabel || "Copy";
      copy.textContent = copy.dataset.label;
      copy.addEventListener("click", () => copyText(copy, text));
      if (sql) {
        const toggle = q(".sql-toggle"), panel = q(".sql-panel");
        q(".sql-panel code").textContent = sql;
        toggle.hidden = false;
        toggle.addEventListener("click", () => {
          panel.hidden = !panel.hidden;
          toggle.textContent = panel.hidden ? "Show SQL" : "Hide SQL";
        });
      }
      q(".act-meta").textContent = meta || "";
      scrollDown();
    },
  };
}

// ---------------------------------------------------------------- actions
async function ask(question) {
  question = question.trim();
  if (!question || busy) return;
  busy = true;
  setEmpty(false);
  closeSidebar();
  addUser(question);
  input.value = "";
  autosize();
  const bot = addBot();
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, conversation_id: conversationId }),
    });
    const data = await res.json();
    if (!res.ok) {
      bot.finish({ text: data.error || "Request failed.", notice: "Error" });
    } else {
      const u = data.usage || {};
      const cost = typeof u.cost_usd === "number" ? ` · ${fmtUsd(u.cost_usd)}` : "";
      const secs = (data.elapsed_ms / 1000).toFixed(1) + " s" + cost;
      let notice = null, noticeKind = null;
      if (!data.ok) {
        if (data.text.startsWith("That question can't be answered safely")) notice = "Blocked by the safety layer";
        else if (data.text.startsWith("I can't help")) notice = "Refused";
        else notice = "Not answered";
      } else if (data.unverified && data.unverified.length) {
        notice = "Some figures could not be verified";
      } else if (data.context_turns > 0) {
        notice = "Follow-up · uses this chat's earlier questions";
        noticeKind = "info";
      }
      bot.finish({ text: data.text, sql: data.sql, notice, noticeKind, meta: secs,
                   columns: data.columns, rows: data.rows, truncated: data.truncated });
    }
  } catch (e) {
    bot.finish({ text: "Could not reach the server. Is it still running?", notice: "Connection error" });
  } finally {
    busy = false;
    autosize();
    input.focus();
    loadRecent();
  }
}

async function showSummary() {
  if (busy) return;
  setEmpty(false);
  closeSidebar();
  addUser("Show the daily summary");
  const bot = addBot("Building the summary for 31 Oct vs 30 Oct");
  try {
    const data = await (await fetch("/api/summary")).json();
    bot.finish({ text: data.text, summary: true, notice: "Telegram message · no AI involved", noticeKind: "ok",
                 copyLabel: "Copy for Telegram" });
  } catch (e) {
    bot.finish({ text: "Could not load the daily summary.", notice: "Error" });
  }
}

function newChat() {
  if (busy) return;
  thread.replaceChildren();
  conversationId = newConversationId();      // fresh memory on the server
  setEmpty(true);
  closeSidebar();
  input.value = "";
  autosize();
  input.focus();
}

async function loadRecent() {
  const list = $("recent");
  try {
    const { entries } = await (await fetch("/api/log?n=12")).json();
    list.replaceChildren();
    if (!entries.length) {
      const li = document.createElement("li");
      li.className = "empty-note";
      li.textContent = "Nothing yet";
      list.append(li);
      return;
    }
    for (const e of entries) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      const when = e.ts ? new Date(e.ts).toLocaleString() : "";
      b.title = `${e.question}\n${when} · ${e.user} · ${e.elapsed_ms} ms · ` +
                (e.ok ? `${e.rows} row(s)` : (e.error || "not answered")) +
                (typeof e.cost_usd === "number" ? ` · ${fmtUsd(e.cost_usd)} (${e.model})` : "");
      const dot = document.createElement("span");
      dot.className = "dot" + (e.ok ? "" : " bad");
      const q = document.createElement("span");
      q.className = "q";
      q.textContent = e.question;
      b.append(dot, q);
      b.addEventListener("click", () => { input.value = e.question; autosize(); input.focus(); closeSidebar(); });
      li.append(b);
      list.append(li);
    }
  } catch (e) { /* sidebar list is optional */ }
}

async function loadInfo() {
  try {
    const info = await (await fetch("/api/info")).json();
    // An old server process (started before the code changed) still serves these new page files,
    // but runs old logic, e.g. no memory. Say so instead of failing silently.
    if (info.stale !== false) {
      $("stale").hidden = false;
      $("stale-text").textContent = info.stale === undefined
        ? "This server is running an older version of the assistant (no conversation memory)."
        : "The assistant's code changed after this server started, so it is still running the old version.";
    }
    $("limits").textContent = `${info.timeout_s} s timeout · ${info.max_rows} rows`;
    $("period").textContent = `Data 1–31 Oct 2026 · “yesterday” = 31 Oct`;
  } catch (e) { /* keep defaults */ }
}

// ---------------------------------------------------------------- sidebar (mobile)
function openSidebar() { app.classList.add("side-open"); $("scrim").hidden = false; }
function closeSidebar() { app.classList.remove("side-open"); $("scrim").hidden = true; }

// ---------------------------------------------------------------- wiring
form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
});
input.addEventListener("input", autosize);
$("suggestions").addEventListener("click", (e) => {
  const s = e.target.closest(".suggest");
  if (s) ask(s.dataset.q);
});
$("new-chat").addEventListener("click", newChat);
$("summary-btn").addEventListener("click", showSummary);
$("log-refresh").addEventListener("click", loadRecent);
$("open-side").addEventListener("click", openSidebar);
$("close-side").addEventListener("click", closeSidebar);
$("scrim").addEventListener("click", closeSidebar);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSidebar(); });

greet();
loadInfo();
loadRecent();
autosize();
input.focus();
