#!/usr/bin/env bash
# 測 scripts/probe_traceability.sh。
#
# 為什麼這支要存在：那個守衛守的是**這份專案所有數字的前提** ——
# 「這一輪是哪一版探針跑的」。它自己壞掉的時候不會有人發現，因為它壞掉的
# 方式是把一個壞掉的東西講成好的（或反過來）：
#
#   · 讀不到雜湊時說「變了」   → 假失敗，跑了好幾小時的數字被丟掉
#   · 讀不到雜湊時說「沒變」   → 把「沒守到」講成「守住了」**而且報告很漂亮**
#   · changed 不蓋過 2         → 儀器被換掉的那一輪，結束碼看起來像「設計如此」
#
# 三種都不會讓任何既有的測試變紅。所以每個分支都要有自己的斷言，而且斷言
# 盯的是**回傳的那個字**（它就是要給呼叫端讀的可區辨字串，D-026 第五節）。
#
# 最後再加兩個突變（與 test_ollama_log_corroboration.sh 同一個做法）：
# 少了它們，一組「永遠回 stable」的實作也會全綠。
#
# 用法：bash scripts/test_probe_traceability.sh
# 結束碼：0 = 全過；1 = 有案例沒過；3 = 受測模組 source 不進來

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="$SCRIPT_DIR/probe_traceability.sh"

if [[ ! -r "$MODULE" ]]; then
  fail "找不到受測模組：$MODULE"
  exit 3
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── source 的副作用 ─────────────────────────────────────
# 這個模組被四支 verify-*.sh source（還有一支測試），它不可以在被 source 時
# 就開始做事、也不可以改動呼叫端的 shell 選項。這一條是斷言，不是客套：
# source 進來就 `set -e` 的模組會讓呼叫端的 `|| rc=$?` 全部失效。
_OPTS_BEFORE="$-"
# shellcheck source=/dev/null
source "$MODULE"
_OPTS_AFTER="$-"

PASS=0
FAILED=0

check() {   # <標籤> <期望> <實際>
  local label="$1" want="$2" got="$3"
  if [[ "$want" != "$got" ]]; then
    fail "$label —— 預期 $(printf '%q' "$want")，得到 $(printf '%q' "$got")"
    FAILED=$((FAILED + 1))
  else
    PASS=$((PASS + 1))
    ok "$label"
  fi
}

echo "source 本身"
check "source 不改動呼叫端的 shell 選項" "$_OPTS_BEFORE" "$_OPTS_AFTER"
check "source 不該開啟 errexit" "${_OPTS_AFTER#*e}" "${_OPTS_AFTER#*e}"
echo

# 兩個看起來像真的、但不同的雜湊。用真的 sha256 形狀（64 個十六進位），
# 因為長度不對的字串會讓「這個函式有沒有在驗形狀」變成一個沒被測到的假設。
A=ec7e991e07ce707a1111111111111111111111111111111111111111111111
B=ec7e991e0700000000000000000000000000000000000000000000000000000

echo "probe_stable_verdict：三態"
# 這一條就是守衛的正常路徑：跑前跑後同一個雜湊 —— D-036 那一輪就是這樣，
# 探針自報 ec7e991e07ce707a，與 git show 的那一版相符。
check "沒變 → stable" stable "$(probe_stable_verdict "$A" "$A")"
check "變了 → changed" changed "$(probe_stable_verdict "$A" "$B")"

# 空字串是「算不出來」。這三條守的是**假失敗**那一面：讀不到檔就喊儀器被
# 換掉，會讓一整輪幾小時的數字被丟掉，而那正是 D-016 說的最糟的錯。
check "跑之前讀不到 → unknown（不是 changed）" unknown "$(probe_stable_verdict "" "$A")"
check "跑之後讀不到 → unknown（不是 changed）" unknown "$(probe_stable_verdict "$A" "")"
check "兩次都讀不到 → unknown" unknown "$(probe_stable_verdict "" "")"
# 反過來那一面：unknown 不可以被當成 stable，否則「沒守到」讀起來像「守住了」。
if [[ "$(probe_stable_verdict "" "$A")" == "stable" ]]; then
  fail "unknown 被當成 stable —— 這是最危險的錯，報告會看起來一切正常"
  FAILED=$((FAILED + 1))
else
  PASS=$((PASS + 1))
  ok "unknown ≠ stable（沒守到不會讀成守住了）"
fi
echo

