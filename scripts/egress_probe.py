#!/usr/bin/env python3
"""讀 Open WebUI 的**實際資料庫設定**，列出資料可能流向哪些外部服務。

為什麼是讀資料庫而不是讀 .env：

  lock-signup.sh 的舊版讀 .env 判斷註冊是否關閉，於是 .env 寫 false、實際
  開著時，它回報「已是 false，無需變更」——**一個讀起來像成功的假成功**。
  環境變數在 Open WebUI 只是「第一次開機時的種子」，之後真正的值在資料庫
  （config.py 的 `Config.seed_defaults`，docstring 明寫 "Existing DB values
  take precedence over defaults"）。

  所以任何「現在的設定是什麼」的問題，唯一可靠的來源都是資料庫。

為什麼需要這支探針（2026-09-19 實測發現）：

  本專案的目標是「資料由公司自己控制」，但 Open WebUI 的原始碼裡

      ENABLE_OPENAI_API      預設 'True'
      OPENAI_API_BASE_URLS   預設 ''，而空字串是 fallback 不是關閉，
                             會被填成 ['https://api.openai.com/v1']

  兩者相加的結果，實測的資料庫裡就是：

      openai.enable         = true
      openai.api_base_urls  = ["https://api.openai.com/v1"]

  也就是說：一個以「資料不出公司」為目標的堆疊，**預設就把 OpenAI 官方
  API 接上了，而且開著**。而 OPENAI_API_KEY 是很多開發機上全域匯出的環境
  變數，一旦有，模型就會出現在選單裡，公司資料可以在沒有人在本專案設定過
  的情況下送出去。

  這與 ENABLE_MCP（不存在的變數）、ENABLE_SIGNUP（只第一次開機有效）是同一
  類：**看起來是關的，其實不是。** 這支探針把「其實不是」變成看得見的東西。

用法（通常透過包裝腳本 scripts/check-egress.sh）：
    python3 egress_probe.py scan [--db PATH] [--json]

結束碼：0 = 沒有啟用中的外部端點；1 = 有；2 = 無法判定（讀不到資料庫）

安全：本腳本**不印出任何金鑰值**，只印「已設定／未設定」。
"""

import argparse
import json
import re
import sqlite3
import sys

DEFAULT_DB = "/app/backend/data/webui.db"

# 值裡面可能出現 URL 的欄位：有些是純字串，有些是清單（openai.api_base_urls）。
_URL_RE = re.compile(r"https?://[^\s\"'\\,)\]]+")


def is_private_url(url):
    """這個位址是否在私有網路／本機上。

    與 probe_openai.py 的 _is_private_host 同一條規則，理由也一樣：
    判準不是「有沒有對外」，而是「**這個位址**算不算離開公司」。
    localhost、docker service name、RFC1918 都算留在自己家裡。
    """
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 - 解析不出來時保守地當成外部
        return False
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if "." not in host:                     # docker service name
        return True
    if host.endswith(".local"):
        return True
    if re.match(r"^(10\.|192\.168\.|127\.|169\.254\."
                r"|172\.(1[6-9]|2[0-9]|3[01])\.)", host):
        return True
    if host.startswith(("fc", "fd", "fe80")):   # IPv6 私有 / link-local
        return True
    return False


