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

# ── 磁碟閘門的兩個常數（D-058）───────────────────────────
#
# 舊版把四件事混進一個 `2 * g + 4`，所以每一個數字都沒有名字、也沒有地方
# 單獨改它。拆開之後本來有三個常數；第三個（下載期間的峰值倍數）在提交 2
# **被量測刪掉了** —— 見下面那一段。剩下的兩個就是那個公式僅有的魔數。
#
# `DISK_SYSTEM_MARGIN_GB` —— 第 5 步起的容器、volume 與系統本身。**這仍然
# 是斷言值，不是量測值**（D-022 的 KV cache 同一個處置：沒量過的數字要自己
# 講）。它吸收的是 `df` 的整數截斷（每個數字最多少算 1 GB，四個數字就是
# 4 GB 的最壞情況，這裡刻意不假裝算得準）。
#
# 提交 2 順手量到同一個量級、但那**不足以**把它變成量測值：靜止態的容器
# 可寫層合計 66 MB、open-webui 的 volume 2.34 GB。協定 C（真佈署期間的
# 增量）沒有跑，所以它維持斷言值。
DISK_SYSTEM_MARGIN_GB=2

# `DISK_IMAGE_GB` —— compose 要拉的映像，**有缺的時候**才需要為它們留的空間。
#
# 舊版的「4GB 給三個 image」實測是錯的：ollama/ollama:latest 9.19 GB ＋
# ghcr.io/open-webui/open-webui:main 7.16 GB ＝ 16.35 GB，差了約 4 倍。
#
# **提交 1 先把結構改成有條件的**（`image_need_gb`：都在本機就回 0），因為
# 直接改大而沒有那個條件，會讓一次「映像都在本機」的重跑被要求 17 GB ——
# 比原本的誤擋更糟。條件落地之後，把數字改對就沒有那個副作用了。
#
# **但 17 是「最終佔用」，不是「拉取期間的峰值」** —— 後者沒有量到，而且
# 這裡不假裝它是：量測當時這台機器只有 12.2 GB 可用（放不下 16.35 GB），
# 唯一的替代方案（`down.sh` ＋ `rmi` 再重拉）會拆掉正式堆疊，沒有明講
# 核准不做。docker 是逐層拉、解完一層才拉下一層，所以真正的峰值會是
# 16.35 ＋ 最大那一層的壓縮檔，**比 17 大**：這是一個已知偏低的下界。
DISK_IMAGE_GB=17

# `DISK_PULL_PEAK_MULTIPLIER` —— **已經沒有這個常數了。這是它被刪掉的理由。**
#
# 它原本是 2，來源是 README 的一列表格：沒有引註、沒有 D-0xx，而本檔又反過來
# 說「2 倍是 README 既有的記載」—— 自我引用的閉環（D-058 §六）。
#
# 2026-09-26 用 `scripts/measure-pull-peak.sh` 真的拉了一次 `qwen3:8b`
# （最終 5,225,422,848 B），在**獨立的 volume ＋ 第二個 ollama 容器**上
# 每秒採樣一次，1184 筆樣本、1183 秒。兩個獨立的估計法都給 1.000：
#
#   由可用空間算（D-058 §七 事先指定的決策規則）：
#     5,227,950,080 / 5,225,422,848 = 1.000
#   由 volume 的實際佔用算（交叉檢查，不受其他寫入者污染）：
#     5,225,422,848 / 5,225,422,848 = 1.000
#   兩者的差 2,527,232 B 就是機器上其他寫入者（生產堆疊的 log）的污染量。
#
# 也就是說：**ollama 拉一個模型，除了成品本身之外不需要任何額外空間。**
# 「壓縮檔與解壓檔同時佔用」在 ollama 的 registry 上不成立 —— blob 就是成品，
# 而且是**稀疏地預先配置**的。這也是為什麼佔用量必須用 `st_blocks * 512` 量：
# 用 `st_size` 會量到一個「峰值永遠等於最終」的假答案（D-058 §六）。
#
# 依 D-058 §七 **事先寫好、在看到數據之前就寫好**的規則：倍數 ≤1.05 → 刪掉
# 常數、模型項直接寫 `g`。證據：
# docs/evidence/2026-09-26-pull-peak-measurement.txt
#
# **一個沒有作用的 1.0 倍與那則沒有引註的 2 倍是同一種罪**：前者讓人以為這裡
# 有量過的東西，後者讓人以為這裡有查證過的東西。

