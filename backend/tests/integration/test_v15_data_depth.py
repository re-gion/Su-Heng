import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from yuqing.render.html import render_html
from yuqing.scripts.import_dataset import load_event_records
from yuqing.scripts.import_weibo_hot_history import parse_weibo_archive
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.historical_data import (
    DatasetAssetInput,
    HistoricalDataService,
    HistoricalEventInput,
    HotSnapshotInput,
)
from yuqing.services.hotlist import DailyHotCollector
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, TaskCreate


@pytest.mark.asyncio
async def test_existing_v1_hot_snapshot_table_is_migrated_in_place(runtime_dir: Path):
    database_path = runtime_dir / "v1-existing.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """CREATE TABLE hot_snapshot(
               id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL,
               captured_at TEXT NOT NULL, rank INTEGER NOT NULL, title TEXT NOT NULL,
               heat_value REAL, url TEXT, raw TEXT
           )"""
    )
    connection.execute(
        "INSERT INTO hot_snapshot(platform,captured_at,rank,title) VALUES('weibo','2026-01-01',1,'旧记录')"
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    await database.initialize()
    columns = await database.fetch_all("PRAGMA table_info(hot_snapshot)")
    old_row = await database.fetch_one("SELECT asset_id,title FROM hot_snapshot WHERE id=1")

    assert "asset_id" in {row["name"] for row in columns}
    assert dict(old_row) == {"asset_id": None, "title": "旧记录"}
    await database.close()


@pytest.mark.asyncio
async def test_reimport_binds_legacy_hot_snapshot_to_registered_asset(runtime_dir: Path):
    database = Database(runtime_dir / "legacy-binding.db")
    await database.initialize()
    history = HistoricalDataService(database)
    point = HotSnapshotInput(platform="weibo", captured_at="2026-01-01", rank=1, title="旧记录")
    await history.record_hot_snapshots([point])
    asset = await history.register_asset(
        DatasetAssetInput(
            slug="archive",
            name="历史归档",
            source_url="https://data.example.com/archive",
            license_label="fixture",
            upstream_rights_note="测试",
            redistribution="restricted",
        ),
        record_count=0,
        metadata={},
    )

    inserted = await history.record_hot_snapshots([point.model_copy(update={"asset_id": asset.id})])
    row = await database.fetch_one("SELECT asset_id FROM hot_snapshot WHERE title='旧记录'")

    assert inserted == 0
    assert row["asset_id"] == asset.id
    await database.close()


@pytest.mark.asyncio
async def test_imported_covered_event_finds_explainable_local_comparisons(runtime_dir: Path):
    database = Database(runtime_dir / "history.db")
    await database.initialize()
    history = HistoricalDataService(database)

    result = await history.import_events(
        DatasetAssetInput(
            slug="fixture-public-events",
            name="公开事件 fixture",
            source_url="https://data.example.com/events",
            license_label="fixture-only",
            upstream_rights_note="测试数据，不用于分发",
            redistribution="restricted",
        ),
        [
            HistoricalEventInput(
                event_name="甲品牌食品召回",
                event_time_start="2024-03-01",
                summary="甲品牌因抽检问题召回食品。",
                outcome="完成召回并公布整改结果",
                nature="食品安全",
                outbreak_path="抽检通报后媒体集中报道",
                response="企业公告召回",
                regulatory_involvement="市场监管部门通报",
                source_url="https://news.example.com/a",
                source_title="甲品牌召回通报",
                source_name="示例新闻",
                keywords=["食品安全", "产品召回", "监管通报"],
                aliases=["甲品牌召回事件"],
            ),
            HistoricalEventInput(
                event_name="乙品牌食品召回",
                event_time_start="2023-06-02",
                summary="乙品牌在监管通报后启动产品召回。",
                outcome="公开整改并完成召回",
                nature="食品安全",
                outbreak_path="监管通报引发平台热议",
                response="企业致歉并召回",
                regulatory_involvement="市场监管部门介入",
                source_url="https://news.example.com/b",
                source_title="乙品牌召回报道",
                source_name="示例新闻",
                keywords=["食品安全", "产品召回", "监管通报"],
            ),
            HistoricalEventInput(
                event_name="丙公司软件发布",
                event_time_start="2023-06-02",
                summary="丙公司发布新软件。",
                outcome="正常上线",
                nature="产品发布",
                source_url="https://news.example.com/c",
                source_title="软件发布报道",
                source_name="示例新闻",
                keywords=["软件", "发布"],
            ),
        ],
    )
    matches = await history.dataset_query("甲品牌召回事件", limit=3)

    assert result.imported == 3
    assert result.asset.record_count == 3
    assert [item.event_name for item in matches] == ["乙品牌食品召回"]
    assert matches[0].provenance == "本地库命中"
    assert {"食品安全", "产品召回", "监管通报"}.issubset(matches[0].matched_terms)
    assert matches[0].dimensions["最终结局"] == "公开整改并完成召回"
    assert await history.dataset_query("！！！") == []
    await database.close()


@pytest.mark.asyncio
async def test_dataset_with_unknown_rights_is_disabled_by_default(runtime_dir: Path):
    database = Database(runtime_dir / "unknown-rights.db")
    await database.initialize()
    history = HistoricalDataService(database)

    with pytest.raises(ValueError, match="数据许可不明"):
        await history.import_events(
            DatasetAssetInput(
                slug="unknown-rights",
                name="许可未核对数据",
                source_url="https://data.example.com/unknown",
                license_label="unknown",
                upstream_rights_note="尚未核对",
                redistribution="unknown",
            ),
            [
                HistoricalEventInput(
                    event_name="不应入库",
                    summary="不应入库",
                    source_url="https://news.example.com/blocked",
                )
            ],
        )

    assert await database.fetch_one("SELECT id FROM historical_event") is None
    await database.close()


@pytest.mark.asyncio
async def test_hotlist_query_returns_only_real_covered_points(runtime_dir: Path):
    database = Database(runtime_dir / "hotlist.db")
    await database.initialize()
    history = HistoricalDataService(database)
    await history.record_hot_snapshots(
        [
            HotSnapshotInput(
                platform="weibo",
                captured_at="2026-08-10T08:00:00+08:00",
                rank=2,
                title="甲品牌食品召回",
                heat_value=810000,
                url="https://s.weibo.com/topic/a",
            ),
            HotSnapshotInput(
                platform="weibo",
                captured_at="2026-08-10T12:00:00+08:00",
                rank=1,
                title="甲品牌食品召回进展",
                heat_value=1200000,
                url="https://s.weibo.com/topic/a2",
            ),
            HotSnapshotInput(
                platform="zhihu",
                captured_at="2026-08-10T12:00:00+08:00",
                rank=1,
                title="无关软件发布",
                heat_value=9999999,
            ),
        ]
    )

    points = await history.hotlist_query(
        "甲品牌食品召回", date_from="2026-08-10", date_to="2026-08-11"
    )

    assert [(item.platform, item.heat_value) for item in points] == [
        ("weibo", 810000),
        ("weibo", 1200000),
    ]
    assert await history.hotlist_query("完全未覆盖事件") == []
    assert await history.hotlist_query("……") == []
    await database.close()


@pytest.mark.asyncio
async def test_hotlist_query_filters_before_applying_match_limit(runtime_dir: Path):
    database = Database(runtime_dir / "hotlist-limit.db")
    await database.initialize()
    history = HistoricalDataService(database)
    items = [
        HotSnapshotInput(
            platform="weibo",
            captured_at=f"2026-08-10T08:{index:02d}:00+08:00",
            rank=index + 1,
            title=f"无关话题 {index}",
        )
        for index in range(60)
    ]
    items.append(
        HotSnapshotInput(
            platform="weibo",
            captured_at="2026-08-10T10:00:00+08:00",
            rank=1,
            title="目标事件进展",
            heat_value=888,
        )
    )
    await history.record_hot_snapshots(items)

    points = await history.hotlist_query("目标事件", limit=50)

    assert [item.title for item in points] == ["目标事件进展"]
    await database.close()


def test_missing_heat_value_never_becomes_a_fake_zero_curve():
    limitations = {"items": []}
    blocks = FullReportBuilder._propagation_blocks(
        [],
        limitations,
        [HotSnapshotInput(platform="weibo", captured_at="2026-08-10", rank=1, title="无热度记录")],
    )

    assert all(block.get("block_id") != "b_04_hot_chart" for block in blocks)
    assert limitations["items"][-1]["id"] == "L04"
    assert any(item["id"] == "L15" for item in limitations["items"])


def test_dataset_loader_whitelists_fields_and_rejects_rows_without_event_name(tmp_path: Path):
    source = tmp_path / "events.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "title": "历史事件甲",
                        "date": "2024-01-02",
                        "description": "公开经过",
                        "result": "已整改",
                        "url": "https://example.com/a",
                        "keywords": ["整改", "监管"],
                        "user_id": "must-not-be-imported",
                        "nickname": "must-not-be-imported",
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"description": "缺少事件名"}, ensure_ascii=False),
            ]
        ),
        encoding="utf-8",
    )

    events, skipped = load_event_records(source)

    assert skipped == 1
    assert len(events) == 1
    assert events[0].event_name == "历史事件甲"
    assert events[0].summary == "公开经过"
    assert "user_id" not in events[0].model_dump()
    assert "nickname" not in events[0].model_dump()


def test_dataset_loader_rejects_record_without_verifiable_source_url(tmp_path: Path):
    source = tmp_path / "events.json"
    source.write_text(
        json.dumps([{"title": "无来源事件", "summary": "不能作为历史证据"}], ensure_ascii=False),
        encoding="utf-8",
    )

    events, skipped = load_event_records(source)

    assert events == []
    assert skipped == 1


def test_weibo_hot_history_archive_parser_keeps_only_public_rank_fields():
    markdown = """# 2024-05-20

