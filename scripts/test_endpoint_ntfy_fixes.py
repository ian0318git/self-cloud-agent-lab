#!/usr/bin/env python3
"""Behavioural verifier for the two ntfy defects in the endpoint notebook generator.

    python3 scripts/test_endpoint_ntfy_fixes.py <master_build_notebook.py> [...]

The generator has no test suite of its own, and both defects are invisible from
reading the code: one is a rate limit that only shows up over minutes, the other
hides behind its own `except Exception: pass`. So this drives the code the
generator actually emits -- its own string literals, i.e. literally the text that
would land in the notebook -- against a fake ntfy and a fake clock.

  1. signal() throttling. 628s of HF download progress used to emit 628
     messages; ntfy.sh allows ~60 burst then one per 5s per IP, and this IP also
     spends that budget polling. The bucket drained and the one-shot
     TUNNEL ACQUIRED was discarded with a 429 that signal() swallowed. PASS =
     demand fits the budget AND the tunnel message survives the bucket.

  2. check_kill_signals(). /raw without poll=1 is a blocking subscribe, so a
     KILL replayed at connect time sat unread until the socket closed (1923s
     into a healthy run) and then killed it. PASS = a KILL published before this
     session started is ignored, one published after is obeyed, and a
     rate-limited watermark fetch fails closed.

The budget is modelled as an actual token bucket, so "the tunnel URL survives"
is demonstrated rather than inferred from arithmetic. The verdict needs both:
the arithmetic test is phase-independent (a design that survives only because
the final message happened to land on a refill boundary is still broken), while
the bucket replay shows what that arithmetic does to the message that matters.

NOTE ON THE VERDICT: a pristine generator FAILS check 1 on purpose -- that is the
bug being demonstrated. Run it in both directions: the patched file must pass,
the pristine file must fail. A verifier that passed against both would be
checking nothing. scripts/apply-endpoint-ntfy-fixes.sh does exactly that.

Exit 0 only if every file passed every check.
"""

from __future__ import annotations

import ast
import sys
import threading

# --------------------------------------------------------------------------
# Extracting the emitted notebook source out of the generator
# --------------------------------------------------------------------------


def _quiet(*a, **kw) -> None:
    """signal() prints every message it sends. 628 download-progress lines would
    bury the verdict, so the emitted print is swapped for this."""
    return None


def gen_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


def _block(lines: list[str], start_marker: str, end_marker: str) -> str:
    """Join the generator's string literals between two markers."""
    i = next(k for k, l in enumerate(lines) if start_marker in l)
    j = next(k for k, l in enumerate(lines) if k > i and end_marker in l)
    out = []
    for line in lines[i:j]:
        s = line.strip()
        if s.endswith(","):
            s = s[:-1]
        s = s.strip()
        if s.startswith(('"', "'")):
            out.append(ast.literal_eval(s))
    return "".join(out)


def emitted(lines: list[str], markers: list[str], end_marker: str) -> str:
    """Join emitted source starting from the first marker the file actually has.

    The patch adds a preamble (_sig_lock / _sig_last / SIG_MIN_GAP) ABOVE
    `def signal(m`. Extracting from the def alone would leave those undefined,
    and signal()'s own bare `except Exception: pass` would swallow the
    NameError -- reporting "0 messages sent" as though the throttle worked.
    """
    for m in markers:
        if any(m in l for l in lines):
            return _block(lines, m, end_marker)
    raise AssertionError(f"none of {markers} found")


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class Horizon(BaseException):
    """Ends a run. BaseException on purpose.

    Both kill-switch variants end in `while True: ... time.sleep(n)`. With an
    instant clock that is an infinite spin, so the clock has to cut the thread
    off -- and because the emitted code wraps its body in `except Exception`,
    anything derived from Exception would be swallowed and an endless loop
    would read as a clean pass.
    """


