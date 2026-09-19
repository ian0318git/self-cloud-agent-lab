#!/usr/bin/env python3
"""OpenAI-compatible API 的相容性探針 —— 把「可替換的 LLM Runtime」變成可驗證的主張。

為什麼需要這支腳本，而不是「換了再跑跑看」：

  本專案的目標架構是

      Open WebUI / RAG / MCP / LangGraph / Agent
                      ↓
            OpenAI-compatible API
            ↙                      ↘
        Ollama                    Endpoint
        （本機 / VPS）             （Kaggle T4×2 或其他 GPU）
                                      ↓
                                  llama.cpp / GGUF

  這個架構的**全部價值**建立在一個主張上：上層只說 OpenAI 協定，不知道底下是誰。
  這個主張若不被機械檢查，它就只是一句話 —— 而本專案已經吃過太多次
  「文件寫了、沒有人驗」的虧（D-014：教訓記下來了，卻沒有變成判準）。

  所以這支探針**只說 OpenAI 協定**：`/v1/models`、`/v1/chat/completions`、
  `/v1/embeddings`。它不碰任何 Ollama 專屬端點、不使用任何廠商 SDK。
  判準因此很簡單：**任何能通過它的 runtime 都能接手這個平台；任何需要為它
  改上層程式碼的 runtime，都不能。**

為什麼現在就能驗：Ollama 本身也提供 OpenAI-compatible API（`/v1/...`）。
把探針指向 `http://ollama:11434/v1` 跑的是與 Endpoint、vLLM **完全相同**的
程式路徑 —— 不需要 Kaggle 帳號就能先把介面這一層釘死。等到真的有 GPU
runtime 時，換的是 `--base-url`，不是程式碼。

用法：
  python3 scripts/probe_openai.py --base-url http://ollama:11434/v1
  python3 scripts/probe_openai.py --base-url http://ollama:11434/v1 --model qwen3:4b
  python3 scripts/probe_openai.py --base-url https://xxx.trycloudflare.com/v1 \\
      --api-key "$ENDPOINT_API_KEY" --model qwen2.5:7b

  通常不需要直接呼叫，用包裝腳本即可：
      bash scripts/probe-openai.sh http://ollama:11434/v1

結束碼：0 = 全部通過；1 = 有檢查未通過；2 = 無法判定（連不上，不是「壞掉」）

安全：本腳本**不印出 API key**，只印「已提供／未提供」。金鑰請由命令列或
      環境變數傳入，不要寫進版控（.env 已被 .gitignore 排除）。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NS = 1e9

# 狀態必須分得開（同 verify_api.py 的 tri()）：
# 未執行 / 執行失敗 / 無法判定 / 通過 / 未通過，另加「警告」。
# 「警告」不是「未通過」—— 併在一起會讓一個正常運作的 runtime 被講成
# 不能接手平台（假失敗），本專案已經吃過太多次這個虧。
PASS = "通過"
FAIL = "未通過"
WARN = "警告"
SKIP = "未執行"
UNKNOWN = "無法判定"


def _is_private_host(base):
    """端點是否在私有網路上。

    為什麼這件事決定「沒有認證」是警告還是失敗：
      Ollama 沒有任何認證機制（D-003），這在**同一個 docker 網路內**是可以
      的 —— 它根本沒有對外發布埠。但同一個設定搬到公開網址上，就等於把
      推論服務送給全世界。

      「同一個設定，安全或危險取決於機器在哪」—— check-exposure.sh 已經
      處理過同一個形狀，這裡沿用同一條規則。理由也一樣：一個在安全情境下
      也照樣報紅的檢查，最後會被忽略，而被忽略的檢查等於沒有檢查。
    """
    try:
        host = (urllib.parse.urlsplit(base).hostname or "").lower()
    except Exception:  # noqa: BLE001 - 網址解析失敗時保守地當成公開
        return False
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if "." not in host:          # docker service name，例如 ollama
        return True
    if host.endswith(".local"):
        return True
    if re.match(r"^(10\.|192\.168\.|127\.|169\.254\."
                r"|172\.(1[6-9]|2[0-9]|3[01])\.)", host):
        return True
    if host.startswith(("fc", "fd", "fe80")):   # IPv6 私有 / link-local
        return True
    return False


def _request(url, payload=None, api_key=None, timeout=120, accept=None,
             stream=False):
    """回傳 (HTTP 狀態碼, 內容)。連不上時狀態碼為 None。

    只用標準庫 —— 探針若依賴廠商 SDK，它驗到的就是 SDK 的相容性，
    不是協定的相容性。
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if accept:
        req.add_header("Accept", accept)
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        if stream:
            return resp.status, resp
        with resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 讀不到 body 不該蓋掉狀態碼
            pass
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - 連線層的錯全部歸為「無法判定」
        return None, f"{type(exc).__name__}: {exc}"


