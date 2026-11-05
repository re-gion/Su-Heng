import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from yuqing.core.fetch.builtin import BuiltinFetchProvider
from yuqing.core.fetch.security import UnsafeUrlError, validate_public_url
from yuqing.core.search.base import SearchParams
from yuqing.core.search.yuanbao import YuanbaoSearchProvider
from yuqing.services.configuration import ConfigService
from yuqing.services.provider_quota import ProviderQuotaManager, QuotaAwareSearchProvider
from yuqing.storage.db import Database


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="验证元宝摘要、全文抓取与 SSRF 防线；实际调用计入月限额"
    )
    parser.add_argument("query")
    parser.add_argument("--fetch-first", action="store_true")
    parser.add_argument("--data-dir", default=os.getenv("YUQING_DATA_DIR", "data"))
    args = parser.parse_args()
    data_path = await asyncio.to_thread(Path(args.data_dir).resolve)
    database = Database(data_path / "yuqing.db")
    await database.initialize()
    provider = None
    fetcher = None
    try:
        environ = await ConfigService(database).resolved_environ()
        quotas = ProviderQuotaManager(database, environ=environ)
        provider = QuotaAwareSearchProvider(
            YuanbaoSearchProvider(
                environ.get("YUANBAO_API_KEY", ""),
                version=environ.get("YUANBAO_VERSION", "standard"),
            ),
            quotas,
            max_calls_per_task=quotas.yuanbao_task_limit,
        )
        results = await provider.search(SearchParams(query=args.query, top_k=5))
        if not results:
            raise RuntimeError("元宝没有返回可用网页，尚未通过语义探针")
        for item in results:
            print(item.model_dump_json(exclude={"raw"}))
        if args.fetch_first:
            fetcher = BuiltinFetchProvider()
            fetched = await fetcher.fetch(results[0].url)
            print(f"fetched={fetched.url} chars={len(fetched.content_text)}")
        try:
            validate_public_url("http://127.0.0.1")
        except UnsafeUrlError as exc:
            print(f"ssrf_blocked={exc}")
    finally:
        if provider is not None:
            await provider.client.aclose()
        if fetcher is not None:
            await fetcher.aclose()
        await database.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
