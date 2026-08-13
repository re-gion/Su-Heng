from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field


class CollectedComment(BaseModel):
    native_id: str
    parent_native_id: str | None = None
    text: str = Field(min_length=1, max_length=4000)
    published_at: str | None = None
    like_count: int | None = Field(default=None, ge=0)
    reply_count: int | None = Field(default=None, ge=0)
    depth: int = Field(default=0, ge=0, le=3)


@dataclass(frozen=True)
class AdapterDefinition:
    platform: str
    hosts: tuple[str, ...]
    path_pattern: re.Pattern[str]
    home_url: str
    login_markers: tuple[str, ...]


class SocialPlatformAdapter:
    """平台 URL 与脱敏评论的确定性边界；不持有 Cookie，也不绕过访问控制。"""

    def __init__(self, definition: AdapterDefinition):
        self.definition = definition
        self.platform = definition.platform

    def matches(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return any(
            host == item or host.endswith(f".{item}") for item in self.definition.hosts
        ) and bool(self.definition.path_pattern.search(parts.path))

    def canonicalize(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not self.matches(url):
            raise ValueError(f"不支持的 {self.platform} 帖子 URL")
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(("https", host, path, "", ""))

    def looks_logged_in(self, html: str) -> bool:
        lowered = html.lower()
        return any(marker.lower() in lowered for marker in self.definition.login_markers)

    def extract_comments(self, payload: Any, *, limit: int) -> list[CollectedComment]:
        """从浏览器已收到的响应中提取常见评论形态，不保留作者字段。"""

        found: list[CollectedComment] = []
        seen: set[str] = set()
        root_by_id: dict[str, str] = {}
        replies_per_root: dict[str, int] = {}

        def number(value: Any) -> int | None:
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
            if isinstance(value, str) and value.isdigit():
                return int(value)
            return None

        def visit(value: Any, parent: str | None = None, depth: int = 0) -> None:
            if len(found) >= limit:
                return
            if isinstance(value, list):
                for item in value:
                    visit(item, parent, depth)
                return
            if not isinstance(value, dict):
                return
            raw_text = value.get("content") or value.get("text") or value.get("message")
            if isinstance(raw_text, dict):
                raw_text = (
                    raw_text.get("message") or raw_text.get("text") or raw_text.get("content")
                )
            text = str(raw_text or "").strip()
            raw_id = (
                value.get("comment_id") or value.get("id") or value.get("cid") or value.get("rpid")
            )
            if text and raw_id is not None:
                native_id = str(raw_id)
                if native_id not in seen:
                    root_id = root_by_id.get(parent or "", parent or native_id)
                    if parent is not None and replies_per_root.get(root_id, 0) >= 3:
                        return
                    seen.add(native_id)
                    root_by_id[native_id] = root_id
                    if parent is not None:
                        replies_per_root[root_id] = replies_per_root.get(root_id, 0) + 1
                    found.append(
                        CollectedComment(
                            native_id=native_id,
                            parent_native_id=parent,
                            text=text[:4000],
                            published_at=str(
                                value.get("created_at")
                                or value.get("create_time")
                                or value.get("ctime")
                                or ""
                            )
                            or None,
                            like_count=number(
                                value.get("like_count")
                                or value.get("digg_count")
                                or value.get("like")
                            ),
                            reply_count=number(
                                value.get("reply_count")
                                or value.get("sub_comment_count")
                                or value.get("rcount")
                            ),
                            depth=min(depth, 3),
                        )
                    )
                    parent = native_id
                    depth = min(depth + 1, 3)
            for key, child in value.items():
                if key.lower() in {
                    "comments",
                    "comment_list",
                    "replies",
                    "reply_comment",
                    "sub_comments",
                    "sub_comment_list",
                    "children",
                    "data",
                    "items",
                    "list",
                }:
                    visit(child, parent, depth)

        visit(payload)
        return found[:limit]

    @staticmethod
    def task_hash(task_id: str, source_url: str, native_id: str) -> str:
        return hashlib.sha256(f"{task_id}:{source_url}:{native_id}".encode()).hexdigest()[:24]


def _definition(
    platform: str,
    hosts: tuple[str, ...],
    pattern: str,
    home_url: str,
    markers: tuple[str, ...],
) -> SocialPlatformAdapter:
    return SocialPlatformAdapter(
        AdapterDefinition(platform, hosts, re.compile(pattern), home_url, markers)
    )


PLATFORM_ADAPTERS = (
    _definition(
        "weibo",
        ("weibo.com",),
        r"/(?:\d+|status)/[A-Za-z0-9]+",
        "https://weibo.com",
        ("退出登录", "我的主页"),
    ),
    _definition(
        "bilibili",
        ("bilibili.com", "b23.tv"),
        r"/(?:video/)?(?:BV|av)[A-Za-z0-9]+",
        "https://www.bilibili.com",
        ("退出登录", "个人中心"),
    ),
    _definition(
        "zhihu",
        ("zhihu.com",),
        r"/(?:question|answer)/\d+",
        "https://www.zhihu.com",
        ("退出", "创作中心"),
    ),
    _definition(
        "xiaohongshu",
        ("xiaohongshu.com", "xhslink.com"),
        r"/(?:explore|discovery/item)/[A-Za-z0-9]+",
        "https://www.xiaohongshu.com",
        ("我", "发布"),
    ),
    _definition(
        "douyin",
        ("douyin.com",),
        r"/(?:video|note)/\d+",
        "https://www.douyin.com",
        ("退出登录", "我的"),
    ),
    _definition(
        "kuaishou",
        ("kuaishou.com",),
        r"/(?:short-video|f)/[A-Za-z0-9]+",
        "https://www.kuaishou.com",
        ("退出登录", "个人主页"),
    ),
    _definition(
        "tieba", ("tieba.baidu.com",), r"/p/\d+", "https://tieba.baidu.com", ("退出", "我的i贴吧")
    ),
)


def adapter_for_url(url: str) -> SocialPlatformAdapter:
    for adapter in PLATFORM_ADAPTERS:
        if adapter.matches(url):
            return adapter
    raise ValueError("URL 不属于已支持的七个平台帖子")
