"""VRAM gate bridge.

The workstation keeps one definition of "is there enough VRAM for this" and one
definition of "who is holding it" in `~/.ai-station/tools/gpu_gate.py`. This
module does not reimplement either; it finds that file, or reports that the
gate is not available on this machine.

Two states, and they are deliberately different prices:

  gate="ai-station"  -> the decision comes from gpu_gate, thresholds from its config,
                        re-read on every call.
  gate="absent"      -> no workstation on this machine, so there is no meter, no threshold and
                        no holder to name. The answer is `UNKNOWN`: a missing guard is not
                        evidence that the work is safe, and this module will not issue a permit
                        it cannot sign.

`IRSOFKA_COMFY_GATE=off` is the escape hatch for a machine that has no workstation and where
the operator accepts the risk of an OOM. It is opt-in and it is stamped into every answer it
touches (`state: BYPASSED`), so nothing submitted under it can later be mistaken for a gated
submission.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Named once, spelled the same in every answer that touches it. The `off` value is the only one
# this reads; any other value leaves the gate in charge.
GATE_ENV = "IRSOFKA_COMFY_GATE"

# The states that mean "go", spelled once. The words belong to the station's `gpu_gate.py`
# (ALLOW / NOT_GPU_WORK / UNKNOWN / DENY); this module adds BYPASSED for the opt-out below.
#
# Why a list and not a truthiness check: on 2026-10-15 a caller invented its own test —
# `verdict.get("allow")`, a key `decide()` has never returned — and refused every render while
# looking careful. Two spellings of one word, one of them wrong.
#
#   ALLOW        the station gate measured the card and there is room
#   NOT_GPU_WORK the station's rules matched nothing for this text, so the work is not classified
#                as a GPU workload. For this server the useful reading is queue inspection: a job
#                that runs is a job ComfyUI itself decided the device for. A diffusion submit that
#                does reach the card matches a rule and gets ALLOW or DENY, not this.
#   BYPASSED     the operator turned the gate off with IRSOFKA_COMFY_GATE=off
# Anything else — UNKNOWN, a refusal, a shape this module does not recognise — is NOT permission.
GATE_GO = ("ALLOW", "NOT_GPU_WORK", "BYPASSED")


def is_go(verdict: dict) -> bool:
    return (verdict or {}).get("state") in GATE_GO


HEAVY_HINTS = (
    "trellis", "pixal3d", "image-to-3d", "image_to_3d", "3d_pipeline", "voxel remesh",
    "z-image", "flux", "sdxl", "txt2img", "juggernaut", "controlnet", "hunyuan",
)


def workstation_root() -> Path | None:
    """Where the ai-station lives, if it lives here at all.

    `AI_STATION_HOME` is authoritative when set: pointing it at a path with no gate in it
    means "no gate", and does NOT fall back to `~/.ai-station`. Silently reading a different
    directory than the one an operator named is how a guard ends up guarding something else.
    A path is not a gate either way — the directory only counts when the module that owns the
    decision is actually in it.
    """
    override = os.environ.get("AI_STATION_HOME")
    cand = Path(override).expanduser() if override else Path.home() / ".ai-station"
    return cand if (cand / "tools" / "gpu_gate.py").is_file() else None


def _load_gate():
    root = workstation_root()
    if root is None:
        return None, None
    tools = str(root / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    try:
        import gpu_gate  # noqa: PLC0415
    except Exception:  # a broken gate must not crash the server
        return None, root
    return gpu_gate, root


def mode() -> str:
    if os.environ.get(GATE_ENV, "").strip().lower() == "off":
        return "off"
    gate, _ = _load_gate()
    return "ai-station" if gate is not None else "absent"


def decide(text: str) -> dict:
    """Gate one piece of work. Never raises: an unreadable gate is a refusal, not a stack trace."""
    m = mode()
    if m == "off":
        return {"state": "BYPASSED", "gate": "off",
                "why": f"{GATE_ENV}=off on a machine without a workstation gate"}
    gate, root = _load_gate()
    if gate is None:
        # No meter here, so nothing can be measured and nothing can be promised. This is the
        # branch an outside user hits first, and it refuses rather than issuing a permit this
        # module cannot sign: `ALLOW` here would be an implicit yes wearing a confident word.
        heavy = [w for w in HEAVY_HINTS if w in (text or "").lower()]
        return {"state": "UNKNOWN", "gate": "absent",
                **({"matched": heavy[0]} if heavy else {}),
                "why": "no workstation gate on this machine, so VRAM cannot be measured; "
                       f"refusing rather than guessing. Set {GATE_ENV}=off to accept the risk "
                       "of an out-of-memory yourself"}
    try:
        res = dict(gate.gate(text))
    except Exception as exc:  # the gate exists but threw: refuse with its name
        return {"state": "UNKNOWN", "gate": "ai-station", "error": str(exc)[:200],
                "why": "gpu_gate.gate() raised; refusing instead of guessing"}
    res["gate"] = "ai-station"
    res.setdefault("root", str(root))
    return res


# Tidak ada `report()` di sini, dan itu keputusan yang diukur. Versi lama punya fungsi itu dengan
# docstring "what the operator sees in irsofka_gpu_gate" — padahal tool itu memanggil `decide()`,
# dan tidak ada satu pun pemanggil `report()` di seluruh repo. Fungsi mati yang mengklaim mewakili
# angka hidup adalah dua definisi untuk satu kata, dan yang mati tidak akan pernah dibetulkan.
# Stasiun sudah publishes pembacaannya sendiri (`tools/gpu_gate.py status`); salinan keduanya di
# server yang tidak pernah membacanya bukan fitur, hanya utang.
