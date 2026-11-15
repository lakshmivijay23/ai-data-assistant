"""Append-only JSON-lines audit log: one line per question."""
import json
import threading
from datetime import datetime, timezone
from .config import LOG_PATH

# The web server answers questions in parallel threads; the lock keeps two log lines from
# being interleaved into one unreadable line.
_lock = threading.Lock()


def log_event(**fields) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    with _lock:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
