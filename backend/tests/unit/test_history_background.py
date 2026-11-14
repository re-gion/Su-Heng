import copy

from yuqing.core.history_cards import background_complete, unique_history_cards


def test_reading_copy_does_not_count_near_named_same_subject_cases_twice():
    from yuqing.core.verification_presentation import enrich_report_sources

    cards = [
        {
            "event_name": "昆明长丰学校食堂生肉异味事件",
            "event_time": "资料发布：2024-10-19",
            "evidence_refs": ["E001"],
        },
        {
            "event_name": "昆明市官渡区长丰学校食堂肉品异味事件",
            "event_time": "资料发布：2024-10-20",
            "evidence_refs": ["E002"],
        },
        {
            "event_name": "昆明市官渡区长丰学校图书馆火灾事件",
            "event_time": "资料发布：2024-10-21",
            "evidence_refs": ["E003"],
        },
    ]
    report = {"blocks": [{"type": "history_compare", "cards": cards, "reference_candidates": []}]}
    displayed = enrich_report_sources(report)["blocks"][0]
    assert len(displayed["cards"]) == 2
    assert displayed["reference_candidates"][0]["evidence_refs"] == ["E002"]
    assert displayed["reference_candidates"][0]["reason"]
    assert len(report["blocks"][0]["cards"]) == 3


def test_same_named_subject_in_different_material_months_remains_distinct():
    from yuqing.core.verification_presentation import enrich_report_sources

    cards = [
        {"event_name": "昆明长丰学校食堂生肉异味事件", "event_time": "资料发布：2024-10-19"},
        {"event_name": "昆明长丰学校食堂肉品异味事件", "event_time": "资料发布：2025-10-20"},
    ]
    displayed = enrich_report_sources({"blocks": [{"type": "history_compare", "cards": cards}]})
    assert len(displayed["blocks"][0]["cards"]) == 2


def test_generic_school_suffix_does_not_conflate_different_named_subjects():
    from yuqing.core.verification_presentation import enrich_report_sources

    cards = [
        {"event_name": name, "event_time": "资料发布：2024-10-19"}
        for name in ("北京实验学校食堂异物事件", "上海实验学校食堂异物事件")
    ]
    displayed = enrich_report_sources({"blocks": [{"type": "history_compare", "cards": cards}]})
    assert len(displayed["blocks"][0]["cards"]) == 2


def test_restaurant_identity_alone_cannot_count_as_case_event_and_progress():
    card = {
        "background_facts": [{"text": "监管局通报称涉事餐厅为某学校第一食堂。"}],
        "background_anchors": {
            "subject": "监管局",
            "event": "涉事餐厅",
            "progress": "涉事餐厅为某学校第一食堂",
        },
    }
    assert not background_complete(card)


def test_background_can_be_covered_by_multiple_supported_atomic_facts():
    cards = [
        {
            "event_name": "甲公司召回案",
            "claim_ref": "C001",
            "claim_refs": ["C000"],
            "background_facts": [{"claim_ref": "C001", "text": "甲公司车型存在踏板断裂风险。"}],
            "background_anchors": {"subject": "甲公司", "event": "踏板断裂风险"},
        },
        {
            "event_name": "甲公司召回案",
            "claim_ref": "C002",
            "background_facts": [{"claim_ref": "C002", "text": "甲公司随后启动召回。"}],
            "background_anchors": {"progress": "启动召回"},
        },
    ]
    original = copy.deepcopy(cards)
    merged = unique_history_cards(cards)[0]
    assert merged["claim_refs"] == ["C000", "C001", "C002"]
    assert len(merged["background_facts"]) == 2
    assert background_complete(merged)
    assert cards == original
    merged["background_anchors"]["progress"] = "监管已确认缺陷"
    assert not background_complete(merged)
    merged["background_anchors"]["progress"] = "踏板断裂风险"
    assert not background_complete(merged)


def test_incomplete_background_is_not_repaired_with_a_model_outcome():
    card = {
        "background_facts": [{"text": "甲公司车型存在缺陷。"}],
        "background_anchors": {"subject": "甲公司", "event": "存在缺陷"},
        "outcome": "已处理完毕",
    }
    assert not background_complete(card)
