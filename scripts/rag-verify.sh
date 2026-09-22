#!/usr/bin/env bash
# 第二階段 RAG 的機械驗證：把 rag_grounding_probe.py 送進 open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），主機端連不到 Ollama，也連不到 Chroma 的檔案。
# 容器之間可透過 ai-net 互通，因此借用已在執行的 open-webui 容器執行 ——
# 那裡面同時有 open_webui 套件、設定好的嵌入函式與向量資料庫。
#
# 這支回答的是 README「Phase 2: RAG」清單裡的四項，而且不用帳號：
#   建立知識庫 → 上傳文件 → 問文件裡的事 → **問文件裡沒有的事**
#
# 用法：
#   bash scripts/rag-verify.sh                     使用 .env 的 OLLAMA_MODEL
#   bash scripts/rag-verify.sh --model qwen3:4b    指定模型
#   bash scripts/rag-verify.sh --timeout 900       單次生成的時限（秒）
#   bash scripts/rag-verify.sh --json              機器可讀輸出
#
# 結束碼：0 全部通過、1 有項目未通過、2 無法判定（連不上或太慢，不是失敗）、
#         3 探針自己壞掉。
#
# 關於 --timeout：思考型模型（qwen3 系列）在 CPU 上會超出預設的 300 秒。
# 2026-09-19 對 qwen3:4b 實測，三次生成全部逾時 —— 那是「太慢」，不是
# 「連不上」，探針會分別講清楚。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REMOTE=/tmp/rag_grounding_probe.py
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --model=*) MODEL="${1#--model=}"; shift ;;
    --timeout) ARGS+=("--timeout" "$2"); shift 2 ;;
    --timeout=*) ARGS+=("--timeout" "${1#--timeout=}"); shift ;;
    --json) ARGS+=("--json"); shift ;;
    -h|--help)
      usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      fail "不認得的參數：$1"
      echo "  用法：bash scripts/rag-verify.sh [--model <模型>] [--timeout <秒>] [--json]"
      exit 1 ;;
  esac
done

require_docker
detect_compose
load_env

# 模型一律走位置參數優先、其次 .env。不要改成只在命令列設環境變數 ——
# load_env 會 source .env，把命令列的環境變數覆蓋掉（D-011 記錄過這個
# 根因造成的兩次靜默失敗）。
MODEL="${MODEL:-${OLLAMA_MODEL:-}}"
if [[ -z "$MODEL" ]]; then
  fail "沒有指定模型，且 .env 裡也沒有 OLLAMA_MODEL。"
  echo "  請用：bash scripts/rag-verify.sh --model qwen3:4b"
  exit 1
fi

# ── 前置檢查 ────────────────────────────────────────────
for svc in ollama open-webui; do
  if ! service_running "$svc"; then
    fail "$svc 未在執行中。請先啟動堆疊：bash scripts/up.sh"
    exit 1
  fi
done

# 生成用的模型必須在。嵌入模型不在這裡檢查 —— 那是探針要報的結論之一，
# 先擋下來會讓「嵌入模型沒下載」這件事變成一句 shell 錯誤而不是一項發現。
installed_raw="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
found=0
while IFS= read -r line; do
  if [[ -n "$line" && "$(normalize_model "$line")" == "$(normalize_model "$MODEL")" ]]; then
    found=1
    break
  fi
done <<<"$installed_raw"

if [[ $found -eq 0 ]]; then
  fail "ollama 裡沒有模型 $MODEL。"
  echo "  請先下載：bash scripts/pull-model.sh $MODEL"
  exit 1
fi
ok "生成模型 $MODEL 已在 ollama 中"

echo
info "複製探針腳本至 open-webui 容器（$REMOTE）..."
if ! $COMPOSE cp "$PROJECT_ROOT/scripts/rag_grounding_probe.py" "open-webui:$REMOTE"; then
  fail "無法複製腳本進容器。請確認 open-webui 仍在執行中。"
  exit 1
fi

info "開始驗證（模型 $MODEL，會實際生成三次，請勿中斷）..."
echo

# 暫時關閉 errexit，才能取得結束碼再自行判斷
set +e
# -u 留著沒有壞處（週邊套件的訊息會即時出來），但它**不是**「跑五分鐘
# 沒有輸出」的原因。真正的原因在探針自己：run() 用 p.add() 收集所有列，
# dump() 由呼叫端在 run() 回傳**之後**才印 —— 整輪本來就什麼都不會印，
# 與 stdio 緩衝無關。**跑五分鐘沒有輸出是預期行為，不是當掉。**
#
# 這裡原本把成因寫成「Python 的 stdout 整塊緩衝」。觀察是對的，成因是錯的
# —— 而錯的成因比沒有成因更糟：下一個人會照著它去調緩衝設定、發現沒用，
# 然後不再相信這支腳本的註解。（見 D-018 第七節，第九次）
$COMPOSE exec -T open-webui python3 -u "$REMOTE" --model "$MODEL" "${ARGS[@]}"
STATUS=$?
set -e

# 清理容器內的暫存檔；失敗不影響結果，但會講一聲。
# 這裡的 rm 只刪暫存腳本。**測試集合的清理在 Python 端**，而且是在
# finally 裡做的 —— 它必須在探針自己崩潰時也發生。
if ! $COMPOSE exec -T open-webui rm -f "$REMOTE" >/dev/null 2>&1; then
  warn "無法刪除容器內的 $REMOTE（不影響結果）"
fi

echo
case $STATUS in
  0) ok "RAG 驗證通過" ;;
  1) fail "RAG 驗證未通過 —— 請看上方標示「未通過」的項目。" ;;
  2) warn "無法判定 —— 有東西連不上。這不是失敗，請先確認服務狀態。" ;;
  3) fail "探針本身執行失敗 —— 這**不是** RAG 的結論。" ;;
  *) fail "探針異常結束（結束碼 $STATUS）" ;;
esac

exit $STATUS
