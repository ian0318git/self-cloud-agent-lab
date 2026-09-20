#!/usr/bin/env bash
# 第三階段 item 3 的驗證入口：mem0 的 add() 到底多花多少？
#
# 為什麼要另開一個容器跑：探針要用 mem0 與 ollama 的 python 套件，這些**不該**
# 裝進 open-webui 的映像裡 —— 那會污染第二階段已經驗過的那條路徑。映像獨立
# 建、用完即丟（--rm）。理由與 verify-langgraph-tools.sh、verify-chroma-dims.sh
# 相同，映像也共用同一個需求檔（requirements-mem0.txt）。
#
# 跟 verify-chroma-dims.sh 的差別：**這支會跑 LLM**，而且跑很久。
#   · 需要 qwen3:4b 這種生成模型（不只嵌入模型）
#   · 一次執行可能是幾十分鐘，不是幾秒
#   · 記憶體需求高得多（模型本身 2.5GB）
#
# ⚠ **中斷這支腳本不會取消 ollama 端已經開始的生成。** ollama 會把它跑完，
#    並且佔住 OLLAMA_NUM_PARALLEL 的槽位，讓下一次執行看起來像卡住。
#    這是實測到的（見 D-027）：第一次偵察被 Ctrl-C 之後，ollama 又跑了十幾
#    分鐘才停。所以這裡在開跑前先做一次「ollama 有沒有在忙」的探測。
#
# 用法：bash scripts/verify-mem0-add-cost.sh [--model NAME] [--embed-model NAME]
#                                            [--quick] [--rebuild] [--json]
#                                            [--sections C2,C2b,C3,C4,add]
#
# --quick 把真實 add() 那兩次的 max_tokens 壓到 200，讓整輪快很多。
#         **機制判準 C2~C4 不受影響**（它們量的是 prompt 處理，不是生成），
#         但成本觀察的秒數就只是下限，不要拿去跟 D-027 的數字比。
#
# --sections 只跑指定的幾節。**分段重跑用**：某一節的儀器壞掉、修好之後
#            只想重跑那一節 —— C1／C5 那兩次是真的 add()，一小時以上。
#            沒跑滿全部時結束碼是 2，**不會是 0**：只跑一部分永遠不算通過。
#
# 結束碼：0 = 通過（C1~C5 全過）
#         1 = 未通過（判準沒過 —— 通常代表上游變了，重讀 D-027）
#         2 = 無法判定（容器未執行、模型沒下載、連不上 —— 不是判準的問題）
#         3 = 探針自己壞掉（D-018：這不是受測對象的問題，是這支腳本該修）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# 讀 ollama 日誌的旁證工具。獨立成檔是為了能被單獨測試 —— 見它開頭的說明。
source "$(dirname "${BASH_SOURCE[0]}")/ollama_log_corroboration.sh"

# SCRIPT_DIR 必須在 load_env 之前算：load_env 會 cd 到專案根目錄，
# 之後再解析相對路徑就會指到錯的地方（而錯誤會延後到 docker build 才爆）。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

require_docker
detect_compose
load_env

IMAGE="self-cloud-agent-lab-mem0-probe:latest"
REQ_FILE="$SCRIPT_DIR/requirements-mem0.txt"
PROBE="$SCRIPT_DIR/mem0_add_cost_probe.py"

# 生成模型與嵌入模型是**兩個不同的模型** —— 這支探針兩個都要。
MODEL="${MEM0_COST_PROBE_MODEL:-${OLLAMA_MODEL:-qwen3:4b}}"
EMBED_MODEL="${MEM0_COST_PROBE_EMBED_MODEL:-${EMBEDDING_MODEL:-qwen3-embedding:0.6b}}"
OLLAMA_URL="http://ollama:11434"
REBUILD=0
QUICK=0
JSON_ARGS=()
SECTION_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2 ;;
    --embed-model) EMBED_MODEL="$2"; shift 2 ;;
    --rebuild)     REBUILD=1; shift ;;
    --quick)       QUICK=1; shift ;;
    --json)        JSON_ARGS+=(--json); shift ;;
    --sections)    SECTION_ARGS+=(--sections "$2"); shift 2 ;;
    # 註解的結尾用「第一個空行」找，不用寫死的行號 —— 寫死的話每次改上面
    # 那段註解，--help 就會開始印出一半的說明（或印到程式碼）。
    -h|--help)     sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知參數：$1（--help 看用法）"; exit 3 ;;
  esac
done

MODEL="$(normalize_model "$MODEL")"
EMBED_MODEL="$(normalize_model "$EMBED_MODEL")"

# ── 前置：ollama 要在跑 ─────────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -qx "ollama"; then
  fail "ollama 容器未在執行中 —— 先執行 bash scripts/up.sh"
  exit 2
fi

