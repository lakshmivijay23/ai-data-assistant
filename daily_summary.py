"""Task 2 - Daily summary as a short Telegram message.

  python daily_summary.py                      # 31 Oct 2026 vs 30 Oct 2026
  python daily_summary.py --date 2026-10-20    # any day vs the day before

Pure SQL + Python (no AI), so the numbers are deterministic and cannot be hallucinated.
Definitions follow the brief: approval rate by count; withdrawals dated by requested_at;
Hold = approved deposits - approved withdrawals.
"""
import argparse
import sys
from datetime import date, timedelta


from assistant.config import DB_PATH, ROOT
from assistant.guard import connect_readonly

PSPS = ["PayNova", "QuickUPI", "SwiftPe", "ZenPay", "BankLink", "CoinBridge"]
MIN_ATTEMPTS_FOR_PSP_COMPARISON = 30   # ignore PSP moves based on tiny volumes


def day_metrics(con, d: date) -> dict:
    nxt = d + timedelta(days=1)
    dep = con.execute("""
        SELECT count(*), count(*) FILTER (status='approved'),
               coalesce(sum(amount_inr) FILTER (status='approved'), 0)
        FROM deposits WHERE created_at >= ? AND created_at < ?""", [d, nxt]).fetchone()
    psp = {r[0]: r[1:] for r in con.execute("""
        SELECT psp, count(*), count(*) FILTER (status='approved'),
               coalesce(sum(amount_inr) FILTER (status='approved'), 0)
        FROM deposits WHERE created_at >= ? AND created_at < ? GROUP BY psp""", [d, nxt]).fetchall()}
    wd = con.execute("""
        SELECT count(*), coalesce(sum(amount_inr), 0),
               count(*) FILTER (status='approved'),
               coalesce(sum(amount_inr) FILTER (status='approved'), 0)
        FROM withdrawals WHERE requested_at >= ? AND requested_at < ?""", [d, nxt]).fetchone()
    m = dict(attempts=dep[0], approved_n=dep[1], approved_amt=dep[2],
             approval_rate=dep[1] / dep[0] if dep[0] else None,
             wd_req_n=wd[0], wd_req_amt=wd[1], wd_appr_n=wd[2], wd_appr_amt=wd[3],
             hold=dep[2] - wd[3], psp={})
    for p in PSPS:
        a, ok, amt = psp.get(p, (0, 0, 0))
        m["psp"][p] = dict(attempts=a, approved_n=ok, approved_amt=amt, rate=ok / a if a else None)
    return m


def pct_change(new, old):
    if old in (0, None) or new is None:
        return None
    return (new - old) / abs(old) * 100


def arrow(change):
    if change is None:
        return "n/a"
    return f"{'▲' if change > 0 else '▼' if change < 0 else '►'} {abs(change):.1f}%"


def pp_arrow(new, old):
    if new is None or old is None:
        return "n/a"
    diff = (new - old) * 100
    return f"{'▲' if diff > 0 else '▼' if diff < 0 else '►'} {abs(diff):.1f} pp"


def pct(x):
    """76.3%, or n/a when there were no attempts (no division by zero, no crash)."""
    return "n/a" if x is None else f"{x:.1%}"


def inr(x):
    return f"₹{x:,.0f}"


