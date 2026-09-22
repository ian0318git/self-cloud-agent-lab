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
#                                            [--quick] [--add-max-tokens N]
#                                            [--rebuild] [--json]
#                                            [--sections C2,C2b,C3,C4,add]
#
# --quick 把真實 add() 那兩次的 max_tokens 壓到 200，讓整輪快很多。
#         **機制判準 C2~C4 不受影響**（它們量的是 prompt 處理，不是生成），
#         但成本觀察的秒數就只是下限，不要拿去跟 D-027 的數字比。
#
# --add-max-tokens N 把真實 add() 那兩次的生成上限**放大**（或縮小）到 N。
#         這是 D-035 第八節那個對照組用的旋鈕：`num_predict` 是「預算」這個
#         候選唯一可以直接動的變數，而它必須與 `num_ctx` 一起看 —— 生成超過
#         `num_ctx − prompt_tokens` 就會觸發 llama-server 的 context shift
#         （D-035 第三節），所以放大它之前先確認餘裕夠。
#         與 --quick 同時給時以這裡為準（並警告）。
#
# --sections 只跑指定的幾節。**分段重跑用**：某一節的儀器壞掉、修好之後
#            只想重跑那一節 —— C1／C5 那兩次是真的 add()，一小時以上。
#            沒跑滿全部時結束碼是 2，**不會是 0**：只跑一部分永遠不算通過。
#
# **生成預算是由 num_ctx 推導的（D-037，不必給旗標）。** mem0 的 OllamaLLM
# 固定送 num_predict=2000，而 D-036 證明那就是「抽取回傳 0 筆」的成因。所以
# 這支腳本會讀 ollama 的 OLLAMA_CONTEXT_LENGTH 並傳給探針（--ctx N），由探針
# 推導 `max_tokens = num_ctx − 抽取 prompt 的上界`：
#   · 這個組合**不會觸發 context shift**（不變式：prompt ≤ 界 ⇒
#     prompt + 預算 ≤ num_ctx），而且 num_ctx 變大時預算自己跟著變大
#   · 兩個前提各有判準盯著（C6：界還成立、C7：預算真的夠）
#   · 覆寫（--add-max-tokens／--quick）永遠贏 —— 那是重現 D-027 條件的
#     受控實驗，那時預算太小是**設計**，C6／C7 不判
#   · num_ctx 小到推不出預算（例如 C2／C3 用的 4096）時**不覆寫**，量的
#     就是 mem0 的實況（2000），而且輸出會明講「沒有推導」
#   · 讀不到 num_ctx 時也一樣，並警告「抽取預期是空白的」（D-036）
#
# 結束碼：0 = 通過（C1~C7 全過）
#         1 = 未通過（判準沒過 —— 通常代表上游變了，重讀 D-027）
#         2 = 無法判定（容器未執行、模型沒下載、連不上，或只跑了一部分）。
#             **但 C6／C7 在分段輪次裡照判** —— 上方判準段若列了它們，那是真的
#             沒過（D-037／D-040），不要因為結束碼是 2 就整段跳過。
#         3 = 探針自己壞掉，**或探針在這一輪執行期間被換掉**（見下面「探針修訂版
#             的守衛」）。兩者都是儀器的問題，不是受測對象的問題（D-018）。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# 讀 ollama 日誌的旁證工具。獨立成檔是為了能被單獨測試 —— 見它開頭的說明。
source "$(dirname "${BASH_SOURCE[0]}")/ollama_log_corroboration.sh"
# 「這一輪是哪一版探針跑的」—— 探針是即時 bind-mount 進去的，映像雜湊蓋不到它。
source "$(dirname "${BASH_SOURCE[0]}")/probe_traceability.sh"
# C2／C3 前提檢查用的門檻與判定（ctx_meets_mem0、MEM0_ADD_MIN_CTX）。
# 放在那個模組裡是為了能離線測試 —— 這一條錯了會製造假失敗。
source "$(dirname "${BASH_SOURCE[0]}")/deploy-vps-decisions.sh"

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
ADD_MAX_TOKENS=""
JSON_ARGS=()
SECTION_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2"; shift 2 ;;
    --embed-model) EMBED_MODEL="$2"; shift 2 ;;
    --rebuild)     REBUILD=1; shift ;;
    --quick)       QUICK=1; shift ;;
    --add-max-tokens) ADD_MAX_TOKENS="$2"; shift 2 ;;
    --json)        JSON_ARGS+=(--json); shift ;;
    --sections)    SECTION_ARGS+=(--sections "$2"); shift 2 ;;
    # 註解的結尾用「第一個空行」找，不用寫死的行號 —— 寫死的話每次改上面
    # 那段註解，--help 就會開始印出一半的說明（或印到程式碼）。
    -h|--help)
      # 判準是**內容**（一直印到第一行非註解、非空行為止），不是行號。轉換前
      # 這裡是 `sed -n '2,/^$/p'` —— 對這一支它**今天是等價的**（檔頭 56 行裡
      # 沒有空行，實測逐位元組相同），但那只不過是檔頭剛好沒有空行；只要有人在
      # 檔頭中間補一個空行，它就會從那裡開始截斷，而且沒有症狀（D-039）。
      usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) fail "未知參數：$1（--help 看用法）"; exit 3 ;;
  esac
