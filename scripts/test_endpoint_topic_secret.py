#!/usr/bin/env python3
"""Behavioural verifier for cut C: the ntfy topic name stops being derivable from
a public identifier (D-069).

    python3 scripts/test_endpoint_topic_secret.py <site-packages root>
    python3 scripts/test_endpoint_topic_secret.py <notebook> <core.py> <commands.py>

WHY THIS EXISTS. The control topic used to be named
`sha256(<public Kaggle username>)[:12]`, so anyone who knew the account could
derive it -- and the topic is a bearer capability: knowing it lets you read the
lifecycle and forge a KILL. Cut C derives it from a local 32-hex secret instead.
This verifier pins the whole property: the derivation is *one* derivation (two
copies exist, and they used to disagree), it fails closed when the secret is
missing or malformed, and the name never reaches an error message.

WHAT IS DRIVEN, AND WHY NOT import. Importing the package would bind
`endpoint.core` to the *installed* tree rather than the tree under test, and
construct a real Config (the user's own config file). Instead the derivations are
sliced out of the source by AST and exec'd against stubs: the `signal_topic` and
`control_topic` bodies become free functions taking a stub with `topic_prefix`
and `topic_secret`, and the generator's `get` / `require_topic_secret` /
`compute_signal_topic` are exec'd with the real `get`. Check D7 goes further and
runs the real generator in a TMPDIR, on a **canary** config -- the notebook it
produces bakes the channel name in, so no real value may appear in it.

NO NETWORK, NO REAL CREDENTIALS. Everything here is local. The bake runs the
generator with a canary api_key and a **fake** Kaggle username, so the real
account never enters the picture (and, in particular, is never written to a file
by this verifier).

DISCRIMINATORS AND GUARDS. A pristine (cut-B-applied) tree fails D1 through D7
and D10. D8, D9 and D11 are guards: they pass in both directions by design,
because they pin invariants that must survive the change.

  * D8 re-runs the cut-B verifier and requires it to be byte-identical to what
    shipped with D-068. Cut C shares three files with cut B; if someone edits
    B's guard to accommodate C, B's independence is gone.
  * D9 pins engine/engine.py to what cut A produced (D-067), so that A's gate
    cannot have been quietly polluted -- and so that a reverted cut A shows up
    here instead of silently making this patch ride an unknown base.
  * D11 reads the apply script's ten pinned hashes and requires them to describe
    the tree under test. Those pins are hand-written; this is what makes a typo
    in one of them go red instead of producing a `state_of: unknown` on a tree
    that is actually fine.

ONE HONEST DEVIATION FROM THE PLAN. The plan asked the agreement table (D3) to
include a non-ASCII secret. It cannot be a *derivation* row -- the shape gate
rejects anything outside `[0-9a-f]{32}` before the hash is reached -- so the
non-ASCII case is asserted where it actually lands: both sides must reject it.

Run it in both directions: the patched tree must pass, the pristine tree must
fail. A verifier that passed against both would be checking nothing.
scripts/apply-endpoint-topic-secret.sh does exactly that.

Exit 0 only if every check passed, 1 if any failed, 2 on usage error.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

DOMAIN = "endpoint-signal-v1:"

# The cut-B verifier as shipped in D-068 (commit 28daf4d) and the engine as cut A
# left it (D-067, commit 42c5f75). Both pinned so that "we did not touch the
# guards" is checkable rather than promised.
CUT_B_VERIFIER_SHA = "abc4125a1212f14fcad0a19604ce08e84ca870b0a8b2adb2dcab47b4730a83c6"
CUT_B_VERIFIER_NAME = "test_endpoint_tunnel_url_privacy.py"
ENGINE_SHA = "68dfa5a26614e1606c9f489c2288b0ea0071c6a4fc891a524a635e69f99e653e"

# Canary values. The secret is a *fixed* literal so the expected topic can be
# pinned as a golden value: any drift in the formula -- dropping the domain
# separator, changing the truncation, reordering the concatenation -- turns red
# instead of producing a different-but-plausible name that everything agrees on.
CANARY_SECRET = "0123456789abcdef0123456789abcdef"
GOLDEN_TOPIC = "endpoint-6055c1326a30"
CANARY_APIKEY = "CANARY-NOT-REAL-0000000000000000"
CANARY_USERNAME = "bake-user-not-real"

# (prefix, secret) rows for the agreement check. The mixed-case prefix is the
# load-bearing one: the two copies of the derivation used to differ by a single
# `.lower()`, which was harmless while the digest came from a lowercase username
# and is not harmless now that the prefix is the only case-bearing part.
AGREEMENT_ROWS: list[tuple[str, str]] = [
    ("endpoint", CANARY_SECRET),
    ("Endpoint", CANARY_SECRET),
    ("ENDPOINT", "ffffffffffffffffffffffffffffffff"),
    ("", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    ("endpoint", "00000000000000000000000000000000"),
    ("topic_Under-Mixed", "00ff00ff00ff00ff00ff00ff00ff00ff"),
]

MISSING_ROWS: list[Any] = [None, "", "   ", "not-hex-at-all", "0123456789ABCDEF0123456789abcdef"]
MISSING_ROWS += ["0" * 31, "0" * 33, "秘密" * 8, 12345, True]


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
            and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
        )
        if hit:
            out.extend(lines[node.lineno - 1 : node.end_lineno])
    return "".join(out)


def _method_source(path: Path, cls: str, name: str) -> str | None:
    """Dedented source of a method, so it can be exec'd as a free function.

    The body is the tree's own body -- only the indentation changes, which is
    what lets a stub stand in for `self`.
    """
    src = path.read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if not (isinstance(node, ast.ClassDef) and node.name == cls):
            continue
        for sub in node.body:
            if isinstance(sub, ast.FunctionDef) and sub.name == name:
                seg = ast.get_source_segment(src, sub)
                return textwrap.dedent(seg) if seg else None
    return None


def _has_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _mentions_identifier(src: str, names: set[str]) -> bool:
    """Whether *code* in *src* uses one of these identifiers.

    Deliberately AST-based rather than a substring search. Several of the
    comments in this cut explain what was removed and therefore *name* it -- a
    substring check would fire on the explanation and a `sed` of the comment
    would be the "fix". Only a Name, an attribute or a parameter counts.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            return True
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.arg) and node.arg in names:
            return True
    return False


def _joined_strs(path: Path) -> list[ast.JoinedStr]:
    return [n for n in ast.walk(_tree(path)) if isinstance(n, ast.JoinedStr)]


