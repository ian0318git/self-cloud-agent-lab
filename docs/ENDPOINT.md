# Connecting a GPU runtime (Kaggle + Endpoint)

This is the guide for attaching a **second LLM runtime** to the platform — a
bigger model on somebody else's GPU — without changing a single line of the
upper layers.

繁體中文版：[`ENDPOINT.zh-TW.md`](ENDPOINT.zh-TW.md)

---

## Why this exists

The architecture only pays off if this sentence is true:

> The upper layers speak only the OpenAI protocol; they do not know which
> runtime is underneath.

That sentence is worth nothing until it is mechanically checked, so it is
checked — by `scripts/probe-openai.py`, which speaks *only* the OpenAI protocol
and uses no vendor SDK. Any runtime that passes it can take over the platform.
Any runtime that needs upper-layer code changes cannot.

**It is already verified for one runtime.** Ollama serves an OpenAI-compatible
API at `/v1`, so the entire path is exercised today without a Kaggle account:

```
$ bash scripts/probe-openai.sh
  ✓ GET /v1/models               通過  4 個模型
  ✓ POST /v1/chat/completions    通過  18.3s，回應 好
  ✓ 串流（SSE）                   通過  3 個 chunk，收到 [DONE]
  結論：通過 —— 只換 --base-url，不改任何上層程式碼。
```

Kaggle is not special. It is one more `--base-url`.

## What is **not** verified

Being explicit about this, because the whole project depends on not confusing
"the interface works" with "it works":

| Claim | Status |
|---|---|
| A runtime passing the probe can take over the upper layers | **Verified** (against Ollama `/v1`) |
| Kaggle + Endpoint can run a larger GGUF model | **Not verified** — needs your Kaggle account |
| Speed, VRAM headroom, which model sizes fit on 2×T4 | **Not measured** |
| That the tunnel stays up for a whole session | **Not measured** |

Interface conformance is not the same as capacity. The probe deliberately does
not claim otherwise — its own output says so.

## Terms of use: Kaggle is a measurement tool, not a product environment

**This section did not exist before, and it is the one that decides whether
this path can be handed to a company.**

Kaggle's [Terms of Use](https://www.kaggle.com/terms) restrict the service to
**personal, non-commercial** use. The wording, as quoted in Kaggle's own Q&A:

> You will only use the Services for your own internal, personal,
> non-commercial use, and not on behalf of or for the benefit of any
> third party.

This project's long-term goal is to let small companies run internal
assistants — that is commercial use, and the two do not fit together. So keep
the two purposes apart:

| Purpose | Is Kaggle the right fit? |
|---|---|
| Verifying "a larger model actually runs", measuring speed and VRAM | **Yes** — this is exactly what this document is for |
| The product path: the runtime for a company's internal assistant | **No** — the terms restrict it to personal, non-commercial use |

The product path ends at a VPS + GPU + vLLM (see the last section). **Kaggle is
one measurement on the way there, not part of that path.**

**How far this claim goes** (this project's rule: a claim carries its evidence,
and its boundaries):

- The quote above comes from **Kaggle's own Q&A quoting the Terms**, consulted
  2026-09-19.
- **The Terms text itself was not read directly.** `kaggle.com/terms` is a
  JS-rendered page — fetching it returned only the title, no clauses. So this
  is a **second-hand quote**, not a check against the source.
- **The Acceptable Use Policy text (`kaggle.com/aup`) was likewise not
  retrieved**, so nothing here should be read as stating what it says about
  running a notebook as a server.
- **This is not legal advice.** Before using it commercially, read the terms
  yourself, or ask whoever handles that for you.

---

## Prerequisites

- A **phone-verified** Kaggle account (required for API and GPU/internet access)
- Python 3.12+ on the machine where you run the CLI
- The `endpoint` CLI:

```bash
uv tool install endpoint-vps   # then run: endpoint boot
# The package is `endpoint-vps`; the executable is `endpoint`.
# `uvx endpoint-vps` fails with "not provided by package".
# One-off without installing: uvx --from endpoint-vps endpoint boot
# or: pip install endpoint-vps / pipx install endpoint-vps
```

## Step 1 — Kaggle token

Generate a **new** token at <https://www.kaggle.com/settings> — it begins with
`kgat_`. The older username/key pair is no longer accepted.

