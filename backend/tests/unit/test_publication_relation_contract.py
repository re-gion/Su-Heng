import copy
import json
from types import SimpleNamespace

import pytest

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.fetch.builtin import extract_page_publication_identity
from yuqing.core.publication_relations import prune_relation_conflicts, publication_identity
from yuqing.render.html import render_html
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.investigation_scope import InvestigationScope


def sources(quote):
    return [
        {
            "evidence_ref": "E001",
            "title": "甲大学食堂事件调查",
            "publisher": "甲新闻网",
            "source_name": "甲新闻网",
            "url": "https://news.example/original",
            "published_at": "2023-06-04",
            "excerpt_windows": ["甲大学公布调查进展。" * 8],
        },
        {
            "evidence_ref": "E002",
            "title": "甲大学食堂事件后续报道",
            "publisher": "乙新闻网",
            "source_name": "乙新闻网",
            "url": "https://other.example/follow-up",
            "published_at": "2023-06-08",
            "excerpt_windows": [quote, "乙新闻网记者进行独立采访。"],
        },
    ]


def edge(quote, relation):
    return {
        "from_evidence_id": "E001",
        "to_evidence_id": "E002",
        "support_evidence_id": "E002",
        "quote": quote,
        "relation": relation,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["follow_up", "response"])
async def test_later_material_is_the_target_even_when_draft_reverses_edge(relation):
    quote = "乙新闻网跟进甲新闻网6月4日的调查，公布对涉事食堂的回应。"
    later = edge(quote, relation)
    reversed_edge = {**later, "from_evidence_id": "E002", "to_evidence_id": "E001"}

    class Gateway:
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "verifier":
                body = json.loads(prompt.split("\n", 1)[1])
                assert body["edge"] == later
                return {"accepted": True}
            return {"edges": [reversed_edge]}

    reporter = OpenAIReportAgent(Gateway(), "fixture")
    pair = sources(quote)
    assert reporter._relation_structure_codes(reversed_edge, {s["evidence_ref"]: s for s in pair})
    [published] = await reporter.recover_relations(pair)
    assert published["from_evidence_id"] == "E001"
    assert published["to_evidence_id"] == published["support_evidence_id"] == "E002"


@pytest.mark.asyncio
@pytest.mark.parametrize("different_carrier", [True, False])
async def test_repost_review_checks_carrier_even_when_editorial_attribution_matches(
    different_carrier,
):
    credit = "来源：示例新闻网"
    pair = sources(credit)
    original, carrier = pair
    original.update(
        url="https://www.example.cn/article/1",
        publisher="示例新闻网",
        source_name="示例新闻网",
        published_at="2025-09-20T09:53:21",
        excerpt_windows=["调查复核事项已经公开，后续情况将由主管机构发布。" * 20],
    )
    carrier.update(
        url="https://www.carrier.cn/news/9"
        if different_carrier
        else "https://wap.example.cn/news/9",
        title=original["title"],
        publisher="示例新闻网",
        platform="另家媒体" if different_carrier else "示例新闻网",
        identity_status="credited_source",
        published_at="2025-09-20T10:22:50",
        excerpt_windows=[credit, *original["excerpt_windows"]],
    )

    class Gateway:
        reviews = []

        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "reporter":
                return {"edges": [edge(credit, "repost")]}
            body = json.loads(prompt.split("\n", 1)[1])
            self.reviews.append(body)
            assert body["publication_identity"]["from"]["carrier"] == "示例新闻网"
            assert body["publication_identity"]["to"]["carrier"] == "另家媒体"
            return {"accepted": len(self.reviews) == 2, "reason": "初审混淆两端稿源和刊载方"}

    gateway = Gateway()
    result = await OpenAIReportAgent(gateway, "fixture").recover_relations(pair)
    if different_carrier:
        assert len(gateway.reviews) == 2
        assert result and result[0]["review_status"] == "accepted"
    else:
        assert not gateway.reviews
        assert not result


