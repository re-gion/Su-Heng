import copy
import json
from types import SimpleNamespace

import pytest

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.llm.gateway import LLMOutputTruncated
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.historical_data import HotSnapshotPoint
from yuqing.services.report_analysis import (
    assemble_analysis,
    deduplicate_limitations,
    event_timeline,
    report_context,
    select_priority_timeline_nodes,
)


def inputs():
    quote = "校方通报本次收到有效诉求120条，已交由工作组核查。"
    source = SimpleNamespace(
        local_id="E001",
        title="校方通报",
        source_name="校方",
        publisher_entity="校方",
        source_domain="example.edu",
        source_role="party",
        source_tier=2,
        fetch_status="fetched",
        content_text=quote,
        snippet="摘要省略具体数字",
        published_at="2026-09-01",
    )
    fact = {
        "claim_ref": "C001",
        "text": quote,
        "badge": "unverified",
        "verification_state": "complete",
        "origin_agent": "fact_investigator",
        "citations": [{"evidence_ref": "E001", "relation": "support", "quote": quote}],
    }
    analysis = {
        "section": "07",
        "title": "核查期间保持程序透明",
        "claim_refs": ["C001"],
        "interpretation": "若诉求正在核查，公布处理进度有助于减少信息真空。",
        "implication": "沟通重点应包括核查步骤与反馈渠道。",
        "action": "发布已受理事项的处理流程和反馈入口",
        "owner": "负责对外沟通与诉求受理的部门",
        "trigger": "核查进度发生变化时",
        "uncertainty": "尚无材料证明诉求已经解决。",
    }
    measurement = {
        "evidence_ref": "E001",
        "claim_refs": ["C001"],
        "quote": quote,
        "label": "有效诉求",
        "value_text": "120条",
    }
    return (
        [fact],
        [source],
        {"summary_claim_refs": ["C001"], "analyses": [analysis], "measurements": [measurement]},
    )


def test_long_timeline_keeps_user_focus_and_surrounding_context():
    items = [
        {"date": f"2024-{month:02d}-01", "window_label": "重点窗口前的本事件经过"}
        for month in range(1, 5)
    ]
    items += [{"date": f"2025-{month:02d}-01", "window_label": None} for month in (6, 7)]
    items += [
        {"date": f"2026-{month:02d}-01", "window_label": "重点窗口后的本事件进展"}
        for month in range(1, 9)
    ]
    selected = select_priority_timeline_nodes(items)
    assert len(selected) == 12
    assert {"2025-06-01", "2025-07-01"} <= {item["date"] for item in selected}
    assert any(item["window_label"] == "重点窗口前的本事件经过" for item in selected)
    assert any(item["window_label"] == "重点窗口后的本事件进展" for item in selected)


def test_observation_and_measurement_are_grounded_and_unverified_is_visible():
    facts, sources, draft = inputs()
    draft["analyses"][0]["observation"] = "模型偷偷改成已处理完毕"
    summary, blocks, quality = assemble_analysis(draft, facts, sources)
    analysis = next(b for b in blocks if b["type"] == "analysis")["items"][0]
    assert summary["what"][0]["text"] == facts[0]["text"]
    assert analysis["observation"] == facts[0]["text"]
    assert "尚未全部证实" in analysis["uncertainty"]
    metric = next(b for b in blocks if b["type"] == "metric_cards")["items"][0]
    assert metric["value"] == "120条"
    assert metric["scope"] == sources[0].content_text
    assert "未独立复算" in metric["verification_note"]
    assert quality["status"] == "evidence_brief"  # 只有建议，没有议题/传播分析。


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ({"claim_refs": ["C999"]}, "analysis_reference"),
        ({"evidence_refs": ["E999"]}, "analysis_binding"),
        ({"interpretation": "次生风险概率为58%"}, "unsupported_number"),
        ({"owner": ""}, "analysis_incomplete"),
        ({"section": "01"}, "analysis_reference"),
    ],
)
def test_invalid_analysis_is_removed_with_auditable_reason(mutation, reason):
    facts, sources, draft = inputs()
    draft["analyses"][0].update(mutation)
    _, blocks, quality = assemble_analysis(draft, facts, sources)
    assert not any(b["type"] == "analysis" for b in blocks)
    assert quality["rejected_items"][reason] == 1


