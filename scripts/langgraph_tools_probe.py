#!/usr/bin/env python3
"""第三階段 item 1 的探針：工具呼叫跨不跨得過 code path 的改變？

HANDBOOK 第三階段第 1 項是個 [open]：D-011 證明的是 **Open WebUI 自己的**
tool calling 流程會過（D-023 用 call_id 鏈補上了機械證據），而 LangGraph 走的是
`bind_tools` + Ollama 整合的原生 function calling —— **完全不同的實作**。
D-014 的教訓就是：在某組條件下量到的結果，不要套用到沒量過的地方。

所以這支探針要重新量一次，而且量的是**同一件事**：

  第一輪  「幫我丟一顆骰子」              → 模型要**自己**決定呼叫 roll_die
  第二輪  「再丟兩顆骰子，然後告訴我      → 要發出**兩個** roll_die，
          這兩顆加起來是多少」              且最終答案要等於兩次**真實回傳**的加總

第二輪的問法刻意寫成「再丟**兩顆**」而不是「再丟一顆，然後告訴我兩顆加起來」。
後者是有歧義的：模型可以合理地只丟一顆、拿第一輪的點數相加 —— 那樣最後一輪
只有 1 次呼叫，會被下面的判準判成不通過，而模型的作答其實完全正確。
**問句必須與評分對齊，否則量到的是「模型沒照我的意思做」，不是「工具呼叫壞了」**
（D-016：假失敗比漏報更糟，因為它會讓人去修一個沒壞的東西）。

### 評分靠什麼（D-023 的教訓）

不靠「答案讀起來對」。靠兩件事：

1. **call_id 鏈** —— 模型發出的每個 tool_call 都有一個 id，結果會以**同一個 id**
   回來。有任何一個呼叫沒有對應的回傳，就是「發了沒回來」，記為問題。
2. **數字要能追溯到真實回傳** —— 最終答案裡的加總必須等於**工具實際回傳的點數**
   相加，而不是等於模型自己講的數字。

D-023 第六節的失敗 B 就是反面教材：模型在**完全沒有 function_call** 的情況下
回答了「6」。單看答案完全看不出它沒丟骰子。

### 為什麼評分邏輯要獨立成純函式

本專案慣例（scripts/test_ask_probe.py、D-016）：**評分邏輯本身要被實測過**。
一個只跑過「輸出為空」的評分器，在第一次真正跑到之前沒有任何防線。
因此 `extract()` 與 `grade()` 只讀屬性、不檢查型別（duck typing），
離線測試可以用替身物件餵它們，**不需要安裝 langchain**。

這也是為什麼 langchain 的 import 全部延後到 `run_probe()` 裡面 ——
模組頂層匯入會讓 `test_langgraph_tools_probe.py` 在沒有 langchain 的
環境下直接 import 失敗。

用法（通常由 scripts/verify-langgraph-tools.sh 呼叫）：
    python3 langgraph_tools_probe.py --model qwen2.5:3b \
        --ollama-url http://ollama:11434 \
        --mcp-url http://mcp-test-server:8000/mcp

結束碼：0 = 通過
        1 = 未通過（判定準則沒過，見輸出）
        2 = 無法判定（連不上、模型沒下載 —— 不是判準的問題）
        3 = 探針自己壞掉（D-018：這不是受測對象的問題）
"""

import argparse
import asyncio
import json
import re
import sys
import time

# ── 結束碼（D-018 的約定，全專案一致）──────────────────
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INDETERMINATE = 2
EXIT_BROKEN = 3

# 工具的辨識用「包含」而不是「等於」：工具經過 MCP 之後名字會被加上連線的
# id（實測 Open WebUI 送給 ollama 的是 `mcp-test_roll_die`），langchain 端
# 也可能再變一次。硬比對字串會讓探針在命名慣例改變時**誤報失敗** ——
# 那比漏報更糟（D-016）。
ROLL_DIE = "roll_die"

# 只認「整段就是一個整數」。mcp-test-server 的 roll_die 回傳形狀就是如此
# （見 scripts/verify-mcp-server.sh 的 tools/call 檢查）。
_INT_RE = re.compile(r"-?\d+")


# ══════════════════════════════════════════════════════════
# 純函式區 —— 這一段是 test_langgraph_tools_probe.py 的受測對象
# ══════════════════════════════════════════════════════════


