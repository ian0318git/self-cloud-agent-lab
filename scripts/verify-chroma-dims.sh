#!/usr/bin/env bash
# 第三階段 item 2 的驗證入口：mem0 的 Chroma 後端與本專案的 embeddings 對不對得上？
#
# 為什麼要另開一個容器跑：探針要用 mem0 與 chromadb，這兩個套件**不該**裝進
# open-webui 的映像裡 —— 那會污染第二階段已經驗過的那條路徑。映像獨立建、
# 用完即丟（--rm）。理由與 verify-langgraph-tools.sh 相同。
#
# 跟 verify-langgraph-tools.sh 的差別：**這支不需要 mcp-test-server**。
# item 2 問的是向量存放，與工具呼叫無關。少一個前置條件就少一種
# 「環境問題被誤讀成受測對象的問題」。
#
# 又跟 verify-mcp-server.sh 的差別：那支要 open-webui（驗的是 Open WebUI 走
# 的那條路）；這支只要 ollama —— 向量是探針直接向 ollama 要的。
#
# 用法：bash scripts/verify-chroma-dims.sh [--model NAME] [--alt-model NAME]
#                                          [--rebuild] [--json]
#
# 模型預設取 .env 的 EMBEDDING_MODEL（沒有就用 qwen3-embedding:0.6b，
# 對齊 D-013 量到的 1024 維）。--alt-model 是用來示範「同維度但不可互比」的
# 對照模型，預設 bge-m3:latest。
#
# 結束碼：0 = 通過（C1~C7 全過）
#         1 = 未通過（判準沒過 —— 通常代表上游變了，重讀 D-026）
#         2 = 無法判定（容器未執行、模型沒下載、連不上 —— 不是判準的問題）
#         3 = 探針自己壞掉（D-018：這不是受測對象的問題，是這支腳本該修）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# SCRIPT_DIR 必須在 load_env 之前算：load_env 會 cd 到專案根目錄，
# 之後再解析相對路徑就會指到錯的地方（而錯誤會延後到 docker build 才爆）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

require_docker
detect_compose
load_env

IMAGE="self-cloud-agent-lab-mem0-probe:latest"
REQ_FILE="$SCRIPT_DIR/requirements-mem0.txt"
PROBE="$SCRIPT_DIR/chroma_dims_probe.py"

MODEL="${CHROMA_PROBE_MODEL:-${EMBEDDING_MODEL:-qwen3-embedding:0.6b}}"
ALT_MODEL="${CHROMA_PROBE_ALT_MODEL:-bge-m3:latest}"
OLLAMA_URL="http://ollama:11434"
REBUILD=0
JSON_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)      MODEL="$2"; shift 2 ;;
    --alt-model)  ALT_MODEL="$2"; shift 2 ;;
    --rebuild)    REBUILD=1; shift ;;
    --json)       JSON_ARGS+=(--json); shift ;;
    -h|--help)    sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知參數：$1（--help 看用法）"; exit 3 ;;
  esac
done

MODEL="$(normalize_model "$MODEL")"
ALT_MODEL="$(normalize_model "$ALT_MODEL")"

# ── 前置：ollama 要在跑 ─────────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -qx "ollama"; then
  fail "ollama 容器未在執行中 —— 先執行 bash scripts/up.sh"
  exit 2
fi

# ── 前置：兩個模型都要真的在 ollama 裡 ──────────────────
# 沒有這道檢查，探針會在 ollama 回 404 之後才失敗，而那個訊息長得像
# 「探針壞了」。而且 mem0 的 OllamaEmbedding 建構子遇到不存在的模型會
# **自己 pull**（實測）—— 那會讓一次「驗證」變成一次數 GB 的下載。
for NAME in "$MODEL" "$ALT_MODEL"; do
  if ! $COMPOSE exec -T ollama ollama list 2>/dev/null \
       | awk 'NR>1 {print $1}' | grep -qx -- "$NAME"; then
    fail "ollama 裡沒有 $NAME —— 先下載："
    fail "  docker compose exec ollama ollama pull $NAME"
    exit 2
  fi
done

