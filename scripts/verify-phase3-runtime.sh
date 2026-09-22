#!/usr/bin/env bash
# 第三階段 item 4／5／6 的進入點。
#
#   item 4 —— `recursion_limit` 對 Plan-and-Execute 是不是太緊
#   item 5 —— 第二套向量庫（ChromaDB）的維運那一半
#   item 6 —— `APScheduler` 在 process 內，重啟就丟排程
#
# 三項的判定都在純函式模組裡（phase3_runtime_probe.py、
# phase3-storage-decisions.sh），可以離線測；這支腳本只做 I/O：
# 量、把量到的餵給判定、印出來。
#
# item 4／6 需要 langgraph 與 apscheduler，只有探針映像裡有，所以在容器裡跑；
# item 5 量的是**這一台機器**的磁碟與 volume，所以在本機跑。
#
# 用法：
#   bash scripts/verify-phase3-runtime.sh            # 三項全跑
#   bash scripts/verify-phase3-runtime.sh --json     # 機器可讀
#
# 結束碼（D-018）：
#   0 = 三項都量到，且都符合判定
#   1 = 有判定不成立（上游變了，或判定本身是錯的）
#   2 = 環境不具備，量不到（**不代表通過**）
#   3 = 這支腳本自己壞了

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# load_env 會 cd 到 PROJECT_ROOT，所以 SCRIPT_DIR 必須在那之前先算好
# （lib.sh 的既有陷阱，每個進入點都要處理）。

REQ_FILE="$SCRIPT_DIR/requirements-phase3.txt"
PROBE="$SCRIPT_DIR/phase3_runtime_probe.py"
STORAGE_MODULE="$SCRIPT_DIR/phase3-storage-decisions.sh"
PROBE_IMAGE="self-cloud-agent-lab-phase3-probe:latest"
MEM0_PROBE="$SCRIPT_DIR/mem0_add_cost_probe.py"

AS_JSON=no
for arg in "$@"; do
  case "$arg" in
    --json) AS_JSON=yes ;;
    -h|--help) sed -n '2,/^$/p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "[3] 未知參數：$arg" >&2; exit 3 ;;
  esac
done

# --json 時，人看的輸出全部改走 stderr，把 stdout 讓給 JSON。
# 沒有這一步的話，json.load 會讀到整份報告的第一行就炸（我自己踩過）。
if [[ "$AS_JSON" == "yes" ]]; then
  exec 3>&1 1>&2
fi

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/probe_traceability.sh"
# shellcheck source=/dev/null
source "$STORAGE_MODULE"

section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ── 前提 ───────────────────────────────────────────────────────────────────
section "前提"
require_docker || exit 3
detect_compose || exit 3
# 網路名不要自己拼。compose 的專案名不一定是目錄名，而 `_default` 更是猜的
# —— 這個堆疊的網路實際叫 `self-cloud-agent-lab_ai-net`（compose 檔有命名）。
# 既有進入點的做法是問執行中的容器（verify-langgraph-tools.sh 的
# `container_running` 迴圈）—— 引用寫內容不寫行號：行號會安靜地過期。
networks_of() {
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$1" 2>/dev/null | grep -v '^$'
}
NET="$(first_line "$(networks_of ollama)")"
if [[ -z "$NET" ]]; then
  fail "找不到 ollama 的網路 —— 容器沒在跑？先 bash scripts/up.sh"
  exit 2
fi
ok "docker 可用，compose 指令：${COMPOSE[*]}"

for f in "$REQ_FILE" "$PROBE" "$STORAGE_MODULE"; do
  [[ -r "$f" ]] || { fail "缺少 $f"; exit 3; }
done
ok "受測檔案齊全"

# ── 探針映像（以 requirements 的 sha256 打標籤）─────────────────────────────
# 標籤是為了讓「這次的結論是哪一組相依跑出來的」可回溯 —— 探針的結論會被
# 寫進 DECISIONS.md，沒有記錄量測條件的量測不算量測。
REQ_HASH="$(sha256sum "$REQ_FILE" | cut -d' ' -f1)"
HAVE_HASH="$(docker image inspect -f '{{index .Config.Labels "lab.req-sha256"}}' \
  "$PROBE_IMAGE" 2>/dev/null || echo '')"

