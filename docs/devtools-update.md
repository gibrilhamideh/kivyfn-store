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
- Live requests/s in Activity, using every display filter and a rolling
  one-second arrival window independent of list retention.
- Visible shared recording filters, clear controls for display and recording
  filters, and clearer empty/paused Activity messages.
- Frontend regression tests and updated `docs/devtools.md`.

Changes are confined to frontend code, its tests, bundled browser assets and
documentation. The Python Store, Host, Client, synchronization protocol and
subscription system use their existing implementations. Query results in this
UI are one-shot snapshots; editing is for canonical schema records.

## Verification

The original view/editing update passed TypeScript, ESLint, Prettier,
26 frontend unit tests and 62 Chromium browser tests across direct and Socket.IO.
The request-rate addition passes TypeScript, ESLint, Prettier, 32 unit tests
and 14 Chromium browser checks across Direct and Socket.IO for filtering,
timing, display/recording pauses, reconnects and Activity responses. The release
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

## Activity request rate

The counter shows matching captured requests/s, updates every 250 ms and returns
to zero after one idle second. Each request record (a Host request batch) counts
once. Replies, commits and restored history never inflate the count. It follows
all display filters, so selecting responses or commits shows zero requests/s.
It continues observing while you browse State/History or pause the Activity list.
Paused recording and disconnections display an unavailable marker.

Only traffic captured and received by this browser can be measured. Shared
recording filters are now visible with a clear button; excluded earlier traffic
cannot be recovered. Display filters and recording filters remain independent.
