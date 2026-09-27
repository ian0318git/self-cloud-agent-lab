#!/usr/bin/env bash
# 突變測試：把 deploy-vps-decisions.sh 的判準**故意弄壞**，確認
# test_deploy_vps_decisions.sh 真的會叫。
#
# 為什麼需要（D-024 第八節）：一組「永遠通過」的測試和「永遠失敗」的測試
# 一樣沒用。**唯一能證明測試有效的方法，是讓它面對一個已知的錯誤，
# 然後看它有沒有叫。**
#
# 這是本專案第一份**針對 shell 模組**的突變清單（前三份都在守 Python），
# 而 shell 多了一個 Python 沒有的限制：
#
#   **被指定為突變目標的行不可以含有 `|`。**
#
# 清單用 `label|原字串|取代字串` 三個欄位、以 `|` 切開（`${entry%%|*}` 與
# `${entry#*|}`），所以目標字串裡多一個 `|` 就會把欄位切錯 —— 而症狀是
# 「找不到目標字串」，跟「原始碼改過了」長得一模一樣。deploy-vps-decisions.sh
# 裡好幾處把 `-z "$a" || -z "$b"` 拆成兩行、把 `||` 寫成 `if`，就是為了讓
# 那些行可以當突變目標。**改那支模組時要維持這個性質**，否則對應的突變
# 會靜默地失去目標（這支腳本會叫 NOT_FOUND，不會靜默通過）。
#
# 這份清單裡每一條守的東西：
#   · 綁定預設值回錯 → 整個堆疊在公網上，而所有輸出都正常（最貴的一條）
#   · 閘門的 2 被讀成安全 → 把「我沒查到」講成「我查過了」
#   · 資源門檻的邊界（含端點、unknown 不當 0）→ 假失敗／假通過
#   · 記憶體狀態的兩個 unknown 併回去 → 「缺輸入」被講成「讀不到」，
#     於是處方給錯：人去查環境，而該做的是補 --model-gb（#93）
#   · context 門檻的 +1 → D-027 的結論本身，差一個 token 就是砍掉 1 個 token
#   · .env 的每一條邊界 → 改的是使用者唯一的設定檔，且失敗後會被重跑
#   · 架構三層 → 前六組全是「同一台已知機器上設定對不對」，這一組是第一個
#     「這台機器根本不一樣」。它的失效方式是**安靜地跑出不同結果**：
#     `arm64 → verified` 是把沒量過講成量過；`不等時回 native` 是讓 qemu
#     模擬執行整個靜默；`讀不到欄位回 no` 則是對著一台好機器硬擋
#
# 做法：把模組與測試複製到暫存目錄、用字串取代植入突變，從那份副本跑測試。
# 原始檔從頭到尾不被修改。
#
# 用法：bash scripts/test_deploy_vps_decisions_mutants.sh
#
# 結束碼：0 = 每個突變都被抓到，且對照組通過
#         1 = 有突變沒被抓到（測試有洞）、或對照組失敗（測試壞了）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="$SCRIPT_DIR/deploy-vps-decisions.sh"
TEST="$SCRIPT_DIR/test_deploy_vps_decisions.sh"
PROBE="$SCRIPT_DIR/deploy_smoke_probe.py"
PROBE_TEST="$SCRIPT_DIR/test_deploy_smoke_probe.py"
LIFECYCLE="$SCRIPT_DIR/profile-lifecycle.sh"
LIFECYCLE_TEST="$SCRIPT_DIR/test_profile_lifecycle.sh"
DEPLOY="$SCRIPT_DIR/deploy-vps.sh"
LIB="$SCRIPT_DIR/lib.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# **不要寫 .pyc。** 突變是用字串取代植入的，而 CPython 對 .pyc 的有效性
# 檢查只比對「原始檔的 mtime（**秒**）與大小」。兩條連續的突變若長度相同
# 又落在同一秒內，第二次寫入就會命中第一次留下的 .pyc —— 那個突變**根本
# 沒有被執行**，測試當然通過，而突變台把它報成「抓到」。
# 實測：`return 1` → `return 2` 這種同長度取代，在同一秒內被完全忽略。
# 一個會謊報「抓到」的突變台是 fail-open —— 比沒有突變台更糟，因為它讓
# 「全部抓到」這句話變成假的。
export PYTHONDONTWRITEBYTECODE=1

# 測試自己會去 source（bash）或 import（python）同目錄的受測檔案，所以
# 每一份都要在 $WORK。lib.sh 不突變 —— 只放在那裡讓決定模組的測試找得到
# （它的 PROJECT_ROOT 在那個測試裡用不到，沒有任何函式會讀它）。
#
# **`deploy-vps.sh` 也在這份清單裡，但它不是突變目標**：決定模組的測試有一
# 條會去讀它的 `case` 接線（#93）。少了這一份，那兩條在 $WORK 裡會因為
# 「找不到檔案」而失敗，而症狀是**對照組就掛掉** —— 看起來像測試本身壞了，
# 不是像少複製一個檔案。
cp "$LIB" "$WORK/lib.sh"
cp "$MODULE" "$WORK/deploy-vps-decisions.sh"
cp "$TEST" "$WORK/test_deploy_vps_decisions.sh"
cp "$DEPLOY" "$WORK/deploy-vps.sh"
cp "$PROBE" "$WORK/deploy_smoke_probe.py"
cp "$PROBE_TEST" "$WORK/test_deploy_smoke_probe.py"
cp "$LIFECYCLE" "$WORK/profile-lifecycle.sh"
cp "$LIFECYCLE_TEST" "$WORK/test_profile_lifecycle.sh"

# ── 對照組先跑：不動任何東西，兩份測試都必須通過 ────────
# 沒有這一步，下面「每個突變都被抓到」可能只是因為測試**永遠失敗**。
info "對照組（無突變）"
if ! bash "$WORK/test_deploy_vps_decisions.sh" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_deploy_vps_decisions.sh 本身有問題，先修它"
  exit 1
fi
if ! python3 "$WORK/test_deploy_smoke_probe.py" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_deploy_smoke_probe.py 本身有問題，先修它"
  exit 1
fi
if ! bash "$WORK/test_profile_lifecycle.sh" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_profile_lifecycle.sh 本身有問題，先修它"
  exit 1
fi
ok "對照組通過（三份測試都不是永遠失敗）"
echo

