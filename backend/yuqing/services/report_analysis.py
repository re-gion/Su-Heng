"""有证据边界的报告编辑：模型选择与解释，代码回填事实及数字。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any

from yuqing.core.measurements import MEASUREMENT, measurement_values

NUMBER = re.compile(r"\d+(?:[.,]\d+)*(?:%|％)?")
ANALYSIS_FIELDS = (
    "title",
    "interpretation",
    "implication",
    "action",
    "owner",
    "trigger",
    "uncertainty",
)


def event_timeline(
    facts: list[dict], *, date_from: str | None = None, date_to: str | None = None
) -> list[dict]:
    """Use an explicit action date at the start or just after its named actor."""
    candidates = []
    for fact in distinct_facts(facts, limit=len(facts) or 1):
        if fact.get("origin_agent") == "history_insight":
            continue
        match = re.match(r"^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", fact["text"])
        if not match:
            match = re.match(
                r"^[^\d，。；;：:]{2,24}?(?:于|在)?\s*"
                r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
                r"(?=\s*(?:发布|公布|通报|回应|宣布|召开|作出|启动|提交|披露))",
                fact["text"],
            )
        if not match:
            match = re.match(
                r"^[^\d，。；;：:]{2,24}?(?:于|在)\s*"
                r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
                r"(?=[^。；;]{0,40}(?:发布|公布|通报|回应|宣布|召开|作出|启动|提交|披露|判决|裁定|维持|撤销))",
                fact["text"],
            )
        if not match:
            continue
        try:
            stamp = date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            continue
        window_label = (
            "重点窗口前的本事件经过"
            if date_from and stamp < date_from[:10]
            else "重点窗口后的本事件进展"
            if date_to and stamp > date_to[:10]
            else None
        )
        candidates.append(
            {
                "date": stamp,
                "text": fact["text"],
                "window_label": window_label,
                "claim_refs": [fact["claim_ref"]],
                "evidence_refs": [c["evidence_ref"] for c in fact.get("citations", [])],
                "badge": fact.get("badge", "unverified"),
                "source_name": "时点取自陈述正文",
            }
        )
    # 每天不重复铺陈多家转载，但完整陈述及对应来源仍保留在核查记录。
    per_day: Counter[str] = Counter()
    chosen = []
    for item in candidates:
        if per_day[item["date"]] < 2:
            chosen.append(item)
            per_day[item["date"]] += 1
    chosen.sort(key=lambda item: item["date"])
    return select_priority_timeline_nodes(chosen)


def select_priority_timeline_nodes(items: list[dict], limit: int = 12) -> list[dict]:
    """Keep the user's focus window visible when a long before/after history is shown."""
    if len(items) <= limit:
        return items
    focused = [item for item in items if not item.get("window_label")]
    if not focused:
        return items[:4] + items[-(limit - 4) :]
    if len(focused) >= limit:
        return focused[:4] + focused[-(limit - 4) :]
    before = [
        item for item in items if item.get("window_label") and item["date"] < focused[0]["date"]
    ]
    after = [
        item for item in items if item.get("window_label") and item["date"] > focused[-1]["date"]
    ]
    selected = list(focused)
    while len(selected) < limit and (before or after):
        if before:
            selected.append(before.pop())
        if after and len(selected) < limit:
            selected.append(after.pop(0))
    return sorted(selected, key=lambda item: item["date"])


def _text(value: Any, limit: int = 600) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _refs(value: Any) -> list[str]:
    return (
        list(dict.fromkeys(item for item in value if isinstance(item, str)))
        if isinstance(value, list)
        else []
    )


def distinct_facts(facts: Sequence[dict], limit: int = 6) -> list[dict]:
    """仅摘要选材去重，不合并数据库陈述或核验结论；数字/徽章不同必须保留。"""
    chosen: list[dict] = []
    ordered = sorted(
        facts,
        key=lambda x: (
            x.get("origin_agent") == "history_insight",
            x.get("badge") != "verified",
            x.get("verification_state", "complete") != "complete",
        ),
    )
    for fact in ordered:
        text = re.sub(r"\W", "", fact["text"])
        pairs = {text[i : i + 2] for i in range(len(text) - 1)}
        duplicate = False
        for previous in chosen:
            if fact.get("badge") != previous.get("badge") or NUMBER.findall(
                fact["text"]
            ) != NUMBER.findall(previous["text"]):
                continue
            old = re.sub(r"\W", "", previous["text"])
            old_pairs = {old[i : i + 2] for i in range(len(old) - 1)}
            if text == old or (pairs and len(pairs & old_pairs) / len(pairs | old_pairs) > 0.72):
                duplicate = True
                break
        if not duplicate:
            chosen.append(fact)
        if len(chosen) >= limit:
            break
    return chosen


