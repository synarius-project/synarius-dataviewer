"""CLI entry for synarius-dataviewer (extend with real viewers later)."""

from synarius_dataviewer._version import __version__


def main() -> int:
    print(f"synarius-dataviewer {__version__}: no viewer wired yet; package skeleton is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
