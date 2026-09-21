#!/usr/bin/env python3
"""第三階段 item 4（recursion_limit）與 item 6（APScheduler 持久化）的判定與量測。

**這裡的每個常數都是量到的，不是讀文件推的。** 量測腳本與原始輸出記在 D-030；
這個檔案只負責把那些量測變成可重複執行的判定。

兩項的判定邏輯都不長，但都屬於「安靜地錯」那一類，所以它們是純函式、可離線測：

  item 4 —— `recursion_limit` 設得太緊時，langgraph 不會降級，它會**報錯中止一個
            完全正常的計畫**。從外面看，「擋下失控的迴圈」與「砍掉正常的計畫」
            是同一個 GraphRecursionError。判定必須說出是哪一種。

  item 6 —— job store 換成持久化之後，重啟時**看得到** job 不代表它**會跑**。
            APScheduler 預設 1 秒的 misfire 寬限會把停機期間到期的 job 直接丟掉，
            而 `date` 觸發器跑完會自我移除 —— 所以「跑掉了」與「被丟掉了」在
            store 裡長得一模一樣。沒有外部日誌就分不出來。

結束碼（D-018）：
  0 = 量到的行為符合判定
  1 = 量到的行為**違反**判定（上游變了）
  2 = 量不到（環境不具備，例如映像沒有 apscheduler）—— 不代表通過
  3 = 探針自己壞了（判定函式對自己的測試案例給不出答案）
"""

from __future__ import annotations

import importlib.metadata as md
import contextlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 2
EXIT_BROKEN = 3

# 量到的兩個常數（D-030）——改動它們就必須重跑量測，見 verify-phase3-runtime.sh
#
# 1. 所需 limit = super-step 總數 + 1。量測方式：對多種拓樸二分搜尋
#    「完成所需的最小 limit」，再與獨立量到的 super-step 數比對。
RECURSION_LIMIT_HEADROOM = 1
#
# 2. APScheduler 的 misfire_grace_time 預設值（秒）。量測方式：固定逾期 2.2 秒，
#    掃 grace = default/1/2/3/10，邊界落在 grace 與逾期時間之間。
APSCHEDULER_DEFAULT_GRACE_SECONDS = 1


# ── item 4：recursion_limit ────────────────────────────────────────────────
def tight_limit_verdict(scan: list[dict[str, Any]], node_count: int) -> dict[str, Any]:
    """太緊的 limit 實際上會怎樣？判定「在哪裡停」與「停下來之後剩什麼」。

    `scan` 的每一列是：{"limit": int, "aborted": bool, "completed": int, "next": list}
    由量測端產生（linear(N) 逐個 limit 試）。這一函式只負責判讀。

    這一條是 item 4 的實務重點，而且**與「+1 通則」是不同的兩件事**：
    通則說「需要多少」，這裡說「不夠的時候會發生什麼」。量到的答案是：

      limit = node_count 時，**每一個節點都跑完了、next 也空了**，卻仍然中止 ——
      那個 +1 花在「收尾」那一步上。也就是說守衛是在工作**全部做完之後**才響的：
      你做完了 100% 的事，拿到的是一個例外而不是結果。

    這種失敗最容易被誤讀成「圖太大了」，於是把 limit 調大當成解法；
    真正的問題是它把「計畫正常」與「圖失控」兩種情況報成**同一個例外型別**。
    """
    aborted = [r for r in scan if r["aborted"]]
    if not scan:
        return {"verdict": "unknown", "message": "沒有掃描資料，無法判定。"}
    if not aborted:
        # 掃描範圍裡沒有任何 limit 會中止 —— 這代表掃描本身沒有涵蓋到邊界
        return {"verdict": "unknown",
                "message": "掃描範圍內沒有任何 limit 中止 —— 邊界不在掃描範圍裡，這一輪沒量到。"}
    worst = max(aborted, key=lambda r: r["limit"])
    all_done = worst["completed"] == node_count and not worst["next"]
    if all_done:
        return {
            "verdict": "all_work_then_abort",
            "worst_limit": worst["limit"],
            "message": f"limit={worst['limit']} 時 {node_count}/{node_count} 個節點都跑完了、"
                       f"沒有下一步，卻仍然中止 —— 那個 +1 花在收尾那一步上。"
                       f"守衛在工作全部做完之後才響。",
        }
    return {
        "verdict": "partial_then_abort",
        "worst_limit": worst["limit"],
        "message": f"limit={worst['limit']} 時只完成 {worst['completed']}/{node_count} 個節點就中止。",
    }


