"""
fusion_connection.py
--------------------
Talks directly to the FusionMCPBridge add-in HTTP server at localhost:7654.

Architecture:
    Claude Desktop <-> server.py (MCP stdio) -+
                                               +-> HTTP :7654 <-> FusionMCPBridge add-in <-> Fusion API
    Your Python agent -------------------------+

Both Claude Desktop and your agent talk to the SAME add-in endpoint.
server.py is just a relay for Claude -- your agent bypasses it entirely.

Usage:
    from fusion_connection import fusion_execute, fusion_screenshot

    result = fusion_execute("print(design.rootComponent.name)")
    print(result.stdout)
"""

from __future__ import annotations

import json
import time
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("fusion_connection")

# -- Bridge config --------------------------------------------------------------
FUSION_PORT = 7654
FUSION_BASE = f"http://127.0.0.1:{FUSION_PORT}"
SECRET_FILE = Path.home() / ".fusion-mcp-secret"
DEFAULT_TIMEOUT = 35.0
RETRY_COUNT = 3
RETRY_BACKOFF = 2.0


def _load_secret() -> str:
    """Load the shared secret token from ~/.fusion-mcp-secret"""
    try:
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        log.warning(
            "Secret file not found at %s -- requests will be unauthenticated.",
            SECRET_FILE
        )
        return ""


def _auth_headers() -> dict:
    secret = _load_secret()
    return {"Authorization": f"Bearer {secret}"} if secret else {}


# -- Result dataclass -----------------------------------------------------------

@dataclass
class FusionResult:
    stdout:  str
    success: bool
    error:   Optional[str] = None

    def raise_on_error(self):
        if not self.success:
            raise RuntimeError(f"Fusion script failed:\n{self.error}")

    def __str__(self):
        return self.stdout if self.success else f"ERROR: {self.error}"


# -- Health check ---------------------------------------------------------------

def is_bridge_running() -> bool:
    """Return True if the Fusion add-in HTTP server is reachable."""
    try:
        r = requests.get(
            f"{FUSION_BASE}/health",
            headers=_auth_headers(),
            timeout=3,
        )
        return r.status_code == 200
    except Exception:
        return False


def get_bridge_status() -> dict:
    """Return status info from the Fusion add-in health endpoint."""
    try:
        r = requests.get(
            f"{FUSION_BASE}/health",
            headers=_auth_headers(),
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# -- Core execution -------------------------------------------------------------

def fusion_execute(
    script: str,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = RETRY_COUNT,
    backoff: float = RETRY_BACKOFF,
) -> FusionResult:
    """
    Execute a Python script inside Fusion 360 via the add-in HTTP server.

    The script context provides:
        adsk    -- the adsk module
        app     -- adsk.core.Application.get()
        ui      -- app.userInterface
        design  -- active Fusion design, or None

    All geometry units are CENTIMETRES internally.
    Use print() to return data -- return values are ignored.
    """
    if not is_bridge_running():
        raise ConnectionError(
            f"FusionMCPBridge add-in is not reachable at {FUSION_BASE}.\n\n"
            "Checklist:\n"
            "  1. Fusion 360 is open\n"
            "  2. FusionMCPBridge add-in is running:\n"
            "       Fusion -> Shift+S -> Add-Ins tab -> FusionMCPBridge -> Run\n"
            "  3. Secret file exists at: ~/.fusion-mcp-secret\n"
        )

    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{FUSION_BASE}/execute",
                json={"script": script},
                headers=_auth_headers(),
                timeout=timeout,
            )

            if r.status_code == 401:
                raise PermissionError(
                    "Unauthorized (401) -- secret token mismatch.\n"
                    "Check that ~/.fusion-mcp-secret matches what the add-in expects.\n"
                    "Try regenerating it:\n"
                    "  python -c \"import secrets; "
                    "open(r'C:/Users/jansa/.fusion-mcp-secret','w')"
                    ".write(secrets.token_hex(32))\"\n"
                    "Then restart Fusion and re-run the add-in."
                )

            r.raise_for_status()
            data = r.json()

            # Add-in returns {"result": "stdout...", "error": null | "traceback"}
            output = data.get("result", "")
            error  = data.get("error")

            if error:
                log.warning("Fusion script error:\n%s", error)
                return FusionResult(stdout=output or "", success=False, error=error)

            return FusionResult(stdout=output, success=True)

        except (PermissionError, ConnectionError):
            raise  # don't retry auth or connection errors
        except requests.exceptions.Timeout:
            last_exc = TimeoutError(f"Fusion did not respond within {timeout}s")
        except Exception as e:
            last_exc = e

        wait = backoff ** attempt
        log.warning(
            "Attempt %d/%d failed: %s -- retrying in %.1fs",
            attempt + 1, retries, last_exc, wait
        )
        time.sleep(wait)

    raise RuntimeError(
        f"fusion_execute failed after {retries} attempts: {last_exc}"
    ) from last_exc


