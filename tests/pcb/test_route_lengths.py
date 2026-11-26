"""Length matching in `cs pcb route` on a real (generated) board.

Skipped where KiCad is not installed. The meander and bump geometry is
unit-tested in test_routing_geometry.py and the via heights in
tests/unit/test_stackup.py; this checks what the Router does with them on a
6-layer board with a declared stack-up and net classes:

- pair A is routed with two bends the same way (deliberate skew), unequal
  pin lengths, and a hop through B.Cu; its skew is compensated with bumps;
- pair B runs straight on an inner layer and is lengthened to pair A;
- CLK is a single trace lengthened to a given length;
- the two pairs form one match group, CLK another.

The reported lengths are compared with lengths measured here from the
written board file (segments, and via heights worked out from the stack-up
numbers by hand), and with KiCad's own figure, read from a DRC `length`
rule. KiCad's DRC then has to pass with no violations at all.
"""

import json
import math
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

from circuit_synth.pcb import scripts
from circuit_synth.pcb.scripts.kicadpcb import find_kicad_cli, find_kicad_python, items, one, parse_sexpr

SCRIPTS = Path(scripts.__path__[0])

# The stack-up the board declares: 6 layers, 1oz outside and 0.5oz inside.
OUTER, INNER, PREPREG, CORE = 0.035, 0.0152, 0.1194, 0.2
# Depth of the middle of each copper layer below the top of the board,
# added up by hand (not with the tool's own stackup.copper_depths).
DEPTH = {
    "F.Cu": OUTER / 2,
    "In1.Cu": OUTER + PREPREG + INNER / 2,
    "In2.Cu": OUTER + PREPREG + INNER + CORE + INNER / 2,
    "In3.Cu": OUTER + PREPREG + INNER + CORE + INNER + PREPREG + INNER / 2,
    "In4.Cu": OUTER + PREPREG + INNER + CORE + INNER + PREPREG + INNER + CORE + INNER / 2,
    "B.Cu": OUTER + 4 * INNER + 3 * PREPREG + 2 * CORE + OUTER / 2,
}
# Pin lengths the routing file declares, and one a pad carries on the board.
DECLARED_PADS = {"A_P": 0.2, "A_N": 0.5}
BOARD_PADS = {"B_N": 0.25}

