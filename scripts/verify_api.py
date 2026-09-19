#!/usr/bin/env python3
"""第二階段前置驗證：直接對 Ollama API 實測，不經過 Open WebUI。

由 scripts/verify.sh 複製進 open-webui 容器後執行。之所以要繞這一段，是因為
docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠（見 DECISIONS.md D-003），
主機端連不到 Ollama API；但容器之間可透過 ai-net 互通，因此借用已在執行的
open-webui 容器（內建 Python）來發請求。

只用 Python 標準函式庫，容器內不需要額外安裝任何套件。

要回答的五個問題：
  1. 4b 在 2-core CPU 上的實際生成速度（tok/s）
  2. 4b 能否產生 tool calls，並在多輪之後收斂成答案（見 D-005）
  3. 能否關閉 thinking 模式 —— 它會吃掉大量 token 與時間
  4. 4b 的答案**事實是否正確**，並以對照題隔離「模型不可靠」與「知識不存在」（D-014）
  5. 能否用系統提示詞強制繁體中文輸出

v2 修正（源自 2026-09-18 首次實跑的三個發現）：
  • thinking 模型的思考內容位於獨立的 thinking 欄位，不在 response。
    v1 只讀 response，造成「明明有產出卻顯示空白」。
  • 空輸出必須明講「（空）」。v1 讓繁體中文檢測在空字串上誤判為通過 ——
    這是典型的靜默失敗。
  • num_predict 上限過小，模型還在思考就被截斷，多輪測試因此假性失敗。

v3 修正（同一次實跑，但屬於判準本身的缺陷）：
  • 測試 2 原本以「token 數是否提早收斂」判斷 thinking 是否被關閉，這是錯的
    判準 —— 模型即使不思考，也可能對模糊的提示詞長篇回覆。
  • 改以 thinking 欄位為判準，並加入基準對照組。
  • 找到可用的關閉方式後，測試 3/4/5 沿用（SUPPRESSION）。

v4 修正（2026-09-18 第二次實跑，推翻了 v3 的結論）：
  • 「thinking 欄位被清空」不等於「thinking 被關閉」。實測 think=false 的
    欄位確實是 0 字，但模型仍在思考，內容改從 response 流出 —— 252 字的
    內心獨白，沒有作答，比基準組（169 tokens、正確答案「2」）更慢更差。
    它關掉的是標籤，不是行為。判準因此必須一併檢查答案本身。
  • /no_think 與 raw 在 Ollama 0.34.2 皆回 HTTP 500，這兩條路已斷。
  • 測試 5 的「沒有簡化字」是假通過：輸出是外流的思考、內容是幻覺、且撞上
    上限中斷。現加入複述題目、done_reason=length 兩項檢查。
  • 部分執行（VERIFY_ONLY）時，未執行的測試不可顯示成「未通過」，verify.sh
    也不可宣稱「關鍵項目全數通過」。

結論：本環境沒有可用的 thinking 關閉方式，維持預設即可 —— Ollama 會把思考
隔離在 thinking 欄位，消費端只讀 response 就能拿到乾淨答案。

v5 修正（2026-09-19，套用 D-014 決策 1）：
  • 測試 5 原本問「什麼是 MCP」卻只檢查**形式** —— 回應非空、沒有洩漏提示、
    沒有被截斷。實測 qwen3:4b 回答「MCP 是阿里雲提供的模型」時，每一項形式
    檢查都通過。**驗證於是在答案是假的時候顯示通過。** 問題不是判準寫錯，
    是根本沒有判準存在 —— 形式檢查不會、也不可能發現內容是假的。
    現加入 expect_all 機械比對（見 FACTUAL_QUESTIONS）。
  • 同時加入**對照題**（HTTP）。只問 MCP 而答錯，無法區分「模型不可靠」與
    「訓練資料早於 MCP」，而這兩者的處置完全相反：前者要換模型，後者換同
    世代的模型也沒用，只能靠 RAG（D-014 決策 2）。
  • 事實判定與簡化字判定**分開**：用字正不正確不該汙染真偽判定。
  • 空 response 判為「無法判定」而非「答錯」—— 模型可能把 1024 個 token
    全用在 thinking 上，這兩者的處置不同（見 D-014 第五節）。

用法：
  VERIFY_ONLY=2 python3 verify_api.py      只跑測試 2（見 scripts/verify.sh）
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT", "900"))

# 僅用於啟發式提示的簡化字集合。這只是輔助線索，最終仍以肉眼判讀為準。
#
# 2026-09-18 擴充：原本漏收「气、摄」等常用字，導致 1.7b 的輸出
# 「台北目前的天气是多云，气温28摄氏度。」完全沒被偵測到。新增的字都
# 經過確認 —— 它們在繁體中文裡不會單獨出現（繁體作「氣」「攝」「電」…）。
SIMPLIFIED_HINTS = set(
    "说这个认确请时间现对开关们会发语词试结论过还种样让从没见觉问题简单杂运执选择输读写学长门车书买验测证"
    "气电摄东乐爱头医与为义风飞马鸟鱼点无专业师报场银铁图团园农华"
)

# 這些字在繁體中也合法，但在簡體中可能是「另一個字」的簡化：
#   ㆍ云 → 雲（多云 vs 多雲）   ㆍ后 → 後   ㆍ里 → 裡   ㆍ只 → 隻
#   ㆍ干 → 乾/幹   ㆍ准 → 準   ㆍ面 → 麵   ㆍ台 → 臺（但「台灣」為通行寫法）
# 因此不可逕判為簡體（會大量誤判），也不該完全忽略（會漏判，如「多云」）。
# 出現時只提出警告，交由人工複核，不影響通過與否。
AMBIGUOUS_HINTS = set("后里台面只干云准")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查詢指定城市目前的天氣狀況",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名稱，例如：台北、高雄",
                    }
                },
                "required": ["city"],
            },
        },
    }
]


# ── HTTP ────────────────────────────────────────────────
def _post(path, payload, timeout=TIMEOUT):
    """POST JSON 至 Ollama API。錯誤一律往上拋，不吞例外。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── 小工具 ──────────────────────────────────────────────
