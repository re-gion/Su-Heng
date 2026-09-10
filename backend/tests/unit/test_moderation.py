import pytest

from yuqing.services.moderation import OpenAIModerator


class SequencedGateway:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0
        self.requests = []

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        self.requests.append((args, kwargs))
        return self.values.pop(0)


@pytest.mark.asyncio
async def test_moderator_retries_schema_failure_and_accepts_corrected_review():
    gateway = SequencedGateway(
        [
            {"gaps": ["缺少官方回应"], "release": False, "reason": "结构错误"},
            {
                "gaps": [
                    {"agent": "fact_investigator", "desc": "缺少官方回应", "priority": "high"}
                ],
                "blind_spots": [],
                "conflicts": [],
                "unresolved_critical": [],
                "directives": [{"agent": "fact_investigator", "instruction": "补查官方回应"}],
                "release": False,
                "reason": "需继续补查",
            },
        ]
    )
    review = await OpenAIModerator(gateway, "主持人").review("事件", [], 3, 1)
    assert gateway.calls == 2
    assert review.gaps[0].desc == "缺少官方回应"
    assert review.degraded is False
    assert '"suggested_query"' in gateway.requests[0][0][2]


@pytest.mark.asyncio
async def test_moderator_falls_back_after_two_invalid_schema_responses():
    gateway = SequencedGateway([{"gaps": ["坏"]}, {"directives": ["仍坏"]}])
    review = await OpenAIModerator(gateway, "主持人").review("事件", [], 3, 1)
    assert review.release is False
    assert review.degraded is True
    assert review.unresolved_critical
    assert any(gap.priority == "high" for gap in review.gaps)
    assert review.directives
    assert any("gaps.0" in item for item in review.diagnostics)
    assert any("directives.0" in item for item in review.diagnostics)
