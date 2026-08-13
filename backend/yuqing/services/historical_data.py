from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl

from yuqing.storage.db import Database, now_iso
from yuqing.storage.models import EvidenceCreate


class DatasetAssetInput(BaseModel):
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=200)
    source_url: HttpUrl
    license_label: str = Field(min_length=1, max_length=100)
    upstream_rights_note: str = Field(min_length=1, max_length=500)
    redistribution: Literal["allowed", "restricted", "unknown"] = "unknown"
    content_sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HistoricalEventInput(BaseModel):
    event_name: str = Field(min_length=1, max_length=300)
    event_time_start: str | None = None
    event_time_end: str | None = None
    summary: str = Field(min_length=1, max_length=3000)
    outcome: str | None = Field(default=None, max_length=1000)
    nature: str | None = Field(default=None, max_length=200)
    outbreak_path: str | None = Field(default=None, max_length=1000)
    response: str | None = Field(default=None, max_length=1000)
    regulatory_involvement: str | None = Field(default=None, max_length=1000)
    source_url: HttpUrl
    source_title: str | None = Field(default=None, max_length=500)
    source_name: str | None = Field(default=None, max_length=200)
    source_published_at: str | None = None
    keywords: list[str] = Field(default_factory=list, max_length=30)
    aliases: list[str] = Field(default_factory=list, max_length=30)


class HotSnapshotInput(BaseModel):
    asset_id: str | None = None
    platform: str = Field(min_length=1, max_length=50)
    captured_at: str
    rank: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=500)
    heat_value: float | None = Field(default=None, ge=0)
    url: str | None = None
    raw: dict[str, Any] | None = None


class DatasetAssetRecord(BaseModel):
    id: str
    slug: str
    name: str
    source_url: str
    license_label: str
    upstream_rights_note: str
    redistribution: str
    personal_fields_removed: bool
    content_sha256: str | None
    record_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    imported_at: str
    updated_at: str


class HistoricalMatch(BaseModel):
    event_id: str
    event_name: str
    event_time_start: str | None
    summary: str
    source_url: str
    source_title: str | None
    source_name: str | None
    source_published_at: str | None
    score: float
    matched_terms: set[str]
    dimensions: dict[str, str]
    provenance: Literal["本地库命中"] = "本地库命中"


class TaskHistoricalMatch(HistoricalMatch):
    evidence_id: str


class HotSnapshotPoint(BaseModel):
    asset_id: str | None = None
    platform: str
    captured_at: str
    rank: int
    title: str
    heat_value: float | None
    url: str | None


@dataclass(frozen=True)
class DatasetImportResult:
    asset: DatasetAssetRecord
    imported: int
    skipped: int


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.lower())


def _terms(*values: str | None) -> set[str]:
    result: set[str] = set()
    for value in values:
        if not value:
            continue
        for part in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", value.lower()):
            if len(part) <= 6:
                result.add(part)
            if re.search(r"[\u4e00-\u9fff]", part):
                result.update(part[index : index + 2] for index in range(len(part) - 1))
    return {item for item in result if item}


