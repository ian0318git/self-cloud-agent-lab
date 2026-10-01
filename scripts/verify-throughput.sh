#!/usr/bin/env bash
# 推論吞吐量基準線的進入點。
#
# 為什麼要有這一支：記錄上**所有**的 token 速率都來自舊 VM（2 vCPU／3.8 GB）。
# 2026-09-20 18:15 之後機器是 4 vCPU／16 GB，而 HANDBOOK 還在引用 D-024 的
# `~14 tok/s` —— 那個數字連在舊 VM 上都不成立（D-030 第七節）。
#
# 判定全在純函式模組 throughput_probe.py 裡，可以離線測；這支腳本只做 I/O。
#
# 用法：
#   bash scripts/verify-throughput.sh                # 完整矩陣（約 35 分鐘）
#   bash scripts/verify-throughput.sh --quick        # 只掃 num_ctx，每條件一次取樣
#   bash scripts/verify-throughput.sh --json         # 機器可讀（走 stdout）
#   bash scripts/verify-throughput.sh -- --ctx 4096  # `--` 之後原樣傳給探針
#   bash scripts/verify-throughput.sh -- --repeats 5 # 改每個條件的取樣次數（預設 7）
#
# **整輪時間大致與取樣次數成正比**：3 次約 15 分鐘，7 次約 35 分鐘。
# 中間會休眠的機器不要開這一輪 —— 量到一半睡著就是一次殘缺的矩陣，
# 而殘缺的矩陣不能給出結論（D-014）。
#
# 結束碼（D-018）：
#   0 = 量到且穩定，可以當基準線
#   1 = 準則不成立（上限沒生效、樣本不穩、只有單一條件）—— 數字不可信
#   2 = 環境不具備，量不到（**不代表通過**）。`--quick` 也走這裡：
#       它每個條件只取樣一次，**沒有嘗試**建立基準線，把沒做的事記成
#       做失敗是假失敗（D-016）
#   3 = 這支腳本自己壞了
#
# 這一支**刻意不建映像**：探針只用標準函式庫（urllib），而 open-webui
# 容器裡已經有 python3。所以用 `compose exec -T open-webui python3 -`
# 把原始碼從 stdin 送進去跑 —— 不必建置、不必拉映像，在乾淨的 VPS 上也成立。
# （需要額外套件的探針才走 verify-phase3-runtime.sh 那種建映像的路。）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# load_env 會 cd 到 PROJECT_ROOT，所以 SCRIPT_DIR 必須在那之前先算好
# （lib.sh 的既有陷阱，每個進入點都要處理）。

PROBE="$SCRIPT_DIR/throughput_probe.py"

AS_JSON=no
QUICK=no
PASSTHRU=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) AS_JSON=yes; shift ;;
    --quick) QUICK=yes; shift ;;
    # lib.sh 要到下面（`--json` 的轉向之後）才載入，所以這裡自己先載入一次
    # —— source 兩次是幂等的，而把那一行往前搬會動到 --json 的轉向順序。
    # 少了這一行的話，--help 會印出**空白**（`usage_text: command not found`）。
    -h|--help) source "$SCRIPT_DIR/lib.sh"; usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --) shift; PASSTHRU=("$@"); break ;;
    *) echo "[3] 未知參數：$1（要傳給探針的參數請放在 -- 之後）" >&2; exit 3 ;;
  esac
done

if [[ ! -r "$PROBE" ]]; then
  echo "[3] 找不到探針：$PROBE" >&2
  exit 3
fi

# --json 時，人看的輸出全部改走 stderr，把 stdout 讓給 JSON。
# 沒有這一步的話，json.load 會讀到整份報告的第一行就炸（我自己踩過）。
if [[ "$AS_JSON" == "yes" ]]; then
  exec 3>&1 1>&2
fi

# shellcheck source=/dev/null
source "$SCRIPT_DIR/lib.sh"

section() { printf '\n\033[1m%s\033[0m\n' "$*"; }

require_docker || exit 2

# detect_compose **不印任何東西**：它把 COMPOSE 設定好並 export 當作副作用。
# 所以一定要**裸呼叫**。寫成 `COMPOSE="$(detect_compose)"` 的話，函式會在
# 子殼裡跑、export 消失、捕捉到空字串，而 `$COMPOSE ps …` 就退化成系統的
# `/usr/bin/ps` —— 一個完全不同的指令，而且它會抱怨 docker 的旗標，
# 看起來像「compose 壞了」。我自己踩過。
detect_compose || exit 3
if [[ -z "${COMPOSE:-}" ]]; then
  echo "[3] detect_compose 沒有設出 COMPOSE —— 拒絕繼續（空字串會讓後面的 \$COMPOSE 呼叫變成別的指令）" >&2
  exit 3
fi

