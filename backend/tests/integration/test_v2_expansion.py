import json
import sqlite3
from pathlib import Path

import pytest

from yuqing.render.html import render_html
from yuqing.services.comment_plugin import (
    CollectedPost,
    CommentCandidateInput,
    CommentPluginService,
    PlaywrightCommentCollector,
    adapter_for_url,
)
from yuqing.services.full_report import FullReportBuilder
from yuqing.services.report_builder import BriefReportBuilder
from yuqing.storage.db import Database
from yuqing.storage.models import ClaimCreate, EvidenceCreate, TaskCreate


def test_all_seven_social_platform_urls_have_explicit_adapters():
    cases = {
        "weibo": "https://weibo.com/123456/AbCdEf",
        "bilibili": "https://www.bilibili.com/video/BV1xx411c7mD",
        "zhihu": "https://www.zhihu.com/question/123456789",
        "xiaohongshu": "https://www.xiaohongshu.com/explore/64abcdef1234567890abcdef",
        "douyin": "https://www.douyin.com/video/7312345678901234567",
        "kuaishou": "https://www.kuaishou.com/short-video/3xabcdefghi",
        "tieba": "https://tieba.baidu.com/p/9123456789",
    }

    assert {name: adapter_for_url(url).platform for name, url in cases.items()} == {
        name: name for name in cases
    }


@pytest.mark.parametrize(
    "platform", ["weibo", "bilibili", "zhihu", "xiaohongshu", "douyin", "kuaishou", "tieba"]
)
def test_each_platform_fixture_extracts_sanitized_root_and_reply(platform: str):
    fixture = Path(__file__).parents[1] / "fixtures" / "comments" / f"{platform}.json"
    adapter = next(
        item
        for item in CommentPluginService.__new__(CommentPluginService).adapters
        if item.platform == platform
    )

    comments = adapter.extract_comments(json.loads(fixture.read_text(encoding="utf-8")), limit=10)

    assert len(comments) >= 2
    assert all(item.text for item in comments)
    assert "removed" not in str([item.model_dump() for item in comments])


def test_adapter_limits_each_root_to_three_replies():
    adapter = adapter_for_url("https://weibo.com/123456/AbCdEf")
    payload = {
        "comments": [
            {
                "id": "root",
                "content": "根评论",
                "replies": [
                    {"id": f"reply-{index}", "content": f"回复 {index}"} for index in range(6)
                ],
            }
        ]
    }

    comments = adapter.extract_comments(payload, limit=20)

    assert [item.native_id for item in comments] == [
        "root",
        "reply-0",
        "reply-1",
        "reply-2",
    ]


def test_collector_limits_same_root_to_three_replies_across_payload_batches():
    target = []
    seen: set[str] = set()
    roots: dict[str, str] = {}
    replies_per_root: dict[str, int] = {}
    adapter = adapter_for_url("https://weibo.com/123456/AbCdEf")

    for index in range(6):
        batch = adapter.extract_comments(
            {
                "comments": [
                    {
                        "id": "root",
                        "content": "根评论",
                        "replies": [{"id": f"r{index}", "content": f"回复 {index}"}],
                    }
                ]
            },
            limit=20,
        )
        PlaywrightCommentCollector.merge_comment_batch(
            target,
            batch,
            seen=seen,
            roots=roots,
            replies_per_root=replies_per_root,
            limit=20,
        )

    assert [item.native_id for item in target] == ["root", "r0", "r1", "r2"]


