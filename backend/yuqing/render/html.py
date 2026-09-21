from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

BADGE_LABELS = {
    "verified": "已证实",
    "unverified": "待核验",
    "disputed": "有争议",
    "refuted": "已证伪",
}

RELATION_LABELS = {
    "support": "支持",
    "partial": "部分支持",
    "contradict": "反证",
    "not_mentioned": "未提及",
    "unverified": "未核验",
}

SECTION_LABELS = {
    "00": "调查概览",
    "01": "执行摘要",
    "02": "事件与回应脉络",
    "03": "关键事实核查",
    "04": "传播与回应分析",
    "05": "观点与评论样本",
    "06": "历史对照",
    "07": "研判与建议",
    "08": "局限性声明",
    "09": "证据与数据附录",
}


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _citation_link(evidence_ref: str) -> str:
    ref = _escape(evidence_ref)
    return f'<a class="citation" href="#evidence-{ref}" aria-label="查看证据 {ref}">[{ref}]</a>'


def _claim_link(claim_ref: str) -> str:
    ref = _escape(claim_ref)
    return f'<a class="claim-link" href="#claim-{ref}" aria-label="查看事实 {ref}">[{ref}]</a>'


def _summary(block: dict[str, Any], claim_badges: dict[str, str]) -> str:
    groups = []
    for key, title in (
        ("what", "关键进展及其核验状态"),
        ("why", "为何引发关注"),
        ("so_what", "目前意味着什么"),
    ):
        rendered_items = []
        for item in block.get(key, []):
            if item.get("is_editorial"):
                refs = "".join(_claim_link(ref) for ref in item.get("claim_refs", []))
                refs += "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
                rendered_items.append(
                    '<li><span class="badge strength" title="有证据边界的分析判断">分析判断</span>'
                    f"{_escape(item.get('text'))}{refs}"
                    f'<small class="verify-state">不确定性：{_escape(item.get("uncertainty"))}</small></li>'
                )
                continue
            claim_ref = str(item.get("claim_ref") or "")
            badge = claim_badges.get(claim_ref, "unverified")
            badge_label = BADGE_LABELS.get(badge, badge)
            rendered_items.append(
                f'<li><span class="badge {badge}" title="事实核查结论">'
                f"{_escape(badge_label)}</span>{_escape(item.get('text'))}"
                f"{_claim_link(claim_ref)}</li>"
            )
        items = "".join(rendered_items)
        if items:
            groups.append(f"<h3>{title}</h3><ul>{items}</ul>")
    return f'<section id="summary"><h2>执行摘要</h2><p class="lede">{_escape(block.get("lede"))}</p>{"".join(groups)}</section>'


def _fact_table(block: dict[str, Any]) -> str:
    cards: list[tuple[str, str]] = []
    for item in block.get("items", []):
        badge = item.get("badge", "unverified")
        citations = "".join(
            _citation_link(citation.get("evidence_ref")) for citation in item.get("citations", [])
        )
        note = ""
        if item.get("evidence_grade") == "snippet_only":
            citation_notes = [
                str(citation.get("note") or "") for citation in item.get("citations", [])
            ]
            failed = sum("抓取失败" in value for value in citation_notes)
            not_attempted = max(0, len(citation_notes) - failed)
            if failed and not_attempted:
                strength = f"原文：{failed} 条抓取失败 · {not_attempted} 条仅有摘要"
            elif failed:
                strength = "原文抓取失败"
            else:
                strength = "仅有搜索摘要"
            note = (
                f'<span class="strength" title="当前引用未取得完整原文">{_escape(strength)}</span>'
            )
        # 核验未完成与"证据不支持"同为待核验黄标，但成因不同：前者是没跑完（上游不可用
        # 或预算截断），后者是跑完了没有支持。必须让读者分得清，否则会把"没核完"读成"没依据"。
        state_chip = ""
        if item.get("verification_state", "complete") != "complete":
            state = item["verification_state"]
            state_label = {
                "incomplete": "核验未完成",
                "skipped": "核验被预算跳过",
                "pending": "尚未核验",
            }.get(state, state)
            state_chip = (
                f'<span class="verify-state" title="{_escape(item.get("verify_reason"))}">'
                f"{_escape(state_label)}</span>"
            )
        correction = (
            f'<p class="correction">实际情况：{_escape(item.get("correction_text"))}</p>'
            if item.get("correction_text")
            else ""
        )
        rumor = (
            f'<p class="rumor">网络流传：{_escape(item.get("rumor_text"))}</p>'
            if item.get("rumor_text")
            else ""
        )
        claim_ref = str(item.get("claim_ref") or "")
        cards.append(
            (
                claim_ref,
                f'<article class="claim-card" id="claim-{_escape(claim_ref)}">'
                f'<div class="claim-meta"><span class="badge {badge}">{BADGE_LABELS.get(badge, badge)}</span>{note}{state_chip}</div>'
                f'<h3>{_escape(item.get("text"))}</h3>{rumor}{correction}<div class="claim-refs">{citations}</div></article>',
            )
        )
    requested = [str(ref) for ref in block.get("priority_claim_refs", []) if ref]
    priority_refs = set(requested[:8])
    if requested:
        cards_by_ref = dict(cards)
        visible = [cards_by_ref[ref] for ref in requested[:8] if ref in cards_by_ref]
        remaining = [card for ref, card in cards if ref not in priority_refs]
    else:
        visible = [card for _, card in cards[:8]]
        remaining = [card for _, card in cards[8:]]
    overflow = (
        '<details class="claim-overflow"><summary>'
        f"其余陈述与核验记录（{len(remaining)}）</summary>{''.join(remaining)}</details>"
        if remaining
        else ""
    )
    return f'<section id="facts"><h2>关键事实核查</h2>{"".join(visible)}{overflow}</section>'


def _limitations(block: dict[str, Any]) -> str:
    items = "".join(
        f"<li><strong>{_escape(item.get('category'))}</strong>：{_escape(item.get('text'))}</li>"
        for item in block.get("items", [])
    )
    return f'<section id="limitations"><h2>局限性声明</h2><ul>{items}</ul></section>'