# `--model-gb` 的上限。用途不是「模型不可能這麼大」，而是**接住單位錯誤**：
# `qwen3:8b` 是 5,200 MB，把 MB 當 GB 填進來就是 5200。1024 給合法的最大
# 模型（llama3.1:405b 約 231 GB）留了四倍餘裕，同時擋掉那個錯誤。
MODEL_GB_MAX=1024

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

# 一個綁定位址屬於哪一類。回 loopback / wildcard / private / public / unknown。
#
# 為什麼要分類，而不是只問「是不是 0.0.0.0」：check-exposure.sh 原本只認得萬用
# 位址，其餘一律印「僅綁在 loopback」。那個推論有兩個錯，第二個是安全問題：
#
#   · 綁在特定網卡上（例：192.168.44.128）時，那句話**與事實相反** ——
#     同一層網路的裝置直接連得到，不經過 tunnel，也不經過 Cloudflare Access。
#   · 綁在**公開位址**上時，那支腳本同樣會回「未發現對外暴露」，而機器其實
#     對 Internet 開著。那是安全檢查的 fail-open —— 正是這支腳本檔頭說要
#     消滅的那個形狀（把「我沒檢查」講成「沒問題」）。
#
# 方向刻意 fail-closed：認不出來的一律回 unknown（呼叫端當成無法判定），
# 長得像 IP 卻不屬於任何私有段的回 public。把公開位址誤判成私有才是要防的洞，
# 反過來只是多一次警告。
bind_addr_scope() {
  local addr="${1:-}"
  # `docker port` 把 IPv6 寫成 [::1]:3000 這種形式，所以先脫掉方括號再分類。
  # 不脫的話 `[fd00::1]` 會因為開頭是 `[` 而落到「看起來像 IP」那條，被當成
  # 公開位址 —— 方向是安全的，但結論是錯的，而錯的結論會讓人開始不信這支腳本。
  addr="${addr#\[}"
  addr="${addr%\]}"
  # 每個樣式**各佔一行**，不是排版偏好：突變測試要求目標字串裡不能有 `|`
  # （那個字元是欄位分隔符），而 case 的樣式用 `|` 串接。寫成一行的話
  # `10.*|192.168.*` 這種字串就沒辦法被突變瞄準 —— 也就是這一格會沒有守衛。
  case "$addr" in
    '')                        printf 'unknown' ;;
    '0.0.0.0')                 printf 'wildcard' ;;
    '::')                      printf 'wildcard' ;;
    '::1')                     printf 'loopback' ;;
    127.*)                     printf 'loopback' ;;

    # RFC1918 與 link-local。
    10.*)                      printf 'private' ;;
    192.168.*)                 printf 'private' ;;
    169.254.*)                 printf 'private' ;;
    172.1[6-9].*)              printf 'private' ;;
    172.2[0-9].*)              printf 'private' ;;
    172.3[01].*)               printf 'private' ;;

    # CGNAT（100.64.0.0/10）—— 電信級的私有段，從外面連不到，與 RFC1918 同類。
    100.6[4-9].*)              printf 'private' ;;
    100.[7-9][0-9].*)          printf 'private' ;;
    100.1[01][0-9].*)          printf 'private' ;;
    100.12[0-7].*)             printf 'private' ;;

    # IPv6：ULA（fc00::/7）與 link-local（fe80::/10）。
    fc*)                       printf 'private' ;;
    fd*)                       printf 'private' ;;
    fe[89ab]*)                 printf 'private' ;;

    # 看起來是 IP 卻不屬於上面任何一段 —— 當成公開位址（fail-closed）。
    *.*.*.*)                   printf 'public' ;;
    *:*)                       printf 'public' ;;

    *)                         printf 'unknown' ;;
  esac
}