def _secs(ns):
    return (ns or 0) / 1e9


def _clip(text, limit=180):
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _show(text, limit=180):
    """輸出用的字串。空的一律明講「（空）」，不讓空白冒充成功。"""
    stripped = (text or "").strip()
    return "（空）" if not stripped else _clip(stripped, limit)


def _thinking_text(resp):
    """thinking 模型的思考內容位於獨立欄位，不在 response。"""
    return resp.get("thinking") or ""


def _thinking_state(resp):
    """判斷這次生成是否有思考內容。以 thinking 欄位為準。"""
    think = _thinking_text(resp)
    if think.strip():
        return f"有（thinking 欄位 {len(think)} 字）"
    if resp.get("done_reason") == "length":
        return "可能有（輸出被 num_predict 截斷）"
    return "無"


def _speed(resp):
    """回傳 (生成 token 數, 生成秒數, tok/s 或 None)。"""
    count = resp.get("eval_count") or 0
    dur = _secs(resp.get("eval_duration"))
    return count, dur, (count / dur if count and dur > 0 else None)


def _fmt_speed(count, dur, tps):
    if tps is None:
        return f"{count} tokens / {dur:.1f}s（無法計算速率）"
    return f"{count} tokens / {dur:.1f}s = {tps:.2f} tok/s"


# thinking 相關的字面值以串接方式組成。直接連著寫時，這些字串會在
# 編輯與傳輸過程中被當成標籤處理而遺失，因此刻意拆開。
THINK_OPEN = "<" + "think" + ">"
THINK_CLOSE = "</" + "think" + ">"
THINK_BLOCK_CLOSED = THINK_OPEN + "\n\n" + THINK_CLOSE + "\n\n"

# 由測試 2 決定；值為 None / "no_think" / "think_false" / "raw"
SUPPRESSION = None