def _appendix(block: dict[str, Any]) -> str:
    cited_cards = []
    uncited_cards = []
    for item in block.get("items", []):
        ref = _escape(item.get("evidence_ref"))
        state = item.get("fetch_status")
        strength = (
            "已存原文快照"
            if state == "fetched"
            else ("原文抓取失败" if state == "fetch_failed" else "原文未取得")
        )
        snapshot = (
            f'<a href="/api/evidence/{_escape(item.get("snapshot_pk"))}/snapshot">查看清洗快照</a>'
            if state == "fetched" and item.get("snapshot_pk")
            else ""
        )
        quotes = "".join(
            f"<blockquote><span>{_claim_link(citation.get('claim_ref'))}</span>"
            f"{_escape(citation.get('quote') or '未记录关键句')} "
            f"<small>{_escape(citation.get('relation') or '未核验')} · "
            f"{_escape(citation.get('note') or '')}</small></blockquote>"
            for citation in item.get("citations", [])
        )
        excerpt_label = "原文" if state == "fetched" else "搜索摘要（非原文）"
        original = (
            f'<blockquote class="original"><strong>{excerpt_label}（{_escape(item.get("lang") or "unknown")}）</strong><br>{_escape(item.get("original_excerpt"))}</blockquote>'
            if item.get("original_excerpt")
            else ""
        )
        translation = (
            f'<blockquote class="translation"><strong>机器翻译（中文，仅供阅读，不参与逐字核验）</strong><br>{_escape(item.get("machine_translation_zh"))}</blockquote>'
            if item.get("machine_translation_zh")
            else ""
        )
        card = (
            f'<details class="evidence-card" id="evidence-{ref}"><summary>'
            f'<span class="evidence-id">{ref}</span><span class="evidence-title">{_escape(item.get("title") or "未命名证据")}</span>'
            f'<span class="evidence-source"><a href="{_escape(item.get("url"))}" rel="noreferrer">{_escape(item.get("source_name") or "来源未标注")}</a> · L{_escape(item.get("source_tier") or "?")}</span>'
            f'<span class="evidence-status">{_escape(strength)}</span></summary><div class="evidence-body">'
            f'<p class="published">发布日期：{_escape(item.get("published_at") or "未知")}</p>'
            f'<p class="published">证据类型：{_escape(item.get("kind") or "web")} · 语言：{_escape(item.get("lang") or "unknown")} · 范围：{_escape(item.get("scope_label") or item.get("scope_status") or "未分类")}</p>'
            f'{quotes}{original}{translation}<p><a href="{_escape(item.get("url"))}" rel="noreferrer">访问公开来源</a> {snapshot}</p>'
            "</div></details>"
        )
        if item.get("cited_in_report", True):
            cited_cards.append(card)
        else:
            uncited_cards.append(card)
    uncited_group = (
        '<details class="uncited-evidence-group"><summary>'
        f"未被正文引用的证据（{len(uncited_cards)}）</summary>{''.join(uncited_cards)}</details>"
        if uncited_cards
        else ""
    )
    return (
        f'<section id="evidence"><h2>证据卡片</h2>{"".join(cited_cards)}{uncited_group}</section>'
    )


def _basis_label(value: Any) -> str:
    return {
        "evidence_database": "本任务去重检索材料与抓取状态",
        "claim_evidence_database": "关键陈述与引用材料的核验关系",
        "hot_snapshot_database": "本地热榜快照真实采集",
        "quoted_evidence": "引文所载数值（仅按来源原口径转述）",
    }.get(value, str(value or "未标注"))


