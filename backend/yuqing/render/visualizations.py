"""Offline, evidence-linked views over existing report fields; never invent chart data."""

from __future__ import annotations

import html
import re
from collections import defaultdict
from typing import Any

from yuqing.core.claim_semantics import publication_actor
from yuqing.core.verification_presentation import LABELS, NOTES, presentation_status


def _escape(value: Any) -> str:
    return html.escape(str(value) if value is not None else "", quote=True)


def _refs(item: dict) -> str:
    claims = item.get("claim_refs") or ([item["claim_ref"]] if item.get("claim_ref") else [])
    return "".join(
        f'<a class="{kind}" href="#{prefix}-{_escape(ref)}" aria-label="查看{label} {_escape(ref)}">[{_escape(ref)}]</a>'
        for kind, prefix, label, refs in (
            ("claim-link", "claim", "事实", claims),
            ("citation", "evidence", "证据", item.get("evidence_refs", [])),
        )
        for ref in dict.fromkeys(refs)
    )


def _category(text: str) -> str:
    # Only classify the explicit issuer, not institutions mentioned inside its account.
    actor = publication_actor(text)
    if not actor:
        actor = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日[，,\s]*", "", text)
        actor = re.split(r"于|在|\d|[，,。；;]", actor, maxsplit=1)[0][:35]
    if re.search(r"法院|法庭|检察院", actor):
        return "司法程序"
    if re.search(r"大学|学院|学校|校方", actor):
        return "学校处置"
    if re.search(r"部|监管局|调查组|政府|公安|委员会", actor):
        return "机构与监管"
    if re.search(r"汽车|公司|集团|客服|企业", actor):
        return "企业回应"
    if re.search(r"测试|检测|懂车帝|实验室", actor):
        return "测试与检测"
    if re.search(r"报|新闻|网|媒体|澎湃|中央社", actor):
        return "媒体报道"
    return "其他进展"


def _source_details(item: dict) -> str:
    quotes = "".join(
        "<blockquote><p>"
        + _escape(citation["quote"])
        + "</p><footer>"
        + _escape(citation.get("source_name") or citation.get("evidence_ref"))
        + " · "
        + _escape(
            {"verbatim": "原文引述", "paraphrase": "要点转述", "snippet": "搜索摘要"}.get(
                citation.get("quote_type"), "引用记录"
            )
        )
        + "</footer></blockquote>"
        for citation in item.get("citations", [])
        if citation.get("quote")
    )
    return (
        quotes + '<p class="visual-refs">回查依据 ' + _refs(item) + "</p>"
        '<p class="visual-print-reference">完整引文可在 HTML 节点或证据包中回查。</p>'
    )


def timeline_visual(block: dict) -> str:
    items = [item for item in block.get("items", []) if item.get("date") and item.get("text")]
    if not items:
        return ""
    items = sorted(items, key=lambda item: str(item["date"]))
    categories = list(dict.fromkeys(_category(item["text"]) for item in items))
    options = "".join(
        f'<option value="{_escape(category)}">{_escape(category)}</option>'
        for category in categories
    )
    events = []
    for item in items:
        category = _category(item["text"])
        lane = categories.index(category) + 1
        badge = presentation_status(item)
        window = (
            f'<span class="visual-window">{_escape(item["window_label"])}</span>'
            if item.get("window_label")
            else ""
        )
        events.append(
            f'<li class="visual-event" data-visual-category="{_escape(category)}" style="--lane:{lane};--lane-color:var(--lane-{(lane - 1) % 4})">'
            f'<time datetime="{_escape(item["date"])}">{_escape(str(item["date"])[:10])}</time>'
            '<span class="visual-dot" aria-hidden="true"></span><div class="visual-event-body">'
            f'<span class="visual-category">{_escape(category)}</span>{window}'
            '<details class="visual-detail"><summary>'
            f'<span class="visual-event-text">{_escape(item["text"])}</span>'
            '<span class="visual-expand-label">查看依据</span></summary>'
            + _source_details(item)
            + f'</details><span class="badge {badge}" title="{_escape(NOTES.get(badge, "事实核查结论"))}">{_escape(LABELS.get(badge, badge))}</span>'
            "</div></li>"
        )
    controls = (
        '<div class="visual-controls"><div class="visual-switch" role="group" aria-label="时间线布局">'
        '<button type="button" data-visual-layout="chronology" aria-pressed="true">按时间</button>'
        '<button type="button" data-visual-layout="lanes" aria-pressed="false">按类别分轨</button></div>'
        '<label>事件类别 <select data-visual-filter aria-label="筛选事件类别"><option value="">全部类别</option>'
        + options
        + '</select></label><button type="button" data-visual-reset>恢复全部</button>'
        '<output class="visual-filter-status" aria-live="polite">显示全部节点</output></div>'
        if len(categories) > 1
        else ""
    )
    return (
        f'<section class="chart correction-timeline report-visual visual-chronology" id="{_escape(block.get("block_id"))}" data-visual data-layout="chronology" style="--lane-count:{min(len(categories), 3)}">'
        f"<h2>{_escape(block.get('title') or '事件与回应脉络')}</h2>"
        '<p class="visual-guide">沿时间查看关键记录；展开节点可回查依据。类别仅用于阅读分组，不表示因果关系。</p>'
        + controls
        + '<ol class="visual-timeline">'
        + "".join(events)
        + "</ol>"
        '<p class="visual-caption">日期按节点原有记录展示；分轨视图各类别内按时间排序，节点间距不代表时间长度。'
        + _escape(block.get("note") or "核验状态仅作用于所列陈述，不表示其中的所有说法均已证实。")
        + "</p></section>"
    )