def _chatml(prompt, system=None):
    """手工組出 Qwen3 的 ChatML，並預填一個「已關閉的思考區塊」。

    Qwen3 的對話樣板在關閉思考時，會把 assistant 段預先填成
    「THINK_OPEN ＋ 兩個換行 ＋ THINK_CLOSE ＋ 兩個換行」，模型接著就直接輸出答案。
    這條路 bypass 掉 Ollama 的樣板，因此不受 think 參數是否被支援影響。

    注意：這是實驗性做法，格式若不對就會失敗 —— 而失敗本身也是有用的資訊。
    """
    parts = []
    if system:
        parts.append(f"<|im_start|>system\n{system}<|im_end|>\n")
    parts.append(f"<|im_start|>user\n{prompt}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n" + THINK_BLOCK_CLOSED)
    return "".join(parts)


# _guard 專用的哨兵。回傳它代表「測試本身拋出例外」，
# 這與「測試正常執行但回傳 None」是兩件不同的事，不可混為一談。
# 2026-09-18 實測時測試 5 收到 HTTP 500（模型端 unexpected EOF），
# 就是因為兩者共用 None，總結才會顯示成「無法判定（輸出為空）」——
# 原因完全錯誤。這與 D-011 記錄的兩次判準失誤是同一類毛病。
FAILED = object()


def _guard(fn, *args):
    """執行單一測試並捕捉例外，不讓一個測試拖垮整輪。

    成功時回傳測試的傳回值；拋出例外時回傳 FAILED。
    """
    try:
        return fn(*args)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        print(f"  ✗ HTTP {exc.code}：{body}")
    except Exception as exc:  # noqa: BLE001 - 這裡刻意廣抓，確保失敗可見
        print(f"  ✗ {type(exc).__name__}：{exc}")
    return FAILED


# ── 測試 ────────────────────────────────────────────────
def test_baseline():
    """模型預設行為下的生成速度。這是使用者實際會體驗到的狀況。"""
    print("\n【1/5】基準生成與速度（模型預設行為，不調整 thinking）", flush=True)
    started = time.time()
    resp = _post(
        "/api/generate",
        {
            "model": MODEL,
            "prompt": "說：正常",
            "stream": False,
            "options": {"num_predict": 384},
        },
    )
    count, dur, tps = _speed(resp)
    answer = resp.get("response") or ""
    thinking = _thinking_text(resp)

    print(f"  模型載入      : {_secs(resp.get('load_duration')):.1f}s")
    print(f"  牆鐘耗時      : {time.time() - started:.1f}s")
    print(f"  生成速率      : {_fmt_speed(count, dur, tps)}")
    print(f"  thinking 狀態 : {_thinking_state(resp)}")
    print(f"  最終答案      : {_show(answer)}")
    # 首次實跑時，思考內容被誤當成「沒有輸出」。這裡明示它存在。
    print(f"  思考內容摘要  : {_show(thinking, 120)}")
    return tps