def _chart(block: dict[str, Any]) -> str:
    title = _escape(block.get("title") or "数据板块")
    items = block.get("items", [])
    kind = block.get("chart_kind")
    if kind == "series":
        return (
            '<section class="chart-series"><h2>'
            + title
            + "</h2>"
            + "".join(_chart(series) for series in block.get("series", []))
            + "</section>"
        )
    note = f'<p class="chart-note">{_escape(block.get("note"))}</p>' if block.get("note") else ""
    fallback = (
        f'<p class="fallback-note">{_escape(block.get("fallback_text"))}</p>'
        if block.get("fallback_text")
        else ""
    )
    if kind == "funnel":
        maximum = max((float(item.get("value", 0)) for item in items), default=1) or 1
        stages = "".join(
            f'<div class="funnel-stage"><div><span>{_escape(item.get("label"))}</span>'
            f"<strong>{_escape(item.get('value'))}</strong></div>"
            f'<i style="--funnel:{float(item.get("value", 0)) / maximum:.3f}"></i></div>'
            for item in items
        )
        return (
            f'<section class="chart funnel-chart" id="{_escape(block.get("block_id"))}">'
            f"<h2>{title}</h2>{note}{stages}{fallback}<small>数据口径："
            f"{_escape(_basis_label(block.get('data_basis')))}</small></section>"
        )
    if kind == "matrix":
        rows = []
        for item in items:
            relations = item.get("relations") or {}
            cells = "".join(
                f'<td class="relation relation-{key}">{_escape(relations.get(key, 0))}</td>'
                for key in ("support", "partial", "contradict", "not_mentioned", "unverified")
            )
            badge = item.get("badge", "unverified")
            rows.append(
                f'<tr><th scope="row"><a href="#claim-{_escape(item.get("claim_ref"))}">'
                f"{_escape(item.get('claim_ref'))}</a><span>{_escape(item.get('text'))}</span></th>"
                f"{cells}<td>{_escape(item.get('independent_sources', 0))}</td>"
                f'<td><span class="badge {badge}">{_escape(BADGE_LABELS.get(badge, badge))}</span></td></tr>'
            )
        headings = "".join(
            f'<th scope="col">{_escape(RELATION_LABELS[key])}</th>'
            for key in ("support", "partial", "contradict", "not_mentioned", "unverified")
        )
        table = (
            '<div class="matrix-scroll"><table class="verification-matrix"><thead><tr>'
            f'<th scope="col">关键陈述</th>{headings}<th scope="col">独立支持</th>'
            '<th scope="col">结论</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
        return (
            f'<section class="chart matrix-chart" id="{_escape(block.get("block_id"))}">'
            f"<h2>{title}</h2>{note}{table if rows else fallback}<small>数据口径："
            f"{_escape(_basis_label(block.get('data_basis')))}</small></section>"
        )
    if kind == "timeline":
        nodes = []
        for item in items:
            relations = item.get("relations") or {}
            chips = "".join(
                f'<span class="relation-chip relation-{key}">{_escape(RELATION_LABELS.get(key, key))} {_escape(value)}</span>'
                for key, value in relations.items()
                if value
            )
            refs = "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
            claims = " ".join(_claim_link(ref) for ref in item.get("claim_refs", []))
            if item.get("badge"):
                badge = item["badge"]
                chips += f'<span class="badge {_escape(badge)}">{_escape(BADGE_LABELS.get(badge, badge))}</span>'
            nodes.append(
                f"<li><time>{_escape(str(item.get('date') or '')[:10])}</time><div>"
                f'<p class="timeline-source">{_escape(item.get("source_name"))}</p>'
                f"<h3>{_escape(item.get('text'))}</h3><p>{chips}</p><p>{claims}{refs}</p></div></li>"
            )
        return (
            f'<section class="chart correction-timeline" id="{_escape(block.get("block_id"))}">'
            f"<h2>{title}</h2>{note}"
            f"<ol>{''.join(nodes)}</ol>"
            f"{fallback if not nodes else ''}<small>数据口径："
            f"{_escape(_basis_label(block.get('data_basis')))}</small></section>"
        )
    values = [float(item.get("value", 0)) for item in items]
    maximum = max(values, default=1) or 1
    if kind == "line":
        width = 720
        height = 220
        step = width / max(len(values) - 1, 1)
        positions = [index * step for index in range(len(values))]
        try:
            stamps = [
                datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
                for item in items
            ]
            times = [
                (stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp).timestamp()
                for stamp in stamps
            ]
            span = max(times) - min(times)
            if span > 0:
                positions = [(stamp - min(times)) / span * width for stamp in times]
        except (KeyError, ValueError, TypeError):
            pass  # 旧 IR 无时间字段时保留原有离散点语义。
        points = " ".join(
            f"{positions[index]:.1f},{height - (value / maximum * (height - 24)):.1f}"
            for index, value in enumerate(values)
        )
        labels = "".join(
            f"<li><span>{_escape(item.get('label'))}</span><strong>{_escape(item.get('value'))}</strong></li>"
            for item in items
        )
        return (
            f'<section class="chart line-chart" id="{_escape(block.get("block_id"))}"><h2>{title}</h2>'
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}"><polyline points="{points}" fill="none" stroke="currentColor" stroke-width="4" vector-effect="non-scaling-stroke"/></svg>'
            f'<ol class="line-labels">{labels}</ol>{note}<small>数据口径：'
            f"{_escape(_basis_label(block.get('data_basis')))}</small></section>"
        )
    bars = "".join(
        f'<div class="bar-row"><span>{_escape(item.get("label"))}</span><i style="--bar:{float(item.get("value", 0)) / maximum:.3f}"></i><strong>{_escape(item.get("value"))}</strong></div>'
        for item in items
    )
    return (
        f'<section class="chart" id="{_escape(block.get("block_id"))}"><h2>{title}</h2>'
        f"{note}{bars}{fallback}<small>数据口径："
        f"{_escape(_basis_label(block.get('data_basis')))}</small></section>"
    )


def _data_quality(block: dict[str, Any]) -> str:
    summary = "".join(
        f"<div><strong>{_escape(item.get('value'))}</strong><span>{_escape(item.get('label'))}</span></div>"
        for item in block.get("summary", [])
    )
    method = block.get("verification_method") or {}
    distributions = []
    for distribution in block.get("distributions", []):
        items = distribution.get("items", [])
        maximum = max((float(item.get("value", 0)) for item in items), default=1) or 1
        rows = "".join(
            f'<div class="quality-row"><span>{_escape(item.get("label"))}</span>'
            f'<i style="--bar:{float(item.get("value", 0)) / maximum:.3f}"></i>'
            f"<strong>{_escape(item.get('value'))}</strong></div>"
            for item in items
        )
        distributions.append(f"<h3>{_escape(distribution.get('title'))}</h3>{rows}")
    return (
        f'<section class="data-quality" id="{_escape(block.get("block_id"))}">'
        f"<details><summary><span>{_escape(block.get('title'))}</span><small>展开查看计算口径与检索库存统计</small></summary>"
        f'<div class="quality-summary">{summary}</div>'
        f'<p class="method-note">已证实陈述占比：{float(method.get("verified_rate", 0)):.0%}；'
        f"信源加权值：{float(method.get('weighted_verified_rate', 0)):.0%}；"
        f"权重口径：{_escape(method.get('weight_scheme'))}。这些数值描述核验结论，不代表系统运行成功率。</p>"
        f"{''.join(distributions)}</details></section>"
    )


def _optional_field(label: str, value: Any, class_name: str = "") -> str:
    if value in (None, "", []):
        return ""
    class_attr = f' class="{class_name}"' if class_name else ""
    return f"<p{class_attr}><strong>{_escape(label)}：</strong>{_escape(value)}</p>"


def _analysis(block: dict[str, Any]) -> str:
    cards = []
    for item in block.get("items", []):
        refs = "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
        claims = "".join(_claim_link(ref) for ref in item.get("claim_refs", []))
        body = "".join(
            (
                _optional_field("解释", item.get("interpretation")),
                _optional_field("影响", item.get("implication")),
                _optional_field("建议行动", item.get("action"), "analysis-action"),
                _optional_field("责任主体", item.get("owner")),
                _optional_field("触发条件", item.get("trigger")),
                _optional_field("不确定性", item.get("uncertainty"), "analysis-uncertainty"),
            )
        )
        cards.append(
            '<article class="analysis-card">'
            '<div class="analysis-kind">分析判断</div>'
            f"<h3>{_escape(item.get('title') or '研判要点')}</h3>{body}"
            f'<details class="analysis-basis"><summary>查看本条事实依据</summary><p>{_escape(item.get("observation"))}</p></details>'
            f'<div class="analysis-refs">{claims}{refs}</div></article>'
        )
    basis = block.get("editorial_basis") or "基于所列事实与证据作出的编辑性研判"
    return (
        f'<section id="{_escape(block.get("block_id"))}" class="analysis-module">'
        f"<h2>{_escape(block.get('title') or '分析研判')}</h2>"
        f'<p class="analysis-notice"><strong>分析判断：</strong>{_escape(basis)}。以下内容不是确定性事实，需结合后续信息复核。</p>'
        f"{''.join(cards)}</section>"
    )


