from __future__ import annotations

import asyncio
import hashlib
import json
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

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        schema = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        connection.executescript(schema)
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
            """INSERT INTO task(id,event_query,user_note,time_range_from,time_range_to,depth,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'queued',?,?)""",
            (
                task_id,
                data.event_query,
                data.user_note,
                data.time_range_from,
                data.time_range_to,
                data.depth,
                stamp,
                stamp,
            ),
        )
        record = await self.get_task(task_id)
        assert record is not None
        return record

    async def get_task(self, task_id: str) -> TaskRecord | None:
        row = await self.fetch_one("SELECT * FROM task WHERE id=?", (task_id,))
        return TaskRecord.model_validate(dict(row)) if row else None

    async def list_tasks(self, limit: int = 20) -> list[TaskRecord]:
        rows = await self.fetch_all(
            "SELECT * FROM task ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [TaskRecord.model_validate(dict(row)) for row in rows]

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

    async def add_evidence(self, data: EvidenceCreate) -> EvidenceRecord:
        normalized = normalize_url(str(data.url))
        url_hash = hashlib.sha256(normalized.encode()).hexdigest()

        def operation(connection: sqlite3.Connection) -> str:
            existing = connection.execute(
                "SELECT local_id FROM evidence WHERE task_id=? AND url_hash=?",
                (data.task_id, url_hash),
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
                    pk,task_id,local_id,url,url_hash,title,source_name,source_domain,publisher_entity,
                    origin_url,source_role,source_tier,published_at,discovered_at,fetch_status,fetched_at,
                    snippet,content_text,snapshot_path,content_sha256,retrieval_query,provider,lang,extra)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    data.task_id,
                    local_id,
                    normalized,
                    url_hash,
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
        return EvidenceRecord.model_validate(dict(row)) if row else None

    async def list_evidence(self, task_id: str) -> list[EvidenceRecord]:
        rows = await self.fetch_all(
            "SELECT * FROM evidence WHERE task_id=? ORDER BY local_id", (task_id,)
        )
        return [EvidenceRecord.model_validate(dict(row)) for row in rows]

    async def add_claim(self, data: ClaimCreate) -> ClaimRecord:
        def operation(connection: sqlite3.Connection) -> str:
            task_row = connection.execute(
                "SELECT depth FROM task WHERE id=?", (data.task_id,)
            ).fetchone()
            if task_row is None:
                raise ValueError("task not found")
            max_evidence = {"quick": 3, "standard": 4, "deep": 6}[task_row[0]]
            if len(data.evidence_ids) > max_evidence:
                raise ValueError(f"当前深度单条 claim 最多绑定 {max_evidence} 条证据")
            existing_claim = connection.execute(
                "SELECT local_id FROM claim WHERE task_id=? AND text=?", (data.task_id, data.text)
            ).fetchone()
            if existing_claim:
                return str(existing_claim[0])
            evidence_rows = connection.execute(
                f"SELECT pk, local_id, snippet, content_text, fetch_status FROM evidence WHERE task_id=? AND local_id IN ({','.join('?' for _ in data.evidence_ids)})",
                (data.task_id, *data.evidence_ids),
            ).fetchall()
            evidence_by_id = {row[1]: row for row in evidence_rows}
            if set(evidence_by_id) != set(data.evidence_ids):
                raise ValueError("claim references unknown evidence")
            count = connection.execute(
                "SELECT COUNT(*) FROM claim WHERE task_id=?", (data.task_id,)
            ).fetchone()[0]
            local_id = f"C{count + 1:03d}"
            claim_pk = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO claim(pk,task_id,local_id,text,statement_kind,rumor_text,correction_text,
                   agent,round,section,is_editorial,is_key,is_key_reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    now_iso(),
                ),
            )
            quotes = {quote.evidence_id: quote for quote in data.quotes}
            for order, evidence_id in enumerate(data.evidence_ids):
                evidence = evidence_by_id[evidence_id]
                quote_data = quotes.get(evidence_id)
                quote = quote_data.quote if quote_data else evidence[2]
                quote_type = (
                    quote_data.quote_type
                    if quote_data
                    else ("paraphrase" if evidence[4] == "fetched" else "snippet")
                )
                start = end = None
                verified = 0
                if quote_type == "verbatim":
                    content = evidence[3] or ""
                    start = content.find(quote or "")
                    if start < 0:
                        raise ValueError("verbatim quote is not present in content")
                    end = start + len(quote or "")
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
                    (claim_pk, evidence[0], quote, quote_type, start, end, verified, order),
                )
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
    ) -> None:
        await self.execute_write(
            """UPDATE evidence SET fetch_status='fetched', fetched_at=?, content_text=?,
               snapshot_path=?, content_sha256=? WHERE task_id=? AND local_id=?""",
            (now_iso(), content_text, snapshot_path, content_sha256, task_id, local_id),
        )

    async def update_evidence_failed(self, task_id: str, local_id: str, reason: str) -> None:
        await self.execute_write(
            "UPDATE evidence SET fetch_status='fetch_failed', extra=? WHERE task_id=? AND local_id=?",
            (json.dumps({"fetch_error": reason}, ensure_ascii=False), task_id, local_id),
        )

    async def evidence_by_pk(self, evidence_pk: str) -> sqlite3.Row | None:
        return await self.fetch_one("SELECT * FROM evidence WHERE pk=?", (evidence_pk,))

    async def claim_evidence_rows(self, claim_pk: str) -> list[sqlite3.Row]:
        return await self.fetch_all(
            """SELECT ce.*, e.pk AS pk, e.pk AS evidence_pk, e.task_id, e.local_id AS evidence_id,
                      e.local_id, e.url, e.title, e.snippet, e.source_name, e.source_domain,
                      e.publisher_entity, e.source_role, e.source_tier, e.published_at,
                      e.fetch_status, e.content_text, e.snapshot_path, e.content_sha256, e.provider
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

    async def mark_orphaned_tasks(self) -> int:
        return await self.execute_write(
            "UPDATE task SET status='failed', updated_at=? WHERE status IN ('running','pausing','stopping')",
            (now_iso(),),
        )
