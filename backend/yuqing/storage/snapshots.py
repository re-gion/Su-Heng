from __future__ import annotations

import gzip
import hashlib
import html
from pathlib import Path


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, task_id: str, evidence_pk: str, raw_html: str) -> tuple[str, str]:
        target = self.root / task_id / f"{evidence_pk}.html.gz"
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = raw_html.encode("utf-8")
        with gzip.open(target, "wb") as file:
            file.write(payload)
        return str(target), hashlib.sha256(payload).hexdigest()

    def read_sanitized(self, path: str, content_text: str) -> str:
        # M0 采用最保守的纯文本回放：原始 HTML 仍留档，但不会进入浏览器执行环境。
        return f"<!doctype html><html><head><meta charset='utf-8'></head><body><article><pre>{html.escape(content_text)}</pre></article></body></html>"