done

MODEL="$(normalize_model "$MODEL")"
EMBED_MODEL="$(normalize_model "$EMBED_MODEL")"

# 參數值在這裡就驗掉，**不要留到後面才擋** —— 後面幾百行有前置檢查、映像
# 建置、模型下載，把「打錯字的旗標」留到那裡才講，等於讓一個純粹的輸入
# 錯誤先去付那些成本。
if [[ -n "$ADD_MAX_TOKENS" ]] \
   && { [[ ! "$ADD_MAX_TOKENS" =~ ^[0-9]+$ ]] || (( ADD_MAX_TOKENS < 1 )); }; then
  fail "--add-max-tokens 必須是正整數：$ADD_MAX_TOKENS"
  exit 3
fi

# ── 前置：ollama 要在跑 ─────────────────────────────────
# **不可以寫成 `docker ps … | grep -qx ollama`。** lib.sh:5 開了 pipefail，
# 而 `grep -q` 一配對到就離開：生產者只要還會再寫一次（哪怕只多一行），那一次
# 就吃 EPIPE → 生產者死於 141 → pipefail 把整條管線變非零 → **明明有 ollama
# 也被讀成「沒有」**。判準是「讀者離開之後，生產者還會不會再寫」，與內建／
# 輸出大小都無關：外部行程、只有 17 位元組照樣發作（實測 200/200 誤判）。
# 先把輸出收下來，再用 shell 自己的樣式整行比對 —— 完全不開子行程，所以沒有
# 任何行程會吃到 EPIPE，是**結構上**不可能而不是量不到（D-037 的規矩）。
# 先例、實測與突變測試：scripts/ollama_log_corroboration.sh:54 與
# test_verify_mem0_add_cost_guard.sh 的案例 I／K。
PS_NAMES="$(docker ps --format '{{.Names}}' || true)"
if [[ $'\n'"$PS_NAMES"$'\n' != *$'\n'ollama$'\n'* ]]; then
  fail "ollama 容器未在執行中 —— 先執行 bash scripts/up.sh"
  warn "  這次 docker ps 看到的容器名：[${PS_NAMES//$'\n'/ }]"
  exit 2
fi

# ── ollama 生效的 num_ctx：**無條件先讀**（D-037）────────
#
# 讀的是容器啟動時的環境（Config.Env）—— 那是 ollama **實際讀到**的值。
# 注意這仍然是「設定值」而不是「生效值」，本專案對這兩者的差別吃過虧
# （D-028 記了 OLLAMA_CONTEXT_LENGTH 只被證明過名字存在）。所以探針那邊
# 自己會讀 /api/ps 的生效值，並在兩者不一致時講出來（觀察段，不是判準）
# —— 推導的**輸入**與生效的值都會被記下來，不是二選一。
#
# 這個值要用來**推導生成預算**（`max_tokens = num_ctx − 抽取 prompt 的上界`）。
# mem0 送死 `num_predict=2000`，而 D-036 證明那就是「抽取回傳 0 筆」的成因。
#
# 讀不到、或讀到的不是可用的 num_ctx：**不擋**（無知不是缺陷，D-016），
# 但**要講清楚這一輪沒有預算可推** —— 那時的抽取預期是空白的（D-036）。
# 也不傳 `--ctx`：不覆寫，量到的就是 mem0 的實況。
#
# 這裡先讀、下面 C2／C3 的前提檢查沿用**同一個讀數** —— 同一件事不讀兩次，
# 免得兩處對同一個容器給出不同的答案。
CTX_CONFIGURED="$(docker inspect \
    -f '{{range .Config.Env}}{{println .}}{{end}}' \
    "$($COMPOSE ps -q ollama 2>/dev/null || true)" 2>/dev/null \
  | sed -n 's/^OLLAMA_CONTEXT_LENGTH=//p' | tail -1 || true)"
