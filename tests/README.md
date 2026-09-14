# KivyFN Store tests

The suite is split by behavior so failures identify the broken layer quickly.

- `tests/store/`: Store transaction orchestration.
- `tests/integration/test_store.py`: real StoreClient -> Socket.IO -> StoreHost -> Store.
- `tests/integration/state/`: complete reactive State path.
- `tests/schemas/`: reusable realistic schemas, trigger, foreign key, and persistence.

Each integration test receives its own directory under:

`~/Data/kivyfn-store/tests/`

The directory is cleaned before that test starts and left on disk afterward for inspection.

Recommended commands:

```bash
pyright
pytest tests -o log_cli=true -o log_cli_level=DEBUG
```

For the State suite only:

```bash
pytest tests/integration/state -o log_cli=true -o log_cli_level=DEBUG
```
