#!/usr/bin/env python3
"""部署後的煙霧測試：證明這個堆疊真的生成得出東西，而且生成是**完整的**。

用法（在 open-webui 容器裡跑，見 scripts/deploy-vps.sh）：

    $COMPOSE exec -T open-webui python3 - smoke <model> [num_predict] < scripts/deploy_smoke_probe.py
    $COMPOSE exec -T open-webui python3 - ctx   <model> <expected_ctx> < scripts/deploy_smoke_probe.py

為什麼要寫成一個檔案、用 stdin 餵進容器，而不是一行 `python3 -c`：
`ollama` 服務**刻意不發佈任何埠**（docker-compose.yml:20-22），所以主機上
沒有任何東西能直接呼叫它。全 repo 既有的做法是在同一個網路裡的容器內跑
客戶端（lib.sh:64-75 的 wait_for_webui、open-webui 的 healthcheck），而
open-webui 映像檔保證有 python3 與標準庫。這裡**只用標準庫**：不需要建
image、不需要拉 curl、不需要主機上的 pip 套件 —— 這在全新的 VPS 上是重點。

── 為什麼煙霧測試不能只問「有沒有回話」 ──────────────────────

這是本專案重複最多次的教訓（D-027 第十一節，六處）：**被切斷的生成不能
拿來當完整的量測**。一個只檢查 `content` 非空的煙霧測試正好會踩進去：

  qwen3:4b 是推理模型，推理的 token 也計入 `eval_count`。所以一個太小的
  `num_predict` 可以**整個被推理吃掉**，回傳空的 `content` —— 而那與
  「部署壞了」在輸出上無法區分。run2 的 C3 canary 就是差一點被寫成
  「判準沒過」而其實是儀器壞掉。

所以請求帶 `num_predict=512`（推理模型也跑得完，見下）、`temperature=0`
（可重現），而**證據是四條斷言**，不是一條：

  1. HTTP 200 且 `content` 非空 —— 有回話
  2. `done_reason == "stop"` —— 模型自己講完了（**主要的那一道**，與模型無關）
  3. `eval_count > 0` —— 真的生成了東西
  4. `eval_count < num_predict` —— 沒有撞到上限

第 2 條不論原因是什麼都偵測得到；第 4 條是它的獨立複核（兩者都指向
「沒講完」，但來源不同）。

── 這裡原本送 `think: false`，2026-09-21 拿掉了（第七次同一個教訓）──

原本的設計是送 `think: false`、`num_predict=32`，理由寫的是「拿掉推理那個
變項、預算就不會被吃掉」。**這個理由在實測下是錯的。** 對同一個 prompt、
同一個 `qwen3:4b`，實測三次：

  A `think=false, num_predict=32`  → done_reason=length, eval_count=32,
                                     thinking=None, content=**推理過程**
  B `think=false, num_predict=256` → done_reason=stop,   eval_count=152,
                                     thinking=None, content=**推理過程**
  C 不帶 think, num_predict=256    → done_reason=stop,   eval_count=152,
                                     thinking=推理過程, content=**'ok'**

`think: false` **沒有關掉推理，只是把推理從 `thinking` 欄位搬到 `content`
欄位**：B 與 C 的 `eval_count` 一模一樣（152），預算照樣被吃掉。所以它非但
沒有達到原本宣稱的效果，還讓「`content` 非空」這條斷言**可以被推理前言單獨
滿足** —— 一個從頭到尾自言自語、從沒回答的模型也會過關。那正是這支探針存在
的理由，卻在它自己身上重現了一次。

不帶 `think` 反而更接近真實：open-webui 與 mem0 都不送這個參數，所以煙霧
測試現在送出的請求形狀**就是應用程式送出的形狀**。`num_predict` 也從 32
提到 512（實測推理要 152，給三倍餘裕；上限仍然存在，第 4 條照樣守著）。

**但真正該記的不是「參數調錯了」，而是「這個錯誤早就被記錄過」。** 同一個
`DECISIONS.md` 裡、三天前的 D-005 就寫著：

    API `think: false` → 假象。thinking 欄位確實清空，但模型仍在思考，
    內容改從 response 流出。它關掉的是標籤，不是行為。

而且它被列在「**已推翻的假設**（曾提出且已證偽，記錄以免重蹈）」底下，原文
就是「~~`think: false` 可關閉 thinking~~ —— 見上，只是把思考搬家」。D-005
甚至自己列了三個判準缺陷，其中第二個正是「用『thinking 欄位是否為空』判斷
—— 仍是間接指標，再次誤判」—— 那正是這裡原本的推理方式。

**「記錄以免重蹈」只有在你會去讀它的時候才成立。** 所以：

  · 要對外部系統（ollama、mem0、Open WebUI）的行為寫下任何宣稱之前，
    **先 grep 一次 DECISIONS.md**。這次只要搜 `think` 就會撞到那行結論。
  · 離線測試再密也照不到這一類錯誤 —— 它們測的是「實作有沒有照著我的
    理解走」，不是「我的理解對不對」。這次的缺陷通過了 32 個離線測試與
    53 個突變，是**第一次真的端到端跑**才抓到的。

── 為什麼「讀不到」與「答錯了」要分開 ────────────────────────

判準回三態，不是布林：`True`（過）／`False`（上游變了，答錯了）／`None`
（**讀不到，無法判定**）。D-018 的結束碼對應 0／1／2。這個區分不是潔癖：
`/api/ps` 讀不到與 `num_ctx` 沒生效，處置完全不同（前者要重讀，後者要改
設定），而兩者在「有沒有拿到我們要的值」這個問題上長得一模一样。

結束碼（D-018）：0 = 通過；1 = 上游變了／答錯了；2 = 環境（連不上 ollama、
模型沒拉、讀不到）；3 = **探針自己壞了**（參數錯誤、回應不是 JSON）。
"""

