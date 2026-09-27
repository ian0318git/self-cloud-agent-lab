#!/usr/bin/env python3
"""Behavioural verifier for the `endpoint stop` silent no-op (D-060).

    python3 scripts/test_endpoint_stop_fixes.py <site-packages root>
    python3 scripts/test_endpoint_stop_fixes.py <core.py> <commands.py>

WHY THIS EXISTS. `endpoint stop` reported "No running kernel found." with exit
code 0 while a GPU session was alive and burning the weekly quota. The cause is
not a missing check: `get_kernel_status()` returned "offline" from *every* one of
its failure paths, and `run_stop` reads "offline" as the fact "nothing is
running". So the one value the caller trusted was produced exclusively by "could
not ask". Pristine code is indistinguishable from working code by reading it --
you have to drive it.

WHAT IS DRIVEN, AND WHY NOT import. The payload here is real Python, so each
function is sliced out of its file by AST and exec'd against fakes. Importing the
package is not an option: it would bind `endpoint.core` to the *installed* tree
rather than the tree under test, it would construct a real Config (the user's own
config file) and a real Console singleton -- and a real `send_kill_signal` in the
namespace publishes to the live ntfy control topic. The apply script runs this
verifier three times per cycle, so a verifier with side effects would fire kill
signals on every dry run. Nothing here touches the network, the filesystem, or a
subprocess.

NOTE ON TWO DELIBERATE PROPERTIES:

  * `sys` is the REAL module, never a fake -- the exit-code contract is the point
    of the fix, so `sys.exit(2)` must actually raise.
  * `run_boot()` is ~400 lines and calls dozens of things; it cannot be exec'd.
    Only its `for i in range(12):` loop (unique in the file, verified) is sliced
    out and driven, which is the part the patch changes.

NOTE ON THE VERDICT: a pristine tree FAILS check A (four cases) and check D --
that is the bug being demonstrated. Run it in both directions: the patched tree
must pass, the pristine tree must fail. A verifier that passed against both would
be checking nothing. scripts/apply-endpoint-stop-fixes.sh does exactly that.

Exit 0 only if every check passed, 1 if any failed, 2 on usage error.
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path
from types import SimpleNamespace

# --------------------------------------------------------------------------
# Slicing functions out of the files under test
# --------------------------------------------------------------------------


def _source(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _func_node(src: str, name: str) -> ast.FunctionDef:
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}()")


def load_function(path: Path, name: str, ns: dict) -> object:
    """exec one top-level function by AST slice, with `ns` as its globals.

    Annotations are evaluated at def time here: neither file has
    `from __future__ import annotations`, so the caller MUST supply every name
    used in a signature (`Config`, `Any`) or the exec raises NameError before a
    single assertion runs.
    """
    src = _source(path)
    seg = ast.get_source_segment(src, _func_node(src, name))
    if seg is None:  # pragma: no cover - only if the file is unparsable
        raise AssertionError(f"{name}() 取不出原始碼")
    exec(compile(seg, f"<{name}@{path}>", "exec"), ns)
    return ns[name]


def load_boot_clear_loop(path: Path) -> str:
    """run_boot() 太大不能整個 exec，只挑出它清舊 kernel 的那個 for。"""
    src = _source(path)
    for node in ast.walk(_func_node(src, "run_boot")):
        if (
            isinstance(node, ast.For)
            and (ast.get_source_segment(src, node.iter) or "").strip() == "range(12)"
        ):
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("run_boot() 裡找不到 `for i in range(12)`")


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class Recorder:
    """One ordered list of every call, so 'was X called' and 'in what order'
    are the same question."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def note(self, name: str) -> bool:
        self.calls.append(name)
        return True

    def has(self, name: str) -> bool:
        return name in self.calls

    def count(self, name: str) -> int:
        return self.calls.count(name)


def _rec(rec: Recorder, name: str, retval=None):
    def f(*_a, **_kw):
        rec.note(name)
        return retval

    return f


