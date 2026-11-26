"""Design-rule conventions shared by every board, beyond the fab minimums.

stdlib only: imported by `cs pcb sync` (pcbnew Python, via fab_rules) and
by `build_project` (the circuit-synth environment, no pcbnew).

- Standard net classes: STANDARD_NET_CLASSES are stamped into every
  build's .kicad_pro (net_settings.classes) on each sync and build. Missing
  classes are added and drifted values of the standard ones are corrected;
  any other class that Python does not declare is left alone.
- A design's own net classes: `build_project(..., netclass_defs=[
  net_class("USB3_85R", diff_pair_width=0.19, diff_pair_gap=0.15,
  layers={"inner": {"diff_pair_width": 0.13}}), ...])` declares classes
  beyond the standard ones, with per-layer overrides keyed "outer", "inner"
  or a copper layer name (a layer name wins over outer/inner). They are
  stamped into the .kicad_pro like the standard classes. KiCad's classes
  hold one value per field, so the per-layer values go into the board's
  .kicad_dru as generated rules (net_class_layer_rules), which are
  rewritten - not appended-if-missing - whenever the declaration changes.
  The declaration itself travels to the pcbnew-side tools in the build's
  board-setup file (`<build>.board_setup.json`, see write_board_setup), and
  NetRules resolves a net's width / gap / via size on a layer from it for
  `cs pcb route`.
- Net-class assignments are per design, since net names differ per board:
  `build_project(..., netclasses={"Power_24V": ["/V24_IN", "VM", ...]})`
  writes them as the project's netclass_patterns (KiCad net-name patterns,
  `*`/`?` wildcards, matched against the full board net name, e.g.
  "/V24_IN" for a hierarchical-label net, "VM" for a global one). Python is
  the source of truth when the argument is given: the pattern list is
  replaced. Without it, the patterns in the project file are left as-is.
- Part-carried DRC rules: a part dir (footprint lib) may ship a
  `rules.kicad_dru` fragment with custom rules that belong to the package
  rather than to a board, e.g. a clearance exception for a fine-pitch
  footprint. On sync, the fragment's rules are appended to the board's
  .kicad_dru when any footprint from that lib is on the board, with the
  same append-if-missing-by-rule-name behavior as the stamped fab rules
  (so per-board edits of a rule survive, and an edited fragment rule is not
  re-stamped over an existing one of the same name). A fragment is a
  normal .kicad_dru: `(version 1)` header optional, comments directly above
  a rule travel with it. Scope conditions to the footprint lib, e.g.
  `A.memberOfFootprint('<Lib>:*')`, so the rule only fires on its part.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

# File name a part dir uses to ship DRC rules for its footprint.
PART_DRU_NAME = "rules.kicad_dru"


def _net_class(name, clearance, track_width, via_diameter, via_drill, priority):
    # Every field KiCad 9 writes for a class (net_settings version 4); the
    # ones not passed are KiCad's stock values, identical across classes.
    return {
        "bus_width": 12,
        "clearance": clearance,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": name,
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": priority,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": track_width,
        "via_diameter": via_diameter,
        "via_drill": via_drill,
        "wire_width": 6,
    }


# Values (mm) as first tuned on jelly_legs. Default is KiCad's catch-all
# class (priority = INT_MAX, i.e. lowest); clearance 0.13 sits just above
# the fab's 0.1 minimum. Power_24V is the motor/main 24V rail (3mm tracks,
# 0.3mm clearance); Power_24V_SRC is the 24V node between an ideal-diode
# controller's source pin and its FETs, which fans out of small pins, so it
# carries a narrower track and clearance.
STANDARD_NET_CLASSES = [
    _net_class("Default", 0.13, 0.2, 0.6, 0.3, 2147483647),
    _net_class("Power_24V", 0.3, 3.0, 1.2, 0.6, 0),
    _net_class("Power_24V_SRC", 0.2, 0.7, 1.2, 0.6, 0),
    _net_class("Power_5V", 0.13, 0.3, 0.8, 0.4, 1),
]

# Fields a design may override per layer, and the scopes it may key them by
# (the same choices as a .kicad_dru `(layer ...)` clause).
LAYER_FIELDS = ("clearance", "track_width", "diff_pair_width", "diff_pair_gap")
_LAYER_SCOPE_RE = re.compile(r"^(outer|inner|F\.Cu|B\.Cu|In[1-9]\d*\.Cu)$")


def net_class(
    name: str,
    *,
    clearance: float | None = None,
    track_width: float | None = None,
    via_diameter: float | None = None,
    via_drill: float | None = None,
    diff_pair_width: float | None = None,
    diff_pair_gap: float | None = None,
    diff_pair_via_gap: float | None = None,
    priority: int = 0,
    layers: Mapping[str, Mapping[str, float]] | None = None,
) -> dict:
    """A design's own net class, for `build_project(netclass_defs=[...])`.

    Values are mm; anything not given takes the standard Default class's
    value. `diff_pair_via_gap` is the copper gap between the two vias of a
    pair's layer change. `priority` decides between classes when a net
    matches the patterns of several (lower wins, as in KiCad).

    `layers` holds per-layer overrides of LAYER_FIELDS, keyed "outer"
    (F.Cu and B.Cu), "inner", or a copper layer name:

        net_class("USB3_85R", diff_pair_width=0.19, diff_pair_gap=0.15,
                  layers={"inner": {"diff_pair_width": 0.13}})
    """
    if not name or any(ch in name for ch in "'\"\\"):
        raise ValueError(f"net class name {name!r} must be non-empty, without quotes or backslashes")
    base = STANDARD_NET_CLASSES[0]
    given = {
        "clearance": clearance,
        "track_width": track_width,
        "via_diameter": via_diameter,
        "via_drill": via_drill,
        "diff_pair_width": diff_pair_width,
        "diff_pair_gap": diff_pair_gap,
        "diff_pair_via_gap": diff_pair_via_gap,
    }
    cls = {**base, "name": name, "priority": priority}
    cls.update({k: v for k, v in given.items() if v is not None})
    overrides: dict[str, dict[str, float]] = {}
    for scope, values in (layers or {}).items():
        if not _LAYER_SCOPE_RE.match(scope):
            raise ValueError(f"net class {name}: layer scope {scope!r} is not 'outer', 'inner' or a copper layer name")
        unknown = sorted(set(values) - set(LAYER_FIELDS))
        if unknown:
            raise ValueError(
                f"net class {name}: {', '.join(unknown)} cannot be set per layer (only {', '.join(LAYER_FIELDS)})"
            )
        overrides[scope] = dict(values)
    cls["layers"] = overrides
    return cls


def layer_kind(layer: str) -> str:
    """"outer" for F.Cu / B.Cu, "inner" for every other copper layer."""
    return "outer" if layer in ("F.Cu", "B.Cu") else "inner"


# Defaults for a project whose net_settings is still empty (the blank
# project circuit-synth writes before KiCad has ever saved it).
_NET_SETTINGS_DEFAULTS = {
    "meta": {"version": 4},
    "net_colors": None,
    "netclass_assignments": None,
    "netclass_patterns": [],
}


def apply_net_classes(
    pro_path: Path,
    netclasses: Mapping[str, Iterable[str]] | None = None,
    defs: Iterable[Mapping] = (),
) -> list[str]:
    """Stamp the standard net classes, the design's own classes (`defs`,
    from net_class; one with a standard class's name replaces it) and, if
    given, the design's net-class assignments as netclass_patterns into a
    .kicad_pro. Returns log messages; a missing project file is a no-op."""
    notes: list[str] = []
    if not pro_path.exists():
        return notes
    raw = pro_path.read_text()
    data = json.loads(raw)
    settings = data.setdefault("net_settings", {})
    for key, value in _NET_SETTINGS_DEFAULTS.items():
        settings.setdefault(key, json.loads(json.dumps(value)))
    classes = settings.setdefault("classes", [])

    by_name = {c.get("name"): i for i, c in enumerate(classes)}
    wanted = {c["name"]: c for c in STANDARD_NET_CLASSES}
    # KiCad's class has no per-layer values; those live in the .kicad_dru.
    declared = {d["name"]: {k: v for k, v in d.items() if k != "layers"} for d in defs}
    wanted.update(declared)
    added, corrected, updated = [], [], []
    for name, cls in wanted.items():
        i = by_name.get(name)
        if i is None:
            classes.append(dict(cls))
            added.append(name)
        elif any(classes[i].get(k) != v for k, v in cls.items()):
            # Keep any keys a newer KiCad added; force the wanted values.
            classes[i] = {**classes[i], **cls}
            (updated if name in declared else corrected).append(name)
    if added:
        notes.append(f"net classes added to {pro_path.name}: {', '.join(added)}")
    if corrected:
        notes.append(f"net classes reset to standard values in {pro_path.name}: {', '.join(corrected)}")
    if updated:
        notes.append(f"net classes updated from the design in {pro_path.name}: {', '.join(updated)}")

    if netclasses is not None:
        known = {c.get("name") for c in classes}
        unknown = sorted(set(netclasses) - known)
        if unknown:
            raise ValueError(
                f"netclasses refers to undefined net class(es) {', '.join(unknown)}; "
                f"defined: {', '.join(sorted(n for n in known if n))}"
            )
        patterns = [
            {"netclass": cls, "pattern": pattern}
            for cls, pats in netclasses.items()
            for pattern in ([pats] if isinstance(pats, str) else pats)
        ]
        if settings.get("netclass_patterns") != patterns:
            settings["netclass_patterns"] = patterns
            notes.append(f"net-class assignments written to {pro_path.name}: {len(patterns)} pattern(s)")

    if notes:
        # Keep the file's trailing-newline style (the schematic generator
        # writes none) so a stamp doesn't churn the rest of the file.
        out = json.dumps(data, indent=2)
        pro_path.write_text(out + "\n" if raw.endswith("\n") else out)
    return notes


_NET_NAME_RE = re.compile(r'\(net \(code "?\d+"?\) \(name "((?:[^"\\]|\\.)*)"')


def unmatched_patterns(netclasses: Mapping[str, Iterable[str]], net_path: Path) -> list[str]:
    """Patterns that match no net in a KiCad netlist (typos, a missing
    hierarchical "/" prefix). Assumes KiCad's default wildcard patterns."""
    if not net_path.exists():
        return []
    nets = [n.replace('\\"', '"') for n in _NET_NAME_RE.findall(net_path.read_text())]
    out = []
    for cls, pats in netclasses.items():
        for pattern in [pats] if isinstance(pats, str) else pats:
            if not any(fnmatch.fnmatchcase(n, pattern) for n in nets):
                out.append(f"{cls}: {pattern}")
    return out