echo "probe_guard_verdict：changed 無條件蓋成 3"
# 四個探針結束碼各一條。`changed` 全部要變 3 —— 包含 0：一份追不回修訂版
# 的「通過」是假的通過，那正是這個守衛存在的理由。
check "0 ／ changed → 3（假的通過不可以留著）" 3 "$(probe_guard_verdict 0 changed)"
check "1 ／ changed → 3" 3 "$(probe_guard_verdict 1 changed)"
check "2 ／ changed → 3（蓋過 --sections 的設計值）" 3 "$(probe_guard_verdict 2 changed)"
check "3 ／ changed → 3" 3 "$(probe_guard_verdict 3 changed)"
echo

echo "probe_guard_verdict：stable 與 unknown 不改結束碼"
check "0 ／ stable → 0" 0 "$(probe_guard_verdict 0 stable)"
check "2 ／ stable → 2（分段重跑仍然是 2）" 2 "$(probe_guard_verdict 2 stable)"
# unknown 只警告，不改結束碼（D-016）。這一條與上面「unknown ≠ stable」
# 是一對：那條守的是判定本身，這條守的是呼叫端拿它做的事。
check "0 ／ unknown → 0（讀不到不等於變了）" 0 "$(probe_guard_verdict 0 unknown)"
check "2 ／ unknown → 2（分段重跑不會因為讀不到雜湊就升級）" 2 "$(probe_guard_verdict 2 unknown)"
echo

# ── 突變 1：stable 的判準極性反轉 ────────────────────────
# 把 `==` 改成 `!=`。這是最貴的一種壞法：一切照常，只是每一輪的結論都
# 反過來 —— 沒被改過的那一輪說 changed（假失敗），被改過的那一輪說 stable
# （假通過）。上面的斷言是唯一會叫起來的地方。
# sed 的腳本用單引號包住（`$` 要寫成 `\$`、`|` 要寫成 `\|`），而**取代字串
# 裡不可以出現單引號** —— 所以這裡把 `printf 'stable'` 寫成 `printf "stable"`，
# 對 bash 而言完全等價，卻讓整個 sed 腳本可以待在單引號裡。用 `'"'"'` 硬接
# 也行，但那是在一個本來就容易寫錯的地方再加一層容易寫錯的東西。
# `.stable.` 的三個點是 sed 的萬用字元，對應原始碼裡的單引號。
sed 's|if \[\[ "\$1" == "\$2" \]\]; then printf .stable.; return 0; fi|if [[ "\$1" != "\$2" ]]; then printf "stable"; return 0; fi|' \
    "$MODULE" > "$WORK/mutated-polarity.sh"
if ! grep -qF '"$1" != "$2"' "$WORK/mutated-polarity.sh"; then
  fail "突變植入失敗 —— sed 的目標字串不在 $MODULE 裡（改了模組就要更新這支腳本）"
  FAILED=$((FAILED + 1))
else
  # shellcheck source=/dev/null
  source "$WORK/mutated-polarity.sh"
  if [[ "$(probe_stable_verdict "$A" "$A")" == "changed" ]]; then
    PASS=$((PASS + 1))
    ok "突變被抓到（stable 極性反轉 → 沒變的那一輪會說 changed）"
  else
    FAILED=$((FAILED + 1))
    fail "突變沒被抓到 —— 極性反轉還是全綠，代表上面那組斷言沒有真的在比對回傳值"
  fi
fi

# ── 突變 2：changed 不再蓋過結束碼 ───────────────────────
# 這一條守的是**決定**那一半，不是判定那一半：判定說 changed，而呼叫端
# 照樣回 0。症狀是「儀器被換掉的那一輪，結束碼看起來像通過」—— 沒有任何
# 既有斷言會發現，因為判定本身還是對的。
sed 's|if \[\[ "\$2" == "changed" \]\]; then printf .3.; return 0; fi|if false; then printf "3"; return 0; fi|' \
    "$MODULE" > "$WORK/mutated-override.sh"
if ! grep -qF 'if false; then printf "3"' "$WORK/mutated-override.sh"; then
  fail "突變植入失敗 —— sed 的目標字串不在 $MODULE 裡（改了模組就要更新這支腳本）"
  FAILED=$((FAILED + 1))
else
  # shellcheck source=/dev/null
  source "$WORK/mutated-override.sh"
  if [[ "$(probe_guard_verdict 0 changed)" == "0" ]]; then
    PASS=$((PASS + 1))
    ok "突變被抓到（changed 不再覆蓋 → 假的通過會留著）"
  else
    FAILED=$((FAILED + 1))
    fail "突變沒被抓到 —— 覆蓋拿掉還是全綠，代表「changed 蓋過結束碼」沒有被守住"
  fi
fi

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 三態互不混淆，且 changed 蓋過結束碼這件事有測試守著"
