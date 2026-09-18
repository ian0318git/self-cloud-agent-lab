#!/usr/bin/env bash
# 第二階段前置驗證。把 verify_api.py 送進 open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），所以主機端連不到 Ollama API。容器之間則可透過
# ai-net 互通，因此借用已在執行的 open-webui 容器（內建 Python）來發請求。
#
# 這樣做同時避開了「在終端機貼上長指令被折行」的問題 —— 腳本以檔案形式進版控。
#
# 用法：
#   bash scripts/verify.sh          跑全部五項（約 10 分鐘）
#   bash scripts/verify.sh 2        只跑第 2 項（關閉 thinking，約 1 分鐘）
#   bash scripts/verify.sh 2,5      只跑第 2 與第 5 項

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

MODEL="${OLLAMA_MODEL:-qwen3:4b}"
REMOTE=/tmp/verify_api.py
VERIFY_ONLY="${1:-}"

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

echo "── 執行前記憶體 ──────────────────────────────"
free -h | awk 'NR==1 || /^Mem:/'
echo

# ── 送入容器並執行 ──────────────────────────────────────
info "複製驗證腳本至 open-webui 容器（$REMOTE）..."
if ! $COMPOSE cp "$PROJECT_ROOT/scripts/verify_api.py" "open-webui:$REMOTE"; then
  fail "無法複製腳本進容器。請確認 open-webui 仍在執行中。"
  exit 1
fi

if [[ -n "$VERIFY_ONLY" ]]; then
  info "開始實測（僅測試 $VERIFY_ONLY，請勿中斷）..."
else
  info "開始實測（整輪約 10 分鐘，請勿中斷）..."
fi
echo

# 暫時關閉 errexit，才能取得測試的結束碼再自行判斷
set +e
$COMPOSE exec -T \
  -e "OLLAMA_MODEL=$MODEL" \
  -e "OLLAMA_BASE_URL=http://ollama:11434" \
  -e "VERIFY_ONLY=$VERIFY_ONLY" \
  open-webui python3 "$REMOTE"
STATUS=$?
set -e

# 清理容器內的暫存檔；失敗不影響結果
$COMPOSE exec -T open-webui rm -f "$REMOTE" >/dev/null 2>&1 || true

echo
echo "── 執行後記憶體 ──────────────────────────────"
free -h | awk 'NR==1 || /^Mem:/'
echo

# 部分執行時不可宣稱「關鍵項目全數通過」—— 沒跑的項目不代表通過。
# 這個假通過是 2026-09-18 加上 VERIFY_ONLY 時自己製造出來的。
if [[ -n "$VERIFY_ONLY" ]]; then
  if [[ $STATUS -eq 0 ]]; then
    ok "部分測試執行完畢（測試 $VERIFY_ONLY）—— 未做整體結論"
  else
    fail "部分測試有項目失敗（結束碼 $STATUS）"
  fi
  exit $STATUS
fi

if [[ $STATUS -eq 0 ]]; then
  ok "驗證完成，關鍵項目全數通過"
  cat <<'EOF'

後續：
  • 若 tool calling 通過，第二階段可直接以 Open WebUI 原生 MCP 進行
  • 請將上方完整輸出保留，作為更新 DECISIONS.md 的依據
EOF
else
  fail "驗證結束，但有項目未通過（結束碼 $STATUS）"
  fail "請保留上方完整輸出，作為調整模型或策略的依據。"
fi

exit $STATUS
