"""Prices in USD. Update by hand when Anthropic/Tavily change rates.
Cost is computed at write-time and stored on each UsageEvent, so past events
don't drift if you edit this file later.
"""

# Anthropic — USD per 1M tokens
# https://www.anthropic.com/pricing
CLAUDE: dict[str, dict[str, float]] = {
    # Claude 4.x family — current prices (2026)
    "claude-opus-4-7": {
        "input": 15.00, "output": 75.00,
        "cache_write": 18.75, "cache_read": 1.50,
    },
    "claude-opus-4-7[1m]": {  # 1M-context tier, 2x base rate
        "input": 30.00, "output": 150.00,
        "cache_write": 37.50, "cache_read": 3.00,
    },
    "claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.00, "output": 5.00,
        "cache_write": 1.25, "cache_read": 0.10,
    },
    # Fallback — used when the model string doesn't match above.
    # Tweak these if you see $0 rows in the admin page.
    "_default": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    },
}


# Tavily — USD per credit.
#   Free: 1000 credits/mo included
#   Standard: ~$0.008/credit ($8 per 1k)
#   basic search = 1 credit, advanced = 2 credits
TAVILY_USD_PER_CREDIT = 0.008
TAVILY_CREDITS_PER_DEPTH = {
    "basic": 1,
    "advanced": 2,
}


# Voyage AI — USD per 1M tokens. https://docs.voyageai.com/docs/pricing
# voyage-3-lite is what spec 08 uses. Other rows are here so a model swap
# in app/ranker/config.py reflects in cost telemetry without code changes.
VOYAGE: dict[str, float] = {
    "voyage-3-lite": 0.02,
    "voyage-3": 0.06,
    "voyage-3-large": 0.18,
    "_default": 0.02,
}


def voyage_cost(model: str, input_tokens: int) -> float:
    rate = VOYAGE.get(model, VOYAGE["_default"])
    return input_tokens / 1_000_000 * rate


def _pick(model: str) -> dict[str, float]:
    if model in CLAUDE:
        return CLAUDE[model]
    # Prefix match for versioned ids (e.g., "claude-haiku-4-5-20251001")
    for key, rates in CLAUDE.items():
        if key == "_default":
            continue
        if model.startswith(key):
            return rates
    return CLAUDE["_default"]


def claude_cost(model: str, input_tokens: int, output_tokens: int,
                cache_read: int = 0, cache_write: int = 0) -> float:
    r = _pick(model)
    return (
        input_tokens   / 1_000_000 * r["input"]
        + output_tokens / 1_000_000 * r["output"]
        + cache_read    / 1_000_000 * r["cache_read"]
        + cache_write   / 1_000_000 * r["cache_write"]
    )


def tavily_cost(depth: str) -> tuple[int, float]:
    credits = TAVILY_CREDITS_PER_DEPTH.get(depth, 2)
    return credits, credits * TAVILY_USD_PER_CREDIT
