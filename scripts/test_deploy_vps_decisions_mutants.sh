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
#   · context 門檻的 +1 → D-027 的結論本身，差一個 token 就是砍掉 1 個 token
#   · .env 的每一條邊界 → 改的是使用者唯一的設定檔，且失敗後會被重跑
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
LIB="$SCRIPT_DIR/lib.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 測試自己會去 source（bash）或 import（python）同目錄的受測檔案，所以
# 每一份都要在 $WORK。lib.sh 不突變 —— 只放在那裡讓決定模組的測試找得到
# （它的 PROJECT_ROOT 在那個測試裡用不到，沒有任何函式會讀它）。
cp "$LIB" "$WORK/lib.sh"
cp "$MODULE" "$WORK/deploy-vps-decisions.sh"
cp "$TEST" "$WORK/test_deploy_vps_decisions.sh"
cp "$PROBE" "$WORK/deploy_smoke_probe.py"
cp "$PROBE_TEST" "$WORK/test_deploy_smoke_probe.py"

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
ok "對照組通過（兩份測試都不是永遠失敗）"
echo

# 突變清單：label|原字串|取代字串
# 原字串必須在該目標檔案裡**唯一**出現一次，而且**不含 `|`**（見開頭的說明）。
#
# label 的前綴決定突變哪一份、跑哪一份測試：
#   `py:` → deploy_smoke_probe.py ／ test_deploy_smoke_probe.py
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
  "NAT 判定：不確定的也當成命中|  printf ''|  printf 'unknown'"}

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

  # ── 估算與需求 ──
  "模型大小：4b 查到錯的數字|    qwen3:4b)   printf '2.5'; return 0 ;;|    qwen3:4b)   printf '0.0'; return 0 ;;"
  "磁碟需求：忘了 pull 要兩倍空間|  awk -v g=\"\$gb\" 'BEGIN { printf \"%d\", 2 * g + 4 }'|  awk -v g=\"\$gb\" 'BEGIN { printf \"%d\", g + 4 }'"
  "記憶體需求：KV cache 那項不算進去|    'BEGIN { printf \"%d\", g * 1024 + (c * kv) / 1024 + 2048 }'|    'BEGIN { printf \"%d\", g * 1024 + 2048 }'"

  # ── ctx_meets_mem0：D-027 的結論本身 ──
  "context：門檻忘了 +1（>= 寫成 >）|  if (( n >= MEM0_ADD_MIN_CTX )); then printf 'yes'; return 0; fi|  if (( n > MEM0_ADD_MIN_CTX )); then printf 'yes'; return 0; fi"
  "context：門檻常數少一|MEM0_ADD_MIN_CTX=8101|MEM0_ADD_MIN_CTX=8100"

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
  "py:煙霧：不固定 temperature|        \"options\": {\"temperature\": 0, \"num_predict\": num_predict},|        \"options\": {\"temperature\": 1, \"num_predict\": num_predict},"
  # **把 `think: false` 加回去。** 這是實際踩過的坑：它不會關掉推理，只把
  # 推理從 `thinking` 搬進 `content`，於是「content 非空」可以被推理前言
  # 單獨滿足 —— 一個從頭到尾自言自語、從沒回答的模型也會過關。
  "py:煙霧：把 think=false 加回去（推理會搬進 content）|        \"stream\": False,|        \"stream\": False,\n        \"think\": False,"
  # 預設預算被調回一個註定不夠的值：實測推理要 152 個 token，32 會全部
  # 被吃掉，然後 done_reason=length 被讀成「上游變了」—— 假失敗（D-016）。
  #
  # **要突變的是預算，不是量到的長度。** 第一版這條寫成把
  # MEASURED_REASONING_TOKENS 調小，結果它存活了 —— 因為把量到的數字調小
  # 只會讓 `預算 > 量測` 更容易成立。那暴露了測試的一個真弱點（見底下
  # test_smoke_default_budget_covers_a_reasoning_model 的範圍斷言）。
  "py:煙霧：預設預算調回 32（推理會吃掉全部預算）|DEFAULT_NUM_PREDICT = 512|DEFAULT_NUM_PREDICT = 32"
  # HTTP 錯誤是「這支探針送的請求有問題」，叫人去查模型等於指錯方向。
  "py:煙霧：HTTP 錯誤當成模型失敗|        return EXIT_BROKEN, (|        return EXIT_FAIL, ("
  # ── 結束碼的對應：環境 ≠ 上游 ≠ 探針自己壞了（D-018）──
  "py:煙霧：模型沒拉當成探針壞了|        if \"not found\" in low or \"no such model\" in low:|        if False:"
  "py:參數：錯誤回 2 而不是 3|    return EXIT_BROKEN  # 參數錯誤是「這支腳本自己壞了」，不是「環境無法判定」|    return EXIT_INDETERMINATE  # 參數錯誤是「這支腳本自己壞了」，不是「環境無法判定」"
  # ── ctx_verdict：一個設了卻沒生效的開關 ──
  "py:ctx：不符時當成相符|        if got == expected:|        if True:"
  "py:ctx：名字不正規化（未帶 tag 的會被誤報）|    want = normalize_model(model)|    want = model"
  "py:ctx：不挑模型，用第一筆|        if normalize_model(name) != want:|        if False:"
  # 這一條是**設計決定**本身：num_ctx 沒生效走 2 不走 1。改了它，
  # run_ctx 就再也沒有告訴任何人「堆疊是好的、只是這個設定沒生效」。
  "py:ctx：沒生效當成判準沒過（回 1）|EXIT_CTX_NOT_APPLIED = EXIT_INDETERMINATE|EXIT_CTX_NOT_APPLIED = EXIT_FAIL"
)