def test_suppression():
    """比較三種關閉 thinking 的方式，看哪一種真的有效。

    判準以 thinking 欄位本身為準，不是「token 數是否提早收斂」。
    首版用 token 數當判準，結果把有效的關閉方式誤判為無效 —— 因為模型
    即使不思考，也可能對模糊的提示詞長篇回覆。這裡改用有明確短答案的
    提示詞，並把基準組一起列出來對照。
    """
    global SUPPRESSION

    print("\n【2/5】關閉 thinking 的三種方式（效率關鍵）", flush=True)
    probe = "1+1 等於多少？只回答數字。"
    cap = 256

    variants = (
        ("baseline", "基準（不套用任何方式）", {"prompt": probe}),
        ("no_think", "提示詞加 /no_think", {"prompt": probe + " /no_think"}),
        ("think_false", "API think=false", {"prompt": probe, "think": False}),
        ("raw", "raw 預填已關閉的思考區塊", {"prompt": _chatml(probe), "raw": True}),
    )

    working = []   # 真正有效：欄位清空「且」答案合格
    cleared = []   # 只有欄位被清空，答案卻更差 —— 假象，不可採用
    baseline_think = None

    for key, name, extra in variants:
        payload = {
            "model": MODEL,
            "stream": False,
            "options": {"num_predict": cap},
        }
        payload.update(extra)
        try:
            t0 = time.time()
            resp = _post("/api/generate", payload)
            wall = time.time() - t0
        except urllib.error.HTTPError as exc:
            print(f"  ✗ {name}：API 拒絕（HTTP {exc.code}）")
            continue

        think = _thinking_text(resp)
        answer = resp.get("response") or ""
        count = resp.get("eval_count") or 0
        is_baseline = key == "baseline"
        think_off = not think.strip()
        compliant = _answer_compliant(answer, "2")

        print(f"  • {name}{'（對照組）' if is_baseline else ''}")
        if is_baseline:
            baseline_think = len(think)
        blank = "  ← 欄位已清空" if think_off and not is_baseline else ""
        print(f"      思考欄位：{len(think)} 字{blank}")
        print(f"      最終答案：{_show(answer, 80)}")
        print(f"      生成    ：{count} tokens / {wall:.0f}s")

        if is_baseline:
            continue
        if think_off and compliant:
            working.append(key)
            print("      ✓ 欄位清空且直接作答 —— 有效")
        elif think_off:
            cleared.append(key)
            print("      ✗ 欄位雖清空，但思考內容被搬進 response，答案並未變好")
        else:
            print("      ✗ 仍在思考")

    if working:
        SUPPRESSION = working[0]
        print(f"  真正有效的關閉方式：{SUPPRESSION}（後續測試將沿用）")
    else:
        SUPPRESSION = None
        print("  沒有真正可用的關閉方式 —— 維持 thinking 開啟（預設）。")
        if cleared:
            print("  注意：下列方式只是清空 thinking 欄位，模型其實仍在思考，")
            print("        內容改從 response 流出，答案更長也更差。不可採用：")
            for key in cleared:
                print(f"        • {key}")
    if baseline_think is not None:
        print(f"  對照：基準組的思考欄位有 {baseline_think} 字")
    return working


def _answer_compliant(answer, expected):
    """模型被要求「只回答數字」時，合格的答案必須短且直接。

    這個檢查是 2026-09-18 第二次實跑後補上的。當時 think=false 讓 thinking
    欄位清空，看似成功，實際上思考內容被搬進了 response —— 輸出長達 252 字
    且沒有作答。只看欄位會把它誤判為有效，必須一併檢查答案本身。

    用「包含」而非「開頭等於」：模型被要求只回答數字，但回「答案是 2」也
    算合格，不該因此判成失敗。真正要擋的是長篇大論，長度才是主要判準。
    """
    text = (answer or "").strip()
    return expected in text and len(text) <= 8


def _suppress_chat(payload):
    """測試 3/4 用：chat API 只認得 think 參數。"""
    if SUPPRESSION == "think_false":
        payload["think"] = False
    return payload


def _suppress_generate(payload, prompt, system=None):
    """測試 5 用：generate API，依測試 2 的結果套用可用的關閉方式。"""
    if SUPPRESSION == "think_false":
        payload["think"] = False
    elif SUPPRESSION == "raw":
        payload.pop("system", None)
        payload["prompt"] = _chatml(prompt, system=system)
        payload["raw"] = True
    elif SUPPRESSION == "no_think":
        payload["prompt"] = payload.get("prompt", prompt) + " /no_think"
    return payload


