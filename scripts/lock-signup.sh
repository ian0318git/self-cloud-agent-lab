#!/usr/bin/env bash
# 確認 Open WebUI 的「開放註冊」是關著的；若開著，就把它關掉。
#
# ── 這支腳本為什麼重寫過（2026-09）────────────────────────
# 舊版把 ENABLE_SIGNUP=false 寫進 .env 再重啟容器，然後回報成功。
# 實測證明那是**靜默空操作**：改 .env + 重建容器之後，註冊仍然是開的。
#
# 原因：註冊端點的執法點讀的是資料庫（auths.py → Config.get('ui.enable_signup')），
# 而環境變數只在資料庫「沒有」該 key 時才生效 —— 第一次開機就已經寫進去了。
# 舊版還有一個假成功路徑：它的冪等檢查讀的是 .env 而不是真實狀態，所以
# .env 是 false 但實際開著時，它會印「已是 false，無需變更」就結束。
#
# 因此新版改成：**一律以 HTTP 讀到的真實狀態為準**，並且在變更之後讀回確認。
#
# 證據與實驗：scripts/verify-lock-signup.sh（三次開機證明 .env 無效）
#             scripts/signup_control_probe.py（證明哪條路真的有效）
#
# 用法：
#   bash scripts/lock-signup.sh              驗證，若開著就詢問是否關閉
#   bash scripts/lock-signup.sh --check      只驗證，不做任何變更（適合放進部署流程）
#   bash scripts/lock-signup.sh --url URL    指定 Open WebUI 網址
#
# 非互動（自動化）用法：設定環境變數後加上 --yes
#   OPENWEBUI_ADMIN_EMAIL=... OPENWEBUI_ADMIN_PASSWORD=... bash scripts/lock-signup.sh --yes
#
# 結束碼：0 = 已確認關閉；1 = 開著或無法關閉；2 = 連不上

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

URL="${OPENWEBUI_URL:-http://localhost:3000}"
MODE="interactive"   # interactive | check
ASSUME_YES=0
PROBE="$PROJECT_ROOT/scripts/signup_state.py"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE="check" ;;
    --yes|-y) ASSUME_YES=1 ;;
    --url) shift; URL="${1:-}" ;;
    --url=*) URL="${1#--url=}" ;;
    -h|--help)
      cat <<'USAGE'
確認 Open WebUI 的「開放註冊」是關著的；若開著，就把它關掉。

用法：
  bash scripts/lock-signup.sh              驗證，若開著就詢問是否關閉
  bash scripts/lock-signup.sh --check      只驗證，不做任何變更（適合放進部署流程）
  bash scripts/lock-signup.sh --url URL    指定 Open WebUI 網址

非互動（自動化）用法：
  OPENWEBUI_ADMIN_EMAIL=... OPENWEBUI_ADMIN_PASSWORD=... \
    bash scripts/lock-signup.sh --yes

結束碼：0 = 已確認關閉；1 = 開著或無法關閉；2 = 連不上

注意：ENABLE_SIGNUP 環境變數只在「資料庫還沒有這個 key」時有效，
      也就是只有第一次開機。之後改 .env 再重啟不會有任何效果。
USAGE
      exit 0
      ;;
    *) fail "未知的參數：$1"; exit 2 ;;
  esac
  shift
done

if [[ ! -f "$PROBE" ]]; then
  fail "找不到 $PROBE"
  exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
  fail "需要 python3 才能執行 $PROBE"
  exit 2
fi