DRIVER = textwrap.dedent('''
    import contextlib, io, json, sys
    from pathlib import Path

    # routing.py is rewritten many times a second here; a cached .pyc of the
    # same age and size would be run in its place
    sys.dont_write_bytecode = True
    sys.path.insert(0, sys.argv[1])
    work = Path(sys.argv[2])
    import pcbnew
    import route_pcb
    from design_rules import apply_net_classes, net_class, write_board_setup
    from fab_rules import apply_fab_rules
    from stackup import dielectric, stackup, write_stackup

    PREPREG, CORE = dielectric("prepreg", 0.1194), dielectric("core", 0.2)
    STACKUP = stackup([PREPREG, CORE, PREPREG, CORE, PREPREG])
    DEFS = [
        net_class("SE_50R", track_width=0.18, layers={"inner": {"track_width": 0.11}}),
        net_class("DIFF_90R", diff_pair_width=0.17, diff_pair_gap=0.15, diff_pair_via_gap=0.3,
                  layers={"inner": {"diff_pair_width": 0.115}}),
    ]
    ASSIGN = {"SE_50R": ["CLK"], "DIFF_90R": ["A_?", "B_?"]}
    NETS = ("A_P", "A_N", "B_P", "B_N", "CLK")

    def make(project, declare_stackup=True):
        project.mkdir()
        board = pcbnew.CreateEmptyBoard()
        board.SetCopperLayerCount(6)
        nets = {}
        for name in NETS:
            nets[name] = pcbnew.NETINFO_ITEM(board, name)
            board.Add(nets[name])
        for a, b in (((2, 2), (48, 2)), ((48, 2), (48, 48)), ((48, 48), (2, 48)), ((2, 48), (2, 2))):
            edge = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
            edge.SetStart(pcbnew.VECTOR2I_MM(*a)); edge.SetEnd(pcbnew.VECTOR2I_MM(*b))
            edge.SetLayer(pcbnew.Edge_Cuts); edge.SetWidth(pcbnew.FromMM(0.1))
            board.Add(edge)
        def part(ref, pads):
            fp = pcbnew.FOOTPRINT(board)
            fp.SetReference(ref)
            # bare pads: no silkscreen for DRC to find overlapping
            fp.Reference().SetVisible(False)
            fp.Value().SetVisible(False)
            board.Add(fp)
            for number, x, y, net, die in pads:
                pad = pcbnew.PAD(fp)
                pad.SetNumber(number)
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pcbnew.PAD.SMDMask())
                pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I_MM(0.25, 0.25))
                pad.SetPosition(pcbnew.VECTOR2I_MM(x, y))
                pad.SetNet(nets[net])
                if die:
                    pad.SetPadToDieLength(pcbnew.FromMM(die))
                fp.Add(pad)
        part("J1", [("1", 10, 9.75, "A_P", 0), ("2", 10, 10.25, "A_N", 0), ("3", 10, 29.75, "B_P", 0),
                    ("4", 10, 30.25, "B_N", 0), ("5", 10, 40, "CLK", 0)])
        # pair A arrives at U1 heading south (P on the east side), pair B and CLK heading east
        part("U1", [("1", 30.25, 26, "A_P", 0), ("2", 29.75, 26, "A_N", 0), ("3", 40, 29.75, "B_P", 0),
                    ("4", 40, 30.25, "B_N", 0.25), ("5", 40, 40, "CLK", 0)])
        pcb = project / "default.kicad_pcb"
        pcbnew.SaveBoard(str(pcb), board)
        write_stackup(pcb, STACKUP)
        apply_net_classes(project / "default.kicad_pro", ASSIGN, DEFS)
        setup = {"copper_layers": 6, "net_classes": DEFS}
        if declare_stackup:
            setup["stackup"] = STACKUP
        write_board_setup(project, "default", setup)
        apply_fab_rules(project, "default")

    def run(project, routing, *flags):
        (project / "routing.py").write_text(routing)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = route_pcb.main([str(project), "default", "--no-fill", *flags])
        return {"code": code, "out": out.getvalue()}

    PAIR_A = """
        a = r.pair(("A_P", "A_N"), ("J1.1", "J1.2"), (11, 10), SKEW_A (19, 10), (21, 10), "via:B.Cu",
                   (25, 10), (30, 15), (30, 20), "via:F.Cu", (30, 25), ("U1.1", "U1.2")SKEW_KW)
        print("PAIR_A", a.p_length, a.n_length, a.skew, a.length, a.via_pairs)
    """
    PAIR_B = """
        b = r.pair(("B_P", "B_N"), ("J1.3", "J1.4"), (11, 30), (14, 30), "via:In2.Cu",
                   MEANDER_B (34, 30), "via:F.Cu", SKEW_B (39, 30), ("U1.3", "U1.4")TUNE_B)
        print("PAIR_B", b.p_length, b.n_length, b.skew, b.length, b.via_pairs)
    """
    CLK = """
        r.route("CLK", "J1.5", (15, 40), "via:In4.Cu", MEANDER_CLK (35, 40), "via:F.Cu", "U1.5"TUNE_CLK)
        print("CLK", r.length("CLK"))
    """

    def routing(tuned=True, extra=""):
        text = "def route(r):\\n    r.pad_lengths({\\"J1.1\\": 0.2, \\"J1.2\\": 0.5})\\n" + PAIR_A + PAIR_B + CLK + extra
        swaps = {
            "SKEW_A": '"skew",', "SKEW_KW": ", skew=0.02",
            "MEANDER_B": '"meander:left:1.5",', "SKEW_B": '"skew",', "TUNE_B": ", skew=0.02, length=a.length",
            "MEANDER_CLK": '"meander:1.0",', "TUNE_CLK": ", length=38.0",
        }
        for key, value in swaps.items():
            text = text.replace(key, value if tuned else "")
        return text

    GROUPS = """
        r.match("LANES", ["A_?", "B_?"], tolerance=0.05)
        r.match("CLOCK", "CLK", tolerance=0.01, length=38.0)
    """

    result = {}
    tuned, plain, bare, hops, scratch = (work / name for name in ("tuned", "plain", "bare", "hops", "scratch"))
    if sys.argv[3] == "make":
        # The boards are made in a process of their own, as `cs pcb sync` and `cs pcb route` are:
        # pcbnew keeps the project of a board it saved, without the net classes stamped into the
        # file afterwards, and would write that back over them on the next save.
        make(tuned)
        make(plain)
        make(bare, declare_stackup=False)
        make(hops)
        make(scratch)
        sys.exit(0)
    # as planned: skew compensated, pair B lengthened to pair A, the groups hold
    result["tuned"] = run(tuned, routing(extra=GROUPS), "--lengths")
    # the same waypoints without any tuning: the groups are out of tolerance
    result["plain"] = run(plain, routing(tuned=False, extra=GROUPS), "--lengths=A_*")
    # a design that declares no stack-up
    result["bare"] = run(bare, routing(extra=GROUPS))
    pair_a = 'r.pair(("A_P", "A_N"), ("J1.1", "J1.2"), (11, 10), %s (19, 10), (21, 10), "via:B.Cu", ' \\
             '(25, 10), (30, 15), (30, 20), "via:F.Cu", (30, 25), ("U1.1", "U1.2")%s)'
    pair_b = 'r.pair(("B_P", "B_N"), ("J1.3", "J1.4"), (11, 30), %s (39, 30), ("U1.3", "U1.4")%s)'
    clk = 'r.route("CLK", "J1.5", %s "U1.5"%s)'
    for name, line in {
        "skew_without_a_hop": pair_a % ("", ", skew=0.02"),
        "skew_hop_without_a_limit": pair_a % ('"skew",', ""),
        "skew_hop_too_short": 'r.pair(("A_P", "A_N"), ("J1.1", "J1.2"), (11, 10), "skew", (12.5, 10), (21, 10), "via:B.Cu", '
                              '(25, 10), (30, 15), (30, 20), "via:F.Cu", (30, 25), ("U1.1", "U1.2"), skew=0.02)',
        "skew_on_a_single_trace": clk % ('"skew",', ""),
        "meander_without_a_length": pair_b % ('"meander:left:1.0",', ""),
        "length_without_a_hop": pair_b % ("", ", length=40"),
        "length_already_longer": pair_b % ('"meander:left:1.0",', ", length=25"),
        "meander_without_room": pair_b % ('"meander:left:0.3",', ", length=60"),
        "meander_hop_not_straight": clk % ('(15, 40), "meander:left:1.0", (30, 43),', ", length=38"),
        "meander_bad_side": clk % ('(15, 40), "meander:up:1.0", (30, 40),', ", length=38"),
        "meander_before_a_via": clk % ('(15, 40), "meander:left:1.0", "via:In4.Cu", (30, 40),', ", length=38"),
        "unknown_pad_length": 'r.pad_lengths({"J9.1": 1.0})',
        "match_without_nets": 'r.match("G", "NOPE_*", tolerance=0.1)',
    }.items():
        result[name] = run(scratch, "def route(r):\\n    " + line + "\\n")["out"]
    # two "skew" hops: one on the diagonal between the two bends, one after the second via; a group
    # whose members are two nets in series each; a group whose nets are not routed
    result["hops"] = run(hops, """
    def route(r):
        a = r.pair(("A_P", "A_N"), ("J1.1", "J1.2"), (11, 10), (19, 10), (21, 10), "via:B.Cu", (25, 10),
                   "skew", (30, 15), (30, 20), "via:F.Cu", "skew", (30, 25), ("U1.1", "U1.2"), skew=0.02)
        r.route("CLK", "J1.5", "U1.5")
        print("PAIR_A", a.p_length, a.n_length, a.skew, a.length, a.via_pairs)
        r.match("LINK", [("A_P", "CLK"), ("A_N", "CLK")], tolerance=0.01)
        r.match("OPEN", "B_?", tolerance=1.0)
    """)
    # copper added to a tuned net afterwards makes its target stale
    late = run(scratch, "def route(r):\\n    " + clk % ('(15, 40), "meander:left:1.0", (35, 40),', ", length=38")
               + "\\n    r.track(\\"CLK\\", [(40, 40), (40, 41)])\\n")
    result["late_copper"] = late
    print("RESULT " + json.dumps(result))
''')

