"""Dated API list-price equivalents for reported usage, never account charges."""

from decimal import Decimal
from .usage import PriceRates, token_count

VERIFIED_ON = "2026-09-05"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/pricing"
ANTHROPIC_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"

# Standard, global API rates per million tokens. CLI subscription fees are separate.
_CARDS = {
    "gpt-6-astra": ("10", "50", "1", "12.5", OPENAI_SOURCE, 272000),
    "gpt-5.6-sol": ("4", "20", ".4", "5", OPENAI_SOURCE, 272000),
    "gpt-5.6-terra": ("2", "12", ".2", "2.5", OPENAI_SOURCE, 272000),
    "gpt-5.6-luna": (".2", "1.2", ".02", ".25", OPENAI_SOURCE, 272000),
    "claude-opus-5": ("5", "25", ".5", "6.25", ANTHROPIC_SOURCE, None),
    "claude-opus-4-8": ("5", "25", ".5", "6.25", ANTHROPIC_SOURCE, None),
    "claude-opus-4-7": ("5", "25", ".5", "6.25", ANTHROPIC_SOURCE, None),
    "claude-opus-4-6": ("5", "25", ".5", "6.25", ANTHROPIC_SOURCE, None),
    "claude-sonnet-5": ("2", "10", ".2", "2.5", ANTHROPIC_SOURCE, None),
    "claude-sonnet-4-6": ("3", "15", ".3", "3.75", ANTHROPIC_SOURCE, None),
    "claude-haiku-4-5": ("1", "5", ".1", "1.25", ANTHROPIC_SOURCE, None),
}


def api_reference_cost(model: str, usage: dict) -> dict:
    model = "gpt-5.6-sol" if model == "gpt-5.6" else model
    card = _CARDS.get(model)
    result = {
        "amount_range": None,
        "model": model,
        "source": card[4] if card else None,
        "verified_on": VERIFIED_ON,
        "basis": "official_standard_api_reference",
        "currency": "USD",
        "billed_amount": None,
        "assumptions": [],
    }
    if card is None:
        result["unavailable_reason"] = "exact_model_price_unavailable"
        return result
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    if token_count(incoming) is None or token_count(outgoing) is None:
        result["unavailable_reason"] = "usage_unavailable"
        return result
    cached, written = usage.get("cached_input_tokens"), usage.get("cache_write_tokens")
    assumptions = result["assumptions"]
    if cached is None:
        cached = 0
        assumptions.append("unreported_cache_discounts_excluded")
    if written is None:
        written = 0
        assumptions.append("unreported_cache_writes_excluded")
    if token_count(cached) is None or token_count(written) is None:
        result["unavailable_reason"] = "invalid_usage"
        return result
    includes = usage.get("input_includes_cache")
    inclusion_cases = [includes] if isinstance(includes, bool) else [True, False]
    if not isinstance(includes, bool) and (cached or written):
        assumptions.append("cache_inclusion_unknown_range")
    write_rates = [Decimal(card[3])]
    if written and card[4] == ANTHROPIC_SOURCE:
        write_rates.append(Decimal(card[0]) * 2)
        assumptions.append("cache_write_ttl_unknown_range")
    amounts = []
    for included in inclusion_cases:
        total_input = incoming if included else incoming + cached + written
        long_context = card[5] is not None and total_input > card[5]
        for write_rate in write_rates:
            rates = PriceRates(
                Decimal(card[0]) * (2 if long_context else 1),
                Decimal(card[1]) * (Decimal("1.5") if long_context else 1),
                Decimal(card[2]) * (2 if long_context else 1),
                write_rate * (2 if long_context else 1),
                source=card[4],
            )
            priced = rates.estimate(
                {
                    **usage,
                    "cached_input_tokens": cached,
                    "cache_write_tokens": written,
                    "input_includes_cache": included,
                }
            )["amount"]
            if priced is not None:
                amounts.append(Decimal(priced))
    if amounts:
        result["amount_range"] = [str(min(amounts)), str(max(amounts))]
    else:
        result["unavailable_reason"] = "inconsistent_usage"
    return result