import json
import os
import sys
import urllib.error
import urllib.request

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INDETERMINATE = 2
EXIT_BROKEN = 3

# num_ctx 沒生效時**刻意**走 2 而不是 1（理由見 run_ctx）。它獨立成一個
# 常數，是因為那是一個設計決定而不是一個實作細節 —— 設計決定要有名字，
# 否則下一個讀到這裡的人只會看到一個「跟隔壁一樣」的 return。
EXIT_CTX_NOT_APPLIED = EXIT_INDETERMINATE

# 實測：qwen3:4b 對下面那個一行 prompt 也會先生成 **152 個 token 的推理**
# 才回答（三次量測見檔頭）。這是一個**量出來的數字，不是估的**，所以它值得
# 有一個名字 —— 離線測試拿它來確認預設預算沒有被調回一個註定不夠的值。
MEASURED_REASONING_TOKENS = 152

# 512 不是「隨便挑一個大數字」：它是 MEASURED_REASONING_TOKENS 的三倍餘裕。
# 原本的 32 就是被那 152 個 token 吃光的，而症狀（done_reason=length）與
# 「模型壞了」長得一模一樣。同時保留一個真實的上限，讓第 4 條斷言有意義。
DEFAULT_NUM_PREDICT = 512
# 生成要在很弱的機器上跑得完。這個 prompt 極短，但**推理模型還是會先生成
# ~150 個 token**，而新 VPS 可能只有 1 vCPU、實測 1.3–1.7 t/s（item 3）——
# 150 ÷ 1.3 ≈ 115 秒，300 秒只剩 2.6 倍餘裕。**逾時被讀成「模型壞了」**正是
# D-016 說的假失敗，所以放寬到 600 秒；真的要更長可以設 SMOKE_TIMEOUT。
DEFAULT_TIMEOUT = 600

# 煙霧測試用的 prompt。要求短、可重現、不引發長篇推理。
SMOKE_PROMPT = "Reply with exactly this word: ok"


