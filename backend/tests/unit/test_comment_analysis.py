import asyncio
import json

import pytest

from yuqing.agents.comment_analysis import OpenAICommentAgent, comment_batches, prepare_comments


@pytest.mark.asyncio
async def test_classification_overlaps_two_batches_and_preserves_every_member():
    class Overlapping(Gateway):
        def __init__(self):
            super().__init__()
            self.active = self.peak = 0
            self.two_started = asyncio.Event()

        async def complete_json(self, role, system, prompt, **kwargs):
            if role != "analyst_b":
                return await super().complete_json(role, system, prompt, **kwargs)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active == 2:
                self.two_started.set()
            try:
                await asyncio.wait_for(self.two_started.wait(), timeout=0.1)
                return await super().complete_json(role, system, prompt, **kwargs)
            finally:
                self.active -= 1

    gateway = Overlapping()
    result = await OpenAICommentAgent(gateway, "comments").analyze("高校事件", rows(36))
    assert gateway.peak == 2
    assert len(gateway.seen) == len(set(gateway.seen)) == 36
    assert result["coverage"]["classified"] == 36
    assert result["coverage"]["unclassified"] == 0


def rows(count=121):
    return [
        {
            "id": str(i),
            "text": f"第{i}条希望校方解释处分程序及后续纠正",
            "source_url": "https://example.test/post",
            "platform": "weibo",
            "evidence_ref": "E001",
        }
        for i in range(count)
    ]


class Gateway:
    def __init__(self):
        self.seen = []
        self.accept = True
        self.incomplete = False

    async def complete_json(self, role, system, prompt, **kwargs):
        data = json.loads(prompt.split("\n", 1)[1])
        if role == "analyst_b":
            self.seen.extend(s["id"] for s in data["comments"])
            assignments = [
                {"id": s["id"], "relevant": True, "issue": "校纪处分与复核", "stance": "质疑"}
                for s in data["comments"]
            ]
            return {"assignments": assignments[:-1] if self.incomplete else assignments}
        if role == "reporter":
            return {
                "themes": [
                    {
                        "title": "要求解释处置程序",
                        "group_ids": [g["id"] for g in data["groups"]],
                        "interpretation": "样本中有人希望解释处分依据。",
                        "response_gap": "公开处置程序",
                        "uncertainty": "只代表已采集样本。",
                    }
                ]
            }
        return {"accepted": self.accept}


def test_deduplication_preserves_platform_and_post_boundaries():
    original = rows(1)
    source = [
        *original,
        original[0].copy(),
        {**original[0], "id": "other", "source_url": "https://example.test/other"},
    ]
    samples, coverage = prepare_comments(source)
    assert len(samples) == 2
    assert coverage["duplicates_or_empty"] == 1
    assert samples[0]["id"] != samples[1]["id"]


def test_batches_do_not_drop_middle_or_long_comments():
    original = rows(150)
    original[60]["text"] = "完整长评论" * 4000
    samples, _ = prepare_comments(original)
    batches = comment_batches(samples)
    assert [s for batch in batches for s in batch] == samples
    assert max(len(batch) for batch in batches) <= 50


@pytest.mark.asyncio
async def test_all_comments_are_classified_and_quotes_come_from_members():
    gateway = Gateway()
    result = await OpenAICommentAgent(gateway, "comments").analyze("高校事件", rows())
    assert len(set(gateway.seen)) == 121
    assert result["coverage"]["classified"] == 121
    assert result["coverage"]["unclassified"] == 0
    assert result["items"][0]["sample_count"] == 121
    assert result["items"][0]["evidence_refs"] == ["E001"]
    assert all(q["id"] in result["items"][0]["comment_refs"] for q in result["items"][0]["quotes"])


@pytest.mark.asyncio
async def test_added_comments_reuse_unchanged_classification():
    first = await OpenAICommentAgent(Gateway(), "comments").analyze("高校事件", rows(3))
    gateway = Gateway()
    resumed = await OpenAICommentAgent(gateway, "comments").analyze(
        "高校事件", rows(4), previous=first
    )
    assert len(gateway.seen) == 1
    assert resumed["coverage"]["classified"] == 4


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_split_or_retry_comment_batches():
    from yuqing.core.llm.gateway import LLMBudgetExhausted

    class Exhausted:
        calls = 0

        async def complete_json(self, *args, **kwargs):
            self.calls += 1
            raise LLMBudgetExhausted()

    gateway = Exhausted()
    result = await OpenAICommentAgent(gateway, "comments").analyze("高校事件", rows(121))
    # Only the two admitted batches may attempt reservation; no splits or next wave.
    assert gateway.calls == 2
    assert result["coverage"]["classified"] == 0
    assert result["diagnostics"][0]["category"] == "local_budget"