MEASURE = """
(rule "measure"
  (condition "A.NetName != ''")
  (constraint length (max 0.001mm)))
"""


def _copper(pcb: Path) -> dict:
    """What is on a saved board, read from its text: per net the segments
    (layer, start, end) and via positions."""
    tree = parse_sexpr(pcb.read_text())
    names = {net[1]: net[2] for net in items(tree, "net")}
    found: dict[str, dict] = {}
    for kind in ("segment", "via"):
        for item in items(tree, kind):
            entry = found.setdefault(names[one(item, "net")[1]], {"segments": [], "vias": []})
            if kind == "via":
                entry["vias"].append(tuple(float(v) for v in one(item, "at")[1:3]))
            else:
                start, end = (tuple(float(v) for v in one(item, key)[1:3]) for key in ("start", "end"))
                entry["segments"].append((one(item, "layer")[1], start, end, float(one(item, "width")[1])))
    return found


def _measured(copper: dict, net: str, depth=DEPTH) -> dict:
    """Length of a net worked out from the board text: tracks per layer, and
    for each via the height between the layers of the segments that end on it."""
    layers: dict[str, float] = {}
    for layer, start, end, _ in copper[net]["segments"]:
        layers[layer] = layers.get(layer, 0.0) + math.dist(start, end)
    via = 0.0
    for at in copper[net]["vias"]:
        joined = {layer for layer, start, end, _ in copper[net]["segments"] if at in (start, end)}
        assert len(joined) == 2, f"{net}: via at {at} joins {joined}"
        via += abs(depth[joined.pop()] - depth[joined.pop()]) if depth else 0.0
    pads = DECLARED_PADS.get(net, 0.0) + BOARD_PADS.get(net, 0.0)
    return {"layers": layers, "track": sum(layers.values()), "via": via, "pads": pads,
            "total": sum(layers.values()) + via + pads}


