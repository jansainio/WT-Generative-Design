# Wind Turbine Blade Design Agent

A multi-agent pipeline that automates parametric wind turbine blade design using **Autodesk Fusion 360** and **ANSYS Mechanical** — connected through a custom MCP bridge and coordinated via a SQLite pipeline database.

The system generates multiple blade variants across a parameter sweep, exports them as geometry files, queues them for structural simulation, and logs all results to a persistent database with full audit trail.

---

## Architecture

```
Parameter sweep definition
         │
         ▼
  Design Agent ──────────────► Fusion 360
  (Pydantic + Jinja2)          (MCP bridge)
         │                          │
         │                    STEP export
         ▼                          │
   pipeline.db ◄────────────────────┘
  (SQLite — status machine)
         │
         ▼
 Simulation Agent ──────────► ANSYS Mechanical
  (PyANSYS embedded)          (Student v261)
         │
         ▼
   pipeline.db
  (results logged)
```

### Agents

| Agent | File | Responsibility |
|-------|------|---------------|
| Design Agent | `design_agent.py` | Validates parameters, renders Fusion script via Jinja2, executes in Fusion, exports STEP |
| Fusion Connection | `fusion_connection.py` | HTTP connection to Fusion MCP bridge with auth, retry logic, and health checks |
| Simulation Agent | `simulation_agent.py` | Launches ANSYS, imports geometry, meshes, applies boundary conditions, solves |

---

## Pipeline Database

All inter-agent communication flows through a single SQLite database (`pipeline.db`). The `status` field is the handoff mechanism — no direct coupling between agents.

| Status | Meaning |
|--------|---------|
| `pending` | Variant queued, not yet built in Fusion |
| `building` | Design agent is generating geometry |
| `ready` | STEP exported, awaiting simulation |
| `simulating` | Simulation agent has locked and is processing |
| `done` | Simulation complete, results logged |
| `failed` | Error at any stage — `error_msg` populated |

### Atomic locking

The `poll_and_lock()` function uses an atomic `SELECT + UPDATE` transaction to claim variants. This prevents race conditions when running multiple simulation agents in parallel — no variant can be processed twice.

```python
# Atomic: claim variant in a single transaction
db.execute("""
    UPDATE variants SET status='simulating'
    WHERE id = (
        SELECT id FROM variants WHERE status='ready' LIMIT 1
    )
""")
```

---

## Design Agent

### Parameter validation

Every blade variant is defined by a Pydantic model. Invalid parameters are caught before any Fusion API call.

```python
class BladeSection(BaseModel):
    z_mm:      float = Field(..., ge=0)
    chord_mm:  float = Field(..., gt=0)
    twist_deg: float
    sweep_mm:  float = Field(0.0)

class BladeVariant(BaseModel):
    variant_id: str
    span_mm:    float
    sections:   List[BladeSection]
```

### Jinja2 templating

Rather than building Fusion scripts dynamically by string concatenation, the agent renders a complete Python script from a Jinja2 template. The rendered script can be inspected directly for debugging.

```jinja2
import adsk.core, adsk.fusion

# Blade: {{ variant.variant_id }}
sections = [
    {% for s in variant.sections %}
    {"z_mm": {{ s.z_mm }}, "chord_mm": {{ s.chord_mm }}, "twist_deg": {{ s.twist_deg }}},
    {% endfor %}
]
```

### Parameter sweep

```python
variants = generate_sweep(
    twist_values=[25, 30, 35],   # root twist degrees
    chord_values=[16, 20],       # root chord mm
)
# Generates 6 variants: blade_t25_c16, blade_t25_c20, blade_t30_c16, ...
```

**Results — 7 variants generated and exported:**

| Variant | Root Chord | Root Twist | File Size |
|---------|-----------|-----------|-----------|
| blade_v001 | 18mm | 30° | 128 KB |
| blade_t25_c16 | 16mm | 25° | 82 KB |
| blade_t25_c20 | 20mm | 25° | 87 KB |
| blade_t30_c16 | 16mm | 30° | 77 KB |
| blade_t30_c20 | 20mm | 30° | 78 KB |
| blade_t35_c16 | 16mm | 35° | 76 KB |
| blade_t35_c20 | 20mm | 35° | 78 KB |

---

## Fusion 360 MCP Connection

The standard `fusion360-mcp-bridge` exposes a `stdio` relay for Claude Desktop. To call it from an external Python script, the connection layer bypasses `server.py` entirely and POSTs directly to the Fusion add-in endpoint at `localhost:7654`.

