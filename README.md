# IRSofka ComfyUI MCP

A stdio [Model Context Protocol](https://modelcontextprotocol.io) server for ComfyUI, written
clean-room from scratch for the Irsofka AI Workstation. Licensed **Business Source License 1.1**
for the entire work, with Change License **Apache License, Version 2.0**.

No third-party code is vendored or copied: the protocol was implemented from the receiver side
over ComfyUI's public HTTP API (default `http://127.0.0.1:8188`).

```bash
$ python3 server.py --surface-policy  # what is registered right now, and what is pending consent
$ python3 server.py --selftest        # 21 checks, no ComfyUI needed, no request ever sent
```

## Architecture and Safety Policies

1. **Protocol from the receiver side**: communicates with ComfyUI over HTTP loopback
   (`POST /prompt`, `GET /history/{id}`, `GET /queue`, `GET /view`, `GET /system_stats`).
   If ComfyUI is not reachable, calls return structured `error_type: "SERVICE_OFFLINE"` with endpoint
   hints, never crashing.
2. **Submit guarding**: `guard_submits()` intercepts all workflow execution submissions before
   `POST /prompt`. Workloads that cannot be cleared by the VRAM gate are stopped locally and
   return structured data (`error: "VRAM_GATE_REFUSED"`), never raising exceptions.
3. **Outbound network gating**: tools reaching remote URLs (`view_image` with external URLs)
   are **not registered** unless `IRSOFKA_COMFY_ALLOW_NETWORK=1` is set.
4. **Station VRAM gate**: `vram.decide()` guards GPU workloads. On unmetered machines without
   a workstation gate, it answers `UNKNOWN` and refuses rather than issuing an unmeasured permit.
   `IRSOFKA_COMFY_GATE=off` is the explicit override producing `BYPASSED`.

## Tools

| group | tools |
|---|---|
| local workflows & execution (ours) | `generate_image`, `run_workflow`, `get_queue_status`, `get_job`, `cancel_job`, `list_models` |
| network tools (ours, gated) | `view_image` (with remote URLs, requires `IRSOFKA_COMFY_ALLOW_NETWORK=1`) |
| station layer (ours) | `irsofka_gpu_gate`, `irsofka_surface_policy` |

## License

Business Source License 1.1, `LICENSE` in this directory. Licensor **PT IRSofka Data Teknologi**,
Change Date **four years after publication**, Change License **Apache License, Version 2.0**.
See `NOTICE` for brand attribution.
