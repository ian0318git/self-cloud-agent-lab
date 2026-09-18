#!/usr/bin/env python3
"""第二階段前置驗證：直接對 Ollama API 實測，不經過 Open WebUI。

由 scripts/verify.sh 複製進 open-webui 容器後執行。之所以要繞這一段，是因為
docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠（見 DECISIONS.md D-003），
主機端連不到 Ollama API；但容器之間可透過 ai-net 互通，因此借用已在執行的
open-webui 容器（內建 Python）來發請求。

只用 Python 標準函式庫，容器內不需要額外安裝任何套件。

要回答的三個問題：
  1. 4b 在 2-core CPU 上的實際生成速度（tok/s）
  2. 4b 能否穩定產生 tool calls —— 這是 MCP 階段的前提（見 D-005）
  3. 能否用系統提示詞強制繁體中文輸出

任何未預期的錯誤都會印出並計入失敗，不靜默通過。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
TIMEOUT = int(os.environ.get("VERIFY_TIMEOUT", "600"))

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


def _answer_only(text):
    """去掉 thinking 段落，只留下最終答案。"""
    text = text or ""  # 模型可能回傳 content: null，不能讓它炸掉
    marker = "</think>"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text.strip()


def _speed(resp):
    """回傳 (生成 token 數, 生成秒數, tok/s 或 None)。"""
    count = resp.get("eval_count") or 0
    dur = _secs(resp.get("eval_duration"))
    return count, dur, (count / dur if count and dur > 0 else None)


def _fmt_speed(count, dur, tps):
    if tps is None:
        return f"{count} tokens / {dur:.1f}s（無法計算速率）"
    return f"{count} tokens / {dur:.1f}s = {tps:.2f} tok/s"


def _thinking_state(resp):
    """判斷這次生成是否處於 thinking 模式。

    </think> 出現  → 確定有思考
    被長度截斷     → 很可能還卡在思考中
    其餘           → 沒有觀察到思考
    """
    text = resp.get("response") or ""
    if "</think>" in text:
        return "有（偵測到 </think>）"
    if resp.get("done_reason") == "length":
        return "可能有（輸出被 num_predict 截斷，未及產出答案）"
    return "未觀察到"


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
            "options": {"num_predict": 64},
        },
    )
    count, dur, tps = _speed(resp)

    print(f"  模型載入      : {_secs(resp.get('load_duration')):.1f}s")
    print(f"  牆鐘耗時      : {time.time() - started:.1f}s")
    print(f"  生成速率      : {_fmt_speed(count, dur, tps)}")
    print(f"  thinking 狀態 : {_thinking_state(resp)}")
    print(f"  原始輸出      : {_clip(resp.get('response'))}")
    return tps


def test_no_think():
    """比較兩種關閉 thinking 的方式，看哪一種真的有效。

    判準是生成 token 數：答案只有兩個字，若 thinking 關閉，token 數應該個位數；
    若仍開啟，會暴增到數十至數百。
    """
    print("\n【2/5】關閉 thinking 的兩種方式是否有效", flush=True)
    outcome = {}

    # 方式 A：提示詞加上 /no_think（Qwen3 的軟開關）
    resp = _post(
        "/api/generate",
        {
            "model": MODEL,
            "prompt": "說：正常 /no_think",
            "stream": False,
            "options": {"num_predict": 64},
        },
    )
    count, _, _ = _speed(resp)
    outcome["提示詞加 /no_think"] = (count, _thinking_state(resp))

    # 方式 B：API 的 think 參數（Ollama 伺服器層級）
    try:
        resp = _post(
            "/api/generate",
            {
                "model": MODEL,
                "prompt": "說：正常",
                "stream": False,
                "think": False,
                "options": {"num_predict": 64},
            },
        )
        count, _, _ = _speed(resp)
        outcome["API think=false"] = (count, _thinking_state(resp))
    except urllib.error.HTTPError as exc:
        outcome["API think=false"] = (None, f"API 拒絕（HTTP {exc.code}）")

    for name, (count, state) in outcome.items():
        tokens = "？（未取得）" if count is None else f"{count} tokens"
        print(f"  • {name}：{tokens}｜thinking {state}")

    print("  判讀：token 數若在個位數 → 該方式有效；若數十以上 → 無效。")
    return outcome


def test_tool_single():
    """單輪 tool calling —— 這是整個 MCP 階段的前提。"""
    print("\n【3/5】Tool calling — 單輪（MCP 階段的前提）", flush=True)
    resp = _post(
        "/api/chat",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "台北現在天氣如何？"}],
            "tools": TOOLS,
            "stream": False,
            "options": {"num_predict": 256},
        },
    )
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
        print(f"      {_clip(_answer_only(message.get('content', '')))}")
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
    resp = _post(
        "/api/chat",
        {
            "model": MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "options": {"num_predict": 256},
        },
    )
    message = resp.get("message", {})
    content = _answer_only(message.get("content", ""))
    again = message.get("tool_calls") or []
    count, dur, tps = _speed(resp)

    if again:
        print("  ! 第二輪仍要求呼叫工具，未收斂成答案（可能是循環）")
    if content:
        print("  ✓ 產生了最終自然語言回覆：")
        print(f"      {_clip(content, 240)}")
    else:
        print("  ✗ 沒有產生最終回覆")
    print(f"  生成速率：{_fmt_speed(count, dur, tps)}")

    return bool(content) and not again


def test_traditional_chinese():
    """測試能否用系統提示詞強制繁體輸出。"""
    print("\n【5/5】繁體中文輸出控制", flush=True)
    resp = _post(
        "/api/generate",
        {
            "model": MODEL,
            "system": "你必須一律使用繁體中文回答，嚴禁使用簡體字。",
            "prompt": "用一句話說明什麼是 MCP。",
            "stream": False,
            "options": {"num_predict": 200},
        },
    )
    answer = _answer_only(resp.get("response", ""))
    count, dur, tps = _speed(resp)
    hits = sorted(set(answer) & SIMPLIFIED_HINTS)

    print(f"  輸出：{_clip(answer, 240)}")
    if hits:
        print(f"  ! 偵測到疑似簡化字：{''.join(hits)}（啟發式，請複核）")
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

    # 前置檢查：模型不存在就沒必要往下跑，先講清楚
    try:
        tags = _get("/api/tags")
    except Exception as exc:  # noqa: BLE001 - 連不上就沒戲唱，直接回報
        print(f"\n✗ 無法連線 Ollama（{BASE}）：{exc}")
        return 2

    names = [m.get("name") for m in tags.get("models", []) if m.get("name")]
    if MODEL not in names:
        print(f"\n✗ 模型 {MODEL} 不存在。目前已有：{', '.join(names) or '（無）'}")
        print("   請先執行：bash scripts/up.sh")
        return 2
    print(f"\n前置檢查通過：模型 {MODEL} 存在")

    speed = _guard(test_baseline)
    _guard(test_no_think)
    calls = _guard(test_tool_single)
    multi_ok = _guard(test_tool_multi, calls)
    chinese_ok = _guard(test_traditional_chinese)

    # ── 總結 ────────────────────────────────────────────
    print("\n" + "=" * 62)
    print(" 總結")
    print("=" * 62)
    if speed:
        print(f"  生成速度      : {speed:.2f} tok/s")
    else:
        print("  生成速度      : 未取得（測試 1 失敗）")
    print(f"  單輪 tool call: {'通過' if calls else '未通過'}")
    print(f"  多輪 tool call: {'通過' if multi_ok else '未通過'}")
    print(f"  繁體中文控制  : {'通過' if chinese_ok else '未通過或未偵測'}")
    print("=" * 62)

    critical = bool(speed) and bool(calls) and bool(multi_ok)
    if critical:
        print(" 結論：MCP 階段的核心前提（tool calling）成立。")
    else:
        print(" 結論：有關鍵項目未通過，第二階段需調整模型或策略。")
    return 0 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
