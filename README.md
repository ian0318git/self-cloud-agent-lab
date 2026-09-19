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
- [Phase 3 and 4 — planned, not yet measured](#phase-3-and-4--planned-not-yet-measured)
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

### What carries over unchanged

| Thing | Why it survives |
|---|---|
| `docker-compose.yml` | Two containers and a bridge network. Nothing Codespaces-specific. |
| Open WebUI state | Chats, Knowledge, MCP connections, users, settings all live in the `open_webui_storage` volume. Copy the volume, keep the data. |
| Model choice | `OLLAMA_MODEL` in `.env` is the **only** place a model is named. Scripts and compose read that variable. Swapping to a larger model is a one-line change. |
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

Also keep 11434 unpublished (D-003). Ollama has no authentication at all; the
script checks this too.

### Step 2: enlarge the model and the limits

Three values in `.env` were tuned for 2 cores and 8GB. They are the first things to
raise, and all three are already configurable — no compose edits needed:

```bash
OLLAMA_MODEL=qwen3:70b          # or whatever the VPS can hold
OLLAMA_MAX_LOADED_MODELS=3
OLLAMA_NUM_PARALLEL=4           # big throughput win; shares one model load
OLLAMA_KEEP_ALIVE=-1            # keep resident; reloading costs tens of seconds
```

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
already running — `up.sh` and `down.sh` pass `--remove-orphans` so the stale
`cloudflared` is actually removed. Without that, "I turned the tunnel off" would
be a false belief while the service stayed reachable.

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
`scripts/test_runtime_state.py` and `scripts/test_rag_grounding_probe.py` —
offline unit tests: five for the probes, one for the runtime-config reader,
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
machine by definition; see [Phase 3 and 4](#phase-3-and-4--planned-not-yet-measured)
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

## Phase 3 and 4 — planned, not yet measured

> **Status: a plan, not a result.** Nothing on this page has been run. The
> section exists so the design is written down *before* it is built, and so the
> assumptions it rests on are visible enough to be tested. Every claim below is
> marked **[verified]** (checked against docs/source on 2026-09-19) or **[open]**
> (measured by nothing yet).

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
| Loop guard | `recursion_limit=5` | see **[open]** item 4 |
| Memory | `mem0` + ChromaDB | **[open]** items 2 and 3 |
| Scheduler | `APScheduler`, in-process | **[open]** item 6 |
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

Each of these is a measurement this project has not made. They are listed in
the order they should be settled, because later ones are wasted effort if an
earlier one fails.

1. **[open] Does tool calling survive the change of code path?** D-011 proved
   multi-turn tool calling through **Open WebUI's** pipeline. LangGraph binds
   tools via `bind_tools` and the Ollama integration's native function calling —
   a different implementation. **Re-measure before building anything on it**;
   see the Phase 2 MCP note above and D-014.

2. **[open] Chroma will reject this project's embeddings by default.** mem0's
   Chroma backend defaults to **1536 dimensions** (OpenAI-sized). This project's
   embedding model is `qwen3-embedding:0.6b`, measured at **1024 dimensions**
   (D-013). Left at defaults this fails at write time with a shape mismatch.
   `embedding_dims` and the collection's dimensions must both be pinned to 1024
   — and the embedding model must not be changed later without re-embedding,
   for the same reason noted in Phase 2 above.

3. **[open] mem0 costs one extra LLM call per conversation, on 2 vCPU.**
   mem0's write path runs an LLM to extract facts before storing them. That call
   is *in addition to* generating the answer. At the rates measured here
   (3.8–6.9 tok/s, D-011/D-014) this may be the single largest cost in Phase 4 —
   large enough that it should be timed on its own before memory is designed in,
   not discovered after.

4. **[open] `recursion_limit=5` is likely too tight for Plan-and-Execute.**
   The limit counts graph super-steps, and Plan → Execute → Check → Retry can
   exceed five by itself once a retry occurs. Set the guard deliberately per
   graph rather than inheriting one global value; a limit that trips during
   normal operation gets raised until it stops meaning anything.

5. **[open] A second vector store on 8GB.** Open WebUI already maintains its own
   store for Knowledge; ChromaDB would be a second. The 8GB budget in D-001
   (OS 1.0 + Open WebUI 1.0 + model 3.0 ≈ 5.0GB) never included it.

6. **[open] `APScheduler` in-process loses its jobs on restart.** In-process
   avoids a Redis container, which is the point — but without a persistent job
   store, every container restart silently drops the schedule. Decide whether
   the schedules must survive restarts before relying on them.

---

## Codespaces gotchas

Verified issues that are easy to lose hours to:

| Gotcha | Detail |
|---|---|
| **No GPU, ever** | The Codespaces GPU machine type was **deprecated 2025-08-29**. All inference is CPU-only. |
| **Slow inference** | A 3B–4B Q4 model on 2 shared vCPUs runs at roughly single-digit tokens/sec. Open WebUI's default 300-second HTTP timeout can be hit by long completions. |
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