def required_recursion_limit(super_steps: int) -> int:
    """跑完一個會執行 `super_steps` 個 super-step 的圖，所需的最小 limit。

    量到的通則。注意它數的是 **super-step**，不是節點執行次數 —— 這正是
    README 原本的猜測，也是唯一需要量測才能確認的部分：14 個「每個 super-step
    恰好一個節點」的圖形**無法區分**這兩種講法。平行扇出才能：
    width=1,2,3,4,6 全部給出同一個 limit ⇒ 數的是 super-step。
    """
    if super_steps < 0:
        raise ValueError(f"super_steps 不能是負數：{super_steps}")
    return super_steps + RECURSION_LIMIT_HEADROOM


def recursion_guard_verdict(limit: int, super_steps: int) -> dict[str, Any]:
    """這個 `recursion_limit` 對這個圖而言是什麼？

    回傳 verdict 為下列之一：

      `ample`    —— 有餘裕。正常計畫跑得完，還留得下重試的空間。
      `tight`    —— 剛好夠。跑得完，但**零餘裕**：任何一次重試都會撞牆。
      `blocks`   —— **不夠**。這不是「擋下失控」，是「砍掉正常計畫」。

    `blocks` 是這一項存在的理由。它必須在訊息裡講清楚：langgraph 不會降級、
    不會回傳部分結果、不會說「超出上限但這是你要的嗎」—— 它丟
    GraphRecursionError，而那個例外跟「迴圈失控被擋下」是同一個型別。
    分不出來的檢查等於沒有檢查（D-024 第八節）。
    """
    need = required_recursion_limit(super_steps)
    slack = limit - need

    if slack < 0:
        return {
            "verdict": "blocks",
            "required": need,
            "slack": slack,
            "message": (
                f"recursion_limit={limit} 對這個圖**不夠**：它需要 {need}。"
                f"langgraph 不會降級，會丟 GraphRecursionError 中止一個正常的計畫。"
                f"這個錯誤與『迴圈失控被擋下』是同一個型別，從例外分不出來。"
            ),
        }
    if slack == 0:
        return {
            "verdict": "tight",
            "required": need,
            "slack": 0,
            "message": (
                f"recursion_limit={limit} 剛好等於所需。跑得完，但**零餘裕**——"
                f"任何一次重試都會變成 blocks。"
            ),
        }
    return {
        "verdict": "ample",
        "required": need,
        "slack": slack,
        "message": f"recursion_limit={limit} 對需要 {need} 的圖有 {slack} 步餘裕。",
    }


