"""Token usage and dollar cost per question (written to the audit log).

Prices are USD per 1 million tokens, standard (non-batch) paid tier, checked on PRICES_CHECKED.
Gemini bills its hidden "thinking" tokens at the output rate; Claude's usage.output_tokens
already includes thinking. Override with LLM_PRICE_INPUT_PER_MTOK / LLM_PRICE_OUTPUT_PER_MTOK
if your rates differ (other model, regional or negotiated pricing).
Sources: https://ai.google.dev/gemini-api/docs/pricing and https://platform.claude.com/docs (pricing).
Note: Gemini's free tier is not billed at all; the log shows what the same calls would cost on paid.
"""
import os
from dataclasses import dataclass

PRICES_CHECKED = "2026-11-15"
PRICES_PER_MTOK = {               # model id: (input, output) in USD per 1M tokens
    "gemini-2.5-flash": (0.30, 2.50),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def price_for(model: str | None):
    """(input, output) USD per 1M tokens, or None if the model is unknown and not overridden."""
    env_in, env_out = os.getenv("LLM_PRICE_INPUT_PER_MTOK"), os.getenv("LLM_PRICE_OUTPUT_PER_MTOK")
    if env_in and env_out:
        return float(env_in), float(env_out)
    return PRICES_PER_MTOK.get(model or "")


@dataclass
class Usage:
    """Adds up every model call made while answering one question."""
    model: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0          # billed output, thinking included
    thinking_tokens: int = 0        # part of output_tokens, shown separately for insight

    def add(self, model: str, input_tokens: int, output_tokens: int, thinking_tokens: int = 0) -> None:
        self.model = model
        self.calls += 1
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)
        self.thinking_tokens += int(thinking_tokens or 0)

    @property
    def cost_usd(self) -> float | None:
        if not self.calls:
            return 0.0
        price = price_for(self.model)
        if price is None:
            return None
        return round((self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000, 6)

    def as_log_fields(self) -> dict:
        return {"model": self.model, "llm_calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "thinking_tokens": self.thinking_tokens,
                "cost_usd": self.cost_usd}
