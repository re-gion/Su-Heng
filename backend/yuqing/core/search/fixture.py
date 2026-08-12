from yuqing.core.search.base import SearchParams, SearchResult


class FixtureSearchProvider:
    name = "fixture"
    capabilities = {"freshness", "domain_filter", "publish_time"}

    def __init__(self, results: list[SearchResult]):
        self.results = results
        self.calls = 0

    async def search(self, params: SearchParams) -> list[SearchResult]:
        self.calls += 1
        return self.results[: params.top_k]
