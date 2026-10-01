# self-cloud-agent-lab

**A private AI stack that stays inside your boundary.**

Your own models, your own documents, your own tools — measured from a free sandbox
to rented GPU infrastructure.

[English](README.md) · [繁體中文](README.zh-TW.md)

> **Measured, not assumed.**
> This project records what works, what fails, what it costs, and what is still
> only a projection.

---

## Why this exists

Hand a hosted AI service your company's contracts and you have handed them over.
Run the model yourself instead and you need hardware — a 7–8B model wants **~8 GB
of VRAM** before it will say a word to you.

The gap between those two options is not technical. It is that **nobody tells you
what self-hosting actually costs** — in dollars, in tokens per second, in hours of
setup — before you commit. Product pages say "production-ready"; they do not say
*"5 token/s on a CPU VPS"*, which is slower than most people type.

Most guides show how to make the pieces work. This one measures what happens when
you actually put them together — **in cost, tokens/s, setup time, and failures.**

**Start small. Measure it. Keep the failures. Scale only when the evidence says it
is worth scaling.**

---

## What it is

A complete private stack — inference, chat, RAG, and tool calling — behind a
network design where **nothing is ever exposed** ([D-003](DECISIONS.md)).

| Component | Role |
| --- | --- |
| **Ollama** | Local inference engine. **Never published** — reachable only inside the Docker network. |
| **Open WebUI** | Chat UI, built-in RAG, native MCP support. |
| **mcp-test-server** | Tool calling, over MCP. Belongs to Phase 2. |
| **cloudflared** | *Optional*, off by default. An **outbound** tunnel — no inbound port is opened. |

Only **port 3000** is ever forwarded; every other service stays inside the Docker
network. You keep the data, you keep the model — and you keep the bill, which is
the part the brochures leave out.

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
measurement. A passing item only ever meant that trying the next one was no longer
wasted effort.

Numbers are measured where possible. Unmeasured figures are explicitly labelled as
projections. See [`DECISIONS.md`](DECISIONS.md) for the engineering record.

---

## Architecture

```text
                         Browser
                            │
                         :3000
                            │
                            ▼
┌──────────────────────────────────────────────────┐
│ Codespace / VPS                                  │
│                                                  │
│   Docker network: ai-net                         │
│                                                  │
│   ┌────────────────┐       ┌─────────────────┐   │
│   │     Ollama     │◄──────│   Open WebUI    │   │
│   │     :11434     │       │      :8080      │   │
│   │  NOT EXPOSED   │       └─────────────────┘   │
│   └────────────────┘                             │
│                                                  │
└──────────────────────────────────────────────────┘

             Optional outbound tunnel
                         │
                         ▼
                  GPU Endpoint
                  Larger models
```

### Design principles

* **Private by default** — Ollama is never published directly.
* **Outbound access** — remote access uses a tunnel rather than inbound ports.
* **Evidence over assumptions** — measurements are recorded instead of estimated.
* **Incremental scaling** — validate the architecture before paying for more.

---

## Quick Start

### 1. Codespaces — start here

Create a Codespace from `main` using **2 cores / 8 GB RAM**.
**Do not pick 4-core — it quadruples your quota burn.**

The devcontainer starts the stack automatically, including the model download.
Open forwarded port **3000**, create the first account (**it automatically becomes
the administrator**), then lock registration:

```bash
bash scripts/lock-signup.sh
```

The default model is **Qwen3 4B** (2.5 GB, 256K context, tool calling).

> **Codespaces is a sandbox by measurement, not by preference.** The free tier buys
> about **2 hours a day**; running 24/7 would exhaust the month in **~2.5 days**,
> and storage is billed while the codespace merely *exists*, stopped or not.
> Validate on it, then delete it ([D-001](DECISIONS.md)).

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

