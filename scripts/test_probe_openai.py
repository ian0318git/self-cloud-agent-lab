#!/usr/bin/env python3
"""probe_openai.py 的離線單元測試（不連網、不需容器）。

這支探針的用途是回答「這個 runtime 能不能接手本平台」。它答錯的方向有兩種，
而**假失敗比漏報更糟**：把一個正常運作的 runtime 講成不能接手，會讓人去修
一個不存在的問題，最後連真的問題也不信了。

所以這裡最重要的是兩條：
  · 警告（WARN）不得讓整體變成「未通過」——
    否則 Ollama（無認證，D-003）會在私有網路上被判成不能接手
  · 金鑰不得出現在輸出裡——輸出會被貼進 issue、日誌與 DECISIONS.md

執行：python3 scripts/test_probe_openai.py
"""

import contextlib
import json
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe_openai as p  # noqa: E402

# ── _is_private_host：決定「沒有認證」是警告還是失敗 ────────
# 判準不是「有沒有認證」，而是「**這個位置**沒有認證能不能接受」。
# check-exposure.sh 處理過同一個形狀：同一個設定，安全或危險取決於機器在哪。
for base in ("http://ollama:11434/v1", "http://localhost:8080/v1",
             "http://127.0.0.1:8000/v1", "http://10.0.0.7:8000/v1",
             "http://192.168.1.9/v1", "http://172.16.0.3/v1",
             "http://gpu-box.local/v1"):
    assert p._is_private_host(base), f"應判為私有：{base}"

for base in ("https://xxx.trycloudflare.com/v1", "https://api.openai.com/v1",
             "http://172.32.0.1/v1", "https://gpu.example.com/v1"):
    assert not p._is_private_host(base), f"應判為公開：{base}"


# ── Probe：狀態不可互相汙染 ─────────────────────────────────
def _collect(entries):
    buf = io.StringIO()
    probe = p.Probe()
    with contextlib.redirect_stdout(buf):
        for name, state in entries:
            probe.add(name, state)
    return probe


# WARN 不是 FAIL —— 這是本專案反覆吃過的虧（D-016：假失敗比漏報更糟）。
warned = _collect([("a", p.PASS), ("b", p.WARN)])
assert not warned.failed(), "警告不該讓整體變成未通過"
assert not warned.any_unknown()

failed = _collect([("a", p.PASS), ("b", p.FAIL)])
assert failed.failed()

# 未執行／無法判定都不是未通過，但「無法判定」要被單獨認出來 ——
# 它與「未通過」需要不同的下一步（一個是去修，一個是去確認網路）。
unknown = _collect([("a", p.PASS), ("b", p.UNKNOWN)])
assert not unknown.failed()
assert unknown.any_unknown(), "無法判定必須與未通過分得開"

skipped = _collect([("a", p.PASS), ("b", p.SKIP)])
assert not skipped.failed() and not skipped.any_unknown()

# ── _short：訊息要短到能放進一行，且不該在結尾留下破折號 ────
assert p._short("a" * 500) == "a" * 200 + "…"
assert p._short(None) == ""
assert p._short("  多個   空白  ") == "多個 空白"


# ── main()：連不上時是「無法判定」（結束碼 2），不是「未通過」 ──
# 用一個必定拒絕連線的位址，不需要網路也不會等太久。
SECRET = "sk-THIS-MUST-NEVER-APPEAR-0123456789"

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = p.main(["--base-url", "http://127.0.0.1:1/v1",
                   "--api-key", SECRET, "--timeout", "2"])
out = buf.getvalue()

assert code == 2, f"連不上應回報無法判定（2），實得 {code}"

# ── 最重要的一條：金鑰不得出現在輸出裡 ──
assert SECRET not in out, "金鑰外洩到探針輸出中"
assert "sk-" not in out, "金鑰前綴外洩到探針輸出中"
assert "已提供" in out, "應只說明金鑰已提供，而不是印出它"


# ── 沒有金鑰時不得誤報成「未提供」以外的狀態 ────────────────
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    p.main(["--base-url", "http://127.0.0.1:1/v1", "--timeout", "2"])
assert "未提供" in buf.getvalue()

# ── 「端點有回應」不等於「無法判定」─────────────────────────
#
# 這是一條**迴歸測試**，對應 2026-09-19 在 probe_openai.py 自己身上發現的
# 缺陷：一個回 404 的端點是**明確回答**「我沒有這個路徑」，探針卻把它標成
# aborted，於是印出「✗ 未通過 HTTP 404」之後總結說「無法判定 —— 端點拒絕」
# 並回傳 2。connect-endpoint.sh 因此叫使用者去查網路，而網路根本沒問題。
#
# 那是假失敗，也正是 D-016 的同一形狀：「無法判定」與「未通過」被併在一起。
# 差別只在這次發生在探針自己身上。
#
# 判準：只有**連不上**（status is None）才是無法判定。端點有回應就是有結論。
_orig_request = p._request


