#!/usr/bin/env bash
# 離線測試：lib.sh 的「清單裡有沒有這一行」那組函式（line_in_list／
# container_running／service_running／model_in_ollama／first_line）。
#
# ── 為什麼這幾個函式需要自己的測試 ──────────────────────────────
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
# ── 它**不**涵蓋什麼 ────────────────────────────────────────
#
#   · 真的 docker／compose（全部由假 docker 接掉；真 docker 的冒煙檢查是
#     跑一次 `bash scripts/status.sh`，不是這支）
#   · 呼叫端腳本的接線（那是各自的測試，這幾支目前**沒有** —— 見 commit）
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
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 與被取代的形狀同意義，且在被取代的形狀必錯的輸入上是對的"
