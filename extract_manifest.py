"""
extract_manifest.py
───────────────────
Generates label-manifest.json from a DXF file and a labels list.
Includes full coordinate transform chain: DXF → SVG → PNG → Leaflet CRS.Simple

Usage:
    # SVG-only (no PNG yet) — produces manifest + debug SVG:
    python extract_manifest.py \\
        --dxf drawing.dxf \\
        --labels labels.txt \\
        --svg drawing.svg \\
        --transform transform.json \\
        --out label-manifest.json \\
        --debug-svg debug_labels.svg

    # With PNG (full Leaflet coords):
    python extract_manifest.py \\
        --dxf drawing.dxf \\
        --labels labels.txt \\
        --svg drawing.svg \\
        --transform transform.json \\
        --out label-manifest.json \\
        --debug-svg debug_labels.svg

    # Inline labels:
    python extract_manifest.py \\
        --dxf drawing.dxf \\
        --labels-inline DV001 EV301 HV201 \\
        --transform transform.json

Requirements:
    pip install ezdxf lxml
"""

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


# ──────────────────────────────────────────────
# 1.  DXF EXTRACTION
# ──────────────────────────────────────────────

def extract_dxf_text_entities(dxf_path: str) -> list[dict]:
    """Walk every TEXT and MTEXT entity in the DXF modelspace."""
    try:
        import ezdxf
    except ImportError:
        sys.exit("ezdxf not installed. Run: pip install ezdxf")

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    entities = []
    for entity in msp:
        etype = entity.dxftype()
        if etype == "TEXT":
            raw = _parse_text(entity)
            if raw:
                entities.append(raw)
        elif etype == "MTEXT":
            raw = _parse_mtext(entity)
            if raw:
                entities.append(raw)
    return entities


def extract_dxf_extents(dxf_path: str) -> dict | None:
    """Scan entity bounding box for drawing extents."""
    try:
        import ezdxf
        from ezdxf.bbox import extents as bbox_extents
    except ImportError:
        sys.exit("ezdxf not installed. Run: pip install ezdxf")

    doc  = ezdxf.readfile(dxf_path)
    msp  = doc.modelspace()
    bbox = bbox_extents(msp)

    if bbox is None or not bbox.has_data:
        return None

    x_min, y_min = bbox.extmin.x, bbox.extmin.y
    x_max, y_max = bbox.extmax.x, bbox.extmax.y
    return {
        "x_min": round(x_min, 6), "y_min": round(y_min, 6),
        "x_max": round(x_max, 6), "y_max": round(y_max, 6),
        "width":  round(x_max - x_min, 6),
        "height": round(y_max - y_min, 6),
    }


def _parse_text(e) -> dict | None:
    try:
        text = (e.dxf.text or "").strip()
        if not text:
            return None
        insert   = e.dxf.insert
        rotation = getattr(e.dxf, "rotation", 0.0) or 0.0
        height   = getattr(e.dxf, "height", 0.0) or 0.0
        layer    = getattr(e.dxf, "layer", "0") or "0"
        style    = getattr(e.dxf, "style", "STANDARD") or "STANDARD"
        halign   = getattr(e.dxf, "halign", 0)
        valign   = getattr(e.dxf, "valign", 0)
        # width_factor: scales glyph widths (default 1.0)
        width_factor = getattr(e.dxf, "width", 1.0) or 1.0
        return {
            "handle":       e.dxf.handle,
            "type":         "TEXT",
            "text":         text,
            "layer":        layer,
            "insert":       [round(insert.x, 4), round(insert.y, 4)],
            "rotation":     round(rotation, 4),
            "height":       round(height, 4),
            "style":        style,
            "halign":       halign,
            "valign":       valign,
            "width_factor": round(width_factor, 4),
        }
    except Exception:
        return None


def _parse_mtext(e) -> dict | None:
    try:
        raw_text = e.plain_mtext().strip()
        if not raw_text:
            return None
        insert   = e.dxf.insert
        rotation = math.degrees(getattr(e.dxf, "rotation", 0.0) or 0.0)
        height   = getattr(e.dxf, "char_height", 0.0) or 0.0
        layer    = getattr(e.dxf, "layer", "0") or "0"
        # MTEXT attachment_point encodes halign/valign (1-9 grid)
        attach   = getattr(e.dxf, "attachment_point", 1) or 1
        # MTEXT reference_column_width constrains line wrapping
        col_width = getattr(e.dxf, "width", 0.0) or 0.0
        return {
            "handle":        e.dxf.handle,
            "type":          "MTEXT",
            "text":          raw_text,
            "layer":         layer,
            "insert":        [round(insert.x, 4), round(insert.y, 4)],
            "rotation":      round(rotation, 4),
            "height":        round(height, 4),
            "style":         None,
            "halign":        None,
            "valign":        None,
            "width_factor":  1.0,
            "attach":        attach,
            "col_width":     round(col_width, 4),
        }
    except Exception:
        return None


