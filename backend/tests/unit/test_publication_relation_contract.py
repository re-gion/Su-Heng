import copy
from types import SimpleNamespace

import pytest

from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.fetch.builtin import extract_page_publication_identity
from yuqing.core.publication_relations import prune_relation_conflicts, publication_identity
from yuqing.render.html import render_html
from yuqing.services.full_report import FullReportBuilder


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
