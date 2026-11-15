"""Run:  pytest -q      (needs the warehouse: python -m assistant.build_db)"""
import json

import duckdb
import pandas as pd
import pytest

from assistant import audit_log, guard, llm
from assistant.config import DATA_DIR, DB_PATH
from assistant.guard import UnsafeQuery, connect_readonly, run_query, validate_sql

# ---------------------------------------------------------------- safety
@pytest.mark.parametrize("sql", [
    "DELETE FROM deposits",
    "DROP TABLE players",
    "UPDATE deposits SET status='approved'",
    "INSERT INTO deposits SELECT * FROM deposits",
    "CREATE TABLE x AS SELECT 1",
    "SELECT 1; DROP TABLE deposits",
    "ATTACH 'other.db'",
    "PRAGMA database_list",
    "COPY deposits TO '/tmp/x.csv'",
    "SELECT phone FROM players",
    "select EMAIL from players",
    "SELECT * FROM players WHERE phone LIKE '+91%'",
    "",
])
def test_validator_rejects(sql):
    with pytest.raises((UnsafeQuery, guard.BadSql)):     # "" is BadSql (retryable), the rest UnsafeQuery
        validate_sql(sql)


def test_validator_accepts_select_and_cte():
    assert validate_sql("SELECT 1;") == "SELECT 1"
    validate_sql("WITH a AS (SELECT 1 x) SELECT * FROM a")


def test_pii_stored_once_but_view_hides_it():
    con = connect_readonly(DB_PATH)
    # The data IS kept: restricted.players has phone/email for every player
    assert con.execute("SELECT count(phone), count(email) FROM restricted.players").fetchone() == (5000, 5000)
    # ...but no object the assistant may query has such a column
    exposed = con.execute("SELECT count(*) FROM duckdb_columns() WHERE schema_name = 'main' "
                          "AND lower(column_name) IN ('phone','email')").fetchone()[0]
    assert exposed == 0
    con.close()
    r = run_query("SELECT * FROM players LIMIT 1")             # even SELECT * cannot return them
    assert not {"phone", "email"} & set(r.columns)


def test_base_table_is_reachable_without_the_guard_so_the_allowlist_is_the_gate():
    """Documents the DuckDB limitation honestly: no GRANTs, so a raw connection CAN read the base
    table. Only run_query() (allowlist) stands between the AI and it. On ClickHouse: a GRANT."""
    con = connect_readonly(DB_PATH)
    assert con.execute("SELECT count(phone) FROM restricted.players").fetchone()[0] == 5000
    con.close()
    with pytest.raises(UnsafeQuery):
        run_query("SELECT count(phone) FROM restricted.players")
    with pytest.raises(UnsafeQuery):
        run_query("SELECT player_id FROM restricted.players")    # even without a PII word in it


@pytest.mark.parametrize("sql", [
    "ATTACH 'other.duckdb' AS other",
    f"SELECT * FROM read_csv('{(DATA_DIR / 'players.csv').as_posix()}')",
    f"SELECT * FROM '{(DATA_DIR / 'players.csv').as_posix()}'",
    "SET enable_external_access = true",
    "SELECT phone FROM players",
    "DELETE FROM deposits",
])
def test_engine_blocks_files_writes_and_settings_without_python_checks(sql):
    """Skips validate_sql entirely: the raw connection itself must refuse these."""
    con = connect_readonly(DB_PATH)
    with pytest.raises(duckdb.Error):
        con.execute(sql).fetchall()
    con.close()


@pytest.mark.parametrize("sql", [
    "SELECT * FROM restricted.players",
    'SELECT * FROM "restricted"."players"',
    "SELECT player_id FROM players WHERE player_id IN (SELECT player_id FROM restricted.players)",
    "WITH x AS (SELECT * FROM restricted.players) SELECT * FROM x",
    "SELECT * FROM players p JOIN restricted.players r USING (player_id)",
    "SELECT * FROM query_table('restricted.players')",
    "SELECT * FROM query('SELECT * FROM restricted.players')",
    "SELECT * FROM warehouse.restricted.players",
    "SELECT * FROM warehouse.main.players",
    "SELECT * FROM information_schema.columns",
    "SELECT * FROM duckdb_columns()",
    "SELECT * FROM read_csv('data/players.csv')",
    "SELECT * FROM 'data/players.csv'",
])
def test_restricted_data_unreachable(sql):
    with pytest.raises(UnsafeQuery):
        validate_sql(sql)