# ------------------------------------------------------- net-class geometry


class NetRules:
    """The net classes of one build as `cs pcb route` reads them: which
    class a net is in, and that class's value for a field on a layer.

    `classes` and `patterns` are the .kicad_pro's net_settings.classes and
    netclass_patterns; `declared` the design's own classes (net_class) with
    their per-layer overrides. A net's classes are those whose patterns
    match its full board name (the same wildcard matching as
    unmatched_patterns), ordered by priority then name as KiCad orders
    them, with Default last; a value comes from the first of them that
    has the field.
    """

    def __init__(
        self,
        classes: Iterable[Mapping] = (),
        patterns: Iterable[Mapping] = (),
        declared: Iterable[Mapping] = (),
    ) -> None:
        self.classes: dict[str, Mapping] = {c["name"]: c for c in STANDARD_NET_CLASSES}
        self.classes.update({c["name"]: c for c in classes if c.get("name")})
        self.declared: dict[str, Mapping] = {d["name"]: d for d in declared}
        self.classes.update(self.declared)
        self.patterns = [(p["pattern"], p["netclass"]) for p in patterns]

    @classmethod
    def load(cls, project_dir: Path, build_name: str) -> "NetRules":
        """From a build's .kicad_pro and board-setup file (either may be absent)."""
        pro_path = Path(project_dir) / f"{build_name}.kicad_pro"
        settings = json.loads(pro_path.read_text()).get("net_settings", {}) if pro_path.exists() else {}
        declared = load_board_setup(project_dir, build_name).get("net_classes", [])
        return cls(settings.get("classes") or [], settings.get("netclass_patterns") or [], declared)

    def classes_of(self, net: str) -> list[str]:
        """Names of the net's classes, highest priority first, Default last."""
        matched = {c for pattern, c in self.patterns if c in self.classes and fnmatch.fnmatchcase(net, pattern)}
        matched.discard("Default")
        ordered = sorted(matched, key=lambda c: (self.classes[c].get("priority", 0), c))
        return [*ordered, "Default"]

    def class_of(self, net: str) -> str:
        """Name of the net's (highest-priority) class."""
        return self.classes_of(net)[0]

    def value(self, net: str, field: str, layer: str | None = None) -> float:
        """A class field (mm) for a net, e.g. "track_width"; with `layer`,
        the design's override for that layer name, else for its
        outer/inner kind, wins over the class value."""
        for name in self.classes_of(net):
            if layer is not None:
                overrides = self.declared.get(name, {}).get("layers") or {}
                for scope in (layer, layer_kind(layer)):
                    if field in overrides.get(scope, {}):
                        return overrides[scope][field]
            if field in self.classes[name]:
                return self.classes[name][field]
        raise KeyError(f"net class field {field!r}")


