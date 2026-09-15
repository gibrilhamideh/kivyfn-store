"""Require bundled browser assets without a Node installation at build time."""

from pathlib import Path
from runpy import run_path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).resolve().parent


def check_assets() -> None:
    namespace = run_path(str(ROOT / "scripts" / "build_release.py"))
    namespace["verify_assets"](ROOT)


class BuildPython(build_py):
    def run(self) -> None:
        check_assets()
        super().run()


class SourceDistribution(sdist):
    def run(self) -> None:
        check_assets()
        super().run()


setup(cmdclass={"build_py": BuildPython, "sdist": SourceDistribution})
