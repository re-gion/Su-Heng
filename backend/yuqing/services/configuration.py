from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from yuqing.core.llm.roles import ROLES
from yuqing.services.budget import (
    BUDGET_FIELDS,
    DEFAULT_BUDGET_TABLE,
    budget_table_to_json,
    dump_budget_overrides,
    parse_budget_overrides,
    resolve_budget_table,
)
from yuqing.services.provider_quota import ProviderQuotaManager
from yuqing.storage.db import Database

DOMESTIC_SEARCH_PROVIDERS = ("langsearch", "exa", "qianfan", "bocha")
FOREIGN_SEARCH_PROVIDERS = ("exa", "tavily", "serper")
DEFAULT_SEARCH_PROVIDER_ORDER = tuple(
    dict.fromkeys((*DOMESTIC_SEARCH_PROVIDERS, *FOREIGN_SEARCH_PROVIDERS))
)
# 智谱适配器仍保留给旧配置与诊断脚本，但不再属于产品检索链。
COMPATIBLE_SEARCH_PROVIDERS = ("zhipu",)
SEARCH_PROVIDERS = (*DEFAULT_SEARCH_PROVIDER_ORDER, *COMPATIBLE_SEARCH_PROVIDERS)
FETCH_PROVIDERS = ("builtin", "firecrawl")
DEFAULT_FETCH_PROVIDER_ORDER = FETCH_PROVIDERS
SECRET_KEYS = {
    "DEFAULT_API_KEY",
    *(f"LLM_{role.upper()}_API_KEY" for role in ROLES),
    *(f"{name.upper()}_API_KEY" for name in SEARCH_PROVIDERS),
    "FIRECRAWL_API_KEY",
}
MASK_PATTERN = re.compile(r"^.{1,12}-?\*{3}.{3}$")


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    prefix = value.split("-", 1)[0] + "-" if "-" in value else value[:2]
    return f"{prefix}***{value[-3:]}"


