#!/usr/bin/env bash
# 共用函式庫。所有 scripts/*.sh 都應先 source 此檔。
# 用法： source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

set -euo pipefail

# 專案根目錄（scripts/ 的上一層）
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PROJECT_ROOT

# ── 輸出 ────────────────────────────────────────────────
info()  { printf '\033[0;34m→\033[0m %s\n' "$*"; }
ok()    { printf '\033[0;32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m!\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[0;31m✗\033[0m %s\n' "$*" >&2; }

# ── Ollama 模型名稱正規化 ───────────────────────────────
# ollama 把 "bge-m3" 與 "bge-m3:latest" 當成同一個模型，而 `ollama list`
# 一律顯示帶 tag 的形式。因此「使用者打的名字」與「清單裡的名字」字面上
# 經常不相等 —— 直接比對字串就會把一個明明已下載的模型誤報成缺漏。
#
# 這不是理論問題：實測 `bash scripts/rag_probe.sh bge-m3` 在 bge-m3 已下載
# 的情況下仍印出「以下模型尚未下載：bge-m3」，然後叫使用者去執行一個
# 只會回他「已存在，略過下載」的指令。那正是本專案最想避免的假失敗 ——
# 叫人去查一個不存在的問題。
#
# tag 的判定要小心：只有「最後一個 ':' 出現在最後一個 '/' 之後」才是 tag。
# 前者可能是 registry 的埠號（localhost:5000/foo），那是名字的一部分。
normalize_model() {
  local m="${1:-}"
  if [[ "${m##*/}" == *:* ]]; then
    printf '%s' "$m"
  else
    printf '%s:latest' "$m"
  fi
}

# ── Docker Compose 偵測 ─────────────────────────────────
# Codespaces 提供 compose v2 外掛（docker compose）；
# 部分較舊環境只有 v1 獨立執行檔（docker-compose）。
detect_compose() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
    warn "偵測到舊版 docker-compose (v1)，建議升級至 Compose v2"
  else
    fail "找不到 docker compose 或 docker-compose"
    fail "請確認 Docker 已安裝，且 daemon 正在執行：docker info"
    exit 1
  fi
  export COMPOSE
}

# ── 等待 open-webui 就緒 ────────────────────────────────
# 需要 $COMPOSE（先呼叫 detect_compose）與執行中的 open-webui 容器。
# 必須用容器內的 /health，不是主機的 3000 埠 —— 埠的綁定位址是可設定的
# （WEBUI_BIND_ADDR），而且重啟期間主機埠會先關再開。容器內的 8080 才是
# 服務本身是否活著。
#
# 為什麼要等而不是 sleep 固定秒數：啟動時間取決於首次開機是否要下載嵌入
# 模型（實測 100–110 秒，慢速連線時更久）。固定的 sleep 不是等太久就是
# 等不夠，而等不夠會讓後續的檢查對著一個還沒起來的服務下判斷。
wait_for_webui() {
  local limit="${1:-60}" i
  for ((i = 0; i < limit; i++)); do
    if $COMPOSE exec -T open-webui python3 -c \
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)" \
        >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

# ── 「這一行在不在這份清單裡」：不開子行程，所以結構上不可能踩到 SIGPIPE ──
#
# 為什麼要有這幾個函式：`docker … | grep -qx NAME` 這個形狀**會誤判**。
# `grep -q` 一配對到就離開，生產者只要還會再寫一次（哪怕只多一行），那一次就
# 吃 EPIPE → 生產者死於 141（SIGPIPE）→ 上面第 5 行的 pipefail 把整條管線變成
# 非零 → 呼叫端的 `!` 就讀成「服務不在跑」。**判準是「讀者離開之後，生產者還會
# 不會再寫」**，與「是不是內建」「有沒有超過 64 KiB」都無關：
#
#   · 實測 `docker compose ps --services`／`--format` 各寫 **2 次** → 會發作
#   · 實測 `docker ps --format '{{.Names}}'`、`docker inspect` 各寫 **1 次**
#     → 今天不發作，但那是**量出來的**，不是結構保證的（換一版 docker、
#       多一行警告就可能變 2 次）
#   · 測試裡的假 docker（一支外部 bash 行程、總共 18 位元組、2 次寫）實測讓
#     同型的檢查誤判 88/1000 次；把兩次寫拉開 20 ms 是 100/100。確定性版本見
#     test_lib_running.sh 的案例 D（新函式要答對）與 E（同一支假 docker，
#     被取代的舊形狀必須答錯 —— 對照組）
#
# 所以這裡一律**先把輸出收下來**，再用 shell 自己的樣式整行比對：完全不開
# 子行程，沒有任何行程會吃到 EPIPE。這是**結構上**不可能，不是量不到
# （D-037 的規矩）。**新寫的前置檢查請用這幾個函式，不要再寫管線。**
line_in_list() {   # <多行清單> <要比對的整行>；整行相等（＝ grep -x）
  local list="$1" needle="$2"
  # 空字串一律回「不在」。grep 會把它配對到空行，這裡嚴格一點 —— 我們的清單
  # 不會有空行，而「把空的名稱讀成在跑」是更糟的那個方向。
  [[ -n "$needle" ]] || return 1
  [[ $'\n'"$list"$'\n' == *$'\n'"$needle"$'\n'* ]]
}

