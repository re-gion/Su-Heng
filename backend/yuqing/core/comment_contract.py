"""Deterministic comment sample statistics and reviewed dependency closure."""

from __future__ import annotations

import re
from collections import Counter

COMMENT_STANCES = {"认可", "质疑", "审慎", "其他"}


def focused_evidence_context(context, question_text, limit=8):
    """Select relevant source windows; full bodies stay local and never enter the prompt."""
    words = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]{3,}", question_text)
    terms = {word[i : i + 2] for word in words for i in range(len(word) - 1)} - {
        "样本",
        "评论",
        "认为",
        "有人",
        "希望",
        "是否",
        "材料",
        "当前",
        "武汉",
        "大学",
        "学校",
        "校方",
        "武大",
    }
    ranked = sorted(
        context,
        key=lambda e: (
            -sum(
                t
                in str(e.get("title", ""))
                + str(e.get("claims", ""))
                + str(e.get("_full_text") or e.get("excerpt", ""))
                for t in terms
            )
        ),
    )[:limit]
    result = []
    for source in ranked:
        value = {k: v for k, v in source.items() if not k.startswith("_")}
        value["claims"] = sorted(
            source.get("claims", []), key=lambda c: -sum(t in str(c.get("text", "")) for t in terms)
        )[:4]
        value["claim_scope"] = "当前来源与问题相关的陈述节选，核验状态保持原值"
        body = source.get("_full_text") or source.get("excerpt") or ""
        positions = sorted({body.find(t) for t in terms if body.find(t) >= 0})
        candidates = []
        for position in positions:
            start = max(0, position - 140)
            end = min(len(body), position + 460)
            candidates.append((start, end, {t for t in terms if t in body[start:end]}))
        windows, seen = [], set()
        while candidates and len(windows) < 3:
            start, end, matched = max(
                candidates, key=lambda w: (len(w[2] - seen), len(w[2]), -w[0])
            )
            windows.append((start, end))
            seen.update(matched)
            candidates = [w for w in candidates if w[1] <= start or w[0] >= end]
        windows.sort()
        value["excerpt"] = (
            "\n[…节选…]\n".join(body[a:b] for a, b in windows) if windows else body[:1200]
        )
        value["excerpt_scope"] = "与问题相关的材料节选，不等同于全文或全部公开资料"
        result.append(value)
    return result


def _comment_time_bucket(value):
    text = str(value or "").strip()
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else "时间未知"


def observation_statistics(observations, samples) -> dict:
    by_id = {s["id"]: s for s in samples}
    members = sorted({r for o in observations for r in o["comment_refs"] if r in by_id})
    stances = {
        stance: len({r for o in observations if o["stance"] == stance for r in o["comment_refs"]})
        for stance in COMMENT_STANCES
        if any(o["stance"] == stance for o in observations)
    }
    return {
        "comment_refs": members,
        "sample_count": len(members),
        "observation_count": len(observations),
        "stance_counts": stances,
        "platform_counts": dict(Counter(by_id[r]["platform"] for r in members)),
        "time_counts": dict(
            Counter(_comment_time_bucket(by_id[r].get("published_at")) for r in members)
        ),
    }


def reconcile_questions(block: dict) -> None:
    """Invalidate dependent units rather than silently rewriting their reviewed meaning."""
    samples = {s["id"]: s for s in block.get("samples", [])}
    observations = [
        o
        for o in block.get("observations", [])
        if o.get("review_status") == "accepted"
        and o.get("comment_refs")
        and set(o["comment_refs"]) <= set(samples)
    ]
    block["observations"] = observations
    by_id = {o["id"]: o for o in observations}
    retained = []
    for question in block.get("items", []):
        refs = question.get("observation_refs", [])
        if not refs or not set(refs) <= set(by_id) or question.get("review_status") != "accepted":
            continue
        question.update(observation_statistics([by_id[r] for r in refs], list(samples.values())))
        comparison_ids = {
            c["id"] for c in question.get("comparisons", []) if c.get("review_status") == "accepted"
        }
        question["judgements"] = [
            j
            for j in question.get("judgements", [])
            if j.get("review_status") == "accepted"
            and set(j.get("observation_refs", [])) <= set(refs)
            and set(j.get("comparison_refs", [])) <= comparison_ids
        ]
        retained.append(question)
    block["items"] = retained
    rank = {"立即回应": 0, "补充说明": 1, "持续观察": 2}
    block["priority_order"] = sorted(
        [
            {
                "question_ref": q["id"],
                "title": q["title"],
                "priority": j["priority"],
                "reason": j["priority_reason"],
                "sample_count": q["sample_count"],
                "comment_refs": q["comment_refs"],
            }
            for q in retained
            for j in q.get("judgements", [])
        ],
        key=lambda p: (rank[p["priority"]], -p["sample_count"], p["title"]),
    )
    coverage = block.setdefault("coverage", {})
    members = {r for o in observations for r in o["comment_refs"]}
    grouped = {r for q in retained for r in q["observation_refs"]}
    coverage.update(
        reviewed_observations=len(observations),
        samples_with_observations=len(members),
        reviewed_questions=len(retained),
        reviewed_judgements=sum(len(q.get("judgements", [])) for q in retained),
        ungrouped_observations=len(set(by_id) - grouped),
        in_reviewed_themes=len({r for q in retained for r in q["comment_refs"]}),
        reviewed_themes=len(retained),
    )
    coverage["relevant_without_reviewed_theme"] = max(
        0,
        coverage.get("classified", 0)
        - coverage.get("irrelevant", 0)
        - coverage["in_reviewed_themes"],
    )