# 讓 .env 與真實狀態一致，這樣將來若 volume 重建（全新資料庫），第一次開機
# 就是關的。**只對將來的全新資料庫有效** —— 對目前這個已經有資料庫的實例
# 沒有任何作用，所以它不能取代下面的讀回確認。
#
# 定義在檔案的頂層：先前把它寫在 `status == 0` 的分支裡，那個分支會 exit，
# 於是「關閉成功」那條路徑上函式根本不存在。
sync_env_hint() {
  local env_file="$PROJECT_ROOT/.env"

  # .env 的同步只是「順手」，失敗不該讓一件已經成功的事被回報成失敗。
  # 實測確認：在 set -e 下，函式回傳非零會直接中止呼叫端 —— 所以這裡若讓
  # sed 的失敗往外傳，會變成「註冊其實已經關好了，但腳本結束碼非 0」，
  # 而那個非 0 會被部署流程當成沒關成功。故一律 return 0。
  if [[ ! -f "$env_file" ]]; then
    warn "找不到 .env，略過同步（不影響已完成的結果）"
    return 0
  fi
  if grep -qE '^ENABLE_SIGNUP=false$' "$env_file"; then
    return 0
  fi

  if grep -qE '^ENABLE_SIGNUP=' "$env_file"; then
    if sed -i.bak 's/^ENABLE_SIGNUP=.*/ENABLE_SIGNUP=false/' "$env_file"; then
      rm -f "$env_file.bak"
    else
      warn "寫入 .env 失敗，略過（不影響已完成的結果）"
      return 0
    fi
  elif ! printf '\n# 由 scripts/lock-signup.sh 加入\nENABLE_SIGNUP=false\n' >>"$env_file"; then
    warn "寫入 .env 失敗，略過（不影響已完成的結果）"
    return 0
  fi

  info "已同步 .env 的 ENABLE_SIGNUP=false（僅對「將來的全新資料庫」有效）"
  return 0
}

# 先讀真實狀態。這一步不需要任何憑證 —— /api/config 是未認證端點。
status=0
python3 "$PROBE" check --url "$URL" || status=$?

if [[ $status -eq 2 ]]; then
  fail "無法連線到 $URL"
  echo "  請確認 Open WebUI 正在執行。若堆疊沒起來：bash scripts/up.sh"
  echo "  若服務在別的位址，用 --url 指定，或設定 OPENWEBUI_URL。"
  exit 2
fi

if [[ $status -eq 0 ]]; then
  sync_env_hint
  exit 0
fi

# status == 1：註冊是開著的
echo
if [[ "$MODE" == "check" ]]; then
  fail "註冊仍為開啟狀態。"
  echo "  關閉方式：bash scripts/lock-signup.sh"
  exit 1
fi

warn "註冊目前是開著的 —— 任何能連到 $URL 的人都能自行建立帳號。"
echo "  關閉需要一組管理員帳號（會用來呼叫設定 API，不會被儲存或顯示）。"
echo

if [[ -n "${OPENWEBUI_ADMIN_EMAIL:-}" && -n "${OPENWEBUI_ADMIN_PASSWORD:-}" ]]; then
  email="$OPENWEBUI_ADMIN_EMAIL"
  password="$OPENWEBUI_ADMIN_PASSWORD"
  info "使用環境變數提供的管理員帳號"
elif [[ -t 0 ]]; then
  read -r -p "  管理員 email: " email
  read -r -s -p "  管理員密碼（輸入不會顯示）: " password
  echo
else
  fail "非互動環境，且未設定 OPENWEBUI_ADMIN_EMAIL／OPENWEBUI_ADMIN_PASSWORD。"
  echo "  或者手動關閉：Admin 設定 → 一般 → 關閉「允許新使用者註冊」。"
  exit 1
fi

if [[ -z "$email" || -z "$password" ]]; then
  fail "沒有取得完整的帳號資訊，未做任何變更。"
  exit 1
fi

if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then
  read -r -p "  確定要關閉註冊嗎？[y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    info "已取消，未做任何變更。"
    exit 1
  fi
fi

echo
close_status=0
python3 "$PROBE" close --url "$URL" --email "$email" --password "$password" || close_status=$?

# 不把憑證留在這個 shell 的變數裡
unset password

if [[ $close_status -eq 0 ]]; then
  sync_env_hint
  exit 0
fi

fail "未能確認註冊已關閉。"
echo "  備援做法：以管理員登入 → Admin 設定 → 一般 → 關閉「允許新使用者註冊」。"
echo "  關閉後可再跑一次確認：bash scripts/lock-signup.sh --check"
exit 1
