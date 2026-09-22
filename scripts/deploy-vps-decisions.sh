#!/usr/bin/env bash
# scripts/deploy-vps.sh 與 scripts/check-exposure.sh 的**決策邏輯**，
# 抽成一個可以被 source 的純函式檔。
#
# 為什麼要獨立成一個檔案（與 ollama_log_corroboration.sh 同一個理由）：
# 這裡有兩個判斷，錯了都不會有人發現 ——
#
#   1. **綁定位址的預設值。** deploy-vps.sh 全部的安全性都建立在「容器起來
#      之前 .env 就已經寫成 loopback」這件事上。這個函式回錯一個字，整個
#      堆疊就在公網上，而且看起來一切正常。
#   2. **暴露閘門的結束碼判讀。** check-exposure.sh 的 2 是「無法判定」，
#      不是「安全」。把它讀成 pass 就是把「我沒查到」講成「我查過了」。
#
# 兩者都是「形狀正確、結論相反」的錯誤 —— 測試盯的是回傳的那個字（那**就是**
# 可區辨的字串），突變測試則證明測試真的在盯它。
#
# 本檔**不做任何 I/O**：不呼叫 docker、不讀檔、不寫檔、不碰網路。所有輸入
# 都由參數傳入，所有輸出都走 stdout。需要讀 /proc/meminfo 或 `docker port`
# 的部分留在 deploy-vps.sh 裡。
#
# 呼叫端（deploy-vps.sh 與 scripts/test_deploy_vps_decisions.sh）都必須先
# source scripts/lib.sh，本檔要用它的訊息函式。
#
# 另外：突變清單用 `label|原字串|取代字串` 三個欄位以 `|` 切開，所以**被指定
# 為突變目標的行不可以含有 `|`**（`||` 也不行）。下面幾處把 `-z "$a" || -z "$b"`
# 拆成兩行、把 `||` 寫成 `if`，都是為了這個 —— 不是風格偏好。

# ── 結束碼（D-018 的約定，全專案一致）──────────────────────
DEPLOY_PASS=0
DEPLOY_FAIL=1
DEPLOY_INDETERMINATE=2
DEPLOY_BROKEN=3

# ── 門檻與估算值 ──────────────────────────────────────────
# mem0 的抽取 prompt 實測 8,052 與 8,100 個 token（D-027），截斷規則是
# `num_ctx >= prompt tokens + 1`，所以門檻是 8,101。**這個 +1 就是結論本身**
# —— 與 mem0_add_cost_probe.py 的 min_ctx_required() 是同一個數字。
MEM0_ADD_MIN_CTX=8101

# ── 第二個門檻：prompt 要**待完整段生成**，不只是進得去 ──────
#
# 上面那個門檻只管**生成之前**的截斷。ollama 是用 `--context-shift --keep 4`
# 起 llama-server 的，所以生成只要把 context 用滿，llama-server 不會停 ——
# 它會從 prompt **中段**丟掉一整塊（8192 之下是 4,093 個 token）再繼續生成。
# 8192 配 8,052 的 prompt 只剩 140 個生成 token 的餘裕，兩次真實 add() 都
# 撞到了（D-035 第三節）。
#
# 所以真正要滿足的是 `num_ctx > prompt_tokens + num_predict`：
#   8,100（實測到最大的 prompt）＋ 2,000（mem0 的生成上限）＋ 1 ＝ 10,101
#
# **證據強度要說清楚：** 觸發規則（「生成到 num_ctx - prompt_tokens 就 shift」）
# 是從**兩個同一個 num_ctx 的觀測**配出來的，在 16,384 上只驗過一次而且那次
# 是預測 0 次、實測 0 次（D-035 第三節）。**不足以宣稱它對所有 num_ctx 成立。**
# 這裡的 +1 因此是保守取值，不是量出來的邊界。
MEM0_ADD_MAX_PROMPT=8100
MEM0_ADD_NUM_PREDICT=2000
MEM0_ADD_HOLD_CTX=10101

# KV cache 的每 token 估算（KiB）。這是 D-022 的**估算值，本堆疊未實測**：
# 由 Qwen2.5-3B 的架構推得（36 層 × 2 個 KV head × head_dim 128 × f16）。
# 它與模型的架構綁定（KV head 多的模型會是倍數），所以**只能拿來產生一句
# 帶說明文字的警告，永遠不能單獨構成阻擋**。
KV_KIB_PER_TOKEN_ESTIMATE=36

