import json
from types import SimpleNamespace

import pytest

from yuqing.agents.comment_analysis import OpenAICommentAgent
from yuqing.agents.reporter import OpenAIReportAgent
from yuqing.core.llm.gateway import upstream_diagnostic
from yuqing.services.budget import DEFAULT_BUDGET_TABLE
from yuqing.services.v1_orchestrator import V1Orchestrator


@pytest.mark.asyncio
async def test_relationship_review_failure_keeps_previously_accepted_edge():
    class Gateway:
        reviews = 0

        async def complete_json(self, role, system, user, **kwargs):
            if role == "reporter":
                return {
                    "edges": [
                        {
                            "from_evidence_id": "E001",
                            "to_evidence_id": ref,
                            "support_evidence_id": ref,
                            "relation": "repost",
                            "quote": "本报道明确转载第一家媒体的原始报道。",
                        }
                        for ref in ("E002", "E003")
                    ]
                }
            self.reviews += 1
            if self.reviews == 2:
                raise RuntimeError("upstream unavailable")
            return {"accepted": True}

    result = await OpenAIReportAgent(Gateway(), "report").recover_relations(
        [
            {"evidence_ref": ref, "excerpt": "本报道明确转载第一家媒体的原始报道。"}
            for ref in ("E001", "E002", "E003")
        ]
    )
    assert len(result) == 1
    assert result[0]["to_evidence_id"] == "E002"


@pytest.mark.asyncio
async def test_comment_review_failure_does_not_block_other_themes():
    class Gateway:
        reviews = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            data = json.loads(prompt.split("\n", 1)[1])
            if role == "analyst_b":
                return {
                    "assignments": [
                        {
                            "id": s["id"],
                            "relevant": True,
                            "issue": "机构回应" if i == 0 else "媒体报道",
                            "stance": "质疑",
                        }
                        for i, s in enumerate(data["comments"])
                    ]
                }
            if role == "reporter":
                return {
                    "themes": [
                        {
                            "title": g["topic"],
                            "group_ids": [g["id"]],
                            "interpretation": "样本提出具体问题",
                            "response_gap": "补充说明",
                            "uncertainty": "仅代表样本",
                        }
                        for g in data["groups"]
                    ]
                }
            self.reviews += 1
            if self.reviews == 1:
                raise RuntimeError("upstream unavailable")
            return {"accepted": True}

    result = await OpenAICommentAgent(Gateway(), "评论分析").analyze(
        "公开事件",
        [
            {
                "id": str(i),
                "text": "希望说明" + str(i),
                "platform": "weibo",
                "source_url": "https://example.test/post",
                "evidence_ref": "E001",
            }
            for i in range(2)
        ],
    )
    assert result["items"]
    assert result["coverage"]["classified"] == 2


def test_bad_request_diagnostic_redacts_body_and_distinguishes_known_cause():
    from httpx import Request, Response
    from openai import BadRequestError

    response = Response(400, request=Request("POST", "https://example.test"))
    error = BadRequestError(
        "private sample sk-secret",
        response=response,
        body={
            "error": {
                "code": "context_length_exceeded",
                "message": "maximum context length sk-secret",
            }
        },
    )
    diagnostic = upstream_diagnostic(error, stage="comment_synthesis", batch="batch-2")
    assert diagnostic["category"] == "request_length"
    assert diagnostic["status"] == 400
    assert "sk-secret" not in json.dumps(diagnostic)
    unknown = BadRequestError("private", response=response, body={})
    assert upstream_diagnostic(unknown)["category"] == "unknown"


def test_key_fact_corroboration_only_selects_dated_independent_original():
    claim = SimpleNamespace(
        local_id="C001",
        agent="fact_investigator",
        is_key=True,
        verification_state="complete",
        badge="unverified",
        verdict="support",
        independent_sources=1,
        evidence_ids=["E001"],
        text="学校于2025年7月25日公布复核结果。",
    )

    def source(local_id, domain, role="independent", date="2025-07-25"):
        return SimpleNamespace(
            local_id=local_id,
            source_domain=domain,
            publisher_entity=None,
            source_role=role,
            source_tier=2,
            fetch_status="fetched",
            extra={"scope_status": "main"},
            published_at=date,
            content_text="学校于2025年7月25日公布复核结果。",
        )

    evidence = [
        source("E001", "www.cnr.cn"),
        source("E002", "m.cnr.cn"),
        source("E003", "www.news.cn"),
        source("E004", "example.test", date=None),
        source("E005", "other.test", role="syndicated"),
    ]
    candidates = V1Orchestrator._corroboration_candidates([claim], evidence, 6)
    assert [(item.local_id, candidate.local_id) for _, item, candidate in candidates] == [
        ("C001", "E003")
    ]


@pytest.mark.asyncio
async def test_key_fact_corroboration_rechecks_new_relation_before_upgrading():
    claim = SimpleNamespace(
        local_id="C001",
        agent="fact_investigator",
        is_key=True,
        verification_state="complete",
        badge="unverified",
        verdict="support",
        independent_sources=1,
        evidence_ids=["E001"],
        text="学校于2025年7月25日公布复核结果。",
        statement_kind="fact",
        round=1,
        section="fact_check",
    )

    def source(local_id, domain):
        return SimpleNamespace(
            local_id=local_id,
            source_domain=domain,
            publisher_entity=None,
            source_role="independent",
            source_tier=2,
            fetch_status="fetched",
            extra={"scope_status": "main"},
            published_at="2025-07-25",
            content_text=claim.text,
        )

    class Database:
        checkpoints = {}
        linked = None

        async def checkpoint(self, task_id, key):
            return self.checkpoints.get(key)

        async def save_checkpoint(self, task_id, key, value):
            self.checkpoints[key] = value

        async def get_task(self, task_id):
            return SimpleNamespace(status="running")

        async def list_claims(self, task_id):
            return [claim]

        async def list_evidence(self, task_id):
            return [source("E001", "www.cnr.cn"), source("E002", "www.news.cn")]

        async def get_claim(self, task_id, local_id):
            return claim

        async def add_claim(self, value, **limits):
            self.linked = value.evidence_ids
            return claim

    class Verification:
        calls = 0

        async def verify_claim(self, value, *, reuse_completed):
            self.calls += 1
            assert reuse_completed is True
            assert value is claim
            claim.badge = "verified"
            return claim

    class Events:
        calls = 0

        async def emit(self, task_id, event, data):
            self.calls += 1

    class Harness:
        database = Database()
        verification = Verification()
        events = Events()
        _limitations = []
        _corroboration_candidates = staticmethod(V1Orchestrator._corroboration_candidates)

        def budget_for(self, depth):
            return DEFAULT_BUDGET_TABLE[depth]

        def _llm_phase_has_room(self, minimum):
            return True

        async def _emit_budget(self, task_id, depth):
            return False

    harness = Harness()
    await V1Orchestrator._corroborate_key_fact(harness, "task", "standard")
    assert harness.database.linked == ["E002"]
    assert harness.verification.calls == 1
    assert harness.database.checkpoints["report:fact_corroboration"]["attempted"] == [
        ("C001", "E002")
    ]
    assert harness.events.calls == 1
