#!/usr/bin/env bash
# 實測：volume 一旦建立之後，ENABLE_SIGNUP 這個環境變數還能不能改變註冊開關？
#
# 為什麼要實測，而不是讀原始碼就好：
#   這個專案已經在「用推論判斷 compose／設定行為」上錯了兩次（D-011 的
#   OLLAMA_MODEL、D-012 的 ENABLE_SIGNUP）。原始碼顯示執法點讀的是 DB
#   （auths.py:909 → Config.get('ui.enable_signup')），而 config 的讀取
#   邏輯是「DB 有值就用 DB，沒有才回退到 env」。這個推論必須被**觀察到**，
#   不能只被讀到 —— 讀到的是程式碼的路徑，觀察到的才是它的行為。
#
# 實驗設計（三次開機，共用同一個 volume）：
#   開機 1  ENABLE_SIGNUP=true    全新 volume    → 預期 true
#   開機 2  ENABLE_SIGNUP=false   同一個 volume  → 這正是 lock-signup.sh 的情境
#   開機 3  ENABLE_SIGNUP=true    同一個 volume  → 對稱檢查（反向也無效嗎）
#
#   判讀：若開機 2 仍回報 true，代表改 .env + 重啟**無效**，
#         lock-signup.sh 是靜默空操作。若回報 false，代表它有效。
#
# 觀測點：GET /api/config 的 features.enable_signup（main.py:2302）。
#   選這個端點的理由：它是未認證的（登入頁需要它才知道要不要顯示註冊表單），
#   而且回傳的就是 Config.get('ui.enable_signup') —— 跟註冊端點 auths.py:909
#   的執法點讀的是同一個值。所以它觀察到的就是真正在執法的那個值，
#   不是另一個長得像的旗標。
#
# 安全性：使用拋棄式的容器與 volume，**完全不碰** 正式堆疊與 .env。
#         對外埠只綁 127.0.0.1。
#
# 用法：bash scripts/verify-lock-signup.sh [--keep]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker

IMAGE="ghcr.io/open-webui/open-webui:main"
CONTAINER="owui-signup-probe"
VOLUME="owui-signup-probe-data"
PORT="${PROBE_PORT:-18080}"
SECRET="probe-secret-not-a-real-key"
KEEP=0
BOOT_TIMEOUT=180   # 秒。首次開機要跑資料庫 migration，慢一點是正常的。

[[ "${1:-}" == "--keep" ]] && KEEP=1

cleanup() {
  if [[ $KEEP -eq 1 ]]; then
    warn "--keep：保留容器 $CONTAINER 與 volume $VOLUME 供檢查"
    warn "  檢查完請自行清除：docker rm -f $CONTAINER && docker volume rm $VOLUME"
    return
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ── 前置檢查 ────────────────────────────────────────────
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  fail "找不到映像檔 $IMAGE。"
  echo "  請先下載：docker pull $IMAGE"
  exit 1
fi

if curl -sf -m 2 "http://127.0.0.1:$PORT/api/config" >/dev/null 2>&1; then
  fail "埠 $PORT 已有服務在回應，怕誤測到別的實例。"
  echo "  請改用其他埠：PROBE_PORT=18081 bash scripts/verify-lock-signup.sh"
  exit 1
fi

# 讀出真正在執法的那個值。讀不到就回傳空字串（呼叫端據此判斷失敗）。
read_signup_flag() {
  curl -s -m 10 "http://127.0.0.1:$PORT/api/config" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin)
    print(str(cfg["features"]["enable_signup"]).lower())
except Exception:
    pass
' 2>/dev/null || true
}

boot() {
  local flag="$1"

  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

  docker run -d --name "$CONTAINER" \
    -p "127.0.0.1:$PORT:8080" \
    -e "WEBUI_SECRET_KEY=$SECRET" \
    -e "ENABLE_SIGNUP=$flag" \
    -v "$VOLUME:/app/backend/data" \
    "$IMAGE" >/dev/null

  local waited=0 value=""
  while [[ $waited -lt $BOOT_TIMEOUT ]]; do
    value="$(read_signup_flag)"
    [[ -n "$value" ]] && return 0

    # 容器若已經死掉，就沒必要繼續等到逾時 —— 直接把它為什麼死講出來
    if ! docker ps --filter "name=^${CONTAINER}$" --filter status=running -q | grep -q .; then
      fail "容器在啟動過程中停止。最後 20 行日誌："
      docker logs --tail 20 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2
      return 1
    fi

    sleep 3
    waited=$((waited + 3))
  done

  fail "等待 $BOOT_TIMEOUT 秒後仍讀不到 /api/config。最後 20 行日誌："
  docker logs --tail 20 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2
  return 1
}