@pytest.mark.parametrize(
    "case", ["snippet", "invented_quote", "unbound", "recommendation_stream", "unit_change"]
)
def test_measurement_cannot_be_promoted_from_unsupported_or_unrelated_content(case):
    facts, sources, draft = inputs()
    item = draft["measurements"][0]
    if case == "snippet":
        sources[0].fetch_status = "discovered"
    elif case == "invented_quote":
        item["quote"] += "所有诉求均已解决。"
    elif case == "unbound":
        facts[0]["citations"][0]["evidence_ref"] = "E002"
    elif case == "recommendation_stream":
        sources[0].content_text += "延伸阅读：比赛参与人数有300人。"
        item.update(value_text="300人", label="参与人数", quote="比赛参与人数有300人。")
    else:
        item["value_text"] = "120万条"
    _, blocks, quality = assemble_analysis(draft, facts, sources)
    assert not any(b["type"] == "metric_cards" for b in blocks)
    assert quality["rejected_items"]["measurement_not_grounded"] == 1


def test_duplicates_and_authority_overrides_do_not_inflate_report():
    facts, sources, draft = inputs()
    draft["analyses"] *= 2
    draft["metrics"] = {"verified_rate": 1}
    _, blocks, quality = assemble_analysis(draft, facts, sources)
    assert len(next(b for b in blocks if b["type"] == "analysis")["items"]) == 1
    assert quality["rejected_items"] == {"authoritative_override": 1, "duplicate_analysis": 1}
    limits = [{"id": "L01", "text": "预算不足"}, {"id": "L02", "text": "预算不足"}]
    assert deduplicate_limitations(limits) == [limits[0]]


def test_report_context_contains_raw_numbers_metadata_and_later_evidence():
    facts, sources, _ = inputs()
    later = copy.copy(sources[0])
    later.local_id = "E999"
    later.title = "后续纠偏"
    context = report_context({}, facts, sources + [later], [])
    assert context["sources"][-1]["evidence_ref"] == "E999"
    assert "120条" in context["sources"][0]["measurement_passages"][0]
    assert context["sources"][0]["source_role"] == "party"


@pytest.mark.asyncio
async def test_live_report_agent_requires_semantic_review_and_fails_closed():
    facts, sources, draft = inputs()

    class Gateway:
        def __init__(self):
            self.roles = []

        async def complete_json(self, role, _system, _user, **kwargs):
            self.roles.append(role)
            if role == "reporter":
                assert kwargs["max_tokens"] >= 6000
                return copy.deepcopy(draft)
            raise ConnectionError("private upstream response must not leak")

    gateway = Gateway()
    result = await OpenAIReportAgent(gateway, "system").enrich(
        report_context({}, facts, sources, [])
    )
    assert gateway.roles.count("reporter") == 3
    assert gateway.roles.count("verifier") == 1
    assert result["analyses"] == []
    assert result["measurements"]
    assert result["analysis_review"]["status"] == "unavailable"
    assert "private" not in str(result)


@pytest.mark.asyncio
async def test_empty_reporter_json_is_rejected_for_each_chapter():
    facts, sources, _ = inputs()

    class Gateway:
        def __init__(self):
            self.roles = []

        async def complete_json(self, role, *_args, **_kwargs):
            self.roles.append(role)
            return {}

    gateway = Gateway()
    result = await OpenAIReportAgent(gateway, "system").enrich(
        report_context({}, facts, sources, [])
    )
    assert gateway.roles == ["reporter"] * 3
    assert result["analyses"] == []
    assert result["analysis_review"]["status"] == "unavailable"
    assert len(result["section_warnings"]) == 3
    assert all("ValueError" in warning for warning in result["section_warnings"])