def _drc(project: Path, rules: str = "") -> dict:
    """kicad-cli's DRC report for the project's board, with extra rules appended."""
    dru = project / "default.kicad_dru"
    original = dru.read_text()
    dru.write_text(original + rules)
    report = project / "drc.json"
    try:
        subprocess.run([find_kicad_cli(), "pcb", "drc", "--format", "json", "--severity-all", "-o", str(report),
                        str(project / "default.kicad_pcb")], capture_output=True)
        return json.loads(report.read_text())
    finally:
        dru.write_text(original)


@pytest.fixture(scope="module")
def routed(tmp_path_factory):
    try:
        python = find_kicad_python()
        find_kicad_cli()
    except RuntimeError:
        pytest.skip("KiCad (kicad-cli and a pcbnew-capable Python) is not installed")
    work = tmp_path_factory.mktemp("lengths")
    driver = work / "driver.py"
    driver.write_text(DRIVER)
    made = subprocess.run([python, str(driver), str(SCRIPTS), str(work), "make"], capture_output=True, text=True)
    assert made.returncode == 0, f"driver failed:\n{made.stdout[-2000:]}\n{made.stderr[-4000:]}"
    res = subprocess.run([python, str(driver), str(SCRIPTS), str(work), "route"], capture_output=True, text=True)
    lines = [line for line in res.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"driver failed:\n{res.stdout[-2000:]}\n{res.stderr[-4000:]}"
    result = json.loads(lines[-1][len("RESULT "):])
    result["work"] = work
    for name in ("tuned", "plain", "bare", "hops"):
        result[name]["copper"] = _copper(work / name / "default.kicad_pcb")
    return result


def _printed(run, tag):
    """The floats a routing file printed after `tag`."""
    line = next(line for line in run["out"].splitlines() if line.startswith(tag + " "))
    return [float(v) for v in line.split()[1:]]


def _table(run, header):
    """Rows (as word lists) of the output block that starts with `header`,
    up to the next block."""
    lines = run["out"].splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(header))
    rows = []
    for line in lines[start + 1:]:
        if len(line) - len(line.lstrip()) <= len(lines[start]) - len(lines[start].lstrip()):
            break
        rows.append(line.split())
    return rows


# ------------------------------------------------------------ the untuned route

def test_without_tuning_the_pairs_are_skewed_and_unequal(routed):
    run = routed["plain"]
    p, n, skew, length, vias = _printed(run, "PAIR_A")
    # two 45 degree bends the same way on a 0.17 + 0.15 pair: 2 x 0.828 x 0.32 longer outside,
    # less the 0.3mm more of pin length the inside trace has
    assert skew == pytest.approx(4 * math.tan(math.radians(22.5)) * 0.32 - 0.3, abs=1e-6)
    assert skew == pytest.approx(p - n) and length == pytest.approx((p + n) / 2) and vias == 2
    bp, bn, b_skew, b_length, _ = _printed(run, "PAIR_B")
    assert b_skew == pytest.approx(-0.25, abs=1e-6)          # mirror-image traces; N's pad carries 0.25mm
    # pair A: 33.626 of track (mean), 2 x 0.854 of via, 0.35 of pin; pair B: 30.578, 2 x 0.3597, 0.125
    assert length == pytest.approx(35.684, abs=2e-3) and b_length == pytest.approx(31.422, abs=2e-3)