# ------------------------------------------------------- board-setup file

# What a design declares about its board (copper layer count, stack-up, its
# own net classes) is known to build.py, which runs in the circuit-synth
# environment, but applied by the pcbnew-side tools (`cs pcb sync` creates
# the board and stamps its rules, `cs pcb route` resolves geometry). It
# crosses over in this file next to the project, written by build_project.
BOARD_SETUP_SUFFIX = ".board_setup.json"


def board_setup_path(project_dir: Path, build_name: str) -> Path:
    return Path(project_dir) / f"{build_name}{BOARD_SETUP_SUFFIX}"


def write_board_setup(project_dir: Path, build_name: str, setup: Mapping) -> list[str]:
    """Write the build's board-setup file (keys with no value are left out)
    or, when the design declares nothing, remove a stale one. Returns log
    messages; unchanged content is not rewritten."""
    path = board_setup_path(project_dir, build_name)
    setup = {k: v for k, v in setup.items() if v}
    if not setup:
        if path.exists():
            path.unlink()
            return [f"removed {path.name} (the design declares no board setup)"]
        return []
    text = json.dumps(setup, indent=2) + "\n"
    if path.exists() and path.read_text() == text:
        return []
    path.write_text(text)
    return [f"board setup written to {path.name}: {', '.join(sorted(setup))}"]


