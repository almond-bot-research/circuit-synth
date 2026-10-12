"""cs bom verify's Mouser check must not confuse same-named parts from other makers.

Found on circuits-py jelly_legs: the Littelfuse SMCJ30A's correct Mouser
number (576-SMCJ30A) failed because an MPN search for "SMCJ30A" returns
Bourns' 652-SMCJ30A first.
"""

from circuit_synth.manufacturing.catalog import Mouser
from circuit_synth.manufacturing.sourcing_check import FAIL, NOTE, check_mouser


def _part(number, mpn, maker, stock="100"):
    return {
        "MouserPartNumber": number,
        "ManufacturerPartNumber": mpn,
        "Manufacturer": maker,
        "AvailabilityInStock": stock,
        "LifecycleStatus": "",
    }


# What Mouser's exact part-number search returns for each query
CATALOG = {
    "SMCJ30A": [
        _part("652-SMCJ30A", "SMCJ30A", "Bourns"),
        _part("576-SMCJ30A", "SMCJ30A", "Littelfuse"),
    ],
    "576-SMCJ30A": [_part("576-SMCJ30A", "SMCJ30A", "Littelfuse")],
    "652-SMCJ30A": [_part("652-SMCJ30A", "SMCJ30A", "Bourns")],
}


class FakeMouser(Mouser):
    def __init__(self):
        super().__init__(api_key="")

    def _exact(self, number):
        return CATALOG.get(number, [])


def _entry(mouser, maker="Littelfuse Inc."):
    return {"manufacturer": maker, "DigiKey": "", "Mouser": mouser, "designs": set()}


def _levels(results):
    return [level for level, _ in results]


def test_correct_number_for_generic_mpn_passes():
    assert check_mouser(FakeMouser(), "SMCJ30A", _entry("576-SMCJ30A")) == []


def test_other_makers_number_fails():
    results = check_mouser(FakeMouser(), "SMCJ30A", _entry("652-SMCJ30A"))
    assert _levels(results) == [FAIL]
    assert "Bourns" in results[0][1]


def test_unknown_number_fails_and_suggests_our_makers_number():
    results = check_mouser(FakeMouser(), "SMCJ30A", _entry("576-SMCJ30AX"))
    assert _levels(results) == [FAIL]
    assert "576-SMCJ30A" in results[0][1]


def test_no_stored_number_is_fine_when_our_maker_is_carried():
    assert check_mouser(FakeMouser(), "SMCJ30A", _entry("")) == []


def test_no_stored_number_ignores_other_makers():
    results = check_mouser(FakeMouser(), "SMCJ30A", _entry("", maker="Vishay"))
    assert _levels(results) == [NOTE]


def test_lookup_filters_by_manufacturer():
    mo = FakeMouser()
    assert mo.lookup("SMCJ30A")["MouserPartNumber"] == "652-SMCJ30A"
    assert mo.lookup("SMCJ30A", "Littelfuse Inc.")["MouserPartNumber"] == "576-SMCJ30A"


def test_same_maker_handles_distributor_spellings():
    from circuit_synth.manufacturing.catalog import same_maker

    assert same_maker("Würth", "Wurth Elektronik")
    assert same_maker("Stackpole Electronics Inc", "SEI Stackpole")
    assert same_maker("Vishay General Semiconductor", "Vishay Semiconductors")
    assert same_maker("Diodes Incorporated", "Diodes Incorporated - Diodes Division")
    assert not same_maker("Littelfuse Inc.", "Bourns")
    assert not same_maker("Texas Instruments", "Microchip Technology")
    assert not same_maker("Littelfuse Inc.", "Bel Fuse Inc.")