```bash
export KAGGLE_API_TOKEN='kgat_xxxxxxxxxxxxxxxx'
```

Put that line in your shell rc file so it survives, or write
`~/.kaggle/kaggle.json`.

> **This token is a credential.** It is not project-specific and must never be
> committed. This repo's `.gitignore` covers `.env`; it cannot cover your shell
> rc file. Keep it out of anything you paste into an issue.

Then run the wizard:

```bash
endpoint init
```

It writes `~/.config/endpoint/endpoint-config.yaml` and asks for your Kaggle
username, kernel slug, and default model.

## Step 2 — Boot it

```bash
endpoint -g boot        # T4 x2
```

Other targets: `endpoint boot` (CPU), `endpoint -t boot` (TPU v5e-8),
`endpoint boot --p100`.

**Both `init` and `boot` are interactive.** `boot`'s argparse takes only
`--no-watch`, `--p100` and `--community` — there is **no `--model`**; the model
is chosen at a prompt. Neither step can be wired into a scheduler, a CI job, or
any other non-interactive tool. `--no-watch` means "don't stream status" — it
does not mean "don't prompt".

Useful afterwards:

| Command | What it does |
|---|---|
| `endpoint status` | Kernel state, tunnel URL, deployed models |
| `endpoint base-url` | Just the URL, with ready-to-use curl examples |
| `endpoint doctor` | System diagnostics — check the config was actually read |
| `endpoint models` / `upload` / `settings` | List or upload models; view or change engine parameters |
| `endpoint logs` / `watch` | Engine logs (SSE), or the status-signal stream |
| `endpoint stop` | Stop this instance |
| `endpoint kill-all` | **Terminate every running Kaggle kernel on the account** |

> `kill-all` is account-wide, not per-instance. That is what makes it useful —
> say a notebook you opened by hand is quietly burning GPU quota — and also what
> makes it a shotgun rather than a rifle.

**Do not hard-code the quota or the hardware into anything.** Kaggle changes
these (P100 was retired 2026-09-15), and they differ per account. Whatever
`endpoint` reports is the truth.

## Step 3 — Get the URL and the key

```bash
endpoint base-url
```

You want two things: a `https://xxxx.trycloudflare.com` URL ending in `/v1`,
and the API key. The CLI auto-sets `ENDPOINT_API_KEY`; the key can also be
fetched with `GET /v1/apikey`.

Put the key where the repo's scripts expect it — **`.env`, which is gitignored**:

```bash
ENDPOINT_API_KEY=<paste here>
```

## Step 4 — Verify **before** connecting

```bash
bash scripts/connect-endpoint.sh --url https://xxxx.trycloudflare.com/v1 --check
```

`--check` runs the conformance probe and **changes nothing**. This ordering is
the whole point: if you wire first and it fails, you have already pointed the
platform's only model source at a broken service — and Open WebUI will not
complain, it will just show an empty model list.

## Step 5 — Connect

```bash
bash scripts/connect-endpoint.sh --url https://xxxx.trycloudflare.com/v1
```

The script:

1. runs the conformance probe and **refuses to continue if it fails**
2. writes `openai.enable` / `openai.api_base_urls` / `openai.api_keys` **into the
   database** — not into `.env`, which is inert after first boot (D-017)
3. restarts Open WebUI
4. reads the settings back **through the application's own `Config.get_many`
   path** — the same call `get_all_models` uses — rather than re-reading the row
   it just wrote, which would only be proving itself

Companion commands:

```bash
bash scripts/connect-endpoint.sh --status       # what is wired right now
bash scripts/connect-endpoint.sh --disconnect   # back to Ollama only
```

## Step 6 — The step only you can do

**Open <http://localhost:3000> and look at the model picker.**

This is not optional, and the scripts cannot do it for you. Open WebUI fetches
the model list from configured endpoints **lazily** — only when an
authenticated user opens the model list. Verified on 2026-09-19 with a canary
server: after a restart with `openai.enable=true` pointing at the canary,
*no request arrived* until something asked for the model list.

So the scripts can prove "the application reads the configuration that points
at your runtime". They cannot prove "the models were fetched". Only you can,
by looking.

