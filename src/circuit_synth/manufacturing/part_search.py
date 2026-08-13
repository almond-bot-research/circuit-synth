"""DigiKey/Mouser-first part selection: search, inspect, fetch datasheets.

The workflow these support: search the distributor catalogs for candidates
that match the exact specs and are in stock (`cs parts search`), pull the
full parameter list to confirm the choice (`cs parts detail`), then download
the datasheet next to the part definition so future readers have the same
context (`cs parts datasheet`).

Sourcing rule: never store a distributor number you have not seen in a
catalog response; these commands print catalog numbers verbatim so they can
be copied into `parts.py` as-is.
"""

from __future__ import annotations

import re
from pathlib import Path

from .catalog import (
    digikey_client,
    digikey_numbers,
    digikey_offer,
    mouser_client,
    norm,
)


# ---------------------------------------------------------------- formatting

def _digikey_row(product: dict) -> dict:
    variations = product.get("ProductVariations") or []
    pricing = ""
    for var in variations:
        if "cut tape" in ((var.get("PackageType") or {}).get("Name", "")).lower() or len(variations) == 1:
            breaks = var.get("StandardPricing") or []
            pricing = "  ".join(f"{b['BreakQuantity']}@${b['UnitPrice']:.4f}" for b in breaks[:3])
            break
    return {
        "source": "DigiKey",
        "mpn": product.get("ManufacturerProductNumber", ""),
        "manufacturer": (product.get("Manufacturer") or {}).get("Name", ""),
        "number": digikey_offer(product),
        "stock": product.get("QuantityAvailable", 0),
        "status": (product.get("ProductStatus") or {}).get("Status", ""),
        "description": (product.get("Description") or {}).get("ProductDescription", ""),
        "package": next(
            (
                p.get("ValueText", "")
                for p in product.get("Parameters") or []
                if p.get("ParameterText") in ("Package / Case", "Supplier Device Package")
            ),
            "",
        ),
        "pricing": pricing,
        "datasheet": product.get("DatasheetUrl", ""),
    }


def _mouser_row(part: dict) -> dict:
    breaks = part.get("PriceBreaks") or []
    pricing = "  ".join(f"{b['Quantity']}@{b['Price']}" for b in breaks[:3])
    return {
        "source": "Mouser",
        "mpn": part.get("ManufacturerPartNumber", ""),
        "manufacturer": part.get("Manufacturer", ""),
        "number": part.get("MouserPartNumber", ""),
        "stock": part.get("AvailabilityInStock") or part.get("Availability", ""),
        "status": part.get("LifecycleStatus") or "",
        "description": part.get("Description", ""),
        "package": next(
            (
                a.get("AttributeValue", "")
                for a in part.get("ProductAttributes") or []
                if "package" in (a.get("AttributeName") or "").lower()
            ),
            "",
        ),
        "pricing": pricing,
        "datasheet": part.get("DataSheetUrl", ""),
    }


def _print_row(row: dict) -> None:
    print(f"{row['source']:8} {row['manufacturer']}  {row['mpn']}")
    print(f"         number: {row['number']}   stock: {row['stock']}   status: {row['status']}")
    if row["package"]:
        print(f"         package: {row['package']}")
    if row["description"]:
        print(f"         {row['description']}")
    if row["pricing"]:
        print(f"         pricing: {row['pricing']}")
    if row["datasheet"]:
        print(f"         datasheet: {row['datasheet']}")
    print()


# ------------------------------------------------------------------- search

def search(query: str, limit: int = 10, in_stock: bool = False, package: str = "") -> int:
    """Print catalog candidates for a keyword query, DigiKey first."""
    dk = digikey_client()
    mo = mouser_client()
    if dk is None and mo is None:
        raise SystemExit(
            "no distributor credentials (DIGIKEY_CLIENT_ID/SECRET, MOUSER_API_KEY); "
            "set them in the environment or ~/.circuit_synth/credentials"
        )

    rows: list[dict] = []
    if dk is not None:
        rows += [_digikey_row(p) for p in dk.search(query, limit=limit, in_stock=in_stock)]
    else:
        print("no DigiKey client - skipping DigiKey")
    if mo is not None:
        rows += [_mouser_row(p) for p in mo.search(query, limit=limit, in_stock=in_stock)]
    else:
        print("no Mouser client (MOUSER_API_KEY) - skipping Mouser")

    if package:
        want = norm(package)
        rows = [
            r
            for r in rows
            if want in norm(r["package"]) or want in norm(r["description"]) or want in norm(r["mpn"])
        ]

    if not rows:
        print(f"no results for {query!r}")
        return 1
    for row in rows:
        _print_row(row)
    print(f"{len(rows)} candidates. Store numbers exactly as printed (never synthesize one).")
    return 0


# ------------------------------------------------------------------- detail

def detail(mpn: str) -> int:
    """Print the full catalog record(s) for one MPN: parameters, offers, stock."""
    dk = digikey_client()
    mo = mouser_client()
    if dk is None and mo is None:
        raise SystemExit("no distributor credentials; nothing to look up")

    found = False
    if dk is not None:
        product = dk.by_number(mpn)
        if product is None:
            exact = [p for p in dk.search(mpn) if norm(p.get("ManufacturerProductNumber", "")) == norm(mpn)]
            product = exact[0] if exact else None
        if product is not None:
            found = True
            row = _digikey_row(product)
            _print_row(row)
            offers = digikey_numbers(product)
            if offers:
                print("  DigiKey offers:")
                for number, pkg in offers.items():
                    print(f"    {number}  ({pkg})")
            params = product.get("Parameters") or []
            if params:
                print("  Parameters:")
                for p in params:
                    print(f"    {p.get('ParameterText')}: {p.get('ValueText')}")
            print()

    if mo is not None:
        part = mo.lookup(mpn)
        if part is not None:
            found = True
            _print_row(_mouser_row(part))
            attrs = part.get("ProductAttributes") or []
            if attrs:
                print("  Attributes:")
                for a in attrs:
                    print(f"    {a.get('AttributeName')}: {a.get('AttributeValue')}")
            print()

    if not found:
        print(f"no catalog carries {mpn!r}")
        return 1
    return 0


# ---------------------------------------------------------------- datasheet

def _datasheet_url(mpn: str) -> str:
    dk = digikey_client()
    if dk is not None:
        product = dk.by_number(mpn)
        if product is None:
            exact = [p for p in dk.search(mpn) if norm(p.get("ManufacturerProductNumber", "")) == norm(mpn)]
            product = exact[0] if exact else None
        url = (product or {}).get("DatasheetUrl", "")
        if url:
            return url
    mo = mouser_client()
    if mo is not None:
        part = mo.lookup(mpn)
        url = (part or {}).get("DataSheetUrl", "")
        if url:
            return url
    return ""


def fetch_datasheet(mpn: str, out_dir: Path, filename: str = "") -> int:
    """Download the datasheet for an MPN into `out_dir`.

    Convention: every IC's part directory carries its datasheet PDF so agents
    picking or reviewing the part can read it without a network round trip.
    """
    from .catalog import download

    url = _datasheet_url(mpn)
    if not url:
        print(f"no datasheet URL in any catalog for {mpn!r}")
        return 1
    if url.startswith("//"):
        url = "https:" + url

    name = filename or re.sub(r"[^A-Za-z0-9._-]+", "_", mpn) + ".pdf"
    dest = Path(out_dir) / name
    download(url, dest)
    print(f"{mpn}: {url}\n  -> {dest}")
    return 0