section "探針映像"
if [[ "$HAVE_HASH" == "$REQ_HASH" ]]; then
  ok "映像已是最新（requirements sha256=${REQ_HASH:0:12}…）"
else
  info "建置映像（requirements sha256=${REQ_HASH:0:12}…）"
  if ! docker build -t "$PROBE_IMAGE" --label "lab.req-sha256=$REQ_HASH" \
       -f - "$SCRIPT_DIR" >/dev/null 2>&1 <<'DOCKERFILE'
FROM python:3.12-slim
COPY requirements-phase3.txt /tmp/req.txt
RUN pip install --no-cache-dir --disable-pip-version-check --quiet -r /tmp/req.txt
DOCKERFILE
  then
    fail "映像建置失敗 —— 先手動跑一次看錯誤"
    exit 2
  fi
  ok "映像建置完成"
fi

# ── item 5：這一台機器的儲存與磁碟 ─────────────────────────────────────────
section "item 5：儲存與磁碟（量這一台，不是容器裡）"

# 磁碟
read -r DISK_TOTAL DISK_USED DISK_AVAIL DISK_PCT < <(
  df -BM / 2>/dev/null | awk 'NR==2 {gsub("M",""); printf "%d %d %d %d\n", $2, $3, $4, $5+0}')
if [[ ! "${DISK_AVAIL:-}" =~ ^[0-9]+$ ]]; then
  fail "讀不到磁碟資訊 —— 量不到，不代表通過"
  DISK_VERDICT="unknown"
else
  info "根目錄：總 ${DISK_TOTAL}MB／已用 ${DISK_USED}MB／可用 ${DISK_AVAIL}MB（${DISK_PCT}%）"
fi

# 映像可回收量。**`--format` 並沒有給出機器可讀的數字** ——
# `{{.Reclaimable}}` 是 `16.41GB (88%)`，跟預設輸出裡那個人看的字串一樣，
# 百分比黏在同一個欄位裡。所以還是得自己切。切不出來就當 0 **並講出來**：
# 讀不到不可以被當成「沒有東西可回收」而讓判定看起來更寬鬆（D-029 的形狀）。
RECLAIM_BYTES="$(docker system df --format '{{.Type}}\t{{.Reclaimable}}' 2>/dev/null \
  | awk -F'\t' '$1=="Images"{print $2}')"