class FakeConsole:
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec
        self.lines: list[tuple[str, str]] = []

    def _emit(self, kind: str, msg: object) -> None:
        self.lines.append((kind, str(msg)))
        self.rec.note("console." + kind)

    @staticmethod
    def _mk(kind: str):
        def f(self, msg):  # noqa: ANN001
            self._emit(kind, msg)

        return f

    dim = _mk("dim")
    ok = _mk("ok")
    info = _mk("info")
    warn = _mk("warn")
    print = _mk("print")
    header = _mk("header")

    def err(self, msg):
        """core.py:69-74 -- err 印完就 sys.exit(1)，不回來。這裡沒有照抄的話，
        「unknown 路徑上沒有呼叫 err」這條檢查就是空的。"""
        self._emit("err", msg)
        raise SystemExit(1)

    def texts(self) -> list[str]:
        return [t for _, t in self.lines]

    def all_text(self) -> str:
        return "\n".join(self.texts())


class FakeConfig:
    def __init__(self, *, username: str = "someuser", proxy: bool = False) -> None:
        self.kaggle_username = username
        self.kernel_id = f"{username}/some-kernel" if username else ""
        self.proxy_enabled = proxy
        self.unregister_url = "https://proxy.invalid/unregister" if proxy else ""


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


def _true_failure_status(core: Path) -> str:
    """問目標自己的 get_kernel_status：讀不到憑證時，它回什麼？

    這是端到端檢查的關鍵一步。直接把 "unknown" 注入 run_stop 是測不出原始版
    的缺陷的：原始版收到 "unknown" 也會照常動作。缺陷在於原始版**產生不出**
    這個值 —— 所以狀態必須由目標自己算出來，再餵進去。
    """
    ns = {
        "Config": FakeConfig,
        "sys": SimpleNamespace(stderr=io.StringIO()),
        "get_kaggle_token": lambda: None,
        "requests": SimpleNamespace(post=None),
    }
    return load_function(core, "get_kernel_status", ns)(FakeConfig())


def invoke(fn, args) -> tuple[str, object]:
    """('exit', code) or ('return', None).

    SystemExit derives from BaseException, so `except Exception` would let it
    escape and kill the harness -- and `sys.exit()` gives code None, which is not
    the same thing as returning.
    """
    try:
        fn(args)
    except SystemExit as exc:
        return ("exit", exc.code)
    else:
        return ("return", None)


# --------------------------------------------------------------------------
# Check A -- get_kernel_status must not call a failure "offline"
# --------------------------------------------------------------------------

# (說明, 注入的 token, HTTP 碼或 "raise", 狀態字, 期望回傳值, 是否應呼叫 post)
STATUS_CASES = [
    ("沒有 token（非互動 shell 的常態）", None, None, None, "unknown", False),
    ("Kaggle 回 HTTP 500", "tok", 500, None, "unknown", True),
    ("連線丟例外", "tok", "raise", None, "unknown", True),
    ("Kaggle 說 RUNNING", "tok", 200, "RUNNING", "running", True),
    ("Kaggle 說 COMPLETE", "tok", 200, "COMPLETE", "complete", True),
    ("Kaggle 說 ERROR", "tok", 200, "ERROR", "error", True),
    # 真的會出現、但掃描清單沒有它 —— 停在「停止中」的 kernel 就是這個字。
    # 它必須落成 unknown（往會做事的那邊倒），不是 offline（往什麼都不做倒）。
    ("Kaggle 說 CANCEL_ACKNOWLEDGED", "tok", 200, "CANCEL_ACKNOWLEDGED", "unknown", True),
]