CTX_ARGS=()
CTX_VERDICT="$(num_ctx_verdict "${CTX_CONFIGURED:-}")"
case "$CTX_VERDICT" in
  ok\ *)
    CTX_ARGS=(--ctx "${CTX_VERDICT#ok }")
    info "ollama 的 OLLAMA_CONTEXT_LENGTH（容器設定）：$CTX_CONFIGURED"
    info "  生成預算：由 num_ctx=${CTX_ARGS[1]} 推導（--ctx，D-037）—— 真實 add() 不再用 mem0 的 2000"
    ;;
  not_integer)
    if [[ -n "$CTX_CONFIGURED" ]]; then
      info "ollama 的 OLLAMA_CONTEXT_LENGTH（容器設定）：$CTX_CONFIGURED"
      warn "它不是整數、不能當 num_ctx 用 —— 這一輪**不推導生成預算**"
    else
      info "ollama 沒有設 OLLAMA_CONTEXT_LENGTH —— 用 ollama 自己的預設"
      warn "讀不到 num_ctx ⇒ 這一輪**不推導生成預算**"
    fi
    warn "會用 mem0 的預設 num_predict=2000 ⇒ ⚠ 抽取**預期是空白的**（D-036："
    warn "2,000 個 token 會被 thinking 吃光）—— 這一輪的量測不代表抽取的極限"
    ;;
  *)
    info "ollama 的 OLLAMA_CONTEXT_LENGTH（容器設定）：$CTX_CONFIGURED"
    warn "這個值不能當 num_ctx 用（$CTX_VERDICT，只在 512~1048576 之間有定義）——"
    warn "這一輪**不推導生成預算**，會用 mem0 的預設 2000 ⇒ ⚠ 抽取預期是空白的（D-036）"
    ;;
esac

# ── 前置：兩個模型都要真的在 ollama 裡 ──────────────────
# mem0 的 OllamaEmbedding 建構子遇到不存在的嵌入模型會**自己 pull**
# （實測，見 verify-chroma-dims.sh 的同一段）—— 那會讓一次「驗證」變成
# 一次數 GB 的下載。生成模型沒有這道檢查則會在第一次 add() 才失敗，
# 而那時已經花掉幾分鐘了。
for NAME in "$MODEL" "$EMBED_MODEL"; do
  # **不可以寫成 `$COMPOSE exec … | awk … | grep -qx -- "$NAME"`**：同一個
  # pipefail×SIGPIPE 形狀（`grep -q` 一配對到就離開），而 `docker compose ps`
  # 這類指令實測就是**兩次寫**。`model_in_ollama` 先收下來再整行比對。
  if ! model_in_ollama "$NAME"; then
    fail "ollama 裡沒有 $NAME —— 先下載："
    fail "  docker compose exec ollama ollama pull $NAME"
    exit 2
  fi
done

