#!/usr/bin/env bash
# 顯示堆疊狀態、已安裝模型，以及記憶體用量。
# 在 8GB 的 codespace 上，記憶體是主要瓶頸，因此一併顯示。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# tunnel 的四態判定（tunnel_state_verdict）在這個模組裡。
source "$(dirname "${BASH_SOURCE[0]}")/profile-lifecycle.sh"

detect_compose

# 讓 docker compose 的指令把 tunnel profile 算進來。
#
# **原本這裡寫的理由不成立**：「否則即使 cloudflared 正在執行，它也不會出現
# 在 ps 的輸出裡，看起來像沒在跑」—— 實測（2026-09-21，compose v5.5.1）
# 顯示對**正在執行**的被停用 profile 容器，`compose ps` 一律會列出，
# export 與否結果相同。
#
# 還是留著，因為它是零成本的版本保險：這行為在 compose 各版本間並不保證
# 一致，而本專案會跑在 Codespaces、本機、以及將來的 VPS 上。**這一段的結論
# 已經不依賴它** —— tunnel 是開是關由容器決定（見下面的四態判定）。
#
# 這裡直接讀 .env 而不呼叫 load_env，避免 status 產生 golden key 等副作用。
COMPOSE_PROFILES="$(grep -E '^COMPOSE_PROFILES=' "$PROJECT_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
export COMPOSE_PROFILES

echo "── 容器狀態 ──────────────────────────────────"
$COMPOSE ps 2>/dev/null || warn "無法取得容器狀態（堆疊可能尚未啟動）"

echo
echo "── 已安裝模型 ────────────────────────────────"
if service_running ollama; then
  $COMPOSE exec -T ollama ollama list 2>/dev/null || warn "無法列出模型"
else
  warn "ollama 未在執行中"
fi

echo
echo "── 記憶體 ────────────────────────────────────"
free -h | awk 'NR==1 || /^Mem:/'

echo
echo "── 對外連線（Cloudflare Tunnel）──────────────"
# **判斷依據是容器，不是 .env 的那一行。**
# 原本這裡的閘門是「`COMPOSE_PROFILES` 裡有沒有 tunnel」，於是「profile 被
# 停用、但 cloudflared 還跑著」會走到 else 那一支，被印成「未啟用」—— 而
# 那個容器還在跟 Cloudflare 邊緣保持連線、你的主機名還在服務 Open WebUI。
# 上面「容器狀態」那一段會把它列出來，跟這一段的結論互相矛盾。
#
# 使用者問的是「tunnel 現在是開的還是關的」，那個答案在容器上，不在設定檔裡。
TUNNEL_PROFILE=no
if [[ ",${COMPOSE_PROFILES:-}," == *",tunnel,"* ]]; then
  TUNNEL_PROFILE=yes
fi
TUNNEL_RUNNING=no
if service_running cloudflared; then
  TUNNEL_RUNNING=yes
fi

# 這一支只印訊息、不改結束碼：status.sh 是**報告**不是閘門，而它被 up.sh 與
# deploy-vps.sh 在結尾直接呼叫 —— 讓它回非 0 會在那兩條路徑上變成假失敗
# （D-016）。擋下殘留容器是 up.sh 的工作，這裡的工作是**把它說出來**。
case "$(tunnel_state_verdict "$TUNNEL_PROFILE" "$TUNNEL_RUNNING")" in
  active)
    ok "cloudflared 執行中 —— Open WebUI 可透過你的網域存取"
    # 連線是否真的建立要看日誌裡有沒有註冊成功的訊息，容器「跑著」不等於「通了」
    # 逐行比對（＝ 原本 grep -iE 的語意）：整份日誌當成一個字串比對會被
    # `*` 跨越換行，於是「上一行有 connection、下一行有 registered」也算命中
    # —— 那正是儀器說謊的方向。
    TUNNEL_LOGS="$($COMPOSE logs --tail=50 cloudflared 2>/dev/null || true)"
    TUNNEL_REGISTERED=no
    while IFS= read -r _line; do
      _lo="${_line,,}"   # 原本是 grep -i：用 ,, 保留「不分大小寫」的語意
      if [[ "$_lo" == *"registered tunnel connection"* || "$_lo" == *"connection "*" registered"* ]]; then
        TUNNEL_REGISTERED=yes
        break
      fi
    done <<< "$TUNNEL_LOGS"
    if [[ "$TUNNEL_REGISTERED" == "yes" ]]; then
      echo "   日誌顯示已向 Cloudflare 註冊連線"
    else
      warn "日誌未看到註冊成功的訊息，請確認：docker compose logs --tail=20 cloudflared"
    fi ;;

  enabled-but-down)
    warn "tunnel profile 已啟用，但 cloudflared 不在執行中" ;;

  stale)
    fail "cloudflared **還在執行**，但 .env 已經沒有啟用 tunnel profile。"
    echo "   這不是「已關閉」—— 它仍然連著 Cloudflare 邊緣，主機名還在服務。"
    echo "   殘留的原因：compose 的 --remove-orphans 不會清掉被停用 profile 的"
    echo "   容器，因為那些服務在 compose 檔裡「仍然有定義」（D-029）。"
    echo "   要真的停掉："
    echo "       bash scripts/down.sh"
    echo "   或者下次 bash scripts/up.sh 會自動清掉它。" ;;

  off)
    echo "未啟用（僅在本機／Codespaces 埠轉送內可用）"
    # 這裡原本只有一句「見 .env.example 的 token 段落」—— 一個**指標**，
    # 而不是那幾行。同一個缺口在 deploy-vps.sh 的完成訊息裡也有（#84／D-064）；
    # 補指示時兩處要一起補，否則其中一處會繼續指著別的地方。
    #
    # 第 1 步刻意排在 .env 之前：Access 政策要在 tunnel 起來**之前**就存在。
    # 第一個帳號的註冊窗口是開著的，而 tunnel 一通，那條路就是對整個
    # Internet 開的 —— 這支腳本看不到 Cloudflare 那邊，沒辦法替你確認。
    echo "   啟用方式："
    echo "     1. 確認 Cloudflare Access 政策已存在（tunnel 一通，這條路就是對"
    echo "        整個 Internet 開的；up.sh 只看得到 token，看不到 Access）"
    echo "     2. .env：填 CLOUDFLARE_TUNNEL_TOKEN，並取消 COMPOSE_PROFILES=tunnel"
    echo "        那一行的註解"
    echo "     3. bash scripts/up.sh"
    echo "   完整步驟（含在 Zero Trust 建 tunnel 與 Public hostname）見"
    echo "   .env.example 的 CLOUDFLARE_TUNNEL_TOKEN 段落" ;;
esac

echo
echo "── 容器資源用量 ──────────────────────────────"
docker stats --no-stream --format \
  'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null \
  || warn "無法取得容器資源用量"
