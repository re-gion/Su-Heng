"""网页抓取适配器。"""

from yuqing.core.fetch.base import FetchError, FetchProvider, FetchResult
from yuqing.core.fetch.builtin import BuiltinFetchProvider
from yuqing.core.fetch.chain import FetchChain
from yuqing.core.fetch.firecrawl import (
    FirecrawlCloudProvider,
    FirecrawlFetchProvider,
    FirecrawlProvider,
)

__all__ = [
    "BuiltinFetchProvider",
    "FetchChain",
    "FetchError",
    "FetchProvider",
    "FetchResult",
    "FirecrawlCloudProvider",
    "FirecrawlFetchProvider",
    "FirecrawlProvider",
]
