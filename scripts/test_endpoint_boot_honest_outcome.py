#!/usr/bin/env python3
"""Behavioural verifier for boot's unwired-endpoint report (D-073).

    python3 scripts/test_endpoint_boot_honest_outcome.py <site-packages root>
    python3 scripts/test_endpoint_boot_honest_outcome.py <commands.py>

WHY THIS EXISTS. On the path measured on 2026-09-30 (D-072), `endpoint boot`
printed

    -> Endpoint IS ONLINE

and exited 0 -- while the endpoint was never wired up. `run_boot` reads the
tunnel URL out of the Kaggle kernel log, and while the session is running that
log is EMPTY: `ListKernelSessionOutput` is the *persisted* log, served only
after the session has ended. So `tunnel_url` stayed `None`, and

    if success:
        if tunnel_url:            # <-- never taken
            _wait_for_model_ready(tunnel_url)
            if config.proxy_enabled:
                _register_with_proxy(config, tunnel_url)
            display_endpoint(config, tunnel_url)

skipped all three. `clear_cached_endpoint()` had already run near the top of
boot, so afterwards neither the proxy nor the cache knew where the tunnel was.
The kernel was genuinely running; the endpoint was not reachable. Two sentences
in the CLI were false on that path:

  * "Tunnel is up, but its URL is not in the kernel log yet." -- "yet" never
    arrives. The URL is only served once the session ends, and the tunnel dies
    with it.
  * "Read it in a moment with: endpoint base-url" -- that command goes
    `resolve_tunnel_url()` -> cache (cleared) -> `get_kernel_log_url()` (empty
    while running), so the prescription cannot be carried out.

This patch does not fix the missing URL -- that is the open question D-072 left
for cut B. It removes the false comfort: the two prescriptions are replaced
with what was measured, and the success path now says out loud that the endpoint
was not wired up, then exits 2.

WHY EXIT 2 AND NOT 1. `sys.exit(1)` in `run_boot` already means "the kernel did
not come up" -- it prints "Boot failed", scrapes the notebook for cell errors
and tells the reader to open Kaggle. Reusing it here would send that reader
hunting notebook errors that do not exist. `run_stop` already established 2 for
"the operation did not reach a determinate good state" (`commands.py:1574`,
"Exiting 2 (cannot determine)"), and `migrate-kaggle-token.sh --check` returns 2
for `unknown`. 2 is that same family: the kernel is up, the wiring is not.

CONSEQUENCE, STATED PLAINLY. Because the URL is currently unreadable on *every*
boot, this patch makes `endpoint boot` exit 2 on *every* boot. That is the
intended effect, not a regression: as long as cut B is unresolved the endpoint
really is unwired after boot, and an exit code of 0 was the part that was wrong.
This verifier does not assert that the exit is reached -- it asserts the report
is *conditional* on the outcome (check D), so that the day cut B lands and the
URL comes back, the honest path is the one that runs.

WHAT IS DRIVEN, AND WHY NOT import. Nothing is imported. `endpoint.commands`
pulls in `requests`, `rich` and a `Config()` that reads the user's endpoint
config; the apply script runs this verifier twice per cycle, so a verifier with
side effects would touch the user's real configuration on every dry run. The
file is parsed instead, and every check is a statement about the shape of the
control flow or about the bytes that would be printed. Nothing here touches the
network, the filesystem (beyond reading the target), or a subprocess.

WHAT EACH CHECK IS FOR.

  A  The two false prescriptions are gone. Not "the text was edited" but "these
     two exact sentences are nowhere in the file". A pristine tree FAILS this --
     that is the bug being demonstrated.

  B  Positive control: the branch must still warn. A and C are both satisfied by
     deleting the TUNNEL branch outright, which would leave the reader with no
     warning at all -- strictly worse than the false one. This check fails the
     moment the branch stops producing warnings, so A cannot be passed by
     subtraction.

  C  `success = True` is still UNCONDITIONAL in the TUNNEL branch -- a direct
     child of that branch's body, not nested under `if tunnel_url:`. This is the
     D-033 guard, and it guards the opposite direction from A. The tempting
     "fix" for the unwired endpoint is to move `success = True` inside the
     `if tunnel_url:` guard, or to flip it to False, so that boot stops claiming
     success. That would be the mirror image of D-033: turning a rule red to
     make a different red go away. The kernel really is running -- `success`
     means exactly that, and the wrong claim was never `success`, it was the
     silence about the wiring. This check fails if a future edit conflates the
     two. NOTE: it passes on the pristine file too; it is a guard, not the bug.

  D  The new report is conditional, on the success path, and does not pre-empt
     the Kaggle upload monitor. Three assertions on the AST of `run_boot`:
     the `sys.exit(2)` must sit inside an `if not tunnel_url:` whose parent is
     the `if success:` body; and that block must come AFTER the
     `_monitor_kaggle_upload` call, so a boot that is still uploading a model
     from Kaggle is not cut short by the report. The `while` loop's own
     `elif kernel_status == "running":` fallback (which reads the URL a second
     time before giving up) must also still exist -- the patch must not have
     removed the last chance to obtain the URL.

  E  Exit codes keep their meanings. `sys.exit(1)` must still be reachable in
     the `if not success:` branch (kernel did not come up), and the new exit
     must be 2. Asserted as a pair so that neither can drift into the other's
     slot.

Exit 0 only if every check passed, 1 if any failed, 2 on usage error.
"""

