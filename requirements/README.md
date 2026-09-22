# Lock files

Pinned, resolvable sets of third-party dependencies, one per install context.
CI installs from these instead of resolving `pyproject.toml` at job time, so a
new release on PyPI cannot change what a workflow runs.

| File | Contents |
| --- | --- |
| `base.lock` | core dependencies only |
| `gee.lock` | core + `gee` (Sentinel collection) |
| `noaa.lock` | core + `noaa` (OPeNDAP SST) |
| `transform.lock` | core + `transform` (dbt + DuckDB) |
| `ml.lock` | core + `transform` + `ml` + `ml-tracking` |
| `ci.lock` | core + `transform` + `ml` + `ml-tracking` + `dev` |

They are compiled **universally** (environment markers kept, no platform
assumption) so one file serves Linux runners and local machines alike.

## Regenerating

```sh
uv pip compile --universal --python-version 3.12 --no-header pyproject.toml -o requirements/base.lock
uv pip compile --universal --python-version 3.12 --no-header pyproject.toml --extra gee -o requirements/gee.lock
uv pip compile --universal --python-version 3.12 --no-header pyproject.toml --extra noaa -o requirements/noaa.lock
uv pip compile --universal --python-version 3.12 --no-header pyproject.toml --extra transform -o requirements/transform.lock
uv pip compile --universal --python-version 3.12 --no-header pyproject.toml --extra transform --extra ml --extra ml-tracking -o requirements/ml.lock
uv pip compile --universal --python-version 3.12 --no-header pyproject.toml --extra dev --extra transform --extra ml --extra ml-tracking -o requirements/ci.lock
```

Recompile whenever `pyproject.toml` changes and commit the result in the same
change. `tests/test_workflows.py` fails if a workflow installs from anywhere
other than one of these files, so the two cannot drift apart.