# ＝ `docker ps --format '{{.Names}}' | grep -qx <容器名>`
container_running() {   # <容器名>
  local names
  names="$(docker ps --format '{{.Names}}' 2>/dev/null || true)"
  line_in_list "$names" "$1"
}

# ＝ `$COMPOSE ps --status running --services | grep -qx <服務名>`
# 需要先呼叫 detect_compose（$COMPOSE 才會有值）。
service_running() {   # <服務名>
  local services
  services="$($COMPOSE ps --status running --services 2>/dev/null || true)"
  line_in_list "$services" "$1"
}

# ＝ `$COMPOSE exec -T ollama ollama list | awk 'NR>1 {print $1}' | grep -qx <模型名>`
# NR>1 是濾掉表頭 —— 少了它，表頭那行 `NAME` 會被當成一個模型名。
model_in_ollama() {   # <模型名>
  local models
  models="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' || true)"
  line_in_list "$models" "$1"
}

# 多行字串的第一行（＝ `… | head -1`，但讀者不會提早離開：這裡沒有子行程）
first_line() {   # <多行字串>
  printf '%s' "${1%%$'\n'*}"
}

# ── `--help` 要印的檔頭說明 ─────────────────────────────
# 印出**開頭連續的註解區塊**，遇到第一行非註解、非空行就停。`# ` 前綴不剝
# （呼叫端自己接 `| sed 's/^# \{0,1\}//'`）。
#
# 為什麼不能靠「位置」決定印到哪一行：位置會過期，而過期**沒有症狀**。2026-09-22
# 把 HEAD 的舊範圍逐支套回真正的檔頭比對（9 支有 `--help` 的腳本），結果是
#
#   寫死行號 `sed -n '2,Np'` —— **4 支在印程式碼**：
#     check-egress            2,40p  （檔頭 32 行）多印 7 行
#     connect-endpoint        2,40p  （檔頭 34 行）多印 5 行
#     verify-chroma-dims      2,31p  （檔頭 25 行）多印 5 行（程式碼＋檔尾註解）
#     verify-langgraph-tools  2,30p  （檔頭 28 行）多印 1 行（`source lib.sh`）
#     rag-verify              2,24p  （檔頭 23 行）**正確** —— 純粹是巧合
#   停在第一個空行 `sed -n '2,/^$/p'` —— **1 支截斷**：
#     deploy-vps                      （檔頭 110 行）只印 18 行（16%）
#     verify-mem0-add-cost／verify-phase3-runtime／verify-throughput **正確**
#     —— 而且它們是靠「檔頭裡剛好沒有空行」才對的，那不是設計
#
# 「多印程式碼」之所以難發現：說明還是印得出來，只是尾巴多了幾行 `source …`／
# `require_docker`。「截斷」更難發現：少的那幾行不會有人知道自己少了。
# 兩種的共同點是**判準是位置而不是內容** —— 所以判準改成內容（一直印到第一行
# 非註解、非空行為止），檔頭多長、中間有沒有空行都不影響，**結構上**不可能再
# 過期（D-038／D-039 同一條規矩）。這些數字由 test_usage_text.sh 釘住，
# 它同時跑 11 支腳本的 `--help` 逐字比對（含「不含任何一行程式碼」）。
usage_text() {   # <檔案路徑>
  awk 'NR > 1 {
         if ($0 ~ /^#/) { print; next }   # 檔頭註解
         if ($0 ~ /^$/) { print; next }   # 檔頭內的空行（deploy-vps.sh 有一個）
         exit                             # 第一行非註解、非空行＝說明到這裡為止
       }' "$1"
}

# ── Docker 前置檢查 ─────────────────────────────────────
#
# `docker info` 失敗有**三個**原因，而三個的處方完全不同。這條判斷只寫在
# 這裡（不是每個呼叫端各判一次）—— 寫兩次就會有一邊寫錯（#93／D-062 §三）。
#
# 舊版把它們收成一句「無法連線 Docker daemon」，然後給了一個**第四件事**的
# 處方（Codespaces 的 docker-in-docker feature）。**一句訊息裡有兩個不同的
# 處方，等於沒有處方**（#93／D-062 §二），而這裡更糟：它連「裝了沒」都沒有
# 區分 —— 在一台根本沒有 Docker 的機器上，它會說你「連不上 daemon」，
# 於是使用者去查一個不存在的服務。
#
# 回 arm 的名字，不回句子。**刻意不寫行號**：行號會走（D-062 §八）。
# 參數：<有沒有 docker 這個指令> <docker info 成功嗎> <失敗訊息像不像權限問題>
docker_verdict() {
  local bin="${1:-}" info_ok="${2:-}" denied="${3:-}" v
  # 認不得的輸入要**大聲**，不可以落到任何一個正常 arm。空的 `bin` 若落進
  # `absent`，這一支就會對著一台可能裝了 Docker 的機器宣稱「沒有安裝」——
  # 那正是這個家族的病（把讀不到摺進不在）。
  for v in "$bin" "$info_ok" "$denied"; do
    if [[ "$v" != "yes" ]] && [[ "$v" != "no" ]]; then printf 'unknown'; return 0; fi
  done
  # 順序是刻意的：`bin` 比 `info_ok` 更根本（沒有指令就沒得問）。
  if [[ "$bin" != "yes" ]]; then printf 'absent'; return 0; fi
  if [[ "$info_ok" == "yes" ]]; then printf 'ok'; return 0; fi
  if [[ "$denied" == "yes" ]]; then printf 'no_permission'; return 0; fi
  printf 'not_running'
}

