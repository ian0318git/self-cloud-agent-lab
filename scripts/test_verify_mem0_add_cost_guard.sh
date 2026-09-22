#!/usr/bin/env bash
# **整合**測試：scripts/verify-mem0-add-cost.sh 的探針修訂版守衛，接線對不對。
#
# 這支補的是 scripts/test_probe_traceability.sh 補不了的那一半：
#
#   test_probe_traceability.sh  → 守**判定**（probe_stable_verdict／
#                                 probe_guard_verdict 回哪一個字）
#   這一支                      → 守**接線**（那兩個判定有沒有被接上、接的順序
#                                 對不對、結束碼有沒有真的傳到最後那個 case）
#
# 兩者都不能省。判定全綠而接線接錯，症狀是「守衛看起來在跑，實際上什麼都沒
# 擋到」—— 那正是這個守衛存在的理由（把沉默的失敗變成一聲響），所以它自己
# 沉默掉是最糟的結果。
#
# ── 它怎麼跑得起真的腳本，而不用花幾十分鐘 ──────────────────────────
#
# 把真的 verify-mem0-add-cost.sh **照原樣**跑起來，只把 `docker` 換成一個
# 假的（與 test_ollama_log_corroboration.sh 同一個做法）。探針那一次執行由
# 假 docker 假裝 —— 它會照 STUB_PROBE_RC 回一個結束碼，並且在「執行期間」
# 照 STUB_MUTATE 決定要不要動探針檔。
#
# **整棵 scripts/ 是先複製到暫存目錄才跑的**，所以「動探針檔」動的是副本。
# 這一條不是潔癖：第一版直接在 repo 的工作樹上跑，跑完之後
# scripts/mem0_add_cost_probe.py 就多了一行註解 —— 一個「驗證守衛」的測試
# 把受守衛保護的檔案改掉了，而且它是真的寫進工作樹的。副本讓那件事不可能
# 發生，也不必靠 trap 去復原（trap 擋不住 kill -9）。
#
# ── 它**不**涵蓋什麼（講清楚，免得這份綠燈被讀成更多）─────────────────
#
#   · 探針自己的行為（那是 test_mem0_add_cost_probe.py ＋ 它的突變台）
#   · 任何真的 docker／ollama 互動（全部被假 docker 接掉了）
#   · 前置檢查、映像建置、ollama 日誌旁證的**內容**（只確認它們不影響結論）
#
# 用法：bash scripts/test_verify_mem0_add_cost_guard.sh
# 結束碼：0 = 全過；1 = 有案例沒過；3 = 前置檔案不齊（跑不起來）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/verify-mem0-add-cost.sh"

for f in "$TARGET" "$SCRIPT_DIR/lib.sh" "$SCRIPT_DIR/probe_traceability.sh" \
         "$SCRIPT_DIR/ollama_log_corroboration.sh" "$SCRIPT_DIR/deploy-vps-decisions.sh" \
         "$SCRIPT_DIR/requirements-mem0.txt"; do
  if [[ ! -r "$f" ]]; then
    fail "找不到 $f —— 這支測試跑不起來"
    exit 3
  fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 把整棵 scripts/ 複製過去，並造一個不碰真 .env 的最小環境 ──────────
# 只複製 scripts/：PROJECT_ROOT 是它的上一層，所以 load_env 會在 $WORK 裡找
# .env。**故意不放真的 .env** —— 那裡面有金鑰，而測試沒有任何理由碰它。
cp -r "$SCRIPT_DIR" "$WORK/scripts"
printf 'WEBUI_SECRET_KEY=0000000000000000000000000000000000000000000000000000000000000000\n' \
  > "$WORK/.env"

PROBE_COPY="$WORK/scripts/mem0_add_cost_probe.py"
PRISTINE="$WORK/pristine-probe.py"
cp "$PROBE_COPY" "$PRISTINE"

# 需求檔雜湊：用**真的** sha256sum 算（這支測試自己的 PATH 不動），
# 讓假 docker 回的映像標籤與它相符 —— 否則會走到 docker build。
REQ_HASH="$(sha256sum "$WORK/scripts/requirements-mem0.txt" | cut -d' ' -f1)"

