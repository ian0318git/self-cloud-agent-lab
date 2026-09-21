#!/usr/bin/env python3
"""throughput_probe.py 的離線測試 —— 不需要 docker、不需要 ollama。

只測判定函式。真正的量測留給 verify-throughput.sh；這裡測「量到之後怎麼判」，
而那一半正是本專案反覆出錯的地方。

每個判定都要有**會死的**斷言（D-024 第八節、D-026 第五節）：只斷言「差很多」
的案例時，把門檻從 `>` 寫成 `>=` 也不會被發現。所以每個判定的**邊界兩側**
都是獨立案例 —— 門檻 15% 就同時要有 15.0 與 15.1 兩個案例。

結束碼：0 = 全部通過，1 = 有失敗。
"""

from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from throughput_probe import (  # noqa: E402
    COLD_LOAD_THRESHOLD_NS,
    D2_N3,
    D2_N7,
    DURATION_IDENTITY_TOLERANCE_S,
    EXIT_BROKEN,
    EXIT_FAIL,
    EXIT_UNMEASURED,
    MIN_SAMPLES_FOR_STABILITY,
    RANGE_TOLERANCE_PCT,
    STABILITY_CV_PCT,
    _all_ok,
    baseline_verdict,
    cap_respected_verdict,
    cv_is_stable,
    long_generation_overall,
    long_generation_verdict,
    classify_failure,
    cpu_topology_verdict,
    dependence_verdict,
    duration_identity_error_s,
    kv_slope_verdict,
    new_result,
    parse_kv_buffer_bytes,
    parse_n_threads,
    rate,
    reservation_verdict,
    sample_usable,
    summarize,
    threads_verdict,
    truncation_limit,
    truncation_verdict,
)

FAILURES: list[str] = []
COUNT = 0


def check(label: str, got, want) -> None:
    global COUNT
    COUNT += 1
    if got != want:
        FAILURES.append(f"{label}\n      預期 {want!r}\n      得到 {got!r}")


def check_close(label: str, got, want, tol=0.01) -> None:
    global COUNT
    COUNT += 1
    if got is None or abs(got - want) > tol:
        FAILURES.append(f"{label}\n      預期 {want!r} ±{tol}\n      得到 {got!r}")


def sample(eval_count=100, eval_ns=10 * 10**9, load_ns=0, prompt_n=10, prompt_ns=10**8, total_ns=None):
    """組一個 ollama 形狀的回應。total 預設與各階段一致。"""
    if total_ns is None:
        total_ns = load_ns + prompt_ns + eval_ns
    return {
        "eval_count": eval_count,
        "eval_duration": eval_ns,
        "load_duration": load_ns,
        "prompt_eval_count": prompt_n,
        "prompt_eval_duration": prompt_ns,
        "total_duration": total_ns,
    }


# ══ rate：量不到就要說量不到 ════════════════════════════════════════════════
print("rate")
before = COUNT
check_close("100 token / 10 s = 10 t/s", rate(100, 10 * 10**9), 10.0)
check("eval_count=0 → None（不是 0.0）", rate(0, 10**9), None)
check("eval_count 負數 → None", rate(-5, 10**9), None)
check("duration=0 → None（不是 inf）", rate(100, 0), None)
check("duration 負數 → None", rate(100, -1), None)
check("欄位缺（None）→ None", rate(None, 10**9), None)
check("欄位是字串 → None", rate("100", 10**9), None)
# 這一條是重點：把「沒有資料」講成「很慢」是本專案最貴的錯
check("量不到時不可以回 0", rate(0, 10**9) == 0, False)
print(f"  {COUNT - before} 項")

# ══ duration_identity_error_s：欄位意義沒變的判準 ══════════════════════════
print("duration_identity_error_s")
before = COUNT
check_close("三階段相加等於 total → 誤差 0", duration_identity_error_s(sample()), 0.0)
check_close("total 多 0.3 s → 回報 0.3", duration_identity_error_s(sample(total_ns=10 * 10**9 + 10**8 + 3 * 10**8)), 0.3)
check_close("total 少 0.3 s → 回報 -0.3", duration_identity_error_s(sample(total_ns=10 * 10**9 + 10**8 - 3 * 10**8)), -0.3)
check("缺 total_duration → None", duration_identity_error_s({k: v for k, v in sample().items() if k != "total_duration"}), None)
check("缺 eval_duration → None", duration_identity_error_s({k: v for k, v in sample().items() if k != "eval_duration"}), None)
check("欄位是字串 → None", duration_identity_error_s({**sample(), "total_duration": "x"}), None)
check("門檻常數是 0.5 秒", DURATION_IDENTITY_TOLERANCE_S, 0.5)
print(f"  {COUNT - before} 項")

# ══ cap_respected_verdict：本探針最重要的一條 ═══════════════════════════════
print("cap_respected_verdict")
before = COUNT
check("撞到上限且數量相符 → respected", cap_respected_verdict(160, 160, "length")["verdict"], "respected")
check("模型自己停、數量少於上限 → respected", cap_respected_verdict(131, 160, "stop")["verdict"], "respected")
# 這就是開發時燒掉十幾分鐘的 bug：頂層 num_predict 被忽略
check("eval_count 超過上限 → ignored", cap_respected_verdict(2876, 24, "stop")["verdict"], "ignored")
check("ignored 的訊息要點出 options", "options" in cap_respected_verdict(2876, 24, "stop")["message"], True)
# 邊界：剛好超過 1 個 token 就必須被抓到
check("只超過 1 個 token 也要 ignored", cap_respected_verdict(25, 24, "stop")["verdict"], "ignored")
check("剛好等於上限 → respected", cap_respected_verdict(24, 24, "length")["verdict"], "respected")
check("length 但數量不符 → unknown（兩個欄位對不起來）",
      cap_respected_verdict(20, 24, "length")["verdict"], "unknown")
check("讀不到 eval_count → unknown", cap_respected_verdict(None, 24, "stop")["verdict"], "unknown")
check("num_predict 是字串 → unknown", cap_respected_verdict(10, "24", "stop")["verdict"], "unknown")
check("bool 不算整數以外的好數字（eval_count=True）",
      cap_respected_verdict(True, 24, "stop")["verdict"], "unknown")
print(f"  {COUNT - before} 項")

# ══ long_generation_verdict：撞到上限 vs 提早講完 ═══════════════════════════
print("long_generation_verdict / long_generation_overall")
before = COUNT
check("撞到上限且數量相符 → matched", long_generation_verdict(1024, 1024, "length")["verdict"], "matched")
# **這一條是這一組存在的理由。** 模型提早講完時，cap_respected_verdict 回
# respected（上限沒被違反 —— 那是好事），但這一輪就**不是** D-014 的對照
# 了：那一次生成的正是 1024 個 token。同一個樣本，兩條判準的答案相反。
check("提早講完時 cap_respected_verdict 說 respected（它問的是別的問題）",
      cap_respected_verdict(131, 1024, "stop")["verdict"], "respected")
check("同一個樣本，長生成判準說 void", long_generation_verdict(131, 1024, "stop")["verdict"], "void")
check("void 的訊息要說出少了幾個 token", "893" in long_generation_verdict(131, 1024, "stop")["message"], True)
check("void 的訊息要說出不可以當對照",
      "不可以" in long_generation_verdict(131, 1024, "stop")["message"], True)
