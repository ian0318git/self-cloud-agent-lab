#!/usr/bin/env bash
# 顯示堆疊狀態、已安裝模型，以及記憶體用量。
# 在 8GB 的 codespace 上，記憶體是主要瓶頸，因此一併顯示。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

detect_compose

# 讓 docker compose 看見 tunnel profile —— 否則即使 cloudflared 正在執行，
# 它也不會出現在 ps 的輸出裡，看起來像沒在跑。
# 這裡直接讀 .env 而不呼叫 load_env，避免 status 產生 golden key 等副作用。
COMPOSE_PROFILES="$(grep -E '^COMPOSE_PROFILES=' "$PROJECT_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
export COMPOSE_PROFILES

echo "── 容器狀態 ──────────────────────────────────"
$COMPOSE ps 2>/dev/null || warn "無法取得容器狀態（堆疊可能尚未啟動）"

echo
echo "── 已安裝模型 ────────────────────────────────"
if $COMPOSE ps --status running --services 2>/dev/null | grep -qx ollama; then
  $COMPOSE exec -T ollama ollama list 2>/dev/null || warn "無法列出模型"
else
  warn "ollama 未在執行中"
fi

echo
echo "── 記憶體 ────────────────────────────────────"
free -h | awk 'NR==1 || /^Mem:/'

echo
echo "── 對外連線（Cloudflare Tunnel）──────────────"
if [[ ",${COMPOSE_PROFILES:-}," == *",tunnel,"* ]]; then
  if $COMPOSE ps --status running --services 2>/dev/null | grep -qx cloudflared; then
    ok "cloudflared 執行中 —— Open WebUI 可透過你的網域存取"
    # 連線是否真的建立要看日誌裡有沒有註冊成功的訊息，容器「跑著」不等於「通了」
    if $COMPOSE logs --tail=50 cloudflared 2>/dev/null | grep -qiE 'Registered tunnel connection|Connection .* registered'; then
      echo "   日誌顯示已向 Cloudflare 註冊連線"
    else
      warn "日誌未看到註冊成功的訊息，請確認：docker compose logs --tail=20 cloudflared"
    fi
  else
    warn "tunnel profile 已啟用，但 cloudflared 不在執行中"
  fi
else
  echo "未啟用（僅在本機／Codespaces 埠轉送內可用）"
  echo "   啟用方式見 .env.example 的 CLOUDFLARE_TUNNEL_TOKEN 段落"
fi

echo
echo "── 容器資源用量 ──────────────────────────────"
docker stats --no-stream --format \
  'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null \
  || warn "無法取得容器資源用量"
