#!/usr/bin/env bash
# 一行把**全新的 VPS**變成一個安全設定好、而且被證明過的堆疊。
#
# 用法：
#   bash scripts/deploy-vps.sh [--model NAME] [--embed-model NAME]
#                              [--num-ctx N] [--model-gb N]
#                              [--expose] [--keep-data] [--dry-run]
#
#   --model NAME        對話模型（預設沿用 .env 的 OLLAMA_MODEL）
#   --embed-model NAME  嵌入模型（預設沿用 .env 的 EMBEDDING_MODEL）
#   --num-ctx N         伺服器端的 context 長度，**預設 16384**（見下方說明）
#   --model-gb N        這個模型要多少 GB 磁碟。**只有查表查不到的模型才需要它**
#                       —— 表外的模型不給這個值時，磁碟閘門會明印「不知道」而
#                       不是猜一個數字（見下方說明）
#   --expose            刻意綁 0.0.0.0。**這是對一個真實取捨的確認，不是方便旗標**
#   --keep-data         機器上已經有人類的資料時，仍然繼續（見下方說明）
#   --dry-run           只做前置檢查與 .env 的差異顯示，**不寫任何檔案、不起容器**
#
# GPU **不是旗標**：唯一的控制點是 `.env` 的 `OLLAMA_GPU`（auto／on／off），
# 由本腳本依偵測結果決定要不要掛上 docker-compose.gpu.yml。加一個 --gpu 旗標
# 會造出第二個真相來源 —— 那正是 D-054 的病灶。
#
# 結束碼：0 = 佈署完成且通過煙霧測試（有開 GPU 時，也通過「模型真的在 GPU 上」）
#         1 = 未通過（發現暴露、資料已存在、煙霧測試失敗、說要用 GPU 卻跑在
#             CPU 上、**磁碟不足** —— 都附完整輸出）
#         2 = 無法判定（容器起不來、模型下載失敗、num_ctx 沒生效、GPU 用量量不到）
#         3 = 這支腳本自己壞掉（參數錯誤）

# ── 為什麼不是直接用 up.sh ──────────────────────────────
#
# `up.sh` 是一鍵**啟動**，它為 Codespaces 測試台而寫，而它的每一個預設值
# 在一台有公開 IP 的機器上都是錯的：
#
#   1. **Open WebUI 會發布到整個 Internet。** docker-compose.yml 的
#      `"${WEBUI_BIND_ADDR:-0.0.0.0}:3000:8080"` 是檔案裡唯一發布的埠，而它
#      預設綁所有介面。在 VPS 上這會**繞過 Cloudflare Access** —— Access
#      保護的是經過 tunnel 的那條路，不是這個埠。
#   2. **修正這件事有個陷阱：docker compose 先解析 shell 環境，再讀 .env。**
#      而 lib.sh 的 load_env() 會 `set -a; source .env`，把值**匯出**。
#      所以「先用 load_env、再改 .env」是無效的 —— compose 會繼續用那個
#      已經匯出的舊值，而且是無聲的。**寫 .env 必須在 load_env 之前。**
#   3. **OLLAMA_CONTEXT_LENGTH 原本根本設不了**（沒有任何一行 compose 讀它），
#      於是 mem0 的抽取 prompt（實測 8,052 與 8,100 個 token）會被 ollama 的
#      4096 預設值截斷到 2,050 —— 砍掉的還是**開頭與中段，也就是模型自己的
#      指令**（D-027）。
#   4. **EMBEDDING_MODEL 原本沒有人定義**，兩支驗證腳本各自 fallback 到一個
#      沒有人負責下載的寫死模型。
#
# ── 安全性是**建構出來的**，不是檢查出來的 ──────────────
#
# 步驟 3～5 的順序就是安全性論證本身：**埠在容器存在之前就已經關上了**。
# 所以沒有任何一個瞬間是「開著」的 —— 包括腳本中途被打斷，因為中斷只會
# 留下「沒有容器」或「容器已經是安全設定」兩種狀態。
#
# 因此後面的暴露閘門是**對一個已經安全的狀態做確認**，不是主要防線。
# 它防的是「.env 寫進去了但沒有生效」這件事靜默發生。
#
# ── 只支援全新安裝 ──────────────────────────────────────
#
# 這支腳本不搬資料。它會先問「這台機器上有沒有人類的資料」（Open WebUI 的
# 帳號與對話）—— 有就停下來，除非明講 --keep-data。
# 刻意**不用**「volume 存不存在」或「模型下載了沒」當判準：那樣子一次在
# 煙霧測試失敗的佈署，會讓腳本自己不能再跑第二次，而「跑到一半失敗」正是
# 佈署最常見的狀態。模型與 volume 都是可以重建的產物；帳號與對話不是。
#
# ── 磁碟閘門為什麼分成兩段（D-058）──────────────────────
#
# 上面那條原則（不看「下載了沒」）原本只套用在資料閘門上，而磁碟閘門**正在
# 犯同一個錯**：舊公式是 `2 × 模型 + 4`，與「這次要不要下載」無關。於是模型
# 已經在本機的重跑仍然被要求兩倍空間而被擋下來 —— 卡在它自己說最該支援的
# 那個處境上。
#
# 修法是把它拆成兩段，而**分段不是偏好，是順序逼出來的**：
#
#   第 2 步（容器還不存在）—— 只判**無條件**需要的量：映像 ＋ 系統餘裕。
#     `docker images` 是本地查詢，不需要容器也不需要網路，所以問得到。
#     模型那一份**不在這個數字裡**，訊息會明講。
#
#   第 8 步（下載之前）—— 模型在不在，只有 ollama 容器起來之後才問得到。
#     所以硬擋放在真的會下載的那條分支上：`pull_if_missing` 已經有一份
#     「在不在」的判斷，閘門就長在同一條分支上，兩者不可能漂移。
#
# 兩段的 `unknown` 都**不擋**，但一定會印出來 —— 「我不知道」不是「沒問題」，
# 也不是「有問題」。
#
# `--model-gb` 是為了表外的模型。`model_gb_estimate()` 只認得 `.env.example`
# 列出的那幾個；`llama3:70b` 這種查不到的，不給旗標時閘門會明印「不知道」，
# **不會猜一個數字**（D-055 §3(2)：修這個缺陷的時候不可以把它改成靜默通過）。
# 猜錯的方向有兩種，而兩種都貴：猜太小 = 放行到 pull 中途失敗，猜太大 = 對著
# 一台空間夠的機器誤擋。
#
# ── --num-ctx 為什麼預設 16384 ──────────────────────────
#
# **「大到 prompt 進得去」是兩個門檻，而且明顯的那一個不夠。**
#
# 第一個門檻是**生成之前**的截斷：`num_ctx >= prompt tokens + 1`（D-027）。
# mem0 的 ADDITIVE_EXTRACTION_PROMPT 實測 8,052 與 8,100 個 token，所以
# 8,101 就能讓 prompt **進得去**。
#
# 第二個門檻是**生成期間**的 context shift：ollama 是用
# `--context-shift --keep 4` 起 llama-server 的，生成只要超過
# `num_ctx - prompt_tokens`，llama-server 不會停 —— 它會**從 prompt 中段
# 丟掉一整塊再繼續生成**（日誌：`slot context shift, n_keep = 4,
# n_left = 8187, n_discard = 4093`）。8192 之下那個餘裕只有 **140** 個
# token，兩次真實 add() 都撞到了（D-035）。
#
# 真正該滿足的是 `num_ctx > prompt_tokens + num_predict` —— 8,052 + 2,000
# = 10,052，所以 **16384** 才是下一個好記的 2 的次方（留 6,332 個 token，
# 足以讓 num_predict 開到約 8,000 都還不觸發 shift）。
#
# **那個「餘裕足以開到 8,000」現在不是估的（D-036）：** 把 num_predict 由
# mem0 預設的 2,000 開到 8,000、num_ctx 維持 16384，context shift **0 次**，
# 而抽取由 **0 則變成 2 則**（原本三輪都空手）。生成預算就是成因。
#
# ⚠ **這個腳本仍然只佈署「context」那一半。「預算」那一半現在有處方了，但
# 它住在探針裡，不在佈署面上（D-037）。**
#
# mem0 的 `max_tokens` 是 library 層的設定，本堆疊沒有任何地方寫它（`.env`／
# compose／ollama 都沒有），所以佈署完之後抽取仍然用 mem0 的 2,000 預設值，
# 抽取仍然是空的。D-036 第十節把結論寫成「開大」，而 D-037 把它寫成**推導**，
# 不是另一個常數：
#
#     max_tokens = num_ctx − 抽取 prompt 的上界（8,192）
#
# 不變式是「prompt ≤ 上界 ⇒ prompt + 預算 ≤ num_ctx」，所以這個組合**結構上
# 不會觸發 context shift**，而 `num_ctx` 調大時預算自己跟著變大 —— 這才是它
# 與魔數的差別。實作在 scripts/mem0_add_cost_probe.py（由 `--ctx` 驅動），
# 兩個前提各有哨兵判準盯著：**C6**（界還成立）、**C7**（預算真的夠，
# `done_reason=stop`）。上面 `--num-ctx` 這個旋鈕因此**同時是預算的輸入**。
#
# **這一輪沒有生產環境的 mem0 可以改** —— 這個 repo 裡沒有任何地方在生產環境
# 用 mem0（`grep -rn 'import mem0'` 只命中探針；mcp-server 沒有記憶工具；
# open-webui 的 memory 層整個沒開）。所以交付的是第四階段要沿用的**推導 ＋
# 會叫的判準**，不是「佈署面上已生效」。要讓佈署面生效，得把同一條推導接進
# 真正會呼叫 mem0 的那個服務 —— 而在那之前，**這一支 script 不該假裝它做了**。
#
# **代價要說清楚，而且這次有一半是實測的：**
#   * **時間**：同一份 prompt、同樣生成 2,000 個 token，從 8192 開到 16384
#     讓牆上時間 **+45.5%**、生成速率掉 **36%**（1.79 → 1.15 tok/s，D-035）。
#     這是這個預設值真正的代價，而且是量出來的。
#   * **記憶體**：KV cache 隨這個值線性成長，而這個代價在本堆疊**沒有實測過**。
#     D-022 的 ~36 KiB/token 是從 Qwen2.5-3B 的架構推得的**估算值**，而且與
#     模型的 KV head 數綁定（KV head 多的模型會是倍數）。16,384 對 3B 級模型
#     大約是 GB 的量級，對 14B 級模型則不是。下面的資源檢查會用這個估算值
#     提醒你，但**只有實測到的模型大小能擋下佈署**，估算值不會。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# 所有判斷都在這裡（純函式、可離線測試）：綁定預設、暴露閘門的四態、
# 資源門檻、context 的 +1、以及 .env 的 upsert。
source "$(dirname "${BASH_SOURCE[0]}")/deploy-vps-decisions.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SMOKE_PROBE="$SCRIPT_DIR/deploy_smoke_probe.py"