def test_allowlist_permits_normal_queries():
    for sql in ["SELECT * FROM players", "SELECT * FROM main.players",
                "WITH t AS (SELECT * FROM deposits) SELECT count(*) FROM t",
                "SELECT d.psp FROM approved_deposits d JOIN players p USING (player_id)",
                "SELECT * FROM range(3)"]:
        validate_sql(sql)


def test_file_access_blocked_even_for_valid_select():
    with pytest.raises(Exception):
        run_query(f"SELECT * FROM read_csv('{DATA_DIR / 'players.csv'}') LIMIT 1")


def test_cannot_unlock_settings_or_write():
    for sql in ["SELECT 1; SET enable_external_access=true", "SET enable_external_access=true"]:
        with pytest.raises(UnsafeQuery):
            run_query(sql)


def test_row_cap():
    r = run_query("SELECT * FROM deposits", max_rows=100)
    assert len(r.rows) == 100 and r.truncated


def test_timeout():
    with pytest.raises(UnsafeQuery, match="longer than"):
        run_query("SELECT count(*) FROM range(3000000000) a, range(3000000000) b", timeout_s=1)


def test_scrubber_masks_pii_shaped_text():
    r = run_query("SELECT 'call +918185963407 or user1@example.com' AS note")
    assert "8185963407" not in r.rows[0][0] and "@" not in r.rows[0][0]

# ------------------------------------------- correctness vs independent pandas
@pytest.fixture(scope="module")
def df():
    rd = lambda n, **k: pd.read_csv(DATA_DIR / n, **k)
    return dict(
        players=rd("players.csv", parse_dates=["first_deposit_time"]),
        dep=rd("deposits.csv", parse_dates=["created_at"]),
        wd=rd("withdrawals.csv", parse_dates=["requested_at"]),
        bet=rd("bet_sessions.csv", parse_dates=["start_time", "end_time"]),
    )


def q(sql):
    return run_query(sql).rows


def test_approval_rate_by_psp_yesterday(df):
    d = df["dep"]; d = d[(d.created_at >= "2026-10-31") & (d.created_at < "2026-11-01")]
    exp = d.groupby("psp").apply(lambda g: (g.status == "approved").mean())
    got = dict(q("""SELECT psp, count(*) FILTER (status='approved')*1.0/count(*) FROM deposits
                    WHERE created_at >= '2026-10-31' AND created_at < '2026-11-01' GROUP BY psp"""))
    for psp, v in exp.items():
        assert got[psp] == pytest.approx(v)


def test_ftd_last_week(df):
    p = df["players"]; n = ((p.first_deposit_time >= "2026-10-19") & (p.first_deposit_time < "2026-10-26")).sum()
    assert q("SELECT count(*) FROM players WHERE first_deposit_time >= '2026-10-19' AND first_deposit_time < '2026-10-26'")[0][0] == n


def test_casino_margin_october(df):
    b = df["bet"]; c = b[b["product"] == "casino"]
    exp = (c.wager_inr.sum() - c.win_inr.sum()) / c.wager_inr.sum()
    got = q("SELECT sum(ggr_inr)/sum(wager_inr) FROM bet_sessions WHERE product='casino'")[0][0]
    assert got == pytest.approx(exp)


def test_hold_october(df):
    exp = df["dep"].query("status=='approved'").amount_inr.sum() - df["wd"].query("status=='approved'").amount_inr.sum()
    got = q("SELECT (SELECT sum(amount_inr) FROM approved_deposits) - (SELECT sum(amount_inr) FROM approved_withdrawals)")[0][0]
    assert got == pytest.approx(exp)


def test_daily_summary_numbers(df):
    from daily_summary import day_metrics
    con = connect_readonly(DB_PATH)
    from datetime import date
    m = day_metrics(con, date(2026, 10, 31))
    d = df["dep"]; d = d[d.created_at.dt.date == date(2026, 10, 31)]
    w = df["wd"]; w = w[w.requested_at.dt.date == date(2026, 10, 31)]
    assert m["attempts"] == len(d)
    assert m["approved_amt"] == d[d.status == "approved"].amount_inr.sum()
    assert m["approval_rate"] == pytest.approx((d.status == "approved").mean())
    assert m["wd_req_n"] == len(w)
    assert m["wd_appr_amt"] == w[w.status == "approved"].amount_inr.sum()
    assert m["hold"] == d[d.status == "approved"].amount_inr.sum() - w[w.status == "approved"].amount_inr.sum()