# ─────────────────────────────────────────────────────────
# 純函式（離線可測，見 scripts/test_deploy_smoke_probe.py）
# ─────────────────────────────────────────────────────────


def normalize_model(name):
    """把模型名補成帶 tag 的形式，比對用。

    與 scripts/lib.sh:29-36 的 normalize_model() 同一個規則：只有「最後一個
    `:` 出現在最後一個 `/` 之後」才是 tag，否則可能是 registry 的埠號
    （localhost:5000/foo），那是名字的一部分。

    這不是理論問題 —— lib.sh 的註解記著一次實際發生的事：`rag_probe.sh bge-m3`
    在 bge-m3 **已經下載**的情況下報「尚未下載」，因為它拿使用者打的名字去
    比對 `ollama list` 帶 tag 的輸出。同一個錯誤在這裡的後果是「模型沒拉」
    的假失敗。
    """
    m = name or ""
    tail = m.rsplit("/", 1)[-1]
    if ":" in tail:
        return m
    return m + ":latest"


def smoke_verdict(response, num_predict):
    """判斷一次生成是不是**完整**的。回 (ok, reason)。

    ok 是三態：True 過、False 上游變了、**None 讀不到所以無法判定**。

    讀不到的狀況一律先判、而且回 None —— 絕不可以讓「欄位不存在」掉進
    「值不對」那一側。那會把一個儀器問題講成模型問題（D-016 的鏡像：
    假失敗）。
    """
    if not isinstance(response, dict):
        return None, "回應不是 JSON 物件（%s）" % type(response).__name__

    message = response.get("message")
    if not isinstance(message, dict):
        return None, "回應裡沒有 message 物件 —— 讀不到生成的內容"

    content = message.get("content")
    if not isinstance(content, str):
        return None, "message.content 不是字串 —— 讀不到生成的內容"

    # ── 主要的那一道：模型自己講完了嗎 ──
    # 欄位不存在時回 None 而不是 False：這個 ollama 版本沒送這個欄位，
    # 我們就**沒有套用到主要防護**，那是儀器的狀態，不是模型的狀態。
    if "done_reason" not in response:
        return None, (
            "回應裡沒有 done_reason —— 主要的那道防護套不上"
            "（這個 ollama 版本可能太舊）"
        )
    done_reason = response.get("done_reason")

    eval_count = response.get("eval_count")
    if not isinstance(eval_count, int):
        return None, (
            "讀不到 eval_count（拿到 %r）—— 無法判斷生成是不是被切斷的"
            % (eval_count,)
        )

    # ── 第 1 條：有回話 ──
    # 空 content 有兩種，訊息必須分開：撞到上限（上面那句）與模型真的回空的。
    if not content.strip():
        if done_reason == "length":
            return False, (
                "content 是空的，而且 done_reason=length —— 生成被 num_predict=%d "
                "切斷了。**被切斷的生成不能當成完整的量測**：若模型是推理模型，"
                "預算可能整個被推理吃掉" % num_predict
            )
        return False, (
            "content 是空的（done_reason=%r）—— 模型沒有回答任何東西"
            % done_reason
        )

    # ── 第 2 條：模型自己講完了（主要防線，與模型無關）──
    if done_reason != "stop":
        return False, (
            "done_reason=%r，不是 'stop' —— 這次生成沒有自然結束"
            "（eval_count=%d／num_predict=%d）。**沒有講完的生成不是量測結果**"
            % (done_reason, eval_count, num_predict)
        )

    # ── 第 3 條：真的生成了東西 ──
    if eval_count <= 0:
        return False, "eval_count=%d —— 沒有生成任何 token" % eval_count

    # ── 第 4 條：沒有撞到上限（第 2 條的獨立複核）──
    # 正常情況下 done_reason=stop 時不會走到這裡；走到這裡代表兩條斷言
    # 互相矛盾，那本身就是要講出來的資訊。
    if eval_count >= num_predict:
        return False, (
            "eval_count=%d 撞到 num_predict=%d 的上限，但 done_reason 說是 'stop' "
            "—— 這兩者矛盾，先查清楚再相信任何一邊"
            % (eval_count, num_predict)
        )

    return True, (
        "生成完整：content %d 字元、eval_count=%d（上限 %d）、done_reason=stop"
        % (len(content), eval_count, num_predict)
    )


