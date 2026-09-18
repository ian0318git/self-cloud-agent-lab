#!/usr/bin/env python3
"""verify_api.py 的離線單元測試（不連網、不需容器）。

只驗證不涉及 HTTP 的純函式 —— 解析、速率計算、thinking 判定、防呆與邊界條件。
這些是最容易在真實環境中出錯、卻最難從輸出看出來的部分。

執行：python3 scripts/test_verify_api.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_api as v  # noqa: E402

# ── _answer_only：必須正確剝除 thinking 段落 ────────────
assert v._answer_only("思考中</think>\n\n正常") == "正常"
assert v._answer_only("正常") == "正常"
assert v._answer_only("") == ""
# 模型可能回傳 content: null（多輪 tool calling 時的常見情形）
assert v._answer_only(None) == ""
# 只有一個 </think> 時，取第一個之後的全部
assert v._answer_only("a</think>b</think>c") == "b</think>c"

# ── _speed：速率計算與零除保護 ──────────────────────────
assert v._speed({"eval_count": 100, "eval_duration": 50_000_000_000}) == (100, 50.0, 2.0)
assert v._speed({}) == (0, 0.0, None)
# eval_count 有值但時間為 0 → 不可回傳速率，避免 ZeroDivisionError
assert v._speed({"eval_count": 5, "eval_duration": 0}) == (5, 0.0, None)
assert v._speed({"eval_count": None, "eval_duration": None}) == (0, 0.0, None)

# ── _thinking_state：三種狀態必須可區分 ─────────────────
assert "有" in v._thinking_state({"response": "x</think>y", "done_reason": "stop"})
assert "可能" in v._thinking_state({"response": "x", "done_reason": "length"})
assert "未觀察" in v._thinking_state({"response": "正常", "done_reason": "stop"})
# </think> 優先於 done_reason
assert "有" in v._thinking_state({"response": "x</think>", "done_reason": "length"})
# response 為 null 或欄位從缺時，不得拋例外
assert "未觀察" in v._thinking_state({"response": None, "done_reason": "stop"})
assert "未觀察" in v._thinking_state({})
assert "可能" in v._thinking_state({"response": None, "done_reason": "length"})

# ── _clip：截斷與 None 防護 ────────────────────────────
assert v._clip("a" * 300).endswith("…")
assert len(v._clip("a" * 300)) == 181
assert v._clip("abc") == "abc"
assert v._clip(None) == ""
assert v._clip("有\n換行") == "有 換行"

# ── _guard：例外必須被吞掉並回傳 None，不得往外炸 ────────
# 註：這兩行會印出 ✗ 訊息，那是 _guard 的預期行為，不是測試失敗。
assert v._guard(lambda: 1 / 0) is None
assert v._guard(lambda x: x * 2, 21) == 42
assert v._guard(lambda: (_ for _ in ()).throw(ValueError("boom"))) is None

# ── SIMPLIFIED_HINTS：不可誤收繁體亦合法的字 ─────────────
for ch in "后里台面只干云准":
    assert ch not in v.SIMPLIFIED_HINTS, f"誤收繁體合法字：{ch}"
for ch in "说这个请时间对开关们会":
    assert ch in v.SIMPLIFIED_HINTS, f"漏收簡化字：{ch}"

# ── TOOLS：結構必須符合 Ollama 的 tools 格式 ─────────────
tool = v.TOOLS[0]
assert tool["type"] == "function"
assert tool["function"]["name"] == "get_weather"
assert tool["function"]["parameters"]["required"] == ["city"]

print("✓ verify_api.py 純函式測試全數通過")
