"""Do-not-populate parts: kept on the board, left out of what gets bought and placed."""

import csv
import json

from circuit_synth.manufacturing import bom_csv
from circuit_synth.manufacturing.sourcing_check import collect_parts
from circuit_synth.pcb.scripts.kicadpcb import is_dnp, load_dnp, load_fields
from circuit_synth.project.build import _sync_dnp


def _part(mpn: str, **extra) -> dict:
    return {
        "value": mpn,
        "footprint": f"Lib:{mpn}",
        "_extra_fields": {"Manufacturer": "ACME", "Partnumber": mpn, "DigiKey": f"{mpn}-ND", **extra},
    }


def _circuit(tmp_path, name="default") -> None:
    circuit = {
        "name": name,
        "components": {
            "J1": _part("CONN-A", address="j_a"),
            "J2": _part("CONN-B", address="j_b", DNP=True),
            "J3": _part("CONN-B", address="j_c", DNP=True),
        },
        "subcircuits": [
            {"name": "sub", "components": {"R1": _part("RES-1", address="sub.r"), "R2": _part("RES-1", DNP="true")}}
        ],
    }
    (tmp_path / f"{name}.json").write_text(json.dumps(circuit))


def test_is_dnp_truthiness():
    assert is_dnp({"DNP": True})
    assert is_dnp({"DNP": "true"}) and is_dnp({"DNP": "Yes"}) and is_dnp({"DNP": "DNP"})
    assert not is_dnp({"DNP": False}) and not is_dnp({"DNP": "false"}) and not is_dnp({"DNP": ""})
    assert not is_dnp({}) and not is_dnp(None)


def test_board_fields_carry_no_dnp_text_and_dnp_refs_are_listed(tmp_path):
    _circuit(tmp_path)
    fields = load_fields(tmp_path, "default")
    assert all("DNP" not in f for f in fields.values())
    assert fields["J2"]["Partnumber"] == "CONN-B"  # the rest of the part is untouched
    assert load_dnp(tmp_path, "default") == {"J2", "J3", "R2"}


def test_bom_leaves_dnp_parts_out(tmp_path, capsys):
    _circuit(tmp_path)
    out = bom_csv.export(tmp_path, "default")
    rows = list(csv.DictReader(out.open()))
    assert [(r["Refs"], r["Qty"], r["MPN"]) for r in rows] == [("J1", "1", "CONN-A"), ("R1", "1", "RES-1")]
    assert "DNP, omitted: J2, J3, R2" in capsys.readouterr().out


def test_sourcing_check_skips_dnp_parts(tmp_path):
    _circuit(tmp_path)
    parts = collect_parts([(tmp_path, "default")], tmp_path)
    assert set(parts) == {"CONN-A", "RES-1"}
    assert parts["RES-1"]["per_board"] == 1  # the unpopulated R2 is not counted


_SYMBOL = """\t(symbol
\t\t(lib_id "Lib:{ref}")
\t\t(at 10 10 0)
\t\t(unit 1)
\t\t(exclude_from_sim no)
\t\t(in_bom {in_bom})
\t\t(on_board {on_board})
\t\t(dnp {dnp})
\t\t(uuid "0000-{ref}")
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0 0)
\t\t)
\t\t(property "Note" "(dnp no) is only text here"
\t\t\t(at 0 0 0)
\t\t)
\t)
"""


def _flags(text: str, ref: str) -> tuple[str, str, str]:
    block = text[text.index(f'(lib_id "Lib:{ref}")'):]
    block = block[: block.index("(uuid")]
    pick = lambda flag: block.split(f"({flag} ")[1].split(")")[0]
    return pick("in_bom"), pick("on_board"), pick("dnp")


def test_schematic_symbols_get_the_dnp_flag_both_ways(tmp_path):
    _circuit(tmp_path)
    sheet = "(kicad_sch\n" + "".join(
        (
            _SYMBOL.format(ref="J1", in_bom="yes", on_board="yes", dnp="yes"),  # stale: no longer DNP
            _SYMBOL.format(ref="J2", in_bom="no", on_board="no", dnp="no"),  # off-board, as the old DNP handling left it
            _SYMBOL.format(ref="J3", in_bom="yes", on_board="yes", dnp="no"),
        )
    ) + ")\n"
    (tmp_path / "default.kicad_sch").write_text(sheet)
    (tmp_path / "sub.kicad_sch").write_text(
        "(kicad_sch\n" + _SYMBOL.format(ref="R2", in_bom="yes", on_board="yes", dnp="no") + ")\n"
    )

    assert _sync_dnp(tmp_path, "default") == ["J2", "J3", "R2"]

    top = (tmp_path / "default.kicad_sch").read_text()
    assert _flags(top, "J1") == ("yes", "yes", "no")
    assert _flags(top, "J2") == ("yes", "yes", "yes")
    assert _flags(top, "J3") == ("yes", "yes", "yes")
    assert _flags((tmp_path / "sub.kicad_sch").read_text(), "R2") == ("yes", "yes", "yes")
    assert top.count('"(dnp no) is only text here"') == 3  # property text is never rewritten

    before = top
    _sync_dnp(tmp_path, "default")
    assert (tmp_path / "default.kicad_sch").read_text() == before  # idempotent
