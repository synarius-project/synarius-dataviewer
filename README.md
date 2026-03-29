# synarius-dataviewer

Python package for viewing and inspecting Synarius-related data. It depends on **synarius-core** (no GUI in the core library; this package can add viewers, CLIs, or small UIs later).

## Monorepo setup (this folder next to `synarius-core`)

```bash
cd synarius-core
pip install -e .

cd ../synarius-dataviewer
pip install -e .
```

Console entry point (placeholder):

```bash
synarius-dataviewer
```

## Layout

- `src/synarius_dataviewer/` — package code
