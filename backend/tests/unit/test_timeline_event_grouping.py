"""Timeline cards represent events while every authoritative claim stays traceable."""

from yuqing.core.event_chronology import event_timeline as dated_claims
from yuqing.core.timeline_grouping import group_timeline_events
from yuqing.render.visualizations import timeline_visual


def event_timeline(facts):
    return group_timeline_events(dated_claims(facts))


def record(ref, text, evidence="E001", **fields):
    return {
        "claim_ref": ref,
        "text": text,
        "badge": "unverified",
        "verification_state": "complete",
        "origin_agent": "fact_investigator",
        "citations": [{"evidence_ref": evidence, "relation": "support"}],
        **fields,
    }


def test_near_duplicate_test_publications_from_different_sources_share_one_event():
    facts = [
        record(
            "C005",
            "2026年10月8日，懂车帝相关账号发布测试视频，称3台尊界V800在100km/h至0紧急制动测试中刹车踏板支架全部断裂",
            "E001",
        ),
        record(
            "C016",
            "懂车帝2026年10月8日发布测试视频称3台尊界V800在100km/h至0紧急制动时刹车踏板支架断裂",
            "E018",
        ),
    ]
    nodes = event_timeline(facts)
    assert len(nodes) == 1
    assert set(nodes[0]["claim_refs"]) == {"C005", "C016"}
    assert set(nodes[0]["evidence_refs"]) == {"E001", "E018"}
    assert nodes[0]["text"] in {fact["text"] for fact in facts}


def test_same_announcement_combines_overview_and_details_without_deleting_claims():
    facts = [
        record("C009", "尊界汽车于2026年10月8日发布情况说明，回应其制动踏板支架底座相关问题。"),
        record(
            "C011",
            "尊界汽车于2026年10月8日发布情况说明称，在用户实际使用场景中未发生制动踏板支架底座断裂故障，并将提供免费升级。",
        ),
        record(
            "C020",
            "尊界汽车2026年10月8日发布情况说明称自首批车辆交付以来用户实际使用场景中未发生制动踏板支架底座断裂故障",
            "E016",
        ),
    ]
    nodes = event_timeline(facts)
    assert len(nodes) == 1
    assert nodes[0]["text"] == facts[1]["text"]
    assert {item["claim_ref"] for item in nodes[0]["related_claims"]} == {"C009", "C011", "C020"}
    assert {item["text"] for item in nodes[0]["related_claims"]} == {item["text"] for item in facts}


def test_same_day_distinct_actions_and_later_progress_remain_separate():
    facts = [
        record("C001", "2026年10月8日，测试账号发布测试视频称3台车辆踏板支架断裂。"),
        record("C002", "甲公司2026年10月8日发布情况说明，回应踏板支架断裂问题。"),
        record("C003", "甲公司2026年10月8日宣布启动召回。"),
        record("C004", "甲公司2026年10月9日发布情况说明，回应踏板支架断裂问题。"),
    ]
    assert len(event_timeline(facts)) == 4


def test_different_numbers_units_negation_titles_and_known_times_are_not_merged():
    pairs = [
        ("称3台车辆发生断裂。", "称4台车辆发生断裂。"),
        ("称三台车辆发生断裂。", "称四台车辆发生断裂。"),
        ("称测试载荷为100N。", "称测试位移为100mm。"),
        ("称用户实际使用中发生断裂。", "称用户实际使用中未发生断裂。"),
        ("称用户实际使用中存在断裂故障。", "称用户实际使用中无断裂故障。"),
        ("称复核本事件后撤销原有处分决定。", "称复核本事件后维持原有处分决定。"),
        ("称3台受检车辆全部出现断裂故障。", "称3台受检车辆部分出现断裂故障。"),
        ("称静态加载1500N时符合检测要求。", "称动态加载1500N时符合检测要求。"),
    ]
    for left, right in pairs:
        assert (
            len(
                event_timeline(
                    [
                        record("C001", "甲公司2026年10月8日发布情况说明" + left),
                        record("C002", "甲公司2026年10月8日发布情况说明" + right),
                    ]
                )
            )
            == 2
        )
    for left, right in [
        (
            "甲公司2026年10月8日发布《甲产品情况说明》称启动免费升级。",
            "甲公司2026年10月8日发布《乙产品情况说明》称启动免费升级。",
        ),
        (
            "甲公司2026年10月8日上午发布情况说明，宣布启动免费升级。",
            "甲公司2026年10月8日下午发布情况说明，宣布启动免费升级。",
        ),
    ]:
        assert len(event_timeline([record("C001", left), record("C002", right)])) == 2


