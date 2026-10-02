# self-cloud-agent-lab

**A private AI stack that stays inside your boundary.**

Your own models, your own documents, your own tools — measured from a free sandbox
to rented GPU infrastructure.

[English](README.md) · [繁體中文](README.zh-TW.md)

> **Measured, not assumed.**
> What works, what fails, what it costs, and what is still only a projection.

---

## Why this exists

Sending sensitive documents to a hosted AI service means trusting someone else's
infrastructure. Running the model yourself means owning the hardware, the setup,
and the bill.

The gap is not technical. It is that **nobody tells you what self-hosting actually
costs** — in dollars, in tokens per second, in hours of setup — before you commit.
Product pages say "production-ready"; they do not say *"5 token/s on a CPU VPS"*,
which is slower than most people type.

Most guides show how to make the pieces work. This one measures what happens when
you put them together.

**Start small. Measure it. Keep the failures. Scale only when the evidence says so.**

---

## What it is

A complete private stack — inference, chat, RAG, and tool calling — behind a
network design in which **internal inference services are never exposed**
([D-003](DECISIONS.md)).

| Component | Role |
| --- | --- |
| **Ollama** | Local inference engine. **Never published** — reachable only inside the Docker network. |
| **Open WebUI** | Chat UI, built-in RAG, native MCP support. |
| **mcp-test-server** | Tool calling, over MCP. Belongs to Phase 2. |
| **cloudflared** | *Optional*, off by default. An **outbound** tunnel — no inbound port is opened. |

Only **port 3000** is ever forwarded; every other service stays inside the Docker
network. You keep the data, the model — and the bill, which is the part brochures
leave out.

---

## Status

