from types import SimpleNamespace

import pytest

from yuqing.agents.openai_runtime import (
    EVIDENCE_CONTEXT_MAX_ITEMS,
    OpenAIInvestigationAgent,
    build_evidence_digest,
)


class StaticGateway:
    def __init__(self, result: dict):
        self.result = result
        self.calls = []

    async def complete_json(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.mark.asyncio
async def test_summarize_drops_claim_with_unsupported_statement_kind():
    gateway = StaticGateway(
        {
            "claims": [
                {
                    "text": "部分报道持批评意见。",
                    "statement_kind": "opinion",
                    "evidence_ids": ["E001"],
                },
                {
                    "text": "某媒体于当日发布了相关报道。",
                    "statement_kind": "fact",
                    "evidence_ids": ["E001"],
                },
            ]
        }
    )
    agent = OpenAIInvestigationAgent(gateway, "system", role="analyst_b")
    evidence = [
        SimpleNamespace(local_id="E001", title="公开报道", snippet="该媒体于当日发布报道。")
    ]

    claims = await agent.summarize("测试事件", evidence)

    assert [claim.text for claim in claims] == ["某媒体于当日发布了相关报道。"]
    assert claims[0].statement_kind == "fact"


def _evidence(index: int, *, role: str = "independent", lang: str = "zh"):
    return SimpleNamespace(
        local_id=f"E{index:03d}",
        title=f"材料 {index}",
        snippet=f"摘要 {index}",
        source_name=f"来源 {index}",
        source_domain=f"source-{index}.example",
        publisher_entity=f"主体 {index}",
        source_role=role,
        source_tier=2,
        published_at=f"2026-09-{(index % 28) + 1:02d}T08:00:00+08:00",
        fetch_status="fetched",
        content_text=f"原文开头 {index} " + ("长内容" * 1000) + f" 原文结尾 {index}",
        lang=lang,
    )


def test_evidence_digest_keeps_metadata_and_both_ends_without_tail_starvation():
    digest = build_evidence_digest([_evidence(index) for index in range(1, 41)])

    assert "E001 | 标题=材料 1 | 来源=主体 1 | 角色=independent | 等级=L2" in digest
    assert "E040 | 标题=材料 40 | 来源=主体 40 | 角色=independent | 等级=L2" in digest
    assert "[搜索摘要] 摘要 40" in digest
    assert "原文开头 40" in digest
    assert "原文结尾 40" in digest
    assert len(digest) <= 50_000


def test_evidence_digest_samples_large_inventory_across_groups():
    evidence = [
        _evidence(
            index,
            role="authority" if index % 17 == 0 else "independent",
            lang="en" if index % 19 == 0 else "zh",
        )
        for index in range(1, 151)
    ]

    digest = build_evidence_digest(evidence)

    assert f"均衡选取 {EVIDENCE_CONTEXT_MAX_ITEMS} / 150 条证据" in digest
    assert "角色=authority" in digest
    assert "语言=en" in digest
    assert "E150" in digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("analyst_a", "权威通报、当事方回应及其明确日期"),
        ("analyst_b", "首发、回应、独立采编与转载"),
        ("analyst_c", "历史对照"),
    ],
)
async def test_plan_prompt_is_role_specific_and_rejects_fake_population_metrics(role, expected):
    gateway = StaticGateway({"queries": [{"query": "测试", "language": "zh", "region": "CN"}]})
    agent = OpenAIInvestigationAgent(gateway, "system", role=role)

    await agent.plan("测试事件")

    prompt = gateway.calls[0][0][2]
    assert expected in prompt
    assert "不得规划主观情感比例、全网声量估计" in prompt
    assert "避免只生成泛化的 latest/news 查询" in prompt


@pytest.mark.asyncio
async def test_summarize_prompt_uses_existing_claims_as_novelty_guard_not_fuzzy_drop_rule():
    gateway = StaticGateway({"claims": []})
    agent = OpenAIInvestigationAgent(gateway, "system", role="analyst_a")

    await agent.summarize(
        "测试事件\n【已有陈述】\n- C001: 校方已发布情况说明。",
        [_evidence(1)],
    )

    prompt = gateway.calls[0][0][2]
    assert "C001: 校方已发布情况说明" in prompt
    assert "不得仅换同义词重复" in prompt
    assert "新数字及其口径" in prompt
    assert "含新数字、否定关系或主体差异的陈述不得因主题相近而省略" in prompt
