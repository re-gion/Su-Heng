from __future__ import annotations

import copy
from typing import Any

CURRENT_SCHEMA_VERSION = "0.8"
CURRENT_READER_MINOR = 8


class UnsupportedReportVersion(ValueError):
    pass


def migrate_report(report: dict[str, Any]) -> dict[str, Any]:
    """把持久化 IR 升到当前次版本；迁移只补字段，不改权威事实与引用。"""

    value = copy.deepcopy(report)
    version = str(value.get("schema_version") or "")
    if version == CURRENT_SCHEMA_VERSION:
        return value
    if version not in {"0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7"}:
        raise UnsupportedReportVersion(f"不支持报告 IR {version or 'unknown'}")
    history = list(value.get("migration_history") or [])
    if version == "0.1":
        for block in value.get("blocks", []):
            if block.get("type") != "history_compare":
                continue
            for card in block.get("cards", []):
                card.setdefault("provenance", "历史报告迁移")
                card.setdefault("dimensions", {})
        if "0.1->0.2" not in history:
            history.append("0.1->0.2")
        version = "0.2"
    if version == "0.2":
        for block in value.get("blocks", []):
            if block.get("type") == "evidence_appendix":
                for item in block.get("items", []):
                    item.setdefault("kind", "web")
                    item.setdefault("lang", "zh")
        if "0.2->0.3" not in history:
            history.append("0.2->0.3")
        version = "0.3"
    if version == "0.3" and "0.3->0.4" not in history:
        history.append("0.3->0.4")
    if version not in {"0.5", "0.6", "0.7"} and "0.4->0.5" not in history:
        history.append("0.4->0.5")
    if version not in {"0.6", "0.7"} and "0.5->0.6" not in history:
        history.append("0.5->0.6")
    if version != "0.7":
        history.append("0.6->0.7")
    history.append("0.7->0.8")
    quality = value.setdefault("quality", {})
    quality.setdefault("chapter_status", {})
    quality.setdefault("scope_review", {"status": "unknown", "message": "旧报告未记录逐项范围审查"})
    quality.setdefault("call_diagnostics", {"recorded_requests": 0, "recorded_tokens": None})
    quality.setdefault(
        "investigation_outcome", {"end_reason": "unknown", "message": "旧报告未记录调查结束原因"}
    )
    value["schema_version"] = CURRENT_SCHEMA_VERSION
    value["min_reader_minor"] = CURRENT_READER_MINOR
    value["migration_history"] = history
    return value