| Track | State |
| --- | --- |
| **Phase 1** — local stack | ✅ **Working.** Verified end to end. |
| **Phase 2** — RAG + MCP | ✅ **Working.** Verified through both the app's own code path and the HTTP path. |
| **Phase 3** — agent runtime | 📐 **Measured, not built.** |
| **Phase 4** — persistent storage | 📄 **Proposal only** — [`PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html). |
| **GPU endpoint** | ⚠️ **Usable — one manual step remains.** |

**"Measured, not built" is precise here.** As of 2026-09-21 all six open Phase 3
unknowns had been measured, and one item's *premise did not survive* the
measurement.

---

## What you can try

* Run a private LLM chat stack in a Codespace — no GPU, no local install.
* Ask questions over your own documents with RAG.
* Call tools through MCP.
* Keep Ollama off the public network — the default, not a setting to remember.
* Boot a larger model on a rented GPU — without renting one for the rest of the
  stack.

The first four need no GPU at all.

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

And, optionally, beyond that boundary:

```
                        Browser
                            │
                            ▼
             outbound tunnel (no inbound port)
                            │
                            ▼
                       GPU Endpoint
                       Larger models
```

### Design principles

* **Private by default** — Ollama is never published directly.
* **Outbound access** — a tunnel, not inbound ports.
* **Evidence over assumptions** — measurements, not estimates.
* **Incremental scaling** — validate the architecture before paying for more.

---

## Quick Start

All four routes start from a clone; [the handbook](docs/HANDBOOK.md#quick-start) has
every command and gate.

### 1. Codespaces — start here

Create a Codespace from `main` using **2 cores / 8 GB RAM**. **Do not pick 4-core —
it quadruples your quota burn.** The devcontainer starts the stack, model download
included. Open forwarded port **3000**, create the first account (**it automatically
becomes the administrator**), then lock registration:

```bash
bash scripts/lock-signup.sh
```

The default model is **Qwen3 4B** (2.5 GB, 256K context, tool calling).

> **Codespaces is a sandbox by measurement, not by preference** — the free tier buys
> about **2 hours a day**, and storage is billed while it merely *exists*, stopped or
> not. Validate on it, then delete it ([D-001](DECISIONS.md)).

### 2. Local machine

Docker Compose v2 and at least **8 GB RAM**. On a 4 GB machine, set
`OLLAMA_MODEL=qwen3:1.7b` in `.env`.

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

Open <http://localhost:3000>.

### 3. Own VPS

Docker and Compose v2 must **already be installed** — the script checks, names
which of three causes it found, and stops. It never installs them.

```bash
bash scripts/deploy-vps.sh --model qwen3:8b --num-ctx 16384
```

On a GPU machine: set `OLLAMA_GPU` in `.env`. The script attaches
`docker-compose.gpu.yml` and verifies the model is *actually* on the GPU — **exit
`0` requires it.** The script itself *is* verified end to end ([D-028](DECISIONS.md)).

> ⚠️ **That overlay has never run on real rented GPU hardware.** Treat the first run
> as the experiment it is. [Hardware sizing](docs/HANDBOOK.md#hardware-sizing) says
> which machine to rent.

### 4. GPU endpoint

A larger model can be booted on a GPU runtime such as Kaggle — T4 ×2, with no GPU
rented for the rest of the stack.

```bash
uv tool install endpoint-vps   # the CLI is called `endpoint`
endpoint -g boot               # then read the URL off the notebook's own output
```

> ⚠️ **On this path `boot` exits `2` rather than claiming success** — intended, not a
> failure: the engine comes up and streams real tokens, but Kaggle returns the kernel
> log empty while the session runs, so the tunnel URL is read off the notebook by hand
> ([D-073](DECISIONS.md)). `-g` goes *before* `boot`; the `scripts/` patches are
> required before the first boot. [The handbook](docs/HANDBOOK.md#kaggle-hands-on-manual)
> has both.

---

## Evidence and documentation

The raw evidence behind the claims is kept here, and **a number that was not
measured is labelled as not measured**. Every document exists in English and Chinese
— editing one means editing the other.

| Document | Purpose |
| --- | --- |
| [`docs/HANDBOOK.md`](docs/HANDBOOK.md) | Deployment and operational guide — every command, every gate, every gotcha. **The long form of this file** |
| [`DECISIONS.md`](DECISIONS.md) | 77 numbered, dated entries: what was decided, what it overturned, and what would change it |
| [`docs/ENDPOINT.md`](docs/ENDPOINT.md) | The GPU endpoint track |
| [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md) | Known rot, recorded rather than hidden |
| [`docs/evidence/`](docs/evidence/) | Raw verification evidence, dated |

The project distinguishes between five kinds of statement:

* **Measured** — observed on the running system
* **Verified** — confirmed through the application path
* **Projected** — expected, but not yet measured
* **Proposed** — design only
* **Known limitation** — understood constraint or failure

A few of those decisions, as examples:

* **[D-001](DECISIONS.md)** — Codespaces is a measured sandbox, not a long-term host.
* **[D-003](DECISIONS.md)** — Ollama's port is never published.
* **[D-047](DECISIONS.md)** — Performance figures for un-rented infrastructure are
  projections.
* **GPU deployment is kept separate** from the local stack, so endpoint failures do
  not affect the core system.

---

## Known limitations

* **The Kaggle endpoint flow still needs one manual tunnel-URL step**, and `boot`
  exits `2` on that path.
* **GPU Compose has not been verified on real rented GPU hardware.**
* **VPS performance figures remain projections** — no VPS has been rented.
* **Phase 3's agent runtime is measured but not built, and Phase 4 is still a
  proposal** — both per Status above.

---

## License

This project's own code — compose files, scripts, documentation — is
**[MIT licensed](LICENSE)**. That does **not** cover the images it pulls: Ollama and
mcpo are MIT, the Qwen3 models are Apache-2.0, and **Open WebUI is *not* MIT** —
BSD-3-style *plus a branding clause*, which matters past 50 users. The full table is
in [the handbook](docs/HANDBOOK.md#license).

---

> **Build less. Measure more. Keep the evidence.**