# 前置檢查：Docker 要在，而且連得上。
#
# **刻意只檢查、不安裝。** 在一台別人的機器上跑 `curl | sh` 裝 Docker 是這支
# 腳本不該自己決定的事（雲廠商多半提供一鍵 Docker 映像），而 repo 裡已經有
# 同一個形狀的先例：GPU runtime 拿不到時也是**硬擋 ＋ 印出處方**，不是自動裝
# （`deploy-vps.sh` 的 `blocked`，D-056 §四：「一個硬擋如果不附處方，它就只是
# 把問題丟回給人。」）。完整理由見 D-063。
require_docker() {
  local bin="no" info_ok="no" denied="no" msg="" verdict
  if command -v docker >/dev/null 2>&1; then
    bin="yes"
    if msg="$(docker info 2>&1)"; then
      info_ok="yes"
    elif [[ "$msg" == *"permission denied"* ]]; then
      denied="yes"
    fi
  fi

  verdict="$(docker_verdict "$bin" "$info_ok" "$denied")"
  case "$verdict" in
    ok) : ;;
    absent)
      fail "Docker：找不到 docker 這個指令 —— 這台機器沒有安裝 Docker。"
      echo "  這些腳本的前提是 Docker 已經裝好而且執行中；它們刻意不替你安裝。"
      echo
      echo "  裝好它（Debian/Ubuntu，需要 root）："
      echo "    curl -fsSL https://get.docker.com | sh"
      echo "    sudo usermod -aG docker \$USER   # 之後要重新登入"
      echo
      echo "  驗證：docker info 要看得到 Server Version，然後重跑。"
      echo
      echo "  （若你自己有既定的安裝方式 —— 套件庫、雲廠商的映像、rootless ——"
      echo "   就用那個。上面的官方腳本只是其中一條路。）"
      exit 1 ;;
    not_running)
      fail "Docker：docker 指令在，但連不上 daemon —— 「裝了」與「在跑」是兩件事。"
      echo "  這裡卡住的是後者（或 socket 的位置不對）。"
      echo
      echo "  啟動它："
      echo "    sudo systemctl start docker"
      echo
      echo "  Codespaces：確認 docker-in-docker feature 已啟用。"
      echo "  驗證：docker info 要看得到 Server Version，然後重跑。"
      exit 1 ;;
    no_permission)
      fail "Docker：連得上 docker.sock 但權限不足 —— 你這個使用者不在 docker 群組裡。"
      echo "  把自己加進去，然後**重新登入**（群組成員資格在登入時就決定了）："
      echo "    sudo usermod -aG docker \$USER"
      echo
      echo "  驗證：docker info 要看得到 Server Version，然後重跑。"
      echo
      echo "  （只想讓目前這個 shell 生效：newgrp docker。若加了群組仍然被拒，"
      echo "   那是別的原因 —— rootless Docker 的 socket 擁有者、或 SELinux／"
      echo "   AppArmor，這兩種要看發行版的文件，上面的指令解不了。）"
      exit 1 ;;
    *)
      # 認不得的 arm 不可以靜默放行 —— 那正是 #93 的缺陷本體（D-062 §四）。
      # 3 ＝「這支腳本自己壞掉」，不是環境（D-018 的結束碼映射）。
      fail "Docker：前置檢查回了一個認不得的值「$verdict」—— 這是 lib.sh 自己壞掉，不是環境。"
      exit 3 ;;
  esac
}

# ── 載入 .env 並確保必要變數存在 ────────────────────────
load_env() {
  cd "$PROJECT_ROOT"

  if [[ ! -f .env ]]; then
    info "找不到 .env，從 .env.example 建立"
    cp .env.example .env
  fi

  # 產生 WEBUI_SECRET_KEY（冪等：已有值就不覆蓋）
  if ! grep -qE '^WEBUI_SECRET_KEY=.+$' .env; then
    info "產生 WEBUI_SECRET_KEY"
    local key
    if command -v openssl >/dev/null 2>&1; then
      key="$(openssl rand -hex 32)"
    else
      # 退化路徑：以 /dev/urandom 產生 32 bytes 十六進位
      key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    # 移除既有的空行，再寫入
    sed -i.bak '/^WEBUI_SECRET_KEY=/d' .env && rm -f .env.bak
    printf 'WEBUI_SECRET_KEY=%s\n' "$key" >> .env
    ok "WEBUI_SECRET_KEY 已寫入 .env"
  fi

  # 載入為環境變數（供 compose 插值使用）
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}