check("超過上限（上限被忽略）→ void", long_generation_verdict(2000, 1024, "stop")["verdict"], "void")
check("讀不到 eval_count → unknown", long_generation_verdict(None, 1024, "length")["verdict"], "unknown")
check("讀不到 num_predict → unknown", long_generation_verdict(1024, None, "length")["verdict"], "unknown")
check("差 1 個 token 就不算對照", long_generation_verdict(1023, 1024, "stop")["verdict"], "void")
check("多 1 個 token 也不算", long_generation_verdict(1025, 1024, "length")["verdict"], "void")
# 總結：只要有一個樣本沒撞到上限，整組就不能用 —— 速率統計會把不同
# 生成長度的樣本平均在一起，而平均值不屬於其中任何一個。
ok_v = long_generation_verdict(1024, 1024, "length")
bad_v = long_generation_verdict(131, 1024, "stop")
check("全部 matched → matched", long_generation_overall([ok_v, ok_v])["verdict"], "matched")
check("三個樣本裡有一個提早停 → void", long_generation_overall([ok_v, bad_v, ok_v])["verdict"], "void")
check("void 的訊息沿用那個壞樣本的說明", long_generation_overall([ok_v, bad_v])["message"], bad_v["message"])
check("沒有可用樣本 → unknown（不是 matched）", long_generation_overall([])["verdict"], "unknown")
print(f"  {COUNT - before} 項")

# ══ sample_usable：冷樣本一定要被擋掉 ═══════════════════════════════════════
print("sample_usable")
before = COUNT
check("正常樣本 → 可用", sample_usable(sample())["usable"], True)
check("真的重新載入（6.79s）→ 冷樣本，不可用",
      sample_usable(sample(load_ns=6_786_962_554))["usable"], False)
check("冷樣本的理由要說得出是冷", sample_usable(sample(load_ns=6_786_962_554))["reason"], "cold")
check("load_duration=0 不算冷", sample_usable(sample(load_ns=0))["reason"], "ok")

# **這一節的重點。** ollama 對**每一個**請求都回非零的 `load_duration`
# （排程器把已載好的模型找出來、釘住的時間），同組連跑五次的實測是
# 6.79s → 1.1ms → 1.2ms → 4.0ms → 4.3ms。所以判準若寫成 `> 0`，
# **整份矩陣的每一個樣本**都會被排除，而症狀是「沒有任何條件量到速率」。
# 下面三個數字就是那三筆毫秒級的實測值 —— 它們必須是「可用」。
check("1.1ms 的行政開銷不算冷（實測值）", sample_usable(sample(load_ns=1_145_765))["reason"], "ok")
check("4.3ms 的行政開銷不算冷（實測值）", sample_usable(sample(load_ns=4_276_818))["reason"], "ok")
# 門檻的兩側各一格。門檻是 0.1 秒，用 `>` 比較，所以剛好等於門檻時不算冷。
check("剛好等於門檻（0.1s）→ 不算冷",
      sample_usable(sample(load_ns=COLD_LOAD_THRESHOLD_NS))["reason"], "ok")
check("門檻多 1 奈秒 → 冷",
      sample_usable(sample(load_ns=COLD_LOAD_THRESHOLD_NS + 1))["reason"], "cold")
check("eval_count=0 → no_generation", sample_usable(sample(eval_count=0))["reason"], "no_generation")
check("eval_duration=0 → no_duration", sample_usable(sample(eval_ns=0))["reason"], "no_duration")
check("eval_count 缺 → unreadable", sample_usable({"eval_duration": 10**9, "load_duration": 0})["reason"], "unreadable")
check("load_duration 缺 → unreadable（不知道冷不冷，就不敢用）",
      sample_usable({"eval_count": 10, "eval_duration": 10**9})["reason"], "unreadable")
check("eval_count 是字串 → unreadable", sample_usable({"eval_count": "10", "eval_duration": 10**9, "load_duration": 0})["reason"], "unreadable")
print(f"  {COUNT - before} 項")

# ══ summarize：穩定度（判定看 CV，全距只是印出來） ══════════════════════════
print("summarize")
before = COUNT
s = summarize([6.71, 6.76, 6.72])
check_close("中位數取中間那個", s["median"], 6.72)
check("三個樣本 n=3", s["n"], 3)
# n < MIN_SAMPLES_FOR_STABILITY → unknown。**不是 stable。**
# 這一條是 2026-09-22 改的：原本 1 個樣本時全距是 0.0，於是 `--quick`
# 會讓每個條件都「穩定」，一整輪基準線就從一次取樣生出來了。
check("3 個樣本 → unknown（樣本不足，不是通過）", s["stability"], "unknown")
check("樣本不足時 cv_pct 是 None，不是 0", s["cv_pct"], None)
check("樣本不足的訊息要講「不判定」", "不判定" in s["message"], True)

# 全距仍然算得出來、仍然印 —— 只是不參與判定。分母是**中位數**。
check_close("剛好 15% 全距（對稱於中位數 100）", summarize([92.5, 107.5])["spread_pct"], 15.0, 0.05)
check_close("全距用中位數當分母，不是最大值", summarize([100.0, 115.0])["spread_pct"], 13.95, 0.05)
check("空集合 → n=0", summarize([])["n"], 0)
check("空集合 → 中位數 None（不是 0）", summarize([])["median"], None)
check("空集合 → stability unknown", summarize([])["stability"], "unknown")
check("全部是 None → n=0", summarize([None, None])["n"], 0)
check("負速率被濾掉", summarize([-1.0, 5.0])["n"], 1)

# ── 門檻的來歷：換估計量，不是放寬門檻 ────────────────────────────────────
check("舊門檻保留為常數（報表仍然印全距）", RANGE_TOLERANCE_PCT, 15.0)
check("CV 門檻 = 舊門檻 / d₂(3)", STABILITY_CV_PCT, round(RANGE_TOLERANCE_PCT / D2_N3, 1))
check("d₂(3) 是常態理論值", D2_N3, 1.6926)
# 這一條是整個改動的理由。留著它，下次有人想把門檻「調回 15」時會先看到
# 15 在 n=7 下等效的 CV 是 5.55 —— 那不是原來的門檻，是嚴格 59% 的門檻。
check("15% 全距在 n=7 下等效的 CV", round(RANGE_TOLERANCE_PCT / D2_N7, 2), 5.55)
check("所以 CV 門檻比它寬，這正是把 n=3 的嚴格度還原", STABILITY_CV_PCT > RANGE_TOLERANCE_PCT / D2_N7, True)

# ── n 要夠才判定 ──────────────────────────────────────────────────────────
check("MIN_SAMPLES_FOR_STABILITY 是 5", MIN_SAMPLES_FOR_STABILITY, 5)
check("4 個樣本仍然 unknown", summarize([6.7, 6.8, 6.6, 6.75])["stability"], "unknown")
check("5 個樣本開始判定", summarize([6.7, 6.8, 6.6, 6.75, 6.72])["stability"], "stable")

