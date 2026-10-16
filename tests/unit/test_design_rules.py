"""Board conventions stamped by `cs pcb sync`: standard net classes,
per-design net-class assignments, and DRC rules carried by part dirs.

Found on circuits-py: jelly_legs hand-defined its net classes and an
LM74502 fine-pitch clearance rule in its own .kicad_pro/.kicad_dru, while
almond_safety (same 24V input stage) still had KiCad's stock Default class
and no LM74502 rule.
"""

import json
import sys
import types
from pathlib import Path

import pytest

from circuit_synth.pcb.scripts import design_rules
from circuit_synth.pcb.scripts.design_rules import (
    PART_DRU_NAME,
    STANDARD_NET_CLASSES,
    append_dru_rules,
    apply_net_classes,
    parse_dru_rules,
    part_dru_rules,
    unmatched_patterns,
)

LM74502_RULE = """\
# The LM74502's TSOT-23-8 has 0.65mm pitch / 0.36mm pads, leaving 0.29mm
# between pins, under Power_24V's 0.3mm.
(rule "LM74502 fine-pitch pins"
  (condition "A.memberOfFootprint('TI_LM74502QDDFRQ1:*') || B.memberOfFootprint('TI_LM74502QDDFRQ1:*')")
  (constraint clearance (min 0.25mm)))
"""

# KiCad's stock Default as written into a fresh project (almond_safety).
STOCK_DEFAULT = {
    "bus_width": 12,
    "clearance": 0.2,
    "diff_pair_gap": 0.25,
    "diff_pair_via_gap": 0.25,
    "diff_pair_width": 0.2,
    "line_style": 0,
    "microvia_diameter": 0.3,
    "microvia_drill": 0.1,
    "name": "Default",
    "pcb_color": "rgba(0, 0, 0, 0.000)",
    "priority": 2147483647,
    "schematic_color": "rgba(0, 0, 0, 0.000)",
    "track_width": 0.2,
    "via_diameter": 0.6,
    "via_drill": 0.3,
    "wire_width": 6,
}


def _pro(tmp_path, net_settings):
    path = tmp_path / "default.kicad_pro"
    path.write_text(json.dumps({"board": {}, "net_settings": net_settings}, indent=2))
    return path


def _classes(path):
    return {c["name"]: c for c in json.loads(path.read_text())["net_settings"]["classes"]}