def ctx_verdict(ps, model, expected):
    """比對 ollama **目前載入的那個模型**的 context_length。回 (ok, reason)。

    為什麼讀 /api/ps 而不是讀日誌：`OLLAMA_CONTEXT_LENGTH` 這個**名字**在
    ollama 0.34.2 的二進位檔裡存在（DECISIONS.md:2465-2468 用 `grep -a -o`
    證實過），但沒有任何人證明過它**生效**。所以要斷言結果，不是斷言設定。
    而日誌是這裡最差的儀器 —— scripts/ollama_log_corroboration.sh 記著三種
    無聲的失敗模式（`--since` 靜默回 0 行、中段損壞、回一個看起來很合理的
    舊窗口），它存在就是因為那件事花掉過半小時。

    /api/ps 的 context_length 是**現在生效的 num_ctx**，不是模型的最大值
    —— 與 mem0_add_cost_probe.py 的 _ps_lookup() 讀的是同一個欄位。
    """
    if not isinstance(ps, dict):
        return None, "回應不是 JSON 物件（%s）—— 無法判定 num_ctx" % type(ps).__name__

    models = ps.get("models")
    if not isinstance(models, list):
        return None, "回應裡沒有 models 清單 —— 無法判定 num_ctx"

    want = normalize_model(model)
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("model") or ""
        if normalize_model(name) != want:
            continue
        got = entry.get("context_length")
        if not isinstance(got, int):
            return None, (
                "模型 %s 有載入，但讀不到 context_length（拿到 %r）"
                "—— 無法判定 num_ctx" % (model, got)
            )
        if got == expected:
            return True, "context_length=%d，與要求的 --num-ctx 相符" % got
        return False, (
            "context_length=%d，不是要求的 %d —— 這個設定**沒有生效**。"
            "堆疊本身是好的、可以用，但 mem0 的抽取 prompt 仍會被截斷"
            "（D-027：需要 >= 8101 才完整）"
            % (got, expected)
        )


    return None, (
        "模型 %s 不在 /api/ps 的清單裡（目前載入的是 %r）—— 模型可能已被卸載，"
        "或名字對不上，所以**量不到** num_ctx"
        % (model, [e.get("name") for e in models if isinstance(e, dict)])
    )


# ─────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────


def _base_url():
    return os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434").rstrip("/")


def _request(path, payload, timeout):
    """回 (status, parsed)。連不上時回 (None, {"error": ...})。"""
    url = _base_url() + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # ollama 的錯誤訊息在 body 裡（`{"error": "model ... not found"}`），
        # 不在 HTTP 狀態列上。吃掉它會讓「模型沒拉」變成一句沒有資訊的
        # 「HTTP 404」。
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"error": body[:400]}
    except (urllib.error.URLError, OSError) as e:
        return None, {"error": str(e)}
    except ValueError as e:
        return None, {"error": "回應不是 JSON：%s" % e}


def _err_text(parsed):
    if isinstance(parsed, dict):
        return str(parsed.get("error") or parsed)[:400]
    return str(parsed)[:400]