# ──────────────────────────────────────────────
# 2.  TIGHT HITBOX IN DXF SPACE
#
#  DXF TEXT halign codes:
#    0=Left  1=Center  2=Right  3=Aligned  4=Middle  5=Fit
#  DXF TEXT valign codes:
#    0=Baseline  1=Bottom  2=Middle  3=Top
#
#  MTEXT attachment_point (1-9):
#    1=TL 2=TC 3=TR  4=ML 5=MC 6=MR  7=BL 8=BC 9=BR
#
#  Glyph width estimation:
#    Most DXF fonts use ~0.6 × height per character as advance width.
#    SHX condensed styles narrow this; width_factor further scales it.
#    We add a small pad (5 % each side) so the box is never clipped.
# ──────────────────────────────────────────────

# Per-glyph advance-width as a fraction of cap-height.
# Values are normalised so capital 'H' ≈ 0.68.
# Narrow glyphs (I, 1, f, i, l, j, r, t) are explicitly narrower;
# wide glyphs (M, W, m, w) are wider.  Everything else falls back to
# the style default.
_GLYPH_WIDTH: dict[str, float] = {
    # Very narrow
    "I": 0.34, "i": 0.32, "l": 0.32, "1": 0.46, "!": 0.34,
    "|": 0.30, "j": 0.34, ":": 0.34, ";": 0.34, ".": 0.34,
    ",": 0.34, "'": 0.32, "`": 0.32, " ": 0.38,
    # Narrow
    "f": 0.50, "r": 0.52, "t": 0.54, "J": 0.54,
    # Slightly narrow
    "s": 0.62, "S": 0.68, "c": 0.64, "e": 0.64, "a": 0.66,
    "z": 0.62, "x": 0.64, "k": 0.66, "v": 0.64, "y": 0.64,
    "C": 0.74, "E": 0.68, "F": 0.66, "L": 0.66, "P": 0.70,
    "Z": 0.70, "K": 0.74, "X": 0.74, "Y": 0.72, "V": 0.74,
    # Normal
    "A": 0.78, "B": 0.76, "D": 0.80, "G": 0.80, "H": 0.80,
    "N": 0.80, "O": 0.82, "Q": 0.82, "R": 0.76, "T": 0.72,
    "U": 0.78, "b": 0.70, "d": 0.70, "g": 0.70, "h": 0.70,
    "n": 0.70, "o": 0.70, "p": 0.70, "q": 0.70, "u": 0.70,
    "0": 0.74, "2": 0.70, "3": 0.70, "4": 0.72, "5": 0.70,
    "6": 0.72, "7": 0.66, "8": 0.74, "9": 0.72,
    # Wide
    "m": 0.96, "w": 0.92, "M": 0.92, "W": 0.98,
    # Symbols
    "-": 0.50, "_": 0.70, "/": 0.54, "\\": 0.54,
    "(": 0.46, ")": 0.46, "[": 0.46, "]": 0.46,
    "&": 0.84, "@": 1.00, "#": 0.82, "%": 0.84,
    "+": 0.78, "=": 0.78, "<": 0.74, ">": 0.74,
}
_DEFAULT_GLYPH_WIDTH = 0.74   # fallback for unmapped chars

_STYLE_SCALE: dict[str, float] = {
    "STANDARD":        1.00,
    "ROMANS":          0.96,
    "ROMANC":          1.04,
    "ROMAND":          1.04,
    "ROMANT":          1.08,
    "ITALICC":         1.00,
    "SCRIPT":          0.98,
    "SIMPLEX":         0.96,
    "MONOTXT":         1.00,
    "ARIAL":           1.00,
    "ARIAL NARROW":    0.82,
    "TIMES NEW ROMAN": 0.96,
}
_DEFAULT_STYLE_SCALE = 1.00

_PAD_FACTOR = 0.12   # 12 % of height on each side


def _estimate_text_width(text: str, style: str | None, height: float,
                         width_factor: float) -> float:
    """Return estimated advance width in DXF units for the given text string."""
    scale = _STYLE_SCALE.get((style or "").upper(), _DEFAULT_STYLE_SCALE)
    raw = sum(_GLYPH_WIDTH.get(ch, _DEFAULT_GLYPH_WIDTH) for ch in text)
    return raw * height * scale * width_factor