def test_rule_a_independent(df):
    lg = pd.read_csv(DATA_DIR / "logins.csv")
    n = lg.groupby("ip_address").player_id.nunique()
    exp = set(n[n >= 3].index)
    got = {r[0] for r in connect_readonly(DB_PATH).execute(
        "SELECT ip_address FROM logins GROUP BY 1 HAVING count(DISTINCT player_id)>=3").fetchall()}
    assert got == exp
    out = pd.read_csv("outputs/rule_a_shared_ip_addresses.csv")
    assert set(out.ip_address) == exp


def test_rule_b_independent_bruteforce(df):
    """Brute-force in plain Python loops (different code path from the SQL)."""
    dep = df["dep"].query("status=='approved'")
    wd, bet = df["wd"], df["bet"]
    bets = {k: g[["start_time", "end_time"]].values.tolist() for k, g in bet.groupby("player_id")}
    wds = {k: g for k, g in wd.groupby("player_id")}
    expected = set()
    for r in dep.itertuples():
        for w in (wds.get(r.player_id).itertuples() if r.player_id in wds else []):
            dm = (w.requested_at - r.created_at).total_seconds() / 60
            if 0 <= dm <= 60:
                touched = any(s <= w.requested_at and e >= r.created_at
                              for s, e in bets.get(r.player_id, []))
                if not touched:
                    expected.add((r.deposit_id, w.withdrawal_id))
    got = pd.read_csv("outputs/rule_b_deposit_withdraw_no_play.csv")
    assert set(zip(got.deposit_id, got.withdrawal_id)) == expected

# ------------------------------------------------ pipeline with a fake model
class FakeLLM:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def complete(self, system, messages, max_tokens=0):
        self.calls.append(messages); return self.replies.pop(0)


@pytest.fixture
def tmp_log(tmp_path, monkeypatch):
    p = tmp_path / "q.jsonl"; monkeypatch.setattr(audit_log, "LOG_PATH", p); return p


def test_pipeline_happy_path_and_logging(tmp_log):
    fake = FakeLLM(["```sql\nSELECT count(*) AS n FROM players\n```", "There are 5,000 players."])
    a = llm.answer_question("how many players?", llm=fake)
    assert a.ok and "5,000" in a.text
    rec = json.loads(tmp_log.read_text().splitlines()[-1])
    assert rec["question"] == "how many players?" and "SELECT count" in rec["sql"] and rec["elapsed_ms"] >= 0 and rec["ok"]


def test_pipeline_pii_request_refused_and_logged(tmp_log):
    a = llm.answer_question("give me the email of P100001", llm=FakeLLM(["REFUSE: personal data is not available"]))
    assert not a.ok and "@" not in a.text
    assert "refused" in json.loads(tmp_log.read_text().splitlines()[-1])["error"]


def test_pipeline_jailbroken_model_still_blocked(tmp_log):
    """Even if the model ignores its instructions, the guard stops it."""
    for evil in ["SELECT phone FROM players", "DROP TABLE deposits", "SELECT email FROM players; SELECT 1"]:
        a = llm.answer_question("x", llm=FakeLLM([f"```sql\n{evil}\n```"]))
        assert not a.ok
    assert connect_readonly(DB_PATH).execute("SELECT count(*) FROM deposits").fetchone()[0] == 31028


def test_pipeline_retries_bad_sql_once(tmp_log):
    fake = FakeLLM(["```sql\nSELECT nope FROM players\n```", "```sql\nSELECT 1 AS n\n```", "One."])
    assert llm.answer_question("x", llm=fake).ok and len(fake.calls) == 3