def test_out_of_tolerance_groups_fail_the_run_but_the_board_is_written(routed):
    run = routed["plain"]
    assert run["code"] == 1
    assert "LANES: 4 net(s), target" in run["out"] and "OUT OF TOLERANCE" in run["out"]
    assert "length error: match group LANES is out of tolerance" in run["out"]
    assert "length error: match group CLOCK is out of tolerance" in run["out"]
    assert set(run["copper"]) == {"A_P", "A_N", "B_P", "B_N", "CLK"}
    rows = {row[0]: row for row in _table(run, "LANES:")}
    longest = max(float(row[1]) for row in rows.values())
    assert float(rows["A_P"][1]) == longest and float(rows["A_P"][2]) == 0
    assert float(rows["B_N"][2]) == pytest.approx(float(rows["B_N"][1]) - longest, abs=2e-3)
    assert "out" in rows["B_N"] and "out" in rows["A_N"] and "out" not in rows["A_P"]


# ------------------------------------------------------------ the tuned route

def test_tuned_run_succeeds_and_says_what_it_did(routed):
    run = routed["tuned"]
    assert run["code"] == 0, run["out"]
    out = run["out"]
    assert "via heights from the declared stack-up" in out
    tuning = "\n".join(" ".join(row) for row in _table(run, "tuning"))
    # pair A: 0.230mm of skew, taken out with bumps on the shorter trace (N), on the marked F.Cu hop
    assert re.search(r"A_P / A_N: skew \+0\.230 -> [+-]0\.000 \(allowed 0\.02\): 2 bump\(s\) of 0\.139 on A_N, F\.Cu", tuning)
    # pair B: the pad's 0.25mm made up on P, then both traces lengthened to pair A on In2
    assert re.search(r"B_P / B_N: skew -0\.250 -> [+-]0\.000 \(allowed 0\.02\): 3 bump\(s\) of 0\.101 on B_P, F\.Cu", tuning)
    assert re.search(r"B_P / B_N: length \d+\.\d+ -> \d+\.\d+: \d+ meander\(s\) of \d\.\d+ \(left\), In2\.Cu", tuning)
    assert re.search(r"CLK: length 31\.419 -> 38\.000: \d+ meander\(s\) of 0\.\d+ \(both\), In4\.Cu", tuning)


def test_skew_is_compensated_within_the_limit(routed):
    run = routed["tuned"]
    for tag in ("PAIR_A", "PAIR_B"):
        p, n, skew, length, vias = _printed(run, tag)
        assert abs(skew) < 1e-4 and skew == pytest.approx(p - n, abs=1e-9) and vias == 2
    # measured on the board: within the 0.02mm asked for (in fact to a few steps of its 1nm grid)
    copper = run["copper"]
    for p_net, n_net in (("A_P", "A_N"), ("B_P", "B_N")):
        assert abs(_measured(copper, p_net)["total"] - _measured(copper, n_net)["total"]) < 1e-4


def test_pair_is_lengthened_to_the_other_pair(routed):
    run = routed["tuned"]
    a_length, b_length = _printed(run, "PAIR_A")[3], _printed(run, "PAIR_B")[3]
    assert b_length == pytest.approx(a_length, abs=1e-4)
    copper = run["copper"]
    lengths = {net: _measured(copper, net)["total"] for net in ("A_P", "A_N", "B_P", "B_N")}
    assert max(lengths.values()) - min(lengths.values()) < 1e-4
    # pair B was 4mm and more shorter before
    assert _printed(routed["plain"], "PAIR_B")[3] < a_length - 4


def test_single_trace_reaches_the_length_asked_for(routed):
    run = routed["tuned"]
    assert _printed(run, "CLK") == [pytest.approx(38.0, abs=1e-4)]
    measured = _measured(run["copper"], "CLK")
    assert measured["total"] == pytest.approx(38.0, abs=1e-4)
    # 30mm of plain route and two F.Cu-In4.Cu vias before
    assert _measured(routed["plain"]["copper"], "CLK")["total"] == pytest.approx(30 + 2 * (DEPTH["In4.Cu"] - DEPTH["F.Cu"]))


