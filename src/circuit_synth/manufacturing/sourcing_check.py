"""Check every part's sourcing data against the DigiKey and Mouser catalogs.

For each manufacturer part number in a build this asks both distributors
whether the part exists, whether the distributor number shipped in the BOM
really belongs to that MPN, and whether it is orderable today (active
lifecycle, stock on hand). A distributor number that looks plausible but
that no catalog carries is the failure this exists to catch - DigiKey's
`<supplier-prefix>-<MPN>CT-ND` form is easy to synthesise and impossible to
tell apart from a real one by eye.

Run it before sending a quote package. Exit status is non-zero when any part
fails, so it can gate an order.

Credentials come from the environment or ~/.circuit_synth/credentials (see
circuit_synth.manufacturing.catalog); a distributor is skipped when its
credentials are missing.

Usage:
  cs bom verify                                       # every build under cwd
  cs bom verify designs/mantis/layouts/default default
  cs bom verify --only SDCL1V4030-100M-R
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
    """mpn -> {manufacturer, DigiKey, Mouser, designs}."""
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
                    },
                )
                entry["designs"].add(label)

            for sub in circuit.get("subcircuits", []):
                walk(sub)

        walk(json.loads(path.read_text()))
    return parts


# ------------------------------------------------------------------ checks

def health(product: dict, distributor: str) -> list[tuple[str, str]]:
    out = []
    status = (product.get("ProductStatus") or {}).get("Status", "")
    if dead(status):
        out.append((WARN, f"{distributor} product status is {status!r}"))
    if product.get("QuantityAvailable") == 0:
        out.append((WARN, f"{distributor} has no stock"))
    return out


def check_digikey(dk: DigiKey, mpn: str, entry: dict) -> list[tuple[str, str]]:
    """Verify the stored DigiKey number really is this part's, via that number.

    The productdetails endpoint resolves DigiKey part numbers, so looking ours
    up is a direct existence test: a synthesised number simply 404s. Keyword
    search by MPN then supplies the number we should have used.
    """
    ours, out = entry["DigiKey"], []
    catalog = dk.by_number(ours) if ours else None

    if catalog is not None:
        out += health(catalog, "DigiKey")
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


def check_mouser(mo: Mouser, mpn: str, entry: dict) -> list[tuple[str, str]]:
    ours = entry["Mouser"]
    part = mo.lookup(mpn)
    if part is None:
        return [(FAIL if ours else NOTE, f"Mouser does not carry {mpn}")]

    out = []
    theirs = part.get("MouserPartNumber", "")
    if ours and norm(ours) != norm(theirs):
        out.append((FAIL, f"Mouser number {ours} does not match the catalog's {theirs}"))

    status = part.get("LifecycleStatus") or ""
    if dead(status):
        out.append((WARN, f"Mouser lifecycle status is {status!r}"))

    stock = part.get("AvailabilityInStock")
    if stock in ("0", 0):
        out.append((WARN, f"Mouser has no stock ({part.get('Availability') or 'none reported'})"))
    return out


# -------------------------------------------------------------------- main

def verify(builds: list[tuple[Path, str]], only: str | None = None, root: Path | None = None) -> int:
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
    failures, warnings = [], []
    for mpn, entry in sorted(parts.items()):
        findings: list[tuple[str, str]] = []
        for client, checker in ((dk, check_digikey), (mo, check_mouser)):
            if client is None:
                continue
            try:
                findings += checker(client, mpn, entry)
            except Exception as err:  # keep going; one bad part should not stop the run
                findings.append((WARN, f"{type(client).__name__} lookup error: {err}"))

        worst = FAIL if any(l == FAIL for l, _ in findings) else WARN if any(l == WARN for l, _ in findings) else "ok"
        if worst == FAIL:
            failures.append(mpn)
        elif worst == WARN:
            warnings.append(mpn)

        marker = {"ok": "ok  ", FAIL: "FAIL", WARN: "WARN"}[worst]
        print(f"{marker} {mpn}  ({entry['manufacturer']})")
        for level, message in findings:
            print(f"       {level}: {message}")

    print(f"\n{len(parts) - len(failures) - len(warnings)} ok, {len(warnings)} with warnings, {len(failures)} failed")
    if failures:
        print("failed: " + ", ".join(failures))
    return 1 if failures else 0