# ── 前提：C2／C3 只在「伺服器預設 num_ctx < 8101」時有定義 ──────────
#
# C2 問的是「mem0 送出的 prompt 有沒有被**伺服器預設的 num_ctx** 截斷」，C3
# 接著問「截斷吃哪一端」。兩者都以「預設值小到會截斷」為前提。而
# `scripts/deploy-vps.sh --num-ctx`（現在預設 16384）或任何 ≥ 8101 的值 ——
# 包含這個腳本原本假設的 8192 —— 都會把這個前提拿掉：prompt 完整進得去 →
# C2 回「沒有截斷」→ 這一輪就以 **1「上游變了」**結束。
#
# 那是假的。上游沒變，是**判準的前提被我們自己移除了**。而且受害者會是
# 那個照著文件把 num_ctx 調大的人 —— 他被自己的驗證腳本告知系統壞了，
# 於是去查一個不存在的問題。這就是 D-016 說的假失敗（比漏報更糟）。
# 所以這裡回 2「無法判定」，不是 1。
#
# `CTX_CONFIGURED` 是在上面**無條件**讀的（那份註解也解釋了「設定值 vs 生效
# 值」）。這裡只是把**同一個讀數**拿去擋一個已知會造成假失敗的設定。
#
# 只在真的會跑到 C2 或 C3 時擋。--sections add 這種分段重跑與這條前提無關，
# 不該被它擋下來（讀取本身則不受此限 —— 推導生成預算要用它）。
SECTIONS_STR="${SECTION_ARGS[1]:-C2,C2b,C3,C4,add}"
if [[ ",$SECTIONS_STR," == *",C2,"* || ",$SECTIONS_STR," == *",C3,"* ]]; then
  if [[ "$(ctx_meets_mem0 "${CTX_CONFIGURED:-0}")" == "yes" ]]; then
    fail "OLLAMA_CONTEXT_LENGTH=${CTX_CONFIGURED} 已經 >= $MEM0_ADD_MIN_CTX —— **無法判定**。"
    echo "  C2／C3 問的是「prompt 有沒有被伺服器預設的 num_ctx 截斷」，而這個"
    echo "  設定讓 prompt 完整進得去（mem0 的抽取 prompt 實測 8,052 與 8,100"
    echo "  個 token，門檻是 +1 之後的 $MEM0_ADD_MIN_CTX）。前提不成立，判準"
    echo "  就沒有定義 —— 這**不是**「上游變了」，所以不回 1。"
    echo
    echo "  兩個選擇："
    echo "    · 要驗證「截斷」這件事本身 → 把 num_ctx 調回 4096 再跑："
    echo "        OLLAMA_CONTEXT_LENGTH=4096 docker compose up -d ollama"
    echo "      （或改 .env 後重啟 ollama；這是**暫時**的，驗完再調回去）"
    echo "    · 只是想確認堆疊正常 → 那要跑的是 scripts/deploy-vps.sh 的煙霧"
    echo "      測試，或是這支探針的 --sections add。"
    echo
    echo "  ⚠ **在正確設定的堆疊上，C2／C3 已經是不可滿足的判準。** .env.example"
    echo "    現在出貨 16384（D-035 的第二個門檻），而 C2／C3 的前提是「伺服器"
    echo "    預設值小到會截斷」。所以照著文件佈署的機器跑這支不帶 --sections 的"
    echo "    指令，**每次都會拿到這個 2**。要跑 C2／C3 就得刻意把 num_ctx 調到"
    echo "    8101 以下 —— 也就是說，那兩個判準只在受控的對照實驗裡還有定義。"
    exit 2
  fi
fi

# ── 前置：探針容器的網路 ────────────────────────────────
# `head -1` 是同一個形狀的另一個讀者：`sort -u` 只要還會再吐一次（輸出超過
# 它的緩衝區就會），head 就已經走了。改成整串收下來再自己取第一行。
NET="$(first_line "$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' ollama 2>/dev/null \
       | grep -v '^$' | sort -u)")"
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

# 生成上限只有一個地方決定，優先序是**四層**（D-037）：
#
#   --add-max-tokens N  >  --quick（200）  >  由 num_ctx 推導  >  mem0 的預設 2000
#
# 前三層是「這一輪要用什麼」，第四層是「mem0 出貨值，實測抽不出東西」。
# 給定的永遠贏過推導的 —— 覆寫是**重現 D-027 條件的受控實驗**，那時預算太
# 小是設計，不是缺陷（探針也因此不在覆寫時判 C6／C7）。
#
# **被誰蓋掉就明講** —— 一個被靜默忽略的旗標，正好是這支腳本最不該有的東西。
QUICK_ARGS=()
if [[ -n "$ADD_MAX_TOKENS" ]]; then
  QUICK_ARGS=(--add-max-tokens "$ADD_MAX_TOKENS")
  if [[ "$QUICK" == "1" ]]; then
    warn "--quick 與 --add-max-tokens 同時給了 —— **以 --add-max-tokens $ADD_MAX_TOKENS 為準**，"
    warn "--quick 的 200 被忽略（--quick 其餘行為不變）。"
  fi
  warn "真實 add() 的生成上限設為 num_predict=$ADD_MAX_TOKENS。"
  if [[ "${#CTX_ARGS[@]}" -gt 0 ]]; then
    warn "  （--add-max-tokens 覆寫 ⇒ 這一輪**不用推導值** —— 沒送 --ctx ${CTX_ARGS[1]}，"
    warn "   C6／C7 也不判：這是重現 D-027 條件的受控實驗，預算小是設計。）"
    CTX_ARGS=()
  fi
  # **放大預算之前先看餘裕**，因為那正是 D-035 第三節的機制：生成一旦超過
  # `num_ctx − prompt_tokens`，llama-server 會從 prompt 中段丟掉一整塊再繼續
  # —— prompt 完整這件事就不成立了，而那一輪量到的東西分不出成因。
  # 這裡只提醒，不代擋：探針自己會讀 /api/ps 把生效的 num_ctx 記下來，
  # 判斷留給讀的人（也留給旁證的日誌切片去對）。
  warn "  提醒：生成超過 num_ctx − prompt_tokens 會觸發 context shift（D-035）。"
  warn "  以 prompt 8,100、num_ctx 16,384 為例，餘裕是 8,284 —— 超過就 shift。"