def test_reported_lengths_are_what_the_board_file_holds(routed):
    run = routed["tuned"]
    copper = run["copper"]
    reported = {}
    for tag, nets in (("PAIR_A", ("A_P", "A_N")), ("PAIR_B", ("B_P", "B_N"))):
        reported[nets[0]], reported[nets[1]] = _printed(run, tag)[:2]
    reported["CLK"] = _printed(run, "CLK")[0]
    for net, length in reported.items():
        assert length == pytest.approx(_measured(copper, net)["total"], abs=1e-4), net
    # the --lengths table: total, via count and each share, to the 0.001mm it prints
    header, *body = _table(run, "lengths (mm), 5 net(s):")
    assert header == ["net", "total", "vias", "per", "layer"]
    rows = {row[0]: row for row in body}
    assert set(rows) == {"A_P", "A_N", "B_P", "B_N", "CLK"}
    for net, row in rows.items():
        measured = _measured(copper, net)
        assert float(row[1]) == pytest.approx(measured["total"], abs=1.5e-3), net
        assert int(row[2]) == len(copper[net]["vias"]) == 2
        shares = dict(re.findall(r"([\w.]+) (\d+\.\d+)", " ".join(row[3:]).split("[")[0]))
        for layer, mm in measured["layers"].items():
            assert float(shares[layer]) == pytest.approx(mm, abs=1.5e-3), (net, layer)
        assert float(shares["vias"]) == pytest.approx(measured["via"], abs=1.5e-3), net
        assert float(shares.get("pads", 0)) == pytest.approx(measured["pads"], abs=1e-9), net
    # each via counts the height between the two layers it joins, not the board's thickness
    assert _measured(copper, "A_P")["via"] == pytest.approx(2 * (DEPTH["B.Cu"] - DEPTH["F.Cu"]))
    assert _measured(copper, "B_P")["via"] == pytest.approx(2 * (DEPTH["In2.Cu"] - DEPTH["F.Cu"]))
    assert float(rows["B_P"][3:][rows["B_P"][3:].index("vias") + 1].rstrip(",")) == pytest.approx(0.719, abs=1e-3)


def test_match_groups_report_each_member_and_its_error(routed):
    run = routed["tuned"]
    out = run["out"]
    assert re.search(r"LANES: 4 net\(s\), target \d+\.\d+ \(the longest\), tolerance 0\.05: ok", out)
    assert "CLOCK: 1 net(s), target 38.000, tolerance 0.01: ok" in out
    copper = run["copper"]
    rows = {row[0]: row for row in _table(run, "LANES:")}
    assert set(rows) == {"A_P", "A_N", "B_P", "B_N"}
    longest = max(_measured(copper, net)["total"] for net in rows)
    for net, row in rows.items():
        assert float(row[1]) == pytest.approx(_measured(copper, net)["total"], abs=1.5e-3)
        assert float(row[2]) == pytest.approx(_measured(copper, net)["total"] - longest, abs=1.5e-3)
        assert abs(float(row[2])) <= 0.05
    # --lengths names the group, with the error, on each member's row
    table = {row[0]: " ".join(row) for row in _table(run, "lengths (mm), 5 net(s):")[1:]}
    assert all(re.search(r"\[match LANES [+-]0\.000\]", table[net]) for net in rows)
    assert re.search(r"\[match CLOCK [+-]0\.000\]", table["CLK"])


def test_lengths_agree_with_kicads_own_figure(routed):
    """KiCad's DRC adds up a net for a `length` rule: track, via height from
    the board's stack-up, and the pads' own pad-to-die length. It does not
    know the pin lengths a routing file declares. KiCad 9.0 only finds the
    layers a via joins when they are F.Cu and B.Cu (its search skips the
    inner layers), so only pair A's vias are compared; for the other nets
    KiCad's figure has to be the track and pad lengths alone or include the
    same via heights."""
    run = routed["tuned"]
    copper = run["copper"]
    report = _drc(routed["work"] / "tuned", MEASURE)
    kicad = {}
    for violation in report["violations"]:
        if violation["type"] != "length_out_of_range":
            continue
        net = re.search(r"\[([^\]]+)\]", violation["items"][0]["description"]).group(1)
        kicad[net] = float(re.search(r"actual ([0-9.]+) mm", violation["description"]).group(1))
    assert set(kicad) == {"A_P", "A_N", "B_P", "B_N", "CLK"}
    reported = dict(zip(("A_P", "A_N"), _printed(run, "PAIR_A")[:2]))
    for net in ("A_P", "A_N"):
        assert kicad[net] == pytest.approx(reported[net] - DECLARED_PADS[net], abs=2e-4), net
    for net, length in (("B_P", _printed(run, "PAIR_B")[0]), ("B_N", _printed(run, "PAIR_B")[1]),
                        ("CLK", _printed(run, "CLK")[0])):
        measured = _measured(copper, net)
        assert kicad[net] == pytest.approx(measured["track"] + BOARD_PADS.get(net, 0.0), abs=2e-4) or \
            kicad[net] == pytest.approx(length, abs=2e-4), net


def test_tuned_board_passes_kicad_drc(routed):
    # with the rules a real build carries: the fab's limits and the design's net classes
    project = json.loads((routed["work"] / "tuned" / "default.kicad_pro").read_text())
    assert {p["pattern"]: p["netclass"] for p in project["net_settings"]["netclass_patterns"]} == {
        "CLK": "SE_50R", "A_?": "DIFF_90R", "B_?": "DIFF_90R"}
    assert project["board"]["design_settings"]["rules"]["min_track_width"] > 0
    assert "(rule " in (routed["work"] / "tuned" / "default.kicad_dru").read_text()
    report = _drc(routed["work"] / "tuned")
    assert [f"{v['type']}: {v['description']}" for v in report["violations"]] == []
    assert report["unconnected_items"] == []


