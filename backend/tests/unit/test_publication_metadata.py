import pytest

from yuqing.services.evidence_store import extract_page_published_at


def test_explicit_article_header_date_wins_over_later_index_metadata():
    html = '<meta property="article:published_time" content="2026-10-16T09:08:00">'
    content = "首页\n新闻发布\n部门联合核查公告\n发布时间：2026-10-15 22:04\n来源：办公厅、官方账号\n公告正文"

    assert extract_page_published_at(html, content) == ("2026-10-15T22:04:00", "page_visible")


@pytest.mark.parametrize(
    "metadata, expected, provenance",
    [
        (
            '<span id="pubtime_baidu">2025-09-20 09:53:21</span>',
            "2025-09-20T09:53:21",
            "page_metadata",
        ),
        (
            '<div class="article-header"><h1>调查通报</h1>'
            '<span class="timer">2024-07-22 20:12</span></div>',
            "2024-07-22T20:12:00",
            "page_visible",
        ),
        (
            '<h1>调查通报</h1><div class="content_left_time">'
            "2025年09月20日 09:53 来源：<a href='/'>新闻机构</a></div>",
            "2025-09-20T09:53:00",
            "page_visible",
        ),
    ],
)
@pytest.mark.parametrize("prefix_tag", ["nav", "div"])
def test_article_date_after_long_navigation_is_not_lost(metadata, expected, provenance, prefix_tag):
    page = (
        f"<html><head></head><body><{prefix_tag}>"
        + "页面布局" * 12000
        + f"</{prefix_tag}>"
        + metadata
    )
    assert extract_page_published_at(page, "机构在2025年9月17日作出决定。") == (
        expected,
        provenance,
    )


@pytest.mark.parametrize("container", ["nav", "aside", "footer"])
def test_navigation_and_sidebar_dates_do_not_become_article_dates(container):
    page = (
        f"<{container}><span class='timer'>2026-11-15 09:00</span>"
        f"<time datetime='2026-11-15'>其他新闻</time></{container}>"
        "<h1>调查通报</h1><article>2025年9月17日，机构作出决定。</article>"
    )
    assert extract_page_published_at(page, "2025年9月17日，机构作出决定。") == (None, None)


def test_labeled_event_date_in_article_prose_is_not_publication_metadata():
    assert extract_page_published_at(
        "<article>此次事件发生时间：2023年7月11日。</article>",
        "此次事件发生时间：2023年7月11日。",
    ) == (None, None)


@pytest.mark.parametrize(
    "unrelated",
    [
        '<article><time datetime="2023-07-11">事件发生日期</time></article>',
        '<script type="application/ld+json">{"@type":"ItemList","itemListElement":'
        '[{"@type":"NewsArticle","datePublished":"2023-07-11"}]}</script>',
        '<script type="application/ld+json">{"@type":"Event",'
        '"datePublished":"2023-07-11"}</script>',
        '<article>{"datePublished":"2023-07-11"}</article>',
        '<script type="application/ld+json">{"@type":"NewsArticle",'
        '"url":"https://news.example/other", "datePublished":"2023-07-11"}</script>',
    ],
)
def test_event_times_and_related_article_json_do_not_override_publication_node(unrelated):
    page = unrelated + '<span id="pubtime_baidu">2025-09-20 09:53:21</span>'
    assert extract_page_published_at(page, "事件经过", "https://news.example/current") == (
        "2025-09-20T09:53:21",
        "page_metadata",
    )
    assert extract_page_published_at(unrelated, "事件经过", "https://news.example/current") == (
        None,
        None,
    )


def test_matching_article_json_and_explicit_publication_time_remain_supported():
    page = (
        '<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle",'
        '"url":"https://news.example/current", "datePublished":"2025-09-20T09:53:21"}]}</script>'
    )
    assert extract_page_published_at(page, "正文", "https://news.example/current") == (
        "2025-09-20T09:53:21",
        "page_metadata",
    )
    assert extract_page_published_at(
        '<time datetime="2025-09-20T09:53:21" itemprop="datePublished">发布日期</time>',
        "正文",
    ) == ("2025-09-20T09:53:21", "page_metadata")
