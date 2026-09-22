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
- [Architecture](#architecture)
- [Quick start](#quick-start)
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
bash scripts/deploy-vps.sh --model qwen3:4b --num-ctx 8192
```

That is the whole deploy. It closes the port, pulls the models, starts the stack,
verifies the stack generates, and confirms the context size actually took effect.
The sections below are what it does and why — read them if you want to do it by
hand, or if something in the script needs to change.

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
| `docker-compose.yml` | Two containers and a bridge network. Nothing Codespaces-specific. |
| Open WebUI state | Chats, Knowledge, MCP connections, users, settings all live in the `open_webui_storage` volume. Copy the volume, keep the data. |
| Model choice | `OLLAMA_MODEL` in `.env` is where the chat model is named, and `EMBEDDING_MODEL` is where the embedding model is named — those two are the only places. Scripts and compose read those variables. Swapping to a larger model is a one-line change. |
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
`docker port`, not what the compose file claims, and it tells the difference
between "exposed to the internet" and "reachable on your LAN only" by looking for
public addresses on this host. Note that the LAN-only case is **held back by your
router, not by this stack** — the same config becomes a real exposure the moment it
lands on a public IP.

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

Four values in `.env` were tuned for 2 cores and 8GB. They are the first things to
raise:

```bash
OLLAMA_MODEL=qwen3:70b          # or whatever the VPS can hold
OLLAMA_CONTEXT_LENGTH=8192      # see below — the default 4096 truncates memory extraction
OLLAMA_MAX_LOADED_MODELS=3
OLLAMA_NUM_PARALLEL=4           # big throughput win; shares one model load
OLLAMA_KEEP_ALIVE=-1            # keep resident; reloading costs tens of seconds
```

Unlike the other three, `OLLAMA_CONTEXT_LENGTH` is **not** free — a larger context
costs KV-cache memory proportional to it (roughly 36 KiB per token for a 3B-class
model; that figure is an **estimate** from Qwen2.5-3B's architecture, not measured
on this stack, and it scales with the model's layer/head count). The reason the
default is worth raising anyway: **memory extraction silently truncates.** A
`mem0` `add()` call sends an extraction prompt measured at 8,052 and 8,100 tokens,
and ollama's 4096 default cuts it to 2,050 — dropping the instructions at the end
of the prompt, so extraction returns zero facts with no error. The operating rule
is `num_ctx >= prompt_tokens + 1`, so anything ≥ 8,101 preserves that prompt
(D-027). `deploy-vps.sh` explains this at the point it matters and defaults
`--num-ctx` to 8192.

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
| `bash scripts/deploy-vps.sh --model M --num-ctx N` | **One-click deploy onto a fresh VPS.** Writes `.env` safely *before* loading it, starts the stack, gates on real port bindings, pulls both models, then proves the stack generates and that `num_ctx` took effect. Fresh installs only — refuses if the database already has accounts or chats |
| `bash scripts/deploy-vps.sh --dry-run` | Same preconditions and the `.env` diff, writing nothing |
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

Attaching a GPU runtime (Kaggle + Endpoint, or a VPS + vLLM) has its own guide:
[`docs/ENDPOINT.md`](docs/ENDPOINT.md) · [`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md).
| `bash scripts/lock-signup.sh` | Verify signup is really off, and close it via the config API if it is open |
| `bash scripts/lock-signup.sh --check` | Verify only — no changes. Exits non-zero if signup is open |

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
| `bash scripts/verify-mem0-add-cost.sh` | That mem0's `add()` costs exactly one extra LLM call, and that at the default `num_ctx` that call never sees its own instructions (D-027) |
| `bash scripts/test_mem0_add_cost_probe_mutants.sh` | That the grader above is actually exercised — 78 mutations of its own criteria, every one must be caught |
| `bash scripts/test_ollama_log_corroboration.sh` | That the server-side log corroboration reports "the instrument is broken" and "no truncation this run" as two different sentences |
| `bash scripts/deploy-vps.sh --dry-run` | What a deploy would write to `.env` and whether the machine has the disk/RAM — changes nothing |
| `bash scripts/test_deploy_vps_decisions.sh` | That the bind-address policy, the exposure gate's four states, the resource thresholds and the `.env` writer each have a "should pass" and a "should block" case |
| `bash scripts/test_deploy_vps_decisions_mutants.sh` | That the grader above is actually exercised — 62 mutations across the three bash modules and the smoke probe, every one must be caught |
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

- [ ] **Containers healthy** — `bash scripts/status.sh` shows both containers
      `running` and `open-webui` as `healthy`
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

3. **[verified] mem0 does cost one extra LLM call — and at the default
   `num_ctx` that call never sees its own instructions.** Measured 2026-09-20
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
| **Pulls need ~2× disk** | Model downloads need space for the compressed *and* extracted forms at the same time, so a nearly-full volume fails pulls — sometimes silently. |
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

Ollama supports resuming partial downloads.

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