def compute_dxf_bbox(entity: dict) -> dict | None:
    """
    Return a tight axis-aligned bounding box for the entity in DXF space.

    Returns:
        {
          "x": left edge,
          "y": bottom edge,
          "width": ...,
          "height": ...,
          "cx": centre x,
          "cy": centre y,
          "rotation": degrees (for rendering a rotated rect),
          # corners of the oriented bounding box (pre-rotation → then rotated)
          "corners": [[x0,y0],[x1,y1],[x2,y2],[x3,y3]],
        }
    All values in DXF units.  Returns None when height==0.
    """
    h = entity.get("height", 0.0) or 0.0
    if h == 0.0:
        return None

    text     = entity.get("text", "") or " "
    style    = entity.get("style")
    wf       = entity.get("width_factor", 1.0) or 1.0
    rotation = entity.get("rotation", 0.0) or 0.0

    raw_w = _estimate_text_width(text, style, h, wf)
    pad   = h * _PAD_FACTOR

    # ── Unrotated bbox extents relative to the alignment point ──────────
    # We compute (local_x_min, local_y_min, local_x_max, local_y_max)
    # where local coords have the text baseline running along +X.

    halign = entity.get("halign") or 0
    valign = entity.get("valign") or 0
    etype  = entity.get("type", "TEXT")

    if etype == "MTEXT":
        # attachment_point: 1=TL,2=TC,3=TR, 4=ML,5=MC,6=MR, 7=BL,8=BC,9=BR
        attach  = entity.get("attach", 1) or 1
        h_code  = (attach - 1) % 3      # 0=Left, 1=Center, 2=Right
        v_code  = (attach - 1) // 3     # 0=Top,  1=Middle, 2=Bottom
        col_w   = entity.get("col_width", 0.0) or 0.0
        if col_w > 0:
            raw_w = col_w
        # local X offset
        if h_code == 0:       # Left
            lx_min, lx_max = 0,          raw_w
        elif h_code == 1:     # Center
            lx_min, lx_max = -raw_w/2,  raw_w/2
        else:                 # Right
            lx_min, lx_max = -raw_w,    0
        # local Y offset (MTEXT Y-down attachment)
        # v_code 0=Top means insert is at the top
        if v_code == 0:       # Top
            ly_min, ly_max = -h, 0
        elif v_code == 1:     # Middle
            ly_min, ly_max = -h/2, h/2
        else:                 # Bottom
            ly_min, ly_max = 0, h
    else:
        # TEXT halign
        if halign in (0, 3, 5):   # Left / Aligned / Fit
            lx_min, lx_max = 0, raw_w
        elif halign == 1:          # Center
            lx_min, lx_max = -raw_w/2, raw_w/2
        elif halign == 2:          # Right
            lx_min, lx_max = -raw_w, 0
        elif halign == 4:          # Middle (centred on both axes)
            lx_min, lx_max = -raw_w/2, raw_w/2
        else:
            lx_min, lx_max = 0, raw_w

        # TEXT valign (DXF Y-up)
        if valign == 0:            # Baseline
            ly_min, ly_max = -h * 0.2, h          # descenders ≈ 20 % below baseline
        elif valign == 1:          # Bottom
            ly_min, ly_max = 0, h
        elif valign == 2:          # Middle
            ly_min, ly_max = -h/2, h/2
        elif valign == 3:          # Top
            ly_min, ly_max = -h, 0
        else:
            ly_min, ly_max = -h * 0.2, h

    # Apply padding
    lx_min -= pad;  lx_max += pad
    ly_min -= pad;  ly_max += pad

    # ── Rotate the four corners around the insert point ─────────────────
    theta = math.radians(rotation)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    ix, iy = entity["insert"]

    def rotate(lx, ly):
        rx = ix + lx * cos_t - ly * sin_t
        ry = iy + lx * sin_t + ly * cos_t
        return [round(rx, 4), round(ry, 4)]

    corners = [
        rotate(lx_min, ly_min),
        rotate(lx_max, ly_min),
        rotate(lx_max, ly_max),
        rotate(lx_min, ly_max),
    ]

    # Axis-aligned envelope of the rotated corners
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    ax_min, ax_max = min(xs), max(xs)
    ay_min, ay_max = min(ys), max(ys)

    return {
        "x":        round(ax_min, 4),
        "y":        round(ay_min, 4),
        "width":    round(ax_max - ax_min, 4),
        "height":   round(ay_max - ay_min, 4),
        "cx":       round((ax_min + ax_max) / 2, 4),
        "cy":       round((ay_min + ay_max) / 2, 4),
        "rotation": round(rotation, 4),
        "corners":  corners,
    }


# ──────────────────────────────────────────────
# 3.  SVG TEXT EXTRACTION  (optional)
# ──────────────────────────────────────────────

