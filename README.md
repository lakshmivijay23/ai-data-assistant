# AI Weekend Assignment – Plain-English Data Assistant

Ask business questions in English; an AI model (Gemini or Claude) writes SQL; DuckDB runs it safely; you get the answer in plain language with the time period.

| Task | Where |
|---|---|
| 1. Data assistant (web page, CLI, optional Telegram bot) | `assistant/` |
| 2. Daily summary (31 Oct vs 30 Oct) | `daily_summary.py` → `outputs/daily_summary_2026-10-31.txt` |
| 3. Risk flags | `risk_flags.py` → `outputs/*.csv`, logic in `outputs/risk_flags_logic.md` |
| 4. 90-day plan | `docs/90_day_plan.docx` (and `.pdf`) |
| Demo script, all assumptions, next improvements | `docs/DEMO_GUIDE.md` |
| One-page flow diagram (how a question is answered) | `docs/flow_diagram.pdf` |

## Set-up (about 5 minutes)

Needs Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # put ONE key in .env: GEMINI_API_KEY or ANTHROPIC_API_KEY (the assistant uses whichever is set)

# put the five CSVs from 02_Data into ./data/  (players, deposits, withdrawals, bet_sessions, logins)
python -m assistant.build_db          # loads the CSVs into warehouse.duckdb (≈2 seconds)
```

## Run

```bash
python -m assistant.web                                                    # web page: open http://127.0.0.1:8000
python -m assistant.cli "What was the approval rate by PSP yesterday?"   # one question
python -m assistant.cli                                                    # interactive
python daily_summary.py                                                    # Task 2
python risk_flags.py                                                       # Task 3
pytest -q                                                                  # 75 tests, no API key needed
python eval_gold.py                                                        # 12 questions vs gold answers (uses your key)
python -m assistant.cost_report                                            # AI cost so far, from the audit log
python -m assistant.telegram_bot                                           # optional; needs TELEGRAM_BOT_TOKEN
```
The `.env` file in the project folder is read automatically (no need to export anything).

Settings (all optional, see `.env.example`): `LLM_PROVIDER` (auto/gemini/anthropic), `GEMINI_MODEL` (default `gemini-2.5-flash`), `CLAUDE_MODEL`, `ASSISTANT_TODAY`, `QUERY_TIMEOUT_SECONDS`, `MAX_ROWS`.

## Web page

`python -m assistant.web`, then open http://127.0.0.1:8000. It has a chat box, one-click example questions (including a request for a phone number and a `DROP TABLE` attempt, to show the safety layer), the daily summary, the SQL behind each answer, and a live view of the audit log. When a result has more than one row, the page (and the CLI) shows the rows **straight from SQL as a table**, and the AI writes only a short summary above it. The AI never re-types long lists, so they cannot be cut off or mistyped. It uses only the Python standard library and calls the same `answer_question()` as the CLI and Telegram bot, so the same guard, limits and logging apply. It listens on 127.0.0.1 only (set `WEB_HOST` / `WEB_PORT` to change), serves only its three static files, caps questions at 500 characters, and sends a strict Content-Security-Policy. All text is inserted with `textContent`, so an answer cannot inject HTML. There is no login, so it is meant for a local demo; production would put it behind company SSO.

## Conversation memory

Follow-ups work: "What was the approval rate by PSP yesterday?" → "and for last week?" → "only PayNova". Each chat (browser tab, Telegram chat, CLI session) remembers its **last 3 questions and the SQL written for them**, never the result rows, so no player data goes back to the AI. Only answered questions are remembered; refused or blocked ones are not. The prompt tells the model to use the context only for real follow-ups and to ignore it for complete new questions. Memory is kept on the server (`assistant/memory.py`), expires after 2 idle hours, and is cleared by **New chat** / `/new` / typing `new` in the CLI. The audit log records how much context each question used.

## Number check (no invented figures)

The numbers in the result come from SQL, but the sentence around them is written by the AI, and that is where a wrong figure could slip in. `assistant/grounding.py` extracts every number from the answer and checks it against the result: the raw values, the same values as percentages or in thousands/lakh/million/crore, the row count, and numbers in the question or SQL. If a figure cannot be matched, the AI is asked once to rewrite using only result numbers. If it still cannot be matched, the answer carries a visible note and the figure is logged as `unverified_numbers`. Long lists are never re-typed by the AI: the page and CLI show the rows directly as a table.

## How it works

```
question ─► AI model (schema + business definitions + date rules) ─► SQL
        ─► guard.py: validate ─► read-only DuckDB, timeout, row cap, scrub
        ─► AI model (question + SQL + rows) ─► plain-English answer with the period
        every step ─► logs/queries.jsonl