# ── item 6：APScheduler 的 job store ───────────────────────────────────────
def scheduler_persistence_verdict(
    store_kind: str,
    lateness_seconds: float,
    grace_seconds: float | None = None,
) -> dict[str, Any]:
    """重啟之後，一個「停機期間到期」的 job 會不會跑？

    `store_kind`       ：`memory` 或 `persistent`
    `lateness_seconds` ：job 的到期時間距離排程器重新啟動多久（秒）
    `grace_seconds`    ：`misfire_grace_time`；`None` 表示不設寬限

    回傳 verdict 為下列之一：

      `lost_with_store`   —— 記憶體 store，重啟即消失。
      `dropped_as_misfire`—— **持久化 store，job 還在，但不會跑。**
      `runs`              —— 會跑。

    `dropped_as_misfire` 才是這一項的重點，而且它很容易被誤讀成「持久化沒生效」。
    不是：持久化**生效了**（量到重啟後 store 裡確實有那個 job），是 APScheduler
    的 misfire 規則把它丟掉了。兩者的處置完全不同。
    """
    if store_kind not in ("memory", "persistent"):
        raise ValueError(f"store_kind 只能是 memory/persistent，收到 {store_kind!r}")
    if lateness_seconds < 0:
        raise ValueError(f"lateness_seconds 不能是負數：{lateness_seconds}")

    if store_kind == "memory":
        return {
            "verdict": "lost_with_store",
            "runs": False,
            "message": "記憶體 job store：行程結束時 job 就沒了，重啟後 store 裡是空的。",
        }

    if grace_seconds is None:
        return {
            "verdict": "runs",
            "runs": True,
            "message": f"持久化 store，未設寬限：逾期 {lateness_seconds}s 仍會補跑。",
        }

    if grace_seconds < 0:
        raise ValueError(f"grace_seconds 不能是負數：{grace_seconds}")

    if lateness_seconds > grace_seconds:
        return {
            "verdict": "dropped_as_misfire",
            "runs": False,
            "message": (
                f"持久化 store，但逾期 {lateness_seconds}s > 寬限 {grace_seconds}s："
                f"job 被當成 misfire **丟掉**。它在 store 裡還在過，只是永遠不會跑，"
                f"而且沒有任何錯誤訊息——`date` 觸發器跑完會自我移除，所以"
                f"『跑掉了』與『被丟掉了』在 store 裡長得一模一樣。"
            ),
        }
    return {
        "verdict": "runs",
        "runs": True,
        "message": (
            f"持久化 store，逾期 {lateness_seconds}s <= 寬限 {grace_seconds}s：會補跑。"
        ),
    }


def job_callable_verdict(store_kind: str, is_module_level: bool) -> dict[str, Any]:
    """這個 job 函式能被這個 store 收下嗎？

    量到的落差：`MemoryJobStore` 收得下 lambda，`SQLAlchemyJobStore` 當場
    `ValueError: This Job cannot be serialized since the reference to its callable…`。

    所以「換成持久化 store」不是 drop-in —— 它會回頭限制你能排什麼。
    這個落差值得一個獨立的判定，因為它只在**第一次真的用到非模組層級函式時**
    才會爆，而那可能是上線之後。
    """
    if store_kind not in ("memory", "persistent"):
        raise ValueError(f"store_kind 只能是 memory/persistent，收到 {store_kind!r}")
    if store_kind == "memory":
        return {
            "accepted": True,
            "message": "記憶體 store 不序列化 job，任何可呼叫物件都收得下。",
        }
    if is_module_level:
        return {
            "accepted": True,
            "message": "持久化 store 需要可 pickle 的 job；模組層級函式可以。",
        }
    return {
        "accepted": False,
        "message": (
            "持久化 store 會拒絕非模組層級的 job（lambda／閉包／區域函式），"
            "當場丟 ValueError。這是改用持久化 store 的**必要代價**，不是 bug。"
        ),
    }


