"""Provider usage normalization and explicit, non-invoice price estimates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


def token_count(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def usage_document(
    usage: Any, *, detail: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    detail = detail or {}
    result = {
        "schema_version": "ac.llm.usage.v1",
        "input_tokens": token_count(getattr(usage, "input_tokens", None)),
        "output_tokens": token_count(getattr(usage, "output_tokens", None)),
        "cached_input_tokens": token_count(getattr(usage, "cached_input_tokens", None)),
        "cache_write_tokens": token_count(detail.get("cache_write_tokens")),
        "reasoning_tokens": token_count(detail.get("reasoning_tokens")),
        "input_includes_cache": detail.get("input_includes_cache"),
        "reasoning_in_output": detail.get("reasoning_in_output"),
    }
    for key in ("input_includes_cache", "reasoning_in_output"):
        if not isinstance(result[key], bool):
            result[key] = None
    result["availability"] = (
        "reported"
        if result["input_tokens"] is not None and result["output_tokens"] is not None
        else (
            "partial"
            if any(
                result[k] is not None
                for k in ("input_tokens", "output_tokens", "cached_input_tokens")
            )
            else "unavailable"
        )
    )
    return result


@dataclass(frozen=True)
class PriceRates:
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None
    currency: str = "USD"
    source: str = "user configured"

    def __post_init__(self) -> None:
        for name in (
            "input_per_million",
            "output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
        ):
            raw = getattr(self, name)
            if raw is not None:
                value = Decimal(str(raw))
                if not value.is_finite() or value < 0:
                    raise ValueError("Prices must be finite non-negative decimals.")
                object.__setattr__(self, name, value)

    def estimate(self, usage: Mapping[str, Any]) -> dict[str, Any]:
        incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
        cached, written = usage.get("cached_input_tokens"), usage.get(
            "cache_write_tokens"
        )
        amount = None
        if all(
            token_count(v) is not None for v in (incoming, outgoing, cached, written)
        ) and isinstance(usage.get("input_includes_cache"), bool):
            ordinary = (
                incoming - cached - written
                if usage["input_includes_cache"]
                else incoming
            )
            if (
                ordinary >= 0
                and (not cached or self.cache_read_per_million is not None)
                and (not written or self.cache_write_per_million is not None)
            ):
                total = (
                    ordinary * self.input_per_million
                    + outgoing * self.output_per_million
                )
                total += cached * (
                    self.cache_read_per_million or Decimal(0)
                ) + written * (self.cache_write_per_million or Decimal(0))
                amount = str(total / Decimal(1_000_000))
        return {
            "amount": amount,
            "currency": self.currency,
            "basis": "usage_at_configured_rates",
            "source": self.source,
            "billed_amount": None,
        }
