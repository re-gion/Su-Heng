from __future__ import annotations

import html
from typing import Any, Literal

BADGE_LABELS = {
    "verified": "已证实",
    "unverified": "待核验",
    "disputed": "有争议",
    "refuted": "已证伪",
}


def _escape(value: Any) -> str:
    return html.escape(str(value or ""))


def _citation_link(evidence_ref: str) -> str:
    ref = _escape(evidence_ref)
    return f'<a class="citation" href="#evidence-{ref}" aria-label="查看证据 {ref}">[{ref}]</a>'


def _claim_link(claim_ref: str) -> str:
    ref = _escape(claim_ref)
    return f'<a class="claim-link" href="#claim-{ref}" aria-label="查看事实 {ref}">[{ref}]</a>'


def _summary(block: dict[str, Any]) -> str:
    groups = []
    for key, title in (
        ("what", "发生了什么"),
        ("why", "为何引发关注"),
        ("so_what", "目前意味着什么"),
    ):
        items = "".join(
            f"<li>{_escape(item.get('text'))}{_claim_link(item.get('claim_ref'))}</li>"
            for item in block.get(key, [])
        )
        if items:
            groups.append(f"<h3>{title}</h3><ul>{items}</ul>")
    return f'<section id="summary"><h2>执行摘要</h2><p class="lede">{_escape(block.get("lede"))}</p>{"".join(groups)}</section>'


def _fact_table(block: dict[str, Any]) -> str:
    cards = []
    for item in block.get("items", []):
        badge = item.get("badge", "unverified")
        citations = "".join(
            _citation_link(citation.get("evidence_ref")) for citation in item.get("citations", [])
        )
        note = ""
        if item.get("evidence_grade") == "snippet_only":
            note = '<span class="strength">原文未取得</span>'
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
        cards.append(
            f'<article class="claim-card" id="claim-{_escape(item.get("claim_ref"))}">'
            f'<div class="claim-meta"><span class="badge {badge}">{BADGE_LABELS.get(badge, badge)}</span>{note}</div>'
            f'<h3>{_escape(item.get("text"))}</h3>{rumor}{correction}<div class="claim-refs">{citations}</div></article>'
        )
    return f'<section id="facts"><h2>关键事实核查</h2>{"".join(cards)}</section>'


def _limitations(block: dict[str, Any]) -> str:
    items = "".join(
        f"<li><strong>{_escape(item.get('category'))}</strong>：{_escape(item.get('text'))}</li>"
        for item in block.get("items", [])
    )
    return f'<section id="limitations"><h2>局限性声明</h2><ul>{items}</ul></section>'


def _appendix(block: dict[str, Any]) -> str:
    cards = []
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
        original = (
            f'<blockquote class="original"><strong>原文（{_escape(item.get("lang") or "unknown")}）</strong><br>{_escape(item.get("original_excerpt"))}</blockquote>'
            if item.get("original_excerpt")
            else ""
        )
        translation = (
            f'<blockquote class="translation"><strong>机器翻译（中文，仅供阅读，不参与逐字核验）</strong><br>{_escape(item.get("machine_translation_zh"))}</blockquote>'
            if item.get("machine_translation_zh")
            else ""
        )
        cards.append(
            f'<article class="evidence-card" id="evidence-{ref}"><div class="evidence-id">{ref}</div>'
            f"<h3>{_escape(item.get('title'))}</h3><p>{_escape(item.get('source_name'))} · L{_escape(item.get('source_tier'))} · {_escape(strength)}</p>"
            f'<p class="published">发布日期：{_escape(item.get("published_at") or "未知")}</p>'
            f'<p class="published">证据类型：{_escape(item.get("kind") or "web")} · 语言：{_escape(item.get("lang") or "unknown")}</p>'
            f'{quotes}{original}{translation}<p><a href="{_escape(item.get("url"))}" rel="noreferrer">访问公开来源</a> {snapshot}</p></article>'
        )
    return f'<section id="evidence"><h2>证据卡片</h2>{"".join(cards)}</section>'