def extract_svg_text_bboxes(svg_path: str) -> list[dict]:
    """
    Parse SVG <text> elements.
    ezdxf SVGBackend produces real <text> nodes (unlike matplotlib).
    """
    try:
        from lxml import etree
    except ImportError:
        print("Warning: lxml not installed — SVG text matching skipped.", file=sys.stderr)
        return []

    NS = "http://www.w3.org/2000/svg"
    tree = etree.parse(svg_path)
    root = tree.getroot()

    results = []
    for idx, el in enumerate(root.iter(f"{{{NS}}}text")):
        content = "".join(el.itertext()).strip()
        if not content:
            continue
        x         = _float_attr(el, "x")
        y         = _float_attr(el, "y")
        font_size = _parse_font_size(el)
        transform = el.get("transform", "") or _inherit_transform(el)
        approx_w  = round(len(content) * font_size * 0.6, 2) if font_size else None
        approx_h  = round(font_size * 1.2, 2) if font_size else None
        results.append({
            "element_index": idx,
            "text":          content,
            "x": x, "y": y,
            "font_size":     font_size,
            "transform":     transform,
            "bbox": {"x": x, "y": y, "width": approx_w, "height": approx_h}
                    if (x is not None and y is not None) else None,
        })
    return results


def _float_attr(el, attr) -> float | None:
    v = el.get(attr)
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except ValueError:
        return None


def _parse_font_size(el) -> float | None:
    fs = el.get("font-size")
    if fs:
        try:
            return float(re.sub(r"[^\d.]", "", fs))
        except ValueError:
            pass
    m = re.search(r"font-size\s*:\s*([\d.]+)", el.get("style", ""))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _inherit_transform(el):
    parent = el.getparent()
    while parent is not None:
        t = parent.get("transform")
        if t:
            return t
        parent = parent.getparent()
    return ""


# ──────────────────────────────────────────────
# 4.  COORDINATE TRANSFORMS
#
#  Spaces:
#    DXF     — unitless CAD, Y-up, origin bottom-left
#    SVG     — mm (ezdxf viewBox), Y-down, origin top-left
#    PNG     — pixels, Y-down (only available after external PNG render)
#    Leaflet — CRS.Simple: lat=-png_y, lng=png_x
# ──────────────────────────────────────────────

