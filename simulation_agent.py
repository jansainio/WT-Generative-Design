"""
simulation_agent.py
Uses PyANSYS embedded App() - direct Python API, no IronPython scripts.
Run from ansys-env:
  C:\\Users\\jansa\\ansys-env\\Scripts\\activate
  python simulation_agent.py
"""

from __future__ import annotations
import json, logging, sqlite3, time
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional

log = logging.getLogger("simulation_agent")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

DB_PATH       = Path(r"C:\Users\jansa\Downloads\pipeline.db")
RESULTS_DIR   = Path(r"C:\Users\jansa\Downloads\output\results")
ANSYS_PATH    = r"C:\Program Files\Ansys Inc\ANSYS Student\v261"
ANSYS_VERSION = 261
WIND_PRESSURE_PA     = 500.0
MATERIAL_NAME        = "Structural Steel"
MESH_SIZE_MM         = 3.0
SAFETY_FACTOR_TARGET = 2.0
YIELD_STRESS_PA      = 250e6


def get_db(db_path=DB_PATH):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    existing = {row[1] for row in conn.execute("PRAGMA table_info(variants)")}
    for col, dtype in [("results_path","TEXT"),("max_stress_pa","REAL"),
                       ("max_deform_mm","REAL"),("safety_factor","REAL")]:
        if col not in existing:
            conn.execute("ALTER TABLE variants ADD COLUMN " + col + " " + dtype)
    conn.commit()
    return conn


def log_event(conn, variant_id, event, detail=""):
    conn.execute(
        "INSERT INTO pipeline_log (variant_id, event, detail, ts) VALUES (?,?,?,?)",
        (variant_id, event, detail, datetime.now(UTC).isoformat()))
    conn.commit()


def set_status(conn, variant_id, status, error_msg="", extra=None):
    fields = "status=?, error_msg=?, updated_at=?"
    values = [status, error_msg, datetime.now(UTC).isoformat()]
    if extra:
        for k, v in extra.items():
            fields += ", " + k + "=?"
            values.append(v)
    values.append(variant_id)
    conn.execute("UPDATE variants SET " + fields + " WHERE variant_id=?", values)
    conn.commit()


def poll_and_lock(conn):
    with conn:
        rows = conn.execute(
            "SELECT variant_id, step_path, span_mm, sections_json, profile "
            "FROM variants WHERE status=\'ready\' ORDER BY created_at ASC"
        ).fetchall()
        if not rows:
            return []
        ids = [r["variant_id"] for r in rows]
        placeholders = ",".join(["?"] * len(ids))
        conn.execute(
            "UPDATE variants SET status=\'simulating\', updated_at=? WHERE variant_id IN (" + placeholders + ")",
            [datetime.now(UTC).isoformat()] + ids)
    log.info("Locked %d variant(s): %s", len(rows), ids)
    for vid in ids:
        log_event(conn, vid, "simulation_started")
    return [dict(r) for r in rows]


def launch_ansys():
    import ansys.mechanical.core as pymechanical
    log.info("Launching ANSYS Mechanical embedded (v%d)...", ANSYS_VERSION)
    app = pymechanical.App(ansys_path=ANSYS_PATH, version=ANSYS_VERSION)
    log.info("ANSYS ready.")
    return app


