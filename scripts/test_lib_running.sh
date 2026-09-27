#!/usr/bin/env bash
# 離線測試：lib.sh 的兩組函式。
#
#   A–I  「清單裡有沒有這一行」（line_in_list／container_running／
#        service_running／model_in_ollama／first_line）
#   J–K  Docker 前置檢查（docker_verdict／require_docker，#86）
#
# ── 為什麼 A–I 這幾個函式需要自己的測試 ──────────────────────
#
# 它們存在的唯一理由是**取代** `docker … | grep -qx NAME` 那個會誤判的形狀。
# 所以這支測試要證兩件事，缺一不可：
#
#   1. 它們與被取代的形狀**同意義**（整行相等、子字串不算、表頭要被濾掉…）
#      —— 少了這一半，改完可能只是「不再誤判」但「答案也變了」。
#   2. 在被取代的形狀**一定錯**的輸入上，它們是對的
#      —— 案例 D／E：假 docker 分兩次寫、中間隔 50 ms，舊寫法 100/100 誤判，
#      新函式照樣答對。這一條同時是**鑑別力**的證明（在腳本層級的同型案例
#      隨 #67 進 test_verify_mem0_add_cost_guard.sh）。
#
# ── 為什麼 J–K 也需要（#86）────────────────────────────────
#
# `require_docker` 原本把「沒裝 Docker」「裝了但 daemon 沒跑」「daemon 在跑
# 但沒有權限」收成同一句話，而它給的處方是**第四件事**（Codespaces 的
# docker-in-docker feature）。所以 K 段要證的不是「有印訊息」，而是
# **三份訊息兩兩不同**（K11）—— 那一條就是缺陷本身，它擋得住「併回一句」。
# 三個原因各自的處方見 `lib.sh` 與 D-063。
#
# ── 它**不**涵蓋什麼 ────────────────────────────────────────
#
#   · 真的 docker／compose（全部由假 docker 接掉；真 docker 的冒煙檢查是
#     跑一次 `bash scripts/status.sh`，不是這支）
#   · 呼叫端腳本的接線（那是各自的測試，這幾支目前**沒有** —— 見 commit）
#   · **12 支呼叫 `require_docker` 的腳本沒有任何一支被測到。** K 段測的是
#     這個函式本身；「每一支都在動到東西**之前**呼叫它」沒有東西守著。
#     這是已知的缺口，記在 D-063 的無測試聲明裡，不是被忘了。
#   · 假的訊息字串**在 2026-09-27 對真 docker 核對過了**（本機 29.1.3）。
#     核對的結果是措辭換了：Docker 29 講「docker API」，舊版講「docker daemon
#     socket」。判準 `permission denied` 在新舊兩種裡都在，所以分類本來就是
#     對的；但當時的假字串只有舊的，抓不到「把判準改成那串較長的舊片語」這種
#     編輯。現在兩種措辭都餵進去（K13–K16）＋一條對應的突變。
#   · 但**新措辭只在這一台、這一個版本上核對過**：別台機器／別的 Docker
#     （尤其是 VPS 上套件庫那種版本）若換一種講法，判準仍然只認那個子字串。
#
# ── 一個踩過的坑（留著當警訊）──────────────────────────────
#
# 假 docker 的模式**不可以**寫成 `STUB_PS=fail check "…" "$(yesno …)"`：
# 臨時賦值是在參數展開**之後**才進到環境裡，所以 `$(…)` 那時候看到的還是舊值
# （`FOO=bar cmd "$FOO"` 拿到空字串是同一件事）。模式一律獨立一行、而且要
# `export` —— 假 docker 是子行程。
#
# 用法：bash scripts/test_lib_running.sh
# 結束碼：0 = 全過；1 = 有案例沒過；3 = 前置檔案不齊（跑不起來）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 假的 docker：一支外部 bash 行程（**不是**內建），輸出極小 ──────────
# `ps` 預設就是那個危險形狀：**2 次寫、中間隔 50 ms**。這是刻意的 —— 有了那個
# 間隔，勝負不再取決於排程，案例 D／E 就不再靠運氣撞（間隔拿掉是 88/1000，
# 有間隔是 100/100；兩種都實測過）。
mkdir -p "$WORK/bin"
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
set -u
case "${1:-}" in
  # require_docker 用的。三個模式對應三個**原因**，而它們的處方完全不同
  # （#86）：沒裝、裝了沒跑、跑了但沒權限。
  #
  # `down` 與 `denied` 是 2026-09-27 在 **docker 29.1.3 上實測**回來的字串
  # —— 措辭已經換了：現在講「docker **API**」，舊版講「docker daemon socket」。
  # （實測時 DOCKER_HOST 指向暫時路徑，這裡換回預設路徑；措辭逐字相同。）
  # `*_legacy` 是 20.10 前後的舊措辭。
  #
  # **兩種措辭必須歸成同一個原因。** 判準是子字串 `permission denied`，不是
  # 整句。舊措辭裡有一串 `Docker daemon socket`（大寫 D）—— 若有人把判準改成
  # 去認那串較長的字，**舊機器上完全正確、新機器上把「沒權限」講成「沒在跑」**
  # （＝叫人去啟動一個已經在跑的 daemon）。那種錯只有餵新措辭才抓得到：
  # 突變台那一條在舊假字串下會活下來。K13–K16 ＋ 那兩條突變就是釘這件事。
  info)
    case "${STUB_INFO:-ok}" in
      ok)            echo "Server Version: 29.1.3"; exit 0 ;;
      down)          echo "failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is correct and if the daemon is running: dial unix /var/run/docker.sock: connect: no such file or directory" >&2; exit 1 ;;
      down_legacy)   echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2; exit 1 ;;
      denied)        echo "permission denied while trying to connect to the docker API at unix:///var/run/docker.sock" >&2; exit 1 ;;
      denied_legacy) echo "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock: Get \"http://%2Fvar%2Frun%2Fdocker.sock/v1.24/info\": dial unix /var/run/docker.sock: connect: permission denied" >&2; exit 1 ;;
    esac ;;
  ps)
    case "${STUB_PS:-split}" in
      split)  printf 'ollama\n'; sleep 0.05; printf 'open-webui\nmcp-test-server\n'; exit 0 ;;
      absent) printf 'open-webui\nmcp-test-server\n'; exit 0 ;;
      fail)   echo "假的：Cannot connect to the Docker daemon" >&2; exit 1 ;;
    esac ;;
  compose)
    case "${2:-}" in
      ps)
        case "${STUB_SVC:-split}" in
          split)  printf 'ollama\n'; sleep 0.05; printf 'open-webui\ncloudflared\n'; exit 0 ;;
          absent) printf 'open-webui\n'; exit 0 ;;
        esac ;;
      exec)
        # 表頭之後才是模型名 —— model_in_ollama 的 NR>1 靠這個案例守
        printf 'NAME\tID\tSIZE\tMODIFIED\n'
        printf 'qwen3:4b\tabc\t2.5 GB\t2 days ago\n'
        printf 'qwen3-embedding:0.6b\tdef\t639 MB\t3 days ago\n'
        exit 0 ;;
    esac
    exit 0 ;;
