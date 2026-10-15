"""cs bom verify's stock rules, over the distributor numbers the BOM records.

Found on circuits-py almond_safety: RC0603FR-0710KL (311-10.0KHRCT-ND) and
friends passed verification with an empty DigiKey cut-tape listing, because
productdetails leaves the product-level QuantityAvailable unset and the
stock lives per packaging variation.
"""

import json

from circuit_synth.manufacturing import sourcing_check
from circuit_synth.manufacturing.catalog import DigiKey, Mouser
from circuit_synth.manufacturing.sourcing_check import (
    FAIL,
    WARN,
    check_digikey,
    check_mouser,
    check_stock,
    collect_parts,
    digikey_stock,
)


def _entry(digikey="311-10.0KHRCT-ND", mouser="603-RC0603FR-0710KL", per_board=1):
    return {
        "manufacturer": "YAGEO",
        "DigiKey": digikey,
        "Mouser": mouser,
        "designs": set(),
        "per_board": per_board,
    }


def _levels(results):
    return [level for level, _ in results]


# ------------------------------------------------------------ check_stock

def test_plenty_everywhere_is_ok():
    assert check_stock(_entry(), {"DigiKey": 4_513_879, "Mouser": 2_046_812}) == []


def test_digikey_at_zero_fails_even_with_mouser_stock():
    results = check_stock(_entry(), {"DigiKey": 0, "Mouser": 2_046_812})
    assert _levels(results) == [FAIL]
    assert "311-10.0KHRCT-ND" in results[0][1]


def test_backup_at_zero_while_digikey_stocked_warns():
    assert _levels(check_stock(_entry(), {"DigiKey": 50_000, "Mouser": 0})) == [WARN]


def test_only_listing_at_zero_fails():
    results = check_stock(_entry(digikey=""), {"Mouser": 0})
    assert _levels(results) == [FAIL]


def test_every_listing_at_zero_fails():
    assert _levels(check_stock(_entry(), {"DigiKey": 0, "Mouser": 0})) == [FAIL, FAIL]


def test_lookup_error_is_not_out_of_stock():
    # DigiKey errored (no stock figure); Mouser at 0 is then only a warning
    assert _levels(check_stock(_entry(), {"Mouser": 0})) == [WARN]
    assert _levels(check_stock(_entry(), {"DigiKey": None, "Mouser": 0})) == [WARN]
    assert check_stock(_entry(), {}) == []


def test_low_stock_scales_with_per_board_quantity():
    entry = _entry(per_board=20)
    # 100 boards x 20 per board = 2,000 needed
    assert check_stock(entry, {"DigiKey": 2_000, "Mouser": 5_000}) == []
    results = check_stock(entry, {"DigiKey": 1_999, "Mouser": 5_000})
    assert _levels(results) == [WARN]
    assert "1,999" in results[0][1]


def test_low_stock_threshold_is_configurable():
    stock = {"DigiKey": 300, "Mouser": 300}
    assert check_stock(_entry(), stock) == []
    assert _levels(check_stock(_entry(), stock, low_stock=301)) == [WARN, WARN]
    assert check_stock(_entry(), stock, low_stock=300) == []
    assert check_stock(_entry(), stock, low_stock=0) == []


def test_zero_threshold_still_fails_out_of_stock():
    assert _levels(check_stock(_entry(), {"DigiKey": 0, "Mouser": 10}, low_stock=0)) == [FAIL]


# ------------------------------------------------ catalog stock extraction

def _dk_product(cut_tape_stock):
    return {
        "ManufacturerProductNumber": "RC0603FR-0710KL",
        "Manufacturer": {"Name": "YAGEO"},
        "ProductStatus": {"Status": "Active"},
        "ProductVariations": [
            {
                "DigiKeyProductNumber": "311-10.0KHRTR-ND",
                "PackageType": {"Name": "Tape & Reel (TR)"},
                "QuantityAvailableforPackageType": 250_000,
            },
            {
                "DigiKeyProductNumber": "311-10.0KHRCT-ND",
                "PackageType": {"Name": "Cut Tape (CT)"},
                "QuantityAvailableforPackageType": cut_tape_stock,
            },
        ],
    }


