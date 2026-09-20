#!/usr/bin/env bash
# 第三階段 item 1 的驗證入口：工具呼叫跨不跨得過 code path 的改變？
#
# 為什麼要另開一個容器跑：探針要用 langchain / langgraph，這兩個套件**不該**
# 裝進 open-webui 的映像裡 —— 那會污染第二階段已經驗過的那條路徑，也會讓
# 「第三階段的量測」與「第二階段的量測」共用一個會變動的環境。所以映像
# 獨立建、用完即丟（--rm）。
#
# 為什麼是 `docker run --network` 而不是 `docker compose exec`：
# 這支探針不是任何一個 compose service，沒有容器可以 exec 進去。
# 但**網路必須跟 ollama 與 mcp-test-server 同一張**，否則容器名稱解析不到，
# 探針會連不上而回報「無法判定」—— 那是環境問題被誤讀成受測對象的問題。
# 因此下面取兩者的網路交集，取不到就直接說明白。
#
# 跟 verify-mcp-server.sh 的差別：那支要 open-webui（驗的是 Open WebUI 要走
# 的那條路）；這支**不需要** —— LangGraph 完全不經過 Open WebUI。這是
# item 1 的整個重點。
#
# 用法：bash scripts/verify-langgraph-tools.sh [--model NAME] [--num-ctx N]
#                                              [--rebuild] [--json]
#
# 模型預設取 .env 的 OLLAMA_MODEL。VM 只有 3.8 GB，跑不動時先換小模型：
#   bash scripts/verify-langgraph-tools.sh --model qwen2.5:3b
#
# 結束碼：0 = 通過（模型自主呼叫工具、有連續呼叫、加總正確）
#         1 = 未通過（判準沒過 —— 這是**受測對象**的結果）
#         2 = 無法判定（容器未執行、模型沒下載、連不上 —— 不是判準的問題）
#         3 = 探針自己壞掉（D-018：這不是受測對象的問題，是這支腳本該修）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# SCRIPT_DIR 必須在 load_env 之前算：load_env 會 cd 到專案根目錄，
# 之後再解析相對路徑就會指到錯的地方（而錯誤會延後到 docker build 才爆）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

require_docker
detect_compose
load_env

IMAGE="self-cloud-agent-lab-langgraph-probe:latest"
REQ_FILE="$SCRIPT_DIR/requirements-langgraph.txt"
PROBE="$SCRIPT_DIR/langgraph_tools_probe.py"

MODEL="${LANGGRAPH_PROBE_MODEL:-${OLLAMA_MODEL:-qwen2.5:3b}}"
NUM_CTX="${LANGGRAPH_PROBE_NUM_CTX:-4096}"
OLLAMA_URL="http://ollama:11434"
MCP_URL="http://mcp-test-server:8000/mcp"
REBUILD=0
JSON_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)    MODEL="$2"; shift 2 ;;
    --num-ctx)  NUM_CTX="$2"; shift 2 ;;
    --rebuild)  REBUILD=1; shift ;;
    --json)     JSON_ARGS+=(--json); shift ;;
    -h|--help)  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知參數：$1（--help 看用法）"; exit 3 ;;
  esac
done

MODEL="$(normalize_model "$MODEL")"

# ── 前置：兩個容器都要在跑 ──────────────────────────────
for NAME in ollama mcp-test-server; do
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    fail "$NAME 容器未在執行中 —— 先執行 bash scripts/up.sh 與 docker compose up -d mcp-test-server"
    exit 2
  fi
done

# ── 前置：模型要真的在 ollama 裡 ────────────────────────
# 沒有這道檢查，探針會在 ollama 回 404 之後才失敗，而那個訊息長得像
# 「探針壞了」。先問一次，訊息就能直接給出該執行的指令。
if ! $COMPOSE exec -T ollama ollama list 2>/dev/null \
     | awk 'NR>1 {print $1}' | grep -qx -- "$MODEL"; then
  fail "ollama 裡沒有 $MODEL —— 先下載："
  fail "  docker compose exec ollama ollama pull $MODEL"
  exit 2
fi

