#!/usr/bin/env bash
# 量測 ollama **下載期間**的磁碟峰值（D-058 §七 協定 A）。
#
# 為什麼需要這支：磁碟閘門的 `DISK_PULL_PEAK_MULTIPLIER` 原本寫著 `2`，而那個
# 2 **沒有量測根據** —— 它的來源是一條自我引用的閉環（README 上無引註的表格 →
# commit 自稱查證 → 程式註解又回頭引 README），而同一份 README 裡還寫著 ollama
# 支援續傳，與「壓縮檔＋解壓檔同時佔用」互相矛盾（D-058 §六）。
#
# 做什麼：建一個**獨立的 volume ＋ 第二個 ollama 容器**（完全不碰生產堆疊），
# 每秒採樣 (時間, volume 內位元組, partial 檔數, 檔案系統可用位元組)，然後對
# 指定模型拉一次。
#
# **兩個獨立的估計值，刻意都印出來**：一個從「可用空間掉了多少」算，一個從
# 「volume 裡實際佔了多少區塊」算。前者會被機器上其他寫入者污染（生產堆疊一直
# 在寫 log），後者不會 —— 兩者不一致本身就是一個要看的訊號。
#
# **決策規則用的是「可用空間」那一個** —— 那是 D-058 第七節在**看到數據之前**
# 就寫下來的，不可以在這裡事後改成另一個。volume 那一個是交叉檢查：它不該被
# 別的寫入者污染，所以兩者差多少，就等於污染量有多少。
#
# **佔用量必須用 `st_blocks * 512`，不是 `st_size`。** ollama 會把 blob **預先
# 配置到完整大小**（稀疏檔案），所以 `st_size` 在開始下載的下一秒就等於成品
# 大小了 —— 拿它當佔用量，這一支會回報一個「峰值永遠等於最終」的假答案。
# 實測證據見 docs/evidence/2026-09-26-pull-preallocation-diagnostic.txt：
# 同一時刻 apparent 5,225,377,718 B vs allocated 732,086,272 B。
#
# 用法：
#   bash scripts/measure-pull-peak.sh [--model NAME] [--min-free-gb N] [--keep]
#
#   --model NAME       要拉的模型（預設 qwen3:8b）
#   --min-free-gb N    可用空間低於這個數就拒絕開始（預設 8）
#   --keep             量完**保留**暫時的 volume 與 ollama 容器供檢查
#                      （取樣容器一律移除 —— 它只是儀器，而且是唯讀掛載的）
#
# 結束碼：0 = 量到（輸出含 peak_extra 與 multiplier）
#         1 = 判定失敗（可用空間太少、暫時物件已存在、取樣映像不在本機）
#         2 = 無法判定（容器起不來、pull 失敗、一個樣本都沒有）
#         3 = 這支腳本自己壞掉（參數錯誤）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# 只為了重用它那兩個已經有測試守著的純函式（`is_gb_number`／`image_in_list`）。
source "$(dirname "${BASH_SOURCE[0]}")/deploy-vps-decisions.sh"

VOL="pullpeak-volume"
OLLAMA_C="pullpeak-ollama"
SAMPLER_C="pullpeak-sampler"
SAMPLER_IMAGE="python:3.12-slim"
PROD_C="ollama"

# 這支腳本自己會下載一整個模型，所以它必須先確定自己不會把磁碟吃滿 ——
# 一次量測失敗可以重跑，一台被寫滿的機器不行。
MIN_FREE_GB=8