If your runtime's models do not appear, the endpoint is reachable but the
fetch is failing — check `docker compose logs --tail=50 open-webui`.

---

## Security: the tunnel is the weak part

Everything above is about capability. This section is about what you are
accepting.

`endpoint` exposes the engine through a **cloudflared Quick Tunnel**
(`*.trycloudflare.com`). That is not a named tunnel and **it has no Cloudflare
Access policy**. Whatever protection exists is the API key alone.

This is in direct tension with this project's own rule for inbound access:
[D-012](../DECISIONS.md) says external access goes through Cloudflare Tunnel
**+ Access**, never a bare public endpoint. Ollama is deliberately never
exposed ([D-003](../DECISIONS.md)). The GPU runtime is the opposite: it *is*
public, by construction.

Concretely, that means:

- **Anyone with the tunnel URL and the key can use your GPU.** The URL is
  random, which is obscurity, not authentication.
- **Quick Tunnel URLs change on every boot.** Do not build anything on a fixed
  URL.
- **Check whether `GET /v1/apikey` is reachable without a token.** The endpoint
  docs mention this route. If it is unauthenticated, the tunnel URL alone is
  enough to obtain the key — verify this yourself with `curl` once it is up.
  This project has not verified it.
- Kaggle notebooks have a **maximum session length** (about 12 hours at time of
  writing) and a weekly GPU quota. The runtime will disappear. Design for that:
  treat it as an accelerator that comes and goes, not as the only model source.
- The `endpoint` project offers an optional Cloudflare Worker proxy with
  per-IP rate limiting and hashed keys. That is a real improvement over the
  bare tunnel. Consider it before putting anything sensitive through.

**Practical rule:** while you are in the POC phase and have no private data on
the platform, a Quick Tunnel is an acceptable trade. Before you upload company
documents, either move the GPU in-house (VPS + GPU + vLLM, below) or put a
named tunnel with Access in front of it.

---

## What the probe does not measure

The probe answers "will the upper layers work against this?" It does **not**
answer "is this fast enough / big enough". Measure those yourself:

| Question | How |
|---|---|
| Tokens per second | Time a real generation, not a one-word reply |
| Does a bigger GGUF fit? | Try it — 2×T4 is 16 GB VRAM each, and tensor split changes what fits |
| Does it survive a long session? | Run it for a few hours |
| Context length actually usable | `LLM_CONTEXT_LEN` vs. what the KV cache costs |

The model itself is a variable, not part of the architecture. Changing it is not a
code change — `OLLAMA_MODEL` in `.env` names it for the scripts, and the model
selector in Open WebUI picks it per conversation.

---

## Swapping to a VPS + GPU later

This is the point of the whole exercise. The upper layers do not change:

```bash
bash scripts/connect-endpoint.sh --disconnect
bash scripts/connect-endpoint.sh --url http://your-vps:8000/v1
```

Two things to get right on the way:

- **Bind the runtime to the private network, not to `0.0.0.0`.** On a VPS with
  a public IP, an inference server on `0.0.0.0` is open to the internet — and
  it bypasses Cloudflare Access the same way port 3000 does. See
  `scripts/check-exposure.sh`.
- **Leave the embedding engine where it is unless you mean to move it.**
  Changing the embedding model invalidates every vector already stored
  ([D-013](../DECISIONS.md)). Chat can move to the GPU while embeddings stay
  put. The probe reports embeddings separately for exactly this reason.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Probe exits 2 | The endpoint is unreachable — the tunnel is down, the notebook stopped, or the URL is stale. This is *not* "the runtime is broken". |
| Probe exits 1 | The endpoint answered, but not in OpenAI shape. Read which check failed. |
| `connect-endpoint.sh` refuses with "不做任何變更" | Working as intended. Fix the failing check first. |
| Models do not appear in the UI | The config is right but the fetch failed. Check the open-webui logs. |
| Everything works, then stops after ~12h | Kaggle session limit. Reboot the endpoint and reconnect. |
| `check-egress.sh` reports an enabled external endpoint | Expected once connected — that is what connecting means. Confirm it is yours. |

After connecting, `bash scripts/check-egress.sh` will list your runtime under
"啟用中". That is correct and intended. The probe exists so that it is a
decision rather than an accident.
