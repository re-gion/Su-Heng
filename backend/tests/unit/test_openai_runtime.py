from types import SimpleNamespace

import pytest

from yuqing.agents.openai_runtime import (
    EVIDENCE_CONTEXT_MAX_ITEMS,
    OpenAIInvestigationAgent,
    build_evidence_digest,
)
from yuqing.core.llm.gateway import LLMOutputTruncated


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
        ("analyst_c", "独立事件"),
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


@pytest.mark.asyncio
async def test_summarize_recovers_claims_when_large_inventory_returns_empty_object():
    class SizeSensitiveGateway:
        def __init__(self):
            self.calls = []

        async def complete_json(self, _role, _system, prompt, **_kwargs):
            self.calls.append(prompt)
            ids = [f"E{index:03d}" for index in range(1, 17) if f"E{index:03d} |" in prompt]
            if len(ids) > 4:
                return {}
            return {
                "claims": [
                    {"text": f"来源 {item} 记录了相关事件。", "evidence_ids": [item]}
                    for item in ids[:2]
                ]
            }

    gateway = SizeSensitiveGateway()
    agent = OpenAIInvestigationAgent(gateway, "system", role="analyst_a")

    claims = await agent.summarize("测试事件", [_evidence(index) for index in range(1, 17)])

    assert len(gateway.calls) > 1
    assert len(claims) >= 4
    assert all(claim.evidence_ids for claim in claims)


@pytest.mark.asyncio
async def test_summarize_keeps_other_batches_when_one_output_is_truncated():
    class TruncatingGateway:
        async def complete_json(self, _role, _system, prompt, **_kwargs):
            if "E001 |" in prompt:
                raise LLMOutputTruncated("输出已截断")
            return {"claims": [{"text": "第二批的事实", "evidence_ids": ["E005"]}]}

    agent = OpenAIInvestigationAgent(TruncatingGateway(), "system", role="analyst_a")

    claims = await agent.summarize("测试事件", [_evidence(index) for index in range(1, 9)])

    assert [claim.text for claim in claims] == ["第二批的事实"]


@pytest.mark.asyncio
async def test_summarize_repairs_compound_claim_before_it_enters_verification():
    class CompoundGateway:
        def __init__(self):
            self.prompts = []

        async def complete_json(self, _role, _system, prompt, **_kwargs):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return {
                    "claims": [
                        {
                            "text": "校方发布情况说明；法院随后作出判决；媒体又报道了复核结果。",
                            "evidence_ids": ["E001"],
                        }
                    ]
                }
            return {"claims": [{"text": "校方发布了情况说明。", "evidence_ids": ["E001"]}]}

    gateway = CompoundGateway()
    agent = OpenAIInvestigationAgent(gateway, "system", role="analyst_a")

    claims = await agent.summarize("测试事件", [_evidence(1)])

    assert [claim.text for claim in claims] == ["校方发布了情况说明。"]
    assert len(gateway.prompts) == 2
    assert "单一" in gateway.prompts[1]


@pytest.mark.asyncio
async def test_summarize_separates_notice_content_from_signature_before_verification():
    class Gateway:
        calls = 0

        async def complete_json(self, _role, _system, prompt, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "claims": [
                        {
                            "text": "武汉大学发布通报，称给予学生记过处分，通报落款为学生工作部。",
                            "evidence_ids": ["E001"],
                        }
                    ]
                }
            return {
                "claims": [
                    {"text": "武汉大学发布通报，称给予学生记过处分。", "evidence_ids": ["E001"]},
                    {"text": "处理通报落款为学生工作部。", "evidence_ids": ["E001"]},
                ]
            }

    gateway = Gateway()
    claims = await OpenAIInvestigationAgent(gateway, "system").summarize("测试事件", [_evidence(1)])
    assert gateway.calls == 2
    assert [claim.text for claim in claims] == [
        "武汉大学发布通报，称给予学生记过处分。",
        "处理通报落款为学生工作部。",
    ]


@pytest.mark.asyncio
async def test_reflection_truncation_keeps_agent_in_safe_fallback():
    class TruncatingGateway:
        async def complete_json(self, *_args, **_kwargs):
            raise LLMOutputTruncated("输出已截断")

    agent = OpenAIInvestigationAgent(TruncatingGateway(), "system", role="analyst_a")

    reflection = await agent.reflect("测试事件", [])

    assert reflection.should_continue is False
    assert "结构化输出失败" in reflection.reason