# ── 假的 docker ─────────────────────────────────────────
mkdir -p "$WORK/bin"
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
set -u
case "${1:-}" in
  info)  exit 0 ;;
  ps)    printf 'ollama\nopen-webui\n' ; exit 0 ;;
  compose)
    case "${2:-}" in
      version) exit 0 ;;
      exec)    # `compose exec -T ollama ollama list` —— 表頭之後第一欄是模型名
               printf 'NAME\tID\tSIZE\tMODIFIED\n'
               printf 'qwen3:4b\tabc\t2.5 GB\t2 days ago\n'
               printf 'qwen3-embedding:0.6b\tdef\t639 MB\t3 days ago\n'
               exit 0 ;;
      ps)      printf 'stub-container\n'; exit 0 ;;
    esac
    exit 0 ;;
  # 兩種 inspect：num_ctx 的前提檢查與網路。都回得讓腳本走得下去就好 ——
  # 前提檢查讀不到 OLLAMA_CONTEXT_LENGTH 時是「不擋」（D-016）。
  inspect) printf 'lab_default\n'; exit 0 ;;
  image)   printf '%s\n' "${STUB_REQ_HASH:-}"; exit 0 ;;
  logs)    printf '[GIN] 2026/09/22 - 10:00:00 | 200 | 1.000000ms | 12\n'; exit 0 ;;
  build)   echo "假 docker：不該走到 build（映像標籤應該要對得上）" >&2; exit 1 ;;
  run)
    for a in "$@"; do
      if [[ "$a" == "/probe/mem0_add_cost_probe.py" ]]; then
        # 「探針執行期間」被改動 —— 只有在 STUB_MUTATE 非空時才發生
        [[ -n "${STUB_MUTATE:-}" ]] && printf '\n# 假 docker：模擬執行期間被改動\n' >> "$STUB_PROBE"
        echo "假探針輸出（這一輪沒有真的跑 mem0）"
        exit "${STUB_PROBE_RC:-0}"
      fi
    done
    printf '500\n'   # 忙碌探測：500 毫秒
    exit 0 ;;
esac
echo "假 docker：沒有預期到的呼叫：$*" >&2
exit 1
STUB
chmod +x "$WORK/bin/docker"

# 案例 E 要的：一個**只對探針**算不出東西的 sha256sum（模擬探針讀不到）。
#
# 第一版寫成「一律失敗」，結果連需求檔的雜湊都算不出來 —— 於是映像標籤對不
# 上、走到 docker build，而 `set -euo pipefail`（lib.sh:5）在更早的地方就把
# 腳本帶走了：rc=1，停在 `REQ_HASH=` 那一行。**那個 1 與守衛無關**，它只是
# 說明「一個壞掉的 sha256sum 會從別的地方先爆」。要測守衛的 unknown 那條路，
# 就得讓失敗只落在探針上。
# 只放進 $WORK/bin-nohash，所以只有掛了那個 PATH 前綴的子行程會用到它。
mkdir -p "$WORK/bin-nohash"
cat > "$WORK/bin-nohash/sha256sum" <<'NOHASH'
#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    *mem0_add_cost_probe.py) echo "sha256sum: 模擬讀不到探針" >&2; exit 1 ;;
  esac
done
exec /usr/bin/sha256sum "$@"
NOHASH
chmod +x "$WORK/bin-nohash/sha256sum"

PASS=0
FAILED=0

# run <額外 PATH 前綴目錄或空> <STUB_PROBE_RC> <STUB_MUTATE> <額外旗標...>
run() {
  local binprefix="$1" probe_rc="$2" mutate="$3"; shift 3
  local out rc=0
  out="$(env PATH="${binprefix:+$binprefix:}$WORK/bin:$PATH" \
             STUB_PROBE_RC="$probe_rc" STUB_MUTATE="$mutate" \
             STUB_REQ_HASH="$REQ_HASH" STUB_PROBE="$PROBE_COPY" \
             bash "$WORK/scripts/verify-mem0-add-cost.sh" \
               --model qwen3:4b --embed-model qwen3-embedding:0.6b "$@" 2>&1)" || rc=$?
  RC="$rc"; OUT="$out"
}