caught=0
missed=()

# 植入器寫成檔案，而不是每次在 `if` 裡塞一個 heredoc —— bash 解析不了
# `if ! VAR="$(cmd <<'PY' ... PY)"` 那種組合（實際踩過，錯誤訊息只說
# 「syntax error near unexpected token `then'」，不會告訴你是 heredoc）。
cat > "$WORK/inject.py" <<'PY'
import io, sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
n = src.count(old)
if n != 1:
    # 兩種原因要分開講。合併成一句「不是唯一字串」會誤導：目標字串裡
    # 多寫了一個 `|` 時欄位會被切錯，症狀是**找不到**，但訊息會說
    # 「不是唯一」，於是會去翻原始碼找重複，而該改的是清單那一行。
    print("NOT_FOUND" if n == 0 else "NOT_UNIQUE:%d" % n, file=sys.stderr)
    sys.exit(1)
io.open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
PY

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  # 先還原兩份目標檔，再依 label 的前綴決定要動哪一份、跑哪一份測試。
  cp "$MODULE" "$WORK/deploy-vps-decisions.sh"
  cp "$TEST" "$WORK/test_deploy_vps_decisions.sh"
  cp "$PROBE" "$WORK/deploy_smoke_probe.py"
  cp "$PROBE_TEST" "$WORK/test_deploy_smoke_probe.py"

  case "$label" in
    py:*)
      label="${label#py:}"
      TARGET="$WORK/deploy_smoke_probe.py"
      SRCFILE="$PROBE" ;;
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
      *)
        fail "植入失敗：$label（$INJECT_MSG）" ;;
    esac
    missed+=("$label（植入失敗）")
    continue
  }

  # 測試必須**失敗**才算抓到。跑哪一支由上面那個 case 決定。
  if [[ "$TARGET" == *.py ]]; then
    python3 "$WORK/test_deploy_smoke_probe.py" >/dev/null 2>&1 && mut_rc=0 || mut_rc=1
  else
    bash "$WORK/test_deploy_vps_decisions.sh" >/dev/null 2>&1 && mut_rc=0 || mut_rc=1
  fi

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

ok "$caught/$total 個突變全數被抓到，且兩份對照組都通過 —— 綁定預設、閘門四態、資源門檻、context 的 +1、.env 的每條邊界，以及煙霧測試的四條斷言都有測試守著"