def _fake_request_factory(handler):
    def _fake(url, payload=None, api_key=None, timeout=120, accept=None,
              stream=False):
        return handler(url, payload)
    return _fake


# 404：端點活著，但沒有這個路徑 → 未通過，不是無法判定。
p._request = _fake_request_factory(lambda url, payload: (404, "Not Found"))
try:
    probe, _mid, aborted = p.probe("http://x:1/v1", "", "", 5)
    assert probe.failed(), "404 應判為未通過"
    assert aborted is None, \
        "404 是端點明確的回應，不該被當成無法判定（那會叫人去查一個沒問題的網路）"
finally:
    p._request = _orig_request

# 200 但回應不是 OpenAI 格式 → 同樣是未通過，不是無法判定。
p._request = _fake_request_factory(lambda url, payload: (200, "<html>hi</html>"))
try:
    probe, _mid, aborted = p.probe("http://x:1/v1", "", "", 5)
    assert probe.failed(), "非 OpenAI 格式應判為未通過"
    assert aborted is None, "格式不符是明確的結論，不是無法判定"
finally:
    p._request = _orig_request

# 連不上 → 這才是無法判定。
p._request = _fake_request_factory(
    lambda url, payload: (None, "URLError: connection refused"))
try:
    probe, _mid, aborted = p.probe("http://x:1/v1", "", "", 5)
    assert not probe.failed(), "連不上不該被記成未通過"
    assert aborted is not None, "連不上才是無法判定"
finally:
    p._request = _orig_request

# 模型清單 OK，但 chat completion 失敗 → 未通過（上層會踩到的洞）。
def _chat_fails(url, payload):
    if url.endswith("/models"):
        return (200, json.dumps({"data": [{"id": "m1"}]}))
    return (500, "boom")


p._request = _fake_request_factory(_chat_fails)
try:
    probe, _mid, aborted = p.probe("http://x:1/v1", "", "", 5)
    assert probe.failed(), "chat completion 失敗應判為未通過"
    assert aborted is None
finally:
    p._request = _orig_request


# ── 思考型模型的假失敗（2026-09-24）────────────────────────
#
# 這一組是**迴歸測試**，對應一個讓 Kaggle runtime 接不上來的缺陷：兩個 chat
# 檢查都用寫死的 `max_tokens: 16`，而 Qwen3 這類「先思考再回答」的模型會把
# 那 16 個 token 全部花在思考段 —— `content` 是空的、`finish_reason` 是
# `length`，於是探針把一個完全正常的 runtime 判成未通過，
# connect-endpoint.sh 照著結束碼拒絕寫入設定。實測 qwen3:8b 回答「好」這一個
# 字要用掉 200 個 token，16 是它的 1/12。
#
# **判準是 `finish_reason`，不是「content 是不是空的」。** 兩件事都會讓
# content 變空，而它們的下一步完全相反：
#   `length` → 我們的預算不夠，去把 CHAT_MAX_TOKENS 開大
#   `stop`   → 模型自己停了卻什麼都沒說，那才是 runtime 的問題（D-014）
# 併在一起正是這個缺陷當初能存在的原因。

_MODELS_OK = (200, json.dumps({"data": [{"id": "m1"}]}))


def _chat_body(content="", finish="stop", reasoning=None):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning"] = reasoning
    return json.dumps({"choices": [{"index": 0, "message": msg,
                                    "finish_reason": finish}]})


def _sse(*deltas, done=True):
    """把 delta 串成 SSE 主體。

    形狀要跟真的回應一致：`_request(stream=True)` 交出來的是**可迭代、可 close
    的檔案物件**（探針在 `finally` 裡呼叫 `resp.close()`）。回傳字串或 list
    會讓替身自己爆掉 —— 那時紅的是測試，不是被測的程式。
    """
    lines = []
    for d in deltas:
        body = json.dumps({"choices": [{"index": 0, "delta": d,
                                        "finish_reason": None}]})
        lines.append("data: " + body)
    if done:
        lines.append("data: [DONE]")
    return io.BytesIO(("\n".join(lines) + "\n").encode())


def _result(probe, name):
    for n, s, d in probe.results:
        if n == name:
            return s, d
    return None, None


def _probe_with(chat_body=None, sse=None, seen=None):
    """跑一次探針，chat 回應由呼叫端指定；`seen` 收下每個請求的 payload。

    輸出吞掉：這裡的斷言看的是 `probe.results`，六次探針的逐項列印只會蓋掉
    真正該看的訊息。
    """
    def handler(url, payload):
        if seen is not None and payload is not None:
            seen.append((url, payload))
        if url.endswith("/models"):
            return _MODELS_OK
        if payload and payload.get("stream"):
            # 串流請求一律回檔案物件，沒指定內容時給空串流 —— 但**仍然要**是
            # 檔案物件，理由見 _sse。
            return (200, sse if sse is not None else io.BytesIO(b""))
        if chat_body is not None:
            return (200, chat_body)
        return (500, "boom")

    p._request = _fake_request_factory(handler)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return p.probe("http://x:1/v1", "", "m1", 5)[0]
    finally:
        p._request = _orig_request