共 2 条

最后更新时间：2024-05-20 11:21 PM

1. [监管通报事件](https://m.weibo.cn/search?q=event) `社会` - 1865009
2. [品牌召回进展](https://m.weibo.cn/search?q=recall) `财经` - 12.3万
"""

    points = parse_weibo_archive(markdown, "2024-05-20")

    assert [(item.rank, item.title, item.heat_value) for item in points] == [
        (1, "监管通报事件", 1_865_009),
        (2, "品牌召回进展", 123_000),
    ]
    assert points[0].captured_at == "2024-05-20T23:21:00+08:00"


@pytest.mark.asyncio
async def test_daily_hot_collector_normalizes_common_response_shapes(runtime_dir: Path):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/weibo"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": [
                    {
                        "title": "热榜事件",
                        "hot": "123.4万",
                        "index": 1,
                        "url": "https://x/a",
                        "category": "社会",
                        "uid": "must-not-be-stored",
                        "avatar": "must-not-be-stored",
                    },
                    {"title": "第二条", "desc": "热度 8899", "rank": 2},
                ],
            },
        )

    database = Database(runtime_dir / "collector.db")
    await database.initialize()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://hot.test")
    collector = DailyHotCollector(
        HistoricalDataService(database), ["https://hot.test"], client=client
    )

    result = await collector.collect(["weibo"], captured_at="2026-08-13T10:00:00+08:00")
    points = await HistoricalDataService(database).hotlist_query("热榜事件")

    assert result.inserted == 2
    assert result.platforms == {"weibo": "ok"}
    assert points[0].heat_value == 1_234_000
    stored = await database.fetch_one("SELECT raw FROM hot_snapshot WHERE title='热榜事件'")
    assert json.loads(stored["raw"]) == {"category": "社会"}
    asset = await database.fetch_one(
        "SELECT id,source_url,record_count FROM dataset_asset WHERE slug LIKE 'dailyhot-weibo-%'"
    )
    bound = await database.fetch_one("SELECT asset_id FROM hot_snapshot WHERE title='热榜事件'")
    assert asset["source_url"] == "https://hot.test/weibo"
    assert asset["record_count"] == 2
    assert bound["asset_id"] == asset["id"]
    await collector.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_local_history_and_hotlist_are_bound_into_full_report(
    runtime_dir: Path, claim_limits: dict[str, int]
):
    database = Database(runtime_dir / "v15-report.db")
    await database.initialize()
    history = HistoricalDataService(database)
    await history.import_events(
        DatasetAssetInput(
            slug="report-fixture",
            name="报告历史库",
            source_url="https://data.example.com/report-events",
            license_label="fixture-only",
            upstream_rights_note="测试数据",
            redistribution="restricted",
        ),
        [
            HistoricalEventInput(
                event_name="甲品牌食品召回",
                summary="甲品牌食品召回。",
                nature="食品安全",
                source_url="https://news.example.com/anchor",
                keywords=["食品安全", "产品召回"],
            ),
            HistoricalEventInput(
                event_name="乙品牌食品召回",
                event_time_start="2023-01-02",
                summary="乙品牌在通报后召回产品。",
                outcome="完成召回",
                nature="食品安全",
                outbreak_path="监管通报",
                response="公开召回",
                regulatory_involvement="已介入",
                source_url="https://news.example.com/history",
                source_title="乙品牌历史报道",
                source_name="示例新闻",
                keywords=["食品安全", "产品召回"],
            ),
        ],
    )
    await history.record_hot_snapshots(
        [
            HotSnapshotInput(
                platform="weibo",
                captured_at="2026-08-10T08:00:00+08:00",
                rank=2,
                title="甲品牌食品召回",
                heat_value=810000,
            ),
            HotSnapshotInput(
                platform="weibo",
                captured_at="2026-08-10T12:00:00+08:00",
                rank=1,
                title="甲品牌食品召回",
                heat_value=1200000,
            ),
        ]
    )
    task = await database.create_task(TaskCreate(event_query="甲品牌食品召回"))
    prepared = await history.prepare_task_context(task.id, task.event_query)
    local_evidence = (await database.list_evidence(task.id))[0]
    await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="乙品牌在通报后召回产品。",
            agent="history_insight",
            section="history",
            evidence_ids=[local_evidence.local_id],
        ),
        **claim_limits,
    )

    _, report, _ = await FullReportBuilder(
        database, runtime_dir / "reports", historical_data=history
    ).build(task.id)

    history_block = next(block for block in report["blocks"] if block["type"] == "history_compare")
    hot_chart = next(
        block for block in report["blocks"] if block.get("block_id") == "b_04_hot_chart"
    )
    assert "本地历史库命中" in prepared
    assert history_block["cards"][0]["event_name"] == "乙品牌食品召回"
    assert history_block["cards"][0]["provenance"] == "本地库命中"
    assert history_block["cards"][0]["dimensions"]["最终结局"] == "完成召回"
    assert history_block["cards"][0]["evidence_refs"] == [local_evidence.local_id]
    assert hot_chart["data_basis"] == "hot_snapshot_database"
    assert [item["value"] for item in hot_chart["items"]] == [810000, 1200000]
    assert "<polyline" in render_html(report, view="full")
    await database.close()
