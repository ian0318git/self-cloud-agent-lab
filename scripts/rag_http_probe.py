#!/usr/bin/env python3
"""RAG 四項的 **HTTP 路徑** 驗證 —— 走應用程式自己的 API，不是自己組裝的檢索。

    「從 UI 上傳文件的那條路，也能動嗎？」

`rag_grounding_probe.py` 證明的是「檢索這條鏈在目前設定下能動」，但它把攝入
那一段**由探針自己組裝**（自己呼叫切塊器、嵌入函式、向量客戶端）。HANDBOOK
「Phase 2: RAG」清單問的是另一件事：**建立知識庫 → 上傳文件 → 問文件裡的
事 → 問文件裡沒有的事**。這一支走的就是那條路，全程經過 HTTP：

    POST /api/v1/knowledge/create           ← UI 的「建立知識庫」
    POST /api/v1/files/?process=true        ← UI 的「上傳文件」
    POST /api/v1/knowledge/{id}/file/add    ← UI 的「把文件加進知識庫」
    POST /api/chat/completions              ← UI 的「問問題」

── 為什麼要有 API 金鑰，以及它為什麼不進命令列 ────────────────

這條路需要一個已登入的使用者，所以需要一組 API 金鑰。金鑰由 **stdin** 讀入：

  · **不進 argv** —— 同機器上任何人 `ps` 都看得到 argv。
  · **不進環境變數** —— `docker compose exec -e` 會把它放進主機端
    `docker compose` 的命令列，同樣是 `ps` 看得到的。
  · **不回印** —— 任何訊息、任何 `--json` 輸出都不含金鑰。
  · **不比對、不落地** —— 用完就留在這次執行的記憶體裡。

── 三個實測（讀原始碼）確認的坑，寫在這裡免得下一個人重踩 ──────

1. **`files` 在 body 的頂層，不是 `metadata` 裡。**
   前端送的是 `{"model":…, "messages":[…], "files":[…]}`。伺服器端
   `main.py` 用 `form_data.get('files')` 組出內部的 metadata，接著
   `form_data['metadata'] = metadata` —— **覆寫**掉呼叫者送來的 metadata。

   所以照著「middleware.py 讀的是 `metadata.files`」去組請求、把 files 放進
   `metadata`，它會被**靜默丟掉**：HTTP 200、答案正常、只是完全沒有檢索。
   正向題於是失敗，而看起來像模型不聽話。這裡照**伺服器實際讀的位置**送。

2. **文件必須「處理完」才能加進知識庫。**
   `/knowledge/{id}/file/add` 會檢查 `file.data`，沒有就回
   `FILE_NOT_PROCESSED`。上傳預設是**背景**處理（`process_in_background=true`），
   回來的時候還沒處理完。所以這裡用 `process=true&process_in_background=false`
   讓它在回應之前處理完 —— 加進知識庫成功，本身就是「處理完了」的機械證據。

3. **`chat_id` 給 `temporary:` 開頭，才不會在資料庫留下測試對話。**
   不給 chat_id 的話伺服器會自己生一個 uuid，那是「會被存檔」的形式
   （`is_saved_chat_id`）。測試不該在使用者的聊天紀錄裡留東西。

── 這支分得出什麼、分不出什麼 ────────────────────────────────

分得出：**檢索到的內容有沒有被用上**。要問的值全都是捏造的（見
`rag_grounding_probe.DOCUMENT`），模型不可能從參數記憶裡答對 —— 答對只可能
來自檢索。（這一點不因走 HTTP 而改變。）

分不出：**「檢索挑錯段落」與「模型沒用檢索到的內容」**。非串流的 chat
completion 回應**不含** sources（那些走 WebSocket 事件，API 金鑰看不到）。
要分這兩者，跑 `rag-grounding` 那一支。兩支一起跑才是完整的一句話。

結束碼同 `rag_grounding_probe.py`（D-016）：0 通過、1 未通過、2 無法判定、
3 探針自己壞掉。

用法（容器內）：
    printf '%s' "$KEY" | python3 rag_http_probe.py --model qwen2.5:3b
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

# 這支被 `docker compose cp` 到 /tmp 再執行，sys.path[0] 會是 /tmp，
# `rag_grounding_probe.py` 也在那裡（外層的 .sh 會一起複製）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 評分邏輯**不重寫**。`_ABSENT_RE` 那份樣式是實機跑了四次假失敗才收斂的，
# 抄一份過來等於保證兩份會分岔 —— 而分岔的那一天，兩支腳本會對同一個回應
# 給出不同答案。`_short` 雖然是私有名，但它是同一專案內的共用小工具。
from rag_grounding_probe import (  # noqa: E402
    DOCUMENT,
    FAIL,
    PASS,
    QUESTIONS,
    SKIP,
    UNKNOWN,
    Probe,
    _short,
    grade_absent,
    grade_grounded,
)

DEFAULT_BASE = "http://localhost:8080"
DEFAULT_TIMEOUT = 300
SHORT_TIMEOUT = 60

# 上傳的檔名用 ASCII。multipart 的 Content-Disposition 標頭塞非 ASCII 檔名
# 要靠 RFC 2231 編碼，各家實作鬆緊不一 —— 這條變數與要驗的東西無關，
# 不該讓它有機會變成一個假失敗。（文件**內容**照樣是中文。）
FILENAME = "typhoon-attendance-policy.md"
CONTENT_TYPE = "text/markdown"


# ── HTTP ────────────────────────────────────────────────

def _decode(body):
    """空 body → None；JSON → dict；其他 → 原樣字串（錯誤訊息常常不是 JSON）。"""
    text = (body or b"").decode("utf-8", "replace")
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def request(base, path, key, *, json_body=None, raw=None, content_type=None,
            timeout=SHORT_TIMEOUT, method=None):
    """回傳 (status, body)。連線層的錯誤原樣往外丟，由呼叫端分類。

    金鑰只出現在 Authorization 標頭，不進任何回傳值、不進任何訊息。
    """
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type

    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _decode(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())


def build_multipart(filename, content, content_type=CONTENT_TYPE):
    """組出 multipart/form-data，欄位名固定 `file`（files.py 的 File(...)）。

    回傳 (content_type 標頭值, body bytes)。抽成函式是為了能離線測試 ——
    這段純字串組裝，錯了會在伺服器端變成 422，而 422 的訊息不會說出
    「你的 boundary 少了一個 --」。
    """
    boundary = "----raghttp" + uuid.uuid4().hex
    parts = []
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    parts.append(content.encode("utf-8"))
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def build_chat_body(model, question, knowledge_id, name):
    """組出聊天請求。**`files` 在頂層** —— 理由見檔頭第 1 點。

    不用參數化的 dict 產生器，是為了讓「files 在頂層」這件事在測試裡
    是一行斷言，而不是一個慣例。
    """
    return {
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "files": [
            {
                "type": "collection",
                "id": knowledge_id,
                "name": name,
                "collection_name": knowledge_id,
            }
        ],
        # 不給的話伺服器自己生 uuid，那是會被存檔的形式（檔頭第 3 點）。
        "chat_id": f"temporary:raghttp-{uuid.uuid4()}",
        "stream": False,
    }


def extract_answer(body):
    """從 chat completion 回應裡取出助手回覆的文字。取不到回空字串。"""
    if not isinstance(body, dict):
        return ""
    for choice in body.get("choices") or []:
        message = choice.get("message") or {}
        content = message.get("content")
        if content:
            return content
    return ""


def find_sources(body):
    """回應裡若有 sources 就撈出來（非串流通常沒有 —— 有就順手用）。"""
    if not isinstance(body, dict):
        return []
    sources = body.get("sources")
    if isinstance(sources, list):
        return sources
    for choice in body.get("choices") or []:
        s = (choice.get("message") or {}).get("sources")
        if isinstance(s, list):
            return s
    return []


# ── 判定輔助 ────────────────────────────────────────────

def auth_problem(status):
    """401 是「金鑰不對」，不是「RAG 不對」。這兩件事的處置完全不同。"""
    return status in (401, 403)


def http_detail(body):
    if isinstance(body, dict):
        return body.get("detail") or body.get("error") or body
    return body


# ── 主流程 ──────────────────────────────────────────────

def run(base, key, model, timeout=DEFAULT_TIMEOUT, keep=False):
    p = Probe()
    created = {}          # 清理用；只有在真的建立之後才會有 key
    # 清理結果要在 finally 裡放進同一份報告，所以先留位置。
    cleanup_rows = []

    try:
        # ── 0. 金鑰與身分 ──────────────────────────────
        t0 = time.time()
        status, body = request(base, "/api/v1/auths/", key)
        if auth_problem(status) or status != 200:
            p.add("API 金鑰（GET /api/v1/auths/）", UNKNOWN,
                  f"HTTP {status}：{_short(str(http_detail(body)), 70)}"
                  " —— 這是設定問題，不是 RAG 的結論")
            return p
        role = (body or {}).get("role", "?")
        p.add("API 金鑰（GET /api/v1/auths/）", PASS,
              f"身分 role={role}｜{time.time() - t0:.1f}s")

        # 模型必須在 /api/models 裡，否則 chat 會回 'Model not found'。
        # 先驗，是為了讓「模型沒開」不要偽裝成「RAG 壞了」。
        status, body = request(base, "/api/models", key)
        ids = [m.get("id") for m in (body or {}).get("data", [])] if isinstance(body, dict) else []
        if status != 200:
            p.add("模型可用性（GET /api/models）", UNKNOWN,
                  f"HTTP {status}：{_short(str(http_detail(body)), 70)}")
            return p
        if model not in ids:
            p.add("模型可用性（GET /api/models）", UNKNOWN,
                  f"{model} 不在模型清單中（共 {len(ids)} 個）"
                  " —— 先讓它出現在 UI 的模型選單裡")
            return p
        p.add("模型可用性（GET /api/models）", PASS, f"{model} 可用")

        # ── 1. 建立知識庫 ──────────────────────────────
        kb_name = f"rag-http-probe-{uuid.uuid4().hex[:12]}"
        status, body = request(base, "/api/v1/knowledge/create", key, json_body={
            "name": kb_name,
            "description": "rag-http-probe 的暫時知識庫（跑完會刪除）",
        })
        kid = (body or {}).get("id") if isinstance(body, dict) else None
        if status != 200 or not kid:
            p.add("1 建立知識庫", FAIL,
                  f"HTTP {status}：{_short(str(http_detail(body)), 80)}")
            return p
        created["knowledge_id"] = kid
        p.add("1 建立知識庫", PASS, kid)

        # ── 2. 上傳文件，並加進知識庫 ──────────────────
        ct, payload = build_multipart(FILENAME, DOCUMENT)
        t0 = time.time()
        status, body = request(
            base, "/api/v1/files/?process=true&process_in_background=false", key,
            raw=payload, content_type=ct, timeout=timeout,
        )
        fid = (body or {}).get("id") if isinstance(body, dict) else None
        if status != 200 or not fid:
            p.add("2 上傳文件（POST /api/v1/files/）", FAIL,
                  f"HTTP {status}：{_short(str(http_detail(body)), 80)}")
            return p
        created["file_id"] = fid

        status, body = request(base, f"/api/v1/knowledge/{kid}/file/add", key,
                               json_body={"file_id": fid})
        if status != 200:
            p.add("2 上傳文件（POST /api/v1/files/）", FAIL,
                  f"加入知識庫失敗 HTTP {status}："
                  f"{_short(str(http_detail(body)), 70)}")
            return p

        # 回讀：知識庫自己說它有幾個檔案。這個專案量過太多次
        # 「呼叫沒拋例外」與「狀態真的改了」是兩件事。
        status, body = request(base, f"/api/v1/knowledge/{kid}", key)
        files = (body or {}).get("files") if isinstance(body, dict) else None
        if status != 200 or not files:
            p.add("2 上傳文件（POST /api/v1/files/）", FAIL,
                  f"回讀知識庫失敗或檔案數為 0（HTTP {status}）")
            return p
        p.add("2 上傳文件（POST /api/v1/files/）", PASS,
              f"已處理並加入知識庫｜{len(files)} 個檔案｜{time.time() - t0:.1f}s")

        # ── 3. 問文件裡有的事（正向題）─────────────────
        q = QUESTIONS["positive"]
        t0 = time.time()
        status, body = request(base, "/api/chat/completions", key, timeout=timeout,
                               json_body=build_chat_body(model, q["q"], kid, kb_name))
        if status != 200:
            p.add(q["label"], FAIL,
                  f"HTTP {status}：{_short(str(http_detail(body)), 80)}")
            return p
        answer = extract_answer(body)
        elapsed = time.time() - t0
        if not answer:
            # 空回覆不等於答錯 —— 可能是 token 全用在自己的思考上。
            p.add(q["label"], UNKNOWN,
                  f"回應沒有內容｜{elapsed:.1f}s｜回應鍵：{sorted(body)[:6] if isinstance(body, dict) else type(body).__name__}")
            return p
        p.add(q["label"], PASS if grade_grounded(answer, q["must_match"]) else FAIL,
              f"答出文件裡的捏造值｜{elapsed:.1f}s｜{_short(answer)}")

        # ── 4. 問文件裡沒有的事（反向題）───────────────
        q = QUESTIONS["negative"]
        t0 = time.time()
        status, body = request(base, "/api/chat/completions", key, timeout=timeout,
                               json_body=build_chat_body(model, q["q"], kid, kb_name))
        if status != 200:
            p.add(q["label"], FAIL,
                  f"HTTP {status}：{_short(str(http_detail(body)), 80)}")
            return p
        answer = extract_answer(body)
        elapsed = time.time() - t0
        if not answer:
            p.add(q["label"], UNKNOWN,
                  f"回應沒有內容｜{elapsed:.1f}s")
            return p
        ok, why = grade_absent(answer)
        p.add(q["label"], PASS if ok else FAIL,
              f"{why}｜{elapsed:.1f}s｜{_short(answer)}")

        return p
    finally:
        # 清理一律發生，包含上面任何一個 `return p` 提前結束的路徑。
        # 清理的結果**只加不減**：它不能把「未通過」翻成「通過」，也不該把
        # 「無法判定」翻成「未通過」—— rag_grounding_probe 的清理那段就是
        # 因為把 NotFoundError 當成失敗，從後門把 D-016 分開的兩種狀態重新
        # 合併起來（見該檔 cleanup_collection 的說明）。
        cleanup_rows.extend(cleanup(base, key, created, keep=keep))
        for name, state, detail in cleanup_rows:
            p.add(name, state, detail)


def cleanup(base, key, created, keep=False):
    """刪掉這輪建立的東西，並且**回讀確認**。回傳列，不印東西。"""
    if keep:
        ids = "、".join(f"{k}={v}" for k, v in created.items())
        return [("清理", SKIP, f"--keep：保留 {ids}（請自行刪除）")]

    rows = []
    kid, fid = created.get("knowledge_id"), created.get("file_id")

    if kid:
        status, body = request(base, f"/api/v1/knowledge/{kid}/delete", key, method="DELETE")
        if status != 200:
            rows.append(("清理知識庫", FAIL,
                         f"刪除失敗 HTTP {status}：{_short(str(http_detail(body)), 60)}"
                         f" —— 請手動刪除 {kid}"))
        else:
            status, body = request(base, f"/api/v1/knowledge/{kid}", key)
            # 刪掉之後 GET 應該 404（或回 null）。這才是「真的刪了」。
            rows.append(("清理知識庫", PASS if status == 404 or body is None else FAIL,
                         "已刪除（回讀確認）" if status == 404 or body is None
                         else f"刪除後仍讀得到（HTTP {status}）—— 請手動刪除 {kid}"))

    if fid:
        status, body = request(base, f"/api/v1/files/{fid}", key, method="DELETE")
        if status != 200:
            rows.append(("清理上傳的文件", FAIL,
                         f"刪除失敗 HTTP {status}：{_short(str(http_detail(body)), 60)}"
                         f" —— 請手動刪除 {fid}"))
        else:
            status, body = request(base, f"/api/v1/files/{fid}", key)
            rows.append(("清理上傳的文件", PASS if status == 404 or body is None else FAIL,
                         "已刪除（回讀確認）" if status == 404 or body is None
                         else f"刪除後仍讀得到（HTTP {status}）"))

    if not rows:
        rows.append(("清理", PASS, "沒有需要清理的東西（從未建立）"))
    return rows


def main():
    ap = argparse.ArgumentParser(description="RAG 四項的 HTTP 路徑驗證")
    ap.add_argument("--model", required=True, help="用來生成回答的模型")
    ap.add_argument("--base-url", default=DEFAULT_BASE,
                    help=f"Open WebUI 的位址（預設 {DEFAULT_BASE}，即容器內自己）")
    ap.add_argument("--json", action="store_true", help="以 JSON 輸出")
    ap.add_argument("--keep", action="store_true",
                    help="保留知識庫與上傳的文件（除錯用；正常情況下不要用）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"單次生成與上傳處理的時限秒數（預設 {DEFAULT_TIMEOUT}）")
    args = ap.parse_args()

    key = sys.stdin.readline().strip()
    if not key:
        print("無法判定：stdin 沒有讀到 API 金鑰。", file=sys.stderr)
        print("  這支腳本預期金鑰從 stdin 進來（不要放進命令列，見檔頭說明）。",
              file=sys.stderr)
        return 2

    print("=" * 62)
    print(" RAG HTTP 路徑探針 —— 上傳、檢索、接地，全程走 API")
    print("=" * 62)
    print()

    try:
        probe = run(args.base_url, key, args.model, timeout=args.timeout, keep=args.keep)
    except urllib.error.URLError as exc:
        # 連不上 ≠ 失敗。這是最容易誤報成「RAG 壞了」的一種情況。
        print(f"無法判定：連不上 {args.base_url}（{exc.reason}）")
        print("  這不是 RAG 的結論 —— 先確認 open-webui 容器在跑、且它自己")
        print(f"  聽得到 {args.base_url}（容器內是 8080）。")
        return 2
    except Exception as exc:                               # noqa: BLE001
        # **探針自己壞掉，不是 RAG 壞掉。** 把它報成「未通過」會叫人去修
        # 一個沒壞的系統（D-016 的形狀）。
        import traceback
        traceback.print_exc()
        print()
        print("─" * 62)
        print(" 結論：**探針本身執行失敗** —— 這不是 RAG 的結論。")
        print(f"       {type(exc).__name__}: {exc}")
        print("       請把上方 traceback 當成缺陷回報，不要先去改設定。")
        return 3

    print()
    probe.dump(as_json=args.json)

    if args.json:
        return 1 if probe.failed() else (2 if probe.unknown() else 0)

    print()
    print("─" * 62)
    if probe.failed():
        print(" 結論：**未通過** —— 上面標「未通過」的就是結論。")
        print("       注意：正向題若答不出捏造值，可能是檢索真的沒撈到，也可能是")
        print("       模型沒用它 —— 這條路看不到 sources，要分這兩者請跑")
        print("       bash scripts/rag-verify.sh。")
        return 1
    if probe.unknown():
        print(" 結論：**無法判定** —— 有東西連不上、沒在時限內跑完，或金鑰不對。")
        print("       這不是失敗，也還不是 RAG 的結論 —— 看上面每一列寫的是哪一種。")
        return 2
    print(" 結論：通過 —— HANDBOOK「Phase 2: RAG」那四項，走 HTTP 也成立。")
    print()
    print(" 邊界：本探針走的是應用程式的 HTTP API，不是 UI 的點擊本身；")
    print("       它證明了那條路徑能動，沒有證明 UI 上每個按鈕都接對。")
    print("       也看不到檢索回傳了哪些 chunk —— 那要 rag-verify.sh。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