def _short(text, limit=200):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class Probe:
    """收集檢查結果。每一項都是四種狀態之一，不可互相汙染。"""

    def __init__(self):
        self.results = []

    def add(self, name, state, detail=""):
        self.results.append((name, state, detail))
        mark = {PASS: "✓", FAIL: "✗", WARN: "!", SKIP: "·", UNKNOWN: "?"}[state]
        color = {PASS: "\033[0;32m", FAIL: "\033[0;31m", WARN: "\033[0;33m",
                 SKIP: "\033[0m", UNKNOWN: "\033[0;33m"}[state]
        print(f"  {color}{mark}\033[0m {name:<28} {state}"
              + (f"  {detail}" if detail else ""), flush=True)

    def failed(self):
        return any(s == FAIL for _, s, _ in self.results)

    def any_unknown(self):
        return any(s == UNKNOWN for _, s, _ in self.results)


def probe(base, api_key, model, timeout):
    p = Probe()
    model_id = model

    # ── 1. GET /v1/models ───────────────────────────────
    # 第一個檢查就決定了後面能不能跑：列不出模型，後面都是空談。
    status, body = _request(f"{base}/models", api_key=api_key, timeout=timeout)
    if status is None:
        p.add("GET /v1/models", UNKNOWN, _short(body, 120))
        return p, model_id, "連不上"
    # ⚠ HTTP 回應碼**不是**「無法判定」。
    #
    # 一個回 404 的端點是**明確地回答**「我沒有這個路徑」，不是「我測不出來」。
    # 第一版把這裡也標成 aborted，於是探針印出「✗ 未通過 HTTP 404」之後，
    # 總結卻說「無法判定 —— 端點拒絕」並回傳 2 —— 呼叫端因此叫使用者去查
    # 網路，而網路根本沒問題。那是**假失敗**，也正是 D-016 的同一形狀：
    # 「無法判定」與「未通過」被併在一起。差別只在這次發生在探針自己身上。
    #
    # 判準：只有**連不上**（status is None）才叫無法判定。端點有回應就是
    # 有結論 —— 回非 200 就是未通過。
    if status != 200:
        p.add("GET /v1/models", FAIL, f"HTTP {status} {_short(body, 120)}")
        return p, model_id, None

    try:
        models = [m.get("id") for m in (json.loads(body).get("data") or [])]
        models = [m for m in models if m]
    except Exception as exc:  # noqa: BLE001 - 回應不是預期的 JSON 形狀
        p.add("GET /v1/models", FAIL, f"回應不是 OpenAI 格式：{exc}")
        return p, model_id, None

    p.add("GET /v1/models", PASS, f"{len(models)} 個模型")

    if not model_id:
        if not models:
            p.add("POST /v1/chat/completions", SKIP, "端點未列出任何模型")
            return p, model_id, "沒有模型"
        model_id = models[0]
    print(f"      使用模型：{model_id}", flush=True)

    # ── 2. 非串流 chat completion ───────────────────────
    # 這是上層真正會走的路徑。要求一句極短的回應以縮短等待。
    t0 = time.time()
    status, body = _request(
        f"{base}/chat/completions",
        payload={
            "model": model_id,
            "messages": [{"role": "user", "content": "回答一個字：好"}],
            "max_tokens": 16,
            "stream": False,
        },
        api_key=api_key,
        timeout=timeout,
    )
    wall = time.time() - t0
    if status is None:
        p.add("POST /v1/chat/completions", UNKNOWN, _short(body, 120))
    elif status != 200:
        p.add("POST /v1/chat/completions", FAIL,
              f"HTTP {status} {_short(body, 160)}")
    else:
        try:
            choice = json.loads(body)["choices"][0]
            content = (choice.get("message") or {}).get("content") or ""
            if content.strip():
                p.add("POST /v1/chat/completions", PASS,
                      f"{wall:.1f}s，回應 {_short(content, 40)}")
            else:
                # 空 content 不是「通過」—— 上層拿到的是空字串，
                # 但 HTTP 是 200，呼叫端會以為成功（D-014 的假通過形狀）。
                p.add("POST /v1/chat/completions", FAIL,
                      f"{wall:.1f}s，HTTP 200 但 content 為空")
        except Exception as exc:  # noqa: BLE001
            p.add("POST /v1/chat/completions", FAIL,
                  f"回應不是 OpenAI 格式：{exc} {_short(body, 120)}")

    # ── 3. 串流（SSE）───────────────────────────────────
    # Open WebUI 預設用串流顯示。缺了它，上層的體感會退化。
    status, resp = _request(
        f"{base}/chat/completions",
        payload={
            "model": model_id,
            "messages": [{"role": "user", "content": "回答一個字：好"}],
            "max_tokens": 16,
            "stream": True,
        },
        api_key=api_key,
        timeout=timeout,
        accept="text/event-stream",
        stream=True,
    )
    if status is None:
        p.add("串流（SSE）", UNKNOWN, _short(resp, 120))
    elif status != 200:
        p.add("串流（SSE）", FAIL, f"HTTP {status}")
    else:
        chunks, done, raw = 0, False, []
        try:
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        break
                    if payload:
                        chunks += 1
                        if len(raw) < 3:
                            raw.append(payload)
        finally:
            resp.close()
        if chunks and done:
            p.add("串流（SSE）", PASS, f"{chunks} 個 chunk，收到 [DONE]")
        elif chunks:
            # 有 chunk 但沒有 [DONE]：多數前端仍能顯示，但串流沒有正常結束。
            p.add("串流（SSE）", FAIL, f"{chunks} 個 chunk，但未收到 [DONE]")
        else:
            p.add("串流（SSE）", FAIL, f"未收到任何 SSE chunk：{_short(str(raw), 120)}")

    # ── 4. 認證是否真的被強制 ───────────────────────────
    # 只有在「有提供金鑰」時才檢查 —— 沒提供金鑰時問「沒金鑰能不能過」
    # 沒有意義，只會製造一則沒有資訊的警告。
    #
    # 這一項的判定取決於端點在私有網路還是公開網址（見 _is_private_host）：
    # 判準不是「有沒有認證」，而是「**這個位置**沒有認證能不能接受」。
    if api_key:
        status, body = _request(
            f"{base}/chat/completions",
            payload={"model": model_id,
                     "messages": [{"role": "user", "content": "hi"}],
                     "max_tokens": 4},
            api_key=None,          # 刻意不帶金鑰
            timeout=timeout,
        )
        if status == 200:
            if _is_private_host(base):
                p.add("未帶金鑰應被拒絕", WARN,
                      "端點不檢查金鑰 —— 私有網路上可接受（Ollama 無認證，D-003），"
                      "但不可原樣帶到公開網址")
            else:
                p.add("未帶金鑰應被拒絕", FAIL,
                      "**公開端點**接受了沒有 Authorization 的請求 —— "
                      "任何知道這個網址的人都能用你的推論服務")
        elif status in (401, 403):
            p.add("未帶金鑰應被拒絕", PASS, f"HTTP {status}")
        elif status is None:
            p.add("未帶金鑰應被拒絕", UNKNOWN, _short(body, 100))
        else:
            p.add("未帶金鑰應被拒絕", UNKNOWN, f"HTTP {status}（非 401/403）")
    else:
        p.add("未帶金鑰應被拒絕", SKIP, "未提供金鑰")

    # ── 5. embeddings（資訊性，不列為失敗）───────────────
    # 這一項刻意**不**計入通過與否：RAG 的嵌入引擎可以與聊天引擎是**不同**
    # 的 runtime（本專案目前就是 ollama 的 qwen3-embedding:0.6b，見 D-013）。
    #
    # 但要記得：換掉嵌入模型會讓既有向量全部失效 —— 向量維度與語意空間都
    # 變了。所以搬家時「聊天換到 GPU、嵌入留在原處」是可行的，
    # 「嵌入也換一顆」則必須重建整個向量庫。這是這一項要提醒的事。
    status, body = _request(
        f"{base}/embeddings",
        payload={"model": model_id, "input": "測試"},
        api_key=api_key,
        timeout=timeout,
    )
    if status is None:
        p.add("POST /v1/embeddings（資訊）", UNKNOWN, _short(body, 100))
    elif status == 200:
        try:
            vec = json.loads(body)["data"][0]["embedding"]
            p.add("POST /v1/embeddings（資訊）", PASS, f"維度 {len(vec)}")
            print(f"      注意：換嵌入模型會使既有向量失效（D-013），"
                  f"維度 {len(vec)} 必須與向量庫一致", flush=True)
        except Exception:  # noqa: BLE001
            p.add("POST /v1/embeddings（資訊）", UNKNOWN, "回應格式非預期")
    else:
        # 不支援不是缺陷 —— 這裡只記錄「這個 runtime 不提供嵌入」。
        p.add("POST /v1/embeddings（資訊）", SKIP,
              f"HTTP {status}（此 runtime 不提供，嵌入可續用其他 runtime）")

    return p, model_id, None


