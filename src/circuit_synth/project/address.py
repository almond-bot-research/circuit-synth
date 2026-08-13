"""Hierarchical addressing and designator control.

Components carry an `address` extra field: the local instance name at
construction (e.g. "c_in"), expanded to the full dotted address (e.g.
"ldo.c_in") by `finalize_design`. Addresses are the stable key that ties a
component to its layout footprint; the layout tooling keys footprint
placement off them, so they survive designator renumbering.

Designators can be pinned from a ref map (address -> ref) so they match a
previously laid-out board exactly, which keeps ported KiCad layouts 1:1.
"""

from __future__ import annotations

import json
from pathlib import Path

from circuit_synth import Circuit


def instance(subcircuit: Circuit, name: str) -> Circuit:
    """Rename a subcircuit instance (sheet name and address prefix)."""
    subcircuit.name = name
    return subcircuit


def _walk(circuit: Circuit, prefix: str, out: dict[str, object]) -> None:
    for comp in list(circuit._components.values()):
        local = comp._extra_fields.get("address")
        if not local:
            continue
        full = f"{prefix}.{local}" if prefix else local
        comp._extra_fields["address"] = full
        if full in out:
            raise ValueError(f"duplicate address: {full}")
        out[full] = comp
    for sub in circuit._subcircuits:
        sub_prefix = f"{prefix}.{sub.name}" if prefix else sub.name
        _walk(sub, sub_prefix, out)


def _rekey(circuit: Circuit) -> None:
    comps = list(circuit._components.values())
    circuit._components = {c.ref: c for c in comps}
    if len(circuit._components) != len(comps):
        raise ValueError(f"designator collision in circuit {circuit.name}")
    for sub in circuit._subcircuits:
        _rekey(sub)


def finalize_design(circuit: Circuit, ref_map: dict[str, str] | Path | None = None) -> dict[str, object]:
    """Expand addresses to full dotted paths and optionally pin designators.

    Must be called on the top-level circuit after it is fully built and before
    project generation. Returns the address -> Component mapping.
    """
    addressed: dict[str, object] = {}
    _walk(circuit, "", addressed)

    if ref_map is not None:
        if isinstance(ref_map, (str, Path)):
            ref_map = json.loads(Path(ref_map).read_text())
        for address, comp in addressed.items():
            ref = ref_map.get(address)
            if ref:
                comp.ref = ref
        _rekey(circuit)

    return addressed
