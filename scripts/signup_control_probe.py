#!/usr/bin/env python3
"""註冊開關「控制機制」實測的 HTTP 部分。

背景：scripts/verify-lock-signup.sh 已用三次開機證明「改 .env + 重啟」無效。
      那支腳本回答的是「.env 這條路能不能用」。這支回答的是下一個問題：
      **那到底什麼能控制它？**

三個候選機制，逐一實測：
  A. 第一位註冊者自動鎖上（auths.py:878 的 Config.upsert）
  B. POST /api/v1/configs/import（需 admin token；configs.py:101 → Config.upsert）
  C. 反過來把開關打開，確認它真的是被這條路控制，而不是碰巧

為什麼每一步都要驗「讀回值」而不是只看 HTTP 200：
  lock-signup.sh 的教訓就是回報成功但實際沒生效。所以這個探針一律以
  GET /api/config 的觀測值、以及**註冊端點實際回傳的狀態碼**為準。

最關鍵的一步是最後一步：把開關關掉之後，真的送出註冊請求，確認拿到 403。
  只看 /api/config 的旗標，仍然可能是「旗標關了但端點沒在管」。
  要證明鎖得住，就得實際去撞它。

用法： python3 signup_control_probe.py <base_url>    例如 http://127.0.0.1:18080
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:18080"

RESULTS = []


def record(ok, label, detail=""):
    RESULTS.append((ok, label))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {label}")
    if detail:
        print(f"      {detail}")


def call(method, path, body=None, token=None, timeout=30):
    """回傳 (status_code, parsed_json_or_None)。HTTP 錯誤不拋例外，回傳狀態碼。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
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
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, None
    except Exception as exc:  # 連線層級的失敗：明確回報，不要當成「通過」
        return None, {"_error": str(exc)}


def signup_flag():
    """讀出真正在執法的那個值；讀不到回傳 None。"""
    status, body = call("GET", "/api/config")
    if status != 200 or not isinstance(body, dict):
        return None
    try:
        return bool(body["features"]["enable_signup"])
    except (KeyError, TypeError):
        return None


def try_signup(email, name):
    return call(
        "POST",
        "/api/v1/auths/signup",
        {"name": name, "email": email, "password": "Probe-password-1234"},
    )


print(f"目標：{BASE}")
print()

# ── 步驟 0：基準狀態 ────────────────────────────────────
print("[0] 基準狀態")
base_flag = signup_flag()
if base_flag is None:
    print("  ✗ 讀不到 /api/config，無法繼續。容器還沒起來？")
    sys.exit(1)
record(base_flag is True, f"全新 volume、ENABLE_SIGNUP=true → 開關為 {base_flag}")
print()

# ── 步驟 1：第一位註冊者是否自動上鎖（機制 A）────────────
print("[1] 機制 A：第一位註冊者是否自動關閉註冊？")
status, body = try_signup("admin@example.com", "Probe Admin")
if status != 200 or not isinstance(body, dict) or "token" not in body:
    record(False, f"第一位註冊者應成功（實得 HTTP {status}）", str(body)[:200])
    sys.exit(1)
admin_token = body["token"]
record(True, "第一位註冊者成功建立，並取得 token")

flag_after_first = signup_flag()
record(
    flag_after_first is False,
    f"第一位註冊者建立後，開關自動變為 {flag_after_first}",
    "原始碼 auths.py:878 會在「使用者總數 == 1」時 upsert ui.enable_signup=False",
)

# 端點與形狀皆為實測確認（第一次寫成 GET /api/v1/auths/ 並預期 list，
# 但那個端點回的是「目前登入者」的 dict，不是清單 —— 於是誤判為失敗）。
# 列出所有使用者的端點是 GET /api/v1/users/，回傳 {"users": [...], "total": N}。
status, payload = call("GET", "/api/v1/users/", token=admin_token)
users = payload.get("users") if isinstance(payload, dict) else None
if status == 200 and isinstance(users, list) and users:
    role = users[0].get("role")
    record(role == "admin", f"第一位註冊者的角色是 {role}")
    record(
        payload.get("total") == 1,
        f"此時使用者總數為 {payload.get('total')}（應為 1）",
        "自動上鎖的條件正是 get_num_users() == 1，所以這個數字要對得上",
    )
