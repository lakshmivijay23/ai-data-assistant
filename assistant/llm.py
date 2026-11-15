"""Question -> SQL -> result -> plain-English answer, using Claude or Gemini."""
import re
import sys
import time
from dataclasses import dataclass, field

from .audit_log import log_event
import os

from .config import GEMINI_MODEL, MAX_ROWS, MODEL, PROVIDER
from .grounding import ungrounded_numbers
from .guard import UnsafeQuery, run_query, scrub_text
from .memory import MAX_TURNS
from .pricing import Usage
from .prompts import ANSWER_SYSTEM, sql_system_prompt

SQL_BLOCK = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)
MAX_ROWS_TO_LLM = 100   # rows shown to the model when writing the answer


@dataclass
class Answer:
    text: str
    sql: str | None
    ok: bool
    columns: list = field(default_factory=list)   # the result table, so a front-end can show it
    rows: list = field(default_factory=list)      # exactly as SQL returned it (not re-typed by the AI)
    truncated: bool = False
    unverified: list = field(default_factory=list)   # figures in `text` not found in the result
    usage: dict = field(default_factory=dict)         # model, tokens and cost_usd for this question
    question: str = ""     # the question as sent to the AI (PII masked); store THIS in memory


class ClaudeClient:
    """Thin wrapper so tests can swap in a fake model.

    No `temperature`: current Claude models reject non-default sampling values with a 400.
    Thinking is on by default and counts against max_tokens, so the limit is kept generous."""

    def __init__(self, model: str = MODEL):
        import anthropic                      # imported lazily so tests run without a key
        self.client = anthropic.Anthropic(timeout=120.0, max_retries=2)
        self.model = model

    def complete_with_usage(self, system: str, messages: list, max_tokens: int = 1200):
        resp = self.client.messages.create(
            model=self.model, max_tokens=max(max_tokens, 16000),
            system=system, messages=messages)
        usage = {"model": self.model, "input_tokens": resp.usage.input_tokens,
                 "output_tokens": resp.usage.output_tokens, "thinking_tokens": 0}
        if resp.stop_reason == "refusal":          # declined by the model's safety layer
            return "REFUSE: the model declined this request", usage
        return "".join(b.text for b in resp.content if b.type == "text"), usage

    def complete(self, system: str, messages: list, max_tokens: int = 1200) -> str:
        return self.complete_with_usage(system, messages, max_tokens)[0]


class GeminiClient:
    """Same interface as ClaudeClient, backed by Google Gemini (google-genai SDK)."""

    def __init__(self, model: str = GEMINI_MODEL):
        from google import genai              # imported lazily so tests run without the SDK/key
        from google.genai import types
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        # v1beta is required for system instructions (the stable "v1" endpoint rejects them)
        # timeout (ms): without it a stuck API call would hang the request (and a web thread) forever
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(api_version="v1beta",
                                                                               timeout=60_000))
        self.model = model

    RETRIES = 2          # extra attempts after the first one
    RETRY_WAIT_S = 1.0   # doubled on each retry

    @staticmethod
    def _is_transient(e: Exception) -> bool:
        """Dropped connection, timeout, rate limit (429) or server error (5xx): worth retrying.
        The SDK's own retry_options only cover HTTP status codes, NOT a dropped connection, which
        is what a flaky network actually produces ("Server disconnected without sending a response")."""
        code = getattr(e, "code", None)
        if isinstance(code, int):
            return code == 429 or code >= 500
        return type(e).__module__.split(".")[0] in ("httpx", "httpcore", "httpx2") or \
            isinstance(e, (ConnectionError, TimeoutError))

    def complete_with_usage(self, system: str, messages: list, max_tokens: int = 1200):
        from google.genai import types
        # Anthropic-style roles -> Gemini roles ("assistant" is called "model")
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=[types.Part(text=m["content"])]) for m in messages]
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=0,
            # generous: Gemini "thinking" tokens count against this limit
            max_output_tokens=max(max_tokens, 8192))
        for attempt in range(self.RETRIES + 1):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
                break
            except Exception as e:
                if attempt == self.RETRIES or not self._is_transient(e):
                    raise
                print(f"[retry] Gemini call failed ({type(e).__name__}); retry {attempt + 1}/{self.RETRIES}",
                      file=sys.stderr)
                time.sleep(self.RETRY_WAIT_S * 2 ** attempt)
        um = getattr(resp, "usage_metadata", None)
        count = lambda name: (getattr(um, name, 0) or 0) if um else 0
        thinking = count("thoughts_token_count")
        usage = {"model": self.model,
                 "input_tokens": count("prompt_token_count"),
                 # thinking tokens are billed at the output rate
                 "output_tokens": count("candidates_token_count") + thinking,
                 "thinking_tokens": thinking}
        return resp.text or "", usage

    def complete(self, system: str, messages: list, max_tokens: int = 1200) -> str:
        return self.complete_with_usage(system, messages, max_tokens)[0]


