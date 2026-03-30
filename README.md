# Synarius Apps

![Synarius title image](docs/_static/synarius-title.png)

Repository **synarius-apps** ships **Synarius Dataviewer** (MDI data inspection UI) plus shared GUI building blocks under **`synariustools`** (reusable Qt scope/legend plot widget). Everything builds on **synarius-core**.

**Repository:** [synarius-project/synarius-apps](https://github.com/synarius-project/synarius-apps)

**Python distribution** (install name from `pyproject.toml`): `synarius-apps`

**Note (local checkout):** If Windows still has a folder named `synarius-dataviewer`, you can work through a junction `synarius-apps` that points to it (standard in this workspace until nothing locks the path). For a **physical** rename: close IDEs and terminals using that folder, remove the junction (`rmdir synarius-apps` — only removes the link), then rename the directory to `synarius-apps`.

## Install (development)

**CI / clones** resolve `synarius-core` via the Git URL pinned in `pyproject.toml`. Measurement file I/O is implemented in **synarius-core** (`synarius_core.io`); this app still declares `pandas` / `pyarrow` / `asammdf` / `numpy` so installers resolve one consistent stack.

```bash
pip install -e .
```

**Local monorepo** (sibling checkout of `synarius-core`):

```bash
cd ../synarius-core && pip install -e ".[timeseries]"
cd ../synarius-apps && pip install -e .
```

If `pip` reports a conflict between the pinned Git revision of `synarius-core` and your local editable core, install the app with `pip install -e . --no-deps` after `pip install -e "../synarius-core[timeseries]"`, then add missing deps manually.

Console entry point for the Dataviewer application (unchanged name for compatibility):

```bash
synarius-dataviewer
```

## Branches and automation

| Branch | Workflows |
|--------|-----------|
| `main` | **CI** — Ruff + pytest |
| `dev`  | **Docs** — Sphinx build, deploy to GitHub Pages (repository settings must enable Pages from “GitHub Actions”) |
| Tag `vX.Y.Z` | **Release** — sdist/wheel artifact job, Windows PyInstaller `.exe`, WiX **MSI**, GitHub Release with the MSI (same layout as `synarius-studio`) |

Create a release (example):

```bash
git tag v0.0.1
git push origin v0.0.1
```

## Layout

- `src/synarius_dataviewer/` — Dataviewer application package (console script `synarius-dataviewer`)
- `src/synariustools/tools/plotwidget/` — reusable Qt plot widget (`DataViewerWidget`, `create_data_viewer`, …)
- `docs/` — Sphinx + sphinx-needs + zerovm theme
- `synarius_dataviewer.spec` — PyInstaller one-file spec for the Windows installer job
- `DISCLAIMER.txt` — license text shown in the MSI

### Plot widget (standalone-style)

```python
from synariustools.tools.plotwidget import create_data_viewer, PlotViewerMode

# embedded=True: toolbar + widget in a small host (default, same idea as the MDI child)
viewer = create_data_viewer(my_callable_or_data_source, parent=None, embedded=True)

# Static mode with legend hidden at startup:
viewer = create_data_viewer(
    my_callable_or_data_source,
    mode=PlotViewerMode.static(legend_visible_by_default=False),
)
```

Imports from `synarius_dataviewer.widgets.data_viewer` remain valid shims to the same implementation.

## Docs (local)

```bash
pip install -e ".[docs]"
sphinx-build -b html docs docs/_build/html
```