# 突變清單：label|原字串|取代字串
# 原字串必須在該目標檔案裡**唯一**出現一次，而且**不含 `|`**（見開頭的說明）。
#
# label 的前綴決定突變哪一份、跑哪一份測試：
#   `py:` → deploy_smoke_probe.py ／ test_deploy_smoke_probe.py
#   `pl:` → profile-lifecycle.sh ／ test_profile_lifecycle.sh
#   其餘 → deploy-vps-decisions.sh ／ test_deploy_vps_decisions.sh
# 前綴在比對時會被去掉，輸出裡看不到。
MUTANTS=(
  # ── default_bind_addr：整個部署最重要的一個字 ──
  # 這一條是整份清單裡代價最高的：回錯一個字，堆疊就在公網上，而
  # 「容器起來了、健康檢查過了、模型也下載了」全部照常。
  "綁定預設：非 Codespaces 也給 0.0.0.0|  printf '127.0.0.1'|  printf '0.0.0.0'"
  "綁定預設：沒有明講時回空字串|  printf '127.0.0.1'|  printf ''"

  # ── resolve_bind_addr：.env 的值不能照單全收 ──
  # 這一條就是實際發生過的缺陷：`.env.example` 出貨時是 0.0.0.0，而全新 VPS
  # 的 `.env` 是 `cp .env.example .env` 來的 —— 少了萬用位址的判斷，「檔案裡
  # 本來就有 0.0.0.0」會讓整支腳本安安靜靜地把洞原封不動留下來，而它所有的
  # 輸出（容器起來了、健康檢查過了、模型下載了）照常。
  "解析綁定：.env 有值就照單全收（不判萬用位址）|  elif [[ -n \"\$env_value\" && \"\$(bind_value_is_wildcard \"\$env_value\")\" == \"no\" ]]; then|  elif [[ -n \"\$env_value\" ]]; then"
  # 覆蓋了卻不記錄原值 = 那個「已改成 127.0.0.1」的警告永遠不會印。使用者
  # 於是不會知道自己原本的設定被改了 —— 一個靜默的變更。
  "解析綁定：覆蓋掉 .env 的值卻不記錄（警告永遠不印）|    if [[ -n \"\$env_value\" && \"\$env_value\" != \"\$resolved\" ]]; then|    if false; then"
  # --expose 是唯一的例外出口；它失效等於那個出口不存在。
  "解析綁定：--expose 不再無條件開放|  if [[ \"\$expose\" == \"1\" ]]; then|  if false; then"

  # ── bind_is_wildcard：讀不到不等於安全 ──
  # 測試裡「讀不到時回 unknown」那一條守著它。少了它，一個把空輸入讀成
  # "no"（= 沒綁在萬用位址 = 安全）的實作會全綠 —— 那正是 fail-open。
  "綁定判讀：讀不到時當成沒綁在萬用位址|  if [[ -z \"\$text\" ]]; then printf 'unknown'; return 0; fi|  if false; then printf 'unknown'; return 0; fi"

  # ── bind_addr_scope ／ port_bind_scope：「不是 0.0.0.0」不等於 loopback ──
  # 這一組是這次要補的洞。原本 check-exposure.sh 只問「是不是萬用位址」，
  # 其餘一律印「僅綁在 loopback」—— 綁在區網位址時那句話與事實相反，
  # 綁在公開位址時它是把「對 Internet 開著」講成「未發現對外暴露」。
  #
  # **最貴的一條就是第一條**：公開位址被判成 private 的話，整套安全檢查
  # 就是空的，而它的每一行輸出都照常。
  "位址分類：公開 IPv4 被判成私有（fail-open）|    *.*.*.*)                   printf 'public' ;;|    *.*.*.*)                   printf 'private' ;;"
  "位址分類：公開 IPv6 被判成私有|    *:*)                       printf 'public' ;;|    *:*)                       printf 'private' ;;"
  # 方括號不脫 → `[fd00::1]` 與 `[::1]` 都會落到「看起來像 IP」那條被判公開。
  # 方向是安全的，但結論是錯的，而錯的結論會讓人開始不信這支腳本。
  "位址分類：IPv6 的方括號不脫|  addr=\"\${addr#\\[}\"|  true"
  "位址分類：10.x 不再算私有（會落到公開那條）|    10.*)                      printf 'private' ;;|    10.*)                      printf 'public' ;;"
  # 萬用位址的短路拿掉之後，`0.0.0.0` 會落到逐條解析，而縮減的 case 沒有
  # wildcard 這一格 —— 於是它會被算成 loopback。
  "整體綁定：萬用位址不再短路（會被算成 loopback）|  if [[ \"\$(bind_is_wildcard \"\$text\")\" == \"yes\" ]]; then printf 'wildcard'; return 0; fi|  if false; then printf 'wildcard'; return 0; fi"
  # 解析不出任何位址時回 loopback = 原本那個 fail-open 的原形。
  "整體綁定：解析不出位址時當成 loopback|  if [[ \$found -eq 0 ]]; then printf 'unknown'; return 0; fi|  if false; then printf 'unknown'; return 0; fi"
  # 多條綁定取最寬的那兩格。少一格，一條公開 + 一條 loopback 就會回 loopback。
  "整體綁定：私有被 loopback 吃掉|      private) if [[ \"\$worst\" == 'loopback' ]]; then worst='private'; fi ;;|      private) : ;;"
  "整體綁定：unknown 被吃掉|      unknown) if [[ \"\$worst\" != 'public' ]]; then worst='unknown'; fi ;;|      unknown) : ;;"

  # ── bind_value_is_wildcard：萬用位址的定義 ──
  # 空字串代表「.env 沒設這個鍵」，不是「開放到全部介面」。兩者混為一談會
  # 讓「沒設」被讀成「已明確開放」，於是 default_bind_addr 永遠不被套用。
  "萬用位址：空字串也當成萬用位址|    *)                  printf 'no' ;;|    *)                  printf 'yes' ;;"

  # ── exposure_gate_verdict：0/1/2/3 是四個不同的處置 ──
  "閘門：發現暴露也放過|  if [[ \"\$rc\" == \"1\" ]]; then printf 'block'; return 0; fi|  if [[ \"\$rc\" == \"1\" ]]; then printf 'pass'; return 0; fi"
  "閘門：2 與 3 混為一談|  if [[ \"\$rc\" != \"2\" ]]; then printf 'broken'; return 0; fi|  if [[ \"\$rc\" != \"9\" ]]; then printf 'broken'; return 0; fi"
  "閘門：lenient 不降級（等於兩邊同一個政策）|  if [[ \"\$mode\" == \"lenient\" ]]; then printf 'warn'; return 0; fi|  if false; then printf 'warn'; return 0; fi"

  # ── nat_fronted_environment：這台的可見位址推不推得出對外可達性 ──
  # 兩條一組：前者讓雲主機漏判（fail-open），後者讓一般機器誤判（假失敗）。
  "NAT 判定：AWS 不算 1:1 NAT|    *\"Amazon EC2\"*)            printf 'AWS EC2'; return 0 ;;|    *\"Amazon EC2\"*)            printf ''; return 0 ;;"
  "NAT 判定：只比對廠商就命中（Surface 會被誤判）|    *\"Microsoft Corporation\"*\"Virtual Machine\"*) printf 'Azure 或 WSL2'; return 0 ;;|    *\"Microsoft Corporation\"*) printf 'Azure 或 WSL2'; return 0 ;;"
  # 這一條原本寫成「把 haystack 的宣告改成有初值」，而下一行立刻用
  # `haystack="$(...)"` 覆蓋掉 —— 於是它**沒有改變任何行為**，而沒改變
  # 行為的突變從外面看起來與「測試有洞」一模一樣。第一輪跑就是它沒被抓到，
  # 而處理方式是修突變、不是修測試（與 test_mem0_add_cost_probe_mutants.sh
  # 記的那條同一個形狀：倖存的突變要先懷疑突變本身）。
  # 改成真的實作那個 bug：不確定的（非雲廠商）也當成 1:1 NAT 環境。
  "NAT 判定：不確定的也當成命中|  printf ''|  printf 'unknown'"

  # ── wide_bind_verdict：綁在萬用位址時的結論 ──
  # **最貴的兩條。** 前者是這次要補的洞（讀不到 ip 被讀成安全），後者是
  # 使用者明確要求不可以翻的那個極性。
  "萬用綁定：讀不到 ip 當成只有私有位址|  if [[ \"\$ip_available\" != \"true\" ]]; then printf 'unknown'; return 0; fi|  if false; then printf 'unknown'; return 0; fi"
  "萬用綁定：只有私有位址也當成無法判定（翻轉極性）|  printf 'lan_only'|  printf 'unknown'"
  "萬用綁定：1:1 NAT 的雲當成只有私有位址|  if [[ -n \"\$nat_env\" ]]; then printf 'nat_fronted'; return 0; fi|  if false; then printf 'nat_fronted'; return 0; fi"
  # 順序顛倒 → Codespaces（跑在 Azure 上）會被 nat 判定吃掉。
  "萬用綁定：Codespaces 不再優先|  if [[ \"\$is_codespace\" == \"true\" ]]; then printf 'codespace'; return 0; fi|  if false; then printf 'codespace'; return 0; fi"
  "萬用綁定：有公開位址不再優先|  if [[ \"\$has_public\" == \"true\" ]]; then printf 'exposed'; return 0; fi|  if false; then printf 'exposed'; return 0; fi"

  # ── fresh_install_verdict：看的是「有沒有人類的資料」──
  "全新安裝：有資料也當成全新（會蓋掉別人的堆疊）|  if [[ \"\$has_data\" -eq 1 ]]; then|  if false; then"
  "全新安裝：--keep-data 無效|    if [[ \"\$keep\" -eq 1 ]]; then printf 'kept'; return 0; fi|    if false; then printf 'kept'; return 0; fi"

  # ── disk_verdict：唯一會硬擋的資源 ──
  "磁碟：讀不到也往下判|  if [[ ! \"\$free_gb\" =~ ^[0-9]+\$ ]]; then printf 'unknown'; return 0; fi|  if false; then printf 'unknown'; return 0; fi"
  "磁碟：門檻從 < 變成 <=（剛好夠也被擋）|  if (( free_gb < need_gb )); then printf 'insufficient'; return 0; fi|  if (( free_gb <= need_gb )); then printf 'insufficient'; return 0; fi"

  # ── ram_verdict：只警告，但也不可以謊報 ──
  "記憶體：非數字當成 0（會變成假的 tight）|  if [[ ! \"\$avail_mb\" =~ ^[0-9]+\$ ]]; then printf 'unknown'; return 0; fi|  if false; then printf 'unknown'; return 0; fi"

  # ── memory_verdict：兩個 unknown 不可以併回去，空需求更不可以變成 OK ──
  # 第一條就是 #93 本身 —— 需求量是空字串時回 ok，也就是那個「綠色 OK 配
  # 空白需求數字」。它必須被 unknown_need 那幾條抓到，否則那一組是空的：
  # 一個測不到缺陷的迴歸測試，與沒有這個測試是一樣的。
  #
  # 第二條守的是**處方**：兩個 unknown 一旦併回去，「缺輸入」就會被講成
  # 「讀不到 /proc/meminfo」，於是人去查環境，而該做的是補 --model-gb。
  "記憶體狀態：估不出需求被講成 ok（#93 本體）|  printf 'unknown_need'|  printf 'ok'"
  "記憶體狀態：兩個 unknown 併回一個（缺輸入被講成讀不到）|  if [[ ! \"\$avail_mb\" =~ ^[0-9]+\$ ]]; then printf 'unknown_avail'; return 0; fi|  if false; then printf 'unknown_avail'; return 0; fi"
  "記憶體狀態：一律回 ok（連 tight 都不見了）|  if [[ \"\$verdict\" != \"unknown\" ]]; then printf '%s' \"\$verdict\"; return 0; fi|  if true; then printf 'ok'; return 0; fi"

  # ── 估算與需求 ──
  "模型大小：4b 查到錯的數字|    qwen3:4b)             printf '2.5'; return 0 ;;|    qwen3:4b)             printf '0.0'; return 0 ;;"
  "記憶體需求：KV cache 那項不算進去|    'BEGIN { printf \"%d\", g * 1024 + (c * kv) / 1024 + 2048 }'|    'BEGIN { printf \"%d\", g * 1024 + 2048 }'"

  # ── ctx_meets_mem0：D-027 的結論本身 ──
  "context：門檻忘了 +1（>= 寫成 >）|  if (( n >= MEM0_ADD_MIN_CTX )); then printf 'yes'; return 0; fi|  if (( n > MEM0_ADD_MIN_CTX )); then printf 'yes'; return 0; fi"
  "context：門檻常數少一|MEM0_ADD_MIN_CTX=8101|MEM0_ADD_MIN_CTX=8100"

  # ── ctx_holds_through_generation：D-035 的第二個門檻 ──
  # 這一條突變把「生成期間」的門檻換成「生成之前」的門檻 —— 也就是**把
  # 8192 這個壞掉的預設值重新變成合格的**。它必須被測到，因為那正是這個
  # 函式唯一的存在理由，而它與 ctx_meets_mem0 長得幾乎一樣。
  "context：第二個門檻被換成第一個|  if (( n >= MEM0_ADD_HOLD_CTX )); then printf 'yes'; return 0; fi|  if (( n >= MEM0_ADD_MIN_CTX )); then printf 'yes'; return 0; fi"
  "context：生成期間的門檻忘了 num_predict|MEM0_ADD_HOLD_CTX=10101|MEM0_ADD_HOLD_CTX=8101"
  "context：生成期間的門檻邊界鬆一格（>= 寫成 >）|  if (( n >= MEM0_ADD_HOLD_CTX )); then printf 'yes'; return 0; fi|  if (( n > MEM0_ADD_HOLD_CTX )); then printf 'yes'; return 0; fi"

  # ── num_ctx_verdict：參數錯誤要走 3，不是 2 ──
  "num_ctx：非整數照收|  if [[ ! \"\$raw\" =~ ^[0-9]+\$ ]]; then printf 'not_integer'; return 0; fi|  if false; then printf 'not_integer'; return 0; fi"
  "num_ctx：下限不檢查|  if (( raw < 512 )); then printf 'too_small'; return 0; fi|  if false; then printf 'too_small'; return 0; fi"

  # ── env_upsert：改的是使用者唯一的設定檔，而且會被重跑 ──
  # 前三條合起來就是這支函式存在的理由：重複鍵是最後一行生效，所以
  # 「我改過了」可以與事實相反，而且是無聲的。
  "env：註解掉的行也算生效|    if [[ \"\$line\" == \"\$key=\"* ]]; then|    if [[ \"\$line\" == *\"\$key=\"* ]]; then"
  "env：前綴相同的鍵也配對|    if [[ \"\$line\" == \"\$key=\"* ]]; then|    if [[ \"\$line\" == \"\$key\"* ]]; then"
  "env：重複的生效行全部保留|      if [[ \"\$found\" -eq 0 ]]; then printf '%s=%s\\n' \"\$key\" \"\$value\"; fi|      printf '%s=%s\\n' \"\$key\" \"\$value\""
  "env：沒有的鍵不附加|  if [[ \"\$found\" -eq 0 ]]; then printf '%s=%s\\n' \"\$key\" \"\$value\"; return 0; fi|  if false; then printf '%s=%s\\n' \"\$key\" \"\$value\"; return 0; fi"
  "env：不補結尾換行（下一個鍵會黏在同一行）|    printf '%s\\n' \"\$line\"|    printf '%s' \"\$line\""

  # ══════════════════════════════════════════════════════════
  # deploy_smoke_probe.py（前綴 py:）
  # ══════════════════════════════════════════════════════════
  # 這一組守的是**部署當下唯一那個會說「好了」的判斷**。最貴的一條是
  # 「被切斷的生成被當成完整的量測」（D-027 第十一節，六處）。
  "py:煙霧：沒講完也算過關（done_reason 不檢查）|    if done_reason != \"stop\":|    if False:"
  "py:煙霧：撞到 num_predict 上限也算過關|    if eval_count >= num_predict:|    if False:"
  "py:煙霧：完全沒生成也算過關|    if eval_count <= 0:|    if False:"
  "py:煙霧：空 content 不算失敗|    if not content.strip():|    if False:"
  # 這一條與上一條改同一行、但只放過「純空白」。它證明了那條測試（而不是
  # 別的測試）在守 strip() —— 少了它，一個把 `not x.strip()` 寫成 `not x`
  # 的實作會全綠。
  "py:煙霧：只有空白也算有內容|    if not content.strip():|    if not content:"
  # 讀不到時回「無法判定」而不是「失敗」—— 這一組守的是**假失敗**。
  "py:煙霧：done_reason 缺席時當成失敗|    if \"done_reason\" not in response:|    if False:"
  "py:煙霧：讀不到 message 時當成空字串|    if not isinstance(message, dict):|    if False:"
  # 讓判定有意義的請求參數，各一條。
  "py:煙霧：不關 streaming（會讀到一串 NDJSON）|        \"stream\": False,|        \"stream\": True,"
  "py:煙霧：不固定 temperature|        model, SMOKE_PROMPT, {\"temperature\": 0, \"num_predict\": num_predict}|        model, SMOKE_PROMPT, {\"temperature\": 1, \"num_predict\": num_predict},"
  # **把 `think: false` 加回去。** 這是實際踩過的坑：它不會關掉推理，只把
  # 推理從 `thinking` 搬進 `content`，於是「content 非空」可以被推理前言
  # 單獨滿足 —— 一個從頭到尾自言自語、從沒回答的模型也會過關。
  # 多行要用 `$'...'` —— 雙引號**不展開 `\n`**，植入的會是一個字面的
  # 反斜線加 n，症狀是 SyntaxError：測試確實失敗了，但失敗的原因是模組
  # 匯入不了，不是哪一條斷言叫了。同一個家族（no-op／壞掉的植入）在
  # `test_phase3_mutants.sh` 的 `< -0` 與 `test_throughput_probe_mutants.sh`
  # 的 `"message": (` 各出現過一次。
  $'py:煙霧：把 think=false 加回去（推理會搬進 content）|        "stream": False,|        "stream": False,\n        "think": False,'
  # 預設預算被調回一個註定不夠的值：實測推理要 152 個 token，32 會全部
  # 被吃掉，然後 done_reason=length 被讀成「上游變了」—— 假失敗（D-016）。
  #
  # **要突變的是預算，不是量到的長度。** 第一版這條寫成把
  # MEASURED_REASONING_TOKENS 調小，結果它存活了 —— 因為把量到的數字調小
  # 只會讓 `預算 > 量測` 更容易成立。那暴露了測試的一個真弱點（見底下
  # test_smoke_default_budget_covers_a_reasoning_model 的範圍斷言）。
  "py:煙霧：預設預算調回 32（推理會吃掉全部預算）|DEFAULT_NUM_PREDICT = 512|DEFAULT_NUM_PREDICT = 32"
  # HTTP 錯誤是「這支探針送的請求有問題」，叫人去查模型等於指錯方向。
  "py:煙霧：HTTP 錯誤當成模型失敗|    return EXIT_BROKEN, (|    return EXIT_FAIL, ("
  # ── 結束碼的對應：環境 ≠ 上游 ≠ 探針自己壞了（D-018）──
  "py:煙霧：模型沒拉當成探針壞了|    if \"not found\" in low or \"no such model\" in low:|    if False:"
  "py:參數：錯誤回 2 而不是 3|    return EXIT_BROKEN  # 參數錯誤是「這支腳本自己壞了」，不是「環境無法判定」|    return EXIT_INDETERMINATE  # 參數錯誤是「這支腳本自己壞了」，不是「環境無法判定」"
  # ── ctx_verdict：一個設了卻沒生效的開關 ──
  "py:ctx：不符時當成相符|    if got == expected:|    if True:"
  "py:ctx：名字不正規化（未帶 tag 的會被誤報）|    want = normalize_model(model)|    want = model"
  "py:ctx：不挑模型，用第一筆|        if normalize_model(name) != want:|        if False:"
  # 這一條是**設計決定**本身：num_ctx 沒生效走 2 不走 1。改了它，
  # run_ctx 就再也沒有告訴任何人「堆疊是好的、只是這個設定沒生效」。
  "py:ctx：沒生效當成判準沒過（回 1）|EXIT_CTX_NOT_APPLIED = EXIT_INDETERMINATE|EXIT_CTX_NOT_APPLIED = EXIT_FAIL"

  # ── gpu_verdict / run_gpu：我們說它在 GPU 上，而它其實不在 ──
  # 這一組全部是**同一個病**的不同入口：把「我們宣稱的事沒有發生」降級成
  # 「量不到」。降級之後每一條都還是「不通過」，但代價不一樣 —— 部署腳本會
  # 把它歸類成環境問題（2）而不是自己的宣稱失效（1），於是沒有人去修。
  "py:GPU：size_vram=0 不算失敗（退化成「量不到」）|    if vram == 0:|    if False:"
  "py:GPU：全在 GPU 上不認得（判準永遠不通過）|    if vram == total:|    if False:"
  # 這是最惡的一條：讀不到欄位時給它一個預設值 0，於是**對著好好的 GPU 佈署
  # 大喊「它跑在 CPU 上」**。會誤報的守衛比沒有守衛更糟。
  "py:GPU：讀不到 size_vram 當成 0（對好好的 GPU 誤報 CPU）|    vram = entry.get(\"size_vram\")|    vram = entry.get(\"size_vram\", 0)"
  # bool 是 int 的子類：True == 1，不擋的話會拿「1 個位元組」去算比例。
  "py:GPU：bool 不擋（True 被當成 1 位元組）|        if isinstance(value, bool) or not isinstance(value, int):|        if not isinstance(value, int):"
  # size=0 時 0==0 —— 少了這一條，比例失去意義的那一刻會回一個「整顆都在
  # GPU 上」的**假通過**。
  "py:GPU：size=0 時 0==0 被當成「整顆在 GPU 上」|    if total <= 0:|    if False:"
  # keep_alive 縮成 0：模型在回應之後立刻被卸載，於是永遠讀不到它 ——
  # 判定會從「它在 CPU 上」變成「量不到」，靜默就從這裡回來。
  "py:GPU：keep_alive 給 0（模型在讀 /api/ps 前就被卸載）|GPU_CHECK_KEEP_ALIVE = \"5m\"|GPU_CHECK_KEEP_ALIVE = \"0\""
  # 這一條是**設計決定**本身：GPU 明明在跑 CPU 時走 1 不走 2。改了它，
  # #83 要消滅的靜默失敗就從後門回來了 —— 部署腳本會把它當成「量不到」放過去。
  "py:GPU：跑在 CPU 上卻回 2（靜默從後門回來）|        # #83 要消滅的東西，所以它必須響，不能混進「無法判定」裡被忽略。
        return EXIT_FAIL, reason|        # #83 要消滅的東西，所以它必須響，不能混進「無法判定」裡被忽略。
        return EXIT_INDETERMINATE, reason"

  # ── profile-lifecycle.sh：殘留容器與對外連線狀態 ──
  # 這一組的兩個方向都會出錯，而**誤殺比漏清貴**：漏清留下一個還在對外服務
  # 的 cloudflared（D-029 的缺陷本體），誤殺則是把正在服務的容器砍掉，而
  # 使用者只會看到服務突然中斷。下面兩條各守一邊。
  #
  # 漏清的方向：不回報任何東西 = 這整套修正等於沒做。
  "pl:殘留清理：完全不回報（修正等於沒做）|    printf '%s\\n' \"\$name\"|    :"
  # 誤殺的方向：不問作用中清單就通通當成殘留 —— 這一條會砍掉整個堆疊。
  "pl:殘留清理：不問清單，全部當成殘留（會砍掉整個堆疊）|    if grep -qxF \"\$svc\" <<<\"\$active\"; then|    if false; then"
  # `-x` 拿掉：服務 `ollama` 會匹配到 `ollama-extra`，那個容器就被留下來了。
  "pl:殘留清理：少了 -x，前綴相同的服務被當成同一個|    if grep -qxF \"\$svc\" <<<\"\$active\"; then|    if grep -qF \"\$svc\" <<<\"\$active\"; then"
  # `-F` 拿掉：服務名裡的 `.` 變成萬用字元。
  "pl:殘留清理：少了 -F，服務名被當成正則|    if grep -qxF \"\$svc\" <<<\"\$active\"; then|    if grep -qx \"\$svc\" <<<\"\$active\"; then"
  # 這條守衛擋的是「compose 檔一有語法錯誤就砍掉整個堆疊」——實測
  # `config --services` 在壞掉的 compose 檔上回 1 且沒有任何輸出。
  "pl:殘留清理：拿掉空清單守衛（compose 檔一壞就砍掉整個堆疊）|  if [[ -z \"\$active\" ]]; then|  if false; then"
  # 沒有 service 標籤的容器無從判斷，守衛拿掉就會被當成殘留砍掉。
  "pl:殘留清理：拿掉空 service 守衛（不確定的容器也砍）|    if [[ -z \"\$svc\" ]]; then|    if false; then"
  # 介面契約：呼叫方拿輸出直接餵 `docker rm -f`，回服務名會刪錯東西。
  "pl:殘留清理：回服務名而不是容器名|    printf '%s\\n' \"\$name\"|    printf '%s\\n' \"\$svc\""

  # ── gpu_verdict／gpu_runtime_registered：掛不掛 GPU override 全靠它 ──
  # 這一組守的是 D-055 §二 那個缺口：這個堆疊在沒有裝置保留區的機器上
  # **不會報錯**，它會起得來、答得出話，只有速度是錯的。所以下面每一條
  # 「靜默降級」都必須被測試抓住，否則修正等於沒做。
  #
  # 核心案例：有硬體、但 runtime 沒裝好時回 cpu 而不是 blocked —— 那就回到
  # 修正前的行為，而且連一行警告都沒有。
  "gpu：有硬體但沒 runtime 時靜默降級成 CPU（核心案例）|    printf 'blocked'; return 0|    printf 'cpu'; return 0"
  # runtime 的判定翻轉：會在沒註冊 nvidia runtime 的機器上掛裝置保留區，
  # 於是 compose 起不來 —— 從「安靜地慢」變成「當場壞掉」，一樣沒被擋。
  "gpu：runtime 判定翻轉（沒裝的機器上掛保留區）|    if [[ \"\$rt\" == \"yes\" ]]; then printf 'gpu'; return 0; fi|    if [[ \"\$rt\" == \"no\" ]]; then printf 'gpu'; return 0; fi"
  # mode=on 是使用者明確要了。給不出來卻安靜地跑 CPU，就是這一整個家族在防的事。
  "gpu：mode=on 拿不到 GPU 時靜默降級|  if [[ \"\$mode\" == \"on\" ]]; then printf 'blocked'; return 0; fi|  if [[ \"\$mode\" == \"on\" ]]; then printf 'cpu'; return 0; fi"
  # mode=off 是使用者唯一的關閉方式；不尊重它就再也關不掉 GPU。
  "gpu：mode=off 被忽略（使用者關不掉 GPU）|  if [[ \"\$mode\" == \"off\" ]]; then printf 'cpu'; return 0; fi|  if false; then printf 'cpu'; return 0; fi"
  # 預設值漂到 on 的話，「什麼都沒設定」會變成「要了 GPU」，於是沒卡的機器
  # 一律被擋 —— 一個把正常情況變成失敗的錯誤預設。
  "gpu：省略參數時的預設漂成 on|  local hw=\"\${1:-no}\" rt=\"\${2:-no}\" mode=\"\${3:-auto}\"|  local hw=\"\${1:-no}\" rt=\"\${2:-no}\" mode=\"\${3:-on}\""
  # 子串比對：`nvidia-experimental` 這種 runtime 會被當成 nvidia，於是
  # compose 用一個不存在的 runtime 起容器。這一條就是那個函式存在的理由。
  "gpu runtime：改成子串比對（nvidia-experimental 被誤認）|  if [[ \"\$text\" == *'\"nvidia\":'* ]]; then printf 'yes'; return 0; fi|  if [[ \"\$text\" == *nvidia* ]]; then printf 'yes'; return 0; fi"
  # 讀不到 docker info 的輸出時回 yes —— 無知被當成「有」。
  "gpu runtime：讀不到時回 yes（無知當成有）|  local text=\"\${1:-}\"|  local text=nvidia"

  # ── env_remove：COMPOSE_FILE 的「不存在」必須是真的不存在 ──
  # 只要有一行生效的 COMPOSE_FILE，compose 就不再自動載入 override 檔 ——
  # 那是使用者沒要求我們動的行為。所以「沒刪掉」與「刪錯」都要被抓住。
  "env_remove：什麼都不移除（關不掉 GPU）|    if [[ \"\$line\" != \"\$key=\"* ]]; then printf '%s\\n' \"\$line\"; fi|    :"
  "env_remove：少了 =，前綴相同的鍵被誤刪|    if [[ \"\$line\" != \"\$key=\"* ]]; then printf '%s\\n' \"\$line\"; fi|    if [[ \"\$line\" != \"\$key\"* ]]; then printf '%s\\n' \"\$line\"; fi"

  # ── tunnel_state_verdict：四態裡只有 stale 是危險的 ──
  # 把 stale 併回 off，就回到修正前的行為 ——「設定檔說關了」被當成「沒連線」，
  # 而容器還在跟 Cloudflare 邊緣保持連線。
  "pl:對外：stale 併入 off（說關了其實還開著）|      printf 'stale'|      printf 'off'"
  "pl:對外：profile 的判定翻轉（四態全部錯位）|  if [[ \"\$profile\" == \"yes\" ]]; then|  if [[ \"\$profile\" != \"yes\" ]]; then"

  # ── cpu_arch：唯一的映射表，錯一個字整條階梯都歪 ──
  # 這四條與 GPU 那組的形狀不同：它們錯的時候**不會有任何症狀** —— 佈署
  # 照樣成功，只是「在沒量過的平台上」那句話沒被說出來（或反過來，被說成
  # 「量過了」）。架構是這個 repo 裡第一個「換一台機器」類的判準。
  "架構：x86_64 不映射（x86 主機被當成認不得）|  if [[ \"\$m\" == \"x86_64\" ]]; then m=\"amd64\"; fi|  :"
  "架構：aarch64 映射成 amd64（ARM 主機被當成 x86）|  if [[ \"\$m\" == \"aarch64\" ]]; then m=\"arm64\"; fi|  if [[ \"\$m\" == \"aarch64\" ]]; then m=\"amd64\"; fi"

  # **這一條就是 #82 存在的理由**：把 arm64 講成 verified，等於宣稱
  # 「這個 lab 在 ARM 上驗證過」。上面全部的吞吐量數字都是 x86 量的。
  "宣告：arm64 回 verified（宣稱在 ARM 上驗證過）|  if [[ \"\$a\" == \"arm64\" ]]; then printf 'unverified'; return 0; fi|  if [[ \"\$a\" == \"arm64\" ]]; then printf 'verified'; return 0; fi"
  # 反方向：x86 反而變成「沒量過」，於是量過的那條路上也多一句假警告 ——
  # 而假警告會訓練人忽略警告（D-056）。
  "宣告：amd64 回 unverified（量過的那條路變成沒量過）|  if [[ \"\$a\" == \"amd64\" ]]; then printf 'verified'; return 0; fi|  if [[ \"\$a\" == \"amd64\" ]]; then printf 'unverified'; return 0; fi"

  # ── image_arch_listed：這一組的失效方式全都是「對著好機器說它不能跑」 ──
  # **代價最高的一條。** 單一平台的 manifest 沒有 architecture 欄位，讀成
  # `no` 就會對一台映像檔明明在本地、容器也起來了的機器硬擋，並印出「它有的
  # 是：」後面接一句空白。那是 D-056 的 size_vram 同一課。
  "裝置：讀不到 architecture 欄位時回 no（誤擋一台好好的機器）|  if [[ \"\$flat\" != *'\"architecture\":'* ]]; then printf 'unknown'; return 0; fi|  if [[ \"\$flat\" != *'\"architecture\":'* ]]; then printf 'no'; return 0; fi"
  # 少了短路，`want=unknown` 會命中 manifest 裡**真的存在**的
  # `\"architecture\": \"unknown\"`（attestation，不是平台）→ 回 yes。
  # 而 unknown 正是呼叫端認不得 uname -m 時唯一會傳進來的值。
  "裝置：want=unknown 不短路（attestation 被當成平台）|  if [[ \"\$want\" == \"unknown\" ]]; then printf 'unknown'; return 0; fi|  true"
  "裝置：want 是空字串時回 yes（無知當成有）|  if [[ -z \"\$want\" ]]; then printf 'unknown'; return 0; fi|  if [[ -z \"\$want\" ]]; then printf 'yes'; return 0; fi"
  # 真實的 `docker manifest inspect` 是 3 空格縮排，不去空白就永遠比不中 ——
  # 症狀是每個映像都被判成「沒有這個架構」。
  "裝置：不去空白（排版的 JSON 永遠比不中）|  local flat=\"\${json//[[:space:]]/}\"|  local flat=\"\$json\""
  # 子串比對：ollama 有 arm64 而沒有 arm，但 \"arm64\" 這個字串裡有 \"arm\"。
  # 這是這個 repo 的老題目 —— 形狀正確、結論相反。
  "裝置：改成子串比對（arm 誤中 arm64）|  if [[ \"\$flat\" == *'\"architecture\":\"'\"\$want\"'\"'* ]]; then printf 'yes'; return 0; fi|  if [[ \"\$flat\" == *\"\$want\"* ]]; then printf 'yes'; return 0; fi"

  # ── arch_match_verdict：唯一「跑得動又不報錯」的架構失敗 ──
  # **這是整份清單裡最貴的一條架構突變**：不等時回 native，就等於 qemu 模擬
  # 執行**完全靜默**。compose 起得來、健康檢查過、模型也答話，只是慢 10–100×。
  "執行：不等時回 native（**模擬執行整個靜默**）|  printf 'emulated'|  printf 'native'"
  "執行：相等時回 emulated（原生被判成模擬）|  if [[ \"\$h\" == \"\$o\" ]]; then printf 'native'; return 0; fi|  if [[ \"\$h\" == \"\$o\" ]]; then printf 'emulated'; return 0; fi"
  # 兩邊讀不到時回 native：把「不知道」講成「沒問題」。D-016 說無知不是缺陷，
  # 但它也不是「沒問題」—— 上面那兩條測試盯的就是這個第三個字。
  "執行：主機讀不到時回 native（無知當成沒問題）|  if [[ \"\$h\" == \"unknown\" ]]; then printf 'unknown'; return 0; fi|  if [[ \"\$h\" == \"unknown\" ]]; then printf 'native'; return 0; fi"
  "執行：映像讀不到時回 native（同一課的另一邊）|  if [[ \"\$o\" == \"unknown\" ]]; then printf 'unknown'; return 0; fi|  if [[ \"\$o\" == \"unknown\" ]]; then printf 'native'; return 0; fi"

  # ══════════════════════════════════════════════════════════
  # 磁碟閘門（D-058）
  # ══════════════════════════════════════════════════════════
  # 這一組的形狀與上面每一組都不同：**它的失效方向有一半是「誤擋」**。
  # 上面那些判準壞掉時大多會靜默放行（安全問題）；磁碟閘門壞掉的兩個方向
  # 都貴 —— 放行 = pull 中途失敗（有時是無聲的），誤擋 = 對著一台好好的
  # 機器說它空間不夠，而那正是這支腳本最該支援的「重跑失敗的佈署」。
  #
  # 所以下面三條是骨幹，缺一條這整套修正就等於沒做：
  #   · 表外模型靜默回一個數字（D-055 §3(2) 明文禁止的那個突變）
  #   · 缺映像／讀不到清單也當成 0（fail-open）
  #   · 漏傳 will_download 時當成「不會下載」（同一個 fail-open 的另一邊）

  # **缺陷 (1) 本體。** 少了這條分支，模型已經在本機的重跑仍然被要求 2 倍
  # 空間 —— 也就是回到修正前的行為，而所有輸出都照常。
  "磁碟需求：不會下載時仍然算模型那一份（重跑誤擋，缺陷 1 本體）|  if [[ \"\$will_download\" == \"no\" ]]; then printf '%d' \"\$base_gb\"; return 0; fi|  if false; then printf '%d' \"\$base_gb\"; return 0; fi"
  # fail-closed 的預設。漏傳參數的後果必須是「多要一點空間」，不是「靜默地
  # 不檢查」—— 前者看得到，後者要等到 pull 壞在半路才知道。
  "磁碟需求：漏傳 will_download 時當成「不會下載」（fail-open）|  if [[ \"\$will_download\" == \"no\" ]]; then printf '%d' \"\$base_gb\"; return 0; fi|  if [[ \"\$will_download\" != \"yes\" ]]; then printf '%d' \"\$base_gb\"; return 0; fi"
  # 基準是「其他消費者要多少」。空字串收斂成 0 會算出一個太小的需求。
  "磁碟需求：基準空字串當成 0（fail-open）|  if [[ ! \"\$base_gb\" =~ ^[0-9]+\$ ]]; then printf ''; return 0; fi|  base_gb=0"
  # 模型大小未知時唯一正確的答案就是空字串（呼叫端據此走 unknown 並印出來）。
  "磁碟需求：模型大小未知也照算（把不知道講成一個數字）|  if [[ \"\$(is_gb_number \"\$gb\")\" != \"yes\" ]]; then printf ''; return 0; fi|  if false; then printf ''; return 0; fi"
  # **提交 2 把那個倍數刪掉了**（真的拉一次量到 1.000），所以原本瞄準常數的
  # 突變失去了目標。它換成守著**這個結論本身**的兩條 —— 因為現在最大的風險
  # 已經不是「倍數設錯」，而是「有人覺得 2 倍比較保險，就把它加回來」。
  "磁碟需求：倍數被加回來（1.0 倍變 2 倍）|'BEGIN { printf \"%d\", g + b }'|'BEGIN { printf \"%d\", 2 * g + b }'"
  "磁碟需求：模型那一項整個被吃掉（只算基準）|'BEGIN { printf \"%d\", g + b }'|'BEGIN { printf \"%d\", b }'"
  "無條件需求：忘了加系統餘裕|  printf '%d' \"\$(( img_gb + DISK_SYSTEM_MARGIN_GB ))\"|  printf '%d' \"\$img_gb\""
  # 這是 D-058 裡最錯的那個數字（舊版 4 GB 給三個 image，實測 16.35 GB）。
  # 把它改回一個小數字，就是**把 fail-open 重新種回去**，而所有輸出都正常。
  "無條件需求：映像大小改回低估的舊值|DISK_IMAGE_GB=17|DISK_IMAGE_GB=2"
  # **這條第一版寫成 `if false`，結果是個 no-op**：往下掉會進到算術展開，
  # 而 `set -u` 讓 `$(( unknown + 2 ))` 當場失敗、輸出**也是空的** —— 與正確
  # 答案一模一樣，所以它「存活」了。症狀是「測試有洞」，其實是突變自己沒有
  # 改變行為（與 NAT 判定那條同一個形狀：倖存的突變要先懷疑突變本身）。
  # 改成直接把 unknown 收斂成 `0`，那才是我要防的那個 fail-open。
  "無條件需求：讀不到映像需求也當 0（無知變成「不需要」）|  if [[ \"\$(is_gb_number \"\$img_gb\")\" != \"yes\" ]]; then printf ''; return 0; fi|  if [[ \"\$(is_gb_number \"\$img_gb\")\" != \"yes\" ]]; then printf '0'; return 0; fi"

  # ── 映像清單：這一組的失效方向全部是「重跑誤擋」 ──
  # 缺映像卻回 0 = 全新 VPS 會在閘門說「足夠」的地方壞在半路。
  "映像需求：缺映像也回 0（fail-open）|  if [[ -z \"\$missing\" ]]; then printf '0'; return 0; fi|  if true; then printf '0'; return 0; fi"
  # 讀不到清單 ≠ 不在。讀成 no 就是對著一台映像檔都在本機的機器硬擋。
  "映像：讀不到清單當成「不在」（對著好機器誤擋）|  if [[ -z \"\$list\" ]]; then printf 'unknown'; return 0; fi|  if [[ -z \"\$list\" ]]; then printf 'no'; return 0; fi"
  "映像缺哪些：本機清單讀不到時不印 unknown|  if [[ -z \"\$local_list\" ]]; then printf 'unknown'; return 0; fi|  if false; then printf 'unknown'; return 0; fi"
  # 子串比對：ollama/ollama-x 會命中 ollama/ollama —— 與 nvidia／
  # nvidia-experimental 是同一個形狀（形狀正確、結論相反）。
  "映像：改成子串比對（ollama/ollama-x 命中 ollama/ollama）|    if [[ \"\$line\" == \"\$want\" ]]; then printf 'yes'; return 0; fi|    if [[ \"\$line\" == *\"\$want\"* ]]; then printf 'yes'; return 0; fi"

  # ── 模型大小：D-055 §3(2) 的紅線 ──
  # **這是整組裡最貴的一條。** 表外的模型靜默回一個數字，就是把「我不知道
  # 這個模型多大」變成一個看起來很確定的答案 —— 而 D-055 §3(2) 明文寫著
  # 修這個缺陷的時候不可以把它改成靜默通過。
  "模型大小：表外模型靜默回一個數字（D-055 §3(2) 明文禁止）|    qwen3-embedding:0.6b) printf '0.6'; return 0 ;;|    qwen3-embedding:0.6b) printf '0.6'; return 0 ;;"$'\n'"    *) printf '5.2'; return 0 ;;"
  "模型大小解析：沒明講時猜一個數字（而不是 unknown）|  model_gb_estimate \"\$model\"|  printf '5.2'"
  "模型大小解析：旗標被查表蓋掉（使用者明講的無效）|  if [[ -n \"\$override\" ]]; then printf '%s' \"\$override\"; return 0; fi|  if false; then printf '%s' \"\$override\"; return 0; fi"
  # 嵌入模型由 deploy-vps.sh 無條件下載；查不到的話，每一次全新佈署都會在
  # 下載前的閘門印「不知道」—— 一個永遠亮著的警告與沒有警告是一樣的。
  "模型大小：嵌入模型查不到（每次全新佈署都印不知道）|    qwen3-embedding:0.6b) printf '0.6'; return 0 ;;|    qwen3-embedding:0.6b) printf 'unknown'; return 0 ;;"

  # ── --model-gb 的驗證：參數錯誤要走 3 ──
  # 0 會讓閘門算出「需要 0 GB」—— 比不填旗標更寬鬆。那是把 fail-open
  # 做成一個選項。
  "model-gb：0 也收（把 fail-open 做成選項）|  if ! awk -v v=\"\$raw\" 'BEGIN { exit !(v > 0) }'; then printf 'not_positive'; return 0; fi|  if false; then printf 'not_positive'; return 0; fi"
  "model-gb：非數字也收|  if [[ \"\$(is_gb_number \"\$raw\")\" != \"yes\" ]]; then printf 'not_number'; return 0; fi|  if false; then printf 'not_number'; return 0; fi"
  # 上限接的是**單位錯誤**（5200 是把 5,200 MB 當成 GB）。
  "model-gb：上限不檢查（單位錯誤被放行）|  if ! awk -v v=\"\$raw\" -v m=\"\$MODEL_GB_MAX\" 'BEGIN { exit !(v <= m) }'; then|  if false; then"
  "GB 字面：空字串也算數字|  if [[ \"\$v\" =~ ^[0-9]+([.][0-9]+)?\$ ]]; then printf 'yes'; return 0; fi|  if true; then printf 'yes'; return 0; fi"
  "GB 字面：負數也算數字|  if [[ \"\$v\" =~ ^[0-9]+([.][0-9]+)?\$ ]]; then printf 'yes'; return 0; fi|  if [[ \"\$v\" =~ ^-?[0-9]+([.][0-9]+)?\$ ]]; then printf 'yes'; return 0; fi"
)

