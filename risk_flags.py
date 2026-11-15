"""Task 3 - Simple risk flags.   python risk_flags.py

Rule A (shared IP): an IP address used by >= 3 DIFFERENT players in October.
Rule B (deposit & run): an APPROVED deposit followed by a withdrawal REQUEST from the same
        player within 60 minutes, with NO betting activity in between.

Writes CSVs to outputs/. Logic and assumptions are in outputs/risk_flags_logic.md.
"""
import csv


from assistant.config import DB_PATH, ROOT
from assistant.guard import connect_readonly

OUT = ROOT / "outputs"

RULE_A_IPS = """
SELECT ip_address,
       count(DISTINCT player_id)                      AS distinct_players,
       count(*)                                       AS logins,
       string_agg(DISTINCT player_id, ' ' ORDER BY player_id) AS player_ids,
       min(login_time)                                AS first_login,
       max(login_time)                                AS last_login
FROM logins
GROUP BY ip_address
HAVING count(DISTINCT player_id) >= 3
ORDER BY distinct_players DESC, ip_address
"""

# Every (approved deposit, withdrawal request) pair from the same player where the request
# came 0-60 minutes after the deposit. Withdrawal status is NOT filtered: the rule is about the
# request. "Betting activity in between" = any bet session that overlaps [deposit, request]
# (it started before the request AND had not ended before the deposit; boundaries inclusive).
PAIRS = """
WITH pairs AS (
  SELECT d.player_id, d.deposit_id, d.created_at AS deposit_time, d.amount_inr AS deposit_amount,
         d.psp, w.withdrawal_id, w.requested_at AS withdrawal_time, w.amount_inr AS withdrawal_amount,
         w.status AS withdrawal_status,
         date_diff('minute', d.created_at, w.requested_at) AS minutes_between
  FROM deposits d
  JOIN withdrawals w
    ON w.player_id = d.player_id
   AND w.requested_at >= d.created_at
   AND w.requested_at <= d.created_at + INTERVAL 60 MINUTE
  WHERE d.status = 'approved'
)
SELECT p.*,
  -- sessions that START inside the window (betting in between, in the narrowest sense)
  (SELECT count(*) FROM bet_sessions b WHERE b.player_id = p.player_id
      AND b.start_time >= p.deposit_time AND b.start_time <= p.withdrawal_time) AS sessions_started_in_window,
  -- sessions already running when the deposit landed and still running/ended after it
  (SELECT count(*) FROM bet_sessions b WHERE b.player_id = p.player_id
      AND b.start_time < p.deposit_time AND b.end_time >= p.deposit_time)       AS sessions_in_progress_at_deposit
FROM pairs p
ORDER BY p.deposit_time, p.player_id
"""


def write_csv(name, columns, rows):
    path = OUT / name
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)
    print(f"wrote {path.name:<42} {len(rows):>4} rows")


def main():
    OUT.mkdir(exist_ok=True)
    con = connect_readonly(DB_PATH)

    # ---- Rule A ----
    cur = con.execute(RULE_A_IPS)
    a_cols, a_rows = [d[0] for d in cur.description], cur.fetchall()
    write_csv("rule_a_shared_ip_addresses.csv", a_cols, a_rows)
    shared = {r[0]: r for r in a_rows}
    a_players = {}                                    # player_id -> list of (ip, n_players)
    for ip, n, _, pids, *_ in a_rows:
        for pid in pids.split():
            a_players.setdefault(pid, []).append((ip, n))

    # ---- Rule B ----
    cur = con.execute(PAIRS)
    b_cols, pairs = [d[0] for d in cur.description], cur.fetchall()
    idx = {c: i for i, c in enumerate(b_cols)}
    flagged = [r for r in pairs if r[idx["sessions_started_in_window"]] == 0
               and r[idx["sessions_in_progress_at_deposit"]] == 0]
    borderline = [r for r in pairs if r[idx["sessions_started_in_window"]] == 0
                  and r[idx["sessions_in_progress_at_deposit"]] > 0]
    write_csv("rule_b_deposit_withdraw_no_play.csv", b_cols, flagged)
    write_csv("rule_b_borderline_session_in_progress.csv", b_cols, borderline)
    b_players = {}
    for r in flagged:
        b_players.setdefault(r[idx["player_id"]], []).append(r)

    # ---- Combined player-level list ----
    all_ids = sorted(set(a_players) | set(b_players))
    rows = []
    for pid in all_ids:
        a = a_players.get(pid, [])
        b = b_players.get(pid, [])
        rows.append([
            pid,
            "Y" if a else "N", "Y" if b else "N",
            ("A+B" if a and b else "A" if a else "B"),
            "; ".join(f"{ip} (shared by {n} players)" for ip, n in a),
            len(b),
            sum(r[idx["withdrawal_amount"]] for r in b) if b else 0,
            "; ".join(f"{r[idx['deposit_id']]}->{r[idx['withdrawal_id']]} ({r[idx['minutes_between']]} min)" for r in b),
        ])
    write_csv("risk_flags_players.csv",
              ["player_id", "rule_a_shared_ip", "rule_b_deposit_and_run", "rules_triggered",
               "rule_a_ips", "rule_b_pair_count", "rule_b_withdrawal_amount_inr", "rule_b_pairs"], rows)

    n_both = sum(1 for r in rows if r[3] == "A+B")
    print(f"\nRule A: {len(a_rows)} IPs shared by 3+ players -> {len(a_players)} players")
    print(f"Rule B: {len(flagged)} deposit->withdrawal pairs -> {len(b_players)} players "
          f"(+{len(borderline)} borderline pairs where a session was in progress at deposit time)")
    print(f"Total flagged players: {len(rows)} (both rules: {n_both})")
    con.close()


if __name__ == "__main__":
    main()