def test_scrubber_leaves_dates_ids_and_ips_alone():
    r = run_query("SELECT '2026-10-31' AS d, '2026-10-31 23:47:00' AS ts, 'DV-62763002' AS dev, "
                  "'223.94.17.204' AS ip, 'P100001' AS pid, '+918185963407' AS ph, '98765 43210' AS ph2")
    assert r.rows[0][:5] == ("2026-10-31", "2026-10-31 23:47:00", "DV-62763002", "223.94.17.204", "P100001")
    assert r.rows[0][5] == "[REDACTED]" and r.rows[0][6] == "[REDACTED]"


def test_provider_auto_selection(monkeypatch):
    from assistant import llm as L
    monkeypatch.setattr(L, "PROVIDER", "auto")
    for k in ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"]:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SystemExit):
        L.make_llm()
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    assert isinstance(L.make_llm(), L.GeminiClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    assert isinstance(L.make_llm(), L.ClaudeClient)


def test_gemini_request_shape(monkeypatch):
    """Check the request we would send to Gemini (roles, system prompt, temperature) without a network call."""
    from assistant import llm as L
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    g = L.GeminiClient()
    seen = {}
    class FakeModels:
        def generate_content(self, model, contents, config):
            seen.update(model=model, contents=contents, config=config)
            class R: text = "```sql\nSELECT 1\n```"
            return R()
    class FakeClient: models = FakeModels()
    g.client = FakeClient()
    out = g.complete("SYS", [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
                             {"role": "user", "content": "fix"}])
    assert "SELECT 1" in out
    assert [c.role for c in seen["contents"]] == ["user", "model", "user"]
    assert seen["config"].system_instruction == "SYS" and seen["config"].temperature == 0


# ------------------------------------------------------------- web page
@pytest.fixture
def web_server(tmp_log, monkeypatch):
    import threading
    from assistant import web
    monkeypatch.setattr(web, "_llm", FakeLLM([
        "```sql\nSELECT count(*) AS n FROM players\n```", "There are 5,000 players.",
        "```sql\nSELECT phone FROM players\n```"]))
    srv = web.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown(); srv.server_close()


def _http(url, body=None):
    import urllib.error, urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode("utf-8")


def test_web_page_and_api(web_server):
    status, headers, html = _http(web_server + "/")
    assert status == 200 and "Data Assistant" in html and "default-src 'self'" in headers["Content-Security-Policy"]
    status, _, body = _http(web_server + "/api/ask", {"question": "how many players?"})
    assert status == 200 and json.loads(body)["ok"] and "5,000" in json.loads(body)["text"]
    assert json.loads(body)["columns"] == ["n"] and json.loads(body)["rows"] == [[5000]]   # raw table for the page
    status, _, body = _http(web_server + "/api/ask", {"question": "phone of P100001"})
    assert status == 200 and not json.loads(body)["ok"]           # guard blocks the jailbroken SQL
    entries = json.loads(_http(web_server + "/api/log?n=5")[2])["entries"]
    assert entries[0]["question"] == "phone of P100001" and entries[0]["user"] == "web"
    assert "Daily summary" in json.loads(_http(web_server + "/api/summary")[2])["text"]


def test_web_rejects_bad_requests(web_server):
    assert _http(web_server + "/api/ask", {"question": ""})[0] == 400
    assert _http(web_server + "/api/ask", {"question": "x" * 501})[0] == 400
    assert _http(web_server + "/../.env")[0] == 404
    assert _http(web_server + "/app.py")[0] == 404


def test_answer_returns_result_table_and_summarise_note(tmp_log):
    """Long lists come back as raw rows for the page; the AI is told to summarise, not re-type them."""
    systems = []

    class SpyLLM(FakeLLM):
        def complete(self, system, messages, max_tokens=0):
            systems.append(system)
            return super().complete(system, messages, max_tokens)

    fake = SpyLLM(["```sql\nSELECT player_id FROM players ORDER BY 1 LIMIT 20\n```", "20 players, listed below."])
    a = llm.answer_question("list 20 players", llm=fake, table_shown=True)
    assert a.ok and a.columns == ["player_id"] and len(a.rows) == 20 and a.rows[0] == ("P100001",)
    assert "FULL RESULT TABLE" in systems[1]

    systems.clear()
    llm.answer_question("list 20 players",
                        llm=SpyLLM(["```sql\nSELECT player_id FROM players ORDER BY 1 LIMIT 20\n```", "ok"]))
    assert "FULL RESULT TABLE" not in systems[1]          # Telegram: no table, so no such note


# ------------------------------------------------------- conversation memory
def test_memory_keeps_last_turns_per_conversation_and_expires():
    from assistant.memory import ConversationMemory
    m = ConversationMemory(max_turns=3, max_conversations=2, ttl_s=3600)
    for i in range(5):
        m.add("a", f"q{i}", f"SELECT {i}")
    assert [q for q, _ in m.history("a")] == ["q2", "q3", "q4"]      # only the last 3
    assert m.history("b") == [] and m.history("") == []
    m.add("b", "x", "SELECT 1"); m.add("c", "y", "SELECT 1")          # 3rd conversation evicts the oldest
    assert m.history("a") == []
    m.clear("b"); assert m.history("b") == []
    old = ConversationMemory(ttl_s=0); old.add("z", "q", "SELECT 1")
    import time as _t; _t.sleep(0.01)
    assert old.history("z") == []                                     # idle conversations are forgotten
    m.add("d", "refused question", None)                              # no SQL -> nothing stored
    assert m.history("d") == []


def test_followup_sends_previous_question_and_sql_but_no_rows(tmp_log):
    fake = FakeLLM(["```sql\nSELECT count(*) AS n FROM players WHERE vip_level = 'Gold'\n```", "368 Gold players."])
    history = [("How many Gold players?", "SELECT count(*) AS n FROM players WHERE vip_level = 'Silver'")]
    a = llm.answer_question("and Gold?", llm=fake, history=history)
    assert a.ok
    sql_call = fake.calls[0]
    assert [m["role"] for m in sql_call] == ["user", "assistant", "user"]
    assert sql_call[0]["content"] == "How many Gold players?" and "vip_level = 'Silver'" in sql_call[1]["content"]
    assert sql_call[2]["content"] == "and Gold?"
    assert "1334" not in json.dumps(sql_call)                         # result rows are never replayed
    rec = json.loads(tmp_log.read_text().splitlines()[-1])
    assert rec["followup_context"] == 1


def test_web_memory_per_conversation(web_server, monkeypatch):
    from assistant import web
    monkeypatch.setattr(web, "_llm", FakeLLM([
        "```sql\nSELECT count(*) AS n FROM players\n```", "5,000 players.",
        "```sql\nSELECT count(*) AS n FROM deposits\n```", "31,028 deposits.",
        "```sql\nSELECT 1 AS n\n```", "Done."]))
    ask = lambda q, cid: json.loads(_http(web_server + "/api/ask", {"question": q, "conversation_id": cid})[2])
    assert ask("how many players?", "chat-aaaaaaaa")["context_turns"] == 0
    assert ask("and deposits?", "chat-aaaaaaaa")["context_turns"] == 1     # same chat remembers
    assert ask("hello", "chat-bbbbbbbb")["context_turns"] == 0             # another chat does not
    assert _http(web_server + "/api/ask", {"question": "x", "conversation_id": "bad id!"})[0] == 400


# ------------------------------------------------------- number check
def test_grounding_accepts_real_figures_in_any_format():
    from assistant.grounding import ungrounded_numbers
    from datetime import date
    cols, rows = ["psp", "rate", "amount", "day"], [("PayNova", 0.76327, 945590.0, date(2026, 10, 31))]
    text = "On 31 Oct 2026 PayNova approved 76.33% of attempts, worth ₹945,590 (about 9.46 lakh). 1 row."
    assert ungrounded_numbers(text, cols, rows, "approval rate yesterday", "SELECT ... LIMIT 5") == []


def test_grounding_flags_invented_figures():
    from assistant.grounding import ungrounded_numbers
    cols, rows = ["n"], [(5000,)]
    assert ungrounded_numbers("There are 5,000 players, up 12.5% on September (4,444).", cols, rows, "q", "SELECT 1") \
        == ["12.5", "4,444"]


def test_answer_with_invented_number_is_rewritten_then_flagged(tmp_log):
    # 1st answer invents 4,321 -> rewrite requested; rewrite is clean -> no warning
    fake = FakeLLM(["```sql\nSELECT count(*) AS n FROM players\n```", "There are 4,321 players.", "There are 5,000 players."])
    a = llm.answer_question("how many players?", llm=fake)
    assert a.ok and a.text == "There are 5,000 players." and a.unverified == []
    assert "4,321" in fake.calls[2][-1]["content"]                    # the rewrite request names the bad figure
    # model keeps inventing -> answer is kept but clearly flagged, and logged
    fake = FakeLLM(["```sql\nSELECT count(*) AS n FROM players\n```", "There are 4,321 players.", "Still 4,321 players."])
    a = llm.answer_question("how many players?", llm=fake)
    assert a.unverified == ["4,321"] and "could not match" in a.text
    assert json.loads(tmp_log.read_text().splitlines()[-1])["unverified_numbers"] == ["4,321"]


# ------------------------------------------------------- cost logging
class MeteredLLM(FakeLLM):
    """Fake model that reports token usage the way the real clients do."""
    def complete_with_usage(self, system, messages, max_tokens=0):
        return self.complete(system, messages, max_tokens), {
            "model": "gemini-2.5-flash", "input_tokens": 1000, "output_tokens": 300, "thinking_tokens": 200}


def test_cost_is_logged_per_question(tmp_log, monkeypatch):
    monkeypatch.delenv("LLM_PRICE_INPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)
    a = llm.answer_question("how many players?",
                            llm=MeteredLLM(["```sql\nSELECT count(*) AS n FROM players\n```", "5,000 players."]))
    rec = json.loads(tmp_log.read_text().splitlines()[-1])
    # 2 calls x (1000 in, 300 out) at $0.30 / $2.50 per 1M = 2 x (0.0003 + 0.00075) = $0.0021
    assert rec["llm_calls"] == 2 and rec["input_tokens"] == 2000 and rec["output_tokens"] == 600
    assert rec["thinking_tokens"] == 400 and rec["model"] == "gemini-2.5-flash"
    assert rec["cost_usd"] == pytest.approx(0.0021)
    assert a.usage["cost_usd"] == pytest.approx(0.0021)
    # refusals cost money too (1 call) and are logged with their cost
    llm.answer_question("phone of P1", llm=MeteredLLM(["REFUSE: personal data is not available"]))
    assert json.loads(tmp_log.read_text().splitlines()[-1])["cost_usd"] == pytest.approx(0.00105)


def test_pricing_overrides_and_unknown_models(monkeypatch):
    from assistant.pricing import Usage
    u = Usage(); u.add("some-new-model", 1_000_000, 1_000_000)
    monkeypatch.delenv("LLM_PRICE_INPUT_PER_MTOK", raising=False)
    monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)
    assert u.cost_usd is None                                         # unknown price -> no made-up cost
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "1"); monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "2")
    assert u.cost_usd == pytest.approx(3.0)
    c = Usage(); c.add("claude-sonnet-5-5", 1_000_000, 1_000_000)
    monkeypatch.delenv("LLM_PRICE_INPUT_PER_MTOK"); monkeypatch.delenv("LLM_PRICE_OUTPUT_PER_MTOK")
    assert c.cost_usd == pytest.approx(12.0)                          # $2 in + $10 out