def _topic_names_in(path: Path, func_name: str) -> list[int]:
    """Line numbers where a top-level function mentions a topic *property*."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    spans = [
        (n.lineno, n.end_lineno)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name
    ]
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {
            "signal_topic",
            "control_topic",
            "topic_secret",
        }:
            if any(lo <= node.lineno <= hi for lo, hi in spans):
                hits.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in {"signal_topic", "control_topic"}:
            if any(lo <= node.lineno <= hi for lo, hi in spans):
                hits.append(node.lineno)
    return sorted(set(hits))


class _StubConfig:
    """Stands in for Config: the derivation only reads these two attributes.

    `signal_topic` is a property because `control_topic` is written as
    `f"{self.signal_topic}-control"` -- it *calls* the derivation rather than
    recomputing it, so a stub without that attribute makes the second derivation
    untestable. `signal_fn` is filled in once core.py has been exec'd.
    """

    signal_fn: Any = None

    def __init__(self, prefix: Any, secret: Any, username: str = CANARY_USERNAME) -> None:
        self.topic_prefix = prefix
        self.topic_secret = secret
        self.kaggle_username = username

    @property
    def signal_topic(self) -> str:
        if _StubConfig.signal_fn is None:
            raise AttributeError("signal_topic")
        return _StubConfig.signal_fn(self)


def _load_core_derivations(core: Path) -> dict:
    ns: dict = {
        "hashlib": hashlib,
        "re": re,
        "Any": Any,
        "Config": _StubConfig,  # the annotation is evaluated at def time
    }
    src = _segment(
        core,
        {
            "TOPIC_SECRET_DOMAIN",
            "TOPIC_SECRET_RE",
            "TOPIC_SECRET_HELP",
            "topic_secret_of",
            "topic_digest",
        },
    )
    exec(compile(src, str(core), "exec"), ns)  # noqa: S102
    for name in ("signal_topic", "control_topic"):
        body = _method_source(core, "Config", name)
        if body is None:
            raise KeyError(f"Config.{name} not found")
        exec(compile(body, str(core), "exec"), ns)  # noqa: S102
    _StubConfig.signal_fn = ns["signal_topic"]
    return ns


def _load_generator_derivations(notebook: Path) -> dict:
    ns: dict = {"hashlib": hashlib, "re": re, "Any": Any, "os": os, "sys": sys}
    src = _segment(
        notebook,
        {
            "TOPIC_SECRET_DOMAIN",
            "TOPIC_SECRET_RE",
            "TOPIC_SECRET_HELP",
            "get",
            "require_topic_secret",
            "compute_signal_topic",
        },
    )
    exec(compile(src, str(notebook), "exec"), ns)  # noqa: S102
    return ns


# ── D1: the derivation no longer touches the public username ───────────────


def check_no_username_in_derivation(core: Path, fails: list[str]) -> None:
    body = _method_source(core, "Config", "signal_topic")
    if body is None:
        fails.append("D1: Config.signal_topic not found in core.py")
        print("    ✗ core.py 找不到 Config.signal_topic")
        return
    if _mentions_identifier(body, {"kaggle_username"}):
        fails.append("D1: signal_topic still references kaggle_username")
        print("    ✗ signal_topic 仍然引用 kaggle_username —— 主題名還是公開可推導")
    else:
        print("    ✓ signal_topic 不再引用 kaggle_username")

    # The helpers on the path must be clean too: a username that merely moved one
    # call deeper would satisfy the check above while changing nothing.
    for label, names in (
        ("topic_secret_of", {"TOPIC_SECRET_DOMAIN", "topic_secret_of", "topic_digest"}),
        ("topic_digest", {"TOPIC_SECRET_DOMAIN", "topic_secret_of", "topic_digest"}),
    ):
        src = _segment(core, names)
        if _mentions_identifier(src, {"kaggle_username"}):
            fails.append(f"D1: kaggle_username reached {label}'s slice")
            print(f"    ✗ 推導鏈上的 {label} 看得到 kaggle_username")
            return
    print("    ✓ 推導鏈（topic_secret_of / topic_digest）也都沒有帳號")


# ── D2: Config gained a topic_secret that reads signal.topic_secret ────────


def check_topic_secret_property(core: Path, fails: list[str]) -> None:
    found = False
    for node in _tree(core).body:
        if not (isinstance(node, ast.ClassDef) and node.name == "Config"):
            continue
        for sub in node.body:
            if not (isinstance(sub, ast.FunctionDef) and sub.name == "topic_secret"):
                continue
            found = True
            keys: list[str] | None = None
            defaults: list[Any] = []
            for inner in ast.walk(sub):
                if not (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "_get"
                ):
                    continue
                if inner.args and isinstance(inner.args[0], ast.List):
                    keys = [e.value for e in inner.args[0].elts if _has_str(e)]
                # `_get(path, default)` takes it positionally; accept the keyword
                # form too so a harmless refactor does not turn this red.
                if len(inner.args) > 1:
                    defaults.append(getattr(inner.args[1], "value", None))
                for kw in inner.keywords:
                    if kw.arg == "default":
                        defaults.append(getattr(kw.value, "value", None))
            if keys == ["signal", "topic_secret"]:
                print('    ✓ Config.topic_secret 讀 ["signal", "topic_secret"]')
            else:
                fails.append(f"D2: Config.topic_secret reads {keys!r}")
                print(f"    ✗ Config.topic_secret 讀的是 {keys!r}，不是 signal.topic_secret")
            if defaults and defaults[0] == "":
                print('    ✓ 缺席時回空字串（交給 topic_secret_of 統一大聲失敗）')
            else:
                fails.append(f"D2: Config.topic_secret default is {defaults!r}")
                print(f"    ✗ 缺席時的預設值是 {defaults!r}，應為空字串")
    if not found:
        fails.append("D2: Config has no topic_secret")
        print("    ✗ Config 沒有 topic_secret")


# ── D3: one derivation, two copies, and the golden value ──────────────────


def check_agreement(core: Path, notebook: Path, fails: list[str]) -> None:
    try:
        core_ns = _load_core_derivations(core)
        gen_ns = _load_generator_derivations(notebook)
    except Exception as exc:  # noqa: BLE001
        fails.append(f"D3: could not load the derivations ({type(exc).__name__}: {exc})")
        print(f"    ✗ 載不進推導：{type(exc).__name__}: {exc}")
        return

    golden = core_ns["signal_topic"](_StubConfig("endpoint", CANARY_SECRET))
    if golden == GOLDEN_TOPIC:
        print(f"    ✓ 黃金值成立（{golden}）")
    else:
        fails.append(f"D3: golden topic drifted: {golden!r} != {GOLDEN_TOPIC!r}")
        print(f"    ✗ 黃金值漂移：實得 {golden!r}，應為 {GOLDEN_TOPIC!r}")

    for prefix, secret in AGREEMENT_ROWS:
        stub = _StubConfig(prefix, secret)
        # Each row is protected on its own. A row that raises is a *row* failure,
        # not the end of the check -- on the pristine tree every row raises
        # (there is no `require_topic_secret`), and aborting at the first would
        # hide the disagreement this check exists to display.
        try:
            a = core_ns["signal_topic"](stub)
        except Exception as exc:  # noqa: BLE001
            fails.append(f"D3: CLI derivation raised for prefix {prefix!r}: {exc!r}")
            print(f"    ✗ 前綴 {prefix!r}：CLI 端推導就拋了 {type(exc).__name__}: {exc}")
            continue
        try:
            b = gen_ns["compute_signal_topic"](
                gen_ns["require_topic_secret"](_cfg(prefix, secret)), prefix
            )
        except Exception as exc:  # noqa: BLE001
            fails.append(f"D3: generator derivation raised for prefix {prefix!r}: {exc!r}")
            print(f"    ✗ 前綴 {prefix!r}：產生器端推導拋了 {type(exc).__name__}: {exc}")
            continue
        ctl_a = core_ns["control_topic"](stub)
        if a == b:
            print(f"    ✓ 兩份推導一致：{prefix!r} → {a}")
        else:
            fails.append(f"D3: derivations disagree for prefix {prefix!r}: {a!r} vs {b!r}")
            print(f"    ✗ 前綴 {prefix!r}：CLI 得 {a!r}，產生器得 {b!r} —— 兩邊聽不同主題")
        if ctl_a == a + "-control":
            print(f"    ✓ control == signal + '-control'（{ctl_a}）")
        else:
            fails.append(f"D3: control_topic is {ctl_a!r} for {a!r}")
            print(f"    ✗ control_topic 得 {ctl_a!r}，應為 {a!r}-control")

    # The generator writes `CONTROL_TOPIC` as a hand-written second copy of the
    # suffix (the CLI's `control_topic` is the first). Both must exist: a CLI
    # listening on `-control` while the kernel listens on the bare signal topic
    # is a silent split with no symptom.
    hit = 0
    for js in _joined_strs(notebook):
        consts = [n.value for n in js.values if _has_str(n)]
        names = [
            n.value.id
            for n in js.values
            if isinstance(n, ast.FormattedValue)
            and isinstance(n.value, ast.Name)
        ]
        if (
            any(c.startswith("CONTROL_TOPIC") for c in consts)
            and any("-control" in c for c in consts)
            and "signal_topic" in names
        ):
            hit += 1
    if hit == 1:
        print("    ✓ 產生器把 CONTROL_TOPIC 寫成 signal_topic + '-control'（唯一一份）")
    else:
        fails.append(f"D3: expected exactly 1 CONTROL_TOPIC f-string, found {hit}")
        print(f"    ✗ 產生器的 CONTROL_TOPIC 拼接有 {hit} 處（應為 1）")

    # Both generator call sites must emit the *derived* topic, not the username.
    sig = 0
    for js in _joined_strs(notebook):
        consts = [n.value for n in js.values if _has_str(n)]
        names = [
            n.value.id
            for n in js.values
            if isinstance(n, ast.FormattedValue)
            and isinstance(n.value, ast.Name)
        ]
        if any(c.startswith("SIGNAL_TOPIC") for c in consts) and "signal_topic" in names:
            sig += 1
    if sig == 2:
        print("    ✓ 兩個產生器（完整版與 upload-only）都烤進推導出的 SIGNAL_TOPIC")
    else:
        fails.append(f"D3: expected 2 SIGNAL_TOPIC f-strings, found {sig}")
        print(f"    ✗ 烤進 SIGNAL_TOPIC 的地方有 {sig} 處（應為 2：完整版＋upload-only）")


def _cfg(prefix: str, secret: str) -> dict:
    return {"signal": {"topic_prefix": prefix, "topic_secret": secret}}


# ── D4: fail closed, with no path back to the username ────────────────────


def check_fail_closed(core: Path, notebook: Path, fails: list[str]) -> None:
    try:
        core_ns = _load_core_derivations(core)
        gen_ns = _load_generator_derivations(notebook)
    except Exception as exc:  # noqa: BLE001
        fails.append(f"D4: could not load the derivations ({type(exc).__name__}: {exc})")
        print(f"    ✗ 載不進推導：{type(exc).__name__}: {exc}")
        return

    for label, ns, call in (
        ("core.topic_secret_of", core_ns, lambda v: core_ns["topic_secret_of"](_StubConfig("endpoint", v))),
        ("core.signal_topic", core_ns, lambda v: core_ns["signal_topic"](_StubConfig("endpoint", v))),
        (
            "generator.require_topic_secret",
            gen_ns,
            lambda v: gen_ns["require_topic_secret"](_cfg("endpoint", v)),
        ),
    ):
        del ns
        for bad in MISSING_ROWS:
            try:
                got = call(bad)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            fails.append(f"D4: {label} accepted {bad!r} -> {got!r}")
            print(f"    ✗ {label} 接受了不合規的值（{[type(bad).__name__]}）→ {got!r}")
            return
        print(f"    ✓ {label} 對 {len(MISSING_ROWS)} 種不合規的值全部拒絕")

    # The failure must be *loud and named*, not a bare exception: the message has
    # to point at the fix, or the operator is left with `Boot monitoring failed
    # unexpectedly.` -- the false-failure family D-066 section 2 recorded.
    try:
        core_ns["signal_topic"](_StubConfig("endpoint", ""))
    except ValueError as exc:
        msg = str(exc)
        if "set-endpoint-topic-secret.sh" in msg and "signal.topic_secret" in msg:
            print("    ✓ 失敗訊息點名欄位與修法腳本")
        else:
            fails.append(f"D4: the refusal message names neither key nor fix: {msg!r}")
            print("    ✗ 拒絕訊息沒有同時點名欄位與修法腳本")

    # No fallback may exist on the path: grepping the slice for the old
    # derivation is what makes "we removed the escape hatch" checkable.
    src = (
        _method_source(core, "Config", "signal_topic") or ""
    ) + _segment(core, {"topic_secret_of", "topic_digest", "TOPIC_SECRET_DOMAIN", "TOPIC_SECRET_RE", "TOPIC_SECRET_HELP"})
    gen = _segment(
        notebook,
        {"require_topic_secret", "compute_signal_topic", "TOPIC_SECRET_DOMAIN", "TOPIC_SECRET_RE", "TOPIC_SECRET_HELP"},
    )
    for label, text in (("core.py", src), ("產生器", gen)):
        if _mentions_identifier(text, {"kaggle_username", "username"}):
            fails.append(f"D4: {label} still uses a username in the derivation")
            print(f"    ✗ {label} 的推導區塊仍用到帳號識別碼 —— 舊路還在附近")
        else:
            print(f"    ✓ {label} 的推導區塊沒有任何帳號識別碼")


# ── D5: every generator call site validates, none keeps the old input ─────


def check_call_sites(notebook: Path, fails: list[str]) -> None:
    tree = _tree(notebook)
    sites: list[tuple[str, ast.Call]] = []
    scopes = [
        (n.lineno, n.end_lineno, n.name)
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "compute_signal_topic"
        ):
            continue
        owner = "<module>"
        for lo, hi, name in scopes:
            if lo <= node.lineno <= hi:
                owner = name
                break
        sites.append((owner, node))

    owners = sorted({o for o, _ in sites})
    if owners == ["build_notebook", "build_upload_notebook"]:
        print("    ✓ 兩個呼叫點都在（build_notebook／build_upload_notebook）")
    else:
        fails.append(f"D5: unexpected compute_signal_topic call sites: {owners}")
        print(f"    ✗ 呼叫點不對：{owners}")

    for owner, call in sites:
        arg = call.args[0] if call.args else None
        ok = (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Name)
            and arg.func.id == "require_topic_secret"
        )
        if ok:
            print(f"    ✓ {owner} 傳的是 require_topic_secret(cfg)（驗證過才用）")
        else:
            fails.append(f"D5: {owner} does not validate before deriving")
            print(f"    ✗ {owner} 沒有先驗證 —— 第一個引數是 {ast.dump(arg)[:60]}")

    # And the function itself must take a secret, not a username: a renamed
    # parameter with the old body would otherwise look fine here.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "compute_signal_topic":
            params = [a.arg for a in node.args.args]
            if params and params[0] != "username":
                print(f"    ✓ compute_signal_topic 的第一個參數是 {params[0]!r}（不是 username）")
            else:
                fails.append(f"D5: compute_signal_topic still takes {params!r}")
                print(f"    ✗ compute_signal_topic 的參數仍是 {params!r}")
            body = ast.get_source_segment(notebook.read_text(encoding="utf-8"), node) or ""
            if "TOPIC_SECRET_DOMAIN" in body and "secret" in body:
                print("    ✓ 雜湊的輸入是領域分隔字串 ＋ 秘密")
            else:
                fails.append("D5: compute_signal_topic does not hash DOMAIN + secret")
                print("    ✗ 雜湊的輸入不是領域分隔字串 ＋ 秘密")


# ── D6: the topic never reaches an error path ─────────────────────────────


class _FakeRequests:
    """Minimal `requests` with a real exception hierarchy to catch."""

    class RequestException(Exception):
        pass

    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return object()


class _BreakingConfig:
    """A config whose channel name cannot be derived -- the P0 case."""

    @property
    def control_topic(self) -> str:
        raise ValueError("Missing or malformed signal.topic_secret in config")


def check_error_paths(core: Path, commands: Path, fails: list[str]) -> None:
    # run_watch: static. Nothing parses that header, so printing the name is pure
    # exposure -- into scrollback and into anything pasted from it.
    hits = _topic_names_in(commands, "run_watch")
    if hits:
        fails.append(f"D6: run_watch mentions the topic at lines {hits}")
        print(f"    ✗ run_watch 在行 {hits} 提到主題（會進終端機回捲）")
    else:
        print("    ✓ run_watch 不提到主題")

    src = _segment(commands, {"run_watch"})
    if "topic_secret_of" in src:
        print("    ✓ run_watch 仍然先驗證（缺席時大聲失敗，不是靜靜地監看空氣）")
    else:
        fails.append("D6: run_watch no longer validates the channel")
        print("    ✗ run_watch 不再驗證通道 —— 缺席時它會對著一條不存在的主題等")

    # send_kill_signal: driven. The leaked string was never the literal in the
    # print -- it was the *exception text*, because requests embeds the whole URL
    # in its own message, and the URL embeds the topic.
    gen = _segment(core, {"send_kill_signal"})
    if "def send_kill_signal" not in gen:
        fails.append("D6: send_kill_signal not found in core.py")
        print("    ✗ core.py 找不到 send_kill_signal")
        return

    topic = "endpoint-canarytopic01"

    class _Cfg:
        control_topic = topic

    def drive(ns: dict, cfg) -> tuple[str, Exception | None]:
        import io
        import contextlib

        buf = io.StringIO()
        err: Exception | None = None
        try:
            with contextlib.redirect_stderr(buf):
                ns["send_kill_signal"](cfg)
        except Exception as exc:  # noqa: BLE001
            err = exc
        return buf.getvalue(), err

    ns = {"requests": _FakeRequests(), "time": __import__("time"), "sys": sys, "Config": _Cfg}
    exec(compile(gen, str(core), "exec"), ns)  # noqa: S102

    transport = _FakeRequests.RequestException(
        f"HTTPConnectionPool(host='ntfy.sh', port=80): Max retries exceeded with "
        f"url: /{topic} (Caused by NewConnectionError)"
    )
    fake = _FakeRequests(exc=transport)
    ns["requests"] = fake
    out, err = drive(ns, _Cfg())
    if topic in out:
        fails.append("D6: send_kill_signal printed the topic")
        print("    ✗ send_kill_signal 把主題印出來了（requests 的例外字串內嵌整個 URL）")
    else:
        print("    ✓ 傳輸失敗時不吐主題")
    if err is not None:
        fails.append(f"D6: transport failure escaped: {type(err).__name__}")
        print(f"    ✗ 傳輸失敗穿出去了：{type(err).__name__} —— stop 會少印那句話")
    else:
        print("    ✓ 傳輸失敗仍然只印警告（不中斷 stop）")
    if "RequestException" in out:
        print("    ✓ 保留例外類別名（可診斷性沒有被犧牲掉）")
    else:
        fails.append("D6: the warning no longer names the exception class")
        print("    ✗ 警告不再點名例外類別 —— 出錯時無從診斷")
    if fake.calls and topic in str(fake.calls[0]["url"]):
        print(f"    ✓ 它仍然真的去打 {fake.calls[0]['url'].split('/')[2]}")
    else:
        fails.append("D6: the kill signal no longer reaches ntfy")
        print("    ✗ 沒有真的送出 KILL")

    # The P0: a config error must propagate. Swallowing it downgraded "this shell
    # cannot derive the topic" into a warning and let `endpoint stop` report a
    # kill it never sent (D-060's shape).
    _, escaped = drive(ns, _BreakingConfig())
    if isinstance(escaped, ValueError):
        print("    ✓ 設定錯誤會穿出去（不再被降級成一句 warning）")
    else:
        fails.append(f"D6: a config error did not propagate (got {escaped!r})")
        print(f"    ✗ 設定錯誤被吞掉了（實得 {escaped!r}）—— 那正是 D-060 的形狀")


# ── D7: bake -- run the real generator on a canary config ─────────────────


def _bake(notebook: Path, scratch: Path, fails: list[str]) -> tuple[str, Path, str] | None:
    cfg = {
        "default_model": "qwen3-8b",
        "default_model_file": "qwen3-8b.gguf",
        "default_model_index": 0,
        "engine": {
            "accelerator": "gpu_t4",
            "engine_dataset_ref": "example/endpoint-engines",
            "engine_port": 5003,
            "version": "0.1.2",
        },
        "identity": {
            "api_key": CANARY_APIKEY,
            "kaggle_username": CANARY_USERNAME,
            "kernel_slug": "endpoint-engine",
        },
        "kaggle_dataset": "endpoint-bake-not-real",
        "llm": {
            "batch_size": 2048,
            "context_length": 40960,
            "max_tokens": 40960,
            "ngl": 999,
            "temperature": 0.7,
            "tensor_split": "1,1",
            "top_p": 0.95,
        },
        "model_source": "kaggle",
        "signal": {"topic_prefix": "endpoint", "topic_secret": CANARY_SECRET},
    }
    cfg_path = scratch / "bake-config.yaml"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    env = dict(os.environ, TMPDIR=str(scratch), ENDPOINT_API_KEY=CANARY_APIKEY)
    proc = subprocess.run(
        [sys.executable, str(notebook), "--config", str(cfg_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        fails.append(f"D7: generator exited {proc.returncode}")
        print(f"    ✗ 產生器失敗（結束碼 {proc.returncode}）")
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            print(f"  → {line[:100]}")
        return None
    out = scratch / "endpoint-engine-output" / "endpoint_setup.ipynb"
    if not out.is_file():
        fails.append(f"D7: no notebook at {out}")
        print(f"    ✗ 找不到烘出來的 notebook：{out}")
        return None
    return out.read_text(encoding="utf-8"), out, (proc.stdout or "") + (proc.stderr or "")


def check_bake(notebook: Path, fails: list[str]) -> None:
    scratch = Path(tempfile.mkdtemp(prefix="cutC-bake-"))
    try:
        baked = _bake(notebook, scratch, fails)
        if baked is None:
            return
        text, out, log = baked
        src = "".join(
            "".join(c.get("source") or [])
            for c in (json.loads(text).get("cells") or [])
        )

        # Positive control: the canary key comes from the environment, so its
        # presence proves the generator actually ran rather than exiting early
        # on a half-written notebook. Without it a "clean" notebook proves
        # nothing.
        if CANARY_APIKEY in src:
            print("    ✓ canary 金鑰被烘進去（證明整條路真的走完）")
        else:
            fails.append("D7: the canary api_key never reached the notebook")
            print("    ✗ canary 金鑰不在 notebook 裡 —— 這次烘驗是空跑")
            return

        # And the *config* was the one consumed. The username is a good proxy for
        # that precisely because it is NOT baked as a literal -- the generator
        # only emits it through runtime f-strings (`{KAGGLE_DATASET}` and
        # friends), so the sole sign that `identity.kaggle_username` was read is
        # the absence of this warning. Asserting the canary username appears
        # would be asserting something the generator never does.
        if "kaggle_username not set" in log:
            fails.append("D7: the generator never read identity.kaggle_username")
            print("    ✗ 產生器抱怨 kaggle_username 沒設 —— 它讀的不是我們給的那份 config")
            print("  → 那麼這次烘驗證明的東西就不是我們以為的那些。")
        else:
            print("    ✓ 產生器讀到了 config 裡的 identity.kaggle_username（沒有示警）")

        m = re.search(r"SIGNAL_TOPIC\s*=\s*'([^']+)'", src)
        ctl = re.search(r"CONTROL_TOPIC\s*=\s*'([^']+)'", src)
        if not m:
            fails.append("D7: no SIGNAL_TOPIC literal in the baked notebook")
            print("    ✗ 烘出來的 notebook 裡沒有 SIGNAL_TOPIC 字面值")
            return
        got = m.group(1)
        if got == GOLDEN_TOPIC:
            print("    ✓ 烘進去的 SIGNAL_TOPIC 逐字等於黃金值")
        else:
            fails.append(f"D7: baked SIGNAL_TOPIC is {got!r}, want {GOLDEN_TOPIC!r}")
            print(f"    ✗ 烘進去的 SIGNAL_TOPIC 是 {got!r}，應為 {GOLDEN_TOPIC!r}")
        if ctl and ctl.group(1) == got + "-control":
            print("    ✓ 烘進去的 CONTROL_TOPIC == SIGNAL_TOPIC + '-control'")
        else:
            fails.append(f"D7: baked CONTROL_TOPIC is {ctl.group(1) if ctl else None!r}")
            print(f"    ✗ 烘進去的 CONTROL_TOPIC 是 {ctl.group(1) if ctl else None!r}")

        old_style = hashlib.sha256(CANARY_USERNAME.encode()).hexdigest()[:12]
        if got.endswith(old_style):
            fails.append("D7: the baked topic is still the username derivation")
            print("    ✗ 烘進去的主題仍然是帳號推導 —— 這一刀沒有生效")
        else:
            print("    ✓ 烘進去的主題不是帳號推導出來的那個")

        # The secret itself must never leave the machine. Base64 first: the
        # notebook embeds whole files as blobs, and a hit inside one would be
        # invisible to a plain substring search over the JSON text.
        if CANARY_SECRET in text:
            fails.append("D7: the secret was baked into the notebook")
            print("    ✗ 秘密被烘進 notebook 了 —— 它應該只留在本機")
        else:
            print("    ✓ 秘密沒有被烘進 notebook")
        blobs = re.findall(r"[A-Za-z0-9+/=]{200,}", text)
        leaked = [b for b in blobs if CANARY_SECRET.encode() in _try_b64(b)]
        if leaked:
            fails.append("D7: the secret is inside a base64 blob")
            print("    ✗ 秘密藏在 base64 區塊裡（純文字搜尋看不到）")
        else:
            print(f"    ✓ {len(blobs)} 個 base64 區塊裡都沒有秘密")

        mode = out.parent.stat().st_mode & 0o777
        if mode == 0o700:
            print("    ✓ 輸出目錄是 0700（notebook 裡有通行憑證）")
        else:
            fails.append(f"D7: output dir mode is {mode:04o}, want 0700")
            print(f"    ✗ 輸出目錄權限是 {mode:04o}，應為 0700")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _try_b64(blob: str) -> bytes:
    import base64
    import binascii

    try:
        return base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return b""


# ── D8: cut B's guard is untouched and still passes ───────────────────────


def check_cut_b_guard_intact(script_dir: Path, root: Path, fails: list[str]) -> None:
    sibling = script_dir / CUT_B_VERIFIER_NAME
    if not sibling.is_file():
        fails.append(f"D8: sibling verifier missing: {sibling}")
        print(f"    ✗ 找不到 {CUT_B_VERIFIER_NAME}")
        return
    got = hashlib.sha256(sibling.read_bytes()).hexdigest()
    if got != CUT_B_VERIFIER_SHA:
        fails.append(f"D8: cut-B verifier was edited (sha {got})")
        print("    ✗ 切 B 的驗證器被改過了 —— 它的獨立性已經沒了")
        print(f"  → 預期 {CUT_B_VERIFIER_SHA}")
        print(f"  → 實得 {got}")
        return
    print("    ✓ 切 B 的驗證器逐位元未變（獨立性成立）")

    proc = subprocess.run(
        [sys.executable, str(sibling), str(root)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        fails.append(f"D8: cut-B verifier now exits {proc.returncode}")
        print(f"    ✗ 切 B 的驗證器對這棵樹不再通過（結束碼 {proc.returncode}）")
        for line in (proc.stdout or "").strip().splitlines()[-5:]:
            print(f"  → {line}")
        return
    print("    ✓ 切 B 的驗證器對這棵樹仍然通過")


# ── D9: cut A's file is untouched ─────────────────────────────────────────


def check_engine_untouched(root: Path, fails: list[str]) -> None:
    """engine/engine.py must be exactly what cut A produced.

    Cut C does not touch it, and it may not: D-067's gate pins that file's hash,
    so any edit here would invalidate cut A's guard. Pinning the D-067 value
    makes a *reverted* cut A show up as a red here rather than letting this
    patch ride an unknown base.
    """
    engine = root / "engine" / "engine.py"
    if not engine.is_file():
        fails.append(f"D9: engine.py not found at {engine}")
        print(f"    ✗ 找不到 {engine}")
        return
    got = hashlib.sha256(engine.read_bytes()).hexdigest()
    if got == ENGINE_SHA:
        print("    ✓ engine/engine.py 逐位元等於切 A 留下的那一份")
        return
    fails.append(f"D9: engine.py drifted from the cut-A value ({got})")
    print("    ✗ engine/engine.py 不等於切 A 留下的那一份")
    print(f"  → 預期 {ENGINE_SHA}")
    print(f"  → 實得 {got}")
    print("  → 切 A 被還原了，或上游升級了。這一刀定義成疊在 ntfy→stop→B 之上，")
    print("     所以先把它們的狀態弄清楚再回來。")


# ── D10: the tunnel reader must not consult the topic ─────────────────────


def check_reader_ignores_topic(core: Path, fails: list[str]) -> None:
    """`get_kernel_log_url`/`resolve_tunnel_url` must not read the topic.

    They own the found/absent/unknown contract (D-060/D-068). If either derived
    the topic, an unrelated config error would manufacture an `absent` or
    `unknown` -- a false state produced by something that has nothing to do with
    reading the log.
    """
    for name in ("get_kernel_log_url", "resolve_tunnel_url"):
        hits = _topic_names_in(core, name)
        if hits:
            fails.append(f"D10: {name} reads the topic at lines {hits}")
            print(f"    ✗ {name} 在行 {hits} 讀了主題 —— 它會製造出假的 absent/unknown")
        else:
            print(f"    ✓ {name} 不碰主題")


# ── D11: the apply script's ten pins describe this tree ───────────────────


PIN_ORDER: list[tuple[str, str]] = [
    ("endpoint/core.py", "CORE"),
    ("endpoint/commands.py", "COMMANDS"),
    ("scripts/master_build_notebook.py", "NB"),
    ("endpoint/data/endpoint-config.example.yaml", "EXCONF"),
    ("endpoint/data/endpoint.1", "MANPAGE"),
]


def check_apply_pins(script_dir: Path, root: Path, fails: list[str]) -> None:
    script = script_dir / "apply-endpoint-topic-secret.sh"
    patch = script_dir / "endpoint-topic-secret.patch"
    if not script.is_file() or not patch.is_file():
        fails.append("D11: apply script or patch missing")
        print(f"    ✗ 找不到 {script.name} 或 {patch.name}")
        return

    text = script.read_text(encoding="utf-8")
    want_rels = [rel for rel, _ in PIN_ORDER]
    block = re.search(r"^RELS=\(\n(.*?)^\)$", text, re.S | re.M)
    rels = re.findall(r'"([^"]+)"', block.group(1)) if block else []
    if rels == want_rels:
        print(f"    ✓ 腳本釘的檔案清單就是這 {len(rels)} 個（順序也對）")
    else:
        fails.append(f"D11: apply script RELS is {rels!r}")
        print(f"    ✗ 腳本的檔案清單是 {rels!r}，應為 {want_rels!r}")

    pins: dict[tuple[str, str], str] = {}
    for key, kind, val in re.findall(r"^([A-Z_]+)_(PRISTINE|PATCHED)_SHA=([0-9a-f]+)$", text, re.M):
        pins[(key, kind)] = val

    if len(pins) != 2 * len(PIN_ORDER):
        fails.append(f"D11: expected {2 * len(PIN_ORDER)} pins, found {len(pins)}")
        print(f"    ✗ 釘了 {len(pins)} 個雜湊，應為 {2 * len(PIN_ORDER)}")
        return

    for rel, key in PIN_ORDER:
        pristine, patched = pins.get((key, "PRISTINE")), pins.get((key, "PATCHED"))
        if not pristine or not patched:
            fails.append(f"D11: {rel} has incomplete pins")
            print(f"    ✗ {rel} 的雜湊不齊")
            continue
        if len(pristine) != 64 or len(patched) != 64:
            fails.append(f"D11: {rel} pin is not 64 hex chars")
            print(f"    ✗ {rel} 的雜湊長度不對")
            continue
        if pristine == patched:
            fails.append(f"D11: {rel} has identical pristine/patched pins")
            print(f"    ✗ {rel} 的原／改雜湊相同 —— 補丁對它沒有作用")
            continue
        target = root / rel
        if not target.is_file():
            fails.append(f"D11: {rel} missing from the tree")
            print(f"    ✗ 樹裡沒有 {rel}")
            continue
        got = hashlib.sha256(target.read_bytes()).hexdigest()
        if got == pristine:
            print(f"    ✓ {rel} 是原始版（釘的雜湊正確）")
        elif got == patched:
            print(f"    ✓ {rel} 是修補後版（釘的雜湊正確）")
        else:
            fails.append(f"D11: {rel} pin does not describe this tree ({got})")
            print(f"    ✗ {rel} 的雜湊與兩個釘值都不符 —— 手寫的釘值有錯，或樹是第三種狀態")
            print(f"  → 實得 {got}")

    touched = set(re.findall(r"^--- a/(\S+)", patch.read_text(encoding="utf-8"), re.M))
    if touched == set(want_rels):
        print("    ✓ 補丁只碰這五個檔案（engine/engine.py 不在其中）")
    else:
        fails.append(f"D11: patch touches {sorted(touched)}")
        print(f"    ✗ 補丁碰的檔案是 {sorted(touched)}")


# ── D12: the setter never prints the value, and --dry-run writes nothing ──


def _run_setter(script: Path, home: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=dict(os.environ, HOME=str(home), ENDPOINT_TOOL_DIR=str(home / "no-such-tool")),
        timeout=120,
        check=False,
    )


def check_setter(script_dir: Path, fails: list[str]) -> None:
    """The setter is the one script that *holds* the secret; it must not say it.

    Driven for real, against a throwaway HOME, so the user's own config is never
    read or written. What is asserted here is the contract the header promises:
    only a fingerprint leaves the process, a dry run changes nothing, and a
    present secret is reported without being echoed.

    (f) additionally asks what the *liveness gate decides*. Everything else here
    is about the secret's shape and whereabouts, which is why the gate's polarity
    was wrong in the shipped script without this function noticing.
    """
    setter = script_dir / "set-endpoint-topic-secret.sh"
    if not setter.is_file():
        fails.append(f"D12: {setter.name} missing")
        print(f"    ✗ 找不到 {setter.name}")
        return

    scratch = Path(tempfile.mkdtemp(prefix="cutC-setter-"))
    try:
        home = scratch / "home"
        cfgdir = home / ".config" / "endpoint"
        cfgdir.mkdir(parents=True)
        cfg = cfgdir / "endpoint-config.yaml"
        cfg.write_text(
            "engine:\n  version: 0.1.2\nsignal:\n  topic_prefix: endpoint\n"
            f'  topic_secret: "{CANARY_SECRET}"\n',
            encoding="utf-8",
        )
        before = cfg.read_bytes()

        # (a) --dry-run: a fingerprint, nothing else, and no write.
        proc = _run_setter(setter, home, "--dry-run")
        out = proc.stdout
        if proc.returncode == 0:
            print("    ✓ --dry-run 結束碼 0")
        else:
            fails.append(f"D12: --dry-run exited {proc.returncode}")
            print(f"    ✗ --dry-run 結束碼 {proc.returncode}")
        fp_lines = [ln for ln in out.splitlines() if re.fullmatch(r"sha256:[0-9a-f]{12}", ln)]
        if len(fp_lines) == 1:
            print("    ✓ --dry-run 只印一個指紋行")
        else:
            fails.append(f"D12: --dry-run printed {len(fp_lines)} fingerprint lines")
            print(f"    ✗ --dry-run 印了 {len(fp_lines)} 個指紋行（應為 1）")
        hits = re.findall(r"[0-9a-f]{32}", out)
        if hits:
            fails.append("D12: --dry-run printed a 32-hex run")
            print("    ✗ --dry-run 的輸出裡有 32 個十六進位字元的字串 —— 那可能就是值")
        else:
            print("    ✓ --dry-run 的輸出裡沒有任何 32-hex 字串（值沒有離開行程）")
        if cfg.read_bytes() == before:
            print("    ✓ --dry-run 沒有改動 config（一個位元都沒有）")
        else:
            fails.append("D12: --dry-run modified the config")
            print("    ✗ --dry-run 改動了 config")

        # (b) --check on a usable secret: exit 0, and the secret stays unspoken.
        proc = _run_setter(setter, home, "--check")
        blobby = proc.stdout + proc.stderr
        if proc.returncode == 0:
            print("    ✓ --check 對可用的秘密回 0")
        else:
            fails.append(f"D12: --check exited {proc.returncode} with a good secret")
            print(f"    ✗ --check 在有可用秘密時回 {proc.returncode}")
        if CANARY_SECRET in blobby:
            fails.append("D12: --check echoed the secret")
            print("    ✗ --check 把秘密印出來了")
        else:
            print("    ✓ --check 只報告指紋，不報告值")

        # (c) --check with the key absent: exit 2, so a caller can branch on it.
        cfg.write_text("engine:\n  version: 0.1.2\nsignal:\n  topic_prefix: endpoint\n", "utf-8")
        proc = _run_setter(setter, home, "--check")
        if proc.returncode == 2:
            print("    ✓ --check 在沒有秘密時回 2（可分支，不是靜默通過）")
        else:
            fails.append(f"D12: --check exited {proc.returncode} with no secret")
            print(f"    ✗ --check 在沒有秘密時回 {proc.returncode}（應為 2）")

        # (d) The writer: value in via stdin, fingerprinted out, mode tightened.
        new_secret = "fedcba9876543210fedcba9876543210"
        proc = _run_setter(setter, home, "--stdin", "--yes", stdin=new_secret + "\n")
        if proc.returncode == 0:
            print("    ✓ --stdin --yes 寫入成功")
        else:
            fails.append(f"D12: writer exited {proc.returncode}")
            print(f"    ✗ 寫入回 {proc.returncode}")
        text = cfg.read_text(encoding="utf-8")
        if new_secret in text:
            print("    ✓ 值真的寫進去了（用 stdin，沒有經過 argv）")
        else:
            fails.append("D12: the new secret did not land in the config")
            print("    ✗ 新值沒有寫進 config")
        if new_secret in proc.stdout + proc.stderr:
            fails.append("D12: the writer echoed the new secret")
            print("    ✗ 寫入路徑把新值印出來了")
        else:
            print("    ✓ 寫入路徑也沒有印出值")
        mode = cfg.stat().st_mode & 0o777
        if mode == 0o600:
            print("    ✓ config 權限收成 0600")
        else:
            fails.append(f"D12: config mode is {mode:04o} after writing, want 0600")
            print(f"    ✗ 寫入後 config 權限是 {mode:04o}，應為 0600")
        if cfg.read_text(encoding="utf-8").count("topic_secret:") == 1:
            print("    ✓ 就地取代，沒有多出第二個 topic_secret")
        else:
            fails.append("D12: the writer duplicated the key")
            print("    ✗ 寫入後有兩個 topic_secret")

        # (e) The main mode: no --stdin, so the script must *generate* one. This
        # is the path that actually gets run, and it is the one a truthiness bug
        # silently redirects into the read-from-stdin branch -- where it blocks
        # on a TTY and dies on a pipe, having generated nothing. D12 (a) does not
        # catch that, because --dry-run exits before the fork.
        cfg.write_text("engine:\n  version: 0.1.2\nsignal:\n  topic_prefix: endpoint\n", "utf-8")
        proc = _run_setter(setter, home, "--yes")
        if proc.returncode == 0:
            print("    ✓ 不帶 --stdin 的主要模式會自己產生（不再誤入 stdin 分支）")
        else:
            fails.append(f"D12: the generating mode exited {proc.returncode}")
            print(f"    ✗ 主要模式回 {proc.returncode} —— 它應該自己產生一個")
        generated = re.search(r'topic_secret: "([^"]+)"', cfg.read_text(encoding="utf-8"))
        if generated and re.fullmatch(r"[0-9a-f]{32}", generated.group(1)):
            print("    ✓ 產生的是 32 個小寫十六進位字元，並插入了本來沒有的鍵")
        else:
            fails.append(f"D12: generated value is {generated.group(1) if generated else None!r}")
            print("    ✗ 產生的值形狀不對，或鍵沒有被插入")
        if generated and generated.group(1) in proc.stdout:
            fails.append("D12: the generated secret was echoed")
            print("    ✗ 產生的值被印出來了")

        # (f) The liveness arm. This is the hole the rest of this function had:
        # every other assertion here is about *the secret* (does it land, does it
        # leak, what shape is it), so none of them ever asked what the gate
        # *decides*. It decided wrong -- `ok:*` read "unknown", the one word
        # `get_kernel_status`'s own docstring says must never be read that way
        # ("Every path that reaches it is a *failure to ask*"), as "nothing is
        # running, go ahead". A stub `bin/python3` drives the real case arms with
        # each word the probe can produce, so what is measured is the shipped
        # control flow rather than a copy of it -- a copy would have drifted.
        #
        # The `running` row is a positive control and it is load-bearing: without
        # it, deleting the whole gate would satisfy the other three.
        stub_tool = scratch / "tool"
        (stub_tool / "bin").mkdir(parents=True)
        stub = stub_tool / "bin" / "python3"
        stub.write_text('#!/usr/bin/env bash\nprintf "ok\\t%s\\n" "${FAKE_WORD:-unknown}"\n',
                        encoding="utf-8")
        stub.chmod(0o755)

        def verdict(word: str) -> tuple[int, str]:
            cfg.write_text("engine:\n  version: 0.1.2\nsignal:\n  topic_prefix: endpoint\n"
                           f'  topic_secret: "{CANARY_SECRET}"\n', encoding="utf-8")
            proc = subprocess.run(
                ["bash", str(setter), "--yes"],
                capture_output=True, text=True,
                env=dict(os.environ, HOME=str(home), ENDPOINT_TOOL_DIR=str(stub_tool),
                         FAKE_WORD=word),
                timeout=120, check=False)
            blob = proc.stdout + proc.stderr
            if "拒絕改動" in blob:
                return proc.returncode, "refused"
            if "已經結束了，可以改" in blob:
                return proc.returncode, "safe"
            if "這不等於沒有東西在跑" in blob:
                return proc.returncode, "unknown"
            return proc.returncode, "none"

        rows = [(w, e, *verdict(w)) for w, e in (
            ("running", "refused"),   # positive control: the gate must still bite
            ("complete", "safe"),     # and must not over-correct into refusing all
            ("unknown", "unknown"),   # D-060 itself
            ("STOPPED", "unknown"),   # a word outside the vocabulary
        )]
        if all(got == want for _, want, _, got in rows):
            print("    ✓ 活性臂列舉安全字：" + "、".join(f"{w}→{g}" for w, _, _, g in rows))
        else:
            fails.append("D12: liveness polarity -- " + ", ".join(
                f"{w}: want {want} got {got}" for w, want, _, got in rows if want != got))
            print("    ✗ 活性臂的極性不對 —— 這一格以前整條電池都沒問過：")
            for word, want, rc, got in rows:
                print(f"      {'★' if want != got else ' '} {word:<9} 期望 {want:<8} "
                      f"實得 {got:<8}（結束碼 {rc}）")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ── D13: the apply script's secret gate actually runs and says the truth ──

# Set by D13 on the apply script it spawns, and inherited by the verifier that
# script spawns in turn. Without it the chain closes into a loop -- D13 runs the
# apply script, whose reverse gate runs this verifier, whose D13 runs the apply
# script -- so the guard is not an optimisation, it is what makes D13 existable.
REENTRY_VAR = "CUT_C_GATE_ALREADY_PROVEN"


def _fake_home(parent: Path, name: str, secret: str | None) -> Path:
    home = parent / name
    (home / ".config" / "endpoint").mkdir(parents=True)
    body = "engine:\n  version: 0.1.2\nsignal:\n  topic_prefix: endpoint\n"
    if secret is not None:
        body += f'  topic_secret: "{secret}"\n'
    (home / ".config" / "endpoint" / "endpoint-config.yaml").write_text(body, encoding="utf-8")
    return home


def _tree_fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def check_apply_gate(script_dir: Path, root: Path, fails: list[str]) -> None:
    """Drive the secret gate -- the one thing that makes the P0 state unreachable.

    It is deliberately driven through the *pristine* tree: on the patched tree the
    apply script exits early ("all five are already patched") and the gate never
    runs, so a broken gate would look exactly like a healthy one.

    Found here for real: `secret_probe` was defined and never called, and
    `SECRET_OK` was inverted on top of that. Two defects covering for each other
    -- `((SECRET_OK))` under `set -u` died with `unbound variable` first, so
    nobody ever saw that the polarity was backwards too. The script could not
    apply the patch at all, and the only symptom was one unreadable shell error.
    """
    if os.environ.get(REENTRY_VAR):
        print("    · 略過（重入：本驗證器是套用腳本叫起來的，外層已經證明過一次）")
        return

    apply_sh = script_dir / "apply-endpoint-topic-secret.sh"
    patch = script_dir / "endpoint-topic-secret.patch"
    if not apply_sh.is_file() or not patch.is_file():
        fails.append("D13: apply script or patch missing")
        print("    ✗ 找不到套用腳本或補丁")
        return

    scratch = Path(tempfile.mkdtemp(prefix="cutC-gate-"))
    try:
        pristine = scratch / "pristine"
        shutil.copytree(root, pristine, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        # `-f` 與 DEVNULL 兩個都要。少了它們，`patch` 在反轉不了的時候會問
        # `Ignore -R? [n]`，而 stdin 從終端繼承過來時它會**停在那裡等輸入** ——
        # 一個掛住的驗證器比一個失敗的驗證器糟得多，而且症狀看起來像當機。
        # `-f` 是「一律回答 n」：反轉不了就失敗，不要改成往前套用。
        rev = subprocess.run(
            ["patch", "-R", "-p1", "--fuzz=0", "-s", "-f", "-i", str(patch)],
            cwd=pristine,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=120,
            check=False,
        )
        if rev.returncode != 0:
            fails.append(f"D13: reverse-applying the patch failed ({rev.returncode})")
            print("    ✗ 反向套用補丁失敗 —— 無從取得原始樹來驅動閘門")
            print(f"  → {(rev.stderr or rev.stdout).strip().splitlines()[:1]}")
            return
        print("    ✓ 補丁反向套用成功（閘門需要一棵原始樹才跑得起來）")

        env = dict(os.environ, **{REENTRY_VAR: "1"})
        before = _tree_fingerprint(pristine)

        good = _fake_home(scratch, "good", CANARY_SECRET)
        proc = subprocess.run(
            ["bash", str(apply_sh), "--dry-run", "--target", str(pristine)],
            capture_output=True,
            text=True,
            env=dict(env, HOME=str(good)),
            timeout=600,
            check=False,
        )
        out = proc.stdout + proc.stderr
        if "找到可用的 signal.topic_secret" in out:
            print("    ✓ 有可用秘密時，閘門說「找到可用的」")
        else:
            fails.append("D13: the gate did not recognise a good secret")
            print("    ✗ 有可用秘密時閘門沒有說「找到可用的」—— 它會擋下正確的套用")
            for line in _gate_lines(out):
                print(f"  → {line}")
        if "沒有任何路徑提供可用的" in out:
            fails.append("D13: the gate refused a good secret")
            print("    ✗ 閘門同時也說「沒有任何路徑提供可用的」—— 極性反了")
        if proc.returncode == 0:
            print("    ✓ --dry-run 走完全程回 0")
        else:
            fails.append(f"D13: --dry-run exited {proc.returncode}")
            print(f"    ✗ --dry-run 回 {proc.returncode}（應為 0）")
            for line in _gate_lines(out):
                print(f"  → {line}")
        if _tree_fingerprint(pristine) == before:
            print("    ✓ --dry-run 沒有動到目標樹（一個位元都沒有）")
        else:
            fails.append("D13: --dry-run modified its target tree")
            print("    ✗ --dry-run 改動了目標樹")

        # And the other direction: a malformed secret must be refused, not
        # silently accepted. A gate that only ever says yes is not a gate.
        bad = _fake_home(scratch, "bad", "not-a-secret-at-all")
        proc = subprocess.run(
            ["bash", str(apply_sh), "--dry-run", "--target", str(pristine)],
            capture_output=True,
            text=True,
            env=dict(env, HOME=str(bad)),
            timeout=600,
            check=False,
        )
        out = proc.stdout + proc.stderr
        if "沒有任何路徑提供可用的" in out and "找到可用的" not in out:
            print("    ✓ 形狀不對的秘密會被拒絕")
        else:
            fails.append("D13: the gate accepted a malformed secret")
            print("    ✗ 形狀不對的秘密沒有被拒絕 —— 那不是閘門")
            for line in _gate_lines(out):
                print(f"  → {line}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _gate_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "秘密閘門" in ln or "signal.topic_secret" in ln][:4]


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
            "用法：test_endpoint_topic_secret.py <site-packages root>\n"
            "      test_endpoint_topic_secret.py <notebook> <core.py> <commands.py>",
            file=sys.stderr,
        )
        return 2
    script_dir, notebook, core, commands = paths
    for p in (notebook, core, commands):
        if not p.is_file():
            print(f"找不到檔案：{p}", file=sys.stderr)
            return 2

    # engine/engine.py sits above core.py, and the example config / man page sit
    # under endpoint/data/. A partial tree is a usage error, not a silent skip:
    # half the checks would quietly not run.
    root = core.parent.parent
    for extra in (root / "engine" / "engine.py", root / "endpoint" / "data" / "endpoint.1"):
        if not extra.is_file():
            print(
                f"找不到 {extra}\n"
                "這支驗證器需要一棵完整的 site-packages 樹：D9 要讀 engine/engine.py，"
                "D11 要讀 endpoint/data/ 底下隨套件出貨的兩個檔案。",
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
        ("D1", lambda: check_no_username_in_derivation(core, fails)),
        ("D2", lambda: check_topic_secret_property(core, fails)),
        ("D3", lambda: check_agreement(core, notebook, fails)),
        ("D4", lambda: check_fail_closed(core, notebook, fails)),
        ("D5", lambda: check_call_sites(notebook, fails)),
        ("D6", lambda: check_error_paths(core, commands, fails)),
        ("D7", lambda: check_bake(notebook, fails)),
        ("D8", lambda: check_cut_b_guard_intact(script_dir, root, fails)),
        ("D9", lambda: check_engine_untouched(root, fails)),
        ("D10", lambda: check_reader_ignores_topic(core, fails)),
        ("D11", lambda: check_apply_pins(script_dir, root, fails)),
        ("D12", lambda: check_setter(script_dir, fails)),
        # 放最後：它會叫起套用腳本，而套用腳本自己會跑兩次本驗證器（反向閘門
        # 與暫存空跑），所以它是這份清單裡唯一以「分鐘」計的檢查。
        ("D13", lambda: check_apply_gate(script_dir, root, fails)),
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