# ── 邊界：門檻含，兩側都要有案例 ──────────────────────────────────────────
# 用 [100−k, 100−k, 100, 100+k, 100+k]：離均差是 ∓k、∓k、0，
# 變異數 = 4k²/4 = k²，所以 σ = k、CV = k%。k 直接就是 CV。
check("CV 5%（很穩） → stable", summarize([95, 95, 100, 105, 105])["stability"], "stable")
check("CV 9%（散） → noisy", summarize([91, 91, 100, 109, 109])["stability"], "noisy")
check_close("CV 8.89 的資料集", summarize([91.11, 91.11, 100, 108.89, 108.89])["cv_pct"], 8.89, 0.01)
check("CV 8.89 → stable（門檻下方一點）", summarize([91.11, 91.11, 100, 108.89, 108.89])["stability"], "stable")
check("CV 8.91 → noisy（門檻上方一點）", summarize([91.09, 91.09, 100, 108.91, 108.91])["stability"], "noisy")
# **「門檻含」只能這樣測。** 8.9 在二進位浮點裡不精確，所以「CV 剛好等於
# 門檻」的資料集造不出來 —— `100 ± 8.9` 的離均差算出來是 8.900000000000006。
# 直接拿常數比才測得到 `<=` 與 `<` 的差別，而那就是「門檻含不含」。
check("門檻含：CV 剛好等於門檻 → 過", cv_is_stable(STABILITY_CV_PCT), True)
check("門檻不含：差一點點就下去", cv_is_stable(STABILITY_CV_PCT + 0.01), False)
check("CV 0 → 過（完全一致）", cv_is_stable(0.0), True)
check("noisy 的訊息要同時講 CV 與全距", "CV" in summarize([91, 91, 100, 109, 109])["message"] and "全距" in summarize([91, 91, 100, 109, 109])["message"], True)

# ── 這一組是「換估計量」的證據，也是它唯一會被誤讀的地方 ──────────────────
# n=7、全距 20%、CV 6.56% → **stable**。在舊規則（全距 ≤15%）下它會是
# noisy。這不是放寬：常態下 n=7 的全距期望值本來就是 2.70σ，20% 的全距
# 完全對得起 6.56% 的 σ。**單一資料集會雙向翻轉，校準的目標是「同一台
# 機器、同一個真實 σ，被擋下的機率不變」，不是「同一筆資料得到同一個
# 判定」。** 沒有這一條，把判定改回全距的突變不會被抓到。
_s = summarize([90, 95, 98, 100, 102, 105, 110])
check_close("n=7 全距 20% 的 CV", _s["cv_pct"], 6.56, 0.01)
check("n=7、全距 20% → stable（CV 才是被判定的那個）", _s["stability"], "stable")
check("同一筆資料的全距仍然是 20%", _s["spread_pct"], 20.0)
print(f"  {COUNT - before} 項")

# ══ dependence_verdict：速率隨什麼變 ════════════════════════════════════════
print("dependence_verdict")
before = COUNT
check("兩邊幾乎一樣 → flat", dependence_verdict("長生成", "短生成", 6.8, 6.9, 15.0)["verdict"], "flat")
check("差 30% → dependent", dependence_verdict("長生成", "短生成", 4.5, 6.7, 15.0)["verdict"], "dependent")
check_close("差距百分比算得對", dependence_verdict("長生成", "短生成", 4.5, 6.7, 15.0)["drop_pct"], 32.84, 0.05)
# 邊界兩側
check("剛好 15% → flat（門檻含）", dependence_verdict("a", "b", 85.0, 100.0, 15.0)["verdict"], "flat")
check("15.5% → dependent", dependence_verdict("a", "b", 84.5, 100.0, 15.0)["verdict"], "dependent")
check("有一邊量不到 → unknown", dependence_verdict("a", "b", None, 6.7, 15.0)["verdict"], "unknown")
check("有一邊是 0 → unknown（不是 100% 差距）", dependence_verdict("a", "b", 0.0, 6.7, 15.0)["verdict"], "unknown")
# 方向不可以講反
msg = dependence_verdict("長生成", "短生成", 4.5, 6.7, 15.0)["message"]
check("講得出誰比誰慢", "長生成 比 短生成 慢" in msg, True)
check("dependent 的訊息要提醒不能只報一個數", "不能只報一個數" in msg, True)
print(f"  {COUNT - before} 項")

# ══ baseline_verdict：D-014 的實作 ══════════════════════════════════════════
print("baseline_verdict")
before = COUNT


def cond(r, stab="stable"):
    return {"rate": r, "stability": stab}


check("只有一個條件 → single_condition（不能當基準線）",
      baseline_verdict([cond(6.7)])["verdict"], "single_condition")
check("single_condition 要說出 D-014", "D-014" in baseline_verdict([cond(6.7)])["message"], True)
check("兩個條件 → usable", baseline_verdict([cond(6.7), cond(6.5)])["verdict"], "usable")
check("有 noisy 條件 → unstable", baseline_verdict([cond(6.7), cond(6.5, "noisy")])["verdict"], "unstable")
# **允許清單**：不是 stable 的一律擋。這一條原本是封鎖清單（只有 noisy
# 才擋），所以 `unknown` 會一路走到「可以當基準線」—— 樣本不足的條件就是
# 從那個洞過去的。
check("有 unknown 條件 → unstable（不是放行）",
      baseline_verdict([cond(6.7), cond(6.5, "unknown")])["verdict"], "unstable")
check("unstable 的訊息要指出是哪幾個條件",
      "qwen" in baseline_verdict([{**cond(6.7), "label": "qwen3:4b"}, {**cond(6.5, "noisy"), "label": "qwen2.5:3b"}])["message"], True)
check("兩種壞法要分開講", "樣本數不足" in baseline_verdict([cond(6.7, "unknown"), cond(6.5, "noisy")])["message"], True)
check("完全沒有速率 → unknown", baseline_verdict([])["verdict"], "unknown")
check("速率是 None 不算數（等同沒有）", baseline_verdict([cond(None), cond(None)])["verdict"], "unknown")
check("速率是 0 不算數", baseline_verdict([cond(0), cond(0)])["verdict"], "unknown")
check("measured_conditions 數得對", baseline_verdict([cond(1.0), cond(2.0), cond(None)])["measured_conditions"], 2)
check("門檻可以調高", baseline_verdict([cond(1.0), cond(2.0)], min_conditions=3)["verdict"], "single_condition")
# `--quick`：從設計上就沒建立基準線。回一個**獨立**的 verdict，不回
# unstable —— 那一輪沒有不穩，它只是每條件取樣一次。
check("attempted=False → not_attempted",
      baseline_verdict([cond(6.7), cond(6.5)], attempted=False)["verdict"], "not_attempted")
check("not_attempted 不是 unstable", baseline_verdict([cond(6.7, "noisy")], attempted=False)["verdict"] != "unstable", True)
check("not_attempted 的訊息要講清楚沒嘗試",
      "沒有嘗試" in baseline_verdict([cond(6.7)], attempted=False)["message"], True)
check("attempted=True 是預設", baseline_verdict([cond(6.7), cond(6.5)])["verdict"], "usable")
print(f"  {COUNT - before} 項")

# ══ kv_slope_verdict：用 /api/ps 的 size 差求 KV 成本 ═══════════════════════
print("kv_slope_verdict")
before = COUNT
MIB = 1024**2


def kvp(ctx, mib):
    return {"context_length": ctx, "size_bytes": int(mib * MIB)}


