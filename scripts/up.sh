#!/usr/bin/env bash
# 啟動第一階段堆疊（Ollama + Open WebUI），並確保模型已下載。
# 此腳本是冪等的：重複執行不會重啟已在運作的容器，也不會重新下載模型。
# 可由 .devcontainer 的 postCreateCommand / postStartCommand 自動呼叫。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# 閘門的判定表（exposure_gate_verdict）在這個模組裡 —— 它可被離線測試，
# 而「把結束碼 2 讀成安全」正是這整件事最不能出的錯。
source "$(dirname "${BASH_SOURCE[0]}")/deploy-vps-decisions.sh"

require_docker
detect_compose
load_env

MODEL="${OLLAMA_MODEL:-qwen3:4b}"

# ── Cloudflare Tunnel（選用）────────────────────────────
# 缺 token 就在啟動前擋下來，不要讓 cloudflared 帶著空 token 起來後才在
# 日誌裡留下看不懂的錯誤。compose 檔無法做這個檢查，原因見該檔的註解。
if [[ ",${COMPOSE_PROFILES:-}," == *",tunnel,"* ]]; then
  if [[ -z "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]]; then
    fail "COMPOSE_PROFILES 含 tunnel，但 CLOUDFLARE_TUNNEL_TOKEN 是空的。"
    fail "請在 .env 填入 Cloudflare Zero Trust 的 tunnel token，"
    fail "或將 COMPOSE_PROFILES 那行註解掉以停用 tunnel。"
    exit 1
  fi
  ok "Cloudflare Tunnel 已啟用"
fi

info "啟動容器（等待 healthcheck 通過，首次可能需要 1-2 分鐘）..."
# --wait 會等到所有帶 healthcheck 的服務轉為 healthy 才返回；
# 任一服務失敗則以非零結束，不會靜默通過。
#
# --remove-orphans 在這裡是安全措施，不只是清理：
# 若使用者停用 tunnel profile 後執行本腳本，舊的 cloudflared 容器並不會
# 因為「不在作用中的 profile 裡」而自動停止 —— 它會繼續把服務對外。
# 少了這個旗標，「我已經把 tunnel 關掉了」就會是個錯誤的認知。
if ! $COMPOSE up -d --wait --remove-orphans; then
  fail "容器啟動失敗。以下為最近日誌："
  $COMPOSE logs --tail=40 >&2
  exit 1
fi
ok "容器已就緒"

# ── 對外暴露閘門 ───────────────────────────────────────
# **位置是刻意的：容器剛就緒、模型下載之前。** 暴露在 pull 的那幾分鐘裡就
# 已經存在了；閘門放在 pull 之後等於讓它在最長的視窗裡開著。
#
# 為什麼要有這道閘門：同一份 compose 檔在 Codespaces 上是安全的、在 VPS 上
# 是危險的，差別不在檔案而在**機器在哪**。而 `up.sh` 正是那個兩邊都會被執行
# 到的入口 —— 一個在 Codespaces 上反覆驗證過「沒問題」的設定，會在搬家的
# 那一刻變成問題（D-014）。
#
# 這裡**刻意不自動 down**：本腳本是使用者的日常入口，也是 .devcontainer 的
# postStartCommand。在那裡把容器收掉會弄壞一個本來可用的 workflow（D-016）。
# 所以它只把話講清楚，把處置留給人 —— 但**不會安靜地放過**。
#
# 輸出只在「有話要說」時才露面。check-exposure.sh 在正常設定下（區網、或
# Codespaces）是**帶警告回 0**，無條件印出來等於每次 up.sh 都印一段警告 ——
# 而一個在正常設定下也照樣響的檢查會被忽略，被忽略的檢查等於沒有檢查
# （這個論證寫在 check-exposure.sh 自己的檔頭）。
EXPOSURE_RC=0
EXPOSURE_OUT="$(bash "$(dirname "${BASH_SOURCE[0]}")/check-exposure.sh" 2>&1)" || EXPOSURE_RC=$?

# 結束碼的判讀在 exposure_gate_verdict() 裡（那裡可測）。lenient 是給這支
# 腳本用的政策：把「無法判定」升級成失敗會弄壞日常 workflow，見上面。
case "$(exposure_gate_verdict "$EXPOSURE_RC" lenient)" in
  pass)
    : ;;  # 正常設定：安靜通過
  block)
    fail "發現對外暴露 —— 而且堆疊**已經在跑了**，現在的設定就是對外開放的。"
    echo
    printf '%s\n' "$EXPOSURE_OUT"
    echo
    fail "容器沒有被收掉（這是刻意的，避免弄壞你正在用的環境）。"
    fail "請依上方指示修正 .env，然後再執行一次：bash scripts/up.sh"
    exit 1 ;;
  warn)
    warn "無法判定是否對外暴露（check-exposure.sh 回 2）—— 這不是「安全」，是「不知道」。"
    printf '%s\n' "$EXPOSURE_OUT"
    echo ;;
  *)
    warn "對外暴露檢查本身失敗了（結束碼 $EXPOSURE_RC）—— 閘門沒有生效，請自行確認。"
    printf '%s\n' "$EXPOSURE_OUT"
    echo ;;
esac

# ── 模型下載（冪等）────────────────────────────────────
# **兩邊都正規化**，與 pull-model.sh:29-36 同一個寫法。原本這裡是
# `grep -qx "$MODEL"` 的純字串比對，而 ollama 把 `bge-m3` 與
# `bge-m3:latest` 當成同一個模型、`ollama list` 一律顯示帶 tag 的形式 ——
# 於是「使用者打的名字」與「清單裡的名字」字面上不相等，一個已下載的模型
# 會被誤報成缺漏，然後叫人去執行一個只會回他「已存在，略過下載」的指令。
# 那正是 normalize_model() 自己的註解在講的假失敗（lib.sh:22-25）。
MODEL_WANT="$(normalize_model "$MODEL")"
MODEL_INSTALLED=false
while IFS= read -r line; do
  if [[ -n "$line" && "$(normalize_model "$line")" == "$MODEL_WANT" ]]; then
    MODEL_INSTALLED=true
    break
  fi
done <<<"$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"

if [[ "$MODEL_INSTALLED" == "true" ]]; then
  ok "模型 $MODEL 已存在，略過下載"
else
  info "下載模型 $MODEL（首次約需數分鐘，請勿中斷）..."
  if ! $COMPOSE exec -T ollama ollama pull "$MODEL"; then
    fail "模型 $MODEL 下載失敗。"
    fail "堆疊本身已啟動，可稍後重試：bash scripts/pull-model.sh"
    exit 1
  fi
  ok "模型 $MODEL 下載完成"
fi

# ── 結果 ───────────────────────────────────────────────
echo
"$(dirname "${BASH_SOURCE[0]}")/status.sh"

cat <<EOF

Open WebUI 位址：
  • 在 Codespaces 中：開啟「PORTS」面板 → 點擊 3000 埠的網址
  • 本機：http://localhost:3000

首次使用：
  1. 註冊第一個帳號 —— 它會自動成為管理員
  2. 登入後立刻鎖住註冊：bash scripts/lock-signup.sh

⚠  在 ENABLE_SIGNUP=true 的期間，任何能觸及 Open WebUI 的人都能自行
   註冊帳號並使用你的模型。對外之前務必先做上面的步驟 2。

⚠  額度提醒：2-core codespace 每月僅有 60 真實小時。
   用完請務必停止：bash scripts/down.sh
EOF
