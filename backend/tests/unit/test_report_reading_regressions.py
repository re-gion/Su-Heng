"""Reading order and identity grouping must preserve original review records."""

import copy
import re
from html.parser import HTMLParser

from yuqing.core.event_chronology import event_timeline
from yuqing.core.history_cards import unique_history_cards
from yuqing.core.timeline_grouping import group_timeline_events
from yuqing.render.history import history_chapter
from yuqing.render.html import _fact_table, _history_chapter


def fact(ref, text, evidence):
    return {
        "claim_ref": ref,
        "text": text,
        "badge": "unverified",
        "verification_state": "complete",
        "citations": [{"evidence_ref": evidence, "relation": "support"}],
    }


def test_posting_aliases_and_relative_dates_share_one_reading_card():
    facts = [
        fact(
            "C001",
            "2023年10月11日，一名女生发帖称同年7月在明川大学图书馆自习时遭该校一名男生骚扰。",
            "E001",
        ),
        fact(
            "C002",
            "2023年10月11日下午，一名女生在社交平台发帖称，2023年7月在明川大学图书馆自习时受到该校一名男生骚扰。",
            "E002",
        ),
        fact(
            "C003",
            "2023年10月11日下午，一名女生在微信公众号发布自述文章，称当年7月在明川大学图书馆自习时遭一名男生骚扰。",
            "E003",
        ),
    ]
    before = copy.deepcopy(facts)
    nodes = group_timeline_events(event_timeline(facts))
    assert len(nodes) == 1
    assert set(nodes[0]["claim_refs"]) == {"C001", "C002", "C003"}
    assert {n["text"] for n in nodes[0]["related_claims"]} == {f["text"] for f in facts}
    assert group_timeline_events(nodes) == nodes
    assert facts == before


def test_same_release_issuer_website_is_not_a_different_actor():
    facts = [
        fact("C001", "云海公司2026年2月3日发布情况说明，宣布召回3台设备。", "E001"),
        fact("C002", "2026年2月3日，云海公司官网发布情况说明称召回3台设备。", "E002"),
    ]
    assert len(group_timeline_events(event_timeline(facts))) == 1


def test_direct_event_and_public_video_aliases_are_not_repeated_as_separate_events():
    direct = [
        fact(
            "C001",
            "2023年6月1日，学生在明川大学食堂吃出疑似异物，该异物被食堂工作人员当日丢弃。",
            "E001",
        ),
        fact(
            "C002",
            "2023年6月1日，明川大学学生在食堂吃出疑似为异物的物体，该异物被涉事食堂工作人员在事发当日丢弃。",
            "E002",
        ),
    ]
    video = [
        fact(
            "C003",
            "2023年6月1日，一段称明川大学饭菜疑吃出异物的视频在社交平台出现并广泛传播。",
            "E003",
        ),
        fact("C004", "2023年6月1日下午，网络传播一则视频，称明川大学饭菜中疑似吃出异物。", "E004"),
    ]
    assert len(group_timeline_events(event_timeline(direct))) == 1
    assert len(group_timeline_events(event_timeline(video))) == 1
    assert len(group_timeline_events(event_timeline(direct + video))) == 2
    other = copy.deepcopy(direct[1])
    other["text"] = other["text"].replace("明川大学", "星海大学")
    assert len(group_timeline_events(event_timeline([direct[0], other]))) == 2


def test_compound_action_overview_cannot_bridge_conflicting_recall_records():
    facts = [
        fact("C001", "云海公司2024年1月5日发布通报称，发现设备缺陷后启动召回并暂停销售。", "E001"),
        fact(
            "C002",
            "云海公司2024年1月5日发布通报称，发现设备缺陷后启动召回3台设备并暂停销售。",
            "E001",
        ),
        fact(
            "C003",
            "云海公司2024年1月5日发布通报称，发现设备缺陷后启动召回4台设备并暂停销售。",
            "E001",
        ),
        fact(
            "C004",
            "云海公司2024年1月5日发布通报称，发现设备缺陷后启动召回3台设备并恢复销售。",
            "E001",
        ),
    ]
    nodes = group_timeline_events(event_timeline(facts))
    assert {r for n in nodes for r in n["claim_refs"]} == {f["claim_ref"] for f in facts}
    assert not any({"C002", "C003"}.issubset(n["claim_refs"]) for n in nodes)
    assert not any({"C002", "C004"}.issubset(n["claim_refs"]) for n in nodes)
    direct = [
        fact("C005", "2024年1月5日，云海公司发现设备裂纹并终止使用该设备。", "E005"),
        fact("C006", "2024年1月5日，云海公司终止使用该设备。", "E006"),
    ]
    assert len(group_timeline_events(event_timeline(direct))) == 2