def test_tuning_keeps_the_copper_on_45_degree_angles_and_within_its_room(routed):
    copper = routed["tuned"]["copper"]
    for net, entry in copper.items():
        for _, (x0, y0), (x1, y1), _ in entry["segments"]:
            dx, dy = abs(x1 - x0), abs(y1 - y0)
            assert dx < 1e-5 or dy < 1e-5 or abs(dx - dy) < 1e-5, (net, (x0, y0), (x1, y1))
    # pair B's meander stays to the left (north) of its y = 30 centre-line, within 1.5mm of it plus
    # half the pair's pitch for the outer trace; its bumps add at most the pair's 0.15mm gap
    inner = [p for net in ("B_P", "B_N") for layer, start, end, _ in copper[net]["segments"] if layer == "In2.Cu"
             for p in (start, end) if 15 < p[0] < 33]
    pitch = 0.115 + 0.15
    assert min(y for _, y in inner) >= 30 - 1.5 - pitch / 2 - 1e-6
    assert min(y for _, y in inner) < 30 - 0.5 and max(y for _, y in inner) <= 30 + pitch / 2 + 1e-6
    outer = [p for layer, start, end, _ in copper["B_P"]["segments"] if layer == "F.Cu" for p in (start, end)]
    assert min(y for x, y in outer if x > 34.5) == pytest.approx(30 - 0.16 - 0.1006, abs=1e-3)
    # CLK swings both ways, 1.0mm at most
    clk = [p for layer, start, end, _ in copper["CLK"]["segments"] if layer == "In4.Cu" for p in (start, end)]
    assert 39 <= min(y for _, y in clk) < 39.5 and 40.5 < max(y for _, y in clk) <= 41


def test_widths_follow_the_layer_through_the_tuning(routed):
    copper = routed["tuned"]["copper"]
    widths = {(net, layer, width) for net, entry in copper.items() for layer, _, _, width in entry["segments"]}
    assert widths == {
        ("A_P", "F.Cu", 0.17), ("A_P", "B.Cu", 0.17), ("A_N", "F.Cu", 0.17), ("A_N", "B.Cu", 0.17),
        ("B_P", "F.Cu", 0.17), ("B_P", "In2.Cu", 0.115), ("B_N", "F.Cu", 0.17), ("B_N", "In2.Cu", 0.115),
        ("CLK", "F.Cu", 0.18), ("CLK", "In4.Cu", 0.11),
    }


def test_several_skew_hops_each_take_what_has_built_up(routed):
    run = routed["hops"]
    tuning = "\n".join(" ".join(row) for row in _table(run, "tuning"))
    # each bend costs 0.265; by the end of the diagonal between them the first and half the second
    # have built up (0.398 = 4 bumps of 0.120), the rest (0.133 = 2 bumps of 0.080) goes on the last hop
    assert re.search(
        r"A_P / A_N: skew \+0\.530 -> [+-]0\.000 \(allowed 0\.02\): "
        r"4 bump\(s\) of 0\.120 on A_N, B\.Cu \(25\.0, 10\.0\)->\(30\.0, 15\.0\); "
        r"2 bump\(s\) of 0\.080 on A_N, F\.Cu \(30\.0, 20\.0\)->\(30\.0, 25\.0\)", tuning)
    p, n, skew, _, _ = _printed(run, "PAIR_A")
    assert abs(skew) < 1e-4
    # (this routing file declares no pin lengths)
    assert p == pytest.approx(_measured(run["copper"], "A_P")["total"] - DECLARED_PADS["A_P"], abs=1e-4)
    assert n == pytest.approx(_measured(run["copper"], "A_N")["total"] - DECLARED_PADS["A_N"], abs=1e-4)
    # the bumps are on the inside trace, on the diagonal and on the last stretch, nowhere else
    plain = {(layer, start, end) for layer, start, end, _ in routed["plain"]["copper"]["A_N"]["segments"]}
    added = [(layer, start, end) for layer, start, end, _ in run["copper"]["A_N"]["segments"]
             if (layer, start, end) not in plain]
    assert {layer for layer, _, _ in added} == {"B.Cu", "F.Cu"}
    # (N runs 0.16 inside the centre-line: its diagonal from (24.93, 10.16) to (29.84, 14.93), its last
    # stretch down x = 29.84 from the via fan-in at y = 20.29 to the pad dogleg at y = 25.91)
    assert all(24.9 < x < 29.9 and 10.1 < y < 15.2 for layer, *ends in added if layer == "B.Cu" for x, y in ends)
    assert all(29.84 - 0.081 < x < 29.85 and 20.2 < y < 26 for layer, *ends in added if layer == "F.Cu" for x, y in ends)
    assert min(x for layer, *ends in added if layer == "F.Cu" for x, _ in ends) == pytest.approx(29.84 - 0.080, abs=1e-3)
    assert {(layer, start, end) for layer, start, end, _ in run["copper"]["A_P"]["segments"]} == {
        (layer, start, end) for layer, start, end, _ in routed["plain"]["copper"]["A_P"]["segments"]}


