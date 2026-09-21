#!/usr/bin/env bash
# 突變測試：把第三階段 item 4／5／6 的判準**故意弄壞**，確認對應的離線測試會叫。
#
# 為什麼需要（D-024 第八節）：一組「永遠通過」的測試和「永遠失敗」的測試一樣
# 沒用。唯一能證明測試有效的方法，是讓它面對一個已知的錯誤，然後看它有沒有叫。
#
# 這份清單裡每一條守的東西，以及**為什麼它值得一條突變**：
#
#   item 4 的 headroom —— `limit = super-step + 1` 的那個 +1 是量到的，不是
#     推的。它是這一項唯一的實質結論，改掉它就等於改掉結論本身。
#   item 4 的 tight 邊界 —— `slack == 0` 與 `slack < 0` 分界。少了它，
#     一個把「零餘裕」講成「不夠」的實作會全綠，而它會叫使用者把一個
#     **跑得完**的 limit 調大。
#   item 4 的「做完了才被擋」—— 那個判定有**兩個**條件（已完成數 == 節點數，
#     **且**沒有下一步）。這兩條各有一個突變，因為把它們併成一個就會讓
#     「還差得遠」被講成「只差最後一步」，而後者會叫人把 limit 調小。
#   item 6 的 grace 邊界 —— `>` 與 `>=`。量到的邊界正好落在
#     `lateness == grace`，所以這一條**只能**被那個邊界案例殺死。
#   item 6 的 memory store —— 把「重啟就沒了」講成其他任何一種，都是把
#     README 已經寫對的那句話弄丟。
#   item 6 的 lambda 拒收 —— 持久化 store 的代價。寫成「都收」的話，
#     使用者會在第一次真的排一個閉包時才發現，而那可能是上線之後。
#   item 5 的 ephemeral —— **這一條是整份清單裡最重要的。** 把暫存目錄
#     讀成 durable，等於告訴下一個人「那個向量庫有備份、要遷移」，
#     而實際上它每次跑完就沒了。那正是 README 原本那句話的錯誤前提。
#   item 5 的 unknown —— 讀不到不等於安全（D-029 同一個形狀）。
#   item 5 的兩個門檻邊界 —— `>=` 與 `<`。改一個字就會讓「剛好放得下」
#     變成「放不下」，或讓一個 100MiB 的儲存被講成可忽略。
#
# 做法：把兩份模組與兩份測試複製到暫存目錄、用字串取代植入突變，
# 從那份副本跑測試。原始檔從頭到尾不被修改。
#
# 用法：bash scripts/test_phase3_mutants.sh
#
# 結束碼：0 = 每個突變都被抓到，且對照組通過
#         1 = 有突變沒被抓到（測試有洞）、或對照組失敗（測試壞了）
#         3 = 找不到受測檔案

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/phase3_runtime_probe.py"
PROBE_TEST="$SCRIPT_DIR/test_phase3_runtime_probe.py"
STORAGE="$SCRIPT_DIR/phase3-storage-decisions.sh"
STORAGE_TEST="$SCRIPT_DIR/test_phase3_storage_decisions.sh"

for f in "$PROBE" "$PROBE_TEST" "$STORAGE" "$STORAGE_TEST"; do
  if [[ ! -r "$f" ]]; then
    echo "[3] 找不到受測檔案：$f" >&2
    exit 3
  fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ok()   { printf '  ✓ %s\n' "$*"; }
