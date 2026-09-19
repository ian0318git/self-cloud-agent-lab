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