# ── 前提：ollama 必須在跑且在 listen ────────────────────────────────────
section "前提"
if ! service_running ollama; then
  fail "ollama 容器沒有在跑 —— 量不到速率（這不代表通過）"
  exit 2
fi
ok "ollama 容器在跑"

# 從容器裡問 /api/tags：ollama 服務**刻意不 publish port**（設計如此），
# 所以主機上沒有端點可以打。這是本專案既有的慣用做法。
if ! $COMPOSE exec -T open-webui python3 -c "
import json,urllib.request,sys
try:
    json.loads(urllib.request.urlopen('http://ollama:11434/api/tags', timeout=30).read())
except Exception as e:
    sys.stderr.write('%s: %s\n' % (type(e).__name__, e)); sys.exit(1)
" 2>/dev/null; then
  fail "連不上 ollama 的 API —— 量不到速率（這不代表通過）"
  exit 2
fi
ok "ollama API 有回應"

# ── 機器的形狀 ────────────────────────────────────────────────────────
# 這一節不是裝飾。2 vCPU 換到 4 vCPU 之後速率幾乎沒變，而原因幾乎一定
# 藏在這些數字裡（guest 的 CPU 拓樸是「4 個 socket 各 1 核」，宿主是
# 大小核混合的 i7-1260P）。沒有這些，下一個人只會看到一組無法解釋的數字。
section "機器的形狀"
NPROC="$(nproc 2>/dev/null || echo 0)"
MEM_TOTAL_MB="$(awk '/^MemTotal:/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
info "vCPU = $NPROC，記憶體 = ${MEM_TOTAL_MB} MB"

# 這兩行也要收尾的 `|| echo ""`。上面兩行有、這兩行沒有是我自己的不一致，
# 而 lib.sh 的 errexit 加上 `pipefail` 會讓「awk 讀不到 /proc/cpuinfo」這種
# 小事在賦值那一行把整個量測殺掉 —— 就在印完「機器的形狀」標題之後，
# 看起來會像量測根本沒開始。抓不到就只是少一行資訊，不影響判定。
PHYS_IDS="$(awk -F': ' '/^physical id/ {print $2}' /proc/cpuinfo 2>/dev/null | sort -u | tr '\n' ' ' || echo "")"
SIBLINGS="$(awk -F': ' '/^siblings/ {print $2}' /proc/cpuinfo 2>/dev/null | sort -u | tr '\n' ' ' || echo "")"
info "physical id = {${PHYS_IDS% }}，siblings = {${SIBLINGS% }}"

# ollama 自己選的執行緒數。它寫在日誌裡，而且是**載入模型時**才寫的，
# 所以往下拉得夠遠。抓不到就只是少一行資訊，不影響判定。
# 用 `compose ps -q` 拿容器 id，不假設容器剛好叫 `ollama`。
OLLAMA_CID="$($COMPOSE ps -q ollama 2>/dev/null || true)"
if [[ -n "$OLLAMA_CID" ]]; then
  OLLAMA_LOG="$(docker logs "$OLLAMA_CID" 2>&1 || true)"
  THREADS_LINE="$(grep -oE 'n_threads = [0-9]+' <<<"$OLLAMA_LOG" | tail -1 || true)"
  [[ -n "$THREADS_LINE" ]] && info "ollama 的執行緒數：$THREADS_LINE"
  KV_LINE="$(grep -oE 'KV buffer size = +[0-9.]+ [KMGi]+B' <<<"$OLLAMA_LOG" | tail -1 || true)"
  [[ -n "$KV_LINE" ]] && info "日誌裡最後一次載入：$KV_LINE"
fi

# ── 量測 ──────────────────────────────────────────────────────────────
section "量測（$( [[ "$QUICK" == yes ]] && echo '快速模式' || echo '完整矩陣' )）"
info "模型載入約 30 秒；完整矩陣約 35 分鐘（每個條件取樣 7 次）。中途沒有進度輸出是正常的。"

PROBE_ARGS=()
[[ "$QUICK" == "yes" ]] && PROBE_ARGS+=(--quick)

# `--json` 要**傳進去**。少了這一行，探針照樣印人看的表格到 stdout，
# 而上面那個 `exec 3>&1 1>&2` 會把那份表格原封不動送進 JSON 通道 ——
# 檔案**非空**，所以看起來一切正常，直到有人 `json.load` 它。
# 那不是「缺了 JSON」，那是**一份假的 JSON**。
#
# 這一條在 D-030 已經修過一次，修在 verify-phase3-runtime.sh 的
# `[[ "$AS_JSON" == "yes" ]] && PROBE_ARGS+=(--json)`（引用寫內容不寫行號：
# 行號會安靜地過期）。
# 我寫這一支時又犯了一模一樣的錯 —— 同一個缺陷、換一支進入點，
# 而兩支之間沒有任何共用的東西會把修正帶過去。
[[ "$AS_JSON" == "yes" ]] && PROBE_ARGS+=(--json)

