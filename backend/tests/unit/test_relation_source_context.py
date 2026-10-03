from types import SimpleNamespace

from yuqing.services.full_report import FullReportBuilder


def test_relation_context_includes_explicit_news_agency_credit():
    original = SimpleNamespace(
        local_id="E002",
        fetch_status="fetched",
        published_at="2025-09-20T09:00:00+08:00",
        publisher_entity="新华社",
        source_name="新华社",
        source_tier=1,
        title="调查复核情况",
        url="https://example.test/original",
        content_text="新华社发布调查复核情况。",
    )
    credited = SimpleNamespace(
        local_id="E013",
        fetch_status="fetched",
        published_at="2025-09-20T10:00:00+08:00",
        publisher_entity="财新",
        source_name="财新",
        source_tier=2,
        title="调查复核情况转述",
        url="https://example.test/credited",
        content_text="9月20日，据新华社消息，调查复核情况如下。",
    )

    sources = FullReportBuilder._relation_source_context(
        [original, credited], {"E002": {"evidence_id": "E002"}}
    )

    assert {source["evidence_ref"] for source in sources} == {"E002", "E013"}


def test_relation_context_keeps_late_source_credit_and_the_matching_original():
    def source(ref, publisher, text):
        return SimpleNamespace(
            local_id=ref,
            fetch_status="fetched",
            published_at="2025-09-20",
            publisher_entity=publisher,
            source_name=publisher,
            source_tier=2,
            title="公开调查复核情况",
            url=f"https://example.test/{ref}",
            content_text=text,
        )

    original = source("E001", "发布机构", "公开调查复核情况。")
    repost = source("E002", "媒体乙", "正文前部。" * 500 + "来源：发布机构\n公开调查复核情况。")
    context = FullReportBuilder._relation_source_context([original, repost], {})
    assert {s["evidence_ref"] for s in context} == {"E001", "E002"}
    assert "来源：发布机构" in next(s["excerpt"] for s in context if s["evidence_ref"] == "E002")
    assert all(len(s["excerpt"]) <= 2400 for s in context)


def test_credited_publisher_alias_only_selects_candidate_original_for_review():
    original = SimpleNamespace(
        local_id="E001",
        fetch_status="fetched",
        published_at="2025-09-20",
        publisher_entity="央视网",
        source_name="央视网",
        source_tier=1,
        title="调查复核情况",
        url="https://news.cctv.com/2025/09/20/original.shtml",
        content_text="公开调查复核通报。",
    )
    credited = SimpleNamespace(
        local_id="E002",
        fetch_status="fetched",
        published_at="2025-09-20",
        publisher_entity="报道机构",
        source_name="报道机构",
        source_tier=2,
        title="调查复核情况报道",
        url="https://example.test/article",
        content_text="据央视新闻9月20日消息，学校公布复核情况。",
    )
    result = FullReportBuilder._relation_source_context([original, credited], {})
    assert {s["evidence_ref"] for s in result} == {"E001", "E002"}
    assert not any("relation_type" in s for s in result)
    existing = FullReportBuilder._relation_source_context(
        [original, credited], {"E002": {"publisher": "报道机构"}}
    )
    assert {s["evidence_ref"] for s in existing} == {"E001", "E002"}
