"""Design-rule conventions shared by every board, beyond the fab minimums.

stdlib only: imported by `cs pcb sync` (pcbnew Python, via fab_rules) and
by `build_project` (the circuit-synth environment, no pcbnew).

- Standard net classes: STANDARD_NET_CLASSES are stamped into every
  build's .kicad_pro (net_settings.classes) on each sync and build. Missing
  classes are added and drifted values of the standard ones are corrected;
  any other (design-specific) class is left alone.
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
) -> list[str]:
    """Stamp the standard net classes (and, if given, the design's net-class
    assignments as netclass_patterns) into a .kicad_pro. Returns log
    messages; a missing project file is a no-op."""
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
    added, corrected = [], []
    for std in STANDARD_NET_CLASSES:
        i = by_name.get(std["name"])
        if i is None:
            classes.append(dict(std))
            added.append(std["name"])
        elif any(classes[i].get(k) != v for k, v in std.items()):
            # Keep any keys a newer KiCad added; force the standard values.
            classes[i] = {**classes[i], **std}
            corrected.append(std["name"])
    if added:
        notes.append(f"net classes added to {pro_path.name}: {', '.join(added)}")
    if corrected:
        notes.append(f"net classes reset to standard values in {pro_path.name}: {', '.join(corrected)}")

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


# ---------------------------------------------------------------- .kicad_dru


def parse_dru_rules(text: str) -> list[tuple[str, str]]:
    """Split .kicad_dru text into (rule name, rule text) pairs.

    Each rule's text includes the comment lines directly above it and ends
    with a newline, the same shape as fab_rules.DRU_RULES. The
    `(version N)` header and anything else outside a rule is dropped.
    """
    rules: list[tuple[str, str]] = []
    comments: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        match = re.match(r'\(rule\s+"((?:[^"\\]|\\.)*)"', stripped)
        if stripped.startswith("#"):
            comments.append(line)
            i += 1
            continue
        if not match:
            comments = []
            i += 1
            continue
        # Collect lines until the rule's parentheses balance (ignoring
        # parens inside strings and comments).
        body: list[str] = []
        depth = 0
        while i < len(lines):
            body.append(lines[i])
            depth += _paren_delta(lines[i])
            i += 1
            if depth <= 0:
                break
        rules.append((match.group(1), "\n".join([*comments, *body]) + "\n"))
        comments = []
    return rules


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
