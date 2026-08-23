"""Token prices, USD per million tokens, from Moonshot's published pricing.

The whole point of this project is paying per token instead of a
subscription, which is only an advantage if you can see what you are
spending. Cache-hit input is roughly 10x cheaper but the API does not tell
us hit vs miss per call, so estimates here use the MISS price and are
therefore an upper bound -- an estimate that flatters you is worse than
useless.
"""

from __future__ import annotations

# model -> (input per 1M, output per 1M) in USD, cache-miss pricing.
PRICES: dict[str, tuple[float, float]] = {
    "kimi-k3": (3.00, 15.00),
    "kimi-k2.7-code": (0.95, 4.00),
    "kimi-k2.7-code-highspeed": (1.90, 8.00),
    "kimi-k2.6": (0.95, 4.00),
    "kimi-k2.5": (0.60, 3.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Upper-bound USD cost, or None for an unpriced model."""
    price = PRICES.get(model)
    if price is None:
        return None
    return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]
