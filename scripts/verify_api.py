#!/usr/bin/env python3
"""第二階段前置驗證：直接對 Ollama API 實測，不經過 Open WebUI。

由 scripts/verify.sh 複製進 open-webui 容器後執行。之所以要繞這一段，是因為
docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠（見 DECISIONS.md D-003），
主機端連不到 Ollama API；但容器之間可透過 ai-net 互通，因此借用已在執行的
open-webui 容器（內建 Python）來發請求。

只用 Python 標準函式庫，容器內不需要額外安裝任何套件。

要回答的四個問題：
  1. 4b 在 2-core CPU 上的實際生成速度（tok/s）
  2. 4b 能否產生 tool calls，並在多輪之後收斂成答案（見 D-005）
  3. 能否關閉 thinking 模式 —— 它會吃掉大量 token 與時間
  4. 能否用系統提示詞強制繁體中文輸出

v2 修正（源自 2026-09-18 首次實跑的三個發現）：
  • thinking 模型的思考內容位於獨立的 thinking 欄位，不在 response。
    v1 只讀 response，造成「明明有產出卻顯示空白」。
  • 空輸出必須明講「（空）」。v1 讓繁體中文檢測在空字串上誤判為通過 ——
    這是典型的靜默失敗。
  • num_predict 上限過小，模型還在思考就被截斷，多輪測試因此假性失敗。

v3 修正（源自同一次實跑，但屬於判準本身的缺陷）：
  • 測試 2 原本以「token 數是否提早收斂」判斷 thinking 是否被關閉，這是錯的
    判準 —— 模型即使不思考，也可能對模糊的提示詞長篇回覆。實測資料反而顯示
    think=false 與 raw 兩種方式的 thinking 欄位都是空的（相對於基準組的 610
    字），也就是「有效」卻被判成無效。現改以 thinking 欄位本身為判準，並加
    入基準對照組同時列出。
  • 測試 2 找到可用的關閉方式後，測試 3/4/5 會沿用（SUPPRESSION），讓整輪
    更接近實際使用情境，也讓測試 5 能在額度內產出可判讀的答案。

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

# 僅用於啟發式提示的簡化字集合。刻意排除「后、里、台、面、只、干、云、准」
# 等同時也是合法繁體的字，避免誤判。這只是輔助線索，最終仍以肉眼判讀為準。
SIMPLIFIED_HINTS = set(
    "说这个认确请时间现对开关们会发语词试结论过还种样让从没见觉问题简单杂运执选择输读写学长门车书买验测证"
)

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


def _guard(fn, *args):
    """執行單一測試並捕捉例外，不讓一個測試拖垮整輪。"""
    try:
        return fn(*args)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        print(f"  ✗ HTTP {exc.code}：{body}")
    except Exception as exc:  # noqa: BLE001 - 這裡刻意廣抓，確保失敗可見
        print(f"  ✗ {type(exc).__name__}：{exc}")
    return None


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

    working = []
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
        count = resp.get("eval_count") or 0
        is_baseline = key == "baseline"
        think_off = not think.strip()

        if is_baseline:
            baseline_think = len(think)
            note = "（對照組）"
        elif think_off:
            working.append(key)
            note = "  ← thinking 已關閉"
        else:
            note = ""

        print(f"  • {name}{note}")
        print(f"      思考欄位：{len(think)} 字")
        print(f"      最終答案：{_show(resp.get('response'), 80)}")
        print(f"      生成    ：{count} tokens / {wall:.0f}s")

    if working:
        SUPPRESSION = working[0]
        print(f"  有效的關閉方式：{SUPPRESSION}（後續測試將沿用）")
        if baseline_think is not None:
            print(f"  對照：基準組的思考欄位有 {baseline_think} 字")
    else:
        print("  三種方式皆無效 —— 模型一律進入思考模式。")
    return working


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


def test_traditional_chinese():
    """測試能否用系統提示詞強制繁體輸出。

    回傳 True / False / None 三態 —— None 代表無法判定（輸出為空），
    不可與「通過」混為一談。
    """
    print("\n【5/5】繁體中文輸出控制", flush=True)
    system = "你必須一律使用繁體中文回答，嚴禁使用簡體字。"
    prompt = "用一句話說明什麼是 MCP。"
    if SUPPRESSION:
        print(f"  （沿用測試 2 的關閉方式：{SUPPRESSION}）")
    payload = {
        "model": MODEL,
        "system": system,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": 512},
    }
    resp = _post(
        "/api/generate", _suppress_generate(payload, prompt, system=system)
    )
    answer = resp.get("response") or ""
    count, dur, tps = _speed(resp)

    if not answer.strip():
        print("  ? 無法判定：response 為空，模型輸出全落在 thinking 欄位")
        print(f"      思考內容摘要：{_show(_thinking_text(resp), 120)}")
        print(f"  生成速率：{_fmt_speed(count, dur, tps)}")
        return None

    hits = sorted(set(answer) & SIMPLIFIED_HINTS)
    print(f"  輸出：{_show(answer, 240)}")
    if hits:
        print(f"  ✗ 偵測到簡化字：{''.join(hits)}（啟發式，請複核）")
    else:
        print("  ✓ 未偵測到常見簡化字")
    print(f"  生成速率：{_fmt_speed(count, dur, tps)}")
    return not hits


# ── 主流程 ──────────────────────────────────────────────
def main():
    print("=" * 62)
    print(" 第二階段前置驗證 —— 直接對 Ollama API 實測")
    print("=" * 62)
    print(f" 端點：{BASE}")
    print(f" 模型：{MODEL}")
    print(" 每項測試可能耗時 1-3 分鐘，整輪約 10 分鐘，請勿中斷。")
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
    working = (_guard(test_suppression) or []) if want(2) else []
    calls = _guard(test_tool_single) if want(3) else None
    multi_ok = _guard(test_tool_multi, calls) if want(4) else False
    chinese_ok = _guard(test_traditional_chinese) if want(5) else None

    # ── 總結 ────────────────────────────────────────────
    print("\n" + "=" * 62)
    print(" 總結")
    print("=" * 62)
    print(f"  生成速度      : {f'{speed:.2f} tok/s' if speed else '未取得（測試 1 失敗）'}")
    print(f"  可關閉 thinking: {working[0] if working else '沒有可用方式'}")
    print(f"  單輪 tool call: {'通過' if calls else '未通過'}")
    print(f"  多輪 tool call: {'通過' if multi_ok else '未通過'}")
    if chinese_ok is None:
        print("  繁體中文控制  : 無法判定（輸出為空）")
    else:
        print(f"  繁體中文控制  : {'通過' if chinese_ok else '未通過'}")
    print("=" * 62)

    if only:
        print("（僅執行部分測試，不做整體結論）")
        return 0

    critical = bool(speed) and bool(calls) and bool(multi_ok)
    if critical:
        print(" 結論：MCP 階段的核心前提（多輪 tool calling）成立。")
    else:
        print(" 結論：有關鍵項目未通過，第二階段需調整模型或策略。")
    return 0 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
