#!/usr/bin/env python3
"""實測改寫後的 scripts/lock-signup.sh 是否真的鎖得住。

為什麼要另外寫一支，而不是相信 signup_state.py 的輸出：
  signup_state.py 說「已關閉」時，那是它自己的判斷。真正的問題是
  「lock-signup.sh 這支腳本，在真實的伺服器上，能不能把事情做成，
   而且失敗時會不會誠實回報」。所以這裡的做法是：
     - 用 HTTP 直接觀察狀態，不呼叫 signup_state.py 的內部函式
     - 用**結束碼**判斷腳本的成功／失敗
     - 最後實際送出註冊請求，用 403 當作「真的鎖住了」的證據
  這樣測試與被測程式之間沒有共用程式碼，測到的才是真的。

用法： python3 verify_lock_signup_script.py <base_url> <repo_root>
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:18080"
ROOT = sys.argv[2] if len(sys.argv) > 2 else "."

ADMIN_EMAIL = "probe-admin@example.com"
ADMIN_PASSWORD = "Probe-admin-password-1234"
THIRD_EMAIL = "probe-third@example.com"

FAILURES = []


def check(ok, label, detail=""):
    if not ok:
        FAILURES.append(label)
    print(f"  {'✓' if ok else '✗'} {label}")
    if detail:
        print(f"      {detail}")


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            try:
                return resp.status, json.loads(resp.read())
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:
            return exc.code, None
    except Exception as exc:
        return None, {"_error": str(exc)}


def flag():
    status, body = call("GET", "/api/config")
    if status != 200 or not isinstance(body, dict):
        return None
    try:
        return bool(body["features"]["enable_signup"])
    except (KeyError, TypeError):
        return None


def run_script(*args):
    """執行 lock-signup.sh，回傳 (結束碼, 輸出)。"""
    env = dict(os.environ)
    env.update(
        {
            "OPENWEBUI_URL": BASE,
            "OPENWEBUI_ADMIN_EMAIL": ADMIN_EMAIL,
            "OPENWEBUI_ADMIN_PASSWORD": ADMIN_PASSWORD,
        }
    )
    proc = subprocess.run(
        ["bash", "scripts/lock-signup.sh", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# .env 會被 lock-signup.sh 的 sync_env_hint 改寫；測試不該動到使用者的設定，
# 所以先備份、結束時還原。
ENV_PATH = os.path.join(ROOT, ".env")
env_backup = ENV_PATH + ".verify-backup"
had_env = os.path.exists(ENV_PATH)
if had_env:
    shutil.copy2(ENV_PATH, env_backup)

try:
    print(f"目標：{BASE}")
    print()

    # ── [1] 建立管理員 ──────────────────────────────────
    print("[1] 建立第一位管理員（Open WebUI 會自動上鎖）")
    status, body = call(
        "POST",
        "/api/v1/auths/signup",
        {"name": "Probe Admin", "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    if status == 200 and isinstance(body, dict) and body.get("token"):
        admin_token = body["token"]
        check(True, "管理員已建立")
    else:
        # 同一個容器重跑時，這個帳號已經存在（或被鎖）—— 改用登入取得 token。
        # 讓測試可以在同一個容器上反覆跑，不必每次重開（開機要 7 分鐘）。
        status2, body2 = call(
            "POST",
            "/api/v1/auths/signin",
            {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        if status2 != 200 or not isinstance(body2, dict) or not body2.get("token"):
            check(False, f"建立／登入管理員都失敗（HTTP {status} / {status2}）", str(body)[:200])
            raise SystemExit(1)
        admin_token = body2["token"]
        check(True, "管理員已存在，改用登入取得 token（重跑）")
    check(flag() is False, "開關為 False", "第一位註冊者會自動上鎖；這是後續測試的前提")

    # ── [2] 重新打開，才能測「關閉」──────────────────────
    print()
    print("[2] 用 configs API 重新打開註冊（製造出「開著且有管理員」的情境）")
    status, _ = call(
        "POST",
        "/api/v1/configs/import",
        {"config": {"ui.enable_signup": True}},
        token=admin_token,
    )
    check(status == 200 and flag() is True, "已重新打開，開關為 True")

    # ── [3] --check 必須誠實回報「開著」──────────────────
    print()
    print("[3] lock-signup.sh --check（開著時）")
    code, out = run_script("--check")
    check(code == 1, f"結束碼為 1（實得 {code}）", "非 0 才會被部署流程視為失敗")
    check("開啟" in out, "輸出明講註冊是開啟的")
    check("已關閉" not in out, "沒有誤報成功", "舊版就是在這種情況下回報成功的")
    check(flag() is True, "--check 不應該改變任何狀態")

    # ── [4] 真的關閉 ────────────────────────────────────
    print()
    print("[4] lock-signup.sh --yes（帶管理員帳密，非互動）")
    code, out = run_script("--yes")
    check(code == 0, f"結束碼為 0（實得 {code}）")
    check("讀回確認" in out, "輸出顯示做了讀回確認，不是只送出不看結果")
    check(flag() is False, "HTTP 觀察：開關確實變成 False")

    # ── [5] 冪等 ────────────────────────────────────────
    print()
    print("[5] 再跑一次 --yes（冪等性）")
    code, out = run_script("--yes")
    check(code == 0, f"結束碼仍為 0（實得 {code}）")
    # 已關閉時腳本會在 check 階段就結束，根本不會去登入。
    # 這比「登入後發現不用改」更好：不需要用到憑證，也少一次密碼暴露。
    # （第一版的斷言寫成期待 close 路徑的訊息，是斷言錯了，不是程式錯了。）
    check("已關閉" in out, "正確辨識為已關閉")
    check("已以管理員身分登入" not in out, "沒有多做一次不必要的登入")

    # ── [6] --check 在關閉狀態下要回報成功 ───────────────
    print()
    print("[6] lock-signup.sh --check（已關閉時）")
    code, out = run_script("--check")
    check(code == 0, f"結束碼為 0（實得 {code}）")
    check("已關閉" in out, "輸出確認已關閉")

    # ── [7] 獨立驗證：實際撞註冊端點 ─────────────────────
    print()
    print("[7] 獨立驗證：實際送出註冊請求，確認端點真的拒絕")
    status, _ = call(
        "POST",
        "/api/v1/auths/signup",
        {"name": "Probe Third", "email": THIRD_EMAIL, "password": "Probe-third-1234"},
    )
    check(
        status == 403,
        f"註冊端點回傳 HTTP {status}（應為 403）",
        "這一步才證明「鎖得住」：不是旗標好看，也不是腳本自己說成功",
    )

finally:
    if had_env and os.path.exists(env_backup):
        shutil.move(env_backup, ENV_PATH)
        print("\n（已還原測試前的 .env）")

print()
print("── 結果 ───────────────────────────────────────")
if FAILURES:
    print(f"✗ {len(FAILURES)} 項未通過：")
    for f in FAILURES:
        print(f"    • {f}")
    sys.exit(1)
print("✓ 全部通過")
