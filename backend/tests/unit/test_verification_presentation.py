import copy

import pytest

from yuqing.core.claim_semantics import publication_actor
from yuqing.core.verification_presentation import enrich_report_sources, presentation_status
from yuqing.services.openai_verifier import _relevant_material


@pytest.mark.parametrize(
    "text, actor",
    [
        ("2025年8月1日，武汉大学发布情况通报，称已成立工作专班。", "武汉大学"),
        ("武汉大学于2025年8月1日发布情况通报，称已成立工作专班。", "武汉大学"),
        ("武汉大学发布通报，证明涉事学生实施了该行为。", None),
        ("据新华社报道，武汉大学发布情况通报。", None),
        ("涉事学生实施了该行为。", None),
    ],
)
def test_publication_record_is_not_an_unattributed_accusation(text, actor):
    assert publication_actor(text) == actor


def test_short_notice_keeps_body_and_signature_in_verification_context():
    body = "武汉大学发布情况通报。\n" + "其他调查事项。" * 300 + "\n学生工作部\n2023年10月13日"
    assert _relevant_material(body, "武汉大学发布情况通报，通报落款为学生工作部。") == body


@pytest.mark.parametrize(
    "state, relation, role, expected",
    [
        ("complete", "support", "syndicated", "reposted_source_supported"),
        ("complete", "support", "unknown", "source_recorded"),
        ("complete", "partial", "syndicated", "partially_supported"),
        ("complete", "not_mentioned", "independent", "insufficient_evidence"),
        ("incomplete", "support", "syndicated", "verification_incomplete"),
        ("skipped", "support", "independent", "verification_incomplete"),
    ],
)
def test_source_material_relation_is_distinct_from_independent_count(
    state, relation, role, expected
):
    fact = {
        "badge": "unverified",
        "verification_state": state,
        "independent_sources": 0,
        "citations": [{"relation": relation, "source_role": role, "fetch_status": "fetched"}],
    }
    assert presentation_status(fact) == expected
    assert fact["badge"] == "unverified"
    assert fact["independent_sources"] == 0


def test_mixed_fetch_status_does_not_erase_a_direct_supporting_source():
    fact = {
        "badge": "unverified",
        "verification_state": "complete",
        "independent_sources": 1,
        "evidence_grade": "mixed",
        "citations": [
            {"relation": "support", "fetch_status": "fetched", "source_role": "independent"},
            {"relation": "partial", "fetch_status": "fetch_failed"},
        ],
    }
    assert presentation_status(fact) == "single_source_supported"
    fact["citations"][1]["relation"] = "contradict"
    assert presentation_status(fact) == "source_conflict"


def test_social_samples_cannot_receive_fact_support_label():
    fact = {
        "badge": "unverified",
        "verification_state": "complete",
        "citations": [
            {"relation": "support", "fetch_status": "fetched", "kind": "social_comments"}
        ],
    }
    assert presentation_status(fact) == "insufficient_evidence"


def test_old_timeline_uses_the_same_claim_without_mutating_saved_authority():
    fact = {
        "claim_ref": "C001",
        "text": "2025年9月17日，法院公布二审结果。",
        "badge": "unverified",
        "verification_state": "complete",
        "independent_sources": 0,
        "citations": [{"evidence_ref": "E001", "relation": "support"}],
    }
    old = {
        "blocks": [
            {"type": "fact_check_table", "items": [fact]},
            {
                "type": "chart",
                "chart_kind": "timeline",
                "items": [{"text": fact["text"], "claim_refs": ["C001"], "badge": "unverified"}],
            },
            {
                "type": "evidence_appendix",
                "items": [
                    {"evidence_ref": "E001", "fetch_status": "fetched", "source_role": "syndicated"}
                ],
            },
        ]
    }
    saved = copy.deepcopy(old)
    projected = enrich_report_sources(old)
    assert old == saved
    assert presentation_status(projected["blocks"][0]["items"][0]) == "reposted_source_supported"
    assert presentation_status(projected["blocks"][1]["items"][0]) == "reposted_source_supported"
