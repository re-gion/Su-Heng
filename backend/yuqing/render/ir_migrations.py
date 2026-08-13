from __future__ import annotations

import copy
from typing import Any

CURRENT_SCHEMA_VERSION = "0.3"
CURRENT_READER_MINOR = 3


class UnsupportedReportVersion(ValueError):
    pass


def migrate_report(report: dict[str, Any]) -> dict[str, Any]:
    """把持久化 IR 升到当前次版本；迁移只补字段，不改权威事实与引用。"""

    value = copy.deepcopy(report)
    version = str(value.get("schema_version") or "")
    if version == CURRENT_SCHEMA_VERSION:
        return value
    if version not in {"0.1", "0.2"}:
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
    for block in value.get("blocks", []):
        if block.get("type") == "evidence_appendix":
            for item in block.get("items", []):
                item.setdefault("kind", "web")
                item.setdefault("lang", "zh")
    value["schema_version"] = CURRENT_SCHEMA_VERSION
    value["min_reader_minor"] = CURRENT_READER_MINOR
    if "0.2->0.3" not in history:
        history.append("0.2->0.3")
    value["migration_history"] = history
    return value