# ── 參數 ────────────────────────────────────────────────
MODEL_OVERRIDE=""
EMBED_OVERRIDE=""
NUM_CTX_RAW=""
MODEL_GB_RAW=""
EXPOSE=0
KEEP_DATA=0
DRY_RUN=0

# 需要值的旗標：缺值時回 3（參數錯誤是「腳本自己壞掉」，不是「環境」）
need_value() {
  if [[ -z "${2:-}" ]]; then
    fail "$1 需要一個值（--help 看用法）"
    exit 3
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       need_value "$1" "${2:-}"; MODEL_OVERRIDE="$2"; shift 2 ;;
    --embed-model) need_value "$1" "${2:-}"; EMBED_OVERRIDE="$2"; shift 2 ;;
    --num-ctx)     need_value "$1" "${2:-}"; NUM_CTX_RAW="$2"; shift 2 ;;
    --model-gb)    need_value "$1" "${2:-}"; MODEL_GB_RAW="$2"; shift 2 ;;
    --expose)      EXPOSE=1; shift ;;
    --keep-data)   KEEP_DATA=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知參數：$1（--help 看用法）"; exit 3 ;;
  esac
done

# --num-ctx 的驗證。非 `ok` 一律回 3 —— 與 verify-mem0-add-cost.sh 一致。
NUM_CTX_DEFAULT=16384
NUM_CTX_CHECK="$(num_ctx_verdict "${NUM_CTX_RAW:-$NUM_CTX_DEFAULT}")"
case "$NUM_CTX_CHECK" in
  ok\ *) NUM_CTX="${NUM_CTX_CHECK#ok }" ;;
  not_integer)
    fail "--num-ctx 必須是整數：${NUM_CTX_RAW}"
    exit 3 ;;
  too_small)
    fail "--num-ctx 太小（${NUM_CTX_RAW}）—— 下限 512。"
    exit 3 ;;
  too_large)
    fail "--num-ctx 太大（${NUM_CTX_RAW}）—— 上限 1048576。"
    exit 3 ;;
  *)
    fail "--num-ctx 驗證回了一個意料外的結果：$NUM_CTX_CHECK"
    exit 3 ;;
esac

# --model-gb 的驗證。**沒給旗標時整段不跑** —— 空字串是合法的「沒明講」，
# 不是一個壞掉的值。這與 num_ctx 不同：num_ctx 有預設值，所以它一定要驗；
# 而模型大小沒有預設值，硬給一個就是 D-055 §3(2) 禁止的那件事。
MODEL_GB_OVERRIDE=""
if [[ -n "$MODEL_GB_RAW" ]]; then
  MODEL_GB_CHECK="$(model_gb_verdict "$MODEL_GB_RAW")"
  case "$MODEL_GB_CHECK" in
    ok\ *) MODEL_GB_OVERRIDE="${MODEL_GB_CHECK#ok }" ;;
    not_number)
      fail "--model-gb 必須是數字（GB）：${MODEL_GB_RAW}"
      echo "  整數或小數都可以，例如 --model-gb 40 或 --model-gb 7.5。"
      exit 3 ;;
    not_positive)
      fail "--model-gb 必須大於 0（收到 ${MODEL_GB_RAW}）。"
      echo "  填 0 會讓磁碟閘門算出「需要 0 GB」—— 那比**不填**這個旗標更寬鬆，"
      echo "  等於是把閘門關掉卻以為自己設定了它。"
      echo "  不知道模型多大時就不要給：閘門會明印「不知道」，那才是誠實的。"
      exit 3 ;;
    too_large)
      fail "--model-gb 太大（${MODEL_GB_RAW} GB）—— 上限 ${MODEL_GB_MAX} GB。"
      echo "  最常見的原因是**把 MB 當成 GB**：qwen3:8b 是 5,200 MB，所以是 5.2。"
      exit 3 ;;
    *)
      fail "--model-gb 驗證回了一個意料外的結果：$MODEL_GB_CHECK"
      exit 3 ;;
  esac
fi

require_docker
detect_compose

echo "── VPS 一鍵佈署 ──────────────────────────────"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "  模式：--dry-run（不寫任何檔案、不起任何容器）"
fi
echo

# ── 1. 確保 .env 存在（**只為了讀，還沒寫**）────────────
# 這裡刻意不呼叫 load_env：那會把值匯出成 shell 環境變數，而 compose 的
# 插值順序是 shell > .env —— 也就是說，**先匯出就再也改不動了**（見檔頭 2）。
cd "$PROJECT_ROOT"

# --dry-run **絕不**建立或修改 .env：沒有 .env 時用一個暫時的副本算差異，
# 離開時刪掉。少了這個，一次「什麼都不做」的預演會在使用者的專案裡留下
# 一個他自己沒建過的 .env。
ENV_FILE="$PROJECT_ROOT/.env"
ENV_SYNTHETIC_TMP=""
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    ENV_SYNTHETIC_TMP="$(mktemp)"
    cp .env.example "$ENV_SYNTHETIC_TMP"
    ENV_FILE="$ENV_SYNTHETIC_TMP"
    trap '[[ -n "${ENV_SYNTHETIC_TMP:-}" ]] && rm -f "$ENV_SYNTHETIC_TMP"' EXIT
    info "（dry-run）沒有 .env —— 下面的差異是相對於 .env.example 算的"
  else
    info "找不到 .env，從 .env.example 建立"
    cp .env.example .env
  fi
fi

# 只讀 .env 本身，**不退回 .env.example**。安全性相關的鍵要用這個。
read_dotenv_value() {
  local key="$1" v
  [[ -f "$ENV_FILE" ]] || { printf ''; return 0; }
  v="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -1)"
  printf '%s' "$v"
}

# 讀 .env，沒有就退回 .env.example。只給「範本的值就是合理預設」的鍵用
# （模型名稱就是這種：範本寫 qwen3:4b，沿用它是對的）。
read_env_value() {
  local key="$1" f v
  for f in "$ENV_FILE" .env.example; do
    [[ -f "$f" ]] || continue
    v="$(sed -n "s/^${key}=//p" "$f" | tail -1)"
    if [[ -n "$v" ]]; then printf '%s' "$v"; return 0; fi
  done
  printf ''
}

MODEL="${MODEL_OVERRIDE:-$(read_env_value OLLAMA_MODEL)}"
MODEL="${MODEL:-qwen3:4b}"
EMBED_MODEL="${EMBED_OVERRIDE:-$(read_env_value EMBEDDING_MODEL)}"
EMBED_MODEL="${EMBED_MODEL:-qwen3-embedding:0.6b}"

# 綁定位址：**這是整個佈署最重要的一個值。**
#
# 判定本身在 resolve_bind_addr()（deploy-vps-decisions.sh）—— 它是純函式、
# 有離線測試、也有突變測試。放那裡的理由是實際教訓：這段邏輯原本寫在這裡，
# 而它的第一個版本會把 `.env.example` 的 `0.0.0.0` 當成「使用者的決定」照
# 單全收 —— 那正是這支腳本存在的理由，卻因為住在進入點裡而沒有任何測試
# 擋得住它。**這裡只負責讀檔與印訊息。**
#
# 讀的是 read_dotenv_value()：只讀 .env，**不退回 .env.example**。範本裡的
# 值是給 Codespaces 的預設，不是使用者的決定。
BIND_RESOLVED="$(resolve_bind_addr "$(read_dotenv_value WEBUI_BIND_ADDR)" \
                                  "$EXPOSE" "${CODESPACES:-false}")"
BIND_ADDR="$(sed -n '1p' <<<"$BIND_RESOLVED")"
BIND_OVERRIDDEN="$(sed -n '2p' <<<"$BIND_RESOLVED")"

info "對話模型：$MODEL"
info "嵌入模型：$EMBED_MODEL"
info "context 長度：$NUM_CTX"
info "Open WebUI 綁定：$BIND_ADDR"
echo

if [[ -n "$BIND_OVERRIDDEN" ]]; then
  warn "找到 WEBUI_BIND_ADDR=$BIND_OVERRIDDEN —— 那是**萬用位址**，會把 Open WebUI"
  warn "發布到這台機器的每一個介面上。已改成 $BIND_ADDR（下面會寫進 .env）。"
  warn "這一台是 VPS 的話，3000 埠在公網上就是開的 —— 而 Cloudflare Access 只"
  warn "擋 tunnel 那條路，擋不到這個埠（README 的「Moving to a VPS」有寫）。"
  warn "真的要靠邊緣防火牆擋、要在公網上開這個埠，請改用 --expose 明講。"
  echo