def report_context(
    task: dict, facts: list[dict], evidence: Sequence[Any], forum: Sequence[Any]
) -> dict:
    """每份材料公平分配窗口，优先已引用原文与含数字句，避免前段截断吞掉后续轮次。"""
    cited = {c["evidence_ref"] for f in facts for c in f.get("citations", [])}
    ordered = sorted(
        evidence,
        key=lambda e: (e.local_id not in cited, e.fetch_status != "fetched", e.source_tier),
    )[:32]
    inventory = []
    for source in ordered:
        content_origin = (getattr(source, "extra", None) or {}).get("content_origin")
        content = (
            source.content_text
            if source.fetch_status == "fetched" or content_origin == "provider_fulltext"
            else source.snippet
        )
        content = content or ""
        sentences = re.split(r"(?<=[。！？\n])", content)
        measurements = [s.strip()[:220] for s in sentences if MEASUREMENT.search(s)][:2]
        inventory.append(
            {
                "evidence_ref": source.local_id,
                "title": source.title,
                "source_name": source.source_name
                or source.publisher_entity
                or source.source_domain,
                "source_role": source.source_role,
                "published_at": source.published_at,
                "fetch_status": source.fetch_status,
                "content_origin": content_origin,
                "excerpt": _focused_excerpt(content, source.local_id, facts),
                "excerpt_is_preview": len(content) > 2400,
                "measurement_passages": measurements,
            }
        )
    # 论坛只提供待解决的问题，不能拿其分号拼接总结冒充最终分析。
    gaps = list(
        dict.fromkeys(_text(m.content, 300) for m in forum if m.type in {"question", "conflict"})
    )[-10:]
    return {
        "task": task,
        "audience": "公众读者与高校或机构决策者：先解释事件与分歧，再提供有依据的处置建议",
        "facts": [
            {
                **{
                    k: f.get(k)
                    for k in (
                        "claim_ref",
                        "text",
                        "badge",
                        "verdict",
                        "verification_state",
                        "origin_agent",
                        "independent_sources",
                        "evidence_grade",
                    )
                },
                "citations": [
                    {
                        "evidence_ref": c["evidence_ref"],
                        "relation": c.get("relation"),
                        "quote": _text(c.get("quote"), 240),
                    }
                    for c in f.get("citations", [])[:3]
                ],
            }
            for f in facts
        ],
        "sources": inventory,
        "open_questions": gaps,
        "coverage": {"available_sources": len(evidence), "context_sources": len(inventory)},
    }


