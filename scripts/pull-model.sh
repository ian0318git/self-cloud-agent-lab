#!/usr/bin/env bash
# 冪等地下載模型。預設用 .env 的 OLLAMA_MODEL。
# 當 up.sh 的模型下載步驟失敗時，可用此腳本單獨重試。
#
# 用法：
#   bash scripts/pull-model.sh                下載 .env 指定的模型
#   bash scripts/pull-model.sh qwen3:1.7b     下載指定模型（供模型比較用）
#
# 模型必須用參數指定，不能靠環境變數 —— load_env 會 source .env，
# 把 OLLAMA_MODEL 覆蓋回 .env 的值。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

MODEL_OVERRIDE="${1:-}"
MODEL="${MODEL_OVERRIDE:-${OLLAMA_MODEL:-qwen3:4b}}"

if ! service_running ollama; then
  fail "ollama 容器未在執行中。請先執行：bash scripts/up.sh"
  exit 1
fi

# 正規化後再比對（見 lib.sh normalize_model）：`ollama list` 一律顯示
# 帶 tag 的形式（bge-m3:latest），使用者打的卻是 bge-m3。不比對正規化
# 形式的話，每次都會對一個已經存在的模型重新發出 pull。
installed_raw="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
want="$(normalize_model "$MODEL")"
while IFS= read -r line; do
  if [[ -n "$line" && "$(normalize_model "$line")" == "$want" ]]; then
    ok "模型 $MODEL 已存在，略過下載"
    exit 0
  fi
done <<<"$installed_raw"

info "下載模型 $MODEL ..."
$COMPOSE exec -T ollama ollama pull "$MODEL"
ok "模型 $MODEL 下載完成"