@pytest.mark.asyncio
async def test_signed_repost_pairs_the_publisher_with_its_carrier_not_another_reprint():
    quote = "文章来源：甲新闻网"
    examples = sources(quote)
    original, target = examples
    target.update(
        title=original["title"],
        publisher="甲新闻网",
        source_name="甲新闻网",
        platform="乙报",
        identity_status="credited_source",
    )
    target["excerpt_windows"].append(original["excerpt_windows"][0])
    composite = {**target, "evidence_ref": "E003", "url": "https://third.example/composite"}
    composite["excerpt_windows"] = [
        "来源：甲新闻网综合自发行机构、其他媒体等",
        original["excerpt_windows"][0],
    ]

    class Gateway:
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "verifier":
                return {"accepted": True}
            return {"edges": []}

    agent = OpenAIReportAgent(Gateway(), "fixture")
    result = await agent.recover_relations([composite, target, original])
    assert [(item["from_evidence_id"], item["to_evidence_id"]) for item in result] == [
        ("E001", "E002")
    ]


@pytest.mark.asyncio
async def test_explicit_statement_citation_survives_mirrors_and_empty_model_draft():
    passage = "针对网络反映的制动安全问题，三个主管部门在前期调查工作基础上，联合成立调查组开展核查并依法依规处理，有关情况将及时公布。"

    def source(ref, host, path, publisher, text, credits=(), role="independent"):
        return SimpleNamespace(
            local_id=ref,
            url=f"https://{host}/{path}",
            title="三部门联合调查",
            fetch_status="fetched",
            published_at="2026-10-16",
            publisher_entity=publisher,
            source_name=publisher,
            source_tier=2,
            source_role=role,
            content_text=text,
            snippet="",
            provider="fetch",
            lang="zh",
            extra={"page_source_credits": list(credits)},
        )

    credits = ("来源：装备工业一司、工信微报",)
    original = source(
        "E001", "www.gov.test", "article/1827", "主管部门", passage, credits, "authority"
    )
    mirror = source(
        "E002", "wap.gov.test", "article/1827", "主管部门网站", passage, credits, "authority"
    )
    quote = "据工信微报15日消息，" + passage
    reporting = source("E003", "media.test", "stories/74", "日报", quote)
    reporting.extra.update(scope_status="history", date_provenance="page_metadata")
    unrelated = source(
        "E004", "media.test", "stories/95", "日报", "据工信微报15日消息，另一项调查已经结束。"
    )
    unrelated.extra.update(scope_status="history", date_provenance="page_metadata")
    selected = {"E001": {}, "E002": {}}
    scope = InvestigationScope("制动安全问题", ("zh",), date_from="2026-10-01")
    items = FullReportBuilder._relation_source_context(
        [original, mirror], selected, evidence=[reporting, unrelated], scope=scope
    )
    assert {item["evidence_ref"] for item in items} == {"E001", "E003"}

    class Gateway:
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "verifier":
                assert passage in prompt
                return {"accepted": True}
            return {
                "edges": [
                    edge(quote, "repost")
                    | {"to_evidence_id": "E003", "support_evidence_id": "E003"}
                ]
            }

    edges = await OpenAIReportAgent(Gateway(), "fixture").recover_relations(items)
    assert [
        (item["from_evidence_id"], item["to_evidence_id"], item["relation"]) for item in edges
    ] == [("E001", "E003", "follow_up")]


@pytest.mark.asyncio
async def test_inline_reference_is_not_published_as_whole_article_repost():
    quote = "另据甲新闻网6月4日报道，甲大学公布调查进展。"

    class Gateway:
        drafts = 0
        reviewed = []

        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "verifier":
                self.reviewed.append(prompt)
                return {"accepted": True}
            self.drafts += 1
            return {"edges": [edge(quote, "repost" if self.drafts == 1 else "follow_up")]}

    gateway = Gateway()
    result = await OpenAIReportAgent(gateway, "fixture").recover_relations(sources(quote))
    assert len(result) == 1 and result[0]["relation"] == "follow_up"
    assert gateway.drafts == 2
    assert len(gateway.reviewed) == 1


@pytest.mark.asyncio
async def test_same_source_credit_does_not_create_both_repost_and_follow_up_edges():
    quote = "本文来源：甲新闻网"

    class Gateway:
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "verifier":
                return {"accepted": True}
            return {"edges": [edge(quote, "repost"), edge(quote, "follow_up")]}

    result = await OpenAIReportAgent(Gateway(), "fixture").recover_relations(sources(quote))
    assert len(result) == 1
    assert result[0]["relation"] == "repost"


