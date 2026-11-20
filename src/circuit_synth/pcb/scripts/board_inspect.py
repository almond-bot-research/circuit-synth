"""Read-only board queries for routing by hand (`cs pcb pads|drc|render`).

An agent routing a board cannot look at pcbnew, so these give it the
three things it needs: where the pads are, what DRC says (with
coordinates), and a picture of any region.

    board_inspect.py pads   <board> [--ref U3 ...] [--net GND ...] [--box x0,y0,x1,y1]
    board_inspect.py drc    <board> [--all] [--limit N]
    board_inspect.py render <board> <out.png|.svg> [--box x0,y0,x1,y1] [--layers F.Cu,...] [--px N]
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kicadpcb import find_kicad_cli  # noqa: E402


def _opt(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else default


def _multi(argv: list[str], name: str) -> list[str]:
    out, i = [], 0
    while i < len(argv):
        if argv[i] == name:
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                out.append(argv[i])
                i += 1
        else:
            i += 1
    return out


def _box(text: str | None) -> tuple[float, float, float, float] | None:
    if not text:
        return None
    x0, y0, x1, y1 = (float(v) for v in text.split(","))
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


# ------------------------------------------------------------------- pads

def pads(board_path: str, argv: list[str]) -> int:
    """Print `REF.PAD net x y layer size` for the selected pads."""
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    refs, nets, box = set(_multi(argv, "--ref")), set(_multi(argv, "--net")), _box(_opt(argv, "--box"))
    rows = []
    for fp in board.GetFootprints():
        if refs and fp.GetReference() not in refs:
            continue
        for pad in fp.Pads():
            net = pad.GetNetname()
            if nets and net not in nets and net.lstrip("/") not in nets:
                continue
            x, y = pcbnew.ToMM(pad.GetPosition().x), pcbnew.ToMM(pad.GetPosition().y)
            if box and not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                continue
            bb = pad.GetBoundingBox()
            tht = pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
            layer = "THT" if tht else ("F" if pad.IsOnLayer(pcbnew.F_Cu) else "B")
            rows.append((fp.GetReference(), pad.GetNumber(), pad.GetPinFunction(), net, x, y, layer,
                         pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())))
    rows.sort(key=lambda r: (r[0], [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", r[1])]))
    for ref, num, fn, net, x, y, layer, w, h in rows:
        name = f"{ref}.{num}"
        print(f"{name:10s} {fn[:14]:14s} {net:24s} {x:8.3f} {y:8.3f} {layer:3s} {w:.2f}x{h:.2f}")
    print(f"{len(rows)} pads")
    return 0


# -------------------------------------------------------------------- drc

def drc(board_path: str, argv: list[str]) -> int:
    """Run kicad-cli DRC and print a compact summary with coordinates."""
    limit = int(_opt(argv, "--limit", "40"))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "drc.json"
        cmd = [find_kicad_cli(), "pcb", "drc", "--format", "json", "--schematic-parity",
               "--severity-all" if "--all" in argv else "--severity-error", "-o", str(out), board_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not out.exists():
            print("kicad-cli produced no DRC report")
            return 1
        report = json.loads(out.read_text())

    def where(v) -> str:
        return "; ".join(
            f"{i['description']} @({i['pos']['x']:.2f},{i['pos']['y']:.2f})" for i in v.get("items", [])
        )

    viol = report.get("violations", [])
    unconnected = report.get("unconnected_items", [])
    parity = [v for v in report.get("schematic_parity", []) if v.get("severity") == "error"]
    counts = collections.Counter((v["severity"], v["type"]) for v in viol)
    print(f"violations: {len(viol)}  unconnected: {len(unconnected)}  parity errors: {len(parity)}")
    for (sev, typ), n in sorted(counts.items()):
        print(f"  {sev:8s} {typ:28s} {n}")
    shown = 0
    for v in viol:
        if v["severity"] != "error" and "--all" not in argv:
            continue
        if shown >= limit:
            print(f"  ... {len(viol) - shown} more")
            break
        print(f"  {v['type']}: {v['description']} | {where(v)}")
        shown += 1
    by_net = collections.Counter()
    for u in unconnected:
        m = re.search(r"\[([^\]]+)\]", u["items"][0]["description"]) if u.get("items") else None
        by_net[m.group(1) if m else "?"] += 1
    if by_net:
        print("unconnected by net: " + ", ".join(f"{n} {c}" for n, c in by_net.most_common()))
    for u in unconnected[:limit]:
        print(f"  unconnected: {where(u)}")
    for v in parity[:limit]:
        print(f"  parity: {v['description']} | {where(v)}")
    return 0 if not (unconnected or parity or any(v["severity"] == "error" for v in viol)) else 1


# ----------------------------------------------------------------- render

def render(board_path: str, out_path: str, argv: list[str]) -> int:
    """Render a region of the board to SVG or PNG."""
    layers = _opt(argv, "--layers", "F.Cu,B.Cu,Edge.Cuts")
    box = _box(_opt(argv, "--box"))
    px = int(_opt(argv, "--px", "1600"))
    out = Path(out_path)
    with tempfile.TemporaryDirectory() as tmp:
        svg = Path(tmp) / "board.svg"
        # page-size-mode 1 keeps board millimetres as SVG user units
        subprocess.run(
            [find_kicad_cli(), "pcb", "export", "svg", "--layers", layers, "--mode-single",
             "--page-size-mode", "1", "--exclude-drawing-sheet", "-o", str(svg), board_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not svg.exists():
            print("kicad-cli produced no SVG")
            return 1
        text = svg.read_text()
        if box:
            w, h = box[2] - box[0], box[3] - box[1]
            if out.suffix.lower() != ".svg" and not any(shutil.which(t) for t in ("rsvg-convert", "inkscape", "magick")):
                # qlmanage only makes square thumbnails and crops anything else
                side = max(w, h)
                box = (box[0], box[1], box[0] + side, box[1] + side)
                w = h = side
            head_end = text.index(">", text.index("<svg"))
            head = text[: head_end + 1]
            head = re.sub(r'viewBox="[^"]*"', f'viewBox="{box[0]} {box[1]} {w} {h}"', head)
            head = re.sub(r'\swidth="[^"]*"', f' width="{px}"', head, count=1)
            head = re.sub(r'\sheight="[^"]*"', f' height="{round(px * h / w)}"', head, count=1)
            # dark background so copper colours read
            bg = f'<rect x="{box[0]}" y="{box[1]}" width="{w}" height="{h}" fill="#101418"/>'
            text = head + bg + text[head_end + 1:]
        if out.suffix.lower() == ".svg":
            out.write_text(text)
            print(out)
            return 0
        cropped = Path(tmp) / "crop.svg"
        cropped.write_text(text)
        return _to_png(cropped, out, px)


def _to_png(svg: Path, out: Path, px: int) -> int:
    if shutil.which("rsvg-convert"):
        cmd = ["rsvg-convert", "-w", str(px), "-o", str(out), str(svg)]
    elif shutil.which("inkscape"):
        cmd = ["inkscape", str(svg), "-w", str(px), "-o", str(out)]
    elif shutil.which("magick"):
        cmd = ["magick", "-density", "300", str(svg), "-resize", f"{px}x", str(out)]
    elif shutil.which("qlmanage"):  # macOS fallback: square thumbnail
        subprocess.run(["qlmanage", "-t", "-s", str(px), "-o", str(svg.parent), str(svg)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        thumb = svg.parent / (svg.name + ".png")
        if not thumb.exists():
            print("qlmanage could not rasterize the SVG")
            return 1
        shutil.move(str(thumb), str(out))
        print(out)
        return 0
    else:
        print("no SVG rasterizer found (rsvg-convert, inkscape, magick, qlmanage); ask for .svg instead")
        return 1
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0:
        print(out)
    return res.returncode


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("pads", "drc", "render"):
        print(__doc__)
        return 2
    os.environ.setdefault("KICAD_CLI", find_kicad_cli())
    if argv[0] == "pads":
        return pads(argv[1], argv[2:])
    if argv[0] == "drc":
        return drc(argv[1], argv[2:])
    return render(argv[1], argv[2], argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