def simulate_variant(app, variant, results_dir):
    vid          = variant["variant_id"]
    step_path    = variant["step_path"]
    Path(str(results_dir)).mkdir(parents=True, exist_ok=True)
    results_path = str(results_dir) + "\\" + vid + "_results.json"

    log.info("Simulating: %s", vid)
    app.update_globals(globals())

    ExtAPI.DataModel.Project.New()

    geometry_import = Model.GeometryImportGroup.AddGeometryImport()
    geometry_import.Import(
        step_path,
        Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic,
        Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences(),
    )
    log.info("Geometry imported")

    Model.AddStaticStructuralAnalysis()
    analysis = Model.Analyses[0]

    mesh = Model.Mesh
    mesh.ElementSize = Quantity(MESH_SIZE_MM, "mm")
    mesh.GenerateMesh()
    node_count = mesh.Nodes
    elem_count = mesh.Elements
    log.info("Mesh: %d nodes, %d elements", node_count, elem_count)

    # Fixed support - scope to bottom face (min Z = blade root)
    fixed_support = analysis.AddFixedSupport()
    root_sel = ExtAPI.SelectionManager.CreateSelectionInfo(
        Ansys.ACT.Interfaces.Common.SelectionTypeEnum.GeometryEntities
    )
    bodies_geom = [Model.Geometry.Children[i]
                   for i in range(Model.Geometry.Children.Count)]
    root_face_ids = []
    for body in bodies_geom:
        try:
            geo = body.GetGeoBody()
            min_z = min(geo.Faces[i].Centroid.Z for i in range(geo.Faces.Count))
            for i in range(geo.Faces.Count):
                face = geo.Faces[i]
                if abs(face.Centroid.Z - min_z) < 0.001:
                    root_face_ids.append(face.Id)
        except Exception:
            pass
    if root_face_ids:
        root_sel.Ids = root_face_ids
        fixed_support.Location = root_sel
        log.info("Fixed support scoped to %d root face(s)", len(root_face_ids))

    # Wind pressure - scope to all non-root faces
    pressure = analysis.AddPressure()
    pressure.Magnitude.Output.DiscreteValues = [Quantity(WIND_PRESSURE_PA, "Pa")]
    pressure_face_ids = []
    for body in bodies_geom:
        try:
            geo = body.GetGeoBody()
            min_z = min(geo.Faces[i].Centroid.Z for i in range(geo.Faces.Count))
            for i in range(geo.Faces.Count):
                face = geo.Faces[i]
                if abs(face.Centroid.Z - min_z) >= 0.001:
                    pressure_face_ids.append(face.Id)
        except Exception:
            pass
    if pressure_face_ids:
        pres_sel = ExtAPI.SelectionManager.CreateSelectionInfo(
            Ansys.ACT.Interfaces.Common.SelectionTypeEnum.GeometryEntities
        )
        pres_sel.Ids = pressure_face_ids
        pressure.Location = pres_sel
        log.info("Pressure scoped to %d surface face(s)", len(pressure_face_ids))

    log.info("Boundary conditions applied")

    log.info("Solving...")
    analysis.Solve(True)
    log.info("Solve complete")

    solution = analysis.Solution

    # Evaluate results - must add before solve or re-evaluate after
    stress_r = solution.AddMaximumPrincipalStress()
    deform_r = solution.AddTotalDeformation()
    vm_r     = solution.AddEquivalentStress()
    solution.EvaluateAllResults()

    max_stress_pa = stress_r.Maximum.Value
    max_deform_mm = deform_r.Maximum.Value * 1000
    avg_stress_pa = vm_r.Average.Value

    safety_factor = YIELD_STRESS_PA / max_stress_pa if max_stress_pa > 0 else 999.0

    results = {
        "variant_id":       vid,
        "max_stress_pa":    max_stress_pa,
        "max_stress_mpa":   max_stress_pa / 1e6,
        "max_deform_mm":    max_deform_mm,
        "avg_stress_pa":    avg_stress_pa,
        "safety_factor":    safety_factor,
        "mesh_nodes":       node_count,
        "mesh_elements":    elem_count,
        "wind_pressure_pa": WIND_PRESSURE_PA,
        "material":         MATERIAL_NAME,
        "below_sf_target":  safety_factor < SAFETY_FACTOR_TARGET,
    }

    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)

    log.info("Raw values - stress_pa: %s | deform raw: %s | avg_stress: %s",
             max_stress_pa, deform_r.Maximum.Value, avg_stress_pa)
    log.info("Done - stress: %.2f MPa | deform: %.3f mm | SF: %.2f",
             max_stress_pa / 1e6, max_deform_mm, safety_factor)
    return results


class SimulationAgent:
    def __init__(self, db_path=DB_PATH, results_dir=RESULTS_DIR):
        self.conn        = get_db(db_path)
        self.results_dir = Path(str(results_dir))
        self.app         = None

    def _ensure_ansys(self):
        if self.app is None:
            self.app = launch_ansys()

    def run_once(self):
        variants = poll_and_lock(self.conn)
        if not variants:
            log.info("No variants ready.")
            return {}
        self._ensure_ansys()
        summary = {}
        for variant in variants:
            vid = variant["variant_id"]
            try:
                results = simulate_variant(self.app, variant, self.results_dir)
                set_status(self.conn, vid, "done", extra={
                    "results_path":  str(self.results_dir) + "\\" + vid + "_results.json",
                    "max_stress_pa": results["max_stress_pa"],
                    "max_deform_mm": results["max_deform_mm"],
                    "safety_factor": results["safety_factor"],
                })
                log_event(self.conn, vid, "simulation_complete",
                          "SF=" + str(round(results["safety_factor"], 2)))
                summary[vid] = "done"
            except Exception as exc:
                log.error("Variant %s failed: %s", vid, exc)
                set_status(self.conn, vid, "failed", str(exc))
                log_event(self.conn, vid, "simulation_error", str(exc))
                summary[vid] = "failed: " + str(exc)
        return summary

    def reset_failed(self):
        self.conn.execute("UPDATE variants SET status=\'ready\', error_msg=NULL WHERE status=\'failed\'")
        self.conn.commit()
        log.info("Reset failed variants to ready.")

    def get_results_summary(self):
        rows = self.conn.execute(
            "SELECT variant_id, status, max_stress_pa, max_deform_mm, "
            "safety_factor, updated_at FROM variants "
            "WHERE status IN (\'done\',\'failed\') ORDER BY safety_factor DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        if self.app:
            self.app.close()
            log.info("ANSYS closed.")


if __name__ == "__main__":
    agent = SimulationAgent()
    summary = agent.run_once()
    print("\nSimulation summary:")
    for vid, status in summary.items():
        print("  " + vid + ": " + status)
    print("\nAll results:")
    print("  " + "variant".ljust(22) + "status".ljust(12) +
          "stress MPa".rjust(10) + "deform mm".rjust(10) + "SF".rjust(6))
    print("  " + "-" * 62)
    for r in agent.get_results_summary():
        stress = (r["max_stress_pa"] or 0) / 1e6
        deform = r["max_deform_mm"] or 0
        sf     = r["safety_factor"] or 0
        flag   = " <-- low SF" if 0 < sf < SAFETY_FACTOR_TARGET else ""
        print("  " + r["variant_id"].ljust(22) + r["status"].ljust(12) +
              str(round(stress,2)).rjust(10) + str(round(deform,3)).rjust(10) +
              str(round(sf,2)).rjust(6) + flag)
    agent.close()