# ─────────────────────────────────────────────────────────
# 純函式
# ─────────────────────────────────────────────────────────

# 沒有明講時，這個綁定位址該用什麼。**這是整個部署最重要的一行。**
#
# Codespaces 上 0.0.0.0 是可接受的（那裡的埠預設為私有、要通過 GitHub 認證）；
# 其他任何機器上都用 loopback，因為那台機器可能就是有公開 IP 的 VPS，而
# 綁 0.0.0.0 會**繞過 Cloudflare Access**（Access 保護的是 tunnel 那條路）。
#
# 只在 .env **沒有**這個鍵時才呼叫它 —— 使用者明講過的值要尊重。
default_bind_addr() {
  if [[ "${1:-false}" == "true" ]]; then
    printf '0.0.0.0'
    return 0
  fi
  printf '127.0.0.1'
}

# 這個值是不是「萬用位址」—— 綁上去就等於發布到每一個介面。
bind_value_is_wildcard() {
  case "${1:-}" in
    0.0.0.0|::|'[::]') printf 'yes' ;;
    *)                  printf 'no' ;;
  esac
}

# .env 裡的值 + 有沒有 --expose + 是不是 Codespaces → 這次要寫進 .env 的
# 綁定位址，以及「是否覆蓋掉了 .env 原本的值」。
#
# **這不是 default_bind_addr() 的包裝，而是取代它作為對外介面。** 規則刻意
# 不是「.env 有值就尊重」，理由有兩層，第二層是關鍵：
#
#  1. `.env.example` 出貨時寫的就是 `WEBUI_BIND_ADDR=0.0.0.0`（給 Codespaces
#     的，在那裡是對的）。所以「讀 .env，沒有才退回範本」一定會拿到
#     0.0.0.0 —— 範本裡的預設被當成使用者的決定。
#  2. 更根本：全新 VPS 上的 `.env` 正是腳本自己 `cp .env.example .env` 產生
#     的。也就是說**連「只讀 .env」都還是會拿到 0.0.0.0** —— 這支腳本剛建立
#     的那個檔案，不能拿來當「使用者明講過」的證據。
#
# 但也**不是無條件覆蓋**：要關的是「萬用位址」這個洞，不是重新詮釋使用者
# 綁在某張網卡上的選擇。127.0.0.1、10.0.0.5 這類值不是 fail-open，一律尊重。
#
# 輸出兩行：第一行是位址，第二行是被覆蓋掉的原值（沒有就空）。
resolve_bind_addr() {
  local env_value="${1:-}" expose="${2:-0}" codespaces="${3:-false}"
  local resolved overridden=""

  if [[ "$expose" == "1" ]]; then
    resolved="0.0.0.0"
  elif [[ -n "$env_value" && "$(bind_value_is_wildcard "$env_value")" == "no" ]]; then
    resolved="$env_value"
  else
    resolved="$(default_bind_addr "$codespaces")"
    # 空字串不算「覆蓋」—— 那只是 .env 沒這個鍵，沒什麼好警告的。
    if [[ -n "$env_value" && "$env_value" != "$resolved" ]]; then
      overridden="$env_value"
    fi
  fi

  printf '%s\n%s\n' "$resolved" "$overridden"
}

# `docker port open-webui` 的輸出裡，3000 是不是綁在萬用位址上。
#
# 這個檢查**不依賴 `ip` 指令、也不依賴判斷這台機器有沒有公開位址** —— 這正是
# 它存在的理由。check-exposure.sh 要靠 `ip -o addr` 才知道機器有沒有公開位址，
# 而它在兩種常見情況下會失敗：讀不到 `ip`（精簡的 VPS image），以及
# AWS/GCP/Azure 那種 NIC 只有私有位址、公開 IP 是 1:1 NAT 在前面（那個情況
# 下 `ip` 讀得到的**永遠**是 10.x）。兩者都會讓它回「只有私有位址，不到
# Internet」然後結束碼 0。綁定本身是可以直接讀的，所以這裡直接讀它。
#
# 空字串是「讀不到」而不是「沒綁在萬用位址」—— 回 unknown，呼叫端必須自己
# 決定 unknown 要怎麼處置（見 deploy-vps.sh：未知一律當成不安全）。
bind_is_wildcard() {
  local text="$1"
  if [[ -z "$text" ]]; then printf 'unknown'; return 0; fi
  if grep -qE '\-> (0\.0\.0\.0|\[::\]|::):' <<<"$text"; then printf 'yes'; return 0; fi
  printf 'no'
}