@pytest.mark.asyncio
async def test_quick_selection_rejects_more_than_two_posts_atomically(runtime_dir: Path):
    database = Database(runtime_dir / "selection-budget.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="候选预算", depth="quick", comment_mode="smart")
    )
    service = CommentPluginService(database, runtime_dir / "plugin", enabled=True)
    candidates = await service.discover_candidates(
        task,
        [
            CommentCandidateInput(url="https://weibo.com/123456/A1", title="一"),
            CommentCandidateInput(url="https://www.bilibili.com/video/BV1xx411c7mD", title="二"),
            CommentCandidateInput(url="https://tieba.baidu.com/p/9123456789", title="三"),
        ],
    )

    with pytest.raises(ValueError, match="quick 模式最多选择 2 帖"):
        await service.claim_selection(
            task.id,
            [item.id for item in candidates],
            next_phase="comment_collection",
        )

    saved_task = await database.get_task(task.id)
    assert saved_task.status == "queued"
    assert not await database.fetch_all(
        "SELECT * FROM comment_collection WHERE task_id=?", (task.id,)
    )
    await database.close()


def test_bilibili_adapter_understands_native_rpid_and_content_message_shape():
    adapter = adapter_for_url("https://www.bilibili.com/video/BV1xx411c7mD")

    comments = adapter.extract_comments(
        {
            "data": {
                "replies": [
                    {
                        "rpid": 987,
                        "ctime": 1720000000,
                        "like": 18,
                        "rcount": 1,
                        "content": {"message": "真实 B 站字段形态"},
                    }
                ]
            }
        },
        limit=10,
    )

    assert comments[0].native_id == "987"
    assert comments[0].text == "真实 B 站字段形态"
    assert comments[0].like_count == 18


@pytest.mark.asyncio
async def test_v15_database_is_migrated_for_v2_without_losing_existing_evidence(
    runtime_dir: Path,
):
    database_path = runtime_dir / "v15.db"
    connection = sqlite3.connect(database_path)
    schema = (Path(__file__).parents[2] / "yuqing" / "storage" / "schema.sql").read_text(
        encoding="utf-8"
    )
    legacy_schema = schema.replace("  kind TEXT NOT NULL DEFAULT 'web',\n", "").replace(
        "ON evidence(task_id, url_hash, kind);",
        "ON evidence(task_id, url_hash);",
    )
    connection.executescript(legacy_schema)
    connection.execute(
        "INSERT INTO task(id,event_query,depth,status,phase,created_at,updated_at) "
        "VALUES('t_old','旧任务','quick','done','finished','2026-01-01','2026-01-01')"
    )
    connection.execute(
        """INSERT INTO evidence(
          pk,task_id,local_id,url,url_hash,title,source_domain,source_role,source_tier,
          discovered_at,fetch_status,snippet
        ) VALUES('e_old','t_old','E001','https://example.com/a','hash','旧证据',
          'example.com','unknown',4,'2026-01-01','discovered','旧摘要')"""
    )
    connection.commit()
    connection.close()

    database = Database(database_path)
    await database.initialize()

    row = await database.fetch_one("SELECT kind,title FROM evidence WHERE pk='e_old'")
    tables = {
        item["name"]
        for item in await database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'social_%'"
        )
    }
    assert dict(row) == {"kind": "web", "title": "旧证据"}
    assert tables == {"social_candidate", "social_comment"}
    assert await database.fetch_one(
        "SELECT name FROM sqlite_master WHERE name='comment_collection'"
    )
    await database.close()


