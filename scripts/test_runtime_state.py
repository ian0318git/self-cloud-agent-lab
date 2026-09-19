#!/usr/bin/env python3
"""runtime_state.py 的離線單元測試（不連網、不需容器）。

這支腳本是「接上第二個 runtime」的讀寫介面，所以它答錯的代價是：
把平台的模型來源指向一個錯的地方，或更糟 —— 把金鑰印出來。

`runtime_state.py` 只在函式內部 import open_webui，所以模組本身可以離線
載入，這裡測的是不需要容器的那幾塊。

執行：python3 scripts/test_runtime_state.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime_state as r  # noqa: E402

# ── 金鑰永不顯示內容，也不顯示長度 ──────────────────────────
# 輸出會被貼進 issue、日誌與 DECISIONS.md。長度也是一種資訊。
assert r._mask([""]) == ["未設定"]
assert r._mask(["abc"]) == ["已設定"]
assert r._mask(["", "k"]) == ["未設定", "已設定"]
assert r._mask([]) == []
assert r._mask(None) == "（不存在）"
assert r._mask("") == "未設定"
assert r._mask("secret") == "已設定"

# 任何真實金鑰的片段都不得出現在輸出裡。
SECRET = "sk-THIS-MUST-NEVER-APPEAR-0123456789"
assert SECRET not in str(r._mask([SECRET]))
assert "sk-" not in str(r._mask([SECRET]))

# ── OpenAI 官方 API 必須是明示的例外，不是會滑進去的預設 ────
# 本專案的目標是「資料由公司自己控制」。接上 api.openai.com 與那個目標
# 直接衝突，所以清單必須存在，而且必須包含兩種寫法（有無結尾斜線）。
assert "https://api.openai.com/v1" in r.REFUSED_BASE_URLS
assert "https://api.openai.com" in r.REFUSED_BASE_URLS

# 自架的位址不得被誤擋 —— 一個什麼都擋的清單最後會被繞過，等於沒有。
for ok_url in ("http://ollama:11434/v1", "https://x.trycloudflare.com/v1",
               "http://192.168.1.5:8000/v1", "https://api.example.com/v1"):
    assert ok_url not in r.REFUSED_BASE_URLS, f"不該擋自架位址：{ok_url}"

# ── 相依性：讀值必須走應用程式自己的路徑 ────────────────────
# 這是最容易在重構中被改掉、而改掉之後仍然「看起來正常」的一點 ——
# 直接 SELECT 資料列也會印出值，只是證明不了那個值真的被採用。
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "runtime_state.py")).read()
assert "Config.get_many" in src, "讀值必須走應用程式自己的 Config 路徑"
assert "from open_webui.models.config import Config" in src, \
    "必須 import 應用程式自己的 Config 類別（含它的 persistent 規則）"

# ── 寫入端不得使用 :? 之類會讓 compose 插值失敗的東西 ────────
# （這支腳本不碰 compose，但確認它也沒有偷偷呼叫 docker）
assert "subprocess" not in src, "寫入應直接走資料庫，不該呼叫外部程式"
assert "os.system" not in src

print("test_runtime_state.py：全部通過")
