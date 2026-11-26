"""Which file a footprint's 3D model entry resolves to (`cs pcb sync`).

Found on circuits-py: two part dirs both ship
SOT-23-6_L2.9-W1.6-H1.5-LS2.8-P0.95.step (the same package imported for two
parts); with models looked up by bare filename across all libs, one part's
footprints on a board pointed at the other part's file.
"""

from circuit_synth.pcb.scripts.kicadpcb import locate_model, model_index

NAME = "SOT-23-6_L2.9-W1.6-H1.5-LS2.8-P0.95.step"


def _lib(root, name, *files):
    d = root / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_text("model")
    return d


def test_a_footprints_own_lib_dir_wins_over_another_with_the_same_filename(tmp_path):
    buck = _lib(tmp_path, "packages/buck/parts/TI_LMR50410", NAME, "pkg.kicad_mod")
    diodes = _lib(tmp_path, "designs/thor/parts/Diodes_AP22653W6_7", NAME)
    libs = {"TI_LMR50410": buck, "Diodes_AP22653W6_7": diodes}
    index = model_index(libs)
    assert index[NAME.lower()] == (diodes / NAME).resolve()        # first in path order: not a part's own file
    assert locate_model(NAME, buck, index) == (buck / NAME).resolve()
    assert locate_model(NAME, diodes, index) == (diodes / NAME).resolve()
    # a stale path from another checkout or convention still resolves by filename
    assert locate_model("${KIPRJMOD}/../../old/place/" + NAME, buck, index) == (buck / NAME).resolve()


def test_falls_back_to_the_other_libs_when_the_own_dir_does_not_have_the_file(tmp_path):
    passives = _lib(tmp_path, "library/R0603", "R0603.step", "R0603.wrl")
    part = _lib(tmp_path, "parts/Some_Part", "body.step")
    index = model_index({"R0603": passives, "Some_Part": part})
    assert locate_model("R0603.STEP", part, index) == (passives / "R0603.step").resolve()   # case-insensitive
    assert locate_model("R0603.wrl", None, index) == (passives / "R0603.wrl").resolve()     # footprint lib unknown
    assert locate_model("R0603.wrl", tmp_path / "gone", index) == (passives / "R0603.wrl").resolve()


def test_unknown_or_empty_model_is_none(tmp_path):
    part = _lib(tmp_path, "parts/Some_Part", "body.step", "notes.txt")
    index = model_index({"Some_Part": part, "Missing": tmp_path / "nope"})
    assert set(index) == {"body.step"}                             # only model files, only existing dirs
    assert locate_model("${ANT3DMDL}/PLUG_GENERIC_USB-C.step", part, index) is None
    assert locate_model("", part, index) is None
