# kivyfn/store/bootstrap.py
from __future__ import annotations
from kivyfn.runtime.context import FnBootstrapContext
_bootstrapped = False


def bootstrap(ctx: FnBootstrapContext) -> None:
    global _bootstrapped

    if _bootstrapped:
        return

    _bootstrapped = True

    # Register core widgets
    from .factory import register
    register()
