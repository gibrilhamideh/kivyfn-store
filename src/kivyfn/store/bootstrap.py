# kivyfn/store/bootstrap.py
from __future__ import annotations

from kivyfn.runtime.context import FnBootstrapContext
from kivyfn.store import store


_bootstrapped = False


def bootstrap(ctx: FnBootstrapContext) -> None:
    global _bootstrapped

    # Runtime lifecycle hook.
    # This should be registered for the current runtime.
    ctx.on_shutdown(store.stop)

    if _bootstrapped:
        return

    _bootstrapped = True

    # Register store widgets / bindings / factory classes.
    from .factory import register

    register()