def result_text(content):
    """把 ToolMessage.content 正規化成純文字。

    langchain 1.x 的 content 可能是 str、list[str]、list[dict]
    （例如 [{"type": "text", "text": "5"}]）或 None。不先正規化就比對，
    會在「工具明明回傳了 5」的情況下判成沒有回傳。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def calls_of(msg):
    """取出一則訊息發出的 tool calls，正規化成 dict。

    langchain 的 tool_calls 每項是 {"name", "args", "id", "type"}；
    只讀屬性、不驗型別，所以離線測試可以餵替身物件。
    """
    out = []
    for call in getattr(msg, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        out.append(
            {
                "name": call.get("name"),
                "args": call.get("args") or {},
                "call_id": call.get("id"),
            }
        )
    return out


def is_roll_call(call):
    """這是不是一次丟骰子的呼叫？用包含比對，理由見 ROLL_DIE 的註解。"""
    name = call.get("name")
    return bool(name) and ROLL_DIE in str(name)


def extract(turns):
    """把每一輪的訊息序列整理成正規化證據。

    turns 是「每一輪新增的訊息」的序列：[[msg, ...], [msg, ...]]。
    刻意不接收單一扁平清單 —— 判準需要知道「幾個呼叫發生在**同一輪**」，
    扁平清單會讓那個資訊消失。

    回傳（全部是純 dict，可 json 序列化）：
        {
          "turns": [
            {"calls": [...], "results": {call_id: 文字}, "final": 最後回答},
            ...
          ]
        }
    """
    out_turns = []
    for messages in turns:
        calls = []
        results = {}
        final = ""
        for msg in messages:
            calls.extend(calls_of(msg))

            call_id = getattr(msg, "tool_call_id", None)
            if call_id is not None:
                results[call_id] = result_text(getattr(msg, "content", None))

            # 只有**助理**訊息可以當最終答案。兩種要排除：
            #   1. 工具回傳（有 tool_call_id）—— 它的內容是點數，不是回答。
            #      這一條是離線測試實測抓到的缺陷：漏了它，「模型呼叫了工具
            #      卻什麼都沒說」會被最後一筆工具回傳的數字填成一個看起來
            #      正常的答案，空回答檢查於是真的永遠不會觸發（漏報）。
            #   2. 空字串 —— 模型先回空再接工具是常態，把它當最終答案會
            #      誤判成空回答（假失敗）。
            if getattr(msg, "tool_calls", None) or call_id is not None:
                continue
            content = result_text(getattr(msg, "content", None))
            if content.strip():
                final = content

        out_turns.append({"calls": calls, "results": results, "final": final})
    return {"turns": out_turns}


def roll_values(texts):
    """把 roll_die 的回傳文字轉成整數，逐項對應。

    無法解析的項目回 None（不是略過）—— 由 grade 記成問題。
    靜默略過會讓「回傳了非預期的東西」看起來像「一切正常」。
    """
    out = []
    for text in texts:
        stripped = (text or "").strip()
        out.append(int(stripped) if _INT_RE.fullmatch(stripped) else None)
    return out


def last_turn_rolls(evidence):
    """最後一輪的 roll_die 點數（list[int | None]）與其對應的原始文字。"""
    turns = evidence.get("turns") or []
    if not turns:
        return [], []
    calls = [c for c in turns[-1]["calls"] if is_roll_call(c)]
    texts = [
        turns[-1]["results"].get(c["call_id"]) for c in calls
    ]
    return roll_values(texts), texts


def grade(evidence, min_rolls_last_turn=2):
    """純函式：證據 → (passed, problems)。

    problems 每一項都是可以直接印給人看的字串 —— 講「哪裡沒過」，
    不是只說「沒過」。
    """
    problems = []
    turns = evidence.get("turns") or []

    if not turns:
        return False, ["沒有收到任何一輪的訊息 —— 探針沒有跑到判準那一步"]

    # ── C2：每個 tool_call 都要有同一個 call_id 的回傳 ──────
    # 這是 D-023 的核心判準。少了它，分不出「真的呼叫了工具」和
    # 「發了呼叫但結果沒回來，模型自己掰一個數字」。
    for idx, turn in enumerate(turns, 1):
        for call in turn["calls"]:
            if call["call_id"] not in turn["results"]:
                problems.append(
                    "第 %d 輪的 %s 呼叫（call_id=%s）沒有對應的工具回傳"
                    % (idx, call["name"], call["call_id"])
                )
        # 反向也要檢查：有結果卻沒有對應的呼叫，代表證據本身對不起來
        known = {c["call_id"] for c in turn["calls"]}
        for call_id in turn["results"]:
            if call_id not in known:
                problems.append(
                    "第 %d 輪有一筆工具回傳（call_id=%s）找不到對應的呼叫 —— "
                    "證據本身不一致" % (idx, call_id)
                )

    # ── C1：模型要自己決定呼叫（不是被指示的）────────────────
    all_calls = [c for turn in turns for c in turn["calls"]]
    if not any(is_roll_call(c) for c in all_calls):
        problems.append(
            "整個過程沒有任何 %s 呼叫 —— 模型沒有自己決定使用工具"
            "（這一輪的工具呼叫數：%d）" % (ROLL_DIE, len(all_calls))
        )

    # ── C3：最後一輪要有連續兩次以上的呼叫 ──────────────────
    last_rolls, last_texts = last_turn_rolls(evidence)
    if len(last_rolls) < min_rolls_last_turn:
        problems.append(
            "最後一輪只發出 %d 次 %s 呼叫，需要至少 %d 次才算多輪"
            % (len(last_rolls), ROLL_DIE, min_rolls_last_turn)
        )

    # ── 回傳值必須是可解析的點數 ───────────────────────────
    for value, text in zip(last_rolls, last_texts):
        if value is None:
            problems.append(
                "最後一輪有 %s 回傳了無法解析成整數的內容：%r" % (ROLL_DIE, text)
            )

    # ── C4：加總必須等於真實回傳的加總 ─────────────────────
    final = (turns[-1].get("final") or "").strip()
    if not final:
        problems.append("最後一輪沒有產生任何文字回答（空回答）")
    elif all(v is not None for v in last_rolls) and len(last_rolls) >= min_rolls_last_turn:
        expected = sum(last_rolls)
        numbers = {int(m) for m in _INT_RE.findall(final)}
        if expected not in numbers:
            problems.append(
                "最終答案裡的數字 %s 不包含兩次真實回傳的加總 %d"
                "（真實回傳：%s）"
                % (sorted(numbers), expected, "+".join(str(v) for v in last_rolls))
            )

    return (not problems), problems


def observations(evidence):
    """非致命觀察 —— 印給人看，不影響通過與否。

    刻意與 grade() 分開：模型只說「共 11」而不複述兩顆骰子，仍然是對的。
    把這種事算成失敗就是 D-016 那種「假失敗」。
    """
    turns = evidence.get("turns") or []
    notes = []
    if not turns:
        return notes
    last_rolls, _ = last_turn_rolls(evidence)
    final = turns[-1].get("final") or ""
    numbers = {int(m) for m in _INT_RE.findall(final)}
    for i, value in enumerate(last_rolls, 1):
        if value is not None:
            notes.append(
                "第 %d 顆的點數 %d %s出現在最終答案裡"
                % (i, value, "" if value in numbers else "**沒有**")
            )
    notes.append("最終答案全文：%r" % final)
    return notes


# ══════════════════════════════════════════════════════════
# 連網區 —— 需要 langchain / langgraph / ollama / MCP server
#
# 這一區刻意**不在模組頂層 import** langchain：頂層匯入會讓
# test_langgraph_tools_probe.py 在沒有 langchain 的環境下直接失敗，
# 而評分邏輯必須能離線測試（D-016）。
# ══════════════════════════════════════════════════════════

# 兩輪的問法刻意**不提到任何工具名稱**，也不說「請用工具」——
# HANDBOOK 第二階段要驗的是「模型自行決定」，這裡驗的是同一件事換一條路徑
# 之後還成不成立。第一輪對齊 D-023 的驗證題，第二輪要求連續兩次呼叫。
TURN_PROMPTS = (
    "幫我丟一顆骰子",
    "再丟兩顆骰子，然後告訴我這兩顆加起來是多少",
)

# 這是**探針自己**的步數上限，不是第三階段 item 4 在談的那個 `recursion_limit`。
# 第二輪要 chatbot → tools → chatbot → tools → chatbot 共 5 個 super-step，
# 給 25 只是為了不要在探針裡先撞到牆，讓 item 4 的問題混進來。
PROBE_RECURSION_LIMIT = 25


def build_graph(model_with_tools, tools):
    """組出最小的 LangGraph agent：chatbot ↔ tools。

    刻意用手寫的 StateGraph 而不是 `create_agent`：這兩者都走
    `bind_tools` + 原生 function calling（也就是 item 1 要驗的那條路），
    但手寫的版本讓「圖長什麼樣」直接寫在原始碼裡，不必跟著 prebuilt 的
    API 搬家 —— `create_react_agent` 就在 langgraph 與 langchain.agents
    之間搬過一次（本探針的探測階段已實測 `langgraph.prebuilt.create_react_agent`
    還在，但沒有理由去賭它會一直在）。
    """
    from langgraph.graph import START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    async def chatbot(state):
        reply = await model_with_tools.ainvoke(state["messages"])
        return {"messages": [reply]}

    graph = StateGraph(MessagesState)
    graph.add_node("chatbot", chatbot)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "chatbot")
    graph.add_conditional_edges("chatbot", tools_condition)
    graph.add_edge("tools", "chatbot")
    return graph.compile()


async def run_probe(args):
    """跑兩輪，回傳 (evidence, meta)。任何例外往上丟，由 main() 分類。"""
    from langchain.mcp import MCPAdapter
    from langchain_core.messages import HumanMessage
    from langchain_ollama import ChatOllama

    evidence = {"turns": []}
    meta = {"tools": [], "elapsed": []}

    async with MCPAdapter(args.mcp_url) as adapter:
        tools = await adapter.list_tools()
        meta["tools"] = [t.name for t in tools]
        print("從 MCP 取到 %d 個工具：%s" % (len(tools), meta["tools"]))
        if not tools:
            return evidence, meta, EXIT_INDETERMINATE
        print()

        model = ChatOllama(
            model=args.model, base_url=args.ollama_url, num_ctx=args.num_ctx
        )
        app = build_graph(model.bind_tools(tools), tools)

        history = []
        for idx, prompt in enumerate(TURN_PROMPTS, 1):
            history.append(HumanMessage(prompt))
            started = time.monotonic()
            result = await app.ainvoke(
                {"messages": history},
                config={"recursion_limit": PROBE_RECURSION_LIMIT},
            )
            elapsed = time.monotonic() - started

            # ainvoke 回傳的是「輸入 + 新增」，切出新產生的部分。
            # 用長度切而不是比對內容 —— 內容可能重複（同一句話問兩次）。
            new_messages = result["messages"][len(history):]
            history = result["messages"]

            turn = extract([new_messages])["turns"][0]
            evidence["turns"].append(turn)
            meta["elapsed"].append(elapsed)

            print("── 第 %d 輪（%.1f 秒）" % (idx, elapsed))
            print("   問：%s" % prompt)
            if not turn["calls"]:
                print("   模型沒有發出任何工具呼叫")
            for n, call in enumerate(turn["calls"], 1):
                returned = turn["results"].get(call["call_id"], "<沒有回傳>")
                print(
                    "   呼叫 %d：%s%s  call_id=%s"
                    % (n, call["name"], call["args"], call["call_id"])
                )
                print("        → 回傳 %r" % returned)
            print("   答：%r" % turn["final"])
            print()

    return evidence, meta, None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="第三階段 item 1 的探針")
    parser.add_argument("--model", default="qwen2.5:3b")
    parser.add_argument("--ollama-url", default="http://ollama:11434")
    parser.add_argument("--mcp-url", default="http://mcp-test-server:8000/mcp")
    parser.add_argument(
        "--num-ctx", type=int, default=4096, help="對齊 D-022 量到的預設值"
    )
    parser.add_argument("--json", action="store_true", help="額外印出正規化證據")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    print("模型：%s" % args.model)
    print("ollama：%s" % args.ollama_url)
    print("MCP：%s" % args.mcp_url)
    print("num_ctx：%d" % args.num_ctx)
    print()

    try:
        evidence, meta, early = asyncio.run(run_probe(args))
    except ImportError as e:
        print("探針自己壞掉：匯入失敗（%s: %s）" % (type(e).__name__, e))
        print("  相依版本見 scripts/requirements-langgraph.txt —— 不要放寬，先確認是哪一個套件變了。")
        return EXIT_BROKEN
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        text = str(e)
        # 連不上、模型不存在 → 無法判定（不是判準沒過）。其餘一律當成探針壞掉。
        markers = ("ConnectError", "ConnectionError", "ConnectTimeout", "not found",
                   "Connection refused", "All connection attempts failed")
        if any(m in name or m in text for m in markers):
            print("無法判定：%s: %s" % (name, text))
            return EXIT_INDETERMINATE
        print("探針自己壞掉：%s: %s" % (name, text))
        return EXIT_BROKEN

    if early is not None:
        print("無法判定：MCP server 沒有回報任何工具 —— 這是環境問題，不是判準的問題。")
        return early

    if args.json:
        print("── 正規化證據 ──────────────────────────────")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        print()

    passed, problems = grade(evidence)

    print("── 判準 ────────────────────────────────────")
    if passed:
        print("通過：模型自主呼叫了工具，且有連續呼叫與正確加總。")
    else:
        print("未通過，問題如下：")
        for p in problems:
            print("  • %s" % p)
    print()
    print("── 觀察（不影響判定）───────────────────────")
    for note in observations(evidence):
        print("  • %s" % note)
    print()
    total = sum(meta["elapsed"])
    print("工具：%s" % meta["tools"])
    print("總耗時：%.1f 秒（各輪 %s）"
          % (total, ", ".join("%.1f" % e for e in meta["elapsed"])))

    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