# 這台機器的 DMI 字串看起來像不像「NIC 只有私有位址、公開 IP 是 1:1 NAT
# 在前面」的環境。回環境名稱，不像就回空字串。
#
# 為什麼需要：check-exposure.sh 的結論建立在「有沒有讀到公開位址」上，而在
# AWS／GCP／Azure／Oracle 上，`ip -o addr` **永遠**只會讀到私有位址 ——
# 公開 IP 是 NAT 在前面的，不在 NIC 上。於是那個檢查會說「只有私有位址，
# 不到 Internet」然後結束碼 0，而機器其實就在公網上。這是 fail-open：
# 它把「我讀不到」講成「沒有」。
#
# Microsoft 那一條要**兩個欄位都對**才命中：Azure 與 WSL2 的 product_name
# 都是 "Virtual Machine"，而 Surface 筆電的 sys_vendor 也是 "Microsoft
# Corporation"。只比對廠商的話，一台跑 Ubuntu 的 Surface 會被誤判成雲端
# 主機 —— 那正是 D-016 說的假失敗。
#
# WSL2 也會命中，而那是**對的**：那裡的 NIC 同樣是私有位址、前面還有
# Windows 主機。所以命中的意思不是「你在 Azure」，而是
# **「這一台的可見位址推論不出對外可達性」**。
#
# 讀 /sys 是 I/O，所以留在呼叫端；這裡只做字串判斷。
nat_fronted_environment() {
  local vendor="${1:-}" product="${2:-}"
  local haystack
  haystack="$(printf '%s %s' "$vendor" "$product")"
  case "$haystack" in
    *"Amazon EC2"*)            printf 'AWS EC2'; return 0 ;;
    *"Google Compute Engine"*) printf 'Google Cloud'; return 0 ;;
    *"Microsoft Corporation"*"Virtual Machine"*) printf 'Azure 或 WSL2'; return 0 ;;
    *Oracle*)                  printf 'Oracle Cloud'; return 0 ;;
  esac
  printf ''
}

# 埠綁在萬用位址時，**這台機器**到底算不算對外開放。這是 check-exposure.sh
# 的第二道檢查全部的內容，抽出來是因為它的每一條分支的結論都不一樣，而
# 其中兩條（unknown、nat_fronted）是「我無法判定」—— 那與「安全」不同。
#
# 判定順序就是規格，而且有兩個地方不能動：
#
#   1. **Codespaces 最先。** Codespaces 跑在 Azure 上，所以 nat_env 也會
#      命中；順序顛倒的話，一個本來正確的 Codespaces 部署會變成「無法判定」。
#   2. **lan_only 仍然回 lan_only。** 那是「只有私有位址、而且我讀得到
#      `ip`、而且不是 1:1 NAT 的環境」—— 在這個組合下「區網可達、不到
#      Internet」是**推論得到的**，不是猜的。把它的極性翻成「無法判定」
#      會讓每一台在 NAT 後的家庭網路機器都變成失敗（D-016），而那個
#      失敗會讓人開始忽略這支腳本。
#
# 回傳：codespace / exposed / unknown / nat_fronted / lan_only
wide_bind_verdict() {
  local is_codespace="${1:-false}" has_public="${2:-false}"
  local ip_available="${3:-true}" nat_env="${4:-}"
  if [[ "$is_codespace" == "true" ]]; then printf 'codespace'; return 0; fi
  if [[ "$has_public" == "true" ]]; then printf 'exposed'; return 0; fi
  if [[ "$ip_available" != "true" ]]; then printf 'unknown'; return 0; fi
  if [[ -n "$nat_env" ]]; then printf 'nat_fronted'; return 0; fi
  printf 'lan_only'
}