```python
requests.post(
    "http://127.0.0.1:7654/execute",
    json={"script": rendered_script},
    headers={"Authorization": f"Bearer {secret}"},
    timeout=30,
)
```

**Key compatibility issue:** The bridge uses IronPython 2 internally for script execution inside Fusion. All scripts passed via `run_python_script()` must avoid Python 3 syntax — no f-strings, no walrus operators, no type hints. The design agent uses `app.update_globals()` to access Fusion API objects directly, which avoids the IronPython 2 constraint entirely.

---

## Simulation Agent

The simulation agent connects to ANSYS Mechanical 2026 R1 Student via the PyANSYS embedded `App()` interface. This runs ANSYS Mechanical directly inside the Python process, avoiding the gRPC connection issues with the `launch_mechanical()` remote session approach.

### Environment setup

PyANSYS requires Python 3.10–3.12 due to `ansys-pythonnet` — no build exists for Python 3.14. An isolated virtual environment handles this:

```bash
python3.12 -m venv ansys-env
ansys-env\Scripts\activate
pip install ansys-mechanical-core
```

### Current status

All 7 variants complete the full pipeline — IGES import, mesh generation, boundary condition setup, solve. Result extraction returns zero values due to an issue with the `EvaluateAllResults()` call sequence in PyANSYS v261. This is a known limitation documented in the project.

| Stage | Status |
|-------|--------|
| IGES geometry import | ✅ Working |
| Automatic mesh generation | ✅ Working |
| Fixed support + pressure load | ✅ Working |
| Solve | ✅ Working |
| Result extraction | ⚠️ Returns zero — under investigation |

---

## Requirements

```bash
# Design agent
pip install requests pydantic jinja2

# Simulation agent (Python 3.12 venv)
pip install ansys-mechanical-core
```

**External dependencies:**
- Autodesk Fusion 360 with [fusion360-mcp-bridge](https://github.com/fusion360-mcp-bridge) add-in
- ANSYS Mechanical Student 2026 R1

---

## Usage

```bash
# Run design agent — generates all variants
python design_agent.py

# Reset pipeline database
python reset.py

# Run simulation agent
python simulation_agent.py

# Check results
python -c "
import sqlite3
db = sqlite3.connect('pipeline.db')
for row in db.execute('SELECT variant_id, status, max_stress_pa FROM variants'):
    print(row)
"
```

---

## Key Engineering Decisions

**Why SQLite as the communication layer**  
All inter-agent communication flows through a SQLite database rather than direct API calls or a message queue. This gives the pipeline a complete, queryable audit trail of every variant at every stage. Any agent can crash and restart without losing state.

**Why Jinja2 templating over dynamic script building**  
String concatenation for code generation is fragile — any parameter with a special character can silently break the script. Jinja2 templates separate structure from data, produce inspectable output, and make supporting multiple component types as simple as adding a new template file.

**Why IGES instead of STEP for ANSYS import**  
STEP files cause a geometry editor crash in ANSYS Workbench on complex lofted geometry. IGES imports correctly. This was diagnosed by manually testing both formats in the ANSYS GUI before implementing the automated import.

**Why an isolated Python 3.12 environment for ANSYS**  
PyANSYS has no wheel for Python 3.14. Rather than downgrading the system Python, an isolated `venv` keeps ANSYS dependencies fully separated. The environment is reproducible from a single `requirements.txt`.

---

## Planned Extensions

The pipeline foundation supports a full closed-loop optimisation system. Planned but not implemented:

- **Analysis agent** — reads results, computes fitness score (weighted: safety factor, max deformation, mass), ranks variants
- **Orchestrator** — replaces fixed parameter sweep with Bayesian optimisation, drives iteration toward optimal design
- **Surrogate model** — after initial ANSYS batch, trains a Gaussian Process to predict stress/deformation from parameters. Reduces full ANSYS solves from hundreds to tens.
- **Abstraction layer** — component description schema and simulation description schema to make the pipeline generic across component types and simulation types

---

## Project Background

Built as part of an AI orchestration portfolio project. The pipeline demonstrates multi-agent coordination patterns — atomic database handoffs, parametric code generation, commercial CAD/FEA integration — in a real engineering context. The domain knowledge required to make sensible choices about airfoil profiles, blade twist, structural boundary conditions, and simulation parameters comes from a mechanical engineering and machining background.
