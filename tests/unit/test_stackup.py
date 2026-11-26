"""The board stack-up a design declares and `cs pcb sync` writes into the
.kicad_pcb (pcbnew's Python API cannot set it).

Found on circuits-py: the thor carrier (10 layers, impedance controlled)
edited the saved board's (stackup ...) block from its own placement.py.
"""

import pytest

from circuit_synth.pcb.scripts.stackup import (
    copper_layer_count,
    copper_layer_names,
    dielectric,
    read_stackup_block,
    stackup,
    stackup_block,
    write_stackup,
)

PREPREG = dielectric("prepreg", 0.1194, "S1000-2M 2116", 4.29, 0.018)
CORE = dielectric("core", 0.2, "S1000-2M", 4.42, 0.018)

# A board file as pcbnew 9 writes it, cut down to what matters here. The
# title holds text that looks like the blocks being searched for.
BOARD = """(kicad_pcb
\t(version 20241229)
\t(generator "pcbnew")
\t(general
\t\t(thickness 1.6)
\t)
\t(title_block
\t\t(title "odd (setup (stackup \\"x\\") title")
\t)
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(2 "B.Cu" signal)
\t)
\t(setup
\t\t(pad_to_mask_clearance 0)
\t\t(pcbplotparams
\t\t\t(layerselection 0x00000000_00000000_55555555_5755f5ff)
\t\t)
\t)
\t(net 0 "")
)
"""

FOUR_LAYER_BLOCK = """(stackup
\t\t\t(layer "F.SilkS"
\t\t\t\t(type "Top Silk Screen")
\t\t\t)
\t\t\t(layer "F.Paste"
\t\t\t\t(type "Top Solder Paste")
\t\t\t)
\t\t\t(layer "F.Mask"
\t\t\t\t(type "Top Solder Mask")
\t\t\t\t(thickness 0.03)
\t\t\t)
\t\t\t(layer "F.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.035)
\t\t\t)
\t\t\t(layer "dielectric 1"
\t\t\t\t(type "prepreg")
\t\t\t\t(thickness 0.1194)
\t\t\t\t(material "S1000-2M 2116")
\t\t\t\t(epsilon_r 4.29)
\t\t\t\t(loss_tangent 0.018)
\t\t\t)
\t\t\t(layer "In1.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.0152)
\t\t\t)
\t\t\t(layer "dielectric 2"
\t\t\t\t(type "core")
\t\t\t\t(thickness 0.2)
\t\t\t\t(material "S1000-2M")
\t\t\t\t(epsilon_r 4.42)
\t\t\t\t(loss_tangent 0.018)
\t\t\t)
\t\t\t(layer "In2.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.0152)
\t\t\t)
\t\t\t(layer "dielectric 3"
\t\t\t\t(type "prepreg")
\t\t\t\t(thickness 0.1194)
\t\t\t\t(material "S1000-2M 2116")
\t\t\t\t(epsilon_r 4.29)
\t\t\t\t(loss_tangent 0.018)
\t\t\t)
\t\t\t(layer "B.Cu"
\t\t\t\t(type "copper")
\t\t\t\t(thickness 0.035)
\t\t\t)
\t\t\t(layer "B.Mask"
\t\t\t\t(type "Bottom Solder Mask")
\t\t\t\t(thickness 0.03)
\t\t\t)
\t\t\t(layer "B.Paste"
\t\t\t\t(type "Bottom Solder Paste")
\t\t\t)
\t\t\t(layer "B.SilkS"
\t\t\t\t(type "Bottom Silk Screen")
\t\t\t)
\t\t\t(copper_finish "ENIG")
\t\t\t(dielectric_constraints yes)
\t\t)"""


def _four():
    return stackup([PREPREG, CORE, PREPREG], mask=0.03)


def _board(tmp_path, text=BOARD):
    path = tmp_path / "default.kicad_pcb"
    path.write_text(text)
    return path


# ------------------------------------------------------------ the declaration

def test_stackup_implies_the_copper_layer_count():
    assert copper_layer_count(_four()) == 4
    ten = stackup([PREPREG, CORE] * 4 + [PREPREG])
    assert copper_layer_count(ten) == 10
    assert copper_layer_names(10) == ["F.Cu", *(f"In{i}.Cu" for i in range(1, 9)), "B.Cu"]
    assert copper_layer_names(2) == ["F.Cu", "B.Cu"]
    # 1oz outer / 0.5oz inner copper and ENIG unless told otherwise
    assert (ten["outer_copper"], ten["inner_copper"], ten["finish"]) == (0.035, 0.0152, "ENIG")


def test_stackup_rejects_what_no_board_can_be():
    with pytest.raises(ValueError, match="odd number of dielectrics"):
        stackup([PREPREG, CORE])            # 3 copper layers
    with pytest.raises(ValueError, match="odd number of dielectrics"):
        stackup([])
    with pytest.raises(ValueError, match="'core' or 'prepreg'"):
        dielectric("fr4", 0.2)
    with pytest.raises(ValueError, match="positive"):
        dielectric("core", 0)
    with pytest.raises(ValueError, match="'core' or 'prepreg'"):
        stackup([{"type": "glue", "thickness": 0.1}])   # hand-written dicts get the same checks


