"""Check every part's sourcing data against the DigiKey and Mouser catalogs.

For each manufacturer part number in a build this asks both distributors
whether the part exists, whether the distributor number shipped in the BOM
really belongs to that MPN, and whether it is orderable today (active
lifecycle, stock on hand). A distributor number that looks plausible but
that no catalog carries is the failure this exists to catch - DigiKey's
`<supplier-prefix>-<MPN>CT-ND` form is easy to synthesise and impossible to
tell apart from a real one by eye.

Stock is read from the listings the BOM records (the DigiKey and Mouser
numbers), since those are what gets ordered:

- FAIL when the recorded DigiKey listing has no stock - DigiKey is the
  primary source - or when no recorded listing has any stock at all.
- WARN when a backup listing (Mouser) has no stock, or when any recorded
  listing holds fewer than `low_stock` boards' worth of the part
  (stock < low_stock x per-board quantity).

A lookup error leaves that listing's stock unknown; it is never read as
out of stock.

Run it before sending a quote package. Exit status is non-zero when any part
fails, so it can gate an order.

Credentials come from the environment or ~/.circuit_synth/credentials (see
circuit_synth.manufacturing.catalog); a distributor is skipped when its
credentials are missing.

Usage:
  cs bom verify                                       # every build under cwd
  cs bom verify designs/mantis/layouts/default default
  cs bom verify --only SDCL1V4030-100M-R
  cs bom verify --low-stock 500                       # warn below 500 boards' worth
"""

from __future__ import annotations

from pathlib import Path

from .catalog import (
    DigiKey,
    Mouser,
    dead,
    digikey_client,
    digikey_offer,
    mouser_client,
    norm,
    same_maker,
)

import json

FAIL, WARN, NOTE = "FAIL", "WARN", "note"

# Warn when a listing's stock covers fewer boards than this. 100 boards is
# well past any prototype or pilot run we order, so a warning means a real
# order could clear the shelf before it ships.
LOW_STOCK_BOARDS = 100


# ------------------------------------------------------------------- inputs

def discover_builds(root: Path) -> list[tuple[Path, str]]:
    """Every `layouts/<build>/<build>.json` project under `root`."""
    out = []
    for path in sorted(root.glob("**/layouts/*/*.json")):
        if path.name.endswith(".flat.json"):
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.stem == path.parent.name:
            out.append((path.parent, path.stem))
    return out


def collect_parts(builds: list[tuple[Path, str]], root: Path) -> dict[str, dict]:
    """mpn -> {manufacturer, DigiKey, Mouser, designs, per_board}.

    `per_board` is the most references any one build uses of the part.
    """
    parts: dict[str, dict] = {}
    for project_dir, build_name in builds:
        path = Path(project_dir) / f"{build_name}.json"
        if not path.exists():
            raise SystemExit(f"no circuit JSON at {path}")
        try:
            label = str(Path(project_dir).resolve().relative_to(root.resolve()))
        except ValueError:
            label = str(project_dir)

        def walk(circuit: dict) -> None:
            for comp in (circuit.get("components") or {}).values():
                extra = comp.get("_extra_fields") or {}
                mpn = extra.get("Partnumber")
                if not mpn:
                    continue
                entry = parts.setdefault(
                    mpn,
                    {
                        "manufacturer": extra.get("Manufacturer", ""),
                        "DigiKey": extra.get("DigiKey", ""),
                        "Mouser": extra.get("Mouser", ""),
                        "designs": set(),
                        "count": {},
                    },
                )
                entry["designs"].add(label)
                entry["count"][label] = entry["count"].get(label, 0) + 1

            for sub in circuit.get("subcircuits", []):
                walk(sub)

        walk(json.loads(path.read_text()))
    for entry in parts.values():
        entry["per_board"] = max(entry.pop("count").values())
    return parts


# ------------------------------------------------------------------ checks

def health(product: dict, distributor: str, stock: bool = True) -> list[tuple[str, str]]:
    out = []
    status = (product.get("ProductStatus") or {}).get("Status", "")
    if dead(status):
        out.append((WARN, f"{distributor} product status is {status!r}"))
    if stock and product.get("QuantityAvailable") == 0:
        out.append((WARN, f"{distributor} has no stock"))
    return out


