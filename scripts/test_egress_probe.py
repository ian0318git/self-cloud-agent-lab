#!/usr/bin/env python3
"""egress_probe.py 的離線單元測試（不連網、不需容器）。

對應 2026-09-19 的發現：一個以「資料不出公司」為目標的堆疊，資料庫裡卻是
    openai.enable = true
    openai.api_base_urls = ["https://api.openai.com/v1"]
這支探針的存在就是為了讓那件事看得見，所以它自己報錯會比沒有它更糟。

其中最重要的一條是 `test_no_key_value_ever_leaks`：探針的輸出會被貼進
issue、日誌與 DECISIONS.md，金鑰的任何片段都不能出現在裡面。

執行：python3 scripts/test_egress_probe.py
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import egress_probe as e  # noqa: E402

# ── is_private_url：判準是「有沒有離開這台機器」，不是「有沒有對外」 ──
for url in ("http://localhost:8080", "http://127.0.0.1:11434/v1",
            "http://ollama:11434", "http://open-webui:8080/health",
            "http://10.0.0.5/v1", "http://192.168.1.20:8000",
            "http://172.16.4.4/v1", "http://172.31.255.1/v1",
            "http://box.local/v1"):
    assert e.is_private_url(url), f"應判為私有：{url}"

for url in ("https://api.openai.com/v1", "https://api.mistral.ai/v1",
            "http://172.32.0.1/v1",          # 172.32 已超出 RFC1918
            "https://xxx.trycloudflare.com/v1"):
    assert not e.is_private_url(url), f"應判為外部：{url}"

# 解析不出主機名稱時保守地當成「外部」——寧可多報一則，不要漏報。
for url in ("", "not-a-url", "http://"):
    assert not e.is_private_url(url), f"解析不出主機時應保守判為外部：{url!r}"


# ── urls_in：值可能是字串、清單（openai.api_base_urls）或 dict ──
assert e.urls_in("https://a.example/v1") == ["https://a.example/v1"]
assert e.urls_in(["https://a.example/v1", "https://b.example/v1"]) == \
    ["https://a.example/v1", "https://b.example/v1"]
assert e.urls_in(None) == []
assert e.urls_in({"url": "https://a.example/v1"}) == ["https://a.example/v1"]
# 空清單是 Open WebUI 的實際形狀（api_keys 預設就是 [""]），不該炸
assert e.urls_in([""]) == []


# ── _is_key_name：金鑰欄位不該被當成端點來報 ────────────────
for k in ("openai.api_keys", "rag.openai.api_key", "audio.stt.openai.api_key",
          "web.search.tavily_api_key"):
    assert e._is_key_name(k), f"應判為金鑰欄位：{k}"
for k in ("openai.api_base_urls", "web.search.enable", "rag.top_k"):
    assert not e._is_key_name(k), f"不應判為金鑰欄位：{k}"


# ── _gate_for：閘門的歸屬必須正確 ───────────────────────────
# 這是一條**迴歸測試**。第一版的 _gate_for 取「前兩段」當前綴，於是
#   audio.stt.openai.api_base_url 的閘門被報成 audio.stt.deepgram.api_key
# ——把 openai 的端點說成由 deepgram 的金鑰管制。一支叫人「自己覆核」
# 的探針報錯閘門，比不報還糟。修法是從欄位名砍掉結尾還原子系統。
_cfg = {
    "openai.enable": True,
    "openai.api_base_urls": ["https://api.openai.com/v1"],
    "audio.stt.openai.api_base_url": "https://api.openai.com/v1",
    "audio.stt.deepgram.api_key": "",
    "audio.stt.openai.api_key": "",
    "audio.stt.mistral.api_key": "filled",
    "web.search.enable": False,
    "web.search.bing_search_v7_endpoint": "https://api.bing.microsoft.com/v7.0/search",
    "rag.mistral_ocr_api_base_url": "https://api.mistral.ai/v1",
    "rag.mistral_ocr_api_key": "",
    "orphan.url": "https://nothing.example/v1",
}

gate, val, _ = e._gate_for("audio.stt.openai.api_base_url", _cfg)
assert gate == "（需要金鑰）audio.stt.openai.api_key", \
    f"閘門應是 openai 自己的金鑰，實得 {gate}"
assert val is False

gate, val, _ = e._gate_for("audio.stt.mistral.api_base_url", _cfg)
assert gate == "（需要金鑰）audio.stt.mistral.api_key", f"實得 {gate}"
assert val is True, "已填金鑰 → 這個端點是可用狀態"

gate, val, _ = e._gate_for("rag.mistral_ocr_api_base_url", _cfg)
assert gate == "（需要金鑰）rag.mistral_ocr_api_key", f"實得 {gate}"

gate, val, _ = e._gate_for("web.search.bing_search_v7_endpoint", _cfg)
assert (gate, val) == ("web.search.enable", False)

gate, val, _ = e._gate_for("openai.api_base_urls", _cfg)
assert (gate, val) == ("openai.enable", True)

gate, val, _ = e._gate_for("orphan.url", _cfg)
assert gate is None, "沒有開關的端點應回報 None，而不是猜一個"


# ── scan()：對一個合成資料庫端到端跑一次 ─────────────────────
SECRET = "sk-THIS-MUST-NEVER-APPEAR-0123456789"

with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, "webui.db")
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value JSON, "
               "updated_at BIGINT)")
    rows = {
        "openai.enable": False,
        "openai.api_base_urls": ["https://api.openai.com/v1"],
        "openai.api_keys": [SECRET],
        "ollama.enable": True,
        # 私有位址：整筆都不該出現在結果裡
        "ollama.base_urls": ["http://ollama:11434"],
        "rag.ollama.base_url": "http://ollama:11434",
        "web.search.enable": True,
        "web.search.bing_search_v7_endpoint":
            "https://api.bing.microsoft.com/v7.0/search",
        "audio.stt.openai.api_base_url": "https://api.openai.com/v1",
        "audio.stt.openai.api_key": SECRET,
    }
    for k, v in rows.items():
        db.execute("INSERT INTO config VALUES (?,?,?)", (k, json.dumps(v), 0))
    db.commit()
    db.close()

    findings, err = e.scan(db_path)
    assert err is None, f"不該失敗：{err}"

    by_key = {f["key"]: f for f in findings}

    # 私有位址完全不出現 —— 那些沒有離開這台機器，報出來只會製造噪音，
    # 而被噪音淹沒的檢查等於沒有檢查。
    for private_key in ("ollama.base_urls", "rag.ollama.base_url"):
        assert private_key not in by_key, f"{private_key} 是私有位址，不該列出"

    # 金鑰欄位本身不是端點
    for k in ("openai.api_keys", "audio.stt.openai.api_key"):
        assert k not in by_key, f"{k} 是金鑰欄位，不該被當成端點列出"

    # openai 關著 → 未啟用（這正是 --fix 之後的預期狀態）
    assert by_key["openai.api_base_urls"]["state"] == "未啟用"
    # 開了 web.search，且 bing 端點在裡面 → 啟用中
    assert by_key["web.search.bing_search_v7_endpoint"]["state"] == "啟用中"
    # 有填金鑰的端點是可用狀態
    assert by_key["audio.stt.openai.api_base_url"]["state"] == "啟用中"

    active = [f for f in findings if f["state"] == "啟用中"]
    assert len(active) == 2, f"應有 2 個啟用中，實得 {len(active)}"

    # ── 最重要的一條：金鑰值不得出現在輸出裡 ──
    blob = json.dumps(findings, ensure_ascii=False)
    assert SECRET not in blob, "金鑰值外洩到掃描結果中"
    assert "sk-" not in blob, "金鑰前綴外洩到掃描結果中"

    # 讀取是唯讀的：不該在掃描過程中改動資料庫
    db = sqlite3.connect(db_path)
    after = dict(db.execute("SELECT key, value FROM config"))
    db.close()
    assert json.loads(after["openai.enable"]) is False, "掃描不該改動資料庫"
    assert json.loads(after["openai.api_keys"]) == [SECRET], "掃描不該改動金鑰"

# ── 讀不到資料庫時必須是「無法判定」，不是「沒有風險」 ──────
# 這兩者分不開的話，一個讀不到資料庫的環境會被回報成「安全」——
# 那正是本專案最不能接受的假成功。
findings, err = e.scan("/nonexistent/path/webui.db")
assert findings is None and err, "讀不到資料庫時應回報錯誤，而不是回傳空清單"

print("test_egress_probe.py：全部通過")
