"""
rasterise_tiles.py
──────────────────
Converts a DXF-derived SVG → high-res PNG → XYZ tile pyramid for Leaflet.

Reads  : drawing.svg  +  transform.json  (written by render_svg.py)
Writes : tiles/{z}/{x}/{y}.png
         tile_meta.json      ← loaded by DXFViewer.tsx instead of the SVG
         transform.json      ← updated in-place with png + scale + leaflet_bounds

Rasteriser: Inkscape CLI
    Install : https://inkscape.org/release/
    Windows : add to PATH, or pass --inkscape "C:\\Program Files\\Inkscape\\bin\\inkscape.exe"
    Linux   : sudo apt install inkscape  /  sudo dnf install inkscape
    macOS   : brew install inkscape

Requirements (tiling only):
    pip install pillow

Usage:
    python rasterise_tiles.py --svg drawing.svg --transform transform.json
    python rasterise_tiles.py --svg drawing.svg --transform transform.json --max-zoom 6
    python rasterise_tiles.py --svg drawing.svg --transform transform.json \\
        --inkscape "C:\\Program Files\\Inkscape\\bin\\inkscape.exe"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TILE_SIZE         = 256
MIN_FULL_WIDTH_PX = 4096


# ── Inkscape ──────────────────────────────────────────────────────────────────

def _find_inkscape(hint: str | None) -> str:
    candidates = []
    if hint:
        candidates.append(hint)
    candidates.append(shutil.which("inkscape") or "")
    candidates += [
        r"C:\Program Files\Inkscape\bin\inkscape.exe",
        r"C:\Program Files (x86)\Inkscape\bin\inkscape.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    sys.exit(
        "\nERROR: Inkscape not found.\n\n"
        "  Install from : https://inkscape.org/release/\n"
        "  Then either  : add Inkscape\\bin to your PATH\n"
        "      or pass  : --inkscape \"C:\\Program Files\\Inkscape\\bin\\inkscape.exe\"\n"
    )


def _check_inkscape_version(exe: str):
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        line = (result.stdout or result.stderr or "").splitlines()[0]
        print(f"  Inkscape     : {line.strip()}")
    except Exception as exc:
        print(f"  Inkscape     : {exe}  (version check failed: {exc})")


def _rasterise_inkscape(svg_path: str, out_png: str, width_px: int, exe: str):
    cmd = [
        exe,
        os.path.abspath(svg_path),
        f"--export-filename={os.path.abspath(out_png)}",
        f"--export-width={width_px}",
        "--export-background=white",
        "--export-background-opacity=1",
    ]
    print(f"  Running      : {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("\n-- Inkscape stdout --", file=sys.stderr)
        print(result.stdout,            file=sys.stderr)
        print("-- Inkscape stderr --",  file=sys.stderr)
        print(result.stderr,            file=sys.stderr)
        sys.exit(f"\nInkscape exited with code {result.returncode}")
    for line in (result.stderr or "").splitlines():
        if line.strip():
            print(f"  [inkscape] {line}")


# ── tiler ─────────────────────────────────────────────────────────────────────

def _auto_max_zoom(vb_w: float) -> int:
    target_tiles = math.ceil(MIN_FULL_WIDTH_PX / TILE_SIZE)
    z = max(math.ceil(math.log2(target_tiles)), 3)
    return min(z, 8)


def _generate_tiles(img, out_dir: Path, max_zoom: int,
                    full_w: int, full_h: int, tile_sz: int):
    from PIL import Image

    total_tiles = sum((2 ** z) ** 2 for z in range(max_zoom + 1))
    written = 0

    for z in range(max_zoom + 1):
        n        = 2 ** z
        canvas_w = n * tile_sz
        canvas_h = n * tile_sz

        scale_ratio = min(canvas_w / full_w, canvas_h / full_h)
        target_w    = round(full_w * scale_ratio)
        target_h    = round(full_h * scale_ratio)

        scaled = img.resize((target_w, target_h), Image.LANCZOS)

        canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
        if scaled.mode == "RGBA":
            canvas.paste(scaled, (0, 0), mask=scaled.split()[3])
        else:
            canvas.paste(scaled, (0, 0))

        for tx in range(n):
            for ty in range(n):
                left  = tx * tile_sz
                upper = ty * tile_sz
                tile  = canvas.crop((left, upper, left + tile_sz, upper + tile_sz))
                tile_dir = out_dir / str(z) / str(tx)
                tile_dir.mkdir(parents=True, exist_ok=True)
                tile.save(tile_dir / f"{ty}.png", "PNG", optimize=True)
                written += 1

        pct = 100 * written / total_tiles
        print(f"  z={z}  {n:>3}x{n:<3} tiles  [{written}/{total_tiles}  {pct:.0f}%]")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Rasterise DXF SVG to PNG tile pyramid using Inkscape."
    )
    p.add_argument("--svg",       required=True, metavar="FILE",
                   help="SVG from render_svg.py")
    p.add_argument("--transform", required=True, metavar="FILE",
                   help="transform.json from render_svg.py (updated in-place)")
    p.add_argument("--inkscape",  default=None,  metavar="EXE",
                   help="Path to inkscape executable (default: auto-detect)")
    p.add_argument("--max-zoom",  type=int, default=None, metavar="N",
                   help=f"Max tile zoom level (default: auto so drawing is "
                        f">={MIN_FULL_WIDTH_PX}px wide)")
    p.add_argument("--tiles-dir", default="tiles",          metavar="DIR")
    p.add_argument("--tile-meta", default="tile_meta.json", metavar="FILE")
    p.add_argument("--tile-size", type=int, default=TILE_SIZE, metavar="PX")
    return p.parse_args()


def main():
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        sys.exit("Pillow not installed.  Run:  pip install pillow")

    args     = parse_args()
    tile_sz  = args.tile_size
    inkscape = _find_inkscape(args.inkscape)
    _check_inkscape_version(inkscape)

    with open(args.transform) as f:
        transform = json.load(f)

    vb_w  = transform["svg"]["viewbox_w"]
    vb_h  = transform["svg"]["viewbox_h"]
    dxf_w = transform["dxf"]["width"]
    dxf_h = transform["dxf"]["height"]

    max_zoom    = args.max_zoom or _auto_max_zoom(vb_w)
    target_w_px = 2 ** max_zoom * tile_sz
    target_h_px = round(vb_h * (target_w_px / vb_w))

    print(f"SVG viewBox  : {vb_w:.2f} x {vb_h:.2f} mm")
    print(f"Max zoom     : {max_zoom}  "
          f"(grid {2**max_zoom}x{2**max_zoom}, "
          f"target {target_w_px} x {target_h_px} px)")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_png = tmp.name

    try:
        print(f"Rasterising  : {args.svg} -> {target_w_px}px wide ...")
        _rasterise_inkscape(args.svg, tmp_png, target_w_px, inkscape)

        from PIL import Image
        full_img = Image.open(tmp_png)
        full_img.load()
        full_img = full_img.convert("RGBA")
    finally:
        try:
            os.unlink(tmp_png)
        except OSError:
            pass

    actual_w, actual_h = full_img.size
    print(f"Rendered     : {actual_w} x {actual_h} px")

    full_w_px = actual_w
    full_h_px = actual_h

    tiles_dir = Path(args.tiles_dir)
    tiles_dir.mkdir(parents=True, exist_ok=True)
    print(f"Tiling into  : {tiles_dir}/")
    _generate_tiles(full_img, tiles_dir, max_zoom, full_w_px, full_h_px, tile_sz)

    scale_x         = full_w_px / dxf_w
    scale_y         = full_h_px / dxf_h
    px_per_svg_unit = full_w_px / vb_w

    tile_meta = {
        "max_zoom":           max_zoom,
        "tile_size":          tile_sz,
        "full_width_px":      full_w_px,
        "full_height_px":     full_h_px,
        "svg_viewbox_width":  vb_w,
        "svg_viewbox_height": vb_h,
        "px_per_svg_unit":    round(px_per_svg_unit, 6),
        "px_per_dxf_unit":    round(scale_x, 6),
        "leaflet_bounds":     [[-full_h_px, 0], [0, full_w_px]],
    }

    with open(args.tile_meta, "w") as f:
        json.dump(tile_meta, f, indent=2)
    print(f"\nTile meta    : {args.tile_meta}")

    transform["png"] = {
        "width_px":  full_w_px,
        "height_px": full_h_px,
        "dpi":       round(full_w_px / (vb_w / 25.4), 1),
    }
    transform["scale_x"]        = round(scale_x, 6)
    transform["scale_y"]        = round(scale_y, 6)
    transform["leaflet_bounds"] = [[-full_h_px, 0], [0, full_w_px]]

    with open(args.transform, "w") as f:
        json.dump(transform, f, indent=2)
    print(f"Transform    : {args.transform}  (updated with png + scale + leaflet_bounds)")

    total_tiles = sum((2 ** z) ** 2 for z in range(max_zoom + 1))
    print()
    print("-- Summary ---------------------------------------------------")
    print(f"  Drawing     : {full_w_px} x {full_h_px} px")
    print(f"  Zoom levels : 0 - {max_zoom}")
    print(f"  Total tiles : {total_tiles}")
    print(f"  Tile dir    : {tiles_dir}/")
    print(f"  tile_meta   : {args.tile_meta}")
    print()
    print("-- Next step -------------------------------------------------")
    print("  python extract_manifest.py \\")
    print("    --dxf drawing.dxf \\")
    print("    --labels labels.txt \\")
    print("    --svg drawing.svg \\")
    print(f"    --transform {args.transform} \\")
    print("    --out label-manifest.json")
    print()
    print("  Serve tiles:  python -m http.server 8765")
    print("  Then load tile_meta.json + label-manifest.json in DXFViewer")


if __name__ == "__main__":
    main()
