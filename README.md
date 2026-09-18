# self-cloud-agent-lab

**English** | [繁體中文](README.zh-TW.md)

A proof of concept validating whether a self-hosted AI platform can run **inside free cloud quotas**:
running your own LLM, reading your own data, using tools over MCP, and letting an agent work autonomously.

> **This project is a validation sandbox, not a persistent service.**
> The reason is below and it is the single most important thing to understand before you start.

---

## Table of contents

- [Why this is not a persistent service](#why-this-is-not-a-persistent-service)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Verification checklist](#verification-checklist)
- [Quota management](#quota-management)
- [Phase 2](#phase-2)
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

---

## Quick start

### On Codespaces

1. Open this repo on GitHub → **Code** → **Codespaces** → **Create codespace on main**
2. Select the **2-core / 8GB** machine (**do not pick 4-core — it quadruples your quota burn**)
3. Wait for creation — `postCreateCommand` runs `scripts/up.sh` automatically,
   including the model download (a few minutes on first run)
4. Open the **PORTS** panel → click the URL for port **3000**
5. Register the first account (**it automatically becomes the administrator**)
6. Once logged in, set `ENABLE_SIGNUP=false` in `.env`, then:
   ```bash
   docker compose up -d open-webui
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
| `bash scripts/verify.sh` | Phase-2 prerequisite check: speed, tool calling, thinking, Chinese output (~10 min) |
| `bash scripts/verify.sh 2` | Same, but only test 2 (disable thinking, ~1 min) |

---

## Verification checklist

This project has **no automated tests**. It is a feasibility POC, and its core
behaviour — inference quality, MCP tool-calling reliability — is inherently a
human judgement call. The steps below are performed manually.

### Phase 1: base stack

- [ ] **Containers healthy** — `bash scripts/status.sh` shows both containers
      `running` and `open-webui` as `healthy`
- [ ] **Model ready** — `qwen3:4b` appears in the model list
- [ ] **Inference works** — ask "explain MCP in three sentences" in Open WebUI;
      a sensible answer arrives within 10–30 seconds
- [ ] **Streaming works** — the answer appears token by token, not in one block
      after a pause
- [ ] **Memory holds** — during a conversation, the `Mem` line in `status.sh`
      still shows headroom (not fully consumed by swap)
- [ ] **Model unloads** — after idling past `OLLAMA_KEEP_ALIVE`, `docker stats`
      shows the ollama container's memory drop noticeably
- [ ] **Survives restart** — after `bash scripts/down.sh && bash scripts/up.sh`,
      the model is **not** re-downloaded and prior conversations are still there

### Phase 2: RAG

- [ ] Create a knowledge base under **Workspace → Knowledge**
- [ ] Upload a PDF or Markdown document
- [ ] Ask a question about it and confirm the answer cites the document rather
      than being generated from nothing
- [ ] **Negative test:** ask about a detail that is *not* in the document and
      confirm the model says it doesn't know instead of inventing an answer

> **Note for non-English documents:** Open WebUI's default embedding model is
> `sentence-transformers/all-MiniLM-L6-v2` — English-only, 384 dimensions,
> ~500MB RAM. For Chinese or other non-English documents, retrieval quality will
> be poor unless you switch to a multilingual embedding model via
> `RAG_EMBEDDING_ENGINE=ollama` and `RAG_EMBEDDING_MODEL=nomic-embed-text`.
> **Changing the embedding model later requires re-embedding every document**,
> so decide before you upload. Note also that a larger embedding model has been
> reported to spike RAM from 2GB to 14GB — on a small VM, budget for it.

### Phase 2: MCP

- [ ] Add an MCP server under **Admin Settings → External Tools**
      (Type must be **MCP (Streamable HTTP)** — not OpenAPI)
- [ ] Confirm the tool appears in the conversation's tool list with a
      `server:mcp:` prefix
- [ ] Trigger a tool call and confirm the model **decides on its own** to use
      the tool rather than being explicitly told to
- [ ] **Multi-turn test:** a task requiring two or more sequential tool calls —
      confirm the 4B model can hold the flow together

> **The critical unknown for this phase:** whether a 4B-class model can perform
> reliable multi-turn tool calling. If the failure rate is too high, either move
> to `qwen3:8b` and accept the memory pressure, or use a larger machine.

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

**LangGraph is therefore not introduced for now** — it is evaluated only when a
need appears for custom multi-step workflows or an explicit state machine.
See [`DECISIONS.md`](DECISIONS.md) D-005 for the full trade-off.

> **Licensing warning, verified:** the `langgraph-server` / `langgraph-api`
> production container image is **Elastic License 2.0**. Self-hosting it in
> production requires a license key (`LANGSMITH_API_KEY` on Plus or higher, or
> `LANGGRAPH_CLOUD_LICENSE_KEY`); without one it raises `INVALID_LICENSE` at
> startup. The `langgraph` **library** is MIT and free — use it inside your own
> service, or use `langgraph dev`, and avoid the commercial server image.

### Enabling MCP

`ENABLE_MCP=true` is already set. Then:

1. **Admin Settings → External Tools** → **+**
2. Set Type to **MCP (Streamable HTTP)**
3. Enter the server URL and authentication
4. Save

**Known constraints:**

- **Streamable HTTP only** — no stdio, no SSE. This is deliberate
  (browser and multi-tenant security).
- **MCP servers are admin-only.** Non-admins can only add OpenAPI servers.
- **stdio-based servers** (the kind Claude Desktop uses) need the
  [**mcpo**](https://github.com/open-webui/mcpo) proxy to bridge them to OpenAPI.
- OAuth 2.1 tools **cannot** be set as model defaults — they need an interactive
  redirect.

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
