import asyncio
import hashlib
import json

import pytest

from yuqing.services.institution_scope import InstitutionScopeReviewer, only_anonymous_roles_changed


@pytest.mark.asyncio
@pytest.mark.parametrize("report_rejected", [False, True])
async def test_exact_reviewed_comment_is_reusable_but_cannot_override_report_rejection(
    report_rejected,
):
    text = "样本希望机构公开说明复核程序。"

    def key(kind):
        return hashlib.sha256(
            json.dumps(["public-event-v1", "public_event", kind, text], ensure_ascii=False).encode()
        ).hexdigest()

    cache = {key("comment"): {"status": "accepted", "reason": "scope_passed", "text": text}}
    if report_rejected:
        cache[key("report_text")] = {"status": "rejected", "reason": "policy", "text": text}

    class Database:
        async def get_scope_review(self, task_id, fingerprint):
            return cache.get(fingerprint)

    class Gateway:
        async def complete_json(self, *args, **kwargs):
            raise AssertionError("Valid exact cache should avoid a duplicate model call")

    reviewer = InstitutionScopeReviewer(Gateway())
    reviewer.bind(Database(), "t", "public_event")
    result = (await reviewer.review([text], kind="report_text"))[0]
    assert result.allowed is not report_rejected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "report_text"])
async def test_scope_batches_overlap_and_preserve_input_order(kind):
    class ConcurrentGateway:
        active = 0
        peak = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            items = json.loads(prompt.split("\n", 1)[1])
            return {"items": [{"id": item["id"], "allowed": True} for item in items]}

    gateway = ConcurrentGateway()
    texts = [f"公开回应第{i}条" for i in range(30)]
    decisions = await InstitutionScopeReviewer(gateway).review(texts, kind=kind)
    assert gateway.peak == 2
    assert all(item.allowed for item in decisions)
    assert [item.text for item in decisions] == texts


