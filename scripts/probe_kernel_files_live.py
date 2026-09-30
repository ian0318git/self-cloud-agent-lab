#!/usr/bin/env python3
"""Can a kernel's output files be read *while the session is still running*?

    scripts/probe_kernel_files_live.py                    # 用 endpoint 設定裡的 kernel
    scripts/probe_kernel_files_live.py --kernel O/S       # 指定
    scripts/probe_kernel_files_live.py --once             # 只打一次
    scripts/probe_kernel_files_live.py --duration 900 --interval 30
    scripts/probe_kernel_files_live.py --json             # 供機器讀

WHY THIS EXISTS. Cut B moved the tunnel URL off the public ntfy topic and onto the
kernel's own output. D-072 section 4 then measured that the *log* cannot be read
while the session runs -- the URL only appears once the session has ended, by
which time the tunnel is gone. Cut B is therefore unsolved, and D-073 only made
boot stop claiming otherwise.

Option B in D-073 section 7 is: the engine already writes files into Kaggle's
session output (`engine.py:191-193` -> `/kaggle/working/logs/`), so read the file
instead of the log. The carrier already exists and already holds the URL -- that
was measured on 2026-10-01 against the terminated D-072 session, where
`logs/cf_engine.log` carried the same tunnel URL as the log (matching one-way
fingerprints). What was NOT measured is the only thing that matters:

    does any of that work while the session is RUNNING?

That is what this probe answers, and it answers it against a cheap CPU kernel
(`scripts/kaggle-files-live-probe/`) rather than a GPU boot, because the question
is about Kaggle's file API and has nothing to do with GPUs.

TWO APIs, DELIBERATELY BOTH. They are not the same door:

  * `ListKernelSessionOutput` -> carries `files` (name + signed url) and `log`.
    This is what `kaggle kernels output` uses (kaggle_api_extended.py:6689).
  * `ListKernelFiles` -> carries `name` / `size` / `creation_date` and **no url**
    (kagglesdk .../kernels_api_service.py:1341). It returned empty `{}` for both
    the endpoint kernel AND a popular public kernel on 2026-10-01, so it lists
    nothing here -- but it costs one call to keep checking.

WHAT IT NEVER PRINTS. The log body, any URL, the token, and the topic name.
Output is status words, HTTP codes, key names, counts, and file *names* -- names
are paths like `probe_out/t0.txt`, which is exactly what the question is about.

Exit codes: 0 ran to completion, 2 could not read at all, 3 usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import requests

from probe_kernel_log_url import read_token

BASE = "https://api.kaggle.com/v1/kernels.KernelsApiService/"
SESSION_OUTPUT = BASE + "ListKernelSessionOutput"
KERNEL_FILES = BASE + "ListKernelFiles"
SESSION_STATUS = BASE + "GetKernelSessionStatus"

# The words endpoint.core.get_kernel_status() recognises (core.py:1100). Kept in
# step on purpose: a status word outside this set is reported raw, never folded
# into "not running" -- that collapse is D-060.
STATUS_WORDS = ("running", "queued", "pending", "complete", "error")
TERMINAL = ("complete", "error")


def call(url: str, token: str, owner: str, slug: str, timeout: int = 20):
    """POST one RPC. Returns (status_code, payload_dict_or_None, error_or_None)."""
    try:
        r = requests.post(
            url,
            json={"userName": owner, "kernelSlug": slug},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    try:
        return r.status_code, r.json(), None
    except Exception:
        return r.status_code, None, "non-JSON body"


def classify_status(payload) -> str:
    raw = (payload or {}).get("status", "") if isinstance(payload, dict) else ""
    for word in STATUS_WORDS:
        if word in str(raw).lower():
            return word
    return f"raw:{raw!r}" if raw else "—"


def poll(token: str, owner: str, slug: str) -> dict:
    """One round. Returns a flat record; never raises."""
    rec: dict = {}

    code, payload, err = call(SESSION_STATUS, token, owner, slug)
    rec["status_http"] = code
    rec["status"] = classify_status(payload) if code == 200 else "unknown"
    if err:
        rec["status_error"] = err

    code, payload, err = call(SESSION_OUTPUT, token, owner, slug)
    rec["so_http"] = code
    if code == 200 and isinstance(payload, dict):
        rec["so_keys"] = sorted(payload.keys())
        log = payload.get("log") or ""
        rec["so_log_chars"] = len(log)
        files = payload.get("files") or []
        rec["so_files"] = len(files)
        rec["so_names"] = sorted(
            f.get("fileName", "?") for f in files if isinstance(f, dict)
        )
    else:
        rec["so_keys"] = None
        rec["so_files"] = None
        if err:
            rec["so_error"] = err

    code, payload, err = call(KERNEL_FILES, token, owner, slug)
    rec["kf_http"] = code
    if code == 200 and isinstance(payload, dict):
        items = payload.get("files") or []
        rec["kf_items"] = len(items)
        rec["kf_names"] = sorted(
            i.get("name", "?") for i in items if isinstance(i, dict)
        )
    else:
        rec["kf_items"] = None
        if err:
            rec["kf_error"] = err

    return rec


def render(t: int, r: dict) -> str:
    so = "—"
    if r.get("so_keys") is not None:
        names = r.get("so_names") or []
        so = (
            f"{r['so_http']} keys={','.join(r['so_keys'])} "
            f"log={r['so_log_chars']} files={r['so_files']}"
        )
        if names:
            so += f" [{', '.join(names)}]"
    kf = "—" if r.get("kf_items") is None else f"{r['kf_http']} items={r['kf_items']}"
    return f"T+{t:>4}s  status={r['status']:<10} session[{so}]  kernelFiles[{kf}]"


def verdict(polls: list[tuple[int, dict]]) -> tuple[str, str]:
    """The two sentences this whole probe exists to be able to say."""
    running_files = [
        (t, r) for t, r in polls
        if r["status"] == "running" and (r.get("so_files") or 0) > 0
    ]
    terminal_files = [
        (t, r) for t, r in polls
        if r["status"] in TERMINAL and (r.get("so_files") or 0) > 0
    ]
    running_any = [(t, r) for t, r in polls if r["status"] == "running"]
    kf_any = [(t, r) for t, r in polls if (r.get("kf_items") or 0) > 0]

    if not running_any:
        return "INCONCLUSIVE", "沒有任何一次輪詢讀到 status=running —— 這支探針沒有量到問題問的那一刻。"
    if running_files:
        t, r = running_files[0]
        return "B-LIVE", (
            f"跑動中就拿得到檔案（T+{t}s，{r['so_files']} 筆：{', '.join(r.get('so_names') or [])}）。"
            " 選項 B 的前提成立 —— 它不必等 session 結束。"
        )
    if terminal_files:
        return "B-POST-ONLY", (
            "跑動中拿不到，結束後才拿到 —— 與 log 同進退。"
            " 選項 B 與『讀 log』是同一扇落盤後的門，B 不解這個問題。"
        )
    return "NO-FILES-AT-ALL", (
        "全程都沒看到任何檔案，連結束後也沒有。"
        " 這代表這支探針沒有量到檔案 API，不是『門沒開』—— 先確認儀器。"
    )


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, which here means 'could not read'."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(3)


def main(argv: list[str]) -> int:
    p = _Parser(
        prog="probe_kernel_files_live.py",
        description="在 kernel 跑動中輪詢 Kaggle 的檔案 API，判定切 B 選項 B 的前提。",
        add_help=False,
    )
    p.add_argument("--kernel", metavar="OWNER/SLUG", default="",
                   help="要輪詢的 kernel（預設取自 endpoint 設定）")
    p.add_argument("--interval", type=int, default=30, metavar="S", help="輪詢間隔秒數（預設 30）")
    p.add_argument("--duration", type=int, default=900, metavar="S", help="總共輪詢幾秒（預設 900）")
    p.add_argument("--timeout", type=int, default=20, metavar="S", help="單次 HTTP 逾時秒數")
    p.add_argument("--once", action="store_true", help="只打一次就結束")
    p.add_argument("--json", action="store_true", help="只印 JSON，供機器讀")
    p.add_argument("-h", "--help", action="store_true", help="印出這份說明")
    args = p.parse_args(argv[1:])

    if args.help:
        print(__doc__.strip())
        return 0

    token, source = read_token()
    if not token:
        print("✗ 沒有 Kaggle 權杖，讀不了任何東西。", file=sys.stderr)
        return 2

    kernel = args.kernel
    if not kernel:
        try:
            from endpoint.core import Config

            kernel = Config().kernel_id
        except Exception:
            kernel = ""
    if kernel.count("/") != 1 or not all(kernel.split("/", 1)):
        print("✗ 沒有 kernel 可查（--kernel OWNER/SLUG）。", file=sys.stderr)
        return 3
    owner, slug = kernel.split("/", 1)

    polls: list[tuple[int, dict]] = []
    started = time.monotonic()
    deadline = started + (1 if args.once else args.duration)

    while True:
        t = int(time.monotonic() - started)
        rec = poll(token, owner, slug)
        polls.append((t, rec))
        if not args.json:
            print(render(t, rec), flush=True)
        if time.monotonic() >= deadline:
            break
        time.sleep(max(1, args.interval))

    code, why = verdict(polls)

    if args.json:
        print(json.dumps(
            {
                "kernel_owner_slug_present": True,
                "token_source": source,
                "polls": [{"t": t, **r} for t, r in polls],
                "verdict": code,
                "why": why,
            },
            ensure_ascii=False, indent=2, sort_keys=True,
        ))
    else:
        print()
        print(f"判讀  {code} —— {why}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
