#!/usr/bin/env python3
"""Behavioural verifier for the plaintext API-key broadcast (D-067).

    python3 scripts/test_endpoint_apikey_broadcast_fixes.py <site-packages root>
    python3 scripts/test_endpoint_apikey_broadcast_fixes.py <engine.py> <commands.py>

WHY THIS EXISTS. `_startup()` calls

    _broadcast(f"APIKEY:{_get_api_key()}")

and `_broadcast` POSTs `STATUS: [<session>] <msg>` to a *public* ntfy topic whose
name is derived from the public Kaggle username (`core.py:554-556`). So the
running GPU endpoint published its own API key in cleartext, to a topic anyone
who knows the account can reconstruct. Measured end-to-end on 2026-09-28.

The key is not the only way in -- `GET /v1/apikey` is in the auth middleware's
bypass set (`engine.py:1624`), so the tunnel URL alone yields the key in one
unauthenticated request. This patch removes the one link in that chain which
needs no prior knowledge of the tunnel at all. It does not close the chain; that
is cut B.

UPDATE 2026-09-29 (D-070). The fourth cut removed `/v1/apikey` from that bypass
set, so the paragraph above describes the state this patch was written against,
not the state of an installed tree today. Two consequences are handled below
rather than left to surprise a reader: check B's marker moved off the deleted
`APIKEY:` branch, and check C's premise -- that the CLI's HTTP fallback must
keep working -- was deliberately reversed by the fourth cut. Check C's
assertions still guard something real (the route and `_get_api_key` must survive
as an authenticated introspection route), so they stay; only the reason changed.

WHAT IS DRIVEN, AND WHY NOT import. The payload is real Python, so functions are
sliced out by AST and exec'd against fakes. Importing `engine.engine` is not an
option: it binds FastAPI routes, builds a real settings object out of
/kaggle/working, and starts a thread pool -- and `_broadcast` posts to the live
topic. The apply script runs this verifier twice per cycle, so a verifier with
side effects would publish to ntfy on every dry run. Nothing here touches the
network, the filesystem, or a subprocess.

WHAT EACH CHECK IS FOR.

  A  The bytes that would actually leave. Not "the call was removed" but "the
     POST body does not contain the key". `_broadcast` is exec'd for real and
     its `requests.post` is captured, so a positive control (STARTING... and
     WAITING FOR MODEL... are both present) proves the payload was really
     produced rather than skipped.

  B  The precondition that makes the deletion SAFE, asserted rather than
     assumed. `run_boot()`'s 600s wait loop breaks on TUNNEL ACQUIRED and only
     there; the `APIKEY:` branch never breaks or returns. If a future
     endpoint-vps makes that branch exit the loop, deleting the broadcast would
     hang boot until the timeout. This check fails the moment that stops being
     true. NOTE: it passes on the pristine file too -- it is a guard, not the
     bug.

  C  The route and its helper must survive. This began as "the key must still be
     reachable by the CLI", resting on `run_boot`'s HTTP fallback
     (`commands.py:1454` -> `client.get_api_key()` -> `GET /v1/apikey`). The
     fourth cut removed that fallback on purpose (D-070) -- it could no longer
     succeed once the bypass closed -- so the *stated reason* is now reversed
     while the assertions stay: the route must still exist and still return
     `_get_api_key()`, and `_get_api_key` must not become an orphan. Those are
     still worth guarding; they just guard introspection now, not healing. The
     "the fallback is gone" half is asserted by the fourth cut's own verifier.

NOTE ON THE VERDICT: a pristine engine FAILS check A -- that is the bug being
demonstrated. Run it in both directions: the patched file must pass, the
pristine file must fail. A verifier that passed against both would be checking
nothing. scripts/apply-endpoint-apikey-broadcast-fixes.sh does exactly that.

Exit 0 only if every check passed, 1 if any failed, 2 on usage error.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# A canary that is deliberately NOT key-shaped: this string is written to be
# recognised, and printing it must never look like printing a credential.
CANARY = "CANARY_not-a-real-key_0123456789abcdef"


# --------------------------------------------------------------------------
# Slicing out of the files under test
# --------------------------------------------------------------------------


def _source(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _slice(src: str, name: str) -> str:
    """Return the source text of a top-level function."""
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            if seg is None:  # pragma: no cover - needs a malformed file
                raise AssertionError(f"cannot slice {name}()")
            return seg
    raise AssertionError(f"{name}() not found")


def _constants(node: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _if_branch(scope: ast.AST, marker: str) -> ast.If | None:
    """The `if ...: <marker> ...` branch inside `scope`, by its string literal."""
    for node in ast.walk(scope):
        if isinstance(node, ast.If) and marker in _constants(node.test):
            return node
    return None


def _exits(node: ast.stmt) -> tuple[int, int]:
    """Count break/return statements under `node`, not descending into nested
    function scopes (a nested def's `return` is not this branch's exit)."""

    class _Counter(ast.NodeVisitor):
        def __init__(self) -> None:
            self.breaks = 0
            self.returns = 0

        def visit_FunctionDef(self, n: ast.AST) -> None:  # noqa: N802
            pass

        def visit_AsyncFunctionDef(self, n: ast.AST) -> None:  # noqa: N802
            pass

        def visit_Lambda(self, n: ast.AST) -> None:  # noqa: N802
            pass

        def visit_Break(self, n: ast.AST) -> None:  # noqa: N802
            self.breaks += 1

        def visit_Return(self, n: ast.AST) -> None:  # noqa: N802
            self.returns += 1

    c = _Counter()
    c.visit(node)
    return c.breaks, c.returns


def _boot_loop(src: str) -> ast.While:
    """`run_boot`'s wait loop: the `while ... < 600` that holds the APIKEY branch.

    There are two other `while time.time() - start < N` loops in commands.py, so
    the marker alone is not enough -- the loop that contains the tunnel branch is
    the one that matters.

    2026-09-29 (D-070). The marker used to be the `APIKEY:` branch. The fourth
    cut deleted that branch -- dead since D-067 took away its producer -- so this
    now anchors on `TUNNEL ACQUIRED:`, which the fourth cut deliberately keeps.
    The lesson is worth keeping in view: anchoring a locator on something a later
    patch removes is what made this break, and it broke by raising, not by
    failing a check. The tunnel branch is also the more honest marker, since it
    is the loop's actual exit condition.
    """
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.While) and 600 in {
            n.value for n in ast.walk(node.test) if isinstance(n, ast.Constant)
        }:
            if any("TUNNEL ACQUIRED:" in _constants(sub.test)
                   for sub in ast.walk(node) if isinstance(sub, ast.If)):
                return node
    raise AssertionError("run_boot's wait loop not found")


# --------------------------------------------------------------------------
# Fakes -- `_broadcast` is exec'd for real, its transport is captured
# --------------------------------------------------------------------------


class FakePool:
    """Runs the submitted callable immediately, like a pool with one worker that
    is never busy."""

    def __init__(self) -> None:
        self.jobs = 0

    def submit(self, fn):
        self.jobs += 1
        fn()


class FakeRequests:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post(self, url, data=None, timeout=None, **kw):
        self.calls.append((url, (data or b"").decode("utf-8", "replace")))


class FakeThread:
    def __init__(self, target=None, daemon=None, **kw):
        self.target = target

    def start(self) -> None:
        return None


class FakeThreading:
    Thread = FakeThread


def run_startup(engine_src: str) -> tuple[list[tuple[str, str]], FakePool]:
    """exec `_broadcast` + `_startup` against fakes and return the POSTs."""
    requests, pool = FakeRequests(), FakePool()
    ns = {
        # _broadcast needs these
        "SIGNAL_TOPIC": "topic-under-test",
        "SESSION_ID": "deadbeef",
        "_broadcast_pool": pool,
        "requests": requests,
        # _startup needs these
        "_get_api_key": lambda: CANARY,
        "ensure_dirs": lambda: None,
        "threading": FakeThreading,
        "telemetry_loop": lambda: None,
        "_auto_load_model": lambda: None,
    }
    src = _slice(engine_src, "_broadcast") + "\n\n" + _slice(engine_src, "_startup")
    exec(compile(src, "<emitted engine _broadcast/_startup>", "exec"), ns)
    ns["_startup"]()
    return requests.calls, pool


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def check_payload(engine: Path, fails: list[str]) -> None:
    print("  檢查 A：_startup() 實際送出的 POST 內容")

    calls, pool = run_startup(_source(engine))
    bodies = [body for _url, body in calls]
    leaked = [b for b in bodies if CANARY in b]

    print(f"    送出的訊息              {len(bodies):>3} 則")
    print(f"    其中含金鑰              {len(leaked):>3} 則")

    # Positive control: the harness must have actually run the function. Without
    # this, a broken slice would report "0 messages, no leak" -- a pass.
    control = [b for b in bodies if "STARTING..." in b or "WAITING FOR MODEL..." in b]
    if not control:
        print("    ✗ 正向對照失敗：連 'STARTING...' 都沒送出去 —— 測試環境本身壞了，"
              "不是金鑰沒外洩")
        fails.append("harness broken: _startup() produced no known-good broadcast")
        return
    print(f"    正向對照（STARTING…）   {len(control):>3} 則  ✓")

    if leaked:
        print(f"    ✗ 有 {len(leaked)} 則發布含明文金鑰")
        print(f"      實際內容形如：{leaked[0][:40]!r}…")
        fails.append("_startup() publishes the API key in cleartext")
    else:
        print("    ✓ 沒有任何一則發布含有金鑰")
    print()


def check_boot_safety(engine: Path, commands: Path, fails: list[str]) -> None:
    print("  檢查 B：刪掉那則廣播之後，boot 不會等到逾時（承重前提）")

    loop = _boot_loop(_source(commands))
    key_branch = _if_branch(loop, "APIKEY:")
    tunnel_branch = _if_branch(loop, "TUNNEL ACQUIRED:")

    if key_branch is None:
        # 2026-09-29 (D-070)。第四刀把這個分支刪掉了（它自 D-067 移除產生端之後
        # 就是死的）。守衛的對象消失，不等於可以放行 —— 那會讓這一條變成在檢查
        # 空氣。改成驗退場**乾淨**：這個訊號兩端都不存在，才算前提消滅。
        #
        # 只拆一半會在這裡紅：有人還在等一則永遠不會來的訊息，而外面看不出來。
        # 那正是 D-060 的形狀，也是這一刀當初被要求「一起清」的理由。
        print("    → APIKEY: 分支已於 D-070 退場，改驗訊號兩端皆已不存在")
        stray = [
            f"{label}:{n.lineno}"
            for label, path in (("engine.py", engine), ("commands.py", commands))
            for n in ast.walk(ast.parse(_source(path)))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "APIKEY:" in n.value
        ]
        if stray:
            print(f"    ✗ 分支不見了，但訊號的另一端還在（{'、'.join(stray)}）"
                  " —— 有人還在等一則永遠不會來的訊息")
            fails.append("APIKEY: branch is gone but the signal is still referenced")
        else:
            print("    ✓ 分支已退場，且訊號兩端都不存在 —— 承重前提消滅，守衛一併退場")
        print()
        return
    if tunnel_branch is None:
        print("    ✗ 找不到 TUNNEL ACQUIRED: 分支 —— 前提無從確認")
        fails.append("TUNNEL ACQUIRED: branch not found in run_boot's wait loop")
        return

    kb, kr = _exits(key_branch)
    tb, tr = _exits(tunnel_branch)

    if kb or kr:
        print(f"    ✗ APIKEY: 分支會離開等待迴圈（break ×{kb}、return ×{kr}）——"
              " 砍掉那則廣播會讓 boot 空等到 600 秒逾時")
        fails.append("APIKEY: branch exits the wait loop; deleting the broadcast would hang boot")
    else:
        print("    ✓ APIKEY: 分支不 break、不 return（只 continue 到下一則訊號）")

    if tb:
        print(f"    ✓ TUNNEL ACQUIRED: 分支有 break ×{tb} —— 成功路徑仍然會收工")
    else:
        print("    ✗ TUNNEL ACQUIRED: 分支沒有 break —— 成功路徑不會結束迴圈")
        fails.append("TUNNEL ACQUIRED: branch no longer breaks; boot could never succeed")
    print()


def check_key_still_reachable(engine: Path, fails: list[str]) -> None:
    # 這一行原本印「金鑰仍然拿得到（CLI 的 HTTP 退路沒有被這刀砍掉）」。第四刀
    # 之後那句話是**反的** —— 退路是被刻意拿掉的。標頭改成描述真正被斷言的東西，
    # 否則下一個讀者會在兩個版本之間讀到同一句已經不成立的話。
    print("  檢查 C：路由與 _get_api_key 都還在（守衛；2026-09-29 起不含退路）")

    tree = ast.parse(_source(engine))

    route = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if "/v1/apikey" in _constants(dec):
                    route = node
                    break

    if route is None:
        print("    ✗ 找不到 GET /v1/apikey 路由 —— CLI 的自我修復會失效")
        fails.append("GET /v1/apikey route is gone; the CLI's HTTP fallback would break")
    else:
        returns_key = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_get_api_key"
            for n in ast.walk(route)
        )
        if returns_key:
            print("    ✓ GET /v1/apikey 還在，而且仍然回傳 _get_api_key()")
        else:
            print("    ✗ GET /v1/apikey 還在，但已經不回傳金鑰了")
            fails.append("GET /v1/apikey no longer returns the key")

    # _get_api_key must not become an orphan: the auth middleware still needs it.
    uses = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_get_api_key"
    )
    if uses >= 2:
        print(f"    ✓ _get_api_key 仍有 {uses} 個呼叫點（認證 ＋ 路由）")
    else:
        print(f"    ✗ _get_api_key 只剩 {uses} 個呼叫點 —— 這一刀砍過頭了")
        fails.append("_get_api_key lost its other callers")
    print()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def resolve(argv: list[str]) -> tuple[Path, Path] | None:
    args = argv[1:]
    if len(args) == 1:
        root = Path(args[0])
        if not root.is_dir():
            return None
        return root / "engine" / "engine.py", root / "endpoint" / "commands.py"
    if len(args) == 2:
        return Path(args[0]), Path(args[1])
    return None


def main(argv: list[str]) -> int:
    paths = resolve(argv)
    if paths is None:
        print(
            "用法：test_endpoint_apikey_broadcast_fixes.py <site-packages root>\n"
            "      test_endpoint_apikey_broadcast_fixes.py <engine.py> <commands.py>",
            file=sys.stderr,
        )
        return 2
    engine, commands = paths
    for p in (engine, commands):
        if not p.is_file():
            print(f"找不到檔案：{p}", file=sys.stderr)
            return 2

    print("=" * 72)
    print(f"engine.py    {engine}")
    print(f"commands.py  {commands}")
    print("=" * 72)

    fails: list[str] = []
    for label, fn in (
        ("A", lambda: check_payload(engine, fails)),
        ("B", lambda: check_boot_safety(engine, commands, fails)),
        ("C", lambda: check_key_still_reachable(engine, fails)),
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