def make_llm():
    """Pick the provider: LLM_PROVIDER=anthropic|gemini, or auto = whichever API key is set."""
    provider = PROVIDER
    if provider == "auto":
        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            provider = "gemini"
        else:
            raise SystemExit("No API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY (see .env.example).")
    if provider == "gemini":
        return GeminiClient()
    if provider == "anthropic":
        return ClaudeClient()
    raise SystemExit(f"Unknown LLM_PROVIDER '{provider}' (use anthropic, gemini or auto).")


def extract_sql(text: str) -> str:
    m = SQL_BLOCK.search(text)
    return (m.group(1) if m else text).strip()


def _format_rows(columns, rows, truncated) -> str:
    lines = [" | ".join(columns)] + [" | ".join(str(v) for v in r) for r in rows[:MAX_ROWS_TO_LLM]]
    if truncated or len(rows) > MAX_ROWS_TO_LLM:
        lines.append(f"(partial: only the first {min(len(rows), MAX_ROWS_TO_LLM)} rows shown)")
    return "\n".join(lines)


# Added to the answer prompt when the front-end shows the result table itself (web page, CLI).
TABLE_SHOWN_NOTE = """
- The user ALSO SEES THE FULL RESULT TABLE directly under your answer. If the result has more
  than 8 rows, do NOT list the rows: give the headline in 1-3 sentences (how many rows/items,
  totals, the most notable ones) and say the full list is in the table below.
- Do not count or add up rows yourself. Only quote numbers that appear in the result (or the
  row count given to you); if a total is not in the result, describe the list without it."""


def _history_messages(history) -> list:
    """Previous (question, SQL) pairs as a chat transcript. Result rows are never included."""
    msgs = []
    for q, s in (history or [])[-MAX_TURNS:]:
        msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": f"```sql\n{s}\n```"}]
    return msgs


def _complete(llm, usage: Usage, system: str, messages: list, max_tokens: int = 1200) -> str:
    """One model call, with its tokens added to `usage` (test fakes without usage still work)."""
    if hasattr(llm, "complete_with_usage"):
        text, u = llm.complete_with_usage(system, messages, max_tokens)
        usage.add(**u)
        return text
    return llm.complete(system, messages, max_tokens)