def test_dielectric_material_defaults_are_kicads():
    assert dielectric("core", 1.51) == {
        "type": "core", "thickness": 1.51, "material": "FR4", "epsilon_r": 4.5, "loss_tangent": 0.02,
    }


# ------------------------------------------------------------ the block

def test_block_is_formatted_as_pcbnew_writes_it():
    assert stackup_block(_four()) == FOUR_LAYER_BLOCK


def test_block_quotes_material_names_and_marks_uncontrolled_impedance():
    spec = stackup([dielectric("core", 1.5, 'FR4 "TG155" (std)')], finish="HAL lead-free", impedance_controlled=False)
    block = stackup_block(spec)
    assert '(material "FR4 \\"TG155\\" (std)")' in block
    assert '(copper_finish "HAL lead-free")' in block and "(dielectric_constraints no)" in block
    assert "In1.Cu" not in block


# ------------------------------------------------------------ the board file

def test_write_adds_the_block_at_the_top_of_setup(tmp_path):
    pcb = _board(tmp_path)
    assert write_stackup(pcb, _four()) == ["stack-up written to default.kicad_pcb: 4 copper layers, 3 dielectrics"]
    text = pcb.read_text()
    assert text == BOARD.replace("\t(setup\n", "\t(setup\n\t\t" + FOUR_LAYER_BLOCK + "\n")
    setup, start, end = read_stackup_block(text)
    assert text[start:end] == FOUR_LAYER_BLOCK and text[setup:].startswith("(setup\n\t\t(stackup")


def test_write_is_idempotent(tmp_path):
    pcb = _board(tmp_path)
    write_stackup(pcb, _four())
    once = pcb.read_text()
    stamp = pcb.stat().st_mtime_ns
    assert write_stackup(pcb, _four()) == []
    assert pcb.read_text() == once and pcb.stat().st_mtime_ns == stamp   # not even rewritten


def test_write_replaces_a_different_block_and_nothing_else(tmp_path):
    pcb = _board(tmp_path)
    write_stackup(pcb, stackup([dielectric("core", 1.51)]))              # a 2-layer build first
    assert '"dielectric 1"' in pcb.read_text() and '"dielectric 2"' not in pcb.read_text()
    assert write_stackup(pcb, _four()) == ["stack-up written to default.kicad_pcb: 4 copper layers, 3 dielectrics"]
    assert pcb.read_text() == BOARD.replace("\t(setup\n", "\t(setup\n\t\t" + FOUR_LAYER_BLOCK + "\n")
    assert pcb.read_text().count("(stackup") == 2                        # ours, and the one in the title text


def test_same_stackup_in_another_layout_is_left_alone(tmp_path):
    # one line, other number formatting: what an older KiCad or another tool might have written
    squashed = " ".join(FOUR_LAYER_BLOCK.split()).replace("0.2)", "0.20000)").replace("4.29", "4.290")
    pcb = _board(tmp_path, BOARD.replace("\t(setup\n", "\t(setup\n\t\t" + squashed + "\n"))
    before = pcb.read_text()
    assert write_stackup(pcb, _four()) == []
    assert pcb.read_text() == before


def test_text_that_looks_like_a_block_is_not_mistaken_for_one(tmp_path):
    # the title's "(setup (stackup" is inside a string: the real setup has no stack-up yet
    setup, start, end = read_stackup_block(BOARD)
    assert BOARD[setup:].startswith("(setup\n\t\t(pad_to_mask_clearance 0)")
    assert (start, end) == (-1, -1)


def test_board_without_setup_or_with_broken_text_is_an_error(tmp_path):
    assert read_stackup_block('(kicad_pcb (version 1) (net 0 ""))') is None
    with pytest.raises(ValueError, match="no \\(setup"):
        write_stackup(_board(tmp_path, '(kicad_pcb (version 1) (net 0 ""))'), _four())
    with pytest.raises(ValueError, match="well-formed"):
        read_stackup_block('(kicad_pcb (setup (stackup (layer "F.Cu"')
    with pytest.raises(ValueError, match="well-formed"):
        read_stackup_block("not a board")


# ------------------------------------------------------------ build_project

def test_build_project_checks_the_layer_count_against_the_stackup(tmp_path):
    from circuit_synth.project import build_project

    with pytest.raises(ValueError, match="copper_layers=6 but the stack-up describes 4"):
        build_project(None, tmp_path, "default", [], copper_layers=6, stackup=_four())
    with pytest.raises(ValueError, match="even number"):
        build_project(None, tmp_path, "default", [], copper_layers=5)
    assert not any(tmp_path.iterdir())                                   # refused before writing anything
