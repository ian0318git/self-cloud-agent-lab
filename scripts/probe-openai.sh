#!/usr/bin/env bash
# 對任何 OpenAI-compatible runtime 跑相容性探針。把 probe_openai.py 送進
# open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），主機端連不到它。容器之間可透過 ai-net 互通，
# 因此借用已在執行的 open-webui 容器（內建 Python）來發請求。外部端點
# （例如 Kaggle + Endpoint 的網址）同樣走這條路 —— open-webui 容器有對外
# 網路，而且這樣「本機 runtime」與「遠端 runtime」用的是同一條程式路徑，
# 那正是這支探針要證明的事。理由與做法與 scripts/ask_probe.sh 相同。
#
# 用法：
#   bash scripts/probe-openai.sh
#   bash scripts/probe-openai.sh http://ollama:11434/v1
#   bash scripts/probe-openai.sh https://xxx.trycloudflare.com/v1 --model qwen2.5:7b
#
# 金鑰：從 .env 的 ENDPOINT_API_KEY 讀取（沒有就留空）。也可以直接
# 用 --api-key 覆蓋。金鑰不會被印出。
#
# 注意：金鑰是以 `docker compose exec -e` 傳入，因此會出現在同一台機器上
# 其他行程的 ps 輸出裡。單人開發機可接受；共用主機上請改用其他傳遞方式。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

DEFAULT_URL="http://ollama:11434/v1"
REMOTE=/tmp/probe_openai.py

# 第一個位置參數若是網址就當成端點，其餘原樣傳給 Python。
BASE_URL="$DEFAULT_URL"
PASSTHROUGH=()
for arg in "$@"; do
  case "$arg" in
    http://*|https://*)
      if [[ "$BASE_URL" == "$DEFAULT_URL" ]]; then
        BASE_URL="$arg"
        continue
      fi
      ;;
  esac
  PASSTHROUGH+=("$arg")
done

# 內建端點需要 ollama 在跑；外部端點不需要 —— 要求它反而會擋住
# 「只想驗一顆遠端 runtime」這個正常用法。
if [[ "$BASE_URL" == "$DEFAULT_URL" ]]; then
  if ! service_running ollama; then
    fail "ollama 未在執行中。請先啟動堆疊：bash scripts/up.sh"
    echo "  或指定一個外部端點：bash scripts/probe-openai.sh https://…/v1"
    exit 1
  fi
fi

if ! service_running open-webui; then
  fail "open-webui 未在執行中 —— 探針是借用它的 Python 與網路。"
  echo "  請先啟動堆疊：bash scripts/up.sh"
  exit 1
fi

echo "── 端點 ──────────────────────────────────────"
echo "  $BASE_URL"
if [[ -n "${ENDPOINT_API_KEY:-}" ]]; then
  echo "  金鑰：讀自 .env 的 ENDPOINT_API_KEY"
else
  echo "  金鑰：未設定（.env 的 ENDPOINT_API_KEY 為空）"
fi
echo

info "複製探針腳本至 open-webui 容器（$REMOTE）..."
if ! $COMPOSE cp "$PROJECT_ROOT/scripts/probe_openai.py" "open-webui:$REMOTE"; then
  fail "無法複製腳本進容器。請確認 open-webui 仍在執行中。"
  exit 1
fi
echo

set +e
$COMPOSE exec -T \
  -e "OPENAI_API_KEY=${ENDPOINT_API_KEY:-}" \
  open-webui python3 -u "$REMOTE" --base-url "$BASE_URL" "${PASSTHROUGH[@]}"
STATUS=$?
set -e

$COMPOSE exec -T open-webui rm -f "$REMOTE" >/dev/null 2>&1 || true

echo
if [[ $STATUS -eq 0 ]]; then
  ok "相容性探針通過"
  cat <<'EOF'

意義：任何能通過這支探針的 runtime，上層都不需要為它改一行程式碼。
      搬家的動作因此縮減成「換 --base-url」。

尚未涵蓋：探針只驗介面。**速度、可用 VRAM、模型能否載入**要另外量 ——
          介面相容不等於跑得動。
EOF
elif [[ $STATUS -eq 2 ]]; then
  warn "無法判定 —— 連不上，不是「這個 runtime 壞掉」"
  echo "  先確認網址、網路與服務狀態，再重跑。"
else
  fail "有檢查未通過（結束碼 $STATUS）"
  fail "未通過的項目就是上層會踩到的洞。"
fi

exit $STATUS