def digikey_stock(product: dict, number: str) -> int | None:
    """Stock on hand for one DigiKey number; None when the catalog gives none.

    Stock is per packaging: a cut-tape listing can be empty while the reel
    has thousands. productdetails leaves the product-level QuantityAvailable
    unset, so read the variation that is our number.
    """
    for v in product.get("ProductVariations") or []:
        if norm(v.get("DigiKeyProductNumber", "")) == norm(number):
            return v.get("QuantityAvailableforPackageType")
    return product.get("QuantityAvailable")


def check_digikey(
    dk: DigiKey, mpn: str, entry: dict, stock: dict | None = None
) -> list[tuple[str, str]]:
    """Verify the stored DigiKey number really is this part's, via that number.

    The productdetails endpoint resolves DigiKey part numbers, so looking ours
    up is a direct existence test: a synthesised number simply 404s. Keyword
    search by MPN then supplies the number we should have used. The stored
    number's stock goes into `stock["DigiKey"]` for check_stock.
    """
    ours, out = entry["DigiKey"], []
    catalog = dk.by_number(ours) if ours else None

    if catalog is not None:
        out += health(catalog, "DigiKey", stock=False)
        if stock is not None:
            stock["DigiKey"] = digikey_stock(catalog, ours)
        catalog_mpn = catalog.get("ManufacturerProductNumber", "")
        catalog_mfr = (catalog.get("Manufacturer") or {}).get("Name", "")
        if norm(catalog_mpn) != norm(mpn):
            out.append(
                (WARN, f"{ours} is DigiKey's number for MPN {catalog_mpn!r}, we call the part {mpn!r}")
            )
        if not same_maker(entry["manufacturer"], catalog_mfr):
            out.append((FAIL, f"{ours} is made by {catalog_mfr!r}, we say {entry['manufacturer']!r}"))
        return out

    matches = [
        p
        for p in dk.search(mpn)
        if norm(p.get("ManufacturerProductNumber", "")) == norm(mpn)
        and same_maker(entry["manufacturer"], (p.get("Manufacturer") or {}).get("Name", ""))
    ]
    if ours:
        fix = f"; the catalog number for this part is {digikey_offer(matches[0])}" if matches else ""
        out.append((FAIL, f"DigiKey has no part number {ours}{fix}"))
    elif matches:
        out.append((NOTE, f"no DigiKey number stored; the catalog offers {digikey_offer(matches[0])}"))
    else:
        out.append((NOTE, f"DigiKey does not carry {mpn}"))
    if matches:
        out += health(matches[0], "DigiKey")
    return out


def mouser_health(part: dict, stock: bool = True) -> list[tuple[str, str]]:
    out = []
    status = part.get("LifecycleStatus") or ""
    if dead(status):
        out.append((WARN, f"Mouser lifecycle status is {status!r}"))
    if stock and mouser_stock(part) == 0:
        out.append((WARN, f"Mouser has no stock ({part.get('Availability') or 'none reported'})"))
    return out


def mouser_stock(part: dict) -> int:
    """Stock on hand; Mouser leaves AvailabilityInStock empty when there is none."""
    try:
        return int(str(part.get("AvailabilityInStock") or "0").replace(",", ""))
    except ValueError:
        return 0


def check_mouser(
    mo: Mouser, mpn: str, entry: dict, stock: dict | None = None
) -> list[tuple[str, str]]:
    """Verify the stored Mouser number via that number, like check_digikey.

    Comparing against an MPN search instead is wrong for generic MPNs: Mouser
    lists every maker's SMCJ30A, and the first hit need not be ours.
    """
    ours, out = entry["Mouser"], []
    catalog = mo.by_number(ours) if ours else None

    if catalog is not None:
        out += mouser_health(catalog, stock=False)
        if stock is not None:
            stock["Mouser"] = mouser_stock(catalog)
        catalog_mpn = catalog.get("ManufacturerPartNumber", "")
        catalog_mfr = catalog.get("Manufacturer", "")
        if norm(catalog_mpn) != norm(mpn):
            out.append(
                (WARN, f"{ours} is Mouser's number for MPN {catalog_mpn!r}, we call the part {mpn!r}")
            )
        if not same_maker(entry["manufacturer"], catalog_mfr):
            out.append((FAIL, f"{ours} is made by {catalog_mfr!r}, we say {entry['manufacturer']!r}"))
        return out

    part = mo.lookup(mpn, entry["manufacturer"])
    if ours:
        fix = f"; the catalog number for this part is {part.get('MouserPartNumber')}" if part else ""
        out.append((FAIL, f"Mouser has no part number {ours}{fix}"))
    elif part is None:
        return [(NOTE, f"Mouser does not carry {mpn}")]
    if part is not None:
        out += mouser_health(part)
    return out


