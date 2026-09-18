#!/usr/bin/env bash
# 冪等地下載 .env 中指定的模型。
# 當 up.sh 的模型下載步驟失敗時，可用此腳本單獨重試。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

MODEL="${OLLAMA_MODEL:-qwen3:4b}"

if ! $COMPOSE ps --status running --services 2>/dev/null | grep -qx ollama; then
  fail "ollama 容器未在執行中。請先執行：bash scripts/up.sh"
  exit 1
fi

if $COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
  ok "模型 $MODEL 已存在，略過下載"
  exit 0
fi

info "下載模型 $MODEL ..."
$COMPOSE exec -T ollama ollama pull "$MODEL"
ok "模型 $MODEL 下載完成"