@pytest.mark.asyncio
async def test_same_url_can_hold_web_and_comment_sample_evidence(runtime_dir: Path):
    database = Database(runtime_dir / "evidence-kinds.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="同址证据"))
    common = dict(
        task_id=task.id,
        url="https://weibo.com/123456/AbCdEf",
        title="同一帖子",
        snippet="摘要",
    )

    web = await database.add_evidence(EvidenceCreate(**common, kind="web"))
    comments = await database.add_evidence(
        EvidenceCreate(**common, kind="social_comments", provider="comment_plugin")
    )

    assert web.local_id == "E001"
    assert comments.local_id == "E002"
    assert [item.kind for item in await database.list_evidence(task.id)] == [
        "web",
        "social_comments",
    ]
    await database.close()


@pytest.mark.asyncio
async def test_candidate_scoring_is_bounded_explainable_and_keeps_manual_urls(runtime_dir: Path):
    database = Database(runtime_dir / "candidates.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="国际品牌召回",
            comment_mode="hybrid",
            comment_urls=["https://www.zhihu.com/question/123456789"],
            source_scope="global",
            source_languages=["zh", "en"],
        )
    )
    service = CommentPluginService(database, runtime_dir / "comment-plugin", enabled=False)

    candidates = await service.discover_candidates(
        task,
        [
            CommentCandidateInput(
                url="https://weibo.com/123456/AbCdEf",
                title="国际品牌召回引发争议",
                snippet="评论热议召回范围",
                engagement=8800,
                published_at="2026-08-13T09:00:00+08:00",
            )
        ],
    )

    assert len(candidates) == 2
    assert candidates[0].score <= 100
    assert set(candidates[0].score_breakdown) == {
        "relevance",
        "engagement",
        "recency",
        "controversy",
        "information_gain",
        "diversity",
    }
    manual = next(item for item in candidates if item.selection_mode == "manual")
    assert manual.platform == "zhihu"
    assert "用户指定" in manual.reasons
    assert "与调查主题相关性不足，建议人工复核" in manual.reasons
    await database.close()


@pytest.mark.asyncio
async def test_approved_collection_persists_only_sanitized_comments_and_evidence(runtime_dir: Path):
    class FixtureCollector:
        async def collect(self, candidate, *, limit, stop_requested):
            assert limit == 100
            assert stop_requested() is False
            adapter = adapter_for_url(candidate.url)
            comments = adapter.extract_comments(
                {
                    "comments": [
                        {
                            "id": "native-1",
                            "content": "第一条可引用评论，联系 13812345678 或 test@example.com，感谢@真实昵称发帖",
                            "like_count": 12,
                            "user": {"id": "secret-user", "nickname": "不得落库"},
                            "replies": [
                                {
                                    "id": "native-2",
                                    "content": "楼中楼回复",
                                    "like_count": 3,
                                    "user": {"id": "secret-user-2"},
                                }
                            ],
                        }
                    ]
                },
                limit=limit,
            )
            return CollectedPost(comments=comments, sampling_method="fixture top/latest")

    database = Database(runtime_dir / "collection.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="评论采集", depth="quick", comment_mode="manual")
    )
    service = CommentPluginService(
        database,
        runtime_dir / "comment-plugin",
        enabled=True,
        collector=FixtureCollector(),
    )
    candidates = await service.discover_candidates(
        task,
        [
            CommentCandidateInput(
                url="https://www.bilibili.com/video/BV1xx411c7mD",
                title="评论采集样例",
            )
        ],
    )

    result = await service.collect_selected(task, [candidates[0].id])

    assert result.completed == 1
    comments = await database.fetch_all(
        "SELECT id,parent_id,text,like_count FROM social_comment ORDER BY depth,id"
    )
    assert len(comments) == 2
    assert comments[0]["id"] != "native-1"
    serialized = str([dict(item) for item in comments])
    assert "secret-user" not in serialized
    assert "不得落库" not in serialized
    assert "13812345678" not in serialized
    assert "test@example.com" not in serialized
    assert "@真实昵称" not in serialized
    assert "感谢@[账号提及已脱敏]" in serialized
    assert "[手机号已脱敏]" in serialized
    evidence = (await database.list_evidence(task.id))[0]
    assert evidence.kind == "social_comments"
    assert evidence.fetch_status == "fetched"
    assert "第一条可引用评论" in (evidence.content_text or "")
    await database.close()


