#!/usr/bin/env python3
"""Behavioural verifier for the fourth cut (D-070).

    python3 scripts/test_endpoint_apikey_selfheal.py <site-packages root>
    python3 scripts/test_endpoint_apikey_selfheal.py <engine.py> <commands.py>

WHY THIS EXISTS. `/v1/apikey` returns the API key (`engine.py:1931`) and sat in
the auth middleware's unauthenticated bypass set (`engine.py:1624`). So anyone
holding the tunnel URL could read the key back in one request. Cuts A/B/C took
the key and the URL off the public ntfy topic; this cut closes the last link
that needs only the URL.

THE COST OF THAT CUT, AND WHY THE OBVIOUS VERSION OF IT IS A TRAP. The CLI had
two self-heal sites that fetched the key from the engine over the tunnel when
the local cache was empty (`commands.py:1484-1490` and `:2636`). Removing the
bypass alone kills them: VPSClient pins the *cached* key into its session
headers (`core.py:1274-1276`), so an empty cache sends no Authorization header
at all and a wrong cache sends the wrong one. Either way `GET /v1/apikey` now
401s -- and the only situation the fallback existed for is exactly the one
where it can no longer succeed. Removing the bypass by itself would therefore
turn a working heal path into a dead one that *looks* alive.

So the cut has two halves and this verifier asserts both: the bypass loses
`/v1/apikey`, AND both heal sites read the authoritative local key
(`identity.api_key` in endpoint-config.yaml -- `core.py:543`) instead of making
a network call that cannot work. Chesterton's fence, checked rather than
assumed: the fence is removed only together with the reason it was there.

WHAT IS DRIVEN, AND WHY NOT import. Pure AST -- no import of `engine.engine`
(binds FastAPI routes, builds settings from /kaggle/working, starts threads) and
no import of `endpoint.commands` (reads the real config and the network). The
apply script runs this verifier twice per cycle, so a verifier with side effects
would touch the live machine on every dry run. Nothing here reads anything but
the two source files.

WHAT EACH CHECK IS FOR.

  A  The bypass set no longer contains `/v1/apikey`. Positive control: the set
     still has to contain the other five entries, so "the assignment was
     deleted" cannot pass as "the entry was removed".

  B  The route itself survives and still returns `_get_api_key()`. This is the
     guard that cut A's verifier also carries; keeping it here means the fourth
     cut cannot quietly delete a route that other tooling documents. A guard --
     it passes on the pristine file too.

  C  Both heal sites read the authoritative key first. Not "the network call was
     deleted" but "the value now comes from `config.api_key`": the assignment is
     asserted to be an `or` whose leftmost operand is `config.api_key`. Also
     asserts `get_api_key()` is called nowhere in commands.py -- a heal path
     restored by a later patch would fail here.

  D  When there is no local key, both heal sites fail LOUDLY. The pristine
     `_register_with_proxy` ends in a bare `if not api_key: return` with no
     message at all -- "couldn't find a key" read as "nothing wrong", the
     D-060 defect shape. This check requires a console call in that branch.

  E  The `APIKEY:` signal has no producer and no consumer. Cut A removed the
     producer; this cut removes the consumer that was left reading a message
     nobody sends (`commands.py:1281`, dead since D-067). Asserting BOTH ends
     are gone is what makes the retirement checkable -- a half-removed signal
     fails here instead of looking tidy.

  F  The 401 advice still points somewhere real. It used to say "run 'endpoint
     register' for a fresh key"; after this cut `register` no longer fetches a
     key, so that instruction would name an action that cannot do what it
     claims. A patch must not leave a false instruction behind.

NOTE ON THE VERDICT: the pristine files FAIL A, C, D, E and F -- those are the
things being fixed. B passes on both. Run it in both directions: patched must
pass, pristine must fail. A verifier that passed against both would be checking
nothing; scripts/apply-endpoint-apikey-selfheal.sh does exactly that.

Exit 0 only if every check passed, 1 if any failed, 2 on usage error.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

HEAL_SITES = ("_register_with_proxy", "run_register")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path))


def _constants(node: ast.AST) -> set[str]:
    """Every string literal inside `node`, however deeply nested."""
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _func(tree: ast.Module, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _dotted(node: ast.AST) -> str:
    """`config.api_key` -> "config.api_key"; anything else -> ""."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _names_assigned(node: ast.AST, target: str) -> list[ast.AST]:
    """Values assigned to `target` anywhere inside `node`."""
    out: list[ast.AST] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == target for t in n.targets
        ):
            out.append(n.value)
    return out


# --------------------------------------------------------------------------
# A -- the bypass set
# --------------------------------------------------------------------------