# ── 前置：兩個模型都要真的在 ollama 裡 ──────────────────
# mem0 的 OllamaEmbedding 建構子遇到不存在的嵌入模型會**自己 pull**
# （實測，見 verify-chroma-dims.sh 的同一段）—— 那會讓一次「驗證」變成
# 一次數 GB 的下載。生成模型沒有這道檢查則會在第一次 add() 才失敗，
# 而那時已經花掉幾分鐘了。
for NAME in "$MODEL" "$EMBED_MODEL"; do
  if ! $COMPOSE exec -T ollama ollama list 2>/dev/null \
       | awk 'NR>1 {print $1}' | grep -qx -- "$NAME"; then
    fail "ollama 裡沒有 $NAME —— 先下載："
    fail "  docker compose exec ollama ollama pull $NAME"
    exit 2
  fi
done

# ── 前置：探針容器的網路 ────────────────────────────────
NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' ollama 2>/dev/null \
       | grep -v '^$' | sort -u | head -1)"
if [[ -z "$NET" ]]; then
  fail "取不到 ollama 的網路 —— 探針容器到不了它。"
  exit 2
fi

info "生成模型：$MODEL"
info "嵌入模型：$EMBED_MODEL"
info "網路：$NET"

# ── 前置：ollama 現在是不是在忙 ─────────────────────────
# 這道檢查存在的原因是實測到的：中斷探針**不會**取消 ollama 端的生成。
# 上一次的殘留會佔住 NUM_PARALLEL 的槽位，讓這次的第一個請求排隊，
# 看起來就像探針卡住了。這裡問一個 token 的問題，量它多久才回來。
#
# **只警告，不擋。** 慢有很多種原因（模型要載入、機器忙），擋下來會變成
# D-016 那種假失敗。而且真正的判準在探針裡，不在這裡。
BUSY_MS="$(docker run --rm --network "$NET" "$IMAGE" python3 -c '
import time, sys
try:
    from ollama import Client
    c = Client(host="http://ollama:11434")
    t0 = time.monotonic()
    c.chat(model=sys.argv[1], messages=[{"role": "user", "content": "hi"}],
           options={"num_predict": 1})
    print(int((time.monotonic() - t0) * 1000))
except Exception:
    print(-1)
' "$MODEL" 2>/dev/null || echo -1)"

if [[ "$BUSY_MS" == "-1" ]]; then
  warn "問不到 ollama 的即時狀態 —— 不擋，繼續（探針自己會再確認一次）。"
elif [[ "$BUSY_MS" -gt 60000 ]]; then
  warn "ollama 對一個 1-token 的問題花了 $((BUSY_MS / 1000)) 秒才回 —— 它可能還在跑上一次的殘留。"
  warn "中斷探針**不會**取消 ollama 端的生成（D-027）。要不等它跑完，要不重啟："
  warn "  docker compose restart ollama"
  warn "這只是變慢，不是判準沒過 —— 繼續執行。"
fi

# ── 記憶體：只警告，不擋 ────────────────────────────────
# 這支探針要載入 2.5GB 的生成模型，還會為了對照組把 num_ctx 開到 16384
# （KV cache 也吃記憶體）。門檻沿用 verify-langgraph-tools.sh 的 1200 MB。
free_mb="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)"
if [[ -n "$free_mb" && "$free_mb" -lt 1200 ]]; then
  warn "可用記憶體只有 ${free_mb} MB —— 生成模型可能無法常駐，會明顯變慢或失敗。"
  warn "那是變慢，不是判準沒過。"
fi

# ── 映像：需求檔改了就要重建 ────────────────────────────
# 用需求檔的雜湊當標籤，而不是「映像存在就跳過」。少了這個，改了
# requirements-mem0.txt 之後會**靜默**沿用舊相依 —— 而 D-027 記下的
# 每一條事實都是版本相依的行為（mem0 的 options 裡沒有 num_ctx、
# ADDITIVE_EXTRACTION_PROMPT 的長度…），版本漂移會被誤讀成「上游改行為」。
REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
IMAGE_HASH="$(docker image inspect -f '{{index .Config.Labels "lab.req-sha256"}}' "$IMAGE" 2>/dev/null || true)"

if [[ "$REBUILD" == "1" || "$IMAGE_HASH" != "$REQ_HASH" ]]; then
  if [[ "$REBUILD" != "1" && -n "$IMAGE_HASH" ]]; then
    info "requirements-mem0.txt 已變更 —— 重建探針映像"
  else
    info "建立探針映像（首次會安裝 mem0/chromadb/ollama，需要幾分鐘）"
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

