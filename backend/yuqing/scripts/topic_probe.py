from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from yuqing.core.fetch import BuiltinFetchProvider, FetchChain, FirecrawlCloudProvider
from yuqing.core.llm.factory import LLMClientFactory
from yuqing.core.llm.gateway import LLMGateway
from yuqing.core.search.chain import SearchChain
from yuqing.core.search.providers import (
    BochaSearchProvider,
    ExaSearchProvider,
    QianfanSearchProvider,
    SerperSearchProvider,
    TavilySearchProvider,
    ZhipuSearchProvider,
)
from yuqing.core.search.yuanbao import YuanbaoSearchProvider
from yuqing.services.configuration import DEFAULT_SEARCH_PROVIDER_ORDER, ConfigService
from yuqing.services.provider_quota import (
    ProviderQuotaManager,
    QuotaAwareFetchProvider,
    QuotaAwareSearchProvider,
)
from yuqing.services.source_scope import SourceScopeResolver
from yuqing.services.topic_discovery import (
    OpenAITopicQueryPlanner,
    TopicDiscovery,
    TopicDiscoveryRequest,
)
from yuqing.storage.db import Database
from yuqing.storage.models import TaskCreate, TaskRecord

PROVIDERS = {
    "yuanbao": YuanbaoSearchProvider,
    "zhipu": ZhipuSearchProvider,
    "qianfan": QianfanSearchProvider,
    "bocha": BochaSearchProvider,
    "exa": ExaSearchProvider,
    "tavily": TavilySearchProvider,
    "serper": SerperSearchProvider,
}


def parse_args() -> argparse.Namespace:
    load_dotenv(Path(__file__).parents[3] / ".env")
    parser = argparse.ArgumentParser(description="在线探测宽泛主题的证据化事件候选")
    parser.add_argument("topic")
    parser.add_argument("--data-dir", default=os.getenv("YUQING_DATA_DIR", "data"))
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--source-scope", choices=("auto", "domestic", "global"), default="auto")
    parser.add_argument("--languages", default="zh,en", help="逗号分隔的语言允许列表")
    args = parser.parse_args()
    args.languages = TaskCreate(
        event_query=args.topic, source_languages=args.languages.split(",")
    ).source_languages
    args.data_dir = str(Path(args.data_dir).resolve())
    return args


async def main(args: argparse.Namespace) -> None:
    database = Database(Path(args.data_dir) / "yuqing.db")
    await database.initialize()
    environ = await ConfigService(database).resolved_environ()
    order = [name for name in DEFAULT_SEARCH_PROVIDER_ORDER if name in PROVIDERS]
    providers = []
    quotas = ProviderQuotaManager(database, environ=environ)
    for name in order:
        key = environ.get(f"{name.upper()}_API_KEY", "").strip()
        if not key:
            continue
        provider = (
            PROVIDERS[name](key, version=environ.get("YUANBAO_VERSION", "standard"))
            if name == "yuanbao"
            else PROVIDERS[name](key)
        )
        providers.append(
            QuotaAwareSearchProvider(
                provider,
                quotas,
                max_calls_per_task=quotas.yuanbao_task_limit if name == "yuanbao" else None,
            )
        )
    if not providers:
        await database.close()
        raise RuntimeError("未配置任何搜索 Provider")

    builtin = BuiltinFetchProvider(
        allow_proxy_fake_ip=environ.get("YUQING_FETCH_ALLOW_PROXY_FAKE_IP", "false").lower()
        in {"1", "true", "yes", "on"}
    )
    fetch_order = [
        item.strip()
        for item in environ.get("FETCH_PROVIDER_ORDER", "builtin,firecrawl").split(",")
        if item.strip()
    ]
    firecrawl_key = environ.get("FIRECRAWL_API_KEY", "").strip()
    if "firecrawl" in fetch_order and firecrawl_key:
        firecrawl = QuotaAwareFetchProvider(
            FirecrawlCloudProvider(
                api_key=firecrawl_key,
                allow_proxy_fake_ip=environ.get("YUQING_FETCH_ALLOW_PROXY_FAKE_IP", "false").lower()
                in {"1", "true", "yes", "on"},
            ),
            quotas,
        )
        fetcher = FetchChain(builtin, firecrawl)
    else:
        fetcher = FetchChain(builtin)
    llm_factory = LLMClientFactory(environ)
    try:
        gateway = LLMGateway(llm_factory)
        now = datetime.now(UTC).isoformat()
        probe_task = TaskRecord(
            id="probe-" + uuid.uuid4().hex,
            event_query=args.topic,
            request_kind="topic_discovery",
            source_scope=args.source_scope,
            source_languages=args.languages,
            depth="quick",
            status="running",
            outer_round=0,
            tokens_used=0,
            cost_estimate=0,
            created_at=now,
            updated_at=now,
        )
        resolution = await SourceScopeResolver(gateway).resolve(probe_task)
        print(__import__("json").dumps({"source_scope_resolution": resolution}, ensure_ascii=False))
        outcome = await TopicDiscovery(
            SearchChain(providers),
            fetcher,
            planner=OpenAITopicQueryPlanner(gateway),
        ).discover(
            TopicDiscoveryRequest(
                topic=args.topic,
                languages=tuple(args.languages),
                source_scope=resolution["effective_scope"],
                date_from=args.date_from,
                date_to=args.date_to,
                max_search_calls=8,
                max_fetch_calls=8,
            )
        )
        print(outcome.model_dump_json(indent=2))
    finally:
        await llm_factory.aclose()
        await fetcher.aclose()
        for provider in providers:
            await provider.client.aclose()
        await database.close()


def run() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(parse_args()))


if __name__ == "__main__":
    run()
