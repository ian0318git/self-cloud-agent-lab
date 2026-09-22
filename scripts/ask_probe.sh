#!/usr/bin/env bash
# 量測聊天模型的答案正確性與生成速度。把 ask_probe.py 送進 open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），主機端連不到 Ollama API。容器之間可透過 ai-net
# 互通，因此借用已在執行的 open-webui 容器（內建 Python）來發請求。
# 理由與做法與 scripts/rag_probe.sh 相同。
#
# 用法：
#   bash scripts/ask_probe.sh qwen3:4b qwen2.5:3b
#   bash scripts/ask_probe.sh --think qwen3:4b
#   bash scripts/ask_probe.sh --questions mcp qwen3:4b
#   bash scripts/ask_probe.sh --num-predict 2048 qwen3:4b
#
# 模型一律走位置參數。不要改用環境變數 —— load_env 會 source .env，
# 把命令列的環境變數覆蓋掉（D-011 記錄過這個根因造成的兩次靜默失敗）。
#
# 這支腳本會**實際佔用 CPU 數分鐘**（單題實測約 110 秒，見 D-014）。
# 跑之前先確認沒有別的東西在用 ollama，否則量到的速度沒有意義。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# 參數檢查放在 docker 檢查之前：參數打錯時，該看到的是「你忘了給模型名」，
# 而不是一個跟 docker 有關的錯誤訊息。
if [[ $# -eq 0 ]]; then
  fail "請至少指定一個模型。"
  echo "  例：bash scripts/ask_probe.sh qwen3:4b qwen2.5:3b"
  echo
  echo "  每題約需 1–2 分鐘（2 vCPU 無 GPU），請預留時間。"
  exit 1
fi

require_docker
detect_compose
load_env

REMOTE=/tmp/ask_probe.py

# 只做「要不要檢查模型已下載」所需的最小解析，不重寫一份完整的參數解析 ——
# 完整的解析在 Python 端，兩邊各寫一份就會有兩套會漂移的規則（同 rag_probe.sh）。
MODELS=()
args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  arg="${args[$i]}"
  case "$arg" in
    --think)
      ;;
    --questions|--num-predict)
      # 這兩個選項各吃掉後面一個值，那個值不是模型名。
      i=$((i + 1))
      ;;
    --questions=*|--num-predict=*)
      ;;
    --*)
      # 不認得的選項交給 Python 端報錯，這裡不自行判斷。
      ;;
    *)
      MODELS+=("$arg")
      ;;
  esac
  i=$((i + 1))
done

if [[ ${#MODELS[@]} -eq 0 ]]; then
  fail "只看到選項，沒有看到模型名。"
  echo "  例：bash scripts/ask_probe.sh --questions mcp qwen3:4b"
  exit 1
fi

# ── 前置檢查 ────────────────────────────────────────────
for svc in ollama open-webui; do
  if ! service_running "$svc"; then
    fail "$svc 未在執行中。請先啟動堆疊：bash scripts/up.sh"
    exit 1
  fi
done

# 先確認模型都在，缺的就一次講清楚 —— 不要讓實驗跑到一半才發現某顆模型
# 沒下載，那會浪費前面已經載入與生成完的時間（同 rag_probe.sh）。
installed_raw="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
installed=""
while IFS= read -r line; do
  [[ -n "$line" ]] && installed+="$(normalize_model "$line")"$'\n'
done <<<"$installed_raw"

missing=()
for m in "${MODELS[@]}"; do
  # -F：模型名裡的 '.'（qwen2.5:3b）在 regex 下是萬用字元。
  if ! grep -qxF "$(normalize_model "$m")" <<<"$installed"; then
    missing+=("$m")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  fail "以下模型尚未下載：${missing[*]}"
  echo "  請先下載（3B–4B 約 2–2.5 GB）："
  for m in "${missing[@]}"; do
    echo "    bash scripts/pull-model.sh $m"
  done
  exit 1
fi

# 記憶體快照。用 docker stats 而不是只看 free ——「哪個容器吃掉多少」
# 才是 8GB codespace 上真正的問題（同 rag_probe.sh）。
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
  free -h 2>/dev/null | awk '/^Swap:/ {printf "  Swap：已用 %s / 共 %s\n", $3, $2}' || true
  echo
  return 0
}

mem_snapshot "執行前記憶體"

# ── 送入容器並執行 ──────────────────────────────────────
info "複製探針腳本至 open-webui 容器（$REMOTE）..."
if ! $COMPOSE cp "$PROJECT_ROOT/scripts/ask_probe.py" "open-webui:$REMOTE"; then
  fail "無法複製腳本進容器。請確認 open-webui 仍在執行中。"
  exit 1
fi

info "開始實測（${#MODELS[@]} 個模型，請勿中斷）..."
echo

# 暫時關閉 errexit，才能取得結束碼再自行判斷
set +e
# -u：關掉 Python 的輸出緩衝。少了它，管線裡的 stdout 是 block-buffered，
# 一輪十幾分鐘的實驗期間看不到任何進度，只有全部跑完才一次吐出來 ——
# 看起來與「卡住」完全一樣，而「看起來卡住」會讓人去中斷一個正常的工作。
$COMPOSE exec -T \
  -e "OLLAMA_BASE_URL=http://ollama:11434" \
  open-webui python3 -u "$REMOTE" "$@"
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
  • 「通過／不通過」是字串比對的結果，不是語意理解 ——
    程式只確認關鍵字出現，答案是否真的正確仍需人眼覆核（全文已印出）
  • 單題的通過與否不足以排序模型。這是觀測，不是排行
  • 若要更新 DECISIONS.md，請保留完整輸出作為依據
EOF
else
  fail "探針未完成（結束碼 $STATUS）"
  fail "請保留上方完整輸出。"
fi

exit $STATUS
