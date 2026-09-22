#!/usr/bin/env bash
# 一行把**全新的 VPS**變成一個安全設定好、而且被證明過的堆疊。
#
# 用法：
#   bash scripts/deploy-vps.sh [--model NAME] [--embed-model NAME]
#                              [--num-ctx N] [--expose] [--keep-data] [--dry-run]
#
#   --model NAME        對話模型（預設沿用 .env 的 OLLAMA_MODEL）
#   --embed-model NAME  嵌入模型（預設沿用 .env 的 EMBEDDING_MODEL）
#   --num-ctx N         伺服器端的 context 長度，**預設 16384**（見下方說明）
#   --expose            刻意綁 0.0.0.0。**這是對一個真實取捨的確認，不是方便旗標**
#   --keep-data         機器上已經有人類的資料時，仍然繼續（見下方說明）
#   --dry-run           只做前置檢查與 .env 的差異顯示，**不寫任何檔案、不起容器**
#
# 結束碼：0 = 佈署完成且通過煙霧測試
#         1 = 未通過（發現暴露、資料已存在、煙霧測試失敗 —— 都附完整輸出）
#         2 = 無法判定（容器起不來、模型下載失敗、num_ctx 沒生效）
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
    --expose)      EXPOSE=1; shift ;;
    --keep-data)   KEEP_DATA=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
# 磁碟不足不會優雅地變慢，它會在 pull 到一半的時候中止（README 已經記了
# 「需要約 2 倍空間，否則 pull 會失敗，有時是無聲的」）。CPU 與記憶體不足
# 是「慢」，磁碟不足是「壞在半路」—— 這個差別才是阻擋的理由，不是嚴重程度。
MODEL_GB="$(model_gb_estimate "$MODEL")"
DISK_FREE_GB="$(df -Pk "$PROJECT_ROOT" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}')"
DISK_NEED_GB="$(disk_need_gb "$MODEL_GB")"

BLOCKERS=0
case "$(disk_verdict "${DISK_FREE_GB:-}" "${DISK_NEED_GB:-}")" in
  sufficient)
    ok "磁碟：${DISK_FREE_GB} GB 可用（需要約 ${DISK_NEED_GB} GB）" ;;
  insufficient)
    fail "磁碟不足：只有 ${DISK_FREE_GB} GB 可用，這個模型需要約 ${DISK_NEED_GB} GB。"
    echo "  pull 會佔用下載暫存與展開後的本體（約 2 倍），空間不夠會**中途失敗**，"
    echo "  有時是無聲的。請先清出空間，或換一個較小的模型（--model）。"
    BLOCKERS=$((BLOCKERS + 1)) ;;
  unknown)
    warn "磁碟：無法從模型名稱（$MODEL）估出大小，跳過檢查 —— 這不是「足夠」，是「不知道」。"
    echo "      可用空間 ${DISK_FREE_GB:-?} GB。要自己確認。" ;;
esac

AVAIL_MB="$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || true)"
RAM_NEED_MB="$(ram_warn_mb "$MODEL_GB" "$NUM_CTX")"
case "$(ram_verdict "${AVAIL_MB:-}" "${RAM_NEED_MB:-}")" in
  ok)
    ok "記憶體：${AVAIL_MB} MB 可用（估算需要約 ${RAM_NEED_MB} MB）" ;;
  tight)
    warn "記憶體偏緊：${AVAIL_MB} MB 可用，估算需要約 ${RAM_NEED_MB} MB。"
    warn "  那會是**變慢**（用到 swap），不是錯 —— 所以不擋。"
    warn "  註：這個估算裡的 KV cache 一項是 D-022 的**推測值**"
    warn "  （~${KV_KIB_PER_TOKEN_ESTIMATE} KiB/token，與模型的 KV head 數綁定），不是實測。"
    warn "  真的要省，把 --num-ctx 調小。" ;;
  unknown)
    warn "記憶體：讀不到 /proc/meminfo 或估不出需求，跳過檢查（不擋）。" ;;
esac

VCPU="$(nproc 2>/dev/null || true)"
if [[ -n "$VCPU" && "$VCPU" =~ ^[0-9]+$ && "$VCPU" -lt 2 ]]; then
  warn "只有 ${VCPU} 顆 vCPU —— 可以跑，只是慢。不擋（那是慢，不是錯）。"
fi

if [[ "$BLOCKERS" -gt 0 ]]; then
  fail "前置檢查未通過 —— 沒有動到任何檔案或容器。"
  exit 1
fi

# ── 3. 寫 .env（**必須在 load_env 之前**）───────────────
# 每一次寫入都走 env_upsert()：它保證所有生效的重複行收斂成恰好一行。
# `echo >> .env` 會在多跑一次之後留下重複鍵，而重複鍵是**後面那行生效** ——
# 於是「我已經把 WEBUI_BIND_ADDR 改成 127.0.0.1 了」可以與事實相反，無聲。
ENV_CHANGES=()
apply_env() {
  local key="$1" value="$2" new tmp perms
  new="$(env_upsert "$key" "$value" < "$ENV_FILE")"
  if [[ "$new" == "$(cat "$ENV_FILE")" ]]; then
    return 0          # 已經是這個值，連 mtime 都不動
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  （dry-run）會把 $key 設成 $value"
    return 0
  fi
  tmp="$(mktemp "$PROJECT_ROOT/.env.tmp.XXXXXX")"
  perms="$(stat -c '%a' "$ENV_FILE" 2>/dev/null || echo 600)"
  printf '%s\n' "$new" > "$tmp"
  chmod "$perms" "$tmp"
  # 同一個檔案系統內的 rename 是原子性的：不會留下「寫到一半的 .env」。
  mv -f "$tmp" "$ENV_FILE"
  ENV_CHANGES+=("$key=$value")
}

apply_env WEBUI_BIND_ADDR "$BIND_ADDR"
apply_env OLLAMA_MODEL "$MODEL"
apply_env EMBEDDING_MODEL "$EMBED_MODEL"
apply_env OLLAMA_CONTEXT_LENGTH "$NUM_CTX"

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
  echo "    然後跑對外暴露閘門、下載模型、煙霧測試與 num_ctx 確認。"
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
pull_if_missing() {
  local name="$1" want installed line
  want="$(normalize_model "$name")"
  installed=false
  while IFS= read -r line; do
    if [[ -n "$line" && "$(normalize_model "$line")" == "$want" ]]; then
      installed=true; break
    fi
  done <<<"$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  if [[ "$installed" == "true" ]]; then
    ok "模型 $name 已存在，略過下載"
    return 0
  fi
  info "下載 $name ..."
  if ! $COMPOSE exec -T ollama ollama pull "$name"; then
    fail "模型 $name 下載失敗。堆疊本身已起來，可稍後重試：bash scripts/pull-model.sh $name"
    return 1
  fi
  ok "模型 $name 下載完成"
}

pull_if_missing "$MODEL" || exit 2
# 嵌入模型**一定要拉**：mem0 的 OllamaEmbedding 遇到不存在的嵌入模型會
# 自己 pull（見 verify-chroma-dims.sh），那會讓一次「驗證」變成一次數 GB 的
# 下載；而 chroma 的維度一旦改變，既有的向量庫就全部作廢。
pull_if_missing "$EMBED_MODEL" || exit 2

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

# ── 12. 完成 ────────────────────────────────────────────
ok "佈署完成 —— 堆疊已起來、埠只綁 loopback、模型可用、num_ctx 已生效"
echo
"$SCRIPT_DIR/status.sh" || true

cat <<EOF

Open WebUI 位址：
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