Docker and Compose v2 must **already be installed** — `deploy-vps.sh` checks, tells
you which of three causes it found, and stops. It never installs them.

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/deploy-vps.sh --model qwen3:8b --num-ctx 16384
```

**On a GPU machine there is no flag** — the single control is `OLLAMA_GPU` in
`.env` (`auto` / `on` / `off`):

```bash
printf 'OLLAMA_GPU=on\n' >> .env
bash scripts/deploy-vps.sh --model qwen3:14b --num-ctx 16384
```

The script attaches `docker-compose.gpu.yml` and then checks the model is
*actually* running on the GPU — **exit `0` requires it.**

> ⚠️ **That overlay has never run on real rented GPU hardware.** Treat the first
> run as the experiment it is. [Hardware sizing](docs/HANDBOOK.md#hardware-sizing)
> says which machine to rent.

No VPS has been rented yet, so token/s figures for any particular plan are a
projection from measured calibration points, labelled as such
([D-047](DECISIONS.md)). The deploy script itself *is* verified end to end
([D-028](DECISIONS.md)).

### 4. GPU endpoint

A larger-model endpoint can be booted on a GPU runtime such as Kaggle. The flow
reaches a healthy running endpoint and can stream real tokens. The remaining
limitation is **automatic tunnel URL readback** — Kaggle's API returns the kernel
log empty while the session runs, which is a property of Kaggle, not of the GPU.

**For now the URL is read from the notebook output by hand** and passed to the
connection script:

```bash
uv tool install endpoint-vps   # the CLI is called `endpoint`
endpoint init                  # interactive: username, kernel slug, default model
endpoint -g boot               # GPU T4 ×2 — then read the URL from the notebook
```

> **Two things will otherwise cost you an evening.** `-g` goes *before* `boot`.
> And the notebook-generator patches in `scripts/` are **required before the first
> boot, and in order** — the Kaggle account also has to be phone-verified first.
> [The hands-on manual](docs/HANDBOOK.md#kaggle-hands-on-manual) has both.

⚠️ **On this path `boot` now exits `2` rather than claiming success.** That is
intended, not a failure: the engine is up, and the endpoint was never wired up
because the URL could not be read back ([D-073](DECISIONS.md)). See
[`ENDPOINT.md`](docs/ENDPOINT.md) for the procedure, and [D-075](DECISIONS.md) for
why two of the three proposed fixes are struck out.

---

## Evidence

This repository keeps the raw evidence behind the claims.

```text
docs/
├── evidence/                  # Verbatim probe outputs, dated
├── ENDPOINT.md                # The GPU endpoint track
├── HANDBOOK.md                # Deployment and operational guide
├── ENDPOINT-VERIFIER-ROT.md   # Known rot: verifiers that are red, and why
└── PHASE4-STORAGE-PROPOSAL.html
```

Every document exists in English and Chinese. **Editing one means editing the
other.** The project distinguishes between:

* **Measured** — observed on the running system
* **Verified** — confirmed through the application path
* **Projected** — expected, but not yet measured
* **Proposed** — design only
* **Known limitation** — understood constraint or failure

---

## Key engineering decisions

[`DECISIONS.md`](DECISIONS.md) holds **76 numbered, dated entries** recording what
was decided, what it overturned, and what would change it. A few examples:

* **[D-001](DECISIONS.md)** — Codespaces is a measured sandbox, not a long-term host.
* **[D-003](DECISIONS.md)** — Ollama is never published directly.
* **[D-047](DECISIONS.md)** — Performance figures for un-rented infrastructure are
  projections.
* **GPU deployment is kept separate** from the local stack, so endpoint failures do
  not affect the core system.

Two habits run through all of it:

* **A number that was not measured is labelled as not measured.** Several
  long-standing "facts" turned out to be unsourced when someone finally checked;
  those corrections are kept, not quietly fixed.
* **A document that says "there is rot here" is doing its job.** Known and recorded
  limitations — red verifiers included — are collected in
  [`ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md).

---

## Known limitations

* **The Kaggle endpoint flow still needs one manual tunnel-URL step**, and `boot`
  exits `2` on that path.
* **GPU Compose has not been verified on real rented GPU hardware.**
* **VPS performance figures remain projections** — no VPS has been rented.
* **The LangGraph agent runtime has been measured but is not implemented.**
* **Persistent storage is still a proposal.**

---

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/HANDBOOK.md`](docs/HANDBOOK.md) | Deployment and operational guide — every command, every gate, every gotcha |
| [`DECISIONS.md`](DECISIONS.md) | Architectural decisions |
| [`docs/ENDPOINT.md`](docs/ENDPOINT.md) | The GPU endpoint track |
| [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md) | Known rot, recorded rather than hidden |
| [`docs/evidence/`](docs/evidence/) | Raw verification evidence |

**The handbook is the long form of this file** — same material, everything spelled
out, including the parts that are still open.

---

## License

This project's own code — the compose files, scripts, and documentation — is
**[MIT licensed](LICENSE)**.

That does **not** cover the container images it pulls. Ollama and mcpo are MIT; the
Qwen3 models are Apache-2.0; **Open WebUI is *not* MIT** — it is BSD-3-style *plus
a branding clause*, irrelevant for a one-user proof of concept but relevant if you
ever serve more than 50 people with it. The full table and the clause itself are in
[the handbook](docs/HANDBOOK.md#license).

---

> **Build less. Measure more. Keep the evidence.**