def _metric_cards(block: dict[str, Any]) -> str:
    cards = []
    for item in block.get("items", []):
        refs = "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
        scope = " · ".join(
            value
            for value in (
                str(item.get("scope") or ""),
                str(item.get("period") or ""),
            )
            if value
        )
        cards.append(
            '<article class="metric-card">'
            f'<span class="metric-label">{_escape(item.get("label") or "来源指标")}</span>'
            f"<strong>{_escape(item.get('value'))}<small>{_escape(item.get('unit'))}</small></strong>"
            f"{_optional_field('范围与期间', scope)}"
            f"{_optional_field('来源归属', item.get('source_name') or '来源未标注')}"
            f"{_optional_field('来源原话', item.get('quote'), 'metric-quote')}"
            f"{_optional_field('核验说明', item.get('verification_note') or '该数值为来源自述，未自动视为已核验事实', 'metric-verification')}"
            f'<div class="metric-refs">{refs}</div></article>'
        )
    return (
        f'<section id="{_escape(block.get("block_id"))}" class="metric-module">'
        f"<h2>{_escape(block.get('title') or '证据指标')}</h2>"
        f'<p class="method-note">数据口径：{_escape(_basis_label(block.get("data_basis")))}。数值保留来源归属，不等同于系统核验结论。</p>'
        f'<div class="metric-card-grid">{"".join(cards)}</div></section>'
    )


