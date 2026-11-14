import copy

from yuqing.core.history_cards import background_complete, unique_history_cards


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