# 注意 `.Reclaimable` 是「16.41GB (88%)」——**百分比黏在同一個欄位裡**。
#
# 這段解析在這支腳本裡錯了兩次，兩次都是靜默的，所以把教訓寫在這裡：
#   1. 第一版比對 `GB$` —— 字串結尾是 `(88%)` 不是 `GB`，於是永遠不匹配，
#      回報 0MB。「解析失敗」變成「沒有東西可回收」，判定跟著變寬鬆。
#   2. 第二版加了去掉括號的步驟，但**awk 的 pattern 是各自獨立評估的**，
#      不是 if/else：`gsub("GB","")` 把 $0 改成 `16.41` 之後，最後那條
#      萬用規則又匹配一次，於是印出 `16803` 和 `16` 兩個數字黏在一起 ——
#      回報 1680316MB，比整顆磁碟還大。
# 現在用 if/else 只印一次，再加一道**量級檢查**：可回收量不可能超過磁碟總量。
# 那條檢查才是真正會叫的東西 —— 前兩版都不是靠「小心一點」發現的。
_reclaim_to_mb() {
  printf '%s' "$1" | awk '
    { gsub(/\(.*/, ""); gsub(/ /, "") }
    /GB$/ { sub(/GB$/, ""); printf "%d", $1 * 1024; next }
    /MB$/ { sub(/MB$/, ""); printf "%d", $1;        next }
    /kB$/ { sub(/kB$/, ""); printf "%d", $1 / 1024; next }
    /TB$/ { sub(/TB$/, ""); printf "%d", $1 * 1048576; next }
    /B$/  { sub(/B$/, "");  printf "%d", $1 / 1048576; next }
    { exit 1 }'
}
RECLAIM_MB="$(_reclaim_to_mb "$RECLAIM_BYTES")"
if [[ ! "$RECLAIM_MB" =~ ^[0-9]+$ ]]; then
  # 讀不到就講出來。靜默當成 0 會讓「我不知道」變成「沒有東西可回收」。
  warn "映像可回收量解析失敗（原始字串：'${RECLAIM_BYTES}'）—— 下面磁碟判定以 0 計，會偏保守"
  RECLAIM_MB=0
elif (( RECLAIM_MB > DISK_TOTAL )); then
  # 量級檢查：可回收的映像不可能比整顆磁碟大。這條會叫，才是真的防到了。
  warn "映像可回收量不合理（${RECLAIM_MB}MB > 磁碟總量 ${DISK_TOTAL}MB）—— 解析壞了，以 0 計"
  RECLAIM_MB=0
else
  info "映像可回收：${RECLAIM_MB}MB"
fi

# volume 佈局
info "compose 認得的 named volume：$(docker compose config --volumes 2>/dev/null | tr '\n' ' ')"
VOL_TOTAL_MB=0
for v in $(docker volume ls --format '{{.Name}}' | grep -F "${COMPOSE_PROJECT_NAME:-self-cloud-agent-lab}" || true); do
  sz="$(docker run --rm -v "$v:/d:ro" alpine du -sm /d 2>/dev/null | awk '{print $1}')"
  [[ "$sz" =~ ^[0-9]+$ ]] || sz=0
  VOL_TOTAL_MB=$((VOL_TOTAL_MB + sz))
  info "  ${v#${COMPOSE_PROJECT_NAME:-self-cloud-agent-lab}_} = ${sz}MB"
done
info "named volume 合計：${VOL_TOTAL_MB}MB"

# mem0 的 ChromaDB 住在哪裡 —— 這一項的關鍵事實
CHROMA_KIND="unknown"
if [[ -r "$MEM0_PROBE" ]]; then
  # 判定方式：看探針有沒有把 chroma 指到一個會被保留的位置。
  # 量到的是 `Path(workdir) / "chroma"`，而 workdir 預設是 tempfile.mkdtemp()，
  # 容器又是 `docker run --rm` —— 所以它不是任何一種持久化的掛載。
  if grep -q 'Path(workdir) / "chroma"' "$MEM0_PROBE" \
     && grep -q 'args.workdir or tempfile.mkdtemp' "$MEM0_PROBE"; then
    CHROMA_KIND="container-tmp"
  elif grep -q '"host":' "$MEM0_PROBE"; then
    CHROMA_KIND="unknown"   # 指向遠端服務，這一輪不判
  fi
fi
CHROMA_VERDICT="$(store_durability_verdict "$CHROMA_KIND")"
info "mem0 的 ChromaDB 掛載型態：${CHROMA_KIND} → ${CHROMA_VERDICT}"
info "  $(store_durability_reason "$CHROMA_KIND")"

# Open WebUI 自己的向量庫多大 —— item 5 講的「第二套」其實是這套
WEBUI_VEC_MB=0
for v in $(docker volume ls --format '{{.Name}}' | grep -F "${COMPOSE_PROJECT_NAME:-self-cloud-agent-lab}" || true); do
  case "$v" in
    *open_webui_storage)
      WEBUI_VEC_MB="$(docker run --rm -v "$v:/d:ro" alpine \
        du -sm /d/vector_db 2>/dev/null | awk '{print $1}')" ;;
  esac
done
[[ "$WEBUI_VEC_MB" =~ ^[0-9]+$ ]] || WEBUI_VEC_MB=0
WEBUI_VEC_BYTES=$((WEBUI_VEC_MB * 1048576))
VEC_ROLE="$(store_role_verdict named-volume "$WEBUI_VEC_BYTES")"
info "Open WebUI 的 vector_db：${WEBUI_VEC_MB}MB → ${VEC_ROLE%%|*}"
info "  ${VEC_ROLE#*|}"

