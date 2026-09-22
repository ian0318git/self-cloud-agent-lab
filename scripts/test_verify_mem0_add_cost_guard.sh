#!/usr/bin/env bash
# **整合**測試：四支腳本（verify-mem0-add-cost／verify-chroma-dims／
# verify-langgraph-tools／verify-phase3-runtime）的探針修訂版守衛，接線對不對。
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
# 四支共用同一套守衛（scripts/probe_traceability.sh），接線也共用同一個形狀：
# 跑前取雜湊 → 跑 → 跑後取雜湊 → 判定 → `probe_guard_verdict` 改結束碼。
# 案例 A–H 走 mem0-add-cost（含 --ctx 推導那條線），L／M／N 走另外三支 ——
# 每一支都測兩邊：沒被改動時**不誤報**、被改動時**必須變 3**。
#
# ── 這個假 docker 刻意**不**重現 #67 的形狀 ────────────────────────
#
# `ps` 預設一次寫完，不是分次。這是一個刻意的選擇，理由是量到的數字。
#
# 先是一個容易猜錯的事實：**bash 的 `printf` 與 `echo` 是逐行寫的**。三個名字
# 就是三次 `write(2)`（strace：`printf '%s' "$(printf 'a\nb\nc')"` → `write(1,
# "a\n", 2)`、`write(1, "b\n", 2)`、`write(1, "c", 1)`；`echo $'a\nb\nc'` 一樣
# 三次）。所以「叫 bash 一次寫完」不是換個寫法就好 —— 那條路走不通。
#
# 於是 `verify-mem0-add-cost.sh:118`（`docker ps | grep -qx`）在**多次寫**的
# 生產者面前是看運氣的。同一條管線 500 次，三種假 docker 的實測：
#   `printf '%s' "$(…)"`（3 次寫）→ 誤判 20/500
#   `echo $'…'`（3 次寫）        → 誤判 42/500
#   `printf '%s\n' a b c`（3 次寫）→ 誤判 14/500
#   `cat <<< $'…'`（**1 次寫**）  → 誤判 0/500
# 整份測試跑起來則是 6 輪 × 8 個案例紅 6 次（約一成二），訊息固定是
# 「ollama 容器未在執行中」。那確實是 #67 的證據。
#
# 但它不該讓一個「測守衛接線」的檔案靠運氣紅：一份會假紅的測試會訓練人重跑，
# 而重跑正是 D-016 說的那種「被忽略的檢查」的起點。所以預設走 `cat`（唯一的
# 一次寫形式），#67 的形狀交給 `STUB_PS_MODE=split`（案例 I／J，**確定性**重現：
# 兩次寫之間隔 50 ms，不是碰運氣）。
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

# 假 docker 沒預期到的呼叫會記到這裡（見 stub 的 fallback 與 check()）
UNHANDLED="$WORK/unhandled.log"
: > "$UNHANDLED"

PROBE_COPY="$WORK/scripts/mem0_add_cost_probe.py"
PRISTINE="$WORK/pristine-probe.py"
cp "$PROBE_COPY" "$PRISTINE"
# 四支腳本各有一個探針，每一個都要能還原 —— 案例 L／M／N 動的是另外三個。
OTHER_PROBES=(chroma_dims_probe.py langgraph_tools_probe.py phase3_runtime_probe.py)
for p in "${OTHER_PROBES[@]}"; do cp "$WORK/scripts/$p" "$WORK/pristine-$p"; done

# 需求檔雜湊：用**真的** sha256sum 算（這支測試自己的 PATH 不動），
# 讓假 docker 回的映像標籤與它相符 —— 否則會走到 docker build。
REQ_HASH="$(sha256sum "$WORK/scripts/requirements-mem0.txt" | cut -d' ' -f1)"
# 另外兩支接上守衛的腳本各有自己的需求檔（chroma-dims 也吃 mem0 那份）。
# 假 docker 按映像名回對應的雜湊，否則它們會走到 build。
REQ_HASH_LG="$(sha256sum "$WORK/scripts/requirements-langgraph.txt" | cut -d' ' -f1)"
REQ_HASH_P3="$(sha256sum "$WORK/scripts/requirements-phase3.txt" | cut -d' ' -f1)"