class HistoricalDataService:
    """V1.5 本地历史层：只存字段白名单，不接收账号、昵称等个人字段。"""

    def __init__(self, database: Database):
        self.database = database

    async def register_asset(
        self, asset: DatasetAssetInput, *, record_count: int, metadata: dict[str, Any]
    ) -> DatasetAssetRecord:
        asset_id = f"ds_{hashlib.sha256(asset.slug.encode()).hexdigest()[:16]}"
        stamp = now_iso()
        await self.database.execute_write(
            """INSERT INTO dataset_asset(
                   id,slug,name,source_url,license_label,upstream_rights_note,redistribution,
                   personal_fields_removed,content_sha256,record_count,metadata,imported_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                 name=excluded.name,source_url=excluded.source_url,
                 license_label=excluded.license_label,
                 upstream_rights_note=excluded.upstream_rights_note,
                 redistribution=excluded.redistribution,personal_fields_removed=1,
                 content_sha256=excluded.content_sha256,record_count=excluded.record_count,
                 metadata=excluded.metadata,updated_at=excluded.updated_at""",
            (
                asset_id,
                asset.slug,
                asset.name,
                str(asset.source_url),
                asset.license_label,
                asset.upstream_rights_note,
                asset.redistribution,
                asset.content_sha256,
                record_count,
                json.dumps(metadata, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        row = await self.database.fetch_one("SELECT * FROM dataset_asset WHERE id=?", (asset_id,))
        assert row is not None
        value = dict(row)
        value["metadata"] = json.loads(value.get("metadata") or "{}")
        value["personal_fields_removed"] = bool(value["personal_fields_removed"])
        return DatasetAssetRecord.model_validate(value)

    async def import_events(
        self, asset: DatasetAssetInput, events: list[HistoricalEventInput]
    ) -> DatasetImportResult:
        if asset.redistribution == "unknown":
            raise ValueError("数据许可不明，禁止导入事件内容；请先完成数据资产权利核对")
        asset_id = f"ds_{hashlib.sha256(asset.slug.encode()).hexdigest()[:16]}"

        def operation(connection):
            stamp = now_iso()
            connection.execute(
                """INSERT INTO dataset_asset(
                       id,slug,name,source_url,license_label,upstream_rights_note,redistribution,
                       personal_fields_removed,content_sha256,record_count,metadata,imported_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,1,?,0,?,?,?)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name,source_url=excluded.source_url,
                     license_label=excluded.license_label,
                     upstream_rights_note=excluded.upstream_rights_note,
                     redistribution=excluded.redistribution,
                     personal_fields_removed=1,content_sha256=excluded.content_sha256,
                     metadata=excluded.metadata,updated_at=excluded.updated_at""",
                (
                    asset_id,
                    asset.slug,
                    asset.name,
                    str(asset.source_url),
                    asset.license_label,
                    asset.upstream_rights_note,
                    asset.redistribution,
                    asset.content_sha256,
                    json.dumps(asset.metadata, ensure_ascii=False),
                    stamp,
                    stamp,
                ),
            )
            imported = 0
            skipped = 0
            for item in events:
                source_url = str(item.source_url)
                identity = "|".join(
                    (asset.slug, item.event_name, item.event_time_start or "", source_url)
                )
                event_id = f"he_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex[:20]}"
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO historical_event(
                           id,asset_id,event_name,event_time_start,event_time_end,summary,outcome,
                           nature,outbreak_path,response,regulatory_involvement,source_url,
                           source_title,source_name,source_published_at,keywords,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        asset_id,
                        item.event_name.strip(),
                        item.event_time_start,
                        item.event_time_end,
                        item.summary.strip(),
                        item.outcome,
                        item.nature,
                        item.outbreak_path,
                        item.response,
                        item.regulatory_involvement,
                        source_url,
                        item.source_title,
                        item.source_name,
                        item.source_published_at,
                        json.dumps(sorted(set(item.keywords)), ensure_ascii=False),
                        stamp,
                    ),
                )
                if cursor.rowcount != 1:
                    skipped += 1
                    continue
                imported += 1
                connection.executemany(
                    "INSERT OR IGNORE INTO event_alias(event_id,alias) VALUES(?,?)",
                    [(event_id, alias.strip()) for alias in item.aliases if alias.strip()],
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM historical_event WHERE asset_id=?", (asset_id,)
            ).fetchone()[0]
            connection.execute(
                "UPDATE dataset_asset SET record_count=?,updated_at=? WHERE id=?",
                (count, stamp, asset_id),
            )
            return imported, skipped

        imported, skipped = await self.database.write(operation)
        row = await self.database.fetch_one("SELECT * FROM dataset_asset WHERE id=?", (asset_id,))
        assert row is not None
        value = dict(row)
        value["metadata"] = json.loads(value.get("metadata") or "{}")
        value["personal_fields_removed"] = bool(value["personal_fields_removed"])
        return DatasetImportResult(DatasetAssetRecord.model_validate(value), imported, skipped)

    async def dataset_query(self, query: str, *, limit: int = 3) -> list[HistoricalMatch]:
        rows = await self.database.fetch_all(
            """SELECT h.*,d.slug,d.name AS asset_name,
                      COALESCE(json_group_array(a.alias),'[]') AS aliases
               FROM historical_event h
               JOIN dataset_asset d ON d.id=h.asset_id
               LEFT JOIN event_alias a ON a.event_id=h.id
               GROUP BY h.id ORDER BY h.event_time_start DESC,h.id"""
        )
        if not rows:
            return []
        query_normalized = _normalized(query)
        if not query_normalized:
            return []

        def aliases(row) -> list[str]:
            return [item for item in json.loads(row["aliases"] or "[]") if item]

        def direct_score(row) -> float:
            names = [row["event_name"], *aliases(row)]
            if any(
                _normalized(name) in query_normalized or query_normalized in _normalized(name)
                for name in names
            ):
                return 100.0
            return float(len(_terms(query) & _terms(*names, *json.loads(row["keywords"]))))

        anchor = max(rows, key=direct_score)
        anchor_score = direct_score(anchor)
        if anchor_score <= 0:
            return []
        anchor_keywords = set(json.loads(anchor["keywords"] or "[]"))
        anchor_terms = anchor_keywords | _terms(
            anchor["nature"],
            anchor["outbreak_path"],
            anchor["response"],
            anchor["regulatory_involvement"],
        )
        scored: list[tuple[float, set[str], Any]] = []
        for row in rows:
            if row["id"] == anchor["id"]:
                continue
            keywords = set(json.loads(row["keywords"] or "[]"))
            candidate_terms = keywords | _terms(
                row["nature"],
                row["outbreak_path"],
                row["response"],
                row["regulatory_involvement"],
            )
            shared_keywords = anchor_keywords & keywords
            shared = anchor_terms & candidate_terms
            score = len(shared_keywords) * 3.0 + len(shared - shared_keywords) * 0.25
            if row["nature"] and row["nature"] == anchor["nature"]:
                score += 2.0
            if score > 0:
                scored.append((score, shared, row))
        scored.sort(key=lambda item: (-item[0], item[2]["event_time_start"] or "", item[2]["id"]))
        return [self._match(row, score, shared) for score, shared, row in scored[:limit]]

    @staticmethod
    def _match(row: Any, score: float, shared: set[str]) -> HistoricalMatch:
        dimensions = {
            "事件性质": row["nature"] or "未标注",
            "爆发路径": row["outbreak_path"] or "未标注",
            "企业/机构应对": row["response"] or "未标注",
            "监管介入程度": row["regulatory_involvement"] or "未标注",
            "最终结局": row["outcome"] or "未标注",
        }
        return HistoricalMatch(
            event_id=row["id"],
            event_name=row["event_name"],
            event_time_start=row["event_time_start"],
            summary=row["summary"],
            source_url=row["source_url"],
            source_title=row["source_title"],
            source_name=row["source_name"],
            source_published_at=row["source_published_at"],
            score=round(score, 3),
            matched_terms=shared,
            dimensions=dimensions,
        )

    async def prepare_task_context(
        self,
        task_id: str,
        event_query: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        matches = await self.dataset_query(event_query)
        for match in matches:
            evidence = await self.database.add_evidence(
                EvidenceCreate(
                    task_id=task_id,
                    url=match.source_url,
                    title=match.source_title or match.event_name,
                    snippet=" ".join(
                        item
                        for item in (match.summary, match.dimensions.get("最终结局"))
                        if item and item != "未标注"
                    ),
                    source_name=match.source_name or "本地历史数据集",
                    publisher_entity=match.source_name or "本地历史数据集",
                    source_role="unknown",
                    source_tier=4,
                    published_at=match.source_published_at,
                    retrieval_query=event_query,
                    provider="local_dataset",
                    kind="local_dataset",
                    extra={
                        "historical_event_id": match.event_id,
                        "provenance": match.provenance,
                        "matched_terms": sorted(match.matched_terms),
                    },
                )
            )
            await self.database.execute_write(
                """INSERT INTO task_history_match(
                       task_id,historical_event_id,evidence_pk,score,matched_terms,created_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(task_id,historical_event_id) DO UPDATE SET
                     evidence_pk=excluded.evidence_pk,score=excluded.score,
                     matched_terms=excluded.matched_terms""",
                (
                    task_id,
                    match.event_id,
                    evidence.pk,
                    match.score,
                    json.dumps(sorted(match.matched_terms), ensure_ascii=False),
                    now_iso(),
                ),
            )
        hot_points = await self.hotlist_query(
            event_query, date_from=date_from, date_to=date_to, limit=50
        )
        lines = ["本地历史库命中（仅作辅助检索，不用于预测）："]
        lines.extend(
            f"- {item.event_name}（{item.event_time_start or '时间未知'}）：{item.summary}；"
            f"结局：{item.dimensions['最终结局']}；来源证据已入库。"
            for item in matches
        )
        if hot_points:
            lines.append(f"本地热榜命中 {len(hot_points)} 个真实采集点。")
        return "\n".join(lines) if matches or hot_points else ""

    async def task_matches(self, task_id: str) -> list[TaskHistoricalMatch]:
        rows = await self.database.fetch_all(
            """SELECT h.*,m.score,m.matched_terms,e.local_id AS evidence_id
               FROM task_history_match m
               JOIN historical_event h ON h.id=m.historical_event_id
               JOIN evidence e ON e.pk=m.evidence_pk
               WHERE m.task_id=? ORDER BY m.score DESC,h.event_time_start DESC,h.id""",
            (task_id,),
        )
        return [
            TaskHistoricalMatch(
                **self._match(
                    row, float(row["score"]), set(json.loads(row["matched_terms"]))
                ).model_dump(),
                evidence_id=row["evidence_id"],
            )
            for row in rows
        ]

    async def record_hot_snapshots(self, items: list[HotSnapshotInput]) -> int:
        def operation(connection):
            inserted = 0
            for item in items:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO hot_snapshot(
                           asset_id,platform,captured_at,rank,title,heat_value,url,raw
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        item.asset_id,
                        item.platform.strip().lower(),
                        item.captured_at,
                        item.rank,
                        item.title.strip(),
                        item.heat_value,
                        item.url,
                        json.dumps(item.raw, ensure_ascii=False) if item.raw is not None else None,
                    ),
                )
                was_inserted = max(cursor.rowcount, 0)
                inserted += was_inserted
                if not was_inserted and item.asset_id:
                    connection.execute(
                        """UPDATE hot_snapshot SET asset_id=?
                           WHERE asset_id IS NULL AND platform=? AND captured_at=?
                             AND rank=? AND title=?""",
                        (
                            item.asset_id,
                            item.platform.strip().lower(),
                            item.captured_at,
                            item.rank,
                            item.title.strip(),
                        ),
                    )
            return inserted

        return await self.database.write(operation)

    async def hotlist_query(
        self,
        keyword: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 200,
    ) -> list[HotSnapshotPoint]:
        clauses = []
        params: list[Any] = []
        wanted = _normalized(keyword)
        if not wanted:
            return []
        if date_from:
            clauses.append("captured_at>=?")
            params.append(date_from)
        if date_to:
            clauses.append("captured_at<=?")
            params.append(f"{date_to}T23:59:59")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self.database.fetch_all(
            f"SELECT asset_id,platform,captured_at,rank,title,heat_value,url FROM hot_snapshot{where} ORDER BY captured_at DESC,platform,rank",
            tuple(params),
        )
        wanted_terms = _terms(keyword)
        matches = []
        for row in rows:
            title = _normalized(row["title"])
            overlap = wanted_terms & _terms(row["title"])
            if wanted in title or title in wanted or len(overlap) >= 2:
                matches.append(HotSnapshotPoint.model_validate(dict(row)))
                if len(matches) >= limit:
                    break
        matches.sort(key=lambda item: (item.captured_at, item.platform, item.rank))
        return matches