def validate_questions(block, evidence_ids):
    """Check bindings, independently accepted layers and exact deterministic counts."""
    errors = []
    try:
        samples = {s["id"]: s for s in block.get("samples", [])}
        observations = {o["id"]: o for o in block.get("observations", [])}
        questions = {q["id"]: q for q in block.get("items", [])}
        if (
            len(samples) != len(block.get("samples", []))
            or len(observations) != len(block.get("observations", []))
            or len(questions) != len(block.get("items", []))
        ):
            errors.append("R24: 评论样本、观察或问题编号重复")
        for o in observations.values():
            refs = o.get("comment_refs", [])
            if (
                not refs
                or len(refs) != len(set(refs))
                or not set(refs) <= set(samples)
                or not o.get("text")
                or o.get("review_status") != "accepted"
                or o.get("stance") not in COMMENT_STANCES
                or o.get("kind") not in {"viewpoint", "reason", "request", "question"}
            ):
                errors.append("R24: 评论观察缺少已审原评论关联")
                continue
            if (
                set(o.get("evidence_refs", [])) != {samples[r]["evidence_ref"] for r in refs}
                or not set(o.get("evidence_refs", [])) <= evidence_ids
            ):
                errors.append("R24: 评论观察来源不匹配")
        grouped_observations = set()
        for q in questions.values():
            summary = q.get("summary")
            if summary is not None and (
                not isinstance(summary, str) or not 0 < len(summary.strip()) <= 160
            ):
                errors.append("R24: 评论速读摘要格式无效或过长")
            if q.get("summary") and q.get("summary_review_status") != "accepted":
                errors.append("R24: 评论速读摘要缺少独立审查")
            refs = q.get("observation_refs", [])
            if (
                not refs
                or len(refs) != len(set(refs))
                or not set(refs) <= set(observations)
                or not q.get("title")
                or q.get("review_status") != "accepted"
            ):
                errors.append("R24: 评论问题缺少已审观察关联")
                continue
            if grouped_observations.intersection(refs):
                errors.append("R24: 同一观察重复归入问题，应拆分观察后关联")
            grouped_observations.update(refs)
            expected = observation_statistics(
                [observations[r] for r in refs], list(samples.values())
            )
            if any(q.get(k) != value for k, value in expected.items()):
                errors.append("R24: 评论问题统计与去重样本不一致")
            if set(q.get("evidence_refs", [])) != {
                e for r in refs for e in observations[r]["evidence_refs"]
            }:
                errors.append("R24: 评论问题来源不匹配")
            comps = {c["id"]: c for c in q.get("comparisons", [])}
            judgements = q.get("judgements", [])
            if len(comps) != len(q.get("comparisons", [])) or len(
                {j["id"] for j in judgements}
            ) != len(judgements):
                errors.append("R24: 评论证据对照或研判编号重复")
            for c in comps.values():
                if (
                    c.get("review_status") != "accepted"
                    or c.get("status") not in {"answered", "partial", "unanswered", "incomplete"}
                    or not c.get("text")
                    or not set(c.get("evidence_refs", [])) <= set(evidence_ids)
                    or c.get("status") == "answered"
                    and not c.get("evidence_refs")
                ):
                    errors.append("R24: 证据对照缺少独立审查或有效公开材料")
            for j in q.get("judgements", []):
                if (
                    j.get("review_status") != "accepted"
                    or j.get("priority") not in {"立即回应", "补充说明", "持续观察"}
                    or not all(
                        j.get(k)
                        for k in (
                            "risk_assessment",
                            "response_action",
                            "priority_reason",
                            "uncertainty",
                            "observation_refs",
                            "comparison_refs",
                            "evidence_refs",
                        )
                    )
                    or not set(j["observation_refs"]) <= set(refs)
                    or not set(j["comparison_refs"]) <= set(comps)
                    or not set(j["evidence_refs"]) <= set(evidence_ids)
                ):
                    errors.append("R24: 评论研判缺少已审依据或独立审查")
        import copy

        expected_block = copy.deepcopy(block)
        reconcile_questions(expected_block)
        if expected_block["priority_order"] != block.get("priority_order", []):
            errors.append("R24: 处置排序未绑定已审研判及去重统计")
        for field in (
            "reviewed_observations",
            "samples_with_observations",
            "reviewed_questions",
            "reviewed_judgements",
            "ungrouped_observations",
        ):
            if block.get("coverage", {}).get(field) != expected_block["coverage"][field]:
                errors.append("R24: 评论覆盖统计与已审产物不一致")
                break
    except (KeyError, TypeError, AttributeError):
        errors.append("R24: 分层评论数据结构不完整")
    return errors
