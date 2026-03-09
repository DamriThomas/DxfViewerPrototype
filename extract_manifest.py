"""
extract_manifest.py
───────────────────
Generates label-manifest.json (and optionally a PNG) from a DXF file
and a target label list.

Usage:
    python extract_manifest.py \
        --dxf drawing.dxf \
        --labels labels.txt \
        --out manifest.json \
        --export-png diagram.png \
        --png-dpi 150 \
        --layer-priority TAGS EQUIP ANNO \
        --cluster-radius 50.0

The manifest embeds dxf_extents + png_size so the viewer can compute
the exact DXF-unit → pixel transform with no guesswork:

    scale_x = png_width_px  / (dxf_extents.max_x - dxf_extents.min_x)
    scale_y = png_height_px / (dxf_extents.max_y - dxf_extents.min_y)
    px      = (dxf_x - dxf_extents.min_x) * scale_x
    py      = png_height_px - (dxf_y - dxf_extents.min_y) * scale_y  # Y-flip

Requirements:
    pip install ezdxf
    pip install "ezdxf[draw]" matplotlib   # only needed for --export-png
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


# ──────────────────────────────────────────────────────────────────────────────
# 1.  DXF TEXT EXTRACTION
# ──────────────────────────────────────────────────────────────────────────────

def extract_dxf_text_entities(dxf_path: str) -> list[dict]:
    try:
        import ezdxf
    except ImportError:
        sys.exit("ezdxf not installed.  Run:  pip install ezdxf")

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    entities = []
    for entity in msp:
        etype = entity.dxftype()
        if etype == "TEXT":
            r = _parse_text(entity)
            if r: entities.append(r)
        elif etype == "MTEXT":
            r = _parse_mtext(entity)
            if r: entities.append(r)
    return entities


def _parse_text(e) -> dict | None:
    try:
        text = (e.dxf.text or "").strip()
        if not text: return None
        ins = e.dxf.insert
        return {
            "handle":   e.dxf.handle,
            "type":     "TEXT",
            "text":     text,
            "layer":    getattr(e.dxf, "layer",    "0")        or "0",
            "insert":   [round(ins.x, 4), round(ins.y, 4)],
            "rotation": round(getattr(e.dxf, "rotation", 0.0) or 0.0, 4),
            "height":   round(getattr(e.dxf, "height",   0.0) or 0.0, 4),
            "style":    getattr(e.dxf, "style",   "STANDARD") or "STANDARD",
            "halign":   getattr(e.dxf, "halign",  0),
            "valign":   getattr(e.dxf, "valign",  0),
        }
    except Exception:
        return None


def _parse_mtext(e) -> dict | None:
    try:
        text = e.plain_mtext().strip()
        if not text: return None
        ins = e.dxf.insert
        return {
            "handle":   e.dxf.handle,
            "type":     "MTEXT",
            "text":     text,
            "layer":    getattr(e.dxf, "layer", "0") or "0",
            "insert":   [round(ins.x, 4), round(ins.y, 4)],
            "rotation": round(math.degrees(getattr(e.dxf, "rotation", 0.0) or 0.0), 4),
            "height":   round(getattr(e.dxf, "char_height", 0.0) or 0.0, 4),
            "style":    None, "halign": None, "valign": None,
        }
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 2.  DXF EXTENTS
# ──────────────────────────────────────────────────────────────────────────────

def get_dxf_extents(dxf_path: str) -> dict | None:
    """
    Return model-space extents.  Tries header EXTMIN/EXTMAX first,
    then falls back to scanning entity inserts.
    """
    try:
        import ezdxf
        doc = ezdxf.readfile(dxf_path)

        extmin = doc.header.get("$EXTMIN")
        extmax = doc.header.get("$EXTMAX")
        sentinel = 1e20
        if (extmin and extmax
                and abs(extmin.x) < sentinel and abs(extmax.x) < sentinel
                and extmax.x > extmin.x and extmax.y > extmin.y):
            return {
                "min_x": round(extmin.x, 4), "min_y": round(extmin.y, 4),
                "max_x": round(extmax.x, 4), "max_y": round(extmax.y, 4),
                "source": "header",
            }

        # Fallback: scan inserts
        xs, ys = [], []
        for e in doc.modelspace():
            try:
                ins = e.dxf.insert
                xs.append(ins.x); ys.append(ins.y)
            except Exception:
                pass
        if xs:
            return {
                "min_x": round(min(xs), 4), "min_y": round(min(ys), 4),
                "max_x": round(max(xs), 4), "max_y": round(max(ys), 4),
                "source": "entity_scan",
            }
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# 3.  PROXIMITY CLUSTERING  (handles split labels like "FV" + "101")
# ──────────────────────────────────────────────────────────────────────────────

def build_proximity_clusters(entities: list[dict], radius: float) -> list[dict]:
    """
    Greedy single-linkage clustering of TEXT/MTEXT entities within `radius`
    DXF units of each other.  Returns only clusters with 2+ fragments.
    """
    remaining = list(entities)
    clusters  = []

    while remaining:
        seed   = remaining.pop(0)
        group  = [seed]
        changed = True
        while changed:
            changed = False
            next_remaining = []
            for e in remaining:
                if any(_dist(e, g) <= radius for g in group):
                    group.append(e)
                    changed = True
                else:
                    next_remaining.append(e)
            remaining = next_remaining

        if len(group) < 2:
            continue

        # Sort top-to-bottom then left-to-right
        group.sort(key=lambda e: (-e["insert"][1], e["insert"][0]))
        combined = " ".join(e["text"] for e in group)
        cx = sum(e["insert"][0] for e in group) / len(group)
        cy = sum(e["insert"][1] for e in group) / len(group)

        clusters.append({
            "combined_text": combined,
            "combined_nospace": re.sub(r"\s+", "", combined).upper(),
            "centroid":  [round(cx, 4), round(cy, 4)],
            "layer":     group[0]["layer"],
            "fragments": group,
        })

    return clusters


def _dist(a: dict, b: dict) -> float:
    ax, ay = a["insert"]; bx, by = b["insert"]
    return math.hypot(ax - bx, ay - by)


def find_cluster_match(label: str, clusters: list[dict]) -> dict | None:
    norm = re.sub(r"\s+", "", label).upper()
    best = None
    for c in clusters:
        if norm in c["combined_nospace"]:
            if best is None or len(c["combined_nospace"]) < len(best["combined_nospace"]):
                best = c
    return best


# ──────────────────────────────────────────────────────────────────────────────
# 4.  MATCHING
# ──────────────────────────────────────────────────────────────────────────────

def build_dxf_index(entities: list[dict]) -> dict[str, list[dict]]:
    idx = defaultdict(list)
    for e in entities:
        idx[e["text"].strip()].append(e)
    return dict(idx)


def pick_best(matches: list[dict], layer_priority: list[str]) -> tuple[dict, bool]:
    if len(matches) == 1:
        return matches[0], False
    priority_upper = [l.upper() for l in layer_priority]
    for layer in priority_upper:
        for m in matches:
            if m["layer"].upper() == layer:
                return m, True
    return matches[0], True


def match_labels(
    target_labels:  list[str],
    dxf_index:      dict,
    layer_priority: list[str],
    clusters:       list[dict],
) -> tuple[dict, dict]:
    confirmed  = {}
    potentials = {}

    for label in target_labels:
        key = label.strip()

        # 1. Exact
        matches = dxf_index.get(key, [])
        if matches:
            best, is_dup = pick_best(matches, layer_priority)
            confirmed[key] = _build_entry(key, best, is_dup, matches, "exact")
            continue

        # 2. Case-insensitive
        ci = key.upper()
        ci_matches = [e for k, v in dxf_index.items() if k.upper() == ci for e in v]
        if ci_matches:
            best, is_dup = pick_best(ci_matches, layer_priority)
            confirmed[key] = _build_entry(key, best, is_dup, ci_matches, "fuzzy")
            continue

        # 3. Proximity cluster
        cluster = find_cluster_match(key, clusters)
        if cluster:
            potentials[key] = _build_cluster_entry(key, cluster)
            continue

        # 4. Not found
        confirmed[key] = {
            "text": key, "found": False, "duplicate": False,
            "match_type": "none", "dxf": None, "all_dxf_matches": [],
        }

    return confirmed, potentials


def _build_entry(key, match, is_dup, all_matches, match_type) -> dict:
    entry = {
        "text":       key,
        "found":      True,
        "duplicate":  is_dup,
        "match_type": match_type,
        "dxf": {
            "handle":   match["handle"],
            "type":     match["type"],
            "insert":   match["insert"],
            "rotation": match["rotation"],
            "height":   match["height"],
            "layer":    match["layer"],
            "style":    match["style"],
            "halign":   match["halign"],
            "valign":   match["valign"],
        },
        "all_dxf_matches": [
            {"handle": m["handle"], "layer": m["layer"], "insert": m["insert"]}
            for m in all_matches
        ] if is_dup else [],
    }
    return entry


def _build_cluster_entry(key, cluster) -> dict:
    frag = cluster["fragments"][0]
    return {
        "text":         key,
        "found":        True,
        "duplicate":    False,
        "match_type":   "proximity_cluster",
        "needs_review": True,
        "cluster": {
            "combined_text":  cluster["combined_text"],
            "centroid":       cluster["centroid"],
            "layer":          cluster["layer"],
            "fragment_count": len(cluster["fragments"]),
            "fragments": [
                {"handle": f["handle"], "text": f["text"],
                 "insert": f["insert"], "layer": f["layer"]}
                for f in cluster["fragments"]
            ],
        },
        "dxf": {
            "handle":   frag["handle"],
            "type":     frag["type"],
            "insert":   cluster["centroid"],
            "rotation": frag["rotation"],
            "height":   frag["height"],
            "layer":    cluster["layer"],
            "style":    frag["style"],
            "halign":   frag["halign"],
            "valign":   frag["valign"],
        },
        "all_dxf_matches": [],
    }


# ──────────────────────────────────────────────────────────────────────────────
# 5.  PNG EXPORT
# ──────────────────────────────────────────────────────────────────────────────

def export_png(dxf_path: str, out_png: str, dpi: int = 150) -> dict | None:
    """
    Render the DXF modelspace to a PNG using ezdxf's Matplotlib backend.

    Returns a dict with the PNG pixel dimensions and the DXF extents used
    for the render, so the viewer can compute an exact pixel transform:

        px = (dxf_x - render_extents.min_x) * scale_x
        py = png_h  - (dxf_y - render_extents.min_y) * scale_y   # Y-flip

    where scale_x = png_w / (max_x - min_x),  scale_y = png_h / (max_y - min_y)
    """
    try:
        import ezdxf
        from ezdxf.addons.drawing import RenderContext, Frontend
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
        import matplotlib.pyplot as plt
    except ImportError:
        print("⚠  PNG export requires ezdxf[draw] + matplotlib.")
        print("   Run:  pip install 'ezdxf[draw]' matplotlib")
        return None

    print(f"[PNG] Rendering {dxf_path} at {dpi} DPI …")

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    # Get extents for the render (same logic as get_dxf_extents)
    extents = get_dxf_extents(dxf_path)
    if not extents:
        print("⚠  Could not determine DXF extents — PNG output may be cropped.")

    fig = plt.figure()
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_aspect("equal")
    ax.axis("off")

    ctx      = RenderContext(doc)
    backend  = MatplotlibBackend(ax)
    frontend = Frontend(ctx, backend)
    frontend.draw_layout(msp)

    # Fit the axes tightly to the content
    ax.autoscale()
    fig.set_facecolor("white")

    out_path = Path(out_png)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi, bbox_inches="tight",
                pad_inches=0, facecolor="white")
    plt.close(fig)

    # Read back actual pixel dimensions
    try:
        from PIL import Image
        with Image.open(str(out_path)) as im:
            png_w, png_h = im.size
    except ImportError:
        # Fall back to matplotlib figure size × dpi
        w_in, h_in = fig.get_size_inches()
        png_w = round(w_in * dpi)
        png_h = round(h_in * dpi)

    size_kb = out_path.stat().st_size / 1024
    print(f"[PNG] Written: {out_path}  ({png_w}×{png_h}px, {size_kb:.0f} KB)")

    return {
        "path":    str(out_path),
        "width":   png_w,
        "height":  png_h,
        "dpi":     dpi,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 6.  MANIFEST ASSEMBLY
# ──────────────────────────────────────────────────────────────────────────────

def build_manifest(
    dxf_path:       str,
    target_labels:  list[str],
    layer_priority: list[str],
    cluster_radius: float,
    png_info:       dict | None = None,
) -> dict:
    print(f"[1/4] Reading DXF: {dxf_path}")
    dxf_entities = extract_dxf_text_entities(dxf_path)
    dxf_extents  = get_dxf_extents(dxf_path)
    print(f"      → {len(dxf_entities)} text entities found")
    if dxf_extents:
        print(f"      → extents X {dxf_extents['min_x']}..{dxf_extents['max_x']}  "
              f"Y {dxf_extents['min_y']}..{dxf_extents['max_y']}  ({dxf_extents['source']})")

    print(f"[2/4] Building proximity clusters (radius={cluster_radius})…")
    clusters = build_proximity_clusters(dxf_entities, cluster_radius)
    print(f"      → {len(clusters)} multi-fragment clusters")

    print(f"[3/4] Matching {len(target_labels)} labels…")
    dxf_index = build_dxf_index(dxf_entities)
    confirmed, potentials = match_labels(target_labels, dxf_index, layer_priority, clusters)

    exact   = sum(1 for v in confirmed.values() if v["found"] and v["match_type"] == "exact")
    fuzzy   = sum(1 for v in confirmed.values() if v["found"] and v["match_type"] == "fuzzy")
    missing = sum(1 for v in confirmed.values() if not v["found"])
    dups    = sum(1 for v in confirmed.values() if v.get("duplicate"))

    manifest = {
        "version":        "2.0",
        "source_dxf":     os.path.basename(dxf_path),
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "layer_priority": layer_priority,
        "cluster_radius": cluster_radius,
        # Coordinate reference — viewer uses these to place hitboxes on the PNG
        "dxf_extents":    dxf_extents,
        "png":            png_info,   # { width, height, dpi } or None
        "labels":         confirmed,
        "potential_matches": potentials,
        "stats": {
            "total_searched":            len(target_labels),
            "exact_matches":             exact,
            "fuzzy_matches":             fuzzy,
            "proximity_cluster_matches": len(potentials),
            "not_found":                 missing,
            "duplicate_matches":         dups,
        },
    }

    print(f"[4/4] Done — exact={exact}  fuzzy={fuzzy}  "
          f"cluster={len(potentials)}  not_found={missing}  dups={dups}")
    return manifest


# ──────────────────────────────────────────────────────────────────────────────
# 7.  CLI
# ──────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Generate label-manifest.json (+ optional PNG) from a DXF file"
    )
    p.add_argument("--dxf",  required=True, help="Path to .dxf file")
    p.add_argument("--out",  default="label-manifest.json")

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--labels",        metavar="FILE",  help="Text file, one label per line")
    g.add_argument("--labels-inline", nargs="+", metavar="LABEL")

    p.add_argument("--layer-priority", nargs="*",
                   default=["TAGS", "EQUIP", "ANNO", "TEXT"], metavar="LAYER")
    p.add_argument("--cluster-radius", type=float, default=50.0, metavar="UNITS")

    p.add_argument("--export-png", metavar="PATH", default=None,
                   help="Render DXF to PNG and save here. Requires ezdxf[draw] + matplotlib.")
    p.add_argument("--png-dpi", type=int, default=150,
                   help="DPI for PNG render (default 150)")
    return p.parse_args()


def load_labels(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def dedup(labels: list[str]) -> list[str]:
    seen, out = set(), []
    for l in labels:
        if l not in seen:
            seen.add(l); out.append(l)
    return out


def main():
    args = parse_args()

    labels = dedup(load_labels(args.labels) if args.labels else args.labels_inline)

    # Optional PNG render — do this before manifest so png_info is available
    png_info = None
    if args.export_png:
        png_info = export_png(args.dxf, args.export_png, dpi=args.png_dpi)

    manifest = build_manifest(
        dxf_path=args.dxf,
        target_labels=labels,
        layer_priority=args.layer_priority,
        cluster_radius=args.cluster_radius,
        png_info=png_info,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\nManifest written → {out_path}")

    not_found = [k for k, v in manifest["labels"].items() if not v["found"]]
    if not_found:
        print(f"\n⚠  Unmatched ({len(not_found)}): {', '.join(not_found)}")

    if manifest["potential_matches"]:
        print(f"\n🔍  Needs review ({len(manifest['potential_matches'])}):")
        for label, e in manifest["potential_matches"].items():
            frags = " + ".join(f'"{f["text"]}"' for f in e["cluster"]["fragments"])
            print(f"   {label}  ←  {frags}")


if __name__ == "__main__":
    main()
