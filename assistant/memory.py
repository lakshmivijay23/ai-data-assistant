"""Short-term conversation memory, so follow-ups like "and last week?" work.

What is remembered: the last few (question, SQL) pairs of ONE conversation (a browser chat,
a Telegram chat, a CLI session). What is NOT remembered: result rows. So no player data is
ever sent back to the AI, and the cost per question stays small.

Kept server-side (the browser only holds a random conversation id). A client therefore
cannot plant fake "previous SQL" in the prompt. Only questions that were answered
successfully are stored, so refused or blocked requests never become context.
Memory lives in the process only: it disappears on restart, by design.
"""
import threading
import time
from collections import OrderedDict

MAX_TURNS = 3               # previous Q+SQL pairs sent with a new question
MAX_CONVERSATIONS = 1000    # least recently used conversations are dropped beyond this
TTL_SECONDS = 2 * 60 * 60   # a conversation idle for 2 hours is forgotten


class ConversationMemory:
    def __init__(self, max_turns=MAX_TURNS, max_conversations=MAX_CONVERSATIONS, ttl_s=TTL_SECONDS):
        self.max_turns, self.max_conversations, self.ttl_s = max_turns, max_conversations, ttl_s
        self._data = OrderedDict()          # conversation id -> (last_used, [(question, sql), ...])
        self._lock = threading.Lock()

    def _expire(self, now):
        for cid in [c for c, (t, _) in self._data.items() if now - t > self.ttl_s]:
            del self._data[cid]

    def history(self, cid) -> list:
        if not cid:
            return []
        with self._lock:
            now = time.time()
            self._expire(now)
            if cid not in self._data:
                return []
            _, turns = self._data[cid]
            self._data[cid] = (now, turns)
            self._data.move_to_end(cid)
            return list(turns)

    def add(self, cid, question: str, sql: str) -> None:
        if not cid or not sql:
            return
        with self._lock:
            now = time.time()
            _, turns = self._data.get(cid, (now, []))
            turns = (turns + [(question, sql)])[-self.max_turns:]
            self._data[cid] = (now, turns)
            self._data.move_to_end(cid)
            while len(self._data) > self.max_conversations:
                self._data.popitem(last=False)

    def clear(self, cid) -> None:
        with self._lock:
            self._data.pop(cid, None)
