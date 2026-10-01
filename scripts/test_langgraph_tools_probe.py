#!/usr/bin/env python3
"""langgraph_tools_probe.py 的離線單元測試（不連網、不需容器、不需 langchain）。

這組測試對應兩條既有教訓：

1. **評分邏輯本身要被實測過**（HANDBOOK 第一階段清單、D-016）。一個只跑過
   「輸出為空」的評分器，在第一次真正跑到之前沒有任何防線 —— 它會在最需要
   它的那一刻才第一次運作，而那正是最不該發現它壞掉的時候。
2. **假失敗比漏報更糟**（D-016）。所以這裡**兩邊都驗**：「該過的過」與
   「該擋的擋」。只驗其中一邊的評分器，會在另一邊默默壞掉。

`extract()` 只讀屬性、不檢查型別（duck typing），所以下面用 `_Msg` 當替身，
不需要安裝 langchain。

執行：python3 scripts/test_langgraph_tools_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import langgraph_tools_probe as p  # noqa: E402


class _Msg:
    """LangChain 訊息的替身，只帶 extract() 會讀的三個屬性。"""

    def __init__(self, content=None, tool_calls=None, tool_call_id=None):
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def _ai(text=None, calls=()):
    """助理訊息。calls 是 (name, args, call_id) 的序列。"""
    return _Msg(
        content=text,
        tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in calls] or None,
    )


def _tool(call_id, text):
    return _Msg(content=text, tool_call_id=call_id)


def _turns_pass():
    """真實通過的形狀：兩輪、三次真實呼叫、加總正確。

    第二輪的兩個呼叫放在**同一則** AIMessage 裡 —— 這是 D-023 實測到的形狀
    （Open WebUI 那一輪也是同一次回應發出三個呼叫）。
    """
    return [
        [
            _ai(None, [("roll_die", {}, "c1")]),
            _tool("c1", "5"),
            _ai("你丢的這顆骰子得到的數字是 5。"),
        ],
        [
            _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
            _tool("c2", "5"),
            _tool("c3", "6"),
            _ai("第一顆是 5，第二顆是 6，兩顆加起來一共是 11。"),
        ],
    ]


# ── result_text：content 的形狀不只一種 ─────────────────────
# 不正規化的話，「工具明明回傳了 5」會被判成沒有回傳。
assert p.result_text("5") == "5"
assert p.result_text(["5"]) == "5"
assert p.result_text([{"type": "text", "text": "5"}]) == "5"
assert p.result_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
assert p.result_text(None) == ""
assert p.result_text("") == ""
# 非文字區塊（圖片等）沒有可讀內容 —— 不可讓它變成 repr 字串而看起來「有回傳」
assert p.result_text([{"type": "image", "data": "x" * 50}]) == ""

# ── roll_values：只認「整段就是一個整數」 ───────────────────
assert p.roll_values(["5", " 6 "]) == [5, 6]
assert p.roll_values(["\n5\n"]) == [5]
assert p.roll_values(["5", "boom"]) == [5, None]
# 小數、多個數字、空字串都不是點數 —— 回 None 由 grade 記成問題，不靜默略過
assert p.roll_values(["5.5"]) == [None]
assert p.roll_values(["5 和 6"]) == [None]
assert p.roll_values([""]) == [None]
assert p.roll_values([None]) == [None]

# ── 該過的過 ────────────────────────────────────────────────
_ev = p.extract(_turns_pass())

# 這一條是「評分器本身被實測過」的一半：拿一個**真的有呼叫**的案例餵它。
# 少了這條，下面每一條「該擋的擋」都可能在一個永遠判不通過的評分器上空轉。
assert _ev["turns"][1]["calls"], "通過案例必須真的有呼叫"
assert _ev["turns"][1]["final"].strip(), "通過案例必須真的有回答"
assert p.grade(_ev) == (True, []), p.grade(_ev)[1]
assert not any("**沒有**" in n for n in p.observations(_ev))

# 工具名稱帶前綴也要認得 —— 實測 Open WebUI 送出的名字是 `mcp-test_roll_die`，
# 硬比對字串會讓探針在命名慣例改變時誤報失敗
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("mcp-test_roll_die", {}, "c2"), ("mcp-test_roll_die", {}, "c3")]),
        _tool("c2", "2"),
        _tool("c3", "3"),
        _ai("兩顆加起來是 5。"),
    ],
])
assert p.grade(_ev) == (True, []), p.grade(_ev)[1]

# 兩個呼叫分散在**兩次**往返（真正的 sequential），也要算多輪
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2")]),
        _tool("c2", "1"),
        _ai(None, [("roll_die", {}, "c3")]),
        _tool("c3", "2"),
        _ai("兩顆加起來是 3。"),
    ],
])
assert p.grade(_ev) == (True, []), p.grade(_ev)[1]

# 只講總和、不複述兩顆骰子，**仍然是對的** —— 不可判成失敗（D-016 的假失敗）。
# 但要被 observations 標出來給人看：這是「注意」不是「失敗」。
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "3"),
        _tool("c3", "4"),
        _ai("兩顆加起來是 7。"),
    ],
])
assert p.grade(_ev) == (True, []), p.grade(_ev)[1]
assert any("**沒有**" in n for n in p.observations(_ev))

# 有 tool_calls 的助理訊息即使帶文字，也**不是**最終答案
_ev = p.extract([
    [_ai("我先看看。", [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
])
assert _ev["turns"][0]["final"] == "得到 5。"

# 工具回傳也**不是**最終答案。這條是離線測試實際抓到的缺陷，不是假想：
# 少了它，「呼叫了工具卻什麼都沒說」會被最後一筆工具回傳的「6」填成一個
# 看起來正常的答案，於是空回答檢查永遠不會觸發 —— 那是漏報。
_ev = p.extract([[_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "6")]])
assert _ev["turns"][0]["final"] == "", repr(_ev["turns"][0]["final"])
# 但那個回傳仍然要在證據裡（它證明工具真的被呼叫了）
assert _ev["turns"][0]["results"] == {"c1": "6"}

# ── 該擋的擋 ────────────────────────────────────────────────

# D-023 失敗 B 的形狀：完全沒有呼叫，卻報出一個點數
_ev = p.extract([[_ai("你丟的骰子得到了6點。")]])
_passed, _problems = p.grade(_ev)
assert _passed is False, "沒有呼叫工具卻報數字，必須被擋下"
assert any("沒有自己決定使用工具" in x for x in _problems)

# 第一輪有呼叫、第二輪沒有 —— 一樣是沒有自主呼叫
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "3"), _ai("得到 3。")],
    [_ai("兩顆加起來是 9。")],
])
_passed, _problems = p.grade(_ev)
assert _passed is False
assert any("至少 2 次" in x for x in _problems)

# 孤兒呼叫：發出了 c3 但結果沒回來。這正是 call_id 鏈要抓的東西。
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "5"),
        _ai("兩顆加起來是 11。"),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False
assert any("沒有對應的工具回傳" in x and "c3" in x for x in _problems)

# 反向：有回傳卻沒有對應的呼叫 —— 證據本身不一致
_ev = p.extract([
    [
        _ai(None, [("roll_die", {}, "c1")]),
        _tool("c1", "5"),
        _tool("c9", "3"),
        _ai("共 8。"),
    ]
])
_passed, _problems = p.grade(_ev)
assert _passed is False
assert any("找不到對應的呼叫" in x for x in _problems)

# 模型掰了一個錯的加總（真實是 5+6=11，它說 9）
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "5"),
        _tool("c3", "6"),
        _ai("兩顆加起來一共是 9。"),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False, "加總錯誤必須被擋下"
assert any("加總" in x for x in _problems)

# 加總的比對不可用子字串：答案是「1」而正確是「11」時，
# 用 `in` 比對會因為 "1" in "11" 而誤判成命中
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "5"),
        _tool("c3", "6"),
        _ai("兩顆加起來是 1。"),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False, "『1』不可因為是『11』的子字串而被當成命中"

# 子字串陷阱的另一個方向，也是突變測試抓到的缺口：正確加總是 5，
# 模型說「15」。用 `str(expected) in final` 會因為 "5" in "15" 而放行 ——
# 這是**漏報**，比誤報更難發現，因為它安靜地讓壞掉的結果看起來是好的。
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "2"),
        _tool("c3", "3"),
        _ai("兩顆加起來是 15。"),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False, "『15』不可因為含『5』而被當成正確的加總"

# 空回答（模型呼叫了工具卻什麼都沒說）
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "5"),
        _tool("c3", "6"),
        _ai(""),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False
assert any("空回答" in x for x in _problems)

# 工具回傳了無法解析的東西 —— 不可靜默略過
_ev = p.extract([
    [_ai(None, [("roll_die", {}, "c1")]), _tool("c1", "5"), _ai("得到 5。")],
    [
        _ai(None, [("roll_die", {}, "c2"), ("roll_die", {}, "c3")]),
        _tool("c2", "error: tool failed"),
        _tool("c3", "6"),
        _ai("兩顆加起來是 11。"),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False
assert any("無法解析成整數" in x for x in _problems)

# 別的工具（echo）冒充骰子：有呼叫、有回傳、加總也對，但它不是 roll_die
_ev = p.extract([
    [_ai(None, [("mcp-test_echo", {"text": "hi"}, "c1")]), _tool("c1", "hi"), _ai("你說 hi。")],
    [
        _ai(None, [("mcp-test_echo", {}, "c2"), ("mcp-test_echo", {}, "c3")]),
        _tool("c2", "5"),
        _tool("c3", "6"),
        _ai("兩顆加起來是 11。"),
    ],
])
_passed, _problems = p.grade(_ev)
assert _passed is False, "echo 不算丟骰子"
assert any("沒有自己決定使用工具" in x for x in _problems)

# 完全沒有證據
assert p.grade({"turns": []})[0] is False
assert p.grade({})[0] is False

# 問題描述必須是可直讀的字串（report 直接印這個 list，
# 印出一堆 tuple 等於沒說哪裡沒過）
for _bad in ([_ai("6")],):  # 沒有呼叫
    _, _probs = p.grade(p.extract([_bad]))
    assert _probs
    for _one in _probs:
        assert isinstance(_one, str) and _one.strip()

# observations 對空證據不可爆炸
assert p.observations({"turns": []}) == []
assert isinstance(p.observations(p.extract(_turns_pass())), list)

print("✓ langgraph_tools_probe.py 純函式測試全數通過")
