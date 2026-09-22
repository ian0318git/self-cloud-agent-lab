#!/usr/bin/env python3
"""量測推論吞吐量，並把「速率隨什麼變」一起量出來。

為什麼要重打：記錄上所有的 token 速率都來自舊 VM（2 vCPU／3.8 GB）。
2026-09-20 18:15 機器變成 4 vCPU／16 GB，**沒有任何一個已記錄的速率是
在這台機器上量的**。而 item 3 的 1.3–1.7 t/s 是大 context 下的產物，
依 D-014 不能外推成基準線。

三個設計原則，都是這個專案已經付過學費的：

1. **一個數字不是基準線。** D-014：單一條件下的結果不外推。所以這支探針
   拒絕輸出單一速率 —— 它量 ctx 掃描與長度掃描，並在只有一個條件時
   明確回報 `single_condition`，而不是把那個數字當答案。

2. **「看起來有套用」不等於有套用。** 本專案反覆踩到的類別是「參數看起來
   生效、其實沒有」。開發這支探針時，我自己的臨時腳本把 `num_predict`
   放在請求**頂層**（ollama 只認 `think`），結果它被靜默忽略、生成無上限、
   跑了 2,876 個 token 還沒停。所以這支探針開場就先自我檢查：
   送 `num_predict=N`，然後断言 `eval_count <= N`（`cap_respected_verdict`）。

3. **速率是條件的函數。** 同樣的模型在 `eval_count=131` 量到 6.7 t/s，
   在 `n_gen=2876` 的生成裡掉到 `tg=4.54 t/s`（ollama 自己的
   `slot print_timing`）。只報一個數就是偽造精確度。

維度與單位：ollama 的 `*_duration` 欄位是**奈秒**，`eval_count` 是 token 數。
本檔一律用 `_ns` 後綴標明，並在換算處才除以 `NS`。

退出碼沿用 D-018：0 通過、1 準則不成立（上游變了）、2 量不到、3 探針自己壞。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import statistics
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNMEASURED = 2
EXIT_BROKEN = 3

NS = 1_000_000_000


def _is_num(x: Any) -> bool:
    """是數字，而且不是 bool。`isinstance(True, int)` 在 Python 裡是 True，
    但 `eval_count=True` 不該被當成「1 個 token」算進速率。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


# 重複量測散得太開時，那個中位數就不是機器的性質，是這一輪的運氣。
#
# ── 2026-09-22：判定的統計量換了，**門檻沒有放寬** ─────────────────────
# 原本判定的是「全距 / 中位數 ≤ 15%」，而**全距是樣本數的函數**：常態下
# E[全距] = d₂(n)·σ，d₂(3) = 1.6926、d₂(7) = 2.7044。同一台機器、同一個
# 真實 σ，7 次取樣的全距期望值比 3 次**大 60%**。所以把「3 次 ≤ 15%」
# 原封不動搬到 7 次，會變成一個嚴格 60% 的規則 —— 變嚴的不是機器，是
# 估計量。
#
# 換成**變異係數**（樣本標準差 / 中位數），它不隨 n 改變意義。門檻是從
# 舊門檻**換算**來的，不是重新挑的：
#
#     全距 ≤ 0.15·med  ⟺  σ ≤ 0.15·med / 1.6926 = 0.0886·med  ⟺  CV ≤ 8.86%
#
# **這個換算假設常態。** d₂ 是常態理論值，而這裡的雜訊（排程、降頻）尾巴
# 比常態厚，所以它是一個近似。寫在這裡是為了讓它可以被重新推導或被駁倒，
# 而不是變成一個沒人記得來歷的常數 —— 測試會把這條關係釘住。
#
# 換估計量**不保證下一輪會過**：它拿掉的是估計量的假象。如果下一輪仍然
# 不穩，那就是機器真的這麼吵 —— 那也是一個結果。
RANGE_TOLERANCE_PCT = 15.0  # 舊門檻。保留：報表仍然印全距，只是不拿它判定
D2_N3 = 1.6926  # 常態下 n=3 的全距期望值 ÷ σ
D2_N7 = 2.7044  # 常態下 n=7 的。留著是因為它量化了那個陷阱：15 / 2.7044 = 5.55
STABILITY_CV_PCT = 8.9  # = RANGE_TOLERANCE_PCT / D2_N3，四捨五入

# 幾個可用樣本才夠判定離散度。少於這個數回 `unknown` —— **不是回 `stable`**。
#
# 原本 1 個樣本時全距是 0.0，於是 `--quick`（每條件取樣一次）會讓每一個
# 條件都變成「穩定」，一整輪基準線就從一次取樣生出來了。一個樣本沒有
# 任何離散度可言：那不是穩定，是沒量。
MIN_SAMPLES_FOR_STABILITY = 5

# `total_duration` 與 load+prompt_eval+eval 的差。ollama 的欄位是各自獨立的
# 計時器，這個差就是「沒有被歸類的行政開銷」。超過這個秒數代表欄位不再
# 是我們以為的那個意思 —— 這條恆等式是證明「欄位意義沒變」的判準。
DURATION_IDENTITY_TOLERANCE_S = 0.5

# 截斷的門檻與切完的長度都不是常數，是 num_ctx 的函式 —— 見 `truncation_limit()`。
#
# 這裡原本有一個 `TRUNCATION_FRACTION = 0.95`，搭配的註解把
# `num_ctx − num_predict` 講得像已經證實的事實：「ollama 把輸出上限那一段
# 從 context 裡先扣掉」。**那句話從來沒有被量到過。** 它讀起來像結論，
# 所以後來的每一輪都把它當前前提引用。實際量到的是 num_ctx 的一半附近
# （4096→2050、8192→4098），而且與 num_predict 完全無關。
#
# 留著這一段是因為**註解的語氣比公式更危險**：公式錯了，測試有機會抓到；
# 一句自信的錯話，只會被下一輪當成事實繼承下去。

# 「冷樣本」的判準：`load_duration` 超過 0.1 秒才算真的重新載入。
#
# **不可以寫成 `load_duration > 0`。** 我原本就是那樣寫的，而 ollama 對
# **每一個**請求都會回一個非零的 `load_duration` —— 那是排程器把已經載好的
# 模型找出來、釘住的時間。同一組條件連跑五次的實測：
#
#     6.79 秒（真的載入）→ 1.1ms → 1.2ms → 4.0ms → 4.3ms
#
# 所以 `> 0` 會把**整份矩陣的每一個樣本**都判成冷樣本而排除掉，於是
# 「沒有任何條件量到速率」。一個永遠說「不」的判準，和永遠說「是」的
# 判準一樣沒有鑑別力（D-024 第八節）—— 而且它更難發現，因為它輸出的是
# 「這一輪不算」，看起來像謹慎。
#
# 0.1 秒這個門檻的兩邊都很遠：1.8 GiB 的模型就算完全在 page cache 裡，
# 重新載入也要 6.8 秒（68 倍餘裕）；而行政開銷是 4 毫秒（25 倍餘裕）。
COLD_LOAD_THRESHOLD_NS = 100_000_000  # 0.1 秒

# 單一請求的等待上限。**這個數字是花 16 分鐘買來的。**
#
# 第一次完整量測跑到保留規則那一臂時，`num_predict=2000` 那一筆在
# 1.92 t/s 下需要約 1,041 秒，而客戶端寫死 900 秒 —— 於是它被自己的
# timeout 殺掉（ollama 日誌：`500 | 15m0s | POST /api/generate`，
# `n_gen = 1728, tg = 1.92 t/s`，**不是**撞到上限，是客戶端先放棄）。
#
# 這裡有一個躲不掉的循環：**timeout 不能由速率推導，因為量速率正是目的。**
# 所以只能取一個「明顯大於已知最壞情況」的常數。已知最壞情況是
# num_predict=2000 ÷ 1.92 t/s ≈ 1,041 秒；2,700 秒留了 2.6 倍餘裕，
# 連掉到 0.74 t/s 都還撐得住。
DEFAULT_TIMEOUT_S = 2700.0

SHORT_PROMPT = "Count from 1 to 30."
SHORT_PROMPT_TOKENS = 6  # 粗估；真正的值以 prompt_eval_count 為準

LONG_PROMPT_FILLER = (
    "The quick brown fox jumps over the lazy dog. "
    "Pack my box with five dozen liquor jugs. "
)

# 重現 D-014 那種「撞到上限」的長生成，用的 prompt。
#
# **為什麼不能只把 `num_predict` 調大**：`SHORT_PROMPT`（"Count from 1 to
# 30."）在 `num_predict=160` 下自然停在 125–152 個 token、`done_reason=stop`
# —— 模型講完了。把上限調到 1024，量到的還是同一個生成長度，只是標籤換成
# 1024。生成長度是 **prompt** 決定的，不是 `num_predict` 決定的。
#
# 為什麼是「重複同一句話」而不是「寫一篇長文」：
#   - 「寫長文」的長度取決於模型的興致 —— 上限有沒有生效就變成運氣，而
#     這一輪要的正是**一定**會撞到上限。
#   - 「從 1 數到 1000」有明確終點，但模型可能數錯、跳號、或把好幾個數字
#     擠進同一行，token 數不穩定。
#   - 重複一個固定句子是機械性的：不需要推理、沒有答對答錯、模型不會
#     「講完」，而每次重複的 token 數幾乎相同（約 11 個）。要求 1024 個
#     token 就是要求它重複到被上限擋下來為止。
LONG_GEN_PROMPT = (
    "Repeat the following sentence over and over, without stopping, "
    "without numbering, and without adding any commentary:\n\n"
    "The quick brown fox jumps over the lazy dog.\n"
)


# ──────────────────────────────────────────────────────────────────────────
# 純函式：速率與樣本
# ──────────────────────────────────────────────────────────────────────────