# 真正在吃磁碟的是什麼
# 要新增的量用**實際量到的**向量庫大小，不是一個我填的常數。
# 這裡原本硬寫 6（第一次量到的值），而那個值會隨使用而變 ——
# 判定要用當次量到的數字，否則報告會用一個昨天的數字回答今天的問題。
DISK_VERDICT_LINE="$(disk_pressure_verdict "${DISK_PCT:-0}" "${DISK_AVAIL:-0}" "${WEBUI_VEC_MB:-0}" "$RECLAIM_MB")"
info "磁碟壓力判定：${DISK_VERDICT_LINE%%|*}"
info "  ${DISK_VERDICT_LINE#*|}"


# ── item 4／6：容器裡跑 ────────────────────────────────────────────────────
section "item 4／6：容器裡量"
# `--json` 要**傳進去**。第一版沒傳，於是 --json 模式下探針照樣印人看的表格，
# 下面那段 `probe_out.index("{")` 拋 ValueError，被 except 吃掉，JSON 裡的
# item4_item6 就變成 null —— 一份「合法但整段量測不見了」的輸出。
# 這是同一支腳本裡第四次靜默的解析失敗，而且這次它丟掉的是全部的量測結果。
PROBE_ARGS=()
[[ "$AS_JSON" == "yes" ]] && PROBE_ARGS+=(--json)

# 刻意**不用 `2>&1`**：探針在 --json 模式下把人看的報告寫到 stderr、把 JSON
# 寫到 stdout，合併起來會讓人看的表格混進 JSON 裡。讓 stderr 自己流出去。
#
# 守衛的第三個基準，也在開跑**之前**取：探針自己的雜湊。它是唯一能證明
# 「這一輪跑的是工作樹上那一版」的東西 —— 下面那行 -v 是**即時**掛載，
# 映像標籤只涵蓋需求檔。
# `|| true` 不是裝飾：lib.sh 開頭是 `set -euo pipefail`，少了它，sha256sum
# 一失敗（讀不到檔）就會當場帶走整支腳本，而 probe_stable_verdict 的第三態
# （unknown → 只警告）永遠走不到。
PROBE_SHA_BEFORE="$(sha256sum "$PROBE" 2>/dev/null | awk '{print $1}' || true)"

PROBE_OUT="$(docker run --rm --network "$NET" \
  -v "$PROBE:/probe/phase3_runtime_probe.py:ro" \
  -e PYTHONUNBUFFERED=1 \
  "$PROBE_IMAGE" python3 /probe/phase3_runtime_probe.py "${PROBE_ARGS[@]}")"
PROBE_RC=$?

# ── 探針修訂版的守衛（與 verify-mem0-add-cost.sh 同一套；D-036 第十一節）──
# 跑完再算一次。不一樣就代表上面那一次的量測**對不上任何一個修訂版**：
# 探針只在啟動時讀一次自己的雜湊，事後拿工作樹去比對，分不出「跑的是舊碼」
# 與「跑完才被改」。量到的數字仍然印出來（它是真的量到的），但結束碼不可以
# 是 0 —— 一份追不回修訂版的「通過」是假的通過。
#
# 判定在 scripts/probe_traceability.sh（可離線測試：18 條斷言 ＋ 2 個突變）。
PROBE_SHA_AFTER="$(sha256sum "$PROBE" 2>/dev/null | awk '{print $1}' || true)"
TRACE_VERDICT="$(probe_stable_verdict "$PROBE_SHA_BEFORE" "$PROBE_SHA_AFTER")"
case "$TRACE_VERDICT" in
  stable)  ;;
  changed) fail "探針在這一輪執行期間被改動（${PROBE_SHA_BEFORE:0:16} → ${PROBE_SHA_AFTER:0:16}）—— 這一輪的量測對不上任何一個修訂版，不可追溯。"
           warn "結束碼強制為 3：儀器被換掉不是「無法判定（2）」，是儀器壞了。" ;;
  unknown) warn "算不出探針的雜湊（跑前=${PROBE_SHA_BEFORE:0:16}、跑後=${PROBE_SHA_AFTER:0:16}）—— 無法確認它整輪沒被換掉。"
           warn "只警告不失敗：讀不到不等於它變了（D-016，假失敗比漏掉更糟）。" ;;