kv = kv_slope_verdict([kvp(4096, 3000), kvp(8192, 3600)])
check("400 MiB / 4096 token 的斜率算得對", kv["bytes_per_token"], int(round(600 * MIB / 4096)))
check("兩點 → linear（無法檢查線性，但不能說它非線性）", kv["verdict"], "linear")
# 三點共線
kv3 = kv_slope_verdict([kvp(2048, 2400), kvp(4096, 3000), kvp(8192, 4200)])
check("三點共線 → linear", kv3["verdict"], "linear")
# 中點偏離：斜率 0.2604 MiB/token → 中點預測 2933 MiB，實測 3450 差 14%
kvbad = kv_slope_verdict([kvp(2048, 2400), kvp(4096, 3450), kvp(8192, 4000)])
check("中點預測差太多 → nonlinear", kvbad["verdict"], "nonlinear")
check("稍微偏一點（3%）仍算 linear", kv_slope_verdict([kvp(2048, 2400), kvp(4096, 3025), kvp(8192, 4000)])["verdict"], "linear")
check("少於兩點 → unknown", kv_slope_verdict([kvp(4096, 3000)])["verdict"], "unknown")
# 只有一點時，判定都會是 unknown —— 但**訊息必須說出是哪一種 unknown**。
# `len(pts) < 2` 那條守衛現在只影響訊息（一點時 d_ctx 也會是 0，被下一條
# 擋掉），所以只斷言 verdict 的話，把那條守衛拿掉測試照樣通過 —— 我第一版
# 就是這樣，突變「少於兩點仍硬算斜率」因此漏掉。訊息本身才是被測的東西：
# 「點不夠」與「兩個點的 ctx 相同」要採取的行動不一樣，而後者在一點的
# 情況下還是**胡說**（根本沒有「兩個點」）。
check("只有一點時的訊息講的是「點不夠」，不是「ctx 相同」",
      "少於兩個" in kv_slope_verdict([kvp(4096, 3000)])["message"], True)
check("真的有兩個同 ctx 的點時才講「ctx 相同」",
      "ctx 相同" in kv_slope_verdict([kvp(4096, 3000), kvp(4096, 3000)])["message"], True)
# 零點是 len(pts)<2 那條守衛唯一真正擋得住的案例（一點會被「ctx 相同」擋掉），
# 所以它必須單獨有一條 —— 否則那條守衛拿掉也不會有人發現。
check("完全沒有取樣點 → unknown（不是 IndexError）", kv_slope_verdict([])["verdict"], "unknown")
# **這一條是 2026-09-21 補的。** `size` 的斜率比日誌裡的 KV buffer 斜率多
# 約 23 KiB/token，而且兩個模型多出來的一樣多 —— 所以它與模型無關、與 ctx
# 成正比，不是 KV。訊息不講這件事，讀者會把一個**上界**當成 KV 成本，
# 而那個數字會被拿去做容量規劃。
check("KV 斜率要講明不是純 KV（只能當上界）",
      "上界" in kv_slope_verdict([kvp(4096, 3000), kvp(8192, 3600)])["message"], True)
check("ctx 相同 → unknown", kv_slope_verdict([kvp(4096, 3000), kvp(4096, 3000)])["verdict"], "unknown")
# 反序輸入也要處理：不能假設呼叫端排好了
check("反序輸入也要求得出斜率", kv_slope_verdict([kvp(8192, 3600), kvp(4096, 3000)])["bytes_per_token"],
      int(round(600 * MIB / 4096)))
# ctx 變大記憶體卻沒變大 —— 這個儀器量的不是 KV
check("斜率 ≤0 → anomalous", kv_slope_verdict([kvp(4096, 3000), kvp(8192, 3000)])["verdict"], "anomalous")
check("斜率 <0 → anomalous", kv_slope_verdict([kvp(4096, 3000), kvp(8192, 2000)])["verdict"], "anomalous")
check("anomalous 時不給 bytes_per_token", kv_slope_verdict([kvp(4096, 3000), kvp(8192, 3000)])["bytes_per_token"], None)
check("缺 size → 該點被忽略", kv_slope_verdict([kvp(4096, 3000), {"context_length": 8192}])["verdict"], "unknown")

# ── 分組：`size` 是「權重 + 緩衝 + KV」，權重每個模型都不一樣 ──────────────
# 上面每一條都是單一模型（沒有 model 鍵，全落在同一組），所以那些數字
# 不會因為分組而改變。下面才是分組真正在擋的東西。
def kvpm(model, ctx, mib):
    return {"model": model, "context_length": ctx, "size_bytes": int(mib * MIB)}


# **危險的那一組**：兩個模型的權重不同、但各自的 KV 成本一樣（都是
# 90 KiB/token）。混合排序後首點是 small@4096、末點是 big@8192，
# 混合斜率 = 560 MiB / 4096 = 140 KiB/token —— 一個**看起來完全合理的
# 正數**，而它是錯的。這才是分組要擋的形狀：負斜率自己會被發現，
# 合理的正斜率不會。
DANGEROUS = [
    kvpm("small:3b", 4096, 2000), kvpm("big:4b", 4096, 2200),
    kvpm("small:3b", 8192, 2360), kvpm("big:4b", 8192, 2560),
]
mixed = kv_slope_verdict(DANGEROUS)
check("混合斜率那個「合理的正數」不會被當成 KV 成本",
      mixed["bytes_per_token"], None)
check("多模型時不給單一數字（連 kib_per_token 也不給）", mixed["kib_per_token"], None)
check("small:3b 的 KV 成本算得對", mixed["per_model"]["small:3b"]["bytes_per_token"],
      int(round(360 * MIB / 4096)))
check("big:4b 的 KV 成本算得對", mixed["per_model"]["big:4b"]["bytes_per_token"],
      int(round(360 * MIB / 4096)))
check("混合斜率 140 KiB 沒有出現在任何一組裡",
      sorted(v["kib_per_token"] for v in mixed["per_model"].values()), [90.0, 90.0])

# 實測的形狀（權重取自 ollama list，KV 取自 D-029 那一輪）：權重差很多時
# 混合斜率會變成一個比任何真實值都大的數 —— 也仍然是錯的。
WEIGHTS_4B, WEIGHTS_3B = 2330, 1843  # MiB
REAL = [
    kvpm("qwen3:4b", 4096, WEIGHTS_4B + 576), kvpm("qwen2.5:3b", 4096, WEIGHTS_3B + 144),
    kvpm("qwen3:4b", 8192, WEIGHTS_4B + 1152), kvpm("qwen2.5:3b", 8192, WEIGHTS_3B + 288),
]
real = kv_slope_verdict(REAL)
check("大模型的 KV 成本沒有被小模型的權重污染",
      real["per_model"]["qwen3:4b"]["kib_per_token"], 144.0)
check("小模型的 KV 成本也沒有被大模型的權重污染",
      real["per_model"]["qwen2.5:3b"]["kib_per_token"], 36.0)
check("混合斜率（447 KiB）沒有被當成任何一個模型的 KV 成本",
      real["kib_per_token"], None)

# 單一模型（有 model 鍵）仍然要拿到單一數字，否則分組就只是把功能關掉
one = kv_slope_verdict([kvpm("qwen2.5:3b", 4096, 2000), kvpm("qwen2.5:3b", 8192, 2360)])
check("只有一個模型時照樣給單一數字", one["bytes_per_token"], int(round(360 * MIB / 4096)))
check("只有一個模型時訊息裡有模型名", "qwen2.5:3b" in one["message"], True)

