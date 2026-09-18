#!/usr/bin/env bash
# 顯示堆疊狀態、已安裝模型，以及記憶體用量。
# 在 8GB 的 codespace 上，記憶體是主要瓶頸，因此一併顯示。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

detect_compose

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
echo "── 容器資源用量 ──────────────────────────────"
docker stats --no-stream --format \
  'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null \
  || warn "無法取得容器資源用量"