fi

# ── 2. 資源前置檢查：**只有磁碟會硬擋** ─────────────────
# 磁碟不足不會優雅地變慢，它會在 pull 到一半的時候中止。CPU 與記憶體不足
# 是「慢」，磁碟不足是「壞在半路」—— 這個差別才是阻擋的理由，不是嚴重程度。
#
# **這一段只看「無條件」需要的量**（D-058）：映像 ＋ 系統餘裕。模型那一份要
# 等 ollama 容器起來才問得到「在不在」（第 5 步才建容器），所以它歸第 8 步。
# 舊版在這裡就把模型算進去，於是模型已經在本機的**重跑照樣被擋** —— 而那
# 正是這支腳本在 :56-58 說最該支援的處境。分段是順序逼出來的，不是偏好。
MODEL_GB="$(model_gb_resolve "$MODEL_GB_OVERRIDE" "$MODEL")"
EMBED_GB="$(model_gb_resolve "" "$EMBED_MODEL")"

# 磁碟量的是 **DockerRootDir**，不是專案目錄 —— 模型 blob 與映像層都落在
# 那裡。舊版量 $PROJECT_ROOT，在兩者分屬不同裝置的機器上是**錯的裝置**
# （本機同一個檔案系統，所以現在看不出來）。讀不到就退回專案目錄並講明。
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [[ -n "$DOCKER_ROOT" && -d "$DOCKER_ROOT" ]]; then
  DISK_PATH="$DOCKER_ROOT"
else
  DISK_PATH="$PROJECT_ROOT"
  warn "讀不到 DockerRootDir —— 磁碟量的是專案目錄（$PROJECT_ROOT），"
  warn "  而那不一定是映像與模型實際落地的那個檔案系統。"
fi

# 每一個替換都要 `|| true`：set -euo pipefail 之下，df 一失敗會在**賦值那一行**
# 被 errexit 殺掉，而那一行是「檢查」，不是「動作」。
DISK_FREE_GB="$(df -Pk "$DISK_PATH" 2>/dev/null \
  | awk 'NR==2 {printf "%d", $4/1024/1024}' || true)"

# 這次要拉的映像：取自 compose 自己的模型，不是寫死一份 —— 寫死會在 compose
# 改了之後**無聲地檢查錯的東西**（與下面 ARCH_IMAGES 同一個理由）。
IMG_WANTED="$(cd "$PROJECT_ROOT" && $COMPOSE config 2>/dev/null \
  | grep -oE '^[[:space:]]+image:.*' \
  | sed 's/^[[:space:]]*image:[[:space:]]*//' | sort -u || true)"
IMG_LOCAL="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true)"

# **這裡與第 5 步的 `up -d` 有一個必須同步的耦合**：`$COMPOSE config` 不含
# 未啟用 profile 的服務（實測：tunnel profile 沒開時 `cloudflare/cloudflared:latest`
# 不在清單裡）。那**現在是對的** —— 第 5 步的 `up -d` 也沒有 `--profile tunnel`，
# 所以那次真的不會拉 cloudflared。但**如果有一天 `up` 那行加上了 profile，
# 這一行必須跟著加** —— 否則閘門會少算一個真的會被拉的映像，而且是往寬鬆
# 那邊錯。（那正是任務 #84 要動的地方。）

# 本機已經有的映像不會被重拉（compose 的 pull_policy 預設是 missing），所以
# 它們**不算進需求** —— 這與模型那一份是同一個修法。
IMG_NEED_GB="$(image_need_gb "$IMG_LOCAL" "$IMG_WANTED")"
DISK_NEED_GB="$(disk_unconditional_need_gb "$IMG_NEED_GB")"

BLOCKERS=0
case "$(disk_verdict "${DISK_FREE_GB:-}" "${DISK_NEED_GB:-}")" in
  sufficient)
    ok "磁碟：${DISK_FREE_GB} GB 可用（映像與系統需要約 ${DISK_NEED_GB} GB）" ;;
  insufficient)
    fail "磁碟不足：只有 ${DISK_FREE_GB} GB 可用，光是要拉的映像就需要約 ${DISK_NEED_GB} GB。"
    echo "  這一項是 image（第 5 步 compose 要拉的），**不含模型** —— 模型那一份"
    echo "  要等容器起來才問得到在不在，所以第 8 步會在下載前再擋一次。"
    echo "  空間不夠的話 pull 會**中途失敗**，有時是無聲的。"
    echo "  請先清出空間，或先把映像準備好（本機已有的映像不會被重拉）。"
    BLOCKERS=$((BLOCKERS + 1)) ;;
  unknown)
    warn "磁碟：讀不到可用空間或讀不到映像清單，跳過檢查 —— 這不是「足夠」，是「不知道」。"
    echo "      可用空間 ${DISK_FREE_GB:-?} GB、映像需求 ${DISK_NEED_GB:-?} GB。要自己確認。" ;;
esac

# 模型那一份**不在上面的數字裡**，所以這裡單獨把它講出來 —— 否則「磁碟：
# 2 GB 足夠」會被讀成「連模型都夠了」。第 8 步會在下載前用同一個值再擋一次，
# 那時候「在不在」才問得到。
case "$MODEL_GB" in
  unknown)
    warn "  模型（$MODEL）的磁碟需求：**不知道** —— 表上沒有這個模型，而腳本"
    warn "  不從 tag 猜大小（猜太小 = 放行到 pull 中途失敗，猜太大 = 對著一台"
    warn "  空間夠的機器誤擋）。要讓第 8 步擋得住，請用 --model-gb N 明講。" ;;
  *)
    info "  模型（$MODEL）約 ${MODEL_GB} GB —— 第 8 步下載前會再確認一次。" ;;
esac

# 第 8 步的基準：映像 ＋ 系統餘裕（**不含模型**）。在那裡它與「這次真的要
# 下載的那個模型」相加，才是完整的磁碟需求。
DISK_BASE_GB="$DISK_NEED_GB"

AVAIL_MB="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || true)"
RAM_NEED_MB="$(ram_warn_mb "$MODEL_GB" "$NUM_CTX")"
RAM_VERDICT="$(memory_verdict "${AVAIL_MB:-}" "${RAM_NEED_MB:-}")"
case "$RAM_VERDICT" in
  ok)
    ok "記憶體：${AVAIL_MB} MB 可用（估算需要約 ${RAM_NEED_MB} MB）" ;;
  tight)
    warn "記憶體偏緊：${AVAIL_MB} MB 可用，估算需要約 ${RAM_NEED_MB} MB。"
    warn "  那會是**變慢**（用到 swap），不是錯 —— 所以不擋。"
    warn "  註：這個估算裡的 KV cache 一項是 D-022 的**推測值**"
    warn "  （~${KV_KIB_PER_TOKEN_ESTIMATE} KiB/token，與模型的 KV head 數綁定），不是實測。"
    warn "  真的要省，把 --num-ctx 調小。" ;;
  unknown_avail)
    warn "記憶體：讀不到 /proc/meminfo，跳過檢查 —— 這不是「足夠」，是「不知道」。" ;;
  unknown_need)
    warn "記憶體：模型大小不知道，所以估不出需求 —— 跳過檢查（不擋）。"
    echo "      要用 --model-gb N 明講，這一項才會有數字。" ;;
  *)
    # 認不得的 arm 要**大聲**，不能靜默跳過 —— 靜默跳過正是 #93 的缺陷：
    # 一個沒有被列出的值落到 else，於是閘門對著它算不出來的東西印了綠色 OK。
    fail "記憶體：判準回了認不得的值「$RAM_VERDICT」—— 這是這支腳本自己壞掉，不是環境。"
    BLOCKERS=$((BLOCKERS + 1)) ;;
esac

VCPU="$(nproc 2>/dev/null || true)"
if [[ -n "$VCPU" && "$VCPU" =~ ^[0-9]+$ && "$VCPU" -lt 2 ]]; then
  warn "只有 ${VCPU} 顆 vCPU —— 可以跑，只是慢。不擋（那是慢，不是錯）。"
fi

# ── CPU 架構：宣告層 ＋ 裝置層（D-057）─────────────────────
# D-055 §一 記著：ARM VPS（Graviton／Ampere／Oracle ARM）沒有任何一層被驗證過。
# 這個缺口的形狀與 GPU 那個同屬一個家族 —— **不會報錯，只會安靜地跑出不同
# 結果**：映像是 multi-arch，所以 compose 起得來、健康檢查過、模型答得出話，
# 只有速度與 README 上那些數字是錯的。
#
# 但**不擋**。實查 registry 的 manifest（`docker manifest inspect`，不需要
# ARM 機器）之後，「arm64 上跑不動」是假的：ollama／open-webui／cloudflared
# 都出 arm64。硬擋會擋掉一個真的能用的佈署 —— D-055 §六.2 原本把方向寫成
# 「不認識的架構要 fail-closed」，那是錯的（見 D-057）。
#
# 這裡唯一會硬擋的是裝置層的 `no`：registry **明確列出了清單而裡面沒有**
# 這個架構。那不是「沒量過」，是「已知不可能」—— 與磁碟閘門同一類（壞在半路）。
#
# **只有可判定時才往下查**（見下面的 ARCH_CHECKABLE）：x86_64 是量過的那條路，
# 多問一次 registry 只是多一個網路相依與失敗模式。CPU 路徑上這整段就是一次
# uname 加一行安靜通過 —— **零網路呼叫、零新失敗模式。**
HOST_MACHINE="$(uname -m 2>/dev/null || true)"
ARCH_FAMILY="$(cpu_arch "$HOST_MACHINE")"

