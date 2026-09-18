#!/usr/bin/env bash
# 關閉 Open WebUI 的註冊功能，避免任何能觸及它的人自行建立帳號。
#
# 為什麼需要這一步：Open WebUI 的「第一個」註冊者會自動成為管理員，而只要
# ENABLE_SIGNUP=true，任何人都能建立帳號並使用你的模型。compose 檔與
# .env.example 都有註解提醒，但提醒會被忘記 —— 所以做成腳本。
#
# 時機：建立完自己的管理員帳號之後、把服務對外之前。
#
# 冪等：已經是 false 就直接結束，不會重啟容器。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

ENV_FILE="$PROJECT_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  fail "找不到 $ENV_FILE。請先執行：bash scripts/up.sh"
  exit 1
fi

read_setting() {
  grep -E '^ENABLE_SIGNUP=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

current="$(read_setting)"

if [[ "$current" == "false" ]]; then
  ok "ENABLE_SIGNUP 已是 false，無需變更"
  exit 0
fi

info "將 ENABLE_SIGNUP 設為 false（目前：${current:-未設定}）..."
if grep -qE '^ENABLE_SIGNUP=' "$ENV_FILE"; then
  sed -i.bak 's/^ENABLE_SIGNUP=.*/ENABLE_SIGNUP=false/' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
else
  printf '\n# 由 scripts/lock-signup.sh 加入\nENABLE_SIGNUP=false\n' >> "$ENV_FILE"
fi

# 確認真的寫進去了 —— 不讓「以為改了但其實沒改」發生
if [[ "$(read_setting)" != "false" ]]; then
  fail "寫入失敗，ENABLE_SIGNUP 仍不是 false。請手動檢查 $ENV_FILE"
  exit 1
fi
ok ".env 已更新"

# 必須把新值也放進 shell 環境，否則下面的 up -d 會用「舊值」重建容器。
#
# 原因：compose 的變數優先序是「shell 環境 > .env 檔」，而上面的 load_env
# 已經把 .env 的舊值（true）匯出到 shell 了。只改檔案、不重設環境變數，
# 容器會被以 ENABLE_SIGNUP=true 重建 —— 指令回報成功，功能卻沒關掉。
# 這與 D-011 記錄的 OLLAMA_MODEL 覆蓋問題是同一個根因：load_env 會把
# .env 灌進 shell，此後 shell 的值就壓過檔案。
export ENABLE_SIGNUP=false

if $COMPOSE ps --status running --services 2>/dev/null | grep -qx open-webui; then
  info "重啟 open-webui 使設定生效..."
  if $COMPOSE up -d open-webui; then
    ok "已重啟，註冊功能關閉"
    echo "   既有帳號不受影響，仍可正常登入。"
  else
    fail "重啟失敗。設定已寫入 .env，下次啟動時會生效。"
    exit 1
  fi
else
  warn "open-webui 目前未在執行，設定將於下次啟動時生效"
fi