esac
PROBE_RC="$(probe_guard_verdict "$PROBE_RC" "$TRACE_VERDICT")"

printf '%s\n' "$PROBE_OUT"

# ── 綜合判定 ───────────────────────────────────────────────────────────────
section "綜合"
RC=0
case "$PROBE_RC" in
  0) ok "item 4／item 6 都符合判定" ;;
  1) fail "item 4／item 6 有判定不成立 —— 上游變了，或判定本身是錯的"; RC=1 ;;
  2) fail "item 4／item 6 量不到（環境不具備）—— **這不代表通過**"; [[ "$RC" -eq 0 ]] && RC=2 ;;
  3) if [[ "$TRACE_VERDICT" == "changed" ]]; then
       # 兩種來源要分開講：探針自己壞掉 ≠ 儀器被換掉。訊息說錯會讓人往錯的
       # 方向修（D-036 第七節的形狀）。
       fail "探針在這一輪執行期間被改動 —— 判定不可追溯（見上方守衛那一段）"
     else
       fail "探針自身損壞"
     fi
     [[ "$RC" -eq 0 ]] && RC=3 ;;
  *) fail "探針以非預期結束碼 ${PROBE_RC} 結束"; [[ "$RC" -eq 0 ]] && RC=3 ;;
esac

if [[ "$CHROMA_VERDICT" == "ephemeral" ]]; then
  warn "item 5：第二套向量庫是**暫存的** —— README 原本的『兩套儲存要備份、遷移、保持一致』前提不成立。"
  warn "  這一項要回答的問題因此從『怎麼維運兩套』變成『要不要讓它落地，落地的話放哪』。"
fi

if [[ "$AS_JSON" == "yes" ]]; then
  # 重導向要掛在 python3 這個**簡單指令**上。兩個踩過的坑：
  #   1. 寫成 `fi >&3` 是掛在 if 複合指令上，不管條件真假都會執行 ——
  #      非 JSON 模式下 fd 3 根本沒開，於是 `3: Bad file descriptor`，
  #      而且它讓 rc 變成 1，看起來像判定不成立（假失敗，D-016）。
  #   2. 不能寫成 `PY >&3`：heredoc 的結束字串必須單獨一行，加了東西就不再
  #      是結束字串，bash 會一路讀到檔尾然後 syntax error。
  python3 - "$PROBE_OUT" "$DISK_PCT" "$DISK_AVAIL" "$RECLAIM_MB" \
            "$CHROMA_KIND" "$WEBUI_VEC_MB" >&3 <<'PY'
import json, sys
probe_out, pct, avail, reclaim, chroma, vec = sys.argv[1:7]
try:
    probe = json.loads(probe_out[probe_out.index("{"):probe_out.rindex("}") + 1])
except Exception as e:
    # **不可以靜默變成 null。** 第一版就是這樣：探針沒收到 --json，於是沒有
    # JSON 可解析，`probe` 變成 None，輸出卻是一份合法的 JSON —— 看起來
    # 一切正常，實際上整段量測不見了。讀不到就講出來（同 D-029 的形狀）。
    probe = {"error": f"探針輸出無法解析：{type(e).__name__}: {e}"}
print(json.dumps({
    "item4_item6": probe,
    "item5": {"disk_pct": int(pct or 0), "disk_avail_mb": int(avail or 0),
              "images_reclaimable_mb": int(reclaim or 0),
              "chroma_mount_kind": chroma, "webui_vector_db_mb": int(vec or 0)},
}, ensure_ascii=False, indent=2))
PY
fi

exit "$RC"