class ConfigService:
    def __init__(self, database: Database, environ: Mapping[str, str] | None = None):
        self.database = database
        self.environ = environ or os.environ

    async def _values(self) -> tuple[dict[str, str], dict[str, str]]:
        stored = await self.database.config_values()
        merged = dict(self.environ)
        merged.update({key: value for key, value in stored.items() if value != ""})
        return stored, merged

    @staticmethod
    def _origin(key: str, stored: Mapping[str, str], environ: Mapping[str, str]) -> str:
        if key in stored:
            return "config"
        if environ.get(key, "").strip():
            return "env"
        return "default"

    async def public(self) -> dict[str, Any]:
        stored, values = await self._values()
        quota_status = await ProviderQuotaManager(self.database).public_status()
        default_values = {
            "api_key": values.get("DEFAULT_API_KEY", "").strip() or None,
            "base_url": values.get("DEFAULT_BASE_URL", "").strip() or "https://api.deepseek.com/v1",
            "model": values.get("DEFAULT_MODEL", "").strip() or "deepseek-chat",
        }
        default = {
            field: mask_secret(value) if field == "api_key" else value
            for field, value in default_values.items()
        }
        default["source"] = {
            field: self._origin(f"DEFAULT_{field.upper()}", stored, self.environ)
            for field in ("api_key", "base_url", "model")
        }
        roles: dict[str, Any] = {}
        for role in ROLES:
            prefix = f"LLM_{role.upper()}_"
            explicit: dict[str, str | None] = {}
            effective: dict[str, str | None] = {}
            source: dict[str, str] = {}
            for field in ("api_key", "base_url", "model"):
                key = prefix + field.upper()
                raw = values.get(key, "").strip() or None
                explicit[field] = mask_secret(raw) if field == "api_key" else raw
                inherited = raw or default_values[field]
                effective[field] = mask_secret(inherited) if field == "api_key" else inherited
                source[field] = self._origin(key, stored, self.environ) if raw else "inherit"
            roles[role] = {**explicit, "effective": effective, "source": source}
        fetch_order = [
            name.strip()
            for name in values.get(
                "FETCH_PROVIDER_ORDER", ",".join(DEFAULT_FETCH_PROVIDER_ORDER)
            ).split(",")
            if name.strip() in FETCH_PROVIDERS
        ]
        budget_overrides = parse_budget_overrides(values.get("BUDGET_OVERRIDES"))
        return {
            "llm": {"default": default, "roles": roles},
            "search": {
                "provider_order": list(DEFAULT_SEARCH_PROVIDER_ORDER),
                "keys": {
                    name: mask_secret(values.get(f"{name.upper()}_API_KEY", "").strip() or None)
                    for name in DEFAULT_SEARCH_PROVIDER_ORDER
                },
                "quota": quota_status,
            },
            "fetch": {
                "provider_order": fetch_order,
                "quota": {"firecrawl": await ProviderQuotaManager(self.database).fetch_status()},
                "keys": {
                    "firecrawl": mask_secret(values.get("FIRECRAWL_API_KEY", "").strip() or None)
                },
            },
            "comments": {
                "enabled": values.get("YUQING_COMMENT_PLUGIN_ENABLED", "false").lower()
                in {"1", "true", "yes", "on"}
            },
            "budget": {
                "overrides": budget_overrides,
                "effective": budget_table_to_json(resolve_budget_table(budget_overrides)),
                "defaults": budget_table_to_json(DEFAULT_BUDGET_TABLE),
                "fields": list(BUDGET_FIELDS),
                "depths": list(DEFAULT_BUDGET_TABLE),
                "source": self._origin("BUDGET_OVERRIDES", stored, self.environ),
            },
        }

    async def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("配置请求必须是对象")
        changes: dict[str, str | None] = {}
        llm = payload.get("llm") or {}
        if not isinstance(llm, dict):
            raise ValueError("llm 必须是对象")
        if "default" in llm:
            if not isinstance(llm.get("default"), dict):
                raise ValueError("llm.default 必须是对象")
            for field, value in (llm.get("default") or {}).items():
                if field in {"api_key", "base_url", "model"}:
                    changes[f"DEFAULT_{field.upper()}"] = value
        role_values = llm.get("roles") or {}
        if not isinstance(role_values, dict):
            raise ValueError("llm.roles 必须是对象")
        for role, fields in role_values.items():
            if role not in ROLES:
                raise ValueError(f"未知 LLM 角色：{role}")
            if not isinstance(fields, dict):
                raise ValueError(f"LLM 角色 {role} 必须是对象")
            for field, value in (fields or {}).items():
                if field in {"api_key", "base_url", "model"}:
                    changes[f"LLM_{role.upper()}_{field.upper()}"] = value
        search = payload.get("search") or {}
        if not isinstance(search, dict):
            raise ValueError("search 必须是对象")
        if "provider_order" in search:
            raise ValueError("搜索顺序已经固定，不再支持 provider_order 配置")
        search_keys = search.get("keys") or {}
        if not isinstance(search_keys, dict):
            raise ValueError("search.keys 必须是对象")
        for name, value in search_keys.items():
            if name not in SEARCH_PROVIDERS:
                raise ValueError(f"未知搜索 provider：{name}")
            changes[f"{name.upper()}_API_KEY"] = value
        fetch = payload.get("fetch") or {}
        if not isinstance(fetch, dict):
            raise ValueError("fetch 必须是对象")
        if "provider_order" in fetch:
            order = fetch["provider_order"]
            if order not in (["builtin"], ["builtin", "firecrawl"]):
                raise ValueError("抓取 provider_order 含未知项")
            changes["FETCH_PROVIDER_ORDER"] = ",".join(order)
        fetch_keys = fetch.get("keys") or {}
        if not isinstance(fetch_keys, dict):
            raise ValueError("fetch.keys 必须是对象")
        for name, value in fetch_keys.items():
            if name != "firecrawl":
                raise ValueError(f"未知抓取 provider：{name}")
            changes["FIRECRAWL_API_KEY"] = value
        comments = payload.get("comments") or {}
        if not isinstance(comments, dict):
            raise ValueError("comments 必须是对象")
        if "enabled" in comments:
            if not isinstance(comments["enabled"], bool):
                raise ValueError("comments.enabled 必须是布尔值")
            changes["YUQING_COMMENT_PLUGIN_ENABLED"] = "true" if comments["enabled"] else "false"
        budget = payload.get("budget") or {}
        if not isinstance(budget, dict):
            raise ValueError("budget 必须是对象")
        if "overrides" in budget:
            overrides = budget["overrides"] if budget["overrides"] is not None else {}
            if not isinstance(overrides, dict):
                raise ValueError("budget.overrides 必须是对象")
            # 先按默认表合并校验；非法档位/字段/数值一律拒绝落库。
            resolve_budget_table(overrides)
            changes["BUDGET_OVERRIDES"] = dump_budget_overrides(overrides)
        for key, value in changes.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串或 null")
            if isinstance(value, str) and "***" in value:
                raise ValueError("脱敏占位值不能写入配置")
            if key.endswith("BASE_URL") and value:
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError(f"{key} 必须是 http(s) URL")
        await self.database.set_config_values(changes, secret_keys=SECRET_KEYS)
        return await self.public()

    async def resolved_environ(self) -> dict[str, str]:
        stored = await self.database.config_values()
        values = dict(self.environ)
        values.update(stored)
        # 旧版 SQLite/.env 可能仍保存可调整顺序；运行时统一迁移到当前固定策略。
        values["SEARCH_PROVIDER_ORDER"] = ",".join(DEFAULT_SEARCH_PROVIDER_ORDER)
        return values