@pytest.mark.asyncio
async def test_scope_review_serializes_near_budget_boundary():
    class NearBudgetGateway:
        token_limit = 4000
        tokens_used = 0
        _tokens_reserved = 0
        active = 0
        peak = 0

        async def complete_json(self, role, system, prompt, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            items = json.loads(prompt.split("\n", 1)[1])
            return {"items": [{"id": item["id"], "allowed": True} for item in items]}

    gateway = NearBudgetGateway()
    decisions = await InstitutionScopeReviewer(gateway).review(
        [f"公开材料{i}" for i in range(24)], kind="report_text"
    )
    assert gateway.peak == 1
    assert all(item.allowed for item in decisions)


@pytest.mark.asyncio
async def test_parallel_reviews_keep_one_alias_even_when_second_batch_finishes_first():
    class Database:
        aliases = {}

        async def get_scope_review(self, *args):
            return None

        async def save_scope_review(self, *args):
            pass

        async def get_analysis_batch(self, task, agent, fingerprint):
            return self.aliases.get(fingerprint)

        async def save_analysis_batch(self, task, agent, fingerprint, value):
            self.aliases[fingerprint] = value

    class Conflicting:
        async def complete_json(self, role, system, prompt, **kwargs):
            items = json.loads(prompt.split("\n", 1)[1])
            first = items[0]["text"].startswith("第0条")
            if first:
                await asyncio.sleep(0.01)
            return {
                "items": [
                    {
                        "id": item["id"],
                        "allowed": True,
                        "redactions": [
                            {"text": "张某", "replacement": "当事人甲" if first else "当事人乙"}
                        ],
                    }
                    for item in items
                ]
            }

    reviewer = InstitutionScopeReviewer(Conflicting())
    reviewer.bind(Database(), "task", "public_event")
    decisions = await reviewer.review([f"第{i}条通报涉及张某。" for i in range(24)], kind="comment")
    assert all(d.allowed and "当事人甲" in d.text and "当事人乙" not in d.text for d in decisions)


@pytest.mark.asyncio
async def test_invalid_redaction_does_not_discard_other_reviewed_items():
    reviewer = InstitutionScopeReviewer(
        Gateway(
            [
                {
                    "items": [
                        {"id": 0, "allowed": True},
                        {
                            "id": 1,
                            "allowed": True,
                            "redactions": [{"text": "不存在", "replacement": "相关个人"}],
                        },
                        {"id": 2, "allowed": False},
                    ]
                }
            ]
        )
    )
    decisions = await reviewer.review(
        ["学校公布复核结果。", "学校回应。", "私人指控。"], kind="comment", scope="public_event"
    )
    assert [d.status for d in decisions] == ["accepted", "incomplete", "rejected"]
    assert decisions[1].diagnostic["category"] == "invalid_output"


class Gateway:
    def __init__(self, responses):
        self.responses = list(responses)

    async def complete_json(self, _role, _system, _prompt, *, max_tokens):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_scope_review_fails_closed_on_incomplete_result_and_identifiers():
    reviewer = InstitutionScopeReviewer(
        Gateway(
            [
                {"items": [{"id": index, "allowed": True} for index in range(12)]},
                {"items": []},
            ]
        )
    )
    texts = [
        "校方公布复核安排。",
        "校方公布复核安排，联系人手机号：13800138000。",
        *["校方回应。"] * 12,
    ]

    accepted = await reviewer.accepted(texts, kind="claim")

    assert accepted[:2] == [True, False]
    assert accepted[2:13] == [True] * 11
    assert accepted[13] is False


@pytest.mark.asyncio
async def test_scope_review_rejects_text_longer_than_model_input_limit():
    reviewer = InstitutionScopeReviewer(Gateway([{"items": [{"id": 0, "allowed": True}]}]))

    accepted = await reviewer.accepted(["校方回应。" * 601], kind="claim")

    assert accepted == [False]


@pytest.mark.asyncio
async def test_role_synonyms_do_not_rewrite_already_anonymous_actors():
    reviewer = InstitutionScopeReviewer(
        Gateway(
            [
                {
                    "items": [
                        {
                            "id": 0,
                            "allowed": True,
                            "redactions": [
                                {"text": "涉事男生", "replacement": "涉事学生"},
                                {"text": "涉事女生", "replacement": "另一名学生"},
                            ],
                        }
                    ]
                }
            ]
        )
    )
    original = "校方撤销涉事男生处分，并复核涉事女生论文。"
    decision = (await reviewer.review([original], kind="claim", scope="public_event"))[0]
    assert decision.allowed and decision.text == original
    assert only_anonymous_roles_changed(original, "校方撤销涉事学生处分，并复核另一名学生论文。")
    assert not only_anonymous_roles_changed("张三收到处分。", "涉事学生收到处分。")
    assert not only_anonymous_roles_changed(
        original, "校方维持涉事学生处分，并复核另一名学生论文。"
    )
    assert only_anonymous_roles_changed("校方在女生发帖后复核。", "校方在当事人甲发帖后复核。")


@pytest.mark.asyncio
async def test_cached_role_only_change_is_reused_without_model_call():
    class Database:
        async def get_scope_review(self, *_args):
            return {
                "status": "accepted",
                "reason": "privacy_redacted",
                "text": "涉事学生收到处分。",
                "diagnostic": None,
            }

    reviewer = InstitutionScopeReviewer(Gateway([]))
    reviewer.bind(Database(), "task", "public_event")
    decision = (await reviewer.review(["涉事男生收到处分。"], kind="report_text"))[0]
    assert decision.text == "涉事男生收到处分。"
    assert decision.allowed and decision.reason == "anonymous_roles_preserved"


@pytest.mark.asyncio
async def test_personal_account_and_direct_identifier_guard_override_cached_approval():
    class Database:
        async def get_scope_review(self, *_args):
            raise AssertionError("identifiers must be checked before cached model approval")

    reviewer = InstitutionScopeReviewer(Gateway([]))
    reviewer.bind(Database(), "task", "public_event")
    decisions = await reviewer.review(
        [
            "微信公众号“个人昵称”刊文，以涉事女生自述口吻描述事件。",
            "校方回应，手机号：13800138000。",
        ],
        kind="claim",
    )
    assert all(d.status == "rejected" and d.reason == "private_identifier" for d in decisions)


@pytest.mark.asyncio
async def test_institution_account_remains_a_public_source():
    reviewer = InstitutionScopeReviewer(Gateway([{"items": [{"id": 0, "allowed": True}]}]))
    text = "微信公众号“学校发布”公布情况通报。"
    decision = (await reviewer.review([text], kind="claim", scope="public_event"))[0]
    assert decision.allowed and decision.text == text


@pytest.mark.asyncio
async def test_personal_account_marker_applies_to_earlier_fact_and_later_resume():
    class Database:
        def __init__(self):
            self.identifiers = {}

        async def fetch_all(self, *_args):
            import json

            return [
                {"fingerprint": key, "payload": json.dumps(value)}
                for key, value in self.identifiers.items()
            ]

        async def save_analysis_batch(self, _task, agent, fingerprint, payload):
            assert agent.startswith("privacy_identifier:")
            assert set(payload) == {"length"}
            self.identifiers[fingerprint] = payload

        async def get_scope_review(self, *_args):
            raise AssertionError("known personal identifiers cannot use cached approvals")

    database = Database()
    reviewer = InstitutionScopeReviewer(Gateway([]))
    reviewer.bind(database, "task", "public_event")
    decisions = await reviewer.review(
        [
            "微信公众号“个人昵称”发表文章。",
            "微信公众号“个人昵称”刊文，以涉事女生自述口吻描述事件。",
        ],
        kind="claim",
    )
    assert all(d.status == "rejected" for d in decisions)
    assert len(database.identifiers) == 1
    resumed = InstitutionScopeReviewer(Gateway([]))
    resumed.bind(database, "task", "public_event")
    assert (await resumed.review(["该文章署名个人昵称。"], kind="report_text"))[
        0
    ].status == "rejected"
