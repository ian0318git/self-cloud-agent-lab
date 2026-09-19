#!/usr/bin/env python3
"""讀取與變更 Open WebUI 的註冊開關。

為什麼是 Python 而不是 bash + curl：
  這裡要處理 JSON、bearer token，而且每個動作都必須「送出後讀回確認」。
  用 shell 拼 JSON 字串會讓錯誤路徑很難寫對，也很難測。HTTP 交給 Python，
  流程與提示文字交給 lock-signup.sh。

為什麼 check 讀的是 /api/config：
  那是未認證端點（登入頁需要它才知道要不要顯示註冊表單），而它回傳的
  features.enable_signup 就是註冊端點 auths.py 執法時讀的同一個值
  （Config.get('ui.enable_signup')）。所以它看到的就是真正在執法的那個值 ——
  不是另一個長得像的旗標。**不要改讀 .env，那正是原本腳本錯的地方。**

子命令：
  check                      讀出真正的狀態
  close --email E --password P   登入管理員 → 關閉註冊 → 讀回確認

結束碼：
  0  已關閉（check 與 close 皆同）
  1  開關是開的（check）；或關不掉／無法確認（close）
  2  連不上，或回應無法解析
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

EXIT_LOCKED = 0
EXIT_OPEN = 1
EXIT_UNREACHABLE = 2


def call(base, method, path, body=None, token=None, timeout=20):
    """回傳 (status_code, parsed)。連線失敗時 status 為 None。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, None
    except Exception:
        return None, None


def read_flag(base):
    """回傳 True／False；無法判定時回傳 None。"""
    status, body = call(base, "GET", "/api/config")
    if status != 200 or not isinstance(body, dict):
        return None
    features = body.get("features")
    if not isinstance(features, dict) or "enable_signup" not in features:
        return None
    return bool(features["enable_signup"])


def cmd_check(base):
    flag = read_flag(base)
    if flag is None:
        print(f"✗ 無法從 {base}/api/config 讀出註冊開關。")
        print("  請確認 Open WebUI 正在執行，且這個網址正確。")
        return EXIT_UNREACHABLE

    if flag:
        print("✗ 註冊是**開啟**的（features.enable_signup = true）")
        print("  任何能連到 Open WebUI 的人都可以自行建立帳號。")
        print("  注意：第一位註冊者會成為管理員。")
        return EXIT_OPEN

    print("✓ 註冊已關閉（features.enable_signup = false）")
    return EXIT_LOCKED


def cmd_close(base, email, password):
    before = read_flag(base)
    if before is None:
        print(f"✗ 無法從 {base}/api/config 讀出註冊開關，未做任何變更。")
        return EXIT_UNREACHABLE

    if not before:
        print("✓ 註冊已經是關閉的，無需變更。")
        return EXIT_LOCKED

    # ── 登入取得 admin token ──
    # 絕不把 email／password／token 印出來，連長度或前幾個字都不印。
    status, body = call(
        base, "POST", "/api/v1/auths/signin", {"email": email, "password": password}
    )
    if status != 200 or not isinstance(body, dict) or not body.get("token"):
        print(f"✗ 管理員登入失敗（HTTP {status}）。")
        if status == 400:
            print("  帳號或密碼不正確。")
        elif status is None:
            print("  連線失敗。")
        return EXIT_OPEN

    token = body["token"]
    if body.get("role") != "admin":
        print("✗ 這個帳號不是管理員，無法變更設定。")
        return EXIT_OPEN
    print("✓ 已以管理員身分登入")

    # ── 關閉 ──
    # 送出的 body 只有這一個 key；configs.py 的 import 是 Config.upsert，
    # 只會動傳入的 key，不會洗掉其他設定（已實測：398 → 398 個設定項）。
    status, _ = call(
        base,
        "POST",
        "/api/v1/configs/import",
        {"config": {"ui.enable_signup": False}},
        token=token,
    )
    if status != 200:
        print(f"✗ 送出變更失敗（HTTP {status}）。")
        return EXIT_OPEN

    # ── 讀回確認 ──
    # 這一步不能省。原本的腳本就是回報成功但實際沒生效；HTTP 200 只代表
    # 請求被接受，不代表值真的變了。要以重新讀到的值為準。
    after = read_flag(base)
    if after is None:
        print("✗ 已送出變更，但讀不回狀態，無法確認是否生效。")
        print("  請手動確認：Admin 設定 → 一般 → 關閉註冊。")
        return EXIT_OPEN

    if after:
        print("✗ 讀回值仍是 true —— 變更沒有生效。")
        return EXIT_OPEN

    print("✓ 讀回確認：features.enable_signup = false")
    print("  既有帳號不受影響，仍可正常登入。")
    return EXIT_LOCKED


def main():
    parser = argparse.ArgumentParser(description="讀取或關閉 Open WebUI 的註冊開關")
    parser.add_argument("command", choices=["check", "close"])
    parser.add_argument("--url", default="http://localhost:3000")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    args = parser.parse_args()

    if args.command == "check":
        return cmd_check(args.url)

    if not args.email or not args.password:
        print("✗ close 需要 --email 與 --password（或由 lock-signup.sh 帶入）。")
        return EXIT_OPEN
    return cmd_close(args.url, args.email, args.password)


if __name__ == "__main__":
    sys.exit(main())