# -- Screenshot -----------------------------------------------------------------

def fusion_screenshot(
    direction: str = "iso-top-right",
    width: int = 1024,
    height: int = 768,
    save_path: Optional[str] = None,
) -> bytes:
    """Capture the active Fusion 360 viewport. Returns raw PNG bytes."""
    r = requests.post(
        f"{FUSION_BASE}/screenshot",
        json={"direction": direction, "width": width, "height": height},
        headers=_auth_headers(),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    import base64
    png_bytes = base64.b64decode(data["screenshot"])

    if save_path:
        with open(save_path, "wb") as f:
            f.write(png_bytes)
        log.info("Screenshot saved to %s", save_path)

    return png_bytes


# -- Convenience helpers --------------------------------------------------------

def get_design_info() -> dict:
    """Return basic info about the active Fusion design."""
    result = fusion_execute("""
import adsk.fusion
if design is None:
    print("NO_DESIGN")
else:
    mode = "parametric" if design.designType == adsk.fusion.DesignTypes.ParametricDesignType else "direct"
    print(f"name={design.rootComponent.name}")
    print(f"mode={mode}")
    print(f"bodies={design.rootComponent.bRepBodies.count}")
    print(f"version={app.version}")
""")
    result.raise_on_error()
    if "NO_DESIGN" in result.stdout:
        return {"status": "no_design"}
    info = {"status": "ok"}
    for line in result.stdout.strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    return info


def new_design(name: str = "Untitled") -> FusionResult:
    """Open a fresh Fusion design. Call before each blade variant build."""
    return fusion_execute(f"""
import adsk.core
doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
doc.name = "{name}"
print(f"New design opened: {{doc.name}}")
""")


# -- Quick test -----------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 50)
    print("Fusion 360 Connection Test")
    print("=" * 50)

    # 1. Secret file
    print(f"\n[1] Checking secret file at {SECRET_FILE} ...")
    if SECRET_FILE.exists():
        print(f"OK -- secret file found ({len(SECRET_FILE.read_text().strip())} chars)")
    else:
        print("FAIL -- secret file missing.")
        print("       Run this in Command Prompt to create it:")
        print("       python -c \"import secrets; open(r'C:/Users/jansa/.fusion-mcp-secret','w').write(secrets.token_hex(32))\"")
        exit(1)

    # 2. Bridge reachable
    print(f"\n[2] Checking add-in at {FUSION_BASE} ...")
    status = get_bridge_status()
    if "error" in status:
        print(f"FAIL -- {status['error']}")
        print("\nMake sure:")
        print("  - Fusion 360 is open")
        print("  - FusionMCPBridge add-in is running (Shift+S -> Add-Ins -> Run)")
        exit(1)
    print(f"OK -- Fusion {status.get('version','?')} | "
          f"Document: {status.get('documentName', 'none')} | "
          f"Has design: {status.get('hasDesign', False)}")

    # 3. Design info
    print("\n[3] Checking active design...")
    info = get_design_info()
    if info["status"] == "no_design":
        print("WARN -- No design open. Opening a new one...")
        new_design("ConnectionTest")
        info = get_design_info()
    print(f"OK -- '{info.get('name')}' | mode: {info.get('mode')} | "
          f"bodies: {info.get('bodies')}")

    # 4. Geometry test
    print("\n[4] Running geometry test (small box)...")
    result = fusion_execute("""
import adsk.core, adsk.fusion
root = design.rootComponent
sk   = root.sketches.add(root.xYConstructionPlane)
lines = sk.sketchCurves.sketchLines
lines.addTwoPointRectangle(
    adsk.core.Point3D.create(0, 0, 0),
    adsk.core.Point3D.create(1, 1, 0))
prof = sk.profiles.item(0)
ext  = root.features.extrudeFeatures.createInput(
    prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext.setDistanceExtent(False, adsk.core.ValueInput.createByReal(1.0))
root.features.extrudeFeatures.add(ext)
print(f"Bodies in design: {root.bRepBodies.count}")
print("geometry_ok")
""")
    if result.success and "geometry_ok" in result.stdout:
        print("OK -- geometry test passed")
    else:
        print(f"FAIL -- {result.error or result.stdout}")
        exit(1)

    print("\n" + "=" * 50)
    print("All tests passed -- fusion_connection.py is ready.")
    print("=" * 50)
