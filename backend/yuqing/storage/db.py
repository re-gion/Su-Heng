from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from yuqing.storage.models import (
    ClaimCreate,
    ClaimRecord,
    EvidenceCreate,
    EvidenceRecord,
    TaskCreate,
    TaskRecord,
)

T = TypeVar("T")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = (
        host
        if port is None
        or (parts.scheme == "http" and port == 80)
        or (parts.scheme == "https" and port == 443)
        else f"{host}:{port}"
    )
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        )
    )
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path.rstrip("/") or "/", query, ""))


class Database:
    """短事务单写者 SQLite 边界。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        self._queue: asyncio.Queue[
            tuple[Callable[[sqlite3.Connection], Any], asyncio.Future[Any]] | None
        ] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None

    @staticmethod
    def _assert_investigation_open(connection: sqlite3.Connection, task_id: str) -> None:
        row = connection.execute("SELECT status FROM task WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("task not found")
        if row[0] in {"stopping", "done"}:
            raise RuntimeError("task investigation is sealed")

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        connection.executescript(schema)
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(task)").fetchall()}
        task_migrations = {
            "resolved_event_query": "TEXT",
            "request_kind": "TEXT NOT NULL DEFAULT 'event' CHECK (request_kind IN ('event','topic_discovery'))",
            "investigation_scope": "TEXT NOT NULL DEFAULT 'general' CHECK (investigation_scope IN ('general','institution','public_event'))",
            "source_scope": "TEXT NOT NULL DEFAULT 'auto' CHECK (source_scope IN ('auto','domestic','global'))",
            "source_languages": 'TEXT NOT NULL DEFAULT \'["zh","en"]\'',
            "comment_mode": "TEXT NOT NULL DEFAULT 'off' CHECK (comment_mode IN ('off','smart','manual','hybrid'))",
            "comment_urls": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, declaration in task_migrations.items():
            if name not in task_columns:
                connection.execute(f"ALTER TABLE task ADD COLUMN {name} {declaration}")
        task_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='task'").fetchone()[
            0
        ]
        if "'public_event'" not in task_sql:
            # SQLite cannot widen a CHECK in place. Keep child FKs and rows intact.
            connection.commit()
            connection.execute("PRAGMA foreign_keys=OFF")
            upgraded = re.sub(
                r"CREATE TABLE(?: IF NOT EXISTS)?\s+[\"`]?task[\"`]?",
                "CREATE TABLE task_scope_upgrade",
                task_sql,
                count=1,
                flags=re.I,
            )
            upgraded = re.sub(
                r"'general'\s*,\s*'institution'", "'general','institution','public_event'", upgraded
            )
            try:
                connection.execute("BEGIN")
                connection.execute(upgraded)
                connection.execute("INSERT INTO task_scope_upgrade SELECT * FROM task")
                connection.execute("DROP TABLE task")
                connection.execute("ALTER TABLE task_scope_upgrade RENAME TO task")
                if connection.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("task scope migration violates foreign keys")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
        evidence_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(evidence)").fetchall()
        }
        if "kind" not in evidence_columns:
            connection.execute(
                "ALTER TABLE evidence ADD COLUMN kind TEXT NOT NULL DEFAULT 'web' "
                "CHECK (kind IN ('web','local_dataset','social_comments'))"
            )
        claim_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(claim)").fetchall()
        }
        if "analysis_data" not in claim_columns:
            connection.execute(
                "ALTER TABLE claim ADD COLUMN analysis_data TEXT NOT NULL DEFAULT '{}'"
            )
        connection.execute("DROP INDEX IF EXISTS ux_evidence_task_url")
        connection.execute(
            "CREATE UNIQUE INDEX ux_evidence_task_url ON evidence(task_id,url_hash,kind)"
        )
        hot_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(hot_snapshot)").fetchall()
        }
        if "asset_id" not in hot_columns:
            connection.execute(
                "ALTER TABLE hot_snapshot ADD COLUMN asset_id TEXT REFERENCES dataset_asset(id) ON DELETE SET NULL"
            )
        connection.commit()
        self._connection = connection
        self._writer = asyncio.create_task(self._write_loop(), name="sqlite-single-writer")

    async def close(self) -> None:
        if self._writer is not None:
            await self._queue.put(None)
            await self._writer
            self._writer = None
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    async def _write_loop(self) -> None:
        assert self._connection is not None
        while True:
            item = await self._queue.get()
            if item is None:
                return
            operation, future = item
            try:
                value = operation(self._connection)
                self._connection.commit()
            except Exception as exc:
                self._connection.rollback()
                future.set_exception(exc)
            else:
                future.set_result(value)

    async def write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self._writer is None:
            raise RuntimeError("database is not initialized")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        await self._queue.put((operation, future))
        return await future

    async def execute_write(self, sql: str, params: Sequence[Any] = ()) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(sql, params)
            return cursor.rowcount

        return await self.write(operation)

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._read, sql, params)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def create_task(self, data: TaskCreate) -> TaskRecord:
        stamp = now_iso()
        task_id = f"t_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:8]}"
        await self.execute_write(
            """INSERT INTO task(
                 id,event_query,resolved_event_query,request_kind,user_note,investigation_scope,time_range_from,time_range_to,depth,source_scope,
                 source_languages,comment_mode,comment_urls,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
            (
                task_id,
                data.event_query,
                None,
                data.request_kind,
                data.user_note,
                data.investigation_scope,
                data.time_range_from,
                data.time_range_to,
                data.depth,
                data.source_scope,
                json.dumps(data.source_languages, ensure_ascii=False),
                data.comment_mode,
                json.dumps([str(item) for item in data.comment_urls], ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        record = await self.get_task(task_id)
        assert record is not None
        return record

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = await self.fetch_one("SELECT * FROM task WHERE id=?", (task_id,))
        return self._task_record(row) if row else None

    @staticmethod
    def _task_record(row: sqlite3.Row) -> TaskRecord:
        value = dict(row)
        value["source_languages"] = json.loads(value.get("source_languages") or "[]")
        value["comment_urls"] = json.loads(value.get("comment_urls") or "[]")
        return TaskRecord.model_validate(value)

    async def set_resolved_event_query(self, task_id: str, value: str | None) -> None:
        await self.execute_write(
            "UPDATE task SET resolved_event_query=?,updated_at=? WHERE id=?",
            (value.strip() if value is not None else None, now_iso(), task_id),
        )

    async def set_task_time_range(
        self, task_id: str, date_from: str | None, date_to: str | None
    ) -> None:
        await self.execute_write(
            "UPDATE task SET time_range_from=?,time_range_to=?,updated_at=? WHERE id=?",
            (date_from, date_to, now_iso(), task_id),
        )

    async def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        rows = await self.fetch_all(
            "SELECT * FROM task ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [self._task_record(row) for row in rows]

    async def set_task_status(self, task_id: str, status: str, phase: str | None = None) -> None:
        if phase is None:
            await self.execute_write(
                "UPDATE task SET status=?, updated_at=? WHERE id=?", (status, now_iso(), task_id)
            )
        else:
            await self.execute_write(
                "UPDATE task SET status=?, phase=?, updated_at=? WHERE id=?",
                (status, phase, now_iso(), task_id),
            )

    async def claim_task_status(
        self, task_id: str, expected: Sequence[str], status: str, phase: str
    ) -> bool:
        placeholders = ",".join("?" for _ in expected)
        changed = await self.execute_write(
            f"UPDATE task SET status=?,phase=?,updated_at=? WHERE id=? AND status IN ({placeholders})",
            (status, phase, now_iso(), task_id, *expected),
        )
        return changed == 1

    async def set_outer_round(self, task_id: str, outer_round: int) -> None:
        await self.execute_write(
            "UPDATE task SET outer_round=?, updated_at=? WHERE id=?",
            (outer_round, now_iso(), task_id),
        )

    async def update_task_usage(
        self, task_id: str, *, tokens_used: int, cost_estimate: float
    ) -> None:
        await self.execute_write(
            "UPDATE task SET tokens_used=MAX(tokens_used,?),cost_estimate=MAX(cost_estimate,?),updated_at=? WHERE id=?",
            (tokens_used, cost_estimate, now_iso(), task_id),
        )

    async def set_task_config_snapshot(self, task_id: str, snapshot: dict[str, Any]) -> None:
        await self.execute_write(
            "UPDATE task SET config_snapshot=?,updated_at=? WHERE id=?",
            (json.dumps(snapshot, ensure_ascii=False), now_iso(), task_id),
        )

    async def config_values(self) -> dict[str, str]:
        rows = await self.fetch_all("SELECT key,value FROM config")
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def set_config_value(
        self, key: str, value: str | None, *, is_secret: bool = False
    ) -> None:
        if value is None:
            await self.execute_write("DELETE FROM config WHERE key=?", (key,))
            return
        await self.execute_write(
            """INSERT INTO config(key,value,is_secret,updated_at) VALUES(?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,is_secret=excluded.is_secret,updated_at=excluded.updated_at""",
            (key, value.strip(), int(is_secret), now_iso()),
        )

    async def set_config_values(
        self, changes: dict[str, str | None], *, secret_keys: set[str]
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            stamp = now_iso()
            for key, value in changes.items():
                if value is None:
                    connection.execute("DELETE FROM config WHERE key=?", (key,))
                else:
                    connection.execute(
                        """INSERT INTO config(key,value,is_secret,updated_at) VALUES(?,?,?,?)
                           ON CONFLICT(key) DO UPDATE SET value=excluded.value,is_secret=excluded.is_secret,updated_at=excluded.updated_at""",
                        (key, value.strip(), int(key in secret_keys), stamp),
                    )

        await self.write(operation)

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            tables = {
                "evidence": "evidence",
                "claims": "claim",
                "forum_messages": "forum_message",
                "events": "event_log",
                "reports": "report",
            }
            counts = {
                label: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (task_id,)
                    ).fetchone()[0]
                )
                for label, table in tables.items()
            }
            files = [
                row[0]
                for row in connection.execute(
                    "SELECT snapshot_path FROM evidence WHERE task_id=? AND snapshot_path IS NOT NULL",
                    (task_id,),
                ).fetchall()
            ]
            files.extend(
                row[0]
                for row in connection.execute(
                    "SELECT html_path FROM report WHERE task_id=? AND html_path IS NOT NULL",
                    (task_id,),
                ).fetchall()
            )
            files.extend(
                row[0]
                for row in connection.execute(
                    "SELECT pdf_path FROM report WHERE task_id=? AND pdf_path IS NOT NULL",
                    (task_id,),
                ).fetchall()
            )
            deleted = connection.execute("DELETE FROM task WHERE id=?", (task_id,)).rowcount
            if deleted != 1:
                raise ValueError("task not found")
            counts["files"] = files
            return counts

        return await self.write(operation)

    async def add_forum_message(
        self,
        *,
        task_id: str,
        round_number: int,
        agent: str,
        message_type: str,
        content: str,
        refs: list[str],
        payload: dict[str, Any] | None,
        created_at: str,
    ) -> sqlite3.Row:
        def operation(connection: sqlite3.Connection) -> sqlite3.Row:
            self._assert_investigation_open(connection, task_id)
            cursor = connection.execute(
                """INSERT INTO forum_message(task_id,round,agent,type,content,refs,payload,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    round_number,
                    agent,
                    message_type,
                    content,
                    json.dumps(refs, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM forum_message WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            assert row is not None
            value = dict(row)
            value["refs"] = json.loads(value["refs"] or "[]")
            value["payload"] = json.loads(value["payload"]) if value["payload"] else None
            return value  # type: ignore[return-value]

        return await self.write(operation)

    async def list_forum_messages(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self.fetch_all(
            "SELECT * FROM forum_message WHERE task_id=? ORDER BY id", (task_id,)
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["refs"] = json.loads(value["refs"] or "[]")
            value["payload"] = json.loads(value["payload"]) if value["payload"] else None
            result.append(value)
        return result

    async def add_evidence(self, data: EvidenceCreate) -> EvidenceRecord:
        normalized = normalize_url(str(data.url))
        url_hash = hashlib.sha256(normalized.encode()).hexdigest()

        def operation(connection: sqlite3.Connection) -> str:
            self._assert_investigation_open(connection, data.task_id)
            existing = connection.execute(
                "SELECT local_id FROM evidence WHERE task_id=? AND url_hash=? AND kind=?",
                (data.task_id, url_hash, data.kind),
            ).fetchone()
            if existing:
                return str(existing[0])
            count = connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE task_id=?", (data.task_id,)
            ).fetchone()[0]
            local_id = f"E{count + 1:03d}"
            parts = urlsplit(normalized)
            connection.execute(
                """INSERT INTO evidence(
                    pk,task_id,local_id,url,url_hash,kind,title,source_name,source_domain,publisher_entity,
                    origin_url,source_role,source_tier,published_at,discovered_at,fetch_status,fetched_at,
                    snippet,content_text,snapshot_path,content_sha256,retrieval_query,provider,lang,extra)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    data.task_id,
                    local_id,
                    normalized,
                    url_hash,
                    data.kind,
                    data.title,
                    data.source_name,
                    parts.hostname or "",
                    data.publisher_entity,
                    data.origin_url,
                    data.source_role,
                    data.source_tier,
                    data.published_at,
                    now_iso(),
                    data.fetch_status,
                    data.fetched_at,
                    data.snippet or data.summary,
                    data.content_text,
                    data.snapshot_path,
                    data.content_sha256,
                    data.retrieval_query,
                    data.provider,
                    data.lang,
                    json.dumps(data.extra, ensure_ascii=False) if data.extra else None,
                ),
            )
            return local_id

        local_id = await self.write(operation)
        record = await self.get_evidence(data.task_id, local_id)
        assert record is not None
        return record

    async def get_evidence(self, task_id: str, local_id: str) -> EvidenceRecord | None:
        row = await self.fetch_one(
            "SELECT * FROM evidence WHERE task_id=? AND local_id=?", (task_id, local_id)
        )
        return self._evidence_record(row) if row else None

    async def mark_selected_institution_source(
        self, task_id: str, local_id: str, institution_name: str
    ) -> None:
        """Mark a fetched institutional page as the selected event's own statement."""

        def operation(connection: sqlite3.Connection) -> None:
            self._assert_investigation_open(connection, task_id)
            row = connection.execute(
                "SELECT url,content_text,source_name,fetch_status FROM evidence "
                "WHERE task_id=? AND local_id=?",
                (task_id, local_id),
            ).fetchone()
            if row is None:
                return
            host = (urlsplit(row["url"]).hostname or "").lower()
            if (
                row["fetch_status"] == "fetched"
                and host.endswith(".edu.cn")
                and row["source_name"] == institution_name
                and institution_name in (row["content_text"] or "")
            ):
                connection.execute(
                    "UPDATE evidence SET source_role='party',source_tier=MIN(source_tier,2) "
                    "WHERE task_id=? AND local_id=?",
                    (task_id, local_id),
                )

        await self.write(operation)

    @staticmethod
    def _evidence_record(row: sqlite3.Row) -> EvidenceRecord:
        value = dict(row)
        value["extra"] = json.loads(value["extra"]) if value.get("extra") else None
        return EvidenceRecord.model_validate(value)

    async def list_evidence(self, task_id: str) -> list[EvidenceRecord]:
        rows = await self.fetch_all(
            "SELECT * FROM evidence WHERE task_id=? ORDER BY local_id", (task_id,)
        )
        return [self._evidence_record(row) for row in rows]

    async def add_claim(
        self,
        data: ClaimCreate,
        *,
        max_claims: int,
        max_evidence_per_claim: int,
    ) -> ClaimRecord:
        """写入 claim 并做上限校验。

        上限由调用方注入而非在此查表：storage 是最底层，读不到配置系统，
        把分档表写死在这里会让"改配置"无法生效。校验放在同一事务内是为了
        原子性——三个调查 Agent 并发写 claim 时，事务外的预检查会互相越界。
        """

        def operation(connection: sqlite3.Connection) -> str:
            task_row = connection.execute(
                "SELECT depth,status FROM task WHERE id=?", (data.task_id,)
            ).fetchone()
            if task_row is None:
                raise ValueError("task not found")
            if task_row[1] in {"stopping", "done"}:
                raise RuntimeError("task investigation is sealed")
            if len(data.evidence_ids) > max_evidence_per_claim:
                raise ValueError(f"当前深度单条 claim 最多绑定 {max_evidence_per_claim} 条证据")
            existing_claim = connection.execute(
                "SELECT pk,local_id,agent,statement_kind,analysis_data FROM claim WHERE task_id=? AND text=?",
                (data.task_id, data.text),
            ).fetchone()
            evidence_rows = connection.execute(
                f"SELECT pk, local_id, snippet, content_text, fetch_status FROM evidence WHERE task_id=? AND local_id IN ({','.join('?' for _ in data.evidence_ids)})",
                (data.task_id, *data.evidence_ids),
            ).fetchall()
            evidence_by_id = {row[1]: row for row in evidence_rows}
            if set(evidence_by_id) != set(data.evidence_ids):
                raise ValueError("claim references unknown evidence")
            quotes = {quote.evidence_id: quote for quote in data.quotes}

            def insert_links(claim_pk: str, refs: list[str], start_order: int) -> None:
                for order, evidence_id in enumerate(refs, start=start_order):
                    evidence = evidence_by_id[evidence_id]
                    quote_data = quotes.get(evidence_id)
                    quote = quote_data.quote if quote_data else evidence[2]
                    quote_type = (
                        quote_data.quote_type
                        if quote_data
                        else ("paraphrase" if evidence[4] == "fetched" else "snippet")
                    )
                    begin = finish = None
                    verified = 0
                    if quote_type == "verbatim":
                        content = evidence[3] or ""
                        begin = content.find(quote or "")
                        if begin < 0:
                            raise ValueError("verbatim quote is not present in content")
                        finish = begin + len(quote or "")
                        verified = 1
                    elif quote_type == "snippet":
                        normalized_quote = " ".join((quote or "").replace("\u3000", " ").split())
                        normalized_snippet = " ".join(
                            (evidence[2] or "").replace("\u3000", " ").split()
                        )
                        if normalized_quote != normalized_snippet:
                            raise ValueError("snippet quote does not equal evidence snippet")
                    connection.execute(
                        """INSERT INTO claim_evidence(claim_pk,evidence_pk,quote,quote_type,quote_start,quote_end,quote_verified,ord)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (claim_pk, evidence[0], quote, quote_type, begin, finish, verified, order),
                    )

            if existing_claim:
                if existing_claim[2] != data.agent or existing_claim[3] != data.statement_kind:
                    return str(existing_claim[1])
                prior = connection.execute(
                    """SELECT e.local_id FROM claim_evidence ce JOIN evidence e ON e.pk=ce.evidence_pk
                       WHERE ce.claim_pk=? ORDER BY ce.ord""",
                    (existing_claim[0],),
                ).fetchall()
                prior_ids = {row[0] for row in prior}
                new_ids = [ref for ref in data.evidence_ids if ref not in prior_ids]
                if len(prior) + len(new_ids) > max_evidence_per_claim:
                    raise ValueError(f"当前深度单条 claim 最多绑定 {max_evidence_per_claim} 条证据")
                if new_ids:
                    insert_links(existing_claim[0], new_ids, len(prior))
                    connection.execute(
                        """UPDATE claim SET badge=NULL,verdict=NULL,verify_reason=NULL,
                           verification_state='pending',independent_sources=0,max_source_tier=NULL,
                           verifier_model=NULL,verified_at=NULL WHERE pk=?""",
                        (existing_claim[0],),
                    )
                old_data = json.loads(existing_claim[4] or "{}")
                if data.analysis_data and not old_data:
                    connection.execute(
                        "UPDATE claim SET analysis_data=? WHERE pk=?",
                        (json.dumps(data.analysis_data, ensure_ascii=False), existing_claim[0]),
                    )
                return str(existing_claim[1])
            count = connection.execute(
                "SELECT COUNT(*) FROM claim WHERE task_id=?", (data.task_id,)
            ).fetchone()[0]
            if count >= max_claims:
                raise ValueError(f"任务 claim 总数已达当前深度上限 {max_claims}")
            local_id = f"C{count + 1:03d}"
            claim_pk = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO claim(pk,task_id,local_id,text,statement_kind,rumor_text,correction_text,
                   agent,round,section,is_editorial,is_key,is_key_reason,analysis_data,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    claim_pk,
                    data.task_id,
                    local_id,
                    data.text,
                    data.statement_kind,
                    data.rumor_text,
                    data.correction_text,
                    data.agent,
                    data.round,
                    data.section,
                    int(data.is_editorial),
                    int(data.is_key),
                    data.is_key_reason,
                    json.dumps(data.analysis_data, ensure_ascii=False),
                    now_iso(),
                ),
            )
            insert_links(claim_pk, data.evidence_ids, 0)
            return local_id

        local_id = await self.write(operation)
        record = await self.get_claim(data.task_id, local_id)
        assert record is not None
        return record

    async def get_claim(self, task_id: str, local_id: str) -> ClaimRecord | None:
        row = await self.fetch_one(
            "SELECT * FROM claim WHERE task_id=? AND local_id=?", (task_id, local_id)
        )
        if row is None:
            return None
        evidence_rows = await self.fetch_all(
            """SELECT e.local_id FROM claim_evidence ce JOIN evidence e ON e.pk=ce.evidence_pk
               WHERE ce.claim_pk=? ORDER BY ce.ord""",
            (row["pk"],),
        )
        values = dict(row)
        values["evidence_ids"] = [item["local_id"] for item in evidence_rows]
        values["analysis_data"] = json.loads(values.get("analysis_data") or "{}")
        return ClaimRecord.model_validate(values)

    async def list_claims(self, task_id: str) -> list[ClaimRecord]:
        rows = await self.fetch_all(
            "SELECT local_id FROM claim WHERE task_id=? ORDER BY local_id", (task_id,)
        )
        records = [await self.get_claim(task_id, row["local_id"]) for row in rows]
        return [record for record in records if record is not None]

    async def update_evidence_fetched(
        self,
        task_id: str,
        local_id: str,
        *,
        content_text: str,
        snapshot_path: str,
        content_sha256: str,
        published_at: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            self._assert_investigation_open(connection, task_id)
            existing = connection.execute(
                "SELECT extra FROM evidence WHERE task_id=? AND local_id=?",
                (task_id, local_id),
            ).fetchone()
            merged_extra = json.loads(existing[0]) if existing and existing[0] else {}
            merged_extra.update(extra or {})
            connection.execute(
                """UPDATE evidence SET fetch_status='fetched', fetched_at=?, content_text=?,
                   snapshot_path=?, content_sha256=?, published_at=COALESCE(?,published_at), extra=?
                   WHERE task_id=? AND local_id=?""",
                (
                    now_iso(),
                    content_text,
                    snapshot_path,
                    content_sha256,
                    published_at,
                    json.dumps(merged_extra, ensure_ascii=False) if merged_extra else None,
                    task_id,
                    local_id,
                ),
            )

        await self.write(operation)

    async def update_evidence_failed(self, task_id: str, local_id: str, reason: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            self._assert_investigation_open(connection, task_id)
            existing = connection.execute(
                "SELECT extra FROM evidence WHERE task_id=? AND local_id=?",
                (task_id, local_id),
            ).fetchone()
            merged_extra = json.loads(existing[0]) if existing and existing[0] else {}
            merged_extra["fetch_error"] = reason
            connection.execute(
                "UPDATE evidence SET fetch_status='fetch_failed', extra=? WHERE task_id=? AND local_id=?",
                (json.dumps(merged_extra, ensure_ascii=False), task_id, local_id),
            )

        await self.write(operation)

    async def evidence_by_pk(self, evidence_pk: str) -> sqlite3.Row | None:
        return await self.fetch_one("SELECT * FROM evidence WHERE pk=?", (evidence_pk,))

    async def claim_evidence_rows(self, claim_pk: str) -> list[sqlite3.Row]:
        return await self.fetch_all(
            """SELECT ce.*, e.pk AS pk, e.pk AS evidence_pk, e.task_id, e.local_id AS evidence_id,
                      e.local_id, e.url, e.title, e.snippet, e.source_name, e.source_domain,
                      e.publisher_entity, e.source_role, e.source_tier, e.published_at,
                      e.fetch_status, e.content_text, e.snapshot_path, e.content_sha256, e.provider,
                      e.lang, e.extra, e.kind
               FROM claim_evidence ce JOIN evidence e ON e.pk=ce.evidence_pk
               WHERE ce.claim_pk=? ORDER BY ce.ord""",
            (claim_pk,),
        )

    async def set_evidence_relation(
        self,
        claim_pk: str,
        evidence_pk: str,
        *,
        relation: str,
        reason: str,
        cited_sentence: str,
        cited_verified: bool,
        is_correction: bool = False,
    ) -> None:
        await self.execute_write(
            """UPDATE claim_evidence SET relation=?,verify_reason=?,cited_sentence=?,cited_verified=?,is_correction=?
               WHERE claim_pk=? AND evidence_pk=?""",
            (
                relation,
                reason,
                cited_sentence,
                int(cited_verified),
                int(is_correction),
                claim_pk,
                evidence_pk,
            ),
        )

    async def set_claim_verification(
        self,
        claim_pk: str,
        *,
        badge: str,
        verdict: str,
        reason: str,
        state: str,
        independent_sources: int,
        max_source_tier: int | None,
        verifier_model: str,
    ) -> None:
        await self.execute_write(
            """UPDATE claim SET badge=?,verdict=?,verify_reason=?,verification_state=?,independent_sources=?,
               max_source_tier=?,verifier_model=?,verified_at=? WHERE pk=?""",
            (
                badge,
                verdict,
                reason,
                state,
                independent_sources,
                max_source_tier,
                verifier_model,
                now_iso(),
                claim_pk,
            ),
        )

    async def save_checkpoint(self, task_id: str, step_key: str, payload: dict[str, Any]) -> None:
        stamp = now_iso()
        encoded = json.dumps(payload, ensure_ascii=False)

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO task_state(task_id,step_key,kind,status,replay,payload,result_ref,created_at,updated_at)
                   VALUES(?,?,'round_checkpoint','settled','safe',?,?,?,?)
                   ON CONFLICT(task_id,step_key) DO UPDATE SET status='settled',payload=excluded.payload,
                   result_ref=excluded.result_ref,updated_at=excluded.updated_at""",
                (task_id, step_key, encoded, encoded, stamp, stamp),
            )

        await self.write(operation)

    async def latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        row = await self.fetch_one(
            "SELECT result_ref FROM task_state WHERE task_id=? AND kind='round_checkpoint' AND status='settled' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        return json.loads(row["result_ref"]) if row else None

    async def checkpoint(self, task_id: str, step_key: str) -> dict[str, Any] | None:
        row = await self.fetch_one(
            "SELECT result_ref FROM task_state WHERE task_id=? AND step_key=? AND kind='round_checkpoint' AND status='settled'",
            (task_id, step_key),
        )
        return json.loads(row["result_ref"]) if row else None

    async def save_scope_review(self, task_id: str, fingerprint: str, value: dict) -> None:
        await self.execute_write(
            "INSERT OR REPLACE INTO scope_review VALUES(?,?,?)",
            (task_id, fingerprint, json.dumps(value, ensure_ascii=False)),
        )

    async def get_scope_review(self, task_id: str, fingerprint: str) -> dict | None:
        row = await self.fetch_one(
            "SELECT payload FROM scope_review WHERE task_id=? AND fingerprint=?",
            (task_id, fingerprint),
        )
        return json.loads(row[0]) if row else None

    async def save_analysis_batch(
        self, task_id: str, agent: str, fingerprint: str, value: dict
    ) -> None:
        await self.execute_write(
            "INSERT OR REPLACE INTO analysis_batch VALUES(?,?,?,?)",
            (task_id, agent, fingerprint, json.dumps(value, ensure_ascii=False)),
        )

    async def get_analysis_batch(self, task_id: str, agent: str, fingerprint: str) -> dict | None:
        row = await self.fetch_one(
            "SELECT payload FROM analysis_batch WHERE task_id=? AND agent=? AND fingerprint=?",
            (task_id, agent, fingerprint),
        )
        return json.loads(row[0]) if row else None

    async def record_llm_call(self, task_id: str, value: dict) -> None:
        await self.execute_write(
            "INSERT OR REPLACE INTO llm_call VALUES(?,?,?,?)",
            (task_id, value["call_id"], value["attempt"], json.dumps(value, ensure_ascii=False)),
        )

    async def llm_diagnostics(self, task_id: str) -> dict:
        rows = await self.fetch_all(
            "SELECT payload FROM llm_call WHERE task_id=? ORDER BY rowid", (task_id,)
        )
        calls = [json.loads(row[0]) for row in rows]
        now = datetime.now().astimezone()

        def was_requested(call):
            return call.get("status") != "queued" and not (
                call.get("status") == "cancelled" and not call.get("requested_at")
            )

        by_stage = {}
        activities = []
        for call in calls:
            stage = str(call.get("stage", "unknown"))
            bucket = by_stage.setdefault(
                stage,
                {
                    "requests": 0,
                    "failed": 0,
                    "retries": 0,
                    "request_ms": 0,
                    "queue_ms": 0,
                },
            )
            if was_requested(call):
                bucket["requests"] += 1
            bucket["failed"] += call.get("status") == "failed"
            bucket["retries"] += was_requested(call) and call.get("attempt", 1) > 1
            bucket["request_ms"] += call.get("request_ms", 0)
            bucket["queue_ms"] += call.get("queue_ms", 0)
            if call.get("status") in {"queued", "inflight"}:
                age = 0
                try:
                    started = datetime.fromisoformat(call["started_at"])
                    age = max(0, (now - started).total_seconds())
                except (KeyError, ValueError, TypeError):
                    pass
                activities.append(
                    {
                        "stage": stage,
                        "role": call.get("role", "unknown"),
                        "status": call["status"],
                        "attempt": call.get("attempt", 1),
                        "elapsed_seconds": round(age, 1),
                    }
                )
        return {
            "calls": calls,
            "recorded_requests": sum(was_requested(c) for c in calls),
            "recorded_tokens": sum(c["total_tokens"] for c in calls),
            "queue_ms": sum(c["queue_ms"] for c in calls),
            "request_ms": sum(c["request_ms"] for c in calls),
            "failed_requests": sum(c.get("status") == "failed" for c in calls),
            "retry_requests": sum(was_requested(c) and c.get("attempt", 1) > 1 for c in calls),
            "queued_requests": sum(c.get("status") == "queued" for c in calls),
            "inflight_requests": sum(c.get("status") == "inflight" for c in calls),
            "activities": activities,
            "by_stage": by_stage,
        }

    async def save_usage_checkpoint(self, task_id: str, value: dict) -> None:
        def operation(connection):
            row = connection.execute(
                "SELECT payload FROM task_usage WHERE task_id=?", (task_id,)
            ).fetchone()
            previous = json.loads(row[0]) if row else {}
            merged = {
                k: max(int(previous.get(k, 0)), int(value.get(k, 0)))
                for k in ("tokens_used", "calls")
            }
            connection.execute(
                "INSERT OR REPLACE INTO task_usage VALUES(?,?)", (task_id, json.dumps(merged))
            )
            connection.execute(
                "UPDATE task SET tokens_used=MAX(tokens_used,?) WHERE id=?",
                (merged["tokens_used"], task_id),
            )

        await self.write(operation)

    async def usage_checkpoint(self, task_id: str) -> dict:
        row = await self.fetch_one("SELECT payload FROM task_usage WHERE task_id=?", (task_id,))
        return json.loads(row[0]) if row else {}

    async def save_report(
        self,
        task_id: str,
        report_id: str,
        ir: dict[str, Any],
        html_path: str,
        metrics: dict[str, Any],
    ) -> None:
        await self.execute_write(
            """INSERT OR REPLACE INTO report(id,task_id,ir_json,html_path,metrics,generated_at)
               VALUES(?,?,?,?,?,?)""",
            (
                report_id,
                task_id,
                json.dumps(ir, ensure_ascii=False),
                html_path,
                json.dumps(metrics, ensure_ascii=False),
                now_iso(),
            ),
        )

    async def get_report_for_task(self, task_id: str) -> sqlite3.Row | None:
        return await self.fetch_one(
            "SELECT * FROM report WHERE task_id=? ORDER BY generated_at DESC LIMIT 1", (task_id,)
        )

    async def set_report_pdf(self, report_id: str, pdf_path: str) -> None:
        await self.execute_write("UPDATE report SET pdf_path=? WHERE id=?", (pdf_path, report_id))

    async def consume_quota_bundle(self, entries: Sequence[tuple[str, str, int, int]]) -> bool:
        """Atomically reserve provider quota across daily/monthly/lifetime windows."""

        def operation(connection: sqlite3.Connection) -> bool:
            for scope, period_key, limit, amount in entries:
                row = connection.execute(
                    "SELECT used FROM provider_quota WHERE provider=? AND period_key=?",
                    (scope, period_key),
                ).fetchone()
                used = int(row[0]) if row else 0
                if amount < 1 or used + amount > limit:
                    return False
            for scope, period_key, _limit, amount in entries:
                connection.execute(
                    """INSERT INTO provider_quota(provider,period_key,used) VALUES(?,?,?)
                       ON CONFLICT(provider,period_key) DO UPDATE SET used=used+excluded.used""",
                    (scope, period_key, amount),
                )
            return True

        return await self.write(operation)

    async def consume_quota(self, scope: str, period_key: str, limit: int) -> bool:
        return await self.consume_quota_bundle([(scope, period_key, limit, 1)])

    async def record_provider_usage(self, scope: str, period_key: str, amount: int) -> None:
        if amount <= 0:
            return
        await self.execute_write(
            """INSERT INTO provider_quota(provider,period_key,used) VALUES(?,?,?)
               ON CONFLICT(provider,period_key) DO UPDATE SET used=used+excluded.used""",
            (scope, period_key, amount),
        )

    async def provider_usage(self, scope: str, period_key: str) -> int:
        row = await self.fetch_one(
            "SELECT used FROM provider_quota WHERE provider=? AND period_key=?",
            (scope, period_key),
        )
        return int(row["used"]) if row else 0

    async def request_takedown(self, report_id: str, reason: str) -> None:
        await self.execute_write(
            "INSERT INTO takedown_request(report_id,reason,requested_at) VALUES(?,?,?)",
            (report_id, reason, now_iso()),
        )

    async def bind_demo_owner(self, task_id: str, owner_hash: str) -> None:
        await self.execute_write(
            "INSERT OR REPLACE INTO demo_task_owner(task_id,owner_hash,created_at) VALUES(?,?,?)",
            (task_id, owner_hash, now_iso()),
        )

    async def demo_task_owned_by(self, task_id: str, owner_hash: str) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM demo_task_owner WHERE task_id=? AND owner_hash=?",
            (task_id, owner_hash),
        )
        return row is not None

    async def report_under_review(self, report_id: str) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM takedown_request WHERE report_id=? AND status='pending' LIMIT 1",
            (report_id,),
        )
        return row is not None

    async def data_status(self) -> dict[str, Any]:
        row = await self.fetch_one(
            """SELECT
                 (SELECT COUNT(*) FROM dataset_asset) AS assets,
                 (SELECT COUNT(*) FROM historical_event) AS historical_events,
                 (SELECT COUNT(*) FROM hot_snapshot) AS hot_snapshots,
                 (SELECT MIN(captured_at) FROM hot_snapshot) AS hot_from,
                 (SELECT MAX(captured_at) FROM hot_snapshot) AS hot_to"""
        )
        return dict(row) if row else {}

    async def mark_orphaned_tasks(self) -> int:
        return await self.execute_write(
            "UPDATE task SET status='failed', updated_at=? WHERE status IN ('running','pausing','stopping')",
            (now_iso(),),
        )