def test_same_day_attributed_release_and_institution_department_keep_all_originals():
    facts = [
        fact(
            "C001",
            "明川大学学生工作部2023年10月13日发布处理通报称，经调查核实决定给予2022级本科生一名学生记过处分。",
            "E001",
        ),
        fact(
            "C002",
            "2023年10月13日，明川大学发布处理通报称，对涉及学生的网上举报经调查核实，决定给予2022级本科生一名学生记过处分。",
            "E002",
        ),
        fact(
            "C003",
            "示例新闻网2023年10月13日报道称，明川大学学生工作部当日发布处理通报，给予2022级本科生一名学生记过处分。",
            "E003",
        ),
    ]
    nodes = group_timeline_events(event_timeline(facts))
    assert len(nodes) == 1
    assert {member["claim_ref"] for member in nodes[0]["related_claims"]} == {
        "C001",
        "C002",
        "C003",
    }


def test_conflicting_incident_months_or_posters_are_never_folded_together():
    for first, second in [
        ("同年7月", "同年8月"),
        ("2022年7月", "2023年7月"),
    ]:
        facts = [
            fact(
                "C001", f"2023年10月11日，一名女生发帖称{first}在明川大学图书馆受到骚扰。", "E001"
            ),
            fact(
                "C002", f"2023年10月11日，一名女生发帖称{second}在明川大学图书馆受到骚扰。", "E002"
            ),
        ]
        assert len(group_timeline_events(event_timeline(facts))) == 2
    facts = [
        fact("C001", "2023年10月11日，甲医院护士发帖称收到工作安排。", "E001"),
        fact("C002", "2023年10月11日，乙医院护士发文称收到工作安排。", "E002"),
    ]
    assert len(group_timeline_events(event_timeline(facts))) == 2


def test_anonymous_post_different_object_gender_or_institution_is_not_erased():
    for first, second in [
        ("在明川大学图书馆遭一名男生骚扰", "在明川大学图书馆遭一名女生骚扰"),
        ("在明川大学图书馆遭一名男生骚扰", "在星海大学图书馆遭一名男生骚扰"),
        ("明川大学图书馆发生物品遗失", "星海大学图书馆发生物品遗失"),
    ]:
        facts = [
            fact("C001", "2023年10月11日，一名女生发帖称" + first + "。", "E001"),
            fact("C002", "2023年10月11日，一名女生发文称" + second + "。", "E002"),
        ]
        assert len(group_timeline_events(event_timeline(facts))) == 2


def test_gender_unspecified_anonymous_version_can_share_the_same_post_card():
    facts = [
        fact(
            "C001", "2023年10月11日，一名女生发帖称在明川大学图书馆自习时遭一名男生骚扰。", "E001"
        ),
        fact("C002", "2023年10月11日，明川大学女生发文称其在图书馆自习时遭涉事学生骚扰。", "E002"),
    ]
    nodes = group_timeline_events(event_timeline(facts))
    assert len(nodes) == 1
    assert {n["text"] for n in nodes[0]["related_claims"]} == {f["text"] for f in facts}


def test_fact_table_sorts_dated_actions_and_keeps_unknowns_last_despite_priority():
    block = {
        "section": "03",
        "priority_claim_refs": ["C001", "C004", "C003"],
        "items": [
            fact("C001", "明川大学2025年9月20日发布情况通报。", "E001"),
            fact("C002", "2023年10月11日，明川大学成立调查组。", "E002"),
            fact("C003", "明川大学通报称，关于2023年7月11日事件的调查仍在进行。", "E003"),
            fact("C004", "2024年2月4日，明川大学发布补充材料。", "E004"),
            fact("C005", "2023年10月11日，一名学生发帖。", "E005"),
        ],
    }
    original = copy.deepcopy(block)
    html = _fact_table(block)
    assert re.findall(r'id="claim-(C\d+)"', html) == ["C002", "C005", "C004", "C001", "C003"]
    assert "时间未明确" in html
    assert block == original