# 下面兩層（registry、step 13）都要問「這台機器是不是某個**具體的** Docker
# 架構」，而這個問題有兩種問不成的理由，處置完全不同：
#   · amd64 → 量過的那條路，沒東西要驗
#   · unknown（armv7l／riscv64／認不得的字串）→ **問不出來**：我們不知道
#     這台在 Docker 的詞彙裡叫什麼，所以沒有東西可以拿去比
# 兩種都**不跑**。這兩個條件寫成一個推導值而不是在兩處各寫一次 —— 同一個
# 事實寫兩次就會漂移（D-054 的形狀），而漂移的症狀會是「registry 說有、
# step 13 說模擬」這種自相矛盾的輸出。
ARCH_CHECKABLE=0
if [[ "$ARCH_FAMILY" == "arm64" ]]; then ARCH_CHECKABLE=1; fi

case "$(arch_verdict "$HOST_MACHINE")" in
  verified) : ;;
  unverified)
    warn "CPU 架構：${HOST_MACHINE}（ARM64）—— 這個 lab 只在 x86_64 上驗證過。"
    warn "  映像檔有 arm64，所以它會跑起來。**不擋** —— 那是「沒量過」，不是"
    warn "  「不能用」。但下面這些在 ARM 上沒有證據："
    warn "    · README 尺寸表的吞吐量（5–10 token/s 那幾欄）是 x86 量的"
    warn "    · README「When is this a fit?」的 GPU 建議是另一條沒驗過的路"
    warn "  跑完之後 step 13 會再量一次**本地那份映像**的架構，確認不是模擬執行。" ;;
  *)
    warn "CPU 架構：認不得 \`uname -m\` 的輸出（${HOST_MACHINE:-讀不到}）—— 只認得"
    warn "  x86_64 與 aarch64。不知道這台在 Docker 的詞彙裡叫什麼，所以下面"
    warn "  **兩層都不會跑**：沒有東西可以拿去跟 registry 或映像比。它會跑，"
    warn "  但這個 lab 的數字（含 README 的吞吐量）在這台上是沒有證據的。" ;;
esac

# 裝置層：registry 有沒有這個架構的映像檔。
if [[ "$ARCH_FAMILY" == "unknown" ]]; then
  # 認不得這台機器時**不去問 registry**。理由有兩個，第二個才是關鍵：
  #   1. `want=unknown` 對每一份 manifest 都只可能回 unknown（那條短路是
  #      刻意的）—— 兩次網路呼叫換不到任何資訊。
  #   2. 把 `$ARCH_FAMILY` 直接塞進句子裡會生出「0 個映像有 unknown」這種
  #      句子：`unknown` 是**我們自己的標記**，不是架構的名字。它一被講成
  #      架構名，讀的人就會以為「unknown 這個架構查不到」——
  #      而正確的意思是「我不知道這台是什麼，所以無從問起」。
  warn "  registry 查詢：認不得這台的架構，所以**沒辦法問** —— 這是「不知道」，"
  warn "  不是「沒問題」。拉的時候才會知道。"
elif [[ "$ARCH_CHECKABLE" == "1" ]]; then
  # 映像清單取自 compose 自己的模型，不是寫死一份 —— 寫死會在 compose 改了之後
  # **無聲地檢查錯的東西**。用 `image:` 那幾行而不是 `config --images`：後者會
  # 多出本地建的那個（mcp-test-server 只有 build、沒有 image），而 registry
  # 當然查不到它 —— 那會變成每次非 amd64 佈署都出現一次的**假警告**，而假警告
  # 會訓練人忽略警告。
  ARCH_IMAGES="$(cd "$PROJECT_ROOT" && $COMPOSE config 2>/dev/null \
    | grep -oE '^[[:space:]]+image:.*' \
    | sed 's/^[[:space:]]*image:[[:space:]]*//' | sort -u || true)"
  if [[ -z "$ARCH_IMAGES" ]]; then
    warn "  registry 查詢：讀不到 compose 的映像清單 —— 跳過。這是「不知道」，"
    warn "  不是「沒問題」：pull 的時候才會知道。"
  else
    ARCH_BAD=""
    ARCH_UNKNOWN=0
    ARCH_OK=0
    while IFS= read -r ARCH_IMG; do
      [[ -n "$ARCH_IMG" ]] || continue
      ARCH_MANIFEST="$(timeout 20 docker manifest inspect "$ARCH_IMG" 2>/dev/null || true)"
      case "$(image_arch_listed "$ARCH_MANIFEST" "$ARCH_FAMILY")" in
        yes) ARCH_OK=$((ARCH_OK + 1)) ;;
        no)
          ARCH_SEEN="$(printf '%s' "$ARCH_MANIFEST" \
            | grep -oE '"architecture":[[:space:]]*"[a-z0-9_]+"' \
            | sed 's/.*"\([a-z0-9_]*\)"$/\1/' | sort -u | paste -sd'/' -)"
          ARCH_BAD+="  · ${ARCH_IMG} —— 它有的是：${ARCH_SEEN:-（讀不到）}"$'\n' ;;
        *)
          ARCH_UNKNOWN=$((ARCH_UNKNOWN + 1)) ;;
      esac
    done <<< "$ARCH_IMAGES"

    if [[ -n "$ARCH_BAD" ]]; then
      fail "CPU 架構：registry 上沒有 ${ARCH_FAMILY} 的映像檔 —— 這不是「沒量過」，"
      fail "  是「拉不下來」。它看到的架構清單是："
      printf '%s' "$ARCH_BAD"
      echo "  pull 會**中途失敗**（有時是無聲的）—— 與磁碟閘門同一類，所以擋。"
      echo "  換一台機器，或改用有出這個架構的映像檔。"
      BLOCKERS=$((BLOCKERS + 1))
    elif [[ "$ARCH_UNKNOWN" -gt 0 ]]; then
      warn "  registry：${ARCH_OK} 個映像有 ${ARCH_FAMILY}，${ARCH_UNKNOWN} 個**查不到**"
      warn "  （網路、逾時、需要認證，或那個映像根本不是從 registry 拉的）。"
      warn "  查不到就是查不到 —— 不當成「沒有」（那會誤擋），也不當成「有」。"
    else
      ok "  registry：${ARCH_OK} 個映像都有 ${ARCH_FAMILY} —— 拉得下來。"
    fi
  fi
fi

# ── GPU：偵測、判定，並讓「有沒有在用 GPU」變成看得到的事 ──
# 這一節要消滅的是「宣告面 ≠ 執行面」家族的第三次。`docker-compose.yml`
# 完全沒有任何 device reservation，所以這個堆疊**在任何有 GPU 的機器上都是
# 由建構決定跑 CPU 的**，而且沒有任何訊號 —— 只是慢。而 README 把「一張
# 24 GB GPU」標成 sweet spot，並叫使用者自己去 compose 加一個 block。
#
# 判定邏輯在純函式裡（deploy-vps-decisions.sh 的 gpu_verdict，12 格真值表
# 逐格有測試），這裡只做 I/O。**VRAM 只警告、不擋**：裝不下是「慢」，不是
# 「壞」，與記憶體那條同一個理由。
GPU_MODE="$(read_env_value OLLAMA_GPU)"
GPU_MODE="${GPU_MODE:-auto}"
case "$GPU_MODE" in
  auto|on|off) : ;;
  *)
    warn "OLLAMA_GPU 的值認不得（$GPU_MODE）—— 只認 auto / on / off。"
    warn "  先用預設值 auto 繼續；要明講請改成三者之一。"
    GPU_MODE="auto" ;;
esac

GPU_HW="no"
GPU_NAME=""
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU_HW="yes"
  GPU_NAME="$(nvidia-smi -L 2>/dev/null | head -1)"
elif [[ -e /dev/nvidiactl ]]; then
  # 驅動裝了、裝置節點在，但 nvidia-smi 不在 PATH。硬體仍然是有的。
  GPU_HW="yes"
  GPU_NAME="有 /dev/nvidiactl，但 nvidia-smi 不在 PATH"
fi

GPU_RT="no"
if [[ "$GPU_HW" == "yes" ]] && command -v docker >/dev/null 2>&1; then
  GPU_RT="$(gpu_runtime_registered \
    "$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)")"
fi

