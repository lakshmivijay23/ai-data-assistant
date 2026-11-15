"""AI cost report from the audit log.

  python -m assistant.cost_report            # everything in logs/queries.jsonl
  python -m assistant.cost_report --days 1   # only the last 24 hours

Totals the cost_usd, tokens and model calls written for every question, overall and by
model, user and day. Questions logged before cost tracking was added have no cost and are
counted separately rather than guessed.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import audit_log
from .pricing import PRICES_CHECKED


def load(days: float | None = None) -> list:
    path = audit_log.LOG_PATH
    if not path.exists():
        return []
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and datetime.fromisoformat(rec["ts"]) < since:
            continue
        out.append(rec)
    return out


def summarise(records: list) -> dict:
    costed = [r for r in records if isinstance(r.get("cost_usd"), (int, float))]
    groups = {k: defaultdict(lambda: [0, 0.0]) for k in ("model", "user", "day")}
    for r in costed:
        for key, val in (("model", r.get("model") or "none"), ("user", r.get("user") or "?"),
                         ("day", r["ts"][:10])):
            groups[key][val][0] += 1
            groups[key][val][1] += r["cost_usd"]
    total = sum(r["cost_usd"] for r in costed)
    return {
        "questions": len(records), "with_cost": len(costed), "without_cost": len(records) - len(costed),
        "total_usd": total, "avg_usd": total / len(costed) if costed else 0.0,
        "max_usd": max((r["cost_usd"] for r in costed), default=0.0),
        "llm_calls": sum(r.get("llm_calls", 0) for r in costed),
        "input_tokens": sum(r.get("input_tokens", 0) for r in costed),
        "output_tokens": sum(r.get("output_tokens", 0) for r in costed),
        "thinking_tokens": sum(r.get("thinking_tokens", 0) for r in costed),
        "groups": {k: dict(v) for k, v in groups.items()},
    }


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, help="only include the last N days")
    s = summarise(load(ap.parse_args().days))
    print(f"AI cost report  (prices checked {PRICES_CHECKED}; paid-tier rates, USD)")
    print(f"Questions logged : {s['questions']:,}  ({s['without_cost']:,} from before cost tracking, not counted)")
    print(f"Total cost       : ${s['total_usd']:.4f}")
    print(f"Average/question : ${s['avg_usd']:.5f}   (most expensive: ${s['max_usd']:.5f})")
    print(f"Model calls      : {s['llm_calls']:,}")
    print(f"Tokens           : {s['input_tokens']:,} in, {s['output_tokens']:,} out "
          f"(of which {s['thinking_tokens']:,} thinking)")
    for title, key in (("By model", "model"), ("By user", "user"), ("By day (UTC)", "day")):
        print(f"\n{title}:")
        for name, (n, usd) in sorted(s["groups"][key].items(), key=lambda kv: -kv[1][1]):
            print(f"  {name:<28} {n:>5} questions   ${usd:.4f}")


if __name__ == "__main__":
    main()