# 三個模型、其中一個量不到（只有一點）—— 成功的那些仍然要算出來，
# 但量不到的那個也必須在訊息裡出現，不能只留成功的那一句。
three = kv_slope_verdict([
    kvpm("a:1b", 4096, 1000), kvpm("a:1b", 8192, 1360),
    kvpm("b:2b", 4096, 2000), kvpm("b:2b", 8192, 2360),
    kvpm("c:3b", 4096, 3000),
])
check("其中一個模型只有一點 → 那一組 unknown", three["per_model"]["c:3b"]["verdict"], "unknown")
check("另外兩組照樣算出來", three["per_model"]["a:1b"]["bytes_per_token"], int(round(360 * MIB / 4096)))
check("量不到的那個模型仍然出現在訊息裡", "c:3b" in three["message"], True)

# 分組的鍵不能靠「數值看起來怪不怪」—— 沒有 model 鍵的點要自成一群，
# 而不是被丟掉或跟別人混在一起。
check("沒有 model 鍵的點自成一群，不會消失",
      sorted(kv_slope_verdict([kvp(4096, 3000), kvpm("m:1b", 4096, 1), kvpm("m:1b", 8192, 2)])["per_model"]),
      ["m:1b", "（未標示模型）"])
print(f"  {COUNT - before} 項")

# ══ truncation_limit / truncation_verdict：截斷規則 ═════════════════════════
# 本節的期望值全部是**實測**出來的，不是從公式反推的：
#   ctx 4096 → 2050（D-022 的 limit=2050 prompt=4937；D-027 的 2,050；
#                    2026-09-21 對照組的 limit=2050 prompt=14030）
#   ctx 8192 → 4098（2026-09-21 同一輪重現 11 次）
# 把公式的輸出抄進期望值是**假測試** —— 那正是舊版「預算 = ctx − num_predict」
# 活下來的方式：它的期望值 2096 就是它自己算出來的，而真值是 2050。
print("truncation_limit")
before = COUNT
check("實測上限：ctx 4096 → 2050", truncation_limit(4096), 2050)
check("實測上限：ctx 8192 → 4098", truncation_limit(8192), 4098)
# num_keep 是公式的輸入。**這一條是化簡式擋不住的**：若把 `num_ctx//2 + 2`
# 寫死，這條會回 2050，而正確答案是 2098。專案裡所有觀測都是 num_keep=4，
# 所以只有刻意換一個 num_keep 才分得出「實作完整公式」與「背下化簡結果」。
check("num_keep 不同 → 上限不同（化簡式擋不住的那一條）", truncation_limit(4096, num_keep=100), 2098)
check("room 至少留 1 個 token（num_keep ≥ num_ctx 的退化情形）", truncation_limit(100, num_keep=500), 99)
check("num_ctx=0 夾成 0，不是負數", truncation_limit(0), 0)
print("truncation_verdict")
# **這一條是整節裡鑑別力最高的一條。** 對照組量到：ctx 4096 下送 3040 個
# token，prompt_eval_count 就是 3040 —— **沒有被切**。舊版拿「上限的 95%」
# 當門檻（2050 × 0.95 = 1947.5），3040 遠在它之上，會判成「被切了」；
# 也就是說舊判準會把一個**塞得下的** prompt 講成被截斷。
check("塞得下就不會被切（3040 @ ctx 4096 → intact，不是 truncated）",
      truncation_verdict(3040, 4096)["verdict"], "intact")
check("prompt 遠低於上限 → intact", truncation_verdict(3000, 8192)["verdict"], "intact")
check("撞到上限 → truncated", truncation_verdict(4098, 8192)["verdict"], "truncated")
# D-027 的真實案例：8,052 token 的 prompt、ctx 4,096 → 被切到 2,050
check("D-027 的實際案例（2050 = 上限）→ truncated", truncation_verdict(2050, 4096)["verdict"], "truncated")
check("D-027 的上限算得出來", truncation_verdict(2050, 4096)["limit"], 2050)
# **本輪真正的鑑別題。** 保留臂三筆（num_predict 1／512／2000）在 ctx 8192
# 下都被切到 4098。舊公式（8192 − 2000 = 6192）會判「4098 遠低於預算 →
# intact」，把被切掉的 prompt 講成完整 —— 而 4098 正是上限。
check("本輪保留臂的實際資料（4098 @ ctx 8192）→ truncated，不是 intact",
      truncation_verdict(4098, 8192)["verdict"], "truncated")
# 上限不是 num_ctx：2050/4096 剛好 50%，拿 num_ctx 當分母看起來完全正常
check("上限不等於 num_ctx（2050 對 4096 只有 50%）",
      truncation_verdict(2050, 4096)["limit"] != 4096, True)
# 邊界：**剛好等於 num_ctx 是「上游變了」，不是「完整」。**
# 觸發條件是 `token 數 > NumCtx − 1`（D-023 第五節，讀自 ollama 原始碼），
# 而 D-027 第三節用 P／P+1 夾縫把它量掉了：num_ctx = 687 = P 時，
# prompt_eval_count 是 **346**（= truncated_length(687)），不是 687。
# 所以「送出 num_ctx 個 token」一定被切，count 不可能停在 num_ctx。
# 我一度把它改成 `<= num_ctx` → intact，理由是「剛好填滿是合法的，
# 判 unexpected 會是假失敗」—— **那個理由錯了**，因為它把一個**已經量過**
# 的邊界當成未知的。看到 count == num_ctx 就是規則變了。
check("剛好填滿 context（4096 @ ctx 4096）→ unexpected，不是 intact",
      truncation_verdict(4096, 4096)["verdict"], "unexpected")
# count > num_ctx 卻不等於上限 —— 同上，也是規則變了。沒有這一條，
# 一個沒被想過的數值會被默默歸進其中一邊。
check("count > num_ctx 又不等於上限 → unexpected",
      truncation_verdict(5000, 4096)["verdict"], "unexpected")
# 溢出臂的設計意圖：這一臂**本來就該被切**，所以「沒撞到」要說成沒量到，
# 不可以拿它去當「prompt 完整」的證據。
check("溢出臂：count = 上限 → truncated",
      truncation_verdict(2050, 4096, overflow_expected=True)["verdict"], "truncated")
check("溢出臂：count < 上限 → unknown（沒撞到，不是沒被切）",
      truncation_verdict(2049, 4096, overflow_expected=True)["verdict"], "unknown")
check("溢出臂：count > 上限 → unexpected",
      truncation_verdict(2100, 4096, overflow_expected=True)["verdict"], "unexpected")
check("讀不到 → unknown", truncation_verdict(None, 8192)["verdict"], "unknown")
check("num_keep 讀不到 → unknown", truncation_verdict(100, 8192, num_keep=None)["verdict"], "unknown")
check("num_ctx=0 → unknown（不是 ZeroDivisionError）", truncation_verdict(100, 0)["verdict"], "unknown")
check("num_keep ≥ num_ctx 到算不出正上限 → unknown",
      truncation_verdict(10, 1, num_keep=4)["verdict"], "unknown")
print(f"  {COUNT - before} 項")

# ══ reservation_verdict：拿觀測到的平台去否證 truncation_limit() ════════════
print("reservation_verdict")
before = COUNT


def rrow(npredict, peval):
    return {"num_predict": npredict, "prompt_eval_count": peval}


# 本輪的真實資料：ctx 8192、三筆 num_predict（1／512／2000）全被切在 4098
rv = reservation_verdict(8192, [rrow(1, 4098), rrow(512, 4098), rrow(2000, 4098)])
check("平台與公式相符 → independent", rv["verdict"], "independent")
check("independent 要報出觀測到的平台", rv["observed_limit"], 4098)
check("independent 要報出公式預測的上限", rv["limit"], 4098)
check("訊息要說出上限與 num_predict 無關", "num_predict" in rv["message"], True)
# D-022／D-027 的 ctx 4096 案例：平台 2050 = 4096 − (4096−4)/2
check("ctx 4096 的平台 2050 也與公式相符",
      reservation_verdict(4096, [rrow(1, 2050), rrow(2000, 2050)])["verdict"], "independent")
