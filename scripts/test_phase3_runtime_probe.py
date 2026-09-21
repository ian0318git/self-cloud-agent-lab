#!/usr/bin/env python3
"""phase3_runtime_probe.py 的離線測試 —— 不需要 docker、不需要 langgraph。

只測判定函式。量測（真的跑 langgraph、真的開子行程）留給
verify-phase3-runtime.sh；這裡測的是「量到之後怎麼判」，
而那一半正是本專案反覆出錯的地方，所以它必須能在沒有容器的機器上跑。

每個判定都要有**會死的**斷言（D-024 第八節）：只斷言「差距很大」的案例時，
把 `slack < 0` 寫成 `slack <= 0` 也不會被發現。所以每個判定的**邊界**
都是獨立一個案例。

結束碼：0 = 全部通過，1 = 有失敗。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase3_runtime_probe import (  # noqa: E402
    APSCHEDULER_DEFAULT_GRACE_SECONDS,
    RECURSION_LIMIT_HEADROOM,
    job_callable_verdict,
    recursion_guard_verdict,
    required_recursion_limit,
    scheduler_persistence_verdict,
    tight_limit_verdict,
)

FAILURES: list[str] = []
COUNT = 0


def check(label: str, got, want) -> None:
    global COUNT
    COUNT += 1
    if got != want:
        FAILURES.append(f"{label}\n      預期 {want!r}\n      得到 {got!r}")


def check_raises(label: str, fn, exc) -> None:
    global COUNT
    COUNT += 1
    try:
        fn()
    except exc:
        return
    except Exception as e:  # noqa: BLE001
        FAILURES.append(f"{label}\n      預期 {exc.__name__}，得到 {type(e).__name__}: {e}")
        return
    FAILURES.append(f"{label}\n      預期 {exc.__name__}，沒有拋出")


print("=" * 74)
print("phase3_runtime_probe 離線測試")
print()

# ── item 4：所需 limit ─────────────────────────────────────────────────────
print("item 4：required_recursion_limit")
check("super-steps=0（空圖）也要給出 1，不是 0", required_recursion_limit(0), 1)
check("super-steps=3", required_recursion_limit(3), 4)
check("headroom 常數必須是 1（量到的值）", RECURSION_LIMIT_HEADROOM, 1)
check_raises("負數要拋，不能靜默回一個值", lambda: required_recursion_limit(-1), ValueError)
print(f"  {COUNT} 項")

print()
print("item 4：recursion_guard_verdict —— 邊界就是全部")
before = COUNT
# super_steps=3 → 需要 4。三個 verdict 的分界正好落在 limit = 3 / 4 / 5。
check("limit=3 比所需少 1 → blocks", recursion_guard_verdict(3, 3)["verdict"], "blocks")
check("limit=4 剛好等於所需 → tight", recursion_guard_verdict(4, 3)["verdict"], "tight")
check("limit=5 多 1 → ample", recursion_guard_verdict(5, 3)["verdict"], "ample")
check("limit 差很多時仍是 blocks", recursion_guard_verdict(1, 99)["verdict"], "blocks")
check("slack 要回報實際差距（正）", recursion_guard_verdict(9, 3)["slack"], 5)
check("slack 要回報實際差距（負 1）", recursion_guard_verdict(3, 3)["slack"], -1)
check("slack 要回報實際差距（負很多）", recursion_guard_verdict(2, 3)["slack"], -2)
check("tight 的 slack 必須是 0", recursion_guard_verdict(4, 3)["slack"], 0)
# blocks 的訊息必須說出**後果**，否則它跟 tight 沒有可辨識的差別
msg = recursion_guard_verdict(3, 3)["message"]
check("blocks 訊息要點名 GraphRecursionError", "GraphRecursionError" in msg, True)
check("blocks 訊息要說 langgraph 不降級", "不會降級" in msg, True)
print(f"  {COUNT - before} 項")

print()
print("item 4：tight_limit_verdict —— 太緊的 limit 在哪裡停")
before = COUNT


def _row(limit, aborted, completed, nxt):
    return {"limit": limit, "aborted": aborted, "completed": completed, "next": nxt}


check("沒有掃描資料 → unknown", tight_limit_verdict([], 3)["verdict"], "unknown")
check("掃描裡沒有任何中止 → unknown（邊界不在範圍裡，不是「都沒問題」）",
      tight_limit_verdict([_row(1, False, 5, []), _row(2, False, 5, [])], 5)["verdict"], "unknown")
# 量到的實況：limit=5 時 5/5 節點跑完、next 空了，卻仍然中止
check("最後一個中止點做完全部且沒有下一步 → all_work_then_abort",
      tight_limit_verdict([_row(4, True, 4, ["n4"]), _row(5, True, 5, [])], 5)["verdict"],
      "all_work_then_abort")
# **這一對是區辨力的所在**：把兩個條件併成一個（只看 completed）就不會死。
check("完成了全部節點但還有下一步 → partial（邊界：next 非空就不算做完）",
      tight_limit_verdict([_row(5, True, 5, ["n5"])], 5)["verdict"], "partial_then_abort")
check("只完成一部分就中止 → partial",
      tight_limit_verdict([_row(2, True, 2, ["n2"])], 5)["verdict"], "partial_then_abort")
# 取的是**最大的**中止 limit：掃描裡前面的列一定也是中止的，不能用第一列。
check("要看最大的中止 limit，不是第一列",
      tight_limit_verdict([_row(1, True, 1, ["n1"]), _row(5, True, 5, [])], 5)["verdict"],
      "all_work_then_abort")
# 理由必須說出「工作做完了卻被擋」，否則它跟 partial 沒有可辨識的差別
_aw = tight_limit_verdict([_row(5, True, 5, [])], 5)
check("all_work 的理由要講出全部做完卻中止", "全部做完" in _aw["message"], True)
check("all_work 要回報是哪個 limit", _aw["worst_limit"], 5)
print(f"  {COUNT - before} 項")

# ── item 6：job store ──────────────────────────────────────────────────────
print()
print("item 6：scheduler_persistence_verdict —— 邊界在 lateness == grace")
before = COUNT
check("預設寬限常數必須是 1 秒（量到的值）", APSCHEDULER_DEFAULT_GRACE_SECONDS, 1)
check("記憶體 store：逾期 0 秒也是 lost_with_store",
      scheduler_persistence_verdict("memory", 0.0)["verdict"], "lost_with_store")
check("記憶體 store：逾時很久也是 lost_with_store",
      scheduler_persistence_verdict("memory", 99999.0)["verdict"], "lost_with_store")
# 邊界：逾期 == 寬限 → 剛好趕上，要跑
check("逾期正好等於寬限 → runs",
      scheduler_persistence_verdict("persistent", 1.0, 1.0)["verdict"], "runs")
check("逾期比寬限多一點 → dropped_as_misfire",
      scheduler_persistence_verdict("persistent", 1.001, 1.0)["verdict"], "dropped_as_misfire")
check("逾期比寬限少一點 → runs",
      scheduler_persistence_verdict("persistent", 0.999, 1.0)["verdict"], "runs")
# 量到的實測邊界：逾期 2.2 秒、grace=2 丟掉、grace=3 跑
check("實測點：逾期 2.2s / grace 2 → 丟", scheduler_persistence_verdict("persistent", 2.2, 2.0)["verdict"],
      "dropped_as_misfire")
check("實測點：逾期 2.2s / grace 3 → 跑", scheduler_persistence_verdict("persistent", 2.2, 3.0)["verdict"],
      "runs")
check("grace=None 表示不限，逾期多久都跑",
      scheduler_persistence_verdict("persistent", 86400.0, None)["verdict"], "runs")
check("runs 布林要與 verdict 一致（runs=True）",
      scheduler_persistence_verdict("persistent", 0.1, 1.0)["runs"], True)
check("runs 布林要與 verdict 一致（runs=False）",
      scheduler_persistence_verdict("persistent", 9.0, 1.0)["runs"], False)
# dropped 的訊息必須把它跟「持久化失效」分開 —— 那是最容易誤讀的一種
dmsg = scheduler_persistence_verdict("persistent", 5.0, 1.0)["message"]
check("dropped 訊息要說 job 在 store 裡還在過", "還在過" in dmsg, True)
check("dropped 訊息要說它自我移除、所以兩種結果長得一樣", "一模一樣" in dmsg, True)
check_raises("store_kind 打錯要拋，不能靜默當成 memory",
             lambda: scheduler_persistence_verdict("mem", 1.0), ValueError)
check_raises("負的逾期時間要拋", lambda: scheduler_persistence_verdict("persistent", -1.0), ValueError)
print(f"  {COUNT - before} 項")

print()
print("item 6：job_callable_verdict —— 持久化的必要代價")
before = COUNT
check("記憶體 store 收得下非模組層級", job_callable_verdict("memory", False)["accepted"], True)
check("持久化 store 拒收非模組層級", job_callable_verdict("persistent", False)["accepted"], False)
check("持久化 store 收得下模組層級", job_callable_verdict("persistent", True)["accepted"], True)
check("拒收的訊息要點名 ValueError",
      "ValueError" in job_callable_verdict("persistent", False)["message"], True)
check_raises("store_kind 打錯要拋",
             lambda: job_callable_verdict("disk", True), ValueError)
print(f"  {COUNT - before} 項")

# ── 結果 ───────────────────────────────────────────────────────────────────
print()
print("=" * 74)
if FAILURES:
    print(f"失敗 {len(FAILURES)}/{COUNT}：")
    for f in FAILURES:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"全部通過：{COUNT} 項斷言")
sys.exit(0)