def analysis_visual(block: dict) -> str:
    """Use reviewed analytical fields verbatim; do not infer a new response status."""
    if block.get("section") not in {"04", "05"} or not block.get("is_editorial"):
        return ""
    items = block.get("items", [])
    if not items or not all(
        item.get("title")
        and item.get("observation")
        and item.get("interpretation")
        and item.get("uncertainty")
        and item.get("evidence_refs")
        for item in items
    ):
        return ""
    rows = []
    for item in items:
        columns = []
        for label, key in (
            ("所据记录", "observation"),
            ("解释与分歧", "interpretation"),
            ("边界与待查", "uncertainty"),
        ):
            # Full text stays in the document and in print; only screen reading is folded.
            columns.append(
                f'<div class="visual-issue-cell"><h4>{label}</h4><p>{_escape(item[key])}</p></div>'
            )
        implications = (
            '<p class="visual-implication"><strong>可能影响：</strong>'
            + _escape(item["implication"])
            + "</p>"
            if item.get("implication")
            else ""
        )
        rows.append(
            '<details class="visual-detail visual-issue" open><summary>'
            f'<span>{_escape(item["title"])}</span><span class="visual-expand-label">收起 / 展开</span></summary>'
            '<div class="visual-issue-grid">'
            + "".join(columns)
            + "</div>"
            + implications
            + (
                action_visual(item)
                or "".join(
                    '<p class="visual-implication"><strong>'
                    + label
                    + "：</strong>"
                    + _escape(item[key])
                    + "</p>"
                    for label, key in (
                        ("建议行动", "action"),
                        ("建议负责", "owner"),
                        ("触发条件", "trigger"),
                    )
                    if item.get(key)
                )
            )
            + '<p class="visual-refs">本项依据 '
            + _refs(item)
            + "</p></details>"
        )
    return (
        f'<section class="analysis-module report-visual visual-issues" id="{_escape(block.get("block_id"))}">'
        f"<h2>{_escape(block.get('title') or '问题与依据对照')}</h2>"
        '<p class="analysis-notice"><strong>分析判断：</strong>'
        + _escape(block.get("editorial_basis") or "依据现有事实与证据作出的条件性研判")
        + "。三列用于对照记录、解释和边界；已有回应不等于争议已解决。</p>"
        + "".join(rows)
        + "</section>"
    )


def action_visual(item: dict) -> str:
    if not all(item.get(key) for key in ("trigger", "owner", "action")):
        return ""
    return (
        '<div class="visual-action" role="group" aria-label="条件与建议行动">'
        "<div><span>触发条件</span><p>" + _escape(item["trigger"]) + "</p></div>"
        '<span class="visual-action-arrow" aria-hidden="true">→</span>'
        "<div><span>建议负责</span><p>" + _escape(item["owner"]) + "</p></div>"
        '<span class="visual-action-arrow" aria-hidden="true">→</span>'
        "<div><span>建议行动</span><p>" + _escape(item["action"]) + "</p></div></div>"
    )