def check_bypass(engine: Path, fails: list[str]) -> None:
    print("  檢查 A：`/v1/apikey` 離開免認證清單")

    middleware = _func(_tree(engine), "_auth_middleware")
    if middleware is None:
        print("    ✗ 找不到 _auth_middleware")
        fails.append("_auth_middleware not found")
        print()
        return

    assigned = _names_assigned(middleware, "bypass")
    if not assigned:
        print("    ✗ _auth_middleware 裡找不到 bypass 指派")
        fails.append("bypass assignment not found")
        print()
        return

    value = assigned[0]
    if not isinstance(value, ast.Set):
        print(f"    ✗ bypass 不再是集合（{type(value).__name__}）")
        fails.append("bypass is no longer a set literal")
        print()
        return

    entries = _constants(value)
    if "/v1/apikey" in entries:
        print("    ✗ `/v1/apikey` 還在免認證清單裡 —— 這一刀沒有生效")
        fails.append("/v1/apikey is still in the auth bypass set")
    elif not {"/health", "/metrics", "/tunnel"} <= entries:
        # 正向對照：證明我們找到的是真的那個集合，不是被刪空或認錯的物件。
        print(f"    ✗ 清單裡少了其他本來就該在的項目：{sorted(entries)}")
        fails.append("bypass set lost entries it should still have")
    else:
        print(f"    ✓ 清單剩 {sorted(entries)}，`/v1/apikey` 不在裡面")
    print()


# --------------------------------------------------------------------------
# B -- the route survives (guard; passes on pristine too)
# --------------------------------------------------------------------------


def check_route_kept(engine: Path, fails: list[str]) -> None:
    print("  檢查 B：路由本身還在，而且仍然回傳 _get_api_key()（守衛）")

    tree = _tree(engine)
    route = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if "/v1/apikey" in _constants(dec):
                    route = node
                    break

    if route is None:
        print("    ✗ GET /v1/apikey 路由不見了 —— 這一刀砍過頭")
        fails.append("/v1/apikey route was deleted")
    elif not any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_get_api_key"
        for n in ast.walk(route)
    ):
        print("    ✗ 路由還在，但不再回傳金鑰")
        fails.append("/v1/apikey no longer returns the key")
    else:
        print("    ✓ 路由還在、仍回傳 _get_api_key()（現在要通過認證才到得了）")
    print()


# --------------------------------------------------------------------------
# C -- both heal sites read the authoritative key
# --------------------------------------------------------------------------


def check_heal_reads_local(commands: Path, fails: list[str]) -> None:
    print("  檢查 C：兩個自癒點都先讀真本 config.api_key")

    tree = _tree(commands)

    for name in HEAL_SITES:
        fn = _func(tree, name)
        if fn is None:
            print(f"    ✗ 找不到 {name}")
            fails.append(f"{name} not found")
            continue

        values = _names_assigned(fn, "api_key")
        if not values:
            print(f"    ✗ {name} 裡找不到 api_key 指派")
            fails.append(f"{name}: no api_key assignment")
            continue

        head = values[0]
        leftmost = head.values[0] if isinstance(head, ast.BoolOp) else head
        if _dotted(leftmost) != "config.api_key":
            print(f"    ✗ {name}：最左邊的來源是 {_dotted(leftmost) or ast.dump(leftmost)[:40]!r}，"
                  "不是 config.api_key")
            fails.append(f"{name} does not read config.api_key first")
        else:
            print(f"    ✓ {name}：api_key = config.api_key or …")

    # 没有任何一條路徑再從 tunnel 取金鑰。
    callers = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "get_api_key"
    ]
    if callers:
        print(f"    ✗ commands.py 還有 {len(callers)} 處呼叫 get_api_key()（行 {callers}）"
              " —— 補丁之後那條路在構造上取不到金鑰")
        fails.append("get_api_key() is still called from commands.py")
    else:
        print("    ✓ commands.py 已無任何 get_api_key() 呼叫")
    print()


# --------------------------------------------------------------------------
# D -- missing key fails loudly
# --------------------------------------------------------------------------