else:
    record(False, f"讀不到使用者清單（HTTP {status}，形狀 {type(payload).__name__}）")
print()

# ── 步驟 2：用 import 把開關打開，證明這條路真的在控制它 ──
print("[2] 機制 B：POST /api/v1/configs/import（開啟）")
status, _ = call(
    "POST",
    "/api/v1/configs/import",
    {"config": {"ui.enable_signup": True}},
    token=admin_token,
)
record(status == 200, f"import 回傳 HTTP {status}")

flag_reopened = signup_flag()
record(
    flag_reopened is True,
    f"讀回值：開關 = {flag_reopened}",
    "這一步同時證明「回傳 200」不等於「真的改了」——所以下面一律以讀回值為準",
)

# 真的送一次註冊，確認開關打開時端點確實放行
status2, _ = try_signup("second@example.com", "Probe Second")
record(
    status2 == 200,
    f"開關為 true 時，第二位註冊者被放行（HTTP {status2}）",
    "若這裡沒放行，後面的 403 就不能證明是開關造成的",
)
print()

# ── 步驟 3：用 import 關閉，並用註冊端點撞它 ──────────────
print("[3] 機制 B：POST /api/v1/configs/import（關閉）＋ 實際撞擊")
status, _ = call(
    "POST",
    "/api/v1/configs/import",
    {"config": {"ui.enable_signup": False}},
    token=admin_token,
)
record(status == 200, f"import 回傳 HTTP {status}")

flag_closed = signup_flag()
record(flag_closed is False, f"讀回值：開關 = {flag_closed}")

status3, body3 = try_signup("third@example.com", "Probe Third")
record(
    status3 == 403,
    f"開關為 false 時，註冊端點實際回傳 HTTP {status3}（應為 403）",
    "這才是「鎖得住」的證據：不是旗標好看，而是端點真的拒絕",
)
print()

# ── 步驟 4：部分更新的安全性（會不會洗掉其他設定）────────
print("[4] import 是部分更新還是整份覆蓋？")
status, before = call("GET", "/api/v1/configs/export", token=admin_token)
if status == 200 and isinstance(before, dict):
    n_before = len(before)
    # 只送一個無害的既有 key，觀察其他 key 是否還在
    call(
        "POST",
        "/api/v1/configs/import",
        {"config": {"ui.enable_signup": False}},
        token=admin_token,
    )
    status, after = call("GET", "/api/v1/configs/export", token=admin_token)
    n_after = len(after) if isinstance(after, dict) else -1
    record(
        n_after == n_before,
        f"import 前後設定項數目：{n_before} → {n_after}（未被洗掉）",
        "原始碼 configs.py:102 是 Config.upsert(form_data.config)，只動傳入的 key",
    )
    record(
        (after or {}).get("ui.enable_signup") is False,
        "目標 key 確實被更新",
    )
else:
    record(False, f"讀不到 /api/v1/configs/export（HTTP {status}）")
print()

# ── 判讀 ────────────────────────────────────────────────
failed = [label for ok, label in RESULTS if not ok]
print("── 結果 ───────────────────────────────────────")
if failed:
    print(f"✗ {len(failed)} 項未通過：")
    for f in failed:
        print(f"    • {f}")
    sys.exit(1)

print("✓ 全部通過")
print()
print("結論：控制註冊開關的是資料庫，管道有兩條 ——")
print("  A. 第一位註冊者會自動關閉註冊（Open WebUI 自己的行為，不是我們設的）")
print("  B. POST /api/v1/configs/import 搭配 admin token，可精確、部分地改值")
print("  .env 的 ENABLE_SIGNUP 只在資料庫沒有該 key 時才有效，也就是只有第一次開機。")