class FakeDigiKey(DigiKey):
    def __init__(self, product):
        self.product = product

    def by_number(self, number):
        return self.product

    def search(self, keywords, limit=10, in_stock=False):
        return [self.product]


def test_digikey_stock_reads_our_packaging_not_the_reel():
    assert digikey_stock(_dk_product(0), "311-10.0KHRCT-ND") == 0
    assert digikey_stock(_dk_product(0), "311-10.0KHRTR-ND") == 250_000


def test_check_digikey_records_cut_tape_stock():
    stock = {}
    assert check_digikey(FakeDigiKey(_dk_product(0)), "RC0603FR-0710KL", _entry(), stock) == []
    assert stock == {"DigiKey": 0}


class FakeMouser(Mouser):
    def __init__(self, part):
        super().__init__(api_key="")
        self.part = part

    def _exact(self, number):
        return [self.part]


def _mouser_part(in_stock):
    return {
        "MouserPartNumber": "603-RC0603FR-0710KL",
        "ManufacturerPartNumber": "RC0603FR-0710KL",
        "Manufacturer": "YAGEO",
        "AvailabilityInStock": in_stock,
        "LifecycleStatus": "",
    }


def test_check_mouser_records_stock():
    stock = {}
    check_mouser(FakeMouser(_mouser_part("2046812")), "RC0603FR-0710KL", _entry(), stock)
    assert stock == {"Mouser": 2_046_812}


def test_mouser_empty_availability_means_no_stock():
    stock = {}
    check_mouser(FakeMouser(_mouser_part(None)), "RC0603FR-0710KL", _entry(), stock)
    assert stock == {"Mouser": 0}


# ----------------------------------------------------------- end to end

def _build(tmp_path, refs):
    layout = tmp_path / "layouts" / "default"
    layout.mkdir(parents=True)
    extra = {
        "Partnumber": "RC0603FR-0710KL",
        "Manufacturer": "YAGEO",
        "DigiKey": "311-10.0KHRCT-ND",
        "Mouser": "603-RC0603FR-0710KL",
    }
    circuit = {"components": {ref: {"_extra_fields": extra} for ref in refs}}
    (layout / "default.json").write_text(json.dumps(circuit))
    return layout


def test_collect_parts_counts_per_board_quantity(tmp_path):
    layout = _build(tmp_path, ["R1", "R2", "R3"])
    parts = collect_parts([(layout, "default")], tmp_path)
    assert parts["RC0603FR-0710KL"]["per_board"] == 3


def test_verify_exits_nonzero_on_empty_digikey_listing(tmp_path, monkeypatch, capsys):
    layout = _build(tmp_path, ["R1"])
    monkeypatch.setattr(sourcing_check, "digikey_client", lambda: FakeDigiKey(_dk_product(0)))
    monkeypatch.setattr(sourcing_check, "mouser_client", lambda: FakeMouser(_mouser_part("2046812")))

    assert sourcing_check.verify([(layout, "default")], root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "FAIL RC0603FR-0710KL  (YAGEO)  DigiKey 0 / Mouser 2,046,812" in out
    assert "1 out" in out


def test_verify_passes_and_prints_stock(tmp_path, monkeypatch, capsys):
    layout = _build(tmp_path, ["R1"])
    monkeypatch.setattr(sourcing_check, "digikey_client", lambda: FakeDigiKey(_dk_product(4_513_879)))
    monkeypatch.setattr(sourcing_check, "mouser_client", lambda: FakeMouser(_mouser_part("2046812")))

    assert sourcing_check.verify([(layout, "default")], root=tmp_path) == 0
    assert "ok   RC0603FR-0710KL  (YAGEO)  DigiKey 4,513,879 / Mouser 2,046,812" in capsys.readouterr().out


def test_verify_lookup_error_warns_without_failing(tmp_path, monkeypatch, capsys):
    layout = _build(tmp_path, ["R1"])

    class Broken(FakeDigiKey):
        def by_number(self, number):
            raise OSError("timed out")

    monkeypatch.setattr(sourcing_check, "digikey_client", lambda: Broken(None))
    monkeypatch.setattr(sourcing_check, "mouser_client", lambda: FakeMouser(_mouser_part("0")))

    assert sourcing_check.verify([(layout, "default")], root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "WARN RC0603FR-0710KL  (YAGEO)  DigiKey ? / Mouser 0" in out
