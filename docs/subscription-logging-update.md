# Subscription failures and shared logging

This update applies over the typed-template version of kivyfn-store. The ZIP
contains complete replacements for changed files and the new files, using
repository-relative paths. It does not include a wrapper directory or generated
files. Extract it from your repository root:

```bash
unzip -o ~/Downloads/kivyfn-store-subscription-logging-update.zip -d .
```

## Changes

- Restore the `kivyfn` runtime dependency and use
  `from kivyfn.logging import get_logger` throughout Store's runtime modules.
  No minimum version is invented. Local uv sources use editable `../kivyfn`
  and `../kivy-stubs` checkouts; `kivy-stubs` is a development dependency.
- Treat a valid subscription rejection as `StoreSubscriptionError`, preserving
  the structured `StoreFailure` in its `failure` attribute. Connection health
  and subscription readiness remain separate. Healthy subscriptions, mutations
  and queries continue on the existing connection.
- Retain rejection state on each desired subscription. A subsequent `connect()`
  retries inactive subscriptions without replacing healthy bindings. Releasing
  a failed source removes its synchronization requirement.
- Ignore failed replies belonging to released/replaced subscriptions. Preserve
  existing successful snapshot adoption during release/recreate.
- Recheck pending subscriptions after callbacks before completing synchronization.
  A callback that creates another State target cannot prematurely settle the
  connection's synchronization waiter.
- Log rejected unsubscribe requests without disconnecting healthy peers. The
  source is removed locally; a rejected Host binding remains until disconnect.

## Tests

The new `tests/client/test_subscription_failures.py` contains 30 parametrized
cases across Direct and Socket.IO. They cover initial and incremental unknown
Single/Many/Query sources, retries, healthy binding identity, mixed mutation and
query batches, rejected unsubscribe, late failures after release/recreate,
cancelled connect waiters, recovery from all subscriptions failing, and
subscriptions created by synchronization callbacks. Ordering tests use explicit
asyncio events.

The old malformed-response test no longer classifies an ordinary subscription
rejection as protocol corruption. Wrong Store-instance replies and missing
snapshots still exercise disconnection.

## Verification and limitation

The actual `kivyfn` distribution and `kivyfn.logging` source are not present in
the validation environment. The Client corrections were therefore verified
before replacing the standard-library logger imports. No fake runtime package,
fallback import, or suppressed missing-import diagnostics were introduced.
Adding the user's local source paths does not make those sibling repositories
available in this validation environment; shared logger integration and local
dependency resolution still need verification on the user's PC.

| Check | Result |
| --- | --- |
| Full pytest, Python 3.11, before shared logger imports | 1,108 passed |
| Full pytest, Python 3.12, before shared logger imports | 1,108 passed |
| Pyright, before shared logger imports | 0 errors, 0 warnings |
| Final pytest | Collection blocked by missing `kivyfn.logging` |
| Final Pyright | 16 missing-import errors for `kivyfn.logging`; no other diagnostics |
| Final Ruff lint | Passed |
| Ruff formatting for source, tests, README and docs | Passed |
| Wheel build with installed setuptools | Passed; metadata declares `kivyfn` |

The separate future-feature `PLAN.md` is not included or changed by this update.
Its existing code-example formatting is outside the formatting result above.

After making the real `kivyfn` package available in the project environment,
run the configured checks to verify shared logger integration as well:

```bash
uv sync --group dev
uv run pytest
uv run pyright
uv run ruff check .
uv run ruff format --check .
```

History, undo/redo, devtools, transport protocol shapes, and automatic transaction
retry policy are unchanged. This correction does not implement the planned
history features.