def load_board_setup(project_dir: Path, build_name: str) -> dict:
    """The build's declared board setup; {} when the design declares none."""
    path = board_setup_path(project_dir, build_name)
    return json.loads(path.read_text()) if path.exists() else {}


# ---------------------------------------------------------------- .kicad_dru


def parse_dru_rules(text: str) -> list[tuple[str, str]]:
    """Split .kicad_dru text into (rule name, rule text) pairs.

    Each rule's text includes the comment lines directly above it and ends
    with a newline, the same shape as fab_rules.DRU_RULES. The
    `(version N)` header and anything else outside a rule is dropped.
    """
    lines = text.splitlines()
    return [(name, "\n".join(lines[start:end]) + "\n") for name, start, end in _rule_spans(lines)]


def _rule_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    """(rule name, first line, line after the last) of each rule in a
    .kicad_dru, the comment lines directly above a rule included."""
    spans: list[tuple[str, int, int]] = []
    first_comment: int | None = None
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        match = re.match(r'\(rule\s+"((?:[^"\\]|\\.)*)"', stripped)
        if stripped.startswith("#"):
            if first_comment is None:
                first_comment = i
            i += 1
            continue
        if not match:
            first_comment = None
            i += 1
            continue
        start = i if first_comment is None else first_comment
        # Collect lines until the rule's parentheses balance (ignoring
        # parens inside strings and comments).
        depth = 0
        while i < len(lines):
            depth += _paren_delta(lines[i])
            i += 1
            if depth <= 0:
                break
        spans.append((match.group(1), start, i))
        first_comment = None
    return spans