def answer_question(question: str, llm=None, user: str = "cli", table_shown: bool = False,
                    history: list | None = None, conversation: str | None = None) -> Answer:
    """table_shown=True when the caller displays the result rows itself (web page, CLI). The AI
    then summarises instead of re-typing long lists, so answers are never cut off mid-list.
    history = earlier (question, SQL) pairs of this conversation, for follow-up questions.
    Every call is logged with its SQL, time taken, tokens used and cost in USD."""
    llm = llm or make_llm()
    t0 = time.perf_counter()
    # A user may type personal data INTO the question ("who has phone +91 98765 43210?").
    # Mask it before it reaches the AI provider, the audit log or conversation memory.
    question = scrub_text(question)
    usage = Usage()
    info = {"sql": None, "error": None, "rows": 0, "unverified": []}
    try:
        answer = _answer(question, llm, table_shown, history, usage, info)
    except Exception as e:                           # API down, etc.
        info["error"] = f"error: {type(e).__name__}: {e}"
        print(f"[debug] {info['error']}", file=sys.stderr)   # real cause goes to the console/log, not the user
        answer = Answer("Something went wrong while answering. Please try again.", info["sql"], False)
    answer.usage = usage.as_log_fields()
    answer.question = question
    log_event(user=user, conversation=conversation, followup_context=len(history or []),
              question=question, sql=info["sql"], ok=answer.ok, rows=info["rows"],
              elapsed_ms=int((time.perf_counter() - t0) * 1000), error=info["error"],
              unverified_numbers=info["unverified"] or None, **answer.usage)
    return answer


def _answer(question, llm, table_shown, history, usage: Usage, info: dict) -> Answer:
    system = sql_system_prompt()
    messages = _history_messages(history) + [{"role": "user", "content": question}]
    raw = _complete(llm, usage, system, messages)

    if raw.strip().upper().startswith("REFUSE"):
        reason = raw.split(":", 1)[-1].strip().rstrip(".") or "this request can't be answered"
        info["error"] = f"refused: {reason}"
        return Answer(f"I can't help with that: {reason}.", None, False)

    # Up to 2 attempts: if the database rejects the SQL, show the model the error once.
    for attempt in range(2):
        sql = info["sql"] = extract_sql(raw)
        try:
            result = run_query(sql)
            break
        except UnsafeQuery as e:                 # blocked by the guard: never retry
            info["error"] = f"blocked: {e}"
            return Answer(f"That question can't be answered safely: {e}", sql, False)
        except Exception as e:                   # SQL error (bad column etc.): retry once
            info["error"] = f"sql_error: {e}"
            if attempt == 1:
                return Answer("I couldn't turn that into a working query. Please rephrase.", sql, False)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That SQL failed with: {e}\nReturn corrected SQL only."}]
            raw = _complete(llm, usage, system, messages)

    info["error"] = None
    info["rows"] = len(result.rows)
    context = f"(Follow-up to the earlier question: {history[-1][0]})\n" if history else ""
    payload = (f"{context}Question: {question}\n\nSQL:\n{sql}\n\nResult ({len(result.rows)} rows"
               f"{', truncated at cap' if result.truncated else ''}):\n"
               f"{_format_rows(result.columns, result.rows, result.truncated)}")
    system_answer = ANSWER_SYSTEM + (TABLE_SHOWN_NOTE if table_shown else "")
    answer_msgs = [{"role": "user", "content": payload}]
    text = _complete(llm, usage, system_answer, answer_msgs, max_tokens=1500).strip()

    # Number check: every figure in the sentence must come from the result. One rewrite if not.
    unverified = ungrounded_numbers(text, result.columns, result.rows, question, sql)
    if unverified:
        answer_msgs += [{"role": "assistant", "content": text},
                        {"role": "user", "content": "These figures in your answer are not in the result: "
                         f"{', '.join(unverified)}. Rewrite the answer using ONLY numbers that appear in "
                         "the result rows (or the row count). Do not calculate new numbers."}]
        text = _complete(llm, usage, system_answer, answer_msgs, max_tokens=1500).strip()
        unverified = ungrounded_numbers(text, result.columns, result.rows, question, sql)
        if unverified:
            text += ("\n\nNote: I could not match these figures to the query result: "
                     f"{', '.join(unverified)}. Please rely on the table / SQL for them.")
    info["unverified"] = unverified
    return Answer(text, sql, True, list(result.columns), list(result.rows), result.truncated, unverified)
