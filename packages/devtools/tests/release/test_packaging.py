"""Release assets must be complete, current, and installable without Node."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from runpy import run_path
from zipfile import ZipFile

import pytest

BUILD = run_path(
    str(Path(__file__).resolve().parents[2] / "scripts" / "build_release.py")
)
STATIC = Path("src/kivyfn/store/devtools/static")


def prepared_tree(path: Path) -> Path:
    frontend = path / "devtools/frontend/src"
    frontend.mkdir(parents=True)
    (frontend / "App.tsx").write_text("export const title = 'Store';\n")
    (frontend.parent / "package.json").write_text('{"name":"devtools"}')
    assets = path / STATIC
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text(
        '<script type="module" src="/assets/app.js"></script>'
    )
    (assets / "assets/app.js").write_text("document.title = 'Store';\n")
    BUILD["stamp_assets"](path)
    return path


def test_missing_assets_fail_before_distribution_build(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Compiled devtools assets"):
        BUILD["verify_assets"](tmp_path)


@pytest.mark.parametrize("change", ["missing", "modified", "extra"])
def test_distribution_rejects_incomplete_or_modified_assets(
    tmp_path: Path, change: str
) -> None:
    root = prepared_tree(tmp_path)
    javascript = root / STATIC / "assets/app.js"
    if change == "missing":
        javascript.unlink()
    elif change == "modified":
        javascript.write_text("changed")
    else:
        (javascript.parent / "stale.js").write_text("old build")
    with pytest.raises(RuntimeError, match="incomplete or changed"):
        BUILD["verify_assets"](root)


def test_source_freshness_is_a_release_check_not_an_install_requirement(
    tmp_path: Path,
) -> None:
    root = prepared_tree(tmp_path)
    source = root / "devtools/frontend/src/App.tsx"
    source.write_text("export const title = 'New title';\n")
    BUILD["verify_assets"](root)
    with pytest.raises(RuntimeError, match="stale"):
        BUILD["verify_assets"](root, source=True)


def test_prebuilt_assets_require_matching_source(tmp_path: Path) -> None:
    release = prepared_tree(tmp_path / "release")
    checkout = prepared_tree(tmp_path / "checkout")
    wheel = tmp_path / "release.whl"
    with ZipFile(wheel, "w") as archive:
        for path in (release / STATIC).rglob("*"):
            if path.is_file():
                relative = path.relative_to(release / "src")
                archive.write(path, relative.as_posix())
    BUILD["import_assets"](wheel, checkout)
    BUILD["verify_assets"](checkout, source=True)
    (checkout / "devtools/frontend/src/App.tsx").write_text("changed")
    with pytest.raises(RuntimeError, match="do not match"):
        BUILD["import_assets"](wheel, checkout)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/escape",
        r"..\escape",
        r"assets\..\..\escape",
        r"\escape",
        r"\\host\share\escape",
        "C:/escape",
        r"C:\escape",
        "C:escape",
        "assets/C:escape",
    ],
)
def test_wheel_asset_paths_reject_before_any_extraction_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    root = prepared_tree(tmp_path / "checkout")
    wheel = tmp_path / "bad.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("kivyfn/store/devtools/static/valid.js", "valid")
        archive.writestr("kivyfn/store/devtools/static/" + name, "bad")

    def unexpected_write(path: Path, data: bytes) -> int:
        pytest.fail(f"Asset was written before path rejection: {path}")

    monkeypatch.setattr(Path, "write_bytes", unexpected_write)
    with pytest.raises(ValueError, match="asset path"):
        BUILD["import_assets"](wheel, root)
    BUILD["verify_assets"](root, source=True)


def test_nested_drive_component_can_escape_windows_staging() -> None:
    staging = PureWindowsPath("D:/staging/static")
    escaped = staging.joinpath("assets", "C:escape")
    assert not escaped.is_relative_to(staging)


def test_manifest_records_every_compiled_file(tmp_path: Path) -> None:
    root = prepared_tree(tmp_path)
    manifest = json.loads((root / STATIC / "build-manifest.json").read_text())
    assert set(manifest["files"]) == {"index.html", "assets/app.js"}
    assert manifest["source_sha256"] == BUILD["source_digest"](root)