esac
echo "假 docker：沒有預期到的呼叫：$*" >&2
exit 1
STUB
chmod +x "$WORK/bin/docker"

# 一個**沒有 docker** 的 PATH。K 段測「根本沒裝」時要用它 —— 假 docker 只
# 模擬得了「裝了」，模擬不了「不存在」，而「沒裝」正是 #86 從頭到尾沒有被
# 區分出來的那一個原因。
mkdir -p "$WORK/nobin"

# 讓 $COMPOSE 指向假 docker（service_running／model_in_ollama 要它）
COMPOSE="docker compose"
export PATH="$WORK/bin:$PATH"

PASS=0
FAILED=0

# check <標籤> <期望：yes|no> <實際：yes|no>
check() {
  local label="$1" want="$2" got="$3"
  if [[ "$want" == "$got" ]]; then
    PASS=$((PASS + 1)); ok "$label"
  else
    FAILED=$((FAILED + 1)); fail "$label —— 期望 $want，得到 $got"
  fi
}

# 把「函式回 0／非 0」翻成 yes／no，並保證 set -e 不會把測試帶走
yesno() { if "$@" >/dev/null 2>&1; then printf 'yes'; else printf 'no'; fi; }
# 把兩個字串相不相等翻成 yes／no（用 if，不用 `&&`——後者在 set -e 下容易踩到）
same() { if [[ "$1" == "$2" ]]; then printf 'yes'; else printf 'no'; fi; }
# 子字串在不在 → yes／no（`same` 是全等；訊息比對要的是包含）
contains() { if [[ "$1" == *"$2"* ]]; then printf 'yes'; else printf 'no'; fi; }

