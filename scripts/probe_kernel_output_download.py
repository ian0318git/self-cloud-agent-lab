#!/usr/bin/env python3
"""Can a kernel's output file be *downloaded* -- and is the door discriminating?

    scripts/probe_kernel_output_download.py --kernel OWNER/SLUG probe_out/t0.txt
    scripts/probe_kernel_output_download.py --kernel OWNER/SLUG --seconds 120 a.txt b.txt

WHY THIS EXISTS, SEPARATELY FROM probe_kernel_files_live.py. That probe answers
"does the file *list* show anything while the session runs". This one answers a
different question that the list cannot: **are the bytes retrievable**. They are
not the same door -- D-075's option B needs the tunnel URL out of
`logs/cf_engine.log`, and a name in a list is not the URL.

The two halves are complementary, and the running/post-termination pair is the
whole point:

    2026-10-01, session running   -> both real files 404, control 404   (no info)
    2026-10-01, session complete  -> both real files 200, control 404   (discriminates)

Only the second pair separates "the door is shut" from "the instrument cannot
tell". A control path is therefore **always** requested -- a run where every
path 404s proves nothing on its own, and this script never lets you forget it.

WHAT IT NEVER PRINTS. The response body, and the redirect target. That target
carries an opaque Kaggle token in its path (`.../kf/<id>/<JWE>/<file>`); it is
short-lived and scoped to one file, but there is no reason for it to be in a
transcript, so this script prints the *tail* of the redirect path only -- the
part that is just the file name you asked for.

Output per path: the final HTTP status, the byte length, and the first 12 hex of
the body's sha256. The digest is what lets a reader confirm the content without
the content being printed.

Exit codes: 0 ran, 2 could not read at all, 3 usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

import requests

from probe_kernel_log_url import read_token

BASE = "https://www.kaggle.com/api/v1/kernels/output/download/{owner}/{slug}/{path}"
CONTROL = "endpoint-probe-control-does-not-exist.txt"


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, which here means 'could not read'."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: {message}", file=sys.stderr)
        raise SystemExit(3)


def probe(token: str, owner: str, slug: str, path: str, timeout: int) -> str:
    """One path. Returns a single report line; never raises, never prints a body."""
    url = BASE.format(owner=owner, slug=slug, path=path)
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception as exc:
        return f"  {path:<34} {type(exc).__name__}"
    digest = hashlib.sha256(r.content).hexdigest()[:12] if r.status_code == 200 else "—"
    return f"  {path:<34} HTTP {r.status_code}  bytes={len(r.content):<7} sha256[:12]={digest}"


def main(argv: list[str]) -> int:
    p = _Parser(
        prog="probe_kernel_output_download.py",
        description="下載 kernel 的輸出檔，並永遠附一條對照路徑。",
        add_help=False,
    )
    p.add_argument("--kernel", metavar="OWNER/SLUG", required=True,
                   help="要下載哪顆 kernel 的輸出")
    p.add_argument("--timeout", type=int, default=30, metavar="S", help="單次 HTTP 逾時秒數")
    p.add_argument("--no-control", action="store_true",
                   help="不要打對照路徑（**不建議**：那就分不出門關著與儀器壞了）")
    p.add_argument("-h", "--help", action="store_true", help="印出這份說明")
    p.add_argument("paths", nargs="*", metavar="PATH", help="kernel 輸出裡的相對路徑")
    args = p.parse_args(argv[1:])

    if args.help:
        print(__doc__.strip())
        return 0

    if args.kernel.count("/") != 1 or not all(args.kernel.split("/", 1)):
        print("✗ --kernel 要是 OWNER/SLUG。", file=sys.stderr)
        return 3
    owner, slug = args.kernel.split("/", 1)

    token, source = read_token()
    if not token:
        print("✗ 沒有 Kaggle 權杖，讀不了任何東西。", file=sys.stderr)
        return 2

    print(f"kernel={args.kernel}  token_source={source}")
    for path in args.paths:
        print(probe(token, owner, slug, path, args.timeout))
        sys.stdout.flush()
    if not args.no_control:
        print(probe(token, owner, slug, CONTROL, args.timeout))
        print(f"  （最後一條 {CONTROL} 是對照 —— 它不該存在，所以它必須不是 200）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