# ── 量測（需要 langgraph／apscheduler，只在探針映像裡跑）────────────────────
def _recursion_probe() -> dict[str, Any]:
    """量 super-step 通則：多個拓樸，二分搜尋最小 limit，與獨立量到的 super-step 比對。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.errors import GraphRecursionError
    from langgraph.graph import END, START, StateGraph

    def linear(n: int):
        g = StateGraph(dict)
        for i in range(n):
            # 節點要**留下痕跡**，否則中斷之後讀 state 只會讀到空的 ——
            # 那會讓「完成了幾個節點」永遠是 0，而 0 會安靜地看起來像
            # 「什麼都沒跑」，而不是「我的節點函式什麼都沒寫」。
            g.add_node(f"n{i}", _mark(i))
        for i in range(n):
            g.add_edge(START if i == 0 else f"n{i-1}", f"n{i}")
        g.add_edge(f"n{n-1}", END)
        return g.compile(checkpointer=MemorySaver())

    def fanout(width: int, rounds: int):
        """head → width 個平行節點（**同一個 super-step**）→ join → 回頭。

        平行是關鍵：它是唯一能把「數節點」與「數 super-step」分開的拓樸。
        """
        g = StateGraph(dict)
        g.add_node("head", lambda s: {**s, "r": s.get("r", 0) + 1})
        for i in range(width):
            g.add_node(f"w{i}", lambda s: None)
            g.add_edge("head", f"w{i}")
            g.add_edge(f"w{i}", "join")
        g.add_node("join", lambda s: s)
        g.add_edge(START, "head")
        g.add_conditional_edges(
            "join", lambda s: "head" if s["r"] < rounds else "stop",
            {"head": "head", "stop": END})
        return g.compile(checkpointer=MemorySaver())

    def min_limit(builder, hi: int = 8000, **kw) -> int:
        lo = 1
        while lo < hi:
            mid = (lo + hi) // 2
            try:
                # thread_id 是 checkpointer 要求的；每次二分搜尋用新的 thread，
                # 免得上一輪的 checkpoint 被下一輪讀到（那會讓量測取決於搜尋歷史）
                builder(**kw).invoke({}, config={
                    "recursion_limit": mid,
                    "configurable": {"thread_id": f"limit-{mid}"}})
                hi = mid
            except GraphRecursionError:
                lo = mid + 1
        return lo

    def super_steps(builder, **kw) -> int:
        """**langgraph 自己的** super-step 計數器。

        這一段是這一項的教訓所在，所以寫得囉唆一點：我的第一版用「數節點執行次數」
        當獨立儀器，還把那個變數取名為 `super_steps` —— 於是它在平行扇出上回報 4、
        真實值是 3，而我的表格把這件事印成「判定不成立」。**名字騙了我自己**：
        那個函式量的是節點執行數，不是 super-step，兩者在扇出上必然不同。

        現在改成讀 `get_state().metadata["step"]`；那是 langgraph 記在 checkpoint
        裡的計數，不是我算的。需要 checkpointer 才讀得到，所以上面的兩個 builder
        都掛了 `MemorySaver`。
        """
        # 必須用**同一個 app 實例**：每個 builder() 都建一個新的 MemorySaver，
        # 拿另一個實例去 get_state 只會拿到空 metadata（我第一版就是這樣）。
        app = builder(**kw)
        cfg = {"configurable": {"thread_id": f"probe-{builder.__name__}-{sorted(kw.items())}"}}
        app.invoke({}, config=cfg)
        return int((app.get_state(cfg).metadata or {}).get("step", -1))

    rows = []
    for label, builder, kw in (
        *[(f"linear-{n}", linear, {"n": n}) for n in (1, 3, 5)],
        *[(f"fanout-w{w}-r{r}", fanout, {"width": w, "rounds": r})
          for w in (1, 2, 4) for r in (1, 2)],
    ):
        ss = super_steps(builder, **kw)
        m = min_limit(builder, **kw)
        rows.append({"graph": label, "super_steps": ss, "min_limit": m,
                     "predicted": required_recursion_limit(ss), "ok": m == required_recursion_limit(ss)})

    # 區辨力檢查：width 變動時 limit 必須**不變**，否則通則數的是節點執行數
    widths = [min_limit(fanout, width=w, rounds=1) for w in (1, 2, 3, 4, 6)]

    # ── 太緊的 limit 會在哪裡停、停下來之後剩什麼 ──────────────────────────
    # 「+1 通則」講的是**需要多少**；這裡量的是**不夠的時候會怎樣**。
    # 沒量這一段的話，結論會停在一個數字上，而下一個人需要的是後果。
    N = 5
    scan = []
    for lim in range(1, N + 2):          # 1..N+1，涵蓋「剛好不夠」到「剛好夠」
        app = linear(N)                  # 每個 limit 用全新的 app：乾淨的 store
        cfg = {"recursion_limit": lim, "configurable": {"thread_id": f"scan-{lim}"}}
        aborted = False
        try:
            app.invoke({}, config=cfg)
        except GraphRecursionError:
            aborted = True
        st = app.get_state(cfg)
        trail = (st.values or {}).get("trail") or []
        scan.append({"limit": lim, "aborted": aborted,
                     "completed": len(trail), "next": sorted(st.next or ())})
    verdict = tight_limit_verdict(scan, N)

    # 中斷之後救不救得回來？**只有帶 checkpointer 的那一半能續跑。**
    # 這一條是「太緊的 limit 到底多貴」的答案：有 checkpointer 只是多花一次呼叫，
    # 沒有 checkpointer 就是全部重跑。
    resume: dict[str, Any] = {}
    for label, keep in (("with_checkpointer", True), ("without_checkpointer", False)):
        app = linear(N) if keep else _linear_uncheckpointed(N)
        thread = f"resume-{label}"
        cfg: dict[str, Any] = {"recursion_limit": 3}
        if keep:
            cfg["configurable"] = {"thread_id": thread}
        try:
            app.invoke({}, config=cfg)
        except GraphRecursionError:
            pass
        except Exception as e:  # noqa: BLE001
            resume[label] = {"resumable": False, "state_readable": False,
                             "error": f"{type(e).__name__}: {e}"}
            continue

        seen: dict[str, Any] = {}
        # 先問「中斷的成果讀不讀得到」—— 這才是「能不能續跑」的前提。
        try:
            st = app.get_state({"configurable": {"thread_id": thread}} if keep else {})
            seen["state_readable"] = True
            seen["completed_visible"] = len((st.values or {}).get("trail") or [])
            seen["next"] = sorted(st.next or ())
        except Exception as e:  # noqa: BLE001
            # 「讀不到」要講出來並記下原因，不能靜默當成不可續跑（D-029 的形狀）
            seen["state_readable"] = False
            seen["error"] = f"{type(e).__name__}: {e}"

        resume_cfg: dict[str, Any] = {"recursion_limit": 50}
        if keep:
            resume_cfg["configurable"] = {"thread_id": thread}
        try:
            r = app.invoke(None, config=resume_cfg)
            seen["resumable"] = len((r or {}).get("trail") or []) == N
        except Exception as e:  # noqa: BLE001
            seen["resumable"] = False
            seen["resume_error"] = f"{type(e).__name__}: {e}"
        resume[label] = seen

    return {
        "rows": rows,
        "all_ok": all(r["ok"] for r in rows),
        "width_invariance": widths,
        "width_invariant": len(set(widths)) == 1,
        "default_limit": _default_recursion_limit(),
        "limit_scan": scan,
        "tight_limit": verdict,
        "resume": resume,
    }


def _mark(i: int):
    """節點函式：把「我跑過了」寫進 state。"""
    def f(s: dict) -> dict:
        return {**s, "trail": (s.get("trail") or []) + [i]}
    return f


def _linear_uncheckpointed(n: int):
    """同上但**不掛 checkpointer** —— 用來證明「救得回來」是 checkpointer 給的。"""
    from langgraph.graph import END, START, StateGraph
    g = StateGraph(dict)
    for i in range(n):
        g.add_node(f"n{i}", _mark(i))
    for i in range(n):
        g.add_edge(START if i == 0 else f"n{i-1}", f"n{i}")
    g.add_edge(f"n{n-1}", END)
    return g.compile()


def _default_recursion_limit() -> int | None:
    """從原始碼讀預設值 —— 不用二分搜尋猜（我撞過自己的天花板兩次）。"""
    import os
    import re
    try:
        from langgraph._internal import _config as cfgmod
        import inspect
        src = inspect.getsource(cfgmod)
    except Exception:
        return None
    m = re.search(r'LANGGRAPH_DEFAULT_RECURSION_LIMIT"?,\s*"(\d+)"', src)
    if m:
        return int(m.group(1))
    return None


def _scheduler_probe() -> dict[str, Any]:
    """量 misfire 邊界：逾期固定 2.2 秒，掃 grace，看邊界是否隨 grace 移動。"""
    import subprocess
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="phase3-aps-"))
    db, fired = work / "jobs.sqlite", work / "fired.log"

    script = work / "worker.py"
    script.write_text(_APS_WORKER_SRC)

    def run(phase: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), phase, *args],
                              capture_output=True, text=True, timeout=120)

    def reset() -> None:
        for p in (db, fired):
            if p.exists():
                p.unlink()

    results = {}
    # (1) 重啟後 job 還在不在
    for store in ("memory", "persistent"):
        reset()
        run("schedule", store, "3600", "0.1")
        results[f"{store}_jobs_after_restart"] = run("list", store).stdout.strip()

    # (2) 持久化 store 拒絕非模組層級 job
    reset()
    r = run("schedule_lambda", "persistent", "3600", "0.1")
    results["persistent_accepts_lambda"] = r.returncode == 0
    results["persistent_lambda_error"] = (r.stderr or "").strip().splitlines()[-1:]

    # (3) misfire 邊界隨 grace 移動（逾期固定約 2.2 秒）
    lateness = 2.2
    boundary = {}
    for grace in ("default", "1", "2", "3", "10"):
        reset()
        run("schedule", "persistent", "0.5", "0.15", grace)
        time.sleep(lateness)
        boundary[grace] = "ran" if "fired" in run("resume", "persistent", grace).stdout else "dropped"
    results["misfire_boundary"] = boundary
    results["default_grace"] = APSCHEDULER_DEFAULT_GRACE_SECONDS
    return results


_APS_WORKER_SRC = '''\
"""量測用的子行程 —— 用**新直譯器**跑，那是真的行程重啟，不是模擬。"""
import sys, time
from datetime import datetime, timedelta
from pathlib import Path

WORK = Path(__file__).parent
DB, FIRED = WORK / "jobs.sqlite", WORK / "fired.log"


def job(tag):
    with open(FIRED, "a") as f:
        f.write(tag + "\\n")


def mk(store, grace="default"):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.memory import MemoryJobStore
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    kw = {} if grace == "default" else {
        "misfire_grace_time": None if grace == "none" else int(grace)}
    js = MemoryJobStore() if store == "memory" else SQLAlchemyJobStore(url=f"sqlite:///{DB}")
    return BackgroundScheduler(jobstores={"default": js}, job_defaults=kw)


phase, store = sys.argv[1], sys.argv[2]
grace = sys.argv[5] if len(sys.argv) > 5 else "default"
s = mk(store, grace)
s.start()
if phase in ("schedule", "schedule_lambda"):
    # run_date 必須是 datetime —— float timestamp 會 TypeError（我第一版就是這樣，
    # 兩個 store 都 rc=1，量到的「0 個 job」其實什麼都沒量到）
    fn = job if phase == "schedule" else (lambda: None)
    s.add_job(fn, "date", run_date=datetime.now() + timedelta(seconds=float(sys.argv[3])),
              args=["fired"] if phase == "schedule" else None, id="j", replace_existing=True)
    time.sleep(float(sys.argv[4]))
elif phase == "list":
    print(f"{len(s.get_jobs())}")
elif phase == "resume":
    time.sleep(1.5)
    print("fired" if FIRED.exists() and FIRED.read_text().strip() else "empty")
s.shutdown(wait=False)
'''


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv

    out: dict[str, Any] = {"versions": {}, "item4": {}, "item6": {}}
    for pkg in ("langgraph", "apscheduler", "sqlalchemy"):
        try:
            out["versions"][pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out["versions"][pkg] = None

    # 判定函式先自檢 —— 判定自己壞掉的話，後面量到什麼都沒有意義（D-018 的 3）
    selfcheck = _selfcheck()
    if not selfcheck["ok"]:
        print(f"[3] 探針自身損壞：{selfcheck['failures']}", file=sys.stderr)
        return EXIT_BROKEN

    missing = [p for p, v in out["versions"].items() if v is None]
    if missing:
        print(f"[2] 量不到：映像缺少 {missing}，這一輪不算通過。", file=sys.stderr)
        return EXIT_UNKNOWN

    out["item4"] = _recursion_probe()
    out["item6"] = _scheduler_probe()

    if as_json:
        # 人看的報告改走 stderr，stdout 整個讓給 JSON —— 跟進入點同一個手法
        # （verify-phase3-runtime.sh 的 `exec 3>&1 1>&2`）。這樣 --json 只跑
        # 一次探針（它要跑二分搜尋、還要真的重啟排程器，不便宜），而兩個
        # 輸出都拿得到。
        with contextlib.redirect_stdout(sys.stderr):
            _report(out)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return EXIT_PASS if _all_ok(out) else EXIT_FAIL

    _report(out)
    return EXIT_PASS if _all_ok(out) else EXIT_FAIL


def _all_ok(out: dict[str, Any]) -> bool:
    i4, i6 = out["item4"], out["item6"]
    res = i4.get("resume", {})
    return bool(
        i4.get("all_ok")
        and i4.get("width_invariant")
        # 「太緊的 limit 是在工作全部做完之後才響」—— 這是 item 4 的實務重點，
        # 沒量到它就等於只記了一個數字。
        and i4.get("tight_limit", {}).get("verdict") == "all_work_then_abort"
        # 而且救得回來與否完全取決於 checkpointer
        and res.get("with_checkpointer", {}).get("resumable") is True
        and res.get("without_checkpointer", {}).get("resumable") is False
        and i6.get("misfire_boundary", {}).get("default") == "dropped"
        and i6.get("misfire_boundary", {}).get("10") == "ran"
        and i6.get("persistent_accepts_lambda") is False
    )


def _selfcheck() -> dict[str, Any]:
    """判定函式對自己的案例必須給得出預期的答案。

    這一段是 D-018 的結束碼 3：如果連判定都不會分辨，量測結果無效 ——
    而不是「量到失敗」。
    """
    failures = []
    cases = [
        # super_steps=3 → 需要 limit=4。所以 3 是 blocks、4 是 tight、5 是 ample。
        # 這三個案例的**邊界**才是區辨力所在（D-024 第八節）：
        # 只測「差距很大」的案例時，把 `slack == 0` 寫成 `slack < 1` 也不會死。
        (recursion_guard_verdict(3, 3)["verdict"], "blocks", "limit 比所需少 1 要判 blocks"),
        (recursion_guard_verdict(4, 3)["verdict"], "tight", "剛好等於所需要判 tight"),
        (recursion_guard_verdict(5, 3)["verdict"], "ample", "多 1 步就要判 ample"),
        (recursion_guard_verdict(9, 4)["verdict"], "ample", "有餘裕要判 ample"),
        (scheduler_persistence_verdict("memory", 0.1)["verdict"], "lost_with_store", "記憶體 store"),
        (scheduler_persistence_verdict("persistent", 0.5, 1.0)["verdict"], "runs", "寬限內要跑"),
        (scheduler_persistence_verdict("persistent", 1.5, 1.0)["verdict"], "dropped_as_misfire", "逾期要丟"),
        (scheduler_persistence_verdict("persistent", 99.0, None)["verdict"], "runs", "無寬限永遠跑"),
        (job_callable_verdict("persistent", False)["accepted"], False, "持久化拒絕 lambda"),
        (job_callable_verdict("memory", False)["accepted"], True, "記憶體收得下 lambda"),
        # tight_limit_verdict：四個 verdict 都要分得開。
        # `completed == node_count` 與 `not next` 是**兩個**條件，不是一個 ——
        # 下面第三、四條就是把它們分開的那一對。
        (tight_limit_verdict([], 3)["verdict"], "unknown", "沒有掃描資料要判 unknown"),
        (tight_limit_verdict([{"limit": 1, "aborted": False, "completed": 3, "next": []}], 3)["verdict"],
         "unknown", "掃描裡沒有任何中止要判 unknown（邊界不在掃描範圍裡）"),
        (tight_limit_verdict([{"limit": 2, "aborted": True, "completed": 2, "next": ["n2"]},
                              {"limit": 3, "aborted": True, "completed": 3, "next": []}], 3)["verdict"],
         "all_work_then_abort", "中止點已完成全部且沒有下一步"),
        (tight_limit_verdict([{"limit": 3, "aborted": True, "completed": 3, "next": ["n3"]}], 3)["verdict"],
         "partial_then_abort", "完成了全部節點但**還有下一步**不算做完了（邊界）"),
    ]
    for got, want, label in cases:
        if got != want:
            failures.append(f"{label}：預期 {want!r}，得到 {got!r}")
    return {"ok": not failures, "failures": failures}


def _report(out: dict[str, Any]) -> None:
    w = 78
    print("=" * w)
    print("第三階段 item 4（recursion_limit）與 item 6（APScheduler）")
    print(f"  langgraph {out['versions']['langgraph']}"
          f" | apscheduler {out['versions']['apscheduler']}"
          f" | sqlalchemy {out['versions']['sqlalchemy']}")
    print()
    print("=" * w)
    print("item 4：所需 limit = super-step 數 + 1 ？（二分搜尋量到的）")
    print()
    print(f"  {'圖形':<18} {'super-step':>11} {'預測':>6} {'量到':>6} {'一致':>6}")
    print("  " + "-" * 52)
    for r in out["item4"]["rows"]:
        print(f"  {r['graph']:<18} {r['super_steps']:>11} {r['predicted']:>6} "
              f"{r['min_limit']:>6} {'✓' if r['ok'] else '✗':>6}")
    print()
    print("  區辨力：width=1,2,3,4,6 的 limit = "
          f"{out['item4']['width_invariance']}")
    print("    → " + ("與 width 無關 ⇒ 通則數的是 super-step"
                      if out["item4"]["width_invariant"] else
                      "**隨 width 改變** ⇒ 通則數的是節點執行數（README 的猜測是錯的）"))
    print(f"  預設 limit（讀原始碼，非二分搜尋）：{out['item4']['default_limit']}")
    print()
    print("  太緊的時候會怎樣？（linear-5，逐個 limit 試）")
    print(f"    {'limit':>5}  {'結果':>6}  {'已完成':>7}  {'下一步':<10}")
    print("    " + "-" * 34)
    for r in out["item4"].get("limit_scan", []):
        print(f"    {r['limit']:>5}  {'中止' if r['aborted'] else '跑完':>6}  "
              f"{r['completed']:>7}  {str(r['next']):<10}")
    print(f"    → {out['item4'].get('tight_limit', {}).get('message', '')}")
    res = out["item4"].get("resume", {})
    print(f"  中斷之後救得回來嗎？"
          f" 有 checkpointer：{'可以' if res.get('with_checkpointer', {}).get('resumable') else '不行'}"
          f"（讀得到 {res.get('with_checkpointer', {}).get('completed_visible')} 個節點）"
          f"／沒有：{'可以' if res.get('without_checkpointer', {}).get('resumable') else '不行'}"
          f"（{res.get('without_checkpointer', {}).get('error', '')}）")
    print()
    print("=" * w)
    print("item 6：重啟之後，停機期間到期的 job 會不會跑？")
    print()
    print(f"  重啟後 store 裡的 job 數："
          f" memory={out['item6']['memory_jobs_after_restart']}"
          f" / persistent={out['item6']['persistent_jobs_after_restart']}")
    print(f"  持久化 store 收得下 lambda？ {out['item6']['persistent_accepts_lambda']}")
    print()
    print("  misfire 邊界（逾期固定約 2.2 秒，只改 grace）：")
    print(f"    {'grace':>8}  {'結果':>8}")
    print("    " + "-" * 18)
    for g, res in out["item6"]["misfire_boundary"].items():
        mark = " ← 預設" if g == "default" else ""
        print(f"    {g:>8}  {res:>8}{mark}")
    print()
    print("=" * w)
    print("判讀：" if _all_ok(out) else "**判定不成立**：")
    print("  兩項都符合 D-030 記載的規則。" if _all_ok(out) else
          "  量到的行為與判定不符 —— 上游變了，或判定本身是錯的。")


if __name__ == "__main__":
    sys.exit(main())
