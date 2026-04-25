from pathlib import Path


def _read_version() -> str:
    # Packaged: VERSION ships alongside mfarm/__init__.py.
    # Dev checkout: VERSION lives at the repo root, one level up.
    here = Path(__file__).resolve().parent
    for candidate in (here / "VERSION", here.parent / "VERSION"):
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


__version__ = _read_version()