class CoordTransform:
    """
    Coordinate transform chain built from transform.json.

    transform.json is written by render_svg.py.
    If png block is present, Leaflet coords are also available.
    """

    def __init__(self, t: dict):
        self.dxf      = t["dxf"]
        self.svg      = t["svg"]
        self.has_png  = "png" in t and t.get("scale_x") is not None
        self.scale_x  = t.get("scale_x")
        self.scale_y  = t.get("scale_y")
        self.png_w    = t["png"]["width_px"]  if self.has_png else None
        self.png_h    = t["png"]["height_px"] if self.has_png else None

    def dxf_to_svg(self, dxf_x: float, dxf_y: float) -> tuple[float, float]:
        """
        DXF coords → SVG viewBox coords (mm).
        Y is flipped: DXF Y-up → SVG Y-down.
        Also accounts for viewbox_x/y offset (ezdxf may not start at 0,0).
        """
        nx =  (dxf_x - self.dxf["x_min"]) / self.dxf["width"]
        ny = 1.0 - (dxf_y - self.dxf["y_min"]) / self.dxf["height"]
        vb_x = self.svg["viewbox_x"]
        vb_y = self.svg["viewbox_y"]
        sx = vb_x + nx * self.svg["viewbox_w"]
        sy = vb_y + ny * self.svg["viewbox_h"]
        return round(sx, 4), round(sy, 4)

    def dxf_to_png(self, dxf_x: float, dxf_y: float) -> tuple[float, float]:
        """DXF coords → PNG pixel coords. Requires PNG data in transform.json."""
        if not self.has_png:
            raise ValueError("PNG dimensions not in transform.json — add png block first")
        px =  (dxf_x - self.dxf["x_min"]) * self.scale_x
        py = self.png_h - (dxf_y - self.dxf["y_min"]) * self.scale_y
        return round(px, 4), round(py, 4)

    def dxf_to_leaflet(self, dxf_x: float, dxf_y: float) -> dict | None:
        """DXF coords → Leaflet CRS.Simple {lat, lng}. Requires PNG data."""
        if not self.has_png:
            return None
        px, py = self.dxf_to_png(dxf_x, dxf_y)
        return {"lat": round(-py, 4), "lng": round(px, 4)}

    # ── Bbox transforms ────────────────────────────────────────────────

    def dxf_bbox_to_svg(self, dxf_bbox: dict) -> dict:
        """
        Transform a DXF-space bbox (with corners) to SVG viewBox space.
        Returns the axis-aligned envelope of the projected corners.
        """
        svg_corners = [self.dxf_to_svg(c[0], c[1]) for c in dxf_bbox["corners"]]
        xs = [c[0] for c in svg_corners]
        ys = [c[1] for c in svg_corners]
        x0, y0 = min(xs), min(ys)
        return {
            "x":       round(x0, 4),
            "y":       round(y0, 4),
            "width":   round(max(xs) - x0, 4),
            "height":  round(max(ys) - y0, 4),
            "cx":      round((min(xs) + max(xs)) / 2, 4),
            "cy":      round((min(ys) + max(ys)) / 2, 4),
            "corners": [[round(c[0], 4), round(c[1], 4)] for c in svg_corners],
        }

    def dxf_bbox_to_png(self, dxf_bbox: dict) -> dict | None:
        """Transform bbox corners to PNG pixel space."""
        if not self.has_png:
            return None
        png_corners = [self.dxf_to_png(c[0], c[1]) for c in dxf_bbox["corners"]]
        xs = [c[0] for c in png_corners]
        ys = [c[1] for c in png_corners]
        x0, y0 = min(xs), min(ys)
        return {
            "x":       round(x0, 4),
            "y":       round(y0, 4),
            "width":   round(max(xs) - x0, 4),
            "height":  round(max(ys) - y0, 4),
            "cx":      round((min(xs) + max(xs)) / 2, 4),
            "cy":      round((min(ys) + max(ys)) / 2, 4),
            "corners": [[round(c[0], 4), round(c[1], 4)] for c in png_corners],
        }

    def dxf_bbox_to_leaflet(self, dxf_bbox: dict) -> dict | None:
        """Transform bbox corners to Leaflet {lat,lng} pairs."""
        if not self.has_png:
            return None
        leaflet_corners = []
        for c in dxf_bbox["corners"]:
            lf = self.dxf_to_leaflet(c[0], c[1])
            leaflet_corners.append(lf)
        lats = [c["lat"] for c in leaflet_corners]
        lngs = [c["lng"] for c in leaflet_corners]
        return {
            # Leaflet L.rectangle / L.polygon friendly
            "bounds": [
                [min(lats), min(lngs)],
                [max(lats), max(lngs)],
            ],
            "corners": leaflet_corners,
            "center": {
                "lat": round((min(lats) + max(lats)) / 2, 4),
                "lng": round((min(lngs) + max(lngs)) / 2, 4),
            },
        }

    def leaflet_bounds(self) -> list | None:
        if not self.has_png:
            return None
        return [[-self.png_h, 0], [0, self.png_w]]

    def to_dict(self) -> dict:
        d = {
            "dxf": self.dxf,
            "svg": self.svg,
        }
        if self.has_png:
            d["png"]            = {"width_px": self.png_w, "height_px": self.png_h}
            d["scale_x"]        = self.scale_x
            d["scale_y"]        = self.scale_y
            d["leaflet_bounds"] = self.leaflet_bounds()
        return d


# ──────────────────────────────────────────────
# 5.  MATCHING
# ──────────────────────────────────────────────

def build_dxf_index(entities: list[dict]) -> dict[str, list[dict]]:
    index = defaultdict(list)
    for e in entities:
        index[e["text"].strip()].append(e)
    return dict(index)


def build_svg_index(svg_entities: list[dict]) -> dict[str, list[dict]]:
    index = defaultdict(list)
    for e in svg_entities:
        index[e["text"].strip()].append(e)
    return dict(index)


def pick_best_dxf_match(matches, layer_priority) -> tuple[dict, bool]:
    is_dup = len(matches) > 1
    if not is_dup:
        return matches[0], False
    for layer in [l.upper() for l in layer_priority]:
        for m in matches:
            if m["layer"].upper() == layer:
                return m, True
    return matches[0], True


def match_labels(
    target_labels:  list[str],
    dxf_index:      dict,
    svg_index:      dict,
    layer_priority: list[str],
    transform:      CoordTransform | None,
) -> dict:
    labels = {}
    for label in target_labels:
        key         = label.strip()
        dxf_matches = dxf_index.get(key, [])
        svg_matches = svg_index.get(key, [])

        if not dxf_matches:
            # Case-insensitive fuzzy fallback
            ci_key     = key.upper()
            ci_matches = [
                e for k, elist in dxf_index.items()
                if k.upper() == ci_key for e in elist
            ]
            if ci_matches:
                best, is_dup = pick_best_dxf_match(ci_matches, layer_priority)
                labels[key]  = _build_entry(key, best, svg_matches, is_dup,
                                            ci_matches if is_dup else None,
                                            fuzzy_match=True, transform=transform)
            else:
                labels[key] = _not_found_entry(key)
        else:
            best, is_dup = pick_best_dxf_match(dxf_matches, layer_priority)
            labels[key]  = _build_entry(key, best, svg_matches, is_dup,
                                        dxf_matches if is_dup else None,
                                        fuzzy_match=False, transform=transform)
    return labels


