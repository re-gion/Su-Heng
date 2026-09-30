import json

import pytest

from yuqing.services.budget import (
    BUDGET_FIELDS,
    DEFAULT_BUDGET_TABLE,
    budget_table_to_json,
    dump_budget_overrides,
    parse_budget_overrides,
    resolve_budget_table,
)


def test_three_depths_are_present_and_monotonic():
    assert set(DEFAULT_BUDGET_TABLE) == {"quick", "standard", "deep"}
    ordered = [DEFAULT_BUDGET_TABLE[depth] for depth in ("quick", "standard", "deep")]
    for field in ("token_limit", "search_calls", "fetch_calls", "max_claims", "max_verify_calls"):
        values = [getattr(item, field) for item in ordered]
        assert values == sorted(values), f"{field} 未随档位递增：{values}"


def test_default_budgets_cover_observed_standard_runs_and_history_fetches():
    assert [DEFAULT_BUDGET_TABLE[d].token_limit for d in ("quick", "standard", "deep")] == [
        300_000,
        1_400_000,
        2_000_000,
    ]
    assert DEFAULT_BUDGET_TABLE["standard"].fetch_calls > 45


@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_verify_budget_tracks_the_claim_cap(depth):
    budget = DEFAULT_BUDGET_TABLE[depth]

    # 实测一条 claim 平均消耗约 2.4 条核验关系。核验额度若跟不上 claim 上限，
    # 多出来的陈述会被"核验预算不足"整条跳过、强制降级为待核验。
    assert budget.max_verify_calls >= budget.max_claims * 2


def test_overrides_merge_onto_the_defaults_without_touching_other_fields():
    table = resolve_budget_table({"standard": {"max_claims": 80}})

    assert table["standard"].max_claims == 80
    assert table["standard"].token_limit == DEFAULT_BUDGET_TABLE["standard"].token_limit
    assert table["quick"] == DEFAULT_BUDGET_TABLE["quick"]


def test_unknown_depth_or_field_is_rejected_instead_of_silently_ignored():
    with pytest.raises(ValueError, match="未知的调查深度"):
        resolve_budget_table({"turbo": {"max_claims": 10}})
    with pytest.raises(ValueError, match="未知预算字段"):
        resolve_budget_table({"quick": {"max_claim": 10}})


def test_non_positive_values_are_rejected():
    with pytest.raises(ValueError, match="正整数"):
        resolve_budget_table({"quick": {"max_claims": 0}})


def test_overrides_round_trip_through_the_config_value():
    raw = dump_budget_overrides({"deep": {"max_claims": 120}})

    assert parse_budget_overrides(raw) == {"deep": {"max_claims": 120}}
    assert parse_budget_overrides(None) == {}
    assert parse_budget_overrides("  ") == {}


def test_malformed_overrides_are_reported_not_ignored():
    with pytest.raises(ValueError, match="不是合法 JSON"):
        parse_budget_overrides("{not json")
    with pytest.raises(ValueError, match="必须是对象"):
        parse_budget_overrides(json.dumps([1, 2, 3]))


def test_reported_table_exposes_every_tunable_field():
    table = budget_table_to_json(resolve_budget_table())

    assert set(table["standard"]) == set(BUDGET_FIELDS)
