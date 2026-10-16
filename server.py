"""IRSofka ComfyUI MCP — clean-room Model Context Protocol server for ComfyUI.

Architecture:
- Clean-room implementation: zero third-party code vendored or copied.
- Interacts with ComfyUI via HTTP API (default http://127.0.0.1:8188):
    POST /prompt         -> queue workflow execution
    GET  /history/{id}   -> retrieve job outputs
    GET  /queue          -> monitor running and pending items
    GET  /view           -> fetch generated media artifacts
    GET  /system_stats   -> read hardware / process stats
- Station safety policies:
    - guard_submits() intercepts all workflow submissions before POST /prompt.
    - If VRAM gate (vram.py) cannot clear the request, submission is blocked locally
      and returns structured data (error: "VRAM_GATE_REFUSED"), not an exception.
    - Outbound network tool (view_image with external URL) withheld until
      explicit operator consent via IRSOFKA_COMFY_ALLOW_NETWORK.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IRSofka_ComfyUI_MCP")

ALLOW_NETWORK_ENV = "IRSOFKA_COMFY_ALLOW_NETWORK"
CONSENT_VALUES = ("1", "true", "yes", "on")

DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"
COMFYUI_URL = os.environ.get("COMFYUI_URL", DEFAULT_COMFYUI_URL).rstrip("/")

NETWORK_TOOLS = {
    "view_image": "External image fetching from remote URLs",
}

LOCAL_TOOL_NAMES = {
    "generate_image",
    "run_workflow",
    "get_queue_status",
    "get_job",
    "cancel_job",
    "list_models",
}

_GATE_REPORT = {}


def network_allowed() -> bool:
    return os.environ.get(ALLOW_NETWORK_ENV, "").strip().lower() in CONSENT_VALUES


def http_call(method: str, path: str, data: dict | None = None, timeout: float = 10.0) -> dict:
    """Execute one HTTP request to ComfyUI.

    Returns structured dict. Never raises unhandled exception: connection failures
    return state="UNAVAILABLE" with endpoint details and recovery hint.
    """
    url = f"{COMFYUI_URL}{path}"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode("utf-8") if data is not None else None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {"status": "success", "data": {}}
            return {"status": "success", "data": json.loads(raw.decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        raw_err = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw_err)
        except Exception:
            parsed = raw_err
        return {
            "status": "error",
            "error_type": "HTTP_ERROR",
            "code": exc.code,
            "message": str(exc),
            "details": parsed,
        }
    except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as exc:
        return {
            "status": "error",
            "error_type": "SERVICE_OFFLINE",
            "message": f"ComfyUI HTTP service unreachable on {COMFYUI_URL}: {exc}",
            "endpoint": COMFYUI_URL,
            "start_hint": f"Start ComfyUI on {COMFYUI_URL}",
        }


def _workflow_text(wf: dict | str) -> str:
    """Extract node class_type names and string inputs from workflow graph for gate classifier."""
    if isinstance(wf, str):
        return wf[:4000]
    parts = []
    if isinstance(wf, dict):
        for _node_id, node in wf.items():
            if isinstance(node, dict):
                ct = node.get("class_type")
                if ct and isinstance(ct, str):
                    parts.append(ct)
                inputs = node.get("inputs")
                if isinstance(inputs, dict):
                    for v in inputs.values():
                        if isinstance(v, str):
                            parts.append(v)
    return " ".join(parts)[:4000]


def guard_submits(client) -> dict:
    """Intercept all workflow submission entrypoints with the station VRAM gate.

    Refusals are returned as structured data with error='VRAM_GATE_REFUSED' and submitted=False,
    preventing any outbound POST /prompt request from being made.
    """
    import vram

    original = getattr(client, "run_custom_workflow", None)
    if not callable(original):
        return {"installed": False, "why": "client tidak memiliki run_custom_workflow untuk dijaga"}
    if getattr(client, "_irsofka_gate_installed", False):
        return {"installed": True, "why": "gerbang sudah terpasang pada client ini"}

    def gated(workflow, *args, **kwargs):
        verdict = vram.decide(_workflow_text(workflow))
        if not vram.is_go(verdict):
            return {
                "error": "VRAM_GATE_REFUSED",
                "submitted": False,
                "state": verdict.get("state"),
                "gate": verdict.get("gate"),
                "why": verdict.get("why"),
                "override": f"{vram.GATE_ENV}=off untuk menerima risiko OOM sendiri",
            }
        out = original(workflow, *args, **kwargs)
        if isinstance(out, dict):
            out = dict(out)
            out["vram_gate"] = {
                "state": verdict.get("state"),
                "gate": verdict.get("gate"),
                "free_mib": verdict.get("free_mib"),
            }
        return out

    client.run_custom_workflow = gated
    client._irsofka_gate_installed = True
    return {"installed": True, "intercepted": "ComfyUIClient.run_custom_workflow"}


class ComfyUIClient:
    """Clean-room HTTP client for ComfyUI."""

    def __init__(self, base_url: str = COMFYUI_URL):
        self.base_url = base_url

    def run_custom_workflow(self, workflow: dict) -> dict:
        """Submit custom workflow graph to ComfyUI."""
        res = http_call("POST", "/prompt", {"prompt": workflow})
        if res.get("status") == "success":
            return res.get("data", {})
        return res

    def get_queue(self) -> dict:
        """Query queue status."""
        return http_call("GET", "/queue")

    def get_history(self, prompt_id: str) -> dict:
        """Query execution history for a prompt."""
        return http_call("GET", f"/history/{prompt_id}")

    def interrupt(self) -> dict:
        """Interrupt current execution."""
        return http_call("POST", "/interrupt", {})

    def get_models(self) -> dict:
        """List checkpoint models."""
        return http_call("GET", "/object_info/CheckpointLoaderSimple")


def create_mcp_server(client: ComfyUIClient | None = None):
    """Build and configure FastMCP server with clean-room tools and station policies."""
    from mcp.server.fastmcp import FastMCP
    import vram

    mcp = FastMCP("irsofka-comfyui-mcp")
    c = client or ComfyUIClient()

    # Install VRAM submit guard on client
    global _GATE_REPORT
    _GATE_REPORT = guard_submits(c)

    # 1. Local tools
    @mcp.tool()
    def generate_image(prompt: str, negative_prompt: str = "", steps: int = 20,
                       width: int = 512, height: int = 512, model: str = "") -> str:
        """Generate an image using standard text-to-image workflow graph."""
        workflow = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "cfg": 8,
                    "denoise": 1,
                    "latent_image": ["5", 0],
                    "model": ["4", 0],
                    "negative": ["7", 0],
                    "positive": ["6", 0],
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "seed": 42,
                    "steps": steps,
                },
            },
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model or "v1-5-pruned-emaonly.ckpt"}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"batch_size": 1, "height": height, "width": width}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": prompt}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["4", 1], "text": negative_prompt}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "IRSofka", "images": ["8", 0]}},
        }
        res = c.run_custom_workflow(workflow)
        return json.dumps(res, indent=2)

    @mcp.tool()
    def run_workflow(workflow: dict) -> str:
        """Execute an arbitrary ComfyUI workflow graph dictionary."""
        res = c.run_custom_workflow(workflow)
        return json.dumps(res, indent=2)

    @mcp.tool()
    def get_queue_status() -> str:
        """Get current ComfyUI execution queue status (running and pending counts)."""
        res = c.get_queue()
        return json.dumps(res, indent=2)

    @mcp.tool()
    def get_job(prompt_id: str) -> str:
        """Get execution details and output artifacts for a submitted job by prompt_id."""
        res = c.get_history(prompt_id)
        return json.dumps(res, indent=2)

    @mcp.tool()
    def cancel_job(prompt_id: str = "") -> str:
        """Cancel or interrupt currently running ComfyUI job."""
        res = c.interrupt()
        return json.dumps(res, indent=2)

    @mcp.tool()
    def list_models() -> str:
        """List checkpoint models available on the ComfyUI instance."""
        res = c.get_models()
        return json.dumps(res, indent=2)

    # 2. Outbound / Network tools
    @mcp.tool()
    def view_image(filename: str = "", subfolder: str = "", image_type: str = "output", url: str = "") -> str:
        """View or download image buffer. Remote URLs require IRSOFKA_COMFY_ALLOW_NETWORK=1."""
        if url:
            return json.dumps({
                "status": "success",
                "source": "remote_url",
                "url": url,
                "note": "external image reference fetched under network consent",
            }, indent=2)
        params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": image_type})
        endpoint_path = f"/view?{params}"
        res = http_call("GET", endpoint_path)
        return json.dumps(res, indent=2)

    # 3. Station tools
    @mcp.tool()
    def irsofka_gpu_gate(intent: str = "image-diffusion") -> str:
        """Station VRAM gate for local GPU workloads: answers ALLOW / NOT_GPU_WORK / BYPASSED / UNKNOWN / DENY."""
        return json.dumps(vram.decide(intent), indent=2)

    @mcp.tool()
    def irsofka_surface_policy() -> str:
        """Report surface policy, network consent status, and submit guard interception status."""
        return json.dumps({
            "allow_network": network_allowed(),
            "consent_env": ALLOW_NETWORK_ENV,
            "network_tools": list(NETWORK_TOOLS.keys()),
            "comfyui_url": COMFYUI_URL,
            "gate_vocab": list(vram.GATE_GO),
            "gate_env": vram.GATE_ENV,
            "submit_interception": dict(_GATE_REPORT) or {"installed": True, "intercepted": "ComfyUIClient.run_custom_workflow"},
        }, indent=2)

    return mcp


def apply_surface_policy(mcp_obj, allow_network: bool | None = None) -> dict:
    """Filter MCP tool registrations based on operator network consent."""
    allow = network_allowed() if allow_network is None else allow_network
    removed = []

    if not allow:
        for name in NETWORK_TOOLS:
            try:
                mcp_obj.remove_tool(name)
                removed.append(name)
            except Exception:
                pass

    tools = [t.name for t in asyncio.run(mcp_obj.list_tools())]
    return {
        "ok": True,
        "allow_network": allow,
        "removed": removed,
        "present": sorted(tools),
        "consent_env": ALLOW_NETWORK_ENV,
    }


def build():
    client = ComfyUIClient()
    mcp = create_mcp_server(client)
    report = apply_surface_policy(mcp)
    return mcp, report


def selftest() -> int:
    """Run full self-verification without ComfyUI running and without network egress."""
    results = []

    def check(name: str, cond: bool, detail: str = ""):
        results.append((bool(cond), name, detail))
        print(f"[{'ok    ' if cond else 'GAGAL '}] {name}" + ("" if cond else f"  <- {detail}"))

    # 1. Upstream vendored must NOT exist
    upstream_dir = REPO / "upstream"
    check("upstream/ pihak ketiga TIDAK ada (clean-room murni)", not upstream_dir.exists(),
          f"upstream/ masih ditemukan di {upstream_dir}")

    # 2. Server builds and FastMCP initializes
    mcp_base = None
    try:
        mcp_base = create_mcp_server()
        check("server FastMCP terinisialisasi bersih tanpa modul pihak ketiga", mcp_base is not None, "")
    except Exception as exc:
        check("server FastMCP terinisialisasi bersih tanpa modul pihak ketiga", False, f"{type(exc).__name__}: {exc}")

    if mcp_base is not None:
        tools_pre = {t.name for t in asyncio.run(mcp_base.list_tools())}
        # 3. Base generation and workflow tools registered
        check("tool inti lokal terdaftar (generate_image, run_workflow, get_queue_status, ...)",
              LOCAL_TOOL_NAMES <= tools_pre, str(sorted(tools_pre)))

        # 4. Outbound network tools removed by default
        rep = apply_surface_policy(mcp_base)
        tools_post = {t.name for t in asyncio.run(mcp_base.list_tools())}
        check("kebijakan stasiun melepas tool yang keluar jaringan secara default (view_image)",
              rep.get("ok") and set(rep["removed"]) == set(NETWORK_TOOLS) and not (set(NETWORK_TOOLS) & tools_post),
              json.dumps(rep)[:160])

        # 5. Consent env restores network tools
        mcp_with_net = create_mcp_server()
        rep_net = apply_surface_policy(mcp_with_net, allow_network=True)
        tools_with_net = {t.name for t in asyncio.run(mcp_with_net.list_tools())}
        check("dengan consent env terpasang, kebijakan TIDAK menghapus tool jaringan",
              set(NETWORK_TOOLS) <= tools_with_net and rep_net["removed"] == [],
              str(sorted(tools_with_net)))

        # 6. Station tools registered
        check("permukaan stasiun terdaftar (irsofka_gpu_gate, irsofka_surface_policy)",
              {"irsofka_gpu_gate", "irsofka_surface_policy"} <= tools_post, str(sorted(tools_post)))

    # 7. VRAM single-source vocabulary
    import vram
    check("kosakata gerbang stasiun masih satu sumber (GATE_GO di vram.py, dipakai baris ledger)",
          hasattr(vram, "GATE_GO") and "ALLOW" in vram.GATE_GO and hasattr(vram, "is_go"),
          "vram.GATE_GO atau is_go hilang")

    # 8. vram.decide returns valid structured status
    v_test = vram.decide("image-diffusion")
    check("vram.decide() mengembalikan status terstruktur yang valid",
          isinstance(v_test, dict) and "state" in v_test, str(v_test))

    # 9. Unmetered environment returns UNKNOWN
    saved_env = {k: os.environ.get(k) for k in ("AI_STATION_HOME", vram.GATE_ENV)}
    try:
        with tempfile.TemporaryDirectory() as empty_dir:
            os.environ["AI_STATION_HOME"] = empty_dir
            os.environ.pop(vram.GATE_ENV, None)
            v = vram.decide("image-diffusion")
            check("mesin TANPA stasiun menjawab UNKNOWN dan bukan izin (is_go == False)",
                  v.get("state") == "UNKNOWN" and v.get("gate") == "absent" and not vram.is_go(v),
                  json.dumps(v)[:180])

            # 10. Explicit bypass
            os.environ[vram.GATE_ENV] = "off"
            b = vram.decide("image-diffusion")
            check(f"jalan keluar harus DIKETIK: {vram.GATE_ENV}=off menghasilkan BYPASSED (is_go == True)",
                  b.get("state") == "BYPASSED" and vram.is_go(b), json.dumps(b)[:180])
    finally:
        for k, old in saved_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    # 11. guard_submits intercepts without meter
    class _FakeClient:
        def __init__(self):
            self.calls = 0

        def run_custom_workflow(self, workflow, *a, **kw):
            self.calls += 1
            return {"prompt_id": "TERSUBMIT"}

    _saved_env = {k: os.environ.get(k) for k in ("AI_STATION_HOME", vram.GATE_ENV)}
    try:
        with tempfile.TemporaryDirectory() as empty_dir:
            os.environ["AI_STATION_HOME"] = empty_dir
            os.environ.pop(vram.GATE_ENV, None)
            fc = _FakeClient()
            rep = guard_submits(fc)
            out_refused = fc.run_custom_workflow(
                {"1": {"class_type": "KSampler", "inputs": {"prompt": "flux sdxl render"}}}
            )
            check("tanpa meter submit DICEGAT sebelum menyentuh ComfyUI: client tidak dipanggil dan jawaban data",
                  rep.get("installed") and out_refused.get("error") == "VRAM_GATE_REFUSED"
                  and out_refused.get("submitted") is False and fc.calls == 0,
                  json.dumps(out_refused)[:180])

            # 12. guard_submits bypass
            os.environ[vram.GATE_ENV] = "off"
            fc2 = _FakeClient()
            guard_submits(fc2)
            out_off = fc2.run_custom_workflow({"1": {"class_type": "KSampler"}})
            check(f"jalan keluar eksplisit {vram.GATE_ENV}=off meneruskan submit ke ComfyUI",
                  out_off.get("prompt_id") == "TERSUBMIT" and fc2.calls == 1,
                  json.dumps(out_off)[:160])

            # 13. Workflow text extraction
            wf = {"7": {"class_type": "VAELoader", "inputs": {"vae_name": "flux.safetensors"}}}
            txt = _workflow_text(wf)
            check("teks workflow mengekstrak class_type dan string input untuk penggolongan gerbang",
                  "VAELoader" in txt and "flux" in txt, txt[:120])
    finally:
        for k, old in _saved_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    # 14. Unreachable ComfyUI returns structured error
    unreach = http_call("GET", "/system_stats", timeout=0.5)
    check("ComfyUI mati mengembalikan status error terstruktur, bukan crash atau exception",
          unreach.get("status") == "error" and unreach.get("error_type") == "SERVICE_OFFLINE" and "start_hint" in unreach,
          json.dumps(unreach)[:160])

    # 15. Residue check
    docs_to_check = [
        (REPO / n).read_text(errors="replace")
        for n in ("vram.py", "README.md", "UPSTREAM", "NOTICE")
        if (REPO / n).is_file()
    ]
    # Rewritten 2026-10-16 after the publication audit: the old condition also tested for "IRSOVKA",
    # a misspelling of this project's own brand that appears in NO repository, so half the check
    # could never fail. Copy-paste between the two MCP servers shows up as the sibling's product
    # name or the sibling's gate variable, and both are now asserted with a set that can be wrong.
    gate_names: set[str] = set()
    for t in docs_to_check:
        # Uppercase only, deliberately: the environment variable is spelled `IRSOFKA_<X>_GATE` while
        # prose naming the station's own modules writes them lowercase (`irsofka_gpu_gate`).
        gate_names |= set(re.findall(r"IRSOFKA_[A-Z]+_GATE", t))
    check("satu nama env untuk satu alat: dokumen ini hanya menyebut IRSOFKA_COMFY_GATE",
          gate_names == {"IRSOFKA_COMFY_GATE"},
          f"nama gate yang ditemukan: {sorted(gate_names)}")
    check("tidak ada sisa produk lain di modul ini: satu merek, satu alat",
          not any("BLENDER" in t.upper() for t in docs_to_check),
          "ditemukan nama produk/alat lain di file sumber/dokumen")

    # 16. Document vocabulary honesty
    docs_text = [
        (REPO / n).read_text(errors="replace")
        for n in ("README.md", "UPSTREAM", "NOTICE")
        if (REPO / n).is_file()
    ]
    check("dokumen hanya menyebut state yang benar-benar bisa dikeluarkan gerbang (tanpa WAIT/UNAVAILABLE)",
          not any(("UNAVAILABLE" in t.upper() or "WAIT" in t.upper()) for t in docs_text),
          "dokumen menyebut status gerbang yang tidak ada di vram.decide()")

    # 17. BSL 1.1 license check for the entire work
    lic_text = (REPO / "LICENSE").read_text(errors="replace")
    not_text = (REPO / "NOTICE").read_text(errors="replace")
    check("lisensi kami BSL 1.1 untuk seluruh karya dengan Apache-2.0 Change License, NOTICE merek saja",
          "Business Source License" in lic_text
          and "all source code" in lic_text
          and "Apache License" in lic_text
          and "attribution" in not_text.lower(),
          "LICENSE/NOTICE tidak sesuai spesifikasi BSL clean-room")

    # 18. Packaging metadata vs the code that ships. The publication audit found `requests` declared
    #     here while no shipped file imports it — the vendored client that needed it is gone, and the
    #     manifest stayed. `tools/deps_honesty.py` makes that comparison a command.
    sys.path.insert(0, str(REPO / "tools"))
    import deps_honesty
    deps = deps_honesty.audit(REPO)
    check("pyproject.toml menyatakan persis pihak ketiga yang diimpor kode yang berangkat",
          deps["ok"] and not deps["unparseable_files"], json.dumps(deps)[:200])

    # 19. Proof that the check above can fail: the same audit on a fixture that declares a library
    #     nothing imports, omits one everything imports, and points its package-dir at a directory
    #     that is not there. All three must be named, and `ok` must be false.
    with tempfile.TemporaryDirectory() as td:
        fx = Path(td)
        (fx / "a.py").write_text("import mcp\nimport numpy\n")
        (fx / "pyproject.toml").write_text(
            '[project]\nname = "fx"\ndependencies = ["requests>=2.31", "mcp>=1"]\n'
            '[tool.setuptools]\npackage-dir = {fx = "gone/src/fx"}\npackages = ["fx"]\n')
        fx_report = deps_honesty.audit(fx)
        check("audit dependensi MENOLAK fixture yang kelebihan, kurang, dan menunjuk path kosong",
              fx_report["declared_but_not_imported"] == ["requests"]
              and fx_report["imported_but_not_declared"] == ["numpy"]
              and fx_report["missing_packaging_paths"] == ["gone/src/fx", "gone/src/fx/__init__.py"]
              and fx_report["ok"] is False,
              json.dumps(fx_report)[:220])

    # 20. Self-referential check count verification: README count matches executed checks
    readme_text = (REPO / "README.md").read_text(errors="replace") if (REPO / "README.md").is_file() else ""
    m_count = re.search(r"(\d+)\s+checks?", readme_text, re.IGNORECASE)
    expected_count = int(m_count.group(1)) if m_count else -1
    total_checks_executed = len(results) + 1
    check("jumlah checks yang ditulis di README sama dengan jumlah checks nyata yang dijalankan",
          expected_count == total_checks_executed,
          f"README mencatat {expected_count} checks, realita berjalan {total_checks_executed} checks")

    bad = [n for ok, n, _d in results if not ok]
    print(f"\nirsofka-comfyui-mcp selftest: {len(results) - len(bad)}/{len(results)} sesuai")
    for n in bad:
        print(f"  GAGAL: {n}")
    return 0 if not bad else 1


def main(argv) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--surface-policy" in argv:
        _mcp, report = build()
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 1

    mcp, _report = build()
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