# 模型被我們的預算腰斬 → 未通過，但訊息必須指向**預算**，不是 runtime。
_truncated = _probe_with(chat_body=_chat_body(content="", finish="length",
                                              reasoning="好的，用户让我用一个字回答…"))
_state, _detail = _result(_truncated, "POST /v1/chat/completions")
assert _state == p.FAIL, "被腰斬的模型不該通過 —— 上層拿到的是 HTTP 200 ＋ 空字串"
assert str(p.CHAT_MAX_TOKENS) in _detail, \
    "訊息必須說出預算數字，否則下一個人會去修一個沒壞的 runtime"
assert "length" in _detail, "要說出被截斷，不能只說『content 為空』"

# 模型自己停了卻什麼都沒說 → 這也是未通過，但訊息與上一條**必須不同**：
# 這一條才是 D-014 的假通過形狀本身，處方不是調預算。
_empty_stop = _probe_with(chat_body=_chat_body(content="", finish="stop"))
_state2, _detail2 = _result(_empty_stop, "POST /v1/chat/completions")
assert _state2 == p.FAIL, "HTTP 200 ＋ 空 content 是 D-014 的假通過形狀"
assert _detail2 != _detail, \
    "被截斷與自己停下來是兩種病，訊息不可相同（下一步完全相反）"
assert "stop" in _detail2 and str(p.CHAT_MAX_TOKENS) not in _detail2, \
    "自己停下來的，不該叫使用者去調預算"

# 正常模型照樣通過。
_ok = _probe_with(chat_body=_chat_body(content="好"), sse=_sse({"content": "好"}))
assert _result(_ok, "POST /v1/chat/completions")[0] == p.PASS
assert _result(_ok, "串流（SSE）")[0] == p.PASS

# 兩個 chat 檢查都必須送 CHAT_MAX_TOKENS —— 這是**結構性**的守衛，
# 不是靠自律：兩處只要有一處漂回寫死的值，這個缺陷就會從那個出口回來。
_seen = []
_probe_with(chat_body=_chat_body(content="好"), sse=_sse({"content": "好"}),
            seen=_seen)
_budgets = [pl["max_tokens"] for u, pl in _seen if u.endswith("/chat/completions")]
assert _budgets == [p.CHAT_MAX_TOKENS, p.CHAT_MAX_TOKENS], \
    f"非串流與串流都要用 CHAT_MAX_TOKENS，實得 {_budgets}"

# 陷阱線，不是推導：防的是有人把它改回 16。真正的判準是 finish_reason。
assert p.CHAT_MAX_TOKENS >= 512, \
    "預算要大到能吸收思考型模型（實測需要 200），理由見 CHAT_MAX_TOKENS 的註解"

# 逾時：`--timeout` 是**下限**，生成請求至少拿到 GENERATION_TIMEOUT 秒。
assert p._generation_timeout(5) == p.GENERATION_TIMEOUT
assert p._generation_timeout(120) == p.GENERATION_TIMEOUT
assert p._generation_timeout(99999) == 99999, "--timeout 比下限大時要聽使用者的"

# 串流有 chunk、有 [DONE]，但整條串流沒有一個字進到 content → 未通過。
# 同一個盲點的第二個出口：只算 chunk 數的話，這裡會通過一個空回答。
_stream_empty = _probe_with(
    chat_body=_chat_body(content="好"),
    sse=_sse({"role": "assistant", "content": "", "reasoning": "好的"},
             {"content": "", "reasoning": "，"}))
assert _result(_stream_empty, "串流（SSE）")[0] == p.FAIL, \
    "整條串流沒有 content 卻判通過 —— 上層顯示的會是一片空白"

# 沒有 [DONE] 仍然是未通過（既有行為，不因這次改動而放寬）。
_stream_node = _probe_with(chat_body=_chat_body(content="好"),
                           sse=_sse({"content": "好"}, done=False))
assert _result(_stream_node, "串流（SSE）")[0] == p.FAIL, "串流沒有正常結束仍是未通過"

# ── 探針只說 OpenAI 協定，不得依賴任何廠商 SDK ──────────────
# 若它用了廠商 SDK，驗到的就是那個 SDK 的相容性，而不是協定的相容性 ——
# 那樣「任何相容的 runtime 都能接手」這句話就不成立了。
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "probe_openai.py")).read()
for forbidden in ("import openai", "from openai", "import ollama",
                  "from ollama", "requests."):
    assert forbidden not in src, f"探針不該依賴 {forbidden}"
assert "urllib.request" in src, "應只用標準庫"

print("test_probe_openai.py：全部通過")
