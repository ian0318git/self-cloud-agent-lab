# self-cloud-agent-lab

**English** | [繁體中文](README.zh-TW.md)

A self-hosted AI platform that runs **inside free cloud quotas** — your own model,
your own data, tools over MCP, and an agent that works autonomously.

This repository exists to answer one question honestly: **does the architecture
hold, and is the model good enough?** Every claim here is either measured or
explicitly labelled as not measured. Where a claim failed, the failure was kept.

> **This is a validation sandbox, not a persistent service.**
> Codespaces' free tier buys about **2 hours a day**. Running this 24/7 would
> exhaust the whole monthly allowance in **roughly 2.5 days** — and storage is
> billed while a codespace merely *exists*, stopped or not. The arithmetic is in
> [the handbook](docs/HANDBOOK.md#why-this-is-not-a-persistent-service).

---

## Status

| Track | State |
|---|---|
| **Phase 1** — the local stack: Ollama + Open WebUI on Codespaces | ✅ **Working.** Verified end to end. |
| **Phase 2** — RAG and MCP tools | ✅ **Working.** The four RAG items pass through both the app's own code path and the HTTP path; MCP tool calling is verified. |
| **Phase 3** — an agent runtime (LangGraph) | 📐 **Measured, not built.** As of 2026-09-21 all six unknowns have been measured, and one item's *premise did not survive* measurement. A passing item only ever meant that trying the next one was no longer wasted effort. |
| **Phase 4** — persistent storage | 📄 **A proposal**, not code. [`docs/PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html) |
| **`endpoint`** — a second, larger LLM on a rented GPU | ⚠️ **Not usable end to end.** See below. |

### The `endpoint` track does not currently work

[`docs/ENDPOINT.md`](docs/ENDPOINT.md) is about attaching a **second LLM runtime**
on somebody else's GPU, without changing a single line above it. The engine itself
comes up: a real boot on 2026-09-30 reached

```
→ ENGINE HEALTHY → Endpoint IS ONLINE
```

But the boot **cannot read the tunnel URL back**, and everything downstream is
gated on that URL — the readiness wait, the registration, the printed endpoint.
The measured result of that same boot was

```
→ AVAILABLE MODELS: []
```

and **inference never ran once.** Closing this gap is unsolved; the honest options,
and why two of them are struck out, are in [D-073](DECISIONS.md) and
[D-075](DECISIONS.md). That path now exits **2** instead of claiming success.

**Nothing else depends on it.** The Codespaces stack, Phases 1–3, and the
OpenAI-protocol probe all work without it.

---

## Why this exists

The original question was whether a fully self-hosted AI platform — your own
model, your own documents, your own tools — can run on nothing but free tiers.
The answer turned out to be *yes for the architecture, no for persistence*, and
both halves are documented rather than rounded off.

The project is scoped as a **validation sandbox**: a place to find out whether the
design holds, then delete it. That scoping decision is [D-001](DECISIONS.md) — it
is the first entry in the decision log and the one that shapes all the others.

Two habits run through everything here:

- **A number that was not measured is labelled as not measured.** Several
  long-standing "facts" turned out to be unsourced when someone finally checked;
  those corrections are kept, not quietly fixed.
- **A document that says "there is rot here" is doing its job.** Known and
  recorded limitations are collected in
  [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md), red verifiers
  included.

---

## Architecture

Phase 1, the part implemented in this repository:

```
┌─────────────────────────────────────────────┐
│  GitHub Codespace (2-core / 8GB / 32GB)     │
│                                             │
│   ┌───────────────────────────────────┐     │
│   │  Docker network: ai-net           │     │
│   │                                   │     │
│   │  ┌──────────┐      ┌───────────┐  │     │
│   │  │  ollama  │◄─────│ open-webui│  │     │
│   │  │  :11434  │      │   :8080   │  │     │
│   │  └──────────┘      └─────┬─────┘  │     │
│   │   (not published)         │        │     │
│   └───────────────────────────┼───────┘     │
│                               │             │
│                      port forward :3000     │
└───────────────────────────────┼─────────────┘
                                ▼
                        Browser (Open WebUI)
```

| Component | Role |
|---|---|
| **Ollama** | Local inference engine. **Never published** — reachable only inside `ai-net` ([D-003](DECISIONS.md)). |
| **Qwen3 4B** | Default model: 2.5 GB, 256K context, tool calling. |
| **Open WebUI** | Chat UI, built-in RAG, native MCP support. |
| **cloudflared** | *Optional*, off by default. An **outbound** tunnel for remote access — no inbound port is opened. See [Securing remote access](docs/HANDBOOK.md#securing-remote-access). |
| **mcp-test-server** | Starts with `docker compose up -d`; declares no profile, so unlike `cloudflared` it is not optional. Belongs to Phase 2. |

Only **one** port is ever forwarded — `3000`. Three containers, one door.

---

## Quick start

### On Codespaces

1. **Code → Codespaces → Create codespace on main**
2. Pick the **2-core / 8GB** machine. *Do not pick 4-core — it quadruples your quota burn.*
3. Wait. `postCreateCommand` runs `scripts/up.sh` for you, including the model download.
4. **PORTS** panel → open the URL for port **3000**.
5. Register the first account — **it automatically becomes the administrator.**
6. Lock registration before you do anything else:
   ```bash
   bash scripts/lock-signup.sh
   ```

### Locally

Docker and Compose v2, **at least 8 GB RAM** (on a 4 GB machine, set
`OLLAMA_MODEL=qwen3:1.7b` in `.env`).

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

Then open <http://localhost:3000>.

> **Deploying to a fresh VPS instead?** `bash scripts/deploy-vps.sh --model M --num-ctx N`
> does the whole thing in one command. It requires Docker and Compose v2 to
> already be installed — it checks, tells you which of three causes it found, and
> stops. It never installs them. See
> [Moving to a VPS](docs/HANDBOOK.md#moving-to-a-vps) for sizing and
> [Hardware sizing](docs/HANDBOOK.md#hardware-sizing) for which machine to buy.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Startup fails with **`WEBUI_SECRET_KEY 未設定`** | Deliberate, not a bug. `bash scripts/up.sh` generates the key. **Do not** leave it empty to get past this — it breaks Phase 2's MCP tools with `Error decrypting tokens` after every rebuild. |
| **Model download interrupted** | `bash scripts/pull-model.sh`. Ollama claims to resume partial downloads; **this project has not measured that claim.** The disk side *is* measured: a pull peaks at the model's final size and nothing more ([D-058](DECISIONS.md)). |
| **Responses are very slow** | Expected on 2 vCPUs — single-digit tokens/sec. Use a smaller model, or shorten `OLLAMA_KEEP_ALIVE`. |
| **Containers keep restarting / OOM-killed** | Out of memory. Confirm `OLLAMA_MODEL` is not `qwen3:8b` or larger; check `docker stats`; shorten `OLLAMA_KEEP_ALIVE`. |
| **No port 3000 URL in Codespaces** | Ports are **private by default** and only appear once there is traffic. Add port `3000` manually in the PORTS panel. |

The [handbook's troubleshooting section](docs/HANDBOOK.md#troubleshooting) has the
long form, plus a list of Codespaces gotchas that are easy to lose hours to.

---

## Documentation map

| Looking for | Go to |
|---|---|
| How to run it — every command, every gate, every gotcha | **[`docs/HANDBOOK.md`](docs/HANDBOOK.md)** |
| *Why* anything is the way it is — 76 numbered decisions | **[`DECISIONS.md`](DECISIONS.md)** |
| The second GPU runtime | [`docs/ENDPOINT.md`](docs/ENDPOINT.md) |
| The raw outputs behind the claims | [`docs/evidence/`](docs/evidence/) — 62 verbatim probe outputs |
| Known rot: verifiers that are red, and why | [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md) |
| Phase 4 storage, as a proposal | [`docs/PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html) |

**The handbook is the long form of this file** — same material, everything spelled
out, including the parts that are still open. It is written in both English and
Chinese; **editing one means editing the other.**

---

## License

This project's own code — the compose files, scripts, and documentation — is
**[MIT licensed](LICENSE)**.

**That does not cover the container images it pulls.** Ollama and mcpo are MIT;
the Qwen3 models are Apache-2.0; **Open WebUI is *not* MIT** — it is BSD-3-style
*plus a branding clause*, which is irrelevant for a one-user proof of concept but
becomes relevant if you ever serve more than 50 people with it. The full table and
the clause itself are in
[the handbook](docs/HANDBOOK.md#license).