def test_tool_single():
    """單輪 tool calling —— 這是整個 MCP 階段的前提。"""
    print("\n【3/5】Tool calling — 單輪（MCP 階段的前提）", flush=True)
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "台北現在天氣如何？"}],
        "tools": TOOLS,
        "stream": False,
        "options": {"num_predict": 384},
    }
    resp = _post("/api/chat", _suppress_chat(payload))
    message = resp.get("message", {})
    calls = message.get("tool_calls") or []
    count, dur, tps = _speed(resp)

    if calls:
        print("  ✓ 產生了 tool call：")
        for call in calls:
            fn = call.get("function", {})
            args = json.dumps(fn.get("arguments"), ensure_ascii=False)
            print(f"      {fn.get('name')}({args})")
    else:
        print("  ✗ 沒有產生 tool call，模型改以自然語言回覆：")
        print(f"      {_show(message.get('content'))}")
    print(f"  生成速率：{_fmt_speed(count, dur, tps)}")
    return calls


def test_tool_multi(first_calls):
    """把工具結果餵回去，看模型能否收斂成最終答案。"""
    print("\n【4/5】Tool calling — 多輪（餵回工具結果）", flush=True)
    if not first_calls:
        print("  ─ 略過：單輪未產生 tool call，多輪無從測試")
        return False

    messages = [
        {"role": "user", "content": "台北現在天氣如何？"},
        {"role": "assistant", "content": "", "tool_calls": first_calls},
        {
            "role": "tool",
            "content": json.dumps(
                {"city": "台北", "temp_c": 28, "condition": "多雲"},
                ensure_ascii=False,
            ),
        },
    ]
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "options": {"num_predict": 512},
    }
    resp = _post("/api/chat", _suppress_chat(payload))
    message = resp.get("message", {})
    content = message.get("content") or ""
    again = message.get("tool_calls") or []
    count, dur, tps = _speed(resp)

    if again:
        print("  ! 第二輪仍要求呼叫工具，未收斂成答案（可能是循環）")
    if content.strip():
        print("  ✓ 產生了最終自然語言回覆：")
        print(f"      {_show(content, 240)}")
    else:
        print("  ✗ 沒有最終回覆（content 為空）")
        print(f"      思考內容摘要：{_show(_thinking_text(resp), 120)}")
    print(f"  生成速率：{_fmt_speed(count, dur, tps)}")

    return bool(content.strip()) and not again


# ── 事實正確性題庫（D-014 決策 1）────────────────────────
# 任何「模型知不知道 X」的驗證都必須附機械比對的字串清單，且必須包含
# 一題**對照題** —— 對照題的用途是隔離「模型不可靠」與「知識不存在」。
#
# 為什麼 expect_all 的每一項是一「組」替代字串，而不是單一字串：
#   本測試以繁體中文系統提示提問，模型會用中文作答。若只收
#   "model context protocol"，一個答對但寫「模型上下文協定」的模型會被判成
#   答錯 —— 那是**假失敗**。假失敗比漏報更糟：它會讓人開始懷疑一個其實
#   沒問題的設定（SIMPLIFIED_HINTS 的註解記過同一課）。
#   每一項因此是「其中任一個出現即可」。
#   清單刻意同時收繁體與簡體寫法 —— 用字正不正確由簡化字檢查另行判定，
#   不該讓它汙染真偽判定。
FACTUAL_QUESTIONS = [
    {
        "id": "mcp",
        "prompt": "用一句話說明什麼是 MCP。",
        "expect_all": (
            ("model context protocol", "模型上下文協定", "模型上下文协议",
             "模型情境協定"),
        ),
        "control": False,
        "why": "第二階段的主軸。MCP 由 Anthropic 於 2024 年 11 月發布 —— "
               "這個日期是判讀結果的關鍵：早於此的訓練資料不可能包含它。",
    },
    {
        "id": "http",
        "prompt": "用一句話說明什麼是 HTTP。",
        "expect_all": (
            ("hypertext transfer protocol", "超文本傳輸協定",
             "超文本传输协议", "超文本傳送協定"),
        ),
        "control": True,
        "why": "對照題。HTTP 早於所有候選模型的訓練截止，任何堪用的模型都該"
               "答對。它答對而 MCP 答錯，就把「模型整體不可靠」與「訓練資料"
               "早於 MCP」分開 —— 這兩者的處置完全相反。",
    },
]


