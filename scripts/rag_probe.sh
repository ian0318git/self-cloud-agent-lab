#!/usr/bin/env bash
# 對候選嵌入模型做繁體／簡體對照檢索實驗。把 rag_probe.py 送進 open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），主機端連不到 Ollama API。容器之間可透過 ai-net
# 互通，因此借用已在執行的 open-webui 容器（內建 Python）來發請求。
#
# 用法：
#   bash scripts/rag_probe.sh bge-m3 nomic-embed-text
#   bash scripts/rag_probe.sh qwen3-embedding:0.6b
#
# 模型一律走位置參數。不要改用環境變數 —— load_env 會 source .env，
# 把命令列的環境變數覆蓋掉（D-011 記錄過這個根因造成的兩次靜默失敗）。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# 參數檢查放在 docker 檢查之前：參數打錯時，該看到的是「你忘了給模型名」，
# 而不是一個跟 docker 有關的錯誤訊息。
if [[ $# -eq 0 ]]; then
  fail "請至少指定一個嵌入模型。"
  echo "  例：bash scripts/rag_probe.sh bge-m3 nomic-embed-text"
  echo
  echo "  可用的候選見 DECISIONS.md D-013。"
  exit 1
fi

require_docker
detect_compose
load_env


MODELS=("$@")
REMOTE=/tmp/rag_probe.py

# ── 前置檢查 ────────────────────────────────────────────
running_services() {
  $COMPOSE ps --status running --services 2>/dev/null
}

if ! running_services | grep -qx ollama; then
  fail "ollama 未在執行中。請先啟動堆疊：bash scripts/up.sh"
  exit 1
fi

if ! running_services | grep -qx open-webui; then
  fail "open-webui 未在執行中。請先啟動堆疊：bash scripts/up.sh"
  exit 1
fi

# 先確認模型都在，缺的就一次講清楚。
# 不要讓實驗跑到一半才發現某個模型沒下載 —— 那會浪費前面幾個模型已經
# 載入與計算的時間，而且輸出會混著成功與失敗，難以判讀。
installed="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
missing=()
for m in "${MODELS[@]}"; do
  if ! grep -qx "$m" <<<"$installed"; then
    missing+=("$m")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  fail "以下模型尚未下載：${missing[*]}"
  echo "  請先下載（嵌入模型不大，通常數十至數百 MB）："
  for m in "${missing[@]}"; do
    echo "    bash scripts/pull-model.sh $m"
  done
  exit 1
fi

echo "── 執行前記憶體 ──────────────────────────────"
free -h | awk 'NR==1 || /^Mem:/'
echo

# ── 送入容器並執行 ──────────────────────────────────────
info "複製探針腳本至 open-webui 容器（$REMOTE）..."
if ! $COMPOSE cp "$PROJECT_ROOT/scripts/rag_probe.py" "open-webui:$REMOTE"; then
  fail "無法複製腳本進容器。請確認 open-webui 仍在執行中。"
  exit 1
fi

info "開始實測（${#MODELS[@]} 個模型，請勿中斷）..."
echo

# 暫時關閉 errexit，才能取得結束碼再自行判斷
set +e
$COMPOSE exec -T \
  -e "OLLAMA_BASE_URL=http://ollama:11434" \
  open-webui python3 "$REMOTE" "${MODELS[@]}"
STATUS=$?
set -e

# 清理容器內的暫存檔；失敗不影響結果
$COMPOSE exec -T open-webui rm -f "$REMOTE" >/dev/null 2>&1 || true

echo
echo "── 執行後記憶體 ──────────────────────────────"
free -h | awk 'NR==1 || /^Mem:/'
echo

if [[ $STATUS -eq 0 ]]; then
  ok "探針執行完畢"
  cat <<'EOF'

後續：
  • 上表只是「哪個模型對繁體有鑑別力」，不是「哪個模型最好」——
    它沒有測檢索文件長度、多語言混雜、或大規模語料的行為
  • 請保留完整輸出，作為更新 DECISIONS.md D-013 的依據
  • 嵌入模型決定後**才能開始上傳文件**：換模型需重新嵌入所有既有文件（D-008）
EOF
else
  fail "探針未完成（結束碼 $STATUS）"
  fail "請保留上方完整輸出。"
fi

exit $STATUS
