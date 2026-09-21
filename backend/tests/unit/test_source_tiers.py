from pathlib import Path

import pytest

from yuqing.services.source_tiers import (
    SourceTierClassifier,
    TierConfig,
    TierGroup,
    bundled_classifier,
    registrable_domain,
    resolve_proxy_host,
)

YAML = Path(__file__).parents[2] / "yuqing" / "services" / "source_tiers.yaml"


def bundled() -> SourceTierClassifier:
    return SourceTierClassifier.from_yaml(YAML)


def test_source_tier_table_distinguishes_authority_media_portal_and_unknown():
    classifier = bundled()

    assert classifier.classify("www.samr.gov.cn") == (1, "authority", True)
    assert classifier.classify("news.xinhuanet.com") == (2, "independent", True)
    assert classifier.classify("news.cctv.com") == (2, "independent", True)
    assert classifier.classify("news.cnr.cn") == (2, "independent", True)
    assert classifier.classify("finance.sina.com.cn") == (3, "syndicated", True)
    assert classifier.classify("unlisted.example") == (4, "unknown", False)


@pytest.mark.parametrize(
    "host",
    [
        "hb.news.cn",
        "m.gmw.cn",
        "www.shobserver.com",
        "www.chinadaily.com.cn",
        "www.thepaper.cn",
        "news.qq.com",
        "www.sohu.com",
        "www.toutiao.com",
    ],
)
def test_real_outlets_and_portals_are_no_longer_unidentified(host):
    tier, role, matched = bundled().classify(host)

    assert matched is True
    assert tier != 4


def test_republication_platforms_are_syndicated_not_independent():
    classifier = bundled()

    # 转载/聚合平台不能算独立采编主体，否则三家门户转载同一篇通讯稿就成了"多源互证"。
    for host in ("news.qq.com", "finance.sina.com.cn", "www.sohu.com", "www.toutiao.com"):
        assert classifier.classify(host)[1] == "syndicated"


@pytest.mark.parametrize("host", ["news.cn", "cctv.com", "people.com.cn", "gmw.cn"])
def test_mainstream_media_are_never_authority(host):
    # 01 §5.5：信源等级 ≠ 裁判资格。一旦媒体被标成 tier1/authority，
    # 单条媒体来源就能命中 D8「单源·官方」判绿。
    assert bundled().classify(host)[:2] != (1, "authority")


def test_deny_short_circuits_before_allow():
    classifier = SourceTierClassifier(
        TierConfig(
            deny=[TierGroup(tier=3, role="syndicated", domains=["example.com"])],
            groups=[TierGroup(tier=2, role="independent", domains=["example.com"])],
        )
    )

    assert classifier.classify("news.example.com") == (3, "syndicated", True)


def test_same_outlet_sites_collapse_to_one_publisher_entity():
    classifier = bundled()

    assert classifier.canonical_publisher("www.chinadaily.com.cn") == "中国日报"
    assert classifier.canonical_publisher("www.chinadailyasia.com") == "中国日报"
    assert classifier.canonical_publisher("hb.news.cn") == "新华社"
    assert classifier.canonical_publisher("www.xinhuanet.com") == "新华社"


def test_subdomains_without_an_entity_entry_fall_back_to_the_registrable_domain():
    classifier = bundled()

    # 同一站点的多个子域必须归并为同一个发布主体，否则独立信源数会虚高。
    assert classifier.canonical_publisher("c.m.163.com") == "网易"
    assert classifier.canonical_publisher("m.163.com") == "网易"
    assert registrable_domain("en.m.wikipedia.org") == "wikipedia.org"
    assert registrable_domain("bbc.co.uk") == "bbc.co.uk"


def test_source_name_wins_unless_it_is_actually_a_domain():
    classifier = bundled()

    assert classifier.canonical_publisher("x.example", "主管部门") == "主管部门"
    # 有些 provider 把域名塞进 source_name，那不是采编主体名，仍要走归并。
    assert classifier.canonical_publisher("x.example", "news.sina.com.cn") == "新浪"


def test_institution_proxy_hosts_resolve_to_the_real_publisher():
    assert resolve_proxy_host("www-scmp-com.libproxy1.nus.edu.sg") == "scmp.com"
    # 无法可靠还原时保守放弃，绝不猜一个主体出来。
    assert resolve_proxy_host("proxy.example.com") is None
    assert resolve_proxy_host("api.proxy-service.com") is None


def test_bundled_classifier_is_shared_across_reads():
    assert bundled_classifier() is bundled_classifier()


def test_every_known_publisher_entity_also_has_a_countable_role():
    """实体表与角色表必须一致。

    只在 entities 里登记、却没进 groups 的域名会落 default_role=unknown，
    而 unknown 在 merge_stances 里被整体丢弃——于是"已识别主体"反而拿不到计数，
    正是 verified_rate 恒为 0 的那类静默失败。
    """
    classifier = bundled()

    mismatched = [
        f"{group.name}:{domain}"
        for group in classifier.config.entities
        for domain in group.domains
        if classifier.classify(domain)[1] == "unknown"
    ]

    assert mismatched == []