def check_status(core: Path, fails: list[str]) -> None:
    print("  檢查 A：get_kernel_status() 的分類")
    for label, token, code, word, want, expect_post in STATUS_CASES:
        err = io.StringIO()
        called: list[bool] = []

        # 護欄的來源：`requests.post` 的第一個參數是「位置」傳入的 URL。假函式若
        # 只收 **kwargs，這裡會丟 TypeError，而函式自己的 `except Exception` 會
        # 把它吞成「讀不到」—— 於是每一條都「通過」，卻一條也沒測到。所以要記
        # 錄假 API 是否真的被呼叫，並在下面據此判死。
        def post(*_a, _code=code, _word=word, **_kw):
            called.append(True)
            if _code == "raise":
                raise RuntimeError("simulated transport failure")
            return FakeResponse(_code, {"status": _word} if _word is not None else {})

        ns = {
            "Config": FakeConfig,
            "sys": SimpleNamespace(stderr=err),  # 例外路徑的警告不該弄髒輸出
            "get_kaggle_token": lambda t=token: t,
            "requests": SimpleNamespace(post=post),
        }
        try:
            fn = load_function(core, "get_kernel_status", ns)
            got = fn(FakeConfig())
        except BaseException as exc:  # noqa: BLE001
            print(f"    ✗ {label} → 執行時爆掉：{type(exc).__name__}: {exc}")
            fails.append(f"get_kernel_status raised on: {label}")
            continue

        if expect_post and not called:
            print(f"    ✗ {label} → 假 API 根本沒被呼叫，這條沒測到它宣稱的東西")
            fails.append(f"harness broken: requests.post never called for {label}")
            continue
        if got != want:
            print(f"    ✗ {label} → 期望 {want}，實得 {got!r}")
            fails.append(f"get_kernel_status({label}) returned {got!r}, wanted {want!r}")
            continue
        if code == "raise" and "Warning: kernel status check failed" not in err.getvalue():
            print(f"    ✗ {label} → 回傳值對了，但不是走例外路徑（stderr 沒有警告）")
            fails.append(f"{label}: the exception path did not warn on stderr")
            continue
        print(f"    ✓ {label} → {want}")
    print()


# --------------------------------------------------------------------------
# Check B -- run_stop must act, and must not lie, when the state is unknown
# --------------------------------------------------------------------------


def build_run_stop(commands: Path, status: str, *, username: str = "someuser",
                   proxy: bool = False):
    rec = Recorder()
    con = FakeConsole(rec)
    cfg = FakeConfig(username=username, proxy=proxy)
    ns = {
        "Any": object,  # 簽名裡有 dict[str, Any]
        "Config": lambda: cfg,
        "sys": sys,  # 真的 sys：結束碼是這個修正的重點，sys.exit 必須真的丟
        "console": con,
        "get_kernel_status": _rec(rec, "get_kernel_status", status),
        "send_kill_signal": _rec(rec, "send_kill_signal"),
        "_cancel_kernel_session": _rec(rec, "_cancel_kernel_session", (True, "ok")),
        "clear_cached_endpoint": _rec(rec, "clear_cached_endpoint"),
        "clear_cached_apikey": _rec(rec, "clear_cached_apikey"),
        "load_cached_apikey": _rec(rec, "load_cached_apikey", "cached-key"),
        "unregister_tunnel_from_proxy": _rec(rec, "unregister_tunnel_from_proxy", True),
    }
    fn = load_function(commands, "run_stop", ns)
    return fn, rec, con