def _paren_delta(line: str) -> int:
    depth = 0
    in_str = False
    escaped = False
    for ch in line:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#":
            break
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
    return depth


def dru_rule_names(text: str) -> set[str]:
    return {name for name, _ in parse_dru_rules(text)}


def part_dru_rules(libs: Mapping[str, Path], used_libs: Iterable[str]) -> list[tuple[str, str]]:
    """Rules shipped by the part dirs of the footprint libs a board uses.

    `libs` is the project's fp-lib-table (nickname -> dir); libs without a
    PART_DRU_NAME file contribute nothing. Order is by lib nickname, so the
    appended text is deterministic.
    """
    rules: list[tuple[str, str]] = []
    seen: set[str] = set()
    for lib in sorted(set(used_libs)):
        path = libs.get(lib)
        if path is None:
            continue
        fragment = Path(path) / PART_DRU_NAME
        if not fragment.is_file():
            continue
        for name, text in parse_dru_rules(fragment.read_text()):
            if name in seen:
                raise SystemExit(f"DRC rule {name!r} is defined by two part dirs (second: {fragment})")
            seen.add(name)
            rules.append((name, text))
    return rules


def append_dru_rules(dru_path: Path, rules: list[tuple[str, str]], what: str) -> list[str]:
    """Append the rules missing (by name) from an existing .kicad_dru.
    Returns log messages."""
    text = dru_path.read_text()
    present = dru_rule_names(text)
    missing = [(name, rule) for name, rule in rules if name not in present]
    if not missing:
        return []
    text = text.rstrip("\n") + "\n\n" + "\n".join(rule for _, rule in missing)
    dru_path.write_text(text)
    names = ", ".join(name for name, _ in missing)
    return [f"appended {what} rule(s) to {dru_path.name}: {names}"]


# ------------------------------------------------- per-layer net-class rules

# Rules generated from a design's net classes are named with this prefix
# and carry this comment; only rules with both are ever rewritten.
NET_CLASS_RULE_PREFIX = "net class "
_GENERATED_MARK = "# Generated from the design's netclass_defs (build.py): cs build and"
_GENERATED_NOTE = _GENERATED_MARK + "\n# cs pcb sync rewrite this rule, so change the Python, not this file.\n"


def _scope_order(scope: str) -> tuple[int, int]:
    # KiCad applies the last matching rule, so a layer name must come after
    # outer/inner to win over it.
    if scope in ("outer", "inner"):
        return (0, ("outer", "inner").index(scope))
    if scope == "F.Cu":
        return (1, 0)
    return (1, 10**6) if scope == "B.Cu" else (1, int(scope[2:-3]))


