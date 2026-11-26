"""The generated per-layer net-class rules, loaded by KiCad's own DRC.

KiCad reports nothing when a .kicad_dru does not parse: it drops every rule
in the file. So the rules are checked where it matters, in `kicad-cli pcb
drc` on a small generated board: a sentinel rule appended after them proves
the file was read to its end, and the same rules with their optimum values
turned into hard limits show each condition and layer clause matching the
tracks it should. Skipped where KiCad is not installed.
"""

import json
import re
import subprocess
import textwrap

import pytest

from circuit_synth.pcb.scripts.design_rules import apply_net_class_rules, apply_net_classes, net_class
from circuit_synth.pcb.scripts.kicadpcb import find_kicad_cli, find_kicad_python

DEFS = [
    net_class("T1", track_width=0.2, layers={
        "outer": {"track_width": 0.18}, "inner": {"track_width": 0.11}, "In2.Cu": {"track_width": 0.12}}),
    net_class("P1", diff_pair_width=0.19, diff_pair_gap=0.15, layers={"inner": {"diff_pair_width": 0.13}}),
    net_class("P2", clearance=0.2, diff_pair_width=0.15, diff_pair_gap=0.25, layers={"inner": {"diff_pair_gap": 0.1}}),
]
ASSIGN = {"T1": ["SE*"], "P1": ["USB_?", "LONE"], "P2": ["X_?"]}

# name -> (net, layer, width, y): 10mm tracks, 3mm apart
SINGLES = {
    "T1 outer ok": ("SE1", "F.Cu", 0.18, 5), "T1 outer wrong": ("SE1", "B.Cu", 0.20, 8),
    "T1 inner ok": ("SE1", "In1.Cu", 0.11, 11), "T1 inner wrong": ("SE1", "In1.Cu", 0.12, 14),
    "T1 In2 ok": ("SE1", "In2.Cu", 0.12, 17), "T1 In2 wrong": ("SE1", "In2.Cu", 0.11, 20),
    "P1 pair net ok": ("USB_P", "In1.Cu", 0.13, 23), "P1 pair net wrong": ("USB_P", "In1.Cu", 0.20, 26),
    "P1 lone net ok": ("LONE", "In1.Cu", 0.20, 29), "P1 lone net wrong": ("LONE", "In1.Cu", 0.13, 32),
    "other net": ("OTHER", "In1.Cu", 0.30, 35),
}
# name -> (P net, N net, layer, width, gap, y): coupled tracks
PAIRS = {
    "P1 gap ok": ("USB_P", "USB_N", "In2.Cu", 0.13, 0.15, 40), "P1 gap wrong": ("USB_P", "USB_N", "In1.Cu", 0.13, 0.30, 43),
    "P2 inner gap 0.1": ("X_P", "X_N", "In1.Cu", 0.15, 0.10, 46), "P2 outer gap 0.1": ("X_P", "X_N", "F.Cu", 0.15, 0.10, 49),
}

BOARD = textwrap.dedent('''
    import json, sys
    import pcbnew
    singles, pairs = json.loads(sys.argv[2]), json.loads(sys.argv[3])
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(4)
    nets = {}
    def track(net, layer, width, x0, y):
        if net not in nets:
            nets[net] = pcbnew.NETINFO_ITEM(board, net)
            board.Add(nets[net])
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I_MM(x0, y)); t.SetEnd(pcbnew.VECTOR2I_MM(x0 + 10, y))
        t.SetWidth(pcbnew.FromMM(width)); t.SetLayer(board.GetLayerID(layer)); t.SetNet(nets[net])
        board.Add(t)
    for net, layer, width, y in singles.values():
        track(net, layer, width, 5, y)
    for p, n, layer, width, gap, y in pairs.values():
        track(p, layer, width, 20, y)
        track(n, layer, width, 20, y + width + gap)
    pcbnew.SaveBoard(sys.argv[1], board)
''')

SENTINEL = '\n(rule "sentinel"\n  (condition "A.Type == \'Track\'")\n  (constraint track_width (min 5mm)))\n'
TRACKS = len(SINGLES) + 2 * len(PAIRS)


