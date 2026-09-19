#!/usr/bin/env python3
r"""rag_http_probe.py 的離線單元測試（不連網、不需容器、不需金鑰）。

這支測試盯的是**三件在真機上會靜默出錯、而且錯了看不出來**的事：

  1. `files` 放錯位置。伺服器只讀 body 頂層的 `files`（main.py 用它組出內部
     metadata，然後**覆寫**掉呼叫者送來的 metadata）。放進 `metadata.files`
     的請求會拿到 HTTP 200、正常的答案，只是完全沒有檢索 —— 正向題失敗，
     而看起來像模型不聽話。這是本探針最容易犯、也最難察覺的錯。
  2. multipart 的邊界組錯。錯了在伺服器端變成 422，而 422 的訊息不會說出
     「你的 boundary 少了一個 --」。
  3. 金鑰外洩到輸出裡。探針宣稱「任何訊息、任何 --json 輸出都不含金鑰」，
     這是一句可以被測的宣稱，所以它應該被測。

測法刻意分成兩層：
  · 純函式層 —— 直接驗形狀（multipart 用 email 剖析器**真的解析回來**，
    不是字串包含）。
  · 整條流程層 —— 用假的 HTTP 傳輸餵罐頭回應，跑完整的 run()，確認
    判準與退出碼的分類，並在上面那三件事上做斷言。

執行：python3 scripts/test_rag_http_probe.py
"""

import email
import os
import sys
from email.policy import default as email_policy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rag_grounding_probe as g  # noqa: E402
import rag_http_probe as r  # noqa: E402

# 一個拿來當「外洩偵測器」的假金鑰。它夠特別，出現在任何輸出裡都認得出來。
KEY = "sk-SENTINEL-0123456789abcdef"
KID = "kb-0000-1111"
FID = "file-2222-3333"

POS_Q = g.QUESTIONS["positive"]["q"]
NEG_Q = g.QUESTIONS["negative"]["q"]

# 真實格式的回應原文（取自 rag_grounding_probe 的實機結果）
POS_ANSWER = "本公司自訂的提前停班門檻，平均風速是八級 [1]。"
NEG_ANSWER = "根據文件，沒有提及颱風停班期間的加班費倍率。"


# ── 1. multipart：用剖析器驗，不是用字串包含驗 ──────────────

ct, payload = r.build_multipart(r.FILENAME, g.DOCUMENT)
assert ct.startswith("multipart/form-data; boundary="), ct
boundary = ct.split("boundary=", 1)[1]
assert payload.startswith(f"--{boundary}\r\n".encode()), "body 必須以第一個邊界開頭"
assert payload.endswith(f"\r\n--{boundary}--\r\n".encode()), "body 必須以結束邊界收尾"

# b"Content-Type: ...\r\n\r\n" + payload 用 % 串接，因為 email 剖析器要完整標頭
_parsed = email.message_from_bytes(
    b"Content-Type: " + ct.encode() + b"\r\n\r\n" + payload, policy=email_policy)
_parts = list(_parsed.iter_parts())
assert len(_parts) == 1, f"應該恰好一個 part，得到 {len(_parts)}"

_part = _parts[0]
# 欄位名必須是 `file` —— files.py 的簽章是 file: UploadFile = File(...)。
# 名字不對的欄位在 FastAPI 眼裡等於不存在，回應是 422。
assert _part.get_param("name", header="content-disposition") == "file"
assert _part.get_filename() == r.FILENAME
# 內容必須**逐字**存活。中文經過 UTF-8 編碼再解回來，任何一處出錯都會在這裡現形。
assert _part.get_payload(decode=True).decode("utf-8") == g.DOCUMENT
assert r.FILENAME.isascii(), "檔名改用非 ASCII 會踩到 RFC 2231，不值得"


# ── 2. 聊天請求：files 在頂層 ─────────────────────────────

body = r.build_chat_body("qwen2.5:3b", POS_Q, KID, "kb-name")

# 這一條是整份檔案最重要的一行斷言。
assert "files" in body, "files 必須在 body 頂層"
assert "metadata" not in body, (
    "files 放進 metadata 會被伺服器靜默丟掉：HTTP 200、答案正常、沒有檢索")
assert body["files"] == [{
    "type": "collection", "id": KID, "name": "kb-name", "collection_name": KID,
}], body["files"]
# collection 這個 type 與 id 的組合，是 retrieval/utils.py 的
# `elif item.get('type') == 'collection'` 分支實際讀的形狀。
assert body["model"] == "qwen2.5:3b"
assert body["messages"] == [{"role": "user", "content": POS_Q}]
assert body["stream"] is False, "非串流才拿得到完整的回應 body"