def run_smoke(model, num_predict, timeout):
    # **刻意不送 `think`。** 原本送 `think: false` 並附一段「拿掉推理那個
    # 變項」的說明，但實測顯示它只是把推理搬進 `content`、預算照樣被吃掉
    # （三次量測見檔頭）。不送的話既貼近應用程式的真實請求，`content` 也是
    # 乾淨的答案。連帶地那個「舊版 ollama 不認得 think → 拿掉重試」的分支
    # 也就沒有存在的理由了。
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
        "stream": False,
        "options": {"temperature": 0, "num_predict": num_predict},
    }

    status, parsed = _request("/api/chat", payload, timeout)

    if status is None:
        return EXIT_INDETERMINATE, (
            "連不上 ollama（%s）：%s\n"
            "  → 這是環境問題，不是模型問題。確認 ollama 容器在跑、"
            "且與本容器同網路。" % (_base_url(), _err_text(parsed))
        )

    if status >= 400:
        low = _err_text(parsed).lower()
        if "not found" in low or "no such model" in low:
            return EXIT_INDETERMINATE, (
                "ollama 說找不到模型 %s（HTTP %d）：%s\n"
                "  → 模型還沒拉，或名字拼錯。這是環境狀態，不是模型答錯。"
                % (model, status, _err_text(parsed))
            )
        return EXIT_BROKEN, (
            "ollama 回了 HTTP %d：%s\n"
            "  → 這不是「模型答錯」，是請求本身有問題（參數或 API 形狀）。"
            % (status, _err_text(parsed))
        )

    ok, reason = smoke_verdict(parsed, num_predict)
    if ok is True:
        return EXIT_PASS, reason
    if ok is False:
        return EXIT_FAIL, reason
    return EXIT_INDETERMINATE, reason


def run_ctx(model, expected, timeout):
    status, parsed = _request("/api/ps", None, timeout)
    if status is None:
        return EXIT_INDETERMINATE, (
            "連不上 ollama（%s）：%s" % (_base_url(), _err_text(parsed))
        )
    if status >= 400:
        return EXIT_BROKEN, "ollama 回了 HTTP %d：%s" % (status, _err_text(parsed))

    ok, reason = ctx_verdict(parsed, model, expected)
    if ok is True:
        return EXIT_PASS, reason
    if ok is False:
        # 刻意**不是** 1：部署本身成功了、堆疊可以用，這是一個真實的量測結果，
        # 不是判準沒過（D-018 的 1 是「上游變了」）。但也絕對不是 0 ——
        # 一個我們設了、卻沒有生效的伺服器端開關，正是這個專案一直踩到的
        # 「看起來已經套用、其實沒有」。所以是 2：環境／要求的狀態未成立。
        return EXIT_CTX_NOT_APPLIED, reason
    return EXIT_INDETERMINATE, reason


def _usage():
    return (
        "用法：\n"
        "  python3 - smoke <model> [num_predict]   （預設 num_predict=%d）\n"
        "  python3 - ctx   <model> <expected_ctx>\n"
        % DEFAULT_NUM_PREDICT
    )


def _bad_args(msg):
    print(msg, file=sys.stderr)
    return EXIT_BROKEN  # 參數錯誤是「這支腳本自己壞了」，不是「環境無法判定」


def main(argv):
    if len(argv) < 2:
        return _bad_args(_usage())

    section = argv[1]
    timeout = int(os.environ.get("SMOKE_TIMEOUT", DEFAULT_TIMEOUT))

    if section == "smoke":
        if len(argv) < 3:
            return _bad_args(_usage())
        model = argv[2]
        try:
            num_predict = int(argv[3]) if len(argv) > 3 else DEFAULT_NUM_PREDICT
        except ValueError:
            return _bad_args("num_predict 必須是整數：%r" % argv[3])
        if num_predict <= 0:
            return _bad_args("num_predict 必須是正整數：%d" % num_predict)
        rc, reason = run_smoke(model, num_predict, timeout)

    elif section == "ctx":
        if len(argv) < 4:
            return _bad_args(_usage())
        model = argv[2]
        try:
            expected = int(argv[3])
        except ValueError:
            return _bad_args("expected_ctx 必須是整數：%r" % argv[3])
        rc, reason = run_ctx(model, expected, timeout)

    else:
        return _bad_args("未知的 section：%r\n%s" % (section, _usage()))

    label = {EXIT_PASS: "PASS", EXIT_FAIL: "FAIL", EXIT_INDETERMINATE: "INDET"}.get(
        rc, "BROKEN"
    )
    print("[%s] %s" % (label, reason))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