QUICK_ARGS=()
if [[ "$QUICK" == "1" ]]; then
  QUICK_ARGS=(--add-max-tokens 200)
  warn "--quick：真實 add() 的 max_tokens 壓到 200。"
  warn "機制判準 C2~C4 不受影響（它們不經過 mem0 的 Memory），"
  warn "而 --quick 只會讓生成變短 —— 截斷上限只看 num_ctx，不看 num_predict"
  warn "（D-027，讀 llama_server.go 確認、並用固定 num_ctx 變 num_predict 實測），"
  warn "所以進得去的 prompt 一個 token 都不會多。"
  warn "成本觀察的秒數仍然不要跟 D-027 的數字比：那兩次的生成被壓短了。"
fi

# ── 跑探針 ──────────────────────────────────────────────
# 探針原始碼用唯讀綁定掛進去，不烤進映像：改評分邏輯不必重建映像。
# -u 是必要的：python 在非 tty 下會緩衝 stdout，而這支要跑很久，
# 沒有即時輸出就沒辦法判斷它是在跑還是卡住了（實測吃過這個虧）。
info "開始 —— 這一輪可能幾十分鐘，輸出會即時印出來"

# 旁證的兩個基準，都在開跑**之前**取：
#   · RUN_START_UTC —— 日誌行的時間戳是 UTC（容器 TZ 實測為 UTC），所以
#     `date -u` 的格式可以直接跟它做字串比較，不必靠 docker 解析時間。
#   · PRE_ANCHOR —— 開跑前的最後一行日誌。跑完之後它必須還能在讀得到的
#     區段裡找到；找不到就代表視窗沒蓋到這一輪，旁證不可用（見下面那段）。
RUN_START_UTC="$(date -u +%Y-%m-%dT%H:%M:%S)"
PRE_ANCHOR="$(docker logs --tail 1 ollama 2>&1 | tail -1)"
rc=0
docker run --rm --network "$NET" \
  -v "$PROBE:/probe/mem0_add_cost_probe.py:ro" \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" python3 -u /probe/mem0_add_cost_probe.py \
    --model "$MODEL" --embed-model "$EMBED_MODEL" --ollama-url "$OLLAMA_URL" \
    "${QUICK_ARGS[@]}" "${JSON_ARGS[@]}" "${SECTION_ARGS[@]}" || rc=$?

# ── 獨立的旁證：ollama 自己的日誌 ───────────────────────
# 這是**完全不經過探針**的一份證據 —— 探針包住的是 python client，
# 而這裡讀的是伺服器端的日誌。兩邊對得上，截斷這件事才不只是
# 「我們的儀器這樣說」。
#
# 實測（D-027）：ollama 在截斷時會印
#   time=<RFC3339 UTC> level=WARN source=llama_server.go:318 \
#     msg="truncating input prompt" limit=... prompt=... keep=... new=...
# **但那個警告只進日誌，不會出現在 API 回應裡。** 所以對呼叫方（mem0）
# 而言它是靜默的 —— 這句話要說精確，不能簡化成「ollama 會警告你」。
#
# 讀日誌踩到的兩個**無聲的**坑（`--since` 靜默回 0 行、日誌中段損壞讓
# 全量輸出凍結在幾小時前），以及對應的防護，都寫在
# scripts/ollama_log_corroboration.sh —— 它獨立成一個檔案，就是為了能被
# 單獨測試（scripts/test_ollama_log_corroboration.sh）。一支會回報錯誤
# 窗口的旁證工具，比沒有旁證更糟。
echo
info "ollama 日誌裡的截斷旁證（自 $RUN_START_UTC 起，只採計這之後的行）"
ollama_log_corroboration "$PRE_ANCHOR" "$RUN_START_UTC" || true

echo
if [[ "$rc" != "0" && ${#SECTION_ARGS[@]} -gt 0 ]]; then
  warn "這一輪是 --sections 的分段重跑（${SECTION_ARGS[1]}）—— 結束碼 2 是"
  warn "預期的：**只跑一部分永遠不算通過**，那是設計，不是失敗。"
  warn "要判定整個 item 3，要跑滿 C2、C2b、C3、C4、add。"
fi
case $rc in
  0) ok "第三階段 item 3 通過：C1~C5 全過（見上方觀察段）" ;;
  1) fail "第三階段 item 3 未通過（見上方判準）—— 這通常代表上游變了，重讀 D-027"; exit 1 ;;
  2) fail "無法判定（見上方輸出）—— 環境問題或分段重跑，不是判準的問題"; exit 2 ;;
  3) fail "探針自己壞掉（見上方輸出）—— 不是受測對象的問題，是這支腳本該修"; exit 3 ;;
  *) fail "意外的結束碼 $rc"; exit 3 ;;
esac

# 通過之後仍要看輸出的「觀察」段：判準說的是「有沒有截斷、砍哪一端」，
# 觀察段才是第四階段要規劃的那個數字 —— 「每輪多花幾秒、幾個 token」。
info "要留下機械證據的話，把觀察段的截斷幅度與生成速率抄進 D-027。"
