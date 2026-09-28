#!/usr/bin/env python3
"""Does the Kaggle kernel log actually hold a readable tunnel URL? (D-068, cut B)

    scripts/probe_kernel_log_url.py                 # 用 endpoint 設定裡的 kernel
    scripts/probe_kernel_log_url.py --kernel O/S    # 指定 kernel
    scripts/probe_kernel_log_url.py --json          # 供機器讀
    scripts/probe_kernel_log_url.py --negative-control   # 多打一次不存在的 slug

Cut B moves the tunnel URL off the public ntfy topic and onto the kernel log.
That only works if the log can be read back, so this probe measures the four
things the design assumes and nothing else:

  * the token resolves the way `endpoint.core.get_kaggle_token()` resolves it
  * `ListKernelSessionOutput` accepts it (`Authorization: Bearer`, not
    `X-Kaggle-Authorization` -- that spelling returns 403)
  * the response carries a `log` key
  * that log contains exactly one Cloudflare quick-tunnel URL

WHAT IT NEVER PRINTS. The log body, the URL, the token, the topic name, and the
Kaggle username. Output is status codes, booleans, counts, and a one-way
fingerprint of the URL so two runs can be compared without either of them
revealing it. Treat the tunnel URL as a credential: while it is live, anyone
holding it can reach the engine.

WHY IT DOES NOT IMPORT THE READER. `endpoint.core.get_kernel_log_url()` does not
exist before cut B is applied, and a probe that could only run after the change
could not establish the baseline the change was designed against. This probe
carries its own copy of the pattern instead, and *reports whether its copy still
agrees* with the one in `endpoint.core` -- so drift shows up as a boolean rather
than as a probe that quietly measures the wrong thing.

Exit codes: 0 found, 1 read but no URL yet, 2 could not read at all, 3 usage
error. `absent` and `unknown` stay apart on purpose -- collapsing them is what
let `endpoint stop` read a failure-to-ask as "nothing is running" (D-060).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# Kept in step with endpoint/core.py by the agreement check below, not by hand.
PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
ENDPOINT = "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernelSessionOutput"
KAGGLE_JSON = Path.home() / ".kaggle" / "kaggle.json"

# The reader's answer, reused verbatim so the words match the code under test.
FOUND, ABSENT, UNKNOWN = "found", "absent", "unknown"


def read_token() -> tuple[str | None, str]:
    """Mirror of endpoint.core.get_kaggle_token(), plus which source answered.

    The source matters right now: the token currently lives in `~/.bashrc`, which
    only interactive shells read, and cut B needs it from non-interactive ones
    too. Reporting "file" vs "environment" is how that migration is confirmed.
    """
    env = os.environ.get("KAGGLE_API_TOKEN")
    if env:
        return env, "env:KAGGLE_API_TOKEN"
    try:
        if KAGGLE_JSON.exists():
            data = json.loads(KAGGLE_JSON.read_text(encoding="utf-8"))
            key = data.get("key")
            if key:
                return key, "file:~/.kaggle/kaggle.json"
    except Exception:
        # A malformed kaggle.json is reported as "no token", which is exactly
        # what the real reader would conclude -- do not dress it up as a win.
        return None, "file:~/.kaggle/kaggle.json (unreadable)"
    return None, "none"


def fingerprint(text: str) -> str:
    """Short one-way digest: lets two runs be compared without printing either."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def fetch_log(token: str, owner: str, slug: str, timeout: int):
    """Return (status_code, payload_dict, error_string). Never raises."""
    import requests

    try:
        resp = requests.post(
            ENDPOINT,
            json={"userName": owner, "kernelSlug": slug},
            headers={
                # "Authorization: Bearer" -- NOT "X-Kaggle-Authorization",
                # which returns 403 against this endpoint.
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return resp.status_code, payload, None


def code_pattern() -> re.Pattern[str] | None:
    """The pattern as `endpoint.core` defines it, or None if it cannot be read."""
    try:
        from endpoint.core import _CF_URL_PATTERN  # type: ignore[attr-defined]

        return _CF_URL_PATTERN
    except Exception:
        return None


def code_token_agrees(token: str | None) -> bool | None:
    """Does endpoint.core.get_kaggle_token() reach the same conclusion we did?"""
    try:
        from endpoint.core import get_kaggle_token

        return bool(get_kaggle_token()) == bool(token)
    except Exception:
        return None


def default_kernel() -> str:
    try:
        from endpoint.core import Config

        return Config().kernel_id
    except Exception:
        return ""


def analyse(log_text: str) -> dict:
    """Everything we are willing to say about a log we must not print.

    Deliberately no "is the head truncated?" verdict. Deciding that needs the
    log's real line format, which cannot be confirmed from here without
    credentials -- and a format-guessing boolean is worse than none: the obvious
    heuristic ("a severed timestamp line starts mid-digit") reads *False* on
    exactly the case it is meant to catch, because a cut line often does start
    with a digit. The positive controls below are the honest detector: a
    non-empty log with no boot trace is either a windowed read or a kernel that
    has not got there yet, and only a human can tell which.
    """
    urls = PATTERN.findall(log_text)
    url = urls[-1] if urls else None
    return {
        "log_chars": len(log_text),
        "line_count": log_text.count("\n"),
        "url_count": len(urls),
        "url_position_pct": (
            round(log_text.find(url) * 100 / len(log_text), 1) if url else None
        ),
        "url_fingerprint": fingerprint(url) if url else None,
        # Positive controls: proves we read a real engine log, not an empty shell.
        "has_engine_launched": "ENGINE LAUNCHED" in log_text,
        "has_tunnel_acquired": "TUNNEL ACQUIRED" in log_text,
    }


def collect(args) -> tuple[dict, int]:
    report: dict = {"endpoint": ENDPOINT.split("/")[-1]}
    token, source = read_token()
    report["token_present"] = token is not None
    report["token_source"] = source
    report["token_length"] = len(token) if token else 0
    report["token_matches_endpoint_core"] = code_token_agrees(token)

    cp = code_pattern()
    report["pattern_matches_endpoint_core"] = (cp.pattern == PATTERN.pattern) if cp else None

    if not token:
        report["verdict"] = UNKNOWN
        report["why"] = "沒有權杖 —— 讀不了日誌"
        return report, 2

    kernel = args.kernel or default_kernel()
    # Both halves must be non-empty: an unset kaggle_username yields "/slug",
    # which contains a slash and would otherwise sail through to a 403.
    if kernel.count("/") != 1 or not all(kernel.split("/", 1)):
        report["verdict"] = UNKNOWN
        report["why"] = "沒有 kernel 可查（--kernel OWNER/SLUG，或讓 endpoint 設定提供）"
        return report, 3

    owner, slug = kernel.split("/", 1)
    status, payload, err = fetch_log(token, owner, slug, args.timeout)
    report["http_status"] = status
    report["http_error"] = err

    if status == 200 and isinstance(payload, dict):
        report["response_keys"] = sorted(payload.keys())
        log_text = payload.get("log") or ""
        report["log_present"] = bool(log_text)
        report.update(analyse(log_text))
        if report.get("url_fingerprint"):
            report["verdict"] = FOUND
        else:
            report["verdict"] = ABSENT
            report["why"] = "日誌讀到了，但裡面還沒有通道網址"
    else:
        report["verdict"] = UNKNOWN
        report["why"] = "讀不到日誌（沒有憑證、權杖被拒、或網路問題）"

    if args.negative_control:
        # A slug that does not exist must NOT come back as "absent". If Kaggle
        # ever starts answering 200 with an empty log for unknown kernels, the
        # reader would report "not yet" forever and the three-state design would
        # quietly stop meaning anything.
        nc_status, _, nc_err = fetch_log(token, owner, "no-such-kernel-xyzzy", args.timeout)
        report["negative_control_status"] = nc_status
        report["negative_control_error"] = nc_err
        report["negative_control_is_not_200"] = nc_status != 200

    return report, {FOUND: 0, ABSENT: 1, UNKNOWN: 2}[report["verdict"]]


def render(r: dict) -> None:
    def val(key, fmt=str, dash="—"):
        v = r.get(key)
        return dash if v is None else fmt(v)

    yn = lambda b: "—" if b is None else ("是" if b else "否")  # noqa: E731
    print("=" * 72)
    print("Kaggle 日誌讀取探針 —— 不印日誌內容、網址、權杖或主題")
    print("=" * 72)
    print(f"權杖                {yn(r['token_present'])}（{r['token_source']}"
          f"{'，' + str(r['token_length']) + ' 字元' if r['token_present'] else ''}）")
    print(f"與 endpoint.core 一致   {yn(r.get('token_matches_endpoint_core'))}")
    print(f"樣式與 endpoint.core    {yn(r.get('pattern_matches_endpoint_core'))}")
    print(f"端點                {r['endpoint']}")
    print(f"HTTP 狀態           {val('http_status')}")
    if r.get("http_error"):
        print(f"網路錯誤            {r['http_error']}")
    if r.get("response_keys"):
        print(f"回應鍵              {', '.join(r['response_keys'])}")
    if r.get("log_present") is not None:
        print(f"日誌                {yn(r.get('log_present'))}"
              f"（{val('log_chars')} 字元、{val('line_count')} 行）")
        print(f"通道網址命中數      {val('url_count')}")
        print(f"網址指紋            {val('url_fingerprint')}   ← 單向，供跨次比對")
        pct = r.get("url_position_pct")
        print(f"網址在日誌中的位置  {'—' if pct is None else str(pct) + '%'}")
        print(f"正向對照 ENGINE LAUNCHED    {yn(r.get('has_engine_launched'))}")
        print(f"正向對照 TUNNEL ACQUIRED    {yn(r.get('has_tunnel_acquired'))}")
        if r.get("log_present") and not r.get("has_engine_launched"):
            print("  → 日誌有內容卻沒有開機痕跡：可能是被截斷的視窗，也可能還沒跑到 —— "
                  "請人工看一眼，這個探針不猜。")
    if "negative_control_status" in r:
        print(f"負向對照（不存在的 slug）   HTTP {val('negative_control_status')}"
              f"  {'（不是 200，符合預期）' if r.get('negative_control_is_not_200') else '（是 200 —— 三態設計會被騙）'}")
    print()
    print(f"判讀                {r['verdict']}" + (f" —— {r['why']}" if r.get("why") else ""))


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, which here means "could not read the log".

    Two different failures sharing one code is the kind of collapse this whole
    cut exists to avoid, so a usage error gets its own."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(3)


def main(argv: list[str]) -> int:
    p = _Parser(
        prog="probe_kernel_log_url.py",
        description="讀 Kaggle kernel log，確認裡面有通道網址（不印出網址）。",
        add_help=False,
    )
    p.add_argument("--kernel", metavar="OWNER/SLUG", default="",
                   help="要查的 kernel（預設取自 endpoint 設定）")
    p.add_argument("--timeout", type=int, default=10, metavar="N", help="HTTP 逾時秒數（預設 10）")
    p.add_argument("--json", action="store_true", help="只印 JSON，供機器讀")
    p.add_argument("--negative-control", action="store_true",
                   help="多打一次不存在的 slug，確認它不會回 200")
    p.add_argument("-h", "--help", action="store_true", help="印出這份說明")
    args = p.parse_args(argv[1:])

    if args.help:
        print(__doc__.strip())
        return 0

    report, code = collect(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        render(report)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