PROBE_ARGS+=("${PASSTHRU[@]}")

# 刻意**不用 `2>&1`**：探針在 --json 模式下把人看的報告寫到 stderr、
# 把 JSON 寫到 stdout，合併起來會讓人看的表格混進 JSON 裡。讓 stderr 自己流出去。
#
# **`if ... ; then` 這個寫法不是風格，是必要的。**
# lib.sh:5 是 `set -euo pipefail` —— 光是 source 它就會把 errexit 打開，
# 而這支腳本的其他部分都不是照 errexit 寫的。原本這裡寫的是
# `PROBE_OUT="$(...)"`，而探針回非 0 時（例如中斷那一輪回 1），errexit
# 會在**賦值那一行**直接把腳本殺掉：沒有人看的訊息、沒有 JSON、
# 沒有「判定」那一節，只有一個 1。我當初還在旁邊註解「這支腳本沒有開
# errexit」，那句話是假的，而**假話比沒有註解更糟**。
# 寫成 `if` 條件就與 errexit 無關：條件的失敗不會觸發 errexit，
# 而 `$?` 在 else 分支裡仍然是探針的結束碼。
if PROBE_OUT="$($COMPOSE exec -T open-webui python3 - "${PROBE_ARGS[@]}" < "$PROBE")"; then
  PROBE_RC=0
else
  PROBE_RC=$?
fi

if [[ "$PROBE_RC" -eq 3 ]]; then
  fail "探針自己壞了（參數錯誤）"
  exit 3
fi

if [[ -z "$PROBE_OUT" ]]; then
  fail "探針沒有輸出 —— 環境不具備或容器提早結束（這不代表通過）"
  exit 2
fi

if [[ "$AS_JSON" == "yes" ]]; then
  # 探針在 --json 模式下自己吐 JSON 到 stdout。這裡把它轉手到 fd 3
  # （＝真正的 stdout），人看的報告已經在 stderr 上了。
  #
  # **結束碼原樣傳出去。** 這裡原本是「非 0 就 exit 1」，於是把探針的
  # 2（量不到）講成了 1（準則不成立）—— 和探針內部那個假事實是同一個錯，
  # 只是換了一層再講一次。中斷的那一輪仍然會吐出一份**完整的 JSON**，
  # 裡面的 `measurement_error` 會說明原因，所以呼叫端兩種資訊都拿得到。
  printf '%s\n' "$PROBE_OUT" >&3
  exit "$PROBE_RC"
fi

# 非 JSON 模式：探針自己印報告。最後補上「基準線」那一行的判定，
# 因為那才是這支腳本存在的理由 —— 一個數字不是基準線。
section "判定"
printf '%s\n' "$PROBE_OUT"

# 「量不到」與「準則不成立」要講成兩句話（D-016／D-018）。合併成
# 「數字不可信」會讓下一輪的人去查上游哪裡變了，而實際上只是矩陣沒跑完。
#
# 而「量不到」自己還分兩種，它們要下一輪的人做**相反**的事：
#   - `--quick`：矩陣跑完了。是這個**模式**從設計上就不建立基準線（每條件
#     一個樣本）。要做的不是查，是把 `--quick` 拿掉再跑一次。
#   - 其餘：矩陣沒跑完，要去查上面的診斷。
# 這裡分得出來，是因為 `--quick` 是**呼叫端自己給的輸入**，不是量到的結果 ——
# 所以判斷它不需要去猜探針的輸出，也就沒有「解析人看的文字」這個脆弱點。
if [[ "$PROBE_RC" -eq 0 ]]; then
  ok "可以當基準線 —— 引用時必須連條件（模型、num_ctx、生成長度）一起引用"
elif [[ "$PROBE_RC" -eq 2 ]]; then
  if [[ "$QUICK" == "yes" ]]; then
    # 黃燈而不是紅燈：這是呼叫端自己選的模式，而且是它**預期**的結束碼。
    # 把使用者刻意選的模式畫成紅色失敗，只會訓練人忽略紅色（D-016）。
    # 「不是通過」由結束碼 2 承擔，訊息負責講清楚它也不是失敗。
    warn "這一輪是 \`--quick\`：每個條件只有一個樣本，**沒有嘗試**建立基準線 —— 不判定，既不是通過也不是失敗（理由見上面的「基準線判定」）。要基準線就拿掉 \`--quick\`，完整矩陣約 35 分鐘。"
  else
    fail "這一輪**量不到**（不是「準則不成立」）—— 矩陣沒有跑完，見上面的診斷"
  fi
else
  fail "這一輪的數字不可信（準則不成立，見上面的診斷）"
fi
exit "$PROBE_RC"