# ── 前置：找出 ollama 與 mcp-test-server 共用的網路 ─────
networks_of() {
  # grep -v '^$' 不能省：Go 模板的 range 在結尾會多吐一個空行，
  # sort -u 之後它排在第一位，head -1 就會挑到空字串 ——
  # 於是「有共同網路」被誤判成「沒有共同網路」。
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$1" 2>/dev/null \
    | grep -v '^$' | sort -u
}
NET="$(comm -12 <(networks_of ollama) <(networks_of mcp-test-server) | head -1)"
if [[ -z "$NET" ]]; then
  fail "ollama 與 mcp-test-server 沒有共用的網路 —— 探針容器到不了它們。"
  fail "  ollama:          $(networks_of ollama | paste -sd, -)"
  fail "  mcp-test-server: $(networks_of mcp-test-server | paste -sd, -)"
  exit 2
fi

info "模型：$MODEL（num_ctx=$NUM_CTX）"
info "網路：$NET"
info "MCP：$MCP_URL"

# ── 記憶體：只警告，不擋 ────────────────────────────────
# D-022 實測：模型載入後只剩 286 MB 可用。這台 VM 只有 3.8 GB，遇到
# 換頁會讓每一輪從數秒變成數十秒 —— 但那是**慢**，不是**錯**。
# 把它變成失敗就是 D-016 說的假失敗。
free_mb="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
if [[ -n "$free_mb" && "$free_mb" -lt 1200 ]]; then
  warn "可用記憶體只有 ${free_mb} MB —— 模型可能無法常駐，每一輪會明顯變慢。"
  warn "那是變慢，不是判準沒過。真的跑不動就換小一點的模型（--model）。"
fi

# ── 映像：需求檔改了就要重建 ────────────────────────────
# 用需求檔的雜湊當標籤，而不是「映像存在就跳過」。少了這個，改了
# requirements-langgraph.txt 之後會**靜默**沿用舊相依 —— 那正是這個專案
# 最想避免的失敗：版本漂移被誤讀成模型退化。
REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
IMAGE_HASH="$(docker image inspect -f '{{index .Config.Labels "lab.req-sha256"}}' "$IMAGE" 2>/dev/null || true)"

if [[ "$REBUILD" == "1" || "$IMAGE_HASH" != "$REQ_HASH" ]]; then
  if [[ "$REBUILD" != "1" && -n "$IMAGE_HASH" ]]; then
    info "requirements-langgraph.txt 已變更 —— 重建探針映像"
  else
    info "建立探針映像（首次會安裝 langchain/langgraph，需要幾分鐘）"
  fi
  docker build -t "$IMAGE" --label "lab.req-sha256=$REQ_HASH" -f - "$SCRIPT_DIR" <<'EOF'
FROM python:3.12-slim
COPY requirements-langgraph.txt /tmp/req.txt
RUN pip install --no-cache-dir --disable-pip-version-check --quiet -r /tmp/req.txt
EOF
  ok "映像就緒（相依版本鎖在 requirements-langgraph.txt）"
else
  info "映像已是最新（需求檔雜湊相符）—— 跳過重建，--rebuild 可強制"
fi
echo

# ── 跑探針 ──────────────────────────────────────────────
# 探針原始碼用唯讀綁定掛進去，不烤進映像：改評分邏輯不必重建映像。
# 相依在映像裡，原始碼在掛載點 —— 兩者各自只在自己該變的時候變。
rc=0
docker run --rm --network "$NET" \
  -v "$PROBE:/probe/langgraph_tools_probe.py:ro" \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" python3 /probe/langgraph_tools_probe.py \
    --model "$MODEL" --ollama-url "$OLLAMA_URL" --mcp-url "$MCP_URL" \
    --num-ctx "$NUM_CTX" "${JSON_ARGS[@]}" || rc=$?

echo
case $rc in
  0) ok "第三階段 item 1 通過：工具呼叫跨得過 code path 的改變（LangGraph 路徑）" ;;
  1) fail "第三階段 item 1 未通過（見上方判準）—— 這是受測對象的結果"; exit 1 ;;
  2) fail "無法判定（見上方輸出）—— 這是環境問題，不是判準的問題"; exit 2 ;;
  3) fail "探針自己壞掉（見上方輸出）—— 不是受測對象的問題，是這支腳本該修"; exit 3 ;;
  *) fail "意外的結束碼 $rc"; exit 3 ;;
esac

# 通過之後仍要看輸出的「觀察」段：那裡的點數是**真實回傳**的，
# 可以拿去跟 mcp-test-server 的日誌對照（D-023 的機械證據做法）。
info "要留下機械證據的話，把探針輸出的點數拿去對 MCP server 日誌："
info "  docker compose logs --since 10m mcp-test-server | grep -i call_tool"