# `docker port` 的輸出裡，這個埠整體綁在哪一類位址上。多條綁定取**最寬**的
# 那一條：只要有一條落在公開位址或萬用位址，其餘是 loopback 也救不回來。
#
# 空字串、以及「解析不出任何位址」都回 unknown。這兩者與 loopback 在呼叫端
# 是不同的處置（前者是無法判定）—— 把它們混進 loopback 就是原本那個 fail-open。
port_bind_scope() {
  local text="$1" host scope worst='loopback' found=0
  # 寫成 `||` 而不是 `if`：**這一行的字串必須與 bind_is_wildcard 的那一行不同**。
  # 突變測試要求目標字串在檔案裡唯一，而兩處一樣的話，想瞄準 bind_is_wildcard
  # 的那條突變會變成 NOT_UNIQUE 而失效 —— 一個守衛會靜靜地消失，
  # 症狀是「突變沒被抓到」，看起來像測試有洞，其實是原始碼撞字串。
  # （順帶：`||` 形式在 $text 非空時回 0，不會踩到 set -e。）
  [[ -n "$text" ]] || { printf 'unknown'; return 0; }
  if [[ "$(bind_is_wildcard "$text")" == "yes" ]]; then printf 'wildcard'; return 0; fi
  while IFS= read -r host; do
    if [[ -z "$host" ]]; then continue; fi
    found=1
    scope="$(bind_addr_scope "$host")"
    case "$scope" in
      public)  worst='public' ;;
      unknown) if [[ "$worst" != 'public' ]]; then worst='unknown'; fi ;;
      private) if [[ "$worst" == 'loopback' ]]; then worst='private'; fi ;;
    esac
  done < <(sed -n 's/.*-> \(.*\):[0-9]\{1,\}$/\1/p' <<<"$text")
  if [[ $found -eq 0 ]]; then printf 'unknown'; return 0; fi
  printf '%s' "$worst"
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

# 記憶體那一行要印什麼。**回 arm 的名字，不回數字。**
#
# 存在理由是把 `ram_verdict` 的 `unknown` **拆成兩個**，因為那兩個原因的處置
# 完全不同：「讀不到 /proc/meminfo」是環境問題，「估不出需求」是缺輸入
# （補 `--model-gb` 就有）。原本兩者共用一句「讀不到 /proc/meminfo 或估不出
# 需求」，讀的人無從下手 —— 一句話裡有兩個不同的處方，等於沒有處方。
#
# **為什麼是一個函式，而不是在兩個呼叫點各判一次**：主機記憶體
# （`RAM_VERDICT`）與 VRAM（`VRAM_VERDICT`）問的是**同一個問題**，
# 而兩邊原本各寫各的判斷 —— 於是 VRAM 那一邊的 `else` 把「估不出需求」的
# 空字串收成了綠色 OK，印出「VRAM：24576 MB（估算需要約  MB）」。
# （這裡刻意**不寫行號**：行號會走 —— D-061 就抓到一個壞掉的引註。
# 認這兩個變數名，它們是那個 case 的錨點。）
# **判斷寫兩次，就會有一邊寫錯**；寫在這裡，它就有測試與突變守著（#93）。
#
# 兩個 unknown 的優先序是刻意的：avail 讀不到比 need 估不出來更根本 ——
# 前者讓整個檢查失效，後者只是少了輸入。兩個都不知道時報前者。
memory_verdict() {
  local avail_mb="${1:-}" need_mb="${2:-}"
  local verdict
  verdict="$(ram_verdict "$avail_mb" "$need_mb")"
  if [[ "$verdict" != "unknown" ]]; then printf '%s' "$verdict"; return 0; fi
  if [[ ! "$avail_mb" =~ ^[0-9]+$ ]]; then printf 'unknown_avail'; return 0; fi
  printf 'unknown_need'
}

# 一個「GB 的數字」長什麼樣。**這一條判斷只寫在這裡。**
#
# 三個地方需要它：`--model-gb` 的旗標驗證、`model_gb_estimate` 的查表輸出、
# 以及 `disk_need_gb` 的輸入檢查。寫三次的話，改了一處就會出現「旗標收了
# 一個值、閘門卻說它讀不懂」這種分歧 —— 而且症狀是「閘門靜默地跳過檢查」。
is_gb_number() {
  local v="${1:-}"
  if [[ "$v" =~ ^[0-9]+([.][0-9]+)?$ ]]; then printf 'yes'; return 0; fi
  printf 'no'
}