@pytest.mark.asyncio
async def test_same_native_comment_id_in_two_posts_does_not_replace_previous_row(
    runtime_dir: Path,
):
    class DuplicateNativeIdCollector:
        async def collect(self, candidate, *, limit, stop_requested):
            return CollectedPost(
                comments=[
                    adapter_for_url(candidate.url).extract_comments(
                        {"comments": [{"id": "1", "content": candidate.title}]}, limit=limit
                    )[0]
                ],
                sampling_method="fixture",
            )

    database = Database(runtime_dir / "comment-id-scope.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="跨帖评论 ID", depth="quick", comment_mode="manual")
    )
    service = CommentPluginService(
        database,
        runtime_dir / "plugin",
        enabled=True,
        collector=DuplicateNativeIdCollector(),
    )
    candidates = await service.discover_candidates(
        task,
        [
            CommentCandidateInput(url="https://weibo.com/123456/AbCdEf", title="帖子一"),
            CommentCandidateInput(
                url="https://www.bilibili.com/video/BV1xx411c7mD", title="帖子二"
            ),
        ],
    )

    result = await service.collect_selected(task, [item.id for item in candidates])
    rows = await database.fetch_all(
        "SELECT id,text,source_url FROM social_comment WHERE task_id=? ORDER BY source_url",
        (task.id,),
    )

    assert result.completed == 2
    assert len(rows) == 2
    assert len({row["id"] for row in rows}) == 2
    assert {row["text"] for row in rows} == {"帖子一", "帖子二"}
    await database.close()


@pytest.mark.asyncio
async def test_stop_is_persisted_and_partial_collection_cannot_be_marked_completed(
    runtime_dir: Path,
):
    service_ref = None

    class StopDuringCollection:
        async def collect(self, candidate, *, limit, stop_requested):
            await service_ref.stop(candidate.task_id)
            assert stop_requested() is True
            return CollectedPost(
                comments=adapter_for_url(candidate.url).extract_comments(
                    {"comments": [{"id": "partial", "content": "停止前已取得"}]},
                    limit=limit,
                ),
                sampling_method="停止前的部分样本",
            )

    database = Database(runtime_dir / "comment-stop.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="停止评论采集", depth="quick", comment_mode="manual")
    )
    service_ref = CommentPluginService(
        database, runtime_dir / "plugin", enabled=True, collector=StopDuringCollection()
    )
    candidates = await service_ref.discover_candidates(
        task,
        [
            CommentCandidateInput(url="https://weibo.com/123456/AbCdEf", title="帖子一"),
            CommentCandidateInput(
                url="https://www.bilibili.com/video/BV1xx411c7mD", title="帖子二"
            ),
        ],
    )

    await service_ref.mark_selection(task.id, [item.id for item in candidates])
    await service_ref.collect_selected(task, [item.id for item in candidates])

    collections = await database.fetch_all(
        "SELECT status FROM comment_collection WHERE task_id=? ORDER BY candidate_id",
        (task.id,),
    )
    statuses = [row["status"] for row in collections]
    assert statuses and set(statuses) == {"stopped"}
    candidate_statuses = {
        row["status"]
        for row in await database.fetch_all(
            "SELECT status FROM social_candidate WHERE task_id=?", (task.id,)
        )
    }
    assert candidate_statuses <= {"approved", "collected"}
    await database.close()