def _missing_slots(answer, expect_all):
    """回傳沒有被滿足的槽位。每個槽位是「任一替代字串出現即可」。

    比對前一律轉小寫，因此 "Model Context Protocol"、"MODEL CONTEXT
    PROTOCOL" 都算命中。
    """
    text = (answer or "").lower()
    return [alts for alts in expect_all
            if not any(a.lower() in text for a in alts)]


def test_factual():
    """事實正確性 —— 機械比對，且必附一題對照題（D-014 決策 1）。

    為什麼這一項非有不可：
      本測試原本問了「什麼是 MCP」，卻只檢查形式 —— 回應非空、沒有洩漏
      提示、沒有被截斷。實測 qwen3:4b 回答「MCP 是阿里雲提供的模型」時，
      每一項形式檢查都通過。**驗證於是在答案是假的時候顯示通過。**
      問題不是判準寫錯，是根本沒有判準存在。

    為什麼一定要有對照題：
      只問 MCP 而答錯，無法區分兩件事 ——
        (a) 這顆模型不可靠，這類問題都答錯
        (b) 這顆模型的訓練資料早於 MCP 發布（2024 年 11 月），根本沒見過
      處置完全相反：(a) 要換模型；(b) 換同世代的模型沒用，只能靠 RAG
      （D-014 決策 2：RAG 是第二階段的**前提**，不是一個功能）。

    回傳 dict；無法判定時對應的值是 None，不可與 False 混為一談。
    """
    print("\n【5/5】事實正確性與繁體中文輸出控制", flush=True)
    system = "你必須一律使用繁體中文回答，嚴禁使用簡體字。"
    if SUPPRESSION:
        print(f"  （沿用測試 2 的關閉方式：{SUPPRESSION}）")

    by_id = {}
    script_verdicts = []  # 每題的繁體純度判定；None 代表無法判定

    for q in FACTUAL_QUESTIONS:
        tag = "對照題" if q["control"] else "目標題"
        print(f"\n  ── {q['id']}（{tag}）──")
        print(f"  題目：{q['prompt']}")
        payload = {
            "model": MODEL,
            "system": system,
            "prompt": q["prompt"],
            "stream": False,
            # 上限放到 1024：thinking 佔絕大部分，512 會來不及講完就被截斷
            # （2026-09-18 實測即為如此），那樣只能得到「無法判定」。
            "options": {"num_predict": 1024},
        }
        resp = _post(
            "/api/generate",
            _suppress_generate(payload, q["prompt"], system=system),
        )
        answer = resp.get("response") or ""
        count, dur, tps = _speed(resp)
        truncated = resp.get("done_reason") == "length"

        if not answer.strip():
            # 空回應不可判成「答錯」—— 模型可能把 1024 個 token 全用在
            # thinking 上。這兩者的處置不同，必須分開（D-014 第五節）。
            print("  ? 無法判定：response 為空，模型輸出全落在 thinking 欄位")
            print(f"      思考內容摘要：{_show(_thinking_text(resp), 120)}")
            print(f"  生成速率：{_fmt_speed(count, dur, tps)}")
            by_id[q["id"]] = {"factual": None, "control": q["control"]}
            script_verdicts.append(None)
            continue

        missing = _missing_slots(answer, q["expect_all"])
        hits = sorted(set(answer) & SIMPLIFIED_HINTS)
        ambiguous = sorted(set(answer) & AMBIGUOUS_HINTS)
        # 把題目或系統提示複述出來，代表模型在「處理這個問題」而不是
        # 「回答它」，也就是思考內容外流到 response。
        leaked = q["prompt"] in answer or system in answer

        print(f"  輸出：{_show(answer, 240)}")
        if missing:
            print("  ✗ 事實不正確 —— 缺少關鍵字串：")
            for alts in missing:
                print(f"      {'／'.join(alts)}")
        else:
            print("  ✓ 事實正確（關鍵字串齊備）")
        if hits:
            print(f"  ✗ 偵測到簡化字：{''.join(hits)}（啟發式，請複核）")
        else:
            print("  ✓ 未偵測到常見簡化字")
        if ambiguous:
            print(f"  ! 另有繁體亦合法的字：{''.join(ambiguous)}")
        if leaked:
            print("  ✗ 複述了題目或系統提示 —— 這是外流的思考內容，不是答案")
        if truncated:
            print(f"  ✗ 撞上 num_predict 上限（{count} tokens）即中斷，未產出完整答案")

        # 只有「形式合格」的輸出才可用來判定真偽 —— 外流的思考內容可能剛好
        # 含有關鍵字。這種情形寧可回報「無法判定」，也不要給出假的通過。
        # 注意：簡化字**不**列入形式判定。用字正不正確是另一件事，讓它擋掉
        # 真偽判定，會把「簡體但答錯」誤報成「無法判定」而失去真正的發現。
        wellformed = not leaked and not truncated
        if not wellformed:
            print("  → 輸出不合格（洩漏或截斷），事實判定不可信 —— 判為無法判定。")
        factual = (not missing) if wellformed else None

        print(f"  生成速率：{_fmt_speed(count, dur, tps)}")
        by_id[q["id"]] = {"factual": factual, "control": q["control"]}
        script_verdicts.append(not hits)

    def combine(vals):
        if any(v is None for v in vals):
            return None
        return all(vals)

    controls = [v["factual"] for v in by_id.values() if v["control"]]
    return {
        "by_id": by_id,
        "script_ok": combine(script_verdicts),
        "control_ok": controls[0] if controls else None,
    }


