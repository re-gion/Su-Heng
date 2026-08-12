import argparse
import asyncio
import os

from dotenv import load_dotenv

from yuqing.core.fetch.builtin import BuiltinFetchProvider
from yuqing.core.fetch.security import UnsafeUrlError, validate_public_url
from yuqing.core.search.base import SearchParams
from yuqing.core.search.langsearch import LangSearchProvider


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="验证 LangSearch、全文抓取与 SSRF 防线")
    parser.add_argument("query")
    parser.add_argument("--fetch-first", action="store_true")
    args = parser.parse_args()

    provider = LangSearchProvider(os.getenv("LANGSEARCH_API_KEY", ""))
    results = await provider.search(SearchParams(query=args.query, top_k=5))
    for item in results:
        print(item.model_dump_json(exclude={"raw"}))

    if args.fetch_first and results:
        fetched = await BuiltinFetchProvider().fetch(results[0].url)
        print(f"fetched={fetched.url} chars={len(fetched.content_text)}")

    try:
        validate_public_url("http://127.0.0.1")
    except UnsafeUrlError as exc:
        print(f"ssrf_blocked={exc}")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
