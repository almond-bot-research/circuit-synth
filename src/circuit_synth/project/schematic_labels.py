"""Deterministic net labels for generated schematics.

circuit-synth's schematic writer places net labels per pin *name*, which
breaks on symbols with repeated pin names (multiple IOVDD/GND/EH pins, the
ESD862's two "IO" pins): only the first pin of a group gets a label, and
duplicate names can stack two different nets on one pin, silently shorting
them in KiCad's geometric connectivity.

This pass rewrites every sheet after generation:

- deletes all labels and no_connect markers
- places one label per connected pin, exactly at the pin endpoint, using the
  net map from the canonical circuit JSON (keyed by pin *number*)
- places a local label at every sheet-box pin on the parent side
- marks unconnected pins with no_connect
- derives label/no_connect UUIDs from sheet + pin identity (uuid5), so
  rebuilding an unchanged circuit produces byte-identical sheets

Sheet-box pins themselves (created by circuit-synth from the net names, which
we also use for labels) are left untouched.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path


# ---------------------------------------------------------------- sexpr


def _tokenize(text: str):
    for m in re.finditer(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', text):
        yield m.group(0), m.start(), m.end()


def parse_spans(text: str):
    """Parse an s-expression into nested lists; each list carries (start, end) spans."""
    tokens = list(_tokenize(text))

    def parse(i: int):
        tok, start, _ = tokens[i]
        assert tok == "(", f"expected ( at {start}"
        node = []
        i += 1
        while tokens[i][0] != ")":
            tok, tstart, tend = tokens[i]
            if tok == "(":
                child, i = parse(i)
                node.append(child)
            else:
                node.append(tok[1:-1] if tok.startswith('"') else tok)
                i += 1
        node_span = (start, tokens[i][2])
        return _Node(node, node_span), i + 1

    root, _ = parse(0)
    return root


class _Node(list):
    def __init__(self, items, span):
        super().__init__(items)
        self.span = span


def _items(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def _one(node, key, default=None):
    found = _items(node, key)
    return found[0] if found else default


# ---------------------------------------------------------------- geometry


def _pin_geometry(tree):
    """lib_id -> {pin_number: (x, y, orient_deg)} from embedded lib_symbols."""
    out = {}
    libs = _one(tree, "lib_symbols")
    if not libs:
        return out
    for sym in _items(libs, "symbol"):
        pins = {}
        for unit in _items(sym, "symbol"):
            for pin in _items(unit, "pin"):
                at = _one(pin, "at")
                num = _one(pin, "number")[1]
                orient = float(at[3]) if len(at) > 3 else 0.0
                pins.setdefault(num, (float(at[1]), float(at[2]), orient))
        out[sym[1]] = pins
    return out


def _transform(px: float, py: float, orient: float, sx: float, sy: float, sdeg: float, mirror: str | None):
    """Symbol-space pin (pos + orientation) -> sheet-space (point + visual angle)."""
    dx, dy = math.cos(math.radians(orient)), math.sin(math.radians(orient))
    if mirror == "y":
        px, dx = -px, -dx
    elif mirror == "x":
        py, dy = -py, -dy
    # symbol space is y-up, sheet space is y-down
    py, dy = -py, -dy
    r = math.radians(-sdeg)
    cos_r, sin_r = math.cos(r), math.sin(r)
    x = sx + px * cos_r - py * sin_r
    y = sy + px * sin_r + py * cos_r
    ddx = dx * cos_r - dy * sin_r
    ddy = dx * sin_r + dy * cos_r
    visual = math.degrees(math.atan2(-ddy, ddx)) % 360
    return round(x, 4), round(y, 4), round(visual / 90) % 4 * 90


def _instances(tree):
    """[(ref, lib_id, endpoint map {pin_number: (x, y, label_angle)})]"""
    geo = _pin_geometry(tree)
    out = []
    for sym in _items(tree, "symbol"):
        lib_id_node = _one(sym, "lib_id")
        if lib_id_node is None:
            continue
        lib_id = lib_id_node[1]
        at = _one(sym, "at")
        sx, sy = float(at[1]), float(at[2])
        sdeg = float(at[3]) if len(at) > 3 else 0.0
        mirror_node = _one(sym, "mirror")
        mirror = mirror_node[1] if mirror_node else None
        ref = next((p[2] for p in _items(sym, "property") if p[1] == "Reference"), None)
        pins = {}
        for num, (px, py, orient) in geo.get(lib_id, {}).items():
            x, y, visual = _transform(px, py, orient, sx, sy, sdeg, mirror)
            # label text should extend away from the body (opposite pin direction)
            pins[num] = (x, y, (visual + 180) % 360)
        out.append((ref, lib_id, pins))
    return out


# ---------------------------------------------------------------- json scopes


def _scopes(circuit: dict, out: dict) -> None:
    nets = {}
    for name, nodes in circuit.get("nets", {}).items():
        if isinstance(nodes, dict):  # root nets carry metadata under "nodes"
            nodes = nodes.get("nodes", [])
        pins = {(n["component"], str(n["pin"]["number"])) for n in nodes}
        nets[name] = pins
    out[circuit["name"]] = nets
    for sub in circuit.get("subcircuits", []):
        _scopes(sub, out)


# ---------------------------------------------------------------- rewrite


_REMOVE_KEYS = {"label", "hierarchical_label", "global_label", "no_connect"}

# Historical namespace string (this module started life as
# almond_bot.common.schematic_labels): changing it would churn every label
# UUID in every schematic already generated with it, so it stays.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "almond_bot.common.schematic_labels")


def _label_block(kind: str, text: str, x: float, y: float, angle: int, uid: uuid.UUID) -> str:
    shape = "\t\t(shape input)\n" if kind == "hierarchical_label" else ""
    return (
        f'\t({kind} "{text}"\n'
        f"{shape}"
        f"\t\t(at {x:g} {y:g} {angle})\n"
        f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n\t\t)\n"
        f'\t\t(uuid "{uid}")\n'
        f"\t)\n"
    )


def _no_connect_block(x: float, y: float, uid: uuid.UUID) -> str:
    return f'\t(no_connect\n\t\t(at {x:g} {y:g})\n\t\t(uuid "{uid}")\n\t)\n'


def rewrite_sheet(path: Path, nets: dict[str, set], global_nets: set[str]) -> None:
    """Rewrite one sheet's labels.

    nets: net name -> {(ref, pin_number)} for this sheet's scope
    global_nets: nets spanning multiple scopes; these get global labels because
        circuit-synth only creates sheet-box pins for nets that have root-level
        component pins, so pure child-to-child nets have no hierarchy path.
    """
    text = path.read_text()
    tree = parse_spans(text)

    # spans to delete
    remove = [child.span for child in tree if isinstance(child, list) and child and child[0] in _REMOVE_KEYS]

    # pin -> net lookup
    pin_net: dict[tuple[str, str], str] = {}
    for net, pins in nets.items():
        for ref_pin in pins:
            pin_net[ref_pin] = net

    # Stable identity for each emitted block: sheet + ref + pin (+ occurrence,
    # in case a ref ever repeats), so rebuilds reproduce the same UUIDs.
    seen: dict[str, int] = {}

    def _uid(ref: str, num: str) -> uuid.UUID:
        key = f"{path.name}|{ref}|{num}"
        n = seen.get(key, 0)
        seen[key] = n + 1
        return uuid.uuid5(_UUID_NAMESPACE, f"{key}|{n}")

    additions: list[str] = []
    for ref, _lib_id, pins in _instances(tree):
        if ref is None:
            continue
        for num, (x, y, angle) in pins.items():
            net = pin_net.get((ref, num))
            uid = _uid(ref, num)
            if net is None:
                additions.append(_no_connect_block(x, y, uid))
            elif net in global_nets:
                additions.append(_label_block("global_label", net, x, y, angle, uid))
            else:
                additions.append(_label_block("hierarchical_label", net, x, y, angle, uid))

    # apply removals back-to-front, then insert before final ')'
    for start, end in sorted(remove, reverse=True):
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end) + 1 or end
        if text[line_start:start].strip() == "" and text[end:line_end - 1].strip() == "":
            text = text[:line_start] + text[line_end:]
        else:
            text = text[:start] + text[end:]

    closing = text.rstrip().rfind(")")
    text = text[:closing] + "".join(additions) + text[closing:]
    path.write_text(text)


def compute_global_nets(circuit) -> set[str]:
    """Names of Net objects whose pins span more than one scope.

    Identity-based (the same Net object, not the same name): two subcircuit
    instances may both have an internal net called DVDD, and those must stay
    sheet-local. Raises if two *different* multi-scope nets share a name,
    since their global labels would short in KiCad.
    """
    net_scopes: dict[int, tuple[str, set]] = {}

    def walk(circ) -> None:
        for comp in circ._components.values():
            for pin in comp._pins.values():
                if pin.net is not None:
                    name, scopes = net_scopes.setdefault(id(pin.net), (pin.net.name, set()))
                    scopes.add(circ.name)
        for sub in circ._subcircuits:
            walk(sub)

    walk(circuit)
    names = [name for name, scopes in net_scopes.values() if len(scopes) > 1]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"different multi-scope nets share a name (rename them): {dupes}")
    return set(names)


def rewrite_labels(project_dir: str | Path, build_name: str, global_nets: set[str]) -> None:
    """Rewrite net labels for every sheet of a generated project."""
    project_dir = Path(project_dir)
    circuit_json = json.loads((project_dir / f"{build_name}.json").read_text())

    scopes: dict[str, dict[str, set]] = {}
    _scopes(circuit_json, scopes)

    for name in scopes:
        f = project_dir / f"{name}.kicad_sch"
        if f.exists():
            rewrite_sheet(f, scopes.get(name, {}), global_nets)
