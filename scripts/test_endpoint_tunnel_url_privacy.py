#!/usr/bin/env python3
"""Behavioural verifier for cut B: the tunnel URL leaves the public ntfy topic (D-068).

    python3 scripts/test_endpoint_tunnel_url_privacy.py <site-packages root>
    python3 scripts/test_endpoint_tunnel_url_privacy.py <notebook> <core.py> <commands.py>

WHY THIS EXISTS. The ntfy control topic is derived from the *public* Kaggle
username, so anything posted there is public. The publisher used to post the
Cloudflare tunnel URL -- both base64-encoded as `WS:<url>` and inline in
`TUNNEL ACQUIRED: <url>` -- and the reader (`get_tunnel_url`) decoded it straight
back out. Cut B stops publishing it: the URL now goes only to the Kaggle kernel
log, which needs credentials to read, and the topic carries a value-free
`TUNNEL ACQUIRED:`.

WHAT IS DRIVEN, AND WHY NOT import. The reader is real Python, so it is sliced
out of core.py by AST and exec'd against fakes -- a fake `requests`, a fake
`get_kaggle_token`, and a fake `console`. Importing the package is not an option:
it would bind `endpoint.core` to the *installed* tree rather than the tree under
test, construct a real Config (the user's own config file), and give the reader a
real `requests` that would call Kaggle with the user's real token. The apply
script runs this verifier three times per cycle, so a verifier with side effects
would hit the Kaggle API on every dry run. Nothing here touches the network, the
filesystem (beyond reading the tree under test), or a subprocess -- with the one
deliberate exception of check A, which runs a sibling verifier locally.

THE HEADER ASSERTION IS A MEASUREMENT, NOT A STYLE CHOICE. `X-Kaggle-Authorization`
returns HTTP 403 against `ListKernelSessionOutput`; only `Authorization: Bearer`
works (measured 2026-09-29 against a finished kernel). Check D asserts the header
name, because a plausible-looking refactor to the other spelling would silently
turn every read into "unknown".

HOW TO READ THE RESULT. Checks B, D and E are *discriminators*: a pristine tree
fails all three. Checks A and C are *guards*: they pass in both directions by
design, because they pin behaviour that must survive the change.

  * Check A re-runs the cut-A verifier and requires the guard file to be
    byte-identical to what shipped with D-067. Cut B deliberately keeps the
    literal `TUNNEL ACQUIRED:` and its `break` so that guard needs no edit --
    this check is what makes that claim machine-verifiable rather than a promise.
  * Check C pins the loaded gun: `_broadcast_tunnel()` would put the URL back on
    the public topic, and it is safe today only because `start_tunnel()` (its
    only caller) has no caller itself. That is upstream's code, not ours.

Run it in both directions: the patched tree must pass, the pristine tree must
fail. A verifier that passed against both would be checking nothing.
scripts/apply-endpoint-tunnel-url-privacy.sh does exactly that.

Exit 0 only if every check passed, 1 if any failed, 2 on usage error.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

# The cut-A verifier as shipped in D-067 (commit 42c5f75). Pinned so that "we did
# not touch the guard" is checkable: if this hash moves, someone edited the guard
# to accommodate cut B, and its independence is gone.
CUT_A_VERIFIER_SHA = "05a3e7ac41064f8023f8aacfcc923c24f670b9efb2fce722b5e353ca24e8884d"
CUT_A_VERIFIER_NAME = "test_endpoint_apikey_broadcast_fixes.py"

# Not a real tunnel: `_CF_URL_PATTERN` accepts [a-zA-Z0-9-]+, and `.trycloudflare.com`
# is the only part that has to look right.
CANARY = "https://canary-not-a-real-tunnel.trycloudflare.com"
CANARY_2 = "https://second-canary-not-real.trycloudflare.com"


# ── helpers ────────────────────────────────────────────────────────────────


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _segment(path: Path, names: set[str]) -> str:
    """Source of the top-level statements defining *names*, in file order."""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    out: list[str] = []
    for node in ast.parse(src).body:
        hit = (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in names
        ) or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets
            )
        )
        if hit:
            out.extend(lines[node.lineno - 1 : node.end_lineno])
    return "".join(out)


def _emitted_text(path: Path) -> str:
    """Approximate what the notebook generator emits, from its string constants.

    The generator stores each line of the target cell as its own string literal,
    so joining every string constant in *source order* reconstructs the emitted
    output closely enough for substring assertions -- and, unlike grepping the
    raw file, it cannot be satisfied by text sitting in a comment.
    """
    consts = [
        n
        for n in ast.walk(_tree(path))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    consts.sort(key=lambda n: (n.lineno, n.col_offset))
    return "".join(n.value for n in consts)


def _calls_to(path: Path, func_name: str) -> list[tuple[str, int]]:
    """(enclosing top-level function name, lineno) for every call to *func_name*.

    Module-level calls are attributed to "<module>": a call outside any function
    still runs on import, and must not be able to hide from this check.
    """
    tree = _tree(path)
    scopes = [
        (n.lineno, n.end_lineno, n.name)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == func_name
        ):
            continue
        owner = "<module>"
        for lo, hi, name in scopes:
            if lo <= node.lineno <= hi:
                owner = name
                break
        hits.append((owner, node.lineno))
    return hits


# ── check A: cut A's guard is untouched ────────────────────────────────────


def check_cut_a_guard_intact(script_dir: Path, root: Path, fails: list[str]) -> None:
    """The D-067 guard file is unedited, and still passes against this tree."""
    sibling = script_dir / CUT_A_VERIFIER_NAME
    if not sibling.is_file():
        fails.append(f"check A: sibling verifier missing: {sibling}")
        print(f"    ✗ 找不到 {CUT_A_VERIFIER_NAME}")
        return

    got = hashlib.sha256(sibling.read_bytes()).hexdigest()
    if got != CUT_A_VERIFIER_SHA:
        fails.append(f"check A: cut-A verifier was edited (sha {got})")
        print("    ✗ 切 A 的驗證器被改過了 —— 它的獨立性已經沒了")
        print(f"  → 預期 {CUT_A_VERIFIER_SHA}")
        print(f"  → 實得 {got}")
        return
    print("    ✓ 切 A 的驗證器逐位元未變（獨立性成立）")

    proc = subprocess.run(
        [sys.executable, str(sibling), str(root)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        fails.append(f"check A: cut-A verifier now exits {proc.returncode}")
        print(f"    ✗ 切 A 的驗證器對這棵樹不再通過（結束碼 {proc.returncode}）")
        for line in (proc.stdout or "").strip().splitlines()[-5:]:
            print(f"  → {line}")
        return
    print("    ✓ 切 A 的驗證器對這棵樹仍然通過")


# ── check B: the ntfy URL reader is gone ───────────────────────────────────


def check_no_ntfy_url_reader(core: Path, commands: Path, fails: list[str]) -> None:
    """`get_tunnel_url` must not be defined, and must have no call sites."""
    defined = {
        n.name
        for n in _tree(core).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "get_tunnel_url" in defined:
        fails.append("check B: get_tunnel_url is still defined in core.py")
        print("    ✗ core.py 仍然定義 get_tunnel_url —— 讀公開主題的路還在")
    else:
        print("    ✓ core.py 不再定義 get_tunnel_url")

    for label, path in (("core.py", core), ("commands.py", commands)):
        refs = [
            n.lineno
            for n in ast.walk(_tree(path))
            if isinstance(n, ast.Name) and n.id == "get_tunnel_url"
        ]
        if refs:
            fails.append(f"check B: get_tunnel_url referenced in {label} at {refs}")
            print(f"    ✗ {label} 還在用 get_tunnel_url（行 {refs}）")
        else:
            print(f"    ✓ {label} 沒有任何 get_tunnel_url 參照")


# ── check C: the loaded gun stays unloaded ─────────────────────────────────


def check_broadcast_tunnel_unreachable(engine: Path, fails: list[str]) -> None:
    """`_broadcast_tunnel` may not be reachable, and `start_tunnel` is why.

    Upstream's `start_tunnel()` would put the URL straight back on the public
    topic. It is inert today; this pins *why*, so that wiring it up turns this
    check red instead of silently re-opening the leak.

    Two conditions, and they are not symmetric. Nothing outside `start_tunnel`
    may call `_broadcast_tunnel` -- a caller anywhere else is a path that does
    not run through the dead function below, so the second condition would not
    cover it. And `start_tunnel` itself must have no callers.

    Dropping the call *out of* `start_tunnel` is deliberately allowed: that
    disarms the gun further, and failing on it would punish a safer tree. With
    the call gone, the second condition is what carries the check.

    `main` guarantees *engine* exists before calling this.
    """
    callers = _calls_to(engine, "_broadcast_tunnel")
    strangers = sorted({name for name, _ in callers if name != "start_tunnel"})
    if strangers:
        fails.append(f"check C: _broadcast_tunnel is now called from {strangers}")
        print(f"    ✗ _broadcast_tunnel 多了 start_tunnel 以外的呼叫者：{strangers}")
        print("  → 那條路不經過 start_tunnel，下面的「沒人呼叫」保護不到它")
        return
    if callers:
        print("    ✓ _broadcast_tunnel 除了 start_tunnel 沒有別的呼叫者")
    else:
        print("    ✓ _broadcast_tunnel 沒有任何呼叫者（start_tunnel 也不再呼叫它）")

    st = _calls_to(engine, "start_tunnel")
    if st:
        fails.append(f"check C: start_tunnel is now called from {st}")
        print(f"    ✗ start_tunnel 現在有人呼叫了：{st} —— 網址會回到公開主題上")
        return
    print("    ✓ start_tunnel 仍然沒有呼叫者（那把槍還是沒上膛）")


# ── check D: the kernel-log reader, driven ─────────────────────────────────


class _Resp:
    def __init__(self, status: int, payload: object = None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Requests:
    """Records every call so the header name can be asserted."""

    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None) -> None:
        self.resp = resp
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.exc is not None:
            raise self.exc
        assert self.resp is not None
        return self.resp


class _Console:
    def __init__(self) -> None:
        self.dims: list[str] = []

    def dim(self, msg: str) -> None:
        self.dims.append(msg)


class _Config:
    def __init__(self, kernel_id: str = "owner/slug") -> None:
        self.kernel_id = kernel_id


def _load_reader(core: Path):
    """Slice `get_kernel_log_url` (+ its two helpers) out and exec it against fakes."""
    src = _segment(
        core, {"_CF_URL_PATTERN", "_last_cf_url", "get_kernel_log_url"}
    )
    if "def get_kernel_log_url" not in src:
        return None
    ns: dict = {
        "re": __import__("re"),
        "requests": None,
        "console": None,
        "get_kaggle_token": None,
        "Config": _Config,  # the annotation is evaluated at def time
    }
    exec(compile(src, str(core), "exec"), ns)  # noqa: S102
    return ns


def check_reader_three_states(core: Path, fails: list[str]) -> None:
    """Drive the reader: found / absent / unknown must be three distinct answers."""
    ns = _load_reader(core)
    if ns is None:
        fails.append("check D: get_kernel_log_url not found in core.py")
        print("    ✗ core.py 找不到 get_kernel_log_url")
        return

    reader = ns["get_kernel_log_url"]

    def run(resp=None, exc=None, token="fake-token"):
        ns["get_kaggle_token"] = lambda: token
        ns["requests"] = _Requests(resp=resp, exc=exc)
        ns["console"] = _Console()
        return reader(_Config()), ns["requests"], ns["console"]

    cases = [
        (
            "no credentials -> unknown",
            dict(resp=_Resp(200, {"log": CANARY}), token=None),
            ("unknown", None),
        ),
        (
            "HTTP 403 -> unknown",
            dict(resp=_Resp(403, {})),
            ("unknown", None),
        ),
        (
            "HTTP 500 -> unknown",
            dict(resp=_Resp(500, {})),
            ("unknown", None),
        ),
        (
            "network error -> unknown",
            dict(exc=OSError("boom")),
            ("unknown", None),
        ),
        (
            "200 + url in log -> found",
            dict(resp=_Resp(200, {"log": f"junk\nTUNNEL ACQUIRED: {CANARY}\n"})),
            ("found", CANARY),
        ),
        (
            "200 + no url -> absent",
            dict(resp=_Resp(200, {"log": "engine starting, no tunnel yet\n"})),
            ("absent", None),
        ),
        (
            "200 + log missing -> absent",
            dict(resp=_Resp(200, {})),
            ("absent", None),
        ),
        (
            "200 + two urls -> the LAST one",
            dict(
                resp=_Resp(
                    200,
                    {"log": f"TUNNEL ACQUIRED: {CANARY}\nretry: {CANARY_2}\n"},
                )
            ),
            ("found", CANARY_2),
        ),
    ]

    for label, kwargs, want in cases:
        got, _, _ = run(**kwargs)
        if got == want:
            print(f"    ✓ {label}: {got}")
        else:
            fails.append(f"check D [{label}]: got {got}, want {want}")
            print(f"    ✗ {label}: 實得 {got}，應為 {want}")

    # Positive control: the two "we asked and got nothing" states must differ from
    # each other and from "found". If they collapsed, the cases above would pass
    # while the caller still could not tell "no URL yet" from "could not ask".
    found, _, _ = run(resp=_Resp(200, {"log": f"x {CANARY}\n"}))
    absent, _, _ = run(resp=_Resp(200, {"log": "nothing here\n"}))
    unknown, _, _ = run(resp=_Resp(403, {}))
    if len({found[0], absent[0], unknown[0]}) == 3:
        print("    ✓ 三個狀態彼此互異（absent 不等於 unknown）")
    else:
        fails.append(f"check D: states collapsed: {found[0]}, {absent[0]}, {unknown[0]}")
        print("    ✗ 三個狀態有塌陷 —— 呼叫端分不出「還沒有」與「問不到」")

    # The header spelling is load-bearing (X-Kaggle-Authorization returns 403).
    _, req, _ = run(resp=_Resp(200, {"log": CANARY}))
    assert req.calls, "the reader made no request at all"
    headers = req.calls[0]["headers"] or {}
    if "Authorization" in headers and "X-Kaggle-Authorization" not in headers:
        print("    ✓ 用的是 Authorization: Bearer（X-Kaggle-Authorization 會 403）")
    else:
        fails.append(f"check D: wrong auth header: {sorted(headers)}")
        print(f"    ✗ 認證標頭不對：{sorted(headers)}")

    url = req.calls[0]["url"]
    if url.endswith("ListKernelSessionOutput"):
        print(f"    ✓ 打的是 {url.rsplit('/', 1)[-1]}")
    else:
        fails.append(f"check D: unexpected endpoint: {url}")
        print(f"    ✗ 端點不對：{url}")


# ── check E: the publisher stops publishing the URL ────────────────────────


def check_publisher(notebook: Path, fails: list[str]) -> None:
    """The topic must lose the URL, and the kernel log must keep it."""
    if not notebook.is_file():
        fails.append(f"check E: generator not found at {notebook}")
        print(f"    ✗ 找不到 {notebook}")
        return

    emitted = _emitted_text(notebook)

    # The base64 `WS:<url>` blob that get_tunnel_url used to decode.
    if "broadcast(" in emitted:
        fails.append("check E: the generator still emits a broadcast() call")
        print("    ✗ 產生器仍會發出 broadcast( —— base64 的 WS: 那則還在")
    else:
        print("    ✓ 產生器不再發出 broadcast()（base64 的 WS: 那則已消失）")

    if "topic_only=True" in emitted:
        print("    ✓ 值為空的訊號走 topic_only=True")
    else:
        fails.append("check E: topic_only=True not emitted")
        print("    ✗ 產生器沒有發出 topic_only=True")

    # Positive control: the one-shot signal must survive. Delete too much and boot
    # never learns the tunnel came up -- a worse failure than the leak this cut
    # closes. The probes are ordered so the message names which form was found
    # rather than asserting a consequence that may not be the one that happened.
    if "signal('TUNNEL ACQUIRED:', topic_only=True)" in emitted:
        print("    ✓ 主題上仍收到 TUNNEL ACQUIRED:（值為空）")
    else:
        if "signal(f'TUNNEL ACQUIRED: {tunnel_url}'" in emitted:
            why = "主題上的 TUNNEL ACQUIRED: 仍帶著網址"
        elif "signal('TUNNEL ACQUIRED:'" in emitted:
            why = "TUNNEL ACQUIRED 的訊號形式變了（不是值為空的那一版）"
        elif "TUNNEL ACQUIRED" in emitted:
            why = "TUNNEL ACQUIRED 不再由 signal() 送出 —— boot 會等不到"
        else:
            why = "TUNNEL ACQUIRED 整個不見了 —— boot 會等不到"
        fails.append(f"check E: {why}")
        print(f"    ✗ {why}")

    if "print(f'TUNNEL ACQUIRED: {tunnel_url}', flush=True)" in emitted:
        print("    ✓ 網址仍會明確 print 到 kernel log（只是不上主題）")
    else:
        fails.append("check E: no explicit print of the URL into the kernel log")
        print("    ✗ 沒有看到把網址寫進 kernel log 的那一行 print(...)")


# ── wiring ─────────────────────────────────────────────────────────────────


def resolve(argv: list[str]) -> tuple[Path, Path, Path, Path] | None:
    script_dir = Path(__file__).resolve().parent
    if len(argv) == 2:
        root = Path(argv[1])
        return (
            script_dir,
            root / "scripts" / "master_build_notebook.py",
            root / "endpoint" / "core.py",
            root / "endpoint" / "commands.py",
        )
    if len(argv) == 4:
        return (script_dir, Path(argv[1]), Path(argv[2]), Path(argv[3]))
    return None


def main(argv: list[str]) -> int:
    paths = resolve(argv)
    if paths is None:
        print(
            "用法：test_endpoint_tunnel_url_privacy.py <site-packages root>\n"
            "      test_endpoint_tunnel_url_privacy.py <notebook> <core.py> <commands.py>",
            file=sys.stderr,
        )
        return 2
    script_dir, notebook, core, commands = paths
    for p in (notebook, core, commands):
        if not p.is_file():
            print(f"找不到檔案：{p}", file=sys.stderr)
            return 2

    # engine/engine.py sits above core.py. It is not one of this patch's targets
    # (cut A owns that file), but check C reads it and check A re-runs the cut-A
    # verifier, which needs both engine/ and endpoint/. A partial tree is a usage
    # error, not a silent skip: half the checks would quietly not run.
    root = core.parent.parent
    engine = root / "engine" / "engine.py"
    if not engine.is_file():
        print(
            f"找不到 {engine}\n"
            "這支驗證器需要一棵完整的 site-packages 樹：check C 要讀 engine/engine.py，\n"
            "check A 要重跑切 A 的驗證器（它同時需要 engine/ 與 endpoint/）。",
            file=sys.stderr,
        )
        return 2

    print("=" * 72)
    print(f"notebook     {notebook}")
    print(f"core.py      {core}")
    print(f"commands.py  {commands}")
    print("=" * 72)

    fails: list[str] = []
    for label, fn in (
        ("A", lambda: check_cut_a_guard_intact(script_dir, root, fails)),
        ("B", lambda: check_no_ntfy_url_reader(core, commands, fails)),
        ("C", lambda: check_broadcast_tunnel_unreachable(engine, fails)),
        ("D", lambda: check_reader_three_states(core, fails)),
        ("E", lambda: check_publisher(notebook, fails)),
    ):
        before = len(fails)
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            # A SystemExit escaping from the code under test would make this
            # verifier exit 2 on its own -- which the apply script reads as
            # "verifier failed", the right verdict for the wrong reason, and it
            # would equally mask a broken patched run. Make it loud instead.
            if isinstance(exc, KeyboardInterrupt):
                raise
            print(f"    ✗ 檢查 {label} 無法執行：{type(exc).__name__}: {exc}")
            fails.append(f"check {label} could not run ({type(exc).__name__}: {exc})")
            if len(fails) == before:
                fails.append(f"check {label} aborted")
            print()

    print("=" * 72)
    if fails:
        print(f"  → {len(fails)} 項未過")
        for f in fails:
            print(f"✗ {f}")
        return 1
    print("  → 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
