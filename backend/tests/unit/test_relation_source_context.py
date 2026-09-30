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