GPU_VERDICT="$(gpu_verdict "$GPU_HW" "$GPU_RT" "$GPU_MODE")"
case "$GPU_VERDICT" in
  gpu)
    GPU_VRAM_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits \
      2>/dev/null | head -1 | tr -d ' ' || true)"
    ok "GPU：$GPU_NAME"
    ok "     NVIDIA container runtime 已註冊 —— 會自動掛上 docker-compose.gpu.yml"
    if [[ "$GPU_VRAM_MB" =~ ^[0-9]+$ ]]; then
      GPU_NEED_MB="$(ram_warn_mb "$MODEL_GB" "$NUM_CTX")"
      VRAM_VERDICT="$(memory_verdict "$GPU_VRAM_MB" "$GPU_NEED_MB")"
      case "$VRAM_VERDICT" in
        ok)
          ok "     VRAM：${GPU_VRAM_MB} MB（估算需要約 ${GPU_NEED_MB} MB）" ;;
        tight)
          warn "VRAM 偏緊：這張卡有 ${GPU_VRAM_MB} MB，估算需要約 ${GPU_NEED_MB} MB。"
          warn "  裝不下時 ollama 會**部分卸載**（把放不下的層丟到系統記憶體），"
          warn "  速度差 10–50×（README 的「When is this a fit?」）—— 那是慢，不是錯，"
          warn "  所以不擋。要省請縮小模型或 --num-ctx。"
          warn "  （KV cache 那一項是 D-022 的推測值，與模型的 KV head 數綁定。）" ;;
        unknown_need)
          # 這裡就是 #93：舊版是一個 `if … else`，需求量是空字串時條件不成立，
          # 於是落到 else 印出「VRAM：24576 MB（估算需要約  MB）」—— 綠色 OK
          # 配一個空白的需求數字。**else 不是「沒問題」，是「不是偏緊那個」**。
          warn "     VRAM：模型大小不知道，所以估不出需求 —— 跳過檢查（不擋）。"
          echo "       要用 --model-gb N 明講，這一項才會有數字。" ;;
        *)
          # `unknown_avail` 在外層守衛（`GPU_VRAM_MB` 已經證明是數字）之下
          # **到不了**，所以會落到這裡的只可能是判定與守衛不一致 ——
          # 那要停下來，不要猜。
          fail "     VRAM：判準回了認不得的值「$VRAM_VERDICT」—— 外層守衛與它不一致。"
          BLOCKERS=$((BLOCKERS + 1)) ;;
      esac
    fi ;;
  cpu)
    if [[ "$GPU_MODE" == "off" ]]; then
      info "GPU：OLLAMA_GPU=off —— 刻意跑 CPU，不會掛上 GPU 的那個 compose 檔。"
    else
      info "GPU：這台機器沒有 NVIDIA GPU —— 跑 CPU（不需要做任何事）。"
    fi ;;
  blocked)
    fail "GPU：偵測到 NVIDIA 硬體（$GPU_NAME），但 Docker **沒有註冊** NVIDIA"
    fail "     container runtime —— 容器看不到那張 GPU。"
    if [[ "$GPU_MODE" == "on" ]]; then
      echo "  OLLAMA_GPU=on 的意思是「一定要用 GPU」，不是「盡量用」，所以在這裡停下來。"
    else
      echo "  OLLAMA_GPU=auto 的定義是「偵測到就啟用」，而這裡偵測到了硬體、卻拿不到它"
      echo "  —— 那與「這台沒有 GPU」是兩件不同的事，所以不當成沒看到。"
    fi
    echo "  靜默退回 CPU 正是這一節要消滅的那件事（沒有訊號，只是慢），"
    echo "  所以在動到任何檔案或容器之前停在這裡。"
    echo
    echo "  裝好它（Debian/Ubuntu，需要 root）："
    echo "    sudo apt-get install -y nvidia-container-toolkit"
    echo "    sudo nvidia-ctk runtime configure --runtime=docker"
    echo "    sudo systemctl restart docker"
    echo
    echo "  驗證：docker info --format '{{json .Runtimes}}' 要看得到 nvidia 這個鍵"
    echo "  然後重跑這支腳本。"
    echo
    echo "  這台機器本來就不該用 GPU 的話，在 .env 設 OLLAMA_GPU=off 明講 ——"
    echo "  讓它是**選擇**，而不是意外。"
    if [[ "$ARCH_FAMILY" != "amd64" ]]; then
      echo
      echo "  ⚠ 但這台是 ${HOST_MACHINE}，上面那三行是 **x86 的配方**。ARM 上的"
      echo "    NVIDIA 是另一組套件（Jetson 走 JetPack、Grace 走伺服器版），"
      echo "    而且那條路這個 lab **一次都沒有量過** —— 上游甚至有 GLIBC 不符"
      echo "    而安靜退回 CPU 的案例（D-057）。不要把上面的指令直接貼過來。"
    fi
    BLOCKERS=$((BLOCKERS + 1)) ;;
esac

if [[ "$BLOCKERS" -gt 0 ]]; then
  fail "前置檢查未通過 —— 沒有動到任何檔案或容器。"
  exit 1
fi

# ── 3. 寫 .env（**必須在 load_env 之前**）───────────────
# 每一次寫入都走 env_upsert()：它保證所有生效的重複行收斂成恰好一行。
# `echo >> .env` 會在多跑一次之後留下重複鍵，而重複鍵是**後面那行生效** ——
# 於是「我已經把 WEBUI_BIND_ADDR 改成 127.0.0.1 了」可以與事實相反，無聲。
ENV_CHANGES=()
# 把算好的內容原子性地寫回 .env。回 0 = 真的寫了，回 1 = 沒變或被 dry-run 擋下。
# 兩個呼叫端（設值與移除）共用這一份，才不會有一邊忘了 chmod 或忘了用 rename。
_env_commit() {
  local new="$1" label="$2" tmp perms
  if [[ "$new" == "$(cat "$ENV_FILE")" ]]; then
    return 1          # 已經是這個樣子，連 mtime 都不動
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  （dry-run）$label"
    return 1
  fi
  tmp="$(mktemp "$PROJECT_ROOT/.env.tmp.XXXXXX")"
  perms="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo 600)"
  printf '%s\n' "$new" > "$tmp"
  chmod "$perms" "$tmp"
  # 同一個檔案系統內的 rename 是原子性的：不會留下「寫到一半的 .env」。
  mv -f "$tmp" "$ENV_FILE"
  return 0
}

apply_env() {
  local key="$1" value="$2" new
  new="$(env_upsert "$key" "$value" < "$ENV_FILE")"
  if _env_commit "$new" "會把 $key 設成 $value"; then
    ENV_CHANGES+=("$key=$value")
  fi
}

# 移除是**刪掉那一行**，不是把它設成空字串或某個預設值 —— 見下面 COMPOSE_FILE
# 的說明。與 env_upsert 對稱：它保證所有重複行一起消失。
apply_env_remove() {
  local key="$1" new
  new="$(env_remove "$key" < "$ENV_FILE")"
  if _env_commit "$new" "會移除 $key 這一行"; then
    ENV_CHANGES+=("$key=（已移除）")
  fi
}

apply_env WEBUI_BIND_ADDR "$BIND_ADDR"
apply_env OLLAMA_MODEL "$MODEL"
apply_env EMBEDDING_MODEL "$EMBED_MODEL"
apply_env OLLAMA_CONTEXT_LENGTH "$NUM_CTX"

# ── COMPOSE_FILE 是**推導值**，由上面的 GPU 判定決定 ─────
# 要改的是它的輸入 `OLLAMA_GPU`，不是它自己 —— 推導值被手改之後，沒有任何
# 東西在描述它與輸入的關係，下一次佈署會把它覆寫回去（D-054 的形狀）。
#
# 關閉時刻意是「**移除這一行**」而不是「設成 docker-compose.yml」：只要這個
# 鍵存在（哪怕只指向 base 檔），compose 就**不會**再自動載入
# `docker-compose.override.yml` —— 那是使用者沒要求我們動的行為。
# **缺席才是預設。**
if [[ "$GPU_VERDICT" == "gpu" ]]; then
  apply_env COMPOSE_FILE "docker-compose.yml:docker-compose.gpu.yml"
else
  apply_env_remove COMPOSE_FILE
fi

if [[ "$DRY_RUN" != "1" ]]; then
  if [[ ${#ENV_CHANGES[@]} -eq 0 ]]; then
    ok ".env 已經是最新的（沒有需要改的鍵）"
  else
    ok ".env 已更新：${ENV_CHANGES[*]}"
  fi
fi
echo

# ── 4. load_env（在 .env 寫**之後**）────────────────────
# 這一步就是那個陷阱的另一半：load_env 會 `set -a; source .env`，把值匯出成
# shell 環境變數，而 compose 的插值順序是 **shell > .env**。所以順序顛倒的話
# （先 load_env 再改 .env），compose 會繼續用那個已經匯出的舊值，無聲。
if [[ "$DRY_RUN" == "1" ]]; then
  info "（dry-run）不呼叫 load_env —— 它會匯出環境變數，那是實際佈署才要做的事"
else
  load_env
fi

# ── 5. 起容器 ───────────────────────────────────────────
if [[ "$DRY_RUN" == "1" ]]; then
  info "（dry-run）到這裡為止。實際執行時接下來會："
  echo "    docker compose up -d --wait --remove-orphans"
  if [[ "$GPU_VERDICT" == "gpu" ]]; then
    echo "    然後跑對外暴露閘門、下載模型、煙霧測試、num_ctx 確認，"
    echo "    以及 GPU 確認（讀 /api/ps 的 size_vram）。"
  else
    echo "    然後跑對外暴露閘門、下載模型、煙霧測試與 num_ctx 確認。"
    echo "    （GPU 那一段不會跑：判定是 $GPU_VERDICT。）"
  fi
  # 上面那個磁碟數字**不含模型那一份** —— 那要等容器起來才問得到「在不在」。
  # dry-run 走不到第 8 步，所以這件事必須在這裡講清楚，否則「磁碟：2 GB 足夠」
  # 會被讀成「連模型都夠了」。
  echo "    另外，第 8 步在下載模型之前會**再擋一次**磁碟 —— 那一次才算進模型。"
  if [[ "$MODEL_GB" == "unknown" ]]; then
    echo "    而 $MODEL 的大小是「不知道」（表上沒有），所以那一次沒有判準；"
    echo "    要它擋得住，請加 --model-gb N 明講。"
  else
    echo "    那次會用 $MODEL_GB GB（$MODEL）。"
  fi
  if [[ "$ARCH_FAMILY" == "amd64" ]]; then
    echo "    （架構那一段不會跑：這台是 x86_64，也就是量過的那條路。）"
  elif [[ "$ARCH_CHECKABLE" == "1" ]]; then
    echo "    以及架構確認（比對本地映像的 .Architecture 與主機，確認不是模擬執行）。"
  else
    echo "    （架構那一段只到警告為止：認不得 ${HOST_MACHINE:-這台}，"
    echo "      沒有東西可以拿去跟 registry 或映像比。）"
  fi
  exit 0
fi

info "啟動容器（等待 healthcheck 通過，首次可能需要 1-2 分鐘）..."
if ! $COMPOSE up -d --wait --remove-orphans; then
  fail "容器啟動失敗。以下為最近日誌："
  $COMPOSE logs --tail=40 >&2
  exit 2
fi
ok "容器已就緒"

# ── 6. 這台機器上有沒有人類的資料 ───────────────────────
# 放在 pull 之前：若這裡要停，就不該先花二十分鐘下載模型。
COUNTS="$($COMPOSE exec -T open-webui python3 -c '
import sqlite3, os
p = "/app/backend/data/webui.db"
if not os.path.exists(p):
    print("0 0"); raise SystemExit
try:
    c = sqlite3.connect(p)
    u = c.execute("SELECT COUNT(*) FROM user").fetchone()[0]
    h = c.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
    print("%d %d" % (u, h))
except Exception:
    print("? ?")
' 2>/dev/null || echo "? ?")"
NZ_USERS="${COUNTS%% *}"
NZ_CHATS="${COUNTS##* }"

if [[ "$NZ_USERS" =~ ^[0-9]+$ && "$NZ_CHATS" =~ ^[0-9]+$ ]]; then
  case "$(fresh_install_verdict "$NZ_USERS" "$NZ_CHATS" "$KEEP_DATA")" in
    fresh)
      ok "全新安裝（Open WebUI 沒有任何帳號或對話）" ;;
    kept)
      warn "這台機器上已經有 ${NZ_USERS} 個帳號、${NZ_CHATS} 則對話 —— 但指定了 --keep-data，繼續。"
      warn "  注意：ENABLE_SIGNUP 這類設定**只在第一次開機有效**，之後以資料庫為準（D-012）。" ;;
    occupied)
      fail "這台機器上已經有 ${NZ_USERS} 個帳號、${NZ_CHATS} 則對話 —— 這不是全新安裝。"
      echo
      echo "  這支腳本只支援全新安裝，不搬資料。它不會刪掉任何東西 ——"
      echo "  上面的容器只是起來了，你的 volume 完好無損。"
      echo
      echo "  要繼續的話有兩條路："
      echo "    · 這是刻意的（你就是要更新一個既有安裝）："
      echo "        bash scripts/deploy-vps.sh --keep-data"
      echo "    · 你要的其實是原本的一鍵啟動："
      echo "        bash scripts/up.sh"
      echo
      echo "  資料搬遷是另一份程序，不在這裡做。"
      $COMPOSE down --remove-orphans >/dev/null 2>&1 || true
      exit 1 ;;
  esac
