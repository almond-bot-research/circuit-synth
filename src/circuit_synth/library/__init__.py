"""Bundled standard KiCad libraries.

`footprints/` carries the standard footprint libraries: one directory per
library (the directory name is the lib nickname), each holding
`.kicad_mod` footprints, their 3D models (`.wrl`/`.step`), and - for the
generic passives - a matching `.kicad_sym` symbol.

Projects reference these through the `${CIRCUIT_SYNTH_LIB}` path variable,
which the tooling sets from `library_root()` (see `ensure_env`) and which
`cs setup-kicad` registers in KiCad's user config for GUI sessions.
"""

from __future__ import annotations

import os
from pathlib import Path

LIBRARY_ROOT = Path(__file__).resolve().parent
FOOTPRINTS_DIR = LIBRARY_ROOT / "footprints"

ENV_VAR = "CIRCUIT_SYNTH_LIB"


def library_root() -> Path:
    return LIBRARY_ROOT


def ensure_env() -> None:
    """Expose the library location to KiCad tools via ${CIRCUIT_SYNTH_LIB}."""
    os.environ.setdefault(ENV_VAR, str(LIBRARY_ROOT))


def footprint_lib_dirs() -> list[Path]:
    """The standard footprint library directories (lib nickname = dir name)."""
    if not FOOTPRINTS_DIR.is_dir():
        return []
    return sorted(
        p
        for p in FOOTPRINTS_DIR.iterdir()
        if p.is_dir() and (any(p.glob("*.kicad_mod")) or any(p.glob("*.kicad_sym")))
    )