def _generic_block(block: dict[str, Any]) -> str:
    block_type = block.get("type")
    title = _escape(block.get("title") or "数据板块")
    if block_type == "kpi_grid":
        items = "".join(
            f'<div class="kpi"><strong>{_escape(item.get("value"))}</strong><span>{_escape(item.get("label"))}</span></div>'
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
        values = [float(item.get("value", 0)) for item in block.get("items", [])]
        maximum = max(values, default=1) or 1
        if block.get("chart_kind") == "line":
            width = 720
            height = 220
            step = width / max(len(values) - 1, 1)
            points = " ".join(
                f"{index * step:.1f},{height - (value / maximum * (height - 24)):.1f}"
                for index, value in enumerate(values)
            )
            labels = "".join(
                f"<li><span>{_escape(item.get('label'))}</span><strong>{_escape(item.get('value'))}</strong></li>"
                for item in block.get("items", [])
            )
            basis_label = {
                "hot_snapshot_database": "本地热榜快照真实采集",
            }.get(block.get("data_basis"), block.get("data_basis") or "未标注")
            return (
                f'<section class="chart line-chart" id="{_escape(block.get("block_id"))}"><h2>{title}</h2>'
                f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{title}"><polyline points="{points}" fill="none" stroke="currentColor" stroke-width="4" vector-effect="non-scaling-stroke"/></svg>'
                f'<ol class="line-labels">{labels}</ol><small>数据口径：{_escape(basis_label)}</small></section>'
            )
        bars = "".join(
            f'<div class="bar-row"><span>{_escape(item.get("label"))}</span><i style="--bar:{float(item.get("value", 0)) / maximum:.3f}"></i><strong>{_escape(item.get("value"))}</strong></div>'
            for item in block.get("items", [])
        )
        basis_label = {
            "evidence_database": "本任务证据库实时聚合",
            "hot_snapshot_database": "本地热榜快照真实采集",
        }.get(block.get("data_basis"), block.get("data_basis") or "未标注")
        return f'<section class="chart" id="{_escape(block.get("block_id"))}"><h2>{title}</h2>{bars}<small>数据口径：{_escape(basis_label)}</small></section>'
    if block_type == "history_compare":
        cards = []
        for item in block.get("cards", []):
            dimensions = "".join(
                f"<dt>{_escape(label)}</dt><dd>{_escape(value)}</dd>"
                for label, value in (item.get("dimensions") or {}).items()
            )
            cards.append(
                f'<article class="narrative-card history-card"><div class="card-meta"><span>{_escape(item.get("provenance") or "来源未标注")}</span><time>{_escape(item.get("event_time") or "时间未知")}</time></div>'
                f"<h3>{_escape(item.get('event_name') or '历史事件')}</h3>"
                f"<p>{_escape(item.get('summary') or item.get('comparison'))}</p>"
                f'<p class="comparison">{_escape(item.get("comparison"))}</p>'
                + (f'<dl class="dimension-grid">{dimensions}</dl>' if dimensions else "")
                + "".join(_citation_link(ref) for ref in item.get("evidence_refs", []))
                + "</article>"
            )
        fallback = (
            f'<p class="fallback-note">{_escape(block.get("fallback_text"))}</p>'
            if block.get("fallback_text")
            else ""
        )
        return f'<section id="section-{_escape(block.get("section"))}"><h2>{title}</h2>{"".join(cards)}{fallback}</section>'
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
    return f'<section id="section-{_escape(block.get("section"))}"><h2>{title}</h2>{items}{fallback}</section>'


def render_html(report: dict[str, Any], *, view: Literal["brief", "full"] = "brief") -> str:
    blocks = report.get("blocks", [])
    header = next((block for block in blocks if block.get("type") == "report_header"), {})
    appendix = next(
        (block for block in blocks if block.get("type") == "evidence_appendix"), {"items": []}
    )
    body = []
    for block in blocks:
        if (
            view == "brief"
            and not block.get("in_brief")
            and block.get("type") != "evidence_appendix"
        ):
            continue
        block_type = block.get("type")
        if block_type == "executive_summary":
            body.append(_summary(block))
        elif block_type == "fact_check_table":
            body.append(_fact_table(block))
        elif block_type == "limitations":
            body.append(_limitations(block))
        elif block_type in {
            "kpi_grid",
            "timeline",
            "chart",
            "text",
            "viewpoint_list",
            "history_compare",
            "recommendation",
            "comment_insight",
        }:
            body.append(_generic_block(block))
        elif block.get("fallback_text"):
            body.append(
                f'<section class="fallback"><h2>{_escape(block.get("title") or "兼容摘要")}</h2>'
                f"<p>{_escape(block.get('fallback_text'))}</p></section>"
            )
    body.append(_appendix(appendix))
    title = _escape(
        header.get("event_title") or report.get("task", {}).get("event_query") or "舆情速览"
    )
    css = """
:root{font-family:"Noto Sans SC","Microsoft YaHei",sans-serif;color:#20262d;background:#f5f4f1}*{box-sizing:border-box}body{margin:0}header,main{max-width:940px;margin:auto}header{padding:56px 24px 30px;border-bottom:3px solid #1c3f63}h1{font-family:"Noto Serif SC",SimSun,serif;font-size:38px;margin:0 0 10px;color:#14283c}main{padding:20px 24px 80px}section{padding:24px 0;border-bottom:2px solid #27333d}h2{font-family:"Noto Serif SC",SimSun,serif;color:#1c3f63}.lede{font-size:19px;line-height:1.85}.claim-card,.evidence-card{background:#fff;border:1px solid #cbd1d6;border-radius:2px;padding:18px;margin:14px 0}.claim-card h3{line-height:1.65}.badge,.strength{display:inline-block;padding:4px 8px;margin-right:8px;font-size:13px}.verified{background:#e5f2e9;color:#176638}.unverified{background:#fff4cf;color:#725400}.disputed{background:#ffe5d2;color:#99430b}.refuted{background:#f8d9d9;color:#8a1717}.citation{font-family:Consolas,monospace;margin-left:5px;color:#1c3f63}.evidence-id{font-family:Consolas,monospace;color:#1c3f63}.rumor,.correction{padding:10px;border-left:3px solid #aeb6bd;line-height:1.7}li,p{line-height:1.85}a{color:#1c3f63}@media print{body{background:#fff}.claim-card,.evidence-card{break-inside:avoid}}
.report-tools{position:sticky;top:0;z-index:3;display:flex;flex-wrap:wrap;gap:8px;padding:10px 24px;background:#14283c;color:#fff}.report-tools button,.report-tools a{border:1px solid #8da4b5;background:transparent;color:#fff;padding:7px 10px;text-decoration:none}.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.kpi{padding:16px;background:#14283c;color:#fff}.kpi strong,.kpi span{display:block}.kpi strong{font-size:26px}.timeline{border-left:2px solid #1c3f63}.timeline li{display:grid;grid-template-columns:190px 1fr auto;gap:12px;padding:8px}.chart,.narrative-card{background:#fff;padding:18px;margin:12px 0}.bar-row{display:grid;grid-template-columns:180px 1fr 45px;gap:10px;align-items:center;margin:8px 0}.bar-row i{height:12px;background:#2c7a78;transform:scaleX(var(--bar));transform-origin:left}.line-chart svg{width:100%;max-height:240px;background:#f5f7f8;border:1px solid #ccd5da;color:#2c7a78}.line-labels{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;padding:0;list-style:none}.line-labels li{display:flex;justify-content:space-between;gap:8px;font-size:12px}.card-meta{display:flex;justify-content:space-between;color:#587080;font-size:12px}.card-meta span{color:#b55224;font-weight:700}.comparison{border-left:3px solid #d86a2c;padding-left:10px}.dimension-grid{display:grid;grid-template-columns:150px 1fr;border-top:1px solid #ccd5da}.dimension-grid dt,.dimension-grid dd{margin:0;padding:7px;border-bottom:1px solid #ccd5da}.dimension-grid dt{font-weight:700}.highlight{outline:3px solid #d86a2c}.is-hidden{display:none!important}@media(max-width:700px){.kpi-grid{grid-template-columns:1fr 1fr}.timeline li,.bar-row,.dimension-grid{grid-template-columns:1fr}}@media print{.report-tools{display:none}.chart,.narrative-card,.claim-card,.evidence-card{break-inside:avoid}a{color:#000;text-decoration:underline}}
"""
    script = """document.querySelectorAll('.citation').forEach(a=>a.addEventListener('click',()=>setTimeout(()=>{document.querySelectorAll('.highlight').forEach(x=>x.classList.remove('highlight'));document.querySelector(a.getAttribute('href'))?.classList.add('highlight')},0)));document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>document.querySelectorAll('main>section').forEach(s=>s.classList.toggle('is-hidden',b.dataset.view==='brief'&&!['summary','facts','limitations','evidence','kpi'].includes(s.id)))));document.querySelectorAll('[data-badge-filter]').forEach(b=>b.addEventListener('click',()=>document.querySelectorAll('.claim-card').forEach(c=>c.classList.toggle('is-hidden',b.dataset.badgeFilter!=='all'&&!c.querySelector('.badge')?.classList.contains(b.dataset.badgeFilter)))));new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting)history.replaceState(null,'','#'+e.target.id)}),{threshold:.2}).observe(document.querySelector('main'));"""
    report_id = _escape(report.get("report_id") or "")
    tools = f'<nav class="report-tools"><button data-view="brief">速览</button><button data-view="full">完整</button><button data-badge-filter="all">全部徽章</button><button data-badge-filter="verified">仅已证实</button><a href="/api/reports/{report_id}/pdf">下载 PDF</a><a href="/api/reports/{report_id}/evidence-package">证据包 ZIP</a></nav>'
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="icon" href="data:,"><title>{title}</title><style>{css}</style></head><body>{tools}<header><p>可核验舆情专报</p><h1>{title}</h1><p>{_escape(header.get("subtitle"))}</p></header><main>{"".join(body)}</main><script>{script}</script></body></html>'
