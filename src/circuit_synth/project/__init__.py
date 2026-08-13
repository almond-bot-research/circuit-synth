"""Project build helpers for design repos.

A design repo keeps a `layouts/` dir per package/design with one KiCad
project per build; `build_project` regenerates the schematic + netlists from
Python (deterministically - unchanged circuits rebuild byte-identical), and
the `cs pcb ...` commands maintain the human-owned `.kicad_pcb` next to
them. Components carry a hidden `address` extra field (expanded by
`finalize_design`) that ties a Python component to its board footprint
across designator renumbering.
"""

from .address import finalize_design, instance
from .build import build_project, parts_dir, setup_symbol_dirs
from .schematic_labels import compute_global_nets, rewrite_labels

__all__ = [
    "build_project",
    "compute_global_nets",
    "finalize_design",
    "instance",
    "parts_dir",
    "rewrite_labels",
    "setup_symbol_dirs",
]