def test_fact_table_folds_same_event_and_status_without_hiding_other_badges():
    post = "2023年10月11日，一名女生发帖称同年7月在明川大学图书馆自习时遭该校一名男生骚扰。"
    variant = (
        "2023年10月11日下午，一名女生在社交平台发帖称，"
        "2023年7月在明川大学图书馆自习时受到该校一名男生骚扰。"
    )
    reply = "2023年10月11日晚，明川大学发布情况说明称，已成立工作组调查。"
    certified = {**fact("C004", post, "E004"), "badge": "verified"}
    block = {
        "section": "03",
        "items": [
            fact("C001", post, "E001"),
            fact("C003", reply, "E003"),
            fact("C002", variant, "E002"),
            certified,
        ],
    }
    saved = copy.deepcopy(block)

    class Cards(HTMLParser):
        def __init__(self):
            super().__init__()
            self.inside_details = 0
            self.folded, self.visible = [], []

        def handle_starttag(self, tag, attrs):
            if tag == "details":
                self.inside_details += 1
            if tag == "article":
                (self.folded if self.inside_details else self.visible).append(dict(attrs)["id"])

        def handle_endtag(self, tag):
            if tag == "details":
                self.inside_details -= 1

    page = _fact_table(block)
    cards = Cards()
    cards.feed(page)
    assert cards.visible == ["claim-C001", "claim-C004", "claim-C003"]
    assert cards.folded == ["claim-C002"]
    assert all(f'href="#evidence-E00{i}"' in page for i in range(1, 5))
    assert block == saved


def test_fact_table_folds_undated_media_attribution_to_same_official_publication():
    basis = "明川大学2025年8月1日发布情况通报称，已组建专家组对设备质量开展全面复核。"
    quoted = (
        "地方新闻报道，明川大学2025年8月1日在官网发布情况通报称，"
        "已组建专家组对设备质量开展全面复核。"
    )
    late = "地方新闻2025年9月20日报道，" + basis
    facts = [
        fact("C001", basis, "E001"),
        fact("C002", quoted, "E002"),
        fact("C003", late, "E003"),
    ]
    nodes = event_timeline(facts)
    assert [(n["claim_ref"], n["date"]) for n in nodes] == [
        ("C001", "2025-08-01"),
        ("C002", "2025-08-01"),
        ("C003", "2025-09-20"),
    ]
    rendered = _fact_table({"section": "03", "items": facts})
    assert re.findall(r'id="claim-(C\d+)"', rendered) == ["C001", "C002", "C003"]
    assert "展开" in rendered and 'href="#evidence-E002"' in rendered


def test_exact_named_case_keeps_each_stage_and_its_own_sources():
    cards = [
        {
            "event_name": "云海公司设备召回事件",
            "summary": "云海公司宣布暂停设备出货。",
            "evidence_refs": ["E001"],
        },
        {
            "event_name": "云海公司设备召回事件",
            "summary": "云海公司随后宣布召回问题设备。",
            "evidence_refs": ["E002"],
        },
    ]
    saved = copy.deepcopy(cards)
    merged = unique_history_cards(cards)
    assert len(merged) == 1
    assert {s["summary"] for s in merged[0]["stages"]} == {c["summary"] for c in cards}
    assert [s["evidence_refs"] for s in merged[0]["stages"]] == [["E001"], ["E002"]]
    assert unique_history_cards(merged) == merged
    assert cards == saved


def test_near_named_unconfirmed_history_leads_share_a_card_without_asserting_identity():
    references = [
        {
            "event_name": "明川大学教师被举报骚扰事件",
            "institution": "明川大学",
            "summary": "学校先宣布暂停相关工作。",
            "evidence_refs": ["E001"],
            "reason": "来源日期未确认。",
        },
        {
            "event_name": "明川大学教师被举报骚扰学生事件",
            "institution": "明川大学",
            "summary": "学校随后公布调查处理结果。",
            "evidence_refs": ["E002"],
            "reason": "来源日期未确认。",
        },
        {
            "event_name": "星海大学教师被举报骚扰事件",
            "institution": "星海大学",
            "summary": "另一所学校调查。",
            "evidence_refs": ["E003"],
            "reason": "来源日期未确认。",
        },
    ]
    blocks = [
        {
            "block_id": "history",
            "type": "history_compare",
            "section": "06",
            "cards": [],
            "reference_candidates": references,
        }
    ]
    original = copy.deepcopy(blocks)
    html = _history_chapter(history_chapter(blocks))
    assert html.count('class="narrative-card history-reference"') == 2
    assert "身份待核" in html
    assert all(card["summary"] in html for card in references)
    assert all(f'href="#evidence-E00{i}"' in html for i in (1, 2, 3))
    assert blocks == original
