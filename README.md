# self-cloud-agent-lab

**English** | [繁體中文](README.zh-TW.md)

A private AI stack — your own model, your own documents, your own tools — that runs
**inside your own boundary**, and scales from a free sandbox to a rented GPU.

## Why this exists

Hand ChatGPT your company's contracts and you have handed them over. Run the model
on your own machine instead and you need hardware — a 7–8B model wants **~8 GB of
VRAM** before it will say a word to you.

The gap between those two options is not technical. It is that **nobody tells you
what self-hosting actually costs** — in dollars, in tokens per second, in hours of
setup — before you commit. Product pages say "production-ready"; they do not say
*"5 token/s on a CPU VPS"*, which is slower than most people type.

So this repository does the measuring. Every claim in it is either measured or
explicitly labelled as not measured, and the ones that failed are still here.

## What this is

A complete private stack — **Ollama** for inference, **Open WebUI** for chat and
RAG, **MCP** for tools — behind a network design where **nothing is ever exposed**:
Ollama is reachable only inside the Docker network ([D-003](DECISIONS.md)), and
remote access is an **outbound** tunnel, so no inbound port is ever opened.

You keep the data. You keep the model. You also keep the bill — which is the part
the brochures leave out, so every step below carries its measured price.

## The path

Three steps, from free to capable. The third one needs a hand, and that is
measured too.

| | Step | What it buys you | State |
|---|---|---|---|
| **1** | **Free sandbox** — GitHub Codespaces | Prove the architecture works, for $0 | ✅ Working |
| **2** | **Your own VPS** — `deploy-vps.sh` | A persistent instance that is actually yours | ✅ Script verified end to end |
| **3** | **GPU** — a second, larger model | 14B–70B at 20–60 token/s *(projected)*, instead of 7B at 5–10 | ⚠️ Works — one step by hand |

**Step 1 is free, and it is a sandbox by measurement rather than by preference.**
The free tier buys about **2 hours a day**; running 24/7 would exhaust the month in
**~2.5 days**, and storage is billed while the codespace merely *exists*, stopped or
not. Validate on it, then delete it ([D-001](DECISIONS.md)).

**Step 2 is the finished one.** The deploy script has been verified end to end. What
has *not* been verified is the machine: no VPS has been rented yet, so the token/s
figures for any particular plan are a projection from measured calibration points,
labelled as a projection ([D-028](DECISIONS.md), [D-047](DECISIONS.md)).

**What stops at step 3 is the automation, not the engine.** Each route stops in
one place — the GPU compose overlay has never run on real GPU hardware, and the
Kaggle route cannot read its own tunnel URL back, which is **Kaggle's property,
not the GPU's** (your own VPS uses a fixed hostname and has no such step). The
Kaggle route still works by hand; both are detailed below.

If you are here to decide whether self-hosting is worth it at all, step 1 costs
nothing and answers that. If you are here to deploy, step 2 is ready.

---

## Status

| Track | State |
|---|---|
| **Phase 1** — the local stack: Ollama + Open WebUI on Codespaces | ✅ **Working.** Verified end to end. |
| **Phase 2** — RAG and MCP tools | ✅ **Working.** The four RAG items pass through both the app's own code path and the HTTP path; MCP tool calling is verified. |
| **Phase 3** — an agent runtime (LangGraph) | 📐 **Measured, not built.** As of 2026-09-21 all six unknowns have been measured, and one item's *premise did not survive* measurement. A passing item only ever meant that trying the next one was no longer wasted effort. |
| **Phase 4** — persistent storage | 📄 **A proposal**, not code. [`docs/PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html) |
| **`endpoint`** — a second, larger LLM on a rented GPU | ⚠️ **Usable, but one step is by hand.** See below. |

### The `endpoint` track works — with one step done by hand

[`docs/ENDPOINT.md`](docs/ENDPOINT.md) is about attaching a **second LLM runtime**
on somebody else's GPU, without changing a single line above it. The engine itself
comes up: a real boot on 2026-09-30 reached

```
→ ENGINE HEALTHY → Endpoint IS ONLINE
```

But the boot **cannot read the tunnel URL back** — Kaggle's API returns the
kernel log empty while the session is running — and everything downstream is
gated on that URL: the readiness wait, the registration, the printed endpoint.
The measured result of that same boot was

```
→ AVAILABLE MODELS: []
```

**The URL itself is not lost.** It is printed into the notebook's own cell
output, which is what you read in the browser — the handbook's step 5 has always
said so. Read it there and hand it to `connect-endpoint.sh --url`, and the
endpoint wires up: that is how the 2026-09-28 run streamed real tokens (6.4 s,
112 chunks, [D-066](DECISIONS.md)). **What is broken is the readback as an
automated step, not the path.** Closing *that* is unsolved; the honest options,
and why two of them are struck out, are in [D-073](DECISIONS.md) and
[D-075](DECISIONS.md). The one-command flow now exits **2** instead of claiming
success.

**Nothing else depends on it.** The Codespaces stack, Phases 1–3, and the
OpenAI-protocol probe all work without it.

---

## How to read this repository

Two habits run through everything here:

- **A number that was not measured is labelled as not measured.** Several
  long-standing "facts" turned out to be unsourced when someone finally checked;
  those corrections are kept, not quietly fixed.
- **A document that says "there is rot here" is doing its job.** Known and
  recorded limitations are collected in
  [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md), red verifiers
  included.

Every design choice has a numbered, dated entry in **[`DECISIONS.md`](DECISIONS.md)**
recording what was decided, what it overturned, and what would change it. When this
README says *measured*, that is where the measurement lives.

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

### On a VPS of your own

Docker and Compose v2 must **already be installed** — `deploy-vps.sh` checks, tells
you which of three causes it found, and stops. It never installs them.

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/deploy-vps.sh --model qwen3:8b --num-ctx 16384
```

**On a machine with a GPU there is no flag** — the single control is `OLLAMA_GPU`
in `.env` (`auto` / `on` / `off`):

```bash
printf 'OLLAMA_GPU=on\n' >> .env
bash scripts/deploy-vps.sh --model qwen3:14b --num-ctx 16384
```

The script attaches `docker-compose.gpu.yml` and then checks the model is *actually*
running on the GPU — exit `0` requires it. ⚠️ **That overlay has never run on real
GPU hardware**, so treat the first run as the experiment it is.
[Hardware sizing](docs/HANDBOOK.md#hardware-sizing) says which machine to rent;
[Moving to a VPS](docs/HANDBOOK.md#moving-to-a-vps) has the long form.

### On Kaggle — a second, larger model on someone else's GPU

⚠️ **The one-command flow does not work today**, for the reason in the section
above: the engine boots, but it cannot read its own tunnel URL back, so nothing
downstream of that URL ever happens on its own. **The path is still usable by
hand** — that same URL is printed into the notebook's cell output, for a human
to read; [step 5 of the manual](docs/HANDBOOK.md#kaggle-hands-on-manual) says
where.

```bash
uv tool install endpoint-vps   # the CLI is called `endpoint`
endpoint init                  # interactive: username, kernel slug, default model
endpoint -g boot               # GPU T4 ×2 — then read the URL from the notebook
```

The notebook-generator patches in `scripts/` are **required before the first boot,
and in order** — [the Kaggle hands-on manual](docs/HANDBOOK.md#kaggle-hands-on-manual)
has them, and the account has to be phone-verified before any of it works.

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