def test_platform_upload_is_not_published_under_the_carrier_name():
    body = (
        "甲大学食堂事件调查。\n文丨乙新闻记者 张甲\n"
        "特别声明：本文为甲报新媒体平台作者上传并发布，仅代表作者观点。"
        "甲报仅提供信息发布平台。"
    )
    source = SimpleNamespace(
        title="甲大学食堂事件调查",
        content_text=body,
        snippet="",
        published_at="2023-06-08",
        publisher_entity="甲报",
        source_name="甲报",
        source_domain="platform.example",
        extra={},
    )
    claim = SimpleNamespace(
        agent="media_propagation",
        local_id="C001",
        text="甲大学食堂事件调查",
        analysis_data={
            "publication_node": {
                "evidence_id": "E002",
                "publisher": "甲报",
                "node_type": "independent",
            }
        },
    )
    network, _, _ = FullReportBuilder._publication_network(
        [claim],
        {"E002": source},
        {"E002"},
        {"C001": {"E002"}},
        {"items": []},
        "甲大学食堂异物事件",
    )
    assert network["nodes"][0]["publisher"] == "乙新闻"
    assert network["nodes"][0]["platform"] == "甲报"


def test_conflicting_types_for_overlapping_same_quote_require_fresh_review():
    quote = "另据甲新闻网6月4日报道，甲大学公布调查进展。"
    accepted = [edge(quote, "repost"), edge(quote[:15], "follow_up")]
    survivors, rejected = prune_relation_conflicts(accepted)
    assert survivors == []
    assert len(rejected) == 2


def test_platform_identity_can_be_preserved_when_readability_omits_the_footer():
    raw = (
        "<article><p>甲大学食堂事件调查。</p><p>文丨乙新闻记者 张甲</p>"
        "<p>特别声明：本文由作者上传。甲报仅提供信息发布平台。</p></article>"
    )
    identity = extract_page_publication_identity(raw)
    source = {
        "source_name": "甲报",
        "content_text": "甲大学食堂事件调查。",
        "extra": {"page_publication_identity": identity},
    }
    result = publication_identity(source)
    assert result["publisher"] == "乙新闻"
    assert result["platform"] == "甲报"
    unknown = publication_identity(
        {"source_name": "甲报", "content_text": "特别声明：甲报仅提供信息发布平台。"}
    )
    assert unknown["publisher"] == "作者身份待核"
    assert unknown["platform"] == "甲报"


def test_explicit_document_credit_separates_newsroom_from_government_carrier():
    source = {
        "publisher_entity": "监管机构",
        "source_name": "监管机构",
        "content_text": "召回报道。",
        "extra": {"page_source_credits": ["信息来源：甲新闻网"]},
    }
    identity = publication_identity(source)
    assert identity["publisher"] == "甲新闻网"
    assert identity["platform"] == "监管机构"


@pytest.mark.parametrize("connected", [False, True])
def test_optional_platform_identity_is_escaped_in_both_network_views(connected):
    block = {
        "type": "propagation_network",
        "block_id": "network",
        "section": "04",
        "nodes": [
            {"evidence_id": "E001", "publisher": "原稿机构", "evidence_refs": ["E001"]},
            {
                "evidence_id": "E002",
                "publisher": "作者机构",
                "platform": "<b>载体</b>",
                "evidence_refs": ["E002"],
            },
        ],
        "edges": [
            {
                **edge("据原稿机构报道，甲大学公布调查进展。", "follow_up"),
                "review_status": "accepted",
                "evidence_refs": ["E002"],
            }
        ]
        if connected
        else [],
    }
    original = copy.deepcopy(block)
    html = render_html({"blocks": [block]})
    assert "刊载平台：&lt;b&gt;载体&lt;/b&gt;" in html
    assert "刊载平台：<b>" not in html
    assert block == original
    if connected:
        assert "引述/跟进" in html and "不表示整篇稿件转载" in html
    block["nodes"][1].pop("platform")
    assert "刊载平台：" not in render_html({"blocks": [block]})