# check <標籤> <期望結束碼> <輸出必須包含> <輸出必須不包含>
check() {
  local label="$1" want_rc="$2" want="$3" wantnot="$4" bad=""
  [[ "$RC" == "$want_rc" ]] || bad="結束碼 $RC（期望 $want_rc）"
  [[ "$OUT" == *"$want"* ]] || bad="$bad；輸出裡沒有「$want」"
  if [[ -n "$wantnot" && "$OUT" == *"$wantnot"* ]]; then
    bad="$bad；輸出裡**不該**出現「$wantnot」"
  fi
  if [[ -n "$bad" ]]; then
    FAILED=$((FAILED + 1))
    fail "$label —— $bad"
    printf '%s\n' "$OUT" | tail -20 | sed 's/^/      | /'
  else
    PASS=$((PASS + 1))
    ok "$label"
  fi
  # 每個案例之間把探針副本還原，案例才互相獨立
  cp "$PRISTINE" "$PROBE_COPY"
}

GUARD_MSG="探針在這一輪執行期間被改動"
# **不可以拿「分段重跑」當判別字串。** 守衛自己那兩行訊息裡就有那四個字
# （「分段重跑是**設計**，儀器被換掉是儀器壞了」），所以案例 D 的「不該出現」
# 會撞到自己人 —— 而它會**回報測試失敗**，看起來像程式碼有問題。
# 這是本專案第三次踩到同一件事（D-026 第五節、#58 的探針那句、這裡）：
# **斷言要指到可分辨的句子，不是可分辨的詞。** 下面這一句只出現在那個區塊。
SECTIONS_MSG="只跑一部分永遠不算通過"

echo "A：探針沒被改，整輪通過 → 通過，守衛不講話"
# 這一條同時是**對照組**：它證明這套假 docker 造得出一個 rc=0 的成功執行，
# 所以下面 B 的 rc=3 不是「這套測試什麼都判失敗」。
# 沒有這一步，後面每一條「有擋到」都可能只是因為整個環境壞掉。
run "" 0 "" ; check "A 通過且守衛沉默" 0 "item 3 通過" "$GUARD_MSG"

echo
echo "B：整輪通過，但探針在期間被改動 → **假的通過不可以留著**"
# 這是整個守衛最重要的一條：探針回 0（全過）時更不能放過 —— 一份追不回
# 修訂版的「通過」是假的通過。
run "" 0 "1" ; check "B 被改成 3，且不再宣稱通過" 3 "$GUARD_MSG" "item 3 通過"

echo
echo "C：探針沒被改，--sections 分段重跑 → 仍然是 2（守衛不可以誤報）"
# 守衛自己誤報的話，它會變成 D-016 說的那種「被忽略的檢查」。
#
# **C 與 D 都必須帶 --sections**：少了它，`SECTION_ARGS` 是空的，那一段說明
# 根本不會被執行 —— D 的「不該出現」就會變成一句**恆真**的斷言（永遠通過，
# 什麼都沒檢查）。第一版就是這樣寫的，跑出來 C 紅、D 綠，而 D 的綠是空的。
run "" 2 "" --sections add
check "C 維持 2，且分段重跑的說明有印" 2 "$SECTIONS_MSG" "$GUARD_MSG"

echo
echo "D：探針被改動，而這一輪本來就是分段重跑 → 3 蓋過 2"
# `changed` 比 `--sections` 的 2 嚴重：分段重跑是**設計**，儀器被換掉是儀器壞了。
# 同時確認那一段「結束碼 2 是預期的」**不可以**印出來 —— 印了就是一句被自己
# 那行反駁的話（D-036 第七節）。
run "" 2 "1" --sections add
check "D 被改成 3，且分段重跑的說明不再印" 3 "$GUARD_MSG" "$SECTIONS_MSG"

echo
echo "E：算不出探針的雜湊 → 只警告，結束碼不變（D-016）"
# 讀不到不等於它變了。把它當成 changed 會製造假失敗，而假失敗會讓這個守衛
# 被關掉 —— 那時候它就真的什麼都守不到了。
run "$WORK/bin-nohash" 2 "" ; check "E 維持 2 並發出警告" 2 "無法確認它整輪沒被換掉" "$GUARD_MSG"

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 守衛接得上、蓋得過 --sections 的 2，且不會自己誤報"
