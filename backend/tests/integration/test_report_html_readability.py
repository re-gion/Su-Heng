import copy

from yuqing.render.html import render_html


def _report() -> dict:
    return {
        "report_id": "readability",
        "task": {"task_id": "task/readability", "event_query": "高校决策测试"},
        "blocks": [
            {
                "type": "report_header",
                "section": "00",
                "event_title": "高校决策测试",
            },
            {
                "block_id": "summary",
                "type": "executive_summary",
                "section": "01",
                "in_brief": True,
                "what": [{"text": "尚无充分材料支持该结论。", "claim_ref": "C001"}],
            },
            {
                "block_id": "facts",
                "type": "fact_check_table",
                "section": "03",
                "in_brief": True,
                "items": [
                    {
                        "claim_ref": "C001",
                        "text": "尚无充分材料支持该结论。",
                        "badge": "unverified",
                        "citations": [{"evidence_ref": "E001"}],
                    }
                ],
            },
            {
                "block_id": "history",
                "type": "history_compare",
                "section": "06",
                "title": "历史对照",
                "cards": [
                    {
                        "event_name": "既往事件",
                        "summary": "处置路径相似",
                        "comparison": "处置路径相似",
                        "evidence_refs": ["E001"],
                    }
                ],
            },
            {
                "block_id": "analysis",
                "type": "analysis",
                "section": "07",
                "title": "决策研判",
                "is_editorial": True,
                "editorial_basis": "依据当前公开证据作出的条件性判断",
                "items": [
                    {
                        "title": "观察窗口",
                        "observation": "公开信息仍在更新",
                        "action": "每周复核一次",
                        "owner": "校级工作组",
                        "uncertainty": "后续通报可能改变判断",
                        "claim_refs": ["C001"],
                        "evidence_refs": ["E001"],
                    }
                ],
            },
            {
                "block_id": "metrics",
                "type": "metric_cards",
                "section": "04",
                "title": "来源指标",
                "data_basis": "quoted_evidence",
                "items": [
                    {
                        "label": "报名人数",
                        "value": 120,
                        "unit": "人",
                        "scope": "来源自述样本",
                        "period": "2026 年秋季",
                        "quote": "报名人数为120人",
                        "evidence_refs": ["E001"],
                        "source_name": "学院公告",
                    }
                ],
            },
            {
                "block_id": "appendix",
                "type": "evidence_appendix",
                "section": "09",
                "items": [
                    {
                        "evidence_ref": "E001",
                        "title": "学院公告",
                        "source_name": "某高校",
                        "source_tier": 2,
                        "fetch_status": "fetched",
                        "original_excerpt": "公告原文内容",
                        "citations": [{"claim_ref": "C001", "quote": "关键引文"}],
                        "url": "https://example.edu/notice",
                    }
                ],
            },
        ],
    }


def test_readability_modules_preserve_authority_and_remove_history_duplication():
    rendered = render_html(_report(), view="brief")

    assert rendered.count("处置路径相似") == 1
    assert '<h2 class="sr-only">执行摘要</h2>' in rendered
    assert '<span class="badge unverified" title="事实核查结论">待核验</span>' in rendered
    assert "分析判断" in rendered
    assert "以下内容不是确定性事实" in rendered
    assert "校级工作组" in rendered
    assert "数值保留来源归属，不等同于系统核验结论" in rendered
    assert "该数值为来源自述，未自动视为已核验事实" in rendered
    assert "学院公告" in rendered


def test_evidence_is_collapsed_with_navigation_and_print_safeguards():
    rendered = render_html(_report(), view="brief")

    assert '<details class="evidence-card" id="evidence-E001"><summary>' in rendered
    assert "公告原文内容" in rendered
    assert 'data-evidence-action="expand"' in rendered
    assert 'data-evidence-action="collapse"' in rendered
    assert "applyView('full')" in rendered
    assert "if(ancestor.tagName==='DETAILS')ancestor.open=true" in rendered
    assert "if(location.hash)revealTarget(location.hash)" in rendered
    assert ".evidence-card .evidence-body{display:none!important}" in rendered
    assert 'href="/?task=task%2Freadability"' in rendered


def test_long_fact_table_and_uncited_evidence_are_grouped_without_losing_targets():
    report = _report()
    facts = next(block for block in report["blocks"] if block["type"] == "fact_check_table")
    base_claim = facts["items"][0]
    facts["items"] = []
    for index in range(1, 11):
        claim = copy.deepcopy(base_claim)
        claim["claim_ref"] = f"C{index:03d}"
        claim["text"] = f"陈述 {index}"
        facts["items"].append(claim)
    facts["priority_claim_refs"] = ["C010", "C009"]

    appendix = next(block for block in report["blocks"] if block["type"] == "evidence_appendix")
    uncited = copy.deepcopy(appendix["items"][0])
    uncited.update(
        {
            "evidence_ref": "E002",
            "title": "背景材料",
            "cited_in_report": False,
        }
    )
    appendix["items"].append(uncited)

    rendered = render_html(report, view="brief")

    first_card = rendered.index('id="claim-C010"')
    overflow = rendered.index('class="claim-overflow"')
    assert first_card < overflow
    assert "其余陈述与核验记录（8）" in rendered
    assert 'id="claim-C001"' in rendered
    assert "未被正文引用的证据（1）" in rendered
    assert 'id="evidence-E002"' in rendered


def test_sections_follow_number_order_and_text_body_survives_grouping():
    report = _report()
    report["blocks"].insert(
        0,
        {
            "block_id": "closing-note",
            "type": "text",
            "section": "09",
            "title": "附录说明",
            "text": "正文保留：待核资料应继续核查。",
            "in_brief": True,
        },
    )
    rendered = render_html(report, view="brief")
    assert (
        rendered.index('id="report-section-01"')
        < rendered.index('id="report-section-04"')
        < rendered.index('id="report-section-09"')
    )
    assert rendered.index('id="report-section-07"') < rendered.index('id="report-section-09"')
    assert (
        '<section id="closing-note"><h2>附录说明</h2><p>正文保留：待核资料应继续核查。</p></section>'
        in rendered
    )