# 模型大小（GB）。**只查表，不從 tag 猜。**
#
# 表裡六個數字全部來自 `.env.example` 既有的大小註解，不是這裡新發明的：
# 對話模型那五格在「模型」區的尺寸清單（0.6b=523MB、1.7b=1.4GB、4b=2.5GB、
# 8b=5.2GB、14b=9.3GB），嵌入模型那一格在 `EMBEDDING_MODEL` 上方（639MB）。
# 猜不出來就回 unknown。
#
# **`qwen3-embedding:0.6b` 那一格是 D-058 加的**：嵌入模型由 deploy-vps.sh
# **無條件下載**（:767），所以缺這一格的話，一次全新佈署在下載前的閘門會
# 每次都印「不知道」—— 一個永遠亮著的警告與沒有警告是一樣的（D-056）。
model_gb_estimate() {
  local m="${1:-}"
  case "$m" in
    qwen3:0.6b)           printf '0.5'; return 0 ;;
    qwen3:1.7b)           printf '1.4'; return 0 ;;
    qwen3:4b)             printf '2.5'; return 0 ;;
    qwen3:8b)             printf '5.2'; return 0 ;;
    qwen3:14b)            printf '9.3'; return 0 ;;
    qwen3-embedding:0.6b) printf '0.6'; return 0 ;;
  esac
  printf 'unknown'
}

# 這次要用哪個模型大小：**使用者明講的 > 查表**。
#
# 優先序只寫在這裡。磁碟閘門與記憶體警告都經過它，所以兩邊不可能算出不同
# 的模型大小 —— 那種分歧的症狀是「同一次佈署裡磁碟說夠、記憶體說不夠」，
# 而兩個數字都自稱是模型大小（D-054）。
#
# 空字串的 override 是**合法的「沒明講」**，不是錯誤：旗標沒給就是空的。
model_gb_resolve() {
  local override="${1:-}" model="${2:-}"
  if [[ -n "$override" ]]; then printf '%s' "$override"; return 0; fi
  model_gb_estimate "$model"
}

# `--model-gb` 的參數驗證。比照 num_ctx_verdict：非 `ok` 一律對到結束碼 3。
#
# **`0` 要擋，而且理由是閘門的方向。** 不給旗標時模型大小是 `unknown`，
# 而 unknown 會讓閘門印出「不知道」。填一個 0 進去則會讓它算出「需要 0 GB」
# —— 比什麼都不填**更寬鬆**。把 fail-open 做成一個可選的選項，就是給人一個
# 關掉閘門卻以為自己設定了它的方法。
model_gb_verdict() {
  local raw="${1:-}"
  if [[ "$(is_gb_number "$raw")" != "yes" ]]; then printf 'not_number'; return 0; fi
  if ! awk -v v="$raw" 'BEGIN { exit !(v > 0) }'; then printf 'not_positive'; return 0; fi
  if ! awk -v v="$raw" -v m="$MODEL_GB_MAX" 'BEGIN { exit !(v <= m) }'; then
    printf 'too_large'
    return 0
  fi
  printf 'ok %s' "$raw"
}

# 本機的映像清單裡有沒有這一個。三態，而**中間那一態是它存在的理由**：
#
#   yes      清單裡有
#   no       清單讀得到，確實沒有
#   unknown  **清單讀不到**（docker 不在、指令失敗、回空）。讀不到 ≠ 不在 ——
#            把它讀成 `no` 會讓閘門對著一台映像檔都在本機的機器硬擋，
#            而那是 D-056 的 size_vram 同一課：會誤報的守衛比沒有守衛更糟。
#
# 比對**只認全等**，不做子串。`ollama/ollama-x:latest` 不命中
# `ollama/ollama:latest` —— `nvidia` 與 `nvidia-experimental` 已經教過一次
# （見 gpu_runtime_registered）。compose 裡三個 image 都帶明確的 tag
# （`docker-compose.yml` 的 `:latest`／`:main`），所以全等比對是對的；
# 若哪天有人寫成不帶 tag 的 `image: ollama/ollama`，這裡會說它「不在」
# —— 方向是保守的（多要空間、可能誤擋），不是放行。
#
# **這裡刻意不檢查 `want` 是不是空的。** 「沒有東西要找」那一態屬於呼叫端
# （image_missing_list 已經處理，那裡也有測試），寫在這裡會與
# image_arch_listed 的同名檢查**逐字重複** —— 而逐字重複的行會讓突變台的
# 目標字串變成 NOT_UNIQUE，那個守衛就靜默地消失了（#83 那六條「植入失敗」
# 就是這個形狀，修法是去重，不是改字串）。
image_in_list() {
  local list="${1:-}" want="${2:-}" line
  if [[ -z "$list" ]]; then printf 'unknown'; return 0; fi
  while IFS= read -r line; do
    if [[ "$line" == "$want" ]]; then printf 'yes'; return 0; fi
  done <<<"$list"
  printf 'no'
}