def urls_in(value):
    """從一個設定值取出所有 URL。值可能是字串、清單或 dict。"""
    if value is None:
        return []
    if isinstance(value, str):
        return _URL_RE.findall(value)
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(urls_in(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(urls_in(item))
        return out
    return []


def _is_key_name(key):
    """這個設定名稱是不是在放金鑰（而不是在放網址）。

    ⚠ 必須處理**複數**。第一版只比對 `api_key`，於是實際資料庫裡的
    `openai.api_keys`（複數，Open WebUI 的實際欄位名）沒被認出來 ——
    而那是它最該認出來的一個。
    """
    tail = key.rsplit(".", 1)[-1]
    if tail in ("key", "keys", "token", "tokens"):
        return True
    return tail.endswith(("api_key", "api_keys", "_key", "_keys",
                          "_token", "_tokens"))


# 端點欄位常見的結尾。用來還原「這個端點屬於哪個子系統」。
#
# 為什麼不能只用前兩段當前綴：`audio.stt.openai.api_base_url` 的前兩段是
# `audio.stt`，於是同層的 `audio.stt.deepgram.api_key` 也會被當成它的閘門
# ——實測就發生了：探針把 openai 的端點報成「由 deepgram 的金鑰管制」。
# 一支以「不要誤導人」為目的的探針，自己報錯閘門是最糟的失敗。
_URL_SUFFIXES = ("_api_base_url", "_base_url", "_api_url", "_endpoint", "_url")


def _gate_for(key, cfg):
    """找出這個端點被哪個開關管著，以及那個開關現在的值。

    這是**盡力而為的推測**，不是從 Open WebUI 原始碼推導出來的保證 ——
    所以回傳值帶著它是怎麼被判斷的，讓人能自己覆核。不同子系統的閘門長得
    不一樣：有的用 `X.enable`，有的用「有沒有填金鑰」，有的用 engine 欄位。

    回傳 (閘門名稱, 值, 說明)。找不到就回傳 (None, None, 說明)。
    """
    parts = key.split(".")

    # 1. 同前綴的 .enable（web.search.enable、image_generation.enable、
    #    openai.enable …）。由長到短找，取最接近的那一個。
    for depth in range(min(len(parts) - 1, 3), 0, -1):
        cand = ".".join(parts[:depth]) + ".enable"
        if cand in cfg:
            return cand, cfg[cand], ""

    # 2. 沒有 .enable：看同一子系統底下有沒有金鑰欄位 —— 沒填金鑰就用不了。
    #    子系統的還原方式是**從端點欄位名砍掉結尾**，不是取前幾段：
    #      rag.mistral_ocr_api_base_url  → rag.mistral_ocr  → _api_key ✓
    #      audio.stt.openai.api_base_url → audio.stt.openai → .api_key   ✓
    stem = key
    for suffix in _URL_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    key_fields = [k for k in cfg
                  if k != key and k.startswith(stem) and _is_key_name(k)]
    if key_fields:
        filled = [k for k in key_fields if str(cfg[k] or "").strip()]
        return ("（需要金鑰）" + key_fields[0],
                bool(filled),
                "已填金鑰" if filled else "未填金鑰")

    return None, None, "找不到明確開關"


def scan(db_path):
    """回傳 (findings, error)。findings 每項是一個 dict。"""
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return None, f"無法開啟資料庫 {db_path}：{exc}"

    try:
        rows = db.execute("SELECT key, value FROM config").fetchall()
    except sqlite3.Error as exc:
        return None, f"讀取 config 表失敗：{exc}"
    finally:
        db.close()

    cfg = {}
    for key, raw in rows:
        try:
            cfg[key] = json.loads(raw)
        except (TypeError, ValueError):
            cfg[key] = raw

    findings = []
    for key in sorted(cfg):
        # 金鑰欄位本身不是端點，跳過 —— 它會被當成閘門來讀。
        if _is_key_name(key):
            continue
        for url in urls_in(cfg[key]):
            if is_private_url(url):
                continue
            gate, gate_val, note = _gate_for(key, cfg)
            if gate is None:
                state = "待確認"
            elif gate_val:
                state = "啟用中"
            else:
                state = "未啟用"
            findings.append({
                "key": key,
                "url": url,
                "gate": gate or "",
                "gate_value": bool(gate_val) if gate else None,
                "note": note,
                "state": state,
            })

    return findings, None


def cmd_scan(args):
    findings, err = scan(args.db)
    if err:
        print(f"無法判定：{err}", file=sys.stderr)
        return 2

    active = [f for f in findings if f["state"] == "啟用中"]

    if args.json:
        print(json.dumps({"findings": findings,
                          "active_count": len(active)}, ensure_ascii=False))
        return 1 if active else 0

    print("=" * 66)
    print(" 資料可能流向何處 —— 以 Open WebUI 的實際資料庫為準")
    print("=" * 66)
    print(f" 資料庫：{args.db}")
    print()

    if not findings:
        print(" 沒有發現任何指向外部網路的端點設定。")
        print(" （指向 localhost、私有網段、docker service name 的不列入，")
        print("   因為那些沒有離開這台機器。）")
        return 0

    order = {"啟用中": 0, "待確認": 1, "未啟用": 2}
    findings.sort(key=lambda f: (order.get(f["state"], 9), f["key"]))

    mark = {"啟用中": "✗", "待確認": "?", "未啟用": "·"}
    for f in findings:
        print(f"  {mark.get(f['state'], '?')} {f['state']}  {f['key']}")
        print(f"       {f['url']}")
        if f["gate"]:
            extra = f"（{f['note']}）" if f["note"] else ""
            print(f"       開關：{f['gate']} = {f['gate_value']}{extra}")
        else:
            print("       開關：找不到明確的開關，請自行確認")
        print()

    print("-" * 66)
    if active:
        print(f" 有 {len(active)} 個**啟用中**的外部端點。")
        print(" 這些是資料真的會離開這台機器的地方。")
        print()
        print(" 逐一確認那是你要的。若其中一個是 api.openai.com，而你的目標是")
        print(" 「資料不出公司」，那它就是一個必須關掉的洞 —— 執行：")
        print("     bash scripts/check-egress.sh --fix")
    else:
        print(" 沒有**啟用中**的外部端點。")
        print(" 清單上其餘項目是「已設定但關著」或「沒有明確開關」——")
        print(" 它們不是現在的風險，但只要有人打開開關或填入金鑰就會變成風險。")

    print()
    print(" 注意：這支探針只看 Open WebUI 的設定。它**看不到**：")
    print("   · 容器實際對外連了什麼（要看網路層）")
    print("   · 模型本身的訓練資料來源")
    print("   · 程式碼裡寫死的端點")
    print(" 它回答的是「設定上允許資料去哪裡」，不是「資料實際去了哪裡」。")

    return 1 if active else 0


def main(argv):
    parser = argparse.ArgumentParser(description="外部端點設定掃描")
    parser.add_argument("--db", default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="掃描並列出外部端點")
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
