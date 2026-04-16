#!/usr/bin/env python3
"""Minimales napari-Beispiel: synthetischer 3D-Stack (Z, Y, X) im Viewer.

Voraussetzung: ``pip install napari`` in der synarius-apps-venv (siehe unten).

Ausführen (PowerShell), Arbeitsverzeichnis ``synarius-apps``::

    .\\.venv\\Scripts\\python scripts\\napari_minimal_example.py

Verwenden Sie dieselbe venv, in der ``napari`` installiert ist (z. B. ``synarius-apps\\.venv``).
Mit einer anderen Python-Installation kann ein minimales ``napari``-Metapaket ohne ``view_image`` liegen.

Hinweis: napari öffnet ein eigenes Fenster; das Skript endet, wenn Sie den Viewer schließen.
"""

from __future__ import annotations

import numpy as np

try:
    import napari
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "napari ist nicht installiert. Im Ordner synarius-apps ausführen:\n"
        "  .\\.venv\\Scripts\\pip install napari\n"
    ) from exc


def main() -> None:
    rng = np.random.default_rng(0)
    # Kleiner 3D-Stack: Z × Y × X (napari erwartet oft channel-last; reine Intensität reicht hier)
    stack = rng.random((12, 96, 96), dtype=np.float32)
    stack *= 255.0

    # Viewer-API (stabiler als napari.view_image; funktioniert auch bei lazy_loader-Exports).
    viewer = napari.Viewer(title="Synarius – napari Minimalbeispiel")
    viewer.add_image(stack, name="synthetischer Stack", colormap="viridis")
    napari.run()


if __name__ == "__main__":
    main()