elif [[ "$QUICK" == "1" ]]; then
  QUICK_ARGS=(--add-max-tokens 200)
  warn "--quick：真實 add() 的 max_tokens 壓到 200。"
  if [[ "${#CTX_ARGS[@]}" -gt 0 ]]; then
    warn "  （--quick 覆寫 ⇒ 這一輪**不用推導值** —— 沒送 --ctx ${CTX_ARGS[1]}，"
    warn "   而且 --quick 的意義正是「快」，推導值會讓那兩次 add() 變回一小時以上。）"
    CTX_ARGS=()
  fi
  warn "機制判準 C2~C4 不受影響（它們不經過 mem0 的 Memory），"
  warn "而 --quick 只會讓生成變短 —— 截斷上限只看 num_ctx，不看 num_predict"
  warn "（D-027，讀 llama_server.go 確認、並用固定 num_ctx 變 num_predict 實測），"
  warn "所以進得去的 prompt 一個 token 都不會多。"
  warn "**但「進得去」不等於「待得住」**：壓短不會有壞處，放大才有 —— 見"
  warn "  --add-max-tokens 的說明與 D-035 第三節。"
  warn "成本觀察的秒數仍然不要跟 D-027 的數字比：那兩次的生成被壓短了。"
elif [[ "${#CTX_ARGS[@]}" -gt 0 ]]; then
  # 推導那一層。**要把生效的值在跑之前講出來**（D-035 第七節教訓 3），
  # 所以這裡印的不只是「有推導」，是那個數字本身 —— 探針也會再印一次
  # （`num_predict=…（來源：derived_from_ctx）`），兩邊要一致。
  info "真實 add() 的生成上限：由 num_ctx=${CTX_ARGS[1]} 推導（--ctx，D-037）。"
  info "  不變式：prompt ≤ 抽取 prompt 的上界 8192 ⇒ prompt + 預算 ≤ num_ctx，"
  info "  所以**這個組合不會觸發 context shift**（D-035 第三節的機制）。"
  info "  C6 盯著「界還成立」、C7 盯著「預算真的夠（done_reason=stop）」——"
  info "  界過期時的處方是更新界，預算不夠時的處方是把 num_ctx 開大。"
  info "  預期 5,605 個 token（D-036）；時間要看本機速率，不是常數。"
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

# 守衛的第三個基準，也在開跑**之前**取：探針自己的雜湊。
# 它是唯一能證明「這一輪跑的是工作樹上那一版」的東西 —— 因為下面那行 -v
# 是把工作樹**即時**掛進去，映像標籤（lab.req-sha256）只涵蓋需求檔。
#
# `|| true` 不是裝飾：lib.sh 開頭是 `set -euo pipefail`，少了它，sha256sum
# 一失敗（讀不到檔）就會**當場帶走整支腳本**（rc=1，看起來像「判準沒過」），
# 而 probe_stable_verdict 的第三態（unknown → 只警告）永遠走不到。
# `2>/dev/null` 只擋掉訊息，擋不掉結束碼 —— 這一條是整合測試發現的
# （scripts/test_verify_mem0_add_cost_guard.sh 的案例 E），不是看出來的。
PROBE_SHA_BEFORE="$(sha256sum "$PROBE" 2>/dev/null | awk '{print $1}' || true)"