MODEL="qwen3:8b"
KEEP=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       [[ $# -ge 2 ]] || { fail "--model 需要一個值"; exit 3; }; MODEL="$2"; shift 2 ;;
    --min-free-gb) [[ $# -ge 2 ]] || { fail "--min-free-gb 需要一個值"; exit 3; }; MIN_FREE_GB="$2"; shift 2 ;;
    --keep)        KEEP=1; shift ;;
    -h|--help)     usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知參數：$1（--help 看用法）"; exit 3 ;;
  esac
done

if [[ -z "$MODEL" ]]; then
  fail "--model 不可以是空字串"
  exit 3
fi
if [[ "$(is_gb_number "$MIN_FREE_GB")" != "yes" ]] || (( MIN_FREE_GB < 1 )); then
  fail "--min-free-gb 必須是正整數：$MIN_FREE_GB"
  exit 3
fi

require_docker

WORK="$(mktemp -d)"

# 冪等的拆除。**必須能被明確呼叫一次、再被 EXIT trap 呼叫一次** —— 因為
# 「docker system df 後」那張快照必須在**我們自己的物件都消失之後**才拍，
# 否則它會把自己算進去，那個前後對照就變成假的了。
remove_objects() {
  if [[ -n "${LOGTAIL_PID:-}" ]]; then
    kill "$LOGTAIL_PID" 2>/dev/null || true
    wait "$LOGTAIL_PID" 2>/dev/null || true
    LOGTAIL_PID=""
  fi
  docker rm -f "$SAMPLER_C" >/dev/null 2>&1 || true
  if (( ! KEEP )); then
    docker rm -f "$OLLAMA_C" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
  fi
}
cleanup() { remove_objects; rm -rf "$WORK"; }
trap cleanup EXIT

# ── 前置檢查 ────────────────────────────────────────────
for c in "$OLLAMA_C" "$SAMPLER_C"; do
  if docker container inspect "$c" >/dev/null 2>&1; then
    fail "已經有一個叫 $c 的容器 —— 先移除它再跑（也許是上一次 --keep 留下的）。"
    exit 1
  fi
done
if docker volume inspect "$VOL" >/dev/null 2>&1; then
  fail "已經有一個叫 $VOL 的 volume —— 先移除它再跑（也許是上一次 --keep 留下的）。"
  exit 1
fi

# 用生產容器**實際在跑的那個映像**，不是寫死一個名字 —— 寫死會在 compose 換了
# 映像之後，拿一個不同的 ollama 去量（與 deploy-vps.sh 的 IMG_WANTED 同一個理由）。
OLLAMA_IMAGE="$(docker inspect --format '{{.Config.Image}}' "$PROD_C" 2>/dev/null || true)"
if [[ -z "$OLLAMA_IMAGE" ]]; then
  OLLAMA_IMAGE="ollama/ollama:latest"
  warn "生產容器 $PROD_C 不在，改用 $OLLAMA_IMAGE 量 —— 這是一個**假設**，不是觀察。"
fi

# 兩個映像都必須已經在本機：量測期間自己去拉一個映像，會把那個下載記進它正在
# 量的數字裡 —— 那正是這支腳本最不該犯的錯。
IMG_LOCAL="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true)"
for img in "$SAMPLER_IMAGE" "$OLLAMA_IMAGE"; do
  if [[ "$(image_in_list "$IMG_LOCAL" "$img")" != "yes" ]]; then
    fail "映像 $img 不在本機 —— 先手動拉它再跑這支腳本。"
    exit 1
  fi
done

DISK_PATH="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [[ -z "$DISK_PATH" || ! -d "$DISK_PATH" ]]; then
  DISK_PATH="$PROJECT_ROOT"
  warn "讀不到 DockerRootDir，改用 $DISK_PATH 當磁碟的判準。"
fi
FREE_GB="$(df -Pk "$DISK_PATH" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}' || true)"
if [[ "$(is_gb_number "${FREE_GB:-}")" != "yes" ]] || (( FREE_GB < MIN_FREE_GB )); then
  fail "可用空間 ${FREE_GB:-?} GB，低於下限 ${MIN_FREE_GB} GB。"
  fail "這支腳本在量一個模型要多少空間 —— 自己先把磁碟吃滿就證明不了任何事。"
  exit 1
fi

info "模型：$MODEL"
info "ollama 映像：$OLLAMA_IMAGE"
info "磁碟：$DISK_PATH，可用 ${FREE_GB} GB（下限 ${MIN_FREE_GB} GB）"

ENV_MD5_BEFORE="$(md5sum "$PROJECT_ROOT/.env" 2>/dev/null | cut -d' ' -f1 || true)"
DF_BEFORE="$(docker system df 2>/dev/null || true)"

# ── 取樣器（唯讀掛載，只是儀器）─────────────────────────
cat > "$WORK/sampler.py" <<'PY'
import os, time

# 每列：epoch  表觀(st_size)  實際佔用(st_blocks*512)  partial檔數  可用位元組
#
# 表觀與實際**兩個都印**是刻意的，不是冗餘：ollama 預先把 blob 配置成完整大小的
# 稀疏檔，所以表觀值會在開始下載的下一秒就跳到成品大小，實際值才會慢慢長。
# 兩個並排，那個落差本身就是這一支為什麼要改的理由。
while True:
    apparent = 0
    allocated = 0
    partial = 0
    for root, _dirs, files in os.walk('/data'):
        for f in files:
            try:
                st_f = os.stat(os.path.join(root, f))
            except OSError:
                continue      # 正在被建立或改名 —— 這一刻它還不算數
            apparent += st_f.st_size
            allocated += st_f.st_blocks * 512
            if 'partial' in f:
                partial += 1
    st = os.statvfs('/data')
    print(f"{time.time():.3f} {apparent} {allocated} {partial} {st.f_bavail * st.f_frsize}", flush=True)
    time.sleep(1)
PY

docker volume create "$VOL" >/dev/null

if ! docker run -d --name "$SAMPLER_C" \
      -v "$VOL:/data:ro" -v "$WORK/sampler.py:/sampler.py:ro" \
      --entrypoint python3 "$SAMPLER_IMAGE" /sampler.py >/dev/null; then
  fail "取樣容器起不來。"
  exit 2
fi

docker logs -f "$SAMPLER_C" > "$WORK/samples.txt" 2>&1 &
LOGTAIL_PID=$!

if ! docker run -d --name "$OLLAMA_C" \
      -v "$VOL:/root/.ollama" "$OLLAMA_IMAGE" >/dev/null 2>&1; then
  fail "第二個 ollama 容器起不來。"
  exit 2
fi

# 等 server 真的能回答 —— `docker run -d` 回傳不代表 ollama 已經在聽。
ready=0
for _ in $(seq 1 60); do
  if docker exec "$OLLAMA_C" ollama list >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if (( ! ready )); then
  fail "第二個 ollama 容器 60 秒內沒有開始回答。"
  exit 2
fi

# 等**至少兩個下載前的樣本**落地。不等就直接讀，會讀到一個空的檔案 ——
# 然後基準值變成空字串，而空字串在算術裡不會報錯，只會生出一個垃圾數字。
for _ in $(seq 1 30); do
  [[ "$(wc -l < "$WORK/samples.txt")" -ge 2 ]] && break
  sleep 1
done
AVAIL_BEFORE="$(tail -1 "$WORK/samples.txt" 2>/dev/null | awk '{print $5}')"
if [[ ! "$AVAIL_BEFORE" =~ ^[0-9]+$ ]]; then
  fail "取樣器沒有給出可用的基準值（讀到「$AVAIL_BEFORE」）。"
  exit 2
fi

# ── 量測 ────────────────────────────────────────────────
T0="$(date +%s)"
info "開始下載（這一刻之後的樣本都算進峰值）..."

if ! docker exec "$OLLAMA_C" ollama pull "$MODEL"; then
  T1="$(date +%s)"
  fail "pull 失敗（牆上時間 $((T1 - T0)) 秒）—— 這不是「峰值很小」，是量不到。"
  echo "  取樣數：$(wc -l < "$WORK/samples.txt")"
  exit 2
fi
T1="$(date +%s)"

# 讓最後幾個樣本寫完：ollama 回報完成與檔案真正落地之間有時間差。
sleep 2
if [[ -n "${LOGTAIL_PID:-}" ]]; then
  kill "$LOGTAIL_PID" 2>/dev/null || true
  wait "$LOGTAIL_PID" 2>/dev/null || true
  LOGTAIL_PID=""
fi

if [[ ! -s "$WORK/samples.txt" ]]; then
  fail "一個樣本都沒有 —— 取樣器沒有輸出。"
  exit 2
fi

# 「下載開始後」的樣本。時間戳有小數而 T0 是整數秒，所以門檻用 T0 只會多含
# 最多一個下載前的樣本（那會讓峰值算得**更保守**，不會更寬鬆）。
awk -v t0="$T0" '$1 >= t0' "$WORK/samples.txt" > "$WORK/during.txt"
if [[ ! -s "$WORK/during.txt" ]]; then
  fail "下載期間一個樣本都沒有（下載比取樣間隔還快？）。"
  exit 2
fi

# 欄位對應取樣器的輸出：1=epoch 2=表觀 3=實際佔用 4=partial數 5=可用位元組。
#
# `last_*` 取的是**最後一筆**樣本 —— 而 pull 回來之後又多睡了 2 秒才收工，所以
# 那一筆是下載**完成後**的狀態。「最終」是真的最終，不是下載中途的某一瞬間。
STATS="$(awk '
  { if (min_avail == "" || $5 < min_avail) min_avail = $5
    if ($2 > peak_app)   peak_app = $2
    if ($3 > peak_alloc) peak_alloc = $3
    if ($4 > pmax)       pmax = $4
    last_app   = $2
    last_alloc = $3
    n++ }
  END { printf "%s %s %s %s %s %s %s",
        min_avail, last_app, last_alloc, peak_app, peak_alloc, pmax, n }' \
  "$WORK/during.txt")"
read -r AVAIL_MIN FIN_APP FIN_ALLOC PEAK_APP PEAK_ALLOC PARTIAL_MAX SAMPLE_N <<<"$STATS"

# pull 成功卻量到 0 個位元組，只可能是量測壞了。**不可以讓它往下走** ——
# 分母是 0 的那條路徑會生出一個「倍數 = 0」的假答案，而 0 看起來像好消息。
if [[ ! "$FIN_ALLOC" =~ ^[0-9]+$ ]] || (( FIN_ALLOC <= 0 )); then
  fail "最終佔用量不是正整數（讀到「$FIN_ALLOC」）—— 這不是「倍數很小」。"
  exit 2
fi

echo
echo "── 取樣（每秒一筆：epoch 表觀 實際佔用 partial數 可用位元組）──"
cat "$WORK/samples.txt"
echo
echo "── 結果 ──────────────────────────────"
awk -v ab="$AVAIL_BEFORE" -v am="$AVAIL_MIN" \
    -v fapp="$FIN_APP" -v falloc="$FIN_ALLOC" \
    -v papp="$PEAK_APP" -v palloc="$PEAK_ALLOC" \
    -v pm="$PARTIAL_MAX" -v n="$SAMPLE_N" \
    -v secs="$((T1 - T0))" -v model="$MODEL" '
  function gb(b) { return b / 1024 / 1024 / 1024 }
  BEGIN {
    # D-058 §七 的 peak_extra 定義是「開始前的可用 − 下載期間最低的可用」，也就是
    # **整段下載淨吃掉的量，含模型本體** —— 不是「超出成品的那一段」。
    # 名字很容易讀成後者（我一開始就讀錯了），但定義是明確的：除以最終模型大小
    # 之後拿到的**就是倍數**，這裡沒有漏掉的 +1。
    peak_extra = ab - am
    extra_vol  = palloc - falloc
    m_avail = peak_extra / falloc
    m_vol   = palloc / falloc
    pollute = peak_extra - palloc
    m_final = int(m_avail * 10 + 0.9999) / 10
    if (m_final < 1.0) m_final = 1.0

    printf "模型：%s\n", model
    printf "下載牆上時間：%d 秒（下載期間取樣 %d 筆）\n", secs, n
    printf "partial 檔數最大值：%d\n", pm

    printf "\n── volume 內（交叉檢查，不受其他寫入者污染）──\n"
    printf "最終實際佔用（st_blocks*512）：  %d B = %.3f GiB\n", falloc, gb(falloc)
    printf "最終表觀（st_size）：            %d B = %.3f GiB\n", fapp, gb(fapp)
    printf "峰值實際佔用：                   %d B = %.3f GiB\n", palloc, gb(palloc)
    printf "峰值表觀（預先配置造成的）：     %d B = %.3f GiB\n", papp, gb(papp)
    printf "瞬時超額（實際峰值 − 實際最終）：%d B = %.3f GiB\n", extra_vol, gb(extra_vol)

    printf "\n── 可用空間（**決策規則用這一組**）──\n"
    printf "開始前：                         %d B\n", ab
    printf "下載期間最低：                   %d B\n", am
    printf "peak_extra（開始前 − 最低）：    %d B = %.3f GiB\n", peak_extra, gb(peak_extra)

    printf "\n倍數（決策規則，由可用空間算）：%.3f\n", m_avail
    printf "倍數（交叉檢查，由 volume 算）：%.3f\n", m_vol
    printf "兩者的差：                       %d B = %.3f GiB\n", pollute, gb(pollute)
    if (pollute < 0)
      printf "  ↳ 是負的 —— volume 那側反而比較大，代表可用空間的記帳有延遲。\n"
    else
      printf "  ↳ 是正的 —— 差額就是其他寫入者（生產堆疊一直寫的 log）吃的。\n"

    printf "\n決策規則（D-058 §七）：看由可用空間算的那一個。\n"
    if (m_avail <= 1.05)
      printf "  → %.3f ≤ 1.05：**刪掉 DISK_PULL_PEAK_MULTIPLIER**，模型項寫成 g。\n", m_avail
    else
      printf "  → 進位到小數一位、下限 1.0 → DISK_PULL_PEAK_MULTIPLIER=%.1f\n", m_final
  }'

# ── 收尾斷言 ────────────────────────────────────────────
echo
echo "── 收尾 ──────────────────────────────"
if (( KEEP )); then
  warn "（--keep）暫時物件保留中：容器 $OLLAMA_C、volume $VOL"
  remove_objects   # 取樣器一定要走；volume 與 ollama 容器由 KEEP 擋住
else
  remove_objects
  if docker volume ls --format '{{.Name}}' | grep -qx "$VOL"; then
    fail "暫時 volume $VOL 還在"
  else
    ok "暫時的容器與 volume 都已移除"
  fi
fi

ENV_MD5_AFTER="$(md5sum "$PROJECT_ROOT/.env" 2>/dev/null | cut -d' ' -f1 || true)"
if [[ "$ENV_MD5_BEFORE" == "$ENV_MD5_AFTER" ]]; then
  ok ".env 沒有被動到（md5 $ENV_MD5_AFTER）"
else
  fail ".env 的 md5 變了：$ENV_MD5_BEFORE → $ENV_MD5_AFTER"
fi

echo
echo "── docker system df：前 ────────────────"
echo "$DF_BEFORE"
echo "── docker system df：後 ────────────────"
docker system df 2>/dev/null || true
echo
warn "前後不會**逐字相等**是預期的 —— 生產堆疊一直在寫 log。"
warn "要看的不是相等，而是 volume 的數量回到原值、且沒有一個叫 $VOL 的殘留。"
