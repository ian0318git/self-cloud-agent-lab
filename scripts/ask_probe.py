#!/usr/bin/env python3
"""量測聊天模型的「答案正確性」與「生成速度」—— rag_probe.py 不看的兩個維度。

為什麼需要這支腳本：
  D-001 選 qwen3:4b 時只驗證了「模型跑得起來」，沒有驗證「答得對不對、
  夠不夠快」。但 Phase 1 的驗收清單要求這兩件事（HANDBOOK：
  "a sensible answer arrives within 10–30 seconds"）。驗收項存在，
  對應的量測工具卻不存在 —— 於是那個缺口一直沒被發現，
  直到 Phase 2 要用 MCP 才撞上（D-014）。

為什麼要機械評分，不靠人眼看：
  D-011 的教訓是「把推論寫成結論」。人眼看輸出還有一個更隱蔽的問題：
  答案讀起來結構完整、語氣自信，就會被當成對的。
  實測 qwen3:4b 對「什麼是 MCP」給出**四個不同的錯誤展開**，
  每一個都條理分明、毫不猶豫（見 D-014）。
  這種錯誤正是人眼最容易放過的。因此每題附一份「必須出現」的字串清單，
  由程式判定；人眼只負責覆核程式判出來的結果。

為什麼要有對照題：
  只問「MCP 是什麼」而得到錯誤答案，無法區分兩件事：
    (a) 這顆模型不可靠，這類問題都答錯
    (b) 這顆模型的訓練資料早於 MCP 發布（2024 年 11 月），根本沒見過
  這兩者的處置完全相反：(a) 要換模型；(b) 只要有更新知識的模型即可 ——
  而 (b) 在小模型上是常態，換一顆同世代的模型不會有幫助。
  因此每顆受測模型都要同時回答一題「早於所有候選模型訓練截止」的對照題。
  對照題答對 + MCP 答錯 → 指向 (b)。
  只測 MCP 一題就下結論，等於把 (b) 誤判成 (a)。

為什麼每題都統計簡體字：
  本專案的文件是繁體中文，而候選模型都以簡體語料為主訓練。
  D-013 量過「檢索」這一側的繁簡偏差；**生成這一側沒有人看過**。
  實測 qwen3:4b 對繁體提問回出整段簡體，以及同一句裡繁簡混雜。
  這直接決定輸出能不能用，因此列為每題都報的指標。

用法（見 scripts/ask_probe.sh，需在容器內執行）：
  python3 ask_probe.py qwen3:4b qwen2.5:3b
  python3 ask_probe.py --think qwen3:4b
  python3 ask_probe.py --questions mcp qwen3:4b
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")

# ── 題庫 ────────────────────────────────────────────────
# expect_all 的每一項是一「組」替代字串：**其中任一個出現即可**。槽位之間
# 才是「全部都要滿足」。比對一律轉小寫，是以 "MODEL CONTEXT PROTOCOL"
# 與 "Model Context Protocol" 等價。
#
# 為什麼要分組（2026-09-19，D-016）：本工具原本每題只收一個英文字串，於是
# 「答對但寫中文全名」的模型會被判成答錯 —— 那是**假失敗**。同一課早就寫在
# verify_api.py 的 expect_all 註解裡，卻沒有套用到這裡；教訓記下來了，
# 沒有變成判準，正是 D-014 自己指出的失效形狀。假失敗比漏報更糟：
# 它會讓人開始懷疑一個其實沒問題的設定。
#
# 另一件同一課的事：**問句必須與評分對齊**。原本問「用三句話解釋 X 是什麼」，
# 這是開放題 —— 答對不必出現全名，與 expect_all 天生不相容。實測時對照題
# 答對了卻被判錯，工具於是印出「這才是換模型的訊號」。兩題必須維持同一個
# 形狀，否則隔離「模型不可靠」與「知識不存在」的能力就沒了。
QUESTIONS = [
    {
        "id": "mcp",
        "prompt": "MCP 的英文全名是什麼？",
        "expect_all": (
            ("model context protocol", "模型上下文協定", "模型上下文协议",
             "模型情境協定"),
        ),
        "why": "Phase 2 的主軸。MCP 由 Anthropic 於 2024 年 11 月發布 —— "
               "這個日期是判讀結果的關鍵：早於此的訓練資料不可能包含它。",
    },
    {
        "id": "http",
        "prompt": "HTTP 的英文全名是什麼？",
        "expect_all": (
            ("hypertext transfer protocol", "超文本傳輸協定",
             "超文本传输协议", "超文本傳送協定"),
        ),
        "why": "對照題。HTTP 早於所有候選模型的訓練截止，任何堪用的模型都該答對。"
               "它答對而 MCP 答錯，就把「模型整體不可靠」與「訓練資料早於 MCP」分開。",
    },
]

# ── 推理模型的軟開關 ────────────────────────────────────
# qwen3 系列預設先產出一大段 thinking 才回答。在本專案的硬體上，那段
# thinking 常常吃掉 400 個 token 還沒進到答案（實測，見 D-014）——
# 使用者看到的不是「答錯」，而是「一直不回答」。
# Qwen3 官方提供 /no_think 軟開關。但**只對已知的推理模型加前綴**：
# 對每個模型都加，會讓非推理模型收到一段它不理解的指令，
# 兩邊的提示就不一樣了 —— 那會讓「模型之間的比較」失去意義。
REASONING_MODELS = ("qwen3", "qwq", "deepseek-r1", "magistral")

# ── 繁體純度檢查 ────────────────────────────────────────
# 只收「簡體有、繁體沒有」的字。這份表刻意保守：寧可漏報，不可誤報 ——
# 誤報會讓人開始懷疑一個其實沒問題的輸出，那比漏報更糟。
# 因此輸出的是**命中的字與次數**，不是一個是／否布林值，
# 讓人可以自己判斷那是真的簡體，還是撞到了一個字形相近的字。
SIMPLIFIED_ONLY = set(
    "专态电脑应该项体统络训练据库检测试验结证误写输键词义释确识别区线"
    "组务压动类变换户软认数图缩网为说这么门关问间处现实对开机时题让给"
    "还从会个样种设备汉页风飞马鸟鱼龙学觉语读书长车东乐买卖见观规视计"
    "论讲贝财员圆场块声壳边达过运连进远违适选钟铁银钱错复总经办协单传"
    "优价众伤团济经"
)


def post_json(path, payload, timeout):
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_json(path, timeout):
    """/api/tags 等端點只接受 GET。用 POST 打會得到 405，不是 404 ——
    那個差別很容易被誤讀成「ollama 壞了」，實際上只是動詞用錯。"""
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def is_reasoning_model(model):
    return any(model.startswith(p) or p in model for p in REASONING_MODELS)


def ask(model, prompt, num_predict, think):
    """回傳 (回應 dict, 實際送出的提示)。"""
    sent = prompt
    if not think and is_reasoning_model(model):
        # 軟開關必須在提示最前面才生效。
        sent = "/no_think\n" + prompt

    payload = {
        "model": model,
        "prompt": sent,
        "stream": False,
        "options": {"num_predict": num_predict},
    }
    t0 = time.time()
    resp = post_json("/api/generate", payload, timeout=1800)
    resp["_wall"] = time.time() - t0
    return resp, sent


def simplified_hits(text):
    return sorted({c for c in text if c in SIMPLIFIED_ONLY})


def grade(resp, q):
    """回傳 (是否通過, 缺少的槽位)。

    每個槽位是「任一替代字串出現即可」，回傳的是把該組接起來的說明字串，
    方便直接印給人看。空輸出會讓所有槽位都算缺少 —— 不會被誤判為通過。
    """
    body = (resp.get("response") or "").lower()
    missing = ["／".join(alts) for alts in q["expect_all"]
               if not any(a.lower() in body for a in alts)]
    return (not missing), missing


def report(model, q, resp, sent, num_predict):
    ns = 1e9
    text = resp.get("response") or ""
    ec = resp.get("eval_count", 0)
    ed = resp.get("eval_duration", 0) / ns
    rate = ec / ed if ed else 0.0
    reason = resp.get("done_reason") or "?"
    passed, missing = grade(resp, q)
    hits = simplified_hits(text)

    print(f"  ── 題目 {q['id']} ─────────────────────────────")
    print(f"  判定            {'通過' if passed else '不通過'}")
    if missing:
        print(f"  缺少字串        {missing}")
    print(f"  總牆鐘時間      {resp['_wall']:8.1f} s")
    print(f"  模型載入        {resp.get('load_duration', 0) / ns:8.1f} s")
    print(f"  提示處理        {resp.get('prompt_eval_duration', 0) / ns:8.1f} s")
    print(f"  生成            {ed:8.1f} s")
    print(f"  生成 token 數   {ec:8d}   （上限 {num_predict}）")
    print(f"  生成速率        {rate:8.2f} tok/s")
    print(f"  結束原因        {reason}"
          + ("   ← 被上限截斷，答案不完整" if reason == "length" else ""))

    # thinking 欄位必須一起報。只讀 response，會讓「模型把 token 全燒在思考」
    # 顯示成「模型什麼都沒說」—— 兩者的處置完全不同，而輸出長得一模一樣。
    # D-014 第五節就是卡在這裡：工具沒報這一項，於是 1024 個 token 的去向
    # 無法判讀。少了數字的欄位，比錯的數字更難察覺。
    ttext = resp.get("thinking") or ""
    print(f"  thinking 長度   {len(ttext):8d} 字")
    if ttext:
        print(f"  thinking 開頭   {ttext[:100]}")

    if hits:
        total = len(text)
        print(f"  簡體字          {len(hits)} 種（共 {sum(text.count(c) for c in hits)} 次）"
              f"，回應長度 {total} 字")
        print(f"  命中            {''.join(hits)}")
    else:
        print(f"  簡體字          無（回應長度 {len(text)} 字）")
    print("  ── 回應全文 ──")
    for line in text.splitlines() or [""]:
        print(f"  │ {line}")
    print()


def main(argv):
    args = list(argv)
    think = False
    num_predict = 1024
    only = None
    models = []

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--think":
            think = True
        elif a == "--num-predict":
            i += 1
            num_predict = int(args[i])
        elif a.startswith("--num-predict="):
            num_predict = int(a.split("=", 1)[1])
        elif a == "--questions":
            i += 1
            only = args[i].split(",")
        elif a.startswith("--"):
            print(f"不認得的選項：{a}", file=sys.stderr)
            return 2
        else:
            models.append(a)
        i += 1

    if not models:
        print("請至少指定一個模型。", file=sys.stderr)
        return 2

    qs = [q for q in QUESTIONS if only is None or q["id"] in only]
    if not qs:
        print(f"--questions 沒有對到任何題目：{only}", file=sys.stderr)
        return 2

    print(f"受測模型：{', '.join(models)}")
    print(f"題目：{', '.join(q['id'] for q in qs)}")
    print(f"思考模式：{'開（模型預設）' if think else '關（推理模型加 /no_think）'}")
    print(f"num_predict：{num_predict}")
    print()

    # 先確認每個模型都在，缺的一次講完。理由同 rag_probe.sh：不要讓實驗
    # 跑到一半才發現某顆模型沒下載，那會浪費前面已經跑完的時間。
    try:
        listed = get_json("/api/tags", timeout=60)
        have = {m["name"] for m in listed.get("models", [])}
    except Exception as exc:
        print(f"無法向 ollama 取得模型清單：{exc}", file=sys.stderr)
        return 1

    def norm(n):
        # ollama 一律回報帶 tag 的名字；使用者打的是 bge-m3 這種短名。
        tail = n.rsplit("/", 1)[-1]
        return n if ":" in tail else n + ":latest"

    have_n = {norm(n) for n in have}
    missing = [m for m in models if norm(m) not in have_n]
    if missing:
        print(f"以下模型尚未下載：{' '.join(missing)}", file=sys.stderr)
        for m in missing:
            print(f"  bash scripts/pull-model.sh {m}", file=sys.stderr)
        return 1

    fails = 0
    for model in models:
        print("═" * 62)
        print(f"模型 {model}" + ("（推理模型，已加 /no_think）"
                                if is_reasoning_model(model) and not think else ""))
        print("═" * 62)
        for q in qs:
            print(f"  題目：{q['prompt']}")
            try:
                resp, sent = ask(model, q["prompt"], num_predict, think)
            except urllib.error.URLError as exc:
                print(f"  請求失敗：{exc}")
                fails += 1
                continue
            report(model, q, resp, sent, num_predict)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
