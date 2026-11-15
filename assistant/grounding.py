"""Number check: every figure in the AI's written answer must come from the query result.

The SQL result is computed by the database, but the sentence around it is written by the AI,
and that is where a wrong figure could slip in (a miscount, an addition it did itself,
a typo). This check pulls every number out of the answer and looks for it in the result:
  * the cell values themselves, also as a percentage (0.7633 -> 76.33%) and in thousands /
    lakh / million / crore (945,590 -> 9.46 lakh), allowing for rounding;
  * the row count, and numbers written in the question or in the SQL (e.g. "top 5", ">= 3");
  * numbers inside text and date cells (IPs, IDs, timestamps).
Day numbers 1-31 and years 2020-2030 are always allowed, because the answer must state its
period. This is the one known gap: a small integer the AI made up would not be caught.
"""
import datetime as _dt
import re

NUM_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
SCALES = (1, 100, 0.01, 1e-3, 1e-5, 1e-6, 1e-7)   # as-is, percent, fraction, k, lakh, million, crore


def numbers_in(text: str) -> list:
    out = []
    for m in NUM_RE.finditer(str(text)):
        s = m.group().replace(",", "")
        try:
            out.append((m.group(), float(s)))
        except ValueError:
            pass
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, 0.006 * abs(b))


def _candidates(columns, rows, question, sql) -> set:
    cands = {float(len(rows))}
    for _, v in numbers_in(question) + numbers_in(sql or ""):
        cands.add(v)
    for r in rows:
        for v in r:
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (int, float)):
                cands.update(float(v) * s for s in SCALES)
            elif isinstance(v, (_dt.date, _dt.datetime)):
                cands.update(float(x) for x in (v.year, v.month, v.day))
            else:
                try:                                     # Decimal and numeric strings
                    cands.update(float(v) * s for s in SCALES)
                except (TypeError, ValueError):
                    cands.update(n for _, n in numbers_in(v))
    return cands


def ungrounded_numbers(answer: str, columns, rows, question: str, sql: str) -> list:
    """Return the numbers in `answer` that cannot be traced to the result (empty list = all good)."""
    cands = _candidates(columns, rows, question, sql)
    bad = []
    for raw, n in numbers_in(answer):
        if n.is_integer() and (0 <= n <= 31 or 2020 <= n <= 2030):
            continue
        if any(_close(n, c) for c in cands):
            continue
        bad.append(raw)
    return bad