# 每一輪的觀測值放這裡，最後一起判讀
declare -a OBSERVED=()

run_boot() {
  local flag="$1" label="$2"

  info "$label"
  if ! boot "$flag"; then
    OBSERVED+=("")
    return 1
  fi

  local v
  v="$(read_signup_flag)"
  OBSERVED+=("$v")
  printf '    ENABLE_SIGNUP=%-5s  →  features.enable_signup = %s\n' "$flag" "${v:-（讀不到）}"
  return 0
}

# ── 開始 ────────────────────────────────────────────────
echo "── lock-signup 有效性實測 ─────────────────────"
echo "映像檔：$IMAGE"
echo "比對的是：/api/config 的 features.enable_signup（與註冊端點同一個執法值）"
echo

docker volume create "$VOLUME" >/dev/null

# 每輪都接 `|| true`：實測確認（t2.sh）在 `set -e` 下，函式回傳非零會直接
# 中止整個腳本 —— 那樣就永遠印不到下面的判讀，而判讀才是這個腳本的重點。
# 開機失敗在這裡是「預期內的可能結果」，必須讓它繼續走下去。
run_boot "true"  "開機 1／3：全新 volume，ENABLE_SIGNUP=true（建立基準）" || true
echo
run_boot "false" "開機 2／3：同一個 volume，ENABLE_SIGNUP=false（lock-signup.sh 的情境）" || true
echo
run_boot "true"  "開機 3／3：同一個 volume，ENABLE_SIGNUP=true（對稱檢查）" || true
echo

# ── 判讀 ────────────────────────────────────────────────
b1="${OBSERVED[0]:-}"; b2="${OBSERVED[1]:-}"; b3="${OBSERVED[2]:-}"

echo "── 結果 ───────────────────────────────────────"
printf '  開機 1（env=true ，全新 volume）：%s\n' "${b1:-讀不到}"
printf '  開機 2（env=false，已存在）    ：%s\n' "${b2:-讀不到}"
printf '  開機 3（env=true ，已存在）    ：%s\n' "${b3:-讀不到}"
echo

if [[ -z "$b1" || -z "$b2" ]]; then
  fail "實驗未完成（有開機回合讀不到值），無法判讀。"
  echo "  請保留上方輸出，或加 --keep 保留容器以便檢查日誌。"
  exit 1
fi

if [[ "$b2" == "true" ]]; then
  fail "結論：ENABLE_SIGNUP 的 .env 值在 volume 建立後**無效**。"
  cat <<EOF

  env 設成 false、容器也重建了，註冊開關仍然是 true。
  這證實 scripts/lock-signup.sh 是**靜默空操作** —— 它會回報成功，
  但註冊功能其實還開著。任何能連到 Open WebUI 的人都能自行註冊。

  根因：註冊端點的執法點讀的是資料庫（Config.get('ui.enable_signup')），
  而 env 只在資料庫沒有該 key 時才生效。第一次開機已經把它寫進資料庫了。

  可行動的替代做法見 DECISIONS.md D-012。
EOF
  exit 1
fi

if [[ "$b1" == "false" && "$b2" == "true" ]]; then
  ok "結論：ENABLE_SIGNUP 有效（但方向與預期相反）—— 需要進一步判讀。"
  echo "  全新 volume 用 env=false 竟得到 false、之後改 true 卻生效，"
  echo "  與原始碼的推論不符。這是重要發現，請保留完整輸出。"
  exit 0
fi

ok "結論：ENABLE_SIGNUP 的 .env 值在 volume 建立後仍然有效。"
echo "  scripts/lock-signup.sh 的做法成立，無需修改。"
echo
echo "  注意：這與原始碼推論不符（Config.get 應以 DB 優先）。"
echo "  可能是此映像檔版本的持久化機制與 main 分支不同，請記錄此差異。"
