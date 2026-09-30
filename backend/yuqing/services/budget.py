"""三档调查深度的预算表（02 §8.4）。

**单一事实源**。此前同一份 token 上限在 `v1_orchestrator` 里存在两份完整副本
（其中真正生效的是第二份），`max_claims`/`max_evidence_per_claim` 藏在 storage 层，
`app/main.py` 又抄了一份 outer rounds 和一份评论预算。调一次档要同时改五六处，
漏改一处就得到自相矛盾的档位。现在集中在这里定义，上层读取后注入。

数值约束（2026-09 真机实测）：一条 claim 平均消耗约 2.4 条核验关系，因此
`max_verify_calls` 必须随 `max_claims` 同比例上调，否则超出的 claim 会被
"核验预算不足"整条跳过、强制降级为待核验。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import Any

# 分档预算的全部可调字段。新增字段时必须同步 DEFAULT_BUDGET_TABLE 与
# 前端设置页，否则该字段无法被配置覆盖（resolve_budget_table 会显式报错）。
BUDGET_FIELDS = (
    "token_limit",
    "search_calls",
    "fetch_calls",
    "max_claims",
    "max_evidence_per_claim",
    "max_verify_calls",
    "outer_rounds",
    "top_k",
    "queries_per_round",
    "comment_posts",
    "comments_per_post",
)


@dataclass(frozen=True)
class DepthBudget:
    token_limit: int
    search_calls: int
    fetch_calls: int
    max_claims: int
    max_evidence_per_claim: int
    max_verify_calls: int
    outer_rounds: int
    top_k: int
    queries_per_round: int
    comment_posts: int
    comments_per_post: int


# 各档位之间保持自洽：verify ≈ claims × 2.4 关系，token 按每条 claim 约 1.1 万 token 估。
DEFAULT_BUDGET_TABLE: dict[str, DepthBudget] = {
    "quick": DepthBudget(
        token_limit=300_000,
        search_calls=18,
        fetch_calls=15,
        max_claims=15,
        max_evidence_per_claim=3,
        max_verify_calls=40,
        outer_rounds=1,
        top_k=5,
        queries_per_round=1,
        comment_posts=2,
        comments_per_post=100,
    ),
    "standard": DepthBudget(
        token_limit=1_400_000,
        search_calls=75,
        fetch_calls=70,
        max_claims=60,
        max_evidence_per_claim=4,
        max_verify_calls=150,
        outer_rounds=2,
        top_k=8,
        queries_per_round=3,
        comment_posts=5,
        comments_per_post=200,
    ),
    "deep": DepthBudget(
        token_limit=2_000_000,
        search_calls=140,
        fetch_calls=120,
        max_claims=90,
        max_evidence_per_claim=6,
        max_verify_calls=230,
        outer_rounds=3,
        top_k=10,
        queries_per_round=4,
        comment_posts=8,
        comments_per_post=300,
    ),
}


def resolve_budget_table(
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, DepthBudget]:
    """把覆盖值合并到默认表上；未知档位或未知字段直接报错，不静默忽略。"""
    table = dict(DEFAULT_BUDGET_TABLE)
    for depth, values in (overrides or {}).items():
        if depth not in table:
            raise ValueError(f"未知的调查深度：{depth}")
        if not isinstance(values, dict):
            raise ValueError(f"{depth} 的预算覆盖必须是对象")
        unknown = set(values) - set(BUDGET_FIELDS)
        if unknown:
            raise ValueError(f"{depth} 存在未知预算字段：{sorted(unknown)}")
        patch = {}
        for name, raw in values.items():
            number = int(raw)
            if number <= 0:
                raise ValueError(f"{depth}.{name} 必须是正整数")
            patch[name] = number
        table[depth] = replace(table[depth], **patch)
    return table


def budget_table_to_json(table: dict[str, DepthBudget]) -> dict[str, dict[str, int]]:
    return {depth: asdict(value) for depth, value in table.items()}


def parse_budget_overrides(raw: str | None) -> dict[str, dict[str, Any]]:
    """解析配置表里的 BUDGET_OVERRIDES（JSON）。空值表示全部走默认表。"""
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"BUDGET_OVERRIDES 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("BUDGET_OVERRIDES 必须是对象")
    return parsed


def dump_budget_overrides(overrides: dict[str, dict[str, Any]] | None) -> str:
    return json.dumps(overrides or {}, ensure_ascii=False, sort_keys=True)
