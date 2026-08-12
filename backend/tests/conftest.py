from pathlib import Path

import pytest


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Path:
    path = tmp_path / "runtime"
    path.mkdir()
    return path