# 清單裡**不在本機**的那些，一行一個。全部都在就什麼都不印。
#
# 讀不到本機清單時印 `unknown` —— 呼叫端據此走保守值（見 image_need_gb），
# 而不是走「都在」。
image_missing_list() {
  local local_list="${1:-}" wanted="${2:-}" img state missing=""
  if [[ -z "$wanted" ]]; then printf 'unknown'; return 0; fi
  if [[ -z "$local_list" ]]; then printf 'unknown'; return 0; fi
  while IFS= read -r img; do
    if [[ -z "$img" ]]; then continue; fi
    state="$(image_in_list "$local_list" "$img")"
    if [[ "$state" != "yes" ]]; then missing+="${img}"$'\n'; fi
  done <<<"$wanted"
  printf '%s' "$missing"
}

# 這些映像要吃掉多少磁碟。**問機器，不是猜。**
#
# 全部都在本機 → `0`：compose 的 pull_policy 預設是 `missing`，本機有的映像
# 不會被重拉，所以一次重跑真的不需要為它們留空間。這是 D-058 修掉的那半個
# 誤擋。
#
# 有缺、**或讀不到清單** → 常數。讀不到時走保守值而不是 0：那是「不知道」，
# 而不知道不可以變成「不需要」。
image_need_gb() {
  local missing
  missing="$(image_missing_list "${1:-}" "${2:-}")"
  if [[ -z "$missing" ]]; then printf '0'; return 0; fi
  printf '%s' "$DISK_IMAGE_GB"
}

# 無條件需要的磁碟：映像 ＋ 系統餘裕。**第 2 步只看這個數字。**
#
# 「無條件」是這整個設計的關鍵字：第 2 步在 ollama 容器存在之前跑，所以它
# **問不到**模型在不在（那是第 8 步的事）。它問得到的是映像 —— `docker images`
# 是本地查詢，不需要容器、不需要網路。兩個問題在兩個不同的時間點才可回答，
# 這就是閘門分成兩段的原因，不是偏好。
disk_unconditional_need_gb() {
  local img_gb="${1:-}"
  if [[ "$(is_gb_number "$img_gb")" != "yes" ]]; then printf ''; return 0; fi
  printf '%d' "$(( img_gb + DISK_SYSTEM_MARGIN_GB ))"
}

