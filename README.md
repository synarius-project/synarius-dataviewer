# synarius-dataviewer

Data viewing and inspection tooling built on **synarius-core** (no GUI in core; this package can add viewers, CLIs, or small UIs).

## Install (development)

**CI / clones** resolve `synarius-core` via the Git URL pinned in `pyproject.toml` (same pattern as `synarius-studio`).

```bash
pip install -e .
```

**Local monorepo** (sibling checkout of `synarius-core`):

```bash
cd ../synarius-core && pip install -e .
cd ../synarius-dataviewer && pip install -e .
```

Console entry point:

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

- `src/synarius_dataviewer/` — package code
- `docs/` — Sphinx + sphinx-needs + zerovm theme
- `synarius_dataviewer.spec` — PyInstaller one-file spec for the Windows installer job
- `DISCLAIMER.txt` — license text shown in the MSI

## Docs (local)

```bash
pip install -e ".[docs]"
sphinx-build -b html docs docs/_build/html
```
