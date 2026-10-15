"""VRAM gate bridge.

The workstation keeps one definition of "is there enough VRAM for this" and one
definition of "who is holding it" in `~/.ai-station/tools/gpu_gate.py`. This
module does not reimplement either; it finds that file, or reports that the
gate is not available on this machine.

Two states, and they are deliberately different prices:

  gate="ai-station"  -> the decision comes from gpu_gate, thresholds from its config,
                        re-read on every call.
  gate="absent"      -> no workstation on this machine. Read-only tools keep working.
                        A submit whose text matches a known-heavy class is refused,
                        because "we could not find the guard" is not evidence that
                        the work is safe.

`IRSOVKA_COMFY_GATE=off` is the escape hatch for a machine that has no workstation
and where the operator accepts the risk of an OOM. It is opt-in and it is stamped
into every answer it touches, so nothing submitted under it can later be mistaken
for a gated submission.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

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
    if os.environ.get("IRSOVKA_COMFY_GATE", "").strip().lower() == "off":
        return "off"
    gate, _ = _load_gate()
    return "ai-station" if gate is not None else "absent"


def decide(text: str) -> dict:
    """Gate one piece of work. Never raises: an unreadable gate is a refusal, not a stack trace."""
    m = mode()
    if m == "off":
        return {"state": "BYPASSED", "gate": "off",
                "why": "IRSOVKA_COMFY_GATE=off on a machine without a workstation gate"}
    gate, root = _load_gate()
    if gate is None:
        heavy = [w for w in HEAVY_HINTS if w in (text or "").lower()]
        if heavy:
            return {"state": "UNKNOWN", "gate": "absent", "matched": heavy[0],
                    "why": "no ai-station on this machine, so no VRAM gate; refusing a "
                           "known-heavy submit (set IRSOVKA_COMFY_GATE=off to override)"}
        # Degraded, not blind: this machine has no gate, so there is no threshold to check
        # against and no holder to name. `unaudited` rides along with the answer so a submit
        # made here can never be mistaken later for one the gate cleared.
        return {"state": "ALLOW", "gate": "absent", "unaudited": True,
                "why": "no gate on this machine and no heavy-class keyword; submitting unaudited"}
    try:
        res = dict(gate.gate(text))
    except Exception as exc:  # the gate exists but threw: refuse with its name
        return {"state": "UNKNOWN", "gate": "ai-station", "error": str(exc)[:200],
                "why": "gpu_gate.gate() raised; refusing instead of guessing"}
    res["gate"] = "ai-station"
    res.setdefault("root", str(root))
    return res


def report() -> dict:
    """What the operator sees in comfy_status: which gate is live and what it says now."""
    gate, root = _load_gate()
    if gate is None or os.environ.get("IRSOVKA_COMFY_GATE", "").strip().lower() == "off":
        return {"gate": mode(), "root": str(root) if root else None}
    try:
        st = gate.status()
    except Exception as exc:  # a status that cannot be read is reported as such
        return {"gate": "ai-station", "root": str(root), "error": str(exc)[:200]}
    return {"gate": "ai-station", "root": str(root),
            "free_mib": st.get("free_mib"), "readable": st.get("readable"),
            "holders": st.get("holders"), "rules": st.get("rules")}
