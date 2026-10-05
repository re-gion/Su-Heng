from itertools import product

import pytest

from yuqing.services.verification import BadgeInputs, EntityEvidence, decide_badge, merge_stances


def expected_row(inputs: BadgeInputs) -> str:
    if not inputs.verification_complete:
        return "D1"
    if inputs.ind_s >= 1 and inputs.ind_u >= 1:
        return "D2"
    if inputs.ind_s >= 1 and inputs.ind_u == 0 and inputs.has_conflict:
        return "D3"
    if inputs.ind_s == 0 and inputs.ind_u >= 1 and inputs.authority_u >= 1:
        return "D4"
    if inputs.ind_s == 0 and inputs.ind_u >= 2 and inputs.authority_u == 0:
        return "D5"
    if inputs.ind_s == 0 and inputs.ind_u == 1 and inputs.authority_u == 0:
        return "D6"
    if inputs.ind_s >= 2 and inputs.ind_u == 0 and not inputs.has_conflict:
        return "D7"
    if (
        inputs.ind_s == 1
        and inputs.ind_u == 0
        and not inputs.has_conflict
        and inputs.authority_s == 1
    ):
        return "D8"
    if inputs.ind_s == 0 and inputs.ind_u == 0 and inputs.has_conflict:
        return "D9"
    if inputs.ind_s == 1 and inputs.ind_u == 0 and not inputs.has_conflict:
        return "D10"
    return "D11"


def test_all_100_legal_decision_combinations_have_one_safe_result():
    cases = []
    for ind_s, ind_u, conflict in product(range(3), range(3), (False, True)):
        for authority_s in range(min(ind_s, 1) + 1):
            for authority_u in range(min(ind_u, 1) + 1):
                for complete in (False, True):
                    cases.append(
                        BadgeInputs(ind_s, ind_u, conflict, authority_s, authority_u, complete)
                    )

    assert len(cases) == 100
    for inputs in cases:
        result = decide_badge(inputs)
        assert result.rule == expected_row(inputs)
        assert not (result.badge == "verified" and (inputs.ind_u > 0 or inputs.has_conflict))


@pytest.mark.parametrize(
    ("evidence", "badge", "rule"),
    [
        (
            [
                EntityEvidence("监管机构", "authority", 1, "support", "2026-08-01", False),
                EntityEvidence("监管机构", "authority", 1, "contradict", "2026-08-03", True),
            ],
            "refuted",
            "D4",
        ),
        (
            [
                EntityEvidence("监管机构", "authority", 1, "support", "2026-08-01", False),
                EntityEvidence("监管机构", "authority", 1, "contradict", "2026-08-01", False),
            ],
            "disputed",
            "D9",
        ),
        (
            [
                EntityEvidence("监管机构", "authority", 1, "support", None, False),
                EntityEvidence(
                    "监管机构", "authority", 1, "contradict", "2026-08-03T10:00:00+08:00", True
                ),
            ],
            "disputed",
            "D9",
        ),
        (
            [
                EntityEvidence("媒体甲", "independent", 2, "support"),
                EntityEvidence("媒体乙", "independent", 2, "support"),
                EntityEvidence("媒体丙", "independent", 3, "conflict"),
            ],
            "disputed",
            "D3",
        ),
        (
            [
                EntityEvidence("媒体甲", "independent", 2, "not_mentioned"),
                EntityEvidence("媒体乙", "independent", 2, "not_mentioned"),
            ],
            "unverified",
            "D11",
        ),
    ],
)
def test_entity_stance_gold_cases(evidence, badge, rule):
    inputs = merge_stances(evidence, verification_complete=True)
    result = decide_badge(inputs)
    assert (result.badge, result.rule) == (badge, rule)


def test_syndicated_and_unknown_sources_cannot_manufacture_independent_support():
    inputs = merge_stances(
        [
            EntityEvidence("门户甲", "syndicated", 3, "support"),
            EntityEvidence("门户乙", "syndicated", 3, "support"),
            EntityEvidence("涉事企业", "unknown", 1, "support"),
        ],
        verification_complete=True,
    )
    assert decide_badge(inputs).rule == "D11"


@pytest.mark.parametrize(
    ("evidence", "rule", "note"),
    [
        # 只拿到"部分支持"：徽章仍是待核验（D11），但结论必须写"部分支持"，
        # 不能写成"无有效证据"——那会让读过材料的读者以为没人提过这件事。
        ([EntityEvidence("媒体甲", "independent", 2, "partial")], "D11", "部分支持"),
        (
            [
                EntityEvidence("媒体甲", "independent", 2, "support"),
                EntityEvidence("媒体乙", "independent", 2, "partial"),
            ],
            "D10",
            "部分支持",
        ),
        # 对照：确实没有任何有效立场时，才写"无有效证据"。
        ([EntityEvidence("媒体甲", "independent", 2, "not_mentioned")], "D11", "无有效证据"),
        (
            [EntityEvidence("媒体甲", "independent", 2, "support")],
            "D10",
            "单一非权威来源支持",
        ),
    ],
)
def test_partial_stance_changes_the_card_note_not_the_badge(evidence, rule, note):
    result = decide_badge(merge_stances(evidence, verification_complete=True))

    assert (result.rule, result.note) == (rule, note)
    assert result.badge == "unverified"


def test_authority_group_is_judged_as_a_whole_after_entity_merging():
    # 同一采编主体归并后，组内可能混有 authority 站点与门户号。裁判性权威资格
    # 必须按整组判定，否则会被证据绑定顺序决定，静默丢掉 D8 的单源·官方路径。
    merged_with_portal_first = merge_stances(
        [
            EntityEvidence("同一主体", "syndicated", 3, "support"),
            EntityEvidence("同一主体", "authority", 1, "support"),
        ],
        verification_complete=True,
    )

    assert merged_with_portal_first.authority_s == 1
    assert decide_badge(merged_with_portal_first).rule == "D8"


@pytest.mark.parametrize(
    "complete, extra, badge",
    [
        (True, [], "verified"),
        (False, [], "unverified"),
        (True, [EntityEvidence("媒体甲", "independent", 2, "contradict")], "disputed"),
        (True, [EntityEvidence("媒体甲", "independent", 2, "conflict")], "disputed"),
        (True, [EntityEvidence("门户", "syndicated", 3, "contradict")], "unverified"),
    ],
)
def test_primary_publication_exception_retains_completion_and_conflict_guards(
    complete, extra, badge
):
    own = EntityEvidence("发布机构", "party", 2, "support", primary_publication=True)
    inputs = merge_stances([own, *extra], verification_complete=complete, attribution_claim=True)
    assert decide_badge(inputs).badge == badge
    if badge == "verified":
        assert decide_badge(inputs).rule == "D12"


def test_party_statement_does_not_verify_an_underlying_accusation():
    own = EntityEvidence("发布机构", "party", 2, "support")
    assert decide_badge(merge_stances([own], verification_complete=True)).badge == "unverified"
