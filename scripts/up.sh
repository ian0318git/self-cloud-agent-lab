#!/usr/bin/env bash
# 啟動第一階段堆疊（Ollama + Open WebUI），並確保模型已下載。
# 此腳本是冪等的：重複執行不會重啟已在運作的容器，也不會重新下載模型。
# 可由 .devcontainer 的 postCreateCommand / postStartCommand 自動呼叫。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

MODEL="${OLLAMA_MODEL:-qwen3:4b}"

# ── Cloudflare Tunnel（選用）────────────────────────────
# 缺 token 就在啟動前擋下來，不要讓 cloudflared 帶著空 token 起來後才在
# 日誌裡留下看不懂的錯誤。compose 檔無法做這個檢查，原因見該檔的註解。
if [[ ",${COMPOSE_PROFILES:-}," == *",tunnel,"* ]]; then
  if [[ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
    fail "COMPOSE_PROFILES 含 tunnel，但 CLOUDFLARE_TUNNEL_TOKEN 是空的。"
    fail "請在 .env 填入 Cloudflare Zero Trust 的 tunnel token，"
    fail "或將 COMPOSE_PROFILES 那行註解掉以停用 tunnel。"
    exit 1
  fi
  ok "Cloudflare Tunnel 已啟用"
fi

info "啟動容器（等待 healthcheck 通過，首次可能需要 1-2 分鐘）..."
# --wait 會等到所有帶 healthcheck 的服務轉為 healthy 才返回；
# 任一服務失敗則以非零結束，不會靜默通過。
#
# --remove-orphans 在這裡是安全措施，不只是清理：
# 若使用者停用 tunnel profile 後執行本腳本，舊的 cloudflared 容器並不會
# 因為「不在作用中的 profile 裡」而自動停止 —— 它會繼續把服務對外。
# 少了這個旗標，「我已經把 tunnel 關掉了」就會是個錯誤的認知。
if ! $COMPOSE up -d --wait --remove-orphans; then
  fail "容器啟動失敗。以下為最近日誌："
  $COMPOSE logs --tail=40 >&2
  exit 1
fi
ok "容器已就緒"

# ── 模型下載（冪等）────────────────────────────────────
if $COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
  ok "模型 $MODEL 已存在，略過下載"
else
  info "下載模型 $MODEL（首次約需數分鐘，請勿中斷）..."
  if ! $COMPOSE exec -T ollama ollama pull "$MODEL"; then
    fail "模型 $MODEL 下載失敗。"
    fail "堆疊本身已啟動，可稍後重試：bash scripts/pull-model.sh"
    exit 1
  fi
  ok "模型 $MODEL 下載完成"
fi

# ── 結果 ───────────────────────────────────────────────
echo
"$(dirname "${BASH_SOURCE[0]}")/status.sh"

cat <<EOF

Open WebUI 位址：
  • 在 Codespaces 中：開啟「PORTS」面板 → 點擊 3000 埠的網址
  • 本機：http://localhost:3000

首次使用：
  1. 註冊第一個帳號 —— 它會自動成為管理員
  2. 登入後立刻鎖住註冊：bash scripts/lock-signup.sh

⚠  在 ENABLE_SIGNUP=true 的期間，任何能觸及 Open WebUI 的人都能自行
   註冊帳號並使用你的模型。對外之前務必先做上面的步驟 2。

⚠  額度提醒：2-core codespace 每月僅有 60 真實小時。
   用完請務必停止：bash scripts/down.sh
EOF