echo "A：整行相等 —— 子字串不算（＝ grep -x 的意義）"
LIST=$'open-webui\nollama\nmcp-test-server'
check "A1 清單裡有 ollama"                  yes "$(yesno line_in_list "$LIST" ollama)"
check "A2 只有前綴（open-web）不算"          no  "$(yesno line_in_list "$LIST" open-web)"
check "A3 只有後綴（webui）不算"             no  "$(yesno line_in_list "$LIST" webui)"
check "A4 多一個字元（ollama2）不算"          no  "$(yesno line_in_list "$LIST" ollama2)"
check "A5 大小寫要分（Ollama 不算）"          no  "$(yesno line_in_list "$LIST" Ollama)"

echo
echo "B：空清單與空名稱 —— 兩個方向都要是「不在」"
# 空名稱若回「在」，呼叫端就會對一個空字串說「它正在跑」—— 那是最糟的方向。
check "B1 空清單"                            no  "$(yesno line_in_list "" ollama)"
check "B2 空名稱"                            no  "$(yesno line_in_list "$LIST" "")"

echo
echo "C：名稱裡的 glob 字元要當**字面值**"
# `[[ $x == *$pattern* ]]` 這種寫法會把 pattern 裡的 * ? [ 當成樣式 —— 名稱若含
# 這些字元就會配對到不該配對的行。這裡靠引號把 needle 固定成字面值。
check "C1 清單 a*b、名稱 a*b → 在"            yes "$(yesno line_in_list $'a*b\nother' 'a*b')"
check "C2 清單 axb、名稱 a*b → **不在**（樣式沒有被展開）" no "$(yesno line_in_list $'axb\nother' 'a*b')"
check "C3 清單 [abc]、名稱 [abc] → 在"        yes "$(yesno line_in_list $'[abc]\nother' '[abc]')"

echo
echo "D：container_running —— 假 docker **分兩次寫、中間隔 50 ms**"
# 這是那個 flake 的確定性版本：舊寫法在這裡必錯（下一條），新函式必須答對。
check "D1 ollama 在（生產者寫了兩次）"        yes "$(yesno container_running ollama)"
check "D2 open-webui 在"                     yes "$(yesno container_running open-webui)"
check "D3 zzz 不在"                          no  "$(yesno container_running zzz)"

echo
echo "E：對照組 —— **同一個假 docker**，舊寫法必須誤判（證明 D 有鑑別力）"
# 沒有這一條，D 可能只是「這套假 docker 什麼都答對」。
# 這也是為什麼 lib.sh 那組函式值得存在：同一個輸入，管線形式 100/100 錯。
old="yes"
if ! docker ps --format '{{.Names}}' | grep -qx ollama; then old="no"; fi
check "E1 舊寫法（管線＋grep -qx）誤判成「ollama 不在」" no "$old"
old2="yes"
if ! $COMPOSE ps --status running --services | grep -qx ollama; then old2="no"; fi
check "E2 舊寫法在 compose 那一條也誤判"      no "$old2"

echo
echo "F：連不上 docker／清單裡真的沒有 —— 回「不在」，而且不炸"
STUB_PS=fail; export STUB_PS
check "F1 docker ps 失敗 → 不在"             no  "$(yesno container_running ollama)"
STUB_PS=absent
check "F2 清單裡真的沒有 ollama → 不在"       no  "$(yesno container_running ollama)"
check "F3 清單裡有的仍然在"                   yes "$(yesno container_running open-webui)"
unset STUB_PS

