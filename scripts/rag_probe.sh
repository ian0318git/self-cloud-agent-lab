#!/usr/bin/env bash
# 對候選嵌入模型做繁體／簡體對照檢索實驗。把 rag_probe.py 送進 open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），主機端連不到 Ollama API。容器之間可透過 ai-net
# 互通，因此借用已在執行的 open-webui 容器（內建 Python）來發請求。
#
# 用法：
#   bash scripts/rag_probe.sh qwen3-embedding:0.6b bge-m3
#   bash scripts/rag_probe.sh --engine st Qwen/Qwen3-Embedding-0.6B
#   bash scripts/rag_probe.sh --engine both qwen3-embedding:0.6b
#
# --engine ollama（預設）在 ollama 容器內以量化權重跑，模型須先用 ollama pull 下載。
# --engine st         在 open-webui 行程內用 SentenceTransformers 跑，模型是
#                     HuggingFace 識別碼，首次執行時在容器內下載（數百 MB）。
# --engine both       兩者都跑，用來確認同一顆模型在兩條路上是否一致。
#
# 模型名一律用 Ollama 的標籤。走 ST 時 Python 端會查表換成 HuggingFace
# 識別碼；表上沒有的模型用 NAME=HF_ID 明講：
#   bash scripts/rag_probe.sh --engine both some-model=Org/some-model
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


REMOTE=/tmp/rag_probe.py

# 這裡只做「要不要檢查模型已下載」所需的最小解析，不重寫一份完整的參數解析 ——
# 完整的解析在 Python 端，兩邊各寫一份就會有兩套會漂移的規則。
MODELS=()
ENGINE_ARG="ollama"
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  arg="${args[$i]}"
  case "$arg" in
    --engine)
      i=$((i + 1))
      ENGINE_ARG="${args[$i]:-}"
      ;;
    --engine=*)
      ENGINE_ARG="${arg#--engine=}"
      ;;
    *)
      MODELS+=("$arg")
      ;;
  esac
  i=$((i + 1))
done

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
#
# ST 引擎的模型是 HuggingFace 識別碼，不在 ollama list 裡，跳過這項檢查；
# 它們會在容器內首次使用時自行下載（故也可能因為網路或磁碟空間而失敗）。
if [[ "$ENGINE_ARG" == "st" ]]; then
  info "引擎為 st，模型由容器內的 SentenceTransformers 下載，跳過 ollama 檢查"
  echo "   注意：首次執行需下載模型（數百 MB），且會佔用 open-webui 的 volume 空間"
else
  # 兩邊都先正規化再比對（理由見 lib.sh 的 normalize_model）：
  # `ollama list` 一律顯示 bge-m3:latest，而使用者打的是 bge-m3。
  installed_raw="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  installed=""
  while IFS= read -r line; do
    [[ -n "$line" ]] && installed+="$(normalize_model "$line")"$'\n'
  done <<<"$installed_raw"

  missing=()
  for m in "${MODELS[@]}"; do
    # NAME=HF_ID 語法：ollama 只認 '=' 前面那半。MODELS 保留原樣傳給
    # Python（那裡才解析 '='），這裡只取標籤來比對，否則會把一個明明
    # 已下載的模型誤報成缺漏。
    label="${m%%=*}"
    # -F：模型名裡的 '.'（qwen3-embedding:0.6b）在 regex 下是萬用字元。
    if ! grep -qxF "$(normalize_model "$label")" <<<"$installed"; then
      missing+=("$label")
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
fi

# 記憶體快照。用 docker stats 而不是只看 free：嵌入模型在 ST 引擎下活在
# open-webui 行程裡、在 ollama 引擎下活在 ollama 容器裡 ——「哪個容器吃掉
# 多少」才是 8GB codespace 上真正的問題，主機總量看不出這件事。
#
# 一律接 `|| true`：記憶體只是觀測資訊，取得失敗不該讓整場實驗中止
# （set -e 下函式回傳非零會直接結束呼叫端）。
mem_snapshot() {
  echo "── $1 ──────────────────────────────"
  local ids
  ids="$($COMPOSE ps -q ollama open-webui 2>/dev/null | tr '\n' ' ')"
  if [[ -n "${ids// /}" ]]; then
    docker stats --no-stream --format \
      '  {{.Name}}  記憶體 {{.MemUsage}}（{{.MemPerc}}）  CPU {{.CPUPerc}}' \
      $ids 2>/dev/null || true
  else
    echo "  （取不到容器，略過）"
  fi
  free -h 2>/dev/null | awk '/^Mem:/ {printf "  主機合計：已用 %s / 共 %s\n", $3, $2}' || true
  echo
  return 0
}

mem_snapshot "執行前記憶體"

# ── 送入容器並執行 ──────────────────────────────────────
info "複製探針腳本至 open-webui 容器（$REMOTE）..."
if ! $COMPOSE cp "$PROJECT_ROOT/scripts/rag_probe.py" "open-webui:$REMOTE"; then
  fail "無法複製腳本進容器。請確認 open-webui 仍在執行中。"
  exit 1
fi

info "開始實測（引擎 $ENGINE_ARG，${#MODELS[@]} 個模型，請勿中斷）..."
echo

# 暫時關閉 errexit，才能取得結束碼再自行判斷
set +e
$COMPOSE exec -T \
  -e "OLLAMA_BASE_URL=http://ollama:11434" \
  open-webui python3 "$REMOTE" --engine "$ENGINE_ARG" "${MODELS[@]}"
STATUS=$?
set -e

# 清理容器內的暫存檔；失敗不影響結果
$COMPOSE exec -T open-webui rm -f "$REMOTE" >/dev/null 2>&1 || true

echo
mem_snapshot "執行後記憶體"

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
