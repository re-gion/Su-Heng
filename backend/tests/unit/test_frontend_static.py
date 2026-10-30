from pathlib import Path

from yuqing.app.main import _resolve_frontend_dist


def test_resolves_frontend_dist_from_runtime_root_for_installed_package(tmp_path: Path):
    runtime_root = tmp_path / "image-root"
    frontend_dist = runtime_root / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("Docker frontend", encoding="utf-8")

    installed_main = (
        tmp_path
        / "usr"
        / "local"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "yuqing"
        / "app"
        / "main.py"
    )

    assert _resolve_frontend_dist(runtime_root, installed_main) == frontend_dist


def test_resolves_frontend_dist_from_source_tree_for_local_development(tmp_path: Path):
    project_root = tmp_path / "project"
    frontend_dist = project_root / "frontend" / "dist"
    frontend_dist.mkdir(parents=True)
    (frontend_dist / "index.html").write_text("Local frontend", encoding="utf-8")
    source_main = project_root / "backend" / "yuqing" / "app" / "main.py"

    assert _resolve_frontend_dist(project_root / "backend", source_main) == frontend_dist
