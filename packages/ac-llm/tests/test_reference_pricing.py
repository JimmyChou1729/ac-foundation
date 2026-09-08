from decimal import Decimal
from ac_llm.reference_pricing import api_reference_cost


def test_luna_cache_prices_and_missing_cache_semantics():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cached_input_tokens": 500,
        "cache_write_tokens": 0,
        "input_includes_cache": True,
    }
    exact = api_reference_cost("gpt-5.6-luna", usage)
    assert [Decimal(v) for v in exact["amount_range"]] == [Decimal(".00023")] * 2
    ranged = api_reference_cost("gpt-5.6-luna", {**usage, "input_includes_cache": None})
    assert [Decimal(v) for v in ranged["amount_range"]] == [
        Decimal(".00023"),
        Decimal(".00033"),
    ]
    assert ranged["billed_amount"] is None


def test_long_context_threshold_applies_to_each_whole_request():
    def price(tokens):
        return api_reference_cost(
            "gpt-5.6-luna",
            {
                "input_tokens": tokens,
                "output_tokens": 1000,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "input_includes_cache": True,
            },
        )

    assert Decimal(price(272000)["amount_range"][0]) == Decimal(".0556")
    assert Decimal(price(272001)["amount_range"][0]) == Decimal(".1106004")


def test_unknown_models_and_missing_usage_do_not_become_free():
    assert (
        api_reference_cost("sonnet", {"input_tokens": 100, "output_tokens": 100})[
            "amount_range"
        ]
        is None
    )
    assert api_reference_cost("gpt-5.6-luna", {})["amount_range"] is None
    result = api_reference_cost(
        "gpt-5.6-luna", {"input_tokens": 100, "output_tokens": 100}
    )
    assert result["amount_range"] is not None
    assert "unreported_cache_writes_excluded" in result["assumptions"]


def test_anthropic_cache_write_ttl_remains_a_range():
    result = api_reference_cost(
        "claude-sonnet-5",
        {
            "input_tokens": 100,
            "output_tokens": 100,
            "cached_input_tokens": 1000,
            "cache_write_tokens": 1000,
            "input_includes_cache": False,
        },
    )
    assert [Decimal(v) for v in result["amount_range"]] == [
        Decimal(".0039"),
        Decimal(".0054"),
    ]