def _factual_verdict(f):
    """把目標題與對照題的結果翻成一句判讀。

    這四種組合的處置完全不同，但它們在輸出上長得一樣（都是一句中文回答）——
    這正是 D-014 要求對照題的理由。
    """
    target = next((v["factual"] for v in f["by_id"].values()
                   if not v["control"]), None)
    control = f["control_ok"]
    if control is None or target is None:
        return "無法判定（見上方）"
    if control and target:
        return "通過（目標題與對照題皆正確）"
    if control and not target:
        return "目標題不正確、對照題正確 → 知識不存在，非模型不可靠（D-014）"
    if not control and not target:
        return "兩題皆不正確 → 模型整體不可靠（對照題也不過，這才是換模型的訊號）"
    return "對照題不正確但目標題正確 —— 少見，請人工複核輸出"


# ── 主流程 ──────────────────────────────────────────────
def main():
    print("=" * 62)
    print(" 第二階段前置驗證 —— 直接對 Ollama API 實測")
    print("=" * 62)
    print(f" 端點：{BASE}")
    print(f" 模型：{MODEL}")
    print(" 每項測試可能耗時 1-3 分鐘，整輪約 15 分鐘，請勿中斷。")
    print("=" * 62)

    # 前置檢查：連不上或模型不存在就沒必要往下跑，先講清楚
    try:
        tags = _get("/api/tags")
        version = _get("/api/version").get("version", "未知")
    except Exception as exc:  # noqa: BLE001 - 連不上就沒戲唱，直接回報
        print(f"\n✗ 無法連線 Ollama（{BASE}）：{exc}")
        return 2

    names = [m.get("name") for m in tags.get("models", []) if m.get("name")]
    print(f"\nOllama 版本：{version}")
    if MODEL not in names:
        print(f"✗ 模型 {MODEL} 不存在。目前已有：{', '.join(names) or '（無）'}")
        print("   請先執行：bash scripts/up.sh")
        return 2
    print(f"前置檢查通過：模型 {MODEL} 存在")

    only = {s.strip() for s in os.environ.get("VERIFY_ONLY", "").split(",") if s.strip()}
    if only:
        print(f"\n（僅執行測試 {'、'.join(sorted(only))}）")

    def want(n):
        return not only or str(n) in only

    speed = _guard(test_baseline) if want(1) else None
    working_raw = _guard(test_suppression) if want(2) else []
    working = working_raw if isinstance(working_raw, list) else []
    calls_raw = _guard(test_tool_single) if want(3) else None
    calls = calls_raw if isinstance(calls_raw, list) else None
    multi_ok = _guard(test_tool_multi, calls) if want(4) else False
    factual = _guard(test_factual) if want(5) else None

    # ── 總結 ────────────────────────────────────────────
    print("\n" + "=" * 62)
    print(" 總結")
    print("=" * 62)
    def tri(ran, value):
        """五種狀態必須分得開：未執行 / 執行失敗 / 無法判定 / 通過 / 未通過。

        把它們併成「未通過」會讓人以為功能退步；併成「無法判定」則會掩蓋
        真正的錯誤。這兩個缺陷都是 2026-09-18 實測時自己製造出來的。
        """
        if not ran:
            return "未執行"
        if value is FAILED:
            return "執行失敗（見上方錯誤）"
        if value is None:
            return "無法判定（輸出為空）"
        return "通過" if value else "未通過"

    if not want(1):
        speed_text = "未執行"
    elif speed is FAILED:
        speed_text = "執行失敗（見上方錯誤）"
    elif isinstance(speed, float):
        speed_text = f"{speed:.2f} tok/s"
    else:
        speed_text = "未取得"

    print(f"  生成速度      : {speed_text}")
    if working:
        print(f"  可關閉 thinking: {working[0]}（欄位清空且作答正確）")
    else:
        print("  可關閉 thinking: 沒有可用方式 —— 維持預設（thinking 開啟）")
    print(f"  單輪 tool call: {tri(want(3), calls_raw)}")
    print(f"  多輪 tool call: {tri(want(4), multi_ok)}")

    # 測試 5 回傳的是 dict，不能直接丟進 tri() —— 但「執行失敗」與
    # 「無法判定」仍必須分得開，否則 FAILED（一個物件）會被當成通過。
    f = factual if isinstance(factual, dict) else None
    if not want(5):
        print("  事實正確性    : 未執行")
    elif factual is FAILED:
        print("  事實正確性    : 執行失敗（見上方錯誤）")
    elif f is None:
        print("  事實正確性    : 無法判定")
    else:
        print(f"  事實正確性    : {_factual_verdict(f)}")
    print(f"  繁體中文控制  : {tri(want(5), f['script_ok'] if f else None)}")
    print("=" * 62)

    if only:
        print("（僅執行部分測試，不做整體結論）")
        return 0

    # FAILED 是物件，bool() 為真 —— 必須明確比對，否則「執行失敗」會被
    # 當成「通過」。這裡刻意不用 bool()，就是要擋掉這種情形。
    control_ok = f["control_ok"] if f else None
    target_ok = None
    if f:
        target_ok = next((v["factual"] for v in f["by_id"].values()
                          if not v["control"]), None)

    # 對照題必須通過 —— 它答錯代表模型連「早於所有訓練截止」的問題都答不好，
    # 那才是「該換模型」的訊號。
    # 目標題**不列入** critical：D-014 決策 2 已認定這是預期結果 ——
    # 知識不存在不是缺陷，而是第二階段需要 RAG 的理由。
    critical = (
        isinstance(speed, float)
        and bool(calls)
        and multi_ok is True
        and control_ok is True
    )
    if critical:
        print(" 結論：MCP 階段的核心前提（多輪 tool calling）與模型可靠度皆成立。")
        if target_ok is False:
            print("       目標題顯示模型缺乏該知識 —— 這是 D-014 的預期結果，")
            print("       第二階段的 RAG 是前提而非功能，不可略過。")
    else:
        if control_ok is False:
            print(" 結論：對照題未通過 —— 模型對早於訓練截止的問題也答錯，")
            print("       這是模型可靠度的問題，第二階段需調整模型或策略。")
        else:
            print(" 結論：有關鍵項目未通過，第二階段需調整模型或策略。")
    return 0 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