def publication_visual(block: dict) -> tuple[str, set[str]]:
    nodes = {
        node.get("evidence_id"): node for node in block.get("nodes", []) if node.get("evidence_id")
    }
    groups: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    for edge in block.get("edges", []):
        source, target = edge.get("from_evidence_id"), edge.get("to_evidence_id")
        key = (source, target, edge.get("relation"))
        if (
            source not in nodes
            or target not in nodes
            or source == target
            or key in seen
            or edge.get("relation") not in {"repost", "response", "follow_up"}
            or edge.get("review_status") != "accepted"
            or not edge.get("evidence_refs")
            or not edge.get("quote")
        ):
            continue
        groups[source].append(edge)
        seen.add(key)
    if not groups:
        return "", set()

    def node_card(node: dict) -> str:
        return (
            '<div class="visual-publication"><strong>'
            + _escape(node.get("publisher"))
            + "</strong>"
            "<span>" + _escape(node.get("published_at") or "发布时间待确认") + "</span>"
            "<p>" + _escape(node.get("framing")) + "</p>" + _refs(node) + "</div>"
        )

    connected = set()
    branches = []
    for source, edges in groups.items():
        connected.add(source)
        targets = []
        for edge in edges:
            target = edge["to_evidence_id"]
            connected.add(target)
            label = {"repost": "转载", "response": "回应", "follow_up": "跟进"}[edge["relation"]]
            targets.append(
                '<div class="visual-relation"><div class="visual-relation-arrow"><span>'
                + label
                + '</span><span aria-hidden="true">→</span></div><div>'
                + node_card(nodes[target])
                + '<details class="visual-detail visual-relation-basis"><summary>查看连线依据</summary><blockquote><p>'
                + _escape(edge["quote"])
                + "</p></blockquote>"
                + _refs(edge)
                + "</details></div></div>"
            )
        branches.append(
            '<div class="visual-branch">'
            + node_card(nodes[source])
            + '<div class="visual-targets">'
            + "".join(targets)
            + "</div></div>"
        )
    return (
        '<div class="report-visual visual-publication-map"><p class="visual-guide">可回查的发布关系</p>'
        + "".join(branches)
        + '<p class="visual-caption">连线仅表示已记录并有引用依据的转载、回应或跟进关系，不代表完整传播路径、源头或影响力。发布时间不等于事件发生时间。</p></div>',
        connected,
    )


