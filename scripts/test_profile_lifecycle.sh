#!/usr/bin/env bash
# 測 scripts/profile-lifecycle.sh。
#
# 為什麼這支要存在：那支模組裡的差集決定「哪個容器要被砍掉」，而它**兩個
# 方向都會出錯，症狀卻相反**：
#   · 差集算得太小 → 殘留的 cloudflared 活下來，繼續對外服務，而所有輸出
#     都說它已經關了（這是實際發生過的缺陷，見 D-029）
#   · 差集算得太大 → 砍掉正在服務的容器。更貴，而且更安靜：使用者只會發現
#     服務突然中斷，不會知道是啟動腳本砍的
# 第二個方向特別危險，因為它最容易由「取得作用中清單失敗」觸發 —— 而那正是
# 一個 compose 檔語法錯誤的症狀。
#
# 所以每個判準都要有「該過的過」與「該擋的擋」（D-016／D-024），而斷言盯的是
# **可區辨的字串**（D-026 第五節）：`ollama` 與 `ollama-extra` 在「有沒有被
# 回報」上必須相反，只檢查「輸出非空」是測不出這件事的。
#
# 用法：bash scripts/test_profile_lifecycle.sh
# 結束碼：0 = 全過；1 = 有案例沒過

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/profile-lifecycle.sh"

PASS=0
FAILED=0

check() {   # <名稱> <期望> <實際>
  local name="$1" want="$2" got="$3"
  if [[ "$got" == "$want" ]]; then
    PASS=$((PASS + 1))
    ok "$name"
  else
    FAILED=$((FAILED + 1))
    fail "$name —— 期望「$want」，得到「$got」"
  fi
}

# 輸入用 tab 分隔的「容器名<TAB>服務名」；$'...' 讓 \t 是真的 tab。
# 輸出轉成空白分隔，讓斷言可以寫成一行。
sc() { stale_containers "$1" "$2" | tr '\n' ' '; }

# ═══════════════════════════════════════════════════════════
# stale_containers —— 哪個容器該被砍掉
# ═══════════════════════════════════════════════════════════

# ── 這個缺陷本身：停用 profile 之後留下的容器 ──
check "差集：作用中沒有 cloudflared，容器還在 —— 必須回報它" \
  "c3 " "$(sc $'ollama\nopen-webui' $'c1\tollama\nc2\topen-webui\nc3\tcloudflared')"
# 該擋的反面：全部都還在作用中，就一個都不該動。
check "差集：容器與作用中一致 —— 一個都不動（不可誤殺）" \
  "" "$(sc $'ollama\nopen-webui\ncloudflared' $'c1\tollama\nc2\topen-webui\nc3\tcloudflared')"
check "差集：多個殘留要全部列出" \
  "c2 c3 " "$(sc 'keep' $'c1\tkeep\nc2\tgated\nc3\tgated2')"
check "差集：容器清單是空的 —— 沒有東西可動" \
  "" "$(sc $'ollama\nopen-webui' '')"
check "差集：容器清單有空的 service 欄（省略 tab）—— 不當成殘留" \
  "" "$(sc 'ollama' $'c1\tollama\nc2')"

# ── 這兩個是 `grep -x` 與 `grep -F` 各自的存在理由 ──
#
# **方向要對。** 這一條的第一版寫成 active=`ollama`、service=`ollama-extra`，
# 而那個突變**存活**了：`grep -qF "ollama-extra"` 拿去比對 `ollama` 這一行
# 本來就配不到（模式比行還長），加不加 `-x` 結果完全相同。要讓 `-x` 有意義，
# **作用中的那一行必須比服務名長** —— 這樣「子字串匹配」才配得到、而
# 「整行匹配」配不到。
check "差集：作用中的服務名比容器服務長 —— 不可因子字串而視為同一個（-x）" \
  "c1 " "$(sc 'ollama-extra' $'c1\tollama')"
# 反方向也要成立（模式比行長時兩者都不配），免得修正 -x 時把這條弄壞。
check "差集：容器的服務名比作用中的長 —— 一樣不可視為同一個" \
  "c2 " "$(sc 'ollama' $'c1\tollama\nc2\tollama-extra')"