rc=0
docker run --rm --network "$NET" \
  -v "$PROBE:/probe/mem0_add_cost_probe.py:ro" \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" python3 -u /probe/mem0_add_cost_probe.py \
    --model "$MODEL" --embed-model "$EMBED_MODEL" --ollama-url "$OLLAMA_URL" \
    "${QUICK_ARGS[@]}" "${CTX_ARGS[@]}" "${JSON_ARGS[@]}" "${SECTION_ARGS[@]}" || rc=$?

# ── 探針修訂版的守衛 ────────────────────────────────────
# 跑完再算一次。不一樣就代表上面那幾小時的數字**對不上任何一個修訂版**：
# 探針只在啟動時讀一次自己的雜湊（寫進 evidence 的 meta.probe.sha256_16），
# 所以事後拿工作樹去比對，分不出「跑的是舊碼」與「跑完才被改」。
# 那一輪的數字仍然印出來（它是真的量到的），但結束碼不可以是 0 —— 一份
# 追不回修訂版的「通過」是假的通過。
#
# 判定在 scripts/probe_traceability.sh（可離線測試，18 條斷言 ＋ 2 個突變）。
PROBE_SHA_AFTER="$(sha256sum "$PROBE" 2>/dev/null | awk '{print $1}' || true)"
TRACE_VERDICT="$(probe_stable_verdict "$PROBE_SHA_BEFORE" "$PROBE_SHA_AFTER")"
case "$TRACE_VERDICT" in
  stable)  ;;
  changed) fail "探針在這一輪執行期間被改動（${PROBE_SHA_BEFORE:0:16} → ${PROBE_SHA_AFTER:0:16}）—— 這一輪的數字對不上任何一個修訂版，不可追溯。"
           warn "這一條蓋過上面的 --sections 結束碼 2：分段重跑是**設計**，儀器被換掉是儀器壞了。" ;;
  unknown) warn "算不出探針的雜湊（跑前=${PROBE_SHA_BEFORE:0:16}、跑後=${PROBE_SHA_AFTER:0:16}）—— 無法確認它整輪沒被換掉。"
           warn "只警告不失敗：讀不到不等於它變了（D-016，假失敗比漏掉更糟）。" ;;
esac
rc="$(probe_guard_verdict "$rc" "$TRACE_VERDICT")"

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
# 條件是 `== 2`，不是 `!= 0`。這一段在斷言「結束碼 2 是預期的」，而
# `!= 0` 會讓它在 rc=3 時也印出來 —— 那就變成一句被自己那行反駁的話
# （D-036 第七節的形狀）。3 的可能來源不只一個：探針自己壞掉，或上面那個
# 守衛發現探針被換掉。兩種都不是「只跑一部分」。
if [[ "$rc" == "2" && ${#SECTION_ARGS[@]} -gt 0 ]]; then
  warn "這一輪是 --sections 的分段重跑（${SECTION_ARGS[1]}）—— 結束碼 2 是"
  warn "預期的：**只跑一部分永遠不算通過**，那是設計，不是失敗。"
  warn "要判定整個 item 3，要跑滿 C2、C2b、C3、C4、add。"
fi
case $rc in
  0) ok "第三階段 item 3 通過：C1~C7 全過（見上方觀察段）" ;;
  1) fail "第三階段 item 3 未通過（見上方判準）—— 這通常代表上游變了，重讀 D-027"; exit 1 ;;
  2) fail "無法判定（見上方輸出）—— 環境問題或分段重跑（只跑一部分永遠不算通過）"
     warn "  但**判準段若列了 C6／C7，那是真的沒過** —— 那兩條在分段輪次裡照判（D-040）"
     exit 2 ;;
  3) fail "探針自己壞掉，**或**它在這一輪被改動（見上方輸出；守衛那一段會說是哪一種）"; exit 3 ;;
  *) fail "意外的結束碼 $rc"; exit 3 ;;
esac

# 通過之後仍要看輸出的「觀察」段：判準說的是「有沒有截斷、砍哪一端」，
# 觀察段才是第四階段要規劃的那個數字 —— 「每輪多花幾秒、幾個 token」。
info "要留下機械證據的話，把觀察段的截斷幅度與生成速率抄進 D-027。"
