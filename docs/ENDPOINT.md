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
| That the patched generator survives a real `endpoint boot` | **Not verified** — the patch is verified against simulated ntfy, not against a live Kaggle run |
| That the kernel log can be read **while the kernel is still running** — the premise the new URL reader rests on in practice | **Not measured** — the one reading that was taken was against a *finished* kernel (D-068) |
| That the deployment instruments stay honest under speculative decoding (MTP) | **Known to conflict** — see "One known way to make these instruments lie" below, and D-061 |

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
| `endpoint stop` | Stop this instance. **It tells you which of the three things happened** — see [`endpoint stop` when it cannot see the kernel](#endpoint-stop-when-it-cannot-see-the-kernel) |
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

That key ends up in **three** places, and only one of them heals itself:

| Where | What it is |
|---|---|
| `~/.config/endpoint/endpoint-config.yaml` → `identity.api_key` | The **source of truth**. Every boot copies it into the notebook pushed to Kaggle — **in cleartext**. |
| `.env` → `ENDPOINT_API_KEY` | A **hard copy**, read by `connect-endpoint.sh` and the probes. |
| Open WebUI's database → `openai.api_keys` | A **hard copy**, index-aligned with `openai.api_base_urls`. |

The CLI alone recovers from a stale value — it re-reads the live engine's
`GET /v1/apikey`. The other two never do. They just go quietly stale and surface
much later as a 401, with nothing pointing back here.

```bash
bash scripts/rotate-endpoint-key.sh --check   # do the three still agree?
bash scripts/rotate-endpoint-key.sh           # replace all three at once
```

`--check` prints **fingerprints only, never the key**, changes nothing, and exits
`2` when the three disagree — so it works as a gate.

Rotate **before** a boot, not after. Rotation is a local action: it does not touch
Kaggle. The old key stays live inside the notebook already pushed there until the
next boot replaces it — and while that notebook is private, it is still cleartext
on someone else's server.

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
bash scripts/rotate-endpoint-key.sh --check     # do the three key holders agree?
bash scripts/rotate-endpoint-key.sh             # rotate all three at once
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
- **The key lives in cleartext on Kaggle's servers.** Every boot bakes it into
  the pushed notebook. That notebook is *private*, so this is not a public leak —
  but it is visible to anyone holding the account, and the CLI never rotates it
  on its own. `scripts/rotate-endpoint-key.sh` is the only thing that does, and
  rotation only takes effect at the **next** boot.
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

### One known way to make these instruments lie

Not everything in the table above is "we have not got to it yet". There is one
change that is known to break the instruments themselves, so it is worth naming:

**A draft head for speculative decoding** (Gemma 4's MTP drafters are the current
example). Ollama's `DRAFT` directive loads a second, smaller model alongside the
target one, and [`/api/ps` under-reports the memory when one is
present](https://github.com/ollama/ollama/issues/17951) — 315 MB reported for a
4.4 GB model.

That lands on this project in a specific place. The GPU verdict in
`scripts/deploy_smoke_probe.py` is a **ratio** test (`size_vram == size`) whose
printed evidence is an **absolute** number. A ratio survives a number being
wrong, so the layer D-056 calls the only execution-plane evidence would go on
saying "the whole model is on the GPU" while displaying a figure that is not the
model. The other two instruments do not answer wrongly — they stop answering: a
model outside the six-row table in `model_gb_estimate()` makes the disk gate
print `unknown` (a human becomes the gate), and the host-RAM warning falls
through to "skipping the check".

**This project does not use MTP.** It installs fine — ollama 0.34.2 supports it
— but it raises tokens/second while our bottleneck is token *count* (D-054), and
no T4 numbers exist for it. The full record, including what is still unverified,
is DECISIONS.md **D-061**.

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

## Patching the notebook generator

`endpoint boot` does not run a notebook you can edit. It runs
`master_build_notebook.py` inside the installed `endpoint-vps` package, which
*generates* the notebook and pushes it — so when the generated notebook has a
defect, the generator is the only place to fix it.

Two defects were found by reading the kernel log of a run that died healthy
([D-050](../DECISIONS.md)):

| Defect | What it did |
|---|---|
| `signal()` published every HF download progress line | 629 messages in 628s against a budget of ~60 plus one per 5s per IP. The bucket drained, and the one-shot `TUNNEL ACQUIRED` carrying the URL was discarded with a 429 that `signal()` swallows in silence. |
| `check_kill_signals()` used `/raw?since=5m` without `poll=1` | That is a blocking subscribe, not a poll. `boot` publishes a `KILL` ~20s before the new kernel starts, so every boot replayed its predecessor's kill signal — and fired it 32 minutes later, on a healthy engine. |

Both are fixed together, because they share one budget: the notebook's IP both
publishes signals *and* polls the kill topic.

```bash
bash scripts/apply-endpoint-ntfy-fixes.sh --dry-run   # check; change nothing
bash scripts/apply-endpoint-ntfy-fixes.sh             # apply
bash scripts/apply-endpoint-ntfy-fixes.sh --revert    # undo
```

The script refuses unless the target hashes to the exact version the patch was
built against, refuses if the pristine file already *passes* the behavioural
verifier (which would mean there is nothing left to fix), backs the file up
first, and re-verifies afterwards. `scripts/test_endpoint_ntfy_fixes.py` is that
verifier and takes any generator as an argument: it drives the code the
generator actually *emits*, against a simulated ntfy token bucket and a fake
clock. Note that it is meant to fail against the pristine file — that failure is
the demonstration of the bug.

**This patches a file inside a package-manager directory.** Reinstalling or
upgrading `endpoint-vps` reverts it; re-run the script after an upgrade.

## `endpoint stop` when it cannot see the kernel

On 2026-09-26 `endpoint stop` reported **"No running kernel found." and exited
0 while the GPU session was alive and burning quota** ([D-060](../DECISIONS.md)).
Stopping it took a direct call to `endpoint.core.send_kill_signal`.

The cause was not a missing check. `get_kernel_status()` returned `"offline"`
from *every one* of its failure paths — no credentials, a non-200, a network
error, an unrecognised status word — and `run_stop` read `"offline"` as the fact
*nothing is running*. So the single value the caller trusted was produced
exclusively by "could not ask". Same shape as [D-051](../DECISIONS.md): collapse
"could not read" into "not there", then do nothing on that branch.

**In a non-interactive shell this is the normal path, not an edge case.** The
Kaggle token lives in your shell rc file, which a non-interactive shell does not
read. `get_kaggle_token()` returns `None`, and the state is unreadable every
single time.

After the patch, the exit code says which of three things happened:

| Exit code | Meaning |
|---|---|
| `0` | Stopped, or confirmed there was nothing running. |
| `2` | **The kill signal went out, but the state was never readable** — termination is unconfirmed. Check `endpoint status` in a shell that has the token, or the Kaggle web UI. |
| `1` | A definite failure — the config is missing (`endpoint init` was never run). |

`2` rather than `0` or `1` follows the convention the probes already use
([D-018](../DECISIONS.md)): 1 is a definite failure, 2 is *cannot determine*.
`endpoint stop && echo "stopped"` no longer lies.

When the state is unknown it deliberately does **not**:

- **Call the Kaggle cancel path.** That path leads with
  `kaggle kernels delete -y`, which destroys the kernel and its version history.
  Spending an irreversible action on a state you could not read is the wrong
  shape — and it needs the same credentials that just failed, so it could not
  have worked anyway. The kill signal suffices: the notebook exits on it, and
  that is what actually stopped the GPU on 2026-09-26.
- **Assume the best quietly.** `endpoint status` prints
  `unknown (could not check)` in yellow for a state it could not read, instead of
  painting it red as though `offline` had been confirmed.

```bash
bash scripts/apply-endpoint-stop-fixes.sh --dry-run   # check; change nothing
bash scripts/apply-endpoint-stop-fixes.sh             # apply (two files)
bash scripts/apply-endpoint-stop-fixes.sh --revert    # undo
```

The patch spans **two** files (`endpoint/core.py` and `endpoint/commands.py`),
so the script pins four hashes, backs both up under one timestamp, and restores
them together. It refuses to act when the two files are in *different* states:
one diff across two files cannot half-apply, so that state means an interrupted
run or a hand edit, and guessing which half to finish is worse than asking you
to revert first.

**This patches files inside a package-manager directory.** Reinstalling or
upgrading `endpoint-vps` reverts it; re-run the script after an upgrade.

> **What this does not establish.** Whether Kaggle's API can ever return the word
> `offline` has not been checked either way — it is absent from the SDK's
> `KernelWorkerStatus` enum, so the patch does not rely on it. The fix is
> verified behaviourally against both the pristine and the patched files, and
> end-to-end by running `endpoint stop` in a non-interactive shell — but that run
> could only reach the *unreadable* path, because it is the only path such a
> shell has. Confirming that a **live** kernel really stops still needs a boot,
> which spends GPU quota.

## The API key the engine published in cleartext

`_startup()` calls

```python
_broadcast(f"APIKEY:{_get_api_key()}")
```

and `_broadcast` POSTs `STATUS: [<session>] <msg>` to an **ntfy topic whose name
was derived from the Kaggle username** (`endpoint/core.py`) — a public account.
So every boot published its own API key, in the clear, to a channel anyone who
knew the account name could reconstruct ([D-067](../DECISIONS.md)). Measured
end-to-end on 2026-09-28, not inferred.

```bash
bash scripts/apply-endpoint-apikey-broadcast-fixes.sh --dry-run   # check; change nothing
bash scripts/apply-endpoint-apikey-broadcast-fixes.sh             # apply
bash scripts/apply-endpoint-apikey-broadcast-fixes.sh --revert    # undo
```

One file this time, `engine/engine.py`, and it is the **producer**: the notebook
generator reads `REPO_ROOT/engine/engine.py`, base64-embeds it, and the kernel
decodes it at boot. Patching the installed engine therefore changes what the
next boot runs. The patch deletes that one line — it does not replace it.
`STARTING...` and `WAITING FOR MODEL...` still go out, so the lifecycle signal
on that topic is unchanged.

Deleting the broadcast cannot hang boot, and that is asserted rather than
assumed: `run_boot`'s 600s wait loop breaks on `TUNNEL ACQUIRED` and nowhere
else — the `APIKEY:` branch only `continue`s. The verifier fails the moment that
stops being true, because then the deletion would cost a full timeout.

> **What this does not establish — read this before treating the topic as safe.**
> This cut removes one link, not the chain. `GET /v1/apikey` sits in the auth
> middleware's bypass set, so anyone who obtains the tunnel URL still reads the
> key back with a single unauthenticated request. What this cut removes is the
> only link that needed *no* prior knowledge of the tunnel at all. Closing the
> chain means taking the URL off the public topic — **that is now done**
> (`scripts/apply-endpoint-tunnel-url-privacy.sh`, [D-068](../DECISIONS.md), see
> the next section) — and then taking the topic *name* away from the public
> username, which is **also now done** (`scripts/apply-endpoint-topic-secret.sh`,
> [D-069](../DECISIONS.md), three sections down). What remains is
> `GET /v1/apikey` itself. Obtaining the URL now takes Kaggle credentials, and
> reconstructing the topic now takes a secret that never leaves this machine —
> which is the whole point of both changes.
>
> It is also verified offline only. The bake test decodes the engine source back
> out of a generated notebook and finds zero `APIKEY:` broadcasts where the
> pristine engine produces one, but a real boot was not run — that spends GPU
> quota. The new proof is that the artifact no longer *contains* the broadcast.

## Where the tunnel URL comes from now

Step 3 says `endpoint base-url`. This is where that URL now comes from, and why
it changed.

**The channel it used to come from was public.** The boot signal went to an ntfy
topic whose *name* was derived from the Kaggle username (`endpoint/core.py`) — a
public account. So the tunnel URL sat in cleartext on a channel anyone who knew
the account name could reconstruct — and holding that URL was enough to read the
API key back out of `GET /v1/apikey` with no credentials at all ([D-067](../DECISIONS.md)).
This cut takes the URL off that topic. The **kernel log** replaces it, and
reading the log takes a **Kaggle token**. The topic *name* was still a function
of the public username after this cut — that is the next cut, three sections
down.

It is a smaller change than it sounds, because the URL was **already** in the
kernel log: the generator's `signal()` prints every message *before* it POSTs it.
What the patch changes is the publishing, not the writing.

```python
print(f'TUNNEL ACQUIRED: {tunnel_url}', flush=True)   # URL  -> kernel log only
signal('TUNNEL ACQUIRED:', topic_only=True)           # empty -> topic only
```

The topic still receives a `TUNNEL ACQUIRED:` — with nothing after the colon.
`boot` waits on that signal and nothing else, so its arrival still means exactly
what it meant before; only the payload is gone.

```bash
bash scripts/apply-endpoint-tunnel-url-privacy.sh --dry-run   # check; change nothing
bash scripts/apply-endpoint-tunnel-url-privacy.sh             # apply
bash scripts/apply-endpoint-tunnel-url-privacy.sh --revert    # undo
```

⚠️ **The order is not optional.** This patch is built against a tree that already
has the ntfy fixes *and* the stop fixes applied — `apply-endpoint-ntfy-fixes.sh`
patches the *same* notebook generator, and `apply-endpoint-stop-fixes.sh` patches
the *same* `core.py` and `commands.py`. Apply **ntfy fixes → stop fixes → this
one**, and note that `--revert` on either of the first two **silently undoes this
one**; this patch's hash gate only notices the next time you run it.

**Three states, and two of them are not the same thing.** The reader answers
`found`, `absent` (the log was read and holds no URL *yet*) or `unknown` (the log
could not be read at all). Collapsing `absent` into `unknown` — or into "nothing
is running" — is the defect `endpoint stop` had (D-060), and it is the reason the
two are separate values here. `boot` **succeeds** in all three cases; `absent` and
`unknown` print different sentences, and both point you at `endpoint base-url`.

To check any of it against the real API without a boot:

```bash
python3 scripts/probe_kernel_log_url.py            # exit 0 found / 1 absent / 2 unknown / 3 usage
```

It prints status codes, booleans and counts — **never the log body, the URL, the
token or the topic**. The URL appears only as a one-way fingerprint, so two runs
can be compared without either revealing it. Treat the tunnel URL as a
credential: while it is live, anyone holding it can reach the engine.

> **What this does not establish.** The reader has only ever been pointed at a
> **finished** kernel. `ListKernelSessionOutput` serves a persisted blob and may
> behave differently against a *running* one — and that is exactly the case a
> fresh boot is in. So "`boot` can fetch its own URL afterwards" is **not
> measured**; the fallback is the same as before, `endpoint base-url` a few
> minutes later. Second gap: the log was **not** truncated at the head in the one
> sample read, but if Kaggle ever starts returning only the tail, the URL — at
> 76.5% of that sample — could fall outside the window; the probe deliberately
> refuses to guess, because a format-guessing "is it truncated?" verdict reads
> *false* on exactly the case it is meant to catch. Third: reading the log needs
> a token that non-interactive shells can find, which is why it belongs in
> `~/.kaggle/kaggle.json` (mode `600`, with `username` and `key`) rather than a
> shell rc file. Finally, the patch is verified offline — no real boot was run,
> because that spends GPU quota.

## The topic name is no longer a function of a public account

The previous two cuts emptied the topic of secrets. This one changes **who can
reach the topic at all** ([D-069](../DECISIONS.md)).

A topic name is not a channel identifier. It is a **bearer capability**: ntfy
authenticates nothing, so knowing the name *is* the right to read it and to
publish on it. And until this cut the name was `sha256(kaggle_username)[:12]` —
a function of a public account, computable by anyone in one line. That bought
three things, none of which needed a secret: **read the lifecycle**, **forge a
`KILL`**, and **flood the topic to delay a real `KILL`** by analysis of the
traffic.

The name is now derived from a **secret that never leaves this machine**. The
generator bakes the *derived name* into the notebook; the engine only ever reads
`LLM_SIGNAL_TOPIC` / `LLM_CONTROL_TOPIC` and never recomputes it. So:

> **The secret's blast radius is this machine. The topic's blast radius is this
> machine plus Kaggle plus the kernel log.**

That second half matters, and it is why the topic is still treated as a
credential: it lands in a private Kaggle notebook, in
`/tmp/endpoint-engine-output/endpoint_setup.ipynb` (which **persists** — a
9/28 file was still there), and in the kernel log. The output directory is now
created `0700` for exactly this reason.

**Order matters, and it is the easiest thing here to get wrong.** Writing the
secret changes *nothing* — the running code still derives the old name. The
moment the topic actually switches is the moment the patch lands. So:

```bash
# 1. confirm no kernel is running — endpoint status (needs a Kaggle token)
bash scripts/set-endpoint-topic-secret.sh            # secret in place
bash scripts/apply-endpoint-topic-secret.sh --dry-run # check; change nothing
bash scripts/apply-endpoint-topic-secret.sh           # apply — the switch
```

Do it the other way round and a kernel that is still running **goes deaf**: it
is listening on the old name, so no `stop` and no `KILL` reaches it, and it
keeps burning GPU quota. The 60-minute idle timeout is **not** a recovery path —
it kills the engine process, not the notebook. Recovery is
`endpoint kill-all --yes` (it never constructs a `Config`, so it is unaffected
by the secret) or the Kaggle UI.

**The script refuses to run without a well-formed secret, and there is no
`--force` for that.** That gate is what makes the "patched but no secret" state
unreachable, because that state is a P0: `boot` would kill the healthy kernel
and *then* fail. Everything that touches the topic fails loudly instead of
silently falling back to the old derivation.

**A secret that will not be recoverable.** The setter keeps no backup; the
config file is overwritten in place. The derivation is one-way, so the old name
is not recoverable either — and does not need to be, since it was a function of
a public account. The cost is that **without a Kaggle token there is no
credential-free recovery path**; the Kaggle UI is the other way out.

**One secret per lab.** The domain-separation string in the derivation does not
bind the prefix or the account, so the same secret used in two labs derives the
same topic. And the derivation is a **hash, not a KDF**: anyone who learns the
topic name holds an offline validator and can test candidate secrets at hash
speed. That is why the shape is enforced at 32 hex characters (128 bits) rather
than left to taste — it is an entropy proxy, not a measurement of entropy.

**Verified offline only.** The bake test generates a real notebook and asserts
the derived topic is in it and the canary secret is not, and the thirteen-check
verifier drives both derivations against stubs; the mutation harness shows each
check has teeth. **No real boot was run** — that spends GPU quota — so "a `KILL`
on the new topic actually arrives" is *not* measured. Nothing here proves how a
real boot behaves.

⚠️ Applying this makes the **cut-B** apply script's `state_of` report
`unknown:<hash>` for the three shared files; its message does not mention cut C.
That is recorded, not fixed. And **cut C's pristine hashes are exactly cut B's
patched hashes**, so **B must be applied first**.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Probe exits 2 | The endpoint is unreachable — the tunnel is down, the notebook stopped, or the URL is stale. This is *not* "the runtime is broken". |
| Probe exits 1 | The endpoint answered, but not in OpenAI shape. Read which check failed. |
| `connect-endpoint.sh` refuses with "不做任何變更" | Working as intended. Fix the failing check first. |
| Models do not appear in the UI | The config is right but the fetch failed. Check the open-webui logs. |
| Everything works, then stops after ~12h | Kaggle session limit. Reboot the endpoint and reconnect. |
| `check-egress.sh` reports an enabled external endpoint | Expected once connected — that is what connecting means. Confirm it is yours. |
| Kernel log shows `SHUTDOWN SIGNAL RECEIVED` but nobody ran `stop` | The unpatched kill switch replaying the `KILL` its own boot published ~20s before it started. Apply the patch. |
| `boot` succeeds but the tunnel URL never arrives | The unpatched rate limit spent the budget on download progress. Check with `scripts/apply-endpoint-ntfy-fixes.sh --verify`. |
| `apply-endpoint-ntfy-fixes.sh` refuses with a hash mismatch | `endpoint-vps` was upgraded. Check whether upstream fixed it; otherwise rebuild the patch against the new file. |
| `endpoint stop` says "No running kernel found." but the runtime still answers | The unpatched status read: `get_kernel_status()` called every failure `offline`. Apply `scripts/apply-endpoint-stop-fixes.sh`. (After the patch this becomes exit code 2 with an explicit "could not confirm".) |
| `endpoint stop` exits 2 | Working as intended, not a failure. The kill signal was sent; the state could not be read because this shell has no Kaggle token. Confirm with `endpoint status` in a shell that has one. |
| `apply-endpoint-stop-fixes.sh` refuses, saying the two files disagree | A previous run was interrupted, or one file was edited by hand. Run the script with `--revert` first. |
| A boot has no `API key registered with engine` line | Expected after `scripts/apply-endpoint-apikey-broadcast-fixes.sh` — that message came from the removed broadcast. The key still registers over HTTP; this line going away is not a failure. |
| `apply-endpoint-apikey-broadcast-fixes.sh` warns about link count | `engine.py` is hard-linked out of the uv cache, and `patch` breaks those links. Harmless for the generator (it reads the file you patched), but a reinstall restores the cached copies — re-run the script after an upgrade. |
| `endpoint status` prints `unknown (could not check)` | The patch working: `endpoint status` also needs the token, and this shell does not have it. Yellow, not red — an unreadable state is not a confirmed `offline`. |
| `connect-endpoint.sh` or a probe gets a 401 while the endpoint itself is up | `.env` is holding a **stale hard copy** of the key — it never refreshes. Run `scripts/rotate-endpoint-key.sh --check`. |
| `rotate-endpoint-key.sh --check` exits 2 | Working as intended: the three holders disagree, and disagreeing is silent until something 401s. Run the script without `--check` to make them agree. |
| `rotate-endpoint-key.sh` exits 2 saying open-webui is not running | Working as intended. Rotating only the two reachable holders would leave the third holding a **dead key with no symptom**. Start the stack and re-run. |
| `rotate-endpoint-key.sh` exits 3 | The value is not exactly one `myth-` key in that file. The script prints the **count only**, never the value — look at the file yourself before re-running. |
| `boot` succeeds but says the URL is not in the kernel log yet | Expected, not a failure. The tunnel is up and the log has not caught up. Take it a minute later with `endpoint base-url`. |
| Nothing on the ntfy topic carries the tunnel URL any more | Working as intended after `scripts/apply-endpoint-tunnel-url-privacy.sh` — the URL is in the kernel log now, and the topic deliberately carries `TUNNEL ACQUIRED:` with an empty value. Use `endpoint base-url`. |
| `endpoint base-url` says it could not read the kernel log | Two different sentences, two different causes: **no credentials** (this shell cannot see a Kaggle token) or **Kaggle refused**. Read which one printed. The fix for the first is `~/.kaggle/kaggle.json` — a shell rc file is not read by non-interactive shells, which is where cron and most scripts run. |
| The URL stops arriving right after reverting the ntfy or stop fixes | Expected — those patches touch the same three files this one does. Re-apply in order: ntfy fixes, stop fixes, then `apply-endpoint-tunnel-url-privacy.sh`. The hash gate only notices on the next run, so nothing warns you at the moment it happens. |
| `apply-endpoint-tunnel-url-privacy.sh` refuses with a hash mismatch | Either `endpoint-vps` was upgraded, or the ntfy/stop fixes are not applied (they are the baseline this patch was built against). Run `--verify` on those two first. |
| `boot` / `stop` / `watch` fail loudly saying there is no usable `signal.topic_secret` | Working as intended after `scripts/apply-endpoint-topic-secret.sh` — the patch landed but the secret was never set. Run `scripts/set-endpoint-topic-secret.sh` (with no kernel running). This is the failure the design *wants*: the alternative was silently deriving the old, publicly computable name. |
| `set-endpoint-topic-secret.sh` refuses saying a kernel is `running` | Working as intended. Changing the topic now makes that kernel deaf, and it keeps burning GPU quota. `endpoint stop` (or `endpoint kill-all --yes`) first, then re-run. `--force` overrides the check. |
| `set-endpoint-topic-secret.sh` warns it could not read the kernel status | **"Could not check" is not "nothing is running"** — that is the D-060 shape. It usually means this shell has no Kaggle token. Confirm with `endpoint status` before continuing. |
| `set-endpoint-topic-secret.sh` warns the status word is not in the known list | Working as intended. Kaggle returned a word outside `running`/`queued`/`pending`/`complete`/`error`. Only the two words that *positively* mean "the kernel ended" are allowed to pass; everything else is treated the same as "could not check" — because the opposite mistake, reading it as "nothing is running", is the D-060 failure this gate exists to avoid. Confirm with `endpoint status`. |
| A kernel is running and the topic has already been changed | It cannot be signalled any more. `endpoint kill-all --yes` never constructs a `Config`, so it still works, and so does the Kaggle UI. **The 60-minute idle timeout will not save you** — it kills the engine process, not the notebook. |
| `apply-endpoint-topic-secret.sh` refuses, naming the ntfy / stop / tunnel-url scripts | Working as intended — cut C stacks **on top of cut B**, so its pristine hashes are exactly B's patched hashes. Apply those in order first. |
| The ntfy topic went quiet right after applying cut C | Expected, not a failure — the kernel is now publishing on the *new* name. Confirm a kernel was not running when you applied it (see above); if one was, it is an orphan. |
| `--check` on `set-endpoint-topic-secret.sh` exits 2 | Working as intended: there is no usable secret, and after the patch everything that touches the topic fails. Run the script without `--check`. |

After connecting, `bash scripts/check-egress.sh` will list your runtime under
"啟用中". That is correct and intended. The probe exists so that it is a
decision rather than an accident.