VISUAL_CSS = """
.report-visual{--lane-0:#0f6b78;--lane-1:#4263ad;--lane-2:#916524;--lane-3:#795b95;overflow-wrap:anywhere}
.visual-guide{color:var(--muted);font-size:14px;margin:0 0 18px}.visual-caption{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px;line-height:1.7}
.visual-print-reference{display:none}
.visual-controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:12px;background:#f2f6f8;border:1px solid #dce5eb;border-radius:6px;margin:16px 0 24px}
.visual-controls button,.visual-controls select{font:inherit;font-size:13px;padding:7px 10px;border:1px solid #b7c8d3;border-radius:4px;color:var(--ink-2);background:#fff;cursor:pointer}
.visual-controls label{font-size:13px;color:var(--muted)}.visual-controls button[aria-pressed=true]{background:var(--ink-2);color:#fff;border-color:var(--ink-2)}.visual-controls button:focus-visible,.visual-controls select:focus-visible,.visual-detail>summary:focus-visible{outline:3px solid #c78316;outline-offset:3px}
.visual-switch{display:flex;gap:4px}.visual-filter-status{font-size:12px;color:var(--muted);margin-left:auto}
.correction-timeline .visual-timeline{border:0;padding:0;margin:20px 0;list-style:none}
.correction-timeline .visual-event{display:grid;grid-template-columns:100px 24px minmax(0,1fr);gap:14px;padding:0 0 24px;position:relative}
.correction-timeline .visual-event:before{content:none}.visual-event>time{padding-top:12px;color:var(--ink-2);font:600 13px/1.7 Consolas,monospace}
.visual-dot{position:relative;align-self:stretch}.visual-dot:before{content:'';position:absolute;top:16px;left:6px;width:10px;height:10px;background:var(--lane-color);border-radius:50%;box-shadow:0 0 0 4px #fff,0 0 0 5px #d9e4e9;z-index:1}
.visual-dot:after{content:'';position:absolute;top:26px;bottom:-40px;left:10px;width:2px;background:#dce5eb}.visual-event:last-child .visual-dot:after{display:none}
.visual-event-body{padding:16px 20px;background:#f8fafb;border:1px solid #dce5eb;border-left:3px solid var(--lane-color);border-radius:0 7px 7px 0;min-width:0}
.visual-category{display:inline-block;color:var(--lane-color);font-size:12px;font-weight:700;margin:0 10px 8px 0}.visual-window{font-size:12px;color:#73623d}
.visual-detail>summary{cursor:pointer;list-style:none;display:flex;align-items:start;gap:16px;justify-content:space-between;line-height:1.8}.visual-detail>summary::-webkit-details-marker{display:none}.visual-event-text{font-size:15px;font-weight:600;color:var(--ink)}
.visual-expand-label{flex:0 0 auto;color:var(--teal);font-size:12px;font-weight:400;padding-top:2px}.visual-event .badge{margin-top:10px}.visual-detail blockquote{margin:16px 0 10px!important;padding:10px 14px;border-left:2px solid #96b2c1;background:#fff}.visual-detail blockquote p{margin:0;font-size:14px}.visual-detail blockquote footer{font-size:12px;color:var(--muted);margin-top:6px}.visual-refs{font-size:12px;color:var(--muted)}
.visual-chronology[data-layout=lanes] .visual-timeline{display:grid;grid-template-columns:repeat(var(--lane-count),minmax(240px,1fr));gap:14px;grid-auto-flow:row dense;overflow-x:auto;padding-bottom:12px}
.visual-chronology[data-layout=lanes] .visual-event{display:block;grid-column:var(--visual-column);padding:0}.visual-chronology[data-layout=lanes] .visual-dot{display:none}.visual-chronology[data-layout=lanes] .visual-event>time{display:block;padding:0 0 8px}.visual-chronology[data-layout=lanes] .visual-event-body{height:calc(100% - 30px)}
.visual-event[hidden]{display:none!important}
.visual-issue{border:1px solid #cbd9e2;border-radius:6px;margin:18px 0;background:#fff;overflow:hidden}.visual-issue>summary{background:#edf4f6;padding:16px 18px;font-weight:700;color:var(--ink-2)}.visual-issue-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}.visual-issue-cell{padding:16px 18px;border-right:1px solid #dce5eb;min-width:0}.visual-issue-cell:last-child{border-right:0;background:#fffbf1}.visual-issue-cell h4{margin:0 0 12px;font-size:13px;color:var(--teal)}.visual-issue-cell:last-child h4{color:#856018}.visual-issue-cell p{font-size:14px;white-space:pre-line;margin:0}.visual-issue>.visual-refs,.visual-implication{margin:12px 18px;font-size:14px}
.visual-action{display:grid;grid-template-columns:minmax(0,1fr) 22px minmax(0,.7fr) 22px minmax(0,1.3fr);gap:10px;padding:16px;margin:16px 0;background:#f0f6f5;border:1px solid #d7e6e2;border-radius:6px;align-items:start}.visual-issue>.visual-action{margin:16px 18px}.visual-action>div>span{display:block;font-weight:700;font-size:12px;color:var(--teal);margin-bottom:8px}.visual-action p{font-size:14px;margin:0}.visual-action-arrow{align-self:center;color:var(--teal);font-size:22px}
.visual-publication-map{padding:20px;background:#f7fafb;border:1px solid #d8e3e9;border-radius:6px;margin-bottom:20px}.visual-branch{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.7fr);gap:24px;margin:18px 0;align-items:start}.visual-publication{padding:16px;background:#fff;border:1px solid #cbdbe3;border-top:3px solid var(--teal);border-radius:4px}.visual-publication>strong,.visual-publication>span{display:block}.visual-publication>strong{font-size:15px}.visual-publication>span{font-size:12px;color:var(--muted);margin-top:6px}.visual-publication p{font-size:13px;margin:12px 0}.visual-targets{border-left:2px solid #aac4ce;padding-left:16px}.visual-relation{display:grid;grid-template-columns:44px minmax(0,1fr);gap:8px;margin:0 0 18px}.visual-relation-arrow{color:var(--teal);display:flex;flex-direction:column;align-items:center;font-size:22px}.visual-relation-arrow>span:first-child{font-size:12px}.visual-relation-basis{font-size:12px;margin:8px 0}.visual-publication-extra>summary{cursor:pointer;color:var(--muted);font-size:14px;padding:12px 0}
@media(max-width:1100px){.visual-issue-grid{grid-template-columns:1fr}.visual-issue-cell{border-right:0;border-bottom:1px solid #dce5eb}.visual-issue-cell:last-child{border-bottom:0}}
@media(max-width:700px){.correction-timeline .visual-event{grid-template-columns:18px minmax(0,1fr);gap:10px}.visual-event>time{grid-column:2;padding:0 0 8px}.visual-dot{grid-column:1;grid-row:1/3}.visual-event-body{grid-column:2;padding:14px}.visual-detail>summary{flex-wrap:wrap;gap:4px}.visual-filter-status{margin-left:0;width:100%}.visual-chronology[data-layout=lanes] .visual-timeline{display:block}.visual-chronology[data-layout=lanes] .visual-event{margin-bottom:18px}.visual-action{grid-template-columns:1fr}.visual-action-arrow{transform:rotate(90deg);justify-self:center;line-height:1}.visual-branch{grid-template-columns:1fr;gap:14px}.visual-publication-map{padding:12px}}
@media print{
 .visual-controls,.visual-expand-label{display:none!important}
 .visual-event blockquote{display:none!important}.visual-print-reference{display:block;font-size:11px;color:var(--muted)}
 .report-visual{print-color-adjust:exact;-webkit-print-color-adjust:exact;break-inside:auto!important}
 .report-section:has(.visual-chronology){break-before:page}
 .report-section:has(.visual-chronology)>.section-marker,.report-visual>h2,.visual-guide{break-after:avoid-page}
 .visual-chronology[data-layout] .visual-timeline{display:block!important}
 .visual-chronology[data-layout] .visual-event,.visual-event[hidden]{display:grid!important;grid-template-columns:85px 18px minmax(0,1fr)!important;gap:10px;padding:0 0 14px;break-inside:avoid}
 .visual-chronology[data-layout] .visual-event>time{grid-column:auto;display:block;padding-top:10px}
 .visual-chronology[data-layout] .visual-dot{display:block;grid-column:auto;grid-row:auto}
 .visual-chronology[data-layout] .visual-event-body{grid-column:auto;height:auto;padding:12px}
 .visual-issue{overflow:visible;break-inside:auto}.visual-issue-grid{display:block}.visual-issue-cell{border-right:0;border-bottom:1px solid #dce5eb;break-inside:auto}
 .visual-detail::details-content{content-visibility:visible!important;display:block!important}.visual-detail>summary{display:block}
 .visual-action{display:block;break-inside:auto}.visual-action>div{margin:8px 0}.visual-action-arrow{display:none}
 .visual-publication-map{background:#fff}.visual-branch{grid-template-columns:minmax(0,1fr) minmax(0,1.5fr);gap:12px;break-inside:auto}.visual-relation{break-inside:avoid}.visual-detail blockquote{break-inside:auto}
}
"""


