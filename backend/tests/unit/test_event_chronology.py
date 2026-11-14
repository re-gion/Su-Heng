"""Regressions for losing the beginning and middle of an evidence-backed event."""

import pytest

from yuqing.render.html import render_html
from yuqing.services.report_analysis import event_timeline


def fact(ref, text):
    return {
        "claim_ref": ref,
        "text": text,
        "origin_agent": "fact_investigator",
        "badge": "unverified",
        "verification_state": "complete",
        "citations": [{"evidence_ref": "E001", "relation": "support"}],
    }


@pytest.mark.parametrize(
    "text",
    [
        "联合调查组通报称涉事学院对2023年6月1日食堂食品安全事件负主体责任。",
        "校方表示2023年6月1日学生上传了视频。",
        "关于2023年6月1日事件的调查结果已经公布。",
        "截至2023年6月17日，材料尚未载明后续问责结果。",
    ],
)
def test_nested_incident_or_observation_date_is_not_undated_statement_date(text):
    assert event_timeline([fact("C001", text)]) == []


def test_explicitly_dated_statement_keeps_its_date_when_recounting_earlier_incident():
    nodes = event_timeline(
        [fact("C001", "联合调查组2023年6月17日通报称，学院对6月1日事件负主体责任。")]
    )
    assert nodes[0]["date"] == "2023-06-17"


def test_initial_publication_survives_two_responses_on_the_same_day():
    facts = [
        fact("C001", "2026年10月8日，懂车帝相关账号发布测试视频，称车辆测试中踏板支架断裂。"),
        fact("C002", "尊界汽车于2026年10月8日发布情况说明，并提供免费升级选择。"),
        fact("C003", "某汽车公司于2026年10月8日宣布启动调查。"),
    ]
    assert [n["claim_refs"] for n in event_timeline(facts)] == [["C001"], ["C002"], ["C003"]]


def test_evening_statement_and_actor_containing_model_number_are_kept():
    facts = [
        fact("C001", "某大学2023年10月11日晚发布情况说明，成立工作组调查。"),
        fact("C002", "V800测试账号2026年10月8日发布测试视频。"),
    ]
    assert [n["date"] for n in event_timeline(facts)] == ["2023-10-11", "2026-10-08"]


def test_long_chronology_keeps_origin_middle_and_latest_progress():
    facts = [fact(f"C{i:03d}", f"2025年1月{i}日，机构发布第{i}阶段进展。") for i in range(1, 21)]
    assert len(event_timeline(facts)) == 20


def test_focus_window_does_not_drop_event_origin():
    facts = [fact("C001", "2023年10月11日，当事人发布公开帖文。")]
    facts += [fact(f"C{i:03d}", f"2025年9月{i}日，机构发布第{i}次进展。") for i in range(2, 20)]
    nodes = event_timeline(facts, date_from="2025-09-01", date_to="2025-09-30")
    assert nodes[0]["date"] == "2023-10-11"
    assert nodes[0]["window_label"] == "重点窗口前的本事件经过"


def test_old_report_restores_saved_origin_without_changing_saved_ir():
    import copy

    facts = [
        fact("C001", "2023年10月11日，当事人发帖披露事件。"),
        fact("C002", "2025年9月17日，法院作出二审判决。"),
    ]
    report = {
        "task": {},
        "blocks": [
            {
                "type": "chart",
                "chart_kind": "timeline",
                "block_id": "b_02_correction_timeline",
                "section": "02",
                "items": event_timeline(facts[1:]),
            },
            {"type": "fact_check_table", "section": "03", "items": facts},
        ],
    }
    saved = copy.deepcopy(report)
    page = render_html(report)
    timeline_html = page.split('<ol class="visual-timeline">')[1].split("</ol>")[0]
    assert 'datetime="2023-10-11"' in timeline_html
    assert 'href="#claim-C001"' in timeline_html
    assert report == saved


def test_earlier_source_record_requires_extraction_instead_of_fabricating_node():
    from types import SimpleNamespace

    from yuqing.core.event_chronology import earlier_event_candidates

    facts = [fact("C001", "2025年9月17日，法院作出二审判决。")]
    source = SimpleNamespace(
        local_id="E005",
        fetch_status="fetched",
        extra={"scope_status": "main"},
        content_text="2023年10月11日，当事人发文披露此事。\n2025年9月17日，法院作出判决。",
    )
    gaps = earlier_event_candidates(facts, [source])
    assert gaps == [{"date": "2023-10-11", "evidence_ref": "E005"}]
    assert len(event_timeline(facts)) == 1
    facts.append(fact("C002", "2023年10月11日，当事人发布公开帖文。"))
    assert earlier_event_candidates(facts, [source]) == []


def test_earlier_source_candidates_exclude_history_background_and_unfetched_text():
    from types import SimpleNamespace

    from yuqing.core.event_chronology import earlier_event_candidates

    facts = [fact("C001", "2025年9月17日，法院作出二审判决。")]
    for status, fetched in [
        ("history", "fetched"),
        ("background", "fetched"),
        ("pending", "fetched"),
        ("main", "fetch_failed"),
    ]:
        source = SimpleNamespace(
            local_id="E001",
            extra={"scope_status": status},
            fetch_status=fetched,
            content_text="2023年10月11日，当事人发帖披露事件。",
        )
        assert earlier_event_candidates(facts, [source]) == []


def test_material_excerpt_preserves_dated_origin_between_later_decisions():
    from yuqing.agents.openai_runtime import _evidence_excerpt

    onset = "2023年10月11日，当事人发帖披露事件。"
    content = "2025年9月20日，机构公布最终复核通报。\n" + "\n".join(
        [f"机构决定对第{i}项问题启动调查并作出处分。" for i in range(20)]
        + [onset]
        + [f"机构决定对第{i}项问题维持处理并公布结论。" for i in range(20, 40)]
    )
    excerpt = _evidence_excerpt(content, 600)
    assert onset in excerpt
    assert "2025年9月20日" in excerpt
    assert len(excerpt) <= 600


def test_missing_origin_cannot_pass_full_report_gate_with_two_later_nodes():
    from yuqing.services.investigation_scope import ReportReleaseAssessment

    result = ReportReleaseAssessment.evaluate(
        concrete_event=True,
        main_evidence=3,
        verifiable_key_claims=2,
        event_timeline_nodes=2,
        publication_nodes=2,
        propagation_edges=1,
        summary_has_what=True,
        summary_has_why=True,
        summary_has_action=True,
        evidence_bound_recommendations=1,
        event_chronology_complete=False,
    )
    assert result.label == "evidence_brief"
    assert "event_chronology" in result.missing


def test_same_day_school_response_does_not_substitute_for_initial_disclosure():
    from types import SimpleNamespace

    from yuqing.core.event_chronology import earlier_event_candidates

    facts = [fact("C001", "2023年10月11日晚，学校发布情况说明并成立工作组。")]
    source = SimpleNamespace(
        local_id="E005",
        fetch_status="fetched",
        extra={"scope_status": "main"},
        content_text="2023年10月11日，当事人发帖披露事件。",
    )
    assert earlier_event_candidates(facts, [source]) == [
        {"date": "2023-10-11", "evidence_ref": "E005"}
    ]
    facts.append(fact("C002", source.content_text))
    assert earlier_event_candidates(facts, [source]) == []
