#!/usr/bin/env python3
"""IRSofka ComfyUI MCP — a stdio Model Context Protocol server for a ComfyUI you run yourself.

Seven tools over ComfyUI's own public HTTP API (default `http://127.0.0.1:8188`). Clean-room:
no upstream MCP code was read or copied, every request shape here was measured against a live
server on 2026-10-15 and the numbers are in README.md. Stdlib only, so it installs anywhere
Python 3.9+ runs.

It does one thing the popular ComfyUI MCPs do not: it refuses a submit that the machine cannot
pay for. `vram.decide()` is consulted before `POST /prompt`, and a refusal names who is holding
the VRAM. See `mcp://about/gate`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vram  # noqa: E402

SERVER_NAME = "irsofka-comfyui-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL = "2024-11-05"

# Measured on 2026-10-15: the whole /object_info payload is megabytes and will eat a context
# window if handed over; one named node is ~0.9 kB. Nothing in this file pulls the whole list
# except `comfy_object` action="names", which returns class names only, capped.
MAX_NODES_PER_LOOKUP = 20
MAX_NAMES = 400
MAX_HISTORY = 10
GATE_TEXT_CAP = 20000


def base_url() -> str:
    return os.environ.get("IRSOVKA_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")


def http_call(method: str, path: str, data: bytes | None = None,
              headers: dict | None = None, timeout: float = 20.0):
    """One HTTP exchange. Returns (status_or_None, body_bytes).

    None is not a status code, it is "no answer": the port is closed, the server died, the
    clock ran out. Callers must report that as UNAVAILABLE rather than as a refusal of the work.
    """
    url = base_url() + path
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        return exc.code, raw
    except Exception as exc:
        return None, str(exc).encode("utf-8", "replace")


def _get(path: str, timeout: float = 20.0):
    status, raw = TRANSPORT("GET", path, timeout=timeout)
    return status, _decode(raw)


def _post(path: str, payload: dict, timeout: float = 30.0):
    body = json.dumps(payload).encode("utf-8")
    status, raw = TRANSPORT("POST", path, data=body,
                           headers={"Content-Type": "application/json"}, timeout=timeout)
    return status, _decode(raw)


def _decode(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"_non_json": raw[:400].decode("utf-8", "replace")}


def _text(raw) -> str:
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)


def _unavailable(status, data) -> dict:
    """The port did not answer. That is a fact about the server, never a refusal of the work."""
    if isinstance(data, dict) and "_non_json" in data:
        msg = str(data["_non_json"])
    elif isinstance(data, bytes):
        msg = data.decode("utf-8", "replace")
    else:
        msg = str(data)
    return {"ok": False, "state": "UNAVAILABLE", "url": base_url(), "error": msg[:300],
            "status": status,
            "next": "is ComfyUI running on that port? (measured here: 127.0.0.1:8188)"}


def _mib(nbytes) -> int | None:
    """Bytes to MiB, floored. Rounding up would overstate free VRAM, which is the one mistake
    this whole gate exists to make impossible."""
    return None if nbytes is None else int(int(nbytes) // 1048576)


# ───────────────────────────────── graph helpers ─────────────────────────────────


def graph_text(graph: dict, note: str = "") -> str:
    """Everything the gate is allowed to see about a job: node classes and input strings.

    Deliberately not the raw JSON: a graph carries widget arrays and links whose digits can
    accidentally spell a keyword, and a gate decision should not depend on layout noise.
    """
    parts = [note]
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        parts.append(str(node.get("class_type", "")))
        stack = [node.get("inputs", {})]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                stack.extend(cur.values())
            elif isinstance(cur, (list, tuple)):
                stack.extend(cur)
            elif isinstance(cur, str):
                parts.append(cur)
    return " ".join(" ".join(str(p) for p in parts if p).split())[:GATE_TEXT_CAP]


def load_graph(args: dict):
    """Return (graph, error). Accepts an inline API-format graph or a path to one."""
    if args.get("workflow") is not None and args.get("path"):
        return None, {"ok": False, "error": "AMBIGUOUS_INPUT",
                      "why": "give either `workflow` (object) or `path` (file), not both"}
    raw = args.get("workflow")
    src = "inline"
    if raw is None:
        p = args.get("path")
        if not p:
            return None, {"ok": False, "error": "NO_WORKFLOW",
                          "why": "this tool takes an API-format graph, inline or from a file"}
        fp = Path(str(p)).expanduser()
        if not fp.is_file():
            return None, {"ok": False, "error": "PATH_NOT_FOUND", "path": str(fp)}
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, {"ok": False, "error": "NOT_JSON", "path": str(fp), "why": str(exc)[:200]}
        src = str(fp)
    if not isinstance(raw, dict):
        return None, {"ok": False, "error": "NOT_AN_OBJECT", "got": type(raw).__name__}
    # A UI export carries `nodes`/`links`/`last_node_id`; ComfyUI's POST /prompt wants the API
    # form ({node_id: {class_type, inputs}}). Handing the wrong one over produces a 400 whose
    # message names neither format, so the check happens here, before anything is sent.
    if "nodes" in raw and "links" in raw:
        return None, {"ok": False, "error": "UI_EXPORT_NOT_API_FORMAT", "source": src,
                      "why": "this is a saved UI workflow; export it as API-format JSON "
                             "(ComfyUI: Workflow -> Export (API)) or use the graph object"}
    if not raw or not all(isinstance(v, dict) and "class_type" in v for v in raw.values()):
        return None, {"ok": False, "error": "NOT_API_FORMAT", "source": src,
                      "why": "every value must be an object with a `class_type` key"}
    return raw, None


# ───────────────────────────────────── tools ─────────────────────────────────────


def tool_status(args: dict) -> dict:
    """Server facts + the VRAM gate as it reads right now. `argv` is withheld by default."""
    status, data = _get("/system_stats")
    if status is None:
        return _unavailable(status, data)
    if status != 200 or not isinstance(data, dict):
        return {"ok": False, "state": "ERROR", "status": status, "body": data}
    system = data.get("system", {})
    out = {
        "ok": True,
        "url": base_url(),
        "comfyui_version": system.get("comfyui_version"),
        "python": system.get("python_version"),
        "pytorch": system.get("pytorch_version"),
        "os": system.get("os"),
        "deploy_environment": system.get("deploy_environment"),
        "ram_free_mib": _mib(system.get("ram_free")),
        "ram_total_mib": _mib(system.get("ram_total")),
        "devices": [{"name": d.get("name"), "type": d.get("type"),
                     "vram_free_mib": _mib(d.get("vram_free")),
                     "vram_total_mib": _mib(d.get("vram_total")),
                     "torch_reserved_mib": _mib(d.get("torch_vram_total"))}
                    for d in (data.get("devices") or [])],
        "gate": vram.report(),
    }
    # ComfyUI's /system_stats echoes its own command line, which on some installs carries
    # tokens. It is the operator's data, not ours, so it is opt-in.
    if args.get("include_argv"):
        out["argv"] = system.get("argv")
    else:
        out["argv_omitted"] = "pass include_argv=true to see ComfyUI's launch command line"
    return out


def tool_queue(args: dict, decide=None) -> dict:
    """Submit an API-format workflow, behind the VRAM gate."""
    graph, err = load_graph(args)
    if err:
        return err
    text = graph_text(graph, str(args.get("note") or ""))
    verdict = (decide or vram.decide)(text)
    state = verdict.get("state")
    if state not in ("ALLOW", "NOT_GPU_WORK", "BYPASSED"):
        return {"ok": False, "state": "REFUSED", "reason": "VRAM_GATE",
                "gate_state": state,
                "rule": verdict.get("rule") or verdict.get("matched"),
                "need_mib": verdict.get("need_mib"),
                "free_mib": verdict.get("free_mib"),
                "holders": verdict.get("holders"),
                "why": verdict.get("why"),
                "submitted": False,
                "note": "nothing was sent to ComfyUI; this refusal is the gate, not the server"}
    payload = {"prompt": graph}
    client = args.get("client_id") or str(uuid.uuid4())
    payload["client_id"] = client
    if args.get("front") is not None:
        payload["front"] = bool(args["front"])
    body = json.dumps(payload).encode("utf-8")
    status, raw = TRANSPORT("POST", "/prompt", data=body,
                            headers={"Content-Type": "application/json"},
                            timeout=float(args.get("timeout", 30)))
    if status is None:
        # The POST left and no answer came back. Whether ComfyUI queued it is genuinely unknown,
        # and `submitted: false` would be a claim about the world that this call cannot make.
        return {"ok": False, "state": "UNAVAILABLE", "submitted": None,
                "url": base_url(), "error": _text(raw), "gate_state": state,
                "next": "check comfy_job action=list before re-sending — the job may already be queued"}
    data = _decode(raw)
    if status in (200, 201):
        return {"ok": True, "state": "SUBMITTED", "submitted": True,
                "prompt_id": data.get("prompt_id") if isinstance(data, dict) else None,
                "number": data.get("number") if isinstance(data, dict) else None,
                "client_id": client, "gate_state": state,
                "rule": verdict.get("rule"), "free_mib": verdict.get("free_mib"),
                "need_mib": verdict.get("need_mib")}
    # Measured: an unknown class answers 400 `missing_node_type` and queues nothing, so a 400
    # here is a fact about the graph, not a half-started job.
    return {"ok": False, "state": "REJECTED_BY_COMFY", "submitted": False,
            "status": status, "error": (data or {}).get("error") if isinstance(data, dict) else data,
            "node_errors": (data or {}).get("node_errors") if isinstance(data, dict) else None,
            "gate_state": state}


def tool_job(args: dict) -> dict:
    action = (args.get("action") or "status").strip().lower()
    pid = args.get("prompt_id")
    if action == "list":
        status, data = _get("/queue")
        if status != 200 or not isinstance(data, dict):
            return _unavailable(status, data) if status is None else {"ok": False, "status": status}
        run = [{"number": q[0], "prompt_id": q[1], "node_count": len(q[2]) if len(q) > 2 else None}
               for q in (data.get("queue_running") or []) if isinstance(q, (list, tuple)) and len(q) > 1]
        pend = [{"number": q[0], "prompt_id": q[1]}
                for q in (data.get("queue_pending") or []) if isinstance(q, (list, tuple)) and len(q) > 1]
        return {"ok": True, "running": run, "pending": pend}
    if action == "progress":
        # ComfyUI reports per-node execution only over websocket; /prompt's GET twin hands back
        # the queue depth, which is the honest subset of "how far along is it".
        status, data = _get("/prompt")
        if status is None:
            return _unavailable(status, data)
        return {"ok": True, "queue_remaining": (data or {}).get("exec_info", {}).get("queue_remaining")
                if isinstance(data, dict) else None, "raw_status": status}
    if action in ("interrupt", "clear"):
        if not args.get("confirm"):
            return {"ok": False, "error": "CONFIRM_REQUIRED", "action": action,
                    "why": "this stops work that may be somebody else's running job; "
                           "re-send with confirm=true"}
        if action == "interrupt":
            status, data = _post("/interrupt", {})
            return {"ok": status == 200, "action": "interrupt", "status": status,
                    "body": data, "note": "ComfyUI stops the current job at the next node boundary"}
        status, data = _post("/queue", {"clear": True})
        return {"ok": status == 200, "action": "clear", "status": status, "body": data}
    if not pid:
        return {"ok": False, "error": "NO_PROMPT_ID", "action": action,
                "why": "action=status needs the prompt_id that comfy_queue returned"}
    status, data = _get("/history/" + urllib.parse.quote(str(pid), safe=""))
    if status is None:
        return _unavailable(status, data)
    if status == 404:
        return {"ok": True, "state": "NOT_IN_HISTORY", "prompt_id": pid,
                "why": "ComfyUI keeps finished jobs only; a queued job has no history row yet",
                "check": "comfy_job action=list"}
    entry = data.get(str(pid)) if isinstance(data, dict) else None
    if entry is None:
        return {"ok": True, "state": "NOT_IN_HISTORY", "prompt_id": pid, "body": data}
    st = entry.get("status") or {}
    outputs = []
    for node_out in (entry.get("outputs") or {}).values():
        for key in ("images", "gifs", "audio"):
            for item in (node_out.get(key) or []):
                outputs.append({"kind": key, "filename": item.get("filename"),
                                "subfolder": item.get("subfolder"), "type": item.get("type"),
                                "frame_subindex": item.get("frame_subindex"),
                                "node": node_out.get("_node")})
    return {"ok": True, "prompt_id": pid, "state": st.get("status_str"),
            "completed": st.get("completed"), "outputs": outputs,
            "node_errors": [m for m in (st.get("messages") or [])
                            if isinstance(m, list) and m and m[0] in ("execution_error", "execution_interrupted")]}


def tool_object(args: dict) -> dict:
    action = (args.get("action") or "get").strip().lower()
    if action == "names":
        status, data = _get("/object_info")
        if status is None:
            return _unavailable(status, data)
        if not isinstance(data, dict):
            return {"ok": False, "status": status}
        cap = int(args.get("limit") or MAX_NAMES)
        off = int(args.get("offset") or 0)
        names = sorted(data)
        return {"ok": True, "count": len(names), "offset": off, "limit": cap,
                "returned": len(names[off:off + cap]), "next_offset": off + cap,
                "names": names[off:off + cap],
                "note": "names only, and only a page of them — a node's schema is action=get "
                        "and costs ~1 kB, the whole catalog is ~1.9 MB"}
    names = args.get("names") or []
    if isinstance(names, str):
        names = [names]
    names = [str(n).strip() for n in names if str(n).strip()]
    if not names:
        return {"ok": False, "error": "NO_NAMES",
                "why": "this server never hands over the whole node catalog; name what you want"}
    if len(names) > MAX_NODES_PER_LOOKUP:
        return {"ok": False, "error": "TOO_MANY_NAMES", "count": len(names),
                "max": MAX_NODES_PER_LOOKUP}
    found, missing = {}, []
    for n in names:
        status, data = _get("/object_info/" + urllib.parse.quote(n, safe=""))
        # Measured: an unknown class answers 200 with an empty object, so the status code is
        # not the verdict here. Trusting 200 would report a node that does not exist as found.
        if status == 200 and isinstance(data, dict) and data.get(n):
            body = data[n]
            found[n] = {"category": body.get("category"),
                        "python_module": body.get("python_module"),
                        "inputs": body.get("input"),
                        "optional": (body.get("input") or {}).get("optional"),
                        "outputs": body.get("output"),
                        "output_name": body.get("output_name"),
                        "output_is_list": body.get("output_is_list"),
                        "display_name": body.get("display_name")}
        else:
            missing.append(n)
    return {"ok": True, "found": list(found), "missing": missing, "schema": found,
            "note": "a name in `missing` means that class is not installed on this server"}


def tool_upload(args: dict) -> dict:
    p = Path(str(args.get("path") or "")).expanduser()
    if not p.is_file():
        return {"ok": False, "error": "PATH_NOT_FOUND", "path": str(p)}
    if p.stat().st_size > 200 * 1024 * 1024:
        return {"ok": False, "error": "TOO_LARGE", "bytes": p.stat().st_size,
                "why": "this tool does not stream; >200 MB belongs on disk, not over stdio JSON"}
    data = p.read_bytes()
    name = args.get("filename") or p.name
    over = bool(args.get("overwrite"))
    fields = {"overwrite": "true" if over else "false"}
    if args.get("type"):
        fields["type"] = str(args["type"])
    body, boundary = _multipart(name, data, fields, content_type=_guess_type(name))
    status, raw = TRANSPORT("POST", "/upload/image", data=body,
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                            timeout=120)
    if status is None:
        return _unavailable(status, raw)
    out = _decode(raw)
    if status != 200 or not isinstance(out, dict):
        return {"ok": False, "state": "ERROR", "status": status, "body": out}
    # The uploaded name is what a LoadImage node must reference; return it verbatim.
    return {"ok": True, "name": out.get("name"), "subfolder": out.get("subfolder"),
            "type": out.get("type"), "local_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "use_in_graph": {"class_type": "LoadImage", "inputs": {"image": out.get("name")}}}


def tool_fetch(args: dict) -> dict:
    q = {"filename": args.get("filename"), "subfolder": args.get("subfolder", ""),
         "type": args.get("type", "output")}
    if not q["filename"]:
        return {"ok": False, "error": "NO_FILENAME", "why": "from comfy_job outputs"}
    qs = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
    status, data = TRANSPORT("GET", "/view?" + qs, timeout=120)
    if status is None:
        return _unavailable(status, data)
    if status != 200 or not data:
        return {"ok": False, "state": "ERROR", "status": status,
                "body": _decode(data) if isinstance(data, bytes) else None}
    out_dir = Path(str(args.get("out_dir") or "./comfy-outputs")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / str(Path(q["filename"]).name)
    if dest.exists() and not args.get("overwrite"):
        dest = out_dir / f"{Path(q['filename']).stem}-{hashlib.sha256(data).hexdigest()[:8]}" \
                         f"{Path(q['filename']).suffix}"
    dest.write_bytes(data)
    return {"ok": True, "path": str(dest), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "note": "hash before concluding a run produced what you expected; two identical "
                    "sha256s from different seeds mean the seed never reached the graph"}


def tool_free(args: dict) -> dict:
    if not args.get("confirm"):
        return {"ok": False, "error": "CONFIRM_REQUIRED",
                "why": "unloading models forces whoever is next to reload them; re-send with confirm=true"}
    body = {"unload_models": bool(args.get("unload_models", True)),
            "free_memory": bool(args.get("free_memory", args.get("unload_models", True))),
            "skip_weight_calculation": False}
    status, data = _post("/free", body)
    if status is None:
        return _unavailable(status, data)
    return {"ok": status == 200, "status": status, "requested": body, "body": data,
            "applies_at": "the next queue-worker iteration, not instantly",
            "after": "call comfy_status to confirm what actually came free"}


def _multipart(name: str, data: bytes, fields: dict, content_type: str = "application/octet-stream"):
    boundary = "----irsfoka" + uuid.uuid4().hex
    def line(s: bytes) -> bytes:
        return s + b"\r\n"
    parts = []
    for k, v in fields.items():
        parts.append(line(f'--{boundary}'.encode()))
        parts.append(line(f'Content-Disposition: form-data; name="{k}"'.encode()))
        parts.append(line(b""))
        parts.append(line(str(v).encode()))
    parts.append(line(f'--{boundary}'.encode()))
    parts.append(line(f'Content-Disposition: form-data; name="image"; filename="{name}"'.encode()))
    parts.append(line(f"Content-Type: {content_type}".encode()))
    parts.append(line(b""))
    parts.append(data)
    parts.append(line(b""))
    parts.append(line(f'--{boundary}--'.encode()))
    return b"".join(parts), boundary


def _guess_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".exr": "image/x-exr", ".mp4": "video/mp4",
            ".gif": "image/gif"}.get(ext, "application/octet-stream")


TOOL_IMPLS = {
    "comfy_status": tool_status,
    "comfy_queue": tool_queue,
    "comfy_job": tool_job,
    "comfy_object": tool_object,
    "comfy_upload": tool_upload,
    "comfy_fetch": tool_fetch,
    "comfy_free": tool_free,
}

TOOLS = [
    {"name": "comfy_status",
     "description": "ComfyUI version, per-device VRAM in MiB, and the live VRAM gate. "
                    "ComfyUI's own argv is withheld unless include_argv=true.",
     "inputSchema": {"type": "object", "properties": {
         "include_argv": {"type": "boolean", "default": False}}, "additionalProperties": False}},
    {"name": "comfy_queue",
     "description": "Submit an API-format workflow to POST /prompt, behind the VRAM gate: a job "
                    "the machine cannot pay for is refused here, naming the holder, and nothing "
                    "is sent. Give `workflow` (the graph object) or `path` (a JSON file).",
     "inputSchema": {"type": "object", "properties": {
         "workflow": {"type": "object", "description": "API-format graph: {node_id: {class_type, inputs}}"},
         "path": {"type": "string", "description": "file holding the same shape"},
         "note": {"type": "string", "description": "extra text for the gate (e.g. 'TRELLIS image-to-3D')"},
         "client_id": {"type": "string"}, "front": {"type": "boolean"},
         "timeout": {"type": "number", "default": 30}}, "additionalProperties": False}},
    {"name": "comfy_job",
     "description": "action=status (one prompt_id: state, outputs, error messages), action=list "
                    "(running + pending), action=progress (queue depth), action=interrupt or "
                    "clear (both need confirm=true).",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["status", "list", "progress", "interrupt", "clear"],
                    "default": "status"},
         "prompt_id": {"type": "string"}, "confirm": {"type": "boolean", "default": False}},
         "additionalProperties": False}},
    {"name": "comfy_object",
     "description": "Node schemas by exact class name (max 20), or action=names for the class-name "
                    "catalog only. The whole /object_info payload is never returned: it is megabytes.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["get", "names"], "default": "get"},
         "names": {"type": "array", "items": {"type": "string"}},
         "limit": {"type": "integer", "default": 400},
         "offset": {"type": "integer", "default": 0}}, "additionalProperties": False}},
    {"name": "comfy_upload",
     "description": "Stage a source image or mask in ComfyUI's input folder (POST /upload/image) "
                    "and return the name a LoadImage node must use.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string"}, "filename": {"type": "string"},
         "type": {"type": "string", "default": "input"},
         "overwrite": {"type": "boolean", "default": False}}, "additionalProperties": False}},
    {"name": "comfy_fetch",
     "description": "Download one finished output (GET /view) to a local directory and return its "
                    "sha256, so a run can be compared by hash instead of by filename.",
     "inputSchema": {"type": "object", "required": ["filename"], "properties": {
         "filename": {"type": "string"}, "subfolder": {"type": "string", "default": ""},
         "type": {"type": "string", "enum": ["output", "input", "temp"], "default": "output"},
         "out_dir": {"type": "string", "default": "./comfy-outputs"},
         "overwrite": {"type": "boolean", "default": False}}, "additionalProperties": False}},
    {"name": "comfy_free",
     "description": "Ask ComfyUI to unload models / reset its cache (POST /free). Applies at the "
                    "next queue iteration and cannot touch VRAM held by another process. Needs confirm=true.",
     "inputSchema": {"type": "object", "properties": {
         "unload_models": {"type": "boolean", "default": True},
         "free_memory": {"type": "boolean"}, "confirm": {"type": "boolean", "default": False}},
         "additionalProperties": False}},
]

TRANSPORT = http_call


# ────────────────────────────────── protocol ──────────────────────────────────


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": [
            {"uri": "mcp://about/gate", "name": "VRAM gate",
             "description": "how a submit is approved or refused on this machine",
             "mimeType": "text/plain"}]}}
    if method == "resources/read":
        uri = (msg.get("params") or {}).get("uri")
        text = json.dumps(vram.report(), indent=2) if uri == "mcp://about/gate" else None
        if text is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32002, "message": f"unknown resource {uri}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        impl = TOOL_IMPLS.get(name)
        if impl is None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            out = impl(args)
        except Exception as exc:  # a tool must answer with a fact, never with our stack trace
            out = {"ok": False, "state": "SERVER_ERROR", "tool": name,
                   "error": f"{type(exc).__name__}: {exc}"[:400]}
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(out, indent=2, ensure_ascii=False)}],
            "isError": not bool(out.get("ok"))}}
    if method in ("prompts/list", "logging/setLevel"):
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"prompts": []} if method == "prompts/list" else {}}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method {method} not found"}}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    if "--status" in argv:
        print(json.dumps(tool_status({}), indent=2, ensure_ascii=False))
        return 0
    if "--version" in argv:
        print(f"{SERVER_NAME} {SERVER_VERSION} (protocol {PROTOCOL})")
        return 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        batch = isinstance(msg, list)
        items = msg if batch else [msg]
        replies = [r for r in (handle(m) for m in items if isinstance(m, dict)) if r is not None]
        if not replies:
            continue
        out = replies if batch else replies[0]
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


# ────────────────────────────────── selftest ──────────────────────────────────


def selftest() -> int:
    """Runs against a fake transport and fake gate. It never touches the operator's ComfyUI."""
    global TRANSPORT
    results = []

    def check(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    calls = []

    def fake(status, body=None):
        def _t(method, path, data=None, headers=None, timeout=20.0):
            calls.append((method, path, data))
            if callable(status):
                return status(method, path, data)
            if isinstance(body, bytes):
                return status, body
            return status, json.dumps(body).encode()
        return _t

    saved = TRANSPORT
    saved_env = dict(os.environ)
    try:
        # --- framing
        r = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        check("initialize answers protocol + name",
              r["result"]["protocolVersion"] == PROTOCOL and r["result"]["serverInfo"]["name"] == SERVER_NAME)
        r = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = [t["name"] for t in r["result"]["tools"]]
        check("tools/list exposes exactly the 7 tools",
              len(listed) == 7 and set(listed) == set(TOOL_IMPLS), str(listed))
        check("every listed tool has an inputSchema",
              all("inputSchema" in t for t in r["result"]["tools"]))
        r = handle({"jsonrpc": "2.0", "id": 3, "method": "no_such_method"})
        check("unknown method is -32601", r["error"]["code"] == -32601)
        r = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "nope", "arguments": {}}})
        check("unknown tool is -32602", r["error"]["code"] == -32602)
        r = handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("notification gets no reply", r is None)

        # --- status: argv withheld by default (it is the operator's command line, not ours)
        stats = {"system": {"comfyui_version": "0.39.2", "argv": ["--listen", "SECRET"],
                            "ram_total": 33413443584, "ram_free": 15532331008},
                 "devices": [{"name": "cuda:0", "type": "cuda", "vram_total": 12466847744,
                              "vram_free": 7495767096, "torch_vram_total": 872415232}]}
        TRANSPORT = fake(200, stats)
        out = tool_status({})
        check("status reports version", out.get("comfyui_version") == "0.39.2")
        check("status converts bytes to MiB",
              out["devices"][0]["vram_free_mib"] == 7148 and out["ram_free_mib"] == 14812,
              str(out["devices"]))
        check("argv withheld by default", "argv" not in out and out.get("argv_omitted"))
        out2 = tool_status({"include_argv": True})
        check("argv available on request", out2.get("argv") == ["--listen", "SECRET"])

        # --- a dead server is UNAVAILABLE, never a refusal
        TRANSPORT = fake(None, b"connection refused")
        out = tool_status({})
        check("dead server -> UNAVAILABLE with the url",
              out["state"] == "UNAVAILABLE" and "127.0.0.1" in out["url"])

        # --- queue: ALLOW submits, DENY does not touch the wire
        graph = {"3": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sd_xl_base.safetensors"}},
                 "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox"}}}
        TRANSPORT = fake(200, {"prompt_id": "abc-123", "number": 1})
        calls.clear()
        out = tool_queue({"workflow": graph}, decide=lambda t: {"state": "ALLOW", "free_mib": 8000,
                                                               "need_mib": 5919, "rule": "image-diffusion"})
        check("ALLOW submits and returns prompt_id",
              out["ok"] and out["prompt_id"] == "abc-123" and out["submitted"] is True)
        check("submit body carries an API graph and client_id",
              calls and json.loads(calls[-1][2])["prompt"]["6"]["class_type"] == "CLIPTextEncode"
              and "client_id" in json.loads(calls[-1][2]))
        check("gate numbers ride along in the answer",
              out["rule"] == "image-diffusion" and out["free_mib"] == 8000)

        calls.clear()
        out = tool_queue({"workflow": graph}, decide=lambda t: {"state": "DENY", "free_mib": 600,
                                                                "need_mib": 9966, "holders": ["UnrealEditor 2118MiB"],
                                                                "why": "butuh"})
        check("DENY refuses", out["ok"] is False and out["reason"] == "VRAM_GATE")
        check("DENY names the holder", out["holders"] == ["UnrealEditor 2118MiB"])
        check("DENY sends nothing to ComfyUI", calls == [], str(calls))

        calls.clear()
        out = tool_queue({"workflow": graph}, decide=lambda t: {"state": "UNKNOWN", "why": "no meter"})
        check("UNKNOWN is refused, not allowed", out["ok"] is False and out["gate_state"] == "UNKNOWN")
        check("UNKNOWN sends nothing", calls == [])

        calls.clear()
        out = tool_queue({"workflow": graph}, decide=lambda t: {"state": "NOT_GPU_WORK"})
        check("non-GPU work runs without a number", out["ok"] and calls and out["gate_state"] == "NOT_GPU_WORK")

        # --- a 400 from ComfyUI is a fact about the graph (measured: queues nothing)
        TRANSPORT = fake(400, {"error": {"type": "missing_node_type", "message": "not found"},
                               "node_errors": {}})
        out = tool_queue({"workflow": graph}, decide=lambda t: {"state": "NOT_GPU_WORK"})
        check("REJECTED_BY_COMFY reports the error and submitted=false",
              out["state"] == "REJECTED_BY_COMFY" and out["submitted"] is False
              and out["error"]["type"] == "missing_node_type")

        # --- format guards run before anything is sent
        calls.clear()
        TRANSPORT = fake(200, {"prompt_id": "x"})
        ui = {"nodes": [{"id": 1}], "links": [], "last_node_id": 1}
        out = tool_queue({"workflow": ui})
        check("UI export is named, not posted", out["error"] == "UI_EXPORT_NOT_API_FORMAT" and calls == [])
        out = tool_queue({"workflow": {"1": {"inputs": {}}}})
        check("graph missing class_type is named", out["error"] == "NOT_API_FORMAT")
        out = tool_queue({})
        check("no workflow is named", out["error"] == "NO_WORKFLOW")
        out = tool_queue({"workflow": graph, "path": "/tmp/x.json"})
        check("inline + path is AMBIGUOUS_INPUT", out["error"] == "AMBIGUOUS_INPUT")

        # --- gate text sees the class and the prompt, not the layout
        t = graph_text({"9": {"class_type": "TNoodle", "inputs": {"noise_seed": 1234,
                                                                  "a": ["1", {"b": "TRELLIS pipeline"}]}}},
                       "note here")
        check("gate text sees class, strings and note",
              "TNoodle" in t and "TRELLIS pipeline" in t and "note here" in t)
        check("gate text ignores widget numbers", "1234" not in t)

        # --- object_info: unknown names answer 200 {} and must not be reported as found
        TRANSPORT = fake(lambda m, p, d: (200, json.dumps(
            {"KSampler": {"category": "sampling", "input": {"required": {}}, "output": ["LATENT"]}}
            if "KSampler" in p else {}).encode()))
        out = tool_object({"names": ["KSampler", "NoSuchNodeXYZ"]})
        check("known node returned", out["found"] == ["KSampler"])
        check("200-with-empty-body is NOT a found node", out["missing"] == ["NoSuchNodeXYZ"])
        out = tool_object({"names": []})
        check("empty name list refused rather than pulling the catalog", out["error"] == "NO_NAMES")
        out = tool_object({"names": [f"N{i}" for i in range(25)]})
        check("lookup cap enforced", out["error"] == "TOO_MANY_NAMES")
        # the names page must page, and never echo the catalog it read
        catalog = {f"Node{i:02d}": {"category": "x"} for i in range(12)}
        TRANSPORT = fake(200, catalog)
        page = tool_object({"action": "names", "limit": 5, "offset": 5})
        check("names returns a page, not the catalog",
              page["count"] == 12 and page["returned"] == 5 and page["names"] ==
              [f"Node{i:02d}" for i in range(5, 10)], str(page))
        check("names reports the window so a caller can ask for the next one",
              page["offset"] == 5 and page["next_offset"] == 10)
        check("names never carries node bodies",
              all(isinstance(n, str) for n in page["names"]))

        # --- mutations need a hand from the operator
        calls.clear()
        out = tool_job({"action": "interrupt"})
        check("interrupt needs confirm", out["error"] == "CONFIRM_REQUIRED" and calls == [])
        out = tool_free({})
        check("free needs confirm", out["error"] == "CONFIRM_REQUIRED" and calls == [])
        TRANSPORT = fake(200, {})
        calls.clear()
        out = tool_job({"action": "interrupt", "confirm": True})
        check("confirmed interrupt posts /interrupt", out["ok"] and calls[-1][:2] == ("POST", "/interrupt"))

        # --- history shape: outputs are flattened, missing rows are named
        hist_body = json.dumps({"p1": {
            "status": {"status_str": "success", "completed": True, "messages": []},
            "outputs": {"9": {"images": [{"filename": "f.png", "subfolder": "", "type": "output"}]}}}}).encode()

        def hist(m, p, d):
            return (200, hist_body) if p == "/history/p1" else (404, b"{}")
        TRANSPORT = fake(hist)
        out = tool_job({"action": "status", "prompt_id": "p1"})
        check("history returns the output file",
              out["ok"] and out["outputs"][0]["filename"] == "f.png" and out["state"] == "success")
        out = tool_job({"action": "status", "prompt_id": "other"})
        check("a queued job has no history row, and says so", out["state"] == "NOT_IN_HISTORY")
        out = tool_job({"action": "status"})
        check("status without id is named", out["error"] == "NO_PROMPT_ID")
        TRANSPORT = fake(200, {"queue_running": [[5, "r1", {"1": {}}]], "queue_pending": [[6, "p2", {}]]})
        out = tool_job({"action": "list"})
        check("list splits running from pending",
              out["running"][0]["prompt_id"] == "r1" and out["pending"][0]["prompt_id"] == "p2")

        # --- fetch hashes what it writes
        TRANSPORT = fake(200, b"\x89PNG fake bytes")
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = tool_fetch({"filename": "a.png", "out_dir": td})
            check("fetch writes and hashes", out["ok"] and Path(out["path"]).is_file()
                  and len(out["sha256"]) == 64)
            out = tool_fetch({"filename": "a.png", "out_dir": td})
            check("fetch does not clobber silently", Path(out["path"]).name != "a.png", out["path"])
        out = tool_fetch({})
        check("fetch needs a filename", out["error"] == "NO_FILENAME")

        # --- upload builds a real multipart body
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "cat.png"
            src.write_bytes(b"\x89PNG123")
            calls.clear()
            TRANSPORT = fake(200, {"name": "cat.png", "subfolder": "", "type": "input"})
            out = tool_upload({"path": str(src)})
            body = calls[-1][2].decode("latin-1")
            check("upload posts filename + bytes + overwrite field",
                  out["ok"] and 'filename="cat.png"' in body and "\x89PNG123" in body
                  and 'name="overwrite"' in body)
            check("upload returns what LoadImage needs",
                  out["use_in_graph"]["class_type"] == "LoadImage"
                  and out["use_in_graph"]["inputs"]["image"] == "cat.png")
            out = tool_upload({"path": str(Path(td) / "ghost.png")})
            check("missing local file named", out["error"] == "PATH_NOT_FOUND")

        # --- the gate bridge itself, both sides of "no workstation here"
        os.environ["IRSOVKA_COMFY_GATE"] = "off"
        check("gate=off is a named mode", vram.mode() == "off")
        d = vram.decide("run TRELLIS image-to-3D")
        check("gate=off says BYPASSED, not ALLOW", d["state"] == "BYPASSED", str(d))
        os.environ.pop("IRSOVKA_COMFY_GATE")
        os.environ["AI_STATION_HOME"] = "/nonexistent-irsfoka"
        check("an AI_STATION_HOME with no gate in it means no gate, not the home fallback",
              vram.mode() == "absent", vram.workstation_root() and str(vram.workstation_root()))
        d = vram.decide("run TRELLIS image-to-3D")
        check("absent gate refuses heavy work", d["state"] == "UNKNOWN" and d["gate"] == "absent")
        d = vram.decide("rename a folder")
        check("absent gate submits light work but marks it unaudited",
              d["state"] == "ALLOW" and d.get("unaudited") is True and d["gate"] == "absent", str(d))
        d = vram.decide("run flux txt2img")
        check("absent gate knows diffusion is heavy", d["state"] == "UNKNOWN", str(d))
        # and the refusal really blocks the wire
        calls.clear()
        TRANSPORT = fake(200, {"prompt_id": "should-not-happen"})
        out = tool_queue({"workflow": {"1": {"class_type": "UNETLoader",
                                             "inputs": {"unet_name": "z-image-turbo.safetensors"}}}})
        check("absent gate blocks the submit", out["ok"] is False and calls == [], str(out))
        # light work still gets through when the gate is absent, so the server degrades
        # rather than dying, and the answer says the decision was unaudited.
        calls.clear()
        out = tool_queue({"workflow": {"1": {"class_type": "EmptyImage",
                                             "inputs": {"width": 512, "height": 512}}}})
        check("absent gate still submits light work, stamped unaudited",
              out["ok"] and out["gate_state"] == "ALLOW" and calls[-1][:2] == ("POST", "/prompt"))
        os.environ.pop("AI_STATION_HOME")
        # explicit bypass is a named state, never mistaken for a cleared one
        os.environ["IRSOVKA_COMFY_GATE"] = "off"
        calls.clear()
        out = tool_queue({"workflow": {"1": {"class_type": "UNETLoader",
                                             "inputs": {"unet_name": "trellis.safetensors"}}}})
        check("gate=off lets heavy work through and says BYPASSED",
              out["ok"] and out["gate_state"] == "BYPASSED", str(out))
        os.environ.pop("IRSOVKA_COMFY_GATE")

        # --- a tool that throws answers with a fact, not our stack trace
        def boom(args):
            raise RuntimeError("kaboom")
        TOOL_IMPLS["_boom"] = boom
        TOOLS.append({"name": "_boom", "description": "test only",
                      "inputSchema": {"type": "object", "properties": {}}})
        r = handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                    "params": {"name": "_boom", "arguments": {}}})
        check("exception becomes SERVER_ERROR, isError set",
              r["result"]["isError"] is True and "kaboom" in r["result"]["content"][0]["text"])
        TOOL_IMPLS.pop("_boom"); TOOLS.pop()

        # --- a broken meter reading must not become a fabricated number
        TRANSPORT = fake(200, {"system": {}, "devices": [{"name": "cpu", "type": "cpu"}]})
        out = tool_status({})
        check("no devices with bytes -> no invented MiB",
              out["ok"] and out["devices"][0]["vram_free_mib"] is None)
        TRANSPORT = fake(200, {"system": {"comfyui_version": 0.39}, "devices": []})
        out = tool_status({})
        check("empty device list reported as empty, not zero", out["devices"] == [])

        # --- framing survives garbage on stdin
        import io
        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin = io.StringIO("not json\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 77, "method": "tools/list"}) + "\n")
        buf = io.StringIO()
        sys.stdout = buf
        try:
            rc = main([])
        finally:
            sys.stdin, sys.stdout = saved_in, saved_out
        lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
        check("garbage line gets a parse error, valid line still answered",
              rc == 0 and lines[0]["error"]["code"] == -32700 and lines[1]["id"] == 77)
    finally:
        TRANSPORT = saved
        os.environ.clear()
        os.environ.update(saved_env)

    # --- the dependency claim in README.md is checked, not asserted: a third-party import
    #     here would quietly put a licence in the mouth of a server that promises it has none.
    import ast
    allowed = {"__future__", "ast", "hashlib", "io", "json", "os", "pathlib", "re", "sys",
               "tempfile", "urllib", "uuid", "vram", "gpu_gate"}
    offenders = {}
    here = Path(__file__).resolve().parent
    for fname in ("server.py", "vram.py"):
        try:
            tree = ast.parse((here / fname).read_text(encoding="utf-8"))
        except Exception as exc:
            offenders[fname] = f"unparseable: {exc}"
            continue
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        extra = sorted(found - allowed)
        if extra:
            offenders[fname] = extra
    results.append(("stdlib-only (README promises it)", not offenders, str(offenders)))

    failed = [n for n, ok, _ in results if not ok]
    for n, ok, detail in results:
        if not ok:
            print(f"FAIL  {n}  {detail}")
    print(f"selftest: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