def test_web_reports_when_server_code_is_out_of_date(web_server, monkeypatch):
    from assistant import web
    assert json.loads(_http(web_server + "/api/info")[2])["stale"] is False
    monkeypatch.setattr(web, "STARTUP_FINGERPRINT", "code-as-it-was-at-start")   # simulate an edit after start
    info = json.loads(_http(web_server + "/api/info")[2])
    assert info["stale"] is True and info["memory"] is True



# ------------------------------------------------------- PII typed into the question
def test_pii_in_question_is_masked_before_ai_log_and_memory(tmp_log):
    fake = FakeLLM(["REFUSE: personal data is not available"])
    a = llm.answer_question("which player has phone +91 98765 43210 or email raj.k@example.com?", llm=fake)
    sent = fake.calls[0][-1]["content"]                              # what the AI provider received
    assert "98765" not in sent and "example.com" not in sent and sent.count("[REDACTED]") == 2
    logged = tmp_log.read_text()
    assert "98765" not in logged and "example.com" not in logged      # nor the audit log
    assert "98765" not in a.question                                  # memory stores a.question


def test_question_scrub_leaves_normal_questions_alone(tmp_log):
    q = "What was the approval rate for P100001 on 2026-10-31 for amounts over 10000?"
    fake = FakeLLM(["```sql\nSELECT 1 AS n\n```", "1."])
    llm.answer_question(q, llm=fake)
    assert fake.calls[0][-1]["content"] == q