def _not_found_entry(key: str) -> dict:
    return {
        "text": key, "found": False, "duplicate": False,
        "fuzzy_match": False, "dxf": None, "svg": None,
        "coords": None, "all_dxf_matches": [], "meta": {},
    }


def _build_entry(key, dxf_match, svg_matches, is_duplicate,
                 all_dxf, fuzzy_match, transform) -> dict:
    svg_primary = svg_matches[0] if svg_matches else None
    dxf_x, dxf_y = dxf_match["insert"]

    # ── Tight DXF-space hitbox ─────────────────────────────────────────
    dxf_bbox = compute_dxf_bbox(dxf_match)

    # ── Coordinate transforms ──────────────────────────────────────────
    coords = None
    if transform is not None:
        svg_xy  = transform.dxf_to_svg(dxf_x, dxf_y)
        leaflet = transform.dxf_to_leaflet(dxf_x, dxf_y)   # None if no PNG
        coords  = {
            "dxf":     {"x": dxf_x,    "y": dxf_y},
            "svg":     {"x": svg_xy[0], "y": svg_xy[1]},
            "leaflet": leaflet,
        }
        if transform.has_png:
            png_xy        = transform.dxf_to_png(dxf_x, dxf_y)
            coords["png"] = {"x": png_xy[0], "y": png_xy[1]}

        # ── Bbox in every coordinate space ────────────────────────────
        if dxf_bbox is not None:
            coords["bbox"] = {
                "dxf": dxf_bbox,
                "svg": transform.dxf_bbox_to_svg(dxf_bbox),
            }
            if transform.has_png:
                coords["bbox"]["png"]     = transform.dxf_bbox_to_png(dxf_bbox)
                coords["bbox"]["leaflet"] = transform.dxf_bbox_to_leaflet(dxf_bbox)
        else:
            coords["bbox"] = None

    entry = {
        "text":        key,
        "found":       True,
        "duplicate":   is_duplicate,
        "fuzzy_match": fuzzy_match,
        "dxf": {
            "handle":       dxf_match["handle"],
            "type":         dxf_match["type"],
            "insert":       dxf_match["insert"],
            "rotation":     dxf_match["rotation"],
            "height":       dxf_match["height"],
            "layer":        dxf_match["layer"],
            "style":        dxf_match["style"],
            "halign":       dxf_match["halign"],
            "valign":       dxf_match["valign"],
            "width_factor": dxf_match.get("width_factor", 1.0),
        },
        "svg": {
            "element_index": svg_primary["element_index"],
            "bbox":          svg_primary["bbox"],
            "transform":     svg_primary["transform"],
            "font_size":     svg_primary["font_size"],
        } if svg_primary else None,
        "coords": coords,
        "meta":   {},
    }

    entry["all_dxf_matches"] = [
        {"handle": m["handle"], "layer": m["layer"], "insert": m["insert"]}
        for m in all_dxf
    ] if (is_duplicate and all_dxf) else []

    return entry


# ──────────────────────────────────────────────
# 6.  HITBOXES  (flat Leaflet-ready list)
# ──────────────────────────────────────────────

def build_hitboxes(labels: dict) -> list[dict]:
    """Flat list consumed directly by the Leaflet viewer."""
    hitboxes = []
    for key, entry in labels.items():
        if not entry["found"] or entry["coords"] is None:
            continue
        leaflet = entry["coords"].get("leaflet")
        bbox    = entry["coords"].get("bbox")
        hitboxes.append({
            "label":   entry["text"],
            "found":   True,
            "dxf":     entry["coords"]["dxf"],
            "svg":     entry["coords"]["svg"],
            "leaflet": leaflet,   # None until PNG dims are added to transform.json
            "bbox":    bbox,      # {dxf, svg, png?, leaflet?} tight bounding boxes
            "meta": {
                "layer":       entry["dxf"]["layer"],
                "type":        entry["dxf"]["type"],
                "handle":      entry["dxf"]["handle"],
                "duplicate":   entry["duplicate"],
                "fuzzy_match": entry["fuzzy_match"],
            },
        })
    return hitboxes


