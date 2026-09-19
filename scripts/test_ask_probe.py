#!/usr/bin/env python3
"""ask_probe.py 的離線單元測試（不連網、不需容器）。

這組測試全部對應 D-016：2026-09-19 實測時，出錯的不是模型，是這支工具
**自己** —— 對照題答對了卻被判成答錯，工具於是印出「這才是換模型的訊號」。
本工具原本沒有測試檔，所以那個缺陷在第一次真正跑到之前沒有任何防線。

執行：python3 scripts/test_ask_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ask_probe as a  # noqa: E402


def _resp(text):
    return {"response": text}


_mcp = next(q for q in a.QUESTIONS if q["id"] == "mcp")
_http = next(q for q in a.QUESTIONS if q["id"] == "http")

# ── expect_all 的每一項是「一組替代字串」，不是單一字串 ────
# 原本每題只收一個英文字串，於是**答對但寫中文全名**的模型被判成答錯。
# 那是假失敗 —— 比漏報更糟，因為它讓人開始懷疑一個其實沒問題的設定。
assert a.grade(_resp("The answer is Model Context Protocol."), _mcp) == (True, [])
assert a.grade(_resp("MODEL CONTEXT PROTOCOL"), _mcp) == (True, [])  # 大小寫不敏感
assert a.grade(_resp("MCP 的英文全名是模型上下文協定。"), _mcp) == (True, [])
assert a.grade(_resp("HTTP 的英文全名是超文本傳輸協定。"), _http) == (True, [])
# 簡體中文同樣算命中 —— 用字正不正確由 simplified_hits 另行判定，
# 不該讓它汙染真偽判定
assert a.grade(_resp("HTTP 的全名是超文本传输协议。"), _http) == (True, [])

# 三個**真實發生過**的錯誤答案必須被擋下。放寬比對不可以把真陽性吃掉 ——
# 這三筆分別來自 D-014 與 2026-09-19 的兩次實測（同一個模型，三次都答錯，
# 但每次錯得不一樣，這是「瞎猜」而不是持有特定錯誤觀念的形狀）。
for _wrong in ("MCP 的英文全名是 Master Control Panel。",
               "MCP是指微控制器平台（Microcontroller Platform）。",
               "MCP 是一個由阿里云提供的模型。"):
    _passed, _missing = a.grade(_resp(_wrong), _mcp)
    assert _passed is False, f"錯誤答案必須被擋下：{_wrong}"
    assert _missing, f"缺少的槽位不可為空：{_wrong}"

# 空輸出與欄位從缺不可被誤判為通過
assert a.grade(_resp(""), _mcp)[0] is False
assert a.grade(_resp("   "), _mcp)[0] is False
assert a.grade({}, _mcp)[0] is False
assert a.grade({"response": None}, _mcp)[0] is False

# 缺少的槽位要能直接印給人看 —— report() 直接印這個 list，
# 印出一堆 tuple 等於沒說缺少什麼
_, _missing = a.grade(_resp("不知道"), _mcp)
assert isinstance(_missing[0], str) and "model context protocol" in _missing[0]

# ── 問句必須與評分對齊（D-016 的另一半）──────────────────
# 「用三句話解釋 X 是什麼」是開放題，答對不必出現全名，與 expect_all
# 天生不相容 —— 改回去就會踩到同一個坑，症狀一模一樣。
for _q in a.QUESTIONS:
    assert "英文全名" in _q["prompt"], f"問句未與評分對齊：{_q['id']}"
    assert "解釋" not in _q["prompt"], f"開放題與 expect_all 不相容：{_q['id']}"

# 目標題與對照題必須**同一個形狀**。問法一旦不對稱，隔離「模型不可靠」
# 與「知識不存在」的能力就沒了 —— 而那是對照題唯一的用途。
_prompts = [q["prompt"] for q in a.QUESTIONS]
assert len({p.replace("MCP", "X").replace("HTTP", "X") for p in _prompts}) == 1, \
    "目標題與對照題的問法必須同一個形狀"
assert len({q["id"] for q in a.QUESTIONS}) == len(a.QUESTIONS), "題目 id 不可重複"
assert any(q["id"] == "http" for q in a.QUESTIONS), "對照題不可被移除"

print("✓ ask_probe.py 純函式測試全數通過")
