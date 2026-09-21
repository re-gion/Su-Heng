from pathlib import Path

import pytest


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runtime"
    path.mkdir()
    return path


@pytest.fixture
def claim_limits() -> dict[str, int]:
    """`storage.add_claim` 的上限由调用方注入（storage 读不到配置系统）。

    多数用例并不在验证预算行为，给一组宽松值即可；验证上限本身的用例
    应当显式传自己的数值。
    """
    return {"max_claims": 10_000, "max_evidence_per_claim": 6}