def test_group_members_can_be_nets_in_series_and_must_be_routed(routed):
    run = routed["hops"]
    p, n = _printed(run, "PAIR_A")[:2]
    assert "LINK: 2 member(s), target" in run["out"] and "tolerance 0.01: ok" in run["out"]
    rows = {row[0]: row for row in _table(run, "LINK:")}
    assert float(rows["A_P+CLK"][1]) == pytest.approx(p + 30.0, abs=1.5e-3)
    assert float(rows["A_N+CLK"][1]) == pytest.approx(n + 30.0, abs=1.5e-3)
    assert "OPEN: 2 net(s), target none (nothing routed), tolerance 1: OUT OF TOLERANCE" in run["out"]
    assert [row[:3] for row in _table(run, "OPEN:")] == [["B_N", "not", "routed"], ["B_P", "not", "routed"]]
    assert run["code"] == 1 and "length error: match group OPEN is out of tolerance" in run["out"]
    assert "match group LINK is out of tolerance" not in run["out"]


# ------------------------------------------------------------ no stack-up

def test_without_a_declared_stack_up_vias_add_nothing_and_the_run_says_so(routed):
    run = routed["bare"]
    assert "no stack-up declared" in run["out"] and "via heights from" not in run["out"]
    copper = run["copper"]
    p, n, skew, length, _ = _printed(run, "PAIR_A")
    assert p == pytest.approx(_measured(copper, "A_P", depth=None)["total"], abs=1e-4)
    assert abs(skew) < 1e-4
    # CLK is still brought to 38mm, of track alone this time
    assert _measured(copper, "CLK", depth=None)["total"] == pytest.approx(38.0, abs=1e-4)
    assert run["code"] == 0


# ------------------------------------------------------------ errors

@pytest.mark.parametrize("case, message", [
    ("skew_without_a_hop", "routing error: A_P/A_N: skew +0.530mm is over the 0.02mm allowed; put 'skew' before the waypoint"),
    ("skew_hop_without_a_limit", "A_P/A_N: 'skew' needs the mismatch to allow: skew=<mm>"),
    ("skew_hop_too_short", "A_P/A_N: 'skew' hop (11.0, 10.0)->(12.5, 10.0): 5 bump(s) of 0.128mm need 6.380mm; "
                           "the segment has 1.500mm"),
    ("skew_on_a_single_trace", "CLK: 'skew' is for differential pairs"),
    ("meander_without_a_length", "B_P/B_N: 'meander' needs the length to reach: length=<mm>"),
    ("length_without_a_hop", "short of the 40.000mm asked for; put 'meander:<left|right|both>:<mm>' before the waypoint"),
    ("length_already_longer", "already longer than the 25.000mm asked for"),
    ("meander_without_room", "does not fit: a 28.000mm segment at a 0.940mm leg pitch holds at most"),
    ("meander_hop_not_straight", "CLK: the hop (15.0, 40.0)->(30.0, 43.0) marked 'meander' is not one straight segment"),
    ("meander_bad_side", "'meander:up:1.0' is not 'skew', 'skew:<mm>' or 'meander:<left|right|both>:<mm>'"),
    ("meander_before_a_via", "CLK: 'meander:left:1.0' goes between the two waypoints of the hop it marks"),
    ("unknown_pad_length", "routing error: no pad 'J9.1' on the board"),
    ("match_without_nets", "routing error: match group G: no net matches 'NOPE_*'"),
])
def test_a_plan_that_cannot_be_met_is_a_routing_error(routed, case, message):
    assert message in routed[case]


def test_copper_added_after_a_net_was_tuned_fails_the_run(routed):
    run = routed["late_copper"]
    assert run["code"] == 1
    assert "length error: CLK: 39.000mm on the finished board, not the 38.000mm it was tuned to" in run["out"]