VISUAL_SCRIPT = r"""
document.querySelectorAll('[data-visual]').forEach(visual=>{
  const filter=visual.querySelector('[data-visual-filter]');
  const events=[...visual.querySelectorAll('.visual-event')];
  const refresh=()=>{
    const category=filter?.value||'';
    const categories=[...new Set(events.map(item=>item.dataset.visualCategory))];
    const visible=category?[category]:categories;
    visual.style.setProperty('--lane-count',String(visible.length));
    events.forEach(item=>{
      item.hidden=!!category&&item.dataset.visualCategory!==category;
      item.style.setProperty('--visual-column',String(visible.indexOf(item.dataset.visualCategory)+1));
    });
    const status=visual.querySelector('.visual-filter-status');
    if(status)status.textContent=category?`仅显示：${category}（${events.filter(item=>!item.hidden).length} 个节点）`:'显示全部节点';
  };
  filter?.addEventListener('change',refresh);
  visual.querySelectorAll('[data-visual-layout]').forEach(button=>button.addEventListener('click',()=>{
    visual.dataset.layout=button.dataset.visualLayout;
    visual.querySelectorAll('[data-visual-layout]').forEach(other=>other.setAttribute('aria-pressed',String(other===button)));
    refresh();
  }));
  visual.querySelector('[data-visual-reset]')?.addEventListener('click',()=>{
    if(filter)filter.value='';
    visual.dataset.layout='chronology';
    visual.querySelectorAll('[data-visual-layout]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.visualLayout==='chronology')));
    refresh();
  });
  refresh();
});
// Print must include folded chart details, irrespective of on-screen selection.
let visualPrintSnapshot=null;
const prepareVisualPrint=()=>{
  if(visualPrintSnapshot)return;
  visualPrintSnapshot=[...document.querySelectorAll('details.visual-detail')].map(detail=>[detail,detail.open]);
  visualPrintSnapshot.forEach(([detail])=>{detail.open=true;});
};
const restoreVisualPrint=()=>{
  visualPrintSnapshot?.forEach(([detail,open])=>{detail.open=open;});
  visualPrintSnapshot=null;
};
window.addEventListener('beforeprint',prepareVisualPrint);
window.addEventListener('afterprint',restoreVisualPrint);
window.matchMedia('print').addEventListener('change',event=>event.matches?prepareVisualPrint():restoreVisualPrint());
"""