def main(argv):
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible API 相容性探針")
    parser.add_argument("--base-url", required=True,
                        help="含 /v1 的基底網址，例如 http://ollama:11434/v1")
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""),
                        help="Bearer token。預設讀 OPENAI_API_KEY。不會被印出。")
    parser.add_argument("--model", default="", help="留空則用 /v1/models 的第一個")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    print("=" * 62)
    print(" OpenAI-compatible API 相容性探針")
    print("=" * 62)
    print(f" 端點：{base}")
    # 只說有沒有，不說長度、不說前綴 —— 金鑰的任何片段都不該出現在輸出裡，
    # 因為輸出會被貼進 issue、日誌與 DECISIONS.md。
    print(f" API key：{'已提供' if args.api_key else '未提供'}")
    print("=" * 62)
    print()

    p, model_id, aborted = probe(base, args.api_key, args.model, args.timeout)

    print()
    print("=" * 62)
    print(" 總結")
    print("=" * 62)
    passed = sum(1 for _, s, _ in p.results if s == PASS)
    print(f"  通過 {passed} / {len(p.results)} 項")

    warnings = [(n, d) for n, s, d in p.results if s == WARN]
    if warnings:
        print()
        for name, detail in warnings:
            print(f"  ! {name}：{detail}")

    if aborted:
        print()
        print(f"  結論：無法判定 —— {aborted}。")
        print("        這不是「這個 runtime 壞掉」，是「這次測不出來」。")
        print("        先確認網址、網路與服務狀態，再重跑。")
        return 2

    if p.failed():
        print()
        print("  結論：未通過 —— 這個 runtime 目前不能直接接手本平台。")
        print("        未通過的項目就是上層會踩到的洞，請先修它。")
        return 1

    print()
    print("  結論：通過 —— 這個 runtime 可以直接接手本平台的上層功能。")
    print("        「直接接手」的意思是：只換 --base-url，不改任何上層程式碼。")
    if warnings:
        print("        上方警告不影響相容性，但它是一項**安全**叮嚀，搬家前要處理。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
