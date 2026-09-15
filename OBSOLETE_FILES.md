# Obsolete files

Remove these files from the previous release after extracting this ZIP:

- `src/kivyfn/store/devtools/static/assets/index-CoXhw9f8.js`
- `src/kivyfn/store/devtools/static/assets/index-OH06jaIh.css`
- `src/kivyfn/store/devtools/static/assets/index-bIpLHPTm.js`
- `src/kivyfn/store/devtools/static/assets/index-CKjS_z2i.css`
- `tests/state/test_order.py`

From your repository root:

```sh
rm -f \
  src/kivyfn/store/devtools/static/assets/index-CoXhw9f8.js \
  src/kivyfn/store/devtools/static/assets/index-OH06jaIh.css \
  src/kivyfn/store/devtools/static/assets/index-bIpLHPTm.js \
  src/kivyfn/store/devtools/static/assets/index-CKjS_z2i.css \
  tests/state/test_order.py
```

This list covers both previous deliveries; missing files can be ignored.
The replacement test is `tests/state/test_rows_notifications.py`. Current
compiled assets are `index-Bh_b9i04.js` and `index-Cj0mz7BF.css`.
The ZIP extracts directly over the repository root and cannot remove obsolete
files by itself.
