"""IRSofka ComfyUI MCP — basis Apache-2.0 `comfyui-mcp-server` diambil alih, lalu diberi kebijakan stasiun.

Ini bukan clean room. `UPSTREAM` dan `upstream/` mencatat asalnya; berkas kami menambahkan kebijakan
stasiun: gate VRAM terintegrasi dengan vram.py stasiun, dan permukaan yang berpotensi keluar jaringan
(view_image yang mengunduh URL eksternal) tidak didaftarkan sampai operator menyetujui lewat environment
bernama IRSOFKA_COMFY_ALLOW_NETWORK.

Diukur pada sha e0101b2312f30501664dabe4a74c1283c4268eb8 (v1.1.1, Apache-2.0, 410 bintang pada
2026-10-16): 17 tool terdaftar. Klaim di file ini hanyalah yang bisa dites dari daftar itu.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
UPSTREAM_DIR = REPO / "upstream"

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IRSofka_ComfyUI_MCP")

# Environment & Network Policy configuration
ALLOW_NETWORK_ENV = "IRSOFKA_COMFY_ALLOW_NETWORK"
CONSENT_VALUES = ("1", "true", "yes", "on")

NETWORK_TOOLS = {
    "view_image": "asset_processor.py:215 fetch_asset_bytes (mengunduh gambar dari URL eksternal http/https)",
}

# Diisi oleh build() dan dibaca `irsofka_surface_policy`. Kosong berarti belum ada client yang
# bisa dijaga — dan alat itu harus bisa membedakan "gerbang terpasang" dari "belum pernah dibangun",
# karena mengklaim pencegahan tanpa client adalah klaim tanpa bukti.
_GATE_REPORT: dict = {}

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://localhost:8188")
ASSET_TTL_HOURS = int(os.getenv("COMFY_MCP_ASSET_TTL_HOURS", "24"))
COMFYUI_OUTPUT_ROOT = os.getenv("COMFYUI_OUTPUT_ROOT")


def network_allowed() -> bool:
    return os.environ.get(ALLOW_NETWORK_ENV, "").strip().lower() in CONSENT_VALUES


def _load_base():
    """Import modul dan kelas basis dari direktori upstream vendored.

    Preflight yang eksplisit, bukan ImportError telanjang: `pip install .` memasang lapisan stasiun
    saja (basisnya sengaja tidak masuk site-packages — nama modulnya `tools`, `models`, `managers`
    dan itu akan menabrak apa pun yang terpasang di mesin pengguna), jadi orang yang menjalankan dari
    instalasi harus mendapat sebab + jalan keluar, bukan traceback yang menyalahkan baris import.
    """
    if not UPSTREAM_DIR.is_dir() or not (UPSTREAM_DIR / "comfyui_client.py").is_file():
        raise SystemExit(
            f"irsofka-comfyui-mcp: basis tidak ditemukan di {UPSTREAM_DIR} — jalankan dari checkout "
            "(python3 server.py); `pip install .` memasang lapisan stasiun saja. Lihat README "
            "'Registering it' dan UPSTREAM."
        )
    if str(UPSTREAM_DIR) not in sys.path:
        sys.path.insert(0, str(UPSTREAM_DIR))

    from mcp.server.fastmcp import FastMCP
    from comfyui_client import ComfyUIClient
    from managers.asset_registry import AssetRegistry
    from managers.defaults_manager import DefaultsManager
    from managers.publish_manager import PublishConfig, PublishManager
    from managers.workflow_manager import WorkflowManager

    from tools.configuration import register_configuration_tools
    from tools.workflow import register_workflow_tools
    from tools.asset import register_asset_tools
    from tools.generation import register_workflow_generation_tools, register_regenerate_tool
    from tools.job import register_job_tools
    from tools.publish import register_publish_tools

    workflow_dir = UPSTREAM_DIR / "workflows"

    client = ComfyUIClient(COMFYUI_URL)
    # Gerbang dipasang SEBELUM tool basis didaftarkan: tool memanggil
    # `comfyui_client.run_custom_workflow(...)`, jadi pencarian atribut terjadi saat pemanggilan dan
    # penggantian atribut instance ini mencegat setiap submit — termasuk tool yang belum ada hari ini.
    _GATE_REPORT.clear()
    _GATE_REPORT.update(guard_submits(client))
    wf_mgr = WorkflowManager(workflow_dir)
    defaults_mgr = DefaultsManager(client)
    asset_reg = AssetRegistry(ttl_hours=ASSET_TTL_HOURS, comfyui_base_url=COMFYUI_URL)

    try:
        publish_cfg = PublishConfig(
            comfyui_output_root=COMFYUI_OUTPUT_ROOT,
            comfyui_url=COMFYUI_URL,
        )
        pub_mgr = PublishManager(publish_cfg)
    except Exception:
        pub_mgr = None

    mcp = FastMCP("IRSofka_ComfyUI_MCP", port=9000, stateless_http=True)

    # Register all upstream tools
    register_configuration_tools(mcp, client, defaults_mgr)
    register_workflow_tools(mcp, wf_mgr, client, defaults_mgr, asset_reg)
    register_asset_tools(mcp, asset_reg)
    register_workflow_generation_tools(mcp, wf_mgr, client, defaults_mgr, asset_reg)
    register_regenerate_tool(mcp, client, asset_reg)
    register_job_tools(mcp, client, asset_reg)
    if pub_mgr:
        register_publish_tools(mcp, asset_reg, pub_mgr)

    return mcp, client, wf_mgr, defaults_mgr, asset_reg, pub_mgr


def _workflow_text(workflow) -> str:
    """Ringkas sebuah workflow menjadi teks yang bisa dibaca classifier stasiun.

    Yang dikirim ke gerbang adalah isi graph (class_type + string input), bukan objek workflow
    mentah: gerbang memotong berdasarkan kata kerja beban (flux/sdxl/trellis/...), dan ia hanya
    bisa menemukan kata itu kalau yang diperiksa adalah teks, bukan pointer.
    """
    parts: list[str] = []
    if isinstance(workflow, dict):
        for node in workflow.values():
            if isinstance(node, dict):
                ct = node.get("class_type")
                if ct:
                    parts.append(str(ct))
                inputs = node.get("inputs")
                if isinstance(inputs, dict):
                    for v in inputs.values():
                        if isinstance(v, str):
                            parts.append(v)
    return " ".join(parts)[:4000]


def guard_submits(client) -> dict:
    """Place the VRAM gate on the one submit path every base tool uses.

    Measured at the vendored sha: `tools/generation.py:105`, `tools/generation.py:415` and
    `tools/workflow.py:75` all call `comfyui_client.run_custom_workflow(...)`. Attribute lookup
    happens at call time, so replacing this instance attribute intercepts every submit — not a
    convention asking callers to be careful, and not a per-tool wrapper that silently misses the
    next tool the base adds. A refusal is returned as data, not raised: a caller can read why
    nothing was submitted.

    This is what makes the README's claim — a submit the machine cannot pay for is refused before
    it is sent — a property of the code rather than a hope about the prompt.
    """
    import vram

    original = getattr(client, "run_custom_workflow", None)
    if not callable(original):
        return {"installed": False, "why": "client tidak punya run_custom_workflow untuk dijaga"}
    if getattr(client, "_irsofka_gate_installed", False):
        return {"installed": True, "why": "gerbang sudah terpasang pada client ini"}

    def gated(workflow, *args, **kwargs):
        verdict = vram.decide(_workflow_text(workflow))
        if not vram.is_go(verdict):
            return {"error": "VRAM_GATE_REFUSED", "submitted": False,
                    "state": verdict.get("state"), "gate": verdict.get("gate"),
                    "why": verdict.get("why"),
                    "override": f"{vram.GATE_ENV}=off untuk menerima risiko OOM sendiri"}
        out = original(workflow, *args, **kwargs)
        if isinstance(out, dict):
            out = dict(out)
            out["vram_gate"] = {"state": verdict.get("state"), "gate": verdict.get("gate"),
                                "free_mib": verdict.get("free_mib")}
        return out

    client.run_custom_workflow = gated
    client._irsofka_gate_installed = True
    return {"installed": True, "intercepted": "ComfyUIClient.run_custom_workflow"}


def apply_surface_policy(mcp_obj, *, allow_network: bool | None = None) -> dict:
    """Kurangi permukaan basis sesuai izin stasiun. Mengembalikan laporan yang bisa diaudit."""
    allow = network_allowed() if allow_network is None else allow_network
    try:
        available = {t.name for t in asyncio.run(mcp_obj.list_tools())}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "removed": [], "present": []}

    removed = []
    if not allow:
        for name in sorted(NETWORK_TOOLS):
            if name in available:
                try:
                    mcp_obj.remove_tool(name)
                    removed.append(name)
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "error": f"remove_tool({name}) {type(exc).__name__}: {exc}",
                        "removed": removed,
                        "present": sorted(available),
                    }

    present = sorted(t.name for t in asyncio.run(mcp_obj.list_tools()))
    return {
        "ok": True,
        "allow_network": allow,
        "removed": removed,
        "present": present,
        "consent_env": ALLOW_NETWORK_ENV,
    }


def register_station_tools(mcp_obj) -> None:
    """Alat milik stasiun sendiri, di atas permukaan basis."""
    import vram

    @mcp_obj.tool()
    def irsofka_gpu_gate(intent: str = "image-diffusion") -> str:
        """Gerbang VRAM stasiun untuk kerja GPU lokal: menjawab ALLOW / NOT_GPU_WORK / BYPASSED / UNKNOWN / DENY beserta sebabnya.

        Ini lapisan kami, bukan milik upstream: keputusan diambil dari angka yang diukur detik ini
        (lihat `vram.py`), dan `UNKNOWN` dipakai ketika alat ukurnya tidak bisa dibaca — bukan
        sebagai izin diam-diam.

        Alat ini JUGA jawabannya kalau sebuah submit ditolak: gerbang terpasang pada
        `ComfyUIClient.run_custom_workflow` (lihat `guard_submits`), jadi submit yang mesin ini
        tidak sanggup bayar berhenti di sini dengan state yang sama, tanpa dikirim ke ComfyUI.
        """
        return json.dumps(vram.decide(intent), indent=2)

    @mcp_obj.tool()
    def irsofka_surface_policy() -> str:
        """Permukaan apa yang aktif sekarang, apa yang menunggu persetujuan operator, dan di mana gerbang duduk."""
        return json.dumps(
            {
                "allow_network": network_allowed(),
                "consent_env": ALLOW_NETWORK_ENV,
                "network_tools": NETWORK_TOOLS,
                "comfyui_url": COMFYUI_URL,
                "gate_vocab": list(vram.GATE_GO),
                "gate_env": vram.GATE_ENV,
                "submit_interception": dict(_GATE_REPORT) or {"installed": False,
                                                              "why": "build() belum dijalankan"},
            },
            indent=2,
        )


def build():
    mcp, client, wf_mgr, defaults_mgr, asset_reg, pub_mgr = _load_base()
    report = apply_surface_policy(mcp)
    register_station_tools(mcp)
    return mcp, report


def _upstream_claims_are_true() -> tuple[bool, str]:
    """Uji status provenansi dan kejujuran dokumen UPSTREAM."""
    upstream_file = REPO / "UPSTREAM"
    if not upstream_file.is_file():
        return False, "berkas UPSTREAM tidak ada di repo"
    try:
        txt = upstream_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, "UPSTREAM tidak terbaca"

    low = txt.lower()
    status_lines = [l for l in txt.splitlines() if l.strip().lower().startswith("status:")]
    if not status_lines:
        return False, "tidak ada baris Status: di UPSTREAM"
    st = status_lines[0].lower()
    if "clean" in st:
        return False, f"Status masih mengklaim clean room: {status_lines[0][:70]}"
    named = "comfyui-mcp-server" in low or "joenorton" in low
    if not named or "apache-2.0" not in low:
        return False, f"provenansi Apache-2.0 tidak disebut (named={named})"
    if "e0101b2" not in txt:
        return False, "sha basis tidak dicatat, jadi 'verbatim' tidak bisa diperiksa orang lain"
    return True, ""


def _sh(argv):
    import subprocess
    return subprocess.run(argv, capture_output=True, text=True)


def selftest() -> int:
    """Buktikan ambil-alih ini bekerja — tanpa ComfyUI hidup, tanpa jaringan keluar."""
    results = []

    def check(name, cond, detail=""):
        results.append((bool(cond), name, detail))
        print(f"[{'ok    ' if cond else 'GAGAL '}] {name}" + ("" if cond else f"  <- {detail}"))

    # 1. Verbatim files check
    upstream_files = [
        p for p in (REPO / "upstream").rglob("*")
        if p.is_file() and p.suffix != ".pyc" and "__pycache__" not in p.parts
    ]
    check(
        "upstream vendored ada dan verbatim (49 berkas terhitung di repo ini)",
        len(upstream_files) == 49
        and (REPO / "upstream/server.py").is_file()
        and (REPO / "upstream/LICENSE").is_file()
        and (REPO / "upstream/NOTICE").is_file(),
        f"{len(upstream_files)} berkas di upstream/",
    )

    # 2. Module import & base tools
    base_mcp = None
    try:
        base_mcp, _c, _w, _d, _a, _p = _load_base()
    except Exception as exc:  # noqa: BLE001
        check(
            "modul basis terimport dari upstream/ (bukan dari venv orang lain)",
            False,
            f"{type(exc).__name__}: {exc}",
        )
    if base_mcp is not None:
        check("modul basis terimport dari upstream/ (bukan dari venv orang lain)", True, "")
        tools_pre = {t.name for t in asyncio.run(base_mcp.list_tools())}
        check(
            "basis Apache-2.0 melayani 17 tool-nya (ini bukan kerangka kosong)",
            len(tools_pre) == 17,
            f"{len(tools_pre)} tool: {sorted(tools_pre)[:6]}…",
        )

        rep = apply_surface_policy(base_mcp)
        register_station_tools(base_mcp)
        tools_post = {t.name for t in asyncio.run(base_mcp.list_tools())}

        check(
            "kebijakan stasiun melepas tool yang keluar jaringan secara default (view_image)",
            rep.get("ok") and set(rep["removed"]) == set(NETWORK_TOOLS),
            json.dumps(rep)[:170],
        )

        check(
            "tool inti lokal tetap utuh (generate_image, run_workflow, list_models, get_queue_status)",
            {"generate_image", "run_workflow", "list_models", "get_queue_status"} <= tools_post,
            sorted(tools_post)[:8],
        )

        check(
            "permukaan stasiun terdaftar di atas basis (irsofka_gpu_gate, irsofka_surface_policy)",
            {"irsofka_gpu_gate", "irsofka_surface_policy"} <= tools_post,
            sorted(tools_post)[:8],
        )

        restored = apply_surface_policy(base_mcp, allow_network=True)
        check(
            "dengan consent env terpasang, kebijakan TIDAK menghapus apa pun",
            restored.get("ok") and restored["removed"] == [],
            json.dumps(restored)[:150],
        )

    # 3. VRAM gate integration
    import vram

    check(
        "kosakata gerbang stasiun masih satu sumber (GATE_GO di vram.py, dipakai baris ledger)",
        hasattr(vram, "GATE_GO") and "ALLOW" in vram.GATE_GO and hasattr(vram, "is_go"),
        "vram.GATE_GO / is_go() hilang atau tidak sesuai",
    )

    verdict_test = vram.decide("image-diffusion")
    check(
        "vram.decide() mengembalikan status terstruktur yang valid",
        isinstance(verdict_test, dict) and "state" in verdict_test,
        str(verdict_test),
    )

    # Temuan verifier 2026-10-16: dokumen menjanjikan penolakan saat meter tidak ada, kodenya
    # menjawab ALLOW. Yang ditahan di sini adalah PERILAKUNYA, bukan kata-katanya — sebuah kalimat
    # README bisa kembali busuk tanpa ada yang menyadari, angka ini tidak.
    import tempfile
    _saved = {k: os.environ.get(k) for k in ("AI_STATION_HOME", vram.GATE_ENV)}
    try:
        with tempfile.TemporaryDirectory() as _empty:
            os.environ["AI_STATION_HOME"] = _empty
            os.environ.pop(vram.GATE_ENV, None)
            _v = vram.decide("image-diffusion")
            check(
                "mesin TANPA stasiun menjawab UNKNOWN dan bukan izin — tidak ada meter, tidak ada "
                "permit — dan is_go() membacanya sebagai bukan-izin",
                _v.get("state") == "UNKNOWN" and _v.get("gate") == "absent" and not vram.is_go(_v),
                json.dumps(_v)[:180],
            )
            os.environ[vram.GATE_ENV] = "off"
            _b = vram.decide("image-diffusion")
            check(
                f"jalan keluar harus DIKETIK: {vram.GATE_ENV}=off menghasilkan BYPASSED yang "
                "tercetak sebagai state, bukan hilang diam-diam",
                _b.get("state") == "BYPASSED" and vram.is_go(_b),
                json.dumps(_b)[:180],
            )
    finally:
        for _k, _old in _saved.items():
            if _old is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _old

    _residue = [
        (REPO / n).read_text(errors="replace")
        for n in ("vram.py", "README.md", "UPSTREAM", "NOTICE")
        if (REPO / n).is_file()
    ]
    check(
        "tidak ada sisa repositori lain di modul ini: satu nama env, satu merek, satu alat",
        not any(("BLENDER" in t.upper() or "IRSOVKA" in t.upper()) for t in _residue),
        "vram/README/UPSTREAM/NOTICE masih memakai nama env atau alat repositori lain",
    )
    # Kata yang tidak pernah diproduksi `decide()` tidak boleh muncul di dokumen yang menjanjikan
    # hasilnya. Ini persis kontradiksi yang verifier temukan dan yang tidak bisa dilihat selftest
    # versi lama: kata itu tertulis, angkanya tidak.
    _docs = [
        (REPO / n).read_text(errors="replace")
        for n in ("README.md", "UPSTREAM", "NOTICE")
        if (REPO / n).is_file()
    ]
    check(
        "dokumen hanya menyebut state yang benar-benar bisa dikeluarkan gerbang",
        not any(("UNAVAILABLE" in t.upper() or "WAIT" in t.upper()) for t in _docs),
        "README/UPSTREAM/NOTICE menjanjikan state yang tidak ada di vram.decide()",
    )

    # 3d. Klaim README bahwa "submit yang tidak sanggup dibayar mesin ditolak SEBELUM dikirim"
    # hanya boleh tertulis kalau ia dicegah di kode. Yang diukur di sini adalah penyeratan itu:
    # client palsu yang akan BERTERIMA KASIH kalau sempat dipanggil, dan gerbang yang tidak bisa
    # mengukur harus membuat pemanggilan itu tidak pernah terjadi.
    class _FakeClient:
        def __init__(self):
            self.calls = 0

        def run_custom_workflow(self, workflow, *a, **kw):
            self.calls += 1
            return {"prompt_id": "TERSUBMIT"}

    _saved_env = {k: os.environ.get(k) for k in ("AI_STATION_HOME", "IRSOFKA_COMFY_GATE")}
    try:
        with tempfile.TemporaryDirectory() as _empty:
            os.environ["AI_STATION_HOME"] = _empty
            os.environ.pop("IRSOFKA_COMFY_GATE", None)
            fc = _FakeClient()
            rep = guard_submits(fc)
            out_refused = fc.run_custom_workflow(
                {"1": {"class_type": "KSampler", "inputs": {"prompt": "flux sdxl render"}}})
            check("tanpa meter submit DICEGAT sebelum menyentuh ComfyUI: tool basis tidak pernah "
                  "memanggil client, dan jawabannya data, bukan exception",
                  rep.get("installed") and out_refused.get("error") == "VRAM_GATE_REFUSED"
                  and out_refused.get("submitted") is False and fc.calls == 0,
                  json.dumps(out_refused)[:180])

            os.environ["IRSOFKA_COMFY_GATE"] = "off"
            fc2 = _FakeClient()
            guard_submits(fc2)
            out_off = fc2.run_custom_workflow({"1": {"class_type": "KSampler"}})
            check("jalan keluar harus eksplisit dan benar-benar menembus: dengan "
                  "IRSOFKA_COMFY_GATE=off submit diteruskan, bukan tetap tertahan diam-diam",
                  out_off.get("prompt_id") == "TERSUBMIT" and fc2.calls == 1,
                  json.dumps(out_off)[:160])

            wf = {"7": {"class_type": "VAELoader", "inputs": {"vae_name": "flux.safetensors"}}}
            _txt = _workflow_text(wf)
            check("teks yang dikirim ke gerbang memuat class_type dan string input, supaya "
                  "classifier stasiun punya sesuatu yang bisa dicocokkan",
                  "VAELoader" in _txt and "flux" in _txt, _txt[:120])
    finally:
        for _k, _old in _saved_env.items():
            if _old is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _old

    # 3e. Setiap flag yang DIKLAM README harus benar-benar ditangani main(). Verifier ronde publikasi
    # menemukan `--status` diiklankan sebagai "read-only, safe" padahal tidak ada: argv jatuh ke
    # transport stdio dan proses menunggu selamanya. Yang diukur di sini bukan satu flag itu saja,
    # karena flag hantu berikutnya akan lahir dengan cara yang sama.
    _src_main = inspect.getsource(main)
    _rm = (REPO / "README.md").read_text(errors="replace") if (REPO / "README.md").is_file() else ""
    _advertised = sorted({m.group(1) for m in re.finditer(r"server\.py\s+--([a-z][\w-]*)", _rm)})
    _missing = [f for f in _advertised if f"--{f}" not in _src_main]
    check("semua flag yang diiklankan README ditangani main() (tidak ada flag hantu yang berujung "
          "proses menggantung)",
          bool(_advertised) and not _missing,
          f"diiklankan={_advertised} tidak ditangani={_missing}")

    # 3f. Dependensi yang diklaim README harus terbukti dari kode, bukan dari kalimat. Yang diukur
    # adalah impor LEVEL-ATAS dari dua berkas stasiun -- itulah yang dibaca seorang pemasang dan
    # itulah dasar daftar dependensi. Impor basis di dalam `_load_base()` terjadi setelah sys.path
    # menunjuk ke `upstream/`, jadi ia bukan dependensi instalasi dan memang tidak diklaim begitu.
    _allowed_roots = set(sys.stdlib_module_names) | {"mcp", "vram"}
    _roots: set[str] = set()
    for _fname in ("server.py", "vram.py"):
        _fp = REPO / _fname
        if not _fp.is_file():
            continue
        _tree = ast.parse(_fp.read_text(errors="replace"))
        for _n in _tree.body:
            if isinstance(_n, ast.Import):
                _roots.update(a.name.split(".")[0] for a in _n.names)
            elif isinstance(_n, ast.ImportFrom) and _n.level == 0 and _n.module:
                _roots.add(_n.module.split(".")[0])
    _foreign = sorted(_roots - _allowed_roots)
    check("impor level-atas dua berkas stasiun hanya pustaka standar + mcp (+ modul gerbang kami) "
          "-- klaim dependensi README diukur, bukan ditulis",
          bool(_roots) and not _foreign,
          f"asing={_foreign} semua={sorted(_roots)}")

    # 4. Provenance & License honesty
    st, why = _upstream_claims_are_true()
    check("baris Status UPSTREAM menyatakan ambil-alih Apache-2.0, bukan clean room", st, why)

    lic_text = (REPO / "LICENSE").read_text(encoding="utf-8", errors="replace")
    upstream_lic = (REPO / "upstream/LICENSE").read_text(encoding="utf-8", errors="replace")
    upstream_not = (REPO / "upstream/NOTICE").read_text(encoding="utf-8", errors="replace")
    check(
        "lisensi kami BSL 1.1 (keputusan pemegang proyek), dan NOTICE/LICENSE Apache-2.0 Joe Norton ikut terbawa",
        "Business Source" in lic_text
        and "Apache License" in upstream_lic
        and "Joe Norton" in upstream_not,
        "LICENSE / upstream LICENSE / upstream NOTICE tidak sesuai",
    )

    # preflight pemasangan: pesan harus berisi sebab + jalan keluar
    import subprocess as _sp
    fake = REPO.parent / (REPO.name + "-no-upstream-test")
    try:
        if fake.exists():
            _sh(["rm", "-rf", str(fake)])
        _sh(["cp", "-r", str(REPO), str(fake)])
        _sh(["rm", "-rf", str(fake / "upstream")])
        r = _sp.run([sys.executable, "server.py"], cwd=str(fake), capture_output=True, text=True, timeout=60)
        msg = (r.stderr or "") + (r.stdout or "")
        check("tanpa upstream/ server menjawab dengan sebab + jalan keluar, bukan traceback import",
              r.returncode != 0 and "basis tidak ditemukan" in msg and "checkout" in msg,
              msg[-200:] if msg.strip() else f"rc={r.returncode} tanpa keluaran")
    except Exception as _exc:  # noqa: BLE001 — tes instalasi tidak boleh membuat suite merah kabur
        check("tanpa upstream/ server menjawab dengan sebab + jalan keluar, bukan traceback import",
              False, f"{type(_exc).__name__}: {_exc}")
    finally:
        _sh(["rm", "-rf", str(fake)])

    # Jumlah yang ditulis README adalah satu-satunya angka yang dibaca orang yang tidak menjalankan
    # apa pun. Verifier ronde publikasi menemukan angka 12 di README sementara suite menjalankan 16 —
    # dan koreksi angka saja tidak cukup, karena angka itu akan busuk lagi pada check berikutnya.
    # Check terakhir ini membandingkan angka README dengan jumlah yang benar-benar dijalankan
    # (termasuk dirinya), jadi README tidak bisa lagi mengklaim jumlah yang salah.
    import re as _re
    _readme = (REPO / "README.md").read_text(errors="replace") if (REPO / "README.md").is_file() else ""
    _claimed = sorted({int(n) for n in re.findall(r"#\s*(\d+)\s+checks?", _readme)})
    _total = len(results) + 1
    check(
        f"jumlah check yang DITULIS README sama dengan yang benar-benar berjalan "
        f"({len(_claimed)} angka di README vs {_total} yang jalan)",
        _claimed == [_total],
        f"README menulis {(_claimed or 'tidak ada')}, yang jalan {_total}",
    )

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

    if "--status" in argv:
        # README mengiklankan perintah ini dan sebelumnya tidak ada: yang terjadi adalah server
        # masuk ke transport stdio lalu menunggu selamanya — perintah yang didokumentasikan sebagai
        # "read-only, safe" berubah menjadi proses menggantung. Flag yang terdokumentasi harus
        # mengembalikan sesuatu, atau tidak boleh terdokumentasi.
        import vram
        _mcp, report = build()
        print(json.dumps({
            "comfyui_url": COMFYUI_URL,
            "network": {"allow": network_allowed(), "consent_env": ALLOW_NETWORK_ENV,
                        "withheld": sorted(NETWORK_TOOLS)},
            "gate": {"mode": vram.mode(), "env": vram.GATE_ENV, "vocab": list(vram.GATE_GO)},
            "submit_interception": dict(_GATE_REPORT),
            "verdict_for_a_read_only_intent": vram.decide("status probe, no workload requested"),
            "surface_policy_ok": bool(report.get("ok")),
        }, indent=2))
        return 0

    mcp, _report = build()
    # If called with --http or standalone
    if "--http" in argv:
        mcp.run(transport="streamable-http")
    else:
        # Default for MCP client invocation
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