echo
echo "G：service_running（compose 服務，實測寫入 2 次）"
STUB_SVC=split; export STUB_SVC
check "G1 ollama 在跑"                       yes "$(yesno service_running ollama)"
check "G2 cloudflared 在跑"                  yes "$(yesno service_running cloudflared)"
check "G3 minio 不在跑"                      no  "$(yesno service_running minio)"
STUB_SVC=absent
check "G4 只有 open-webui 時，ollama 不在跑"  no  "$(yesno service_running ollama)"
unset STUB_SVC

echo
echo "H：model_in_ollama —— 表頭（NAME）不可以被當成模型"
check "H1 qwen3:4b 在"                       yes "$(yesno model_in_ollama qwen3:4b)"
check "H2 qwen3-embedding:0.6b 在"            yes "$(yesno model_in_ollama qwen3-embedding:0.6b)"
check "H3 表頭 NAME **不算**在（NR>1 有生效）"  no  "$(yesno model_in_ollama NAME)"
check "H4 qwen3:8b 不在"                      no  "$(yesno model_in_ollama qwen3:8b)"

echo
echo 'I：first_line —— 取代 `| head -1` 而讀者不會提早離開'
# （上面用單引號是必要的：雙引號裡的反引號會被當成命令替換 —— 第一版就是這樣，
#   測試照樣全綠，但多噴一行 syntax error 蓋在標題上。）
check "I1 多行取第一行"                       yes "$(same "$(first_line $'lab_default\nother')" lab_default)"
check "I2 單行"                               yes "$(same "$(first_line 'one')" one)"
check "I3 空字串"                             yes "$(same "$(first_line '')" "")"
check "I4 開頭就是換行 → 回空字串"             yes "$(same "$(first_line $'\nolama')" "")"

echo
echo "J：docker_verdict —— 三個原因不可以收成一個（#86）"
# 舊版的 require_docker 把這三個收成同一句話，而它給的處方還是**第四件事**
# （Codespaces 的 docker-in-docker feature）。這一組逐格釘住分辨力。
check "J1 沒裝 → absent"                     absent        "$(docker_verdict no no no)"
check "J2 裝了、info 失敗 → not_running"      not_running   "$(docker_verdict yes no no)"
check "J3 裝了、權限不足 → no_permission"     no_permission "$(docker_verdict yes no yes)"
check "J4 info 成功 → ok"                    ok            "$(docker_verdict yes yes no)"
check "J5 info 成功時，殘留的 denied 不算（成功就是成功）" ok "$(docker_verdict yes yes yes)"
check "J6 沒裝勝過一切 —— 沒有指令就沒得問（即使別的旗標說 ok）" absent "$(docker_verdict no yes yes)"
echo "   （下面三條同一個家族：認不得的輸入不可以落到任何**正常**的 arm）"
check "J7 空的 bin **不是** absent（那會對一台可能裝了的機器宣稱沒裝）" unknown "$(docker_verdict "" no no)"
check "J8 空的 info_ok → unknown"            unknown       "$(docker_verdict yes "" no)"
check "J9 認不得的 denied → unknown"          unknown       "$(docker_verdict yes no maybe)"

echo
echo "K：require_docker 端到端 —— 三個原因的**訊息必須不一樣**"
# 這一組是 #86 的缺陷本體。只斷言「有印訊息」是不夠的：把三句併回一句也會過。
# 所以 K11 是最承重的一條 —— 它要求三份輸出兩兩不同。
kabsent=0
kabsent_out="$( ( PATH="$WORK/nobin"; require_docker ) 2>&1 )" || kabsent=$?

STUB_INFO=down
export STUB_INFO
kdown=0
kdown_out="$( require_docker 2>&1 )" || kdown=$?

STUB_INFO=denied
export STUB_INFO
kdenied=0
kdenied_out="$( require_docker 2>&1 )" || kdenied=$?