# ── 前置：探針容器的網路 ────────────────────────────────
# 這支只需要 ollama 一個容器，直接取它的網路即可。
NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' ollama 2>/dev/null \
       | grep -v '^$' | sort -u | head -1)"
if [[ -z "$NET" ]]; then
  fail "取不到 ollama 的網路 —— 探針容器到不了它。"
  exit 2
fi

info "嵌入模型：$MODEL"
info "替代模型：$ALT_MODEL"
info "網路：$NET"

# ── 記憶體：只警告，不擋 ────────────────────────────────
# 這支探針**不跑 LLM**，只做嵌入與向量寫入，所以記憶體需求遠低於
# verify-langgraph-tools.sh。警告門檻相應調低 —— 沿用 1200 MB 會變成
# 常態性的假警報，而假警報會讓人開始忽略警告（D-016 的同族問題）。
free_mb="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
if [[ -n "$free_mb" && "$free_mb" -lt 400 ]]; then
  warn "可用記憶體只有 ${free_mb} MB —— 嵌入模型可能無法常駐，會明顯變慢。"
  warn "那是變慢，不是判準沒過。"
fi

# ── 映像：需求檔改了就要重建 ────────────────────────────
# 用需求檔的雜湊當標籤，而不是「映像存在就跳過」。少了這個，改了
# requirements-mem0.txt 之後會**靜默**沿用舊相依 —— 而 D-026 記下的
# 每一條事實都是版本相依的行為（mem0 宣告 512、chroma 的錯誤訊息長相…），
# 版本漂移會被誤讀成「上游改行為」。
REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
IMAGE_HASH="$(docker image inspect -f '{{index .Config.Labels "lab.req-sha256"}}' "$IMAGE" 2>/dev/null || true)"

if [[ "$REBUILD" == "1" || "$IMAGE_HASH" != "$REQ_HASH" ]]; then
  if [[ "$REBUILD" != "1" && -n "$IMAGE_HASH" ]]; then
    info "requirements-mem0.txt 已變更 —— 重建探針映像"
  else
    info "建立探針映像（首次會安裝 mem0/chromadb，需要幾分鐘）"
  fi
  docker build -t "$IMAGE" --label "lab.req-sha256=$REQ_HASH" -f - "$SCRIPT_DIR" <<'EOF'
FROM python:3.12-slim
COPY requirements-mem0.txt /tmp/req.txt
RUN pip install --no-cache-dir --disable-pip-version-check --quiet -r /tmp/req.txt
EOF
  ok "映像就緒（相依版本鎖在 requirements-mem0.txt）"
else
  info "映像已是最新（需求檔雜湊相符）—— 跳過重建，--rebuild 可強制"
fi
echo

# ── 跑探針 ──────────────────────────────────────────────
# 探針原始碼用唯讀綁定掛進去，不烤進映像：改評分邏輯不必重建映像。
rc=0
docker run --rm --network "$NET" \
  -v "$PROBE:/probe/chroma_dims_probe.py:ro" \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" python3 /probe/chroma_dims_probe.py \
    --model "$MODEL" --alt-model "$ALT_MODEL" --ollama-url "$OLLAMA_URL" \
    "${JSON_ARGS[@]}" || rc=$?

echo
case $rc in
  0) ok "第三階段 item 2 通過：C1~C7 全過（見上方觀察段）" ;;
  1) fail "第三階段 item 2 未通過（見上方判準）—— 這通常代表上游變了，重讀 D-026"; exit 1 ;;
  2) fail "無法判定（見上方輸出）—— 這是環境問題，不是判準的問題"; exit 2 ;;
  3) fail "探針自己壞掉（見上方輸出）—— 不是受測對象的問題，是這支腳本該修"; exit 3 ;;
  *) fail "意外的結束碼 $rc"; exit 3 ;;
esac

# 通過之後仍要看輸出的「觀察」段：C6 的序關係是判準，但那兩次檢索的
# top1 是它的操作面後果 —— 「換模型之後回錯文件，而且沒有任何錯誤訊息」
# 是這個 item 真正要記住的一句話。
info "要留下機械證據的話，把探針輸出的 cosine 兩個數字抄進 D-026 的後續修訂。"
