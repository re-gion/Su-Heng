from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

SourceRole = Literal["authority", "party", "independent", "syndicated", "unknown"]

# 图书馆/机构代理会把真实域名压进主机名（www-scmp-com.libproxy1.nus.edu.sg）。
# 不还原就会把代理主机本身当成一个"独立发布主体"，虚高独立信源数。
PROXY_MARKERS = ("libproxy", "ezproxy", "webvpn", "proxy")

# 两级公共后缀。registrable_domain 依赖它，否则 bbc.co.uk 会被截成 co.uk。
TWO_LEVEL_SUFFIXES = {
    "com.cn",
    "net.cn",
    "org.cn",
    "gov.cn",
    "edu.cn",
    "ac.cn",
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.hk",
    "org.hk",
    "edu.hk",
    "gov.hk",
    "com.tw",
    "org.tw",
    "edu.tw",
    "gov.tw",
    "com.au",
    "net.au",
    "org.au",
    "edu.au",
    "gov.au",
    "com.sg",
    "edu.sg",
    "gov.sg",
    "com.my",
    "edu.my",
    "gov.my",
}


def resolve_proxy_host(host: str) -> str | None:
    """把机构代理主机还原成真实主机；无法可靠还原时返回 None（保守放弃）。"""
    labels = host.split(".")
    for index, label in enumerate(labels):
        if not any(marker in label for marker in PROXY_MARKERS):
            continue
        inner = labels[:index]
        if not inner:
            return None
        candidate = ".".join(inner).replace("-", ".")
        if candidate.startswith("www."):
            candidate = candidate[4:]
        # 还原结果至少要是一个像域名的东西，否则宁可放弃也不猜。
        return candidate if "." in candidate else None
    return None


def registrable_domain(host: str) -> str:
    """退化为可注册域，使同一站点的多个子域归并为同一个发布主体。"""
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


class TierGroup(BaseModel):
    tier: int
    role: SourceRole
    domains: list[str]


class EntityGroup(BaseModel):
    """一个采编主体及其多个站点（05-核心契约 §0.4 的独立性归并单位）。"""

    name: str
    domains: list[str]


class TierConfig(BaseModel):
    groups: list[TierGroup]
    # deny 先于 groups 匹配：聚合/转载平台无论 tier 多高都不算独立采编主体。
    deny: list[TierGroup] = []
    entities: list[EntityGroup] = []
    default_tier: int = 4
    default_role: SourceRole = "unknown"


class SourceTierClassifier:
    def __init__(self, config: TierConfig):
        self.config = config

    @classmethod
    def from_yaml(cls, path: Path) -> SourceTierClassifier:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(TierConfig.model_validate(raw))

    @classmethod
    def bundled(cls) -> SourceTierClassifier:
        return cls.from_yaml(Path(__file__).with_name("source_tiers.yaml"))

    @staticmethod
    def _normalize(domain: str) -> str:
        return domain.lower().strip(".")

    @staticmethod
    def _matches(host: str, domains: list[str]) -> bool:
        return any(host == item or host.endswith(f".{item}") for item in domains)

    def classify(self, domain: str) -> tuple[int, SourceRole, bool]:
        host = self._normalize(domain)
        host = resolve_proxy_host(host) or host
        for group in self.config.deny:
            if self._matches(host, group.domains):
                return group.tier, group.role, True
        for group in self.config.groups:
            if self._matches(host, group.domains):
                return group.tier, group.role, True
        return self.config.default_tier, self.config.default_role, False

    def entity_for(self, host: str) -> str | None:
        """按最长域名后缀匹配采编主体；无名可归时返回 None。"""
        host = self._normalize(host)
        best: tuple[int, str] | None = None
        for group in self.config.entities:
            for item in group.domains:
                item = item.lower()
                if host == item or host.endswith(f".{item}"):
                    if best is None or len(item) > best[0]:
                        best = (len(item), group.name)
        return best[1] if best else None

    def canonical_publisher(self, host: str, source_name: str | None = None) -> str:
        """归并到"原始采编主体"（05-核心契约 §0.4）。

        写入证据与核验分组共用同一实现：只在写入时归一会让历史数据残留旧值，
        只在读取时归一则让报告展示的主体与计数主体不一致。
        """
        name = (source_name or "").strip()
        # 有些 provider 把域名塞进 source_name，那不是采编主体名，仍要走归并。
        if name and not looks_like_host(name):
            return name
        target = name or self._normalize(host)
        resolved = resolve_proxy_host(target) or target
        return self.entity_for(resolved) or registrable_domain(resolved)


@lru_cache(maxsize=1)
def bundled_classifier() -> SourceTierClassifier:
    """读取侧复用的分级器（核验分组需要，但核验服务本身不持有一个实例）。"""
    return SourceTierClassifier.bundled()


_HOST_LIKE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def looks_like_host(value: str) -> bool:
    return bool(_HOST_LIKE.match(value))