STUB_INFO=ok
export STUB_INFO
kok=0
kok_out="$( require_docker 2>&1 )" || kok=$?
unset STUB_INFO

# 舊措辭（Docker 20.10 以前，講「docker daemon socket」）。**同一個原因、
# 同一份處方** —— 這兩條走的是完全不同的字串，卻必須落在同一個 arm 上。
STUB_INFO=down_legacy
export STUB_INFO
kdownleg=0
kdownleg_out="$( require_docker 2>&1 )" || kdownleg=$?

STUB_INFO=denied_legacy
export STUB_INFO
kdeniedleg=0
kdeniedleg_out="$( require_docker 2>&1 )" || kdeniedleg=$?
unset STUB_INFO

check "K1 沒裝 → 結束碼 1（這是確定的失敗，不是無法判定）" yes "$(same "$kabsent" 1)"
check "K2 沒裝的訊息講的是「沒有安裝」"        yes "$(contains "$kabsent_out" '沒有安裝 Docker')"
check "K3 沒裝的訊息**不**講 daemon 連不上（那是另一個原因）" no "$(contains "$kabsent_out" '連不上 daemon')"
check "K4 daemon 沒跑 → 結束碼 1"             yes "$(same "$kdown" 1)"
check "K5 沒跑的訊息講的是 daemon 連不上"      yes "$(contains "$kdown_out" '連不上 daemon')"
check "K6 沒跑的訊息**不**宣稱「沒有安裝」（那會叫人去裝一個已經裝好的東西）" no "$(contains "$kdown_out" '沒有安裝 Docker')"
check "K7 權限不足 → 結束碼 1"                yes "$(same "$kdenied" 1)"
check "K8 權限的訊息講的是權限／群組"          yes "$(contains "$kdenied_out" '權限不足')"
check "K9 權限的訊息**不**宣稱「沒有安裝」"     no  "$(contains "$kdenied_out" '沒有安裝 Docker')"
check "K10 daemon 正常 → 結束碼 0"            yes "$(same "$kok" 0)"
# **最承重的一條。** 舊版三種原因共用一句，而它連「裝了沒」都沒區分 ——
# 這一條就是那個缺陷本身，而且它擋得住「把三句合併成一句」這種改法。
#
# 比對的是**整份訊息**，不是行：每句訊息有好幾行，用 `sort -u | wc -l` 數的
# 話會數到「不重複的行數」（實測 20），那個數字與「有幾種不同的訊息」無關。
# 第一版就是那樣寫的，而它連三句完全一樣都會回一個大於 3 的數字。
kdistinct=3
if [[ "$kabsent_out" == "$kdown_out" ]]; then kdistinct=$((kdistinct - 1)); fi
if [[ "$kabsent_out" == "$kdenied_out" ]]; then kdistinct=$((kdistinct - 1)); fi
if [[ "$kdown_out" == "$kdenied_out" ]]; then kdistinct=$((kdistinct - 1)); fi
check "K11 三個原因的訊息兩兩不同（併回一句就會失敗）" "3" "$kdistinct"
check "K12 daemon 正常時**不**印任何東西 —— 安靜才是成功" "" "$kok_out"
# K13／K14 比的是**整份訊息相等**：同一個原因不論 Docker 用哪種措辭講，處方
# 都必須一模一樣。這比「有印東西」強 —— 它同時擋住「多開一個 arm 給舊措辭」
# 這種會讓兩邊慢慢分岔的改法。
check "K13 舊措辭的「連不上」歸成同一個原因（同一份處方）" yes "$(same "$kdownleg_out" "$kdown_out")"
check "K14 舊措辭的「沒權限」歸成同一個原因（同一份處方）" yes "$(same "$kdeniedleg_out" "$kdenied_out")"
check "K15 舊措辭的「連不上」也是結束碼 1"                yes "$(same "$kdownleg" 1)"
check "K16 舊措辭的「沒權限」也是結束碼 1"                yes "$(same "$kdeniedleg" 1)"

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 與被取代的形狀同意義，且在被取代的形狀必錯的輸入上是對的；Docker 前置的三個原因各自有名字（#86）"