# 少了 -F：服務名裡的 `.` 變成「任何字元」→ 誤配。
# **這條的形狀是刻意挑的**：作用中必須有一個「會被那個正則匹配到、但字面上
# 不相等」的名字。第一版寫成 active=`ollama`、service=`ollama.`，那個突變
# **存活**了 —— 因為 `ollama.` 當正則要 7 個字元，而 `ollama` 只有 6 個，
# 配不到，加了 -F 與否結果相同。一條區辨不出突變的斷言等於沒寫。
check "差集：服務名含 . 不可當成正則（-F）—— 字面不同就必須回報" \
  "c1 " "$(sc 'ab' $'c1\ta.')"
check "差集：服務名含 [ ] 不可當成正則（-F）" \
  "c1 " "$(sc 'a1' $'c1\ta[1]')"
# 反過來也要成立：真的相同時不可以被當成不同（否則會誤殺）。
check "差集：完全相同時視為同一服務（不可因 -F/-x 反而漏配）" \
  "" "$(sc 'ollama-extra' $'c1\tollama-extra')"

# ── 空清單守衛：這一條擋的是「誤殺整個堆疊」 ──
# `docker compose config --services` 在 compose 檔有語法錯誤時**回 1 且沒有
# 輸出**。少了這道守衛，「作用中」會是空的，於是差集變成「全部」。
check "守衛：作用中清單為空時，什麼都不動（config 失敗的形狀）" \
  "" "$(sc '' $'c1\tollama\nc2\topen-webui\nc3\tcloudflared')"
check "守衛：兩邊都空" "" "$(sc '' '')"
# 守衛不可以反過來把正常情況也吃掉 —— 只有一個服務、且它不在清單裡時仍要回報。
check "守衛：作用中只有一個服務時仍然正常運作" \
  "c2 " "$(sc 'keep' $'c1\tkeep\nc2\tcloudflared')"

# ── 沒有 service 標籤的容器：不確定就不動 ──
check "標籤：空 service 的容器不碰（無法判斷屬不屬於這個專案）" \
  "c2 " "$(sc 'ollama' $'c1\t\nc2\tcloudflared')"
check "標籤：空名稱的行整個略過" \
  "" "$(sc 'ollama' $'\tollama')"

# ── 輸出必須是「容器名」而不是「服務名」 ──
# 這條守的是介面契約：呼叫方拿輸出直接餵 `docker rm -f`，回服務名會刪錯東西
# （而且 `docker rm -f cloudflared` 在 profile 停用時根本找不到容器）。
check "輸出：回的是容器名，不是服務名" \
  "my-cf-container " "$(sc 'ollama' $'my-cf-container\tcloudflared')"

# ═══════════════════════════════════════════════════════════
# tunnel_state_verdict —— status.sh 說的是實話嗎
# ═══════════════════════════════════════════════════════════
# 四態。`stale` 是這個函式存在的理由：設定檔說關了、容器說還開著 ——
# 舊版的 status.sh 只看設定檔，所以它會把這一態印成「未啟用」。
check "對外：profile 開、容器跑 —— active" "active" "$(tunnel_state_verdict yes yes)"
check "對外：profile 開、容器沒跑 —— enabled-but-down" "enabled-but-down" "$(tunnel_state_verdict yes no)"
check "對外：profile 關、容器還在跑 —— **stale（舊版會說「未啟用」的那一態）**" \
  "stale" "$(tunnel_state_verdict no yes)"
check "對外：profile 關、容器也沒跑 —— off" "off" "$(tunnel_state_verdict no no)"
# 參數缺省時的預設必須落在安全的一邊：「沒說」不等於「開著」，
# 但也不等於「關著」—— 兩個都給 no 時是 off，也就是不會謊報有連線。
check "對外：兩個參數都沒給 —— off（不謊報有連線）" "off" "$(tunnel_state_verdict)"
# 四態必須兩兩可區辨：把四個輸出排起來不該有重複。
check "對外：四態互不相同" "active enabled-but-down off stale" \
  "$(printf '%s\n' "$(tunnel_state_verdict yes yes)" "$(tunnel_state_verdict yes no)" \
      "$(tunnel_state_verdict no no)" "$(tunnel_state_verdict no yes)" | sort | tr '\n' ' ' | sed 's/ $//')"

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 差集的兩個方向（漏清／誤殺）、空清單守衛、標籤邊界，以及對外連線的四態都有測試守著"