# **否證能力**：平台只要跟公式不合，就必須失敗。這是整份探針裡唯一能抓到
# 「上游改了截斷規則」的地方 —— 少了這一條，公式錯了只會被拿去判錯別人。
bad = reservation_verdict(8192, [rrow(1, 4200), rrow(2000, 4200)])
check("平台高於公式 → limit_mismatch（不是 independent）", bad["verdict"], "limit_mismatch")
check("limit_mismatch 要同時報出觀測與預測", (bad["observed_limit"], bad["limit"]), (4200, 4098))
# 平台**低於**公式時，這個函式分不出「這次沒撞到上限」與「撞到了但規則不同」
# —— 因為它不知道我們送出去的 prompt 有多長。這一條把那個極限寫下來：
# 它不是 bug，是**缺一個輸入**（送出的 token 數）。在補上之前，低於公式
# 一律當「沒量到」，那是安全的方向（D-016：寧可沒量到，不要假失敗）。
check("平台低於公式 → not_overflowing（缺送出的長度，分不出來）",
      reservation_verdict(8192, [rrow(1, 4096), rrow(2000, 4096)])["verdict"], "not_overflowing")
# 上限隨 num_predict 移動 = 上游變成「替輸出保留空間」了。
# **這一條以前回 independent，而 _all_ok 接受 independent** —— 也就是說
# 這一臂存在的目的正是偵測這件事，而它偵測到時這一輪照樣回 0。
check("上限隨 num_predict 一對一移動 → reserves_output（不是 independent）",
      reservation_verdict(4096, [rrow(1, 4091), rrow(512, 3580), rrow(2000, 2092)])["verdict"],
      "reserves_output")
check("reserves_output 的訊息要說出斜率", "斜率 -1.00" in
      reservation_verdict(4096, [rrow(1, 4091), rrow(2000, 2092)])["message"], True)
check("上限會動但斜率不是 −1 → moves_with_num_predict（以前回 independent，fail-open）",
      reservation_verdict(4096, [rrow(1, 4091), rrow(2000, 3092)])["verdict"],
      "moves_with_num_predict")
# 反例（**最重要的一條**）：prompt 不夠長、根本沒撞到上限。
# 這一條與上面的 independent 形狀很像（數字都沒動），但意思完全不同：
# 一個是「上限就在這裡」，一個是「這次沒量到」。混在一起會製造假失敗。
# 本輪長 prompt 臂就是這一種：三筆 3021，上限 4098。
check("prompt 沒撞到上限 → not_overflowing（不是 independent）",
      reservation_verdict(8192, [rrow(1, 3021), rrow(2000, 3021)])["verdict"], "not_overflowing")
check("not_overflowing 要說出要加長 prompt",
      "加長 prompt" in reservation_verdict(8192, [rrow(1, 3021), rrow(2000, 3021)])["message"], True)
check("少於兩筆 → unknown",
      reservation_verdict(8192, [rrow(1, 4098)])["verdict"], "unknown")
check("讀不到的列不計入（湊不到兩筆）",
      reservation_verdict(8192, [rrow(1, 4098), {"num_predict": 2000}])["verdict"], "unknown")
check("少於兩筆 → unknown", reservation_verdict(4096, [rrow(1, 4091)])["verdict"], "unknown")
check("兩筆 num_predict 相同 → unknown", reservation_verdict(4096, [rrow(1, 4091), rrow(1, 4000)])["verdict"], "unknown")
check("欄位讀不到的那筆被忽略", reservation_verdict(4096, [rrow(1, 4091), rrow(None, 100), rrow(2000, 2092)])["verdict"], "reserves_output")
print(f"  {COUNT - before} 項")

# ══ cpu_topology_verdict / threads_verdict：機器的形狀 ═══════════════════════
print("cpu_topology_verdict")
before = COUNT
check("本機的形狀（4 個 vCPU、各自一個 socket）→ one_socket_per_cpu",
      cpu_topology_verdict([0, 2, 4, 6], [1, 1, 1, 1])["verdict"], "one_socket_per_cpu")
check("單一 socket → single_socket",
      cpu_topology_verdict([0, 0, 0, 0], [2, 2, 2, 2])["verdict"], "single_socket")
check("1 個 vCPU 不算 one_socket_per_cpu（只有一個沒什麼好說的）",
      cpu_topology_verdict([0], [1])["verdict"], "single_socket")
check("空 → unknown", cpu_topology_verdict([], [])["verdict"], "unknown")
print("threads_verdict")
check("執行緒數等於 vCPU → all_cores", threads_verdict(4, 4)["verdict"], "all_cores")
check("執行緒數少於 vCPU → partial", threads_verdict(2, 4)["verdict"], "partial")
check("partial 要說出多出來的核沒用", "沒有用" in threads_verdict(2, 4)["message"], True)
check("讀不到 → unknown", threads_verdict(None, 4)["verdict"], "unknown")
check("nproc=0 → unknown", threads_verdict(4, 0)["verdict"], "unknown")
print(f"  {COUNT - before} 項")

# ══ 日誌解析：抓不到就要回 None，不可以回 0 ═════════════════════════════════
print("parse_kv_buffer_bytes / parse_n_threads")
before = COUNT
LOG = """srv  cmd = ollama --version
llama_kv_cache:        CPU KV buffer size =  1152.00 MiB
cmn  init: llama threadpool init, n_threads = 4
"""
check("抓得到 MiB", parse_kv_buffer_bytes(LOG), 1152 * MIB)
check("抓得到 n_threads", parse_n_threads(LOG), 4)
check("GiB 也換算得對",
      parse_kv_buffer_bytes("llama_kv_cache: CPU KV buffer size =  1.50 GiB"), int(1.5 * 1024**3))
check("KiB 也換算得對", parse_kv_buffer_bytes("KV buffer size =  512.00 KiB"), 512 * 1024)
# 格式變了 / 沒這行 —— 回 None，不回 0
check("沒有這行 → None（不是 0）", parse_kv_buffer_bytes("no such line here"), None)
check("空字串 → None", parse_kv_buffer_bytes(""), None)
check("不是字串 → None", parse_kv_buffer_bytes(None), None)
check("沒有 n_threads → None", parse_n_threads("nothing"), None)
check("多筆取最後一筆（最後一次載入）",
      parse_kv_buffer_bytes("KV buffer size = 100.00 MiB\nKV buffer size = 200.00 MiB"), 200 * MIB)
check("多筆 n_threads 取最後一筆", parse_n_threads("n_threads = 2\nn_threads = 6"), 6)
# 單位寫法變了 → None，不是猜成 bytes。真正的風險是 ollama 改印 MB 而不是 MiB。
check("單位寫法變了（MB）→ None", parse_kv_buffer_bytes("KV buffer size = 1152 MB"), None)
check("單位寫法變了（GB）→ None", parse_kv_buffer_bytes("KV buffer size = 1.13 GB"), None)
check("小數點壞掉 → None（不是 ValueError 逃出去）",
      parse_kv_buffer_bytes("KV buffer size = 1.1.2 MiB"), None)
print(f"  {COUNT - before} 項")