class FakeTime:
    def __init__(self, horizon: int = 10**9) -> None:
        self.now = 1_000_000.0
        self.horizon = horizon

    def time(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        # Instant, so a 628-second download simulates in microseconds while the
        # rate arithmetic stays honest.
        self.now += secs
        self.horizon -= 1
        if self.horizon < 0:
            raise Horizon()


class Fake:
    """Records what the emitted code would have sent, and fakes its I/O."""

    def __init__(self, horizon: int = 10**9) -> None:
        self.publishes: list[tuple] = []   # (time, payload) at _sp.Popen
        self.sent: list[str] = []          # payloads via the `signal` shim
        self.watermark_calls: list[str] = []  # _sp.run URLs (the /json drain)
        self.poll_calls: list[str] = []    # os.popen commands (the kill poll)
        self.drain_response = ""
        self.popen_response = ""
        self.os_exits: list[int] = []
        self.time = FakeTime(horizon)

        outer = self

        class _Sub:
            DEVNULL = object()

            def Popen(self, argv, **kw):
                # ['curl','-s','-d',payload,url]
                outer.publishes.append((outer.time.time(), argv[3]))

            def run(self, argv, **kw):
                outer.watermark_calls.append(argv[-1])
                return type("R", (), {"stdout": outer.drain_response})()

        class _Os:
            @staticmethod
            def popen(cmd):
                outer.poll_calls.append(cmd)
                return type("P", (), {"read": lambda self: outer.popen_response})()

            @staticmethod
            def system(cmd):
                return 0

            @staticmethod
            def _exit(code):
                outer.os_exits.append(code)
                raise SystemExit(code)

        self._sp = _Sub()
        self._os = _Os()


def load_signal(lines: list[str], fake: Fake):
    src = emitted(lines, ["_sig_lock = threading.Lock()", "def signal(m)"],
                  "def broadcast(url)")
    ns = {
        "threading": threading,
        "time": fake.time,          # the clock, not the Fake itself
        "SESSION_ID": "deadbeef",
        "SIGNAL_TOPIC": "topic",
        "_sp": fake._sp,
        "print": _quiet,
    }
    exec(compile(src, "<emitted signal()>", "exec"), ns)
    return ns["signal"]


def load_kill(lines: list[str], fake: Fake) -> tuple:
    src = _block(lines, "def check_kill_signals():",
                 "threading.Thread(target=check_kill_signals")
    ns = {
        "os": fake._os,
        "time": fake.time,
        "json": __import__("json"),
        "_sp": fake._sp,
        "CONTROL_TOPIC": "topic-control",
        "signal": lambda m, **kw: fake.sent.append(m),
        "print": _quiet,
    }
    exec(compile(src, "<emitted check_kill_signals()>", "exec"), ns)
    # The patch is what introduces the watermark; its absence identifies the
    # pristine variant, which reads the topic a completely different way.
    return ns["check_kill_signals"], "_since" in src


def send(sig, msg, progress=False):
    """Pre-patch signal() takes no progress= kwarg, and the call sites that pass
    it are added by the patch -- so run both variants through one path."""
    try:
        sig(msg, progress=progress)
    except TypeError:
        sig(msg)


# --------------------------------------------------------------------------
# ntfy.sh's per-visitor budget
# --------------------------------------------------------------------------

DOWNLOAD_SECS = 628      # measured: MODEL DOWNLOAD COMPLETED (4795 MB in 628s)
BURST, REFILL = 60, 0.2  # ~60 requests at once, then one per 5s
OLD_POLL_SECS, NEW_POLL_SECS = 10, 20

STALE = '{"id":"ID_STALE","event":"message","message":"KILL:1790176428"}'
FRESH = '{"id":"ID_NEW","event":"message","message":"KILL:1790199999"}'


class Bucket:
    """One token per request, BURST to start, REFILL per second thereafter."""

    def __init__(self) -> None:
        self.tokens = float(BURST)
        self.last: float | None = None

    def take(self, now: float) -> bool:
        if self.last is None:
            self.last = now
        self.tokens = min(BURST, self.tokens + (now - self.last) * REFILL)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def replay(publishes: list[tuple], poll_secs: int, duration: float) -> tuple:
    """Merge the real publishes with the modelled control polls and run them
    through the bucket, in time order. Returns (landed payloads, dropped
    publishes, dropped polls)."""
    if not publishes:
        return [], 0, 0
    start = publishes[0][0]
    events = [(t, True, p) for t, p in publishes]
    k = 1
    while k * poll_secs <= duration:
        events.append((start + k * poll_secs, False, ""))
        k += 1
    events.sort(key=lambda e: e[0])

    bucket = Bucket()
    landed, dropped_pub, dropped_poll = [], 0, 0
    for t, is_pub, payload in events:
        if bucket.take(t):
            if is_pub:
                landed.append(payload)
        elif is_pub:
            dropped_pub += 1
        else:
            dropped_poll += 1
    return landed, dropped_pub, dropped_poll


def check_throttle(lines: list[str], fails: list[str]) -> None:
    fake = Fake()
    sig = load_signal(lines, fake)

    # signal() swallows every exception, so a broken harness is indistinguishable
    # from a working throttle. Make silence loud.
    send(sig, "sanity")
    if not fake.publishes:
        print("    護欄：第一則訊號就送不出去 —— 測試環境本身壞了，"
              "不是節流生效")
        fails.append("harness broken: signal() sent nothing")
        return
    fake.publishes.clear()

    for i in range(DOWNLOAD_SECS):
        send(sig, f"HF PROGRESS: {i}%", progress=True)
        fake.time.sleep(1.0)
    send(sig, "TUNNEL ACQUIRED: https://x.trycloudflare.com")

    # The pristine variant polls every 10s, the patched one every 20s.
    poll_secs = OLD_POLL_SECS if "since=5m" in "\n".join(lines) else NEW_POLL_SECS
    npolls = int(DOWNLOAD_SECS / poll_secs)
    landed, dropped_pub, dropped_poll = replay(fake.publishes, poll_secs,
                                               DOWNLOAD_SECS)

    demand = len(fake.publishes) + npolls
    supply = BURST + REFILL * DOWNLOAD_SECS
    fits = demand <= supply
    # Ordering matters: the tunnel URL is only useful if it is the last thing
    # the client actually received.
    tunnel_ok = bool(landed) and "TUNNEL ACQUIRED" in landed[-1]

    print(f"    下載期間呼叫 signal()    {len(fake.publishes):>5} 則")
    print(f"    加上控制輪詢的總需求     {demand:>5} 次"
          f"（輪詢每 {poll_secs}s 一次，估入 {npolls} 次）")
    print(f"    ntfy 同期可給的額度      {supply:>5.0f} 次")
    print(f"    模擬桶子：實際送達 {len(landed)} 則、"
          f"被丟 {dropped_pub} 則（另有 {dropped_poll} 次輪詢被丟）")
    print(f"    TUNNEL ACQUIRED 送達     {'是' if tunnel_ok else '否'}")

    if not fits:
        print(f"    ✗ 需求是額度的 {demand / supply:.1f} 倍 —— 速率上就不可能"
              f"持續，與相位無關")
        fails.append("demand exceeds the ntfy budget")
    if not tunnel_ok:
        print("    ✗ 一次性的 tunnel 訊息被桶子丟掉了（signal() 對此保持沉默）")
        fails.append("TUNNEL ACQUIRED was dropped")
    if fits and tunnel_ok:
        print("    ✓ 在額度內，且關鍵訊息送達")


def check_kill(lines: list[str], fails: list[str]) -> None:
    def run(stale: bool, fresh: bool, rate_limited: bool):
        fake = Fake(horizon=1)
        fn, patched = load_kill(lines, fake)
        cache = (STALE if stale else "") + (FRESH if fresh else "")
        if patched:
            # The watermark drain replays the cache; the since= poll afterwards
            # returns only what is newer than the watermark.
            fake.drain_response = (
                "rate limit exceeded HTTP:429" if rate_limited
                else cache + " HTTP:200"
            )
            fake.popen_response = FRESH if fresh else ""
        else:
            # /raw?since=5m replays the whole window on connect.
            fake.popen_response = cache

        def target():
            try:
                fn()
            except (Horizon, SystemExit):
                pass

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=2.0)
        return fake, patched

    # A: the topic holds only the KILL that boot published before we started.
    a, patched = run(stale=True, fresh=False, rate_limited=False)
    # B: a KILL published after this session came up -- `stop` must still work.
    b, _ = run(stale=True, fresh=True, rate_limited=False)
    # C: the watermark fetch is rate-limited -- must retry, not fall through.
    c, _ = run(stale=True, fresh=False, rate_limited=True)

    a_ok = not a.os_exits
    b_ok = bool(b.os_exits)
    # Retrying the watermark is the correct response to a 429; what must never
    # happen is reaching the polling loop without one.
    c_ok = patched and not c.poll_calls and not c.os_exits

    print(f"    A 只有開機前那則 KILL      {'✓ 不服從' if a_ok else '✗ 自殺'}")
    print(f"    B 開機後才發布的 KILL      "
          f"{'✓ 服從並終止' if b_ok else '✗ 沒反應（stop 會失效）'}")
    if patched:
        print(f"    C 取水位被 429            "
              f"{'✓ 重試 %d 次、沒有往下走' % len(c.watermark_calls) if c_ok else '✗ 沒有水位就開始輪詢'}")
    else:
        print("    C 取水位被 429            — 原始版沒有水位可取，略過")

    if not a_ok:
        fails.append("obeys the stale pre-start KILL (self-destruct)")
    if not b_ok:
        fails.append("does not obey a genuine KILL")
    if patched and not c_ok:
        fails.append("falls through without a watermark when rate-limited")


def main(argv: list[str]) -> int:
    paths = argv[1:]
    if not paths:
        print("用法：test_endpoint_ntfy_fixes.py <master_build_notebook.py> ...",
              file=sys.stderr)
        return 2

    failed: list[str] = []
    for path in paths:
        print("=" * 72)
        print(path)
        print("=" * 72)
        lines = gen_lines(path)
        local: list[str] = []

        try:
            check_throttle(lines, local)
        except Exception as exc:  # noqa: BLE001 - a broken target is a failure, not a crash
            print(f"    ✗ 節流檢查無法執行：{type(exc).__name__}: {exc}")
            local.append(f"throttle check could not run ({type(exc).__name__}: {exc})")
        print()
        try:
            check_kill(lines, local)
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ kill 檢查無法執行：{type(exc).__name__}: {exc}")
            local.append(f"kill check could not run ({type(exc).__name__}: {exc})")

        print()
        if local:
            print(f"  → {path}：{len(local)} 項未過")
            failed += [f"{path}: {f}" for f in local]
        else:
            print(f"  → {path}：全部通過")
        print()

    print("=" * 72)
    if failed:
        for f in failed:
            print(f"✗ {f}")
        return 1
    print("✓ 全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