def _focused_excerpt(content: str, source_ref: str, facts: list[dict]) -> str:
    """Keep the source opening and passages actually used by the cited facts."""
    if len(content) <= 2400:
        return content
    windows = [(0, 700)]
    for fact in facts:
        for citation in fact.get("citations", []):
            if citation.get("evidence_ref") != source_ref:
                continue
            quote = citation.get("quote") or fact.get("text") or ""
            if len(quote) < 8:
                continue
            offset = content.find(quote)
            if offset >= 0:
                windows.append((max(0, offset - 180), min(len(content), offset + len(quote) + 180)))
    merged = []
    for start, end in sorted(set(windows)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    # Put cited passages first so a smaller chapter window still contains the basis.
    merged.sort(key=lambda window: window[0] == 0)
    return "\n…（中间内容省略）…\n".join(content[a:b] for a, b in merged)[:2400]


def assemble_analysis(
    enrichment: Any,
    facts: list[dict],
    evidence: Sequence[Any],
    *,
    propagation_edges: int | None = None,
) -> tuple[dict, list[dict], dict]:
    """丢弃越权字段和失配引用；事实观察始终来自权威陈述，数字只能逐字来自原文。"""
    result = enrichment if isinstance(enrichment, dict) else {}
    fact_map = {f["claim_ref"]: f for f in facts}
    source_map = {e.local_id: e for e in evidence}
    rejected: Counter[str] = Counter()
    authoritative = {"metrics", "task", "blocks", "badge", "verdict", "citations", "text"}
    rejected["authoritative_override"] = len(authoritative & result.keys())
    rejected += Counter()  # 去掉零值，报告只说明实际发生的拦截。
    selected = [fact_map[r] for r in _refs(result.get("summary_claim_refs")) if r in fact_map][:6]
    selected = distinct_facts(
        [f for f in (selected or facts) if f.get("origin_agent") != "history_insight"]
    )
    summary = {
        "what": [{"claim_ref": f["claim_ref"], "text": f["text"]} for f in selected],
        "why": [],
        "so_what": [],
    }
    sections: dict[str, list[dict]] = {"04": [], "05": [], "06": [], "07": []}
    seen: set[str] = set()
    items = result.get("analyses", [])
    for proposed in items[:16] if isinstance(items, list) else []:
        if not isinstance(proposed, dict):
            rejected["analysis_shape"] += 1
            continue
        section = proposed.get("section")
        refs = _refs(proposed.get("claim_refs"))
        if (
            section not in sections
            or not refs
            or len(refs) > 6
            or any(ref not in fact_map for ref in refs)
        ):
            rejected["analysis_reference"] += 1
            continue
        if section == "06" and (
            len(refs) < 2
            or not any(fact_map[r].get("origin_agent") == "history_insight" for r in refs)
            or not any(fact_map[r].get("origin_agent") != "history_insight" for r in refs)
        ):
            rejected["history_comparison_basis"] += 1
            continue
        fields = {
            key: _text(proposed.get(key), 120 if key == "title" else 500) for key in ANALYSIS_FIELDS
        }
        if not all(
            fields[k] for k in ("title", "interpretation", "implication", "uncertainty")
        ) or (section == "07" and not all(fields[k] for k in ("action", "owner", "trigger"))):
            rejected["analysis_incomplete"] += 1
            continue
        if propagation_edges == 0 and re.search(
            r"(?:争议|舆论|谣言|传闻|网传|不实信息|讨论).{0,12}"
            r"(?:扩散|放大|升温|降温|收敛|转向|增加|减少)",
            fields["interpretation"] + " " + fields["implication"],
        ):
            rejected["unsupported_propagation_effect"] += 1
            continue
        # 模型不能借“编辑判断”编新数值、比例或概率。连建议时限也用自然条件而非臆定数字。
        basis = "\n".join(fact_map[r]["text"] for r in refs)
        numeric_basis = set(NUMBER.findall(basis))
        numeric_text = " ".join(fields.values())
        for ref in refs:
            numeric_text = numeric_text.replace(ref, "")
        if any(n not in numeric_basis for n in NUMBER.findall(numeric_text)) or (
            measurement_values(numeric_text) - measurement_values(basis)
        ):
            rejected["unsupported_number"] += 1
            continue
        key = re.sub(r"\W", "", fields["interpretation"])
        if key in seen or len(sections[section]) >= 4:
            rejected["duplicate_analysis"] += 1
            continue
        seen.add(key)
        bound = list(
            dict.fromkeys(c["evidence_ref"] for r in refs for c in fact_map[r].get("citations", []))
        )
        requested = _refs(proposed.get("evidence_refs"))
        if requested and not set(requested).issubset(bound):
            rejected["analysis_binding"] += 1
            continue
        unconfirmed = [r for r in refs if fact_map[r].get("badge") != "verified"]
        if unconfirmed:
            boundary = (
                "所据陈述尚未全部证实（"
                + "、".join(unconfirmed)
                + "），以下研判以其后续成立为前提。"
            )
            if not fields["uncertainty"].startswith(boundary):
                fields["uncertainty"] = boundary + fields["uncertainty"]
        sections[section].append(
            {
                **fields,
                "observation": basis,
                "claim_refs": refs,
                "evidence_refs": requested or bound,
            }
        )
    # If the dedicated action chapter fails review, reuse only actions already
    # accepted with a substantive chapter. They retain the same evidence and
    # uncertainty; this adds no fresh assertion or unreviewed recommendation.
    if not sections["07"]:
        for source_section in ("04", "05"):
            for item in sections[source_section]:
                if all(item.get(key) for key in ("action", "owner", "trigger")):
                    sections["07"].append(
                        {
                            **item,
                            "title": f"行动建议：{item['title']}",
                            "derived_from_section": source_section,
                        }
                    )
                if len(sections["07"]) >= 2:
                    break
            if sections["07"]:
                break
    blocks = []
    for section, title in (
        ("04", "传播路径与回应缺口"),
        ("05", "核心议题与立场分歧"),
        ("06", "历史案例的可比性与启示"),
        ("07", "决策重点与行动建议"),
    ):
        if (
            section == "07"
            and sections[section]
            and all(item.get("derived_from_section") for item in sections[section])
        ):
            blocks.append(
                {
                    "block_id": "b_07_action_plan",
                    "type": "action_plan",
                    "section": "07",
                    "in_brief": True,
                    "title": "据已审分析形成的行动清单",
                    "is_editorial": True,
                    "editorial_basis": "仅提取第 04 或 05 章已通过审查的行动、负责职能、触发条件与不确定性，不新增事实判断",
                    "items": [
                        {
                            key: item[key]
                            for key in (
                                "title",
                                "action",
                                "owner",
                                "trigger",
                                "uncertainty",
                                "claim_refs",
                                "evidence_refs",
                                "derived_from_section",
                            )
                        }
                        for item in sections[section]
                    ],
                }
            )
        elif sections[section]:
            blocks.append(
                {
                    "block_id": f"b_{section}_analysis",
                    "type": "analysis",
                    "section": section,
                    "in_brief": section == "07",
                    "title": title,
                    "is_editorial": True,
                    "editorial_basis": "下列观察由数据库陈述回填；解释、影响与行动是有条件的分析判断，不是新增已核验事实",
                    "items": sections[section],
                }
            )
    summary["why"] = [
        {
            "text": item["title"],
            "claim_refs": item["claim_refs"],
            "evidence_refs": item["evidence_refs"],
            "uncertainty": item["uncertainty"],
            "is_editorial": True,
        }
        for section in (("04", "05") if sections["04"] or sections["05"] else ("07",))
        for item in sections[section]
    ][:3]
    summary["so_what"] = [
        {
            "text": item["title"],
            "claim_refs": item["claim_refs"],
            "evidence_refs": item["evidence_refs"],
            "uncertainty": item["uncertainty"],
            "is_editorial": True,
        }
        for item in sections["07"]
    ][:3]
    metric_items = []
    measures = result.get("measurements", [])
    metric_seen: dict[tuple, dict] = {}
    for item in measures[:8] if isinstance(measures, list) else []:
        if not isinstance(item, dict):
            rejected["measurement_shape"] += 1
            continue
        ref = item.get("evidence_ref")
        source = source_map.get(ref) if isinstance(ref, str) else None
        quote = _text(item.get("quote"), 350)
        value = _text(item.get("value_text"), 40)
        label = _text(item.get("label"), 60)
        refs = _refs(item.get("claim_refs"))
        # 引用数字除了存在于原文，还必须绑定本任务事实；推荐流中的数字不能直接进入正文。
        linked = [
            r
            for r in refs
            if r in fact_map
            and value in measurement_values(fact_map[r]["text"])
            and ref in {c["evidence_ref"] for c in fact_map[r].get("citations", [])}
        ]
        if (
            not source
            or source.fetch_status != "fetched"
            or not quote
            or quote not in (source.content_text or "")
            or not value
            or value not in measurement_values(quote)
            or not MEASUREMENT.fullmatch(value)
            or not label
            or label not in quote
            or not linked
        ):
            rejected["measurement_not_grounded"] += 1
            continue
        if label == value:
            prefix = re.split(r"[。；！？]", quote[: quote.index(value)])[-1].strip(" ，、：")
            if prefix:
                label = prefix[-48:]
        # Same disclosure can be syndicated under different publishers. Only merge
        # identical passages bound to overlapping facts; equal numbers alone are not enough.
        key = (re.sub(r"\s+", "", quote), value, label, tuple(sorted(linked)))
        if key in metric_seen:
            previous = metric_seen[key]
            previous["evidence_refs"] = list(dict.fromkeys([*previous["evidence_refs"], ref]))
            continue
        metric_items.append(
            {
                "label": label,
                "value": value,
                "unit": "",
                "scope": quote,
                "period": "以来源原话中的时间及范围为准；未披露部分不可补推",
                "quote": quote,
                "evidence_refs": [ref],
                "claim_refs": linked,
                "source_name": source.source_name
                or source.publisher_entity
                or source.source_domain,
                "verification_note": "来源披露值，已与取得的原文逐字匹配；未独立复算，不代表全网总体，不可跨来源直接相加",
            }
        )
        metric_seen[key] = metric_items[-1]
    if metric_items:
        blocks.insert(
            0,
            {
                "block_id": "b_04_measurements",
                "type": "metric_cards",
                "section": "03",
                "in_brief": False,
                "title": "事件事实中的来源披露数据",
                "data_basis": "quoted_evidence",
                "items": metric_items,
            },
        )
    quality = {
        "status": "analysis_available"
        if sections["07"] and (sections["04"] or sections["05"])
        else "evidence_brief",
        "analysis_items": sum(map(len, sections.values())),
        "sourced_measurements": len(metric_items),
        "rejected_items": dict(rejected),
        "semantic_review": result.get("analysis_review", {"status": "not_provided"}),
    }
    return summary, blocks, quality


def deduplicate_limitations(items: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for item in items:
        text = _text(item.get("text"), 2000)
        if text and text not in seen:
            seen.add(text)
            result.append({**item, "text": text})
    return result