def check_loud_failure(commands: Path, fails: list[str]) -> None:
    print("  檢查 D：沒有本機金鑰時是大聲失敗，不是靜默返回")

    tree = _tree(commands)

    for name in HEAL_SITES:
        fn = _func(tree, name)
        if fn is None:
            fails.append(f"{name} not found")
            continue

        guard = None
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.If)
                and isinstance(n.test, ast.UnaryOp)
                and isinstance(n.test.op, ast.Not)
                and isinstance(n.test.operand, ast.Name)
                and n.test.operand.id == "api_key"
            ):
                guard = n
                break

        if guard is None:
            print(f"    ✗ {name} 裡找不到 `if not api_key` 的守衛")
            fails.append(f"{name}: no `if not api_key` guard")
            continue

        spoke = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"warn", "err"}
            and _dotted(n.func.value) == "console"
            for n in ast.walk(guard)
        )
        # 「有沒有出聲」不夠。原始版的 `if not api_key:` 裡**也**有一句
        # console.warn —— 埋在四次重試的 except 裡，只有四種失敗都走完才印。
        # 那不是「告訴操作者沒有金鑰」，那是「告訴操作者連線失敗」。所以要一併
        # 斷言：缺金鑰的這一支**什麼都不嘗試**。有 for/try 就代表它還在想辦法。
        tries = [
            type(n).__name__
            for n in ast.walk(guard)
            if isinstance(n, (ast.For, ast.While, ast.Try))
        ]

        if tries:
            print(f"    ✗ {name}：`if not api_key` 還在嘗試（{sorted(set(tries))}）"
                  " —— 缺金鑰時應該直接說出來，不是再試一次網路")
            fails.append(f"{name}: the missing-key branch still attempts work")
        elif not spoke:
            print(f"    ✗ {name}：`if not api_key` 的內容沒有告訴操作者任何事"
                  " —— 「查不到」被讀成了「沒事」")
            fails.append(f"{name}: missing key returns silently")
        else:
            print(f"    ✓ {name}：缺金鑰時直接出聲，不嘗試任何動作")

    print()


# --------------------------------------------------------------------------
# E -- the APIKEY: signal is gone from both ends
# --------------------------------------------------------------------------


def check_signal_retired(engine: Path, commands: Path, fails: list[str]) -> None:
    print("  檢查 E：`APIKEY:` 訊號兩端都已不存在（產生端與取用端）")

    for label, path in (("engine.py", engine), ("commands.py", commands)):
        hits = sorted(
            n.lineno
            for n in ast.walk(_tree(path))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "APIKEY:" in n.value
        )
        if hits:
            print(f"    ✗ {label} 還有 `APIKEY:` 字串常數（行 {hits}）—— 訊號只拆了一半")
            fails.append(f"{label} still references the APIKEY: signal")
        else:
            print(f"    ✓ {label}：沒有任何 `APIKEY:` 字串常數")

    print()


# --------------------------------------------------------------------------
# F -- the 401 advice points somewhere real
# --------------------------------------------------------------------------


def check_401_advice(commands: Path, fails: list[str]) -> None:
    print("  檢查 F：401 的提示不再指向做不到的動作")

    tree = _tree(commands)
    warns = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "warn"
        and _dotted(n.func.value) == "console"
    ]

    hit = [w for w in warns if any("Authentication denied" in s for s in _constants(w))]
    if not hit:
        print("    ✗ 找不到「Authentication denied」那則警告")
        fails.append("the 401 warning was not found")
        print()
        return

    # 提示是「警告那一句 ＋ 緊接著的補充」一整組，處方通常寫在 console.dim 裡。
    # 只讀 warn 節點本身會漏掉處方，然後把「寫在下一句」誤判成「沒有寫」。
    # 取「仍然包含這則警告的最小區塊」—— 也就是那個 `if status_code == 401`。
    warn = hit[0]
    blocks = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.If, ast.ExceptHandler))
        and any(x is warn for x in ast.walk(n))
    ]
    block = min(blocks, key=lambda n: sum(1 for _ in ast.walk(n))) if blocks else warn
    text = " ".join(sorted(_constants(block)))
    if "endpoint register" in text:
        print("    ✗ 401 提示仍叫人跑 `endpoint register` —— "
              "補丁之後那條路不會帶回新金鑰")
        fails.append("the 401 advice still points at `endpoint register`")
    elif "endpoint boot" not in text:
        print("    ✗ 401 提示沒有指向任何能修好它的動作")
        fails.append("the 401 advice names no workable remedy")
    else:
        print("    ✓ 401 提示指向 `endpoint boot`（重新烘培金鑰）")
    print()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) == 2:
        root = Path(argv[1])
        engine, commands = root / "engine/engine.py", root / "endpoint/commands.py"
    elif len(argv) == 3:
        engine, commands = Path(argv[1]), Path(argv[2])
    else:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        print("用法：test_endpoint_apikey_selfheal.py <site-packages root>", file=sys.stderr)
        print("      test_endpoint_apikey_selfheal.py <engine.py> <commands.py>", file=sys.stderr)
        return 2

    for p in (engine, commands):
        if not p.is_file():
            print(f"✗ 找不到檔案：{p}", file=sys.stderr)
            return 2

    print("第四刀：`/v1/apikey` 離開免認證清單，而自癒改讀真本（D-070）")
    print(f"  engine   {engine}")
    print(f"  commands {commands}")
    print()

    fails: list[str] = []
    check_bypass(engine, fails)
    check_route_kept(engine, fails)
    check_heal_reads_local(commands, fails)
    check_loud_failure(commands, fails)
    check_signal_retired(engine, commands, fails)
    check_401_advice(commands, fails)

    if fails:
        print(f"✗ {len(fails)} 項未過：")
        for f in fails:
            print(f"    - {f}")
        return 1

    print("✓ 六項全過。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
