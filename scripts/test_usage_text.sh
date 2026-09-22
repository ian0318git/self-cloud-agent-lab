#!/usr/bin/env bash
# 離線測試：lib.sh 的 usage_text()（`--help` 要印的檔頭說明）。
#
# ── 為什麼需要這支 ────────────────────────────────────────────
#
# 9 支腳本的 --help 原本都用「位置」決定印到哪一行，而位置會過期。2026-09-22
# 逐支實測（把 HEAD 的舊範圍套回真正的檔頭逐行比對），結果分成三種 —— 三種都
# **讀程式碼看不出來**：
#
#   寫死行號 `sed -n '2,Np'`
#     check-egress            2,40p  （檔頭 32 行）→ 多印 **7** 行（`source`／`require_docker`…）
#     connect-endpoint        2,40p  （檔頭 34 行）→ 多印 **5** 行
#     verify-chroma-dims      2,31p  （檔頭 25 行）→ 多印 **5** 行（程式碼＋檔尾註解）
#     verify-langgraph-tools  2,30p  （檔頭 28 行）→ 多印 **1** 行（`source lib.sh`）
#     rag-verify              2,24p  （檔頭 23 行）→ **正確**
#   停在第一個空行 `sed -n '2,/^$/p'`
#     deploy-vps                      （檔頭 110 行）→ **只印 18 行**（截斷 92 行）
#     verify-mem0-add-cost／verify-phase3-runtime／verify-throughput → **正確**
#     （它們的檔頭裡沒有空行，所以「第一個空行」剛好就是檔頭與程式碼之間那一行）
#   自寫的兩種
#     rag-http-verify  內嵌 awk，判準與 usage_text 相同
#     lock-signup      heredoc，刻意不是檔頭
#
# 也就是「4 支在印程式碼、1 支在截斷、4 支剛好對」。**「剛好對」是這裡最危險
# 的部分**：它沒有症狀，所以沒有任何東西會提醒下一個人它其實靠的是巧合。
#
# 這支測試證五件事，缺一不可：
#
#   A  usage_text 的判準本身（檔頭連續註解、空行不中斷、遇到程式碼就停）
#   B  對照組：同一批 fixture 上，兩種舊形狀各自錯在**不同的方向**
#      —— 沒有這一節，A 可能只是「新函式自己跟自己一致」
#   C  真實腳本端到端：每一支的 `--help`（在**暫存專案根**裡跑，接假 docker）
#      必須逐字等於對該檔**獨立推導**出來的檔頭，而且不含任何一行程式碼
#   D  掃描：沒有 handler 回頭用寫死行號；每個 usage_text 呼叫都搆得到定義；
#      每個 handler 都屬於已知的三種形狀之一（新形狀要來這裡登記理由）
#   E  已知例外（verify-mem0-add-cost.sh 還不能改，見 #67）—— 例外**會自己過期**
#
# ── 它刻意**不**涵蓋什麼 ─────────────────────────────────────
#
#   · 說明文字好不好讀（那是人的事）
#   · `--help` 以外的參數行為（那是各腳本自己的測試）
#   · 真的 docker（C 全部由假 docker 接掉，而且會斷言 --help 只碰兩個閘門）
#
# ── 一個踩過的坑（留著當警訊）──────────────────────────────
#
# 這支測試第一版把說明收進 `$( )` 再比對，於是**結尾的空行全被吃掉了**
# （命令替換會剝掉所有尾端換行），而 `--help` 的輸出剛好都以一個空行收尾。
# 那不是「差一行」而已：它讓 A 與 B 的期望值全部要手動少一個 `|`，也就等於
# 這支測試在比對一份**被改過的**答案。現在一律走檔案 ＋ `cmp`，不再經過 `$( )`。
#
# 用法：bash scripts/test_usage_text.sh
# 結束碼：0 = 全過；1 = 有案例沒過；3 = 前置檔案不齊（跑不起來）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