# ──────────────────────────────────────────────
# 7.  DEBUG SVG
# ──────────────────────────────────────────────

def write_debug_svg(svg_path: str, labels: dict, output_path: str,
                    transform: CoordTransform) -> None:
    """
    Inject tight hitbox rectangles (and a centre dot) at each label's
    SVG viewBox coordinates.  Open in a browser — boxes should closely
    wrap the matching text entities.

    For rotated text the box is rendered as a <polygon> using the four
    projected corners so it stays aligned with the actual glyph run.
    Uses SVG coords so no PNG required.
    """
    import xml.etree.ElementTree as ET
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    tree = ET.parse(svg_path)
    root = tree.getroot()
    ns   = "http://www.w3.org/2000/svg"

    vb_w = transform.svg["viewbox_w"]
    vb_h = transform.svg["viewbox_h"]

    # Scale visual decorations to drawing size
    stroke_w   = round(vb_w * 0.0008, 3)   # very thin outline
    dot_r      = round(vb_w * 0.0015, 3)   # tiny centre dot
    colour     = "#00cc44"                  # green for all hitboxes

    # Semi-transparent overlay group so boxes don't obscure existing text
    grp = ET.SubElement(root, f"{{{ns}}}g")
    grp.set("id",      "debug-hitboxes")
    grp.set("opacity", "1")

    for key, entry in labels.items():
        if not entry["found"] or entry["coords"] is None:
            continue

        bbox_info = entry["coords"].get("bbox")

        # ── Draw hitbox ────────────────────────────────────────────────
        if bbox_info and bbox_info.get("svg") and bbox_info["svg"].get("corners"):
            svg_bb   = bbox_info["svg"]
            corners  = svg_bb["corners"]   # 4 × [x, y] in SVG space
            pts_str  = " ".join(f"{c[0]},{c[1]}" for c in corners)

            poly = ET.SubElement(grp, f"{{{ns}}}polygon")
            poly.set("points",        pts_str)
            poly.set("fill",          "none")
            poly.set("stroke",        colour)
            poly.set("stroke-width",  str(stroke_w))

            cx, cy = svg_bb["cx"], svg_bb["cy"]
        else:
            # No bbox available — fall back to a small square at the insert point
            sx = entry["coords"]["svg"]["x"]
            sy = entry["coords"]["svg"]["y"]
            cx, cy = sx, sy

            rect = ET.SubElement(grp, f"{{{ns}}}rect")
            rect.set("x",            str(round(sx - dot_r * 3, 4)))
            rect.set("y",            str(round(sy - dot_r * 3, 4)))
            rect.set("width",        str(round(dot_r * 6, 4)))
            rect.set("height",       str(round(dot_r * 6, 4)))
            rect.set("fill",         "none")
            rect.set("stroke",       colour)
            rect.set("stroke-width", str(stroke_w))

        dot = ET.SubElement(grp, f"{{{ns}}}circle")
        dot.set("cx",    str(cx))
        dot.set("cy",    str(cy))
        dot.set("r",     str(dot_r))
        dot.set("fill",  colour)

    tree.write(output_path, xml_declaration=True, encoding="unicode")
    print(f"Debug SVG written: {output_path}")
    print("  → Open in browser — green boxes should tightly wrap text entities.")


# ──────────────────────────────────────────────
# 8.  MANIFEST ASSEMBLY
# ──────────────────────────────────────────────