# check-exposure.sh 的結束碼要怎麼讀。mode 是政策，不是事實：
#   strict  —— deploy-vps.sh 用（全新安裝，「無法驗證」不可以當成「安全」）
#   lenient —— up.sh 用（日常入口與 Codespaces 自動啟動，「無法判定」升級成
#              失敗會弄壞一個本來可用的 workflow，見 D-016）
#
# 注意 1 與 2 的處置**不同**，而 0 與 1 也不同。這個函式全部的內容就是那張
# 對照表，一行都不能少。
exposure_gate_verdict() {
  local rc="$1" mode="${2:-strict}"
  if [[ "$rc" == "0" ]]; then printf 'pass'; return 0; fi
  if [[ "$rc" == "1" ]]; then printf 'block'; return 0; fi
  if [[ "$rc" != "2" ]]; then printf 'broken'; return 0; fi
  if [[ "$mode" == "lenient" ]]; then printf 'warn'; return 0; fi
  printf 'indeterminate'
}

# 「全新安裝」問的是**有沒有人類的資料**，不是「volume 存不存在」。
#
# 為什麼要這樣分：若用 volume 或「模型已下載」當判準，那麼一次在煙霧測試
# 失敗的部署會讓**腳本自己不能再跑第二次** —— 而「跑到一半失敗」正是部署最
# 常見的狀態。模型與 volume 都是可以重建的產物；accounts 與 chats 不是。
#
# keep=1 時即使是 occupied 也回 kept（呼叫端用 --keep-data 表示要繼續）。
fresh_install_verdict() {
  local users="${1:-0}" chats="${2:-0}" keep="${3:-0}"
  local has_data=0
  if [[ "$users" -gt 0 ]]; then has_data=1; fi
  if [[ "$chats" -gt 0 ]]; then has_data=1; fi
  if [[ "$has_data" -eq 1 ]]; then
    if [[ "$keep" -eq 1 ]]; then printf 'kept'; return 0; fi
    printf 'occupied'
    return 0
  fi
  printf 'fresh'
}

# 磁碟是唯一會**硬擋**的資源，理由不是嚴重程度而是失敗的形狀：磁碟不足不會
# 優雅地變慢，它會在 pull 到一半的時候中止（README 已經記了「需要約 2 倍
# 空間，否則 pull 會失敗，有時是無聲的」）。CPU 與記憶體不足是「慢」，磁碟
# 不足是「壞在半路」。全 repo 目前**沒有任何磁碟檢查**，這是真的缺口。
#
# 讀不到就回 unknown，而 unknown 不擋（呼叫端負責把它印出來）—— 無知不是
# 缺陷，把無知當成缺陷會製造出假失敗（D-016）。
disk_verdict() {
  local free_gb="$1" need_gb="$2"
  if [[ ! "$free_gb" =~ ^[0-9]+$ ]]; then printf 'unknown'; return 0; fi
  if [[ ! "$need_gb" =~ ^[0-9]+$ ]]; then printf 'unknown'; return 0; fi
  if (( free_gb < need_gb )); then printf 'insufficient'; return 0; fi
  printf 'sufficient'
}

# 記憶體與 CPU 都**只警告**。三個既有的探針都做了一樣的選擇，理由寫在
# verify-langgraph-tools.sh：那是**慢**，不是**錯**。分辨不出「會 OOM」與
# 「會用到 swap 所以很慢」的時候，把後者講成失敗就是 D-016 說的假失敗。
ram_verdict() {
  local avail_mb="$1" need_mb="$2"
  if [[ ! "$avail_mb" =~ ^[0-9]+$ ]]; then printf 'unknown'; return 0; fi
  if [[ ! "$need_mb" =~ ^[0-9]+$ ]]; then printf 'unknown'; return 0; fi
  if (( avail_mb < need_mb )); then printf 'tight'; return 0; fi
  printf 'ok'
}

# 模型大小（GB）。**只查表，不從 tag 猜。**
#
# 表裡五個數字來自 .env.example 既有的大小註解（0.6b=523MB、1.7b=1.4GB、
# 4b=2.5GB、8b=5.2GB、14b=9.3GB），不是這裡新發明的。猜不出來就回 unknown，
# 而 unknown 只會產生警告、不會阻擋 —— 「我不知道這個模型多大」推論不出
# 「這個模型放不下」。
model_gb_estimate() {
  local m="${1:-}"
  case "$m" in
    qwen3:0.6b) printf '0.5'; return 0 ;;
    qwen3:1.7b) printf '1.4'; return 0 ;;
    qwen3:4b)   printf '2.5'; return 0 ;;
    qwen3:8b)   printf '5.2'; return 0 ;;
    qwen3:14b)  printf '9.3'; return 0 ;;
  esac
  printf 'unknown'
}

