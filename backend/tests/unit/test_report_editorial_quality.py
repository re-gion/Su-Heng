import json

import pytest

from yuqing.agents.comment_analysis import OpenAICommentAgent
from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.llm.gateway import LLMBudgetExhausted
from yuqing.services.report_analysis import assemble_analysis, report_context


def fact(ref="C001", source="E001"):
    return {
        "claim_ref": ref,
        "text": "校方已公开撤销处分的复核结果。",
        "badge": "unverified",
        "verdict": "support",
        "verification_state": "complete",
        "origin_agent": "fact_investigator",
        "citations": [{"evidence_ref": source, "relation": "support"}],
    }


def analysis(section="04"):
    return {
        "section": section,
        "title": "复核结果与原处分依据的解释边界",
        "claim_refs": ["C001"],
        "interpretation": "公开复核结果可以说明处置已经改变，但不足以解释原处分依据如何变化。",
        "implication": "读者应区分结果公开与认定过程公开。",
        "uncertainty": "需要原处分决定与复核报告才能判断认定范围。",
        "action": "在已有复核结果旁补充依据与认定范围的对照说明。",
        "owner": "学生工作与法务职能",
        "trigger": "收到对处分依据的具体追问时",
    }


def test_summary_uses_reviewed_headlines_instead_of_repeating_analysis():
    draft = {"analyses": [analysis(), analysis("07")]}
    draft["analyses"][1]["interpretation"] = (
        "对既有处置补充依据说明，有助于区分不同决定的认定范围。"
    )
    summary, blocks, _ = assemble_analysis(draft, [fact()], [])
    assert summary["why"][0]["text"] == draft["analyses"][0]["title"]
    assert summary["so_what"][0]["text"] == draft["analyses"][1]["title"]
    assert blocks[0]["items"][0]["interpretation"] == draft["analyses"][0]["interpretation"]
    assert summary["why"][0]["uncertainty"]


def test_reassembling_reviewed_analysis_does_not_duplicate_evidence_boundary():
    _, blocks, _ = assemble_analysis({"analyses": [analysis()]}, [fact()], [])
    reviewed = blocks[0]["items"][0]
    _, replayed, _ = assemble_analysis({"analyses": [{"section": "04", **reviewed}]}, [fact()], [])
    assert replayed[0]["items"][0]["uncertainty"] == reviewed["uncertainty"]


def test_chapter_context_retains_other_sources_after_many_same_source_facts():
    context = {
        "facts": [fact(f"C{i:03d}") for i in range(1, 19)]
        + [fact("C019", "E002"), fact("C020", "E003")],
        "sources": [
            {"evidence_ref": ref, "excerpt": "来源正文"} for ref in ("E001", "E002", "E003")
        ],
    }
    selected = OpenAIReportAgent._chapter_context(context, "07")
    assert {"C019", "C020"} <= {f["claim_ref"] for f in selected["facts"]}


def test_chapter_preserves_a_cited_passage_from_later_in_source():
    from types import SimpleNamespace

    text = "校方已经撤销处分并公布复核结果。"
    source = SimpleNamespace(
        local_id="E001",
        fetch_status="fetched",
        source_tier=2,
        extra={},
        content_text="背景材料。" * 900 + text,
        snippet="",
        title="复核通报",
        source_name="校方",
        publisher_entity="校方",
        source_domain="example.edu",
        source_role="party",
        published_at="2025-09-20",
    )
    cited = fact()
    cited["citations"][0]["quote"] = text
    selected = OpenAIReportAgent._chapter_context(report_context({}, [cited], [source], []), "07")
    assert text in selected["sources"][0]["excerpt"]
    assert selected["sources"][0]["excerpt_is_preview"]
    assert selected["facts"][0]["verdict"] == "support"


@pytest.mark.asyncio
async def test_supported_third_analysis_is_not_silently_truncated():
    class Gateway:
        async def complete_json(self, role, _system, prompt, **_kwargs):
            if role == "reporter":
                if "第07章" not in prompt:
                    return {"analyses": []}
                items = [analysis("07") for _ in range(3)]
                for i, item in enumerate(items):
                    item["title"] = ("依据解释", "申诉渠道", "个人信息保护")[i]
                return {"analyses": items}
            candidates = json.loads(prompt)["candidates"]
            return {"accepted_indexes": list(range(len(candidates))), "rejections": []}

    result = await OpenAIReportAgent(Gateway(), "system").enrich({"facts": [fact()]})
    assert len(result["analyses"]) == 3


@pytest.mark.asyncio
async def test_completed_verification_is_not_described_as_unperformed():
    class Gateway:
        async def complete_json(self, *_args, **_kwargs):
            return {"accepted_indexes": [0], "rejections": []}

    item = analysis()
    item["uncertainty"] = "上述报道均未经核验，因此只能等待进一步核实。"
    result = await OpenAIReportAgent(Gateway(), "system")._review(
        {"facts": [fact()]}, {"analyses": [item]}, allow_repair=False
    )
    assert result["analyses"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["网暴线索无人认领", "诬告与否至今未经任何程序认定"])
async def test_title_cannot_turn_missing_records_into_global_absence(title):
    class Gateway:
        async def complete_json(self, *_args, **_kwargs):
            return {"accepted_indexes": [0], "rejections": []}

    item = analysis()
    item["title"] = title
    item["uncertainty"] = "本次材料未见处理记录，不能排除已在其他渠道处理。"
    result = await OpenAIReportAgent(Gateway(), "system")._review(
        {"facts": [fact()]}, {"analyses": [item]}, allow_repair=False
    )
    assert result["analyses"] == []


@pytest.mark.asyncio
async def test_comment_classification_reserves_room_for_reviewed_theme():
    class Gateway:
        token_limit = 100_000
        tokens_used = 0

        async def complete_json(self, role, _system, prompt, **_kwargs):
            cost = 20_000 if role == "analyst_b" else 12_000
            if self.tokens_used + cost > self.token_limit:
                raise LLMBudgetExhausted("本地阶段额度不足")
            self.tokens_used += cost
            data = json.loads(prompt.split("\n", 1)[1])
            if role == "analyst_b":
                return {
                    "assignments": [
                        {"id": s["id"], "relevant": True, "issue": "机构回应", "stance": "质疑"}
                        for s in data["comments"]
                    ]
                }
            if role == "reporter":
                return {
                    "themes": [
                        {
                            "title": "解释复核依据",
                            "group_ids": [g["id"] for g in data["groups"]],
                            "interpretation": "样本关心复核依据。",
                            "response_gap": "缺少依据说明",
                            "uncertainty": "仅限已分类样本",
                        }
                    ]
                }
            return {"accepted": True}

    gateway = Gateway()
    rows = [
        {
            "id": str(i),
            "text": f"希望解释复核依据，具体问题{i}",
            "source_url": "https://example.test/post",
            "platform": "weibo",
            "evidence_ref": "E001",
        }
        for i in range(60)
    ]
    result = await OpenAICommentAgent(gateway, "system").analyze("机构复核", rows)
    assert result["items"]
    assert result["items"][0]["review_status"] == "accepted"
    assert 0 < result["coverage"]["classified"] < 60
    assert result["coverage"]["unclassified"] > 0
    assert gateway.token_limit == 100_000
    assert gateway.tokens_used <= gateway.token_limit
