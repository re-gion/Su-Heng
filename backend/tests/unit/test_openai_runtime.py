from types import SimpleNamespace

import pytest

from yuqing.agents.openai_runtime import OpenAIInvestigationAgent


class StaticGateway:
    def __init__(self, result: dict):
        self.result = result

    async def complete_json(self, *_args, **_kwargs):
        return self.result


@pytest.mark.asyncio
async def test_summarize_drops_claim_with_unsupported_statement_kind():
    gateway = StaticGateway(
        {
            "claims": [
                {
                    "text": "部分报道持批评意见。",
                    "statement_kind": "opinion",
                    "evidence_ids": ["E001"],
                },
                {
                    "text": "某媒体于当日发布了相关报道。",
                    "statement_kind": "fact",
                    "evidence_ids": ["E001"],
                },
            ]
        }
    )
    agent = OpenAIInvestigationAgent(gateway, "system", role="analyst_b")
    evidence = [
        SimpleNamespace(local_id="E001", title="公开报道", snippet="该媒体于当日发布报道。")
    ]

    claims = await agent.summarize("测试事件", evidence)

    assert [claim.text for claim in claims] == ["某媒体于当日发布了相关报道。"]
    assert claims[0].statement_kind == "fact"
