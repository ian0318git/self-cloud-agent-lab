# self-cloud-agent-lab

**English** | [繁體中文](README.zh-TW.md)

A proof of concept validating whether a self-hosted AI platform can run **inside free cloud quotas**:
running your own LLM, reading your own data, using tools over MCP, and letting an agent work autonomously.

> **This project is a validation sandbox, not a persistent service.**
> The reason is below and it is the single most important thing to understand before you start.

---

## Table of contents

- [Why this is not a persistent service](#why-this-is-not-a-persistent-service)
- [Moving to a VPS](#moving-to-a-vps)
- [Hardware sizing](#hardware-sizing)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Kaggle hands-on manual](#kaggle-hands-on-manual)
- [Securing remote access](#securing-remote-access)
- [Verification checklist](#verification-checklist)
- [Quota management](#quota-management)
- [Phase 2](#phase-2)
- [Phase 3 and 4 — planned; all six items are measured](#phase-3-and-4--planned-all-six-items-are-measured)
- [Codespaces gotchas](#codespaces-gotchas)
- [Troubleshooting](#troubleshooting)

---

## Why this is not a persistent service

GitHub Codespaces free tier, converted into real numbers:

| Resource | Free allowance | What it actually buys |
|---|---|---|
| Compute | 120 core-hours/month | 2-core burns 2 core-hours/hour → **only 60 real hours/month** |
| Storage | 15 GB-month | **Billed while the codespace exists — including when stopped** |
| Idle timeout | 30 minutes default | 240 minutes maximum |
| Port visibility | **Private by default** | Must be changed manually to public |

**60 hours ÷ 30 days ≈ 2 hours per day.** Running this 24/7 would exhaust the
entire monthly allowance in **about 2.5 days**, after which the `$0` spending
limit blocks the environment entirely.

Storage is calculated as:

```
GB-month = (GB used × hours existing) ÷ 730
```

So keeping one codespace alive 24/7 for a full month requires total usage ≤ 15 GB.
**The trap:** neither `docker compose down` nor stopping a codespace stops the
storage meter. Only **deleting the entire codespace** does.

**Conclusion:** this project is for answering *"does the architecture work, and is
the model smart enough?"* — then delete it. A genuinely persistent deployment needs
a different host; see [Phase 2](#phase-2).

---

## Moving to a VPS

Codespaces is the *test bench*, not the destination. The goal is a portable private
AI platform: when a VPS with enough CPU/RAM/GPU shows up, move this stack there,
swap in a larger local model, and use it the way you use ChatGPT — with your own
data, RAG, MCP, agents, memory, and workflows under your control.

This section records what that move actually involves, so the POC stays a POC
without quietly becoming a trap. **The architecture was checked against this goal
on 2026-09-19;** the findings are below, marked by whether they carry over cleanly.

### One command

```bash
git clone <this repo> && cd self-cloud-agent-lab
bash scripts/deploy-vps.sh --model qwen3:4b --num-ctx 16384
```

That is the whole deploy. It closes the port, pulls the models, starts the stack,
verifies the stack generates, and confirms the context size actually took effect.
The sections below are what it does and why — read them if you want to do it by
hand, or if something in the script needs to change.

**Two prerequisites, stated because the script does not supply either.** Docker
installed and running (Compose v2 included), and this repo already on the machine
— the `git clone` above is the second one, and it is deliberately not inside the
script. `deploy-vps.sh` checks both and **stops**; it never runs an installer on
your host. When Docker is missing or unreachable it names which of the three
causes it found — not installed, installed but the daemon is not running, or
running but you lack permission — and prints that cause's fix, because those
three have three different fixes (D-063).

**The ordering is the safety argument, not an implementation detail.** `up.sh`
alone cannot be used as the deploy path: its defaults were tuned for Codespaces,
where a published port is private, and on a VPS the same defaults are fail-open.
Everything below exists because of that difference.

`deploy-vps.sh` supports **fresh installs only**. It will refuse to proceed if the
Open WebUI database already has accounts or chats, and it never migrates data
(see "Moving existing data" at the end of this section).

### What carries over unchanged

| Thing | Why it survives |
|---|---|
| `docker-compose.yml` | Three containers and a bridge network — nothing Codespaces-specific. The third, `mcp-test-server`, is a Phase 2 fixture and is deleted once its checklist is verified (`docker-compose.yml:196`). |
| Open WebUI state | Chats, Knowledge, MCP connections, users, settings all live in the `open_webui_storage` volume. Copy the volume, keep the data. |
| Model choice | `OLLAMA_MODEL` in `.env` names the chat model **for the scripts** (`up.sh` pulls it, the verify scripts run it). `docker-compose.yml` reads neither it nor `EMBEDDING_MODEL` — no `OLLAMA_MODEL` line exists in that file at all (`grep -n OLLAMA_ docker-compose.yml`, verified 2026-09-23). The embedding model is not in `.env` in any operative sense either: it lives in Open WebUI's own `config` table, is changed with `scripts/set-embedding.sh`, and needs a restart because the process builds its embedding function once at startup (D-013). The model you actually chat with is picked per conversation in the UI, or pinned by a model preset. Both of those live in the database and move with the volume — so "swapping to a larger model" is one line in `.env`, a pull, and one selection in the UI. |
| MCP / RAG / Memory / Agents | All Open WebUI features configured in its database, not in this repo. They move with the volume. |
| Cloudflare Tunnel | `cloudflared` runs behind a compose profile and dials **out**. No inbound port needed — which is exactly why it is the right answer for a VPS too. |

### Step 1 (mandatory): close the port

**Do this before anything else.** `ports: "3000:8080"` binds to `0.0.0.0` — on
Codespaces that is harmless (ports are private and need GitHub auth), but on a VPS
with a public IP it puts Open WebUI on the open internet. What makes it dangerous
rather than merely untidy is that **it bypasses Cloudflare Access**: Access guards
the tunnel path, not this port. Anyone who scans 3000 never touches Access.

```bash
# in .env
WEBUI_BIND_ADDR=127.0.0.1
```

Then recreate the containers and verify mechanically — not by reading the config:

```bash
bash scripts/down.sh && bash scripts/up.sh
bash scripts/check-exposure.sh
```

`check-exposure.sh` reads the **running containers' actual bindings** via
`docker port`, not what the compose file claims, and it classifies the address each
port is bound to — loopback, wildcard, private, or public — instead of only asking
whether the wildcard address was used. Binding to one of this host's own LAN
addresses therefore reports as **LAN-reachable, not loopback**: it is not an
internet exposure, but it does not pass through the tunnel or Cloudflare Access
either, so anything that can route to that address reaches the login page directly.
That case is **held back by your router, not by this stack** — a bind to a public
address is a real exposure, and is reported as one.

**What this check cannot answer:** it reads **ports**, and a Cloudflare Tunnel
publishes no port — that is what makes it a tunnel. So "no open ports" is not
"unreachable". With the tunnel up, this host is serving your hostname while
`check-exposure.sh` reports nothing exposed on any interface. That is not the check
being wrong; it is the check answering a different question. The tunnel's own state
is reported by `bash scripts/status.sh`, which reads the **container**, not the
config file — see "To turn the tunnel off" below for why that distinction is
load-bearing.

**There is a trap in doing this by hand**, and it is why the script writes `.env`
before it calls `load_env`. `docker compose` resolves the *shell* environment
before it reads `.env`, and `load_env` **exports** everything it sources. So if you
fix `.env` and then run `up.sh`, the corrected value is read from the file but the
already-exported stale value still wins — compose keeps using `0.0.0.0` and nothing
tells you. Correcting `.env` after `load_env` is a no-op. Verify with
`docker port open-webui`, never with the file.

Also keep 11434 unpublished (D-003). Ollama has no authentication at all; the
script checks this too.

### Step 2: enlarge the model and the limits

Five values in `.env` were tuned for 2 cores and 8GB. They are the first things to
raise:

```bash
OLLAMA_MODEL=qwen3:70b          # or whatever the VPS can hold
OLLAMA_CONTEXT_LENGTH=16384     # see below — 4096 truncates the prompt, and 8192 is not enough either
OLLAMA_MAX_LOADED_MODELS=3      # must be >= OLLAMA_NUM_PARALLEL; every extra model holds its own weights
OLLAMA_NUM_PARALLEL=4           # big throughput win; shares one model load — but it multiplies the KV cache (see below)
OLLAMA_KEEP_ALIVE=-1            # keep resident; reloading costs tens of seconds
```

Unlike the others, `OLLAMA_CONTEXT_LENGTH` is **not** free — a larger context
costs KV-cache memory proportional to it (roughly 36 KiB per token for a 3B-class
model; that figure is an **estimate** from Qwen2.5-3B's architecture, not measured
on this stack, and it scales with the model's layer/head count). It costs **time**
too: on this stack, going from 8,192 to 16,384 made the same work **45.5% slower**
(D-035). (*That is a wall-clock **total**, which is measured reliably; how a run
splits between prefill and generation is a separate claim that no longer holds —
it is wrong by **6.8–8.2%**, because `eval_duration` is a **subtraction**
(`total − load − prompt_eval`) and therefore absorbs every prefill after the
first, re-labelling it as generation. D-036 §5, answered in D-041 §5. That
"split into two segments" has since been identified as well: it is **not** a
second request — each `add()` is one `POST /api/chat` from start to finish, and
the cancel and re-send both happen inside it, as ollama's own behaviour, so any
long generation through ollama will meet it (D-041 §10).*) The reason the default is worth raising anyway: **memory extraction
silently truncates.** A `mem0` `add()` call sends an extraction prompt measured at
8,052 and 8,100 tokens, and ollama's 4096 default cuts it to 2,050 — dropping the
instructions at the end of the prompt, with no error — extraction returns zero
facts and says nothing (D-027).

**"Large enough that the prompt fits" is two different thresholds, and the obvious
one is not sufficient.** The first is the pre-generation truncation trigger,
`prompt_tokens > num_ctx − 1`, so ≥ 8,101 gets the prompt *in*. But ollama starts
llama-server with `--context-shift --keep 4`: generation that runs past
`num_ctx − prompt_tokens` makes llama-server **discard a block out of the prompt**
and keep going. At `num_ctx=8192` that leaves **140** tokens of room, and both
`add()` calls hit it (D-035). The rule that actually holds is
`num_ctx > prompt_tokens + num_predict` — about 10,052 here, so **16384** is the
value to use. `deploy-vps.sh` explains this at the point it matters.

**Raising it is necessary but still not sufficient.** Both control groups ran on
2026-09-22. At `num_ctx=8192` the prompt was intact going in but got shifted
mid-generation; at `num_ctx=16384` it stayed intact for the whole generation
(`context shift` count: **0**) and extraction *still* returned zero facts, with
generation *still* stopping at `num_predict=2000`. So neither truncation nor the
mid-generation shift emptied the extraction — both are eliminated.

**The third control group confirmed the remaining candidate.** Same `num_ctx`
(16,384), only `num_predict` changed — mem0's default **2,000 → 8,000**: the
first `add()` extracted **2 memories** with `done_reason=stop` and `eval_count`
5,605 (D-036). The generation budget *was* the cause, and the 2,000-token run
shows why: at that cap the whole budget went to `thinking` before an answer
existed (`thinking_chars=7796` against `eval_count=2000`, D-034).

So the pairing to deploy is **16384** for the context and **a `max_tokens` large
enough that the model stops on its own** — not another magic number. The
criterion is `done_reason == "stop"`; this model's natural length for this prompt
was 5,605 tokens, and any fixed cap below it truncates **silently**, in a way
that reads as "there were no facts to extract".

**The budget half is now written down — as a derivation, not a constant
(D-037).** The probe derives it from the context it is given:

```
max_tokens = num_ctx − EXTRACTION_PROMPT_TOKENS_BOUND   # bound = 8192
```

The invariant is what separates it from 8,000-the-number: for any prompt at or
below the bound, `prompt + budget ≤ num_ctx`, so a mid-generation **context shift
is structurally impossible** — and raising `num_ctx` raises the budget with it.
Two hard criteria guard the derivation's two premises, and they are the reason a
stale derivation cannot pass quietly: **C6** fails if this round's measured
`prompt_eval_count` exceeded the bound (the prescription is to *update the
bound*), **C7** fails if generation did not end in `done_reason=stop` (the
prescription is to *raise `num_ctx`*). Both are skipped when the budget was
overridden on purpose (`--add-max-tokens`, `--quick`) — there a small budget is
the experiment, not a defect.

**This is not deployed, and the docs should not read as if it were.** Nothing in
this repo calls mem0 in production: `grep -rn 'import mem0'` matches only the
probe, `mcp-server/` has no memory tool, and Open WebUI's memory layer is off
entirely. What exists is the derivation plus criteria that will shout — the
fourth-phase agent inherits that config template, and the deploy path
(`deploy-vps.sh`) still writes only the context half.

**Verifying it took effect is not optional**, because the variable's *name* was
verified in the binary long before anything proved it does anything (D-028). Read
it back from the **loaded model**, not from the config: `curl` is unavailable on
the host by design (ollama publishes no ports), so run it inside a container —
`deploy-vps.sh` does this for you and reports the value it read.

### Moving existing data

`deploy-vps.sh` is fresh-install only, and it says so rather than half-doing it.
Migrating a populated install is a separate procedure: stop both stacks, copy the
`open_webui_storage` and `ollama_models` volumes, bring the target up, and confirm
the account count and chat count before changing anything else. The script prints
this reminder instead of guessing.

### What genuinely does not carry over

These are real migration costs. They are listed so they are not discovered
mid-move, which is the same reason D-014 exists.

1. **Changing the embedding model forces a re-embed of every document.** Already
   noted in Phase 2 above and D-013, but it becomes load-bearing at move time:
   pick the embedding model *before* you upload the corpus you care about.

2. **The verification tooling speaks Ollama's native API, not OpenAI's.**
   `verify_api.py` and `ask_probe.py` call `/api/generate` and read fields that
   only Ollama has — `thinking`, `done_reason`, `eval_count`, `load_duration`.
   vLLM serves `/v1/chat/completions` and has no equivalents. **Switching the
   runtime to vLLM means rewriting those two scripts**, not just changing a URL.

3. **Switching runtime is a config change in Open WebUI, not in this repo.**
   Open WebUI talks to Ollama over `OLLAMA_BASE_URL`, but it reaches vLLM through
   an **OpenAI-compatible connection** (Admin Panel → Settings → Connections). The two
   can coexist, so this migration can be incremental — run both, compare, then
   drop the one you don't want. Nothing in the repo needs to change for this.

4. **The 8GB memory budget in D-001 stops being the constraint.** That is the
   point of moving, but it also means the reasoning behind several decisions
   (one model loaded, one parallel request, `thinking` left on because nothing
   else worked) was about *this* machine. Re-check them rather than inheriting
   them — D-011's "keep thinking on" in particular deserves a fresh look when a
   GPU makes 264–293 s per answer irrelevant.

---

## Hardware sizing

What this stack costs in hardware, what each configuration buys, and how close a
rented box gets to a hosted assistant. **Everything measured is marked as such;
everything else is extrapolation and is marked too.** The rule behind the
extrapolation is stated so you can recompute it for hardware not listed here.

### The baseline: what this lab box is and what it does

| | |
|---|---|
| CPU | 4 vCPU slice of an Intel i7-1260P (Alder Lake laptop part) |
| RAM | 15 GiB on the host; the ollama container has **no** memory limit |
| GPU | none |
| Runtime | `OLLAMA_NUM_PARALLEL=1` (one slot), `OLLAMA_CONTEXT_LENGTH=16384`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_KEEP_ALIVE=-1` (the last two since 2026-09-23, D-049) |

Measured 2026-09-22/23, per-run logs kept:

| Quantity | qwen2.5:3b (1.9 GB) | qwen3:4b (2.5 GB) |
|---|---|---|
| generation | 5–6 token/s | 2.7–4.8 token/s |
| prompt evaluation (prefill) | 19–24 token/s | 19–24 token/s |
| cold model load | — | 71.8 s |
| warm reload | — | 3.3 s |

Three consequences that the numbers alone do not make obvious:

1. **Prefill is paid per turn, not per answer.** Evaluating a 1,880-token prompt
   took **93.5 s** on this box. Every tool call is another round trip, and every
   round re-evaluates a *larger* prompt — so a task that calls a tool three times
   pays that tax four times.
2. **A reasoning model turns a four-second answer into a ten-minute one.** One
   three-dice round generated **2,966 tokens over 620 s**; the three tool calls
   themselves were instant. Most of those tokens were `thinking`. The tool-calling
   round-trip is not the slow part — the model thinking about it is.
3. **One slot means everything queues.** With `OLLAMA_NUM_PARALLEL=1` a second
   chat does not run slower; it does not run at all until the first one finishes.
   When an answer takes ten minutes, that is the whole box for ten minutes. This
   is the first number to raise on a bigger machine, and it costs no model quality.

**The default model changed on 2026-09-23** (`.env`: `qwen3:4b` → `qwen3:8b`, 5.2 GB,
plus `OLLAMA_KEEP_ALIVE=-1` and `OLLAMA_MAX_LOADED_MODELS=2` — D-049). That makes the
table above the **4b series**, which matters because *rate is a function of the model*.
The same probe run against `qwen3:8b` on this box, one sample per condition:

| Condition | decode | prefill* |
|---|---|---|
| `num_ctx=8192` | 3.77 t/s | 76.7 t/s |
| `num_ctx=16384` | 3.55 t/s | 76.4 t/s |
| `num_ctx=16384`, `num_predict=128` | 2.89 t/s | 56.4 t/s |
| `num_ctx=16384`, `num_predict=512` | 3.12 t/s | 73.0 t/s |

\* **The prefill column is not usable.** The probe does not control prefix-cache
reuse, and says so itself (the same instrument has printed 306–14,710 t/s); ollama's
log for this run shows the real cold prefill at about **25 t/s**. Decode is
unaffected. The run was `--quick` — one sample per condition — so by the probe's own
design it is **not** a baseline (exit 2, "no baseline attempted"), only a reading with
its conditions attached.

Two costs this measures that the earlier series did not: at 16k context the model is
**7.9 GB resident** (`ollama ps`, 100% CPU) against 5.2 GB on disk — the KV cache is
the difference — and the probe's KV slope is an **upper bound** of 146 KiB per token
(two points only, and ~23 KiB/token of that is not KV). During the run `vmstat` showed
no swapping, `id` sat at 0%, and the run queue was 5–6 on four vCPUs, so these numbers
are mildly conservative.

**The two series are now comparable — the 4b has since been measured the same way.**
The same probe, the same conditions, only the model name changed (`--quick -- --models
qwen3:4b --ctx 8192 16384`, exit 2, 3m46s, and **no swapping at any point**):

| Condition | 4b | 8b | 4b/8b | 4b bandwidth | 8b bandwidth |
|---|---|---|---|---|---|
| `num_ctx=8192` | 5.93 t/s | 3.77 t/s | 1.57× | 13.8 GiB/s | 18.4 GiB/s |
| `num_ctx=16384` | **5.96** | **3.55** | **1.68×** | **13.9 GiB/s** | **17.3 GiB/s** |
| `num_ctx=16384`, `num_predict=128` | 5.72 | 2.89 | 1.98× | 13.3 | 14.1 |
| `num_ctx=16384`, `num_predict=512` | 5.23 | 3.12 | 1.68× | 12.2 | 15.2 |

Three things this settles. **Chat contention costs about 20%**: the same 4b ran at
4.78 t/s during a real chat (D-046's evidence table) against 5.96 t/s clean. **The
remaining gap is not contention**: two *clean* readings still differ by 1.24×, because
the 4b is only 1.68× faster while being 2.09× smaller — the rule in the next section
*systematically over-predicts small models*. And **effective bandwidth is not one
number for this machine**: it runs 13.1–19.7 GB/s depending on model size, so quote it
together with the size. That is why D-047 §1's calibration now reads 12–20 GB/s rather
than 12 (D-049 §4–5).

The raw output of both runs — verbatim apart from stripped ANSI codes — is checked in
under `docs/evidence/` (`2026-09-23-throughput-qwen3-4b.txt` and `…-8b.txt`), and
`docs/evidence/README.md` states the rules those transcripts follow.

### The rule that predicts throughput

```
generation rate ≈ efficient bandwidth ÷ model file size
```

Every token requires reading the model's weights once, so throughput is set by
**memory bandwidth**, not core count. Calibrated on this box: 2.5 GB at **5.96 token/s
clean ⇒ ≈14.9 GB/s effective**; the same model under **chat contention** drops to 4.78
token/s (≈12 GB/s), and the 8B file reaches ≈18.6 GB/s — so this is **not one number
but a range, 13.1–19.7 GB/s across model sizes** (D-047 §1 quotes it as 12–20 GB/s;
both series are in the table above). A dual-channel DDR5 desktop is specified at
~77 GB/s, so this VM slice gets a fraction of its host. For the estimates below
we assume **60% of the spec-sheet bandwidth**; measure your own box before
trusting any of it. (Prefill is compute-bound and behaves differently — that is
why a GPU improves it far more than it improves generation.)

### How much memory a model needs

| Model class | Q4_K_M file | + KV cache at 16k context | Realistic minimum |
|---|---|---|---|
| 3–4B | 1.9–2.5 GB *(measured)* | ~0.6 GB | 4 GB |
| 7–8B | ~5 GB | ~1.2 GB | 8 GB |
| 14B | ~9 GB | ~2 GB | 16 GB |
| 32B | ~20 GB | ~4 GB | 24 GB (tight) |
| 70B | ~43 GB | ~8 GB | 48 GB |
| 120B MoE | ~60–65 GB | ~2 GB (fewer layers active) | 80 GB |

The KV-cache column scales from **D-022's ~36 KiB/token for a 3B-class model** —
an *estimate* derived from Qwen2.5-3B's architecture, not a measurement on this
stack. Two things follow from it and both are easy to get wrong: raising
`OLLAMA_CONTEXT_LENGTH` costs memory proportionally, and the context you need for
mem0 extraction (16384, see Step 2) is not free.

### What each machine buys

| Hardware | Effective BW | 3–4B | 8B | 14B | 32B | 70B |
|---|---|---|---|---|---|---|
| this lab box (4 vCPU VM) | **12–20 GB/s** *(measured; varies with model size)* | **5.2–6.0** *(measured, clean)* | **3.5** *(measured)* | — | — | — |
| desktop, DDR5 dual channel | ~46 GB/s | 20–30 | 8–10 | 5 | 2 | — |
| bare metal, 12-ch DDR5 (EPYC) | ~275 GB/s | 100+ | 40–60 | 25–30 | 12–15 | 5–6 |
| 1× 24 GB GPU (RTX 4090/3090) | ~600 GB/s | 100+ | 60–100 | 40–60 | 20–30 | does not fit |
| 1× 48 GB GPU (L40S/A6000) | ~520 GB/s | 100+ | 60–100 | 40–60 | 25–35 | 10–15 |
| 1× 80 GB GPU (A100/H100) | ~1,200–2,000 GB/s | 100+ | 60–100 | 40–60 | 50–80 | 25–45 |

Numbers other than the measured row are **estimates** from the rule above at 60%
efficiency; treat them as orders of magnitude, not promises. `—` means the model
does not fit in system RAM at a sane size, or would be too slow to converse with.

Two rows are worth reading twice. **A CPU-only cloud VPS is usually worse than a
desktop**, because it is a slice of a shared memory bus — the exact resource this
workload needs. And **a single 24 GB GPU is the first configuration that is
qualitatively different**: it is where 14B stops being a wait and starts being a
conversation.

### VPS configurations

| | A — CPU only | B — one 24 GB GPU **(sweet spot)** | C — 80 GB GPU |
|---|---|---|---|
| Spec | 8 vCPU / 32 GB / 200 GB NVMe | 8 vCPU / 32–64 GB / 1× 24 GB / 200 GB NVMe | 16 vCPU / 128 GB / 1× 80 GB / 500 GB NVMe |
| Serves | 7–8B at 5–10 token/s | 14B at 40–60; 32B at 20–30 token/s | 70B at 25–45 token/s |
| Users | 1, patient | 1–5 comfortable | 5–15 |
| Cost band (2026) | ~$40–120/mo | ~$250–650/mo | ~$1,100–2,200/mo |

Costs are **order-of-magnitude only** — GPU rental prices move, and hourly
billing is the honest way to try one. The arithmetic to sit with: **at these
prices self-hosting is not cheaper than an API.** You buy the data path, not the
price. That is the correct reason and it is worth being explicit about, because
the alternative — renting an 80 GB GPU to save money — does not work.

**The token/s figures above were measured on x86_64.** They are the only numbers
in this table that depend on the CPU architecture, and nothing here has been run
on ARM (Graviton, Ampere, Oracle ARM). The images are multi-arch, so an ARM box
**will** come up and serve — it will just do so at a speed nobody has measured.
`scripts/deploy-vps.sh` says so at deploy time and then keeps going, because
"unmeasured" is not "broken" (D-057).

Things that are easy to miss when moving to a GPU instance:

1. **The instance needs the NVIDIA container runtime.** The device reservation
   itself is no longer something you add by hand: `scripts/deploy-vps.sh` detects
   the GPU and attaches `docker-compose.gpu.yml` (which carries the
   `deploy.resources.reservations.devices` block) on its own. Ollama's image
   detects the GPU once the runtime is there. Control it with `OLLAMA_GPU` in
   `.env` — `auto` (default), `on`, or `off`. With hardware present but no
   runtime, the script **stops** rather than silently falling back to CPU. And
   the deploy finishes only after a real inference plus `/api/ps` confirms
   `size_vram == size` — device visibility and a registered runtime are still
   just declarations.
2. **VRAM is a hard wall.** If weights plus KV cache exceed it, ollama offloads
   layers to system RAM and throughput collapses — often 10–50× worse, not 20%
   worse. Size for the model **and the context**, not just the model. The deploy
   script warns (does not block) when the estimate exceeds the detected VRAM —
   and also warns when it **cannot estimate at all** (a model off its table, or
   no `--model-gb`), rather than printing a green OK beside a blank number.
3. **Disk speed shows up in the first answer.** Cold load was 71.8 s here for a
   2.5 GB model; the 70B tier is a 43 GB read.
4. **Never publish 11434.** Ollama has no authentication at all — the "Securing
   remote access" section below is not optional on a public IP.

### How close to ChatGPT can a rented VPS get?

Two questions are hiding in that one, and they have different answers.

**The features** — tools, RAG over your own documents, MCP, multi-user, an
OpenAI-compatible endpoint — are already here, and they move to a VPS unchanged.
No hardware decision changes this layer.

**The model** is the part you cannot rent your way out of. Open-weight models
top out below the frontier, and the gap is most visible exactly where a studio
notices it: long multi-step reasoning, following intricate instructions, and
knowing when it does not know. Rough honesty by tier:

- **7–8B**: fine for summarising, extraction, classification, translation. It will
  also invent a tool result when it has no tool — observed on this stack
  2026-09-23: three fabricated `<tool_response>` blocks in one chat, while the MCP
  server logged **zero** sessions. It needs guardrails wherever being wrong is
  expensive. (The write-up is pending; the runs are in the logs.)
- **14B–32B**: a capable junior assistant that remembers everything you gave it.
  This is the tier where "接近 ChatGPT 的功能" becomes a fair description of the
  *experience*, for a single user or a small studio.
- **70B and MoE 100B+**: noticeably better, still not ChatGPT — and at tier C's
  cost, worth measuring against an API before committing to monthly billing.

**The practical answer: configuration B.** 14B–32B at 20–60 token/s is the point
where the *medium* stops being the problem. Rent it hourly first, run your own
hardest real task through it, and only then decide whether the next tier up is
worth 3–4× the monthly cost. And keep the OpenAI-compatible connection (see the
compose comments) as an escape hatch: sensitive work local, everything else to an
API — that hybrid is cheaper and better than either extreme.

### What "sensitive data" actually requires

Sensitivity is decided by what **leaves**, not by where the model runs.

- `ENABLE_OPENAI_API=false` is the default here **because the upstream default
  sends data to OpenAI** — see the comment in `docker-compose.yml`. Keep it off.
- Web search and external tools are the features that send content out. On a
  sensitive corpus they must stay off; local MCP tools on the internal network
  (like `mcp-test-server`) are a different thing entirely.
- Local by construction: embeddings (`bge-m3`, `qwen3-embedding:0.6b`), chat
  storage in the `open_webui_storage` volume (`webui.db` is the whole install).
- **Still egress, and easy to forget:** the first boot downloads an embedding
  model from Hugging Face, and `ollama pull` fetches from `registry.ollama.ai`.
  Pull every model you need **before** the data arrives.
- Back up both volumes. Losing `webui.db` loses every chat, document and setting.

---

## Architecture

Phase 1 (implemented in this repo):

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
| **Ollama** | Local LLM inference engine |
| **Qwen3 4B** | Default model (2.5 GB, 256K context, tool calling) |
| **Open WebUI** | Chat UI + built-in RAG + native MCP support |
| **cloudflared** | *Optional*, off by default. Outbound tunnel for remote access — see [Securing remote access](#securing-remote-access) |

> `docker compose up -d` starts a **third** container as well: `mcp-test-server`.
> It declares no profile, so unlike `cloudflared` it is not optional. It belongs
> to Phase 2's picture — see [Where the pieces run](#where-the-pieces-run-phase-2)
> below.

---

## Quick start

### On Codespaces

1. Open this repo on GitHub → **Code** → **Codespaces** → **Create codespace on main**
2. Select the **2-core / 8GB** machine (**do not pick 4-core — it quadruples your quota burn**)
3. Wait for creation — `postCreateCommand` runs `scripts/up.sh` automatically,
   including the model download (a few minutes on first run)
4. Open the **PORTS** panel → click the URL for port **3000**
5. Register the first account (**it automatically becomes the administrator**)
6. Once logged in, **lock registration**:
   ```bash
   bash scripts/lock-signup.sh
   ```

### Locally

Requires Docker and Compose v2. **At least 8GB RAM**
(on a 4GB machine, switch to `qwen3:1.7b`).

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

Open <http://localhost:3000>.

### Commands

| Command | Purpose |
|---|---|
| `bash scripts/deploy-vps.sh --model M --num-ctx N [--model-gb G]` | **One-click deploy onto a fresh VPS.** **Requires Docker and Compose v2 already installed and running** — it checks, names which of the three causes it found, and stops; it never installs (D-063). Writes `.env` safely *before* loading it, starts the stack, gates on real port bindings, pulls both models, then proves the stack generates and that `num_ctx` took effect. Fresh installs only — refuses if the database already has accounts or chats. **`--model-gb G`** supplies the on-disk size for a model its built-in table does not know; without it the disk gate says "unknown" rather than guessing, and it never guesses from the tag (D-058) |
| `bash scripts/deploy-vps.sh --dry-run` | Same preconditions and the `.env` diff, writing nothing. Its disk line covers the **unconditional** need only — the compose images this run would pull, plus a system margin. The model is deliberately *not* in that number: whether the model is already present cannot be answered until the containers are up, so that half is re-checked immediately before any download (D-058) |
| `bash scripts/deploy-vps.sh --expose` | Deliberately publish Open WebUI on `0.0.0.0` and downgrade the gate to a warning. **This bypasses Cloudflare Access** — it exists for hosts with real edge filtering, not for convenience |
| `bash scripts/up.sh` | Start the stack + ensure the model exists (idempotent) |
| `bash scripts/down.sh` | Stop containers, **keep** models and chat history |
| `bash scripts/down.sh --purge` | Stop containers and **delete** all volumes |
| `bash scripts/pull-model.sh` | Retry the model download on its own |
| `bash scripts/status.sh` | Container status, model list, memory usage |
| `bash scripts/check-exposure.sh` | Which ports are actually reachable from outside, from the running containers' real bindings |
| `bash scripts/verify.sh` | Phase-2 prerequisite check: speed, tool calling, thinking, factual correctness (with a control question), Chinese output (~15 min). Exit codes: `0` all critical items passed, `1` a critical item failed, `2` **could not be determined** — that is not a failure (D-016) |
| `bash scripts/verify.sh 2` | Same, but only test 2 (disable thinking, ~1 min) |
| `bash scripts/verify.sh 3,4 qwen3:1.7b` | Run selected tests with a specific model (model comparison; the model must be an argument — an env var gets overwritten by `.env`) |
| `bash scripts/ask_probe.sh qwen3:4b qwen2.5:3b` | Whether the model's answers are **factually correct**, plus speed. Every factual question ships with a control question (~1–2 min per question) |
| `bash scripts/rag-verify.sh` | **Phase-2 RAG, mechanically**: does retrieval pick the right chunk, does the model actually *use* it, and does it refuse to invent what the document doesn't say. Uses the app's own retrieval functions. The model comes from `OLLAMA_MODEL` in `.env`, and **the example default is `qwen3:4b` (a thinking model) — run this line as-is and all three answers hit the 300 s ceiling, taking at least 15 min to report "inconclusive"** (measured 2026-09-19). For a run that can actually go green, see the next line. Exit codes `0`/`1`/`2` as above, plus `3` = the probe itself broke (D-018) |
| `bash scripts/rag-verify.sh --model qwen2.5:3b` | Same, against a specific model. **The recommended invocation**: a non-thinking model takes 41–94 s per answer, ~2–5 min for the whole run (four runs, 12 samples — D-018). The model must be an argument — `.env` overwrites env vars |
| `bash scripts/rag-verify.sh --timeout 900` | Raise the per-answer limit, for thinking models. "Too slow" and "unreachable" are reported separately — they send you to different fixes |
| `bash scripts/rag-http-verify.sh` | **The same four RAG items over the real HTTP path** — create a knowledge base, upload a document, ask about it, ask about what it doesn't say — through `/api/v1/files` and `/api/chat/completions` rather than the app's functions. Needs an API key in `.env` as `WEBUI_API_KEY` (which needs **Admin Panel → Settings → Authentication → API Keys** switched on first). Argues with `rag-verify.sh`'s blind spot, and has its own: it cannot see *which* chunks were retrieved. Exit codes `0`/`1`/`2`/`3` (D-019) |
| `bash scripts/verify-throughput.sh` | **How fast this machine actually decodes** — a seven-condition matrix, measured **seven times each** (~35 min), plus the prompt ceiling at two `num_ctx` values. Prints a baseline only if the repeats agree; **exits `1` and names the noisy conditions if they don't**. Scatter is judged by the coefficient of variation, not by the range — the range grows with sample count, so at seven samples the old 15%-range rule would have meant 5.55% (D-032). This is the re-baseline that is still open (D-031, D-032) |
| `bash scripts/check-egress.sh` | **Where data can flow out** — every enabled external endpoint, read from the database's actual values |
| `bash scripts/check-egress.sh --fix` | Turn off `openai.enable`, restart, and read back to confirm (D-017) |
| `bash scripts/probe-openai.sh [URL]` | Conformance probe for any OpenAI-compatible runtime. Defaults to Ollama's `/v1` |
| `bash scripts/connect-endpoint.sh --url URL --check` | Verify a GPU runtime **before** wiring it in. Changes nothing |
| `bash scripts/connect-endpoint.sh --url URL` | Attach an OpenAI-compatible runtime (verify → wire → read back) |
| `bash scripts/connect-endpoint.sh --status` | Which runtime is wired right now |
| `bash scripts/connect-endpoint.sh --disconnect` | Back to Ollama only |
| `bash scripts/rotate-endpoint-key.sh --check` | Compare the **three** places the persistent endpoint key lives — the config yaml (the original, baked into the notebook at boot), `.env`, and Open WebUI's database — and print **fingerprints only, never the key**. Changes nothing; exits `2` when they disagree (so it works as a gate) |
| `bash scripts/rotate-endpoint-key.sh` | Rotate that key across all three **at once**, after reading all three and generating one new value. Asks for `yes`; `--yes` skips. Rotating only two would leave a holder with a dead key and **no symptom**, so a holder it cannot reach stops the whole thing (exit `2`) |
| `bash scripts/apply-endpoint-ntfy-fixes.sh --dry-run` | Check whether the two ntfy fixes in endpoint's notebook generator are needed, and whether they still apply. Changes nothing |
| `bash scripts/apply-endpoint-ntfy-fixes.sh` | Patch that generator (backup → hash gate → apply → re-verify). `--revert` undoes it. **A package-manager file: an `endpoint-vps` upgrade erases this** |
| `python3 scripts/test_endpoint_ntfy_fixes.py FILE` | The verifier behind the above — drives the code the generator *emits* against a simulated ntfy token bucket. **Fails against the pristine file on purpose**; that failure is the demonstration (D-050) |
| `bash scripts/apply-endpoint-stop-fixes.sh --dry-run` | Check whether `endpoint stop` still has its silent no-op, and whether the patch still applies. Changes nothing |
| `bash scripts/apply-endpoint-stop-fixes.sh` | Patch the CLI itself — **two files** (`core.py` + `commands.py`), so four pinned hashes and one shared backup stamp. Refuses when the two files disagree. `--revert` undoes both together. **A package-manager file: an `endpoint-vps` upgrade erases this** |
| `python3 scripts/test_endpoint_stop_fixes.py ROOT` | The verifier behind the above — slices `get_kernel_status`/`run_stop`/`run_boot`'s clear loop out by AST and drives them against fakes (no network, no cache writes). **Fails against the pristine tree on purpose** (D-060) |
| `bash scripts/apply-endpoint-apikey-broadcast-fixes.sh --dry-run` | Check whether the engine still broadcasts the API key to the public ntfy topic, and whether the patch still applies. Changes nothing |
| `bash scripts/apply-endpoint-apikey-broadcast-fixes.sh` | Stop that broadcast — one file, `engine/engine.py`, which is the **producer** the notebook generator base64-embeds (`master_build_notebook.py:331`). `--revert` undoes it. **A package-manager file: an `endpoint-vps` upgrade erases this** |
| `python3 scripts/test_endpoint_apikey_broadcast_fixes.py ROOT` | The verifier behind the above — execs `_startup()` for real against a captured transport and asserts the POST body carries no key, plus the AST guard that deleting the broadcast cannot hang boot. **Fails against the pristine file on purpose** (D-067). The fourth cut legitimately edited this file's own anchor, so D-076 re-pinned its hash — and that re-pin is backed by measurement, not by the value change |
| `bash scripts/test_endpoint_apikey_broadcast_fixes_mutants.sh TREE [--pristine TREE2]` | That the verifier above is actually exercised — **5 mutations, each naming the check that must catch it**, plus 1 guard that must stay silent, plus (when `--pristine` is given) 1 mutation only a pre-cut-A tree can reach: check B's second path. Without `--pristine` it **says so out loud** that the path went untested rather than skipping quietly. Exit `0` all caught, `1` a miss, `2` usage, `3` a malformed table. **It names one assertion it does not cover** (`TUNNEL ACQUIRED:` must `break` — that `break` is not unique in `run_boot`, so a single-line anchor cannot express it), in its own header (D-076) |
| `bash scripts/apply-endpoint-tunnel-url-privacy.sh --dry-run` | Check whether the tunnel URL still goes out on the public ntfy topic, and whether the patch still applies. Changes nothing |
| `bash scripts/apply-endpoint-tunnel-url-privacy.sh` | Take that URL off the topic — **three files** this time (`scripts/master_build_notebook.py`, `endpoint/core.py`, `endpoint/commands.py`), so six pinned hashes and one shared backup stamp. The URL stops being published and is read back from the **Kaggle kernel log** instead, which needs Kaggle credentials to read. ⚠ **Order matters: ntfy fixes → stop fixes → this one**, and `--revert` on either of those two silently undoes this one. `--revert` undoes it. **Package-manager files: an `endpoint-vps` upgrade erases this** |
| `python3 scripts/test_endpoint_tunnel_url_privacy.py ROOT` | The verifier behind the above — drives the new kernel-log reader against fakes to pin `found` / `absent` / `unknown` apart (collapsing the last two is the D-060 defect), asserts `get_tunnel_url` has no caller left, and reads the generator's emitted strings. It also **re-hashes cut A's verifier**: if this cut had touched it, that check goes red. **Fails against the pristine tree on purpose** (D-068) |
| `python3 scripts/probe_kernel_log_url.py` | Ask the real Kaggle API whether the kernel log holds a readable tunnel URL — the premise cut B rests on. **Never prints the log body, the URL, the token or the topic**: status codes, booleans, counts, and a one-way fingerprint of the URL so two runs can be compared without either revealing it. Exit `0` found, `1` read but no URL yet, `2` could not read at all, `3` usage |
| `bash scripts/set-endpoint-topic-secret.sh` | Generate (or rotate) the local secret the ntfy topic name is derived from. **Never prints the value** — only a `sha256:<12>` fingerprint — and **never takes it on the command line** (argv is visible to `ps`); it generates one itself or reads a line from stdin (`--stdin`). **Keeps no backup**, so the replaced secret is unrecoverable on this machine; recovery is `endpoint kill-all --yes` or the Kaggle UI. **Do not run this with a kernel still running** — see the ordering note below. `--check` reports without changing anything (exit `2` when there is no usable secret, so it works as a gate); `--dry-run` prints a fingerprint and writes nothing |
| `bash scripts/apply-endpoint-topic-secret.sh --dry-run` | Check whether the topic is still derived from the public Kaggle username, whether the patch still applies, and **whether a usable `signal.topic_secret` exists on this machine**. Changes nothing. Against the real install today it exits `1` on purpose — cut C stacks **on top of cut B**, which is not applied yet |
| `bash scripts/apply-endpoint-topic-secret.sh` | Stop deriving the topic from the public username — **five files** (`endpoint/core.py`, `endpoint/commands.py`, `scripts/master_build_notebook.py`, and the two files that ship with the package: `endpoint/data/endpoint-config.example.yaml` and `endpoint/data/endpoint.1`), so ten pinned hashes and one shared backup stamp. The topic becomes a function of a local secret, so it stops being printable anywhere. ⚠ **Its secret gate is what makes the "patched but no secret" state unreachable — it refuses to run without a well-formed secret, so there is no `--force`.** ⚠ **Order matters: B must be applied first** — cut C's pristine hashes are exactly cut B's patched hashes. `--revert` undoes it. **Package-manager files: an `endpoint-vps` upgrade erases this** |
| `python3 scripts/test_endpoint_topic_secret.py ROOT` | The verifier behind the above — thirteen checks (D1–D13) that slice the derivations out with AST and **exec them against stubs rather than importing the package**, so the tree under test is what runs. Pins the golden topic, the two shipped data files, cut B's verifier (`e8e2259e…`) and `engine/engine.py` (`68dfa5a2…`), and drives the secret gate through a reverse-applied pristine copy. **Fails against the pristine tree on purpose: 23 items** (D-069) |
| `bash scripts/test_endpoint_topic_secret_mutants.sh TREE` | That the verifier above is actually exercised — **13 mutations, each naming the check that must catch it** (D11 fires on every one of them, so "went red" on its own carries no information), plus 3 guards that must stay silent. Two of the mutations are the real bugs the battery found, not invented ones. Exit `0` all caught, `1` a miss, `2` usage, `3` a mutation that could not even be injected (a stale list is **not** a hole in the verifier, and is reported as such) (D-069) |
| `bash scripts/apply-endpoint-boot-honest-outcome.sh --dry-run` | Check whether boot's two measurement-refuted prescriptions and its "unwired, yet exit 0" are still there, and whether the patch still applies. Changes nothing |
| `bash scripts/apply-endpoint-boot-honest-outcome.sh` | Stop boot claiming something it did not do — one file, `endpoint/commands.py`, with **every change inside `run_boot`** (proved by dumping the AST of each function before and after, not by reading the diff). Before: when the tunnel was up but its URL unreadable, it printed "it will be there in a moment" and "read it with `endpoint base-url`" (which goes down that very unreadable path) while `_register_with_proxy` never ran and the proxy was never told — yet boot still printed `Endpoint IS ONLINE` and exited 0. Now it says the endpoint was **not wired up** and exits **2** (`1` still means "the kernel did not come up"). ⚠ **This fixes the reporting, not the defect** — the URL is still unobtainable (cut B unresolved), so **every boot from here on exits 2**; that is the intended effect. ⚠ **Order matters: its pristine hash is the state with A/B/C and the fourth cut all applied** — after a `--revert` of any of those, this one **refuses to run** (loudly, not silently). `--revert` undoes it. **A package-manager file: an `endpoint-vps` upgrade erases this** |
| `python3 scripts/test_endpoint_boot_honest_outcome.py ROOT` | The verifier behind the above — **five checks (A–E)**, parsed with AST rather than imported (importing would read the user's real config, and the apply script runs this verifier twice per cycle). A pins that both false prescriptions are gone; B is a **positive control** (the branch must still warn, closing off "delete the branch to make A green"); C is the **D-033 guard** (`success = True` must stay unconditional and literally `True` — flipping it to `False` would turn a true fact into a false failure); D pins that the closing report is conditional and on the success path; E pins that the two exit codes keep their own meanings. **Fails against the pristine file on purpose: A, D and E** (D-073) |
| `bash scripts/lock-signup.sh` | Verify signup is really off, and close it via the config API if it is open |
| `bash scripts/lock-signup.sh --check` | Verify only — no changes. Exits non-zero if signup is open |

Attaching a GPU runtime (Kaggle + Endpoint, or a VPS + vLLM) has its own guide:
[`docs/ENDPOINT.md`](docs/ENDPOINT.md) · [`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md).
The hands-on Kaggle walkthrough is [below](#kaggle-hands-on-manual).

### Evidence scripts

These exist because this project has twice shipped a "fix" that reported success
without working. They run real containers and observe real behaviour; run them when you
change anything they cover.

| Command | What it proves |
|---|---|
| `bash scripts/verify-lock-signup.sh` | That `ENABLE_SIGNUP` in `.env` is inert after first boot — three boots, one volume (~25 min) |
| `python3 scripts/signup_control_probe.py URL` | Which mechanism *does* control signup, confirmed by hitting the real endpoint |
| `python3 scripts/verify_lock_signup_script.py URL .` | That `lock-signup.sh` locks, is idempotent, and fails loudly |
| `bash scripts/verify-first-admin.sh` | That `ENABLE_SIGNUP=false` does not lock you out of creating your first admin |
| `bash scripts/verify-langgraph-tools.sh` | That tool calling survives the code-path change to LangGraph, graded on the `call_id` chain rather than on the answer reading correctly |
| `bash scripts/test_langgraph_tools_probe_mutants.sh` | That the grader above is actually exercised — 11 mutations of its own criteria, every one must be caught |
| `bash scripts/verify-chroma-dims.sh` | That mem0 reuses a pre-created ChromaDB collection instead of fighting it, and which metadata key is authoritative for embedding dimensions (D-026) |
| `bash scripts/test_chroma_dims_probe_mutants.sh` | That the grader above is actually exercised — 24 mutations of its own criteria, every one must be caught |
| `bash scripts/verify-mem0-add-cost.sh` | That mem0's `add()` costs exactly one extra LLM call, and that at the default `num_ctx` that call never sees its own instructions (D-027). **Three control groups have since closed the question**: pre-generation truncation (D-034) and the mid-generation context shift (D-035) were *eliminated*, and the **generation budget** was **confirmed as the cause** (D-036) — at `num_ctx` held at 16,384, `num_predict` 2,000 → 8,000 extracts 2 memories where 0 were extracted, with `done_reason=stop`. The script now reads the container's `OLLAMA_CONTEXT_LENGTH` and hands it to the probe as `--ctx`, so the budget is **derived** (`num_ctx − 8192`) rather than fixed, with C6/C7 as the sentinels on the derivation's two premises (D-037). Those two are graded **even in a segmented `--sections add` run** — the early return for a missing section used to swallow them, so the sentinels were silent in the one configuration they are prescribed for (D-040). **A real run followed on 2026-09-23** (Stage B): under the derived budget of 8,192 the first `add()` extracted **2 memories** with `done_reason=stop` and `prompt token=8052`, and the criteria section carried no C6/C7 problem line — that is the only moment the fix is visible in the output (D-040 §7, results). **The full five-section run followed the same night** (Stage C, `num_ctx=4096` — the guard's own prescription): `rc=0`, **C1~C7 all pass**, 54 minutes, and every measurable value reproduces D-027 bit-for-bit (`2050`/`8047`, the `687`/`346`/`687` bracket, a head-blind canary, thinking ≥185 tokens). It added two things: the ollama log's `prompt=` lines gave the **real** `add()` prompts (8,052 and 8,100) in the same round as the probe's *reconstructed* 8,047 — measuring, for the first time, the 5-token gap D-042 §4 had wrongly assumed away — and the `+48` between the two adds reproduced exactly, so that unexplained difference is stable rather than noise (D-044). ⚠ **Its five-section run cannot execute at the shipped config**: C2/C3 presuppose a server default small enough to truncate, and 16,384 removes that premise, so a full run without `--sections` **returns 2 every time** (by design, not broken); `num_ctx` below **8,048** is the configuration where those two criteria are defined — 8,048 is C2's own `min_ctx_for_extraction`, the probe's *reconstructed* prompt (8,047) plus one, which is the object C2 actually sends — and the guard's threshold is 8,101, so **8,048–8,100** slips through and returns a **false failure 1** (two halves, both false: at 8,048–8,052 the real `add()` prompt is *still* truncated and C2's instrument, five tokens shorter, cannot see it; from 8,053 the premise is simply void). That same constant errs in the other direction too: a round whose first extraction prompt reaches 8,101 tokens still has a valid premise, yet the guard answers 2 — a bound rather than a present defect, since that prompt's observed maximum is 8,100 (D-043) |
| `python3 scripts/test_mem0_add_cost_probe.py` | That the verdicts above have discriminating power offline — no Docker, no ollama — including the four-state truncation claim that `#58` replaced (D-036 §11.1) |
| `bash scripts/test_mem0_add_cost_probe_mutants.sh` | That the grader above is actually exercised — 109 mutations of its own criteria, every one must be caught |
| `bash scripts/test_probe_traceability.sh` | That a probe **bind-mounted live** into its container can be proved to be the same file before and after the run — the probe is not baked into the image (four scripts mount it, e.g. `verify-mem0-add-cost.sh` and its `-v "$PROBE:/probe/mem0_add_cost_probe.py:ro"`), so the image label does not cover it and a pass it produced mid-change traces to no revision — recovering that once took a leftover `.pyc` (D-027 §10, D-036 §11.2) |
| `bash scripts/test_lib_running.sh` | That the `lib.sh` helpers that replaced a pipeline — `line_in_list` and the three predicates over it (`container_running` / `service_running` / `model_in_ollama`), plus `first_line` — mean what the forms they replaced meant (whole-line equality, empty name → not found, glob characters literal, a header row that is not a model), and that they are right **on exactly the inputs those forms get wrong**: under `set -o pipefail` a `docker ps … \| grep -qx` check dies of SIGPIPE and reports "not running" for a container that is running. The file's own control cases run the old shape against the same fake docker and watch it misreport (D-038) |
| `bash scripts/test_verify_mem0_add_cost_guard.sh` | That the guard above is actually **wired in**, in **all four** scripts that mount a probe live (`verify-mem0-add-cost` / `-chroma-dims` / `-langgraph-tools` / `-phase3-runtime`): that `changed` overrides a `--sections` exit 2 while a genuine 2 is not misreported, that an unreadable hash warns instead of failing (D-016), that 「結束碼 2 是預期的」 can no longer print at rc 3, and that a readable `OLLAMA_CONTEXT_LENGTH` reaches the probe as `--ctx` while an unreadable one reaches it as **no flag at all** plus a warning (D-037, D-038 §6) |
| `bash scripts/test_usage_text.sh` | That every script's `--help` prints **its own header and nothing else**, and that the criterion for where to stop is **content** (print until the first non-comment, non-blank line) rather than a position. Of the nine scripts that decided that by position, **four were printing code** as documentation (`source …`, `require_docker`), **one was truncating** (`deploy-vps.sh` showed 18 of its 110 header lines) and **four were right only by coincidence** — and the coincidences are the dangerous half: nothing fails, so nothing says anything. Section C runs all eleven `--help`s end-to-end in a temp project root with a fake docker and compares them byte-for-byte against a header **derived independently** (a different algorithm); section D scans for a hardcoded range coming back, and for a `usage_text` call that cannot reach its definition — the first version of this change called it before `lib.sh` was loaded, which prints **nothing** and still exits 0 (D-039) |
| `bash scripts/test_ollama_log_corroboration.sh` | That the server-side log corroboration reports "the instrument is broken" and "no truncation this run" as two different sentences. Its coverage check used to be the worst case of the defect below — under `set -o pipefail` the `printf \| grep -q` form died of SIGPIPE and reported "coverage not established" *precisely when the window covered the most*; it is now a shell pattern match, and the criterion is stated as what it is: **does the producer write again after the reader leaves** (D-034, corrected in D-038 §7) |
| `bash scripts/deploy-vps.sh --dry-run` | What a deploy would write to `.env` and whether the machine has the disk/RAM — changes nothing. Since D-058 its disk line is the **unconditional** need only (images + margin), so the model's share is reported separately and an unknown model is named as unknown instead of being folded into the number |
| `bash scripts/test_deploy_vps_decisions.sh` | That the bind-address policy, the exposure gate's four states, the resource thresholds, the `.env` writer, the three arch layers and the **two-phase disk gate** each have a "should pass" and a "should block" case — 245 cases. The arch ones read **real captured `docker manifest inspect` JSON**, including the trap that a single-platform manifest carries **no** `architecture` field at all (reading that as "no" would block a machine whose container is already up) and that `arm` must not match `arm64` (D-057). The disk ones pin the two ways it could have gone wrong in the other direction: a model outside the table must stay `unknown` — never a number (D-055 §3(2)'s red line) — and an explicit empty `will_download` must count as *will download*, not as a re-run (D-058) |
| `bash scripts/test_deploy_vps_decisions_mutants.sh` | That the grader above is actually exercised — 121 mutations across the three bash modules and the smoke probe, every one must be caught. The arch group's two most expensive: `arm64 → verified` (claiming this lab was validated on ARM) and `mismatch → native` (letting qemu emulation go **entirely silent**). The disk group's three backbones: an off-table model silently returning a number, an unreadable image list read as "nothing to pull", and the second-phase block degraded from `exit 1` to a `return` that the caller turns into "could not be determined" (D-058) |
| `bash scripts/verify-phase3-runtime.sh` | That `recursion_limit` counts super-steps (+1), that a tight limit aborts only after all the work is done, and that a persistent job store still drops a due job silently under the 1-second default grace (D-030) |
| `bash scripts/test_phase3_runtime_probe.py` | That the verdicts above have discriminating power offline — no Docker, no langgraph — with every boundary as its own case |
| `bash scripts/test_phase3_storage_decisions.sh` | That the storage verdicts separate a named volume, a bind mount and a container's temp directory, and that `unknown` is never read as durable |
| `bash scripts/test_phase3_mutants.sh` | That the three graders above are actually exercised — 23 mutations of their own criteria, every one must be caught |
| `python3 scripts/test_deploy_smoke_probe.py` | That the post-deploy smoke test's four assertions each bite, including the trap it exists to avoid: a generation cut off by the token cap reading as success |
| `bash scripts/test_profile_lifecycle.sh` | That the stale-container **set difference** is wrong in neither direction — it must report a profile-disabled leftover *and* must not delete a container that is still in service — plus the empty-list guard and the tunnel's four states (D-029) |
| `bash scripts/verify-throughput.sh` | Decode rates across a seven-condition matrix, the truncation ceiling measured at **two** `num_ctx` values, and the KV-cache slope by model — and it **refuses to call any of it a baseline** when the seven repeats disagree with each other (D-031, D-032, D-033). Its prefill column is marked unquotable on purpose: the probe cannot control for prefix-cache reuse, so it reports 306–14,710 t/s where the real cold prefill is ~25 t/s. **D-033 found the baseline was not merely unmeasured but *unreachable*:** the long-prompt arm was hardcoded to 2 samples while the stability rule needs ≥ 5, so the verdict was `unstable` for *every* possible run — D-031 and D-033 were both doomed before they were measured, and "the machine is noisy" was only half the story. **That is now fixed** (the arm takes the same sample count as every other; D-033 §5), which makes the criterion **satisfiable, not satisfied**: the five conditions that were genuinely noisy are untouched, so the next run can still legitimately fail |
| `bash scripts/test_throughput_probe.py` | That the ceiling formula, the truncation verdict, the "does the ceiling move with `num_predict`" verdict and the KV grouping each bite — 259 assertions, no Docker. **Its last section is different in kind from every other assertion here:** it feeds a stub client into the real `measure()` and asserts on the matrix the probe assembles *itself*. Everything above it tests a grader; that section tests **the case the grader is actually handed** (D-033). It includes a `num_keep` the project has never observed, because a simplification that is equivalent on every observed input cannot be killed by observation |
| `bash scripts/test_throughput_probe_mutants.sh` | That the four graders above are actually exercised — 73 mutations of their own criteria, every one must be caught. Two of them guard the **wiring** rather than a grader: the long-prompt arm's sample count reverted to the hardcoded `2`, and its general form (the sampler silently capping itself). A mutation harness's own output is a claim, so it is checked too: the criteria must not merely be *broken* by the substitution, they must be broken **in a way an assertion notices** — a no-op substitution, a mangled field or a syntax error all "fail the tests" without guarding anything (D-032) |

---

## Kaggle hands-on manual

This is the **walkthrough**: an empty browser to a working GPU runtime, in order.
The *reasoning*, the *security trade-offs* and the *troubleshooting table* live in
[`docs/ENDPOINT.md`](docs/ENDPOINT.md) — this section does not repeat them.

**What you get:** a second LLM runtime on Kaggle's free GPU (T4 ×2, up to ~70B
parameters), exposed as an OpenAI-compatible endpoint. Everything above it sees only a
`--base-url` change.
**What it costs:** no money. It spends Kaggle's GPU quota (30 h/week), and the runtime
disappears on its own.

> **Already partly set up?** Two commands tell you where you stand:
> `endpoint doctor` (is the config readable?) and
> `bash scripts/apply-endpoint-ntfy-fixes.sh --verify` (is the notebook generator
> patched?). Steps 1–4 below are one-time.

### Step 1 — Create the Kaggle account (once)

1. Sign up at <https://www.kaggle.com> (Google or email).
2. **Complete phone verification** — <https://www.kaggle.com/settings> →
   *Phone Verification*.

   **This is the most common first-time blocker.** Without a verified phone, Kaggle
   gives you no API access, no GPU accelerator and no Internet toggle — and it does
   not fail loudly. It fails later, as a boot that never produces a URL.
3. TPU access additionally needs *persona/identity* verification. **T4 ×2 does not.**

### Step 2 — Get the API token (once)

<https://www.kaggle.com/settings> → **API** → **Create New Token**.

- It starts with `kgat_`. The legacy *username + key* pair is **no longer accepted**.
- Put it in your shell rc file — **never in this repo**:

  ```bash
  # ~/.bashrc or ~/.zshrc
  export KAGGLE_API_TOKEN='kgat_xxxxxxxxxxxxxxxx'
  ```

> **That token is a credential.** This repo's `.gitignore` covers `.env`; it does
> **not** cover your shell rc file. Think before you paste it anywhere.

Then open a **new terminal**. On many distros `.bashrc` returns early for
non-interactive shells, so `bash -c '...'` will not see the variable even though your
prompt does.

### Step 3 — Install the CLI and configure it (once)

```bash
uv tool install endpoint-vps   # the package is endpoint-vps; the binary is `endpoint`
endpoint init                  # interactive: Kaggle username, kernel slug, default model
endpoint doctor                # confirm the config was actually read
```

`endpoint --help` lists every accelerator and command. Both `init` and `boot` are
**interactive** and cannot be put into a scheduler, CI, or any non-interactive tool.

### Step 4 — Patch the notebook generator, **before** the first boot

Repo-specific, and not optional. `endpoint boot` runs a notebook that the installed
package *generates*; that generator has two defects (D-050) which make a healthy boot
look like a dead one.

```bash
bash scripts/apply-endpoint-ntfy-fixes.sh --dry-run   # check only, changes nothing
bash scripts/apply-endpoint-ntfy-fixes.sh             # backup → hash gate → apply → re-verify
bash scripts/apply-endpoint-ntfy-fixes.sh --verify    # behaviour, not just the hash
```

Skip this and the symptom is: **the boot succeeds but the tunnel URL never appears**,
and the kernel kills itself after about 32 minutes.

### Step 5 — Boot

```bash
endpoint -g boot
```

- **`-g` goes *before* `boot`.** `endpoint boot --gpu` fails.
- `-g` = **GPU T4 ×2**. Others: `endpoint boot` (CPU), `endpoint -t boot` (TPU v5e-8).
- **It asks which model to deploy.** That is by design; `--no-watch` means "do not
  stream status", **not** "do not ask questions".
- Expect **5–10 minutes**: Kaggle boots, builds `llama.cpp`, loads the model.

While it runs, open Kaggle in the browser:

```
https://www.kaggle.com/code/<your-username>/<your-kernel-slug>
```

`boot` sets these through the Kaggle API — you should not have to click anything. They
are what to check when something is wrong:

| Where | What it must say |
|---|---|
| **Accelerator** (top right) | **GPU T4 ×2** |
| **Settings → Internet** | **On** — no internet, no tunnel |
| **Input → Datasets** | the model dataset is attached |

When the cell output shows **`TUNNEL ACQUIRED`**, the URL exists.

### Step 6 — Take the URL and the key

```bash
endpoint base-url
```

You need two things: a URL that ends in `trycloudflare.com` (append `/v1`), and the
API key. Put the key where this repo's scripts read it — **`.env`, which is
gitignored**:

```bash
ENDPOINT_API_KEY=<paste it here>
```

That key ends up in **three** places, and only one of them heals itself. The
config yaml (`~/.config/endpoint/endpoint-config.yaml`) is the original, and every
`boot` bakes a copy of it — in plaintext — into the notebook pushed to Kaggle. The
repo's `.env` and Open WebUI's database hold **hard copies** that nothing refreshes;
they just go quietly stale and show up much later as a 401.

```bash
bash scripts/rotate-endpoint-key.sh --check   # do the three still agree?
bash scripts/rotate-endpoint-key.sh           # replace all three at once
```

Rotate **before** a boot, not after. The rotation is local — it does not touch
Kaggle. The old key stays alive in the already-pushed notebook until the next boot
replaces it, and that notebook is private but is still plaintext on someone else's
server.

### Step 7 — Verify **before** you wire it in

```bash
bash scripts/connect-endpoint.sh --url https://<tunnel>.trycloudflare.com/v1 --check
```

`--check` runs the conformance probe and **changes nothing**. The order is the whole
point: wire first and test later, and by the time it fails you have already pointed
the platform's only model source at a broken service — and Open WebUI will not
complain, it will just show an empty model list.

### Step 8 — Attach it

```bash
bash scripts/connect-endpoint.sh --url https://<tunnel>.trycloudflare.com/v1
```

The script probes first and **refuses to continue if the probe fails**. It writes
`openai.enable` / `openai.api_base_urls` / `openai.api_keys` into the **database** —
not `.env`, which stops mattering after the first boot (D-017) — restarts Open WebUI,
and reads the configuration back through the application's own `Config.get_many` path
rather than re-reading the row it just wrote.

### Step 9 — The step only you can do

**Open <http://localhost:3000> and look at the model menu.**

Open WebUI fetches a configured endpoint **lazily** — only when a signed-in user opens
the model list. Verified against a decoy service on 2026-09-19: after a restart with
`openai.enable=true` pointing at the decoy, **not one request arrived** until someone
asked for the model list. So the script can prove the *configuration* points at your
runtime; only your eyes can prove the *models* came back.

Nothing there? `docker compose logs --tail=50 open-webui`.

### Step 10 — When you are done

```bash
endpoint stop
```

**Read its exit code, not just its output.** `0` means stopped (or confirmed there was
nothing running); **`2` means the kill signal went out but the state could never be
read**, which in a non-interactive shell is the normal case — the Kaggle token lives in
your shell rc file and a non-interactive shell does not read it. `stop` used to call
that case "No running kernel found." and exit 0 while the GPU kept burning; it no
longer does ([D-060](DECISIONS.md), and `scripts/apply-endpoint-stop-fixes.sh`). To
confirm after a `2`, run `endpoint status` in a shell that has the token, or look at the
Kaggle web UI.

`endpoint kill-all` terminates **every running Kaggle kernel on the account** — useful
when a manually-opened notebook is quietly burning quota, and exactly why it is a
shotgun rather than a rifle.

### What will bite you

| Risk | What it actually is |
|---|---|
| Quota | GPU T4 ×2 = **30 h/week**, a session ends at ~**12 h**, and **60 minutes idle shuts it down** to free the GPU. Only *successful inference* counts — polling `/v1/models` does **not** keep it alive. |
| The URL changes every boot | Quick Tunnel URLs are random. That is obscurity, **not authentication**: anyone with the URL *and* the key can use your GPU. Never hardcode it. |
| An `endpoint-vps` upgrade erases step 4 | The patch edits a package-manager file. Re-run `--verify` after every upgrade. |
| `endpoint status` says `offline` | It can lie — the status file says the kernel is dead while the runtime is still answering. Probe the endpoint instead. |
| `boot` succeeded but no URL | Almost always the unpatched rate limiter (step 4), or the Internet toggle is off. |
| `GET /v1/apikey` | Upstream documents this route. **Whether it requires authentication has not been verified by this project** — if it does not, the URL alone is enough to obtain the key. Check it yourself with `curl` after boot. |

**Terms of use.** Kaggle's terms limit the service to personal, non-commercial use.
That makes Kaggle the right tool for *measuring what a larger model can do*, and
**not** the product path for a company assistant. The full reading — including which
parts of those terms were actually verified and which were not — is in
[`docs/ENDPOINT.md`](docs/ENDPOINT.md).

---

## Securing remote access

### First, where data flows **out**

Security has two directions, and this project originally only documented one.
"Who can reach in" is a question about ports and Cloudflare Access (below).
"**Where does data flow out**" is a different question — and until it was
measured on 2026-09-19, this project had that one wrong:

```
$ bash scripts/check-egress.sh --check
  ✗ 啟用中  openai.api_base_urls
       https://api.openai.com/v1
       開關：openai.enable = True                    EXIT=1
```

Open WebUI's `ENABLE_OPENAI_API` defaults to `True`, and
`OPENAI_API_BASE_URLS`'s default empty string is **a fallback, not an off
switch** — it gets filled in as `https://api.openai.com/v1`. The two together
mean a stack whose stated goal is "data stays in the company" ships with
**OpenAI's public API connected and enabled**. And `OPENAI_API_KEY` is a
globally exported environment variable on many dev machines — if it is present,
`gpt-4o` appears in the model picker.

Fix: `bash scripts/check-egress.sh --fix` (write → restart → read back to
confirm). **Editing `.env` does nothing on an existing deployment** —
environment variables are only the seed for first boot, after which Open WebUI
treats the database as authoritative (exactly the `ENABLE_SIGNUP` shape).
See [`DECISIONS.md`](DECISIONS.md) D-017.

This probe answers "where the *configuration permits* data to go", not "where
data actually went".

---

**Phase 1 carries no private data, so the rest of this section did not matter
yet. RAG is what changes that** — the moment you upload your own documents, the
transport between your browser and the model becomes the thing worth protecting.
Do this **before** you upload anything. See [`DECISIONS.md`](DECISIONS.md) D-012.

An unauthenticated Open WebUI is not just a chat window. Whoever reaches it can
read every conversation already stored, use your model, and — if `ENABLE_SIGNUP`
is still `true` — **register an account**, where the first registrant becomes
administrator.

### Choose one

| Option | Identity check | Needs a domain | Notes |
|---|---|---|---|
| **Codespaces private port** (default) | GitHub account | No | Zero setup. Only you, authenticated, can reach it. **The baseline — already on.** |
| **Cloudflare Tunnel + Access** | Email OTP / Google / GitHub SSO | Yes | Stable hostname, survives codespace rebuilds. Free Zero Trust tier covers 50 users. |
| **Tailscale** | Device | No | Device-level rather than user-level; simplest if all your clients can join the tailnet. |

If you only ever reach it from a browser where you are signed in to GitHub, the
default private port is already sufficient and you can stop reading. The tunnel
earns its place when you want a **memorable hostname** that keeps working after
the codespace is recreated, or **access from a device that is not GitHub-authenticated**.

### Cloudflare Tunnel + Access

The tunnel is established **outbound** by a `cloudflared` container, so **no port
is ever published** — the same reasoning as Ollama in [D-003](DECISIONS.md). Because
the tunnel's identity lives in Cloudflare rather than in the codespace, an
ephemeral environment reconnects to the **same hostname** on every rebuild.

1. **Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel**
2. Add a **Public hostname**; set the service to `http://open-webui:8080`
   (the *container* port 8080, **not** the published 3000)
3. Copy the tunnel token (`eyJ…`) into `.env` as `CLOUDFLARE_TUNNEL_TOKEN`
4. **Access → Applications** → add a policy allowing only your own email
5. Uncomment `COMPOSE_PROFILES=tunnel` in `.env`, then:
   ```bash
   bash scripts/up.sh
   ```

`cloudflared` runs under the `tunnel` compose profile and is **not created at all**
unless you enable it. `bash scripts/up.sh` refuses to start if the profile is on
but the token is empty — a missing token would otherwise surface only as a
confusing error in the container log.

**Verify it worked:**

```bash
bash scripts/status.sh                      # shows whether cloudflared registered
docker compose logs --tail=20 cloudflared   # "Registered tunnel connection"
```

**To turn the tunnel off:** comment `COMPOSE_PROFILES` back out and run
`bash scripts/up.sh`. Disabling a profile does **not** stop a container that is
already running, and — this is the part that is easy to get wrong —
**`--remove-orphans` does not remove it either.** Compose defines an orphan as a
service that is not *defined* in the compose file; a profile-disabled service is
still defined, so the flag never touches it (measured three ways: `up -d
--remove-orphans`, `down --remove-orphans`, and plain `down` all leave it
running). `up.sh` therefore removes stale containers by **set difference** —
services `config --services` reports as active, versus the service labels of the
containers that actually exist — and `down.sh` passes `--profile '*'` so that
"stop the stack" means the whole stack. Without this, "I turned the tunnel off"
would be a false belief while the service stayed reachable, and `status.sh`
would have printed "not enabled" while it did so.

If a `cloudflared` container is running while the profile is off, `status.sh`
now says so in red rather than reporting the tunnel as off.

> ⚠️ **Never use a Quick Tunnel (`*.trycloudflare.com`) for private data.**
> Quick Tunnels have **no Access policy attached** and are effectively public to
> anyone who learns the URL. They are for demos with nothing at stake. This repo
> never uses one.

> ⚠️ **The tunnel token is equivalent to control of that tunnel.** Anyone holding
> it can point your hostname at a server they control. It belongs in `.env`
> (gitignored) and nowhere else.

> **What this does not protect against:** Cloudflare terminates TLS, so Cloudflare
> sees plaintext in transit, as any reverse proxy would. If that is unacceptable,
> use Codespaces' private port or Tailscale instead — with those, no third party
> sits in the path.

### Lock down registration

Whichever option you choose, do this **first**:

```bash
bash scripts/lock-signup.sh
```

It reads the **real** state over HTTP, and if signup is open it closes it through the
config API and reads the value back to confirm. Existing accounts are unaffected.

> **`ENABLE_SIGNUP` in `.env` does not control this.** It is only read on the very first
> boot, when Open WebUI seeds its settings database; after that the database wins and
> editing `.env` (even followed by a container rebuild) has **no effect**. This was
> measured, not inferred — three boots against a shared volume with the env var flipped
> each time left the switch unchanged. Full evidence in `DECISIONS.md` D-012.
>
> In practice you are protected before you get here: Open WebUI automatically disables
> signup when the **first** account is created. This script verifies that, and fixes it
> if it is ever turned back on.
>
> `.env.example` ships `ENABLE_SIGNUP=false`. That does **not** lock you out — a fresh
> install was measured and still creates the first admin (HTTP 200, `role=admin`). See D-012.

---

## Verification checklist

This project **does** have automated tests — `scripts/test_rag_probe.py` (57
checks), `scripts/test_verify_api.py`, `scripts/test_ask_probe.py`,
`scripts/test_egress_probe.py`, `scripts/test_probe_openai.py`,
`scripts/test_runtime_state.py`, `scripts/test_rag_grounding_probe.py` and
`scripts/test_rag_http_probe.py` —
offline unit tests: six for the probes, one for the runtime-config reader,
and one for the RAG grader, whose judgement was wrong four times in a row on
2026-09-19 (all four were **false failures**; the regression cases are the
model's actual replies, kept verbatim). Its tests also pin down the difference
between "the model is slow" and "the model is unreachable" — the probe itself
got that one wrong, reporting a slow model as an unreachable one (D-018) —
plus `scripts/verify.sh` (5 tests
against the live API). `test_ask_probe.py` did not exist until D-016: its grading logic had
never been exercised, and its **first** live run exposed a false failure in it.
An earlier version of this section claimed the opposite, and that claim is kept
here as a record of what it caused: once *"inference quality is inherently a
human judgement call"* was written down as a premise, nothing ever checked
whether an answer was **true**. `verify_api.py` test 5 asks the model about MCP
and graded only its **form** — response non-empty, no prompt leakage, not
truncated. When the model replied *"MCP is a model provided by Alibaba Cloud"*,
every check passed. See `DECISIONS.md` D-014.

The steps below are still performed by hand. What must **not** be by hand is
factual correctness — that is what `scripts/ask_probe.sh` is for.

### Phase 1: base stack

- [ ] **Containers healthy** — `bash scripts/status.sh` shows all three containers
      `running` and `healthy` (`ollama`, `open-webui`, `mcp-test-server`)
- [ ] **Model ready** — `qwen3:4b` appears in the model list
- [ ] **Inference works** — ask a question whose answer you can check by eye.
      Ask about **HTTP, not MCP**: this item used to say "explain MCP", and
      `qwen3:4b` answers that one confidently and **wrongly**, so
      "a sensible answer arrived" passes while being false (D-014)
- [ ] **Facts are mechanically checked, not eyeballed** —
      `bash scripts/ask_probe.sh qwen3:4b` runs **both** the `mcp` question
      and its `http` control question to completion. The control question is the
      whole point: without it you cannot tell *"this model is unreliable"* from
      *"this model never saw MCP"*, and those have opposite fixes (D-014).
      The `mcp` question is **expected to fail** — that is the finding, not a
      defect; don't tune settings to turn it green
- [ ] **The grading itself has been exercised** — a check that has only ever run
      against a model returning empty answers has never been tested. The grading
      in `verify_api.py` first actually ran against `qwen2.5:3b`, and **that first
      run caught a false failure in the check itself**: the `http` control question
      answered correctly but was marked wrong for not spelling out the acronym
      (D-016). **A test that has never produced a verdict is not a tested test**
- [ ] **Speed is measured, not assumed** — expect roughly **30–110 s** per
      answer, and **264–293 s** for `qwen3:4b` with thinking on. The
      "10–30 seconds" previously written here matched no measurement; it
      coexisted with D-011's contradicting numbers for a full day before anyone
      noticed. Thinking, not model size, is the dominant cost — the same class
      of model without it answers in **7–15 s** (D-011, D-014)
- [ ] **Streaming works** — the answer appears token by token, not in one block
      after a pause
- [ ] **Memory holds** — during a conversation, the `Mem` line in `status.sh`
      still shows headroom (not fully consumed by swap)
- [ ] **Model unloads** — after idling past `OLLAMA_KEEP_ALIVE`, `docker stats`
      shows the ollama container's memory drop noticeably
- [ ] **Survives restart** — after `bash scripts/down.sh && bash scripts/up.sh`,
      the model is **not** re-downloaded and prior conversations are still there

### Phase 2: before uploading any document

- [ ] Signup is actually closed — run `bash scripts/lock-signup.sh --check` (exit code 0
      means verified closed; it does **not** read `.env`, which is not what governs this)
- [ ] You have chosen how the service is reached, and are aware of that choice's
      trade-offs (see [Securing remote access](#securing-remote-access) — staying
      on the default private port is a valid answer)
- [ ] You are **not** using a Quick Tunnel

### Phase 2: RAG

- [ ] Create a knowledge base under **Workspace → Knowledge**
- [ ] Upload a PDF or Markdown document
- [ ] Ask a question about it and confirm the answer cites the document rather
      than being generated from nothing
- [ ] **Negative test:** ask about a detail that is *not* in the document and
      confirm the model says it doesn't know instead of inventing an answer

> **Those four items are mechanically checkable — and without an account.**
> `bash scripts/rag-verify.sh` runs them against a **fabricated** document using
> the application's own retrieval functions (`query_collection`,
> `get_embedding_function`, `VECTOR_DB_CLIENT`, `apply_source_context_to_messages`),
> so "the model read the file" cannot be confused with "the model already knew".
> Every fact it asks about is invented, which is the point: a real regulation
> would let the model answer from memory and prove nothing.
>
> It reports **retrieval and grounding separately**, because "the right chunk was
> never found" and "the right chunk was found and ignored" are different bugs
> with different fixes. See D-018.
>
> **What it does not prove:** it ingests through the app's own chunker, embedding
> function and vector client, but *assembled by the probe* — not through the
> `/api/v1/files` HTTP path. That path needs a logged-in user, and this database
> currently has **zero users**: sign-up is locked (D-012), so creating the first
> admin is a step only you can take. Do the four boxes above in the UI once you
> have an account; use the script to know what to expect beforehand.
>
> **That gap has a companion script now:** `bash scripts/rag-http-verify.sh` runs
> the same four items through `/api/v1/knowledge/create`, `/api/v1/files/` and
> `/api/chat/completions` — the path the UI itself takes — using an API key from
> `.env`. It also argues with the *other* blind spot: it cannot see which chunks
> were retrieved, so "the right chunk was never found" and "it was found and
> ignored" are indistinguishable there. Run both; neither alone is the whole
> answer (D-019).
>
> **API keys are off by default in this stack.** Checked in the database on
> 2026-09-20: `auth.enable_api_keys = false` and zero rows in `api_key`, so the
> account page's "Create" either isn't there or returns `403
> API_KEY_CREATION_NOT_ALLOWED`. Switch it on under **Admin Panel → Settings →
> Authentication → API Keys** (`http://localhost:3000/?settings=admin:authentication`)
> first. Like every other Open WebUI setting, that one lives in the database —
> **editing `.env` will not do it** (D-013).
>
> Nor does it stress the context window. The probe's document is 478 characters
> (~765 tokens); this stack loads models with **`num_ctx` 4096**, and Open WebUI
> never sets that value. With `chunk_size=1000`, a retrieval of six full chunks
> is roughly 6700 tokens — the excess would be **silently dropped**, and what
> gets dropped is whatever ranked last. Treat the passing result as "the chain
> works", not as "RAG holds for real documents" (D-018).

> **Note for non-English documents:** Open WebUI's default embedding model is
> `sentence-transformers/all-MiniLM-L6-v2` — English-only, 384 dimensions,
> ~500MB RAM. For Chinese or other non-English documents, retrieval quality will
> be poor unless you switch to a multilingual embedding model. The candidates
> were measured on this stack; the decision (see `DECISIONS.md` D-013) is the
> **`ollama` engine with `qwen3-embedding:0.6b`**.
>
> **Set it in the Admin UI — `.env` will not work.** Open WebUI seeds its config
> table on first boot and existing database values win from then on, so changing
> `RAG_EMBEDDING_ENGINE` in `.env` and recreating the container does nothing
> (verified in the source, not inferred — D-013). Use
> **Admin Panel → Settings → Documents → Embedding**.
>
> Two scripts serve that decision: `bash scripts/set-embedding.sh` reads or
> writes the setting with a read-back confirmation (the Admin UI route needs an
> account; this does not), and `bash scripts/rag_probe.sh` runs a
> Traditional/Simplified retrieval comparison against candidate models. `bash scripts/rag-verify.sh`
> above is the one that tells you whether the model you ended up with actually
> grounds its answers.
>
> Pull the model first: `bash scripts/pull-model.sh qwen3-embedding:0.6b`.
> **Changing the embedding model later requires re-embedding every document**,
> so decide before you upload.
>
> The "RAM spiked from 2GB to 14GB" figure you may have seen is specific to
> `jina-embeddings-v3`, not a general property of larger embedding models —
> see D-013.

### Phase 2: MCP

- [ ] Add an MCP server under **Admin Panel → Settings → Integrations**, in the
      **Tools** section — the **+** next to *External Tool Servers*
      (Type must be **MCP Streamable HTTP** — not OpenAPI)
- [ ] Confirm the tool appears in the conversation's tool list with a
      `server:mcp:` prefix
- [ ] Trigger a tool call and confirm the model **decides on its own** to use
      the tool rather than being explicitly told to
- [ ] **Multi-turn test:** a task requiring two or more sequential tool calls —
      confirm the 4B model can hold the flow together

> **Status (2026-09-20): both of the last two items are now verified — see
> D-023.** With the four feature switches off (37 tools → 16,
> `bash scripts/toggle-builtin-tools.sh --off`), the model called `roll_die` on
> its own from the bare prompt "roll a die for me" — no tool name mentioned, no
> instruction to use one. A two-turn task then produced **three sequential tool
> calls in a single response** (two `roll_die` plus one `echo`), and the final
> answer added the two real results correctly: 5 + 6 = 11.
>
> The evidence is mechanical rather than a judgement of the wording. Every
> `function_call` the model emits carries a `call_id`, and the result comes back
> as a `function_call_output` bearing the **same** `call_id`. That chain is
> stored in the chat record and in the MCP server's `CallToolRequest` log, so it
> survives the removal of any temporary instrumentation — and it is the only way
> to tell a real tool call from a plausible-looking number. D-023 §6 documents a
> turn where the model answered "6" with no `function_call` at all.
>
> **Teardown is deliberately not done yet.** Phase 3 had to re-measure tool
> calling *through LangGraph's code path* — D-011's result does not transfer
> across that boundary — and this server is the ready-made fixture for it. That
> measurement is now done and passed (2026-09-20, D-024), so the original reason
> has expired. **The fixture is still kept**, for a different reason: it is the
> only tool the Phase 3 agent has to call, and items 2–6 will each want it. The
> admin-panel connection can be removed whenever the fixture is no longer wanted
> in Open WebUI — nothing in the LangGraph path goes through it.

> **A ready-made test server:** `docker compose up -d --build mcp-test-server`
> starts a Streamable HTTP MCP server that lives only on the internal `ai-net`
> network (no published port — D-003), exposing two tools: `echo` (the simplest
> possible tool call) and `roll_die` ("roll two dice and add them" is a
> ready-made multi-turn test). Use URL
> `http://mcp-test-server:8000/mcp` with Auth set to *None*. Run
> `bash scripts/verify-mcp-server.sh` first — it performs the full handshake
> from the open-webui container's point of view. Once Phase 2 verification is
> done, delete the `mcp-test-server` service and the `mcp-server/` directory.

> **This is no longer an open question.** D-011 verified that multi-turn tool
> calling **passes** on this stack, so listing "can a 4B-class model do this at
> all?" as *the critical unknown* for this phase was stale (D-014).
>
> **But the result is narrower than it reads, and the difference decides
> Phase 3.** That evidence was gathered through **Open WebUI's own tool-calling
> pipeline**. A LangGraph agent calls tools through `bind_tools` / the Ollama
> integration's native function calling — a **different code path entirely**.
> D-011's pass therefore does **not** transfer, and must be re-measured inside
> the agent framework before Phase 3 leans on it. This is the exact failure mode
> D-014 is about: a result measured under one set of conditions, remembered as a
> conclusion, then reused under conditions it was never measured in.

---

## Quota management

### Check your usage

<https://github.com/settings/billing> — two independent meters:

- **Compute** — 120 core-hours
- **Storage** — 15 GB-month

### Reducing consumption

| Action | Effect |
|---|---|
| Choose 2-core over 4-core | Halves quota burn |
| Run `bash scripts/down.sh` when done | Stops compute billing |
| `gh codespace stop -c <name>` | Stops compute billing |
| **Delete** the codespace (not just stop) | Stops **storage** billing |
| Close the editor tab | Avoids being counted as active |

### Important

Running processes, terminal output, and traffic on an open port all count as
activity — **even when you are away from the keyboard**. Manually stopping is
always the safe move.

Once either quota is exhausted and no payment method is on file, you **cannot
create or resume codespaces** until the monthly reset.

---

## Phase 2

Open WebUI has supported MCP **natively since v0.6.31**, and its built-in agentic
tools already cover most of the original plan:

| Original requirement | Open WebUI built-in tool |
|---|---|
| RAG knowledge base | `query_knowledge_bases` / `search_knowledge_files` / `view_knowledge_file` |
| Memory | `search_memories` / `add_memory` |
| Web retrieval | `search_web` / `fetch_url` |
| Notes / chat history | `search_notes` / `write_note` / `search_chats` |

**LangGraph is not introduced for Phase 2** — D-005 deferred it until *"a need
appears for custom multi-step workflows or an explicit state machine."* That
condition is now being invoked deliberately for Phase 3, which is a state
machine by definition; see [Phase 3 and 4](#phase-3-and-4--planned-all-six-items-are-measured)
below and [`DECISIONS.md`](DECISIONS.md) D-005 for the full trade-off.

> **Licensing warning, verified:** the `langgraph-server` / `langgraph-api`
> production container image is **Elastic License 2.0**. Self-hosting it in
> production requires a license key (`LANGSMITH_API_KEY` on Plus or higher, or
> `LANGGRAPH_CLOUD_LICENSE_KEY`); without one it raises `INVALID_LICENSE` at
> startup. The `langgraph` **library** is MIT and free — use it inside your own
> service, or use `langgraph dev`, and avoid the commercial server image.

### Where the pieces run (Phase 2)

Phase 1's picture has two long-running containers. Phase 2 adds a third, plus a
verification layer that is not a service at all. The diagram below is read off
the compose file and the probe scripts rather than drawn from memory — each
claim in it has a citation in the bullets:

```
┌──────────────────────────────────────────────────────────┐
│  Docker network: ai-net                                  │
│                                                          │
│  long-running (docker compose up -d):                    │
│   ┌──────────┐         ┌───────────────┐                 │
│   │  ollama  │◄────────│  open-webui   │                 │
│   │  :11434  │         │ :8080 → :3000 │                 │
│   └────▲─────┘         └───────┬───────┘                 │
│        │                       │                         │
│        │  MCP (Streamable HTTP)│                         │
│        │                       ▼                         │
│        │               ┌───────────────┐                 │
│        │               │ mcp-test-srv  │                 │
│        │               │   :8000/mcp   │                 │
│        │               │ echo,roll_die │                 │
│        │               └───────────────┘                 │
│        │                                                 │
│  one-off (docker run --rm, per experiment):              │
│   ┌────┴─────────────────────┐                           │
│   │    probe container       │                           │
│   │    mem0 + ChromaDB       │                           │
│   │    + history.db          │                           │
│   └──────────────────────────┘                           │
└──────────────────────────────────────────────────────────┘
```

- **`mcp-test-server` exists for one checklist.** It is built from `./mcp-server`
  and serves two tools — `echo` and `roll_die` — over Streamable HTTP at
  `http://mcp-test-server:8000/mcp`. It publishes no port, so only `open-webui`
  on `ai-net` can reach it (`docker-compose.yml:197-200`, same reasoning as
  ollama's unpublished `:11434`, D-003). The compose comment says to delete the
  service and the `mcp-server/` directory once the MCP checklist is verified
  (`docker-compose.yml:196`).
- **It is not optional; `cloudflared` is.** `mcp-test-server` declares no
  profile, so the bare `docker compose up -d --wait --remove-orphans` in
  `scripts/up.sh:76` starts it and waits on its healthcheck. That healthcheck
  only checks that the port is listening — deliberately, because `GET /mcp` on a
  streamable-http server does not answer `200`, so an HTTP status check would
  report a false failure (`docker-compose.yml:202-204`).
- **The MCP connection itself is database state, not configuration.** There is no
  environment variable for it; it is added in the UI and stored in Open WebUI's
  database — see [Enabling MCP](#enabling-mcp).
- **`:8080 → :3000` is the one published port.** Open WebUI listens on `:8080`
  inside the container; compose publishes it to the host on `:3000`, bound to
  `${WEBUI_BIND_ADDR:-0.0.0.0}` (`docker-compose.yml:88`).
- **The probe layer is not part of the deployed stack.** It is how Phase 2's
  claims get checked: each probe is a `docker run --rm --network
  <project>_ai-net` one-off, with the image built on the spot from an inline
  heredoc (`docker build -t … -f -`, so no `Dockerfile` is on disk) and the
  probe `.py` bind-mounted read-only. Two images exist: `…-mem0-probe` (mem0 +
  ChromaDB) and `…-langgraph-probe` (tool calling, and the Phase 3 runtime
  questions).
- **ChromaDB and `history.db` live inside the probe container**, embedded, so
  they die together with `--rm` (D-027 §7). An experiment that needs to read back
  what an earlier call wrote has to finish inside one run.
- **What Phase 2 does not change:** `qwen3-embedding:0.6b` is served by the same
  `ollama` container as the chat model, `:11434` stays unpublished, and
  `cloudflared` stays behind the `tunnel` profile.

### Enabling MCP

There is **no `ENABLE_MCP` environment variable** — it does not exist in Open WebUI
(verified against the official docs and the backend source), so setting it would only
make you think MCP was on. MCP is enabled by adding a connection, which is stored in
the database:

1. **Admin Panel → Settings → Integrations**
2. Scroll to the **Tools** section and click the **+** next to *External Tool
   Servers*
3. **Click the word `OpenAPI` on the Type row** — it is a **toggle button**, not
   a read-only label; it switches to `MCP Streamable HTTP`
4. Enter the server URL and authentication (the test server in this repo uses
   *None*)
5. Save

> The path above is verified against **Open WebUI v0.11.3**, the version this
> stack runs. Older docs say "Admin Settings → External Tools", which does not
> exist in this version — the tab is **Integrations**. The Type toggle is where
> people get stuck, because it looks like a static label.

**After saving, reload the page (`F5`).** The new MCP tools will not appear in a
chat's tool menu until you do, and there is nothing to wait for — they stay
missing indefinitely. This is client-side store staleness, not a failed
connection, and it is worth knowing before you go debugging the server:

- `routes/(app)/+layout.svelte:187` loads tools into the `$tools` store **once**,
  when the layout mounts.
- `Chat.svelte:990` only re-fetches **if `$tools` is empty** — and an empty array
  is truthy in JavaScript, so once the store holds anything, that guard is shut
  for the rest of the session.
- Navigating between chats is client-side routing: the layout does not re-mount,
  so neither path re-runs.

A full page load re-mounts the layout, re-runs `setTools()`, and the
`server:mcp:<id>` entries appear. **If the tools are still missing after a
reload**, the connection itself is the problem — go back to the admin panel,
where a server that fails its handshake is reported as a toast when the list
loads. (See *Verifying the MCP server* below — the handshake can be checked
directly, without the UI.)

Only administrators can add MCP servers, and only the **Streamable HTTP** transport is
supported natively — for stdio/SSE servers, front them with
[mcpo](https://github.com/open-webui/mcpo). Set `WEBUI_SECRET_KEY` (this stack does) or
OAuth-connected MCP tools break on every container restart with "Error decrypting
tokens".

**Known constraints:**

- **Streamable HTTP only** — no stdio, no SSE. This is deliberate
  (browser and multi-tenant security).
- **MCP servers are admin-only.** Non-admins can only add OpenAPI servers.
- **stdio-based servers** (the kind Claude Desktop uses) need the
  [**mcpo**](https://github.com/open-webui/mcpo) proxy to bridge them to OpenAPI.
- OAuth 2.1 tools **cannot** be set as model defaults — they need an interactive
  redirect.

---

## Phase 3 and 4 — planned; all six items are measured

> **Status: mostly a plan.** The section exists so the design is written down
> *before* it is built, and so the assumptions it rests on are visible enough to
> be tested. Every claim below is marked **[verified]** (checked against
> docs/source on 2026-09-19, or measured since) or **[open]** (measured by
> nothing yet).
>
> **As of 2026-09-21 all six items have been measured** (D-024, D-026, D-027,
> D-030) — tool calling does survive the change of code path, mem0 does reuse a
> pre-created ChromaDB collection, mem0's extraction call does pay for a prompt
> it cannot fully deliver, the recursion guard's rule is measured, and the
> scheduler's failure mode is measured. **Item 5's premise did not survive
> measurement** — see item 5 below. Each item passing only ever meant it was no
> longer wasted effort to try the next. It does not mean Phase 3 works.

This revisits **D-005**, which deferred LangGraph until *"a need appears for
custom multi-step workflows or an explicit state machine."* That condition is
now being invoked deliberately, not quietly assumed away — Phase 3 is a state
machine by definition. D-005's status was *"pending review after Phase 1
verification"*, and Phase 1 is complete. **D-007 still stands unchanged and
unaffected:** the `langgraph` *library* is MIT; the `langgraph-server` /
`langgraph-api` commercial container image is Elastic License 2.0 and is not
used.

### The planned stack

| Layer | Choice | Notes |
|---|---|---|
| State machine | `langgraph` (library) | MIT — embed in our own service, never the ELv2 server image |
| Model binding | `langchain-ollama` | binds `qwen3:4b` / `qwen2.5:3b` once Phase 1 benchmarking settles the choice |
| Tool bridge | `langchain.mcp` → `MCPAdapter` | **[verified]** — see the correction below |
| Loop guard | `recursion_limit` **per graph**, not 5 | **[measured]** item 4 — 5 is too tight |
| Memory | `mem0` + ChromaDB | **[verified]** items 2 and 3 |
| Scheduler | `APScheduler`, in-process | **[measured]** item 6 — persistence is a contract, not a swap |
| Multi-step | LangGraph Plan-and-Execute | Plan → Execute → Check → Retry/Summarize |
| Multi-agent | LangGraph Supervisor (tool-based) | `langgraph-supervisor-py` optional, not introduced up front |

**Correction on the MCP bridge.** The plan said to use `langchain-mcp-adapters`
only if it were still current, pinning the version otherwise. Checking was the
right instinct — **it is no longer the recommended path.** As of LangChain's
2026-09-03 release, MCP ships inside the main package as `langchain.mcp`, and
`MCPAdapter` "replaces the standalone `langchain-mcp-adapters` package."
Install is `pip install "langchain[mcp]"`, requiring `langchain[mcp]>=1.4.0`.
`MultiServerMCPClient.get_tools()` maps to `MCPAdapter.list_tools()`, and
transports are now **inferred from the target** rather than named explicitly.
Be aware `langchain.mcp` is **beta** — importing it raises `LangChainBetaWarning`
and the API may still change, so pin the version once chosen.

### Open items that decide whether this works

Each of these was, at the time of writing, a measurement this project had not
made. They are listed in the order they should be settled, because later ones
are wasted effort if an earlier one fails. **All six are now measured** (item 5's
premise did not survive — read it before acting). Item 3's own rates are
recorded in D-027; they do **not** re-baseline throughput. The re-baseline was
attempted on 2026-09-21 and **failed its own stability criterion** (D-031) — so
it is still open, and every rate on record is still either from the old VM or
from a round that was not stable enough to keep.

Reproduce items 4–6 with:

```bash
bash scripts/verify-phase3-runtime.sh          # all three, human-readable
bash scripts/verify-phase3-runtime.sh --json   # machine-readable on stdout
```

1. **[verified] Does tool calling survive the change of code path?** — **Yes.**
   Measured 2026-09-20 (D-024) via
   `bash scripts/verify-langgraph-tools.sh --model qwen2.5:3b`. D-011 proved
   multi-turn tool calling through **Open WebUI's** pipeline; LangGraph binds
   tools via `bind_tools` and the Ollama integration's native function calling —
   a different implementation. The re-measurement held everything *except* the
   code path constant (same model, same 2 tools, same `num_ctx`, same two
   questions), so the result is attributable to the code path alone:

   - The model called `roll_die` on its own — the prompts never named a tool.
   - Round 2 issued two `roll_die` calls and answered **4** from two real
     returns of **2** each. Because both dice returned the same value, the `4`
     cannot be a restatement of either die — it can only come from adding them.
   - Three independent layers agree: the `call_id` chain (3 calls, 3 matching
     returns, no orphans), the arithmetic against real returns, and the MCP
     server log (`CallToolRequest` × 3 from the probe container — evidence that
     does not pass through the model at all).

   **One difference worth carrying forward: on this path the model issues tool
   calls one at a time, not as a batch.** Open WebUI emitted 3 calls ~10 ms
   apart (one response); LangGraph emitted its 2 calls **7.4 s** apart — three
   separate LLM round trips. At the single-digit tok/s this machine decodes at
   (D-031 and D-033 both measure single-digit t/s and neither is a baseline —
   D-033 found the criterion was structurally unreachable, since fixed),
   "one more step" on
   LangGraph costs *one more LLM round trip*, not one more token. That bears
   directly on items 3 and 4. It is a single observation — not yet attributable
   to LangGraph, langchain, or sampling.

   **Not measured, do not over-read:** the prompts here were 213–364 tokens,
   nowhere near the 4,095 truncation trigger D-022 found — so this run does
   **not** show that Phase 3 can keep `num_ctx=4096`. Once mem0, more tools and
   longer histories are attached, prompts grow back (D-014). Also n=1.

2. **[verified] Chroma does not reject this project's embeddings — but a model
   swap is silent.** Measured 2026-09-20 (D-026) via
   `bash scripts/verify-chroma-dims.sh`. **The prediction above was wrong, and
   measuring it found something worse.** Three corrections:

   - **There is no 1536 anywhere in this path.** `ChromaDbConfig`'s fields are
     `collection_name / client / path / host / port / api_key / tenant` — no
     dimension field at all. 1536 is the default of mem0's *OpenAI* embedder and
     of other vector stores (pgvector, Milvus, Redis, …), not of Chroma. So
     "pin `embedding_dims`" has nowhere to be pinned.
   - **mem0's ollama embedder declares 512, and that value is inert.**
     `OllamaEmbedding.__init__` sets `embedding_dims = … or 512`, but `embed()`
     passes ollama's vector through untouched. Measured: declares 512, returns
     1024.
   - **The dimension is fixed by the first write, not by a default.** A fresh
     collection reports no dimension and `metadata` is `None`; the first insert
     locks it. A later insert of a different length *is* rejected loudly:
     `InvalidArgumentError: Collection expecting embedding with dimension of
     1024, got 512` — but that is the case a dimension check already catches.

   **The real hazard is the opposite of the one predicted.** Both embedding
   models on this machine — `qwen3-embedding:0.6b` and `bge-m3:latest` — are
   **1024-dimensional**. A dimension check therefore has *no* power to detect a
   model swap, and Chroma raises nothing: retrieval simply returns the wrong
   document. Measured on one sentence under both models, cosine **0.0074**; two
   unrelated sentences under one model, cosine **0.3308**. The same sentence
   across models sits *further apart* than two unrelated sentences within one —
   the two spaces are not in the same coordinate system, and nothing in the
   stack says so.

   **What to do instead:** write the embedding model name into the collection's
   `metadata` at creation, and compare it when opening. Measured to work — the
   record survives mem0's `create_col()` (which passes only a name and an
   `embedding_function`), mem0's `ChromaDB` adopts a pre-created collection, and
   re-issuing `get_or_create_collection` with different metadata neither raises
   nor overwrites, which makes the record authoritative. `guard_verdict()` in
   the probe is the reference implementation.

   **Not measured:** anything on mem0's `add()` path. That runs an LLM to
   extract facts before storing, which is item 3. This probe drives the vector
   store and the embedder directly, so it needs no API key and no LLM.

3. **[verified] mem0 does cost one extra LLM call; at the default `num_ctx`
   that call never sees its own instructions; and its default generation budget
   is too small for the extraction to finish.** Measured 2026-09-20
   (D-027) via `bash scripts/verify-mem0-add-cost.sh`. The claim above was
   structurally right, and measurably worse than it sounds:

   - **One call — confirmed by intercepting it, not by reading the source.**
     The probe wraps `llm.client.chat`, so it captures the *same dict* mem0
     sends: same messages, same options, including the
     `Please respond with valid JSON only.` that `format=json` appends. One
     `add()` → one chat call. Phase 5's hash dedup runs **after** Phase 2's
     extraction, so writing the same content twice still pays twice.
   - **The extraction prompt is 33,653 characters of fixed overhead** —
     `ADDITIVE_EXTRACTION_PROMPT`, `configs/prompts.py:468` — and mem0's
     `OllamaLLM` **never sends `num_ctx`** (`llms/ollama.py:129-134` sends only
     temperature / num_predict / top_p). So it takes ollama's default of 4096.
   - **At that default, 5,997 of the prompt's 8,047 tokens never reach the
     model** — and what is dropped is the **beginning**, i.e. the model's own
     instructions. Confirmed with equal-length canaries at the head and tail of
     the system prompt: the tail is visible, the head is not — and with the two
     canaries **swapped**, the same end is still the visible one, which rules
     out the model simply echoing the marker names it was asked about.
     *(This bullet was written before it was measured, and the first
     measurement came back empty — the model's answer was cut off at
     `num_predict` and the probe reported that as "neither end visible". The
     instrument was wrong, not the claim; the line above is the re-measurement
     after fixing it, D-027 §10.)*
   - **To stop truncating, `num_ctx` must exceed the prompt's own token count.**
     The rule is `num_ctx >= prompt tokens + 1`. It is a *threshold*, not an
     extrapolation — measured by bracketing it to within one token using a small
     prompt: at `num_ctx = P` the prompt is cut to `truncated_length(P)`, at
     `num_ctx = P + 1` it passes through untouched. The same bracket falsifies
     both alternative rules (threshold = `num_ctx/2`, threshold = `num_ctx - c`),
     which are indistinguishable from the real one at the single point the old
     reading rested on. **Do not carry `8048` around as "the number for mem0"**:
     it is that rule applied to the prompt *this probe reconstructs* (8,047
     tokens). ollama's own log reports `prompt=8052` and `prompt=8100` for the
     two real `add()` calls in the same run, so a real `add()` needs `>= 8101`.
   - **…but that is only the first of two thresholds, and the obvious one is not
     sufficient.** The rule above governs truncation *before generation starts*.
     ollama launches llama-server with `--context-shift --keep 4`, so a
     generation that fills the context does not stop — llama-server **discards a
     block out of the middle of the prompt** and keeps going (D-035; log line
     `slot context shift, n_keep = 4, n_left = 8187, n_discard = 4093`). At
     `num_ctx=8192` an 8,052-token prompt leaves only **140** tokens of room, and
     both real `add()` calls hit it. The rule that actually holds is
     `num_ctx > prompt_tokens + num_predict` — 8,100 + 2,000 + 1 = **10,101** —
     so **16384** is the value to deploy. **The evidence for that trigger rule is
     weak and is flagged as such**: it is fitted from two observations at the
     *same* `num_ctx`, and 16,384 is its first check at a second one, where it
     predicted zero shifts and zero were observed. That is consistent, not
     proven — so the `+1` there is a conservative choice, not a measured edge.
   - **The server's log confirms the shape of the cut, independently of the
     probe.** Every truncation line in `docker logs ollama` reads
     `limit=2050 prompt=... keep=4 new=2050`, and the slot line that follows
     says `n_keep = 4` — i.e. the first `numKeep = 4` tokens are kept, the tail
     is kept, the middle is dropped. That is the same rule the source and the
     bracket give, reported by the server about real requests.
   - **thinking is billed and then thrown away.** `_parse_response()`
     (`llms/ollama.py:43-90`) returns only `message.content` — `eval_count`,
     `prompt_eval_count` and qwen3's `thinking` are all discarded, so mem0's own
     interface cannot tell you what it spent. That is why this had to be
     measured by wrapping the client from outside.

   **Correction to an earlier draft of this item.** The truncation rule was
   already on record: **D-023 §5** has `num_ctx - max((num_ctx - numKeep)/2, 1)`
   when the token count exceeds `num_ctx - 1`, with `numKeep = 4` — read from
   the same source and cross-checked against the same `limit=2050` log line.
   D-023 §5 also named the measurement it was missing ("I have not actually set
   a second `num_ctx`"), which is exactly the bracket above; re-measuring it is
   what closes that gap. A draft in between used a different formula
   (`num_ctx - num_predict - 46`) that happened to match the one log line it
   was checked against, and "verified" it with an experiment in which both
   formulas predict the same number. See D-027 §4.

   **The third control group answered it (D-036).** `num_ctx` held at 16,384,
   `num_predict` 2,000 → 8,000, nothing else changed: the first `add()`
   extracted **2 memories**, where D-035's run at 2,000 extracted **0** — and
   `done_reason=stop` with `eval_count` 5,605 says it was not cut off. **The
   extraction does happen; the generation budget was the cause.** The rule that
   falls out is not a number: the model stopped on its own, and any fixed
   `max_tokens` below what the model actually needs truncates *silently*, in a
   way that reads as "there were no facts to extract" — which is exactly how
   mem0's own 2,000 was misread for three rounds.

   The second `add()` in that run also returned 0, and **it is a different 0**:
   `done_reason=stop`, `eval_count` 1,236, and its content had already been
   stored by the first call — so "nothing new to extract" is the *right*
   answer. On the older instrument the two zeros are indistinguishable, which
   is the entire reason `done_reason` was added **before** the run started
   (commit `d4a8bf8`, pushed before it began, so the numbers trace to that
   revision). Three more things came out of it:

   - **"A bigger budget costs more" is false.** add 1 went 2,367 → 6,076 s
     (×2.57) but add 2 went 2,430 → **1,361 s (×0.56, faster)** — it stopped at
     1,236 tokens instead of being cut at 2,000. You pay for the budget **used**,
     not the budget **allowed**, so raising the ceiling cost nothing here.
   - **One thing is observed and *not* explained, and is recorded as unknown.**
     At `num_predict=8000` ollama split each generation into **two slot tasks**,
     re-prefilling prompt + generated-so-far; `prompt_eval_duration` therefore
     no longer names what its name says. The totals still reconcile with the
     wall clock (5,527.7 + 531.7 + 7.5 = 6,066.9 s vs 6,075.8 s), so **the
     totals stand — but the prefill/generation split behind every cost claim
     since D-030 needs rechecking.**
   - **`grep 'shift'` is fooled by the flag itself.** All 7 hits in that log are
     llama-server's startup argument dump of `--context-shift`. The verdict
     rests on the **event** line (`slot context shift, …`, absent) and on
     `n_discard`/`n_left` (also absent, not merely uncounted) — not on a
     keyword count that a startup banner satisfies.

   **The context half of that pairing is deployed; the budget half is not.**
   `deploy-vps.sh` writes `OLLAMA_CONTEXT_LENGTH=16384` (D-035 §8), but mem0's
   `max_tokens` is a library-level setting that **nothing in this stack writes**
   — so a deployed instance still extracts with the 2,000 default, and the
   extraction still comes back empty. That is the next action, and it is not
   another magic number: raise it until the model stops on its own
   (`done_reason == "stop"`).

   **The false sentence this item's instrument was known to print was printed
   for real in that round** — 「兩次都被截斷到同一個上限，所以
   `prompt_eval_count` 看不出這個差別」 — on a line whose own text reports
   `prompt token=8167` against 8052 at `num_ctx=16384`, where nothing is
   truncated. D-035 §6 recorded it as found-but-unfixed; this round gave it an
   observed instance, and it is now a four-state verdict (`truncated` /
   `not_truncated` / `disagree` / `unknown`) with a revision guard on the probe,
   in `#58` (D-036 §11).

   **The re-baseline was attempted on 2026-09-21 and did not pass — no rate
   below is a baseline.** The VM was resized on 2026-09-20 from 2 vCPU / 3.8 GB
   to 4 vCPU / 16 GB, *after* D-024 was written (D-024's commit is 18:02, the
   reboot 18:15), so every rate on record up to that point describes a machine
   that no longer exists. `bash scripts/verify-throughput.sh` ran the full
   matrix on the new VM and exited **1**: two conditions repeated three times
   each disagreed with *themselves* by 16.0% and 26.7%, over the 15% ceiling.
   At that noise level "no difference" and "no difference measurable" look
   identical, so the round is recorded as a failure, not a slow success.

   What the round did establish, and what it did **not** (D-031):

   - The only arm whose conditions match D-014 is `qwen2.5:3b` at
     `num_predict=1024`, and its generation hit the cap in 3/3 samples
     (`matched`), so the comparison is at least well-posed: **6.59 t/s**,
     inside the old VM's 6.38–6.85. Its own spread is 15.7% — the instrument's
     noise is larger than the effect being looked for.
   - `qwen3:4b` measured 5.74–5.87 t/s against D-014's 3.78–3.97, but **the two
     do not compare**: D-014 never recorded `num_ctx`, and item 3 above shows
     `num_ctx` decides whether the prompt is truncated — i.e. which condition
     the rate was measured under.
   - **No CPU-scaling claim.** There is no "4 vCPU made it 48% faster" here, and
     nothing was measured about memory bandwidth. The most direct surviving
     explanation for "not obviously faster" is CPU topology (the guest reports
     its 4 vCPUs as 4 single-core sockets on a hybrid P/E host) — and that has
     not been tested either.

   The round also produced the instrument: `scripts/throughput_probe.py`
   (pure verdict functions + measurement), its 247 offline assertions, a
   71-mutation harness, and `verify-throughput.sh`. Eight defects were found and
   fixed in it; the ones worth knowing about are in the Evidence table below.

   **The criterion was then changed (D-032): seven samples, judged by CV.**
   The next round takes seven repeats per condition instead of three and judges
   their scatter by the coefficient of variation (sample stdev / median,
   `<= 8.9%`) instead of the range. This is a **correction of an estimator, not
   a relaxation of the standard**: the range is a function of sample size
   (`E[range] = d₂(n)·σ`, `d₂(3) = 1.6926`, `d₂(7) = 2.7044`), so keeping the
   old 15%-of-range rule at seven samples would have meant an effective
   `CV ≤ 5.55%` — a bar **59% stricter** than the one D-031 failed against, an
   artifact of counting rather than of the machine. `8.9%` is the old threshold
   translated (`15 / 1.6926 = 8.86`). Individual datasets can move either way,
   and **this does not promise the next round passes** — if seven samples still
   scatter, the machine really is that noisy, and that is the result.

4. **[measured] `recursion_limit=5` is too tight — and the guess about *why* was
   right.** Measured 2026-09-21 (D-030) via `bash scripts/verify-phase3-runtime.sh`.
   The rule is `required limit = super-steps + 1`, confirmed on nine topologies
   (linear 1/3/5 and fan-out widths 1/2/4 × rounds 1/2), each binary-searched for
   its minimum working limit. **The limit counts super-steps, not nodes** — a
   width-4 fan-out has six nodes but three super-steps, and sweeping the width
   1→6 leaves the minimum limit at `[4, 4, 4, 4, 4]`, unchanged. That is the
   mechanical evidence; the default is **10007** (read from langgraph's source),
   so `5` is a deliberate narrowing.

   **The +1 is spent finishing.** At `limit = node count` every node runs, `next`
   is empty — the graph has nowhere left to go — and it *still* raises
   `GraphRecursionError`. The guard fires only once all the work is done. That
   also means the exception cannot distinguish "the plan was fine but the guard
   was tight" from "the graph really did loop" — and D-024 measured that one more
   step on this path costs one more LLM round trip (7.4 s there). Whether the
   interrupted run is recoverable then depends entirely on the checkpointer:
   with one, the abort lands on a clean node boundary and the same `thread_id`
   resumes with a larger limit; without one, `get_state` answers
   `ValueError: No checkpointer set` and the work is gone.

   `5` allows four super-steps, so a straight Plan → Execute → Check (3) fits and
   the first retry does not. **That last sentence is a projection**, not a
   measurement — the Plan-and-Execute graph does not exist yet, so its step count
   is derived from a rule that *was* measured. What is measured supports "5 is too
   tight", not "it runs exactly three steps". So set the guard **per graph** and
   read back what the graph actually used:

   ```python
   app.get_state(cfg).metadata["step"]      # langgraph's own counter, not yours
   ```

   (`metadata["step"]` needs a checkpointer and a `thread_id`. Do not count node
   executions — on a fan-out that counts a different thing and reads as a failure.)

5. **[refuted] There is no second vector store to operate.** The premise was
   "two stores to back up, migrate and keep consistent". Measured 2026-09-21
   (D-030): mem0's ChromaDB lives at `Path(workdir) / "chroma"` where `workdir`
   defaults to `tempfile.mkdtemp()` (`mem0_add_cost_probe.py:1931,2048`), and the
   container is `docker run --rm`. **It has never been a volume.** Nothing to back
   up, nothing to migrate, nothing that can disagree with anything. The question
   this item poses has therefore changed: not *how do we operate two stores* but
   **whether to persist it, and where**.

   That distinction is not pedantry. "Two stores to back up" sends the next person
   to plan backup and consistency work — all of which is wasted on a directory
   that disappears when the run ends. This is the highest-value entry in the
   mutation harness for exactly that reason.

   The store that *does* exist is small: Open WebUI's own `vector_db` measures
   **7 MB** inside a 2.2 GB volume. **Disk is genuinely the tight resource**
   (81% of 97 GB) — but the pressure is elsewhere: **16.8 GB of reclaimable
   images**, of which the vector store is 1/2400th. The ordering was backwards,
   so the verdict has three states rather than two: `ok` / `reclaim-first`
   (doesn't fit, but more is reclaimable than needs adding → clean, then add) /
   `blocked`. The middle state is the finding; without it, "81% full" and "full"
   read the same.

6. **[measured] `APScheduler` in-process loses its jobs on restart — and so does
   a persistent store, silently, under a default nobody sets.** Measured
   2026-09-21 (D-030). The README's claim holds: after a real interpreter restart,
   `MemoryJobStore` holds **0** jobs and `SQLAlchemyJobStore` holds **1**.

   **Persistence is necessary but not sufficient.** A job that comes due during
   downtime is dropped as a *misfire* once it is later than `misfire_grace_time`,
   whose default is **1 second**. With lateness fixed at ~2.2 s and only the grace
   varied: default → dropped, 1 → dropped, 2 → dropped, 3 → ran, 10 → ran. An
   independent downtime sweep agrees (0.3 s / 0.8 s ran; 1.5 s / 3.0 s dropped).
   So the boundary is *lateness > grace*, not some fixed duration.

   **What makes it dangerous is that it is indistinguishable from success.** A
   `date` trigger removes itself after firing, so "ran and was removed" and
   "dropped as a misfire" look identical in the store. No inspection of the job
   store can tell them apart — only an external record can.

   Two more costs, both measured. **The swap is not drop-in:** `MemoryJobStore`
   accepts a lambda, `SQLAlchemyJobStore` raises `ValueError: This Job cannot be
   serialized since the reference to its callable … could not be determined` — so
   every scheduled callable must be module-level and picklable, which is a
   constraint on how the code is organised, decided *before* committing to
   persistence. And `run_date` must be a `datetime`, not a float timestamp
   (`TypeError: Unsupported type for run_date: float`).

---

## Codespaces gotchas

Verified issues that are easy to lose hours to:

| Gotcha | Detail |
|---|---|
| **No GPU, ever** | The Codespaces GPU machine type was **deprecated 2025-08-29**. All inference is CPU-only. |
| **Slow inference** | A 3B–4B Q4 model on shared vCPUs runs at roughly single-digit tokens/sec — measured on both the old 2-vCPU box and the current 4-vCPU one (D-014, D-031, D-033). Open WebUI's default 300-second HTTP timeout can be hit by long completions. |
| **Model pulls need almost no extra disk — now measured** | A download needs room for what is arriving, so a nearly-full volume can still fail a pull — sometimes silently. **Measured 2026-09-26:** pulling `qwen3:8b` (5,225,422,848 B) into an isolated volume, sampling once a second — 1184 samples over 1183 s — the peak *allocated* bytes never exceeded the model's final size (instantaneous excess **0 B**; multiplier **1.000** by two independent estimators). The reason: the blob **is** the artifact, and Ollama preallocates it sparsely, so there is never a compressed copy sitting beside an extracted one. The "~2×" this row used to state was not merely unsourced — it was **wrong**, and the constant is gone. A re-run whose model is already present needs nothing extra either, and the deploy gate asks the machine rather than assuming a download is happening. (D-058; `docs/evidence/2026-09-26-pull-peak-measurement.txt`) |
| **Storage may run out first** | Sources disagree on whether storage counts disk *used* or the full 32GB volume allocation. If it is the allocation, one codespace alive for a month is 32 GB-month against a 15 GB-month allowance and runs out in ~2 weeks, before compute does. Watch the billing page. |
| **`localhost` is not the host** | From inside a devcontainer, reach a host service via `host.docker.internal` (add `--add-host=host.docker.internal:host-gateway` on Linux), or — as this repo does — make Ollama a compose service reachable at `http://ollama:11434`. |
| **No Ollama devcontainer feature exists** | There is no `ghcr.io/devcontainers/features/ollama`. Every approach installs it via `onCreateCommand`, uses it as a compose service, or points at a host instance. This repo uses the compose-service approach. |

---

## Troubleshooting

### Startup fails with `WEBUI_SECRET_KEY 未設定`

This is deliberate, not a bug. `bash scripts/up.sh` generates the key
automatically; or `cp .env.example .env` and fill in any 32-byte hex string.

**Do not** leave it empty to work around this — it causes Phase 2's MCP tools to
fail with `Error decrypting tokens` after every container rebuild.

### Model download interrupted

```bash
bash scripts/pull-model.sh
```

Ollama supports resuming partial downloads — **this project has not measured that
claim** (protocol D of D-058 was not run; see the no-test statement in that entry).
What *is* measured is the disk side of it: a pull peaks at the model's final size and
nothing more, and a re-run whose model is already present needs nothing extra at all.
The deploy gate tells those two cases apart instead of assuming a download is
happening (D-058).

### Responses are very slow

Expected on a 2-core machine. Try:

- A smaller model: set `OLLAMA_MODEL=qwen3:1.7b` in `.env`, then `bash scripts/up.sh`
- Shorten `OLLAMA_KEEP_ALIVE` (at the cost of reloading the model each conversation)

### Containers keep restarting / killed by the OOM killer

Out of memory. In order:

1. Confirm `OLLAMA_MODEL` in `.env` is not `qwen3:8b` or larger
2. Use `docker stats` to find which container is consuming memory
3. Shorten `OLLAMA_KEEP_ALIVE` so the model is released sooner

### Can't find the port 3000 URL

In Codespaces, ports are **private by default** and only appear in the **PORTS**
panel once there is traffic. If it's missing, add it manually:

1. **PORTS** panel → **Add Port** → enter `3000`
2. Right-click the port → **Port Visibility** → choose as needed
   (leaving it private is fine — only you can reach it)

---

## A note on model naming

The original plan specified "Qwen 3B/7B". Those sizes do not exist in **Qwen3**:

| Family | Available sizes |
|---|---|
| **Qwen3** | 0.6b · 1.7b · 4b · 8b · 14b · 30b · 32b · 235b |
| **Qwen2.5** | 0.5b · 1.5b · **3b** · **7b** · 14b · 32b · 72b |

Qwen2.5 **does** have 3B and 7B — so if that is what was meant, `qwen2.5:3b`
(1.9 GB) is a reasonable alternative to `qwen3:4b`. This repo defaults to
Qwen3 because it is newer and fully Apache-2.0 (Qwen2.5's 3B and 72B use the
Qwen license).

---

## License

This project's own code — the compose file, scripts, and documentation — is
**[MIT licensed](LICENSE)**.

**This does not cover the container images it pulls.** Each dependency carries
its own license:

| Component | License | Note |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) | MIT | |
| [Open WebUI](https://github.com/open-webui/open-webui) | [Open WebUI License](https://github.com/open-webui/open-webui/blob/main/LICENSE) | ⚠️ **Not MIT.** BSD-3-style **plus a branding clause** — see below. |
| [mcpo](https://github.com/open-webui/mcpo) | MIT | Phase 2 |
| [LangGraph](https://github.com/langchain-ai/langgraph) library | MIT | The `langgraph-server` *container* is Elastic License 2.0 — see [D-007](DECISIONS.md) |
| Qwen3 models | Apache-2.0 | Qwen2.5's 3B and 72B use the Qwen license instead |

### The Open WebUI branding clause

Condition 4 of the Open WebUI License prohibits altering, removing, obscuring,
or replacing any "Open WebUI" branding — name, logo, or visual identifiers —
**except** where:

1. the deployment has **no more than 50 end users** in any rolling 30-day
   period, or
2. prior written permission has been obtained, or
3. (further conditions stated in the license)

**This is irrelevant to a personal POC** — one user, and this project does not
modify the interface at all. It becomes relevant only if you later build a
product on top of Open WebUI serving more than 50 people. Worth knowing before
you get attached to the idea of rebranding it.

Note that merely referencing an image in a compose file does not make the
image's license apply to this repository — the MIT license above covers the
code in *this* repo.
