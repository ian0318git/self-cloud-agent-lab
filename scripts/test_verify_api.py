#!/usr/bin/env python3
"""verify_api.py 的離線單元測試（不連網、不需容器）。

只驗證不涉及 HTTP 的純函式 —— 解析、速率計算、thinking 判定、防呆與邊界條件。
這些是最容易在真實環境中出錯、卻最難從輸出看出來的部分。

其中三項是 2026-09-18 首次實跑後補上的迴歸測試，對應當時真實發生的誤判：
  • thinking 內容在獨立欄位 → _thinking_text
  • 空輸出必須明講「（空）」→ _show
  • 多輪被 num_predict 截斷 → 由呼叫端的額度調整處理，此處僅鎖住解析行為

另有一組是 2026-09-19 補上的 D-016 迴歸測試。那次出錯的不是模型，是**檢查
自己**：對照題答對了卻被判成答錯，工具據此印出「模型整體不可靠，這才是換
模型的訊號」。鎖住的是「問句必須與評分對齊」與「無法判定不得併入未通過」。

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

# ── _guard：例外必須被吞掉並回傳 FAILED，不得往外炸 ──────
# 註：這兩行會印出 ✗ 訊息，那是 _guard 的預期行為，不是測試失敗。
#
# 回傳 FAILED 而非 None 是關鍵：測試正常執行也可能回傳 None（代表無法
# 判定）。2026-09-18 測試 5 收到 HTTP 500，就是因為兩者共用 None，
# 總結才把它顯示成「無法判定（輸出為空）」—— 原因完全錯誤。
assert v._guard(lambda: 1 / 0) is v.FAILED
assert v._guard(lambda x: x * 2, 21) == 42
assert v._guard(lambda: (_ for _ in ()).throw(ValueError("boom"))) is v.FAILED
# FAILED 是物件，bool() 為真 —— 這是它最危險的地方
assert bool(v.FAILED) is True
assert v.FAILED is not None

# ── SIMPLIFIED_HINTS：不可誤收繁體亦合法的字 ─────────────
for ch in "后里台面只干云准":
    assert ch not in v.SIMPLIFIED_HINTS, f"誤收繁體合法字：{ch}"
for ch in "说这个请时间对开关们会":
    assert ch in v.SIMPLIFIED_HINTS, f"漏收簡化字：{ch}"

# 2026-09-18 擴充：原本漏收這些字，導致 1.7b 的實際輸出
# 「台北目前的天气是多云，气温28摄氏度。」完全沒被偵測到。
for ch in "气电摄东乐爱头医与为义风飞马鸟鱼点无专业师报场银铁图团园农华":
    assert ch in v.SIMPLIFIED_HINTS, f"漏收簡化字：{ch}"
# 這句是真實的漏判案例，必須被擋下來
_l1 = "台北目前的天气是多云，气温28摄氏度。"
assert set(_l1) & v.SIMPLIFIED_HINTS, "1.7b 的簡體輸出必須被偵測到"
# 對照：4b 的繁體輸出必須乾淨
_l4 = "台北現在天氣多雲，溫度28度。"
assert not (set(_l4) & v.SIMPLIFIED_HINTS), "4b 的繁體輸出不可誤判"

# ── AMBIGUOUS_HINTS：這幾個字只能警告，不能逕判 ──────────
for ch in "后里台面只干云准":
    assert ch in v.AMBIGUOUS_HINTS, f"漏收易混淆字：{ch}"
assert not (v.AMBIGUOUS_HINTS & v.SIMPLIFIED_HINTS), "兩個集合不可重疊"
# 「多云」的云只能警告；「天气」的气則可逕判 —— 兩者必須分開
assert "云" not in v.SIMPLIFIED_HINTS and "云" in v.AMBIGUOUS_HINTS
assert "气" in v.SIMPLIFIED_HINTS

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

# ── D-016：問句必須與評分對齊 ────────────────────────────
# 2026-09-19 實測抓到這套檢查自己的假失敗。對照題「用一句話說明什麼是
# HTTP」得到的回答是「HTTP 是一組 rules 和約定，用於網際網路傳輸超連結
# 檔案。」—— 完全正確，卻只因沒拼出縮寫全名而被判錯，工具於是印出
# 「模型整體不可靠，這才是換模型的訊號」：**根據一個假失敗叫人換模型。**
#
# 修法是讓問句與評分對齊（直接問全名）。以下斷言把這個對齊鎖住：
# 「用一句話說明 X 是什麼」是開放題，答對不必出現全名，與 expect_all
# 天生不相容 —— 改回去就會踩到同一個坑，而且症狀會一模一樣。
for _q in v.FACTUAL_QUESTIONS:
    assert "英文全名" in _q["prompt"], f"問句未與評分對齊：{_q['id']}"
    assert "用一句話說明" not in _q["prompt"], f"開放題與 expect_all 不相容：{_q['id']}"

# 目標題與對照題必須**同一個形狀**。問法一旦不對稱，隔離「模型不可靠」
# 與「知識不存在」的能力就沒了 —— 而那是整組對照題唯一的用途。
_prompts = [q["prompt"] for q in v.FACTUAL_QUESTIONS]
assert len(set(_prompts)) == len(_prompts), "問句不可重複"
assert len({p.replace("MCP", "X").replace("HTTP", "X") for p in _prompts}) == 1, \
    "目標題與對照題的問法必須同一個形狀"

# 對照題必須恰好一題：control_ok 取的是 controls[0]，多一題會安靜地
# 只檢查第一題，第二題形同裝飾。
_controls = [q for q in v.FACTUAL_QUESTIONS if q["control"]]
assert len(_controls) == 1, f"對照題必須恰好一題，目前 {len(_controls)} 題"
assert len(v.FACTUAL_QUESTIONS) >= 2, "至少要有目標題與對照題各一"

# ── _missing_slots：每個槽位是「任一替代字串出現即可」 ────
# 這是假失敗的防線：模型用中文答對時，不該因為沒寫英文全名而被判錯。
_slots = (("model context protocol", "模型上下文協定"),)
assert v._missing_slots("The answer is Model Context Protocol.", _slots) == []
assert v._missing_slots("答案是模型上下文協定。", _slots) == []
assert v._missing_slots("MODEL CONTEXT PROTOCOL", _slots) == []  # 大小寫不敏感
assert v._missing_slots("MCP 是 Master Control Panel。", _slots) == list(_slots)
# 空輸出與 None 不可被誤判為通過
assert v._missing_slots("", _slots) == list(_slots)
assert v._missing_slots(None, _slots) == list(_slots)

# 真實案例：修正後的問句下，qwen2.5:3b 的正確答案必須通過
_http_slot = next(q["expect_all"] for q in v.FACTUAL_QUESTIONS if q["id"] == "http")
assert v._missing_slots("HTTP 的英文全名是 HyperText Transfer Protocol。",
                        _http_slot) == []
# 而它的真實錯誤答案必須被擋下（真陽性不可因為放寬而被吃掉）
_mcp_slot = next(q["expect_all"] for q in v.FACTUAL_QUESTIONS if q["id"] == "mcp")
for _wrong in ("MCP 的英文全名是 Master Control Panel。",
               "MCP是指微控制器平台（Microcontroller Platform）。"):
    assert v._missing_slots(_wrong, _mcp_slot) == list(_mcp_slot), _wrong


# ── _factual_verdict：四種組合的判讀必須分得開 ─────────────
def _f(target, control):
    return {
        "by_id": {
            "mcp": {"factual": target, "control": False},
            "http": {"factual": control, "control": True},
        },
        "control_ok": control,
    }


# 對照題正確而目標題錯誤 → 知識不存在，這是 D-014 的預期結果，不是缺陷
assert "知識不存在" in v._factual_verdict(_f(False, True))
# 兩題皆錯 → 模型整體不可靠，這才是「該換模型」的訊號
assert "模型整體不可靠" in v._factual_verdict(_f(False, False))
# 兩題皆對 → 通過
assert "通過" in v._factual_verdict(_f(True, True))
# 對照題錯而目標題對 → 少見，必須要求人工複核而非自行判定
assert "人工複核" in v._factual_verdict(_f(True, False))
# 「無法判定」不可與任一者混為一談 —— D-016 的另一半
assert "無法判定" in v._factual_verdict(_f(None, None))
assert "無法判定" in v._factual_verdict(_f(None, True))
assert "無法判定" in v._factual_verdict(_f(True, None))
# 目標題無法判定時，即使對照題通過也不可宣稱「通過」
assert "通過" not in v._factual_verdict(_f(None, True))

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
