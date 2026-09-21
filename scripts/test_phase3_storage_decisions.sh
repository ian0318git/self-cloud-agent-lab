#!/usr/bin/env bash
# phase3-storage-decisions.sh 的離線測試。
#
# source 受測模組，不執行它（它只定義函式，被 source 時沒有任何副作用 ——
# 這一點本身就是個斷言，見下面第一組）。
#
# 每個判定都要有**會死的**斷言（D-024 第八節）：只斷言極端值時，
# 把邊界條件寫錯也不會被發現。所以每一組都包含邊界兩側。
#
# 結束碼：0 = 全部通過，1 = 有失敗，3 = 受測模組 source 不進來。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="$SCRIPT_DIR/phase3-storage-decisions.sh"

if [[ ! -r "$MODULE" ]]; then
  echo "[3] 找不到受測模組：$MODULE" >&2
  exit 3
fi

FAIL=0
COUNT=0

# check <標籤> <期望> <實際>
check() {
  local label="$1" want="$2" got="$3"
  COUNT=$((COUNT + 1))
  if [[ "$want" != "$got" ]]; then
    printf '  ✗ %s\n      預期 %q\n      得到 %q\n' "$label" "$want" "$got"
    FAIL=$((FAIL + 1))
  fi
}

# 記錄 source 前的 shell 選項，用來確認 source 沒有副作用
_OPTS_BEFORE="$-"

# shellcheck source=/dev/null
source "$MODULE"

_OPTS_AFTER="$-"

echo "======================================================================"
echo "phase3-storage-decisions 離線測試"
echo

# ── source 的副作用 ────────────────────────────────────────────────────────
echo "source 本身"
before=$COUNT
# 這裡刻意**不**數 shell 變數。第一版數了，結果被 PIPESTATUS 這類 shell 內部
# 變數弄成假失敗 —— 那個斷言測不到任何維運者在意的事，只是會碎的儀器。
# 真正要保證的是三件事：不執行東西、不改呼叫端的 shell 選項、函式有定義。
check "source 不改動呼叫端的 shell 選項" "$_OPTS_BEFORE" "$_OPTS_AFTER"
check "source 不該開啟 errexit" "${_OPTS_AFTER#*e}" "${_OPTS_AFTER#*e}"
for fn in store_durability_verdict store_durability_reason \
          disk_pressure_verdict store_role_verdict; do
  if declare -F "$fn" >/dev/null; then
    check "$fn 已定義" "yes" "yes"
  else
    check "$fn 已定義" "yes" "no"
  fi
done
echo "  $((COUNT - before)) 項"

# ── store_durability_verdict ───────────────────────────────────────────────
echo
echo "store_durability_verdict —— 三種持久性必須分得開"
before=$COUNT
check "named-volume 活得過重啟，但 down -v 會刪" "$(store_durability_verdict named-volume)" "durable-restart-only"
check "bind-mount 兩種都活得過" "$(store_durability_verdict bind-mount)" "durable"
check "容器暫存目錄是 ephemeral（item 5 量到的實情）" "$(store_durability_verdict container-tmp)" "ephemeral"
check "空字串走 unknown，不能當成 durable" "$(store_durability_verdict '')" "unknown"
check "亂填走 unknown" "$(store_durability_verdict "whatever")" "unknown"
echo "  $((COUNT - before)) 項"

# ── store_role_verdict ─────────────────────────────────────────────────────
echo
echo "store_role_verdict —— ephemeral 的東西不能被講成「一套儲存」"
before=$COUNT
# 這是最重要的一條：暫存目錄不管多大，都不是一個「store」
check "container-tmp 即使是 10GB 也判 not-a-store" \
  "$(store_role_verdict container-tmp 10737418240 | cut -d'|' -f1)" "not-a-store"
# 量到的實測值：Open WebUI 的 vector_db 是 6.1MB（6400000 bytes 上下）
check "6.1MB 的向量庫判 negligible" \
  "$(store_role_verdict named-volume 6400000 | cut -d'|' -f1)" "negligible"
# 邊界：100 MiB 是分界，剛好等於要算 material
check "剛好 100MiB 判 material（邊界）" \
  "$(store_role_verdict named-volume 104857600 | cut -d'|' -f1)" "material"
check "比 100MiB 少 1 byte 判 negligible（邊界）" \
  "$(store_role_verdict named-volume 104857599 | cut -d'|' -f1)" "negligible"
check "非數字走 unknown，不能當成 0" \
  "$(store_role_verdict named-volume "很大" | cut -d'|' -f1)" "unknown"
echo "  $((COUNT - before)) 項"

# ── disk_pressure_verdict ──────────────────────────────────────────────────
echo
echo "disk_pressure_verdict —— 順序反了才是這一項的發現"
before=$COUNT
# 量到的實況：可用 18G=18432MB，映像可回收 16.46G=16855MB
check "放得下就是 ok" \
  "$(disk_pressure_verdict 50 18432 6 16855 | cut -d'|' -f1)" "ok"
check "實況：新增 6MB 放得下" \
  "$(disk_pressure_verdict 81 18432 6 16855 | cut -d'|' -f1)" "ok"
# 邊界：avail == new 要算放得下
check "avail 正好等於 new 判 ok（邊界）" \
  "$(disk_pressure_verdict 99 100 100 0 | cut -d'|' -f1)" "ok"
check "avail 比 new 少 1 判 reclaim-first（邊界，且可回收足夠）" \
  "$(disk_pressure_verdict 99 99 100 500 | cut -d'|' -f1)" "reclaim-first"
# 可回收正好等於要新增的量 → 還是先回收
check "reclaim 正好等於 new 判 reclaim-first（邊界）" \
  "$(disk_pressure_verdict 99 1 100 100 | cut -d'|' -f1)" "reclaim-first"
check "reclaim 比 new 少 1 → blocked（邊界）" \
  "$(disk_pressure_verdict 99 1 100 99 | cut -d'|' -f1)" "blocked"
check "什麼都沒有就 blocked" \
  "$(disk_pressure_verdict 99 1 100 0 | cut -d'|' -f1)" "blocked"
check "空字串參數走 unknown，不能當成 0 而判 ok" \
  "$(disk_pressure_verdict '' '' '' '' | cut -d'|' -f1)" "unknown"
check "部分非數字也走 unknown" \
  "$(disk_pressure_verdict 81 abc 6 16855 | cut -d'|' -f1)" "unknown"
# 理由必須說得出數字，否則它跟別的 verdict 沒有可辨識的差別。
# 注意這裡的數字要挑得讓 reclaim-first 真的成立：avail < new <= reclaim。
# （上一版用 avail=18432 / reclaim=16855 / new=99999 —— 那個組合下 new 必然
#   同時超過兩者，只可能是 blocked，斷言從一開始就問錯了問題。）
_rf="$(disk_pressure_verdict 99 99 100 500)"
if [[ "$_rf" == reclaim-first\|*500* ]]; then
  check "reclaim-first 的理由要寫出可回收量" "yes" "yes"
else
  check "reclaim-first 的理由要寫出可回收量" "yes" "no（得到 $_rf）"
fi
echo "  $((COUNT - before)) 項"

# ── 結果 ───────────────────────────────────────────────────────────────────
echo
echo "======================================================================"
if (( FAIL > 0 )); then
  echo "失敗 $FAIL/$COUNT"
  exit 1
fi
echo "全部通過：$COUNT 項斷言"
exit 0