# 不給 chat_id 的話伺服器自己生 uuid，那是**會被存檔**的形式
# （utils/chat_id.py 的 is_saved_chat_id）—— 測試會在使用者的聊天紀錄裡
# 留下垃圾。temporary: 開頭才是非存檔的。
# （`is_saved_chat_id` 住在應用程式裡，不在這支探針裡 —— 這裡驗的只是
#   「送出去的 chat_id 落在它會判為非存檔的那一側」，也就是 temporary: 前綴。）
assert body["chat_id"].startswith("temporary:"), body["chat_id"]


# ── 3. 回應解析 ───────────────────────────────────────────

assert r.extract_answer({"choices": [{"message": {"content": "答案"}}]}) == "答案"
assert r.extract_answer({"choices": [{"message": {"content": ""}}]}) == ""
assert r.extract_answer({"choices": []}) == ""
assert r.extract_answer({"error": "boom"}) == ""
assert r.extract_answer("不是 dict") == ""
assert r.extract_answer(None) == ""

assert r.find_sources({}) == []
assert r.find_sources({"sources": [{"a": 1}]}) == [{"a": 1}]
assert r.find_sources({"choices": [{"message": {"sources": [1]}}]}) == [1]
assert r.find_sources(None) == []


# ── 4. 結束碼分類：401 不是「RAG 未通過」 ──────────────────

assert r.auth_problem(401) and r.auth_problem(403)
assert not r.auth_problem(400) and not r.auth_problem(404) and not r.auth_problem(200)


# ── 5. 整條流程：假的 HTTP 傳輸 ────────────────────────────
#
# 這裡換掉模組層的 request，run() 與 cleanup() 都用同一個名字查找，
# 所以兩者都會走到假的傳輸上。

class FakeTransport:
    """罐頭回應。路由用 (method, path) 比對，比對不到就大聲失敗。

    刻意**不**用寬鬆的前綴比對 —— 假的傳輸若默默接受了打錯的 URL，
    真的 URL 打錯就測不出來了。
    """

    def __init__(self, *, auth_status=200, auth_body=None, answers=(), delete_status=200,
                 readback_still=False, file_add_status=200, kb_files=1,
                 models=("qwen2.5:3b",)):
        self.calls = []
        self.auth_status = auth_status
        self.auth_body = auth_body if auth_body is not None else {"role": "admin"}
        self.answers = list(answers)
        self.delete_status = delete_status
        # 刪除之後**還讀得到**（模擬刪了沒真的刪）。預設是刪掉就讀不到。
        self.readback_still = readback_still
        self.file_add_status = file_add_status
        self.kb_files = kb_files
        self.models = list(models)
        # 有狀態：讀兩次同一條路徑，刪前與刪後必須得到不同答案。用固定的
        # 罐頭狀態碼會讓「刪除後回讀」這項檢查永遠測不到它該測的東西。
        self.deleted = set()

    def __call__(self, base, path, key, *, json_body=None, raw=None,
                 content_type=None, timeout=60, method=None):
        method = method or ("POST" if (json_body is not None or raw is not None) else "GET")
        self.calls.append((method, path, json_body))
        # 假的傳輸也要收到金鑰 —— 收不到就代表組請求那段壞了。
        assert key == KEY, "請求沒有帶上金鑰"

        if path == "/api/v1/auths/":
            return self.auth_status, self.auth_body
        if path == "/api/models":
            return 200, {"data": [{"id": m} for m in self.models]}
        if path == "/api/v1/knowledge/create":
            return 200, {"id": KID, "name": json_body.get("name")}
        if path == "/api/v1/files/?process=true&process_in_background=false":
            return 200, {"id": FID, "filename": r.FILENAME}
        if path == f"/api/v1/knowledge/{KID}/file/add":
            return self.file_add_status, {"id": KID}
        if path == f"/api/v1/knowledge/{KID}/delete":
            if self.delete_status == 200:
                self.deleted.add("kb")
            return self.delete_status, True
        if path == f"/api/v1/knowledge/{KID}":
            if "kb" in self.deleted and not self.readback_still:
                return 404, None
            return 200, {"id": KID, "files": [{"id": FID}] * self.kb_files}
        if path == f"/api/v1/files/{FID}":
            if method == "DELETE":
                if self.delete_status == 200:
                    self.deleted.add("file")
                return self.delete_status, True
            if "file" in self.deleted and not self.readback_still:
                return 404, None
            return 200, {"id": FID}
        if path == "/api/chat/completions":
            answer = self.answers.pop(0) if self.answers else ""
            return 200, {"choices": [{"message": {"content": answer}}]}
        raise AssertionError(f"假的傳輸沒有這條路由：{method} {path}")


def _run(fake, **kw):
    original = r.request
    r.request = fake
    try:
        probe = r.run("http://localhost:8080", KEY, "qwen2.5:3b", timeout=30, **kw)
    finally:
        r.request = original
    return probe


def _all_text(probe):
    return "\n".join(f"{n}|{s}|{d}" for n, s, d in probe.rows)