def build_manifest(
    dxf_path:       str,
    svg_path:       str | None,
    target_labels:  list[str],
    layer_priority: list[str],
    transform:      CoordTransform | None,
) -> dict:
    print(f"[1/4] Reading DXF: {dxf_path}")
    dxf_entities = extract_dxf_text_entities(dxf_path)
    print(f"      → {len(dxf_entities)} text entities found")

    svg_entities = []
    if svg_path:
        print(f"[2/4] Reading SVG: {svg_path}")
        svg_entities = extract_svg_text_bboxes(svg_path)
        print(f"      → {len(svg_entities)} <text> elements found")
        if not svg_entities:
            print("      (no <text> elements — SVG may use outlined paths, DXF coords used instead)")
    else:
        print("[2/4] No SVG provided — skipping SVG text extraction")

    print(f"[3/4] Matching {len(target_labels)} labels...")
    dxf_index = build_dxf_index(dxf_entities)
    svg_index = build_svg_index(svg_entities)
    labels    = match_labels(target_labels, dxf_index, svg_index,
                             layer_priority, transform)

    found      = sum(1 for v in labels.values() if v["found"])
    not_found  = sum(1 for v in labels.values() if not v["found"])
    duplicates = sum(1 for v in labels.values() if v["duplicate"])
    fuzzy      = sum(1 for v in labels.values() if v.get("fuzzy_match"))
    has_coords = sum(1 for v in labels.values() if v.get("coords") is not None)
    has_leaflet= sum(1 for v in labels.values()
                     if v.get("coords") and v["coords"].get("leaflet"))
    has_bbox   = sum(1 for v in labels.values()
                     if v.get("coords") and v["coords"].get("bbox"))

    hitboxes = build_hitboxes(labels)

    manifest = {
        "version":        "1.3",
        "source_dxf":     os.path.basename(dxf_path),
        "source_svg":     os.path.basename(svg_path) if svg_path else None,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "layer_priority": layer_priority,
        "transform":      transform.to_dict() if transform else None,
        "hitboxes":       hitboxes,
        "labels":         labels,
        "stats": {
            "total_searched":    len(target_labels),
            "found":             found,
            "not_found":         not_found,
            "duplicate_matches": duplicates,
            "fuzzy_matches":     fuzzy,
            "with_coords":       has_coords,
            "with_leaflet":      has_leaflet,
            "with_bbox":         has_bbox,
        },
    }

    print(f"[4/4] Done.")
    print(f"      found={found}  not_found={not_found}  duplicates={duplicates}"
          f"  fuzzy={fuzzy}  coords={has_coords}  leaflet={has_leaflet}  bbox={has_bbox}")

    if has_leaflet == 0 and transform and not transform.has_png:
        print()
        print("  ℹ  No Leaflet coords yet — add PNG dimensions to transform.json")
        print("     then re-run to get hitboxes.leaflet populated.")

    return manifest


# ──────────────────────────────────────────────
# 9.  CLI
# ──────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Generate label-manifest.json from DXF + labels list."
    )
    p.add_argument("--dxf",       required=True, help="Path to .dxf file")
    p.add_argument("--svg",       default=None,  help="Path to .svg file (from render_svg.py)")
    p.add_argument("--transform", default=None,  help="Path to transform.json (from render_svg.py)")
    p.add_argument("--out",       default="label-manifest.json")

    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--labels",        metavar="FILE",
                     help="Text file, one label per line")
    grp.add_argument("--labels-inline", nargs="+", metavar="LABEL")

    p.add_argument("--layer-priority", nargs="*",
                   default=["TAGS", "EQUIP", "ANNO", "TEXT"],
                   metavar="LAYER")
    p.add_argument("--debug-svg", default=None, metavar="PATH",
                   help="Write debug SVG with tight hitbox rectangles at label positions")
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args()


def load_labels_from_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def main():
    args = parse_args()

    target_labels = (load_labels_from_file(args.labels)
                     if args.labels else args.labels_inline)

    # Deduplicate, preserve order
    seen, unique = set(), []
    for l in target_labels:
        if l not in seen:
            seen.add(l)
            unique.append(l)
    if len(unique) < len(target_labels):
        print(f"Warning: removed {len(target_labels) - len(unique)} duplicate labels")

    # Load transform
    transform = None
    if args.transform:
        with open(args.transform, "r", encoding="utf-8") as f:
            transform = CoordTransform(json.load(f))
        print(f"Transform loaded: {args.transform}")
        if transform.has_png:
            print(f"  PNG: {transform.png_w}px × {transform.png_h}px  "
                  f"scale_x={transform.scale_x:.4f} scale_y={transform.scale_y:.4f}")
        else:
            print("  PNG dims not present — Leaflet coords will be null")
    else:
        print("No --transform provided — coords will be null")

    manifest = build_manifest(
        dxf_path=args.dxf,
        svg_path=args.svg,
        target_labels=unique,
        layer_priority=args.layer_priority,
        transform=transform,
    )

    # Write manifest
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\nManifest written : {out_path}")

    # Write hitboxes.json alongside
    hitboxes_path = out_path.parent / "hitboxes.json"
    with open(hitboxes_path, "w", encoding="utf-8") as f:
        json.dump(manifest["hitboxes"], f, indent=2, ensure_ascii=False)
    print(f"Hitboxes written : {hitboxes_path}")

    # Debug SVG
    if args.debug_svg:
        if not args.svg:
            print("Warning: --debug-svg requires --svg", file=sys.stderr)
        elif transform is None:
            print("Warning: --debug-svg requires --transform", file=sys.stderr)
        else:
            write_debug_svg(args.svg, manifest["labels"], args.debug_svg, transform)

    # Unmatched labels
    missing = [k for k, v in manifest["labels"].items() if not v["found"]]
    if missing:
        print(f"\n⚠  Unmatched labels ({len(missing)}):")
        for label in missing:
            print(f"   - {label}")


if __name__ == "__main__":
    main()
