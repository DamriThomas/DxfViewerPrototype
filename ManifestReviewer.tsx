import { useState, useRef, useCallback, useMemo, useEffect } from "react";
import {
  TransformWrapper,
  TransformComponent,
  useTransformContext,
} from "react-zoom-pan-pinch";

// ─── Constants ────────────────────────────────────────────────────────────────
const HIT_W = 90;
const HIT_H = 22;
const DEFAULT_DIMS = { w: 1000, h: 1000 };

// ─── Parse viewBox/width/height from SVG text ─────────────────────────────────
function parseSvgDims(svgText) {
  const vbMatch = svgText.match(/viewBox=["']([^"']+)["']/);
  if (vbMatch) {
    const parts = vbMatch[1]
      .trim()
      .split(/[\s,]+/)
      .map(Number);
    if (parts.length === 4 && parts.every((n) => !isNaN(n))) {
      return { w: parts[2], h: parts[3], minX: parts[0], minY: parts[1] };
    }
  }
  const wMatch = svgText.match(/\bwidth=["']([\d.]+)/);
  const hMatch = svgText.match(/\bheight=["']([\d.]+)/);
  if (wMatch && hMatch) {
    return {
      w: parseFloat(wMatch[1]),
      h: parseFloat(hMatch[1]),
      minX: 0,
      minY: 0,
    };
  }
  return { ...DEFAULT_DIMS, minX: 0, minY: 0 };
}

// ─── Extract actual drawn content bounds from SVG path data ──────────────────
// CAD exporters often use an internal unit scale (e.g. 1 DXF unit = 1000 SVG
// units). We scan all M/L/C path coordinates to find where the content actually
// sits, independent of the viewBox declaration.
function parseSvgContentBounds(svgText) {
  const nums = [];
  // Match every M or L command: "M x y" or "L x y" (space or comma separated)
  const re = /[ML]\s*([-\d.]+)[\s,]([-\d.]+)/g;
  let m;
  while ((m = re.exec(svgText)) !== null) {
    nums.push([parseFloat(m[1]), parseFloat(m[2])]);
  }
  if (nums.length < 2) return null;
  const xs = nums.map((n) => n[0]);
  const ys = nums.map((n) => n[1]);
  return {
    minX: Math.min(...xs),
    maxX: Math.max(...xs),
    minY: Math.min(...ys),
    maxY: Math.max(...ys),
  };
}

// ─── Compute affine transform: DXF model-space → SVG canvas coords ──────────
//
// Priority of coordinate sources (best → worst):
//
//  1. dxfExtents from manifest (header EXTMIN/EXTMAX) — exact, authoritative
//  2. SVG path content bounds — works when SVG has geometry; handles any scale
//  3. DXF label position bounds fitted to SVG viewBox — last resort
//
// The transform maps:  svgCoord = dxfCoord * scale + translate
// DXF Y-up → SVG Y-down, so scaleY is always negative.
function computeTransform(positions, svgDims, svgContentBounds, dxfExtents) {
  // ── Source 1: manifest dxf_extents ──────────────────────────────────────
  if (dxfExtents && svgContentBounds) {
    const dxfW = dxfExtents.max_x - dxfExtents.min_x || 1;
    const dxfH = dxfExtents.max_y - dxfExtents.min_y || 1;
    const svgW = svgContentBounds.maxX - svgContentBounds.minX || 1;
    const svgH = svgContentBounds.maxY - svgContentBounds.minY || 1;

    const scaleX = svgW / dxfW;
    const scaleY = -svgH / dxfH; // Y flip

    // dxfExtents.min_x → svgContentBounds.minX
    // dxfExtents.max_y → svgContentBounds.minY  (top in DXF = top in SVG)
    const translateX = svgContentBounds.minX - dxfExtents.min_x * scaleX;
    const translateY = svgContentBounds.minY - dxfExtents.max_y * scaleY;

    return { scaleX, scaleY, translateX, translateY };
  }

  // ── Source 2: SVG content bounds only (no dxfExtents) ───────────────────
  // Fit DXF label positions onto SVG content area
  const pts = Object.values(positions);
  if (pts.length < 1) {
    return { scaleX: 1, scaleY: -1, translateX: 0, translateY: svgDims.h };
  }
  const dxfXs = pts.map((p) => p.x);
  const dxfYs = pts.map((p) => p.y);
  const dxfMinX = Math.min(...dxfXs),
    dxfMaxX = Math.max(...dxfXs);
  const dxfMinY = Math.min(...dxfYs),
    dxfMaxY = Math.max(...dxfYs);
  const dxfW = dxfMaxX - dxfMinX || 1;
  const dxfH = dxfMaxY - dxfMinY || 1;

  if (svgContentBounds) {
    const svgW = svgContentBounds.maxX - svgContentBounds.minX || 1;
    const svgH = svgContentBounds.maxY - svgContentBounds.minY || 1;
    const scaleX = svgW / dxfW;
    const scaleY = -svgH / dxfH;
    const translateX = svgContentBounds.minX - dxfMinX * scaleX;
    const translateY = svgContentBounds.minY - dxfMaxY * scaleY;
    return { scaleX, scaleY, translateX, translateY };
  }

  // ── Source 3: fit label positions into SVG viewBox ───────────────────────
  const margin = svgDims.w * 0.02;
  const scaleX = (svgDims.w - margin * 2) / dxfW;
  const scaleY = -(svgDims.h - margin * 2) / dxfH;
  const translateX = margin - dxfMinX * scaleX;
  const translateY = margin - dxfMaxY * scaleY;
  return { scaleX, scaleY, translateX, translateY };
}

function applyTransform(pos, t) {
  return {
    x: pos.x * t.scaleX + t.translateX,
    y: pos.y * t.scaleY + t.translateY,
  };
}

// ─── Match type config ────────────────────────────────────────────────────────
const MATCH_TYPE = {
  exact: {
    label: "Exact",
    color: "#4ade80",
    dim: "rgba(74,222,128,0.15)",
    border: "rgba(74,222,128,0.5)",
  },
  fuzzy: {
    label: "Fuzzy",
    color: "#facc15",
    dim: "rgba(250,204,21,0.15)",
    border: "rgba(250,204,21,0.5)",
  },
  proximity_cluster: {
    label: "Proximity",
    color: "#fb923c",
    dim: "rgba(251,146,60,0.15)",
    border: "rgba(251,146,60,0.5)",
  },
  none: {
    label: "No match",
    color: "#f87171",
    dim: "rgba(248,113,113,0.15)",
    border: "rgba(248,113,113,0.5)",
  },
};

// ─── Parse manifest JSON ──────────────────────────────────────────────────────
function parseManifest(json) {
  const confirmed = json.labels || {};
  const potentials = json.potential_matches || {};

  const entries = [];

  Object.entries(confirmed).forEach(([key, v]) => {
    entries.push({
      id: key,
      text: v.text || key,
      matchType: v.found ? v.match_type || "exact" : "none",
      found: v.found,
      duplicate: v.duplicate || false,
      dxf: v.dxf || null,
      svg: v.svg || null,
      cluster: null,
      allMatches: v.all_dxf_matches || [],
      verified: false,
      rejected: false,
      note: "",
    });
  });

  Object.entries(potentials).forEach(([key, v]) => {
    entries.push({
      id: key,
      text: v.text || key,
      matchType: "proximity_cluster",
      found: true,
      duplicate: false,
      dxf: v.dxf || null,
      svg: v.svg || null,
      cluster: v.cluster || null,
      allMatches: [],
      verified: false,
      rejected: false,
      note: "",
    });
  });

  return entries;
}

// ─── Derive screen positions from manifest entries ────────────────────────────
function buildPositions(entries) {
  const pos = {};
  entries.forEach((e) => {
    if (e.dxf?.insert) {
      pos[e.id] = { x: e.dxf.insert[0], y: e.dxf.insert[1] };
    } else if (e.svg?.bbox) {
      pos[e.id] = { x: e.svg.bbox.x, y: e.svg.bbox.y };
    }
  });
  return pos;
}

// ─── Zoom Controls ────────────────────────────────────────────────────────────
function ZoomControls() {
  const { zoomIn, zoomOut, resetTransform } = useTransformContext();
  return (
    <div
      style={{
        position: "absolute",
        bottom: 16,
        right: 16,
        display: "flex",
        flexDirection: "column",
        gap: 3,
        zIndex: 10,
      }}
    >
      {[
        { l: "+", f: () => zoomIn(0.5, 200) },
        { l: "−", f: () => zoomOut(0.5, 200) },
        { l: "⊡", f: () => resetTransform() },
      ].map((b) => (
        <button
          key={b.l}
          onClick={b.f}
          style={{
            width: 32,
            height: 32,
            background: "#111318dd",
            backdropFilter: "blur(8px)",
            border: "1px solid #2a2d3a",
            borderRadius: 6,
            color: "#c8cfe8",
            fontSize: 16,
            cursor: "pointer",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {b.l}
        </button>
      ))}
    </div>
  );
}

// ─── Hit Box Overlay ──────────────────────────────────────────────────────────
function HitOverlay({
  entries,
  positions,
  selected,
  showBoxes,
  onSelect,
  svgDims,
  transform,
}) {
  const { w, h } = svgDims;
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        width: w,
        height: h,
        pointerEvents: "none",
      }}
    >
      {entries.map((entry) => {
        const pos = positions[entry.id];
        if (!pos) return null;
        const isSel = selected?.id === entry.id;
        const mt = MATCH_TYPE[entry.matchType] || MATCH_TYPE.none;
        const show = showBoxes || isSel;
        const svgPos = applyTransform(pos, transform);

        return (
          <div
            key={entry.id}
            id={`hit-${entry.id}`}
            onClick={() => onSelect(entry)}
            style={{
              position: "absolute",
              left: svgPos.x,
              top: svgPos.y - HIT_H,
              width: HIT_W,
              height: HIT_H,
              cursor: "pointer",
              pointerEvents: "all",
              borderRadius: 3,
              background: show ? mt.dim : "transparent",
              border: show
                ? `1.5px solid ${isSel ? mt.color : mt.border}`
                : `1.5px solid ${isSel ? mt.color : "transparent"}`,
              boxShadow: isSel
                ? `0 0 0 2px ${mt.color}66, 0 0 12px ${mt.color}44`
                : "none",
              transition:
                "background 0.12s, border-color 0.12s, box-shadow 0.12s",
            }}
          >
            {entry.verified && (
              <div
                style={{
                  position: "absolute",
                  top: -5,
                  right: -5,
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: "#4ade80",
                  border: "1.5px solid #111318",
                  fontSize: 6,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#111",
                  fontWeight: 700,
                }}
              >
                ✓
              </div>
            )}
            {entry.rejected && (
              <div
                style={{
                  position: "absolute",
                  top: -5,
                  right: -5,
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: "#f87171",
                  border: "1.5px solid #111318",
                  fontSize: 6,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#111",
                  fontWeight: 700,
                }}
              >
                ✕
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Left Panel: Entry List ───────────────────────────────────────────────────
function EntryList({ entries, selected, filter, onSelect }) {
  const outerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(500);
  const ITEM_H = 36;

  useEffect(() => {
    const el = outerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!selected || !outerRef.current) return;
    const idx = entries.findIndex((e) => e.id === selected.id);
    if (idx < 0) return;
    const top = idx * ITEM_H;
    const vis = outerRef.current.scrollTop;
    if (top < vis || top + ITEM_H > vis + height)
      outerRef.current.scrollTop = top - height / 2 + ITEM_H / 2;
  }, [selected, entries, height]);

  const overscan = 6;
  const startIdx = Math.max(0, Math.floor(scrollTop / ITEM_H) - overscan);
  const endIdx = Math.min(
    entries.length,
    startIdx + Math.ceil(height / ITEM_H) + overscan * 2,
  );

  return (
    <div
      ref={outerRef}
      onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
      style={{ height: "100%", overflowY: "auto" }}
    >
      <div style={{ height: entries.length * ITEM_H, position: "relative" }}>
        {entries.slice(startIdx, endIdx).map((entry, i) => {
          const idx = startIdx + i;
          const isSel = selected?.id === entry.id;
          const mt = MATCH_TYPE[entry.matchType] || MATCH_TYPE.none;
          return (
            <div
              key={entry.id}
              style={{
                position: "absolute",
                top: idx * ITEM_H,
                width: "100%",
                height: ITEM_H,
              }}
            >
              <button
                onClick={() => onSelect(entry)}
                style={{
                  width: "100%",
                  height: "100%",
                  background: isSel ? `${mt.color}11` : "transparent",
                  border: "none",
                  borderLeft: `2.5px solid ${isSel ? mt.color : "transparent"}`,
                  padding: "0 12px 0 14px",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  cursor: "pointer",
                  transition: "all 0.1s",
                }}
              >
                {/* Type dot */}
                <div
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: mt.color,
                    boxShadow: `0 0 5px ${mt.color}88`,
                    flexShrink: 0,
                    opacity: entry.verified ? 1 : entry.rejected ? 0.3 : 0.75,
                  }}
                />
                <span
                  style={{
                    fontFamily: "'IBM Plex Mono', monospace",
                    fontSize: 11,
                    color: isSel ? "#e8ecf4" : "#9aa0b8",
                    flex: 1,
                    textAlign: "left",
                    textDecoration: entry.rejected ? "line-through" : "none",
                    opacity: entry.rejected ? 0.4 : 1,
                  }}
                >
                  {entry.text}
                </span>
                {/* State icons */}
                <span style={{ fontSize: 10, flexShrink: 0 }}>
                  {entry.verified && (
                    <span style={{ color: "#4ade80" }}>✓</span>
                  )}
                  {entry.rejected && (
                    <span style={{ color: "#f87171" }}>✕</span>
                  )}
                  {!entry.verified &&
                    !entry.rejected &&
                    entry.matchType === "proximity_cluster" && (
                      <span style={{ color: "#fb923c", fontSize: 9 }}>?</span>
                    )}
                  {entry.duplicate && (
                    <span
                      style={{ color: "#a78bfa", fontSize: 9, marginLeft: 2 }}
                    >
                      2×
                    </span>
                  )}
                </span>
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Right Panel: Detail + Verify ────────────────────────────────────────────
function DetailPanel({
  entry,
  onVerify,
  onReject,
  onUndo,
  onNoteChange,
  onClose,
}) {
  const mt = MATCH_TYPE[entry.matchType] || MATCH_TYPE.none;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        fontFamily: "'IBM Plex Mono', monospace",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "14px 16px 12px",
          borderBottom: "1px solid #1e2030",
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: 8,
          }}
        >
          <div>
            <div
              style={{
                fontSize: 17,
                fontWeight: 600,
                color: "#e8ecf4",
                letterSpacing: "-0.02em",
                marginBottom: 4,
              }}
            >
              {entry.text}
            </div>
            {/* Match type badge */}
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 5,
                background: mt.dim,
                border: `1px solid ${mt.border}`,
                borderRadius: 4,
                padding: "2px 8px",
                fontSize: 10,
                color: mt.color,
                fontWeight: 600,
              }}
            >
              <span
                style={{
                  width: 5,
                  height: 5,
                  borderRadius: "50%",
                  background: mt.color,
                }}
              />
              {mt.label} match
            </span>
            {entry.duplicate && (
              <span
                style={{
                  marginLeft: 6,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 4,
                  background: "rgba(167,139,250,0.12)",
                  border: "1px solid rgba(167,139,250,0.3)",
                  borderRadius: 4,
                  padding: "2px 8px",
                  fontSize: 10,
                  color: "#a78bfa",
                  fontWeight: 600,
                }}
              >
                Duplicate
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              color: "#4a5270",
              fontSize: 18,
              lineHeight: 1,
              padding: "0 0 0 8px",
            }}
          >
            ×
          </button>
        </div>
      </div>

      {/* Body */}
      <div style={{ flex: 1, overflowY: "auto", padding: "14px 16px" }}>
        {/* DXF Position */}
        {entry.dxf && (
          <Section title="DXF Position">
            <Field
              label="Insert"
              value={`${entry.dxf.insert?.[0]?.toFixed(2)}, ${entry.dxf.insert?.[1]?.toFixed(2)}`}
            />
            <Field label="Layer" value={entry.dxf.layer} />
            <Field label="Handle" value={entry.dxf.handle} />
            <Field label="Type" value={entry.dxf.type} />
            <Field label="Height" value={entry.dxf.height} />
          </Section>
        )}

        {/* Cluster fragments */}
        {entry.cluster && (
          <Section
            title={`Cluster Fragments (${entry.cluster.fragment_count})`}
          >
            <div style={{ marginBottom: 8 }}>
              <Field
                label="Combined text"
                value={entry.cluster.combined_text}
              />
              <Field
                label="Centroid"
                value={`${entry.cluster.centroid?.[0]?.toFixed(2)}, ${entry.cluster.centroid?.[1]?.toFixed(2)}`}
              />
            </div>
            {entry.cluster.fragments?.map((f, i) => (
              <div
                key={f.handle}
                style={{
                  background: "#1a1d28",
                  border: "1px solid #1e2030",
                  borderRadius: 5,
                  padding: "8px 10px",
                  marginBottom: 5,
                }}
              >
                <div
                  style={{ fontSize: 10, color: "#6b7280", marginBottom: 3 }}
                >
                  Fragment {i + 1}
                </div>
                <div
                  style={{ fontSize: 12, color: "#c8cfe8", fontWeight: 600 }}
                >
                  "{f.text}"
                </div>
                <div style={{ fontSize: 10, color: "#4a5270", marginTop: 2 }}>
                  {f.insert?.[0]?.toFixed(1)}, {f.insert?.[1]?.toFixed(1)} ·{" "}
                  {f.layer}
                </div>
              </div>
            ))}
            <div
              style={{
                marginTop: 6,
                padding: "8px 10px",
                background: "rgba(251,146,60,0.06)",
                border: "1px solid rgba(251,146,60,0.2)",
                borderRadius: 5,
                fontSize: 10,
                color: "#fb923c",
                lineHeight: 1.5,
              }}
            >
              ⚠ Proximity match — verify that these fragments form the correct
              label before approving.
            </div>
          </Section>
        )}

        {/* Duplicate matches */}
        {entry.allMatches?.length > 0 && (
          <Section title={`All DXF Matches (${entry.allMatches.length})`}>
            {entry.allMatches.map((m, i) => (
              <div
                key={m.handle}
                style={{
                  background: "#1a1d28",
                  border: "1px solid #1e2030",
                  borderRadius: 5,
                  padding: "8px 10px",
                  marginBottom: 5,
                }}
              >
                <div
                  style={{ fontSize: 10, color: "#a78bfa", marginBottom: 2 }}
                >
                  Match {i + 1}
                </div>
                <Field label="Layer" value={m.layer} compact />
                <Field
                  label="Insert"
                  value={`${m.insert?.[0]?.toFixed(2)}, ${m.insert?.[1]?.toFixed(2)}`}
                  compact
                />
                <Field label="Handle" value={m.handle} compact />
              </div>
            ))}
          </Section>
        )}

        {/* Note */}
        <Section title="Review Note">
          <textarea
            value={entry.note}
            onChange={(e) => onNoteChange(entry.id, e.target.value)}
            placeholder="Add a note for this match..."
            rows={3}
            style={{
              width: "100%",
              boxSizing: "border-box",
              background: "#1a1d28",
              border: "1px solid #1e2030",
              borderRadius: 5,
              padding: "8px 10px",
              color: "#c8cfe8",
              fontSize: 11,
              fontFamily: "inherit",
              resize: "vertical",
              outline: "none",
              lineHeight: 1.5,
            }}
          />
        </Section>
      </div>

      {/* Action bar */}
      <div
        style={{
          padding: "12px 16px",
          borderTop: "1px solid #1e2030",
          flexShrink: 0,
          display: "flex",
          flexDirection: "column",
          gap: 7,
        }}
      >
        {/* Status display */}
        {(entry.verified || entry.rejected) && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              background: entry.verified
                ? "rgba(74,222,128,0.07)"
                : "rgba(248,113,113,0.07)",
              border: `1px solid ${entry.verified ? "rgba(74,222,128,0.2)" : "rgba(248,113,113,0.2)"}`,
              borderRadius: 5,
              padding: "6px 10px",
            }}
          >
            <span
              style={{
                fontSize: 11,
                color: entry.verified ? "#4ade80" : "#f87171",
                fontWeight: 600,
              }}
            >
              {entry.verified ? "✓  Verified" : "✕  Rejected"}
            </span>
            <button
              onClick={onUndo}
              style={{
                background: "none",
                border: "1px solid #2a2d3a",
                borderRadius: 4,
                padding: "2px 8px",
                fontSize: 10,
                color: "#6b7280",
                cursor: "pointer",
                fontFamily: "inherit",
              }}
            >
              Undo
            </button>
          </div>
        )}

        {!entry.verified && !entry.rejected && (
          <div style={{ display: "flex", gap: 7 }}>
            <button
              onClick={() => onVerify(entry.id)}
              style={{
                flex: 1,
                padding: "8px 0",
                background: "rgba(74,222,128,0.12)",
                border: "1px solid rgba(74,222,128,0.35)",
                borderRadius: 6,
                color: "#4ade80",
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                fontFamily: "inherit",
                transition: "all 0.12s",
              }}
            >
              ✓ Verify
            </button>
            <button
              onClick={() => onReject(entry.id)}
              style={{
                flex: 1,
                padding: "8px 0",
                background: "rgba(248,113,113,0.10)",
                border: "1px solid rgba(248,113,113,0.3)",
                borderRadius: 6,
                color: "#f87171",
                fontSize: 12,
                fontWeight: 600,
                cursor: "pointer",
                fontFamily: "inherit",
                transition: "all 0.12s",
              }}
            >
              ✕ Reject
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        style={{
          fontSize: 9,
          color: "#4f8ef7",
          fontWeight: 700,
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          marginBottom: 8,
        }}
      >
        {title}
      </div>
      {children}
    </div>
  );
}
function Field({ label, value, compact }) {
  return (
    <div
      style={{
        marginBottom: compact ? 4 : 8,
        paddingBottom: compact ? 0 : 8,
        borderBottom: compact ? "none" : "1px solid #1a1d28",
      }}
    >
      <div style={{ fontSize: 10, color: "#4a5270", marginBottom: 1 }}>
        {label}
      </div>
      <div
        style={{
          fontSize: compact ? 11 : 12,
          color: "#c8cfe8",
          fontFamily: "'IBM Plex Mono', monospace",
        }}
      >
        {value ?? "—"}
      </div>
    </div>
  );
}

// ─── Legend ───────────────────────────────────────────────────────────────────
function Legend() {
  return (
    <div
      style={{
        display: "flex",
        gap: 12,
        alignItems: "center",
        padding: "0 16px",
        height: "100%",
      }}
    >
      {Object.entries(MATCH_TYPE).map(([key, mt]) => (
        <div
          key={key}
          style={{ display: "flex", alignItems: "center", gap: 5 }}
        >
          <div
            style={{
              width: 8,
              height: 8,
              borderRadius: 2,
              background: mt.color,
              opacity: 0.8,
            }}
          />
          <span
            style={{
              fontSize: 10,
              color: "#6b7280",
              fontFamily: "'IBM Plex Mono', monospace",
            }}
          >
            {mt.label}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─── Stats bar ────────────────────────────────────────────────────────────────
function StatsBar({ entries }) {
  const stats = useMemo(() => {
    const total = entries.length;
    const verified = entries.filter((e) => e.verified).length;
    const rejected = entries.filter((e) => e.rejected).length;
    const pending = total - verified - rejected;
    const byType = {};
    entries.forEach((e) => {
      byType[e.matchType] = (byType[e.matchType] || 0) + 1;
    });
    return { total, verified, rejected, pending, byType };
  }, [entries]);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        height: "100%",
        padding: "0 16px",
      }}
    >
      <Stat label="Total" value={stats.total} color="#9aa0b8" />
      <Stat label="Pending" value={stats.pending} color="#facc15" />
      <Stat label="Verified" value={stats.verified} color="#4ade80" />
      <Stat label="Rejected" value={stats.rejected} color="#f87171" />
      <div
        style={{ width: 1, height: 18, background: "#1e2030", flexShrink: 0 }}
      />
      {Object.entries(stats.byType).map(([type, count]) => {
        const mt = MATCH_TYPE[type];
        if (!mt) return null;
        return (
          <Stat key={type} label={mt.label} value={count} color={mt.color} />
        );
      })}
    </div>
  );
}
function Stat({ label, value, color }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
      <span
        style={{
          fontFamily: "'IBM Plex Mono', monospace",
          fontSize: 13,
          fontWeight: 600,
          color,
        }}
      >
        {value}
      </span>
      <span
        style={{
          fontSize: 9,
          color: "#4a5270",
          fontFamily: "'IBM Plex Mono', monospace",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
        }}
      >
        {label}
      </span>
    </div>
  );
}

// ─── Filter Tabs ──────────────────────────────────────────────────────────────
const FILTERS = [
  { id: "all", label: "All" },
  { id: "pending", label: "Pending" },
  { id: "proximity", label: "Proximity" },
  { id: "duplicate", label: "Duplicate" },
  { id: "none", label: "No Match" },
  { id: "verified", label: "Verified" },
  { id: "rejected", label: "Rejected" },
];

// ─── ROOT ─────────────────────────────────────────────────────────────────────
export default function ManifestReviewer() {
  const fileManifestRef = useRef(null);
  const fileSvgRef = useRef(null);
  const transformRef = useRef(null);
  const canvasRef = useRef(null);

  const [manifestData, setManifestData] = useState(null);
  const [entries, setEntries] = useState([]);
  const [svgUrl, setSvgUrl] = useState(null);
  const [svgDims, setSvgDims] = useState({ ...DEFAULT_DIMS, minX: 0, minY: 0 });
  const [svgContentBounds, setSvgContentBounds] = useState(null);
  const [dxfExtents, setDxfExtents] = useState(null);
  const [initialScale, setInitialScale] = useState(null); // null = not yet computed
  const [positions, setPositions] = useState({});
  const [coordTransform, setCoordTransform] = useState({
    scaleX: 1,
    scaleY: -1,
    translateX: 0,
    translateY: 1000,
    flipY: true,
  });
  const [selected, setSelected] = useState(null);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [showBoxes, setShowBoxes] = useState(true);
  const [leftW] = useState(240);
  const [rightW] = useState(300);

  // Load manifest JSON
  const handleManifestUpload = useCallback((e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      try {
        const json = JSON.parse(ev.target.result);
        const parsed = parseManifest(json);
        setManifestData(json);
        setEntries(parsed);
        setPositions(buildPositions(parsed));
        setDxfExtents(json.dxf_extents || null);
        setSelected(null);
        setFilter("all");
      } catch (err) {
        alert("Failed to parse manifest JSON: " + err.message);
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  }, []);

  // Load SVG — read text first to extract viewBox, then create object URL for display
  const handleSvgUpload = useCallback((e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const text = ev.target.result;
      const dims = parseSvgDims(text);
      const bounds = parseSvgContentBounds(text);
      setSvgDims(dims);
      setSvgContentBounds(bounds);
      setInitialScale(null); // force recompute on next render
      const blob = new Blob([text], { type: "image/svg+xml" });
      const url = URL.createObjectURL(blob);
      setSvgUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return url;
      });
    };
    reader.readAsText(file);
    e.target.value = "";
  }, []);

  // Recompute coordinate transform whenever positions or SVG dims change
  useEffect(() => {
    if (Object.keys(positions).length === 0) return;
    setCoordTransform(
      computeTransform(positions, svgDims, svgContentBounds, dxfExtents),
    );
  }, [positions, svgDims, svgContentBounds, dxfExtents]);

  // Compute initial scale to fit SVG in canvas viewport.
  // We derive the viewport size from the window minus fixed UI panels.
  useEffect(() => {
    if (!svgDims.w || !svgDims.h) return;

    const compute = () => {
      // Canvas = window minus left panel, right panel (when open), and some chrome
      const vpW = window.innerWidth - leftW - (selected ? rightW : 0) - 8;
      const vpH = window.innerHeight - 48; // header height
      const scale = Math.min(vpW / svgDims.w, vpH / svgDims.h) * 0.9;
      setInitialScale(Math.max(1e-6, scale));
    };

    compute();
    window.addEventListener("resize", compute);
    return () => window.removeEventListener("resize", compute);
  }, [svgDims, leftW, rightW, selected]);

  // Verify / reject / undo
  const handleVerify = useCallback((id) => {
    setEntries((prev) =>
      prev.map((e) =>
        e.id === id ? { ...e, verified: true, rejected: false } : e,
      ),
    );
    setSelected((prev) =>
      prev?.id === id ? { ...prev, verified: true, rejected: false } : prev,
    );
  }, []);

  const handleReject = useCallback((id) => {
    setEntries((prev) =>
      prev.map((e) =>
        e.id === id ? { ...e, rejected: true, verified: false } : e,
      ),
    );
    setSelected((prev) =>
      prev?.id === id ? { ...prev, rejected: true, verified: false } : prev,
    );
  }, []);

  const handleUndo = useCallback(() => {
    if (!selected) return;
    const id = selected.id;
    setEntries((prev) =>
      prev.map((e) =>
        e.id === id ? { ...e, verified: false, rejected: false } : e,
      ),
    );
    setSelected((prev) =>
      prev ? { ...prev, verified: false, rejected: false } : prev,
    );
  }, [selected]);

  const handleNoteChange = useCallback((id, note) => {
    setEntries((prev) => prev.map((e) => (e.id === id ? { ...e, note } : e)));
    setSelected((prev) => (prev?.id === id ? { ...prev, note } : prev));
  }, []);

  const handleSelect = useCallback(
    (entry, currentPositions, currentTransform, currentSvgDims) => {
      setSelected(entry);
      const pos = currentPositions[entry.id];
      if (!pos || !transformRef.current) return;
      const svgPos = applyTransform(pos, currentTransform);
      // Centre the hit box in the viewport at a comfortable zoom
      const targetScale = 3;
      const { instance } = transformRef.current;
      if (!instance?.transformState) return;
      const wrapperRect = instance.wrapperComponent?.getBoundingClientRect();
      if (!wrapperRect) return;
      const viewW = wrapperRect.width;
      const viewH = wrapperRect.height;
      // Translate so that svgPos ends up centred in the viewport
      const newX = viewW / 2 - (svgPos.x + HIT_W / 2) * targetScale;
      const newY = viewH / 2 - (svgPos.y - HIT_H / 2) * targetScale;
      transformRef.current.setTransform(newX, newY, targetScale, 350);
    },
    [],
  );

  // Save output
  const handleSave = useCallback(() => {
    if (!manifestData || !entries.length) return;

    const output = {
      ...manifestData,
      generated_at: new Date().toISOString(),
      review_complete: entries.every((e) => e.verified || e.rejected),
      labels: { ...manifestData.labels },
      potential_matches: { ...manifestData.potential_matches },
    };

    entries.forEach((e) => {
      const reviewBlock = {
        verified: e.verified,
        rejected: e.rejected,
        note: e.note || null,
        reviewed_at: e.verified || e.rejected ? new Date().toISOString() : null,
      };
      if (output.labels[e.id]) {
        output.labels[e.id].review = reviewBlock;
      } else if (output.potential_matches[e.id]) {
        output.potential_matches[e.id].review = reviewBlock;
      }
    });

    const blob = new Blob([JSON.stringify(output, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "label-manifest-reviewed.json";
    a.click();
    URL.revokeObjectURL(a.href);
  }, [manifestData, entries]);

  // Filtered list
  const filteredEntries = useMemo(() => {
    let list = entries;
    if (filter === "pending")
      list = list.filter((e) => !e.verified && !e.rejected);
    if (filter === "proximity")
      list = list.filter((e) => e.matchType === "proximity_cluster");
    if (filter === "duplicate") list = list.filter((e) => e.duplicate);
    if (filter === "none") list = list.filter((e) => e.matchType === "none");
    if (filter === "verified") list = list.filter((e) => e.verified);
    if (filter === "rejected") list = list.filter((e) => e.rejected);
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter((e) => e.text.toLowerCase().includes(q));
    }
    return list;
  }, [entries, filter, search]);

  const isEmpty = !manifestData && !svgUrl;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        width: "100vw",
        background: "#0c0e14",
        color: "#e8ecf4",
        fontFamily: "'IBM Plex Sans', system-ui, sans-serif",
        overflow: "hidden",
      }}
    >
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
        ::-webkit-scrollbar { width:4px }
        ::-webkit-scrollbar-track { background:transparent }
        ::-webkit-scrollbar-thumb { background:#1e2030; border-radius:2px }
        * { box-sizing:border-box }
        button:hover { opacity:0.85 }
        textarea:focus { border-color:#4f8ef7 !important; }
      `}</style>

      {/* ── Header ── */}
      <header
        style={{
          height: 46,
          display: "flex",
          alignItems: "center",
          padding: "0 16px",
          borderBottom: "1px solid #1a1d28",
          background: "#0a0c11",
          flexShrink: 0,
          gap: 12,
          zIndex: 20,
        }}
      >
        <span
          style={{
            fontFamily: "'IBM Plex Mono', monospace",
            fontSize: 12,
            fontWeight: 600,
            color: "#e8ecf4",
            letterSpacing: "-0.01em",
          }}
        >
          Manifest Reviewer
        </span>
        <div style={{ width: 1, height: 16, background: "#1e2030" }} />

        {/* Load buttons */}
        <button
          onClick={() => fileManifestRef.current?.click()}
          style={{
            background: "#1a1d28",
            border: "1px solid #2a2d3a",
            borderRadius: 5,
            padding: "4px 12px",
            fontSize: 11,
            color: "#9aa0b8",
            cursor: "pointer",
            fontFamily: "inherit",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          ⊕ Load manifest
        </button>
        <button
          onClick={() => fileSvgRef.current?.click()}
          style={{
            background: "#1a1d28",
            border: "1px solid #2a2d3a",
            borderRadius: 5,
            padding: "4px 12px",
            fontSize: 11,
            color: "#9aa0b8",
            cursor: "pointer",
            fontFamily: "inherit",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          ⊕ Load SVG
        </button>

        {/* Stats */}
        {entries.length > 0 && <StatsBar entries={entries} />}

        <div style={{ flex: 1 }} />

        {/* Show boxes toggle */}
        <button
          onClick={() => setShowBoxes((v) => !v)}
          style={{
            background: showBoxes ? "rgba(79,142,247,0.12)" : "#1a1d28",
            border: `1px solid ${showBoxes ? "rgba(79,142,247,0.35)" : "#2a2d3a"}`,
            borderRadius: 5,
            padding: "4px 12px",
            fontSize: 11,
            color: showBoxes ? "#4f8ef7" : "#4a5270",
            cursor: "pointer",
            fontFamily: "inherit",
          }}
        >
          {showBoxes ? "◧ Hide boxes" : "◧ Show boxes"}
        </button>

        {/* Save */}
        {entries.length > 0 && (
          <button
            onClick={handleSave}
            style={{
              background: "rgba(74,222,128,0.1)",
              border: "1px solid rgba(74,222,128,0.3)",
              borderRadius: 5,
              padding: "4px 14px",
              fontSize: 11,
              color: "#4ade80",
              cursor: "pointer",
              fontFamily: "'IBM Plex Mono', monospace",
              fontWeight: 600,
            }}
          >
            ↓ Save reviewed
          </button>
        )}
      </header>

      {/* ── Body ── */}
      <div style={{ flex: 1, display: "flex", overflow: "hidden" }}>
        {/* Left: list panel */}
        {entries.length > 0 && (
          <div
            style={{
              width: leftW,
              flexShrink: 0,
              height: "100%",
              background: "#0e1018",
              borderRight: "1px solid #1a1d28",
              display: "flex",
              flexDirection: "column",
            }}
          >
            {/* Search */}
            <div
              style={{
                padding: "10px 12px 8px",
                borderBottom: "1px solid #1a1d28",
                flexShrink: 0,
              }}
            >
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search labels…"
                style={{
                  width: "100%",
                  padding: "5px 10px",
                  background: "#1a1d28",
                  border: "1px solid #2a2d3a",
                  borderRadius: 5,
                  color: "#e8ecf4",
                  fontSize: 11,
                  fontFamily: "'IBM Plex Mono', monospace",
                  outline: "none",
                }}
              />
            </div>

            {/* Filter tabs */}
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 3,
                padding: "7px 10px 6px",
                borderBottom: "1px solid #1a1d28",
                flexShrink: 0,
              }}
            >
              {FILTERS.map((f) => {
                const count =
                  f.id === "all"
                    ? entries.length
                    : f.id === "pending"
                      ? entries.filter((e) => !e.verified && !e.rejected).length
                      : f.id === "proximity"
                        ? entries.filter(
                            (e) => e.matchType === "proximity_cluster",
                          ).length
                        : f.id === "duplicate"
                          ? entries.filter((e) => e.duplicate).length
                          : f.id === "none"
                            ? entries.filter((e) => e.matchType === "none")
                                .length
                            : f.id === "verified"
                              ? entries.filter((e) => e.verified).length
                              : f.id === "rejected"
                                ? entries.filter((e) => e.rejected).length
                                : 0;
                const active = filter === f.id;
                return (
                  <button
                    key={f.id}
                    onClick={() => setFilter(f.id)}
                    style={{
                      fontSize: 9,
                      padding: "2px 7px",
                      borderRadius: 10,
                      cursor: "pointer",
                      fontFamily: "'IBM Plex Mono', monospace",
                      border: `1px solid ${active ? "#4f8ef7" : "#2a2d3a"}`,
                      background: active
                        ? "rgba(79,142,247,0.12)"
                        : "transparent",
                      color: active ? "#4f8ef7" : "#4a5270",
                    }}
                  >
                    {f.label}{" "}
                    {count > 0 && <span style={{ opacity: 0.6 }}>{count}</span>}
                  </button>
                );
              })}
            </div>

            {/* List */}
            <div style={{ flex: 1, overflow: "hidden" }}>
              <EntryList
                entries={filteredEntries}
                selected={selected}
                filter={filter}
                onSelect={(e) =>
                  handleSelect(e, positions, coordTransform, svgDims)
                }
              />
            </div>
          </div>
        )}

        {/* Canvas */}
        <div
          ref={canvasRef}
          style={{
            flex: 1,
            position: "relative",
            overflow: "hidden",
            background: "#d4d8e0",
          }}
        >
          {isEmpty ? (
            /* Empty state */
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexDirection: "column",
                gap: 20,
              }}
            >
              <div
                style={{
                  background: "#0e1018",
                  border: "1px solid #1e2030",
                  borderRadius: 12,
                  padding: "40px 50px",
                  textAlign: "center",
                  maxWidth: 420,
                }}
              >
                <div style={{ fontSize: 32, marginBottom: 14, opacity: 0.4 }}>
                  ⊹
                </div>
                <div
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: "#e8ecf4",
                    marginBottom: 8,
                  }}
                >
                  Manifest Reviewer
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: "#4a5270",
                    lineHeight: 1.7,
                    marginBottom: 24,
                  }}
                >
                  Load a{" "}
                  <code style={{ color: "#facc15" }}>label-manifest.json</code>{" "}
                  from{" "}
                  <code style={{ color: "#4f8ef7" }}>extract_manifest.py</code>,
                  then optionally load your SVG diagram to see labels overlaid.
                </div>
                <div
                  style={{ display: "flex", gap: 10, justifyContent: "center" }}
                >
                  <button
                    onClick={() => fileManifestRef.current?.click()}
                    style={{
                      background: "rgba(79,142,247,0.12)",
                      border: "1px solid rgba(79,142,247,0.3)",
                      borderRadius: 7,
                      padding: "9px 20px",
                      fontSize: 12,
                      color: "#4f8ef7",
                      cursor: "pointer",
                      fontFamily: "inherit",
                      fontWeight: 600,
                    }}
                  >
                    Load manifest
                  </button>
                  <button
                    onClick={() => fileSvgRef.current?.click()}
                    style={{
                      background: "#1a1d28",
                      border: "1px solid #2a2d3a",
                      borderRadius: 7,
                      padding: "9px 20px",
                      fontSize: 12,
                      color: "#6b7280",
                      cursor: "pointer",
                      fontFamily: "inherit",
                    }}
                  >
                    Load SVG
                  </button>
                </div>
              </div>
            </div>
          ) : initialScale === null ? (
            <div
              style={{
                position: "absolute",
                inset: 0,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <span
                style={{
                  fontSize: 12,
                  fontFamily: "'IBM Plex Mono', monospace",
                  color: "#4a5270",
                }}
              >
                Initialising…
              </span>
            </div>
          ) : (
            <TransformWrapper
              key={`${svgUrl || "no-svg"}-${initialScale}`}
              ref={transformRef}
              initialScale={initialScale}
              minScale={initialScale * 0.05}
              maxScale={initialScale * 500}
              wheel={{ step: 0.1, smoothStep: 0.005 }}
              limitToBounds={false}
              centerOnInit
            >
              <TransformComponent
                wrapperStyle={{ width: "100%", height: "100%" }}
                contentStyle={{ width: svgDims.w, height: svgDims.h }}
              >
                {svgUrl ? (
                  <img
                    src={svgUrl}
                    width={svgDims.w}
                    height={svgDims.h}
                    draggable={false}
                    style={{ display: "block", userSelect: "none" }}
                  />
                ) : (
                  <div
                    style={{
                      width: svgDims.w,
                      height: svgDims.h,
                      background: "#c8cdd8",
                      backgroundImage:
                        "linear-gradient(#b8bcc888 1px, transparent 1px), linear-gradient(90deg, #b8bcc888 1px, transparent 1px)",
                      backgroundSize: "100px 100px",
                    }}
                  />
                )}
                {entries.length > 0 && (
                  <HitOverlay
                    entries={entries}
                    positions={positions}
                    selected={selected}
                    showBoxes={showBoxes}
                    svgDims={svgDims}
                    transform={coordTransform}
                    onSelect={(e) => {
                      const live = entries.find((x) => x.id === e.id) || e;
                      handleSelect(live, positions, coordTransform, svgDims);
                    }}
                  />
                )}
              </TransformComponent>
              <ZoomControls />
            </TransformWrapper>
          )}

          {/* Canvas legend */}
          {entries.length > 0 && (
            <div
              style={{
                position: "absolute",
                bottom: 16,
                left: 14,
                background: "#0e1018cc",
                backdropFilter: "blur(8px)",
                border: "1px solid #1e2030",
                borderRadius: 7,
                padding: "8px 12px",
                zIndex: 10,
              }}
            >
              <Legend />
            </div>
          )}
        </div>

        {/* Right: detail panel */}
        {selected && (
          <div
            style={{
              width: rightW,
              flexShrink: 0,
              height: "100%",
              background: "#0e1018",
              borderLeft: "1px solid #1a1d28",
            }}
          >
            <DetailPanel
              entry={entries.find((e) => e.id === selected.id) || selected}
              onVerify={handleVerify}
              onReject={handleReject}
              onUndo={handleUndo}
              onNoteChange={handleNoteChange}
              onClose={() => setSelected(null)}
            />
          </div>
        )}
      </div>

      {/* Hidden file inputs */}
      <input
        ref={fileManifestRef}
        type="file"
        accept=".json"
        style={{ display: "none" }}
        onChange={handleManifestUpload}
      />
      <input
        ref={fileSvgRef}
        type="file"
        accept=".svg"
        style={{ display: "none" }}
        onChange={handleSvgUpload}
      />
    </div>
  );
}
