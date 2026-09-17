# Devtools UI update

Extract the archive while your shell is in the repository root:

```bash
unzip -o ~/Downloads/kivyfn-store-devtools-update.zip
```

Restart the application serving devtools and refresh its browser page. Compiled
browser assets are included, so applying this update does not require Node.

## Included

- Structured and expandable Dictionary views for State, History and Activity.
- Collection tables with search, sorting, visible columns, paging and row details;
  single schemas show all fields together.
- Structured local drafts with typed inputs, row insertion/removal, review of
  changed fields, and one explicit transaction submission.
- Preserved drafts while browsing, original/current/proposed comparisons for
  stale edits, and the existing guarded advanced-action editor.
- Connected request, response, recorded changes and historical preview panels;
  field-to-Activity navigation and related-row navigation.
- Query result tables and dictionaries, with explicit snapshot timing.
- Frontend regression tests and updated `docs/devtools.md`.

Changes are confined to frontend code, its tests, bundled browser assets and
documentation. The Python Store, Host, Client, synchronization protocol and
subscription system use their existing implementations. Query results in this
UI are one-shot snapshots; editing is for canonical schema records.

## Verification

Validation passed: TypeScript, ESLint, Prettier, 26 frontend unit tests, and
62 Chromium browser tests across direct and Socket.IO backends. The release
asset verifier and extraction-over-baseline checks also passed. Browser checks exercise existing synchronization guarantees as
well as structured draft editing, stale comparisons, row selection, query
results and historical stepping. Desktop and narrow layouts were inspected.

The local browser test environment uses real Store/Host/Client implementations
and a temporary standard-library `get_logger` adapter because the separate
`kivyfn.logging` package was not attached. That adapter is not included in this
archive. The Python core suite was not rerun for this UI-only update.

To run the frontend checks in your normal development environment:

```bash
cd devtools/frontend
npm ci
npm run typecheck
npm run lint
npm run format:check
npm test
DEVTOOLS_TEST_PYTHON="$(pwd)/../../.venv/bin/python" npm run test:browser
```

Playwright's Chromium installation is required for browser tests. Install it
with `npx playwright install chromium` if it is not already present.

To verify the included assets, from the repository root:

```bash
python scripts/build_release.py --check-assets
```

The archive retains the previous bundled asset filenames alongside the current
build so extraction over the supplied repository needs no deletion step. Only
the current assets are referenced by `index.html`. The normal release build
cleans and regenerates this directory when you next build the frontend.
