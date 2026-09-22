#!/usr/bin/env bash
# 測 scripts/ollama_log_corroboration.sh。
#
# 為什麼這支要存在：那段程式碼是 item 3 唯一的**獨立**旁證（不經過探針），
# 而它踩過的兩個坑都是無聲的（`--since` 靜默回 0 行、日誌中段損壞讓全量
# 輸出凍結在幾小時前）。一段會把「儀器壞了」講成「這一輪沒有截斷」的旁證，
# 比沒有旁證更糟 —— 它正好是 D-016 那種假失敗的鏡像。
#
# 所以：每個分支都要有測試，而且斷言盯的是**可區辨的字串**，不是只盯結束碼
# （D-026 第五節：形狀相同、只有訊息不同的兩條路徑，只檢查編號是測不到的）。
# 最後再加一個突變，確認「時間戳篩選」真的有被測到 —— 少了它，一個把
# 篩選拿掉的實作也會全綠。
#
# 用法：bash scripts/test_ollama_log_corroboration.sh
# 結束碼：0 = 全過；1 = 有案例沒過

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/ollama_log_corroboration.sh"
source "$HELPER"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 假的 docker：只認 `logs ... ollama`，一律回傳 $FAKE_LOGS。
# 這樣才能造出「涵蓋不到」「格式變了」這些真實環境裡難以複製的狀態。
docker() {
  if [[ "${1:-}" == "logs" ]]; then printf '%s\n' "$FAKE_LOGS"; return 0; fi
  return 1
}

RUN_START="2026-09-20T10:00:00"
ANCHOR='[GIN] 2026/09/20 - 09:59:59 | 200 | 1.000000ms | 12'

# 造行的輔助：截斷行的 limit 用不同數字，這樣斷言可以指名道姓地
# 說「這一輪的那一行有印出來、上一輪的那一行沒有」。
gin()   { printf '[GIN] %s | 200 | 1.000000ms | 12' "$1"; }
trunc() { printf 'time=%s level=WARN source=llama_server.go:318 msg="truncating input prompt" limit=%s prompt=%s keep=4 new=%s' "$1" "$2" "$3" "$2"; }

PASS=0
FAILED=0

check() {   # <名稱> <期望結束碼> <輸出一!必須包含> <輸出一!必須不包含>
  local name="$1" want_rc="$2" must="$3" mustnot="$4"
  local out rc=0
  out="$(ollama_log_corroboration "$ANCHOR" "$RUN_START" 6000 2>&1)" || rc=$?

  local bad=""
  [[ "$rc" == "$want_rc" ]] || bad="結束碼 $rc（期望 $want_rc）"
  [[ "$out" == *"$must"* ]] || bad="$bad；輸出裡沒有「$must」"
  if [[ -n "$mustnot" && "$out" == *"$mustnot"* ]]; then
    bad="$bad；輸出裡**不該**出現「$mustnot」"
  fi

  if [[ -n "$bad" ]]; then
    FAILED=$((FAILED + 1))
    fail "$name —— $bad"
    printf '%s\n' "$out" | sed 's/^/      | /'
  else
    PASS=$((PASS + 1))
    ok "$name"
  fi
}

# ── A：涵蓋得到，且這一輪有截斷 ─────────────────────────
# 視窗裡故意放一筆**開跑前**的截斷（limit=1111）：它不該被算進來。
FAKE_LOGS="$(gin '2026/09/20 - 09:30:00')"$'\n'"$(trunc '2026-09-20T09:30:00.000Z' 1111 8047)"$'\n'"$ANCHOR"$'\n'"$(trunc '2026-09-20T10:05:00.000Z' 2222 8047)"$'\n'"$(gin '2026/09/20 - 10:05:01')"
check "A 涵蓋到、這一輪有截斷 → 只算這一輪的" 0 "1 次截斷" "1111"