info() { printf '\n── %s ──\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*"; }

restore() {
  cp "$PROBE"       "$WORK/phase3_runtime_probe.py"
  cp "$PROBE_TEST"  "$WORK/test_phase3_runtime_probe.py"
  cp "$STORAGE"     "$WORK/phase3-storage-decisions.sh"
  cp "$STORAGE_TEST" "$WORK/test_phase3_storage_decisions.sh"
}

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

echo "======================================================================"
echo "第三階段 item 4／5／6 突變測試"

# ── 對照組先跑：不動任何東西，兩份測試都必須通過 ────────
# 沒有這一步，下面「每個突變都被抓到」可能只是因為測試**永遠失敗**。
restore
info "對照組（無突變）"
if ! python3 "$WORK/test_phase3_runtime_probe.py" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_phase3_runtime_probe.py 本身有問題，先修它"
  exit 1
fi
if ! bash "$WORK/test_phase3_storage_decisions.sh" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_phase3_storage_decisions.sh 本身有問題，先修它"
  exit 1
fi
ok "對照組通過（兩份測試都不是永遠失敗）"

# 突變清單：label|原字串|取代字串
# 原字串必須在該目標檔案裡**唯一**出現一次，而且**不含 `|`**。
# label 前綴決定動哪一份、跑哪一份測試：
#   `py:` → phase3_runtime_probe.py ／ test_phase3_runtime_probe.py
#   `sh:` → phase3-storage-decisions.sh ／ test_phase3_storage_decisions.sh
MUTANTS=(
  # ══════════════════════════════════════════════════════════
  # item 4：recursion_limit
  # ══════════════════════════════════════════════════════════
  "py:item4：headroom 拿掉（= 所需 limit 就是 super-step 數）|    return super_steps + RECURSION_LIMIT_HEADROOM|    return super_steps"
  "py:item4：headroom 常數改成 0|RECURSION_LIMIT_HEADROOM = 1|RECURSION_LIMIT_HEADROOM = 0"
  # 零餘裕被講成不夠 → 會叫使用者把一個跑得完的 limit 調大（假失敗，D-016）
  "py:item4：tight 被併進 blocks（slack==0 判成不夠）|    if slack < 0:|    if slack <= 0:"
  # 反方向：不夠被講成零餘裕 → 一個跑不完的計畫被說成跑得完（fail-open）
  "py:item4：blocks 被併進 tight（slack<0 判成零餘裕）|    if slack < 0:|    if slack < -0:"
  # 負數不再拋 → 靜默回一個看起來合理的值
  "py:item4：super_steps 負數不再拋|    if super_steps < 0:|    if False:"
  # 「工作全部做完卻被擋」的判定有**兩個**條件（已完成數 == 節點數，**且**沒有下一步）。
  # 拿掉第二個，一個「還有下一步要跑」的中止點就會被講成「做完了卻被擋」——
  # 而那一句話會讓下一個人以為 limit 只差最後一步，實際上它還差得遠。
  "py:item4：all_done 只看已完成數，不看還有沒有下一步|    all_done = worst[\"completed\"] == node_count and not worst[\"next\"]|    all_done = worst[\"completed\"] == node_count"
  # 取第一列而不是最大的中止 limit → 掃描裡前面那些「很緊所以一定中止」的列
  # 會蓋掉真正的邊界，結論變成「limit=1 就做完了全部」這種荒謬值。
  "py:item4：取第一列的中止點而不是最大的|    worst = max(aborted, key=lambda r: r[\"limit\"])|    worst = aborted[0]"
  # 「掃描裡找不到邊界」被講成「邊界顯示最糟的情況」= fail-open。
  # 量不到不可以被讀成量到了什麼（D-029 同一個形狀）。
  # 注意要改的是 **verdict 那一行**，不是 message —— `"verdict": "unknown",`
  # 在檔案裡出現兩次，所以舊字串要連著下一行一起寫才唯一。
  "py:item4：找不到邊界（unknown）被講成 all_work_then_abort|        return {\"verdict\": \"unknown\",
                \"message\": \"掃描範圍內沒有任何 limit 中止|        return {\"verdict\": \"all_work_then_abort\",
                \"message\": \"掃描範圍內沒有任何 limit 中止"
  # 理由講不出「工作做完了卻被擋」→ 它跟 partial 沒有可辨識的差別（D-026 第五節）
  "py:item4：all_work 的理由不再說出全部做完|                       f\"守衛在工作全部做完之後才響。\"|                       f\"守衛在最後才響。\""

  # ══════════════════════════════════════════════════════════
  # item 6：APScheduler
  # ══════════════════════════════════════════════════════════
  # README 已經寫對的那句話：memory store 重啟即消失。弄丟它等於把
  # 一個已經量過的結論退回未量測。
  "py:item6：記憶體 store 不再判 lost|        raise ValueError(f\"lateness_seconds 不能是負數：{lateness_seconds}\")

    if store_kind == \"memory\":|        raise ValueError(f\"lateness_seconds 不能是負數：{lateness_seconds}\")

    if False:"
  # 邊界：lateness == grace 是「剛好趕上」。改成 >= 會讓一個會跑的 job
  # 被講成被丟掉 —— 這一條只能被那個等號案例殺死。
  "py:item6：grace 邊界改成 >=（剛好趕上被判成丟掉）|    if lateness_seconds > grace_seconds:|    if lateness_seconds >= grace_seconds:"
  # 不設寬限被當成不補跑 → 一個明確關掉寬限的排程會被講成永遠不跑
  "py:item6：grace=None 被當成會丟|    if grace_seconds is None:|    if False:"
  # 逾期不再丟 → 把「靜默丟掉」這個發現本身弄不見
  "py:item6：逾期不再判 dropped|    if lateness_seconds > grace_seconds:|    if False:"
  # 持久化 store 什麼都收 → 使用者會在第一次排閉包時才發現
  "py:item6：持久化 store 不再拒收 lambda|    if is_module_level:|    if True:"
  # store_kind 打錯靜默當成 memory → 呼叫端傳錯字串不會被發現
  "py:item6：store_kind 打錯不再拋|    if store_kind not in (\"memory\", \"persistent\"):
        raise ValueError(f\"store_kind 只能是 memory/persistent，收到 {store_kind!r}\")
    if store_kind == \"memory\":
        return {
            \"accepted\": True,|    if False:
        raise ValueError(f\"store_kind 只能是 memory/persistent，收到 {store_kind!r}\")
    if store_kind == \"memory\":
        return {
            \"accepted\": True,"

  # ══════════════════════════════════════════════════════════
  # item 5：儲存與磁碟
  # ══════════════════════════════════════════════════════════
  # **最重要的一條。** 把暫存目錄讀成 durable，等於告訴下一個人
  # 「那個向量庫有備份、要遷移」—— 而它每次跑完就沒了。
  "sh:item5：暫存目錄被讀成 durable（item 5 的錯誤前提本身）|      echo \"ephemeral\" ;;|      echo \"durable\" ;;"
  # down -v 會刪掉 named volume。讀成 durable 會讓人以為備份策略涵蓋它。
  "sh:item5：named-volume 被讀成完全 durable（忽略 down -v）|      echo \"durable-restart-only\" ;;|      echo \"durable\" ;;"
  # 讀不到當成 durable = fail-open（D-029 同一個形狀）
  "sh:item5：未知掛載型態被讀成 durable|      echo \"unknown\" ;;|      echo \"durable\" ;;"
  # ephemeral 的東西不管多大都不是一個 store
  "sh:item5：暫存目錄被當成一般的 store|  if [[ \"\$(store_durability_verdict \"\$kind\")\" == \"ephemeral\" ]]; then|  if false; then"
  # 100MiB 門檻的邊界：改成 <= 會讓「剛好 100MiB」被講成可忽略
  "sh:item5：100MiB 門檻改成 <=（邊界）|  if (( size < 104857600 )); then|  if (( size <= 104857600 )); then"
  # avail >= new 的邊界：改成 > 會讓「剛好放得下」被擋下來
  "sh:item5：avail>=new 改成 >（剛好放得下被判不夠）|  if (( avail_mb >= new_mb )); then|  if (( avail_mb > new_mb )); then"
  # reclaim 的邊界：改成 > 會讓「回收量剛好夠」被講成沒救
  "sh:item5：reclaim>=new 改成 >（回收剛好夠被判 blocked）|  if (( reclaim_mb >= new_mb )); then|  if (( reclaim_mb > new_mb )); then"
  # 非數字被當成 0 → 「讀不到」變成「放得下」
  "sh:item5：非數字不再判 unknown|    [[ \"\$n\" =~ ^[0-9]+\$ ]] |||    [[ -n \"\$n\" ]] ||"
)

CAUGHT=0
MISSED=()

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  restore

  case "$label" in
    py:*) label="${label#py:}"
          TARGET="$WORK/phase3_runtime_probe.py"
          SRCFILE="$PROBE"
          TESTCMD=(python3 "$WORK/test_phase3_runtime_probe.py") ;;
    sh:*) label="${label#sh:}"
          TARGET="$WORK/phase3-storage-decisions.sh"
          SRCFILE="$STORAGE"
          TESTCMD=(bash "$WORK/test_phase3_storage_decisions.sh") ;;
    *)    fail "清單第 '${label}' 條的前綴不認識（只能是 py: 或 sh:）"
          MISSED+=("$label（前綴錯誤）"); continue ;;
  esac

  INJECT_MSG="$(python3 "$WORK/inject.py" "$TARGET" "$old" "$new" 2>&1 >/dev/null)" && : || {
    case "$INJECT_MSG" in
      NOT_FOUND)
        fail "植入失敗：$label —— 目標字串在 $(basename "$SRCFILE") 裡**找不到**"
        fail "  兩個常見原因：(1) 原始碼改了，這條突變對不上（要更新這支腳本）" \
             "；(2) 目標字串裡有 \`|\`，欄位被切錯了（目標行必須不含 \`|\`）" ;;
      NOT_UNIQUE:*)
        fail "植入失敗：$label —— 目標字串在 $(basename "$SRCFILE") 裡出現 ${INJECT_MSG#NOT_UNIQUE:} 次"
        fail "  原始碼改了，這條突變對不上（要更新這支腳本，不是更新原始碼）" ;;
      *)
        fail "植入失敗：$label（$INJECT_MSG）" ;;
    esac
    MISSED+=("$label（植入失敗）")
    continue
  }

  # 測試必須**失敗**才算抓到。
  if "${TESTCMD[@]}" >/dev/null 2>&1; then mut_rc=0; else mut_rc=1; fi

  if [[ "$mut_rc" -eq 0 ]]; then
    fail "漏掉！$label —— 判準壞成這樣，測試還是通過了"
    MISSED+=("$label")
  else
    ok "抓到  $label"
    CAUGHT=$((CAUGHT + 1))
  fi
done

echo
TOTAL="${#MUTANTS[@]}"
echo "======================================================================"
if [[ "${#MISSED[@]}" -gt 0 ]]; then

  printf '  ✗ 有 %d 個突變沒被抓到，共 %d 個 —— 測試有洞：\n' "${#MISSED[@]}" "$TOTAL"
  for m in "${MISSED[@]}"; do printf '      • %s\n' "$m"; done
  exit 1
fi
printf '  ✓ %d/%d 個突變全數被抓到，且兩份對照組都通過 —— item 4 的 +1、tight 邊界與「做完了才被擋」的兩個條件、item 6 的 grace 邊界與記憶體 store 與 lambda 拒收、item 5 的 ephemeral 與兩個門檻邊界，都有測試守著\n' "$CAUGHT" "$TOTAL"
exit 0
