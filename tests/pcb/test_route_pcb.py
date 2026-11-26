"""`cs pcb route` on a real (generated) board, under KiCad's pcbnew Python.

Skipped where no pcbnew-capable interpreter is installed. The geometry is
unit-tested in test_routing_geometry.py and the net-class resolution in
tests/unit/test_design_rules.py; this checks the Router puts them on a
board: layers from the board, via tokens, pairs, class-driven sizes, and
the defaults a design without its own net classes keeps.
"""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_synth.pcb import scripts
from circuit_synth.pcb.scripts.kicadpcb import find_kicad_python

SCRIPTS = Path(scripts.__path__[0])

DRIVER = textwrap.dedent('''
    import contextlib, io, json, sys
    from pathlib import Path

    sys.path.insert(0, sys.argv[1])
    work = Path(sys.argv[2])
    import pcbnew
    import route_pcb
    from design_rules import NetRules, apply_net_classes, net_class, write_board_setup

    DEFS = [
        net_class("SE_50R", track_width=0.18, via_diameter=0.5, via_drill=0.3, layers={"inner": {"track_width": 0.11}}),
        net_class("DP_85R", diff_pair_width=0.19, diff_pair_gap=0.15, diff_pair_via_gap=0.3,
                  layers={"inner": {"diff_pair_width": 0.13}}),
    ]
    ASSIGN = {"SE_50R": ["COAX"], "DP_85R": ["USB_?"], "Power_5V": ["V5V"]}

    def make(project, declare):
        project.mkdir()
        board = pcbnew.CreateEmptyBoard()
        board.SetCopperLayerCount(6)
        nets = {}
        for name in ("COAX", "USB_P", "USB_N", "V5V"):
            nets[name] = pcbnew.NETINFO_ITEM(board, name)
            board.Add(nets[name])
        def part(ref, pads):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            board.Add(fp)
            for number, x, y, net in pads:
                pad = pcbnew.PAD(fp)
                pad.SetNumber(number)
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.PAD.SMDMask())
                pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I_MM(0.25, 0.25))
                pad.SetPosition(pcbnew.VECTOR2I_MM(x, y))
                pad.SetNet(nets[net])
                fp.Add(pad)
        part("J1", [("1", 10, 9.75, "USB_P"), ("2", 10, 10.25, "USB_N"), ("3", 10, 20, "COAX"), ("4", 10, 30, "V5V")])
        part("U1", [("1", 40, 9.75, "USB_P"), ("2", 40, 10.25, "USB_N"), ("3", 40, 20, "COAX"), ("4", 40, 30, "V5V")])
        pcbnew.SaveBoard(str(project / "default.kicad_pcb"), board)
        # the plain project is one written before designs could declare classes
        assign = ASSIGN if declare else {"Power_5V": ["V5V"]}
        apply_net_classes(project / "default.kicad_pro", assign, DEFS if declare else [])
        if declare:
            write_board_setup(project, "default", {"copper_layers": 6, "net_classes": DEFS})

    def copper(project):
        board = pcbnew.LoadBoard(str(project / "default.kicad_pcb"))
        tracks, vias = [], []
        for t in board.GetTracks():
            if t.GetClass() == "PCB_VIA":
                vias.append([t.GetNetname(), pcbnew.ToMM(t.GetPosition().x), pcbnew.ToMM(t.GetPosition().y),
                             pcbnew.ToMM(t.GetWidth(pcbnew.F_Cu)), pcbnew.ToMM(t.GetDrillValue()),
                             board.GetLayerName(t.TopLayer()), board.GetLayerName(t.BottomLayer())])
            else:
                tracks.append([t.GetNetname(), board.GetLayerName(t.GetLayer()), pcbnew.ToMM(t.GetWidth()),
                               [pcbnew.ToMM(v) for v in (t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y)]])
        return {"tracks": sorted(tracks), "vias": sorted(vias)}

    def run(project, routing, *flags):
        (project / "routing.py").write_text(routing)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = route_pcb.main([str(project), "default", *flags])
        return {"code": code, "out": out.getvalue(), **copper(project)}

    result = {}
    declared, plain = work / "declared", work / "plain"
    make(declared, True)
    make(plain, False)
    board = pcbnew.LoadBoard(str(declared / "default.kicad_pcb"))
    router = route_pcb.Router(board, NetRules.load(declared, "default"))
    result["layers"] = router.layers
    result["declared_defaults"] = [router.width, router.via_size]
    legacy = route_pcb.Router(pcbnew.LoadBoard(str(plain / "default.kicad_pcb")), NetRules.load(plain, "default"))
    result["plain_defaults"] = [legacy.width, list(legacy.via_size)]

    result["single"] = run(declared, """
    def route(r):
        r.route("COAX", "J1.3", (15, 20), "via:In4.Cu", (35, 20), "via:F.Cu", "U1.3")
        r.route("V5V", "J1.4", (20, 30), "via", (30, 30), "via", "U1.4")
    """, "--no-fill", "--lengths=COAX")
    result["pair"] = run(declared, """
    def route(r):
        report = r.pair(("USB_P", "USB_N"), ("J1.1", "J1.2"), (11, 10), (20, 10), "via:In2.Cu",
                        (30, 10), "via:F.Cu", (39, 10), "ds", ("U1.1", "U1.2"))
        print("REPORT", report.p_length, report.n_length, report.skew, report.via_pairs)
    """, "--no-fill")
    result["plain"] = run(plain, """
    def route(r):
        r.route("COAX", "J1.3", (15, 20), "via:In4.Cu", (35, 20), "via:F.Cu", "U1.3")
        r.route("V5V", "J1.4", (20, 30), "via", (30, 30), "via", "U1.4")
    """, "--no-fill")
    for name, routing in {
        "bad_via_layer": 'r.route("COAX", "J1.3", (15, 20), "via:In7.Cu", "U1.3")',
        "bad_start_layer": 'r.route("COAX", "J1.3", "U1.3", layer="In9.Cu")',
        "bad_zone_layer": 'r.zone("COAX", ["In5.Cu"], [(0, 0), (5, 0), (5, 5)])',
        "pair_crossed": 'r.pair(("USB_P", "USB_N"), ("J1.1", "J1.2"), (11, 10), (39, 10), ("U1.1", "U1.2"), p="right")',
        "pair_no_side": 'r.pair(("USB_P", "USB_N"), (11, 10), (39, 10))',
        "pair_turn_at_via": 'r.pair(("USB_P", "USB_N"), ("J1.1", "J1.2"), (11, 10), (20, 10), "via:In2.Cu", (20, 15))',
        "pair_mixed_class": 'r.pair(("USB_P", "COAX"), (11, 10), (39, 10), p="left")',
    }.items():
        result[name] = run(declared, "def route(r):\\n    " + routing + "\\n", "--no-fill")["out"]
    print("RESULT " + json.dumps(result))
''')

LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]


@pytest.fixture(scope="module")
def routed(tmp_path_factory):
    try:
        python = find_kicad_python()
    except RuntimeError:
        pytest.skip("no pcbnew-capable Python (install KiCad 9 or set KICAD_PYTHON)")
    work = tmp_path_factory.mktemp("route")
    driver = work / "driver.py"
    driver.write_text(DRIVER)
    res = subprocess.run([python, str(driver), str(SCRIPTS), str(work)], capture_output=True, text=True)
    lines = [line for line in res.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"driver failed:\n{res.stdout[-2000:]}\n{res.stderr[-4000:]}"
    return json.loads(lines[-1][len("RESULT "):])


def _widths(run, net):
    return sorted({(layer, width) for name, layer, width, _ in run["tracks"] if name == net})


def test_layers_come_from_the_board(routed):
    assert routed["layers"] == LAYERS


def test_a_design_with_its_own_net_classes_is_class_driven(routed):
    assert routed["declared_defaults"] == [None, None]
    assert routed["plain_defaults"] == [0.2, [0.6, 0.3]]


def test_via_token_continues_on_the_named_layer_with_that_layers_width(routed):
    run = routed["single"]
    assert run["code"] == 0
    assert _widths(run, "COAX") == [("F.Cu", 0.18), ("In4.Cu", 0.11)]
    coax_vias = [v for v in run["vias"] if v[0] == "COAX"]
    # through vias (F.Cu to B.Cu) of the class's size, where the tokens are
    assert coax_vias == [["COAX", 15.0, 20.0, 0.5, 0.3, "F.Cu", "B.Cu"], ["COAX", 35.0, 20.0, 0.5, 0.3, "F.Cu", "B.Cu"]]
    inner = [ends for name, layer, _, ends in run["tracks"] if name == "COAX" and layer == "In4.Cu"]
    assert inner == [[15.0, 20.0, 35.0, 20.0]]


def test_bare_via_still_swaps_front_and_back_and_standard_classes_apply(routed):
    run = routed["single"]
    # V5V is in the standard Power_5V class (0.3mm, 0.8/0.4 vias): class-driven here
    assert _widths(run, "V5V") == [("B.Cu", 0.3), ("F.Cu", 0.3)]
    assert [v[3:5] for v in run["vias"] if v[0] == "V5V"] == [[0.8, 0.4], [0.8, 0.4]]


def test_length_report_lists_layers_and_vias(routed):
    out = routed["single"]["out"]
    assert "lengths (mm), 1 net(s):" in out
    row = next(line for line in out.splitlines() if line.strip().startswith("COAX"))
    assert row.split() == ["COAX", "30.000", "2", "F.Cu", "10.000,", "In4.Cu", "20.000"]


def test_design_without_net_classes_keeps_the_old_defaults(routed):
    run = routed["plain"]
    assert run["code"] == 0
    # 0.2mm and 0.6/0.3 everywhere, whatever class the net is in: what routing
    # files written before net classes drove the Router rely on
    assert _widths(run, "COAX") == [("F.Cu", 0.2), ("In4.Cu", 0.2)]
    assert _widths(run, "V5V") == [("B.Cu", 0.2), ("F.Cu", 0.2)]
    assert {tuple(v[3:5]) for v in run["vias"]} == {(0.6, 0.3)}


def test_pair_runs_both_traces_with_the_layers_geometry_and_paired_vias(routed):
    run = routed["pair"]
    assert run["code"] == 0, run["out"]
    assert _widths(run, "USB_P") == _widths(run, "USB_N") == [("F.Cu", 0.19), ("In2.Cu", 0.13)]
    # vias 0.6 + 0.3 apart across the route, P above N like the pads
    assert run["vias"] == [
        ["USB_N", 20.0, 10.45, 0.6, 0.3, "F.Cu", "B.Cu"], ["USB_N", 30.0, 10.45, 0.6, 0.3, "F.Cu", "B.Cu"],
        ["USB_P", 20.0, 9.55, 0.6, 0.3, "F.Cu", "B.Cu"], ["USB_P", 30.0, 9.55, 0.6, 0.3, "F.Cu", "B.Cu"],
    ]
    # coupled on In2 at 0.13 + 0.15 pitch between the fan-outs
    inner = sorted(ends for name, layer, _, ends in run["tracks"] if layer == "In2.Cu" and ends[1] == ends[3])
    assert inner == [pytest.approx([20.31, 9.86, 29.69, 9.86]), pytest.approx([20.31, 10.14, 29.69, 10.14])]
    # every trace starts and ends on its own pads
    for net, y in (("USB_P", 9.75), ("USB_N", 10.25)):
        points = {(e[0], e[1]) for n, _, _, e in run["tracks"] if n == net} | {
            (e[2], e[3]) for n, _, _, e in run["tracks"] if n == net}
        assert (10.0, y) in points and (40.0, y) in points


def test_pair_reports_lengths_and_skew(routed):
    out = routed["pair"]["out"]
    p_length, n_length, skew, via_pairs = (float(v) for v in out.split("REPORT ")[1].split()[:4])
    # mirror-image traces: no skew
    assert p_length == pytest.approx(n_length) and skew == pytest.approx(0, abs=1e-9)
    # per outer section: 0.92 out of the pad, 0.08 jog onto the trace, 8.72 coupled, 0.28 fan-out
    # to the via (jogs and fan-outs on diagonals); between the vias 9.38 coupled and two 0.31 fan-outs
    diagonal = 2 ** 0.5
    assert p_length == pytest.approx(2 * (0.92 + 0.08 * diagonal + 8.72 + 0.28 * diagonal) + 9.38 + 2 * 0.31 * diagonal)
    assert via_pairs == 2
    assert "pairs (mm; skew = P - N):" in out and "USB_P / USB_N: P " in out and "2 via pair(s)" in out


@pytest.mark.parametrize("case, message", [
    ("bad_via_layer", "routing error: no copper layer 'In7.Cu' on the board; valid: F.Cu, In1.Cu, In2.Cu, In3.Cu, In4.Cu, B.Cu"),
    ("bad_start_layer", "routing error: no copper layer 'In9.Cu' on the board; valid: F.Cu, In1.Cu, In2.Cu, In3.Cu, In4.Cu, B.Cu"),
    ("bad_zone_layer", "routing error: no copper layer 'In5.Cu' on the board; valid: F.Cu,"),
    ("pair_crossed", "puts P on the other side from its start pad"),
    ("pair_no_side", "pass p='left' or p='right'"),
    ("pair_turn_at_via", "must leave its vias"),
    ("pair_mixed_class", "different net classes (DP_85R, SE_50R)"),
])
def test_inconsistent_routing_is_a_routing_error(routed, case, message):
    assert message in routed[case]