@pytest.mark.asyncio
async def test_reporter_prioritizes_actions_and_bounds_each_chapter_context():
    class Gateway:
        def __init__(self):
            self.prompts = []

        async def complete_json(self, role, _system, user, **_kwargs):
            assert role == "reporter"
            self.prompts.append(user)
            return {"analyses": [], "summary_claim_refs": [], "measurements": []}

    gateway = Gateway()
    context = {
        "task": {"event_query": "机构公开事件"},
        "facts": [
            {
                "claim_ref": f"C{i:03d}",
                "text": "机构在通报中说明处置决定。",
                "badge": "verified" if i == 1 else "unverified",
                "verification_state": "complete",
                "origin_agent": "fact_investigator",
                "citations": [{"evidence_ref": f"E{i:03d}", "relation": "support"}],
            }
            for i in range(1, 33)
        ],
        "sources": [
            {"evidence_ref": f"E{i:03d}", "excerpt": "公开通报正文" * 300} for i in range(1, 33)
        ],
        "open_questions": ["处置进度需持续核查。"],
    }

    await OpenAIReportAgent(gateway, "system").enrich(context)

    assert "第07章" in gateway.prompts[0]
    assert all(len(prompt) < 25_000 for prompt in gateway.prompts)


@pytest.mark.asyncio
async def test_truncated_action_chapter_retries_with_smaller_context():
    facts, sources, draft = inputs()
    prompts = []

    class Gateway:
        async def complete_json(self, role, _system, user, **_kwargs):
            if role == "verifier":
                return {"accepted_indexes": [0], "rejections": []}
            if "第07章" in user:
                prompts.append(user)
                if len(prompts) == 1:
                    raise LLMOutputTruncated("模型输出达到长度上限")
                return copy.deepcopy(draft)
            return {"analyses": [], "summary_claim_refs": [], "measurements": []}

    context = report_context({}, facts, sources, [])
    context["sources"][0]["excerpt"] *= 100
    result = await OpenAIReportAgent(Gateway(), "system").enrich(context)
    assert len(prompts) == 2
    assert len(prompts[1]) < len(prompts[0])
    assert result["analyses"]
    assert not any("07" in warning for warning in result["section_warnings"])


@pytest.mark.asyncio
async def test_reporter_rejects_claim_that_full_source_lacks_text_when_only_preview_is_given():
    class Gateway:
        async def complete_json(self, *_args, **_kwargs):
            raise AssertionError("确定性拒绝后不应调用模型审查")

    result = await OpenAIReportAgent(Gateway(), "system")._review(
        {
            "facts": [{"claim_ref": "C001", "text": "校方公布调查进度。"}],
            "sources": [
                {"evidence_ref": "E001", "excerpt": "通报前段", "excerpt_is_preview": True}
            ],
        },
        {
            "analyses": [
                {
                    "section": "07",
                    "title": "通报节选未含最终结论",
                    "claim_refs": ["C001"],
                    "interpretation": "原网页缺少复核结论。",
                }
            ]
        },
    )

    assert result["analyses"] == []
    assert result["analysis_review"]["rejected"] == 1
    assert "预览" in result["analysis_review"]["reasons"][0]["reason"]


@pytest.mark.asyncio
async def test_reporter_does_not_turn_internal_preview_into_external_action():
    class Gateway:
        async def complete_json(self, *_args, **_kwargs):
            raise AssertionError("确定性拒绝后不应调用模型审查")

    result = await OpenAIReportAgent(Gateway(), "system")._review(
        {
            "facts": [{"claim_ref": "C001", "text": "校方公布调查进度。"}],
            "sources": [
                {"evidence_ref": "E001", "excerpt": "通报前段", "excerpt_is_preview": True}
            ],
        },
        {
            "analyses": [
                {
                    "section": "07",
                    "title": "核对公开材料",
                    "claim_refs": ["C001"],
                    "action": "要求校方核对通报全文与本报告预览的差异。",
                    "uncertainty": "本报告抓取的文本是预览节选。",
                }
            ]
        },
    )
    assert result["analyses"] == []
    assert result["analysis_review"]["rejected"] == 1


