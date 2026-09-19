#!/usr/bin/env python3
"""讀出（或設定）**應用程式實際會用到的** LLM Runtime 設定。

必須在 open-webui 容器內執行 —— 它 import 應用程式自己的模組。

為什麼要有這支，而不是直接 `SELECT value FROM config`：

  直接讀資料列，讀到的是「我剛才寫進去的東西」。然後拿它來證明「設定生效了」，
  是**循環論證** —— 用自己寫的值驗證自己寫的值。本專案已經吃過兩次同一形狀的
  虧：lock-signup.sh 舊版讀 .env 而回報假成功（D-012），以及 check-egress.sh
  第一版讀資料列來確認自己剛寫的資料列（D-017）。

  這支走的是應用程式**自己的讀取路徑** —— `Config.get_many`，也就是
  `routers/openai.py` 的 `get_all_models` 實際呼叫的那個函式。讀出來的值
  經過與正式路徑完全相同的解析（含 `persistent_enabled_for` 判斷）。
  差別是：這條路徑不會說謊。

2026-09-19 用誘餌驗證過這條路徑確實反映真實設定：
  把 openai.api_base_urls 指向一個受控的誘餌服務後，這支讀出來的是誘餌網址。

用法（通常由 scripts/connect-endpoint.sh 呼叫）：
  python3 - show
  python3 - set-openai --url https://xxx.trycloudflare.com/v1 [--key K]
  python3 - disable-openai

安全：**永不印出金鑰值**，只印「已設定／未設定」。
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time

DB_PATH = "/app/backend/data/webui.db"

# 明確拒接的位址。本專案的目標是「資料由公司自己控制」，把模型來源接到
# OpenAI 官方 API 與那個目標直接衝突，所以它必須是**明示的例外**，
# 而不是一個不小心就會滑進去的預設值（D-017）。
REFUSED_BASE_URLS = {
    "https://api.openai.com/v1",
    "https://api.openai.com",
}


def _app_config():
    """載入應用程式自己的 Config 類別（會一併套用它的 persistent 規則）。"""
    import open_webui.config  # noqa: F401  這行會執行 Config.configure(defaults=DEFAULT_CONFIG)
    from open_webui.models.config import Config
    return Config


def _effective(Config, keys):
    async def run():
        return await Config.get_many(*keys)
    return asyncio.run(run())


def _mask(value):
    """金鑰只說有沒有，不說內容也不說長度。"""
    if value is None:
        return "（不存在）"
    if isinstance(value, list):
        return [("已設定" if str(v or "").strip() else "未設定") for v in value]
    return "已設定" if str(value or "").strip() else "未設定"


def _write(updates):
    """寫入資料庫。呼叫端負責重啟容器讓它生效。"""
    db = sqlite3.connect(DB_PATH)
    now = int(time.time())
    for key, value in updates.items():
        db.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value), now))
    db.commit()
    db.close()


def cmd_show(args):
    Config = _app_config()
    got = _effective(Config, ("ollama.enable", "ollama.base_urls",
                              "openai.enable", "openai.api_base_urls",
                              "openai.api_keys"))

    ollama_on = bool(got.get("ollama.enable"))
    openai_on = bool(got.get("openai.enable"))
    openai_urls = got.get("openai.api_base_urls") or []
    keys = _mask(got.get("openai.api_keys"))

    print("=" * 64)
    print(" LLM Runtime 現況（讀自應用程式自己的 Config 路徑）")
    print("=" * 64)
    print()
    print(f" Ollama（原生協定，不經 OpenAI 介面）")
    print(f"   啟用        ：{'是' if ollama_on else '否'}")
    print(f"   位址        ：{got.get('ollama.base_urls')}")
    print()
    print(f" OpenAI-compatible（統一介面 —— runtime 的替換點）")
    print(f"   啟用        ：{'是' if openai_on else '否'}")
    print(f"   位址        ：{openai_urls}")
    print(f"   金鑰        ：{keys}")
    print()

    if openai_on and openai_urls:
        print(f" 目前模型來源：Ollama{' + ' if ollama_on else ''}"
              f"{'1 個外部 runtime' if len(openai_urls) == 1 else f'{len(openai_urls)} 個外部 runtime'}")
    else:
        print(f" 目前模型來源：{'Ollama' if ollama_on else '（無）'}")

    if args.json:
        print()
        print(json.dumps({
            "ollama": {"enable": ollama_on,
                       "base_urls": got.get("ollama.base_urls")},
            "openai": {"enable": openai_on,
                       "base_urls": openai_urls,
                       "keys_set": [bool(str(k or "").strip())
                                    for k in (got.get("openai.api_keys") or [])]},
        }, ensure_ascii=False))

    return 0


def cmd_set_openai(args):
    url = (args.url or "").rstrip("/")
    if not url:
        print("需要 --url", file=sys.stderr)
        return 2

    if url in REFUSED_BASE_URLS or url in {u.rstrip("/") for u in REFUSED_BASE_URLS}:
        print(f"拒絕接上 {url}", file=sys.stderr)
        print("  這是 OpenAI 官方 API。本專案的目標是「資料由公司自己控制」，"
              "接上它與該目標直接衝突。", file=sys.stderr)
        print("  若你確實要接，請自行設定，不要用這支腳本。", file=sys.stderr)
        return 1

    key = args.key or ""
    _write({
        "openai.enable": True,
        "openai.api_base_urls": [url],
        "openai.api_keys": [key],
    })
    print(f"  已寫入 openai.enable = true")
    print(f"  已寫入 openai.api_base_urls = ['{url}']")
    print(f"  已寫入 openai.api_keys = ['{'已設定' if key else '未設定'}']")
    return 0


def cmd_check(args):
    """OpenAI-compatible runtime 是否已接上。

    結束碼 0 = 已啟用且有位址。這**只代表設定就緒**，不代表模型抓得到 ——
    實際抓取需要通過認證的請求觸發（見 D-017 的界線說明）。
    """
    Config = _app_config()
    got = _effective(Config, ("openai.enable", "openai.api_base_urls"))
    on = bool(got.get("openai.enable"))
    urls = [u for u in (got.get("openai.api_base_urls") or []) if str(u).strip()]
    if on and urls:
        print(f"OpenAI-compatible runtime 已接上：{urls[0]}")
        return 0
    print("OpenAI-compatible runtime 未接上。")
    return 1


def cmd_disable_openai(args):
    _write({"openai.enable": False})
    print("  已寫入 openai.enable = false")
    print("  （位址與金鑰刻意保留 —— 只關閘門。要清除請自行處理。）")
    return 0


def main(argv):
    parser = argparse.ArgumentParser(description="LLM Runtime 設定讀寫")
    sub = parser.add_subparsers(dest="cmd")

    p_show = sub.add_parser("show")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_set = sub.add_parser("set-openai")
    p_set.add_argument("--url", required=True)
    p_set.add_argument("--key", default="")
    p_set.set_defaults(func=cmd_set_openai)

    p_off = sub.add_parser("disable-openai")
    p_off.set_defaults(func=cmd_disable_openai)

    p_chk = sub.add_parser("check")
    p_chk.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