def check_stop_unknown(core: Path, commands: Path, fails: list[str]) -> None:
    print("  檢查 B：run_stop() 在狀態 unknown 時的行為")

    # B0 -- 端到端，最承重的一條。狀態由「目標自己的 get_kernel_status」在讀不到
    # 憑證時產生，再餵進 run_stop。這正是 2026-09-26 那天發生的事：非互動 shell
    # 讀不到 token，於是 CLI 回報「沒有在跑的 kernel」、結束碼 0，而 GPU 還活著。
    # 只把 "unknown" 直接注入是測不出這件事的（見 _true_failure_status 的說明）。
    produced = _true_failure_status(core)
    fn_e2e, rec_e2e, _con_e2e = build_run_stop(commands, produced)
    invoke(fn_e2e, {})
    if rec_e2e.has("send_kill_signal"):
        print(f"    ✓ 端到端：讀不到憑證（狀態 {produced!r}）時仍送出 kill 訊號")
    else:
        print(f"    ✗ 端到端：讀不到憑證（狀態 {produced!r}）時沒有送出 kill 訊號"
              " —— 這就是那個靜默的 no-op")
        fails.append("end-to-end: a status the tree cannot read produced no kill signal")

    fn, rec, con = build_run_stop(commands, "unknown")
    how, code = invoke(fn, {})

    # B1 -- the load-bearing one. On the pristine tree this is an early return:
    # nothing is signalled, so the GPU keeps running.
    if rec.has("send_kill_signal"):
        print("    ✓ 送了 kill 訊號（KILL 不需要憑證，所以問不到也要送）")
    else:
        print("    ✗ 沒有送 kill 訊號 —— 這就是那個靜默的 no-op")
        fails.append("run_stop did not call send_kill_signal on an unknown status")

    # B2 -- and it must not claim success.
    if how == "exit" and code == 2:
        print("    ✓ 結束碼 2（無法判定），不是 0")
    else:
        print(f"    ✗ 結束碼應為 2，實得 {how}/{code!r}")
        fails.append(f"run_stop exited {how}/{code!r} on unknown, wanted exit/2")

    # B3 -- console.err does sys.exit(1), and 1 means a definite failure. This is
    # neither.
    if rec.has("console.err"):
        print("    ✗ 走了 console.err（那是結束碼 1 ＝ 確定的失敗）")
        fails.append("run_stop used console.err on an unknown status")
    else:
        print("    ✓ 沒有走 console.err（1 是確定的失敗，不適用）")

    # B4 -- ordering: sys.exit raises immediately, so these two having run at all
    # is what proves they run *before* the exit. Clearing them after would leave
    # a dead tunnel URL cached, which is how the 85-minute false-alive reading
    # was produced in the first place.
    missing = [n for n in ("clear_cached_endpoint", "clear_cached_apikey") if not rec.has(n)]
    if missing:
        print(f"    ✗ 結束前沒有清快取：{', '.join(missing)}（會留下死掉的 tunnel 網址）")
        fails.append(f"run_stop skipped cache clears before exiting: {missing}")
    else:
        print("    ✓ 兩個快取都在結束碼之前清掉了")

    # B5 -- `_cancel_kernel_session` leads with `kaggle kernels delete -y`, which
    # destroys the kernel and its version history. Never spend an irreversible
    # action on a state we could not read.
    if rec.has("_cancel_kernel_session"):
        print("    ✗ 對 unknown 呼叫了 _cancel_kernel_session（它會 kaggle kernels delete）")
        fails.append("run_stop called _cancel_kernel_session on an unknown status")
    else:
        print("    ✓ 沒有呼叫 _cancel_kernel_session（它會刪掉 kernel）")

    # B6 -- the summary has to say what actually happened.
    if "Could not confirm" in con.all_text():
        print("    ✓ 結尾有誠實的總結（說出「無法確認」）")
    else:
        print("    ✗ 沒有說出「無法確認」—— 使用者會以為停好了")
        fails.append("run_stop did not report that termination was unconfirmed")
    print()


# --------------------------------------------------------------------------
# Check C -- the states that really do mean "nothing to do" must not regress
# --------------------------------------------------------------------------


def check_no_regression(commands: Path, fails: list[str]) -> None:
    print("  檢查 C：不得回歸（既有的三種「沒事可做」與 running）")

    for status in ("offline", "complete", "error"):
        fn, rec, _con = build_run_stop(commands, status)
        how, code = invoke(fn, {})
        bad = []
        if rec.has("send_kill_signal"):
            bad.append("送了 kill")
        if rec.has("_cancel_kernel_session"):
            bad.append("呼叫了 cancel")
        if how != "return":
            bad.append(f"結束碼 {code!r}")
        if not (rec.has("clear_cached_endpoint") and rec.has("clear_cached_apikey")):
            bad.append("沒清快取")
        if bad:
            print(f"    ✗ {status}：{'、'.join(bad)}")
            fails.append(f"run_stop regressed on {status}: {', '.join(bad)}")
        else:
            print(f"    ✓ {status}：安靜結束、沒有送訊號、兩個快取都清了")

    fn, rec, _con = build_run_stop(commands, "running", proxy=True)
    how, code = invoke(fn, {})
    bad = []
    if not rec.has("send_kill_signal"):
        bad.append("沒送 kill")
    if not rec.has("_cancel_kernel_session"):
        bad.append("沒呼叫 cancel")
    if how != "return":
        bad.append(f"結束碼 {code!r}")
    if not rec.has("unregister_tunnel_from_proxy"):
        bad.append("沒有從 proxy 註銷")
    if bad:
        print(f"    ✗ running：{'、'.join(bad)}")
        fails.append(f"run_stop regressed on running: {', '.join(bad)}")
    else:
        print("    ✓ running：kill ＋ cancel ＋ proxy 註銷，正常結束")

    # The fake console.err must mirror core.py:69-74, or check B3 proves nothing.
    fn, rec, _con = build_run_stop(commands, "unknown", username="")
    how, code = invoke(fn, {})
    if rec.has("console.err") and how == "exit" and code == 1:
        print("    ✓ 未設定的 config 仍然走 console.err／結束碼 1（假 console 與真的一致）")
    else:
        print(f"    ✗ 未設定的 config 沒有走 err/1，實得 {how}/{code!r} —— 假 console 失真")
        fails.append("fake console.err does not mirror core.py:69-74")
    print()


