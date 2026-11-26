"""Board conventions stamped by `cs pcb sync`: standard net classes,
per-design net-class assignments, and DRC rules carried by part dirs.

Found on circuits-py: jelly_legs hand-defined its net classes and an
LM74502 fine-pitch clearance rule in its own .kicad_pro/.kicad_dru, while
almond_safety (same 24V input stage) still had KiCad's stock Default class
and no LM74502 rule.
"""

import json
import re
import sys
import types
from pathlib import Path

import pytest

from circuit_synth.pcb.scripts import design_rules
from circuit_synth.pcb.scripts.design_rules import (
    PART_DRU_NAME,
    STANDARD_NET_CLASSES,
    NetRules,
    append_dru_rules,
    apply_net_class_rules,
    apply_net_classes,
    board_setup_path,
    layer_kind,
    load_board_setup,
    net_class,
    net_class_layer_rules,
    parse_dru_rules,
    part_dru_rules,
    unmatched_patterns,
    write_board_setup,
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
        "same-net hole to hole", "via-via copper spacing", "THT pads: thermal relief",
        "LM74502 fine-pitch pins",
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


# ------------------------------------------------------------ THT thermal relief

THT_RULE = "THT pads: thermal relief"


def test_tht_thermal_relief_rule(fab_rules):
    # Zones are forced solid on every sync; this rule is what gives
    # through-hole pads spokes. Text verified against KiCad 9's zone filler
    # (spokes on a THT pad in a GND plane, the SMD pads of the same USB-C
    # footprint solid). KiCad doesn't report a bad property/value: an
    # unknown zone_connection value drops the whole .kicad_dru, so pin the
    # exact keywords.
    rules = dict(fab_rules.DRU_RULES)
    rule = rules[THT_RULE]
    assert '(condition "A.Type == \'Pad\' && A.Pad_Type == \'Through-hole\'")' in rule
    assert "(constraint zone_connection thermal_reliefs)" in rule
    assert "(constraint thermal_spoke_width (min 0.5mm))" in rule
    assert "(constraint thermal_relief_gap (min 0.3mm))" in rule
    for text in rules.values():
        for value in re.findall(r"\(constraint zone_connection (\w+)", text):
            assert value in {"solid", "thermal_reliefs", "none"}


def test_existing_board_gets_tht_rule_appended(tmp_path, fab_rules):
    # A board stamped before the rule existed picks it up on the next sync;
    # a per-board edit of an already-present stamped rule survives.
    edited_same_net = dict(fab_rules.DRU_RULES)["same-net hole to hole"].replace("0.3mm", "0.28mm")
    dru = tmp_path / "default.kicad_dru"
    dru.write_text("(version 1)\n\n" + edited_same_net)
    notes = fab_rules.apply_fab_rules(tmp_path, "default")
    assert notes == [
        f"appended stamped rule(s) to {dru.name}: via-via copper spacing, {THT_RULE}"
    ]
    text = dru.read_text()
    assert "0.28mm" in text
    assert [n for n, _ in parse_dru_rules(text)] == [
        "same-net hole to hole", "via-via copper spacing", THT_RULE
    ]
    assert fab_rules.apply_fab_rules(tmp_path, "default") == []


# ------------------------------------------------------------ a design's own net classes

# The thor carrier's impedance classes: one geometry on the outer layers,
# another on the inner signal layers (In2 / In7).
GMSL = dict(track_width=0.18, layers={"inner": {"track_width": 0.11}})
USB3 = dict(diff_pair_width=0.19, diff_pair_gap=0.15, diff_pair_via_gap=0.3,
            layers={"inner": {"diff_pair_width": 0.13}})


def _defs():
    return [net_class("GMSL_50R", **GMSL), net_class("USB3_85R", **USB3)]


def test_net_class_fills_in_the_default_class_values():
    cls = net_class("GMSL_50R", **GMSL)
    default = STANDARD_NET_CLASSES[0]
    assert cls["name"] == "GMSL_50R" and cls["priority"] == 0
    assert cls["track_width"] == 0.18
    for field in ("clearance", "via_diameter", "via_drill", "diff_pair_width", "diff_pair_gap"):
        assert cls[field] == default[field]
    assert cls["layers"] == {"inner": {"track_width": 0.11}}
    # the same field set KiCad writes, plus the per-layer overrides
    assert set(cls) == set(STOCK_DEFAULT) | {"layers"}
    assert net_class("X")["layers"] == {}


@pytest.mark.parametrize("kwargs, message", [
    (dict(layers={"top": {"track_width": 0.1}}), "layer scope 'top'"),
    (dict(layers={"In0.Cu": {"track_width": 0.1}}), "layer scope"),
    (dict(layers={"inner": {"via_diameter": 0.4}}), "via_diameter cannot be set per layer"),
])
def test_net_class_rejects_bad_layer_overrides(kwargs, message):
    with pytest.raises(ValueError, match=message):
        net_class("X", **kwargs)


def test_net_class_name_must_be_usable_in_a_rule_condition():
    for name in ("", "it's", 'a"b'):
        with pytest.raises(ValueError):
            net_class(name)


def test_declared_classes_are_stamped_without_their_layer_overrides(tmp_path):
    pro = _pro(tmp_path, {"classes": list(STANDARD_NET_CLASSES)})
    notes = apply_net_classes(pro, {"USB3_85R": ["UPHY_*"], "Power_5V": ["VBUS"]}, _defs())
    assert notes == [
        f"net classes added to {pro.name}: GMSL_50R, USB3_85R",
        f"net-class assignments written to {pro.name}: 2 pattern(s)",
    ]
    classes = _classes(pro)
    assert list(classes) == ["Default", "Power_24V", "Power_24V_SRC", "Power_5V", "GMSL_50R", "USB3_85R"]
    assert classes["USB3_85R"]["diff_pair_width"] == 0.19 and classes["USB3_85R"]["diff_pair_via_gap"] == 0.3
    assert all(set(c) == set(STOCK_DEFAULT) for c in classes.values())   # no "layers" key in KiCad's file
    assert apply_net_classes(pro, {"USB3_85R": ["UPHY_*"], "Power_5V": ["VBUS"]}, _defs()) == []


def test_declared_class_follows_python_and_may_replace_a_standard_one(tmp_path):
    pro = _pro(tmp_path, {"classes": list(STANDARD_NET_CLASSES)})
    apply_net_classes(pro, defs=_defs())
    changed = [net_class("GMSL_50R", track_width=0.2), net_class("Default", clearance=0.1)]
    assert apply_net_classes(pro, defs=changed) == [
        f"net classes updated from the design in {pro.name}: Default, GMSL_50R"
    ]
    assert _classes(pro)["GMSL_50R"]["track_width"] == 0.2
    assert _classes(pro)["Default"]["clearance"] == 0.1
    assert _classes(pro)["USB3_85R"]["diff_pair_width"] == 0.19   # no longer declared: left alone
    # without the declaration (a sync that knows nothing of it) Default is standard again
    assert apply_net_classes(pro) == [f"net classes reset to standard values in {pro.name}: Default"]


# ------------------------------------------------------------ net-class resolution

def _rules():
    patterns = [
        {"netclass": "GMSL_50R", "pattern": "/gmsl_?/COAX_?"},
        {"netclass": "USB3_85R", "pattern": "UPHY_*"},
        {"netclass": "Power_5V", "pattern": "V5V*"},
    ]
    return NetRules(STANDARD_NET_CLASSES, patterns, _defs())


def test_a_net_resolves_to_the_class_its_full_name_matches():
    rules = _rules()
    assert rules.class_of("/gmsl_a/COAX_A") == "GMSL_50R"
    assert rules.class_of("gmsl_a/COAX_A") == "Default"      # the hierarchical "/" is part of the name
    assert rules.class_of("UPHY_RX1_P") == "USB3_85R"
    assert rules.class_of("V5V_USB") == "Power_5V"
    assert rules.class_of("SPI_SCK") == "Default"
    assert rules.classes_of("UPHY_RX1_P") == ["USB3_85R", "Default"]


def test_width_follows_the_layer():
    rules = _rules()
    coax, usb = "/gmsl_a/COAX_A", "UPHY_RX1_P"
    assert layer_kind("F.Cu") == layer_kind("B.Cu") == "outer" and layer_kind("In7.Cu") == "inner"
    assert rules.value(coax, "track_width", "F.Cu") == 0.18
    assert rules.value(coax, "track_width", "B.Cu") == 0.18
    assert rules.value(coax, "track_width", "In7.Cu") == 0.11
    assert rules.value(coax, "track_width") == 0.18                  # no layer: the class value
    assert rules.value(usb, "diff_pair_width", "F.Cu") == 0.19
    assert rules.value(usb, "diff_pair_width", "In2.Cu") == 0.13
    assert rules.value(usb, "diff_pair_gap", "In2.Cu") == 0.15       # not overridden: the class value
    assert rules.value(usb, "diff_pair_via_gap") == 0.3
    assert (rules.value(usb, "via_diameter"), rules.value(usb, "via_drill")) == (0.6, 0.3)
    # nets outside the declared classes get their (standard) class's values on every layer
    assert rules.value("V5V_USB", "track_width", "In2.Cu") == 0.3
    assert rules.value("SPI_SCK", "track_width", "In2.Cu") == 0.2
    with pytest.raises(KeyError):
        rules.value(usb, "no_such_field")


def test_a_layer_name_override_beats_outer_and_inner():
    cls = net_class("Z", track_width=0.2, layers={
        "outer": {"track_width": 0.18}, "inner": {"track_width": 0.11}, "In2.Cu": {"track_width": 0.12},
        "F.Cu": {"track_width": 0.17},
    })
    rules = NetRules([], [{"netclass": "Z", "pattern": "*"}], [cls])
    assert [rules.value("N", "track_width", layer) for layer in ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")] == [
        0.17, 0.18, 0.11, 0.12
    ]


def test_priority_then_name_decides_between_matching_classes():
    low = net_class("A_LOW", track_width=0.5, priority=3)
    high = net_class("B_HIGH", track_width=0.25, priority=1)
    tie = net_class("A_TIE", track_width=0.4, priority=1)
    patterns = [{"netclass": c, "pattern": "CLK*"} for c in ("A_LOW", "B_HIGH")]
    rules = NetRules([], patterns, [low, high])
    assert rules.classes_of("CLK0") == ["B_HIGH", "A_LOW", "Default"]
    assert rules.value("CLK0", "track_width", "F.Cu") == 0.25
    rules = NetRules([], [*patterns, {"netclass": "A_TIE", "pattern": "CLK0"}], [low, high, tie])
    assert rules.class_of("CLK0") == "A_TIE"                 # same priority: by name, as KiCad sorts them


def test_class_without_a_field_falls_through_to_the_next_class():
    # a class made in KiCad's GUI only carries the fields set there
    partial = {"name": "HV", "clearance": 0.6, "priority": 0}
    rules = NetRules([*STANDARD_NET_CLASSES, partial], [{"netclass": "HV", "pattern": "HV*"},
                                                        {"netclass": "Undefined", "pattern": "HV*"}])
    assert rules.class_of("HV_BUS") == "HV"                  # a pattern for a class that does not exist is ignored
    assert rules.value("HV_BUS", "clearance") == 0.6
    assert rules.value("HV_BUS", "track_width", "F.Cu") == 0.2    # from Default
    assert not rules.declared


def test_net_rules_load_from_the_project_files(tmp_path):
    assert NetRules.load(tmp_path, "default").class_of("ANY") == "Default"   # no files at all
    pro = _pro(tmp_path, {})
    apply_net_classes(pro, {"GMSL_50R": ["/gmsl_?/COAX_?"]}, _defs())
    rules = NetRules.load(tmp_path, "default")
    assert rules.class_of("/gmsl_b/COAX_D") == "GMSL_50R" and not rules.declared
    assert rules.value("/gmsl_b/COAX_D", "track_width", "In7.Cu") == 0.18    # overrides not known yet
    write_board_setup(tmp_path, "default", {"net_classes": _defs()})
    rules = NetRules.load(tmp_path, "default")
    assert set(rules.declared) == {"GMSL_50R", "USB3_85R"}
    assert rules.value("/gmsl_b/COAX_D", "track_width", "In7.Cu") == 0.11


# ------------------------------------------------------------ board-setup file

def test_board_setup_file_round_trip(tmp_path):
    path = board_setup_path(tmp_path, "default")
    assert path.name == "default.board_setup.json"
    assert load_board_setup(tmp_path, "default") == {}
    assert write_board_setup(tmp_path, "default", {"copper_layers": None, "stackup": None, "net_classes": []}) == []
    assert not path.exists()                                  # a design that declares nothing gets no file
    setup = {"copper_layers": 10, "stackup": None, "net_classes": _defs()}
    assert write_board_setup(tmp_path, "default", setup) == [
        f"board setup written to {path.name}: copper_layers, net_classes"
    ]
    assert load_board_setup(tmp_path, "default") == {"copper_layers": 10, "net_classes": _defs()}
    assert write_board_setup(tmp_path, "default", setup) == []
    assert write_board_setup(tmp_path, "default", {"net_classes": []}) == [
        f"removed {path.name} (the design declares no board setup)"
    ]
    assert not path.exists()


# ------------------------------------------------------------ per-layer rules

def test_layer_rules_state_every_value_in_effect_on_the_layer():
    rules = dict(net_class_layer_rules(_defs()))
    assert list(rules) == [
        "net class GMSL_50R: inner", "net class GMSL_50R: inner pair",
        "net class USB3_85R: inner", "net class USB3_85R: inner pair",
    ]
    assert rules["net class GMSL_50R: inner"].endswith(
        '(rule "net class GMSL_50R: inner"\n'
        "  (layer inner)\n"
        "  (condition \"A.hasNetclass('GMSL_50R')\")\n"
        "  (constraint track_width (opt 0.11mm)))\n"
    )
    assert rules["net class USB3_85R: inner pair"].endswith(
        '(rule "net class USB3_85R: inner pair"\n'
        "  (layer inner)\n"
        "  (condition \"A.hasNetclass('USB3_85R') && A.inDiffPair('*')\")\n"
        "  (constraint track_width (opt 0.13mm))\n"
        "  (constraint diff_pair_gap (opt 0.15mm)))\n"
    )
    # a class's single-ended width is not overridden on the inner layers, but
    # its rule still says so, and comes before the pair rule (the last match wins)
    assert "(constraint track_width (opt 0.2mm))" in rules["net class USB3_85R: inner"]
    # every generated rule is a parseable rule carrying the marker comment
    for name, text in rules.items():
        assert parse_dru_rules(text) == [(name, text)]
        assert text.startswith("# Generated from the design's netclass_defs")
    assert net_class_layer_rules([net_class("PLAIN", track_width=0.3)]) == []


def test_layer_rules_order_named_layers_after_outer_and_inner():
    cls = net_class("Z", layers={
        "In7.Cu": {"track_width": 0.12}, "B.Cu": {"track_width": 0.3}, "inner": {"track_width": 0.11},
        "F.Cu": {"clearance": 0.25}, "outer": {"track_width": 0.18}, "In2.Cu": {"track_width": 0.1},
    })
    rules = net_class_layer_rules([cls])
    assert [name for name, _ in rules if not name.endswith("pair")] == [
        f"net class Z: {scope}" for scope in ("outer", "inner", "F.Cu", "In2.Cu", "In7.Cu", "B.Cu")
    ]
    by_name = dict(rules)
    assert '(layer "In7.Cu")' in by_name["net class Z: In7.Cu"]
    # F.Cu only overrides the clearance: its width is still the outer one
    assert "(constraint track_width (opt 0.18mm))" in by_name["net class Z: F.Cu"]
    assert "(constraint clearance (min 0.25mm))" in by_name["net class Z: F.Cu"]
    assert "clearance" not in by_name["net class Z: outer"]


def test_pair_gap_tighter_than_the_clearance_gets_its_own_rule():
    cls = net_class("P2", clearance=0.2, diff_pair_gap=0.25, layers={"inner": {"diff_pair_gap": 0.1}})
    rules = dict(net_class_layer_rules([cls]))
    assert list(rules) == ["net class P2: inner", "net class P2: inner pair", "net class P2: inner pair gap"]
    assert rules["net class P2: inner pair gap"].endswith(
        '(rule "net class P2: inner pair gap"\n'
        "  (layer inner)\n"
        "  (condition \"A.hasNetclass('P2') && AB.isCoupledDiffPair()\")\n"
        "  (constraint clearance (min 0.1mm)))\n"
    )


def test_layer_rules_are_written_below_the_header_and_follow_python(tmp_path):
    dru = tmp_path / "default.kicad_dru"
    existing = "(version 1)\n\n" + LM74502_RULE
    dru.write_text(existing)
    assert apply_net_class_rules(dru, []) == []
    assert dru.read_text() == existing                       # nothing declared: not touched

    assert apply_net_class_rules(dru, _defs()) == [f"per-layer net-class rules written to {dru.name}: 4"]
    text = dru.read_text()
    assert text.startswith("(version 1)\n\n# Generated from the design's netclass_defs")
    # before the part rule, which KiCad therefore lets win
    assert [name for name, _ in parse_dru_rules(text)] == [
        "net class GMSL_50R: inner", "net class GMSL_50R: inner pair",
        "net class USB3_85R: inner", "net class USB3_85R: inner pair", "LM74502 fine-pitch pins",
    ]
    assert text.endswith(LM74502_RULE)
    assert apply_net_class_rules(dru, _defs()) == []         # idempotent
    assert dru.read_text() == text

    # a changed declaration replaces the rules instead of leaving the old ones
    changed = [net_class("GMSL_50R", track_width=0.18, layers={"In7.Cu": {"track_width": 0.105}})]
    assert apply_net_class_rules(dru, changed) == [f"per-layer net-class rules written to {dru.name}: 2"]
    text = dru.read_text()
    assert [name for name, _ in parse_dru_rules(text)] == [
        "net class GMSL_50R: In7.Cu", "net class GMSL_50R: In7.Cu pair", "LM74502 fine-pitch pins",
    ]
    assert "0.105mm" in text and "USB3_85R" not in text

    # and a declaration without per-layer values removes them, leaving the file as it was
    assert apply_net_class_rules(dru, [net_class("GMSL_50R")]) == [
        f"removed 2 generated net-class rule(s) from {dru.name}"
    ]
    assert dru.read_text() == existing


def test_layer_rules_create_the_file_and_leave_hand_written_rules_alone(tmp_path):
    dru = tmp_path / "default.kicad_dru"
    assert apply_net_class_rules(dru, [net_class("PLAIN")]) == []
    assert not dru.exists()
    apply_net_class_rules(dru, _defs())
    assert dru.read_text().startswith("(version 1)\n\n")
    # a rule someone named like ours, but did not generate, is theirs
    mine = '(rule "net class GMSL_50R: inner"\n  (constraint track_width (min 0.5mm)))\n'
    dru.write_text("(version 1)\n\n# my own\n" + mine)
    apply_net_class_rules(dru, [])
    assert dru.read_text() == "(version 1)\n\n# my own\n" + mine


def test_sync_stamps_declared_classes_and_layer_rules(tmp_path, fab_rules):
    pro = _pro(tmp_path, {"classes": [dict(STOCK_DEFAULT)], "meta": {"version": 4},
                          "netclass_patterns": [{"netclass": "USB3_85R", "pattern": "UPHY_*"}]})
    write_board_setup(tmp_path, "default", {"copper_layers": 10, "net_classes": _defs()})
    notes = fab_rules.apply_fab_rules(tmp_path, "default", [("LM74502 fine-pitch pins", LM74502_RULE)])
    assert set(_classes(pro)) == {"Default", "Power_24V", "Power_24V_SRC", "Power_5V", "GMSL_50R", "USB3_85R"}
    names = [n for n, _ in parse_dru_rules((tmp_path / "default.kicad_dru").read_text())]
    assert names[:4] == ["net class GMSL_50R: inner", "net class GMSL_50R: inner pair",
                         "net class USB3_85R: inner", "net class USB3_85R: inner pair"]
    assert names[4:] == ["same-net hole to hole", "via-via copper spacing", "THT pads: thermal relief",
                         "LM74502 fine-pitch pins"]
    assert any("per-layer net-class rules" in n for n in notes)
    assert json.loads(pro.read_text())["net_settings"]["netclass_patterns"] == [
        {"netclass": "USB3_85R", "pattern": "UPHY_*"}
    ]
    assert fab_rules.apply_fab_rules(tmp_path, "default", [("LM74502 fine-pitch pins", LM74502_RULE)]) == []