# ── 假的 docker ─────────────────────────────────────────
mkdir -p "$WORK/bin"
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
set -u
case "${1:-}" in
  info)  exit 0 ;;
  # ps 的三種形狀由 STUB_PS_MODE 選（見檔頭「這個假 docker 刻意不重現 #67」）：
  #   （空）    —— 一次寫完（`cat` 是全緩衝的外部行程：三行就是一次 write(2)）。
  #   split     —— 分兩次寫、中間停 50 ms：會誘發 pipefail×SIGPIPE 的形狀。
  #                這裡刻意用 bash 的 printf（逐行寫）—— 它不是「另一種極端」，
  #                而是**最常見的形狀**：一支 bash 寫的生產者就是長這樣。
  #   no-ollama —— 真的沒有 ollama（案例 J 的對照組）。
  ps)
    case "${STUB_PS_MODE:-}" in
      split)     printf 'ollama\n'; sleep 0.05; printf 'open-webui\nmcp-test-server\n'; exit 0 ;;
      no-ollama) cat <<< $'open-webui\nmcp-test-server'; exit 0 ;;
    esac
    cat <<< $'ollama\nopen-webui\nmcp-test-server'
    exit 0 ;;
  compose)
    case "${2:-}" in
      version) exit 0 ;;
      exec)    # `compose exec -T ollama ollama list` —— 表頭之後第一欄是模型名。
               # **四個都要有**，少一個就會有腳本停在前置檢查而不是走到守衛：
               # chroma-dims 的 --alt-model 預設是 bge-m3、langgraph-tools 沒有
               # .env 的 OLLAMA_MODEL 可用（這份測試的 .env 只有一把假金鑰），
               # 於是它走自己的預設 qwen2.5:3b。也刻意一次寫完（理由同 ps）。
               cat <<< $'NAME\tID\tSIZE\tMODIFIED\nqwen3:4b\tabc\t2.5 GB\t2 days ago\nqwen3-embedding:0.6b\tdef\t639 MB\t3 days ago\nbge-m3:latest\tghi\t1.2 GB\t5 days ago\nqwen2.5:3b\tjkl\t1.9 GB\t6 days ago'
               exit 0 ;;
      ps)      printf 'stub-container\n'; exit 0 ;;
    esac
    exit 0 ;;
  # 兩種 inspect：容器的環境（Config.Env）與網路。**只有前者**會回
  # OLLAMA_CONTEXT_LENGTH，而且只在 STUB_CTX 有設時 —— 這樣「讀得到 num_ctx」
  # 與「讀不到」兩條路都有案例走得到（F 與 G）。
  #
  # 用格式字串分辨，不用「回傳值裡有沒有那個鍵」：網路那一次讀的是
  # NetworkSettings，若兩者都回同一串，`sort -u | head -1` 會把 ctx 的值當成
  # 網路名（實測會變成 `--network OLLAMA_CONTEXT_LENGTH=16384`）—— 假 docker
  # 必須在**語意上**也對得上，不然它會替真的 docker 回答一個奇怪的答案。
  #
  # 比對的是「所有參數裡有沒有 Config.Env」，不是 `$2` —— 呼叫端是
  # `docker inspect -f '<格式>' <容器>`，格式字串在 `$3`，而 `$2` 只是 `-f`。
  # 第一版寫成 `$2` 就是這個錯：F 一直說「讀不到 num_ctx」（也就是說那條路
  # 走的其實是**不推導**的那一支，測到的正好不是它要測的東西）。
  inspect)
    case "$*" in
      *Config.Env*)
        [[ -n "${STUB_CTX:-}" ]] && printf 'OLLAMA_CONTEXT_LENGTH=%s\n' "$STUB_CTX"
        printf 'WEBUI_SECRET_KEY=stub\n'
        exit 0 ;;
    esac
    printf 'lab_default\n'; exit 0 ;;
  # 需求檔雜湊。**每支腳本各自的需求檔不同**，所以按映像名分辨 —— 一律回同一個
  # 值的話，另外三支的映像標籤會對不上、走到 docker build，而下面那個 build
  # 是刻意失敗的，於是那三支會在守衛之前就先死掉（測到的就不是守衛了）。
  image)
    case "$*" in
      *langgraph*) printf '%s\n' "${STUB_REQ_HASH_LG:-}" ;;
      *phase3*)    printf '%s\n' "${STUB_REQ_HASH_P3:-}" ;;
      *)           printf '%s\n' "${STUB_REQ_HASH:-}" ;;
    esac
    exit 0 ;;
  # `docker system df --format '{{.Type}}\t{{.Reclaimable}}'` —— phase3-runtime
  # 靠它算映像可回收量。少了這一段就會落到下面的 fallback（回非零），而呼叫端
  # 是 `RECLAIM_BYTES="$(docker system df … | awk …)"`：pipefail 之下那個失敗被
  # 提升成整條管線的結束碼，lib.sh 的 `set -e` 當場帶走腳本 —— 案例 N 就是
  # 這樣變成 rc=1 的，而且因為呼叫端寫了 `2>/dev/null`，什麼訊息都看不到。
  #
  # 回的字串要帶 `(44%)`：那個百分比黏在同一個欄位裡，是這支腳本踩過兩次的
  # 形狀（見 verify-phase3-runtime.sh 的 _reclaim_to_mb）。回一個乾淨的數字
  # 等於替它把最難的那一半測掉。
  system)
    case "${2:-}" in
      df) printf 'Images\t12.3GB (44%%)\n'; exit 0 ;;
    esac
    exit 0 ;;
  # 這一輪不量 volume：回空清單。讓它落到 fallback 也「只是」一行噪音，但噪音
  # 會進到 OUT，而 OUT 是要被斷言的東西 —— 假 docker 對每個**預期會出現**的
  # 呼叫都該有答案，這一句與其說是給腳本，不如說是給斷言。
  volume)  exit 0 ;;
  logs)    printf '[GIN] 2026/09/22 - 10:00:00 | 200 | 1.000000ms | 12\n'; exit 0 ;;
  build)   echo "假 docker：不該走到 build（映像標籤應該要對得上）" >&2; exit 1 ;;
  run)
    for a in "$@"; do
      # 任何一支探針（四支腳本各有一個）——「執行期間被改動」動的是 STUB_PROBE
      # 指向的那一個檔，所以四支都走得到同一條模擬路徑。
      if [[ "$a" == /probe/*.py ]]; then
        # 「探針執行期間」被改動 —— 只有在 STUB_MUTATE 非空時才發生
        [[ -n "${STUB_MUTATE:-}" ]] && printf '\n# 假 docker：模擬執行期間被改動\n' >> "$STUB_PROBE"
        echo "假探針輸出（這一輪沒有真的跑 mem0）"
        # 把**實際收到的參數**印出來 —— 案例 F／G 靠這一行斷言 `--ctx` 有沒
        # 有真的送到探針。少了它，「腳本算出了 16384」與「探針收到 16384」
        # 就分不出來，而那正是這一輪要驗的接線。
        printf '假探針參數：%s\n' "$*"
        exit "${STUB_PROBE_RC:-0}"
      fi
    done
    printf '500\n'   # 忙碌探測：500 毫秒
    exit 0 ;;
esac
# 沒有預期到的呼叫：印出來**也留一份紀錄**。腳本常常把 stderr 丟掉
# （`2>/dev/null`），所以光印在這裡可能誰都看不到 —— check() 失敗時會把
# STUB_LOG 的內容一起印出來。
echo "假 docker：沒有預期到的呼叫：$*" >&2
printf '%s\n' "$*" >> "${STUB_LOG:-/dev/null}"
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
    *_probe.py) echo "sha256sum: 模擬讀不到探針" >&2; exit 1 ;;
  esac
done
exec /usr/bin/sha256sum "$@"
NOHASH
chmod +x "$WORK/bin-nohash/sha256sum"

PASS=0
FAILED=0

# run <額外 PATH 前綴目錄或空> <STUB_PROBE_RC> <STUB_MUTATE> <STUB_CTX> <額外旗標...>
# STUB_CTX 是假 ollama 容器的 OLLAMA_CONTEXT_LENGTH；空字串代表「容器沒有設」
# （讀不到 num_ctx，D-016 之下不擋，只是推導不出預算）。
run() {
  local binprefix="$1" probe_rc="$2" mutate="$3" stub_ctx="$4"; shift 4
  local out rc=0
  : > "$UNHANDLED"
  out="$(env PATH="${binprefix:+$binprefix:}$WORK/bin:$PATH" \
             STUB_PROBE_RC="$probe_rc" STUB_MUTATE="$mutate" STUB_CTX="$stub_ctx" \
             STUB_REQ_HASH="$REQ_HASH" STUB_REQ_HASH_LG="$REQ_HASH_LG" \
             STUB_REQ_HASH_P3="$REQ_HASH_P3" STUB_PROBE="$PROBE_COPY" \
             STUB_PS_MODE="${STUB_PS_MODE:-}" STUB_LOG="$UNHANDLED" \
             bash "$WORK/scripts/verify-mem0-add-cost.sh" \
               --model qwen3:4b --embed-model qwen3-embedding:0.6b "$@" 2>&1)" || rc=$?
  RC="$rc"; OUT="$out"
}

# run_other <腳本> <探針檔名> <額外 PATH 前綴或空> <STUB_PROBE_RC> <STUB_MUTATE> <旗標...>
#
# 為什麼不共用 run()：那三支的參數不一樣（chroma-dims 不吃 --embed-model、
# langgraph 吃 --num-ctx、phase3 吃 --json），而且探針檔各是一個 —— 硬塞進
# run() 會讓既有九個呼叫端一起改。**這一支不傳 --model 那一組**：三支都有預設值。
run_other() {
  local script="$1" probe_name="$2" binprefix="$3" probe_rc="$4" mutate="$5"; shift 5
  local out rc=0
  : > "$UNHANDLED"
  out="$(env PATH="${binprefix:+$binprefix:}$WORK/bin:$PATH" \
             STUB_PROBE_RC="$probe_rc" STUB_MUTATE="$mutate" \
             STUB_REQ_HASH="$REQ_HASH" STUB_REQ_HASH_LG="$REQ_HASH_LG" \
             STUB_REQ_HASH_P3="$REQ_HASH_P3" \
             STUB_PS_MODE="${STUB_PS_MODE:-}" STUB_LOG="$UNHANDLED" \
             STUB_PROBE="$WORK/scripts/$probe_name" \
             bash "$WORK/scripts/$script" "$@" 2>&1)" || rc=$?
  RC="$rc"; OUT="$out"
}

# check <標籤> <期望結束碼> <輸出必須包含> <輸出必須不包含> [<輸出還必須包含>]
#
# 第五個參數是可選的。**它必須存在於這個檔案裡**（而不是只存在於從這裡
# 衍生的版本）：少了它，多寫的那個斷言會被**靜默丟掉** —— bash 不會抱怨
# 多出來的引數，而案例照樣回報「通過」。這正是這個專案最想避免的形狀
# （一個看起來在做事的斷言），所以寧可讓它在這裡多一個參數。
check() {
  local label="$1" want_rc="$2" want="$3" wantnot="$4" want2="${5:-}" bad=""
  # bad 的接法用 ${bad:+…}：直接寫 bad="$bad；…" 的話，第一個原因會帶著
  # 一個開頭的「；」印出來（不好讀，而且看起來像少了什麼）。
  [[ "$RC" == "$want_rc" ]] || bad="結束碼 $RC（期望 $want_rc）"
  [[ "$OUT" == *"$want"* ]] || bad="${bad:+$bad；}輸出裡沒有「$want」"
  if [[ -n "$wantnot" && "$OUT" == *"$wantnot"* ]]; then
    bad="${bad:+$bad；}輸出裡**不該**出現「$wantnot」"
  fi
  if [[ -n "$want2" && "$OUT" != *"$want2"* ]]; then
    bad="${bad:+$bad；}輸出裡沒有「$want2」"
  fi
  if [[ -n "$bad" ]]; then
    FAILED=$((FAILED + 1))
    fail "$label —— $bad"
    printf '%s\n' "$OUT" | tail -20 | sed 's/^/      | /'
    if [[ -s "$UNHANDLED" ]]; then
      echo "      | 假 docker 收到沒有預期到的呼叫（缺口可能就在這裡）："
      sed 's/^/      |   /' "$UNHANDLED"
    fi
  else
    PASS=$((PASS + 1))
    ok "$label"
  fi
  # 每個案例之間把**四個**探針副本都還原，案例才互相獨立
  cp "$PRISTINE" "$PROBE_COPY"
  for p in "${OTHER_PROBES[@]}"; do cp "$WORK/pristine-$p" "$WORK/scripts/$p"; done
}

GUARD_MSG="探針在這一輪執行期間被改動"
OLLAMA_MSG="ollama 容器未在執行中"
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
run "" 0 "" "" ; check "A 通過且守衛沉默" 0 "item 3 通過" "$GUARD_MSG"

echo
echo "B：整輪通過，但探針在期間被改動 → **假的通過不可以留著**"
# 這是整個守衛最重要的一條：探針回 0（全過）時更不能放過 —— 一份追不回
# 修訂版的「通過」是假的通過。
run "" 0 "1" "" ; check "B 被改成 3，且不再宣稱通過" 3 "$GUARD_MSG" "item 3 通過"

echo
echo "C：探針沒被改，--sections 分段重跑 → 仍然是 2（守衛不可以誤報）"
# 守衛自己誤報的話，它會變成 D-016 說的那種「被忽略的檢查」。
#
# **C 與 D 都必須帶 --sections**：少了它，`SECTION_ARGS` 是空的，那一段說明
# 根本不會被執行 —— D 的「不該出現」就會變成一句**恆真**的斷言（永遠通過，
# 什麼都沒檢查）。第一版就是這樣寫的，跑出來 C 紅、D 綠，而 D 的綠是空的。
run "" 2 "" "" --sections add
check "C 維持 2，且分段重跑的說明有印" 2 "$SECTIONS_MSG" "$GUARD_MSG"

echo
echo "D：探針被改動，而這一輪本來就是分段重跑 → 3 蓋過 2"
# `changed` 比 `--sections` 的 2 嚴重：分段重跑是**設計**，儀器被換掉是儀器壞了。
# 同時確認那一段「結束碼 2 是預期的」**不可以**印出來 —— 印了就是一句被自己
# 那行反駁的話（D-036 第七節）。
run "" 2 "1" "" --sections add
check "D 被改成 3，且分段重跑的說明不再印" 3 "$GUARD_MSG" "$SECTIONS_MSG"

echo
echo "E：算不出探針的雜湊 → 只警告，結束碼不變（D-016）"
# 讀不到不等於它變了。把它當成 changed 會製造假失敗，而假失敗會讓這個守衛
# 被關掉 —— 那時候它就真的什麼都守不到了。
run "$WORK/bin-nohash" 2 "" "" ; check "E 維持 2 並發出警告" 2 "無法確認它整輪沒被換掉" "$GUARD_MSG"

echo
echo "F：ollama 設了 num_ctx=16384 → 推導值要**真的送到探針**（--ctx 16384）"
# 這一條補的是「算得出來」與「送得出去」之間的缺口。腳本自己印「由 num_ctx=
# 16384 推導」不代表探針收到它 —— 兩個字串分別由不同的地方產生，中間隔著
# QUICK_ARGS／CTX_ARGS 那幾行。所以斷言指到**假探針實際印出的參數列**，而且
# 要求整段相鄰：`--ctx 16384` 要落在模型參數與 `--sections` 之間（＝CTX_ARGS
# 那個位置）。只斷言「輸出裡有 --ctx 16384」是不夠的 —— 那正是腳本自己那句
# 「由 num_ctx=16384 推導」搆不到的形狀。
#
# 用 --sections add 當案例：那正是要跑真實 add() 時會用的指令，而且分段重跑
# 永遠回 2（設計），所以這裡的 rc 期望值不是 0。
run "" 2 "" "16384" --sections add
check "F 探針收到 --ctx 16384" 2 \
  "--embed-model qwen3-embedding:0.6b --ollama-url http://ollama:11434 --ctx 16384 --sections add" \
  "$GUARD_MSG"

echo
echo "G：ollama 沒設 num_ctx（讀不到）→ **不送 --ctx**，而且明講抽取預期是空的"
# 兩個方向都要守：送了不該送的旗標（覆寫掉 mem0 的實況）與**靜默**不送
# （量到一輪空白卻沒說為什麼）是兩種不同的病，G 一次擋兩個。
# 「讀不到就不擋」是 D-016；但「讀不到要講」是 D-036 —— 這一輪的 0 筆不是
# 抽取的極限，是預算被 thinking 吃光。
run "" 2 "" "" --sections add
check "G 沒送 --ctx，且說明講出來了" 2 "抽取**預期是空白的**" "--ctx"

echo
echo "H：推導那一層印的是 token 數，不是一個會被機器速率推翻的小時數"
# 原本那句「預期 1~1.5 小時」是拿 D-036 的 **token 數**直接當成時間，沒有除以
# 機器的生成速率。2026-09-22 實測本機是 1.25 t/s（5,605 個 token ≈ 75 分鐘），
# 而同一個組合在本 repo 另有一份 0.18~0.26 tok/s 的量測（探針 `:163`／`:288`，
# ~8k context）—— 兩者差 5 倍，於是「1~1.5 小時」當場低估了一倍以上。
# **速率是環境的性質，不是這個組合的性質**，所以任何寫死的小時數都會在下一次
# 換機器時變成錯的。這一條同時擋兩個方向：要印 token 數，不可以再承諾小時。
#
# 用整句當斷言（不是「小時」這種詞）—— 見上面 `:191-195` 那條規矩。
run "" 2 "" "16384" --sections add
check "H 印 token 數與速率相依，且不再承諾小時" 2 \
  "預期 5,605 個 token（D-036）；時間要看本機速率，不是常數。" "預期 1~1.5 小時"

echo
echo "L：chroma-dims 接上守衛 —— 沒被改動時維持原本的結束碼，守衛沉默"
# D-036 第十一節留下的缺口：守衛原本只接在 mem0-add-cost 上。另外三支有**同一個**
# 性質 —— 探針是用 `-v` 從工作樹即時掛進去，映像標籤只涵蓋需求檔 —— 所以同一個
# 守衛該有一樣的行為。L／M／N 各測兩邊：沒被改動時不誤報、被改動時必須變 3。
run_other verify-chroma-dims.sh chroma_dims_probe.py "" 0 ""
check "L1 探針沒被改 → 維持 0，且守衛沉默" 0 "item 2 通過" "$GUARD_MSG"

echo
echo "M：langgraph-tools 接上守衛 —— 同上"
run_other verify-langgraph-tools.sh langgraph_tools_probe.py "" 0 ""
check "M1 探針沒被改 → 維持 0，且守衛沉默" 0 "item 1 通過" "$GUARD_MSG"

echo
echo "N：phase3-runtime 接上守衛 —— 同上（它的結束碼是聚合出來的，最容易接錯）"
run_other verify-phase3-runtime.sh phase3_runtime_probe.py "" 0 ""
check "N1 探針沒被改 → 維持 0，且守衛沉默" 0 "item 4／item 6 都符合判定" "$GUARD_MSG"

echo
echo "L2／M2／N2：同一組輸入，但探針在執行期間被改動 → **三支都必須變成 3**"
# 少了這三條，L1／M1／N1 只是「跑得起來」：守衛接上了卻沒接到結束碼
# （忘了 probe_guard_verdict 那一步）照樣全綠 —— 那正是「守衛看起來在跑，
# 實際上什麼都沒擋到」，也就是這個測試檔開頭寫的那個最糟的結果。
run_other verify-chroma-dims.sh chroma_dims_probe.py "" 0 "1"
check "L2 被改成 3，且不再宣稱通過" 3 "$GUARD_MSG" "item 2 通過"

run_other verify-langgraph-tools.sh langgraph_tools_probe.py "" 0 "1"
check "M2 被改成 3，且不再宣稱通過" 3 "$GUARD_MSG" "item 1 通過"

run_other verify-phase3-runtime.sh phase3_runtime_probe.py "" 0 "1"
check "N2 被改成 3，且不再宣稱通過" 3 "$GUARD_MSG" "item 4／item 6 都符合判定"

echo
echo "L3：算不出探針的雜湊 → 只警告，結束碼不變（三支共用同一條路徑）"
# 這一條守「unknown 不可以變成 changed」：讀不到不等於它變了（D-016）。
# 只挑 chroma-dims 當代表 —— 三支共用 probe_stable_verdict，判定本身在
# test_probe_traceability.sh 有 18 條斷言；這裡要證的是**接線**上它是警告不是失敗。
run_other verify-chroma-dims.sh chroma_dims_probe.py "$WORK/bin-nohash" 0 ""
check "L3 維持 0 並發出警告" 0 "無法確認它整輪沒被換掉" "$GUARD_MSG"

echo "I：假 docker 分兩次寫（中間隔 50 ms）→ **仍然要認得 ollama**"
# 這一條是那個 flake 的**確定性**版本，不是機率版。
#
# 舊寫法 `docker ps … | grep -qx ollama` 在 lib.sh:5 的 pipefail 之下：grep 配對
# 到第一行就離開 → 生產者第二次寫入吃 EPIPE → 死於 141 → pipefail 把整條管線
# 變非零 → **明明有 ollama 也被讀成「沒有」**。舊寫法在這一條上是 200/200 全錯，
# 所以把修法改回去它就會紅（鑑別力由 K 當場證明）。中間那個 sleep 是刻意的：
# 不靠排程決定勝負之後，這一條就不再靠運氣（原本只能靠機率撞，實測一次 4/10）。
STUB_PS_MODE=split
run "" 2 "" "16384" --sections add
STUB_PS_MODE=""
check "I 生產者分兩次寫，仍然認得 ollama" 2 "$SECTIONS_MSG" "$OLLAMA_MSG"

echo
echo "J：ollama 真的不在（假 docker 只回 open-webui 與 mcp-test-server）→ **仍然要擋，並留下證據**"
# I 少了這一條就可能是空的：一個「永遠說 ollama 在跑」的實作也會讓 I 通過。
# 同時守「失敗要講出看到什麼」—— 現在失敗訊息會列出 docker ps 實際回的容器名，
# 下一次再有怪事，訊息本身就是現場（把沉默的失敗變成一聲響）。
STUB_PS_MODE=no-ollama
run "" 0 "" ""
STUB_PS_MODE=""
check "J 擋下來，且列出實際看到的容器名" 2 "$OLLAMA_MSG" "item 3 通過" \
  "這次 docker ps 看到的容器名：[open-webui mcp-test-server]"

echo
echo "K：把前置檢查突變回管線形式 → **必須變紅**（證明 I 有鑑別力）"
# 做法與 test_ollama_log_corroboration.sh:130 的突變同一條路：把修法改回去，
# 餵同一組輸入，斷言它會誤判。**它證明的是「I 有鑑別力」，不是「修法是必要的」**
# —— 兩者分開講，因為「測試有鑑別力」與「測試盯的是實際的程式碼」是兩件事。
cp "$WORK/scripts/verify-mem0-add-cost.sh" "$WORK/pristine-verify.sh"
mut_err="$(python3 - "$WORK/scripts/verify-mem0-add-cost.sh" 2>&1 <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
fixed = """if [[ $'\\n'"$PS_NAMES"$'\\n' != *$'\\n'ollama$'\\n'* ]]; then"""
pipe  = """if ! docker ps --format '{{.Names}}' | grep -qx \"ollama\"; then"""
assert fixed in s, "突變目標不在腳本裡（改了原始碼就要更新這支測試）"
p.write_text(s.replace(fixed, pipe, 1))
PY
)" || true
if grep -q '| grep -qx "ollama"' "$WORK/scripts/verify-mem0-add-cost.sh"; then
  STUB_PS_MODE=split
  run "" 2 "" "16384" --sections add
  STUB_PS_MODE=""
  check "K 突變後誤判成 ollama 不在（I 有鑑別力）" 2 "$OLLAMA_MSG" "$SECTIONS_MSG"
else
  FAILED=$((FAILED + 1))
  fail "K 突變植入失敗 —— 目標字串不在腳本裡；python 說：$mut_err"
fi
cp "$WORK/pristine-verify.sh" "$WORK/scripts/verify-mem0-add-cost.sh"

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 守衛接得上、蓋得過 --sections 的 2，且不會自己誤報"
