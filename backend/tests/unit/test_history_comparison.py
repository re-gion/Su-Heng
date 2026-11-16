from types import SimpleNamespace

from yuqing.core.history_cards import unique_history_cards
from yuqing.services.history_comparison import independent_case_evidence, same_investigation_event


def test_repeated_claims_about_one_case_make_one_card_with_all_refs():
    cards = [
        {
            "event_name": "清华大学处置事件",
            "case_type": "analogous",
            "summary": "短述",
            "evidence_refs": ["E025"],
            "claim_ref": "C011",
        },
        {
            "event_name": " 清华大学处置事件 ",
            "case_type": "analogous",
            "summary": "更完整的经过",
            "evidence_refs": ["E026"],
            "claim_ref": "C012",
        },
        {"event_name": "另一所学校的事件", "case_type": "analogous", "evidence_refs": ["E027"]},
    ]

    result = unique_history_cards(cards)

    assert len(result) == 2
    assert result[0]["summary"] == "更完整的经过"
    assert result[0]["evidence_refs"] == ["E025", "E026"]
    assert result[0]["claim_refs"] == ["C011", "C012"]


def test_history_aliases_can_share_a_dated_decision_without_discarding_its_later_stages():
    def card(name, published, *facts):
        return {
            "event_name": name,
            "institution": "示例大学",
            "event_time": f"资料发布：{published}",
            "background_status": "complete",
            "claim_refs": [ref for ref, _ in facts],
            "background_facts": [{"claim_ref": ref, "text": text} for ref, text in facts],
        }

    cards = [
        card(
            "示例大学考生因处分起诉案",
            "2023-06-30",
            (
                "C001",
                "示例大学对涉事学生作出开除决定，甲区人民法院2022年7月5日以作出处分前未告知事实理由并听取陈述申辩属程序违法为由撤销决定。",
            ),
        ),
        card(
            "示例大学考试纪律处理争议",
            "2023-07-01",
            ("C002", "2021年11月15日示例大学认定一名基础医学院学生在闭卷考试中使用通讯设备作弊。"),
            (
                "C003",
                "2022年7月5日甲区人民法院判决认定示例大学作出处分前未告知事实理由并听取陈述申辩属程序违法。",
            ),
        ),
        card(
            "示例大学学生被认定作弊案",
            "2023-07-01",
            ("C004", "示例大学2021年11月15日认定一名基础医学院学生在闭卷考试时使用通讯设备作弊。"),
        ),
        card(
            "示例大学纪律处分行政复核案",
            "2023-08-01",
            (
                "C005",
                "2022年8月5日甲区人民法院判决认定示例大学作出处分前未告知事实理由并听取陈述申辩属程序违法。",
            ),
        ),
    ]

    result = unique_history_cards(cards, merge_reviewed_aliases=True)

    assert len(result) == 2
    assert set(result[0]["claim_refs"]) == {"C001", "C002", "C003", "C004"}
    assert result[1]["claim_refs"] == ["C005"]


def claim(case):
    return SimpleNamespace(analysis_data={"historical_case": case}, evidence_ids=["E001"])


def test_changing_the_event_suffix_does_not_create_a_historical_case():
    current = "示例Ｂ800刹车踏板支架断裂问题"
    case = {
        "name": "示例B800刹车踏板支架断裂事件",
        "institution": "示例",
        "independent": True,
        "similarity": "制动调查机制",
        "difference": "被误记为另一起事件",
    }
    item = claim(case)
    item.text = "示例B800的刹车踏板支架断裂后，由主管部门调查。"
    assert not independent_case_evidence(
        item, {"E001": source(case["name"] + "。" + item.text)}, current, "2026-01-01"
    )
    case["name"] = "示例B700制动踏板无法回弹召回案"
    item.text = "示例B700制动踏板无法回弹后启动召回。"
    assert independent_case_evidence(
        item, {"E001": source(case["name"] + "。" + item.text)}, current, "2026-01-01"
    ) == {"E001"}


def test_shared_product_and_close_action_words_reject_locale_variants_of_same_event():
    assert same_investigation_event(
        "示例B800刹车踏板支架断裂问题", "示例B800煞車踏板支架實測斷裂事件"
    )
    assert not same_investigation_event("示例B800刹车踏板支架断裂问题", "示例B800转向助力失效召回")
    assert not same_investigation_event(
        "示例B800刹车踏板支架断裂问题", "示例B700煞車踏板支架實測斷裂事件"
    )


def test_a_component_alias_and_shorter_title_do_not_make_a_second_case():
    assert same_investigation_event("示例B800刹车支架断裂", "示例汽车B800制动踏板支架底座争议")
    assert not same_investigation_event("示例B800刹车支架断裂", "示例汽车B800制动助力器失效召回")