@pytest.mark.asyncio
async def test_incomplete_batch_is_not_reported_as_full_coverage():
    gateway = Gateway()
    gateway.incomplete = True
    result = await OpenAICommentAgent(gateway, "comments").analyze("高校事件", rows(3))
    assert result["coverage"]["classified"] == 0
    assert result["coverage"]["unclassified"] == 3
    assert result["items"] == []


@pytest.mark.asyncio
async def test_failed_semantic_review_removes_theme():
    gateway = Gateway()
    gateway.accept = False
    result = await OpenAICommentAgent(gateway, "comments").analyze("高校事件", rows(3))
    assert result["items"] == []
    assert result["coverage"]["classified"] == 3


@pytest.mark.asyncio
async def test_budget_stop_preserves_unclassified_count():
    async def stop():
        return False

    gateway = Gateway()
    result = await OpenAICommentAgent(gateway, "comments").analyze(
        "高校事件", rows(), can_continue=stop
    )
    assert not gateway.seen
    assert result["coverage"]["unclassified"] == 121


@pytest.mark.asyncio
async def test_failed_large_batch_is_retried_as_smaller_batches():
    class SplittingGateway(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "analyst_b" and len(json.loads(prompt.split("\n", 1)[1])["comments"]) > 4:
                raise ValueError("provider request rejected")
            return await super().complete_json(role, system, prompt, **kwargs)

    result = await OpenAICommentAgent(SplittingGateway(), "comments").analyze("高校事件", rows(50))
    assert result["coverage"]["classified"] == 50
    assert result["coverage"]["unclassified"] == 0
    assert result["items"][0]["sample_count"] == 50


@pytest.mark.asyncio
async def test_resume_reuses_classification_and_pending_theme_without_redrafting():
    class InterruptedReview(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            if role == "verifier":
                raise ConnectionError("fixture outage")
            return await super().complete_json(role, system, prompt, **kwargs)

    first = await OpenAICommentAgent(InterruptedReview(), "comments").analyze("高校事件", rows(3))
    assert first["stages"]["classification"] == "complete"
    assert len(first["pending_reviews"]) == 1

    class ReviewOnly:
        async def complete_json(self, role, *args, **kwargs):
            assert role == "verifier"
            return {"accepted": True}

    resumed = await OpenAICommentAgent(ReviewOnly(), "comments").analyze(
        "高校事件", rows(3), previous=first
    )
    assert resumed["status"] == "complete"
    assert resumed["coverage"]["in_reviewed_themes"] == 3
    assert resumed["pending_reviews"] == []
    assert first["warnings"][0] in resumed["warnings"]


@pytest.mark.asyncio
async def test_invalid_assignment_never_enters_checkpoint():
    class Invalid(Gateway):
        async def complete_json(self, role, system, prompt, **kwargs):
            result = await super().complete_json(role, system, prompt, **kwargs)
            if role == "analyst_b":
                result["assignments"][-1]["relevant"] = "true"
            return result

    saved = []

    async def save(result):
        saved.append(json.loads(json.dumps(result)))

    result = await OpenAICommentAgent(Invalid(), "comments").analyze(
        "高校事件", rows(1), save_progress=save
    )
    assert result["status"] == "failed"
    assert all(not checkpoint["assignments"] for checkpoint in saved)


@pytest.mark.asyncio
async def test_policy_change_reclassifies_even_when_sample_fingerprint_is_unchanged():
    gateway = Gateway()
    agent = OpenAICommentAgent(gateway, "comments")
    first = await agent.analyze("高校事件", rows(4), investigation_scope="institution")
    second = await agent.analyze(
        "高校事件", rows(4), previous=first, investigation_scope="public_event"
    )
    assert len(gateway.seen) == 8
    assert second["coverage"]["classified"] == 4
    assert second["scope_policy"] == "public-event-v1"