# (1) 全部通過 —— 正向題答出捏造值、反向題說不知道、清理回讀確認
fake = FakeTransport(answers=[POS_ANSWER, NEG_ANSWER])
probe = _run(fake)
assert not probe.failed(), f"不該有未通過：{_all_text(probe)}"
assert not probe.unknown(), f"不該有無法判定：{_all_text(probe)}"
states = {name: state for name, state, _ in probe.rows}
assert any(k.startswith("1 建立知識庫") for k in states), states
assert any(k.startswith("2 上傳文件") for k in states), states
assert any("正向題" in k for k in states), states
assert any("反向題" in k for k in states), states
assert any("清理知識庫" in k for k in states), states
assert any("清理上傳的文件" in k for k in states), states

# 這一條是「金鑰不外洩」那句宣稱的測試。整份報告的任何一個字都不該是金鑰。
assert KEY not in _all_text(probe), "金鑰出現在輸出裡"

# 而且金鑰也不該出現在 --json 的序列化結果裡
import io                                                    # noqa: E402
import json                                                  # noqa: E402
_buf = io.StringIO()
_stdout = sys.stdout
sys.stdout = _buf
try:
    probe.dump(as_json=True)
finally:
    sys.stdout = _stdout
assert KEY not in _buf.getvalue(), "金鑰出現在 --json 輸出裡"
assert json.loads(_buf.getvalue())[0]["name"] == probe.rows[0][0]

# (2) 正向題沒答出捏造值 —— 這是**未通過**，不是無法判定
fake = FakeTransport(answers=["我不知道。", NEG_ANSWER])
probe = _run(fake)
assert probe.failed() and not probe.unknown(), _all_text(probe)

# (3) 反向題掰了具體數字 —— 未通過
fake = FakeTransport(answers=[POS_ANSWER, "加班費是 1.5 倍。"])
probe = _run(fake)
assert probe.failed(), _all_text(probe)

# (4) 空回應 —— 是**無法判定**（token 可能全用在自己的思考上），不是答錯。
fake = FakeTransport(answers=["", NEG_ANSWER])
probe = _run(fake)
assert probe.unknown() and not probe.failed(), _all_text(probe)

# (5) 金鑰無效（401）—— 設定問題，不是 RAG 的結論。
#     這一條是 D-016 的形狀：把它報成「未通過」會叫人去修一個沒壞的系統。
fake = FakeTransport(auth_status=401, auth_body={"detail": "Invalid token"})
probe = _run(fake)
assert probe.unknown() and not probe.failed(), _all_text(probe)
assert "設定問題" in _all_text(probe), _all_text(probe)
# 而且要**在第一步就停**，不該繼續去建知識庫
assert len(fake.calls) == 1, f"401 之後不該再打任何請求：{fake.calls}"

# (6) 模型不在清單裡 —— 也要在生成之前停，且是無法判定。
#     不先驗的話，它會在 chat 那一步變成 HTTP 400 'Model not found'，
#     然後被報成「未通過」—— 又一個叫人去修沒壞的東西的假失敗。
fake = FakeTransport(answers=[POS_ANSWER, NEG_ANSWER], models=("別的模型",))
probe = _run(fake)
assert probe.unknown() and not probe.failed(), _all_text(probe)
assert not any(c[1].startswith("/api/v1/knowledge/create") for c in fake.calls), \
    "模型不在清單裡時不該已經建了知識庫"

# (7) 清理：刪不掉是**未通過** —— 而且它不該掩蓋其他項目的結論
fake = FakeTransport(answers=[POS_ANSWER, NEG_ANSWER], delete_status=500)
probe = _run(fake)
assert probe.failed(), _all_text(probe)
assert "請手動刪除" in _all_text(probe), _all_text(probe)

# (8) 清理：刪完還讀得到 —— 一樣是未通過（回讀才是證據）
fake = FakeTransport(answers=[POS_ANSWER, NEG_ANSWER], readback_still=True)
probe = _run(fake)
assert probe.failed(), _all_text(probe)
assert "仍讀得到" in _all_text(probe), _all_text(probe)

# (9) 提前結束（401）時，清理不該宣稱刪掉了不存在的東西
fake = FakeTransport(auth_status=401)
probe = _run(fake)
assert "手動刪除" not in _all_text(probe), \
    f"沒有建立過東西，不該叫人去刪：{_all_text(probe)}"

# (10) --keep 不算失敗，而且要把 id 講出來讓使用者自己刪
fake = FakeTransport(answers=[POS_ANSWER, NEG_ANSWER])
probe = _run(fake, keep=True)
assert not probe.failed() and not probe.unknown(), _all_text(probe)
assert KID in _all_text(probe) and FID in _all_text(probe), _all_text(probe)

# (11) cleanup 在完全沒建立東西時，是一列通過，不是空的、也不是失敗
rows = r.cleanup("http://localhost:8080", KEY, {})
assert len(rows) == 1 and rows[0][1] == r.PASS, rows

print("test_rag_http_probe.py：全部通過")
