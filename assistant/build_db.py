"""Load the CSVs into ONE DuckDB file. Run once:  python -m assistant.build_db

DATA-LAYER PRIVACY: one database, PII stored once, the assistant sees a view without it.

  restricted.players  The full player record, INCLUDING phone and email. Stored, not deleted.
  players (view)      What the assistant queries: the same rows without phone/email, so even
                      `SELECT * FROM players` cannot return them.

DuckDB has no users or GRANTs, so nothing inside DuckDB stops a query from naming
restricted.players directly. guard.py fills that role: every query is read with DuckDB's own
parser and may reference only ASSISTANT_OBJECTS below; anything else (restricted.*, catalog
tables, query_table(...), files) is refused before it runs, and anything it cannot parse is
refused too. In production on ClickHouse the same design is a read-only user with
GRANT SELECT on the PII-free view only; the allowlist then becomes a second line of defence.
"""
import duckdb
from .config import DATA_DIR, DB_PATH

PII_COLUMNS = ("phone", "email")
# Everything the assistant's SQL is allowed to reference (all in the main schema).
ASSISTANT_OBJECTS = frozenset({"players", "deposits", "withdrawals", "bet_sessions", "logins",
                               "approved_deposits", "approved_withdrawals"})


def build() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    csv = lambda name: f"read_csv('{(DATA_DIR / name).as_posix()}', header=true, auto_detect=true)"

    # Full player record (incl. PII), in a schema the assistant is not allowed to reference
    con.execute("CREATE SCHEMA restricted")
    con.execute(f"CREATE TABLE restricted.players AS SELECT * FROM {csv('players.csv')}")
    # Assistant-facing view: same rows, PII columns left out
    con.execute("""
        CREATE VIEW players AS
        SELECT player_id, registration_time, first_deposit_time, state, vip_level
        FROM restricted.players""")

    con.execute(f"CREATE TABLE deposits AS SELECT * FROM {csv('deposits.csv')}")
    con.execute(f"CREATE TABLE withdrawals AS SELECT * FROM {csv('withdrawals.csv')}")
    # ggr_inr = wager - win, precomputed so the AI cannot get the sign wrong
    con.execute(f"""CREATE TABLE bet_sessions AS
        SELECT *, wager_inr - win_inr AS ggr_inr FROM {csv('bet_sessions.csv')}""")
    con.execute(f"CREATE TABLE logins AS SELECT * FROM {csv('logins.csv')}")

    # Convenience views that encode the business definitions
    con.execute("CREATE VIEW approved_deposits AS SELECT * FROM deposits WHERE status = 'approved'")
    con.execute("CREATE VIEW approved_withdrawals AS SELECT * FROM withdrawals WHERE status = 'approved'")

    for t in ["players", "deposits", "withdrawals", "bet_sessions", "logins"]:
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"loaded {t:<13} {n:>7,} rows")

    # Checks: PII is stored, and no object the assistant may query exposes it
    stored = [r[0] for r in con.execute("DESCRIBE restricted.players").fetchall()]
    assert set(PII_COLUMNS) <= set(stored), "PII columns missing from restricted.players"
    exposed = con.execute(
        "SELECT table_name, column_name FROM duckdb_columns() WHERE schema_name = 'main' "
        f"AND table_name IN ({', '.join(repr(o) for o in sorted(ASSISTANT_OBJECTS))}) "
        "AND lower(column_name) IN ('phone', 'email')").fetchall()
    assert not exposed, f"PII visible to the assistant: {exposed}"
    print("restricted.players columns (stored)   :", stored)
    print("players view columns (assistant sees) :", [r[0] for r in con.execute("DESCRIBE players").fetchall()])
    con.close()


if __name__ == "__main__":
    build()
