"""Local web page for the assistant (standard library only, no extra packages).

  python -m assistant.web            ->  open http://127.0.0.1:8000

Endpoints
  GET  /              the page (assistant/static/)
  POST /api/ask       {"question": "...", "conversation_id": "<random id per chat>"}
                      ->  {"text", "sql", "ok", "columns", "rows", "truncated", "unverified",
                           "context_turns", "elapsed_ms"}
  GET  /api/summary   Task 2 daily summary for 31 Oct 2026 (no AI involved)
  GET  /api/log       last few audit-log entries (question, SQL, time taken)
  GET  /api/info      data period and safety limits shown in the page header

Binds to 127.0.0.1 by default, so it is only reachable from this computer.
Every question goes through the same answer_question() as the CLI and Telegram bot,
so the same guard, limits and logging apply.
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import audit_log
from .config import MAX_ROWS, QUERY_TIMEOUT_SECONDS, TODAY
from .llm import answer_question, make_llm
from .memory import ConversationMemory

STATIC_DIR = Path(__file__).with_name("static")
# Only these files are served: no directory listing, no path traversal.
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8")}
MAX_BODY_BYTES = 4096
MAX_QUESTION_CHARS = 500
LOG_FIELDS = ("ts", "user", "question", "sql", "ok", "rows", "elapsed_ms", "error",
              "model", "input_tokens", "output_tokens", "cost_usd")

def code_fingerprint() -> str:
    """Changes whenever any Python file of the assistant changes. Python loads code once at start,
    so if this differs from the value at start-up, the running server is out of date."""
    files = sorted(Path(__file__).parent.glob("*.py")) + [Path(__file__).parent.parent / "daily_summary.py"]
    return ";".join(f"{f.name}:{f.stat().st_mtime_ns}" for f in files if f.exists())


STARTUP_FINGERPRINT = code_fingerprint()

_llm = None
_llm_lock = threading.Lock()
memory = ConversationMemory()                 # per browser chat; the page sends a random id
CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def get_llm():
    """Create the model client on first use, so the page (and the daily summary) works
    even before an API key is configured."""
    global _llm
    with _llm_lock:
        if _llm is None:
            _llm = make_llm()
    return _llm


def recent_log(n: int = 8) -> list:
    path = audit_log.LOG_PATH
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append({k: rec.get(k) for k in LOG_FIELDS})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "DataAssistant"

    # ---------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):          # keep the console quiet; questions go to the audit log
        pass

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "Unexpected Host header."})
        url = urlparse(self.path)
        if url.path in STATIC_FILES:
            name, ctype = STATIC_FILES[url.path]
            return self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
        if url.path == "/api/info":
            return self._json(200, {"today": str(TODAY), "data_from": "2026-10-01", "data_to": "2026-10-31",
                                    "max_rows": MAX_ROWS, "timeout_s": QUERY_TIMEOUT_SECONDS,
                                    "memory": True,
                                    "stale": code_fingerprint() != STARTUP_FINGERPRINT})
        if url.path == "/api/summary":
            from daily_summary import build_summary
            return self._json(200, {"text": build_summary("2026-10-31")})
        if url.path == "/api/log":
            n = parse_qs(url.query).get("n", ["8"])[0]
            n = max(1, min(int(n) if n.isdigit() else 8, 50))
            return self._json(200, {"entries": recent_log(n)})
        self._json(404, {"error": "not found"})

    def _host_ok(self) -> bool:
        """Accept only requests addressed to this server by its own name. Blocks DNS-rebinding,
        where a malicious website points its own domain at 127.0.0.1 to read the local API."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        names = {"127.0.0.1", "localhost", os.getenv("WEB_HOST", "127.0.0.1").lower()}
        return host in {f"{n}:{port}" for n in names} | ({"localhost", "127.0.0.1"} if port == 80 else set())

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "Unexpected Host header."})
        if urlparse(self.path).path != "/api/ask":
            return self._json(404, {"error": "not found"})
        # Requiring JSON means a browser must send a CORS pre-flight first, which this server never
        # approves, so another website cannot make your browser ask questions (and spend API credit).
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self._json(415, {"error": "Send JSON (Content-Type: application/json)."})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            return self._json(400, {"error": "Request is empty or too large."})
        try:
            body = json.loads(self.rfile.read(length))
            question = str(body.get("question", "")).strip()
            cid = str(body.get("conversation_id") or "")
        except (json.JSONDecodeError, AttributeError):
            return self._json(400, {"error": "Send JSON like {\"question\": \"...\"}."})
        if cid and not CONVERSATION_ID_RE.match(cid):
            return self._json(400, {"error": "Invalid conversation id."})
        if not question:
            return self._json(400, {"error": "Please type a question."})
        if len(question) > MAX_QUESTION_CHARS:
            return self._json(400, {"error": f"Please keep questions under {MAX_QUESTION_CHARS} characters."})
        try:
            llm = get_llm()
        except SystemExit as e:                  # make_llm() exits when no API key is set
            return self._json(503, {"error": str(e)})
        t0 = time.perf_counter()
        history = memory.history(cid)
        a = answer_question(question, llm=llm, user="web", table_shown=True,
                            history=history, conversation=cid or None)
        if a.ok:
            memory.add(cid, a.question, a.sql)
        self._json(200, {"text": a.text, "sql": a.sql, "ok": a.ok,
                         "columns": a.columns, "rows": a.rows, "truncated": a.truncated,
                         "unverified": a.unverified, "context_turns": len(history), "usage": a.usage,
                         "elapsed_ms": int((time.perf_counter() - t0) * 1000)})


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    server = make_server(host, port)
    print(f"Data assistant running at http://{host}:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