caught=0
missed=()

# 植入器寫成檔案，而不是每次在 `if` 裡塞一個 heredoc —— bash 解析不了
# `if ! VAR="$(cmd <<'PY' ... PY)"` 那種組合（實際踩過，錯誤訊息只說
# 「syntax error near unexpected token `then'」，不會告訴你是 heredoc）。
cat > "$WORK/inject.py" <<'PY'
import ast, io, os, subprocess, sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
n = src.count(old)
if n != 1:
    # 兩種原因要分開講。合併成一句「不是唯一字串」會誤導：目標字串裡
    # 多寫了一個 `|` 時欄位會被切錯，症狀是**找不到**，但訊息會說
    # 「不是唯一」，於是會去翻原始碼找重複，而該改的是清單那一行。
    print("NOT_FOUND" if n == 0 else "NOT_UNIQUE:%d" % n, file=sys.stderr)
    sys.exit(1)
mutated = src.replace(old, new, 1)

# 植入之後還要再過一關，因為「測試變紅」不等於「有斷言守住」。這一關擋的
# 是**假抓到**，它有兩張臉，而且都不會讓任何一條斷言叫起來：
#   NO_OP       取代沒有改變語意（`if slack < -0:` 就是 `if slack < 0:`）。
#               它殺不掉任何東西，卻在報告裡佔一個「抓到」的名額 —— 更糟
#               的是它讓一份「全部抓到」看起來很完整。`ast.dump` 不比對
#               註解與空白，所以只改註解的那種也會在這裡現形。
#   BROKEN_CODE 植入後根本不是合法程式。測試當然會失敗，但那是模組匯入
#               不了，不是判準被守住。真的混進來過：目標行裡的 `||` 被
#               當成欄位分隔符，以及雙引號裡的字面 `\n`。
# 兩者都只說明**這條突變寫壞了**，原始碼沒有問題。
ext = os.path.splitext(path)[1]
if ext == ".py":
    try:
        before = ast.dump(ast.parse(src))
        after = ast.dump(ast.parse(mutated))
    except SyntaxError as e:
        print("BROKEN_CODE:%s" % e.msg, file=sys.stderr)
        sys.exit(1)
    if before == after:
        print("NO_OP", file=sys.stderr)
        sys.exit(1)
