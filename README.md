# IRSofka ComfyUI MCP

A stdio [Model Context Protocol](https://modelcontextprotocol.io) server for the ComfyUI you run
yourself. Seven tools over ComfyUI's own HTTP API — no cloud, no account, no telemetry.

Its central behaviour is one line: **a submit that the machine cannot pay for is refused before it is
sent, and the refusal names who is holding the VRAM.** Whether other ComfyUI MCPs do this was not
measured, so no claim is made about them — only about this tree.
On the workstation this was built on, that is not a hypothetical. A 3D job once asked for 9.73 GiB on
a card with 6.38 GiB free, and the only thing standing in the way was a sentence in a documentation
file that the engine making the request was not reading.

```
$ python3 server.py --status | head -20          # read-only, safe
$ python3 server.py --selftest                   # 61 checks, fake transport, never touches your server
```

## The gate

`tools/gpu_gate.py` in [Irsofka AI Workstation](https://github.com/irsofka) owns the decision: which
class of work needs how much, measured from the machine's own meter, and which processes are holding
it. This server calls it and adds no policy of its own. Three states, priced differently:

| state | meaning | what the server does |
|---|---|---|
| `ALLOW` | free ≥ needed | submits, and reports the rule, the free figure and the headroom |
| `DENY` | free < needed | refuses, names the top holders and the shortfall in MiB, sends **nothing** |
| `UNKNOWN` | the meter could not be read | refuses. Absence of a measurement is not evidence of free VRAM |

If the workstation is not on this machine, the server degrades rather than dying: read-only tools keep
working, a submit matching a known-heavy class (`trellis`, `flux`, `sdxl`, `controlnet`, …) is refused
as `UNKNOWN`, and anything else submits **stamped `"unaudited": true`** so a decision made without a
guard can never be mistaken later for one that passed one. `IRSOVKA_COMFY_GATE=off` is the explicit
override for a machine with no workstation and an operator who accepts OOM risk; it appears in every
answer it touches as `gate_state: "BYPASSED"`.

## Tools

| tool | endpoint | note |
|---|---|---|
| `comfy_status` | `GET /system_stats` | version, per-device VRAM in MiB, live gate report. ComfyUI's own `argv` is **withheld** unless `include_argv=true` — it can carry tokens and it is your machine, not our data |
| `comfy_queue` | `POST /prompt` | behind the gate. Takes an API-format graph inline or from a file |
| `comfy_job` | `GET /history/<id>`, `GET /queue`, `POST /interrupt`, `POST /queue {clear}` | `interrupt` and `clear` need `confirm=true`: they stop work that may be someone else's |
| `comfy_object` | `GET /object_info/<name>` | up to 20 named classes, or `action=names` for the class-name list only |
| `comfy_upload` | `POST /upload/image` | returns the exact name a `LoadImage` node must reference |
| `comfy_fetch` | `GET /view` | writes to disk and returns **sha256**, so a run is compared by content and not by filename |
| `comfy_free` | `POST /free` | needs `confirm=true`; applies at the next queue iteration, and cannot touch VRAM held by another process |

## Measured on this machine, 2026-10-15

Nothing here is quoted from documentation. RTX 3060 12 GB, ComfyUI Desktop standalone:

| fact | value |
|---|---|
| `comfyui_version` from `/system_stats` | `0.39.2` (a tool on this machine reported `0.40.0` the same day — the endpoint is the one that is true) |
| VRAM free, `GET /system_stats` | 7149 MiB of 11889 MiB |
| VRAM free, whole-card meter | 7498 MiB of 12288 MiB |
| top holders | UnrealEditor 2118 MiB, ComfyUI's own python 1624 MiB |
| `GET /object_info/<one node>` | 891 bytes |
| `GET /object_info` (whole catalog) | 1 951 939 bytes across 976 node classes — never returned to a caller; `action=names` gives the list only |
| `POST /prompt` with an unknown class | `400 {"type":"missing_node_type"}` **and nothing queued** — verified by re-reading `/queue` |
| `GET /object_info/<unknown node>` | `200` with `{}`. The status code is not the verdict; an empty body is |
| gate decision on a 3D submit, live | `DENY`, rule `3d-trellis`, need 9966 MiB, free 7498 MiB, shortfall 2468 MiB, holders named |
| a submit the gate allowed, live | `prompt_id 166cbff0…`, rule `image-diffusion`, need 5919 MiB against 7241 MiB free, finished in **21.0 s** |
| its output, fetched by `comfy_fetch` | 1 290 126 bytes, `PNG 832 x 1216 8-bit RGB`, sha256 `2ea33e87…e4e5` |

The submit above needed no hand-written node wiring: `GET /history` hands back the graph that produced
each past run inside `prompt[2]`, which is a valid API-format graph on the machine that ran it. Read a
finished job, change the seed and the prompt, submit that.

Two numbers for "free VRAM" are on purpose. ComfyUI reports what it can allocate inside its own
torch pool; the whole-card meter reports what the card has left, including what other processes hold.
The gate decides on the whole-card number, because an OOM does not respect the boundary between
"my process" and "someone else's".

## How this was written, and what that costs

Written against ComfyUI's own documented HTTP API, with the Python standard library, in the same
hand-rolled JSON-RPC-over-stdio style this workstation already uses for its own desktop MCP server.
**No line of any upstream MCP server is in this tree — nothing was copied, translated or adapted.**
Said plainly because the strict meaning of "clean room" is that the implementers never saw the
original, and that is not what happened: `comfy-mcp`'s `server.py` was read once, to count its tool
definitions for a documentation correction, and its `LICENSE` and `NOTICE` were read for the analysis
below. Exposure happened; derivation did not. Read it as the whole answer rather than inferring more
protection from the phrase "clean room" than the history supports.

That is a legal choice as much as a technical one. ComfyUI is **GPL-3.0-or-later**; the two better-known
MCP servers around it are **AGPL-3.0** (one of them dual-licensed "AGPL or commercial"). A work built
by translating or adapting their code would carry those terms. A program that talks to a server over
its network API is a separate work, so this repository is free to be under its own licence — and the
licence it is under is the one below.

## License

Business Source License 1.1, `LICENSE` in this directory. Licensor **PT IRSofka Data Teknologi**, Additional Use
Grant **None**, Change Date **four years after each version is first made publicly available** (each
version carries its own date), Change License **MIT**.

In practice:

- you may read it, run it, copy it, modify it, build on it, and use it for anything that is not
  production;
- production use — running it as part of something you operate for users or for revenue — needs a
  paid commercial licence from the Licensor;
- every version of this code becomes MIT four years after it is published, so nothing here is
  locked forever; the newer versions simply cost a licence until they age;
- the name and logo are not licensed. Fork the code freely, do not call your fork IRSofka.

Licensor is **PT IRSofka Data Teknologi**, registered in Indonesia under NIB **1258 0003 4132 6** (OSS) and DUNS **5645 3618 4**.
Commercial licence and showcase enquiries: <admin@irsofka.co.id> — <https://irsofka.co.id>

## Registering it

Run it directly: `python3 server.py`. There is nothing to install — the AST scan in the selftest
confirms both modules import only the standard library (`gpu_gate` is imported lazily and only when
a workstation is present, so it is a bridge, not a dependency).

`pyproject.toml` is committed for packaging, but **it has not been built here**: this machine's system
Python has no `setuptools` and `python3 -m venv` cannot seed one offline, so `python -m build` was
never run. Treat the wheel as unverified until someone builds it. Registration, not installation, is
the supported path.

Registration is a statement about the machine, so it is not in this repository. Pick the one for your
client; both CLIs read their own file.

```json
{
  "mcpServers": {
    "irsofka-comfyui": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/irsofka-comfyui-mcp/server.py"],
      "env": { "IRSOVKA_COMFY_URL": "http://127.0.0.1:8188" }
    }
  }
}
```

| variable | default | effect |
|---|---|---|
| `IRSOVKA_COMFY_URL` | `http://127.0.0.1:8188` | which ComfyUI to talk to |
| `AI_STATION_HOME` | `~/.ai-station` | where the gate lives. If set and it holds no gate, the answer is *no gate* — this server does not quietly look somewhere else |
| `IRSOVKA_COMFY_GATE` | unset | `off` disables the gate by operator order; every affected answer then says `BYPASSED` |

## What this server will not do

- It will not run a paid partner node. There is no API key surface here at all.
- It will not hand over the whole node catalog, because doing so once would cost more context than
  most agents have.
- It will not fabricate a measurement. If `nvidia-smi` cannot be read, the answer is `UNKNOWN` and the
  submit is refused, not approved on hope.
- It cannot unload VRAM belonging to another process. Unreal Editor holding 2 GiB is Unreal Editor's
  business; `comfy_free` does not reach it and no MCP call should pretend otherwise.