# ══ classify_failure ══════════════════════════════════════════════════════
print()
print("classify_failure（例外 → D-018 的結束碼）")
before = COUNT

# **這一節是花 16 分鐘買來的。** 第一次完整量測跑到保留規則那一臂時，
# `num_predict=2000` 在 1.92 t/s 下要約 1,041 秒，超過客戶端 900 秒的
# timeout。但 `TimeoutError` **不是** `urllib.error.URLError` 的子類
# （兩者都繼承 OSError，彼此是兄弟），所以它穿透了 main() 的 except，
# Python 以 **1** 結束 —— 而 D-018 的 1 是「準則不成立（上游變了）」。
# 機器只是慢，那句話是**假的事實**，也是本專案最在意的那種錯。
check("TimeoutError → 量不到（2），不是準則不成立（1）",
      classify_failure(TimeoutError("timed out"))["exit"], EXIT_UNMEASURED)
check("TimeoutError 的種類是 timeout",
      classify_failure(TimeoutError("timed out"))["kind"], "timeout")
# urllib 會把 socket 的 timeout 包進 URLError.reason，那是另一條路
check("URLError 包住的 timeout 也算 timeout（走 .reason）",
      classify_failure(urllib.error.URLError(TimeoutError("timed out")))["kind"], "timeout")
check("連不上（URLError）→ 2",
      classify_failure(urllib.error.URLError("Connection refused"))["exit"], EXIT_UNMEASURED)
check("連不上的種類是 unreachable",
      classify_failure(urllib.error.URLError("Connection refused"))["kind"], "unreachable")
# HTTPError 是 URLError 的**子類** —— 分支順序顛倒就會把「回了 500」
# 講成「連不上」，而那是兩件要去做不同事的情況
http_err = urllib.error.HTTPError("http://x/api/generate", 500, "Internal Server Error", {}, None)
check("HTTPError → http_error（不是 unreachable）", classify_failure(http_err)["kind"], "http_error")
check("HTTPError → 2", classify_failure(http_err)["exit"], EXIT_UNMEASURED)
check("一般 OSError → 2", classify_failure(ConnectionResetError("reset"))["exit"], EXIT_UNMEASURED)
check("KeyError（上游形狀變了）→ 2", classify_failure(KeyError("eval_count"))["exit"], EXIT_UNMEASURED)
check("ValueError → 2", classify_failure(ValueError("bad"))["exit"], EXIT_UNMEASURED)
check("TypeError → 2", classify_failure(TypeError("bad"))["exit"], EXIT_UNMEASURED)
# 真的壞掉的是探針自己 —— 那是 3，意思是「去讀 traceback、去改探針」
check("看不懂的例外 → 探針自己壞了（3）",
      classify_failure(RuntimeError("boom"))["exit"], EXIT_BROKEN)
check("AttributeError → 3", classify_failure(AttributeError("boom"))["exit"], EXIT_BROKEN)

# **整節的重點**：沒有任何一種失敗可以回 1。回 1 等於在說「上游變了、
# 準則不成立」，而上面每一種其實都是「我們量不到」。
BAD_EXCEPTIONS = [
    TimeoutError("timed out"),
    urllib.error.URLError("refused"),
    urllib.error.URLError(TimeoutError("timed out")),
    http_err,
    ConnectionResetError("reset"),
    OSError("o"),
    KeyError("k"),
    ValueError("v"),
    TypeError("t"),
    RuntimeError("r"),
    AttributeError("a"),
]
check("十一種失敗裡沒有任何一種被歸成 1（準則不成立）",
      sorted({classify_failure(e)["exit"] for e in BAD_EXCEPTIONS}), [EXIT_UNMEASURED, EXIT_BROKEN])
# 每一種都要有自己的句子，不然人只看得到一個數字
check("每一種失敗都有非空的說明",
      [bool(classify_failure(e)["message"]) for e in BAD_EXCEPTIONS], [True] * len(BAD_EXCEPTIONS))
print(f"  {COUNT - before} 項")

# ══ new_result / _all_ok ══════════════════════════════════════════════════
print()
print("new_result / _all_ok（中斷過的矩陣不是基準線）")
before = COUNT


def good_out():
    """一個「什麼都漂亮」的結果。

    這不是裝飾：沒有這個對照組，下面「中斷過 → 不可以當基準線」可能只是
    因為 `_all_ok` 永遠回 False，而永遠失敗的斷言與永遠通過的一樣沒用
    （D-024 第八節）。
    """
    out = new_result()
    out.update(
        {
            "ollama_url": "http://ollama:11434",
            "instrument_self_test": {"verdict": "respected"},
            "conditions": [
                {
                    "label": "qwen3:4b num_ctx=4096",
                    "rate": 6.7,
                    "stability": "stable",
                    "cap": {"verdict": "respected"},
                    "usable_samples": 7,
                    "samples": [],
                },
                {
                    "label": "qwen3:4b num_ctx=8192",
                    "rate": 4.1,
                    "stability": "stable",
                    "cap": {"verdict": "respected"},
                    "usable_samples": 7,
                    "samples": [],
                },
            ],
            # 保留臂的正確結論是 `independent`（平台固定、且與公式相符）。
            # 這裡原本填的是 `reserves_output` —— 因為在舊的理解下「上限隨
            # num_predict 移動」才叫正常，而 `_all_ok` 擋的是 `independent`。
            # **對照組本身是照著錯的信念寫的**，所以它只能證明「不是永遠回
            # False」，證明不了判定正確。兩邊一起錯時，誰也抓不到誰。
            "reservation": {"verdict": "independent", "observed_limit": 4098, "limit": 4098},
        }
    )
    return out


check("對照組：完整的矩陣 → 可以當基準線", _all_ok(good_out()), True)

# 保留臂與公式不合 → 上游改了截斷規則，這一輪不可信。
_mm = good_out()
_mm["reservation"] = {"verdict": "limit_mismatch", "observed_limit": 4096, "limit": 4098}
check("保留臂與公式不合 → 不可以當基準線", _all_ok(_mm), False)
# 保留臂說「上限會隨 num_predict 移動」→ 同樣是上游變了。
_rv = good_out()
_rv["reservation"] = {"verdict": "reserves_output"}
check("保留臂說上限隨 num_predict 移動 → 不可以當基準線", _all_ok(_rv), False)
# **截斷閘門。** 這一條對應一個真實的缺陷：判定原本寫在逐條件迴圈**外面**、
# 讀 `out["truncation"]` —— 一個從來沒有人設過的鍵，所以永遠不成立，而
# 長 prompt 臂真的被截斷時這一輪照樣回 0。沒有下面這一條，那個缺陷
# 可以安靜地活到下次有人真的撞上截斷。
_tr = good_out()
_tr["conditions"][0]["truncation"] = {"verdict": "truncated"}
check("長 prompt 臂被截斷 → 不可以當基準線", _all_ok(_tr), False)
_tr2 = good_out()
_tr2["conditions"][0]["truncation"] = {"verdict": "intact"}
check("對照組：長 prompt 沒被截斷 → 仍然可以當基準線", _all_ok(_tr2), True)
# 閘門要寫成**允許清單**（必須是 intact），不是封鎖清單（不等於 truncated）。
# `unexpected` 是「count ≥ num_ctx 卻不等於截斷長度」—— 同樣是上游變了，
# 而封鎖清單式的寫法會放它過去。這一條就是拿來殺那個寫法的。
_tr3 = good_out()
_tr3["conditions"][0]["truncation"] = {"verdict": "unexpected"}
check("截斷判定是 unexpected → 也不可以當基準線", _all_ok(_tr3), False)
_tr4 = good_out()
_tr4["conditions"][0]["truncation"] = {"verdict": "unknown"}
check("截斷判定是 unknown → 也不可以當基準線（沒量到不是沒被切）", _all_ok(_tr4), False)