# ------------------------------------------------------- corner cases found in review
def test_huge_text_value_is_cut_and_scrubbed_fast():
    import time as _t
    t0 = _t.perf_counter()
    r = run_query("SELECT repeat('x', 5000000) AS s, 'a@b.com ' || repeat('y', 3000000) AS t")
    assert _t.perf_counter() - t0 < 3                      # used to hang: quadratic email regex
    assert len(r.rows[0][0]) < 1100 and r.rows[0][0].endswith("[truncated]")
    assert r.rows[0][1].startswith("[REDACTED]")


def test_scrubber_is_linear_on_long_text():
    import time as _t
    t0 = _t.perf_counter()
    guard.scrub_text("x" * 2_000_000 + " mail me at a.b@example.co.in or +91 98765 43210")
    assert _t.perf_counter() - t0 < 2


def test_unparseable_sql_is_retried_not_reported_as_unsafe(tmp_log):
    with pytest.raises(guard.BadSql):
        validate_sql("Here is the query: SELECT 1")
    fake = FakeLLM(["Sure! Here is the query: SELECT count(*) FROM players",       # prose, no code block
                    "```sql\nSELECT count(*) AS n FROM players\n```", "5,000 players."])
    a = llm.answer_question("how many players?", llm=fake)
    assert a.ok and "5,000" in a.text and len(fake.calls) == 3
    fake = FakeLLM(["", "```sql\nSELECT 1 AS n\n```", "1."])                         # empty reply
    assert llm.answer_question("x", llm=fake).ok


