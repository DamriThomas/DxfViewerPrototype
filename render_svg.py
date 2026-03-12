# render_svg.py
# Renders a DXF to SVG and writes transform.json.
#
# Typical pipeline:
#   1. python render_svg.py    input.dxf                → drawing.svg + transform.json
#   2. python rasterise_tiles.py --svg drawing.svg \    → tiles/ + tile_meta.json
#                                --transform transform.json      (updates transform.json)
#   3. python extract_manifest.py --dxf input.dxf \    → label-manifest.json
#                                  --labels labels.txt \
#                                  --svg drawing.svg \
#                                  --transform transform.json
#
# Usage:
#   python render_svg.py input.dxf [output.svg] [--text-to-path]
#
#   --text-to-path   Convert all DXF text/MTEXT to filled outline paths in the
#                    SVG rather than <text> elements.  Use this when your DXF
#                    fonts are not available on the viewing machine, or when you
#                    need pixel-accurate glyph rendering.  Note: extract_manifest
#                    cannot read paths as text, so it will fall back to DXF
#                    coordinate matching (unaffected by this flag).
#
# Outputs:
#   output.svg      -- vector SVG via ezdxf SVGBackend
#   transform.json  -- coordinate transform manifest (consumed by rasterise_tiles.py
#                      and extract_manifest.py)

import argparse
import json
import re
import sys

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.layout import Margins, Page, Settings, Units
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.bbox import extents as bbox_extents

# ---- CLI --------------------------------------------------------------------
parser = argparse.ArgumentParser(
    description="Render DXF to SVG and write transform.json."
)
parser.add_argument("dxf", nargs="?", default="test_diagram.dxf",
                    help="Input DXF file (default: test_diagram.dxf)")
parser.add_argument("svg", nargs="?", default=None,
                    help="Output SVG file (default: <dxf_stem>.svg)")
parser.add_argument("--text-to-path", action="store_true",
                    help="Convert text/MTEXT to outline paths instead of "
                         "<text> elements (font-independent, path-accurate)")
parser.add_argument("--transform-out", default="transform.json", metavar="FILE",
                    help="Output path for transform.json (default: transform.json)")
args = parser.parse_args()

dxf_path     = args.dxf
svg_path     = args.svg or (dxf_path.rsplit(".", 1)[0] + ".svg")
text_to_path = args.text_to_path

print(f"DXF          : {dxf_path}")
print(f"SVG          : {svg_path}")
print(f"text-to-path : {text_to_path}")

doc = ezdxf.readfile(dxf_path)
msp = doc.modelspace()

# ---- 1. DXF extents via entity bbox scan ------------------------------------
# Never trust $EXTMIN/$EXTMAX -- often sentinel values (~1e20)
print("Scanning entity extents...")
bbox = bbox_extents(msp)
if bbox is None or not bbox.has_data:
    sys.exit("ERROR: Could not determine drawing extents")

dxf_x_min, dxf_y_min = bbox.extmin.x, bbox.extmin.y
dxf_x_max, dxf_y_max = bbox.extmax.x, bbox.extmax.y
dxf_w = dxf_x_max - dxf_x_min
dxf_h = dxf_y_max - dxf_y_min
print(f"DXF extents  : x=[{dxf_x_min:.4f}, {dxf_x_max:.4f}]  "
      f"y=[{dxf_y_min:.4f}, {dxf_y_max:.4f}]")
print(f"DXF size     : {dxf_w:.4f} x {dxf_h:.4f} units")

# ---- 2. Build render Settings -----------------------------------------------
# text_policy / text_as_paths controls whether text is emitted as <text>
# elements or converted to filled <path> glyphs in the SVG output.
# We try the modern API first (ezdxf >= 1.1) and fall back gracefully.
def _make_settings(text_to_path: bool) -> Settings:
    if not text_to_path:
        return Settings()
    # Modern API (ezdxf >= 1.1): Settings(text_policy=TextPolicy.FILLING)
    try:
        from ezdxf.addons.drawing.properties import TextPolicy
        print("text-to-path : using TextPolicy.FILLING")
        return Settings(text_policy=TextPolicy.FILLING)
    except (ImportError, AttributeError, TypeError):
        pass
    # Older API: Settings(text_as_paths=True)
    try:
        s = Settings(text_as_paths=True)
        print("text-to-path : using text_as_paths=True")
        return s
    except TypeError:
        pass
    # Last resort: show_text attribute
    s = Settings()
    if hasattr(s, "show_text"):
        s.show_text = False
        print("text-to-path : using show_text=False (fallback)")
    else:
        print("WARNING: text-to-path not supported by this ezdxf version -- "
              "text will remain as <text> elements.", file=sys.stderr)
    return s

settings = _make_settings(text_to_path)

# ---- 3. Render SVG ----------------------------------------------------------
print("Rendering SVG...")
ctx     = RenderContext(doc)
backend = SVGBackend()
Frontend(ctx, backend).draw_layout(msp)

page       = Page(0, 0, Units.mm, Margins(0, 0, 0, 0))
svg_string = backend.get_string(page, settings=settings)

with open(svg_path, "w", encoding="utf-8") as f:
    f.write(svg_string)
print(f"SVG written  : {svg_path}")

# ---- 4. Parse SVG viewBox ---------------------------------------------------
# ezdxf SVGBackend outputs viewBox in mm (Units.mm above)
vb_match = re.search(r'viewBox="([^"]+)"', svg_string)
svg_vb_x, svg_vb_y, svg_vb_w, svg_vb_h = 0.0, 0.0, dxf_w, dxf_h  # fallback
if vb_match:
    parts = [float(x) for x in vb_match.group(1).split()]
    if len(parts) == 4:
        svg_vb_x, svg_vb_y, svg_vb_w, svg_vb_h = parts
print(f"SVG viewBox  : {svg_vb_w:.4f} x {svg_vb_h:.4f} mm  "
      f"(origin {svg_vb_x:.4f}, {svg_vb_y:.4f})")

# ---- 5. Write transform.json ------------------------------------------------
transform = {
    "dxf": {
        "x_min":  dxf_x_min, "y_min": dxf_y_min,
        "x_max":  dxf_x_max, "y_max": dxf_y_max,
        "width":  dxf_w,     "height": dxf_h,
    },
    "svg": {
        "viewbox_x": svg_vb_x,
        "viewbox_y": svg_vb_y,
        "viewbox_w": svg_vb_w,
        "viewbox_h": svg_vb_h,
    },
    # Fill these in after generating your PNG, then re-run extract_manifest.py
    # "png": { "width_px": 0, "height_px": 0, "dpi": 0 },
    # "scale_x": <png_w / dxf_w>,
    # "scale_y": <png_h / dxf_h>,
    # "leaflet_bounds": [[-png_h, 0], [0, png_w]],
}

with open(args.transform_out, "w", encoding="utf-8") as f:
    json.dump(transform, f, indent=2)
print(f"Transform    : {args.transform_out}")

print()
print("── Next step ─────────────────────────────────────────────────────────")
print(f"  python rasterise_tiles.py \\")
print(f"    --svg {svg_path} \\")
print(f"    --transform {args.transform_out}")
print()
print("  rasterise_tiles.py will:")
print("    • rasterise the SVG to a high-res PNG tile pyramid (tiles/)")
print("    • write tile_meta.json for DXFViewer")
print(f"    • update {args.transform_out} with png + scale + leaflet_bounds")
print("      (so extract_manifest.py gains Leaflet hitbox coords automatically)")