```
Files: `prompts.py` (definitions and date conventions: the thing to read if an answer looks off), `guard.py` (all safety), `llm.py` (orchestration, one retry if the SQL errors), `build_db.py`, `audit_log.py`, `cli.py`, `telegram_bot.py`, `web.py` + `static/` (web page).

## Safety (the four requirements)

1. **Personal data (enforced in the data layer, with a view).** One database, PII stored once. The full player record, including `phone` and `email`, **is stored** in `restricted.players`. The assistant queries `players`, a **view without those columns**, so even `SELECT * FROM players` cannot return them. A view only hides columns from queries that go *through* it. DuckDB has no users or GRANTs, so nothing inside DuckDB stops a query naming `restricted.players` directly. `guard.py` fills that role: every query is parsed with DuckDB's own parser (`json_serialize_sql`) and may reference only the allowlisted tables and views. `restricted.*`, `information_schema`, `query_table(...)` and files are refused before execution, and anything that cannot be parsed is refused too. In production on ClickHouse the same view gets `GRANT SELECT` for a read-only assistant user (and no grant on the base table), so the database enforces it and the allowlist becomes a second line of defence. Extra layers: SQL mentioning phone/email/mobile is rejected; the connection is read-only with file access off and settings locked; the prompt tells the model to refuse; and a scrubber masks anything shaped like a phone number or email, in query results **and in the user's own question** before it reaches the AI, the log or conversation memory. Tests: `tests/test_assistant.py` (bypass attempts through subqueries, CTEs, joins, `query_table`, catalog and file access; a test documenting that a raw connection *can* read the base table, so the allowlist is the gate; a "jailbroken model" test where the fake model writes `SELECT phone ...`; and PII typed into a question).
2. **Read-only.** Exactly one statement, and DuckDB's own parser must classify it as a `SELECT` (so no DELETE/DROP/INSERT/CREATE/COPY/ATTACH/PRAGMA/SET). The database file is opened `read_only=True`, `enable_external_access=false` (no `read_csv` of other files) and `lock_configuration=true` (a query cannot switch these back on).
3. **Limits.** 10 s timeout (the query is interrupted) and a 500-row cap, both configurable. The answer tells the user when a list was truncated.
4. **Logging.** One JSON line per question in `logs/queries.jsonl`: time, user, conversation, question, SQL, success, rows, milliseconds, error, plus **model, number of AI calls, input/output/thinking tokens and the cost in USD** (`cost_usd`). `python -m assistant.cost_report` totals it by model, user and day. Prices live in `assistant/pricing.py` (Gemini 2.5 Flash $0.30 / $2.50 per 1M tokens, thinking billed as output; Claude Sonnet 5.5 $2 / $10). Override them with `LLM_PRICE_INPUT_PER_MTOK` / `LLM_PRICE_OUTPUT_PER_MTOK`. Measured: about $0.002 per question on Gemini 2.5 Flash. Refusals and blocked queries are logged too.

## Assumptions (please read)

* **"Today" is 1 Nov 2026** (the brief says treat 31 Oct as yesterday). So: *yesterday* = 31 Oct; *last week* = Mon 19 – Sun 25 Oct; *last 7 days* = 25–31 Oct; *this month* = October (month to date, 1–31 Oct). The assistant always states the period it used, and the rules are in `prompts.py`.
* Approval rate is by count; Hold uses deposit date and withdrawal **request** date; "withdrawals approved" is dated by request date too (consistent with Hold, and with the brief).
* FTD counts `players.first_deposit_time` in the period (not derived from deposits). Sessions are dated by `start_time`.
* The warehouse is rebuilt from the CSVs; I assume the CSVs are final (no duplicates or orphans were found: checked).
* Rule A/B choices are explained in `outputs/risk_flags_logic.md`.

## Design trade-offs

* **One database + view + allowlist** rather than a separate PII-free copy. It keeps one source of truth with no rebuilds, matches the brief's own example and the production design (a view plus `GRANT`), and scales. The cost: in DuckDB the barrier around the base table is the allowlist (Python, failing closed) rather than the engine, because DuckDB has no permissions. I also built and rejected a two-file version: it is enforced by the engine, but it is a copy that must be rebuilt and does not reflect production. (SQLite's authorizer hook could block columns inside the engine, but porting all SQL from DuckDB was not worth the risk.) Trade-off: the assistant cannot answer "how many players have a gmail address". Next step in production: encrypt or tokenise phone/email at column level, with the key held outside the warehouse and never given to the assistant.
* **Two model calls** (SQL, then wording) cost a little more than one but keep the numbers coming from SQL, not from the model's memory. The model only sees the result rows, never the raw tables.
* **Daily summary has no AI**: deterministic SQL and a template, so it cannot hallucinate. "Biggest changes" rule: the largest PSP approval-rate move (≥30 attempts on both days) plus the two largest volume/money moves from different families.
* **Memory holds questions + SQL, never result rows**: follow-ups work, no player data is replayed to the AI, and the cost stays small. Kept server-side per chat so a client cannot plant fake context. Lost on restart (fine for a demo; production would use Redis with the same TTL).
* **Not built (time-box):** charts, auth/rate-limiting per Telegram user, retries for API outages beyond the SDK's own.

## Production design on ClickHouse (not built; how I would run it for real)

The copied mart is a stand-in for permissions that DuckDB lacks. In production I would **not** copy data: a live platform changes every second, a copy goes stale (assistant numbers stop matching other dashboards), full rebuilds get slower as data grows, and every copy is one more place personal data lives. ClickHouse can enforce all four safety rules **inside the database, on live data**:

```sql
-- 1. Limits and read-only, enforced by the engine. readonly = 1 also stops the client
--    changing these settings (the equivalent of DuckDB's lock_configuration).
CREATE SETTINGS PROFILE ai_assistant_profile SETTINGS
    readonly             = 1,          -- SELECT only: no INSERT/ALTER/DROP, no SET
    max_execution_time   = 10,         -- seconds
    max_result_rows      = 500,
    result_overflow_mode = 'break',    -- return the first 500 rows instead of failing
    max_memory_usage     = 1000000000; -- 1 GB per query

-- 2. A dedicated user for the assistant (password from a secret manager, app servers only)
CREATE USER ai_assistant
    IDENTIFIED WITH sha256_password BY '<from secret manager>'
    HOST IP '10.0.5.0/24'
    SETTINGS PROFILE 'ai_assistant_profile';

-- 3. Personal data: column-level grant. phone/email are simply not granted, so
--    `SELECT phone ...` and even `SELECT * FROM players` fail with ACCESS_DENIED.
GRANT SELECT(player_id, registration_time, first_deposit_time, state, vip_level)
      ON dwh.players TO ai_assistant;
GRANT SELECT ON dwh.deposits     TO ai_assistant;
GRANT SELECT ON dwh.withdrawals  TO ai_assistant;
GRANT SELECT ON dwh.bet_sessions TO ai_assistant;
GRANT SELECT ON dwh.logins       TO ai_assistant;
-- No other grants: in particular no FILE/URL/REMOTE/S3 sources and no CREATE TEMPORARY
-- TABLE, so table functions cannot reach files, other servers or the internet.

-- 4. Cost guard-rail per hour
CREATE QUOTA ai_assistant_quota
    FOR INTERVAL 1 hour MAX queries = 500, execution_time = 600
    TO ai_assistant;
```

* **No rebuilds:** grants apply to the live tables, so answers are as fresh as the warehouse.
* **Views for business definitions** (e.g. `approved_deposits`, a `players_safe` view) can be added in a separate `ai_mart` database. They must be created with `SQL SECURITY DEFINER`, otherwise ClickHouse checks the caller's rights on the underlying tables.
* **System tables:** turn on `select_from_system_db_requires_grant` (recent versions) so the assistant user cannot browse `system.*`.
* **Logging in the database too:** `system.query_log WHERE user = 'ai_assistant'` records every query, time taken and rows read. It is an audit trail the application cannot skip, alongside `logs/queries.jsonl`.
* **Extra layer for PII:** encrypt or tokenise phone/email at column level (e.g. AES-256-GCM, or a keyed hash so the risk team can still find accounts sharing a phone). The key lives in a key-management service and is never given to the assistant. Even a wrong grant then exposes nothing readable.
* **The Python guard stays** as defence in depth (allowlist, PII keyword block, scrubber), but the database is the control that matters.

Check the exact syntax against the ClickHouse version in use. `SQL SECURITY` and system-table grants need 24.x or later.

## Known limits

Production gaps I know about (deliberately not built in the time-box):
* **No login on the web page.** It listens on 127.0.0.1 only, rejects other Host headers (DNS rebinding) and non-JSON posts (cross-site requests). Real use needs company SSO in front of it.
* **Telegram access** is open unless `TELEGRAM_ALLOWED_USER_IDS` is set; the bot warns at start-up. No per-user rate limit yet, so one user could run up AI cost.
* **Memory and the audit log are local to one process/file**: memory is lost on restart and the log is not rotated. Production: Redis for memory, log shipping with rotation.
* **"Today" is fixed** at 1 Nov 2026 to match the dataset (`ASSISTANT_TODAY`). Live data would use the real date in Dubai time.
* **The AI sees result values** (up to 100 rows) to word the answer. Free-text columns in real data could carry prompt-injection text; this synthetic data has none.
* A query returning one very large text value is still fetched into memory before being cut to 1,000 characters.

* The model can still misread an ambiguous question and write SQL that runs but answers a slightly different question. This is the main remaining risk. The SQL is shown under every answer so it can be checked, and `eval_gold.py` is how I measured it (12/12).
* The number check cannot catch an invented whole number between 1 and 31 (those are allowed so the answer can state dates).
* Follow-up detection is up to the model: a complete question is meant to ignore earlier context, but a vague one may inherit it. "New chat" (web) or `/new` (Telegram) starts clean.
* `list`-type questions that return more than 500 rows are cut off by design.