@pytest.mark.asyncio
async def test_one_chapter_failure_keeps_other_chapter_analysis_and_measurements():
    facts, sources, template = inputs()

    class Gateway:
        async def complete_json(self, role, _system, user, **_kwargs):
            if role == "verifier":
                return {"accepted_indexes": [0]}
            if "第05章" in user:
                raise RuntimeError("chapter unavailable")
            if "第04章" in user:
                return {
                    "analyses": [],
                    "measurements": copy.deepcopy(template["measurements"]),
                }
            if "第07章" in user:
                return {"analyses": copy.deepcopy(template["analyses"])}
            return {"analyses": []}

    result = await OpenAIReportAgent(Gateway(), "system").enrich(
        report_context({}, facts, sources, [])
    )
    assert [item["section"] for item in result["analyses"]] == ["07"]
    assert result["measurements"] == template["measurements"]
    assert result["analysis_review"]["chapters"]["07"]["status"] == "complete"
    assert any("05" in warning for warning in result["section_warnings"])


@pytest.mark.asyncio
async def test_repaired_analysis_is_reviewed_again_with_only_referenced_facts():
    facts, sources, template = inputs()
    unused = copy.deepcopy(facts[0])
    unused["claim_ref"] = "C999"
    unused["text"] = "未引用且不应进入审查的陈述"
    review_facts = []
    calls = []

    class Gateway:
        async def complete_json(self, role, _system, user, **_kwargs):
            calls.append(role)
            if role == "verifier":
                payload = json.loads(user)
                assert len(payload["candidates"]) == 1
                assert payload["candidates"][0]["index"] == 0
                assert payload["candidates"][0]["analysis"]["claim_refs"] == ["C001"]
                assert "facts" not in payload
                review_facts.append(list(payload["facts_by_ref"]))
                if len(review_facts) == 1:
                    return {
                        "accepted_indexes": [],
                        "rejections": [{"index": 0, "reason": "改为条件判断"}],
                    }
                return {"accepted_indexes": [0]}
            if user.startswith("修订下列"):
                repaired = copy.deepcopy(template["analyses"][0])
                repaired["interpretation"] = "若所引陈述成立，仍需继续观察处理进度。"
                return {"analyses": [repaired]}
            if "第07章" in user:
                return {"analyses": copy.deepcopy(template["analyses"])}
            return {"analyses": []}

    result = await OpenAIReportAgent(Gateway(), "system").enrich(
        report_context({}, facts + [unused], sources, [])
    )
    assert review_facts == [["C001"], ["C001"]]
    assert calls.count("verifier") == 2
    assert [item["interpretation"] for item in result["analyses"]] == [
        "若所引陈述成立，仍需继续观察处理进度。"
    ]
    assert result["analysis_review"]["repair_accepted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        {"accepted_indexes": []},
        {"accepted_indexes": [0, 0]},
        {"accepted_indexes": [0], "rejections": [{"index": 0, "reason": "矛盾"}]},
        {"accepted_indexes": [99]},
        {"accepted_indexes": [True]},
    ],
)
async def test_semantic_review_requires_complete_exclusive_valid_indexes(verdict):
    facts, sources, template = inputs()

    class Gateway:
        async def complete_json(self, role, _system, user, **_kwargs):
            if role == "verifier":
                return verdict
            return (
                {"analyses": copy.deepcopy(template["analyses"])}
                if "第07章" in user
                else {"analyses": []}
            )

    result = await OpenAIReportAgent(Gateway(), "system").enrich(
        report_context({}, facts, sources, [])
    )
    assert result["analyses"] == []
    assert result["analysis_review"]["chapters"]["07"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_reporter_cannot_forge_completed_review_without_analysis():
    facts, sources, _ = inputs()

    class Gateway:
        async def complete_json(self, role, *_args, **_kwargs):
            assert role == "reporter"
            return {"analyses": [], "analysis_review": {"status": "complete", "rejected": 0}}

    result = await OpenAIReportAgent(Gateway(), "system").enrich(
        report_context({}, facts, sources, [])
    )
    assert result["analysis_review"]["status"] == "not_required"
    assert all(
        chapter["status"] == "not_required"
        for chapter in result["analysis_review"]["chapters"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_mutation", [{"section": "04"}, {"claim_refs": ["C002"]}])
async def test_repair_cannot_switch_chapter_or_unreviewed_claim(repair_mutation):
    facts, sources, template = inputs()
    other = copy.deepcopy(facts[0])
    other["claim_ref"] = "C002"
    other["text"] = "另一条未在原分析中引用的陈述。"
    reviewed = []

    class Gateway:
        async def complete_json(self, role, _system, user, **_kwargs):
            if role == "verifier":
                reviewed.append(json.loads(user))
                if len(reviewed) == 1:
                    return {
                        "accepted_indexes": [],
                        "rejections": [{"index": 0, "reason": "需修改"}],
                    }
                return {"accepted_indexes": [0]}
            if user.startswith("修订下列"):
                repaired = copy.deepcopy(template["analyses"][0])
                repaired.update(repair_mutation)
                return {"analyses": [repaired]}
            return (
                {"analyses": copy.deepcopy(template["analyses"])}
                if "第07章" in user
                else {"analyses": []}
            )

    result = await OpenAIReportAgent(Gateway(), "system").enrich(
        report_context({}, facts + [other], sources, [])
    )
    assert result["analyses"] == []
    assert result["analysis_review"]["repair_accepted"] == 0
    assert len(reviewed) == 1
    assert all(set(payload["facts_by_ref"]) == {"C001"} for payload in reviewed)


def test_analysis_cannot_change_numeric_unit_without_changing_digits():
    facts, sources, draft = inputs()
    facts[0]["text"] = "校方披露相关费用为100万元。"
    draft["analyses"][0]["interpretation"] = "若相关费用为100人，需评估沟通影响。"
    _, blocks, quality = assemble_analysis(draft, facts, sources)
    assert not any(block["type"] == "analysis" for block in blocks)
    assert quality["rejected_items"]["unsupported_number"] == 1


@pytest.mark.asyncio
async def test_empty_chapter_review_retries_each_item_once_and_keeps_success():
    facts, sources, draft = inputs()
    second = copy.deepcopy(draft["analyses"][0])
    second["title"] = "第二条"
    draft["analyses"].append(second)
    calls = []

    class Gateway:
        async def complete_json(self, role, _system, user, **_kwargs):
            payload = json.loads(user)
            calls.append(payload)
            candidates = payload["candidates"]
            if len(candidates) == 1 and candidates[0]["analysis"]["title"] != "第二条":
                return {"accepted_indexes": [0], "rejections": []}
            return {}

    result = await OpenAIReportAgent(Gateway(), "system")._review(
        report_context({}, facts, sources, []), draft
    )
    assert len(calls) == 3
    assert len(result["analyses"]) == 1
    assert result["analysis_review"]["status"] == "partial"
    assert result["analysis_review"]["split_review"] is True


@pytest.mark.asyncio
async def test_semantic_reviewer_cannot_add_analyses_or_accept_invalid_indexes():
    facts, sources, draft = inputs()

    class Gateway:
        async def complete_json(self, role, *_args, **_kwargs):
            return (
                copy.deepcopy(draft)
                if role == "reporter"
                else {"accepted_indexes": [True, -1, 99, "0"]}
            )

    result = await OpenAIReportAgent(Gateway(), "system").enrich(
        report_context({}, facts, sources, [])
    )
    assert result["analyses"] == []


def test_hot_charts_do_not_connect_platforms_topics_or_single_points():
    points = [
        HotSnapshotPoint(
            platform=platform, title=title, captured_at=date, rank=1, heat_value=value, url=None
        )
        for platform, title, date, value in [
            ("weibo", "事件A", "2026-09-01T00:00:00+08:00", 10),
            ("weibo", "事件A", "2026-09-02T00:00:00+08:00", 20),
            ("weibo", "事件B", "2026-09-02T00:00:00+08:00", 900),
            ("other", "事件A", "2026-09-02T00:00:00+08:00", 8),
        ]
    ]
    chart = FullReportBuilder._hot_chart(points)
    assert chart["chart_kind"] == "series"
    assert len(chart["series"]) == 3
    assert sorted(s["chart_kind"] for s in chart["series"]) == ["bar", "bar", "line"]


def test_mixed_timezone_metadata_does_not_crash_report():
    evidence = [
        SimpleNamespace(published_at=t)
        for t in ["2026-09-01", "2026-09-03T12:00:00+08:00", "unknown"]
    ]
    assert FullReportBuilder._time_span_days(evidence) == 2


def test_event_timeline_requires_explicit_valid_start_date_and_excludes_history():
    facts = [
        {
            "claim_ref": f"C{day:03d}",
            "text": f"2026年9月{day}日，事件发生第{day}次公开更新。",
            "origin_agent": "fact_investigator",
            "badge": "verified",
            "citations": [{"evidence_ref": f"E{day:03d}"}],
        }
        for day in range(1, 16)
    ]
    facts.extend(
        [
            {
                "claim_ref": "C100",
                "text": "事件进展见后续通报；2026年9月16日发布。",
                "origin_agent": "fact_investigator",
            },
            {
                "claim_ref": "C101",
                "text": "2026年2月30日，日期无效。",
                "origin_agent": "fact_investigator",
            },
            {
                "claim_ref": "C102",
                "text": "2025年9月1日，历史案例发生。",
                "origin_agent": "history_insight",
            },
        ]
    )
    timeline = event_timeline(facts)
    assert [item["date"] for item in timeline] == [
        *(f"2026-09-{day:02d}" for day in range(1, 5)),
        *(f"2026-09-{day:02d}" for day in range(8, 16)),
    ]
    assert timeline[0]["claim_refs"] == ["C001"]
    assert timeline[-1]["evidence_refs"] == ["E015"]


def test_event_timeline_keeps_same_event_outside_priority_window_with_labels():
    facts = [
        {
            "claim_ref": f"C{index:03d}",
            "text": f"2025年{month}月1日，机构公布本事件的第{index}阶段结果。",
            "origin_agent": "fact_investigator",
            "badge": "verified",
            "citations": [{"evidence_ref": f"E{index:03d}"}],
        }
        for index, month in enumerate((5, 7, 9), start=1)
    ]
    timeline = event_timeline(facts, date_from="2025-07-01", date_to="2025-07-31")
    assert [item["date"] for item in timeline] == ["2025-05-01", "2025-07-01", "2025-09-01"]
    assert [item["window_label"] for item in timeline] == [
        "重点窗口前的本事件经过",
        None,
        "重点窗口后的本事件进展",
    ]


def test_event_timeline_reads_dated_institution_action_after_actor_name():
    facts = [
        {
            "claim_ref": "C001",
            "text": "2023年10月11日，学校成立工作组调查该事件。",
            "origin_agent": "fact_investigator",
            "badge": "unverified",
            "citations": [{"evidence_ref": "E001"}],
        },
        {
            "claim_ref": "C002",
            "text": "武汉大学2025年9月20日通报称，复核发现论文存在规范问题。",
            "origin_agent": "fact_investigator",
            "badge": "verified",
            "citations": [{"evidence_ref": "E002"}],
        },
        {
            "claim_ref": "C003",
            "text": "事件后来持续受到关注；2025年9月21日网页发布。",
            "origin_agent": "fact_investigator",
            "badge": "unverified",
            "citations": [{"evidence_ref": "E003"}],
        },
        {
            "claim_ref": "C004",
            "text": "武汉市中级人民法院于2025年9月17日就该案二审判决维持一审判决。",
            "origin_agent": "fact_investigator",
            "badge": "verified",
            "citations": [{"evidence_ref": "E004"}],
        },
    ]
    assert [item["date"] for item in event_timeline(facts)] == [
        "2023-10-11",
        "2025-09-17",
        "2025-09-20",
    ]