REPO_ROOT="$PROJECT_ROOT"
[[ -r "$REPO_ROOT/.env.example" ]] || { fail "找不到 $REPO_ROOT/.env.example（暫存專案根需要它）"; exit 3; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAILED=0

# check <標籤> <期望> <實際>
check() {
  local label="$1" want="$2" got="$3"
  if [[ "$want" == "$got" ]]; then
    PASS=$((PASS + 1)); ok "$label"
  else
    FAILED=$((FAILED + 1)); fail "$label —— 期望 [$want]，得到 [$got]"
  fi
}
same() { if [[ "$1" == "$2" ]]; then printf 'yes'; else printf 'no'; fi; }
# 檔案比對（不比 `$( )` —— 它會吃掉尾端換行，而說明的結尾正好是空行）
same_file() { if cmp -s "$1" "$2"; then printf 'yes'; else printf 'no'; fi; }
lines_of() { wc -l < "$1"; }
short_diff() { diff "$1" "$2" | head -4 | tr '\n' '|'; }

# 把檔頭說明推導出來 —— **刻意用另一種算法**（先找第一行程式碼的行號，再取範圍），
# 不是再呼叫一次 usage_text。同一支函式自己跟自己比永遠會過，那證明不了任何事：
# 這裡要的是兩個獨立實作對同一份檔案的答案一致。
derive_to() {   # <檔案路徑> <輸出檔>
  local first_code
  first_code="$(awk 'NR > 1 && !/^#/ && !/^$/ { print NR; exit }' "$1")"
  [[ -n "$first_code" ]] || return 1
  sed -n "2,$((first_code - 1))p" "$1" | sed 's/^# \{0,1\}//' > "$2"
}
# == `--help` 印出來的東西（usage_text 再剝掉 `# ` 前綴）
usage_to() { usage_text "$1" | sed 's/^# \{0,1\}//' > "$2"; }

# 有 `--help` 的腳本由掃描決定（新增的腳本會自動被 C 覆蓋，不必改這份清單）。
# test_*.sh 與 lib.sh 排除：前者被 `--help` 一叫就會整支跑起來，後者沒有 handler。
mapfile -t HELP_SCRIPTS < <(
  cd "$REPO_ROOT/scripts" && grep -l -- '-h|--help)' *.sh 2>/dev/null \
    | grep -v '^test_' | grep -v '^lib\.sh$' | sed 's/\.sh$//' | sort
)
[[ "${#HELP_SCRIPTS[@]}" -ge 1 ]] || { fail "掃不到任何 --help handler —— 掃描本身壞了"; exit 3; }

# lock-signup 的 --help 是**自寫的 heredoc**，刻意不印檔頭 → 不比對內容、也不做
# 「不含程式碼」那一條（heredoc 的文字本來就住在檔體裡，比對會命中自己）。
HEREDOC_SCRIPTS=(lock-signup)
is_heredoc() { local x; for x in "${HEREDOC_SCRIPTS[@]}"; do [[ "$x" == "$1" ]] && return 0; done; return 1; }

# 已知例外：`verify-mem0-add-cost.sh` 的 handler 是第四種形狀（`sed -n '2,/^$/p'`），
# 因為它正在跑（#66），改它就是改一支執行中的腳本 —— #67 落地時會一起換掉。
# 例外不可能是「靜靜留著」的：E1 斷言它今天還是那個舊形狀，改掉就會失敗叫人來刪。
SHAPE_EXCEPTIONS=(verify-mem0-add-cost)
is_shape_exception() { local x; for x in "${SHAPE_EXCEPTIONS[@]}"; do [[ "$x" == "$1" ]] && return 0; done; return 1; }

# ════════════════════════════════════════════════════════════════════
echo "A：usage_text 的判準（fixture）"
# ════════════════════════════════════════════════════════════════════
FIX="$WORK/fix"
mkdir -p "$FIX"

# 全部照這個形狀：第 1 行是 shebang，第 2 行起是檔頭，最後接程式碼。
printf '#!/usr/bin/env bash\n# 說明一\n# 說明二\n\nset -euo pipefail\n' > "$FIX/short.sh"
printf '#!/usr/bin/env bash\n# 上半\n# 上半二\n\n# 下半（空行之後還有！）\n# 下半二\n\nCODE=1\n' > "$FIX/blankmid.sh"
printf '#!/usr/bin/env bash\nCODE=1\n' > "$FIX/noheader.sh"
printf '#!/usr/bin/env bash\n# 只有註解一\n# 只有註解二\n# 只有註解三\n' > "$FIX/allcomment.sh"
printf '#!/usr/bin/env bash\n# 一\n#!/bin/sh\n# 三\nCODE=1\n' > "$FIX/midshebang.sh"
printf '#!/usr/bin/env bash\n# 一\n  # 縮排的註解\n# 三\nCODE=1\n' > "$FIX/indented.sh"
printf '#!/usr/bin/env bash\n# 沒有結尾換行' > "$FIX/nonewline.sh"
# B2 用：檔頭 3 行、後面 12 行程式碼（讓「範圍 2,12p」越過檔頭）
{ printf '#!/usr/bin/env bash\n# 範圍一\n# 範圍二\n\n'; for i in $(seq 1 12); do printf 'code_line_%s=1\n' "$i"; done; } > "$FIX/range.sh"

# 逐字比對：usage_text 的輸出 == 手寫的期望檔
expect() {   # <標籤> <fixture> <期望內容>
  printf '%b' "$3" > "$WORK/exp.txt"
  usage_to "$2" "$WORK/got.txt"
  if cmp -s "$WORK/exp.txt" "$WORK/got.txt"; then
    PASS=$((PASS + 1)); ok "$1"
  else
    FAILED=$((FAILED + 1)); fail "$1 —— 差異：$(short_diff "$WORK/exp.txt" "$WORK/got.txt")"
  fi
}

# 第 1 行（shebang）不印；檔頭與程式碼之間那個空行**會**印出來 —— 這是刻意的
# （把說明與提示字元隔開），而且它就是現況：每一支真腳本的 --help 都以空行收尾。
expect "A1 短檔頭（含尾端空行）"     "$FIX/short.sh"      '說明一\n說明二\n\n'
expect "A2 檔頭中間的空行**不中斷**" "$FIX/blankmid.sh"   '上半\n上半二\n\n下半（空行之後還有！）\n下半二\n\n'
expect "A3 沒有檔頭 → 空的"           "$FIX/noheader.sh"   ''
expect "A4 整份都是註解 → 全部印出"   "$FIX/allcomment.sh" '只有註解一\n只有註解二\n只有註解三\n'
# 只有第 1 行被跳過：第 3 行的 `#!` 是註解，照印（`# ` 前綴會被剝掉，所以印成 `!/bin/sh`）
expect "A5 #! 只在第 1 行被跳過"      "$FIX/midshebang.sh" '一\n!/bin/sh\n三\n'
# 縮排的註解不是檔頭註解（判準是 `^#`）—— 而且它會**停住**列印。這是刻意的：
# 本專案的檔頭一律從第 1 欄開始，所以「縮排的 #」代表說明已經結束。
expect "A6 縮排的註解不算檔頭，且停在那裡" "$FIX/indented.sh" '一\n'
expect "A7 最後一行沒有結尾換行仍印得出來" "$FIX/nonewline.sh" '沒有結尾換行\n'

# ════════════════════════════════════════════════════════════════════
echo
echo "B：對照組 —— 兩種舊形狀在同一批 fixture 上錯在**不同方向**"
# ════════════════════════════════════════════════════════════════════
# 沒有這一節，A 可能只是「新函式自己跟自己一致」：要證明 A 有鑑別力，就得讓被
# 取代的形狀在同一個輸入上**答錯**，而且錯的方式正是它們當初錯的方式。
usage_to "$FIX/blankmid.sh" "$WORK/new-b1.txt"
sed -n '2,/^$/p' "$FIX/blankmid.sh" | sed 's/^# \{0,1\}//' > "$WORK/old-b1.txt"

# B1：停在第一個空行 → **截斷**（blankmid.sh 的空行之後還有兩行說明）
check "B1 正解 6 行" "yes" "$(same "$(lines_of "$WORK/new-b1.txt")" 6)"
check "B1b 舊形狀（停在第一個空行）少印 3 行" "yes" \
  "$(same "$(( $(lines_of "$WORK/new-b1.txt") - $(lines_of "$WORK/old-b1.txt") ))" 3)"

# B2：寫死行號 → 印出**程式碼**（範圍 2,12p 越過 3 行的檔頭，多印 8 行）
usage_to "$FIX/range.sh" "$WORK/new-b2.txt"
sed -n '2,12p' "$FIX/range.sh" | sed 's/^# \{0,1\}//' > "$WORK/old-b2.txt"
check "B2 舊形狀（範圍過長）比正解多 8 行" "yes" \
  "$(same "$(( $(lines_of "$WORK/old-b2.txt") - $(lines_of "$WORK/new-b2.txt") ))" 8)"
check "B2b 舊形狀印出了程式碼（`code_line_1=1`）" "yes" \
  "$(same "$(grep -c '^code_line_1=1$' "$WORK/old-b2.txt" || true)" 1)"
check "B2c 正解一行程式碼都沒有" "yes" \
  "$(same "$(grep -c '^code_line_' "$WORK/new-b2.txt" || true)" 0)"

# B3：**不變性** —— 這才是這次改動真正買到的東西，也是上面兩個方向共同的原因。
#     · 程式碼區變長／變短 → 說明的輸出逐字不變（舊的寫死範圍會跟著漂）
#     · 檔頭下半多一行     → 說明的輸出跟著多一行（舊的「停在空行」看不到它）
cp "$FIX/blankmid.sh" "$WORK/append.sh"
printf 'x=1\ny=2\nz=3\n' >> "$WORK/append.sh"
usage_to "$WORK/append.sh" "$WORK/new-b3a.txt"
check "B3a 檔尾加 3 行程式碼 → 逐字不變" "yes" \
  "$(same_file "$WORK/new-b3a.txt" "$WORK/new-b1.txt")"
# 插在**空行之後**（第 6 行）：舊形狀停在第 4 行那個空行，所以它看不到這一行
awk 'NR == 6 { print "# 後半段補的一行說明" } { print }' "$FIX/blankmid.sh" > "$WORK/insert.sh"
usage_to "$WORK/insert.sh" "$WORK/new-b3b.txt"
sed -n '2,/^$/p' "$WORK/insert.sh" | sed 's/^# \{0,1\}//' > "$WORK/old-b3b.txt"
check "B3b 檔頭後半補一行 → 正解多那一行（6 → 7）" "yes" \
  "$(same "$(lines_of "$WORK/new-b3b.txt")" 7)"
check "B3c 同一個輸入，舊形狀的輸出**沒有變**（它看不到新說明）" "yes" \
  "$(same_file "$WORK/old-b3b.txt" "$WORK/old-b1.txt")"
check "B3d 而新的那一行確實只在正解裡" "yes" \
  "$(same "$(grep -c '後半段補的一行說明' "$WORK/new-b3b.txt" || true)|$(grep -c '後半段補的一行說明' "$WORK/old-b3b.txt" || true)" "1|0")"

# B4：拿真檔案的不變性（deploy-vps.sh 的檔頭 110 行，是這個 repo 最長的）
cp "$REPO_ROOT/scripts/deploy-vps.sh" "$WORK/dv.sh"
printf '\n# 後面補的\n' >> "$WORK/dv.sh"
usage_to "$WORK/dv.sh" "$WORK/new-b4.txt"
usage_to "$REPO_ROOT/scripts/deploy-vps.sh" "$WORK/new-b4-ref.txt"
check "B4 真檔案（deploy-vps.sh，檔頭 110 行）＋尾端補行 → 逐字不變" "yes" \
  "$(same_file "$WORK/new-b4.txt" "$WORK/new-b4-ref.txt")"

# ════════════════════════════════════════════════════════════════════
echo
echo "C：真實腳本端到端 —— 在暫存專案根裡跑 --help"
# ════════════════════════════════════════════════════════════════════
# 這裡是暫存專案根而不是原 repo，理由有三個：
#   · 3 支腳本在參數迴圈**之前**就呼叫 load_env（它會 cd 到專案根、沒有 .env 就從
#     .env.example 生一個、寫入 WEBUI_SECRET_KEY，而且那兩行訊息走 **stdout**）
#     —— 在原 repo 跑會動到真檔案
#   · 4 支在參數迴圈之前呼叫 require_docker → 需要假的 docker 在 PATH 上
#   · 「檔頭說明」要能在不被環境雜訊污染的情況下逐字比對
ROOT="$WORK/root"
mkdir -p "$ROOT/scripts" "$WORK/bin"
cp "$REPO_ROOT"/scripts/*.sh "$ROOT/scripts/"
cp "$REPO_ROOT/.env.example" "$ROOT/.env.example"
# 先把 .env 種好（＝已經有 key 的狀態）。不種的話，前三支的 --help 會先印出
# load_env 的「產生 WEBUI_SECRET_KEY／已寫入 .env」兩行，比對就不可能逐字相等
# —— 那是 --help 在乾淨機器上的既有行為，不在這次範圍，所以這裡把它隔離掉。
cp "$REPO_ROOT/.env.example" "$ROOT/.env"
printf 'WEBUI_SECRET_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n' >> "$ROOT/.env"

# 假 docker：`--help` 只該走到兩個閘門（require_docker 的 `info`、detect_compose 的
# `compose version`）。其餘一律記進日誌，最後斷言日誌裡沒有別的東西。
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
set -u
echo "$*" >> "${STUB_LOG:?}"
case "${1:-}" in
  info)    exit 0 ;;
  compose) exit 0 ;;
esac
exit 0
STUB
chmod +x "$WORK/bin/docker"

STUB_LOG="$WORK/docker.log"; export STUB_LOG
: > "$STUB_LOG"

for name in "${HELP_SCRIPTS[@]}"; do
  src="$REPO_ROOT/scripts/$name.sh"
  derive_to "$src" "$WORK/want-$name.txt"
  out="$WORK/help-$name.out"
  rc=0
  ( cd "$ROOT" && PATH="$WORK/bin:$PATH" bash "scripts/$name.sh" --help ) > "$out" 2> "$WORK/help-$name.err" || rc=$?

  check "C/$name 結束碼 0（--help 不是錯誤）" "yes" "$(same "$rc" 0)"
  if is_heredoc "$name"; then
    check "C/$name 自寫說明：非空" "yes" "$([[ -s "$out" ]] && echo yes || echo no)"
  else
    check "C/$name --help 逐字 == 獨立推導的檔頭" "yes" "$(same_file "$out" "$WORK/want-$name.txt")"
    # 「沒有印出程式碼」用**檔體**的整行來比對：說明裡出現一行與程式碼完全相同的行
    # 就是漏了（整行相等 —— 說明裡提到 `source` 這個**字**不算，這不是比對子字串）
    awk 'NR > 1 && !/^#/ && !/^$/ { print }' "$src" > "$WORK/body.txt"
    leaked="$(grep -Fxf "$WORK/body.txt" "$out" | head -3 | tr '\n' '|' || true)"
    check "C/$name 輸出不含任何一行程式碼" "" "$leaked"
  fi
done

# 對照組：把這次修好的「handler 裡惰性載入 lib.sh」拿掉，並刪掉主體那次載入
# —— 重現修好之前那一版（我第一版就是這樣）。它會**安靜地**印出空白，而且
# **結束碼 0**：唯一說有事的只有 stderr。沒有這一條，C 只是在驗「今天剛好對」。
awk '
  /-h\|--help\)/ { sub(/source "\$SCRIPT_DIR\/lib\.sh"; /, ""); print; next }
  /^source "\$SCRIPT_DIR\/lib\.sh"$/ { next }
  { print }
' "$ROOT/scripts/verify-throughput.sh" > "$ROOT/scripts/no-lib.sh"
nrc=0
( cd "$ROOT" && PATH="$WORK/bin:$PATH" bash scripts/no-lib.sh --help ) > "$WORK/no-lib.out" 2>"$WORK/no-lib.err" || nrc=$?
check "C-對照 lib.sh 沒載入時：--help 印出空白、**結束碼 0**、只有 stderr 抱怨" \
  "yes|yes|yes" \
  "$(same "$(wc -c < "$WORK/no-lib.out")" 0)|$(same "$nrc" 0)|$(same "$(grep -c 'usage_text: command not found' "$WORK/no-lib.err" || true)" 1)"
rm -f "$ROOT/scripts/no-lib.sh"

bad_calls="$(grep -vE '^(info|compose version)$' "$STUB_LOG" | sort -u | tr '\n' '|' || true)"
check "C/docker 只被兩個閘門呼叫（info／compose version）" "" "$bad_calls"

# ════════════════════════════════════════════════════════════════════
echo
echo "D：掃描 —— 擋住「下一個人又寫一次」"
# ════════════════════════════════════════════════════════════════════
# 這一節是唯一會涵蓋「還沒有人踩到的檔案」的一節，也是這支測試存在的主要理由。
# 已知例外只有一個，而且例外本身也被斷言（它一旦被改掉，E1 就會失敗叫人來刪）。
n_handlers=0; n_usage=0; n_inline_awk=0; n_heredoc=0
# 掃**同一份清單**（${HELP_SCRIPTS[@]}），不重新 glob `scripts/*.sh` —— 重新 glob
# 會掃到這支測試自己（它裡面就有這串字：掃描用的 grep 樣式本身），於是它把自己
# 當成「有一支 handler 沒被歸類」。掃描程式的樣式字串是掃描對象的一部分。
for name in "${HELP_SCRIPTS[@]}"; do
  src="$REPO_ROOT/scripts/$name.sh"
  # `|| true` 是必要的：沒有 handler 的檔案會讓 grep 回 1，而 lib.sh 的 errexit
  # 會在**賦值那一行**把整支測試殺掉（第一版就是這樣，D 跑到一半就沒了）。
  hline="$(grep -n -- '-h|--help)' "$src" | head -1 | cut -d: -f1 || true)"
  [[ -n "$hline" ]] || continue
  n_handlers=$((n_handlers + 1))
  # handler 的範圍：從 `-h|--help)` 起最多 12 行（三個形狀都遠短於此）
  body="$(sed -n "${hline},$((hline + 11))p" "$src")"

  # D1 寫死行號（＝會把程式碼當說明印出來的那一種）
  if grep -qE "sed -n '2,[0-9]+p'" <<<"$body"; then
    FAILED=$((FAILED + 1))
    fail "D/$name 沒有寫死的行號範圍 —— **有**：$(grep -oE "sed -n '2,[0-9]+p'" <<<"$body" | head -1)"
  else
    PASS=$((PASS + 1)); ok "D/$name 沒有寫死的行號範圍"
  fi

  # D2 usage_text 要搆得到定義：lib.sh 在 handler **之前**載入，或在 handler 裡載入
  if grep -q 'usage_text' <<<"$body"; then
    n_usage=$((n_usage + 1))
    libline="$(grep -n 'source .*lib\.sh' "$src" | head -1 | cut -d: -f1 || true)"
    if [[ -n "$libline" && "$libline" -lt "$hline" ]]; then
      check "D/$name usage_text 搆得到定義（lib.sh 先載入）" "yes" "yes"
    elif grep -q 'source .*lib\.sh' <<<"$body"; then
      check "D/$name usage_text 搆得到定義（handler 裡自己載入）" "yes" "yes"
    else
      check "D/$name usage_text 搆得到定義" "yes" "no（會印出空白，而且結束碼 0）"
    fi
  fi

  # D3 handler 屬於已知的三種形狀之一（新的形狀要來這裡登記，並在 lib.sh 寫理由）
  if grep -q 'usage_text' <<<"$body"; then shape="usage_text"
  elif grep -q "awk 'NR>1\|awk 'NR > 1" <<<"$body"; then shape="內嵌 awk"; n_inline_awk=$((n_inline_awk + 1))
  elif grep -q "<<'USAGE'" <<<"$body"; then shape="heredoc"; n_heredoc=$((n_heredoc + 1))
  else shape="未分類"
  fi
  if [[ "$shape" == "未分類" ]] && is_shape_exception "$name"; then
    PASS=$((PASS + 1)); ok "D/$name handler 是已知的形狀（**已知例外**，理由見 E 節）"
  else
    check "D/$name handler 是已知的形狀（usage_text／內嵌 awk／heredoc）" "known" \
      "$([[ "$shape" == "未分類" ]] && echo "**未分類**" || echo known)"
  fi
done

# 掃描涵蓋的 handler 數量：只驗下界（新增腳本是正常的事，掃描壞掉才是問題）
check "D/掃到至少 11 個 handler" "yes" "$([[ "$n_handlers" -ge 11 ]] && echo yes || echo no)"
check "D/每個 handler 都被歸類（未歸類會讓這個總數對不上；+1 是 E 的已知例外）" "yes" \
  "$(same "$((n_usage + n_inline_awk + n_heredoc + 1))" "$n_handlers")"

# ════════════════════════════════════════════════════════════════════
echo
echo "E：已知例外 —— verify-mem0-add-cost.sh（等 #67）"
# ════════════════════════════════════════════════════════════════════
# 這一支正在跑（#66），改它就是改一支執行中的腳本，所以它的 --help 這次不動。
# 例外**會自己過期**：E1 斷言它今天還是舊形狀，一旦有人把它改掉，E1 就會失敗
# 並提醒把這一節刪掉 —— 例外不會安靜地留下來變成裝飾。
MEM0="$REPO_ROOT/scripts/verify-mem0-add-cost.sh"
mem0_hline="$(grep -n -- '-h|--help)' "$MEM0" | head -1 | cut -d: -f1 || true)"
check "E1 例外還在（handler 裡還是那個 sed 範圍）" "yes" \
  "$(same "$(sed -n "${mem0_hline}p" "$MEM0" | grep -c "sed -n '2,/\^\$/p'" || true)" 1)"
check "E2 它今天的 --help 是**對的**（檔頭 56 行、中間沒有空行 —— 巧合，不是設計）" "yes" \
  "$(usage_to "$MEM0" "$WORK/mem0-new.txt"; derive_to "$MEM0" "$WORK/mem0-der.txt"; same_file "$WORK/mem0-new.txt" "$WORK/mem0-der.txt")"
# E3：為什麼它還是得改 —— 檔頭一有空行就會截斷。在它的真檔頭裡插一行說明與一個
#     空行，舊形狀立刻停在那個空行上，而正解照樣印完整份。
awk 'NR == 3 { print "# 新補的一行說明" } NR == 5 { print "" } { print }' "$MEM0" > "$WORK/mem0.sh"
sed -n '2,/^$/p' "$WORK/mem0.sh" | sed 's/^# \{0,1\}//' > "$WORK/mem0-old.txt"
derive_to "$WORK/mem0.sh" "$WORK/mem0-full.txt"
check "E3 檔頭一有空行，舊形狀就截斷（只印到那個空行，不到 7 行）" "yes" \
  "$([[ "$(lines_of "$WORK/mem0-old.txt")" -le 6 ]] && echo yes || echo no)"
check "E3b 同一份檔案，正解印出完整檔頭（> 50 行）" "yes" \
  "$([[ "$(lines_of "$WORK/mem0-full.txt")" -gt 50 ]] && echo yes || echo no)"

# ════════════════════════════════════════════════════════════════════
echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 判準是內容不是行號；兩種舊形狀在對照組裡各自錯在不同的方向；${#HELP_SCRIPTS[@]} 支腳本的 --help 逐字等於對該檔獨立推導出來的檔頭"