def rate(count: Any, duration_ns: Any) -> float | None:
    """token 數 ÷ 奈秒時長 → tok/s。量不到就回 None，**不**回 0 或 inf。

    回 0 會被讀成「很慢」，回 inf 會被讀成「很快」；兩者都是把「沒有資料」
    講成「有一個值」。這個專案對這種事的懲罰最重。
    """
    if not _is_num(count) or not _is_num(duration_ns):
        return None
    if count <= 0 or duration_ns <= 0:
        return None
    return count / (duration_ns / NS)


def duration_identity_error_s(sample: dict[str, Any]) -> float | None:
    """`total − (load + prompt_eval + eval)`，秒。量不到回 None。

    這是「欄位意義沒變」的判準。若 ollama 把某個階段改成不計入
    `total_duration`，這個差就會跳開，而速率會看起來完全正常。
    """
    keys = ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration")
    if not all(_is_num(sample.get(k)) for k in keys):
        return None
    parts = sum(sample[k] for k in keys[1:])
    return (sample["total_duration"] - parts) / NS


def cap_respected_verdict(
    eval_count: Any, num_predict: Any, done_reason: Any
) -> dict[str, Any]:
    """送出去的上限有沒有真的生效？

    這是本探針最重要的一條斷言。開發時把 `num_predict` 放在請求**頂層**，
    ollama 靜默忽略它（只認 `think` 在頂層），生成於是沒有上限。那種失敗
    **不會報錯，只會一直跑** —— 在日誌裡長得像「還在生成」。

    判定：
      - `done_reason == "length"` → 上限是煞車，`eval_count` 必須等於它
      - `done_reason == "stop"`   → 模型自己停的，`eval_count` 只會更少
      - `eval_count > num_predict` → **被忽略了**，整組量測不可信
    """
    if not _is_int(eval_count) or not _is_int(num_predict):
        return {"verdict": "unknown", "message": "eval_count 或 num_predict 讀不到，無法判定上限有沒有生效。"}
    if eval_count > num_predict:
        return {
            "verdict": "ignored",
            "message": (
                f"eval_count={eval_count} 超過 num_predict={num_predict} —— "
                f"上限被靜默忽略了（ollama 只接受頂層的 think，其餘執行期參數"
                f"必須放在 options 裡）。這一輪的速率不可信。"
            ),
        }
    if done_reason == "length" and eval_count != num_predict:
        return {
            "verdict": "unknown",
            "message": (
                f"done_reason=length 但 eval_count={eval_count} ≠ num_predict={num_predict} —— "
                f"兩個欄位對不起來，不知道該信哪一個。"
            ),
        }
    return {
        "verdict": "respected",
        "message": f"eval_count={eval_count} ≤ num_predict={num_predict}（done_reason={done_reason}），上限有效。",
    }


def long_generation_verdict(
    eval_count: Any, num_predict: Any, done_reason: Any
) -> dict[str, Any]:
    """長生成對照組：這一輪的生成長度是不是**真的**等於指定的上限？

    這一條與上面的 `cap_respected_verdict` 問的是**不同的問題**，而兩者的
    答案可以相反。那一條問「上限有沒有被尊重」—— 看到 `done_reason=stop`
    時它回 `respected`，因為模型自己停的並沒有違反上限（那是好事）。
    這一條問「生成長度對不對得上 D-014 的 1024」—— 那裡 `done_reason=stop`
    代表模型提早講完，這一輪就**不是**那一次的對照，不管速率看起來多合理。

    為什麼非要獨立一條：**`num_predict=1024` 寫在條件的標籤裡，不代表產出了
    1024 個 token。** 生成長度是 **prompt** 決定的。本機實測：`SHORT_PROMPT`
    （"Count from 1 to 30."）在 `num_predict=160` 下自然停在 125–152 個 token，
    把上限調到 1024 量到的還是同一個生成長度，只是標籤換了 —— 那會是一個
    標著 1024、實際上是 130 的數字。
    """
    if not _is_int(eval_count) or not _is_int(num_predict):
        return {"verdict": "unknown", "message": "eval_count 或 num_predict 讀不到，無法判定生成長度。"}
    if eval_count > num_predict:
        return {
            "verdict": "void",
            "message": f"eval_count={eval_count} 超過 num_predict={num_predict} —— 上限被忽略了，這一輪不是對照。",
        }
    if eval_count < num_predict:
        return {
            "verdict": "void",
            "message": (
                f"只生成了 {eval_count} 個 token（上限 {num_predict}，done_reason={done_reason}）"
                f"—— 模型提早講完，生成長度與要對照的那一輪不同（少 {num_predict - eval_count} 個），"
                f"**不可以**當成它的對照。"
            ),
        }
    return {
        "verdict": "matched",
        "message": f"生成 {eval_count} 個 token，正好撞到上限（done_reason={done_reason}）—— 生成長度與要對照的條件一致。",
    }


