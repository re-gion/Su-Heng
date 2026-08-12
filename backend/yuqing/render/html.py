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
        cards.append(
            f'<article class="evidence-card" id="evidence-{ref}"><div class="evidence-id">{ref}</div>'
            f"<h3>{_escape(item.get('title'))}</h3><p>{_escape(item.get('source_name'))} · L{_escape(item.get('source_tier'))} · {_escape(strength)}</p>"
            f'<p class="published">发布日期：{_escape(item.get("published_at") or "未知")}</p>'
            f'{quotes}<p><a href="{_escape(item.get("url"))}" rel="noreferrer">访问公开来源</a> {snapshot}</p></article>'
        )
    return f'<section id="evidence"><h2>证据卡片</h2>{"".join(cards)}</section>'


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
"""
    script = "document.querySelectorAll('.citation').forEach(a=>a.addEventListener('click',()=>setTimeout(()=>document.querySelector(a.getAttribute('href'))?.classList.add('highlight'),0)));"
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title><style>{css}.highlight{{outline:3px solid #1c3f63}}</style></head><body><header><p>可核验舆情速览</p><h1>{title}</h1><p>{_escape(header.get("subtitle"))}</p></header><main>{"".join(body)}</main><script>{script}</script></body></html>'