# 這次下載一個模型要多少磁碟：模型 ＋ 基準。
#
# `will_download` 是**這次會不會真的下載**。只有字面上的 `no` 會少算 ——
# 那代表模型已經在本機（`df` 本來就沒算已在磁碟上的 blob，所以模型那一項
# 是 0，只剩基準）。其餘一律（**包括空字串**）當成「會下載」：預設必須落在
# 會擋的那一邊，因為把「不知道」算成「不需要」正是 D-055 §3(1) 那個 bug。
#
# 舊版的 `2 × 模型 + 4` 把四件事混在一起，而且**其中兩件是錯的**：`2×` 是
# 一條沒有引註的猜測，提交 2 量到真值是 **1.000**，所以那個常數已經被刪掉
# （見檔案上方的常數區）；`4` 對三個 image 少了約 4 倍（見 DISK_IMAGE_GB）。
# 拆開之後每個數字都有自己的名字與自己的來源。
disk_need_gb() {
  # `base_gb` 用 `${3:-}` 而不是 `${3:-0}`：**明確傳進來的空字串不是 0。**
  # `${3:-0}` 會把「呼叫端說它不知道基準」收斂成「基準是 0」，於是閘門算出
  # 一個太小的需求、說 sufficient —— 那就是 fail-open，而且無聲。
  # 空字串會落到下面的檢查、回空、讓呼叫端走 unknown（印「不知道」）。
  # （這一條是測試抓到的：`disk_need_gb 2.5 yes ''` 原本回 5。）
  local gb="$1" will_download="${2:-}" base_gb="${3:-}"
  if [[ "$(is_gb_number "$gb")" != "yes" ]]; then printf ''; return 0; fi
  if [[ ! "$base_gb" =~ ^[0-9]+$ ]]; then printf ''; return 0; fi
  if [[ "$will_download" == "no" ]]; then printf '%d' "$base_gb"; return 0; fi
  # **這裡沒有倍數。** 提交 2 量到 ollama 下載期間的瞬時峰值**等於**最終大小
  # （見檔案上方常數區那一段），所以模型那一項就是 `g` 本身，不是 `m * g`。
  #
  # `%d` 會把小數截掉（`5.2` 算成 `5`），所以這一項最多**低估** 1 GB。
  # 說清楚方向：低估需求是偏**寬鬆**的那一邊，不是安全的那一邊 —— 它與
  # 「unknown 不擋」是同一類的取捨，只是這裡有明確的上界（< 1 GB/項）。
  # 由 `DISK_SYSTEM_MARGIN_GB` 吸收。
  #
  # 刻意不進位：`5.2` 進位成 `6` 會是另一個沒有量測根據的數字。這裡選擇
  # 「留一個有上界、講得出來的誤差」，而不是「換一個聽起來比較保守的猜測」。
  awk -v g="$gb" -v b="$base_gb" 'BEGIN { printf "%d", g + b }'
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

# docker info 有沒有註冊 nvidia runtime。輸入是
# `docker info --format '{{json .Runtimes}}'` 的輸出，例如
#   {"io.containerd.runc.v2":{},"nvidia":{},"runc":{}}
#
# **比對的是 `"nvidia":` 這個完整的鍵，不是子串 `nvidia`。** 差別不是潔癖：
# 一個叫 `nvidia-experimental` 或 `my-nvidia` 的 runtime 若被當成同一件事，
# 我們就會把裝置保留區掛上去，然後 compose 用一個不存在的 runtime 起容器 ——
# 「形狀正確、結論相反」，正是這個檔案存在的原因（見檔頭）。
gpu_runtime_registered() {
  local text="${1:-}"
  if [[ "$text" == *'"nvidia":'* ]]; then printf 'yes'; return 0; fi
  printf 'no'
}

# GPU 的三態判定。**這是這次改動的核心**：它決定要不要把
# docker-compose.gpu.yml 掛上去，而掛與不掛的差別就是「真的用 GPU」與
# 「以為用了 GPU」—— 後者已經發生過兩次（endpoint boot 沒加 -g、
# .env 的 KEEP_ALIVE=-1），D-055 §二 記著這個堆疊會是第三次。
#
#   hw   有沒有 NVIDIA 裝置（yes/no）
#   rt   docker 有沒有註冊 nvidia runtime（yes/no，來自 gpu_runtime_registered）
#   mode 使用者要不要（auto/on/off，來自 .env 的 OLLAMA_GPU）
#
# `blocked` 只發生在兩種情況，而兩者都是「有人該被擋下來」：
#
#   1. mode=on 卻拿不到 GPU —— 使用者明確要了。給不出來的時候安靜地跑 CPU，
#      就是這一整個家族在防的那件事，所以不能只警告。
#   2. mode=auto 且偵測到硬體但 runtime 沒註冊 —— 有 GPU 卻跑 CPU。這台機器
#      上 compose 起得來、模型載得進、每個請求都答得出來，**只有速度是錯的**，
#      所以它沒有任何症狀。把症狀製造出來就是這個函式的用途。
#
# mode=off 一律 cpu：使用者明講了，沒有東西要擋（包括 runtime 壞掉的情況 ——
# 那是他自己選的路）。
gpu_verdict() {
  local hw="${1:-no}" rt="${2:-no}" mode="${3:-auto}"
  if [[ "$mode" == "off" ]]; then printf 'cpu'; return 0; fi
  if [[ "$hw" == "yes" ]]; then
    if [[ "$rt" == "yes" ]]; then printf 'gpu'; return 0; fi
    printf 'blocked'; return 0
  fi
  if [[ "$mode" == "on" ]]; then printf 'blocked'; return 0; fi
  printf 'cpu'
}

# 把 .env 裡的 key 整行移除，其餘原封不動。stdin 進、stdout 出。
#
# **為什麼需要它，而不是寫成 `KEY=` 或 `KEY=<預設值>`**：COMPOSE_FILE 這個鍵
# 一旦存在（就算是空字串或指向唯一那個檔），compose 就**不再自動載入**
# docker-compose.override.yml。那是使用者沒要求我們動的行為。所以「不用 GPU」
# 的正確表示是**這行不存在**，不是「這行等於預設值」—— 缺席才是預設。
#
# 與 env_upsert 對稱：只認生效的行（`KEY=` 開頭），`# KEY=...` 這種註解掉的
# 行原樣保留，前綴相同的鍵（`COMPOSE_FILE_EXTRA`）不會被誤配。
#
# 條件反過來寫（`!=`，留下來才印）是刻意的：突變台要求每一條突變的目標
# 字串在檔案裡**唯一**，而這個檔案的兩個函式天生共用同樣的行。照 env_upsert
# 的寫法會讓那一行**一字不差地**再出現一次，於是它的既有突變會以「植入失敗」
# 收場 —— 那是突變台在正確地抱怨（見該檔 :24-26），而這一行讓它不必抱怨。
env_remove() {
  local key="$1"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" != "$key="* ]]; then printf '%s\n' "$line"; fi
  done
}

# ─────────────────────────────────────────────────────────
# CPU 架構（D-057）
# ─────────────────────────────────────────────────────────

# `uname -m` 的輸出 → Docker 的架構詞彙。**這是整份檔案唯一的一張映射表**，
# 下面兩個函式都經過它 —— 同一份映射寫兩次，改了一邊就會出現「形狀正確、
# 結論相反」的分歧（#83 那六條「植入失敗」換來的就是這個教訓）。
#
# 輸出用 Docker 的詞彙（amd64／arm64）而不是 uname 的（x86_64／aarch64），
# 因為下游全部是 Docker：manifest 清單、容器回報的 platform，以及要印給人看
# 的訊息。輸入維持 uname 的詞彙，因為那是它唯一的來源。
#
# 先正規化再判斷，而不是把兩種寫法並列在 `case` 的 `|` 兩邊：突變目標行
# **不可以含有 `|`**（見檔頭），所以那一行會沒辦法被瞄準 —— 而這裡最該被
# 瞄準的就是「aarch64 被當成 amd64」那一條。與 gpu_verdict 把 `||` 寫成
# `if` 是同一個理由。
cpu_arch() {
  local m="${1:-}"
  if [[ "$m" == "x86_64" ]]; then m="amd64"; fi
  if [[ "$m" == "aarch64" ]]; then m="arm64"; fi
  if [[ "$m" == "amd64" ]]; then printf 'amd64'; return 0; fi
  if [[ "$m" == "arm64" ]]; then printf 'arm64'; return 0; fi
  printf 'unknown'
}

# 這台機器的架構有沒有被量過。D-055 §一 記著：ARM VPS（Graviton／Ampere／
# Oracle ARM）沒有任何一層被驗證過，而那個缺口**不會報錯** —— 映像是
# multi-arch，所以它跑得起來，只是每個數字都沒人在上面量過。
#
#   verified    amd64：三次端到端都在這裡跑過
#   unverified  arm64：映像檔有（實查 registry 的 manifest 確認過：
#               ollama／open-webui／cloudflared 都出 arm64），但這個 lab
#               沒有在上面跑過任何一次。**只警告，不擋。**
#   unknown     其餘（armv7l／riscv64／i686…）：連有沒有映像檔都不知道 ——
#               那要問 registry，不是這裡能回答的。
#
# **`unverified` 不擋是刻意的**：D-055 §六.2 原本把方向寫成「不認識的架構要
# 硬擋（fail-closed）」，但在實查到「四個映像檔全部有 arm64」之後，硬擋等於
# 擋掉一個真的能用的佈署。那是「沒量過」，不是「不能用」—— 與 vCPU 那條
# （`deploy-vps.sh`：「可以跑，只是慢。不擋」）同一個判斷。
arch_verdict() {
  local a="$(cpu_arch "${1:-}")"
  if [[ "$a" == "amd64" ]]; then printf 'verified'; return 0; fi
  if [[ "$a" == "arm64" ]]; then printf 'unverified'; return 0; fi
  printf 'unknown'
}

# registry 的 manifest 裡有沒有這個架構。輸入是 `docker manifest inspect
# <image>` 的輸出，真的長這樣（縮排與空白都保留）：
#
#   { "schemaVersion": 2, "mediaType": "…/image.index.v1+json",
#     "manifests": [ { "platform": { "architecture": "amd64", "os": "linux" } }, … ] }
#
# 三態，而**中間那一態是這個函式存在的理由**：
#
#   yes      清單裡有這個架構，可以拉
#   no       清單讀得到，但裡面沒有 —— 這是「已知不可能」，呼叫端會**硬擋**。
#            例如 armv7l 的主機對上只有 amd64+arm64 的 ollama/ollama：那不是
#            慢，是 pull 到一半會失敗（與磁碟閘門同一類）。
#   unknown  **讀不到架構清單**。單一平台的 manifest（不是 index）根本沒有
#            `architecture` 這個欄位（實查：`docker manifest inspect
#            ollama/ollama@<digest>` 的輸出裡 0 個）；空輸出、逾時、被
#            rate limit、需要認證，也都是這一態。
#
# **`unknown` 既不能預設成 `no` 也不能預設成 `yes`。** 前者會對著一台好好
# 的機器說「沒有你這個架構的映像檔」—— 會誤報的守衛比沒有守衛更糟（這是
# D-056 的 size_vram 那一課）；後者則是放行之後在 pull 才失敗，而那正是這
# 一層要提前的事。無知就是一態，不是二選一（D-016）。
#
# 兩個實作細節都是被真實輸出逼出來的：比對前先把空白全部拿掉（index 用 3 個
# 空格縮排、單一 manifest 用 tab，兩種都出現過）；比的是**整個引號值**
# `"architecture":"arm64"`，否則 `arm` 會誤中 `arm64`。
image_arch_listed() {
  local json="${1:-}" want="${2:-}"
  if [[ -z "$want" ]]; then printf 'unknown'; return 0; fi
  if [[ "$want" == "unknown" ]]; then printf 'unknown'; return 0; fi
  local flat="${json//[[:space:]]/}"
  if [[ "$flat" != *'"architecture":'* ]]; then printf 'unknown'; return 0; fi
  if [[ "$flat" == *'"architecture":"'"$want"'"'* ]]; then printf 'yes'; return 0; fi
  printf 'no'
}

# 主機的架構與**另一個來源**回報的架構一不一致 —— 那個來源是本地映像的
# `.Architecture`（`docker image inspect` 的欄位，Docker 自己的詞彙）。兩個
# 輸入都會先過 `cpu_arch`，所以 `aarch64` 與 `arm64` 這兩種寫法都吃得下。
#
# #82 的落地層：**唯一會「跑得動又不報錯」的架構失敗是模擬執行** —— 主機裝了
# qemu/binfmt 時，Docker 會拿 amd64 映像在 aarch64 上跑，慢 10–100×，而
# compose 起得來、健康檢查過、模型也答話，**沒有任何其他訊號**。
#
#   native    兩邊讀到且相同
#   emulated  兩邊讀到但不同 —— 呼叫端回結束碼 1，不是 2。理由與
#             D-056 的 size_vram==0 相同：**失效的是我們自己的宣稱**（我們說
#             這一疊在原生的架構上跑），把它歸進「無法判定」就是讓它被忽略。
#   unknown   任一邊讀不到。無知不是缺陷（D-016），但也不是「沒問題」。
#
# **為什麼量的是映像的欄位，而不是進容器問 `platform.machine()`**：後者要靠
# qemu-user 把 `uname` 假造成被模擬的架構。那個行為我沒有辦法在這裡證明
# （本機是 x86，沒有 qemu），而**一個「失效時會安靜地說 native」的檢查比沒有
# 檢查更糟**。映像的 `.Architecture` 是決定 Docker 挑哪個 manifest 的權威欄位，
# 而且立刻量得到 —— 這一層因此是判準，不是推論（D-057 的界線那節記著這件事）。
arch_match_verdict() {
  local host="${1:-}" other="${2:-}"
  local h="$(cpu_arch "$host")"
  local o="$(cpu_arch "$other")"
  if [[ "$h" == "unknown" ]]; then printf 'unknown'; return 0; fi
  if [[ "$o" == "unknown" ]]; then printf 'unknown'; return 0; fi
  if [[ "$h" == "$o" ]]; then printf 'native'; return 0; fi
  printf 'emulated'
}