def long_generation_overall(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """長生成對照組的總結：**每一個**樣本都要撞到上限，這一輪才算對照。

    任何一個樣本提早停，整組就不能用 —— 那不是「大部分可用」，因為速率
    統計會把不同生成長度的樣本平均在一起，而平均值不屬於其中任何一個。
    """
    if not verdicts:
        return {"verdict": "unknown", "message": "長生成對照組沒有任何可用樣本。"}
    bad = [v for v in verdicts if v["verdict"] != "matched"]
    if bad:
        return {"verdict": "void", "message": bad[0]["message"]}
    return {
        "verdict": "matched",
        "message": f"{len(verdicts)} 個樣本都撞到上限，生成長度一致。",
    }


def sample_usable(sample: dict[str, Any]) -> dict[str, Any]:
    """這個樣本能不能進速率統計？不能的話是哪一種不能。

    「冷樣本」（這次請求順便把模型載進記憶體）一律排除：m1 的掃描裡，
    重新載入後的第一個樣本量到 5.65 t/s，同組其餘是 6.20／6.82 ——
    同一個東西的第一個樣本不是同一個東西。

    判準是 `load_duration` 超過 `COLD_LOAD_THRESHOLD_NS`，**不是**「大於 0」。
    理由與實測數字見那個常數的註解：ollama 對每個請求都回非零的
    `load_duration`，所以「大於 0」等於排除全部。
    """
    if not _is_int(sample.get("eval_count")):
        return {"usable": False, "reason": "unreadable", "message": "eval_count 不是整數，回應格式與預期不同。"}
    if sample["eval_count"] <= 0:
        return {"usable": False, "reason": "no_generation", "message": "eval_count=0，這次沒有產生任何 token。"}
    if not _is_num(sample.get("eval_duration")) or sample["eval_duration"] <= 0:
        return {"usable": False, "reason": "no_duration", "message": "eval_duration 不是正數，算不出速率。"}
    load = sample.get("load_duration")
    if not _is_num(load):
        return {"usable": False, "reason": "unreadable", "message": "load_duration 讀不到，無法判斷這是不是冷樣本。"}
    if load > COLD_LOAD_THRESHOLD_NS:
        return {
            "usable": False,
            "reason": "cold",
            "message": (
                f"load_duration={load / NS:.2f}s > {COLD_LOAD_THRESHOLD_NS / NS:.1f}s "
                f"—— 這是重新載入後的第一個樣本，排除。"
            ),
        }
    return {"usable": True, "reason": "ok", "message": "可用。"}


def cv_is_stable(cv_pct: float) -> bool:
    """CV 有沒有過門檻。**門檻含**（`<=`）。

    抽成一個函式是為了讓邊界**可以被精確測試**：`STABILITY_CV_PCT` 是 8.9，
    而 8.9 在二進位浮點裡不精確，所以「CV 剛好等於門檻」的資料集在數學上
    造不出來（`100 ± 8.9` 的離均差會是 8.900000000000006）。只有直接拿
    常數比，才測得到 `<=` 與 `<` 的差別 —— 而那個差別就是「門檻含不含」。
    """
    return cv_pct <= STABILITY_CV_PCT


def summarize(rates: list[float]) -> dict[str, Any]:
    """一組速率 → 中位數與離散度。空集合回 n=0，不回 None 假裝有值。

    **判定用的是 `cv_pct`（樣本標準差 / 中位數），不是 `spread_pct`。**
    `spread_pct` 是 (max−min)/中位數，仍然算、仍然印，因為它最直觀；但它是
    樣本數的函數，n 一變就換了意思，所以不拿它判穩定（見 STABILITY_CV_PCT
    的推導）。兩個都放進 summary，是為了讓讀者看得到「被判定的」與「被印出的」
    不是同一個數。

    樣本數不足時回 `unknown`，**不回 `stable`** —— 見
    MIN_SAMPLES_FOR_STABILITY。`unknown` 會擋下基準線判定（允許清單），
    所以「沒量到」不會被當成「沒問題」。
    """
    clean = [r for r in rates if _is_num(r) and r > 0]
    if not clean:
        return {
            "n": 0, "min": None, "median": None, "max": None,
            "spread_pct": None, "cv_pct": None,
            "stability": "unknown", "message": "沒有可用的樣本。",
        }
    med = statistics.median(clean)
    spread = (max(clean) - min(clean)) / med * 100.0 if len(clean) > 1 else 0.0
    base = {
        "n": len(clean),
        "min": round(min(clean), 3),
        "median": round(med, 3),
        "max": round(max(clean), 3),
        "spread_pct": round(spread, 2),
    }
    if len(clean) < MIN_SAMPLES_FOR_STABILITY:
        return {
            **base,
            "cv_pct": None,
            "stability": "unknown",
            "message": (
                f"只有 {len(clean)} 個可用樣本（需要 ≥{MIN_SAMPLES_FOR_STABILITY}）"
                f"—— 樣本太少，離散度沒有意義，**不判定**（不是通過）。"
            ),
        }
    cv = statistics.stdev(clean) / med * 100.0
    if cv_is_stable(cv):
        stability, message = "stable", (
            f"CV {cv:.1f}% ≤ {STABILITY_CV_PCT:.1f}%（= 全距門檻 "
            f"{RANGE_TOLERANCE_PCT:.0f}% 換算到 n={len(clean)}）—— 這一組可以留。"
        )
    else:
        stability, message = "noisy", (
            f"CV {cv:.1f}% > {STABILITY_CV_PCT:.1f}%（全距 "
            f"{spread:.1f}%）—— 重複量測彼此不一致，這一組的**中位數**"
            f"不是機器的性質。"
        )
    return {**base, "cv_pct": round(cv, 2), "stability": stability, "message": message}


# ──────────────────────────────────────────────────────────────────────────
# 純函式：速率隨什麼變
# ──────────────────────────────────────────────────────────────────────────


def _drop_pct(high: float, low: float) -> float:
    return (high - low) / high * 100.0


def dependence_verdict(
    high_label: str,
    low_label: str,
    high_rate: float | None,
    low_rate: float | None,
    threshold_pct: float,
) -> dict[str, Any]:
    """兩個條件下的速率有沒有實質差異。

    `high_label` 是「比較不利的那一端」（例如更長的 context、更長的生成），
    `low_label` 是基準端。方向寫死在參數名裡，免得回報時把方向講反。
    """
    if not _is_num(high_rate) or not _is_num(low_rate):
        return {"verdict": "unknown", "drop_pct": None, "message": f"{high_label} 或 {low_label} 沒有可用速率，這一維沒量到。"}
    if high_rate <= 0 or low_rate <= 0:
        return {"verdict": "unknown", "drop_pct": None, "message": "速率不是正數，無法比較。"}
    drop = _drop_pct(max(high_rate, low_rate), min(high_rate, low_rate))
    if drop <= threshold_pct:
        return {
            "verdict": "flat",
            "drop_pct": round(drop, 2),
            "message": f"{low_label} {low_rate:.2f} t/s 對 {high_label} {high_rate:.2f} t/s，差 {drop:.1f}%（門檻 {threshold_pct:.0f}%）—— 這一維不影響速率。",
        }
    slower, faster = (high_label, low_label) if high_rate < low_rate else (low_label, high_label)
    return {
        "verdict": "dependent",
        "drop_pct": round(drop, 2),
        "message": f"{slower} 比 {faster} 慢 {drop:.1f}%（門檻 {threshold_pct:.0f}%）—— 速率**取決於**這一維，不能只報一個數。",
    }


def baseline_verdict(
    conditions: list[dict[str, Any]],
    min_conditions: int = 2,
    attempted: bool = True,
) -> dict[str, Any]:
    """這一輪量到的東西，夠不夠格被叫做「基準線」？

    D-014 的實作：只有一個條件的速率**不是**基準線，它是一個條件下的觀測。
    這條判定故意會擋下「只跑一次就寫進 README」。

    `attempted=False`（`--quick`，每條件一個樣本）回一個**獨立**的 verdict，
    不回 `unstable`：那一輪從設計上就沒有要建立基準線，把它記成「不穩」是
    在說一件沒發生的事。呼叫端據此回 exit 2（量不到）而不是 1（準則不成立）。
    """
    if not attempted:
        return {
            "verdict": "not_attempted",
            "measured_conditions": 0,
            "message": (
                "這一輪每個條件只有一個樣本（`--quick`），從設計上就**沒有嘗試**"
                "建立基準線 —— 不判定。要基準線請讓每條件 ≥"
                f"{MIN_SAMPLES_FOR_STABILITY} 個樣本。"
            ),
        }
    usable = [c for c in conditions if _is_num(c.get("rate")) and c["rate"] > 0]
    if not usable:
        return {"verdict": "unknown", "measured_conditions": 0, "message": "沒有任何條件量到速率。"}
    if len(usable) < min_conditions:
        return {
            "verdict": "single_condition",
            "measured_conditions": len(usable),
            "message": (
                f"只有 {len(usable)} 個條件量到速率（需要 ≥{min_conditions}）—— "
                f"依 D-014 這不能當基準線，只能當「在那個條件下量到的一個數」。"
            ),
        }
    # **允許清單**：不是 "stable" 的一律擋，包含 "unknown"。
    #
    # 原本這裡寫的是封鎖清單（`== "noisy"` 才擋），於是 `unknown` 直接放行。
    # 加進 `unknown` 之後那個洞立刻有意義：樣本數不足的條件會一路走到
    # 「可以當基準線」。封鎖清單漏掉一個值就是 fail-open，這個專案已經
    # 在 `_all_ok` 的截斷閘門上吃過一次同樣的虧。
    bad = [c for c in usable if c.get("stability") != "stable"]
    if bad:
        noisy = [c for c in bad if c.get("stability") == "noisy"]
        thin = [c for c in bad if c.get("stability") != "noisy"]
        parts = []
        if noisy:
            parts.append(
                f"{len(noisy)} 個條件的重複量測散得太開（CV > {STABILITY_CV_PCT:.1f}%）："
                + "、".join(str(c.get("label")) for c in noisy)
            )
        if thin:
            parts.append(
                f"{len(thin)} 個條件的樣本數不足以判定離散度（< {MIN_SAMPLES_FOR_STABILITY}）："
                + "、".join(str(c.get("label")) for c in thin)
            )
        return {
            "verdict": "unstable",
            "measured_conditions": len(usable),
            "message": "；".join(parts) + "。這一輪不能當基準線。",
        }
    return {
        "verdict": "usable",
        "measured_conditions": len(usable),
        "message": f"{len(usable)} 個條件都量到且穩定，可以當基準線 —— 但引用時必須連條件一起引用。",
    }


# `size` 的斜率比日誌裡真正的 KV buffer 斜率多出來的那一塊。**兩個模型
# 一樣多**（qwen3:4b 167.0 vs 144、qwen2.5:3b 59.1 vs 36，各多 23.0／23.1），
# 所以它與模型無關、與 ctx 成正比 —— 不是權重、也不是 KV。
#
# **來源未確認。** 推測是 compute/graph buffer，要用 llama.cpp 的 buffer
# 日誌才分得出來。在那之前它是一個**觀測到的差額**，不是一個常數：
# 觀測只有一次、只有兩個模型、只有兩個 ctx。
NON_KV_SLOPE_KIB_PER_TOKEN = 23.0


def _kv_slope_one_model(points: list[dict[str, Any]]) -> dict[str, Any]:
    """單一模型的取樣點 → 斜率。**呼叫端必須先按模型分組。**

    為什麼用 `size` 的**差**：同一個模型在不同 ctx 下權重不變，所以兩點的
    斜率裡「模型本身」那一塊會消掉，剩下的幾乎都是 KV cache。

    **「幾乎」是 2026-09-21 補上的。** 原本這裡寫的是「只有 KV cache 會變，
    所以斜率就是 KV bytes/token」—— 那句話沒有量過。拿日誌的
    `KV buffer size` 當獨立儀器對照之後：斜率比日誌**多了約
    `NON_KV_SLOPE_KIB_PER_TOKEN` KiB/token**，而且兩個模型多出來的一樣多
    （qwen3:4b 167.0 vs 144、qwen2.5:3b 59.1 vs 36）。與模型無關、與 ctx
    成正比 —— 那不是權重，也不是 KV。所以這個斜率是
    **「KV + 一個未知的常數項」**，引用時只能當上界。

    「同一個模型」是這個推論的前提，不是背景說明。前提不成立時斜率仍然
    算得出來、仍然是一個數字 —— 見 kv_slope_verdict 的說明。

    線性檢查：三點以上時，用前兩點的斜率預測第三點，差超過 10% 就算不線性
    —— 那代表 `size` 裡還有別的東西隨 ctx 變，斜率不能當 KV 成本用。
    """
    pts = [p for p in points if _is_int(p.get("context_length")) and _is_int(p.get("size_bytes"))]
    pts = sorted(pts, key=lambda p: p["context_length"])
    if len(pts) < 2:
        return {"verdict": "unknown", "bytes_per_token": None, "message": "少於兩個 ctx 取樣點，求不出斜率。"}

    a, b = pts[0], pts[-1]
    d_ctx = b["context_length"] - a["context_length"]
    if d_ctx <= 0:
        return {"verdict": "unknown", "bytes_per_token": None, "message": "兩個取樣點的 ctx 相同，求不出斜率。"}
    slope = (b["size_bytes"] - a["size_bytes"]) / d_ctx
    if slope <= 0:
        return {
            "verdict": "anomalous",
            "bytes_per_token": None,
            "message": f"ctx 從 {a['context_length']} 加到 {b['context_length']}，記憶體反而沒有增加（斜率 {slope:.0f} B/token）—— 這個儀器量到的不是 KV cache。",
        }

    linear = True
    detail = ""
    if len(pts) >= 3:
        m = pts[len(pts) // 2]
        predicted = a["size_bytes"] + slope * (m["context_length"] - a["context_length"])
        err_pct = abs(predicted - m["size_bytes"]) / max(m["size_bytes"], 1) * 100.0
        linear = err_pct <= 10.0
        detail = f"中點 ctx={m['context_length']} 的預測值與實測差 {err_pct:.1f}%。"

    return {
        "verdict": "linear" if linear else "nonlinear",
        "bytes_per_token": int(round(slope)),
        "kib_per_token": round(slope / 1024.0, 1),
        "points": [{"context_length": p["context_length"], "size_mib": round(p["size_bytes"] / 2**20, 1)} for p in pts],
        "message": (
            f"`size` 每 token {slope / 1024.0:.1f} KiB"
            + (f"；{detail}" if detail else "（只有兩點，無法檢查線性）")
            + f"；其中約 {NON_KV_SLOPE_KIB_PER_TOKEN:.0f} KiB/token 不是 KV"
            f"（ctx 4096 下約 {NON_KV_SLOPE_KIB_PER_TOKEN * 4096 / 1024:.0f} MiB），"
            f"所以這只能當 KV 成本的**上界**"
        ),
    }


def kv_slope_verdict(points: list[dict[str, Any]]) -> dict[str, Any]:
    """按模型分組之後，各組各求一次 KV 成本。**分組不是整理，是正確性。**

    `size` 是「權重 + 計算緩衝 + KV cache」的總和，而權重那一塊每個模型
    都不一樣（本機：qwen3:4b 2.33 GiB、qwen2.5:3b 1.80 GiB）。把兩個模型的
    取樣點混在一起排序求斜率，量到的是**模型之間的差異**，不是 ctx 的差異。

    本機實測那份混合的斜率是 **-188439 B/token**（負的），因為 qwen2.5:3b
    在 8192 的總量比 qwen3:4b 在 4096 還小。負的 KV 成本很顯眼，會被發現；
    **真正危險的是兩個模型的權重剛好接近的時候** —— 那時混合斜率是一個
    看起來完全合理的正數，而它什麼都不是。所以分組要靠結構，不能靠數值
    看起來怪不怪。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for p in points:
        if _is_int(p.get("context_length")) and _is_int(p.get("size_bytes")):
            groups.setdefault(str(p.get("model") or "（未標示模型）"), []).append(p)

    if not groups:
        return {
            "verdict": "unknown",
            "bytes_per_token": None,
            "kib_per_token": None,
            "per_model": {},
            "message": "沒有任何可用的 ctx 取樣點。",
        }

    per_model = {m: _kv_slope_one_model(pts) for m, pts in groups.items()}

    parts = []
    for m, v in per_model.items():
        if v["verdict"] in ("linear", "nonlinear"):
            parts.append(f"{m} 每 token {v['kib_per_token']} KiB")
        else:
            # anomalous 與 unknown **都要講出來**。只列成功的那幾組等於用
            # 沉默把一個模型刪掉，而「量不到」被講成「沒這回事」正是這一
            # 整個缺陷的形狀。
            parts.append(f"{m}：{v['message']}")
    summary = "；".join(parts)

    if len(per_model) == 1:
        # 只有一個模型：單一數字有指涉對象，照給 —— **整組的判定原樣回傳**，
        # 包含 anomalous 與 unknown。壓成一個統一的判定會把「這個儀器量的
        # 不是 KV」（anomalous）跟「點不夠，求不出來」（unknown）講成同一
        # 件事，而那兩句話要採取的行動完全不一樣。
        name, only = next(iter(per_model.items()))
        return {**only, "per_model": per_model, "message": f"{name}：{only['message']}"}

    # 多個模型各有自己的 KV 成本 —— **不給單一數字**。平均或總和都是發明
    # 一個不存在的量；而**混合斜率比沒有數字更糟**，見上面的說明。
    # 注意這一條與「有幾組算得出來」無關：只要模型不只一個，頂層就沒有
    # 一個數字可以代表它們。
    verdicts = [v["verdict"] for v in per_model.values()]
    if all(v == "linear" for v in verdicts):
        verdict = "linear"
    elif all(v in ("linear", "nonlinear") for v in verdicts):
        verdict = "nonlinear"
    else:
        # 有任何一組量不到（unknown）或量到的不是 KV（anomalous），整體
        # 就不能宣稱一個乾淨的線性判斷。細節在 per_model 與訊息裡。
        verdict = "unknown"

    return {
        "verdict": verdict,
        "bytes_per_token": None,
        "kib_per_token": None,
        "per_model": per_model,
        "message": summary + "。多個模型各有自己的 KV 成本，不給單一數字。",
    }


# ollama 對 `/api/generate` 的 `num_keep` 預設值。**探針不送 `num_keep`**，
# 所以這是**觀測到的**常數（日誌裡 `n_keep = 4`／`keep=4`），不是我們設的值。
# 這一點要在心裡標記：公式的輸入是觀測來的，ollama 改預設值我們不會收到通知。
OLLAMA_DEFAULT_NUM_KEEP = 4


def truncation_limit(num_ctx: int, num_keep: int = OLLAMA_DEFAULT_NUM_KEEP) -> int:
    """prompt **塞不下時**會被切到多長。

        limit = num_ctx − max((num_ctx − num_keep) / 2, 1)

    這個名字很重要。它前一版叫 `prompt_budget()`，語意是「每個請求能放進
    prompt 的預算」—— **那個語意是錯的**，而錯的語意生出了錯的公式。
    它其實只是「被切之後剩多長」，塞得下的 prompt 根本不會碰到它。

    2026-09-21 量到的（全部是 ctx 4096，除了最後一列）：

        送 430   → count 430    沒切
        送 3290  → count 3290   沒切
        送 4431  → count 2050   **切**
        送 14030 → count 2050   切
        ctx 8192：送 3021 → 3021 沒切；送 14011 → 4098 切

    門檻夾在 3290（沒切）與 4431（切）之間，而 `num_ctx`=4096 正好在裡面；
    ctx 8192 那組也一樣 —— 這是**第二組 ctx** 的確認。

    門檻本身早就定案了，別再從這裡重推：**D-023 第五節**讀自 ollama 0.34.2
    原始碼（觸發條件 `token 數 > NumCtx − 1`，截斷目標就是上面那條公式），
    而 **D-027 第三節**用 P／P+1 夾縫把它量到 1 個 token 之內
    （num_ctx = 687 = P → prompt_eval_count 346；num_ctx = 688 → 687 原封不動）。
    上面那六筆只是同一條規則在新 VM 上的再確認。

    **與 `num_predict` 無關** —— 同一輪把 num_predict 設成 1／512／2000，
    三筆都被切在同一個值（ctx 4096 全 2050、ctx 8192 全 4098）。

    `+2` 是 `num_keep=4` 的產物，不是常數。專案裡所有觀測都是 `keep=4`，
    所以化簡式 `num_ctx/2 + 2` 與完整式給出一樣的答案 —— **沒有任何觀測
    會抗議寫死化簡式**。前一版就是這樣活下來的：一次只多一個觀測，而每個
    新觀測都剛好落在舊答案的判定裡。別再化簡。

    更貴的一課是前一版**根本不是化簡，是另一個公式**（`num_ctx − num_predict`，
    D-027 第四節已經否證過它）。而正確的公式**早就在 D-023 第五節**。
    動手改這一支之前先 grep `DECISIONS.md`：這一節兩次都是「答案已經在
    repo 裡」而不是「還沒有人知道」。
    """
    room = max((num_ctx - num_keep) // 2, 1)
    return max(num_ctx - room, 0)


def truncation_verdict(
    prompt_eval_count: Any,
    num_ctx: Any,
    overflow_expected: bool = False,
    num_keep: Any = OLLAMA_DEFAULT_NUM_KEEP,
) -> dict[str, Any]:
    """prompt 有沒有被切掉。

    **「有沒有被切」不是看 count 大不大，是看它跟送出去的長度一不一致。**
    這一版的判準完全改了：前一版拿 `prompt_eval_count` 去比一個「預算」，
    但那個預算是錯的（而且它把「塞得下」也當成「逼近預算」）。

    現在不需要知道送出的確切 token 數 —— 需要的是**這一臂的設計意圖**：

      `overflow_expected=False`（設計上要塞得下）
          塞得下 → count 就是送出長度，而它一定 `< num_ctx`
          被切   → count 會**剛好等於** limit（切到哪是固定的）
      `overflow_expected=True`（設計上要溢出）
          count == limit → 如預期被切
          count <  limit → 沒撞到，這一臂沒量到
          count >  limit → 我們以為會溢出卻沒有，規則與記錄不符

    `limit < num_ctx` 恆成立，所以「完整」與「被切」兩個判定不相交。
    （唯一的破口：一個**剛好** limit 那麼長、又塞得下的 prompt 會被誤判成
    被切。本探針送的長度是 3,021／14,011 這種，不會落在那裡。）
    """
    if not _is_int(prompt_eval_count) or not _is_int(num_ctx) or not _is_int(num_keep):
        return {"verdict": "unknown", "limit": None, "message": "prompt_eval_count／num_ctx／num_keep 讀不到。"}
    if num_ctx <= 0:
        return {"verdict": "unknown", "limit": None, "message": f"num_ctx={num_ctx} 不是正數。"}
    limit = truncation_limit(num_ctx, num_keep)
    if limit <= 0:
        return {
            "verdict": "unknown",
            "limit": limit,
            "message": f"num_ctx={num_ctx}（num_keep={num_keep}）算不出正的截斷長度，這次量測不成立。",
        }
    if overflow_expected:
        if prompt_eval_count == limit:
            return {
                "verdict": "truncated",
                "limit": limit,
                "message": (
                    f"prompt_eval_count={prompt_eval_count} 正好是被切之後的長度 "
                    f"（num_ctx {num_ctx}、num_keep {num_keep}）—— prompt 被切了，這一組的數字不能用。"
                ),
            }
        if prompt_eval_count < limit:
            return {
                "verdict": "unknown",
                "limit": limit,
                "message": (
                    f"prompt_eval_count={prompt_eval_count} 低於被切之後的長度 {limit} —— "
                    f"prompt 沒有溢出，這一臂沒有量到它要量的東西（要加長 prompt）。"
                ),
            }
        return {
            "verdict": "unexpected",
            "limit": limit,
            "message": (
                f"prompt_eval_count={prompt_eval_count} **高於**被切之後的長度 {limit}，"
                f"但這一臂送的是塞不下的 prompt —— 截斷規則與記錄的不一樣。"
            ),
        }
    if prompt_eval_count == limit:
        return {
            "verdict": "truncated",
            "limit": limit,
            "message": (
                f"prompt_eval_count={prompt_eval_count} 正好是被切之後的長度 {limit}"
                f"（num_ctx {num_ctx}、num_keep {num_keep}）—— 這一臂本該塞得下，"
                f"卻被切了，這一組的數字不能用。"
            ),
        }
    if prompt_eval_count < num_ctx:
        return {
            "verdict": "intact",
            "limit": limit,
            "message": (
                f"prompt_eval_count={prompt_eval_count} 小於 num_ctx {num_ctx} —— "
                f"prompt 完整（塞得下就不會被切，limit {limit} 不適用）。"
            ),
        }
    return {
        "verdict": "unexpected",
        "limit": limit,
        "message": (
            f"prompt_eval_count={prompt_eval_count} ≥ num_ctx {num_ctx}，"
            f"卻不等於被切之後的長度 {limit} —— 截斷規則與記錄的不一樣。"
        ),
    }


def reservation_verdict(num_ctx: int, rows: list[dict[str, Any]], num_keep: int = OLLAMA_DEFAULT_NUM_KEEP) -> dict[str, Any]:
    """prompt 被切在哪裡，以及那個位置與公式預測的一不一致。

    這一臂原本問的是「ollama 有沒有替輸出保留 num_predict 的空間」。答案
    （2026-09-21／D-031）是**沒有** —— 上限只跟 num_ctx 與 num_keep 有關，
    同一輪把 num_predict 設成 1／512／2000，三筆都被切在同一個位置。

    所以它現在問一個更有用的問題：**觀測到的平台，與 `truncation_limit()`
    預測的上限一不一致？** 這是整份探針裡唯一能**否證**那個公式的地方。
    其他地方（`truncation_verdict`）是拿公式去判，公式錯了只會安靜地判錯；
    這裡是拿觀測去對公式，兩者不合就是上游變了，判定必須失敗（exit 1）。

    **前一版的守衛是循環論證**：它用「prompt_eval_count < 預算」當成
    「沒有溢出」的證據，但截斷**正是把** count 壓到預算以下的原因 ——
    所以它在真的被截斷時回報 `not_overflowing`，把量到的東西講成沒量到。
    現在平台本身就是觀測值，不再需要拿預算去猜有沒有溢出。

    `rows` 是 `{"num_predict": p, "prompt_eval_count": c}` 的清單，
    每次都用同一個夠長的 prompt（長到一定會被切）。
    """
    ok = [r for r in rows if _is_int(r.get("num_predict")) and _is_int(r.get("prompt_eval_count"))]
    if len(ok) < 2:
        return {
            "verdict": "unknown",
            "observed_limit": None,
            "limit": None,
            "message": "少於兩筆可用的 (num_predict, prompt_eval_count)，分不出上限在哪。",
        }

    limit = truncation_limit(num_ctx, num_keep)

    # 先看有沒有真的溢出。**這一步不能省**：prompt 若不夠長、根本沒撞到上限，
    # 那三筆的 prompt_eval_count 會完全相同，而那不是「上限與 num_predict 無關」，
    # 是「這次根本沒量到」。把這兩件事混成同一個判定會製造假失敗（D-016）。
    counts = {r["prompt_eval_count"] for r in ok}
    if len(counts) == 1:
        observed = ok[0]["prompt_eval_count"]
        if observed < limit:
            return {
                "verdict": "not_overflowing",
                "observed_limit": None,
                "limit": limit,
                "message": (
                    f"三筆的 prompt_eval_count 都是 {observed}，"
                    f"低於公式預測的上限 {limit} —— prompt 不夠長、沒有撞到上限。"
                    f"這次沒量到，不是「沒有保留規則」（要加長 prompt）。"
                ),
            }
        if observed != limit:
            return {
                "verdict": "limit_mismatch",
                "observed_limit": observed,
                "limit": limit,
                "message": (
                    f"prompt 被切在 {observed}，但公式（num_ctx {num_ctx}、"
                    f"num_keep {num_keep}）預測的是 {limit}，差了 {observed - limit} 個 token。"
                    f"上游的截斷規則與記錄的不一樣，這一輪的判定不可信。"
                ),
            }
        npreds = "／".join(str(r["num_predict"]) for r in ok)
        return {
            "verdict": "independent",
            "observed_limit": observed,
            "limit": limit,
            "message": (
                f"prompt 被切在固定的 {observed}，num_predict 分別是 {npreds} 都一樣 —— "
                f"上限與 num_predict 無關。而且它與公式預測的 {limit} 相符。"
            ),
        }

    deltas = []
    for a, b in zip(ok, ok[1:]):
        d_predict = b["num_predict"] - a["num_predict"]
        d_prompt = b["prompt_eval_count"] - a["prompt_eval_count"]
        if d_predict == 0:
            continue
        deltas.append(d_prompt / d_predict)

    # 走到這裡代表平台**不固定** —— 三筆被切在不同位置。以現在的規則
    # （上限只跟 num_ctx／num_keep 有關）這不可能發生，所以這是「上游變了」
    # 的訊號，**每一條出路都必須讓這一輪失敗**。
    #
    # 這裡原本最後一條回的是 `independent`，而 `_all_ok` 接受 `independent`
    # —— 也就是說：這一臂存在的目的正是偵測「上限隨 num_predict 移動」，
    # 而它一旦真的偵測到（斜率不是 1 的那些情況），這一輪**照樣回 0**。
    # 一個只在不會發生時才通過的檢查。
    if not deltas:
        return {
            "verdict": "unknown",
            "observed_limit": None,
            "limit": limit,
            "message": "每一筆的 num_predict 都一樣，沒有可比較的變化 —— 分不出上限在哪。",
        }
    if all(abs(d + 1.0) <= 0.15 for d in deltas):
        return {
            "verdict": "reserves_output",
            "observed_limit": None,
            "limit": limit,
            "message": (
                f"num_predict 每加 1，prompt 上限就少 1（斜率 {deltas[0]:.2f}）—— "
                f"ollama 又開始替輸出保留空間了，與 2026-09-21 量到的規則不符。"
            ),
        }
    return {
        "verdict": "moves_with_num_predict",
        "observed_limit": None,
        "limit": limit,
        "message": (
            f"prompt 上限會動，但不隨 num_predict 一對一移動"
            f"（斜率 {deltas[0]:.2f}，各筆 {sorted(counts)}）—— "
            f"截斷規則與記錄的不一樣，這一輪的判定不可信。"
        ),
    }


# ──────────────────────────────────────────────────────────────────────────
# 純函式：機器自己長什麼樣
# ──────────────────────────────────────────────────────────────────────────


def cpu_topology_verdict(physical_ids: list[int], siblings: list[int]) -> dict[str, Any]:
    """guest 看到的 CPU 拓樸是不是「每個 vCPU 一個獨立 socket」。

    **本機就是這樣**：`physical id: 0/2/4/6`、`siblings: 1` —— 4 個 vCPU
    被報成 4 顆單核 CPU。宿主是 i7-1260P，大小核混合的筆電晶片。
    ollama 是照 CPU 數決定執行緒數的，所以它看到的形狀會影響它開幾條。

    這不是錯誤，是**必須跟速率一起記錄的條件** —— 否則下一個人會以為
    4 vCPU 就是 4 vCPU，而 2 vCPU 換到 4 vCPU 竟然沒有變快這件事
    就永遠得不到解釋。
    """
    if not physical_ids:
        return {"verdict": "unknown", "message": "讀不到 physical id。"}
    if len(set(physical_ids)) == len(physical_ids) and len(physical_ids) > 1 and all(s == 1 for s in set(siblings) or [1]):
        return {
            "verdict": "one_socket_per_cpu",
            "message": f"{len(physical_ids)} 個 vCPU 被報成 {len(set(physical_ids))} 個獨立 socket（siblings=1）—— guest 看不到大小核或共享快取，速率差異可能來自這裡。",
        }
    if len(set(physical_ids)) == 1:
        return {"verdict": "single_socket", "message": f"{len(physical_ids)} 個 vCPU 在單一 socket 下。"}
    return {"verdict": "other", "message": f"拓樸：physical id={sorted(set(physical_ids))}、siblings={sorted(set(siblings))}。"}


def threads_verdict(n_threads: Any, nproc: Any) -> dict[str, Any]:
    """ollama 實際開的執行緒數 vs 機器上的 vCPU 數。

    推論速率對這個數字很敏感。若 `n_threads < nproc`，那多出來的 vCPU
    對這個模型沒有用，而「加大 VM 卻沒變快」就有了一個具體的候選解釋。
    """
    if not _is_int(n_threads) or not _is_int(nproc) or nproc <= 0:
        return {"verdict": "unknown", "message": "讀不到執行緒數或 vCPU 數。"}
    if n_threads >= nproc:
        return {"verdict": "all_cores", "message": f"ollama 開了 {n_threads} 條執行緒，等於全部 {nproc} 個 vCPU。"}
    return {
        "verdict": "partial",
        "message": f"ollama 只開了 {n_threads} 條執行緒，機器有 {nproc} 個 vCPU —— 多出來的核對這個模型沒有用。",
    }


# KV buffer 行：`llama_kv_cache:        CPU KV buffer size =  1152.00 MiB`
# 這是**佐證**用的第二支儀器。主要儀器是 /api/ps 的 size 斜率（純 API）。
# 抓不到就回 None —— 回 0 會被讀成「KV cache 是 0」，而那是一個假的事實。
_KV_LINE_RE = re.compile(r"KV buffer size\s*=\s*([0-9.]+)\s*(MiB|GiB|KiB|B)\b")
_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}


def parse_kv_buffer_bytes(log_text: str) -> int | None:
    """從 ollama 日誌抓 KV buffer 大小（bytes）。抓不到回 None。"""
    if not isinstance(log_text, str):
        return None
    hits = _KV_LINE_RE.findall(log_text)
    if not hits:
        return None
    value, unit = hits[-1]  # 取最後一筆：最後一次載入的
    # 只攔 ValueError，不攔 KeyError：上面的正規式只捕捉 _UNITS 裡有的四個
    # 單位，所以 KeyError 到不了。日誌若改印 MB（而不是 MiB），正規式就
    # **不匹配** —— 那會走 `if not hits` 那條路回 None，正是我們要的。
    try:
        return int(round(float(value) * _UNITS[unit]))
    except ValueError:
        return None


def parse_n_threads(log_text: str) -> int | None:
    """從共用的日誌抓 `n_threads = N`。抓不到回 None。"""
    if not isinstance(log_text, str):
        return None
    hits = re.findall(r"n_threads\s*=\s*(\d+)", log_text)
    return int(hits[-1]) if hits else None


# ──────────────────────────────────────────────────────────────────────────
# 純函式：結果的外殼與失敗的分類
# ──────────────────────────────────────────────────────────────────────────


def new_result() -> dict[str, Any]:
    """結果的外殼。**先建好、再逐步填滿** —— 這個順序是刻意的。

    第一次完整量測跑了 16 分鐘後死在最後一臂（保留規則），而前面 12 分鐘
    的 ctx 掃描與長度掃描**全部跟著陪葬**，因為 `out` 是建在 `measure()`
    裡面的：例外一拋，函式的區域變數就跟著消失了。一次量測的價值等於
    它最脆弱的那一步，而這裡最脆弱的一步是網路。
    """
    return {
        "ollama_url": None,
        "available_models": [],
        "conditions": [],
        "kv_points": [],
        "notes": [],
        "measurement_error": None,
    }


def classify_failure(exc: BaseException) -> dict[str, Any]:
    """把例外分成 D-018 的兩類：量不到（2）與探針自己壞了（3）。

    **這一條也是花錢買來的，而且買到的是最貴的那一種錯誤 —— 一個假的事實。**
    第一次完整量測跑了 16 分鐘後，`num_predict=2000` 那一筆在 1.92 t/s 下
    需要約 1,041 秒，超過客戶端 900 秒的 timeout。但 `TimeoutError`
    **不是** `urllib.error.URLError` 的子類 —— 兩者都繼承 `OSError`，
    彼此是兄弟 —— 所以它穿透了 main() 裡那串 except，Python 以 **1** 結束。
    而 D-018 的 1 是「準則不成立（上游變了）」。那句話是假的：
    機器只是比我們的耐心先到極限。

    分類的**順序是有作用的**：`Exception` 那條 catch-all 必須在最後。
    擺前面的話每種失敗都會被講成「探針自己壞了」（3），而 3 的意思是
    「去讀 traceback、去改探針」—— 對一個只是連不上的網路來說那是誤導。
    """
    if isinstance(exc, urllib.error.HTTPError):
        # `HTTPError` 是 `URLError` 的子類，所以**必須排在它前面** ——
        # 順序顛倒的話「ollama 回了 500」會被講成「連不上 ollama」，
        # 而那是兩件要去做不同事的情況。中斷那一輪的日誌裡就有一筆
        # `500 | 15m0s | POST /api/generate`。
        return {
            "kind": "http_error",
            "exit": EXIT_UNMEASURED,
            "message": f"ollama 回了 HTTP {exc.code}（{exc.reason}）—— 這一輪沒有量到，不是準則不成立。",
        }
    if isinstance(exc, urllib.error.URLError) and not isinstance(getattr(exc, "reason", None), TimeoutError):
        return {
            "kind": "unreachable",
            "exit": EXIT_UNMEASURED,
            "message": f"連不上 ollama：{exc}",
        }
    if isinstance(exc, (urllib.error.URLError, TimeoutError)):
        # Python 3.10 起 `socket.timeout` 就是 `TimeoutError` 的別名，
        # 不必另外列它。這一條是本次的肇事者。
        # `URLError` 也要收進來：它會把 socket 的 timeout 包在 `.reason` 裡，
        # 而那個包裝過的形狀與直接拋出的 `TimeoutError` 是兩條不同的路。
        return {
            "kind": "timeout",
            "exit": EXIT_UNMEASURED,
            "message": (
                f"等不到回應（單一請求上限 {DEFAULT_TIMEOUT_S:.0f} 秒）。"
                f"最可能是這一筆的生成量在當下的速率下算不完 —— "
                f"用 `--timeout` 放寬，或把 `--reservation-predict` 的最大值調小。"
            ),
        }
    if isinstance(exc, OSError):
        return {
            "kind": "io_error",
            "exit": EXIT_UNMEASURED,
            "message": f"與 ollama 的連線出錯：{type(exc).__name__}: {exc}",
        }
    if isinstance(exc, (KeyError, ValueError, TypeError)):
        return {
            "kind": "upstream_shape",
            "exit": EXIT_UNMEASURED,
            "message": f"ollama 回應的形狀與預期不同：{type(exc).__name__}: {exc}",
        }
    return {
        "kind": "probe_broken",
        "exit": EXIT_BROKEN,
        "message": f"探針自己壞了：{type(exc).__name__}: {exc}（traceback 才是線索）",
    }


# ──────────────────────────────────────────────────────────────────────────
# I/O：量測驅動
# ──────────────────────────────────────────────────────────────────────────


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=60) as r:
            return json.loads(r.read())

    def tags(self) -> list[dict[str, Any]]:
        return self._get("/api/tags").get("models", [])

    def ps(self) -> list[dict[str, Any]]:
        return self._get("/api/ps").get("models", [])

    def generate(self, **body: Any) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())


def _request_body(
    model: str, prompt: str, num_ctx: int, num_predict: int, keep_alive: str, seed: int | None
) -> dict[str, Any]:
    """組出請求。**執行期參數一律放 `options`。**

    ollama 的 `/api/generate` 只認得頂層的 `model`／`prompt`／`stream`／
    `think`／`keep_alive` 等；`num_ctx`、`num_predict`、`temperature`
    必須在 `options` 底下。放在頂層**不會報錯，會被忽略**。
    """
    options: dict[str, Any] = {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict}
    if seed is not None:
        options["seed"] = seed
    return {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        "options": options,
    }


def _load_at(client: OllamaClient, model: str, num_ctx: int, keep_alive: str) -> dict[str, Any]:
    """把模型以指定的 num_ctx 載進記憶體，回傳 /api/ps 的那一筆。

    只送 num_predict=1 的請求：目的是逼 ollama 用新的 ctx 重新載入，
    不是要生成。載入本身約 30 秒（本機實測 31.33 秒）。
    """
    client.generate(**_request_body(model, "hi", num_ctx, 1, keep_alive, None))
    for m in client.ps():
        if m.get("name") == model or m.get("model") == model:
            return m
    return {}


def _sample(
    client: OllamaClient, model: str, prompt: str, num_ctx: int, num_predict: int, keep_alive: str, seed: int | None
) -> dict[str, Any]:
    """一次生成，回傳原樣的回應（判定留給純函式）。"""
    return client.generate(**_request_body(model, prompt, num_ctx, num_predict, keep_alive, seed))


def _condition(
    client: OllamaClient,
    label: str,
    model: str,
    prompt: str,
    num_ctx: int,
    num_predict: int,
    repeats: int,
    keep_alive: str,
    seed: int | None,
) -> dict[str, Any]:
    """一個條件 = 一次暖機 + N 次取樣。

    暖機那次不進統計：重新載入後的第一個樣本量到 5.65 t/s，同組其餘是
    6.20／6.82。暖機同時是「把模型留在記憶體裡」的手段 ——
    `keep_alive` 若在取樣途中過期，後面的樣本就會帶 `load_duration > 0`
    而被 `sample_usable` 擋掉，那是看得見的，不會靜默污染。
    """
    warm = _sample(client, model, prompt, num_ctx, num_predict, keep_alive, seed)
    samples = []
    for _ in range(repeats):
        d = _sample(client, model, prompt, num_ctx, num_predict, keep_alive, seed)
        samples.append(
            {
                "eval_count": d.get("eval_count"),
                "eval_duration_ns": d.get("eval_duration"),
                "prompt_eval_count": d.get("prompt_eval_count"),
                "prompt_eval_duration_ns": d.get("prompt_eval_duration"),
                "load_duration_ns": d.get("load_duration"),
                "total_duration_ns": d.get("total_duration"),
                "done_reason": d.get("done_reason"),
                "decode_tps": rate(d.get("eval_count"), d.get("eval_duration")),
                "prefill_tps": rate(d.get("prompt_eval_count"), d.get("prompt_eval_duration")),
                "identity_error_s": duration_identity_error_s(d),
                "usable": sample_usable(d),
            }
        )
    usable = [s for s in samples if s["usable"]["usable"]]
    summary = summarize([s["decode_tps"] for s in usable])
    return {
        "label": label,
        "model": model,
        "num_ctx": num_ctx,
        "num_predict": num_predict,
        "prompt_chars": len(prompt),
        "warmup": {
            "eval_count": warm.get("eval_count"),
            "load_duration_ns": warm.get("load_duration"),
            "done_reason": warm.get("done_reason"),
        },
        "samples": samples,
        "usable_samples": len(usable),
        "excluded": [s["usable"]["reason"] for s in samples if not s["usable"]["usable"]],
        "rate": summary["median"],
        "stability": summary["stability"],
        "summary": summary,
        "prefill_tps": summarize([s["prefill_tps"] for s in usable])["median"],
        "prompt_eval_count": usable[0]["prompt_eval_count"] if usable else None,
        "cap": cap_respected_verdict(
            max((s["eval_count"] or 0) for s in samples) if samples else None, num_predict, samples[0]["done_reason"] if samples else None
        ),
    }


def _instrument_self_test(client: OllamaClient, model: str, num_ctx: int, keep_alive: str) -> dict[str, Any]:
    """開場先證明「送進去的參數有生效」。

    送 `num_predict=24`，若 ollama 忽略了它，`eval_count` 會超過 24
    —— 這正是開發時燒掉十幾分鐘的那個 bug。先擋在這裡，後面所有的
    數字才有意義。
    """
    d = _sample(client, model, "Count slowly from 1 to 200, one number per line.", num_ctx, 24, keep_alive, None)
    verdict = cap_respected_verdict(d.get("eval_count"), 24, d.get("done_reason"))
    return {
        "requested_num_predict": 24,
        "eval_count": d.get("eval_count"),
        "done_reason": d.get("done_reason"),
        **verdict,
    }


def measure(client: OllamaClient, args: argparse.Namespace, out: dict[str, Any]) -> None:
    """跑完整個矩陣，**逐步填進 `out`**。這裡只有 I/O，判定都在純函式裡。

    刻意**不回傳**新字典：呼叫端必須自己先用 `new_result()` 建好外殼再傳進來。
    回傳值的話，這個函式就得在最後一刻才組出結果，中途失敗等於全部重來 ——
    那正是第一次完整量測的下場（見 `new_result()`）。
    """
    out["ollama_url"] = client.base_url

    tags = client.tags()
    out["available_models"] = [
        {"name": m.get("name"), "size_bytes": m.get("size"), "digest": (m.get("digest") or "")[:12]} for m in tags
    ]

    models = args.models
    ctx_values = args.ctx
    repeats = 1 if args.quick else args.repeats

    out["instrument_self_test"] = _instrument_self_test(client, models[0], ctx_values[-1], args.keep_alive)

    # ── ctx × model 掃描：速率的主表
    for model in models:
        for num_ctx in ctx_values:
            ps = _load_at(client, model, num_ctx, args.keep_alive)
            if ps:
                out["kv_points"].append(
                    {
                        "model": model,
                        "context_length": ps.get("context_length"),
                        "size_bytes": ps.get("size"),
                        "label": f"{model}@{num_ctx}",
                    }
                )
            out["conditions"].append(
                _condition(
                    client,
                    f"{model} num_ctx={num_ctx}",
                    model,
                    SHORT_PROMPT,
                    num_ctx,
                    args.num_predict,
                    repeats,
                    args.keep_alive,
                    args.seed,
                )
            )

    # ── 生成長度掃描：速率隨生成位置下降（D-014 的守衛）
    longest = ctx_values[-1]
    for npred in args.length_predict:
        out["conditions"].append(
            _condition(
                client,
                f"{models[0]} num_ctx={longest} num_predict={npred}",
                models[0],
                SHORT_PROMPT,
                longest,
                npred,
                repeats,
                args.keep_alive,
                args.seed,
            )
        )

    # ── 長生成：重現 D-014 的條件（撞到上限、done_reason=length）。
    # 用**專用的 prompt**，理由見 LONG_GEN_PROMPT 的註解：換掉上限沒用，
    # 生成長度是 prompt 決定的。
    # 預設**不跑**（--longgen-predict 預設是空的）：一般基準線不需要它，
    # 而把它塞進預設矩陣會讓每一次重打都多花十幾分鐘。
    for npred in args.longgen_predict:
        cond = _condition(
            client,
            f"{models[0]} num_ctx={longest} num_predict={npred} 長生成",
            models[0],
            LONG_GEN_PROMPT,
            longest,
            npred,
            repeats,
            args.keep_alive,
            args.seed,
        )
        cond["long_generation"] = long_generation_overall(
            [
                long_generation_verdict(s["eval_count"], npred, s["done_reason"])
                for s in cond["samples"]
                if s["usable"]["usable"]
            ]
        )
        out["conditions"].append(cond)

    # ── 長 prompt：prefill 速率與截斷檢查。
    # 這一份**要放得下**（不然它會被判 truncated，而那是另一個實驗的事）。
    #
    # ⚠️ **這一臂的 prefill 數字不可引用**（2026-09-21）：探針不控制 prefix
    # cache 重用，重複送同一個 prompt 會被 ollama 直接複用既有 KV，於是
    # prompt_eval_duration 量到的是「少算了多少」。本輪報出 306～14,710 t/s，
    # 而日誌顯示 `cached n_tokens = 1448`；冷 prefill 實測約 25 t/s。
    # 這一臂仍然有用 —— 它量的是**截斷**，而截斷看的是 prompt_eval_count。
    if not args.quick:
        fitted = LONG_PROMPT_FILLER * args.long_prompt_repeats
        prompt = f"{fitted}\n\nNow count from 1 to 10."
        long_cond = _condition(
            client,
            f"{models[0]} num_ctx={longest} 長 prompt",
            models[0],
            prompt,
            longest,
            32,
            # **不可以寫死。** 這裡原本是字面值 `2`，而穩定度規則要求
            # `>= MIN_SAMPLES_FOR_STABILITY`（5）。`2 < 5` 是常數不等式，
            # 所以這一臂的 `stability` 恆為 `unknown`、`baseline_verdict`
            # 的允許清單恆拒絕、整輪 exit **恆為 1** —— 對任何可能的執行
            # 都一樣，連算都不用算（D-033 第二節，含實測反駁測試）。
            # 症狀是「5 個條件很吵」，而真正的原因藏在那句話後面。
            repeats,
            args.keep_alive,
            args.seed,
        )
        # `overflow_expected` 用預設的 False —— 這一臂設計上**要放得下**。
        # `num_keep` 也不傳：探針不送 num_keep，那是觀測到的 ollama 預設值。
        # 舊版這裡的第三個位置參數傳的是 `long_cond["num_predict"]`，因為當時
        # 記的公式是 `num_ctx − num_predict`。那個公式已被否證（D-031），
        # 而 num_predict 現在連位置都不對了 —— 第三個是 overflow_expected。
        long_cond["truncation"] = truncation_verdict(long_cond["prompt_eval_count"], longest)
        out["conditions"].append(long_cond)

        # ── 截斷規則的**否證處**：只動 num_predict，看被切的位置跟不跟著移動。
        # 量到的是「不跟」—— 上限只跟 num_ctx／num_keep 有關，這才是 D-027 的
        # 「8,052 被切到 2,050」的正解（那裡只有一個觀測，分不出來）。
        # **這一份必須放不下**：prompt 沒撞到上限就不會截斷，
        # 而沒有截斷就量不到上限在哪（會回 not_overflowing，不是結論）。
        overflow = LONG_PROMPT_FILLER * args.overflow_prompt_repeats
        rows = []
        for npred in args.reservation_predict:
            d = _sample(client, models[0], overflow, longest, npred, args.keep_alive, args.seed)
            rows.append(
                {
                    "num_predict": npred,
                    "prompt_eval_count": d.get("prompt_eval_count"),
                    "done_reason": d.get("done_reason"),
                }
            )
        out["reservation_rows"] = rows
        out["reservation"] = reservation_verdict(longest, rows)


# ──────────────────────────────────────────────────────────────────────────
# 報表
# ──────────────────────────────────────────────────────────────────────────


def _all_ok(out: dict[str, Any]) -> bool:
    # 中斷過就沒有基準線 —— 不論中斷前量到的部分看起來多漂亮。
    # 中斷代表矩陣是**殘缺**的，而 D-014 要的正是多個條件；
    # 一份缺了最後一臂的矩陣，與只有一個條件一樣不能外推。
    if out.get("measurement_error"):
        return False
    if out.get("instrument_self_test", {}).get("verdict") != "respected":
        return False
    for c in out.get("conditions", []):
        if c.get("cap", {}).get("verdict") != "respected":
            return False
        # 長生成對照組若提早停，這一輪**不是**它要對照的那個條件 ——
        # 而「上限有沒有被尊重」看不出這件事（stop 也回 respected）。
        # 少了這一條，一個標著 num_predict=1024、實際只生成 130 個 token
        # 的樣本會被當成 1024 的對照寫進記錄裡。
        lg = c.get("long_generation")
        if lg is not None and lg.get("verdict") != "matched":
            return False
        if c.get("usable_samples", 0) < 1:
            return False
        # 這裡原本還有一道穩定度閘門（允許清單），理由同樣是 `unknown` 會
        # 放行。**已經拿掉 —— 它是等效的。** 函式最後把判定整個交給
        # `baseline_verdict(...) == "usable"`，而那裡已經是同一條允許清單，
        # 所以這道閘門不管寫成允許清單還是封鎖清單，都改不了回傳值：
        # 突變台把 `!= "stable"` 換回 `== "noisy"` 之後，216 組輸入的
        # `_all_ok` 輸出**一個都沒變**。
        #
        # 一個改不了結果的檢查不是檢查，是長得像程式的註解；而它更糟的
        # 副作用是生出一條永遠殺不掉的突變 —— 於是一份「全部抓到」的
        # 突變報告裡，有一條其實是在說「這裡有一行無效的程式碼」。
        # 規則**唯一**的權威是 `baseline_verdict`，突變台也只釘那裡。
        for s in c.get("samples", []):
            err = s.get("identity_error_s")
            if err is not None and abs(err) > DURATION_IDENTITY_TOLERANCE_S:
                return False
        # 長 prompt 臂被截斷就沒有基準線。**這一條原本寫在迴圈外面**，
        # 讀的是 `out["truncation"]` —— 而那個鍵從來沒有人設過（截斷是
        # 存在條件裡的 `long_cond["truncation"]`）。所以它讀到 None、
        # 判定永遠不成立：長 prompt 臂真的被截斷時，這一輪照樣回 0。
        # 一個永遠為真的檢查。
        #
        # 寫成**允許清單**：有這個鍵就必須是 `intact`。`unexpected`
        # （count ≥ num_ctx 卻不等於截斷長度）也一併擋下 —— 那也是上游變了。
        # 封鎖清單漏掉一個 verdict 值時是 fail-open，這一輪就是這樣漏的。
        tr = c.get("truncation")
        if tr is not None and tr.get("verdict") != "intact":
            return False
    # 保留臂只有一個可接受的結論：平台固定、且與 truncation_limit() 相符。
    # 寫成**允許清單**而不是封鎖清單 —— 封鎖清單漏掉一個 verdict 值時
    # 是 fail-open，而這一臂的 verdict 正是會隨診斷增修的那種。
    # 前一版是封鎖 `independent`，但 `independent` 在兩條不同的出路都會回，
    # 其中一條（上限隨 num_predict 移動）正是這一臂要抓的上游變更。
    if (out.get("reservation") or {}).get("verdict") != "independent":
        return False
    return (
        baseline_verdict(
            out.get("conditions", []),
            attempted=out.get("baseline_attempted", True),
        )["verdict"]
        == "usable"
    )


def _report(out: dict[str, Any]) -> None:
    p = print
    p("═" * 78)
    p("  推論吞吐量基準線")
    p("═" * 78)

    err = out.get("measurement_error")
    if err:
        p(f"\n**量測中斷**（{err['kind']}）：{err['message']}")
        p(f"  中斷前的結果仍然列在下面，但這一份**不是**基準線 —— 矩陣缺了後面的臂。")

    st = out.get("instrument_self_test", {})
    p(f"\n儀器自我檢查：{st.get('message', '（沒有結果）')}")

    p("\n── 模型 ──")
    for m in out.get("available_models", []):
        p(f"   {m['name']:<24} {m['size_bytes'] / 2**30:>5.2f} GiB  {m['digest']}")

    p("\n── 速率 ──")
    # `CV` 那一欄才是**被判定的**統計量（樣本標準差/中位數）；`全距` 只是
    # 一起印出來看。判定看 CV 的理由見 STABILITY_CV_PCT 的推導 —— 全距是
    # 樣本數的函數，n 一變就換了意思。
    p(f"   {'條件':<44} {'decode t/s':>11} {'prefill t/s*':>12} {'n':>2} {'CV':>7} {'全距':>7}")
    for c in out.get("conditions", []):
        s = c["summary"]
        r = f"{c['rate']:.2f}" if isinstance(c.get("rate"), (int, float)) else "  —  "
        pf = f"{c['prefill_tps']:.1f}" if isinstance(c.get("prefill_tps"), (int, float)) else "  —  "
        cv = f"{s.get('cv_pct'):.1f}%" if isinstance(s.get("cv_pct"), (int, float)) else "  —  "
        sp = f"{s.get('spread_pct'):.1f}%" if isinstance(s.get("spread_pct"), (int, float)) else "  —  "
        p(f"   {c['label']:<44} {r:>11} {pf:>12} {c['usable_samples']:>2} {cv:>7} {sp:>7}")
        # 長生成那一條的判定要緊跟在它的數字下面。把它擺在報表最後的話，
        # 讀者會先看到一個速率、再看到「這不是對照」—— 順序反了就看不出
        # 那個數字不能用了。
        lg = c.get("long_generation")
        if lg is not None:
            p(f"      ↳ 長生成：{lg['message']}")

    # **prefill 那一欄不可以獨立引用。** 2026-09-21 發現：探針沒有控制
    # prefix cache 的重用 —— ollama 對同一個 prompt 前綴會直接複用既有的
    # KV，於是 prompt_eval_duration 量到的是「複用了多少」而不是「算了多少」。
    # 本輪同一支探針報出 306～14,710 t/s，而日誌顯示第一個請求還帶著
    # `cached n_tokens = 1448`；真實的冷 prefill 是 **約 25 t/s**（慢了
    # 三個數量級）。API 回應裡看不到 cached token 數，只有 ollama 日誌有，
    # 所以這不是「再算一次就好」的誤差，是這一欄**現在沒有可信度**。
    # decode 那一欄不受影響：它量的是生成，與 prompt 前綴無關。
    p("   * prefill 速率**沒有控制 prefix cache 重用**，同一支探針報出"
      " 306～14,710 t/s，")
    p("     而日誌顯示請求帶著 cached n_tokens（本輪 1,448）、真實冷 prefill"
      " 約 25 t/s。")
    p("     這一欄現在不可引用；decode 那一欄不受影響。（見 D-031）")

    kv = kv_slope_verdict(out.get("kv_points", []))
    # 標題不能只寫「KV cache」：底下那個數字含一個非 KV 的項（見
    # NON_KV_SLOPE_KIB_PER_TOKEN），寫成 KV 就是替那個數字背書。
    p(f"\n── KV cache（size 的斜率，非純 KV）──\n   {kv['message']}")

    res = out.get("reservation")
    if res:
        # 這一節原本叫「prompt 預算的保留規則」，問的是 ollama 有沒有替輸出
        # 保留 num_predict 的空間。答案是没有（D-031），所以它現在是
        # **公式的否證處**：拿觀測到的平台去對 truncation_limit() 的預測。
        p(f"\n── prompt 上限（截斷規則）──\n   {res['message']}")
        for r in out.get("reservation_rows", []):
            c = r.get("prompt_eval_count")
            p(f"   num_predict={r['num_predict']:>5} → prompt_eval_count={c if c is not None else '—':>6}  done={r.get('done_reason')}")
    for c in out.get("conditions", []):
        if c.get("truncation"):
            p(f"   截斷檢查：{c['truncation']['message']}")

    p("\n── 診斷 ──")
    for note in out.get("notes", []):
        p(f"   {note}")


def build_parser() -> argparse.ArgumentParser:
    """命令列介面。

    抽出來是為了讓離線測試能用**探針自己的預設值**組出 args，餵給
    `measure()` —— 否則測試得自己抄一份預設值，而抄的那一份會漂移，
    於是「測試通過」講的是抄本而不是探針（D-033 第八節第 3 條：
    手邊有一個能查的權威，就不要從別的地方推導）。
    """
    ap = argparse.ArgumentParser(
        description="量測推論吞吐量基準線（速率一律與條件一起回報）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default="http://ollama:11434", help="ollama 端點（預設 %(default)s）")
    ap.add_argument("--models", nargs="+", default=["qwen3:4b", "qwen2.5:3b"], help="要量的模型")
    ap.add_argument("--ctx", nargs="+", type=int, default=[4096, 8192], help="要掃的 num_ctx")
    ap.add_argument("--num-predict", type=int, default=160, help="短 prompt 的生成上限")
    ap.add_argument(
        "--longgen-predict",
        nargs="+",
        type=int,
        default=[],
        help="長生成對照組的上限（用 LONG_GEN_PROMPT，一定會撞到上限）。預設不跑",
    )
    ap.add_argument("--length-predict", nargs="+", type=int, default=[128, 512], help="生成長度掃描的上限")
    ap.add_argument("--reservation-predict", nargs="+", type=int, default=[1, 512, 2000], help="量保留規則時要掃的 num_predict")
    ap.add_argument(
        "--repeats",
        type=int,
        default=7,
        help=(
            f"每個條件取樣幾次（預設 %(default)s）。少於 {MIN_SAMPLES_FOR_STABILITY} 次時"
            "離散度無從判定，該條件會回 unknown 並擋下基準線判定 —— **不是通過**。"
            "取樣次數是最貴的一維：整輪時間大致與它成正比"
        ),
    )
    ap.add_argument("--long-prompt-repeats", type=int, default=150, help="量 prefill 速率的長 prompt 重複次數（要放得下）")
    ap.add_argument("--overflow-prompt-repeats", type=int, default=700, help="量保留規則的長 prompt 重複次數（要放不下）")
    ap.add_argument("--keep-alive", default="10m", help="模型留在記憶體的時間")
    ap.add_argument("--seed", type=int, default=0, help="固定種子（讓重跑的 token 數一致）")
    ap.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help="單一請求的等待上限秒數（預設 %(default)s）。放寬它會讓真的卡住的請求多等一輪，"
        "但設得比最慢的那一筆還短會讓整輪量測白跑 —— 見 DEFAULT_TIMEOUT_S 的註解",
    )
    ap.add_argument("--quick", action="store_true", help="只跑 ctx 掃描、每條件一次取樣")
    ap.add_argument("--json", action="store_true", help="量測結果以 JSON 寫 stdout，人看的報告寫 stderr")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if any(c <= 0 for c in args.ctx):
        print("--ctx 必須是正整數", file=sys.stderr)
        return EXIT_BROKEN
    if args.repeats < 1 or args.num_predict < 1:
        print("--repeats 與 --num-predict 必須 ≥ 1", file=sys.stderr)
        return EXIT_BROKEN

    client = OllamaClient(args.url, args.timeout)

    # 外殼先建好再量。中斷時前面量到的東西才留得住（見 new_result()）。
    out = new_result()
    failure: dict[str, Any] | None = None
    try:
        measure(client, args, out)
    except Exception as e:  # noqa: BLE001 —— 這裡刻意收全部：漏掉任何一種例外，
        # 直譯器就會用 1 結束，而 1 在 D-018 是「準則不成立（上游變了）」。
        # 那是一個假的事實。分類交給純函式，可以離線測。
        failure = classify_failure(e)
        out["measurement_error"] = failure
        if failure["kind"] == "probe_broken":
            print(traceback.format_exc(), file=sys.stderr)
        print(failure["message"], file=sys.stderr)

    out["kv"] = kv_slope_verdict(out.get("kv_points", []))
    # `--quick` 每條件只取樣一次，從設計上就沒有要建立基準線。這是輸入的
    # 性質，不是量到的結果，所以要傳進去而不是從 conditions 推回來。
    out["baseline_attempted"] = not args.quick
    out["baseline"] = baseline_verdict(
        out.get("conditions", []), attempted=out["baseline_attempted"]
    )
    for c in out["conditions"]:
        for s in c.get("samples", []):
            if s.get("load_duration_ns", 0) and s["usable"]["reason"] == "cold":
                out["notes"].append(f"{c['label']}：有冷樣本被排除（keep_alive 可能在取樣途中過期）。")

    if args.json:
        with contextlib.redirect_stdout(sys.stderr):
            _report(out)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        _report(out)
        # 這裡原本寫 `p(...)` —— 但 `p = print` 只是 `_report()` 的**區域**變數，
        # 所以預設（不帶 --json）的這條路一走就 NameError。我自己每次都跑
        # `--json`，所以它一直沒被走到：**沒被走過的路就是沒有測試的路。**
        print(f"\n基準線判定：{out['baseline']['message']}")

    # 中斷時回報「量不到」，不回報「準則不成立」。就算中斷前量到的部分
    # 看起來完全可以下判斷也一樣 —— 這一輪的矩陣是殘缺的，殘缺的矩陣
    # 不能給出結論（D-014）。
    if failure is not None:
        return failure["exit"]
    if _all_ok(out):
        return EXIT_PASS
    # `--quick` 沒有嘗試建立基準線，所以「沒有基準線」不是**失敗** ——
    # 回 2（量不到）而不是 1（準則不成立）。把沒做的事記成做失敗，
    # 就是 D-016 說的假失敗。
    if out["baseline"]["verdict"] == "not_attempted":
        return EXIT_UNMEASURED
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