# ── B：涵蓋得到，但整個視窗都沒有截斷行 ─────────────────
FAKE_LOGS="$ANCHOR"$'\n'"$(gin '2026/09/20 - 10:05:01')"
check "B 涵蓋到、完全沒有截斷行 → 講「沒有截斷紀錄」" 1 "沒有截斷紀錄" ""

# ── C：涵蓋**不到**（日誌中段損壞的症狀）─────────────────
# 這是這支腳本最重要的一條：退回「從檔頭往前讀」時，視窗裡**沒有**開跑
# 前那一行。此時必須說「旁證不可用」，絕對不能說「沒有截斷紀錄」——
# 後者是把儀器壞掉讀成受測對象正常。
FAKE_LOGS="$(gin '2026/09/20 - 08:00:00')"$'\n'"$(trunc '2026-09-20T07:00:00.000Z' 3333 9999)"$'\n'"$(gin '2026/09/20 - 08:12:36')"
check "C 涵蓋不到 → 講「旁證不可用」，不是「沒有截斷」" 2 "旁證不可用" "沒有截斷紀錄"

# ── D：日誌格式變了（有截斷行，但沒有 time= 欄位）────────
# 錯的方向是少報證據，但同樣不可以是無聲的。
FAKE_LOGS="$ANCHOR"$'\n'"level=WARN msg=\"truncating input prompt\" limit=2050 prompt=8047"$'\n'"$(gin '2026/09/20 - 10:05:01')"
check "D 找不到 time= 欄位 → 講「格式變了」，不是「沒有截斷」" 3 "格式變了" "沒有截斷紀錄"

# ── E：時間戳篩選（把「有截斷行」與「這一輪有截斷」分開）──
# 上面 B 是「一行都沒有」，這裡是「有行、但全在開跑之前」。
# 少了這一條，一個把時間戳篩選拿掉的實作也會全綠。
FAKE_LOGS="$ANCHOR"$'\n'"$(trunc '2026-09-20T09:00:00.000Z' 4444 8047)"$'\n'"$(gin '2026/09/20 - 10:05:01')"
check "E 有截斷行但全在開跑前 → 這一輪沒有截斷" 1 "沒有截斷紀錄" "4444"

# ── F：大視窗 ＋ 錨點落在**前段** ────────────────────────
# 這一條不是想出來的，是 2026-09-22 那一輪 `verify-mem0-add-cost.sh` 跑出來
# 的：它的 PRE_ANCHOR 是一行 `srv  update_slots: all slots are idle`，那一行
# 在 6000 行的視窗裡出現 74 次（含前段），於是涵蓋檢查回報「涵蓋不到」——
# 而實際上涵蓋得好好的（視窗第一行是 23:21:14，開跑是 00:05:25）。
#
# 成因是 lib.sh 的 `set -o pipefail`：`grep -q` 一配對到就離開，**生產者只要
# 還會再寫一次**，那一次就吃 EPIPE → 生產者死於 141，pipefail 把它提升成整條
# 管線的結束碼，`!` 再讀成「沒涵蓋」。修法是不開子行程、用 shell 自己的樣式
# 比對（見 helper 裡的註解）。
#
# **判準是「讀者離開之後，生產者還會不會再寫」**（2026-09-22 更正）。這裡原本
# 寫的是「內建 ＋ 超過 64 KiB 緩衝」，那是**低估**：用一支外部 bash 行程實測，
# 只輸出 18 位元組（2 次寫）就誤判了 88/1000 次，把兩次寫拉開 20 ms 更是
# 100/100。確定性版本在 test_lib_running.sh 的案例 D（新述詞要答對）與 E
# （同一支假 docker，被取代的舊形狀必須答錯）。
#
# 而這一條 fixture「視窗必須夠大」是**這支生產者**的性質：`printf` 一次寫一大
# 塊，視窗不夠大它就會搶在 grep 之前寫完，舊寫法反而量不出錯 —— 那正是它活過
# 上面 A~E 五個案例的原因（D-033 的同一種病：測的是法官，不是法官實際拿到的
# 那個案子）。8000 行 × 約 50 B 遠超過管線緩衝。
FAKE_LOGS="$ANCHOR"$'\n'"$(for _ in $(seq 1 8000); do gin '2026/09/20 - 10:00:01'; done)"
check "F 大視窗、錨點在前段 → 涵蓋成立（不是「涵蓋不到」）" 1 "沒有截斷紀錄" "涵蓋不到"