else
  warn "讀不到 Open WebUI 的帳號／對話數（回報「$COUNTS」）—— **不擋**，繼續。"
  warn "  「讀不到」不等於「沒有」；這裡的取捨是：這支腳本不會刪任何東西，"
  warn "  所以猜錯的代價只是把一個既有安裝當成新的來設定。"
fi

# ── 7. 對外暴露閘門（strict）────────────────────────────
# 走到這裡時埠**應該**已經是關的（第 3 步在起容器之前就寫了 .env）。所以
# 這道閘門是對一個已經安全的狀態做確認 —— 它防的是「.env 寫進去了但沒有
# 生效」這件事靜默發生。strict 模式：**「無法判定」不可以當成「安全」**，
# 在全新安裝上那正是最危險的誤讀。
EXPOSURE_RC=0
EXPOSURE_OUT="$(bash "$SCRIPT_DIR/check-exposure.sh" 2>&1)" || EXPOSURE_RC=$?
GATE="$(exposure_gate_verdict "$EXPOSURE_RC" strict)"

if [[ "$EXPOSE" == "1" && "$GATE" != "broken" ]]; then
  # --expose 的合法用法：這台機器有公開 NIC，但前面有真正的邊緣過濾。
  # check-exposure.sh 看不見安全性群組與主機防火牆（它自己講明了），所以
  # 它會誤報 —— 而 D-016 說一個會亂叫的檢查會被忽略。這裡把它降級成警告。
  #
  # **「無法判定」也一起降級。** 指定 --expose 的人已經明講要自己承擔這一塊；
  # 在那种情況下把他擋在「我讀不出來」上面，是拿一個他已經回答過的問題
  # 再問他一次。但訊息要把「我們沒有替你確認」講清楚，不能讀成「確認過了」。
  warn "══ 你指定了 --expose：Open WebUI 正在監聽所有介面 ══"
  echo
  printf '%s\n' "$EXPOSURE_OUT"
  echo
  warn "這代表**任何能連到 3000 埠的人都不經過 Cloudflare Access**。"
  warn "Access 保護的是 tunnel 那條路，不是這個埠。"
  warn "請確認雲端安全性群組或主機防火牆**真的**擋住了 3000 ——"
  warn "這支腳本看不到那兩者，所以它無法替你確認。"
  if [[ "$GATE" == "indeterminate" ]]; then
    warn "而且這次連「這台機器有沒有公開位址」都沒能判定（結束碼 $EXPOSURE_RC）。"
    warn "**我們沒有替你確認任何事** —— 這一步完全靠你自己的邊緣過濾。"
  fi
  echo
elif [[ "$GATE" == "block" ]]; then
  fail "發現對外暴露 —— 佈署中止。"
  echo
  printf '%s\n' "$EXPOSURE_OUT"
  echo
  fail "容器已收掉（down 不帶 -v，volume 完好）。"
  bash "$SCRIPT_DIR/down.sh" >/dev/null 2>&1 || true
  exit 1
elif [[ "$GATE" == "indeterminate" ]]; then
  fail "無法判定是否對外暴露 —— 佈署中止。"
  echo
  printf '%s\n' "$EXPOSURE_OUT"
  echo
  fail "在全新安裝上，「我沒辦法確認埠是關的」不可以當成「埠是關的」。"
  fail "容器已收掉（down 不帶 -v，volume 完好）。"
  bash "$SCRIPT_DIR/down.sh" >/dev/null 2>&1 || true
  exit 2
elif [[ "$GATE" == "broken" ]]; then
  fail "對外暴露檢查本身失敗了（結束碼 $EXPOSURE_RC）—— 佈署中止。"
  printf '%s\n' "$EXPOSURE_OUT"
  exit 3
else
  ok "對外暴露檢查通過（埠只綁在 loopback）"
fi

# --num-ctx 的說明放這裡：上面的閘門可能已經讓腳本結束了，不必對著一個
# 已經中止的佈署解釋 context 長度。
if [[ "$(ctx_meets_mem0 "$NUM_CTX")" == "yes" ]]; then
  ok "num_ctx=$NUM_CTX >= $MEM0_ADD_MIN_CTX —— mem0 的抽取 prompt（實測 8,052／8,100）進得去"
  # 這一條只講了**第一個**門檻（生成之前不被截斷）。第二個門檻（生成期間
  # 不被 context shift 掏空）在下面用同一組常數再判一次 —— 兩個都要滿足，
  # 而 8192 這種「剛好過第一個」的值正是會漏掉第二個的那一種（D-035）。
  if [[ "$(ctx_holds_through_generation "$NUM_CTX")" == "yes" ]]; then
    ok "而且整段生成期間待得住（需要 >= $MEM0_ADD_HOLD_CTX = $MEM0_ADD_MAX_PROMPT + $MEM0_ADD_NUM_PREDICT + 1）"
  else
    warn "num_ctx=$NUM_CTX **不足以讓 prompt 待完整段生成**：生成每超過"
    warn "  num_ctx - prompt 個 token，llama-server 就會 context shift，"
    warn "  從 prompt 中段丟掉一整塊（8192 之下餘裕只有 140，D-035）。"
    warn "  要的是 num_ctx > prompt + num_predict，也就是 >= $MEM0_ADD_HOLD_CTX。"
  fi
else
  warn "num_ctx=$NUM_CTX **小於** $MEM0_ADD_MIN_CTX —— mem0 的抽取 prompt 會被截斷。"
  warn "  截斷規則是 num_ctx >= prompt tokens + 1（D-027），砍掉的是**開頭與中段**，"
  warn "  也就是模型自己的指令。付一樣的錢，換到一次讀不到指令的呼叫。"
fi
echo

# ── 8. 下載模型（兩個，都冪等）──────────────────────────
#
# 這個模型在不在本機。**三態，而中間那一態是 D-058 才加的。**
#
# 舊版只有兩態：讀不到 `ollama list` 就落到「不在」，於是會多拉一次 —— 舊版
# 無害。但這條分支現在要**硬擋**，同一個收斂就變成「對著一台好好的機器說它
# 空間不夠」—— 一個會誤報的守衛比沒有守衛更糟（D-056 的 size_vram 那一課）。
#
# 用**結束碼**區分，不是用輸出：`ollama list` 在一個模型都沒有的機器上只印
# 一行表頭，那與「指令失敗」在輸出上長得一樣。結束碼分得出來。
model_installed() {
  local name="$1" want list rc=0 line
  want="$(normalize_model "$name")"
  list="$($COMPOSE exec -T ollama ollama list 2>/dev/null)" || rc=$?
  if [[ "$rc" -ne 0 ]]; then printf 'unknown'; return 0; fi
  while IFS= read -r line; do
    if [[ -n "$line" && "$(normalize_model "$line")" == "$want" ]]; then
      printf 'yes'; return 0
    fi
  done <<<"$(awk 'NR>1 {print $1}' <<<"$list")"
  printf 'no'
}

