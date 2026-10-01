import copy
from types import SimpleNamespace

import pytest
from test_report_analysis import inputs

from yuqing.services.full_report import FullReportBuilder
from yuqing.services.institution_scope import ScopeReview
from yuqing.services.report_analysis import assemble_analysis


def test_republished_measurement_merges_evidence_without_double_counting():
    facts, sources, draft = inputs()
    copy_source = copy.copy(sources[0])
    copy_source.local_id = "E002"
    copy_source.publisher_entity = "转载网站"
    sources.append(copy_source)
    facts[0]["citations"].append({"evidence_ref": "E002", "relation": "support"})
    draft["measurements"].append({**draft["measurements"][0], "evidence_ref": "E002"})
    _, blocks, _ = assemble_analysis(draft, facts, sources)
    cards = next(b for b in blocks if b["type"] == "metric_cards")["items"]
    assert len(cards) == 1
    assert cards[0]["evidence_refs"] == ["E001", "E002"]


def test_different_disclosure_sentences_are_not_merged():
    facts, sources, draft = inputs()
    source = copy.copy(sources[0])
    source.local_id = "E002"
    source.content_text = "另一院系收到有效诉求120条，仍在等待受理。"
    sources.append(source)
    facts.append(
        {
            **facts[0],
            "claim_ref": "C002",
            "text": source.content_text,
            "citations": [{"evidence_ref": "E002", "relation": "support"}],
        }
    )
    draft["measurements"].append(
        {
            **draft["measurements"][0],
            "evidence_ref": "E002",
            "claim_refs": ["C002"],
            "quote": source.content_text,
        }
    )
    _, blocks, _ = assemble_analysis(draft, facts, sources)
    assert len(next(b for b in blocks if b["type"] == "metric_cards")["items"]) == 2


def test_measurement_label_does_not_repeat_the_number():
    facts, sources, draft = inputs()
    draft["measurements"][0]["label"] = "120条"
    _, blocks, _ = assemble_analysis(draft, facts, sources)
    card = next(b for b in blocks if b["type"] == "metric_cards")["items"][0]
    assert card["label"] != card["value"]
    assert card["label"] in card["quote"]
    assert next(b for b in blocks if b["type"] == "metric_cards")["section"] == "03"


def test_publication_network_skips_other_institution_page():
    claim = SimpleNamespace(
        agent="media_propagation",
        local_id="C001",
        text="其他高校发布招生信息",
        analysis_data={
            "publication_node": {
                "evidence_id": "E001",
                "node_type": "independent",
                "publisher": "湖北大学",
            }
        },
    )
    source = SimpleNamespace(
        title="湖北大学招生复查公告",
        content_text="湖北大学发布招生复查结果。",
        snippet="",
        publisher_entity="湖北大学",
        source_name="湖北大学",
        source_domain="example.edu",
        published_at="2025-09-20",
    )
    network, count, _ = FullReportBuilder._publication_network(
        [claim],
        {"E001": source},
        {"E001"},
        {"C001": {"E001"}},
        {"items": []},
        "武汉大学图书馆事件",
    )
    assert count == 0
    assert network["nodes"] == []


def test_unmeasured_propagation_effect_is_rejected_without_relation_edges():
    facts, sources, draft = inputs()
    draft["analyses"][0]["interpretation"] = "争议并未因此收敛，谣言持续扩散。"
    _, blocks, quality = assemble_analysis(draft, facts, sources, propagation_edges=0)
    assert quality["rejected_items"]["unsupported_propagation_effect"] == 1
    assert not any(b.get("section") == "07" and b["type"] == "analysis" for b in blocks)


def test_reviewed_chapter_action_becomes_compact_action_plan():
    facts, sources, draft = inputs()
    draft["analyses"][0]["section"] = "04"
    summary, blocks, quality = assemble_analysis(draft, facts, sources, propagation_edges=0)
    plan = next(b for b in blocks if b["type"] == "action_plan")
    assert plan["section"] == "07"
    assert plan["items"][0]["derived_from_section"] == "04"
    assert plan["items"][0]["action"] == draft["analyses"][0]["action"]
    assert "interpretation" not in plan["items"][0]
    assert summary["so_what"] and quality["status"] == "analysis_available"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_analysis", [False, True])
async def test_anonymous_source_labels_preserve_graph_without_rewriting_analysis(
    tmp_path, with_analysis
):
    class Reviewer:
        def bind(self, *_args):
            pass

        async def review(self, texts, *, kind):
            assert kind == "report_text"
            return [
                ScopeReview("accepted", "privacy_redacted", t.replace("张三", "相关个人"))
                for t in texts
            ]

    source_node = {
        "evidence_id": "E001",
        "publisher": "张三",
        "framing": "张三发布公开回应",
        "evidence_refs": ["E001"],
    }
    fact = {"text": "机构发布情况通报。", "badge": "verified", "claim_ref": "C001"}
    report = {
        "metrics": {"key_claims_rendered": 1},
        "quality": {"release_label": "full_report", "release_gate_missing": []},
        "blocks": [
            {"block_id": "header", "type": "report_header", "subtitle": "完整报告"},
            {"block_id": "facts", "type": "fact_check_table", "items": [fact]},
            {
                "block_id": "graph",
                "type": "propagation_network",
                "section": "04",
                "nodes": [source_node],
                "edges": [],
            },
            {
                "block_id": "appendix",
                "type": "evidence_appendix",
                "items": [{"evidence_ref": "E001"}],
            },
        ],
    }
    analysis = {"interpretation": "张三的行为说明应调整处置策略。", "evidence_refs": ["E001"]}
    if with_analysis:
        report["blocks"].append(
            {"block_id": "analysis", "type": "analysis", "section": "07", "items": [analysis]}
        )
    before_fact = copy.deepcopy(fact)
    builder = FullReportBuilder(None, tmp_path, scope_reviewer=Reviewer())
    await builder._retain_reviewed_blocks(
        report, SimpleNamespace(id="task", investigation_scope="public_event")
    )
    assert source_node["publisher"] == "相关个人"
    assert source_node["framing"] == "相关个人发布公开回应"
    assert source_node["evidence_id"] == "E001" and source_node["evidence_refs"] == ["E001"]
    assert fact == before_fact
    assert any(b["block_id"] == "graph" for b in report["blocks"])
    scope = report["quality"]["scope_review"]
    assert scope["redacted_source_labels"] == 2
    if with_analysis:
        assert analysis["interpretation"].startswith("相关个人")
        assert any(b["block_id"] == "analysis" for b in report["blocks"])
        assert scope["incomplete_texts"] == 0
    else:
        assert scope["incomplete_texts"] == 0
        assert report["quality"]["release_label"] == "full_report"


def test_missing_action_analysis_keeps_chapter_boundary():
    report = {
        "blocks": [
            {"block_id": "summary", "type": "executive_summary", "section": "01"},
            {"block_id": "limits", "type": "limitations", "section": "08"},
        ]
    }

    assert FullReportBuilder._ensure_action_chapter(report)
    chapter = next(block for block in report["blocks"] if block.get("section") == "07")
    limits = next(block for block in report["blocks"] if block.get("type") == "limitations")
    assert chapter["block_id"] == "b_07_recommendations_fallback"
    assert report["blocks"].index(chapter) < report["blocks"].index(limits)
    assert not FullReportBuilder._ensure_action_chapter(report)