def net_class_layer_rules(defs: Iterable[Mapping]) -> list[tuple[str, str]]:
    """The .kicad_dru rules, as (name, text), that give a design's net
    classes their per-layer values.

    Per class and overridden layer scope, mirroring the implicit rules
    KiCad derives from a class (the class value as the optimum, which is
    what interactive routing in pcbnew picks up; the fab minimums stay the
    hard limits): one rule for the track width (and clearance, if
    overridden), one for the width and gap of the class's differential
    pairs. Both state every value in effect on that layer, so a pair keeps
    its pair width where only the single-ended width is overridden. Where
    the pair gap is tighter than the clearance, a third rule lets the two
    traces of a pair sit that close, as KiCad does for a class-level gap.
    """
    rules: list[tuple[str, str]] = []
    for cls in defs:
        name = cls["name"]
        layers = cls.get("layers") or {}
        for scope in sorted(layers, key=_scope_order):
            kind = {} if scope in ("outer", "inner") else layers.get(layer_kind(scope), {})
            values = {**cls, **kind, **layers[scope]}
            where = scope if scope in ("outer", "inner") else f'"{scope}"'
            member = f"A.hasNetclass('{name}')"
            single = [f"(constraint track_width (opt {_mm(values['track_width'])}))"]
            if "clearance" in layers[scope]:
                single.append(f"(constraint clearance (min {_mm(values['clearance'])}))")
            pair = [
                f"(constraint track_width (opt {_mm(values['diff_pair_width'])}))",
                f"(constraint diff_pair_gap (opt {_mm(values['diff_pair_gap'])}))",
            ]
            variants = [("", member, single), (" pair", f"{member} && A.inDiffPair('*')", pair)]
            if values["diff_pair_gap"] < values["clearance"]:
                coupled = [f"(constraint clearance (min {_mm(values['diff_pair_gap'])}))"]
                variants.append((" pair gap", f"{member} && AB.isCoupledDiffPair()", coupled))
            for suffix, condition, constraints in variants:
                rule = f"{NET_CLASS_RULE_PREFIX}{name}: {scope}{suffix}"
                clauses = [f"(layer {where})", f'(condition "{condition}")', *constraints]
                body = "\n".join(f"  {clause}" for clause in clauses)
                rules.append((rule, f'{_GENERATED_NOTE}(rule "{rule}"\n{body})\n'))
    return rules


def _mm(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") + "mm"


def apply_net_class_rules(dru_path: Path, defs: Iterable[Mapping]) -> list[str]:
    """Make a .kicad_dru carry exactly the generated per-layer rules of
    these net classes (net_class_layer_rules). Returns log messages.

    Unlike the stamped and part rules, these follow the Python declaration:
    earlier generated rules are replaced, and removed when the declaration
    no longer has per-layer values. They go directly below the `(version
    N)` header, i.e. before every other rule, because KiCad lets a later
    rule win and a board- or part-specific rule should override a class's
    general geometry. The file is created if needed, and left untouched
    when it already matches or there is nothing to write or remove.
    """
    rules = net_class_layer_rules(defs)
    old = dru_path.read_text() if dru_path.exists() else ""
    lines = old.splitlines()
    stale = [
        (start, end)
        for name, start, end in _rule_spans(lines)
        if name.startswith(NET_CLASS_RULE_PREFIX) and _GENERATED_MARK in lines[start:end]
    ]
    if not rules and not stale:
        return []
    for start, end in reversed(stale):
        while end < len(lines) and not lines[end].strip():
            end += 1  # the blank line(s) that separated it from the next rule
        del lines[start:end]
    header = next((i for i, line in enumerate(lines) if re.match(r"\(version\s+\d+\)", line.strip())), None)
    head = lines[: header + 1] if header is not None else ["(version 1)"]
    rest = lines[header + 1 :] if header is not None else lines
    while rest and not rest[0].strip():
        rest = rest[1:]
    parts = ["\n".join(head), *(text.rstrip("\n") for _, text in rules)]
    if rest:
        parts.append("\n".join(rest).rstrip("\n"))
    new = "\n\n".join(parts) + "\n"
    if new == old:
        return []
    dru_path.write_text(new)
    if not rules:
        return [f"removed {len(stale)} generated net-class rule(s) from {dru_path.name}"]
    return [f"per-layer net-class rules written to {dru_path.name}: {len(rules)}"]
