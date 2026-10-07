"""Hierarchical addressing and designator control.

Components carry an `address` extra field: the local instance name at
construction (e.g. "c_in"), expanded to the full dotted address (e.g.
"ldo.c_in") by `finalize_design`. Addresses are the stable key that ties a
component to its layout footprint; the layout tooling keys footprint
placement off them, so they survive designator renumbering.

Designators can be pinned from a ref map (address -> ref) so they match a
previously laid-out board exactly, which keeps ported KiCad layouts 1:1.
`build_project` also pins every component to the designator it already has
in the existing schematic, so adding or removing a part never renumbers the
rest of the design; only new components get fresh designators.
"""

from __future__ import annotations

import json
import re
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


def _all_components(circuit: Circuit) -> list:
    comps = list(circuit._components.values())
    for sub in circuit._subcircuits:
        comps.extend(_all_components(sub))
    return comps


_REF_RE = re.compile(r"^(.*?)(\d+)$")


def _split_ref(ref: str) -> tuple[str, int | None]:
    m = _REF_RE.match(ref)
    return (m.group(1), int(m.group(2))) if m else (ref, None)


def _rekey(circuit: Circuit) -> None:
    comps = list(circuit._components.values())
    circuit._components = {c.ref: c for c in comps}
    if len(circuit._components) != len(comps):
        raise ValueError(f"designator collision in circuit {circuit.name}")
    for sub in circuit._subcircuits:
        _rekey(sub)


def existing_refs(project_dir: Path) -> dict[str, str]:
    """Read address -> designator from a project's existing schematic sheets.

    Returns an empty mapping when the project has not been generated yet.
    """
    from .schematic_labels import parse_spans

    refs: dict[str, str] = {}
    for sch in sorted(Path(project_dir).glob("*.kicad_sch")):
        tree = parse_spans(sch.read_text())
        for node in tree:
            if not (isinstance(node, list) and node and node[0] == "symbol"):
                continue
            props = {
                p[1]: p[2]
                for p in node
                if isinstance(p, list) and len(p) > 2 and p[0] == "property"
            }
            address, ref = props.get("address"), props.get("Reference")
            if address and ref and not ref.startswith("#"):
                refs[address] = ref
    return refs


def finalize_design(
    circuit: Circuit,
    ref_map: dict[str, str] | Path | None = None,
    keep_refs: dict[str, str] | None = None,
) -> dict[str, object]:
    """Expand addresses to full dotted paths and optionally pin designators.

    `ref_map` pins designators explicitly (address -> ref). `keep_refs` is the
    address -> ref mapping of the previously generated schematic (see
    `existing_refs`): components still at those addresses keep their
    designators, so adding or removing a part does not renumber the others.
    Explicit `ref_map` entries win. A kept ref is ignored if its prefix no
    longer matches the component (e.g. a resistor replaced by a capacitor at
    the same address). Every component that is not pinned and would collide
    with a pinned designator gets the next free number above the highest one
    of its prefix (pinned, or used anywhere in the previous schematic), so
    freed numbers are not reused.

    Must be called on the top-level circuit after it is fully built and before
    project generation. Returns the address -> Component mapping.
    """
    addressed: dict[str, object] = {}
    _walk(circuit, "", addressed)

    if isinstance(ref_map, (str, Path)):
        ref_map = json.loads(Path(ref_map).read_text())
    pins = {**(keep_refs or {}), **(ref_map or {})}
    if not pins:
        return addressed

    pinned: set[int] = set()
    for address, comp in addressed.items():
        ref = pins.get(address)
        if not ref:
            continue
        if address not in (ref_map or {}) and _split_ref(ref)[0] != _split_ref(comp.ref)[0]:
            continue
        comp.ref = ref
        pinned.add(id(comp))

    comps = _all_components(circuit)
    taken = {c.ref for c in comps if id(c) in pinned}
    highest: dict[str, int] = {}
    # Number new parts above every designator the previous schematic used,
    # including parts being removed in this build, so a deleted part's
    # designator is never handed to a different part
    for ref in taken | set((keep_refs or {}).values()):
        prefix, num = _split_ref(ref)
        if num is not None:
            highest[prefix] = max(highest.get(prefix, 0), num)
    for comp in comps:
        if id(comp) in pinned:
            continue
        if keep_refs or comp.ref in taken:
            prefix, _ = _split_ref(comp.ref)
            highest[prefix] = highest.get(prefix, 0) + 1
            comp.ref = f"{prefix}{highest[prefix]}"
        taken.add(comp.ref)

    _rekey(circuit)
    return addressed