def test_generic_overview_cannot_bridge_conflicting_details():
    nodes = event_timeline(
        [
            record("C001", "甲公司2026年10月8日发布情况说明，回应踏板支架问题。"),
            record("C002", "甲公司2026年10月8日发布情况说明称3台车辆发生断裂。"),
            record("C003", "甲公司2026年10月8日发布情况说明称4台车辆发生断裂。"),
        ]
    )
    assert len(nodes) >= 2
    assert {ref for node in nodes for ref in node["claim_refs"]} == {"C001", "C002", "C003"}
    assert not any({"C002", "C003"} <= set(node["claim_refs"]) for node in nodes)


def test_group_has_no_aggregate_verification_and_preserves_each_claim_and_link():
    first = record("C001", "甲公司2026年10月8日发布情况说明称提供免费升级。", badge="verified")
    second = record("C002", first["text"], "E002", verification_state="pending")
    nodes = event_timeline([first, second])
    html = timeline_visual({"items": nodes})
    assert html.count('class="visual-event"') == 1
    assert 'class="visual-group-label"' in html
    assert html.count('class="visual-related-claim"') == 2
    assert 'class="badge verified"' in html
    assert 'class="badge verification_incomplete"' in html
    assert "含核验未完成陈述" in html
    for ref in ("C001", "C002"):
        assert f'href="#claim-{ref}"' in html
    for ref in ("E001", "E002"):
        assert f'href="#evidence-{ref}"' in html


def test_one_source_page_is_not_proof_that_unrelated_announcements_are_one_event():
    for left, right in [
        ("称暂停旧厂区生产。", "称恢复线上售后服务。"),
        ("称针对甲产品质量问题启动免费升级。", "称针对乙产品质量问题启动免费升级。"),
        ("称启动免费升级。", "发布另一份情况说明称启动免费升级。"),
    ]:
        nodes = event_timeline(
            [
                record("C001", "甲公司2026年10月8日发布情况说明" + left),
                record(
                    "C002",
                    "甲公司2026年10月8日"
                    + (right if right.startswith("发布") else "发布情况说明" + right),
                ),
            ]
        )
        assert len(nodes) == 2


def test_projection_is_read_only_and_idempotent_and_escapes_member_text():
    import copy

    facts = [
        record("C001", "甲公司2026年10月8日发布情况说明，回应制动踏板支架相关问题。"),
        record(
            "C002",
            "甲公司2026年10月8日发布情况说明称制动踏板支架将免费升级。<img src=x onerror=alert(1)>",
        ),
    ]
    saved = copy.deepcopy(facts)
    nodes = event_timeline(facts)
    assert group_timeline_events(nodes) == nodes
    html = timeline_visual({"items": nodes})
    assert "&lt;img" in html
    assert "<img" not in html
    assert facts == saved


def test_material_publication_fallback_is_not_treated_as_a_group_of_claims():
    nodes = [
        {
            "date": "2025-09-17",
            "text": "甲公司发布情况说明称提供免费升级。",
            "claim_refs": ["C001"],
            "evidence_refs": [f"E00{i}"],
        }
        for i in (1, 2)
    ]
    assert len(group_timeline_events(nodes)) == 2
    assert timeline_visual({"items": nodes}).count('class="visual-event"') == 2


def test_disputed_or_refuted_member_is_warned_before_expanding_group():
    text = "甲公司2026年10月8日发布情况说明称将免费升级。"
    nodes = event_timeline(
        [
            record("C001", text, badge="verified"),
            record("C002", text, "E002", badge="refuted", statement_kind="rumor"),
        ]
    )
    html = timeline_visual({"items": nodes})
    assert "含有争议或已证伪陈述" in html
    assert 'class="badge refuted"' in html
