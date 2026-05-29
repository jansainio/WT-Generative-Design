"""
design_agent.py
---------------
Design Agent for the Wind Turbine Blade Optimisation Pipeline.

Responsibilities:
  1. Receive blade variant parameters (from orchestrator or manual call)
  2. Validate parameters with Pydantic
  3. Render a Fusion 360 Python script from a Jinja2 template
  4. Execute the script in Fusion 360 via MCP bridge
  5. Export the resulting body as a STEP file
  6. Log the variant and its file path to SQLite

Dependencies (install into your venv):
  pip install pydantic jinja2 mcp   # mcp = fusion360-mcp-bridge client

Directory layout expected:
  project_root/
    design_agent.py          ← this file
    templates/
      blade_script.py.j2     ← Jinja2 Fusion script template (generated below)
    output/
      variants/              ← STEP files land here
    pipeline.db              ← SQLite database (auto-created)
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# --- third-party ---
from pydantic import BaseModel, Field, field_validator, model_validator
from jinja2 import Environment, FileSystemLoader, StrictUndefined

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("design_agent")


# ─────────────────────────────────────────────
# 1. PARAMETER SCHEMA
# ─────────────────────────────────────────────

class BladeSection(BaseModel):
    """One cross-sectional slice of the blade."""
    z_mm: float = Field(..., description="Height along blade span (mm)")
    chord_mm: float = Field(..., gt=0, description="Chord length at this section (mm)")
    twist_deg: float = Field(..., ge=-90, le=90, description="Aerofoil twist angle (deg)")
    sweep_mm: float = Field(0.0, description="Leading-edge sweep offset in X (mm)")

    @field_validator("chord_mm")
    @classmethod
    def chord_positive(cls, v):
        if v <= 0:
            raise ValueError("chord_mm must be positive")
        return v


class BladeVariant(BaseModel):
    """Full specification for one blade design variant."""
    variant_id: str = Field(..., description="Unique identifier, e.g. 'blade_v001'")
    span_mm: float = Field(..., gt=0, le=300, description="Total blade span (mm)")
    sections: List[BladeSection] = Field(..., min_length=3,
                                         description="At least 3 cross-sections from root to tip")
    profile: str = Field("naca0012", description="Aerofoil profile family")
    export_format: str = Field("STEP", description="Geometry export format: STEP or STL")
    notes: Optional[str] = None

    @model_validator(mode="after")
    def sections_span_root_to_tip(self):
        zs = [s.z_mm for s in self.sections]
        if zs != sorted(zs):
            raise ValueError("sections must be ordered by z_mm (root → tip)")
        if zs[0] != 0:
            raise ValueError("First section must be at z_mm = 0 (blade root)")
        if abs(zs[-1] - self.span_mm) > 0.1:
            raise ValueError(
                f"Last section z_mm ({zs[-1]}) must equal span_mm ({self.span_mm})"
            )
        return self

    @field_validator("export_format")
    @classmethod
    def valid_format(cls, v):
        if v.upper() not in ("STEP", "STL"):
            raise ValueError("export_format must be STEP or STL")
        return v.upper()


# ─────────────────────────────────────────────
# 2. JINJA2 TEMPLATE (inline — also saved to file)
# ─────────────────────────────────────────────

BLADE_TEMPLATE = '''\
"""
Auto-generated Fusion 360 script for variant: {{ variant_id }}
Generated: {{ generated_at }}
Profile:   {{ profile }}
Span:      {{ span_mm }} mm
"""
import adsk.core, adsk.fusion, math

app    = adsk.core.Application.get()
ui     = app.userInterface
design = adsk.fusion.Design.cast(app.activeProduct)
root   = design.rootComponent

sketches  = root.sketches
planes    = root.constructionPlanes
features  = root.features

def offset_plane(base_plane, z_cm):
    pi = planes.createInput()
    pi.setByOffset(base_plane, adsk.core.ValueInput.createByReal(z_cm))
    return planes.add(pi)

def naca0012_points(chord_cm, twist_deg, sweep_cm, n=20):
    """Return list of (x,y) tuples for a NACA 0012 aerofoil, twisted and swept."""
    twist = math.radians(twist_deg)
    t = 0.12  # thickness ratio

    def thickness(xn):
        return 5 * t * (
            0.2969 * math.sqrt(xn)
            - 0.1260 * xn
            - 0.3516 * xn**2
            + 0.2843 * xn**3
            - 0.1015 * xn**4
        )

    upper, lower = [], []
    for i in range(n + 1):
        xn = 0.5 * (1 - math.cos(math.pi * i / n))
        yt = thickness(xn) * chord_cm
        xc = xn * chord_cm
        upper.append((xc,  yt))
        lower.append((xc, -yt))

    profile = upper + lower[-2::-1]
    result = []
    for (px, py) in profile:
        px -= chord_cm * 0.25          # rotate about quarter-chord
        rx = px * math.cos(twist) - py * math.sin(twist)
        ry = px * math.sin(twist) + py * math.cos(twist)
        rx += sweep_cm + chord_cm * 0.25
        result.append((rx, ry))
    return result

# ── Build one sketch per section ──────────────────────────────────────────────
sections = {{ sections }}   {# list of dicts: z_mm, chord_mm, twist_deg, sweep_mm #}
sketch_list = []

for sec in sections:
    z_cm     = sec["z_mm"]     / 10.0
    chord_cm = sec["chord_mm"] / 10.0
    sweep_cm = sec["sweep_mm"] / 10.0
    twist    = sec["twist_deg"]

    pl = offset_plane(root.xYConstructionPlane, z_cm)
    sk = sketches.add(pl)

    pts_raw = naca0012_points(chord_cm, twist, sweep_cm)
    pts     = adsk.core.ObjectCollection.create()
    for (px, py) in pts_raw:
        pts.add(adsk.core.Point3D.create(px, py, 0))

    spline = sk.sketchCurves.sketchFittedSplines.add(pts)
    spline.isClosed = True
    sketch_list.append(sk)

print(f"Created {len(sketch_list)} cross-sections")

# ── Loft through all sections ─────────────────────────────────────────────────
loftFeatures = features.loftFeatures
loftInput    = loftFeatures.createInput(
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation
)

for sk in sketch_list:
    for spline in sk.sketchCurves.sketchFittedSplines:
        loftInput.loftSections.add(spline)
        break

loftInput.isSolid = True
loft = loftFeatures.add(loftInput)

if not loft:
    raise RuntimeError("Loft failed — check section orientations")

body = root.bRepBodies.item(0)
body.name = "{{ variant_id }}"
print(f"Body '{body.name}' created successfully")

# ── Export ────────────────────────────────────────────────────────────────────
export_path = r"{{ export_path }}"
exportMgr   = design.exportManager

{% if export_format == "STEP" %}
opts = exportMgr.createSTEPExportOptions(export_path)
{% else %}
opts = exportMgr.createSTLExportOptions(body, export_path)
opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
{% endif %}

exportMgr.execute(opts)
print(f"Exported to: {export_path}")
'''


# ─────────────────────────────────────────────
# 3. SQLITE DATABASE SETUP
# ─────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS variants (
    variant_id      TEXT PRIMARY KEY,
    span_mm         REAL NOT NULL,
    profile         TEXT NOT NULL,
    sections_json   TEXT NOT NULL,        -- full section data as JSON
    step_path       TEXT,                 -- absolute path to exported STEP/STL
    export_format   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- status values:
    --   pending   → queued, not yet built
    --   building  → Fusion script running
    --   ready     → STEP exported, awaiting simulation
    --   simulating→ simulation agent picked it up
    --   done      → analysis complete
    --   failed    → error during build or export
    error_msg       TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    variant_id  TEXT NOT NULL,
    event       TEXT NOT NULL,   -- e.g. 'build_started', 'exported', 'error'
    detail      TEXT,
    ts          TEXT NOT NULL
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("Database ready at %s", db_path)
    return conn


def log_event(conn: sqlite3.Connection, variant_id: str, event: str, detail: str = ""):
    conn.execute(
        "INSERT INTO pipeline_log (variant_id, event, detail, ts) VALUES (?,?,?,?)",
        (variant_id, event, detail, datetime.utcnow().isoformat()),
    )
    conn.commit()


# ─────────────────────────────────────────────
# 4. FUSION EXECUTION WRAPPER
# ─────────────────────────────────────────────

def run_in_fusion(script: str, retries: int = 3, backoff: float = 2.0) -> str:
    """
    Execute a Python script inside Fusion 360 via the MCP bridge.
    Retries on transient failures with exponential backoff.

    Replace the body of this function if you switch MCP clients.
    """
    # Import here so the rest of the file works even without the bridge installed
    try:
        from fusion360 import fusion_execute  # MCP bridge tool
    except ImportError:
        raise ImportError(
            "fusion360-mcp-bridge is not installed or not on PATH.\n"
            "Follow the setup guide to install and activate the Fusion add-in."
        )

    last_exc = None
    for attempt in range(retries):
        try:
            result = fusion_execute(script=script)
            # The bridge returns stdout as a string; surface any Fusion errors
            if result and ("error" in result.lower() or "traceback" in result.lower()):
                raise RuntimeError(f"Fusion reported an error:\n{result}")
            return result or ""
        except Exception as exc:
            last_exc = exc
            wait = backoff ** attempt
            log.warning(
                "Fusion call failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt + 1, retries, exc, wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"Fusion call failed after {retries} attempts") from last_exc


# ─────────────────────────────────────────────
# 5. DESIGN AGENT — MAIN FUNCTION
# ─────────────────────────────────────────────

class DesignAgent:
    def __init__(
        self,
        db_path: Path = Path("pipeline.db"),
        output_dir: Path = Path("output/variants"),
        template_dir: Path = Path("templates"),
    ):
        self.db         = init_db(db_path)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Save the template to disk on first run
        template_dir.mkdir(parents=True, exist_ok=True)
        tmpl_file = template_dir / "blade_script.py.j2"
        if not tmpl_file.exists():
            tmpl_file.write_text(BLADE_TEMPLATE, encoding="utf-8")
            log.info("Wrote Jinja2 template to %s", tmpl_file)

        self.jinja = Environment(
            loader=FileSystemLoader(str(template_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _register_variant(self, variant: BladeVariant):
        """Insert variant row with status=pending (idempotent)."""
        now = datetime.utcnow().isoformat()
        self.db.execute(
            """INSERT OR IGNORE INTO variants
               (variant_id, span_mm, profile, sections_json,
                export_format, status, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                variant.variant_id,
                variant.span_mm,
                variant.profile,
                json.dumps([s.model_dump() for s in variant.sections]),
                variant.export_format,
                "pending",
                variant.notes,
                now, now,
            ),
        )
        self.db.commit()

    def _set_status(self, variant_id: str, status: str, error_msg: str = ""):
        self.db.execute(
            "UPDATE variants SET status=?, error_msg=?, updated_at=? WHERE variant_id=?",
            (status, error_msg, datetime.utcnow().isoformat(), variant_id),
        )
        self.db.commit()

    def build(self, variant: BladeVariant) -> Path:
        """
        Full build pipeline for one variant.
        Returns the path to the exported STEP/STL file.
        Raises on failure.
        """
        vid = variant.variant_id
        export_path = self.output_dir / f"{vid}.{variant.export_format.lower()}"

        log.info("Building variant: %s", vid)
        self._register_variant(variant)
        log_event(self.db, vid, "build_started")
        self._set_status(vid, "building")

        # ── Render Jinja2 template ────────────────────────────────────────────
        template  = self.jinja.get_template("blade_script.py.j2")
        script    = template.render(
            variant_id    = vid,
            span_mm       = variant.span_mm,
            profile       = variant.profile,
            export_format = variant.export_format,
            export_path   = str(export_path).replace("\\", "\\\\"),
            sections      = json.dumps([s.model_dump() for s in variant.sections]),
            generated_at  = datetime.utcnow().isoformat(),
        )

        # ── Execute in Fusion ─────────────────────────────────────────────────
        try:
            stdout = run_in_fusion(script)
            log.info("Fusion output:\n%s", stdout)
        except Exception as exc:
            self._set_status(vid, "failed", str(exc))
            log_event(self.db, vid, "error", str(exc))
            raise

        # ── Verify export file exists ─────────────────────────────────────────
        if not export_path.exists():
            msg = f"Export file not found after Fusion run: {export_path}"
            self._set_status(vid, "failed", msg)
            log_event(self.db, vid, "error", msg)
            raise FileNotFoundError(msg)

        # ── Update DB with file path and ready status ─────────────────────────
        self.db.execute(
            "UPDATE variants SET step_path=?, status=?, updated_at=? WHERE variant_id=?",
            (str(export_path), "ready", datetime.utcnow().isoformat(), vid),
        )
        self.db.commit()
        log_event(self.db, vid, "exported", str(export_path))
        log.info("Variant %s ready → %s", vid, export_path)

        return export_path

    def build_batch(self, variants: List[BladeVariant]) -> dict:
        """
        Build multiple variants in sequence.
        Returns a summary dict: {variant_id: 'ready' | 'failed'}.
        """
        results = {}
        for variant in variants:
            try:
                self.build(variant)
                results[variant.variant_id] = "ready"
            except Exception as exc:
                log.error("Variant %s failed: %s", variant.variant_id, exc)
                results[variant.variant_id] = f"failed: {exc}"
        return results

    def get_ready_variants(self) -> list:
        """Return all variants with status='ready' (for simulation agent to pick up)."""
        rows = self.db.execute(
            "SELECT * FROM variants WHERE status='ready' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# 6. PARAMETER SWEEP HELPER
# ─────────────────────────────────────────────

def generate_sweep(
    base_span_mm: float = 80.0,
    twist_root_range: tuple = (25, 35),
    twist_steps: int = 3,
    chord_root_range: tuple = (16, 20),
    chord_steps: int = 2,
) -> List[BladeVariant]:
    """
    Generate a grid of variants by sweeping twist and chord at the root.
    Tip section is kept fixed. Returns a list of BladeVariant objects.

    Example: 3 twist × 2 chord = 6 variants total.
    """
    variants = []
    twists = [
        twist_root_range[0] + i * (twist_root_range[1] - twist_root_range[0]) / max(twist_steps - 1, 1)
        for i in range(twist_steps)
    ]
    chords = [
        chord_root_range[0] + i * (chord_root_range[1] - chord_root_range[0]) / max(chord_steps - 1, 1)
        for i in range(chord_steps)
    ]

    count = 0
    for twist_root in twists:
        for chord_root in chords:
            count += 1
            vid = f"blade_t{int(twist_root):02d}_c{int(chord_root):02d}"
            variants.append(BladeVariant(
                variant_id = vid,
                span_mm    = base_span_mm,
                profile    = "naca0012",
                export_format = "STEP",
                sections   = [
                    BladeSection(z_mm=0,              chord_mm=chord_root, twist_deg=twist_root,       sweep_mm=0),
                    BladeSection(z_mm=base_span_mm*0.3, chord_mm=chord_root*0.75, twist_deg=twist_root*0.6, sweep_mm=5),
                    BladeSection(z_mm=base_span_mm*0.6, chord_mm=chord_root*0.5,  twist_deg=twist_root*0.3, sweep_mm=12),
                    BladeSection(z_mm=base_span_mm,   chord_mm=3.0,        twist_deg=0,                sweep_mm=19),
                ],
                notes = f"Sweep variant: twist_root={twist_root:.1f}° chord_root={chord_root:.1f}mm",
            ))

    log.info("Generated %d variants", count)
    return variants


# ─────────────────────────────────────────────
# 7. ENTRY POINT — run directly to test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    agent = DesignAgent(
        db_path    = Path("pipeline.db"),
        output_dir = Path("output/variants"),
        template_dir = Path("templates"),
    )

    # --- Option A: build a single hand-crafted variant ---
    single = BladeVariant(
        variant_id    = "blade_v001",
        span_mm       = 80,
        profile       = "naca0012",
        export_format = "STEP",
        sections = [
            BladeSection(z_mm=0,  chord_mm=18, twist_deg=30, sweep_mm=0),
            BladeSection(z_mm=20, chord_mm=14, twist_deg=20, sweep_mm=4),
            BladeSection(z_mm=45, chord_mm=9,  twist_deg=10, sweep_mm=10),
            BladeSection(z_mm=65, chord_mm=5,  twist_deg=4,  sweep_mm=16),
            BladeSection(z_mm=80, chord_mm=3,  twist_deg=0,  sweep_mm=19),
        ],
    )
    agent.build(single)

    # --- Option B: run a parameter sweep (6 variants) ---
    # variants = generate_sweep(twist_steps=3, chord_steps=2)
    # results  = agent.build_batch(variants)
    # print(results)

    # --- Check what's ready for simulation ---
    ready = agent.get_ready_variants()
    print(f"\nReady for simulation: {len(ready)} variant(s)")
    for v in ready:
        print(f"  {v['variant_id']} → {v['step_path']}")