@pytest.fixture
def fab_rules(monkeypatch):
    """fab_rules as `cs pcb sync` imports it (scripts dir on sys.path), with
    a stand-in pcbnew: only apply_board_fab_defaults needs the real one."""
    monkeypatch.setitem(sys.modules, "pcbnew", types.SimpleNamespace(FromMM=lambda mm: int(mm * 1e6)))
    monkeypatch.syspath_prepend(str(Path(design_rules.__file__).parent))
    for name in ("fab_rules", "design_rules"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import fab_rules as module

    return module


# ------------------------------------------------------------ net classes

def test_standard_classes_match_jelly_legs():
    by_name = {c["name"]: c for c in STANDARD_NET_CLASSES}
    assert list(by_name) == ["Default", "Power_24V", "Power_24V_SRC", "Power_5V"]
    pick = ("clearance", "track_width", "via_diameter", "via_drill", "priority")
    assert {n: tuple(c[k] for k in pick) for n, c in by_name.items()} == {
        "Default": (0.13, 0.2, 0.6, 0.3, 2147483647),
        "Power_24V": (0.3, 3.0, 1.2, 0.6, 0),
        "Power_24V_SRC": (0.2, 0.7, 1.2, 0.6, 0),
        "Power_5V": (0.13, 0.3, 0.8, 0.4, 1),
    }
    # Same field set KiCad writes for its stock class.
    assert all(set(c) == set(STOCK_DEFAULT) for c in STANDARD_NET_CLASSES)


def test_stock_project_gets_standard_classes(tmp_path):
    pro = _pro(tmp_path, {"classes": [dict(STOCK_DEFAULT)], "meta": {"version": 4},
                          "netclass_patterns": []})
    notes = apply_net_classes(pro)
    assert _classes(pro) == {c["name"]: c for c in STANDARD_NET_CLASSES}
    assert any("added" in n and "Power_24V_SRC" in n for n in notes)
    assert any("reset" in n and "Default" in n for n in notes)


def test_extra_classes_and_patterns_are_kept(tmp_path):
    custom = {**STOCK_DEFAULT, "name": "Motor_48V", "clearance": 0.5}
    patterns = [{"netclass": "Motor_48V", "pattern": "V48*"}]
    pro = _pro(tmp_path, {"classes": [*STANDARD_NET_CLASSES, custom],
                          "netclass_patterns": patterns})
    assert apply_net_classes(pro) == []
    data = json.loads(pro.read_text())["net_settings"]
    assert _classes(pro)["Motor_48V"] == custom
    assert data["netclass_patterns"] == patterns


def test_drifted_standard_class_is_corrected(tmp_path):
    drifted = [dict(c) for c in STANDARD_NET_CLASSES]
    drifted[1]["track_width"] = 2.0
    drifted[1]["future_kicad_key"] = 1
    pro = _pro(tmp_path, {"classes": drifted})
    notes = apply_net_classes(pro)
    assert notes == [f"net classes reset to standard values in {pro.name}: Power_24V"]
    fixed = _classes(pro)["Power_24V"]
    assert fixed["track_width"] == 3.0
    assert fixed["future_kicad_key"] == 1  # unknown keys survive
    assert apply_net_classes(pro) == []  # idempotent


def test_blank_generator_project(tmp_path):
    # circuit-synth's blank project has an empty net_settings and no newline.
    pro = _pro(tmp_path, {})
    apply_net_classes(pro, {"Power_5V": ["VBUS"]})
    data = json.loads(pro.read_text())["net_settings"]
    assert data["meta"] == {"version": 4}
    assert data["netclass_patterns"] == [{"netclass": "Power_5V", "pattern": "VBUS"}]
    assert not pro.read_text().endswith("\n")


def test_missing_project_is_noop(tmp_path):
    assert apply_net_classes(tmp_path / "nope.kicad_pro", {"Power_5V": ["VBUS"]}) == []


def test_assignments_replace_patterns_in_order(tmp_path):
    pro = _pro(tmp_path, {"classes": list(STANDARD_NET_CLASSES),
                          "netclass_patterns": [{"netclass": "Power_5V", "pattern": "OLD"}]})
    notes = apply_net_classes(pro, {
        "Power_24V": ["/V24_IN", "VM", "M*_OUT*"],
        "Power_24V_SRC": "/input_protection/SRC",  # a bare string is one pattern
        "Power_5V": ["V5V*", "VBUS"],
    })
    assert notes == [f"net-class assignments written to {pro.name}: 6 pattern(s)"]
    assert json.loads(pro.read_text())["net_settings"]["netclass_patterns"] == [
        {"netclass": "Power_24V", "pattern": "/V24_IN"},
        {"netclass": "Power_24V", "pattern": "VM"},
        {"netclass": "Power_24V", "pattern": "M*_OUT*"},
        {"netclass": "Power_24V_SRC", "pattern": "/input_protection/SRC"},
        {"netclass": "Power_5V", "pattern": "V5V*"},
        {"netclass": "Power_5V", "pattern": "VBUS"},
    ]
    before = pro.read_text()
    assert apply_net_classes(pro, {
        "Power_24V": ["/V24_IN", "VM", "M*_OUT*"],
        "Power_24V_SRC": "/input_protection/SRC",
        "Power_5V": ["V5V*", "VBUS"],
    }) == []
    assert pro.read_text() == before


def test_assignment_to_unknown_class_fails(tmp_path):
    pro = _pro(tmp_path, {"classes": list(STANDARD_NET_CLASSES)})
    with pytest.raises(ValueError, match="Power_12V"):
        apply_net_classes(pro, {"Power_12V": ["V12"]})


def test_unmatched_patterns(tmp_path):
    net = tmp_path / "default.net"
    net.write_text(
        '(export (nets\n'
        '    (net (code "1") (name "/V24_IN") (class "Default")\n'
        '    (net (code "2") (name "VBUS") (class "Default")\n'
        '    (net (code "3") (name "M1_OUTA") (class "Default")))\n'
    )
    assert unmatched_patterns(
        {"Power_24V": ["/V24_IN", "V24_IN", "M*_OUT*"], "Power_5V": "VBUS"}, net
    ) == ["Power_24V: V24_IN"]


# ------------------------------------------------------------ .kicad_dru

def test_parse_dru_rules_keeps_leading_comments():
    text = "(version 1)\n\n# unrelated header comment\n\n" + LM74502_RULE + "\n" + (
        '(rule "two"\n  (condition "A.Type == \'Via\'")  # trailing (comment\n'
        '  (constraint clearance (min 0.1mm)))\n'
    )
    rules = parse_dru_rules(text)
    assert [name for name, _ in rules] == ["LM74502 fine-pitch pins", "two"]
    assert rules[0][1] == LM74502_RULE
    assert rules[1][1].startswith('(rule "two"') and rules[1][1].endswith("0.1mm)))\n")


def test_parse_fab_rules_roundtrip(fab_rules):
    # The fab rules file is itself parseable into the stamped rule list.
    assert parse_dru_rules(fab_rules.JLCPCB_DRU) == fab_rules.DRU_RULES


def _part(root, name, rules=None, footprint=True):
    d = root / name
    d.mkdir(parents=True)
    if footprint:
        (d / "pkg.kicad_mod").write_text("(footprint)\n")
    if rules is not None:
        (d / PART_DRU_NAME).write_text(rules)
    return d


def test_part_rules_only_for_used_libs(tmp_path):
    libs = {
        "TI_LM74502QDDFRQ1": _part(tmp_path, "TI_LM74502QDDFRQ1", "(version 1)\n\n" + LM74502_RULE),
        "Other_Part": _part(tmp_path, "Other_Part", '(rule "other"\n  (constraint clearance (min 1mm)))\n'),
        "R0603": _part(tmp_path, "R0603"),
    }
    assert part_dru_rules(libs, {"R0603", "Standard_Packages"}) == []
    assert part_dru_rules(libs, {"TI_LM74502QDDFRQ1", "R0603"}) == [
        ("LM74502 fine-pitch pins", LM74502_RULE)
    ]


def test_duplicate_part_rule_name_fails(tmp_path):
    rule = '(rule "same"\n  (constraint clearance (min 1mm)))\n'
    libs = {"A": _part(tmp_path, "A", rule), "B": _part(tmp_path, "B", rule)}
    with pytest.raises(SystemExit, match="same"):
        part_dru_rules(libs, {"A", "B"})


def test_append_dru_rules_if_missing(tmp_path):
    dru = tmp_path / "default.kicad_dru"
    edited = LM74502_RULE.replace("0.25mm", "0.22mm")  # per-board tweak survives
    dru.write_text("(version 1)\n\n" + edited)
    rules = [("LM74502 fine-pitch pins", LM74502_RULE),
             ("new", '(rule "new"\n  (constraint clearance (min 1mm)))\n')]
    assert append_dru_rules(dru, rules, "part") == [
        f"appended part rule(s) to {dru.name}: new"
    ]
    text = dru.read_text()
    assert "0.22mm" in text and "0.25mm" not in text
    assert text.endswith('(rule "new"\n  (constraint clearance (min 1mm)))\n')
    assert append_dru_rules(dru, rules, "part") == []


# ------------------------------------------------------------ apply_fab_rules

def test_sync_stamps_classes_and_part_rules(tmp_path, fab_rules):
    pro = _pro(tmp_path, {"classes": [dict(STOCK_DEFAULT)], "meta": {"version": 4},
                          "netclass_patterns": [{"netclass": "Power_5V", "pattern": "VBUS"}]})
    part_rules = [("LM74502 fine-pitch pins", LM74502_RULE)]

    notes = fab_rules.apply_fab_rules(tmp_path, "default", part_rules)
    dru = (tmp_path / "default.kicad_dru").read_text()
    assert [n for n, _ in parse_dru_rules(dru)] == [
        "same-net hole to hole", "via-via copper spacing", "LM74502 fine-pitch pins"
    ]
    assert set(_classes(pro)) == {"Default", "Power_24V", "Power_24V_SRC", "Power_5V"}
    assert _classes(pro)["Default"]["clearance"] == 0.13
    # sync never touches the design's assignments
    assert json.loads(pro.read_text())["net_settings"]["netclass_patterns"] == [
        {"netclass": "Power_5V", "pattern": "VBUS"}
    ]
    assert json.loads(pro.read_text())["board"]["design_settings"]["rules"] == fab_rules.JLCPCB_RULES
    assert any("part rule" in n for n in notes)

    assert fab_rules.apply_fab_rules(tmp_path, "default", part_rules) == []