# ── 突變：把時間戳篩選拿掉，上面的 E 必須失效 ────────────
# 「測試全綠」本身不證明測試有在檢查東西（D-024 第八節）。
sed 's/if (ts >= t) print/if (1) print/' "$HELPER" > "$WORK/mutated.sh"
if ! grep -q 'if (1) print' "$WORK/mutated.sh"; then
  fail "突變植入失敗 —— 目標字串不在 $HELPER 裡（改了原始碼就要更新這支腳本）"
  FAILED=$((FAILED + 1))
else
  # shellcheck disable=SC1090
  source "$WORK/mutated.sh"
  FAKE_LOGS="$ANCHOR"$'\n'"$(trunc '2026-09-20T09:00:00.000Z' 4444 8047)"$'\n'"$(gin '2026/09/20 - 10:05:01')"
  mut_out="$(ollama_log_corroboration "$ANCHOR" "$RUN_START" 6000 2>&1)" || true
  if [[ "$mut_out" == *"4444"* ]]; then
    PASS=$((PASS + 1))
    ok "突變被抓到（拿掉時間戳篩選後，開跑前的那筆被誤算進來）"
  else
    FAILED=$((FAILED + 1))
    fail "突變沒被抓到 —— 拿掉時間戳篩選，測試還是通過，代表 E 沒有真的在檢查篩選"
  fi
fi

# ── 突變：把涵蓋檢查改回 `printf | grep -q`（pipefail × SIGPIPE）──
# 這一條證明的是「這個檢查有鑑別力」：把它改壞，測試會紅。
#
# **它沒有證明的事，也講清楚**（實測過，不是推論）：把上面的 F 停掉，這一條
# 照樣抓得到 —— 因為它自己造了一份大 fixture，不靠 F。所以**不可以**把
# 「突變被抓到」講成「F 是必要的」。兩者守的是不同的東西：
#   · F        → 守**真實的 helper**（回歸測試：真的被改回管線形式就會紅）
#   · 這一條   → 守「改壞了會被發現」（鑑別力）
# 分開講，因為「測試有鑑別力」與「測試盯的是實際的程式碼」是兩件事。
sed 's|case "\$window" in \*"\$pre_anchor"\*) covered=1 ;; esac|printf "%s\\n" "\$window" \| grep -qF -- "\$pre_anchor" \&\& covered=1|' "$HELPER" > "$WORK/mutated-pipe.sh"
if ! grep -q 'grep -qF -- "\$pre_anchor" && covered=1' "$WORK/mutated-pipe.sh"; then
  fail "突變植入失敗 —— sed 的目標字串不在 $HELPER 裡（改了原始碼就要更新這支腳本）"
  FAILED=$((FAILED + 1))
else
  # shellcheck disable=SC1090
  source "$WORK/mutated-pipe.sh"
  FAKE_LOGS="$ANCHOR"$'\n'"$(for _ in $(seq 1 8000); do gin '2026/09/20 - 10:00:01'; done)"
  mut_out="$(ollama_log_corroboration "$ANCHOR" "$RUN_START" 6000 2>&1)" || true
  if [[ "$mut_out" == *"涵蓋不到"* ]]; then
    PASS=$((PASS + 1))
    ok "突變被抓到（改回管線形式後，涵蓋檢查被 SIGPIPE 誤判成「涵蓋不到」）"
  else
    FAILED=$((FAILED + 1))
    fail "突變沒被抓到 —— 改回 printf|grep -q 還是全綠，代表 F 沒有真的在守這個"
  fi
fi

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 旁證的每個分支都有測試守著，且「儀器壞掉」與「沒有截斷」是分開的兩句話"
