#!/usr/bin/env python3
"""verify_api.py 的離線單元測試（不連網、不需容器）。

只驗證不涉及 HTTP 的純函式 —— 解析、速率計算、thinking 判定、防呆與邊界條件。
這些是最容易在真實環境中出錯、卻最難從輸出看出來的部分。

其中三項是 2026-09-18 首次實跑後補上的迴歸測試，對應當時真實發生的誤判：
  • thinking 內容在獨立欄位 → _thinking_text
  • 空輸出必須明講「（空）」→ _show
  • 多輪被 num_predict 截斷 → 由呼叫端的額度調整處理，此處僅鎖住解析行為

執行：python3 scripts/test_verify_api.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_api as v  # noqa: E402

# ── _thinking_text：思考內容在獨立欄位，不在 response ─────
assert v._thinking_text({"thinking": "想很久"}) == "想很久"
assert v._thinking_text({"thinking": None}) == ""
assert v._thinking_text({}) == ""
assert v._thinking_text({"response": "答案"}) == ""

# ── _show：空輸出必須明講，不可讓空白冒充成功 ─────────────
assert v._show("") == "（空）"
assert v._show("   ") == "（空）"
assert v._show(None) == "（空）"
assert v._show("\n\n") == "（空）"
assert v._show("正常") == "正常"
assert v._show("a" * 300).endswith("…")

# ── _thinking_state：以 thinking 欄位為準 ────────────────
assert "有" in v._thinking_state({"thinking": "想很久"})
assert "無" in v._thinking_state({"thinking": "", "done_reason": "stop"})
assert "可能" in v._thinking_state({"thinking": "", "done_reason": "length"})
# thinking 欄位有內容時，優先於 done_reason
assert "有" in v._thinking_state({"thinking": "x", "done_reason": "length"})
# 欄位從缺或為 null 時不得拋例外
assert "無" in v._thinking_state({})
assert "無" in v._thinking_state({"thinking": None, "done_reason": "stop"})

# ── _chatml：Qwen3 關閉 thinking 的預填格式 ──────────────
plain = v._chatml("說：正常")
assert plain.endswith("<|im_start|>assistant\n" + v.THINK_BLOCK_CLOSED)
assert "<|im_start|>user\n說：正常<|im_end|>" in plain
assert "<|im_start|>system" not in plain
# 鎖住結構：前後各兩個換行，中間是關閉標記
assert v.THINK_BLOCK_CLOSED.count("\n") == 4
assert v.THINK_BLOCK_CLOSED.startswith("<" + "think" + ">")
assert v.THINK_BLOCK_CLOSED.endswith("</" + "think" + ">" + "\n\n")
with_system = v._chatml("問題", system="系統提示")
assert with_system.startswith("<|im_start|>system\n系統提示<|im_end|>\n")
assert "<|im_start|>user\n問題<|im_end|>" in with_system

# ── _clip：截斷與 None 防護 ────────────────────────────
assert v._clip("a" * 300).endswith("…")
assert len(v._clip("a" * 300)) == 181
assert v._clip("abc") == "abc"
assert v._clip(None) == ""
assert v._clip("有\n換行") == "有 換行"

# ── _speed：速率計算與零除保護 ──────────────────────────
assert v._speed({"eval_count": 100, "eval_duration": 50_000_000_000}) == (100, 50.0, 2.0)
assert v._speed({}) == (0, 0.0, None)
# eval_count 有值但時間為 0 → 不可回傳速率，避免 ZeroDivisionError
assert v._speed({"eval_count": 5, "eval_duration": 0}) == (5, 0.0, None)
assert v._speed({"eval_count": None, "eval_duration": None}) == (0, 0.0, None)

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

# ── _answer_compliant：答案本身也要檢查，不能只看 thinking 欄位 ──
# 這是 2026-09-18 第二次實跑後補的迴歸測試。當時 think=false 清空了
# thinking 欄位，看似成功，實際上思考內容被搬進 response、長達 252 字
# 且沒有作答。只看欄位會把它誤判為有效。
assert v._answer_compliant("2", "2") is True
assert v._answer_compliant("  2  ", "2") is True
assert v._answer_compliant("2。", "2") is True
# 門檻是 8 字：夠寬容簡短的說明，但擋得住整段外流的思考
assert v._answer_compliant("答案是 2", "2") is True
assert len("答案是 2") <= 8
assert v._answer_compliant("嗯，用户问的是「1+1 等於多少？」…", "2") is False
# 沒有作答、答錯、或整個從缺，一律不合格
assert v._answer_compliant("", "2") is False
assert v._answer_compliant(None, "2") is False
assert v._answer_compliant("3", "2") is False
assert v._answer_compliant("2" * 20, "2") is False

# ── _suppress_chat / _suppress_generate：沿用機制 ────────
# 這兩個函式決定「測試 2 找到的關閉方式，如何套用到後續測試」。
# 錯在這裡不會有例外，只會安靜地讓後續測試失去效力，因此值得鎖住。
_CHAT_CASES = {"think_false": True, "raw": False, "no_think": False, None: False}
for state, should_set in _CHAT_CASES.items():
    v.SUPPRESSION = state
    payload = v._suppress_chat({})
    assert ("think" in payload) is should_set, f"chat 沿用錯誤：{state}"
    # chat API 不吃 raw 或 /no_think，不可污染 messages 之外的其他欄位
    assert set(payload) <= {"think"}

v.SUPPRESSION = "think_false"
assert v._suppress_generate({}, "問題")["think"] is False

v.SUPPRESSION = "no_think"
assert v._suppress_generate({}, "問題")["prompt"] == "問題 /no_think"

v.SUPPRESSION = "raw"
out = v._suppress_generate({"prompt": "問題", "system": "系統"}, "問題", system="系統")
assert out["raw"] is True
# raw 模式必須移除 system，否則 Ollama 會忽略 raw 提示詞
assert "system" not in out
assert "系統" in out["prompt"] and "問題" in out["prompt"]

v.SUPPRESSION = None
assert v._suppress_generate({}, "問題") == {}

# 復原模組狀態，避免影響後續斷言
v.SUPPRESSION = None

# ── TOOLS：結構必須符合 Ollama 的 tools 格式 ─────────────
tool = v.TOOLS[0]
assert tool["type"] == "function"
assert tool["function"]["name"] == "get_weather"
assert tool["function"]["parameters"]["required"] == ["city"]

print("✓ verify_api.py 純函式測試全數通過")