def test_history_subject_must_belong_to_the_claim_not_another_entity_on_same_page():
    case = {
        "name": "中央食品办公室约谈明川大学事件",
        "institution": "Central Food Safety Office",
        "independent": True,
        "similarity": "校园食品安全处置",
        "difference": "处置程序不同",
        "background": {"subject": "明川大学", "event": "食堂异物", "progress": "终止餐饮合同"},
    }
    item = claim(case)
    item.text = "明川大学因食堂异物终止餐饮合同。"
    evidence = source(
        "Central Food Safety Office discussed Example University and its canteen incident."
    )
    assert not independent_case_evidence(item, {"E001": evidence}, "本事件", "2026-01-01")


def test_source_language_anchor_does_not_establish_an_unknown_translated_case_alias():
    case = {
        "name": "Example University canteen incident",
        "institution": "Example University",
        "independent": True,
        "similarity": "食堂处置",
        "difference": "机构不同",
        "background": {"subject": "明川大学", "event": "食堂异物", "progress": "终止餐饮合同"},
    }
    item = claim(case)
    item.text = "明川大学因食堂异物终止餐饮合同。"
    evidence = source("Example University terminates catering contract after canteen incident.")
    assert not independent_case_evidence(item, {"E001": evidence}, "本事件", "2026-01-01")
    # A matching Chinese display name still cannot establish a bilingual alias.
    case["name"] = "明川大学食堂异物事件"
    assert not independent_case_evidence(item, {"E001": evidence}, "本事件", "2026-01-01")
    # The source-language institution must itself be bound to the reviewed claim.
    item.text = "Example University（明川大学）因食堂异物终止餐饮合同。"
    assert independent_case_evidence(item, {"E001": evidence}, "本事件", "2026-01-01") == {"E001"}


def test_unresolved_case_can_be_compared_without_inventing_an_outcome():
    case = {
        "name": "另一品牌制动系统召回事件",
        "institution": "另一品牌",
        "independent": True,
        "similarity": "安全缺陷调查与召回机制",
        "difference": "车型和故障部件不同",
    }
    assert independent_case_evidence(
        claim(case), {"E001": source("另一品牌启动制动系统召回")}, "本品牌安全事件", "2026-01-01"
    ) == {"E001"}
    unconfirmed = source("另一品牌启动制动系统召回")
    unconfirmed.extra["date_provenance"] = "search_provider"
    assert not independent_case_evidence(
        claim(case), {"E001": unconfirmed}, "本品牌安全事件", "2026-01-01"
    )
    assert independent_case_evidence(
        claim(case),
        {"E001": unconfirmed},
        "本品牌安全事件",
        "2026-01-01",
        require_confirmed_date=False,
    ) == {"E001"}
    unconfirmed.published_at = "2026-02-01"
    assert not independent_case_evidence(
        claim(case),
        {"E001": unconfirmed},
        "本品牌安全事件",
        "2026-01-01",
        require_confirmed_date=False,
    )


def source(title, date="2025-05-01", scope="history", fetched=False):
    return SimpleNamespace(
        title=title,
        content_text=title,
        snippet="",
        published_at=date,
        fetch_status="fetched" if fetched else "discovered",
        extra={"scope_status": scope, "date_provenance": "page_visible"},
    )


def test_history_requires_distinct_case_metadata_and_source():
    qualified = {
        "name": "某高校纠纷处置案",
        "institution": "某高校",
        "independent": True,
        "similarity": "复核机制",
        "difference": "主体与事实不同",
        "outcome": "已公开处分",
    }
    assert (
        independent_case_evidence(
            claim({}), {"E001": source("某高校公开复核结果")}, "武汉大学图书馆事件", "2026-01-01"
        )
        == set()
    )
    assert independent_case_evidence(
        claim(qualified), {"E001": source("某高校公开复核结果")}, "武汉大学图书馆事件", "2026-01-01"
    ) == {"E001"}
    assert (
        independent_case_evidence(
            claim(qualified),
            {"E001": source("某高校公开复核结果", date="2026-02-01")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    assert (
        independent_case_evidence(
            claim(qualified),
            {"E001": source("别的事件，未提及机构")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    same = {**qualified, "institution": "武汉大学"}
    assert (
        independent_case_evidence(
            claim(same),
            {"E001": source("武汉大学公开复核结果")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    distinct_at_same_institution = {
        **same,
        "name": "武汉大学另一事件",
        "case_type": "analogous",
    }
    assert independent_case_evidence(
        claim(distinct_at_same_institution),
        {"E001": source("武汉大学另一事件公开复核结果")},
        "武汉大学图书馆事件",
        "2026-01-01",
    ) == {"E001"}
    related = {
        **distinct_at_same_institution,
        "case_type": "related_prior",
        "connection": "影响后续规则",
        "connection_quote": "该事件促成规则修订",
    }
    assert (
        independent_case_evidence(
            claim(related),
            {"E001": source("武汉大学另一事件公开复核结果")},
            "武汉大学图书馆事件",
            "2026-01-01",
        )
        == set()
    )
    assert independent_case_evidence(
        claim(related),
        {"E001": source("武汉大学另一事件：该事件促成规则修订", fetched=True)},
        "武汉大学图书馆事件",
        "2026-01-01",
    ) == {"E001"}