# --------------------------------------------------------------------------
# Check D -- run_boot's clear loop must stop claiming a kernel it cannot see
# --------------------------------------------------------------------------


def run_boot_loop(commands: Path, statuses: list[str]):
    rec = Recorder()
    con = FakeConsole(rec)
    it = iter(statuses)
    last = statuses[-1] if statuses else "offline"

    def next_status(_config):
        rec.note("get_kernel_status")
        return next(it, last)

    ns = {
        "config": FakeConfig(),
        "get_kernel_status": next_status,
        "console": con,
        "_cancel_kernel_session": _rec(rec, "_cancel_kernel_session"),
        "time": SimpleNamespace(sleep=_rec(rec, "time.sleep")),
    }
    exec(compile(load_boot_clear_loop(commands), "<run_boot clear loop>", "exec"), ns)
    return rec, con


def check_boot_loop(core: Path, commands: Path, fails: list[str]) -> None:
    print("  檢查 D：run_boot() 清舊 kernel 的迴圈")

    # End-to-end across the two functions: feed the loop the value the target's
    # own get_kernel_status produces when it cannot read the state. Pristine
    # produces "offline" here, which the loop's early tuple turns into a
    # confident "Old kernel cleared." -- a claim about a kernel it never saw.
    got = _true_failure_status(core)
    rec, con = run_boot_loop(commands, [got])

    if "Old kernel cleared." in con.all_text():
        print(f"    ✗ 讀不到狀態（{got!r}）時印出「Old kernel cleared.」—— 那是一句沒根據的宣稱")
        fails.append("run_boot claims 'Old kernel cleared.' without having read the state")
    else:
        print(f"    ✓ 讀不到狀態（{got!r}）時沒有宣稱清乾淨了")

    if rec.has("_cancel_kernel_session"):
        print("    ✗ 讀不到狀態時仍呼叫了 cancel（那裡沒有憑證可用，重試 12 次只是延遲）")
        fails.append("run_boot called the cancel path without readable state")
    else:
        print("    ✓ 沒有進入 cancel 重試迴圈（boot 的耗時不變）")

    # The control flow must be untouched for real states.
    rec, con = run_boot_loop(commands, ["running", "running", "offline"])
    n = rec.count("_cancel_kernel_session")
    asleep = rec.count("time.sleep")
    if n == 2 and asleep == 2 and "Old kernel cleared." in con.all_text():
        print("    ✓ running ×2 → cancel ×2 ＋ backoff，然後 offline 收工（行為與原本相同）")
    else:
        print(f"    ✗ 既有路徑的控制流變了：cancel ×{n}、sleep ×{asleep}")
        fails.append(f"run_boot control flow changed on real states (cancel={n}, sleep={asleep})")
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
        return root / "endpoint" / "core.py", root / "endpoint" / "commands.py"
    if len(args) == 2:
        return Path(args[0]), Path(args[1])
    return None


def main(argv: list[str]) -> int:
    paths = resolve(argv)
    if paths is None:
        print(
            "用法：test_endpoint_stop_fixes.py <site-packages root>\n"
            "      test_endpoint_stop_fixes.py <core.py> <commands.py>",
            file=sys.stderr,
        )
        return 2
    core, commands = paths
    for p in (core, commands):
        if not p.is_file():
            print(f"找不到檔案：{p}", file=sys.stderr)
            return 2

    print("=" * 72)
    print(f"core.py      {core}")
    print(f"commands.py  {commands}")
    print("=" * 72)

    fails: list[str] = []
    for label, fn in (
        ("A", lambda: check_status(core, fails)),
        ("B", lambda: check_stop_unknown(core, commands, fails)),
        ("C", lambda: check_no_regression(commands, fails)),
        ("D", lambda: check_boot_loop(core, commands, fails)),
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