from __future__ import annotations

import ast
import pathlib
import sys

# The two sentences D-072 measured to be false on the unwired path.
FALSE_PRESCRIPTIONS = (
    "Read it in a moment with: endpoint base-url",
    "Read it later with: endpoint base-url",
)


class Failure(Exception):
    """A check did not hold."""


def find_commands(arg: str) -> pathlib.Path:
    p = pathlib.Path(arg)
    if p.is_file():
        return p
    for rel in ("endpoint/commands.py", "commands.py"):
        cand = p / rel
        if cand.is_file():
            return cand
    raise Failure(f"找不到 commands.py（在 {arg} 底下找過 endpoint/commands.py）")


def find_run_boot(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run_boot":
            return node
    raise Failure("run_boot 不在 commands.py 的最上層")


def ifs_in(fn: ast.FunctionDef) -> list[ast.If]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.If)]


def test_of(src: str, node: ast.If) -> str:
    """The test of *node* as source text, whitespace-collapsed.

    Comparing text rather than walking the AST keeps the checks readable and
    keeps them honest about what a reader would see.
    """
    seg = ast.get_source_segment(src, node.test)
    if seg is None:
        raise Failure("拿不到 AST 節點的原始碼片段")
    return " ".join(seg.split())


def calls_in(fn: ast.FunctionDef, name: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id == name:
                out.append(node)
            elif isinstance(f, ast.Attribute) and f.attr == name:
                out.append(node)
    return out


def exits_in(node: ast.AST) -> list[int]:
    """Exit codes of `sys.exit(<int>)` calls anywhere under *node*."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if not (isinstance(f, ast.Attribute) and f.attr == "exit"):
            continue
        if n.args and isinstance(n.args[0], ast.Constant) and isinstance(
            n.args[0].value, int
        ):
            out.append(n.args[0].value)
    return out


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def check_a(src: str) -> str:
    hits = [s for s in FALSE_PRESCRIPTIONS if s in src]
    if hits:
        for h in hits:
            print(f"    ✗ 假處方還在：{h!r}")
        raise Failure(f"{len(hits)} 條被實測推翻的處方仍留在檔案裡")
    return "兩條被實測推翻的處方都不在了"


def check_b(src: str, fn: ast.FunctionDef) -> str:
    branch = tunnel_branch(src, fn)
    warns = calls_in(branch, "warn")
    if len(warns) < 2:
        raise Failure(
            f"TUNNEL 分支只剩 {len(warns)} 個 warn —— 這個分支被刪掉了，"
            f"讀者會連警告都沒有（比假警告更糟）"
        )
    return f"TUNNEL 分支仍有 {len(warns)} 個警告（沒有被刪掉了事）"


def tunnel_branch(src: str, fn: ast.FunctionDef) -> ast.If:
    for node in ifs_in(fn):
        t = test_of(src, node)
        if "TUNNEL:" in t and "startswith" in t:
            return node
    raise Failure("找不到 TUNNEL 訊號分支")


def check_c(src: str, fn: ast.FunctionDef) -> str:
    branch = tunnel_branch(src, fn)
    for n in branch.body:
        if not isinstance(n, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "success" for t in n.targets):
            continue
        if isinstance(n.value, ast.Constant) and n.value.value is True:
            return "success = True 仍是無條件、且值就是字面上的 True"
        raise Failure(
            "TUNNEL 分支裡對 success 的賦值不再是字面上的 True —— "
            "把為真的『kernel 真的在跑』改成假的失敗，是 D-033 的鏡像。"
        )
    raise Failure(
        "TUNNEL 分支裡找不到直接掛在分支主體上的 `success = True`。"
        "它若不是被移進了 `if tunnel_url:` 底下，就是被改成了別的寫法。"
    )


def check_d(src: str, fn: ast.FunctionDef) -> str:
    success_if = None
    for node in ifs_in(fn):
        if test_of(src, node) == "success":
            success_if = node
            break
    if success_if is None:
        raise Failure("找不到 `if success:` 區塊")

    report = None
    for node in ast.walk(success_if):
        if isinstance(node, ast.If) and test_of(src, node) == "not tunnel_url":
            report = node
            break
    if report is None:
        raise Failure("`if success:` 底下找不到 `if not tunnel_url:` 的收尾宣告")

    if report not in success_if.body:
        raise Failure("`if not tunnel_url:` 不在 `if success:` 的主體裡")
    if 2 not in exits_in(report):
        raise Failure("`if not tunnel_url:` 區塊裡沒有 sys.exit(2)")

    monitors = calls_in(fn, "_monitor_kaggle_upload")
    if not monitors:
        raise Failure("找不到 _monitor_kaggle_upload —— Kaggle 上傳的監看被拿掉了")
    if report.lineno < min(m.lineno for m in monitors):
        raise Failure(
            "收尾宣告排在 _monitor_kaggle_upload 之前 —— "
            "還在從 Kaggle 拉模型的 boot 會被它提早結束"
        )

    retry = [
        n
        for n in ifs_in(fn)
        if "kernel_status" in test_of(src, n) and "running" in test_of(src, n)
    ]
    if not retry:
        raise Failure(
            "`elif kernel_status == \"running\":` 的第二次讀取不見了 —— "
            "那是拿到網址的最後一次機會，不該被這個補丁拿掉"
        )
    return "收尾宣告有條件、在成功路徑上、且不搶在上傳監看之前"


def check_e(src: str, fn: ast.FunctionDef) -> str:
    fail_if = None
    for node in ifs_in(fn):
        if test_of(src, node) == "not success":
            fail_if = node
            break
    if fail_if is None:
        raise Failure("找不到 `if not success:` 區塊")

    codes = exits_in(fail_if)
    if 1 not in codes:
        raise Failure("`if not success:` 裡沒有 sys.exit(1)")
    if 2 in codes:
        raise Failure("`if not success:` 裡出現了 exit 2 —— 兩個離開碼混在一起了")

    success_if = None
    for node in ifs_in(fn):
        if test_of(src, node) == "success":
            success_if = node
            break
    if success_if is None:
        raise Failure("找不到 `if success:` 區塊")
    if 2 not in exits_in(success_if):
        raise Failure("成功路徑上沒有 exit 2")
    if 1 in exits_in(success_if):
        raise Failure("成功路徑上出現了 exit 1 —— 那會說成『kernel 沒起來』")

    return "exit 1 仍專屬『kernel 沒起來』，新收尾用 2"


CHECKS = (
    ("A", "假處方必須消失", lambda src, fn: check_a(src)),
    ("B", "分支仍要出聲（正對照）", check_b),
    ("C", "success 仍為無條件（D-033 護欄）", check_c),
    ("D", "收尾宣告有條件、位置正確", check_d),
    ("E", "離開碼各守本分", check_e),
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2

    try:
        path = find_commands(argv[1])
    except Failure as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2

    src = path.read_text()
    print(f"目標：{path}")
    print()

    failed = 0
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"  A-E 無法執行：commands.py 不是合法 Python（{exc}）", file=sys.stderr)
        return 1

    try:
        fn = find_run_boot(tree)
    except Failure as exc:
        print(f"  A-E 無法執行：{exc}", file=sys.stderr)
        return 1

    for label, title, fn_check in CHECKS:
        print(f"  {label}  {title}")
        try:
            msg = fn_check(src, fn)
        except Failure as exc:
            print(f"    ✗ {exc}")
            failed += 1
        else:
            print(f"    ✓ {msg}")

    print()
    if failed:
        print(f"✗ {failed}/{len(CHECKS)} 項未通過")
        return 1

    print(f"✓ {len(CHECKS)}/{len(CHECKS)} 項通過")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
