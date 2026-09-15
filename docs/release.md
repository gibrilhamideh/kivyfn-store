# Building and verifying a release

The wheel and source distribution contain the compiled devtools application.
Installing either distribution requires Python and the normal runtime
dependencies. It does not run npm, require Node, or fetch browser assets.

Development uses the real `kivyfn` and `kivy-stubs` sibling repositories named
in `pyproject.toml`. Keep those repositories beside `kivyfn-store`. Logging
substitutes and locally invented type stubs do not qualify as release checks.

## Source checkout

Generated static assets are excluded from source commits. With Node and npm
installed, prepare them before the Python development install:

```bash
python scripts/build_release.py --assets-only
uv sync --locked
```

The script runs `npm ci`, builds the React application, and records the source
fingerprint and compiled file hashes. Build inputs include the committed npm
lockfile. A missing or changed compiled file fails Python distribution builds.
Source freshness is checked during release preparation, not during installation
of an already prepared source distribution.

For Python-only development, import matching assets from an existing release:

```bash
python scripts/build_release.py --from-wheel /path/to/kivyfn_store.whl
uv sync --locked
```

The helper accepts only assets matching the local frontend fingerprint. It
does not replace application source files. A mismatch requires a frontend
build or a matching release wheel.

## Complete checks

Install the Playwright browser required by the frontend test configuration once:

```bash
cd devtools/frontend
npm ci
npx playwright install chromium
cd ../..
```

Run all Python and frontend checks, real-backend browser tests, and build both
Python distributions with one command:

```bash
uv run --with build python scripts/build_release.py --checks
```

The Python suite uses real Direct and Socket.IO transports for shared integration
cases. Frontend browser tests start a real Python Host and devtools server.
Missing dependencies or browser binaries are failures, not silently skipped
release gates. Preserve the output of each command in the release report.

Verify the resulting distributions from outside the source checkout:

```bash
uv run --with pip --with build python scripts/check_install.py dist
```

This installs the wheel and sdist separately into temporary locations, using the
actual runtime dependencies already installed in the verification interpreter.
Node and npm are removed from the subprocess search path. Each installation
starts the real Host and devtools listener, fetches packaged HTML/JS/CSS, and
exercises transactions and undo/redo over Direct and Socket.IO, plus real
browser HTTP commands and diagnostic WebSocket delivery. No repository
working-directory resource paths are used.

## Repository overlay archive

A delivery ZIP contains repository-relative paths without an enclosing folder:
`src/`, `tests/`, `docs/`, `scripts/`, `devtools/frontend/`, and root configuration.
It includes the frontend lockfile and compiled `devtools/static/` assets even
though those assets are ignored in normal source commits.

Exclude Git metadata, virtual environments, `node_modules`, caches, temporary
build directories, browser reports, and test results. List any obsolete files
explicitly in the release report: unpacking an overlay ZIP does not remove them.