# 下載前的磁碟閘門（D-058 的第二段）。**放在真的會下載的那條分支上。**
#
# 為什麼是這裡而不是第 2 步：模型在不在，只有 ollama 容器起來（第 5 步）
# 之後才問得到。而把檢查放在「真的會做」的那條分支上，兩者就不可能漂移。
#
# **不足時直接 `exit 1`，不是 `return`。** 呼叫端是 `pull_if_missing ... || exit 2`，
# 而 2 是「無法判定」—— 磁碟不足是**確定的失敗**（D-018）。`exit` 直接結束
# 行程，不會被呼叫端的 `||` 蓋成 2。
#
# **不在這裡呼叫 down.sh**：第 7 步已經證明過綁定是關的，留著堆疊讓使用者
# 清完空間就能重跑 —— 而「跑到一半失敗」正是這支腳本最常被用到的路徑。
# 訊息會明講「這一步沒有開始下載」。
pull_if_missing() {
  local name="$1" gb="${2:-unknown}" state free need recheck
  state="$(model_installed "$name")"

  if [[ "$state" == "yes" ]]; then
    ok "模型 $name 已存在，略過下載（不需要額外空間）"
    return 0
  fi
  if [[ "$state" == "unknown" ]]; then
    warn "讀不到 ollama 的模型清單 —— 當成「不確定」，直接嘗試下載。"
    warn "  這裡刻意不擋：把「讀不到」當成「空間不夠」是假失敗（D-016）。"
  fi

  # 每一次都重新讀 df（不是第 2 步讀一次）—— 第二個模型要看到第一個已經
  # 吃掉的空间。
  free="$(df -Pk "$DISK_PATH" 2>/dev/null \
    | awk 'NR==2 {printf "%d", $4/1024/1024}' || true)"
  need="$(disk_need_gb "$gb" yes "$DISK_BASE_GB")"

  case "$(disk_verdict "${free:-}" "${need:-}")" in
    insufficient)
      fail "磁碟不足：要下載 $name 需要約 ${need} GB，而只有 ${free} GB 可用。"
      echo "  （與第 2 步那個數字的差別：那個不含模型，因為容器還沒起來、"
      echo "   問不到它在不在；這一個才是這次真的要寫進磁碟的量。）"
      echo "  **這一步沒有開始下載** —— 堆疊維持在目前狀態。清出空間之後直接"
      echo "  重跑這支腳本即可，已下載的模型不會重拉。"
      exit 1 ;;
    unknown)
      warn "磁碟：$name 要多少空間無法判定（讀不到可用空間或算不出需求）——"
      warn "  這是「不知道」，不是「足夠」。繼續下載，但失敗時請先懷疑空間。" ;;
    *)
      ok "磁碟：$name 需要約 ${need} GB，可用 ${free} GB" ;;
  esac

  info "下載 $name ..."
  if ! $COMPOSE exec -T ollama ollama pull "$name"; then
    fail "模型 $name 下載失敗。堆疊本身已起來，可稍後重試：bash scripts/pull-model.sh $name"
    # 失敗的線索：重新讀一次 df。已經不足的話就講出來 —— 空間是最常見的
    # 成因，而 pull 自己的錯誤訊息不會那樣說。
    recheck="$(df -Pk "$DISK_PATH" 2>/dev/null \
      | awk 'NR==2 {printf "%d", $4/1024/1024}' || true)"
    if [[ -n "$need" && "$recheck" =~ ^[0-9]+$ ]] && (( recheck < need )); then
      echo "  線索：現在只剩 ${recheck} GB，而這個下載需要約 ${need} GB ——"
      echo "  這次失敗**很可能是空間**，不是網路或 registry。"
    fi
    return 1
  fi
  ok "模型 $name 下載完成"
}

pull_if_missing "$MODEL" "$MODEL_GB" || exit 2
# 嵌入模型**一定要拉**：mem0 的 OllamaEmbedding 遇到不存在的嵌入模型會
# 自己 pull（見 verify-chroma-dims.sh），那會讓一次「驗證」變成一次數 GB 的
# 下載；而 chroma 的維度一旦改變，既有的向量庫就全部作廢。
#
# `$EMBED_GB` 一定要傳：漏了的話 `set -u` 會在這裡大聲失敗（那正是我們要的
# —— 安靜地少一個守衛才是壞事）。
pull_if_missing "$EMBED_MODEL" "$EMBED_GB" || exit 2

# ── 9. 下載後的記憶體檢查：用**實測**的模型大小 ─────────
# 上面那次用的是從 tag 查表估出來的數字。這裡讀 /api/tags 的 size 欄位，
# 那是**位元組**，是實測值。
#
# 刻意不解析 `ollama list` 的 SIZE 欄：那是人看的字串（"2.5 GB"），而本專案
# 在別的地方已經記過「不要從人看的輸出裡撈數值」（connect-endpoint.sh 用
# 結束碼判斷、ollama_log_corroboration.sh 有一整條分支在處理格式變了）。
MEASURED_BYTES="$($COMPOSE exec -T open-webui python3 -c '
import json, sys, urllib.request
want = sys.argv[1]
try:
    r = json.load(urllib.request.urlopen("http://ollama:11434/api/tags", timeout=15))
except Exception:
    sys.exit(0)
for m in r.get("models", []):
    n = m.get("name") or m.get("model") or ""
    if n == want or n.split(":")[0] == want.split(":")[0]:
        print(int(m.get("size") or 0)); break
' "$(normalize_model "$MODEL")" 2>/dev/null || true)"

if [[ "$MEASURED_BYTES" =~ ^[0-9]+$ && "$MEASURED_BYTES" -gt 0 ]]; then
  MEASURED_MB=$((MEASURED_BYTES / 1024 / 1024))
  BASE_MB=2048                       # Open WebUI ~1GB + OS/docker ~1GB
  KV_MB=$((NUM_CTX * KV_KIB_PER_TOKEN_ESTIMATE / 1024))
  if [[ -n "${AVAIL_MB:-}" && "$AVAIL_MB" =~ ^[0-9]+$ ]]; then
    if (( AVAIL_MB < MEASURED_MB + KV_MB + BASE_MB )); then
      warn "實測模型 ${MEASURED_MB} MB ＋ KV 估算 ${KV_MB} MB ＋ 基礎 ${BASE_MB} MB" \
           "＝ $((MEASURED_MB + KV_MB + BASE_MB)) MB，而可用記憶體只有 ${AVAIL_MB} MB。"
      warn "  仍然不擋（那是慢，不是錯）—— 但若下面煙霧測試失敗，先懷疑記憶體。"
      warn "  KV 那一項是推測值；真的要省，把 --num-ctx 調小。"
    else
      ok "記憶體足夠：實測模型 ${MEASURED_MB} MB（可用 ${AVAIL_MB} MB）"
    fi
  fi
else
  warn "讀不到 $MODEL 的實測大小（/api/tags）—— 跳過下載後的記憶體檢查（不擋）。"
fi
echo

# ── 10. 煙霧測試：一次真的生成 ──────────────────────────
# 這一步存在的理由是本專案最常重複的那個教訓：**被切斷的生成不能拿來當
# 完整的量測**（D-027 §11，六個實例）。qwen3 是推理模型，thinking token 會
# 算進 num_predict，所以一個太小的預算可能被 thinking 吃光、回傳空的
# content —— 那與「佈署壞了」**無法區分**。判定邏輯（四條斷言）在探針裡，
# 是可離線測試的純函式。
info "煙霧測試：送一次真的生成（num_predict=512, temperature=0）..."
SMOKE_RC=0
$COMPOSE exec -T open-webui python3 - smoke "$MODEL" < "$SMOKE_PROBE" || SMOKE_RC=$?
case "$SMOKE_RC" in
  0) : ;;
  1)
    fail "煙霧測試未通過 —— 模型有回應，但回應不合格（詳見上方）。"
    fail "這是**上游行為**的問題，不是環境：回應裡看得到它哪裡不對。"
    exit 1 ;;
  2)
    fail "煙霧測試無法判定 —— ollama 連不上、或模型不在（詳見上方）。"
    exit 2 ;;
  *)
    fail "煙霧測試自己的結束碼是 $SMOKE_RC —— 那是這支探針壞了，不是佈署的問題。"
    exit 3 ;;
esac
echo

# ── 11. 確認 num_ctx 真的生效 ───────────────────────────
# **讀 /api/ps，不是讀日誌。** DECISIONS.md 只證明過 OLLAMA_CONTEXT_LENGTH
# 這個**名字**存在於 ollama 的執行檔裡，從沒證明它會生效。而日誌在這裡是
# 錯的儀器：ollama_log_corroboration.sh 的存在，就是因為讀日誌有三種無聲的
# 失敗模式（--since 靜默回 0 行、日誌中段損壞、看起來很合理的過期視窗）。
#
# 這一步刻意**不卸載模型來「保持整潔」**：mem0_add_cost_probe.py 記過，
# 用 /api/chat 卸載會讓探針卡住（推理模型會產生無界的 thinking 區塊）。
# 模型留在記憶體裡，正是 /api/ps 讀得到的原因。
info "確認 ollama 回報的 context_length 是不是 $NUM_CTX ..."
CTX_RC=0
$COMPOSE exec -T open-webui python3 - ctx "$MODEL" "$NUM_CTX" < "$SMOKE_PROBE" || CTX_RC=$?
case "$CTX_RC" in
  0) : ;;
  2)
    warn "══ 堆疊是好的、可以用了，但我們設的 num_ctx 沒有生效 ══"
    echo "  這是「看起來設好了、其實沒有」那一類的 bug，所以**不能回 0**。"
    echo "  它也不是 1：1 代表「上游變了、某條判準沒過」，而這裡上游沒有變，"
    echo "  是一個我們自己設的伺服器端旋鈕沒生效，原因還不知道。"
    echo
    echo "  還能試的："
    echo "    · 直接問 ollama：docker compose exec ollama env | grep OLLAMA_CONTEXT"
    echo "    · Modelfile 的 PARAMETER num_ctx（模型層級的覆寫）"
    echo "    · 換一個更大的 --num-ctx 再跑一次"
    exit 2 ;;
  *)
    fail "num_ctx 確認本身壞掉了（結束碼 $CTX_RC）。"
    exit 3 ;;