def _generic_block(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    title = _escape(block.get("title") or "数据板块")
    if block_type == "text":
        return f'<section id="{_escape(block.get("block_id"))}"><h2>{title}</h2><p>{_escape(block.get("text") or block.get("fallback_text"))}</p></section>'
    if block_type == "kpi_grid":
        items = "".join(
            f'<div class="kpi kpi-{_escape(item.get("tone") or "neutral")}">'
            f"<span>{_escape(item.get('label'))}</span><strong>{_escape(item.get('value'))}</strong>"
            f"<small>{_escape(item.get('note'))}</small></div>"
            for item in block.get("items", [])
        )
        return f'<section id="kpi" class="kpi-grid">{items}</section>'
    if block_type == "timeline":
        items = "".join(
            f"<li><time>{_escape(item.get('date'))}</time><span>{_escape(item.get('text'))}</span>"
            + "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
            + "</li>"
            for item in block.get("nodes", [])
        )
        return f'<section id="timeline"><h2>{title}</h2><ol class="timeline">{items}</ol></section>'
    if block_type == "chart":
        return _chart(block)
    if block_type == "data_quality":
        return _data_quality(block)
    if block_type == "analysis":
        return _analysis(block)
    if block_type == "metric_cards":
        return _metric_cards(block)
    if block_type == "propagation_network":
        nodes = "".join(
            '<article class="narrative-card publication-node">'
            f'<div class="card-meta"><span>{_escape(item.get("node_type"))}</span>'
            f"<time>{_escape(item.get('published_at') or '时间待确认')}</time></div>"
            f"<h3>{_escape(item.get('publisher'))}</h3>"
            f"<p>{_escape(item.get('framing'))}</p>"
            + "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
            + "</article>"
            for item in block.get("nodes", [])
        )
        relation_labels = {"repost": "转载", "response": "回应", "follow_up": "跟进"}
        edges = "".join(
            f'<li><span class="evidence-id">{_escape(item.get("from_evidence_id"))}</span>'
            f" → {_escape(relation_labels.get(item.get('relation'), item.get('relation')))} → "
            f'<span class="evidence-id">{_escape(item.get("to_evidence_id"))}</span></li>'
            for item in block.get("edges", [])
        )
        fallback = (
            f'<p class="fallback-note">{_escape(block.get("fallback_text"))}</p>'
            if block.get("fallback_text")
            else ""
        )
        return (
            f'<section id="{_escape(block.get("block_id"))}" class="propagation-network">'
            f'<h2>{title}</h2>{fallback}<div class="publication-grid">{nodes}</div>'
            f'<ol class="propagation-edges">{edges}</ol></section>'
        )
    if block_type == "history_compare":
        cards = []
        for item in block.get("cards", []):
            dimensions = "".join(
                f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>"
                for label, value in (item.get("dimensions") or {}).items()
            )
            summary = item.get("summary")
            comparison = item.get("comparison")
            summary_html = (
                f'<p class="history-summary">{_escape(summary)}</p>'
                if summary and summary != comparison
                else ""
            )
            comparison_html = (
                f'<p class="comparison">{_escape(comparison)}</p>' if comparison else ""
            )
            cards.append(
                f'<article class="narrative-card history-card"><div class="card-meta"><span>{_escape(item.get("provenance") or "来源未标注")}</span><time>{_escape(item.get("event_time") or "时间未知")}</time></div>'
                f"<h3>{_escape(item.get('event_name') or '历史事件')}</h3>"
                f"{summary_html}{comparison_html}"
                + (f'<dl class="dimension-grid">{dimensions}</dl>' if dimensions else "")
                + "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
                + "</article>"
            )
        fallback = (
            f'<p class="fallback-note">{_escape(block.get("fallback_text"))}</p>'
            if block.get("fallback_text")
            else ""
        )
        return f'<section id="{_escape(block.get("block_id"))}"><h2>{title}</h2>{"".join(cards)}{fallback}</section>'
    if block_type == "comment_insight":
        collections = "".join(
            f"<li><strong>{_escape(item.get('platform'))}</strong> · {_escape(item.get('title'))} · "
            f"{_escape(item.get('collected_count'))} 条 · {_escape(item.get('sampling_method'))}</li>"
            for item in block.get("collections", [])
        )
        insights = "".join(
            f'<article class="narrative-card"><p>{_escape(item.get("text"))}'
            + "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
            + "</p></article>"
            for item in block.get("items", [])
        )
        fallback = (
            f'<p class="fallback-note">{_escape(block.get("fallback_text"))}</p>'
            if block.get("fallback_text")
            else ""
        )
        return (
            f'<section id="comment-insight"><h2>{title}</h2><p class="sample-notice">'
            f"{_escape(block.get('sample_notice'))}</p><ul>{collections}</ul>{insights}{fallback}</section>"
        )
    collection = block.get("items") or block.get("cards") or []
    items = "".join(
        f'<article class="narrative-card"><h3>{_escape(item.get("event_name") or item.get("agent") or "要点")}</h3>'
        f"<p>{_escape(item.get('text') or item.get('comparison'))}</p>"
        + "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
        + "</article>"
        for item in collection
    )
    fallback = (
        f'<p class="fallback-note">{_escape(block.get("fallback_text"))}</p>'
        if block.get("fallback_text")
        else ""
    )
    return f'<section id="{_escape(block.get("block_id"))}"><h2>{title}</h2>{items}{fallback}</section>'


def _render_block(block: dict[str, Any], claim_badges: dict[str, str]) -> str:
    block_type = block.get("type")
    if block.get("section") == "09" and block_type in {"chart", "kpi_grid"}:
        return (
            '<details class="audit-module"><summary>'
            + _escape(block.get("title") or "核验与材料完整度指标")
            + "</summary>"
            + _generic_block(block)
            + "</details>"
        )
    if block_type == "executive_summary":
        return _summary(block, claim_badges)
    if block_type == "fact_check_table":
        return _fact_table(block)
    if block_type == "limitations":
        return _limitations(block)
    if block_type == "evidence_appendix":
        return _appendix(block)
    if block_type in {
        "kpi_grid",
        "timeline",
        "chart",
        "text",
        "viewpoint_list",
        "history_compare",
        "recommendation",
        "comment_insight",
        "data_quality",
        "analysis",
        "metric_cards",
    }:
        return _generic_block(block)
    if block.get("fallback_text"):
        return (
            f'<section class="fallback"><h2>{_escape(block.get("title") or "兼容摘要")}</h2>'
            f"<p>{_escape(block.get('fallback_text'))}</p></section>"
        )
    return ""


def _block_heading(block: dict[str, Any]) -> str:
    return str(
        block.get("title")
        or {
            "executive_summary": "执行摘要",
            "fact_check_table": "关键事实核查",
            "limitations": "局限性声明",
            "evidence_appendix": "证据卡片",
        }.get(str(block.get("type")), "")
    )


def render_html(report: dict[str, Any], *, view: Literal["brief", "full"] = "brief") -> str:
    blocks = report.get("blocks", [])
    header = next((block for block in blocks if block.get("type") == "report_header"), {})
    claim_badges = {
        str(item.get("claim_ref")): str(item.get("badge") or "unverified")
        for block in blocks
        if block.get("type") == "fact_check_table"
        for item in block.get("items", [])
        if item.get("claim_ref")
    }
    grouped: dict[str, list[tuple[bool, str]]] = {}
    for block in blocks:
        if block.get("type") == "report_header":
            continue
        rendered = _render_block(block, claim_badges)
        if block.get("type") == "executive_summary":
            decisions = [
                (analysis.get("block_id"), item.get("title"))
                for analysis in blocks
                if analysis.get("type") == "analysis" and analysis.get("section") == "07"
                for item in analysis.get("items", [])
                if item.get("title")
            ][:3]
            if decisions:
                links = "".join(
                    f'<li><a href="#{_escape(target)}">{_escape(title)}</a></li>'
                    for target, title in decisions
                )
                rendered = rendered.replace(
                    "<h2>执行摘要</h2>",
                    '<h2>执行摘要</h2><div class="analysis-notice"><strong>优先决策事项（分析判断）</strong>'
                    f"<ul>{links}</ul><p>点击查看行动、责任职能、触发条件和不确定性。</p></div>",
                    1,
                )
        if not rendered:
            continue
        section = str(block.get("section") or "09")
        if _block_heading(block) == SECTION_LABELS.get(section):
            rendered = rendered.replace("<h2>", '<h2 class="sr-only">', 1)
        in_brief = bool(block.get("in_brief"))
        grouped.setdefault(section, []).append((in_brief, rendered))

    sections = []
    toc_items = []
    first_section = min(grouped, default=None)
    for section, entries in sorted(grouped.items()):
        section_id = f"report-section-{section}"
        section_has_brief = any(in_brief for in_brief, _ in entries)
        section_hidden = view == "brief" and not section_has_brief
        body = "".join(
            f'<div class="report-block{" is-hidden" if view == "brief" and not in_brief else ""}" data-in-brief="{str(in_brief).lower()}">{content}</div>'
            for in_brief, content in entries
        )
        sections.append(
            f'<section class="report-section{" is-hidden" if section_hidden else ""}" id="{section_id}" data-report-section="{section}">'
            f'<div class="section-marker"><span>{_escape(section)}</span>{_escape(SECTION_LABELS.get(section, "附加章节"))}</div>{body}</section>'
        )
        current = ' aria-current="location"' if section == first_section else ""
        toc_items.append(
            f'<li><a href="#{section_id}" data-section-target="{section_id}"{current}>'
            f"<span>{_escape(section)}</span>{_escape(SECTION_LABELS.get(section, '附加章节'))}</a></li>"
        )

    title = _escape(
        header.get("event_title") or report.get("task", {}).get("event_query") or "舆情速览"
    )
    css = """
h1{font-size:clamp(30px,4vw,46px)!important;max-width:1080px!important;text-wrap:balance}.funnel-stage i{width:calc(var(--funnel)*100%)!important}.verification-matrix{min-width:780px!important}.verification-matrix th:first-child{min-width:260px!important}
:root{--ink:#0b1f33;--ink-2:#173a57;--paper:#fff;--mist:#edf2f5;--line:#c8d4dc;--teal:#0f6b78;--teal-soft:#dceff1;--amber:#c78316;--amber-soft:#fff1cf;--danger:#a4312d;--muted:#587080;--report-width:1120px;--toc-width:220px;--layout-gap:24px;font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",sans-serif;color:var(--ink);background:var(--mist);scroll-behavior:smooth}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#e7eef2 0,#f4f7f8 420px);color:var(--ink)}a{color:var(--ink-2)}button,a{transition:background-color .16s ease,color .16s ease,border-color .16s ease,transform .16s ease,box-shadow .16s ease}button{font:inherit}header{max-width:var(--report-width);margin:auto;padding:58px 34px 34px}header>p:first-child{margin:0 0 10px;color:var(--teal);font:700 12px/1.4 Consolas,monospace;letter-spacing:.16em;text-transform:uppercase}h1{font-family:STZhongsong,"Microsoft YaHei",sans-serif;font-size:clamp(30px,5vw,48px);line-height:1.25;margin:0 0 12px;max-width:900px}h2{font-family:STZhongsong,"Microsoft YaHei",sans-serif;color:var(--ink-2);font-size:26px;margin:0 0 16px}h3{line-height:1.55}.report-tools{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;gap:8px;padding:10px max(20px,calc((100vw - var(--report-width))/2));background:rgba(11,31,51,.96);backdrop-filter:blur(12px);box-shadow:0 5px 18px rgba(11,31,51,.2)}.report-tools button,.report-tools a{border:1px solid #7490a3;background:transparent;color:#fff;padding:8px 12px;text-decoration:none;cursor:pointer;border-radius:3px}.report-tools button:hover,.report-tools a:hover{background:#fff;color:var(--ink);border-color:#fff;transform:translateY(-1px);box-shadow:0 5px 14px rgba(0,0,0,.18)}.report-tools button:active,.report-tools a:active{transform:translateY(0)}.report-tools button.is-active{background:var(--teal-soft);border-color:var(--teal-soft);color:var(--ink);box-shadow:inset 0 -3px 0 var(--teal)}.report-tools button:focus-visible,.report-tools a:focus-visible,.report-toc a:focus-visible,summary:focus-visible{outline:3px solid #f5bd4f;outline-offset:2px}.report-shell{width:100%;margin:0 auto 80px;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,var(--report-width)) minmax(0,1fr);column-gap:var(--layout-gap);padding:0 24px}.report-toc{grid-column:1;grid-row:1;align-self:start;justify-self:end;width:var(--toc-width);position:sticky;top:72px;background:var(--ink);color:#fff;border-radius:4px;box-shadow:0 14px 30px rgba(11,31,51,.16);overflow:hidden}.report-toc summary{cursor:pointer;padding:16px 18px;border-bottom:1px solid #31506a;font-weight:700}.report-toc ol{list-style:none;margin:0;padding:8px}.report-toc a{display:grid;grid-template-columns:32px 1fr;gap:7px;padding:9px 10px;color:#c9d7df;text-decoration:none;border-left:3px solid transparent;font-size:13px}.report-toc a span{font:700 12px/1.5 Consolas,monospace;color:#7fa8b4}.report-toc a:hover{background:#173a57;color:#fff}.report-toc a[aria-current="location"]{background:#214962;border-left-color:#55b7bd;color:#fff}.report-toc a[aria-current="location"] span{color:#8ddbe0}main{grid-column:2;grid-row:1;min-width:0;width:100%;background:var(--paper);border-top:4px solid var(--ink);box-shadow:0 14px 36px rgba(11,31,51,.12)}.report-section{position:relative;padding:26px 34px 38px;border-bottom:1px solid var(--line);scroll-margin-top:74px}.section-marker{display:flex;gap:10px;align-items:center;margin:0 0 24px;color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.06em}.section-marker span{font-family:Consolas,monospace;color:var(--teal);border:1px solid #a8c8ce;padding:3px 6px}.report-block>section{padding:20px 0}.lede{font-size:18px;line-height:1.85}.claim-card,.evidence-card,.narrative-card,.chart{background:#fff;border:1px solid var(--line);border-radius:4px;padding:20px;margin:14px 0;box-shadow:0 4px 14px rgba(11,31,51,.05)}.claim-card{border-left:4px solid #d6a33a}.claim-card h3{line-height:1.65}.badge,.strength,.relation-chip{display:inline-block;padding:4px 8px;margin:2px 6px 2px 0;font-size:12px;border-radius:2px}.verified,.kpi-verified{background:#dff1e6;color:#145a35}.unverified{background:var(--amber-soft);color:#704d06}.disputed{background:#ffe2cc;color:#8b3b08}.refuted{background:#f7d8d7;color:#7d1c1c}.strength{background:#edf2f5;color:#334d60}.verify-state{display:inline-block;padding:4px 8px;margin:2px 6px 2px 0;font-size:12px;border-radius:2px;background:#e6edf2;color:#3d566b;border:1px dashed #9fb3c2}.citation,.claim-link{font-family:Consolas,monospace;margin-left:5px;color:var(--teal)}.evidence-id{font-family:Consolas,monospace;color:var(--teal)}.rumor,.correction{padding:10px;border-left:3px solid #aeb6bd;line-height:1.7}li,p{line-height:1.8}.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{position:relative;min-height:142px;padding:18px;background:var(--ink);color:#fff;border-top:4px solid #6f93a8}.kpi-warning{border-top-color:#f0b33e}.kpi-verified{border-top-color:#4ca979;background:var(--ink);color:#fff}.kpi span,.kpi strong,.kpi small{display:block}.kpi span{font-size:13px;color:#b9cad5}.kpi strong{font-size:28px;margin:14px 0 9px}.kpi small{color:#93aab8;line-height:1.5}.chart-note,.method-note,.fallback-note{color:var(--muted);background:#f1f6f7;border-left:3px solid var(--teal);padding:10px 12px}.funnel-stage{max-width:760px;margin:12px auto}.funnel-stage>div{display:flex;justify-content:space-between;gap:16px;margin-bottom:5px}.funnel-stage i{display:block;height:30px;width:max(9%,calc(var(--funnel)*100%));margin:auto;background:linear-gradient(90deg,var(--ink-2),var(--teal));clip-path:polygon(5% 0,95% 0,100% 100%,0 100%)}.matrix-scroll{overflow:auto;border:1px solid var(--line)}.verification-matrix{border-collapse:collapse;width:100%;min-width:900px;font-size:12px}.verification-matrix th,.verification-matrix td{padding:10px;border-bottom:1px solid var(--line);text-align:center}.verification-matrix th:first-child{text-align:left;min-width:300px}.verification-matrix th:first-child a,.verification-matrix th:first-child span{display:block}.verification-matrix th:first-child span{margin-top:4px;font-weight:400;color:#425d70;line-height:1.5}.relation-support{background:#e0f1e8;color:#155c37}.relation-partial{background:#fff1cf;color:#704d06}.relation-contradict{background:#f8dedd;color:#7d1c1c}.relation-not_mentioned,.relation-unverified{background:#edf2f5;color:#425d70}.correction-timeline ol{list-style:none;padding:0;margin:24px 0;border-left:2px solid var(--teal)}.correction-timeline li{position:relative;display:grid;grid-template-columns:120px 1fr;gap:22px;padding:0 0 24px 22px}.correction-timeline li:before{content:"";position:absolute;left:-7px;top:7px;width:12px;height:12px;border-radius:50%;background:var(--paper);border:3px solid var(--teal)}.correction-timeline time{font:700 12px/1.8 Consolas,monospace;color:var(--teal)}.correction-timeline h3{margin:2px 0 8px}.timeline-source{margin:0;color:var(--muted);font-size:12px}.line-chart svg{width:100%;max-height:240px;background:#f5f7f8;border:1px solid var(--line);color:var(--teal)}.line-labels{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;padding:0;list-style:none}.line-labels li{display:flex;justify-content:space-between;gap:8px;font-size:12px}.bar-row,.quality-row{display:grid;grid-template-columns:190px 1fr 48px;gap:10px;align-items:center;margin:8px 0}.bar-row i,.quality-row i{height:10px;background:var(--teal);transform:scaleX(var(--bar));transform-origin:left}.data-quality details{border:1px solid var(--line);background:#f8fafb}.data-quality summary{display:flex;justify-content:space-between;gap:16px;cursor:pointer;padding:18px 20px;font-weight:700}.data-quality summary small{font-weight:400;color:var(--muted)}.data-quality details>div,.data-quality details>p,.data-quality details>h3,.data-quality .quality-row{margin-left:20px;margin-right:20px}.quality-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;padding:16px 0}.quality-summary div{background:var(--ink);color:#fff;padding:14px}.quality-summary strong,.quality-summary span{display:block}.quality-summary strong{font-size:22px}.quality-summary span{font-size:12px;color:#b9cad5}.card-meta{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}.card-meta span{color:#a84918;font-weight:700}.comparison{border-left:3px solid #d86a2c;padding-left:10px}.dimension-grid{display:grid;grid-template-columns:150px 1fr;border-top:1px solid var(--line)}.dimension-grid dt,.dimension-grid dd{margin:0;padding:7px;border-bottom:1px solid var(--line)}.dimension-grid dt{font-weight:700}.highlight{outline:3px solid #e19a22;outline-offset:4px}.is-hidden{display:none!important}@media(max-width:1640px){.report-shell{display:block}.report-toc{position:relative;top:auto;width:min(var(--report-width),100%);margin:0 auto 18px}.report-toc ol{display:grid;grid-template-columns:repeat(5,minmax(0,1fr))}main{width:min(var(--report-width),100%);margin:0 auto}}@media(max-width:900px){header{padding:42px 20px 26px}.report-shell{padding:0 14px}.report-toc ol{grid-template-columns:1fr}.report-toc details:not([open]){padding:0}main{box-shadow:none}.report-section{padding:22px 18px 32px}.kpi-grid,.quality-summary{grid-template-columns:1fr 1fr}.correction-timeline li,.bar-row,.quality-row,.dimension-grid{grid-template-columns:1fr}.data-quality summary{display:block}.data-quality summary small{display:block;margin-top:5px}}@media(max-width:520px){.report-tools{padding:9px}.report-tools button,.report-tools a{flex:1 1 auto;text-align:center}.kpi-grid,.quality-summary{grid-template-columns:1fr}.report-section{padding-left:14px;padding-right:14px}}@media(prefers-reduced-motion:reduce){:root{scroll-behavior:auto}button,a{transition:none}}@media print{body{background:#fff}.report-tools,.report-toc{display:none}.report-shell{display:block;max-width:none;padding:0}main{width:auto;box-shadow:none;border:0}.report-section{padding:18px 0}.report-block,.report-section{display:block!important}.chart,.narrative-card,.claim-card,.evidence-card{break-inside:avoid}a{color:#000;text-decoration:underline}}
"""
    css += """
.section-marker{gap:14px;margin-bottom:30px;padding:0 0 15px;border-bottom:2px solid var(--ink-2);color:var(--ink-2);font-family:STZhongsong,"Microsoft YaHei",sans-serif;font-size:clamp(22px,2.2vw,30px);line-height:1.35;letter-spacing:0}
.section-marker span{flex:0 0 auto;padding:5px 8px;font:700 14px/1.2 Consolas,monospace}
.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
.evidence-card{padding:0;scroll-margin-top:76px}.evidence-card>summary{display:grid;grid-template-columns:minmax(62px,.45fr) minmax(220px,2fr) minmax(150px,1fr) minmax(120px,.8fr);gap:14px;align-items:center;padding:17px 20px;cursor:pointer;list-style-position:inside}.evidence-card>summary:hover{background:#f3f7f8}.evidence-card[open]>summary{border-bottom:1px solid var(--line);background:#f7fafb}.evidence-title{font-weight:700;line-height:1.45}.evidence-source{color:var(--muted);font-size:13px}.evidence-status{justify-self:end;padding:4px 7px;background:#edf2f5;color:#334d60;font-size:12px}.evidence-body{padding:4px 20px 18px}.evidence-body blockquote{margin-left:0;margin-right:0;padding:12px 16px;background:#f7f9fa;border-left:3px solid var(--teal)}
.claim-overflow,.uncited-evidence-group{margin-top:18px;border:1px solid var(--line);background:#f8fafb}.claim-overflow>summary,.uncited-evidence-group>summary{padding:15px 18px;cursor:pointer;font-weight:700;color:var(--ink-2)}.claim-overflow>.claim-card,.uncited-evidence-group>.evidence-card{margin-left:14px;margin-right:14px;background:#fff}
.analysis-module,.metric-module{max-width:100%}.analysis-notice{padding:14px 16px;background:#eef5f6;border-left:4px solid var(--teal);color:#29485b}.analysis-card{margin:14px 0;padding:20px;border:1px solid var(--line);border-left:4px solid var(--teal);background:#fbfcfc}.analysis-card h3{margin:8px 0}.analysis-kind{display:inline-block;padding:3px 7px;background:var(--ink);color:#fff;font-size:12px;font-weight:700}.analysis-action{background:#edf6f1;padding:8px 10px}.analysis-uncertainty,.metric-verification{color:#664d12;background:#fff6df;padding:8px 10px}.analysis-refs,.metric-refs{margin-top:12px}.metric-card-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}.metric-card{padding:18px;border:1px solid var(--line);border-top:4px solid var(--teal);background:#fff}.metric-card>strong{display:block;margin:10px 0;font-size:28px;color:var(--ink-2)}.metric-card>strong small{margin-left:5px;font-size:14px;color:var(--muted)}.metric-label{font-weight:700}.metric-card p{margin:8px 0}.metric-quote{font-style:italic}
@media(max-width:720px){.evidence-card>summary{grid-template-columns:1fr}.evidence-status{justify-self:start}}
@media print{.evidence-card{break-inside:avoid}.evidence-card>summary{grid-template-columns:70px 2fr 1fr 1fr}.evidence-card .evidence-body{display:none!important}.evidence-tools{display:none!important}}
"""
    initial_view = "brief" if view == "brief" else "full"
    script = f"""
const initialView={initial_view!r};
if(window.matchMedia('(max-width:900px)').matches)document.querySelector('.report-toc details')?.removeAttribute('open');
const applyView=viewName=>{{
  document.querySelectorAll('.report-block').forEach(block=>block.classList.toggle('is-hidden',viewName==='brief'&&block.dataset.inBrief!=='true'));
  document.querySelectorAll('.report-section').forEach(section=>{{
    const visible=[...section.querySelectorAll('.report-block')].some(block=>!block.classList.contains('is-hidden'));
    section.classList.toggle('is-hidden',!visible);
    document.querySelector(`[data-section-target="${{section.id}}"]`)?.closest('li')?.classList.toggle('is-hidden',!visible);
  }});
  document.querySelectorAll('[data-view]').forEach(button=>{{
    const active=button.dataset.view===viewName;button.classList.toggle('is-active',active);button.setAttribute('aria-pressed',String(active));
  }});
}};
document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>applyView(button.dataset.view)));
document.querySelectorAll('[data-evidence-action]').forEach(button=>button.addEventListener('click',()=>{{
  const open=button.dataset.evidenceAction==='expand';
  document.querySelectorAll('.uncited-evidence-group,details.evidence-card').forEach(card=>card.open=open);
}}));
document.querySelectorAll('[data-badge-filter]').forEach(button=>button.addEventListener('click',()=>{{
  const filter=button.dataset.badgeFilter;
  document.querySelectorAll('.claim-card').forEach(card=>card.classList.toggle('is-hidden',filter!=='all'&&!card.querySelector('.badge')?.classList.contains(filter)));
  document.querySelectorAll('[data-badge-filter]').forEach(item=>{{const active=item===button;item.classList.toggle('is-active',active);item.setAttribute('aria-pressed',String(active));}});
}}));
const revealTarget=hash=>{{
  if(!hash||hash==='#')return;
  let target=null;try{{target=document.querySelector(hash)}}catch(_error){{return}}
  if(!target)return;
  applyView('full');
  if(target.tagName==='DETAILS')target.open=true;
  if(target.classList.contains('claim-card'))target.classList.remove('is-hidden');
  let ancestor=target.parentElement;
  while(ancestor){{if(ancestor.tagName==='DETAILS')ancestor.open=true;ancestor=ancestor.parentElement}}
  document.querySelectorAll('.highlight').forEach(item=>item.classList.remove('highlight'));
  target.classList.add('highlight');
  requestAnimationFrame(()=>target.scrollIntoView({{block:'start'}}));
}};
document.querySelectorAll('.citation,.claim-link').forEach(link=>link.addEventListener('click',event=>{{
  event.preventDefault();history.pushState(null,'',link.getAttribute('href'));revealTarget(location.hash);
}}));
window.addEventListener('hashchange',()=>revealTarget(location.hash));
const tocLinks=[...document.querySelectorAll('.report-toc a[data-section-target]')];
const activate=id=>tocLinks.forEach(link=>{{if(link.dataset.sectionTarget===id)link.setAttribute('aria-current','location');else link.removeAttribute('aria-current')}});
tocLinks.forEach(link=>link.addEventListener('click',()=>activate(link.dataset.sectionTarget)));
const observer=new IntersectionObserver(entries=>entries.filter(entry=>entry.isIntersecting).forEach(entry=>activate(entry.target.id)),{{rootMargin:'-15% 0px -70% 0px',threshold:0}});
document.querySelectorAll('.report-section').forEach(section=>observer.observe(section));
applyView(initialView);
if(location.hash)revealTarget(location.hash);
"""
    report_id = _escape(report.get("report_id") or "")
    task_id = quote(str(report.get("task", {}).get("task_id") or ""), safe="")
    console_href = f"/?task={task_id}" if task_id else "/"
    tools = (
        '<nav class="report-tools" aria-label="报告工具">'
        f'<a class="console-link" href="{console_href}">← 返回调查台</a>'
        f'<button data-view="brief" aria-pressed="{str(initial_view == "brief").lower()}">速览</button>'
        f'<button data-view="full" aria-pressed="{str(initial_view == "full").lower()}">完整</button>'
        '<button data-badge-filter="all" aria-pressed="true">全部徽章</button>'
        '<button data-badge-filter="verified" aria-pressed="false">仅已证实</button>'
        '<button class="evidence-tools" data-evidence-action="expand">展开全部证据</button>'
        '<button class="evidence-tools" data-evidence-action="collapse">收起全部证据</button>'
        f'<a href="/api/reports/{report_id}/pdf">下载 PDF</a>'
        f'<a href="/api/reports/{report_id}/evidence-package">证据包 ZIP</a></nav>'
    )
    toc = (
        '<aside class="report-toc"><details open><summary>报告目录</summary>'
        f"<ol>{''.join(toc_items)}</ol></details></aside>"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width"><link rel="icon" href="data:,">'
        f"<title>{title}</title><style>{css}</style></head><body>{tools}"
        f"<header><p>Evidence-led public intelligence</p><h1>{title}</h1>"
        f"<p>{_escape(header.get('subtitle'))}</p></header>"
        f'<div class="report-shell">{toc}<main id="report-main">{"".join(sections)}</main></div>'
        f"<script>{script}</script></body></html>"
    )
