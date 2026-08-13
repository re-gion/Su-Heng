from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from yuqing.services.historical_data import (
    DatasetAssetInput,
    HistoricalDataService,
    HotSnapshotInput,
)
from yuqing.storage.db import now_iso


def parse_heat_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).replace(",", "").strip()
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([万亿]?)", text)
    if not match:
        return None
    multiplier = {"": 1, "万": 10_000, "亿": 100_000_000}[match.group(2)]
    return float(match.group(1)) * multiplier


@dataclass(frozen=True)
class HotCollectionResult:
    inserted: int
    platforms: dict[str, str]


class DailyHotCollector:
    """兼容 DailyHotApi 常见响应形状；单平台失败不会影响其他平台。"""

    def __init__(
        self,
        history: HistoricalDataService,
        base_urls: list[str],
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.history = history
        self.base_urls = [item.rstrip("/") for item in base_urls if item.strip()]
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=True,
            headers={"User-Agent": "YuqingAgent/0.2 hotlist-collector"},
        )

    async def collect(
        self, platforms: list[str], *, captured_at: str | None = None
    ) -> HotCollectionResult:
        stamp = captured_at or datetime.now().astimezone().isoformat(timespec="seconds")
        statuses: dict[str, str] = {}
        snapshots: list[HotSnapshotInput] = []
        asset_ids: set[str] = set()
        for platform in platforms:
            try:
                items, source_url = await self._fetch_platform(platform)
            except Exception as exc:
                statuses[platform] = f"failed:{type(exc).__name__}"
                continue
            statuses[platform] = "ok"
            source_hash = hashlib.sha256(source_url.encode()).hexdigest()[:10]
            asset = await self.history.register_asset(
                DatasetAssetInput(
                    slug=f"dailyhot-{platform.lower()}-{source_hash}",
                    name=f"DailyHotApi {platform} 热榜",
                    source_url=f"{source_url}/{platform}",
                    license_label="api-code-MIT",
                    upstream_rights_note="接口代码许可不等于平台榜单数据许可；仅缓存公开榜单字段。",
                    redistribution="restricted",
                ),
                record_count=0,
                metadata={
                    "collection_method": "DailyHotApi",
                    "cache_policy": "按演示站 TTL 或用户本地策略清理",
                    "deletion": "删除数据资产或任务时联动清理",
                    "personal_fields": "不采集账号、头像、用户 ID",
                },
            )
            asset_ids.add(asset.id)
            for index, item in enumerate(items, start=1):
                title = str(item.get("title") or item.get("name") or "").strip()
                if not title:
                    continue
                rank = item.get("rank") or item.get("index") or index
                try:
                    rank = int(rank)
                except (TypeError, ValueError):
                    rank = index
                snapshots.append(
                    HotSnapshotInput(
                        asset_id=asset.id,
                        platform=platform,
                        captured_at=stamp,
                        rank=max(1, rank),
                        title=title,
                        heat_value=parse_heat_value(
                            item.get("hot")
                            or item.get("hot_value")
                            or item.get("heat")
                            or item.get("desc")
                        ),
                        url=item.get("url") or item.get("link") or item.get("mobileUrl"),
                        raw={
                            key: item[key]
                            for key in ("category", "type", "label", "hot_tag", "source")
                            if key in item and isinstance(item[key], str | int | float | bool)
                        },
                    )
                )
        inserted = await self.history.record_hot_snapshots(snapshots)
        for asset_id in asset_ids:
            row = await self.history.database.fetch_one(
                "SELECT COUNT(*) AS total FROM hot_snapshot WHERE asset_id=?", (asset_id,)
            )
            await self.history.database.execute_write(
                "UPDATE dataset_asset SET record_count=?,updated_at=? WHERE id=?",
                (int(row["total"] if row else 0), now_iso(), asset_id),
            )
        return HotCollectionResult(inserted=inserted, platforms=statuses)

    async def _fetch_platform(self, platform: str) -> tuple[list[dict[str, Any]], str]:
        if not self.base_urls:
            raise RuntimeError("未配置 DailyHotApi 地址")
        errors: list[str] = []
        for base_url in self.base_urls:
            try:
                response = await self.client.get(f"{base_url}/{platform}")
                response.raise_for_status()
                payload = response.json()
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                if isinstance(data, dict):
                    data = data.get("items") or data.get("list") or []
                if not isinstance(data, list):
                    raise ValueError("热榜响应 data 不是数组")
                return [item for item in data if isinstance(item, dict)], base_url
            except Exception as exc:
                errors.append(f"{base_url}:{type(exc).__name__}")
        raise RuntimeError(";".join(errors))

    async def aclose(self) -> None:
        await self.client.aclose()