def biggest_changes(cur, prev, n=3):
    """Pick the n most notable moves vs the previous day.

    Rule (kept simple so it can be explained):
      * Slot 1 is ALWAYS the largest PSP approval-rate move (in percentage points),
        because approval by provider is what the payments team acts on. PSPs with fewer
        than 30 attempts on either day are ignored (too noisy).
      * The remaining slots are the largest absolute % changes among volume/money metrics,
        at most one per family, so the bullets are not three views of the same drop.
    """
    picked = []

    psp_moves = []
    for p in PSPS:
        c, o = cur["psp"][p], prev["psp"][p]
        if min(c["attempts"], o["attempts"]) >= MIN_ATTEMPTS_FOR_PSP_COMPARISON:
            psp_moves.append((abs(c["rate"] - o["rate"]), p, c, o))
    if psp_moves:
        _, p, c, o = max(psp_moves, key=lambda m: m[0])
        picked.append(f"{p} approval rate {pp_arrow(c['rate'], o['rate'])} "
                      f"({o['rate']:.1%} → {c['rate']:.1%}; attempts {o['attempts']} → {c['attempts']})")

    volume = [  # (family, label, key, formatter)
        ("deposits", "Deposit attempts", "attempts", lambda x: f"{x:,}"),
        ("deposits", "Approved deposit amount", "approved_amt", inr),
        ("withdrawals", "Withdrawal requests", "wd_req_n", lambda x: f"{x:,}"),
        ("withdrawals", "Approved withdrawal amount", "wd_appr_amt", inr),
        ("hold", "Hold", "hold", inr),
    ]
    scored = []
    for fam, label, key, f in volume:
        ch = pct_change(cur[key], prev[key])
        if ch is not None:
            scored.append((abs(ch), fam, f"{label} {arrow(ch)} ({f(prev[key])} → {f(cur[key])})"))
    used = set()
    for _, fam, text in sorted(scored, key=lambda x: -x[0]):
        if fam not in used and len(picked) < n:
            picked.append(text)
            used.add(fam)
    return picked


def build_summary(day: str = "2026-10-31", db_path=DB_PATH) -> str:
    d = date.fromisoformat(day)
    p = d - timedelta(days=1)
    con = connect_readonly(db_path)
    cur, prev = day_metrics(con, d), day_metrics(con, p)
    con.close()

    lines = [f"📊 Daily summary – {d:%a %d %b %Y} (vs {p:%a %d %b})", "",
             *(["⚠️ No deposits or withdrawals recorded for this day (data covers 1–31 Oct 2026).", ""]
               if cur['attempts'] == 0 and cur['wd_req_n'] == 0 else []),
             "💳 DEPOSITS",
             f"Attempts: {cur['attempts']:,} ({arrow(pct_change(cur['attempts'], prev['attempts']))})",
             f"Approved: {cur['approved_n']:,} · {inr(cur['approved_amt'])} "
             f"({arrow(pct_change(cur['approved_amt'], prev['approved_amt']))})",
             f"Approval rate: {pct(cur['approval_rate'])} ({pp_arrow(cur['approval_rate'], prev['approval_rate'])})",
             "By PSP:"]
    for ps in PSPS:
        c, o = cur["psp"][ps], prev["psp"][ps]
        lines.append(f"  • {ps}: {pct(c['rate'])} ({pp_arrow(c['rate'], o['rate'])}) · {c['attempts']} attempts")
    lines += ["", "💸 WITHDRAWALS",
              f"Requested: {cur['wd_req_n']:,} · {inr(cur['wd_req_amt'])} "
              f"({arrow(pct_change(cur['wd_req_n'], prev['wd_req_n']))} count)",
              f"Approved amount: {inr(cur['wd_appr_amt'])} ({arrow(pct_change(cur['wd_appr_amt'], prev['wd_appr_amt']))})",
              "", f"🏦 HOLD: {inr(cur['hold'])} (prev {inr(prev['hold'])}, "
              f"{arrow(pct_change(cur['hold'], prev['hold']))})",
              "", "🔎 BIGGEST CHANGES vs previous day"]
    lines += [f"{i}. {t}" for i, t in enumerate(biggest_changes(cur, prev), 1)]
    lines += ["", "Hold = approved deposits − approved withdrawals; withdrawals counted by request date."]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-31")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "daily_summary_2026-10-31.txt"))
    args = ap.parse_args()
    msg = build_summary(args.date)
    sys.stdout.reconfigure(encoding="utf-8")   # emoji/₹ crash Windows cp1252 consoles otherwise
    print(msg)
    if args.date == "2026-10-31":
        open(args.out, "w", encoding="utf-8").write(msg + "\n")
        print(f"\n[saved to {args.out}]")