def check_stock(entry: dict, stock: dict, low_stock: int = LOW_STOCK_BOARDS) -> list[tuple[str, str]]:
    """Stock rules over the recorded listings, see the module docstring.

    `stock` maps distributor -> units on hand for each recorded number the
    catalog resolved; a listing whose lookup failed is absent (or None), so
    it is never counted as out of stock.
    """
    out = []
    recorded = [d for d in ("DigiKey", "Mouser") if entry.get(d)]
    all_out = bool(recorded) and all(stock.get(d) == 0 for d in recorded)
    need = low_stock * entry.get("per_board", 1)
    for distributor in recorded:
        n, number = stock.get(distributor), entry[distributor]
        if n is None:
            continue
        if n == 0:
            primary = distributor == "DigiKey"
            level = FAIL if primary or all_out else WARN
            why = " (primary distributor)" if primary else ""
            out.append((level, f"{distributor} {number} has no stock{why}"))
        elif n < need:
            out.append(
                (WARN, f"{distributor} {number} has {n:,} in stock, under {low_stock} boards' worth ({need:,})")
            )
    return out


def stock_summary(entry: dict, stock: dict) -> str:
    """`DigiKey 4,513,879 / Mouser 2,046,812` over the recorded listings."""
    return " / ".join(
        f"{d} {'?' if stock.get(d) is None else f'{stock[d]:,}'}"
        for d in ("DigiKey", "Mouser")
        if entry.get(d)
    )


# -------------------------------------------------------------------- main

def verify(
    builds: list[tuple[Path, str]],
    only: str | None = None,
    root: Path | None = None,
    low_stock: int = LOW_STOCK_BOARDS,
) -> int:
    root = root or Path.cwd()
    if not builds:
        builds = discover_builds(root)
        if not builds:
            raise SystemExit(f"no layouts/<build>/<build>.json projects under {root}")

    parts = collect_parts(builds, root)
    if only:
        parts = {k: v for k, v in parts.items() if norm(k) == norm(only)}
        if not parts:
            raise SystemExit(f"no part with MPN {only}")

    dk = digikey_client()
    if dk is None:
        print("no DigiKey client - skipping DigiKey")
    mo = mouser_client()
    if mo is None:
        print("no Mouser client (MOUSER_API_KEY) - skipping Mouser")
    if dk is None and mo is None:
        raise SystemExit("no distributor credentials; nothing to check")

    print(f"checking {len(parts)} part numbers\n")
    failures, warnings, no_stock, short = [], [], [], []
    for mpn, entry in sorted(parts.items()):
        findings: list[tuple[str, str]] = []
        stock: dict = {}
        for client, checker in ((dk, check_digikey), (mo, check_mouser)):
            if client is None:
                continue
            try:
                findings += checker(client, mpn, entry, stock)
            except Exception as err:  # keep going; one bad part should not stop the run
                findings.append((WARN, f"{type(client).__name__} lookup error: {err}"))
        stock_findings = check_stock(entry, stock, low_stock)
        findings += stock_findings
        if any(l == FAIL for l, _ in stock_findings):
            no_stock.append(mpn)
        elif stock_findings:
            short.append(mpn)

        worst = FAIL if any(l == FAIL for l, _ in findings) else WARN if any(l == WARN for l, _ in findings) else "ok"
        if worst == FAIL:
            failures.append(mpn)
        elif worst == WARN:
            warnings.append(mpn)

        marker = {"ok": "ok  ", FAIL: "FAIL", WARN: "WARN"}[worst]
        summary = stock_summary(entry, stock)
        print(f"{marker} {mpn}  ({entry['manufacturer']})" + (f"  {summary}" if summary else ""))
        for level, message in findings:
            print(f"       {level}: {message}")

    print(
        f"\n{len(parts) - len(failures) - len(warnings)} ok, {len(warnings)} with warnings, "
        f"{len(failures)} failed; stock: {len(no_stock)} out, {len(short)} low or backup-only "
        f"(low = under {low_stock} boards' worth)"
    )
    if failures:
        print("failed: " + ", ".join(failures))
    if no_stock:
        print("out of stock: " + ", ".join(no_stock))
    return 1 if failures else 0