@pytest.fixture(scope="module")
def drc(tmp_path_factory):
    try:
        python, cli = find_kicad_python(), find_kicad_cli()
    except RuntimeError:
        pytest.skip("KiCad (kicad-cli and a pcbnew-capable Python) is not installed")
    work = tmp_path_factory.mktemp("drc")
    (work / "board.py").write_text(BOARD)
    res = subprocess.run([python, str(work / "board.py"), str(work / "t.kicad_pcb"), json.dumps(SINGLES),
                          json.dumps(PAIRS)], capture_output=True, text=True)
    assert (work / "t.kicad_pcb").exists(), res.stderr[-3000:]
    apply_net_classes(work / "t.kicad_pro", ASSIGN, DEFS)
    apply_net_class_rules(work / "t.kicad_dru", DEFS)
    generated = (work / "t.kicad_dru").read_text()

    def run(rules: str) -> dict[str, list[str]]:
        """Violations by the name of the track (or pair) they are on."""
        (work / "t.kicad_dru").write_text(rules)
        report = work / "drc.json"
        subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report),
                        str(work / "t.kicad_pcb")], capture_output=True)
        where = {y: name for name, (*_, y) in {**SINGLES, **PAIRS}.items()}
        found: dict[str, list[str]] = {}
        for v in json.loads(report.read_text())["violations"]:
            if v["type"] not in ("track_width", "clearance", "diff_pair_gap_out_of_range"):
                continue  # dangling track ends, the missing board outline
            name = where[min(where, key=lambda y: abs(y - v["items"][0]["pos"]["y"]))]
            found.setdefault(name, []).append(v["description"])
        return found

    return generated, run


def _real(found):
    """Violations other than the sentinel's."""
    return {name: [d for d in ds if "min width 5.0000" not in d] for name, ds in found.items()
            if any("min width 5.0000" not in d for d in ds)}


def test_kicad_reads_the_generated_rules_to_the_end(drc):
    generated, run = drc
    found = run(generated + SENTINEL)
    assert sum("min width 5.0000" in d for ds in found.values() for d in ds) == TRACKS
    # as generated the widths and gaps are optimums, not limits: nothing else fires,
    # except the clearance the outer layers still demand of a 0.1mm pair gap
    assert list(_real(found)) == ["P2 outer gap 0.1"]
    assert "netclass 'P2' clearance 0.2000 mm; actual 0.1000 mm" in _real(found)["P2 outer gap 0.1"][0]


def test_the_sentinel_does_notice_a_dropped_file(drc):
    generated, run = drc
    broken = generated.replace("(constraint track_width", "(constraint track_widthh", 1)
    assert not any("min width 5.0000" in d for ds in run(broken + SENTINEL).values() for d in ds)


def test_conditions_and_layer_clauses_match_the_right_tracks(drc):
    generated, run = drc
    # the same rules as hard limits, to see where each one applies
    found = run(re.sub(r"\(opt ([0-9.]+mm)\)", r"(min \1) (max \1)", generated))
    assert sorted(found) == sorted([
        "T1 outer wrong", "T1 inner wrong", "T1 In2 wrong",   # outer / inner / a layer name, which beats inner
        "P1 pair net wrong", "P1 lone net wrong",              # the pair rule beats the single-ended one for pair nets only
        "P1 gap wrong", "P2 outer gap 0.1",
    ])
    assert "rule 'net class T1: outer' max width 0.1800 mm; actual 0.2000 mm" in found["T1 outer wrong"][0]
    assert "rule 'net class T1: inner' max width 0.1100 mm; actual 0.1200 mm" in found["T1 inner wrong"][0]
    assert "min width 0.1200 mm; actual 0.1100 mm" in found["T1 In2 wrong"][0]
    assert "rule 'net class P1: inner pair' max width 0.1300 mm; actual 0.2000 mm" in found["P1 pair net wrong"][0]
    assert "min width 0.2000 mm; actual 0.1300 mm" in found["P1 lone net wrong"][0]
    assert "net class P1: inner pair maximum gap 0.1500 mm; actual 0.3000 mm" in found["P1 gap wrong"][0]
    # (This board keeps the P1 pair on inner layers only. KiCad 9's pair-gap test files all of
    # a pair's segments under the first gap rule it meets, so with segments on an outer layer
    # too, a gap limit that differs per layer is not reliably reported. The generated rules
    # only set optimums, which DRC does not check either way.)


def test_pair_gap_rule_lets_a_pair_sit_closer_than_the_clearance_on_that_layer_only(drc):
    generated, run = drc
    assert "P2 inner gap 0.1" not in _real(run(generated + SENTINEL))
    without = "\n\n".join(rule for rule in generated.split("\n\n") if "pair gap" not in rule)
    found = _real(run(without + SENTINEL))
    assert sorted(found) == ["P2 inner gap 0.1", "P2 outer gap 0.1"]
    assert "clearance 0.2000 mm; actual 0.1000 mm" in found["P2 inner gap 0.1"][0]