@pytest.mark.asyncio
async def test_v2_report_keeps_foreign_original_separate_from_machine_translation(
    runtime_dir: Path,
):
    class Translator:
        async def translate_to_chinese(self, text: str, source_lang: str) -> str:
            assert source_lang == "en"
            return "CrowdStrike 表示已部署修复。"

    database = Database(runtime_dir / "report-v2.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(
            event_query="2024 CrowdStrike global outage",
            source_scope="global",
            source_languages=["zh", "en"],
        )
    )
    unrelated = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.com/unrelated",
            title="与事件无关但带日期的搜索噪声",
            snippet="unrelated",
            published_at="2024-03-13T00:00:00+00:00",
            lang="en",
        )
    )
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.com/crowdstrike-update",
            title="CrowdStrike update",
            snippet="CrowdStrike says a fix has been deployed.",
            content_text="CrowdStrike says a fix has been deployed.",
            fetch_status="fetched",
            fetched_at="2026-08-13T10:00:00+08:00",
            snapshot_path=str(runtime_dir / "source.html"),
            content_sha256="a" * 64,
            published_at="2024-07-19T00:00:00+00:00",
            lang="en",
        )
    )
    await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="CrowdStrike says a fix has been deployed.",
            agent="fact_investigator",
            evidence_ids=[evidence.local_id],
        )
    )

    _, report, _ = await FullReportBuilder(
        database, runtime_dir / "reports", translator=Translator()
    ).build(task.id)
    appendix = next(block for block in report["blocks"] if block["type"] == "evidence_appendix")
    card = next(item for item in appendix["items"] if item["evidence_ref"] == evidence.local_id)
    timeline = next(block for block in report["blocks"] if block["type"] == "timeline")
    html = render_html(report, view="full")

    assert report["schema_version"] == "0.3"
    assert report["language_coverage"]["complete"] == ["en"]
    assert report["language_coverage"]["missing"] == ["zh"]
    assert card["original_excerpt"] == "CrowdStrike says a fix has been deployed."
    assert card["machine_translation_zh"] == "CrowdStrike 表示已部署修复。"
    assert "机器翻译（中文，仅供阅读，不参与逐字核验）" in html
    assert "CrowdStrike says a fix has been deployed." in html
    assert timeline["nodes"] == [
        {
            "date": "2024-07-19T00:00:00+00:00",
            "text": "CrowdStrike update",
            "evidence_refs": [evidence.local_id],
        }
    ]
    assert unrelated.local_id not in str(timeline)
    await database.close()


@pytest.mark.asyncio
async def test_report_rejects_untraceable_paraphrase_instead_of_failing_whole_report(
    runtime_dir: Path,
):
    database = Database(runtime_dir / "report-rejection.db")
    await database.initialize()
    task = await database.create_task(TaskCreate(event_query="引用失败降级"))
    evidence = await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.com/source",
            title="已抓取来源",
            content_text="原文并未提到目标陈述。",
            fetch_status="fetched",
            fetched_at="2026-08-13T10:00:00+08:00",
            snapshot_path=str(runtime_dir / "source.html"),
            content_sha256="b" * 64,
        )
    )
    await database.add_claim(
        ClaimCreate(
            task_id=task.id,
            text="目标陈述成立。",
            agent="fact_investigator",
            evidence_ids=[evidence.local_id],
        )
    )

    _, report, _ = await BriefReportBuilder(database, runtime_dir / "reports").build(task.id)

    facts = next(block for block in report["blocks"] if block["type"] == "fact_check_table")
    limitations = next(block for block in report["blocks"] if block["type"] == "limitations")
    assert facts["items"] == []
    assert report["metrics"]["key_claims_rejected"] == 1
    assert report["metrics"]["rejection_reasons"] == {"引用未通过回溯核验": 1}
    assert any("转述引用未通过原文回溯" in item["text"] for item in limitations["items"])
    await database.close()


@pytest.mark.asyncio
async def test_language_coverage_marks_requested_language_missing_without_evidence(
    runtime_dir: Path,
):
    database = Database(runtime_dir / "language-missing.db")
    await database.initialize()
    task = await database.create_task(
        TaskCreate(event_query="中英覆盖", source_languages=["zh", "en"])
    )
    await database.add_evidence(
        EvidenceCreate(
            task_id=task.id,
            url="https://example.cn/zh",
            title="仅中文来源",
            snippet="只有中文材料。",
            lang="zh",
        )
    )

    _, report, _ = await FullReportBuilder(database, runtime_dir / "reports").build(task.id)

    assert report["language_coverage"]["complete"] == ["zh"]
    assert report["language_coverage"]["missing"] == ["en"]
    await database.close()