esac
echo

# ── 12. 確認模型真的在 GPU 上（**只有宣稱要用 GPU 時才跑**）──
# 「裝置可見」與「runtime 已註冊」都還是**宣告面**：它們證明我們要求了 GPU，
# 不證明 ollama 用上了。`ollama ps` 的 size_vram 是 ollama 自己回報的實際
# 配置，那才是執行面。這一節送一次極小生成把模型載進去，再讀 /api/ps。
#
# **CPU 路徑整個不跑**：沒有宣稱就沒有東西要驗。使用者的選擇（OLLAMA_GPU=off）
# 或這台機器本來就沒 GPU，都不該在完成清單上多出一行「GPU 檢查」。
if [[ "$GPU_VERDICT" == "gpu" ]]; then
  info "確認 ollama 真的把模型放在 GPU 上（讀 /api/ps 的 size_vram）..."
  GPU_CHECK_RC=0
  $COMPOSE exec -T open-webui python3 - gpu "$MODEL" < "$SMOKE_PROBE" || GPU_CHECK_RC=$?
  case "$GPU_CHECK_RC" in
    0) : ;;
    1)
      fail "模型跑在 CPU 上，而我們告訴使用者這台在用 GPU（詳見上方）。"
      fail "失效的是我們自己的宣稱，不是環境無從判定 —— 所以是 1，不是 2。"
      fail "上面那段訊息有確診指令（docker compose exec ollama nvidia-smi -L）。"
      exit 1 ;;
    2)
      warn "GPU 使用情形**量不到**（部分卸載、或 /api/ps 讀不到那個欄位）。"
      warn "  這是真實的量測結果而不是判準沒過：GPU 確實在用，只是無法斷言"
      warn "  整顆模型都在上面。堆疊可以用。"
      exit 2 ;;
    *)
      fail "GPU 檢查本身壞掉了（結束碼 $GPU_CHECK_RC）。"
      exit 3 ;;
  esac
  echo
fi

# ── 13. 確認本地那份映像就是主機的架構（**只有可判定時才跑**）──
# 前三層裡，前面兩層都還是**宣告面**：`uname -m` 說的是主機，`docker manifest
# inspect` 說的是 registry 上有什麼。這一層問的是**這台機器上、剛剛被拿去跑的
# 那份映像**是哪個架構 —— 那才是 Docker 真正拿去執行的位元。
#
# **儀器刻意不是「進容器讀 platform.machine()」**：那個值要靠 qemu-user 把
# `uname` 假造成被模擬的架構。那個行為我沒有辦法在這裡證明（本機是 x86，沒有
# qemu），而**一個「失效時會安靜地說 native」的檢查比沒有檢查更糟** ——
# 與 D-056 的 size_vram 同一課。映像的 `.Architecture` 是決定 Docker 挑哪個
# manifest 的權威欄位，而且是本地中繼資料：量得到就是判準，不是推論。
#
# 沒有宣稱就沒有東西要驗：amd64 主機整個不跑（與 GPU 那一節同一條規則）。
# 認不得的架構也不跑 —— 那時連「主機的 Docker 架構叫什麼」都不知道，比不出
# 東西；跑了只會每次都回 2（無法判定），而那種判準沒有資訊（見 ARCH_CHECKABLE）。
if [[ "$ARCH_CHECKABLE" == "1" ]]; then
  info "確認跑起來的映像是原生架構（讀各映像的 .Architecture）..."
  ARCH_CHECK_RC=0
  ARCH_EMULATED=""
  ARCH_NOLOCAL=""
  # `${ARCH_IMAGES:-}` 而不是 `$ARCH_IMAGES`：它是在 step 2 算的，離這裡五百
  # 多行，而 lib.sh 開了 `set -u`。多一層預設值讓「有人把上面那行移走」的症狀
  # 是「這一層說無法判定」，不是「整支腳本在某一行爆掉」。
  if [[ -z "${ARCH_IMAGES:-}" ]]; then
    warn "  讀不到映像清單（上面 compose config 沒給出 image:）—— 這一層無法判定。"
    ARCH_CHECK_RC=2
  else
    while IFS= read -r ARCH_IMG; do
      [[ -n "$ARCH_IMG" ]] || continue
      ARCH_LOCAL="$(timeout 20 docker image inspect "$ARCH_IMG" \
        --format '{{.Architecture}}' 2>/dev/null || true)"
      case "$(arch_match_verdict "$HOST_MACHINE" "$ARCH_LOCAL")" in
        native) ok "  $ARCH_IMG → $ARCH_LOCAL（原生）" ;;
        emulated)
          fail "  $ARCH_IMG → $ARCH_LOCAL —— 與主機（$HOST_MACHINE）不符"
          ARCH_EMULATED+="  · ${ARCH_IMG} —— 映像 ${ARCH_LOCAL}，主機 $HOST_MACHINE"$'\n' ;;
        *)
          warn "  $ARCH_IMG → 讀不到架構（映像不在本地？）"
          ARCH_NOLOCAL+="  · ${ARCH_IMG}"$'\n' ;;
      esac
    done <<< "$ARCH_IMAGES"

    if [[ -n "$ARCH_EMULATED" ]]; then
      echo
      fail "══ 這份映像不是主機的架構 —— 它**正在被模擬執行** ══"
      printf '%s' "$ARCH_EMULATED"
      echo
      echo "  這種失敗沒有任何其他訊號：compose 起得來、健康檢查過、模型也答話，"
      echo "  只是慢 10–100× —— 所以上面那幾層都攔不到它。"
      echo "  兩個可能的原因，都不在 compose 檔裡："
      echo "    · 環境有 DOCKER_DEFAULT_PLATFORM（或 build 時下了 --platform）"
      echo "    · 拉的時候用了 --platform linux/amd64，而本地留著那一份"
      echo "  確診：docker image inspect <上面那顆映像> --format '{{.Architecture}}'"
      echo "  修法：docker rmi 那份，再讓這次佈署重新拉一次。"
      ARCH_CHECK_RC=1
    elif [[ -n "$ARCH_NOLOCAL" ]]; then
      echo
      warn "══ 有映像在本地讀不到架構 —— 這一層**無法判定** ══"
      printf '%s' "$ARCH_NOLOCAL"
      echo "  注意這不是「沒問題」：讀不到就是不知道它是不是原生。"
      echo "  但容器已經起來了，所以它至少跑得動 —— 不擋。"
      ARCH_CHECK_RC=2
    fi
  fi

  case "$ARCH_CHECK_RC" in
    0) : ;;
    1)
      fail "失效的是我們自己的宣稱（我們說這一疊在原生的架構上跑），所以是 1，不是 2。"
      exit 1 ;;
    2) exit 2 ;;
  esac
  echo
fi

# ── 14. 完成 ────────────────────────────────────────────
if [[ "$GPU_VERDICT" == "gpu" ]]; then
  ok "佈署完成 —— 堆疊已起來、埠只綁 loopback、模型可用、num_ctx 已生效、模型在 GPU 上"
else
  ok "佈署完成 —— 堆疊已起來、埠只綁 loopback、模型可用、num_ctx 已生效"
fi
echo
"$SCRIPT_DIR/status.sh" || true

if [[ "$GPU_VERDICT" == "gpu" ]]; then
  cat <<EOF

GPU：$GPU_NAME
  docker-compose.gpu.yml 已掛上（.env 的 COMPOSE_FILE），而上面那次推論證明
  模型真的在 GPU 上。要改回 CPU 請在 .env 設 OLLAMA_GPU=off 再重跑 ——
  **不要手改 COMPOSE_FILE**，那是推導值，下次佈署會被覆寫回去。
EOF
fi

# 認不得的架構**不印這段**：下面那三句「確認過了」在有跑那兩層時才是真的，
# 沒跑就印會是這個 repo 最貴的那種錯（宣告面冒充執行面）。
if [[ "$ARCH_CHECKABLE" == "1" ]]; then
  cat <<EOF

CPU 架構：$HOST_MACHINE（$ARCH_FAMILY）
  映像有這個架構，pull 拉到了，跑起來的也是原生架構 —— 那些都確認過了。
  但**這個 lab 的數字全部是 x86 量的**：README 尺寸表的吞吐量、GPU 那一節的
  建議，在 $ARCH_FAMILY 上都沒有證據。它會跑，跑多快沒人知道。
EOF
fi

cat <<EOF
  • 本機／SSH 通道：http://localhost:3000
  • 對外：一律走 Cloudflare Tunnel + Access（見 .env.example 的
    CLOUDFLARE_TUNNEL_TOKEN 段落）。**不要**為了方便把埠開出去。

首次使用：
  1. 建立第一個帳號 —— 它會自動成為管理員，所以務必確認那是你自己
  2. 登入後立刻鎖住註冊：bash scripts/lock-signup.sh

這裡**刻意沒有跑**的驗證（它們會跑很久，而且通常不需要每次都跑）：

  bash scripts/check-exposure.sh                  誰能從外面連到這個堆疊
  bash scripts/verify.sh                          第一階段的端到端驗證
  bash scripts/verify-mem0-add-cost.sh            item 3：add() 的成本結構
      ↑ 若上面 num_ctx 用的是 8192，這支會以結束碼 2 停下並說明原因 ——
        它驗的判準以「預設值小到會截斷」為前提，那個前提已經被拿掉了。
  bash scripts/rag-verify.sh --model qwen2.5:3b   第二階段的 RAG 驗證

要留下機械證據的話，把煙霧測試與 num_ctx 的輸出抄進 DECISIONS.md。
EOF