def test_daily_summary_handles_days_without_data():
    from daily_summary import build_summary
    s = build_summary("2026-11-01")
    assert "No deposits or withdrawals recorded" in s and "Approval rate: n/a" in s


def test_telegram_message_split_and_allowlist(monkeypatch):
    pytest.importorskip("telegram")
    from assistant import telegram_bot as tb
    parts = tb.split_message("\n".join(f"line {i} " + "z" * 80 for i in range(200)))
    assert len(parts) > 1 and all(len(p) <= tb.TELEGRAM_MAX_CHARS for p in parts)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    assert tb.is_allowed(42)                                           # demo mode (warned at start-up)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "111, 222")
    assert tb.is_allowed(111) and not tb.is_allowed(42)


def test_web_rejects_cross_site_and_malformed_requests(web_server):
    import urllib.error, urllib.request
    def raw(path, data=None, headers=None):
        req = urllib.request.Request(web_server + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    body = json.dumps({"question": "how many players?"}).encode()
    assert raw("/api/ask", body, {"Content-Type": "text/plain"}) == 415          # cross-site "simple" POST
    assert raw("/api/ask", body, {"Content-Type": "application/json", "Host": "evil.example:80"}) == 403
    assert raw("/api/log", None, {"Host": "evil.example"}) == 403               # DNS-rebinding read
    assert raw("/api/ask", body, {"Content-Type": "application/json", "Content-Length": "abc"}) == 400


def test_gemini_retries_dropped_connections_but_not_bad_requests(monkeypatch):
    from assistant import llm as L
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    g = L.GeminiClient(); g.RETRY_WAIT_S = 0
    Dropped = type("RemoteProtocolError", (Exception,), {"__module__": "httpx"})   # what a flaky network raises
    class R: text = "```sql\nSELECT 1\n```"; usage_metadata = None
    calls = []
    def flaky(model, contents, config):
        calls.append(1)
        if len(calls) < 3:
            raise Dropped("Server disconnected without sending a response.")
        return R()
    g.client = type("C", (), {"models": type("M", (), {"generate_content": staticmethod(flaky)})()})()
    assert "SELECT 1" in g.complete("SYS", [{"role": "user", "content": "q"}]) and len(calls) == 3
    BadRequest = type("ClientError", (Exception,), {"code": 400})
    calls.clear()
    def bad(model, contents, config):
        calls.append(1); raise BadRequest("invalid")
    g.client.models.generate_content = staticmethod(bad)
    with pytest.raises(BadRequest):
        g.complete("SYS", [{"role": "user", "content": "q"}])
    assert len(calls) == 1                                               # not retried
