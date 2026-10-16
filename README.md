# IRSofka ComfyUI MCP

A stdio [Model Context Protocol](https://modelcontextprotocol.io) server for the ComfyUI you run
yourself. It is **a takeover, not a rewrite**: the base is
[ComfyUI-MCP-Server](https://github.com/joenorton/comfyui-mcp-server) by Joe Norton (Apache-2.0),
vendored verbatim, and this repository adds the station's gate and consent policy on top of its 17
tools. No cloud, no account, no telemetry — and nothing that reaches outside the machine unless you
say so.

Its central behaviour is one line: **a submit that the machine cannot pay for is refused before it is
sent, and the refusal names who is holding the VRAM.** The refusal is installed in code, not asked of
the caller: `guard_submits()` replaces `ComfyUIClient.run_custom_workflow` on the client instance
before the base's tools are registered, and every submit tool in the vendored tree calls that
attribute (`tools/generation.py:105`, `tools/generation.py:415`, `tools/workflow.py:75`), so nothing
reaches `POST /prompt` without a verdict. A blocked submit returns `VRAM_GATE_REFUSED` as data — with
the state, the reason and the opt-in — rather than raising, and the base cannot silently grow a new
submit path that bypasses it. Whether other ComfyUI MCPs do this was not
measured, so no claim is made about them — only about this tree.
On the workstation this was built on, that is not a hypothetical. A 3D job once asked for 9.73 GiB on
a card with 6.38 GiB free, and the only thing standing in the way was a sentence in a documentation
file that the engine making the request was not reading.

```
$ python3 server.py --status | head -20          # read-only, safe
$ python3 server.py --selftest                   # 20 checks, no ComfyUI needed, no request ever sent
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

If the workstation is not on this machine, there is no meter, so `vram.decide()` answers `UNKNOWN`:
a missing guard is not evidence that the work is safe, and the server refuses rather than issuing
a permit it cannot sign. `IRSOFKA_COMFY_GATE=off` is the explicit override for a machine with no
workstation and an operator who accepts OOM risk; it appears in every answer it touches as
`state: "BYPASSED"`.

## Tools

The base contributes 17 tools and the station layer adds two. Names come from the running server
(`--surface-policy`), not from a document:

| group | tools |
|---|---|
| generation and workflow (base) | `generate_image`, `generate_song`, `run_workflow`, `regenerate`, `view_image` |
| jobs and queue (base) | `get_job`, `cancel_job`, `get_queue_status` |
| models, assets, workflows (base) | `list_models`, `list_assets`, `list_workflows`, `get_asset_metadata`, `get_defaults`, `set_defaults`, `get_publish_info`, `set_comfyui_output_root`, `publish_asset` |
| station layer (ours) | `irsofka_gpu_gate`, `irsofka_surface_policy` |

`view_image` is the one tool that can make the machine fetch a URL someone supplied, so it is **not
registered** until `IRSOFKA_COMFY_ALLOW_NETWORK` consents; `irsofka_surface_policy` says whether it is
live. `publish_asset` despite its name writes to a session-scoped local registry — checked, not
assumed — so it is not gated as network.

## Measured on this machine, 2026-10-15 — the layer before the takeover

These numbers were produced by the seven-tool clean-room server that this repository contained until
2026-10-16, and the tool names below (`comfy_status`, `comfy_fetch`, `comfy_free`) belong to that
build. They are kept because they are measurements that happened, not documentation to imitate; the
VRAM-gate behaviour they describe is what the station layer still does, now over the base's tools.
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

## Basis Open-Source dan Provenansi

Repositori ini telah **mengambil alih basis terbuka `joenorton/comfyui-mcp-server`** (Apache-2.0, tag v1.1.1, commit `e0101b2312f30501664dabe4a74c1283c4268eb8`, 49 berkas, 6.846 LOC Python) yang disimpan verbatim di `upstream/`. Klaim clean-room / independen sebelumnya ("nothing was copied, translated or adapted") telah ditarik dan digantikan oleh dokumen [UPSTREAM](UPSTREAM).

Lisensi Apache-2.0 upstream mengizinkan pembuatan karya turunan berlisensi Business Source License 1.1 (dengan Change License MIT) dengan tetap menyertakan hak cipta dan `NOTICE` asli di `upstream/NOTICE`. Lapisan stasiun di `server.py` menambahkan penegakan gerbang VRAM (`vram.py`) dan pembatasan izin akses jaringan eksternal (`IRSOFKA_COMFY_ALLOW_NETWORK`).

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

Run it from a checkout: `python3 server.py`, with `mcp` and `requests` installed. `pip install .`
installs the station layer **only** — the base is loaded from `upstream/` beside `server.py`, and its
internal imports use bare top-level names (`tools`, `models`, `managers`) that must not be installed
into site-packages. There is therefore deliberately no console script: an entry point that cannot work
is worse than none, which is the same lesson the packaging audit produced today. The AST scan in the selftest
confirms our two files import only the standard library plus `mcp` — the third-party imports live in
the vendored base, which is exactly why the dependency list above is not empty. The station's gate is
imported lazily and only when a workstation is present, so it is a bridge, not a dependency.

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
      "env": { "COMFYUI_URL": "http://127.0.0.1:8188" }
    }
  }
}
```

| variable | default | effect |
|---|---|---|
| `COMFYUI_URL` | `http://127.0.0.1:8188` | which ComfyUI to talk to |
| `AI_STATION_HOME` | `$HOME/.ai-station` | where the station gate lives. If set and it holds no gate, the answer is *no gate* — this server does not quietly look somewhere else |
| `IRSOFKA_COMFY_GATE` | unset | `off` disables the gate by operator order; every affected answer then says `BYPASSED` |
| `IRSOFKA_COMFY_ALLOW_NETWORK` | unset (refuse) | registers `view_image`, the one tool that can fetch a caller-supplied URL |
| `COMFYUI_OUTPUT_ROOT` | auto-detected | where ComfyUI writes its images, for `view_image` |
| `COMFY_MCP_ASSET_TTL_HOURS` | `24` | how long a fetched asset stays in the local registry |

## What this server will not do

- It will not run a paid partner node. There is no API key surface here at all.
- It will not hand over the whole node catalog, because doing so once would cost more context than
  most agents have.
- It will not fabricate a measurement. If `nvidia-smi` cannot be read, the answer is `UNKNOWN` and the
  submit is refused, not approved on hope.
- It cannot unload VRAM belonging to another process. Unreal Editor holding 2 GiB is Unreal Editor's
  business; `comfy_free` does not reach it and no MCP call should pretend otherwise.