elif ext == ".sh":
    # shell 的等價性在這裡判不了（那要真的比語意），所以只驗語法。
    # 走 stdin 而不是暫存檔：`bash -n` 的訊息就會是乾淨的 `line N`，
    # 不必把一個臨時路徑印進錯誤訊息裡。
    p = subprocess.run(["bash", "-n"], input=mutated,
                       capture_output=True, text=True)
    if p.returncode != 0:
        last = (p.stderr.strip().splitlines() or ["bash -n 失敗"])[-1]
        print("BROKEN_CODE:%s" % last, file=sys.stderr)
        sys.exit(1)

io.open(path, "w", encoding="utf-8").write(mutated)
PY

# 每一條都必須**恰好**兩個 `|`（label|old|new）。多寫一個就會被切錯，而且
# 症狀是**靜默**的：`old` 被截短、`new` 從 `|` 開始，植入的是一段壞掉的
# 程式碼 —— 測試當然會失敗，於是它被算進「抓到」，但它守不住任何判準。
# 這一條真的抓到過一條：目標行裡的 `||` 被當成了分隔符。
BADFIELDS=""
for m in "${MUTANTS[@]}"; do
  pipes="${m//[^|]/}"                      # 只留 `|`，數它有幾個
  [[ ${#pipes} -eq 2 ]] || BADFIELDS+="  ${m%%|*}"$'\n'
done
if [[ -n "$BADFIELDS" ]]; then
  fail "突變清單有條目的 \`|\` 不是剛好兩個 —— 欄位會被切錯，植入的是壞掉的程式碼："
  printf '%s' "$BADFIELDS"
  exit 3
fi

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  # 先還原兩份目標檔，再依 label 的前綴決定要動哪一份、跑哪一份測試。
  cp "$MODULE" "$WORK/deploy-vps-decisions.sh"
  cp "$TEST" "$WORK/test_deploy_vps_decisions.sh"
  cp "$DEPLOY" "$WORK/deploy-vps.sh"
  cp "$PROBE" "$WORK/deploy_smoke_probe.py"
  cp "$PROBE_TEST" "$WORK/test_deploy_smoke_probe.py"
  cp "$LIFECYCLE" "$WORK/profile-lifecycle.sh"
  cp "$LIFECYCLE_TEST" "$WORK/test_profile_lifecycle.sh"

  case "$label" in
    py:*)
      label="${label#py:}"
      TARGET="$WORK/deploy_smoke_probe.py"
      SRCFILE="$PROBE" ;;
    pl:*)
      label="${label#pl:}"
      TARGET="$WORK/profile-lifecycle.sh"
      SRCFILE="$LIFECYCLE" ;;
    *)
      TARGET="$WORK/deploy-vps-decisions.sh"
      SRCFILE="$MODULE" ;;
  esac

  INJECT_MSG="$(python3 "$WORK/inject.py" "$TARGET" "$old" "$new" 2>&1 >/dev/null)" && : || {
    case "$INJECT_MSG" in
      NOT_FOUND)
        fail "植入失敗：$label —— 目標字串在 $SRCFILE 裡**找不到**"
        fail "  兩個常見原因：(1) 原始碼改了，這條突變對不上（要更新這支腳本）" \
             "；(2) 目標字串裡有 \`|\`，欄位被切錯了（目標行必須不含 \`|\`）" ;;
      NOT_UNIQUE:*)
        fail "植入失敗：$label —— 目標字串在 $SRCFILE 裡出現 ${INJECT_MSG#NOT_UNIQUE:} 次"
        fail "  原始碼改了，這條突變對不上（要更新這支腳本，不是更新原始碼）" ;;
      NO_OP)
        fail "植入失敗：$label —— 取代**沒有改變語意**，這條突變殺不掉任何斷言"
        fail "  這不是「很弱的突變」，是沒有突變。掃描會把它算進「抓到」，但它是空的" ;;
      BROKEN_CODE:*)
        fail "植入失敗：$label —— 植入之後**根本不是合法程式**（${INJECT_MSG#BROKEN_CODE:}）"
        fail "  測試會變紅，但那是跑不起來，不是判準守住了。原始碼沒問題，是這條突變寫壞了" ;;
      *)
        fail "植入失敗：$label（$INJECT_MSG）" ;;
    esac
    missed+=("$label（植入失敗）")
    continue
  }

  # 測試必須**失敗**才算抓到。跑哪一支由上面那個 case 決定。
  case "$TARGET" in
    *.py)                 TESTCMD=(python3 "$WORK/test_deploy_smoke_probe.py") ;;
    *profile-lifecycle.sh) TESTCMD=(bash "$WORK/test_profile_lifecycle.sh") ;;
    *)                    TESTCMD=(bash "$WORK/test_deploy_vps_decisions.sh") ;;
  esac
  if "${TESTCMD[@]}" >/dev/null 2>&1; then mut_rc=0; else mut_rc=1; fi

  if [[ "$mut_rc" -eq 0 ]]; then
    fail "漏掉！$label —— 判準壞成這樣，測試還是通過了"
    missed+=("$label")
  else
    ok "抓到  $label"
    caught=$((caught + 1))
  fi
done

echo
total="${#MUTANTS[@]}"
if [[ "${#missed[@]}" -gt 0 ]]; then
  fail "有 ${#missed[@]} 個突變沒被抓到，共 $total 個 —— 測試有洞："
  for m in "${missed[@]}"; do fail "  • $m"; done
  exit 1
fi

ok "$caught/$total 個突變全數被抓到，且三份對照組都通過 —— 綁定預設、閘門四態、資源門檻、記憶體狀態的兩個 unknown、context 的 +1、GPU 的每一個靜默降級、架構三層（含「模擬執行整個靜默」與「讀不到就誤擋」兩個方向）、.env 的每條邊界、煙霧測試的四條斷言，以及殘留容器差集的兩個方向都有測試守著"
