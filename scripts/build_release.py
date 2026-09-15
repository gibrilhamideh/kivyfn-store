"""Build offline devtools assets and the Python release distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path("src/kivyfn/store/devtools/static")
FRONTEND = Path("devtools/frontend")
MANIFEST = "build-manifest.json"


def source_digest(root: Path = ROOT) -> str:
    """Fingerprint the frontend source and its reproducible build inputs."""
    frontend = root / FRONTEND
    paths = [
        path
        for path in frontend.iterdir()
        if path.is_file()
        and path.suffix in {".json", ".js", ".mjs", ".ts", ".html"}
    ]
    for directory in (frontend / "src", frontend / "public"):
        if directory.exists():
            paths.extend(
                path for path in directory.rglob("*") if path.is_file()
            )
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(frontend).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def asset_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != MANIFEST
    }


def verify_assets(root: Path = ROOT, *, source: bool = False) -> None:
    """Check bundled bytes without invoking Node or importing the package."""
    directory = root / STATIC
    manifest_path = directory / MANIFEST
    if not (directory / "index.html").is_file() or not manifest_path.is_file():
        raise RuntimeError(
            "Compiled devtools assets are missing. Run "
            "python scripts/build_release.py --assets-only, or import "
            "matching prebuilt assets with --from-wheel."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files")
    if not files or files != asset_hashes(directory):
        raise RuntimeError(
            "Compiled devtools assets are incomplete or changed"
        )
    if source and manifest.get("source_sha256") != source_digest(root):
        raise RuntimeError("Compiled devtools assets are stale; rebuild them")


def stamp_assets(root: Path = ROOT) -> None:
    directory = root / STATIC
    files = asset_hashes(directory)
    if "index.html" not in files or not any(
        name.endswith(".js") for name in files
    ):
        raise RuntimeError("Frontend build did not produce index.html and JS")
    manifest = {"source_sha256": source_digest(root), "files": files}
    (directory / MANIFEST).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    verify_assets(root, source=True)


def import_assets(wheel: Path, root: Path = ROOT) -> None:
    """Copy matching release assets for Python-only development."""
    prefix = "kivyfn/store/devtools/static/"
    with TemporaryDirectory(prefix="kivyfn-assets-") as temporary:
        staging = Path(temporary)
        destination = staging / STATIC
        destination.mkdir(parents=True)
        with ZipFile(wheel) as archive:
            members = [
                info
                for info in archive.infolist()
                if info.filename.startswith(prefix) and not info.is_dir()
            ]
            if sum(info.file_size for info in members) > 50 * 1024 * 1024:
                raise ValueError("Prebuilt assets exceed the 50 MiB limit")
            targets = []
            for info in members:
                relative = PurePosixPath(info.filename[len(prefix) :])
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in str(relative)
                    or any(
                        PureWindowsPath(part).drive for part in relative.parts
                    )
                ):
                    raise ValueError("Invalid prebuilt asset path")
                targets.append((info, destination.joinpath(*relative.parts)))
            for info, target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
        verify_assets(staging)
        manifest = json.loads((destination / MANIFEST).read_text())
        if manifest["source_sha256"] != source_digest(root):
            raise RuntimeError(
                "Wheel assets do not match this frontend source"
            )
        current = root / STATIC
        if current.exists():
            shutil.rmtree(current)
        shutil.copytree(destination, current)


def run(*command: str, cwd: Path = ROOT) -> None:
    print("Running:", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def check_project() -> None:
    for arguments in (
        ("pytest",),
        ("pyright",),
        ("ruff", "check", "."),
        ("ruff", "format", "--check", "."),
    ):
        run(sys.executable, "-m", *arguments)
    for name in ("typecheck", "lint", "format:check", "test", "test:browser"):
        run("npm", "run", name, cwd=ROOT / FRONTEND)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--assets-only", action="store_true")
    actions.add_argument("--check-assets", action="store_true")
    actions.add_argument("--stamp-assets", action="store_true")
    actions.add_argument("--from-wheel", type=Path)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--checks", action="store_true")
    parser.add_argument("--outdir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.check_assets:
        verify_assets(source=True)
        return
    if args.stamp_assets:
        stamp_assets()
        return
    if args.from_wheel:
        import_assets(args.from_wheel)
        return
    if not args.skip_install:
        run("npm", "ci", cwd=ROOT / FRONTEND)
    run("npm", "run", "build", cwd=ROOT / FRONTEND)
    stamp_assets()
    if args.checks:
        check_project()
    if not args.assets_only:
        run(sys.executable, "-m", "build", "--outdir", str(args.outdir))


if __name__ == "__main__":
    main()
