"""Command-line assistant.
  python -m assistant.cli                      # interactive
  python -m assistant.cli "How many FTDs last week?"
"""
import sys
from .llm import answer_question, make_llm
from .memory import ConversationMemory

MAX_TABLE_ROWS = 50   # rows printed in the terminal (the full result is still capped by MAX_ROWS)


def print_table(columns, rows, truncated) -> None:
    shown = [[("" if v is None else str(v)) for v in r] for r in rows[:MAX_TABLE_ROWS]]
    widths = [min(max([len(c)] + [len(r[i]) for r in shown]), 40) for i, c in enumerate(columns)]
    line = lambda cells: "  ".join(c[:w].ljust(w) for c, w in zip(cells, widths))
    print(line(columns))
    print("  ".join("-" * w for w in widths))
    for r in shown:
        print(line(r))
    if truncated or len(rows) > MAX_TABLE_ROWS:
        print(f"... showing {len(shown)} of {len(rows)}{'+' if truncated else ''} rows")


def show(question: str, llm, history=None):
    a = answer_question(question, llm=llm, table_shown=True, history=history, conversation="cli")
    print(f"\n{a.text}")
    if len(a.rows) > 1:
        print()
        print_table(a.columns, a.rows, a.truncated)
    if a.sql:
        print(f"\n--- SQL used ---\n{a.sql}\n")
    return a


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")      # ₹ and other symbols on Windows consoles
    llm = make_llm()
    if len(sys.argv) > 1:
        show(" ".join(sys.argv[1:]), llm)
        return
    print("Ask a question about the data. Follow-ups work ('and last week?'). "
          "'new' starts over; blank line or 'exit' quits.")
    memory = ConversationMemory()
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in {"exit", "quit"}:
            break
        if q.lower() == "new":
            memory.clear("cli")
            print("New conversation started.")
            continue
        a = show(q, llm, memory.history("cli"))
        if a.ok:
            memory.add("cli", a.question, a.sql)


if __name__ == "__main__":
    main()
