"""All settings in one place. Everything can be overridden with environment variables."""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader (no extra package). Real environment variables win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #")[0].strip().strip('"').strip("'")
        if value:                                   # ignore empty lines like GEMINI_API_KEY=
            os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")

# Read ROOT/.env automatically (API keys etc.). Real environment variables still win.
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
# One database. PII lives in restricted.players; the assistant sees the `players` view.
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "warehouse.duckdb"))
LOG_PATH = Path(os.getenv("LOG_PATH", ROOT / "logs" / "queries.jsonl"))

# Which AI provider writes the SQL: "anthropic", "gemini", or "auto" (use whichever key is set).
PROVIDER = os.getenv("LLM_PROVIDER", "auto").lower()
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")          # used when provider = anthropic
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")    # used when provider = gemini
QUERY_TIMEOUT_SECONDS = float(os.getenv("QUERY_TIMEOUT_SECONDS", "10"))
MAX_ROWS = int(os.getenv("MAX_ROWS", "500"))

# The data ends on 31 Oct 2026 and the brief says to treat 31 Oct as "yesterday",
# so "today" is 1 Nov 2026 unless overridden.
TODAY = date.fromisoformat(os.getenv("ASSISTANT_TODAY", "2026-11-01"))