# 需要多少磁碟：2 × 模型 + 4GB。2 倍是 README 既有的記載（pull 會同時佔用
# 下載暫存與展開後的本體），4GB 給三個 image（ollama、open-webui、本地建的
# mcp-test-server）與系統餘裕。讀不到模型大小就回空字串，呼叫端據此走 unknown。
disk_need_gb() {
  local gb="$1"
  if [[ ! "$gb" =~ ^[0-9]+([.][0-9]+)?$ ]]; then printf ''; return 0; fi
  awk -v g="$gb" 'BEGIN { printf "%d", 2 * g + 4 }'
}

# 建議的記憶體下限（MB）：模型本體 + KV cache 估算 + 2GB 的 Open WebUI 與系統。
# KV 那一項是估算值（見檔頭的說明），所以這個數字**只能拿來警告**。
ram_warn_mb() {
  local gb="$1" num_ctx="${2:-0}"
  if [[ ! "$gb" =~ ^[0-9]+([.][0-9]+)?$ ]]; then printf ''; return 0; fi
  awk -v g="$gb" -v c="$num_ctx" -v kv="$KV_KIB_PER_TOKEN_ESTIMATE" \
    'BEGIN { printf "%d", g * 1024 + (c * kv) / 1024 + 2048 }'
}

# 這個 num_ctx 夠不夠讓 mem0 的抽取 prompt 完整進去。門檻的 +1 是結論本身
# （見上面 MEM0_ADD_MIN_CTX 的說明）。
ctx_meets_mem0() {
  local n="${1:-0}"
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then printf 'no'; return 0; fi
  if (( n >= MEM0_ADD_MIN_CTX )); then printf 'yes'; return 0; fi
  printf 'no'
}

# 這個 num_ctx 夠不夠讓 prompt **整段生成期間**都保持完整（第二個門檻，
# 見上面 MEM0_ADD_HOLD_CTX 的說明）。
#
# **與 ctx_meets_mem0 是兩個獨立的問題，不可以只用這一個取代那一個。**
# 8192 就是「過第一個、不過第二個」的那個值 —— 而它正是這個腳本原本的預設。
ctx_holds_through_generation() {
  local n="${1:-0}"
  if [[ ! "$n" =~ ^[0-9]+$ ]]; then printf 'no'; return 0; fi
  if (( n >= MEM0_ADD_HOLD_CTX )); then printf 'yes'; return 0; fi
  printf 'no'
}

# --num-ctx 的參數驗證。呼叫端把非 `ok` 一律對到結束碼 3（參數錯誤是「腳本
# 自己壞掉」，不是「環境無法判定」）—— 與 verify-mem0-add-cost.sh 一致。
num_ctx_verdict() {
  local raw="${1:-}"
  if [[ ! "$raw" =~ ^[0-9]+$ ]]; then printf 'not_integer'; return 0; fi
  if (( raw < 512 )); then printf 'too_small'; return 0; fi
  if (( raw > 1048576 )); then printf 'too_large'; return 0; fi
  printf 'ok %s' "$raw"
}

# 把 .env 裡的 key 設成 value，其餘原封不動。stdin 進、stdout 出。
#
# **為什麼不用 `echo >> .env`**：那樣子重跑一次就會多一行，而 .env 的重複鍵
# 是後面那行生效 —— 於是「我已經把 WEBUI_BIND_ADDR 改成 127.0.0.1 了」可以
# 與事實相反，而且是無聲的。這個函式保證：所有生效的重複行收斂成**恰好一行**，
# 位置在第一個出現的地方；註解掉的行（`# KEY=...`）不算生效、原樣保留；
# 前綴相同的鍵（`WEBUI_BIND_ADDR_EXTRA`）不會被誤配；值裡面有 `=` 也完整保留。
#
# 冪等是必要條件，不是加分項：deploy-vps.sh 會在失敗後被重跑，而它改的是
# 使用者唯一的設定檔。
env_upsert() {
  local key="$1" value="$2"
  local line found=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$key="* ]]; then
      if [[ "$found" -eq 0 ]]; then printf '%s=%s\n' "$key" "$value"; fi
      found=1
      continue
    fi
    printf '%s\n' "$line"
  done
  if [[ "$found" -eq 0 ]]; then printf '%s=%s\n' "$key" "$value"; return 0; fi
}