# **穩定度閘門。** 同一種缺陷的第三處：原本寫的是封鎖清單（`== "noisy"`
# 才擋），所以 `unknown`（樣本數不足）會直接放行 —— 一次取樣就能生出一整輪
# 基準線。
#
# **這道閘門的權威在 `baseline_verdict`，不在 `_all_ok`。** `_all_ok` 曾經
# 也有一份同樣的允許清單，而它是**等效的**：函式最後把判定交給
# `baseline_verdict(...) == "usable"`，那份清單已經在那裡，所以 `_all_ok`
# 那一份改不動任何結果（突變台把 `!= "stable"` 換回 `== "noisy"`，216 組
# 輸入一個都沒變）。冗餘的那一份已經刪掉；下面這幾條因此**指名**驗判定，
# 而不是只驗一個 `_all_ok(...) is False` —— 那個 False 有太多別的原因
# 可以給（D-024 第八節：理由不對的斷言等於沒有斷言）。
_st = good_out()
_st["conditions"][0]["stability"] = "unknown"
check("樣本數不足（unknown）→ 不可以當基準線", _all_ok(_st), False)
check("  └ 擋下它的理由是判定本身，不是別的閘門順手擋的",
      baseline_verdict(_st["conditions"])["verdict"], "unstable")
_st2 = good_out()
_st2["conditions"][0]["stability"] = "noisy"
check("散射（noisy）→ 不可以當基準線", _all_ok(_st2), False)
check("  └ 擋下它的理由是判定本身", baseline_verdict(_st2["conditions"])["verdict"], "unstable")
check("對照組：兩個條件都 stable → 仍然可以當基準線", _all_ok(good_out()), True)

# 「樣本不夠」與「樣本太散」要叫下一輪的人做**不同**的事（回去補樣本 vs
# 去看機器），所以訊息必須分得出來，而且都要點名是哪個條件。只驗「兩者都
# 被擋下」會讓同一個 verdict 值吃掉兩種診斷 —— 那正是 D-026 第五節。
_vu = baseline_verdict(_st["conditions"])["message"]
_vn = baseline_verdict(_st2["conditions"])["message"]
check("樣本不足與散射的訊息不同", _vu != _vn, True)
check("樣本不足的訊息講出樣本數下限", str(MIN_SAMPLES_FOR_STABILITY) in _vu, True)
check("樣本不足的訊息點名是哪個條件", _st["conditions"][0]["label"] in _vu, True)
check("散射的訊息講出 CV 門檻", f"{STABILITY_CV_PCT:.1f}" in _vn, True)
check("散射的訊息點名是哪個條件", _st2["conditions"][0]["label"] in _vn, True)

# `--quick` 沒有嘗試建立基準線 → 不可以當基準線，但**也不是失敗**。
# 呼叫端據此回 exit 2（量不到）而不是 1（準則不成立）。
_qk = good_out()
_qk["baseline_attempted"] = False
check("--quick（每條件一次取樣）→ 不可以當基準線", _all_ok(_qk), False)
check("--quick 的 baseline verdict 是 not_attempted",
      baseline_verdict(_qk["conditions"], attempted=_qk["baseline_attempted"])["verdict"], "not_attempted")

# `_all_ok` 的結尾是**委派**：判定必須**恰好**是 `usable`。委派寫成
# `!= "unstable"` 之類的否定式就是 fail-open，而漏掉的每一個判定值都是一個
# 放行的洞 —— `not_attempted`（上面那條）、`single_condition`、`unknown`。
# 下面兩條把後兩個也釘住：**被擋下的理由各自不同**才證明是允許清單，
# 而不是碰巧被某一條擋住。
_one = good_out()
_one["conditions"] = _one["conditions"][:1]
check("只有一個條件 → 不可以當基準線", _all_ok(_one), False)
check("  └ 理由是 single_condition（D-014），不是別的",
      baseline_verdict(_one["conditions"])["verdict"], "single_condition")
_zero = good_out()
for c in _zero["conditions"]:
    c["rate"] = 0.0
check("速率全是 0（量不到）→ 不可以當基準線", _all_ok(_zero), False)
check("  └ 理由是 unknown，不是 stable 也不是 unstable",
      baseline_verdict(_zero["conditions"])["verdict"], "unknown")

# 中斷過就不能下結論 —— 就算中斷前量到的每一個條件都漂亮。
# 這裡刻意沿用那份 good_out()：兩者的**唯一**差別就是中斷。
broken = good_out()
broken["measurement_error"] = classify_failure(TimeoutError("timed out"))
check("中斷過 → 不可以當基準線（即使已量到的部分都漂亮）", _all_ok(broken), False)
check("中斷的原因留在 JSON 裡（呼叫端才看得到）",
      broken["measurement_error"]["kind"], "timeout")

check("新外殼的 measurement_error 預設是 None", new_result()["measurement_error"], None)
# 長生成對照組提早停時，這一輪**不是**基準線 —— 它的速率是一個真實的
# 量測，但回答的不是它掛在標籤上的那個問題。少了這一條守衛，一份標著
# num_predict=1024 卻只生成 130 個 token 的樣本會安靜地進到基準線裡。
#
# 下面兩組都**保留兩個條件**，只把 long_generation 加在其中一個上面。
# 我第一版把 conditions 換成只有一個元素的清單，於是 good_out() 的兩個
# 條件變成一個 —— `_all_ok` 會因為 `baseline_verdict` 回 single_condition
# 而回 False，那個 False 與長生成完全無關。症狀是「提早停 → False」照樣
# 通過，而把守衛整個拿掉它還是通過：一條沒有區辨力的斷言。
def _with_lg(verdict: str):
    base = good_out()
    base["conditions"] = [
        {**base["conditions"][0], "long_generation": {"verdict": verdict, "message": "x"}},
        base["conditions"][1],
    ]
    return base


check("長生成提早停 → 不可以當基準線", _all_ok(_with_lg("void")), False)
check("對照組：長生成撞到上限 → 仍然可以當基準線", _all_ok(_with_lg("matched")), True)
check("對照組：長生成 unknown → 也不可以當基準線", _all_ok(_with_lg("unknown")), False)
# 沒有跑長生成那一臂時（預設）不該被擋 —— 多數基準線不含它
check("對照組：沒有長生成那一臂 → 不影響", _all_ok(good_out()), True)

check("新外殼的 conditions 是空清單（不是 None）", new_result()["conditions"], [])
# 外殼先建好再填 —— 這是「中斷時前面量到的東西要活下來」的前提
check("新外殼帶著 ollama_url 這個欄位（先建好、後填）", "ollama_url" in new_result(), True)
print(f"  {COUNT - before} 項")

# ══ 結果 ═══════════════════════════════════════════════════════════════════
print()
print("=" * 74)
if FAILURES:
    print(f"失敗 {len(FAILURES)}/{COUNT}：")
    for f in FAILURES:
        print(f"  ✗ {f}")
    sys.exit(1)
print(f"全部通過：{COUNT} 項斷言")
sys.exit(0)
