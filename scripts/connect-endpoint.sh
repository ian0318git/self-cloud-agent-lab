#!/usr/bin/env bash
# 把一個 OpenAI-compatible runtime（Kaggle + Endpoint、自架 vLLM…）接上平台。
#
# ── 順序很重要：先驗證，後接線 ───────────────────────────
# 這支腳本**拒絕接上沒有通過相容性探針的 runtime**。
#
# 理由不是潔癖，是順序：先接上再測，失敗的話你已經把平台唯一的模型來源
# 指向一個壞掉的服務了 —— 而 Open WebUI 不會因此大聲抗議，它只會列出
# 一個空的模型清單。先驗證的話，最壞情況是「什麼都沒改變」。
#
# ── 為什麼一定要走資料庫，不能只改 .env ──────────────────
# 環境變數只是第一次開機的種子。config.py 的 Config.seed_defaults 明寫
# "Existing DB values take precedence over defaults"，而
# Config.persistent_enabled_for('openai.enable') 回傳 True —— 也就是
# openai.* **以資料庫為準**。改 .env 再重啟不會有任何效果。
#
# 這件事在 2026-09-19 用誘餌服務實證過：把 openai.api_base_urls 指向一個
# 受控的位址後，應用程式自己的 Config 路徑讀出來的就是那個位址。
# 見 DECISIONS.md D-017。
#
# ── 讀回確認也走應用程式的路徑 ───────────────────────────
# 讀自己剛寫的資料列來證明成功是循環論證。腳本用 runtime_state.py，它 import
# 應用程式自己的模組、呼叫 Config.get_many（get_all_models 用的同一條路徑）。
#
# 用法：
#   bash scripts/connect-endpoint.sh --status
#   bash scripts/connect-endpoint.sh --url https://xxx.trycloudflare.com/v1 \
#        --key "$ENDPOINT_API_KEY"
#   bash scripts/connect-endpoint.sh --disconnect       回到只有 Ollama
#   bash scripts/connect-endpoint.sh --url URL --check  只驗證，不寫入
#
# 金鑰不寫進版控：放在 .env 的 ENDPOINT_API_KEY，或用 --key 傳入。
#
# 結束碼：0 = 成功（或狀態正常）；1 = 未通過／失敗；2 = 無法判定

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

STATE_PY="$PROJECT_ROOT/scripts/runtime_state.py"
PROBE_SH="$PROJECT_ROOT/scripts/probe-openai.sh"

URL=""
KEY="${ENDPOINT_API_KEY:-}"
MODE="connect"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --status)     MODE="status" ;;
    --disconnect) MODE="disconnect" ;;
    --check)      MODE="check" ;;
    --url)        shift; URL="${1:-}" ;;
    --url=*)      URL="${1#--url=}" ;;
    --key)        shift; KEY="${1:-}" ;;
    --key=*)      KEY="${1#--key=}" ;;
    -h|--help)    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知的參數：$1"; exit 2 ;;
  esac
  shift
done

if ! service_running open-webui; then
  fail "open-webui 未在執行中。請先啟動堆疊：bash scripts/up.sh"
  exit 2
fi

run_state() { $COMPOSE exec -T open-webui python3 - "$@" < "$STATE_PY" 2>/dev/null; }

# 讀回確認並下結論。$1 = on|off，期望的狀態。
#
# 用 `runtime_state.py check` 的**結束碼**判斷，不從人類可讀輸出裡撈字串 ——
# 撈字串的檢查會在措辭改變時悄悄失效，而失效的檢查看起來與通過一模一樣。
show_and_conclude() {
  local expect="$1" rc=0

  echo "── 讀回確認（走應用程式自己的 Config 路徑）──"
  echo
  run_state show || return 2
  echo

  run_state check >/dev/null 2>&1 || rc=$?

  if [[ "$expect" == "on" ]]; then
    if [[ $rc -ne 0 ]]; then
      fail "讀回確認失敗：runtime 沒有接上。"
      echo "  設定可能沒寫入，或應用程式沒有採用。請看上方輸出。"
      return 1
    fi
    ok "已確認：OpenAI-compatible runtime 為**啟用中**，上層讀得到它。"
    cat <<'EOF'

界線說明：這裡證明的是「應用程式讀到的設定指向這個 runtime」——
          用的是 get_all_models 的同一條 Config 路徑。
          **沒有**證明「模型清單真的抓到了」。Open WebUI 是延遲抓取，
          要有人在 UI 裡開啟模型選單才會發出請求，而那需要登入。
          請自己開一次 http://localhost:3000 確認模型出現在選單裡。
EOF
    return 0
  fi

  if [[ $rc -eq 0 ]]; then
    fail "讀回確認失敗：runtime 仍是啟用中。"
    return 1
  fi
  ok "已確認：模型來源只有 Ollama。"
  return 0
}

# ── --status ─────────────────────────────────────────────
if [[ "$MODE" == "status" ]]; then
  run_state show
  exit $?
fi

# ── --disconnect ─────────────────────────────────────────
if [[ "$MODE" == "disconnect" ]]; then
  echo "把模型來源切回只有 Ollama。"
  echo
  run_state disable-openai || { fail "寫入失敗"; exit 1; }
  echo
  info "重啟 open-webui 讓設定生效..."
  $COMPOSE restart open-webui >/dev/null
  wait_for_webui || warn "容器未在時限內回報健康，請看：docker compose logs --tail=40 open-webui"
  echo
  show_and_conclude off
  exit $?
fi

# ── connect / check ──────────────────────────────────────
if [[ -z "$URL" ]]; then
  fail "需要 --url。用法："
  echo "  bash scripts/connect-endpoint.sh --url https://xxx.trycloudflare.com/v1"
  echo "  bash scripts/connect-endpoint.sh --status"
  exit 2
fi

echo "目標 runtime：$URL"
if [[ -n "$KEY" ]]; then
  echo "金鑰：已提供（不會被顯示或儲存到版控）"
else
  echo "金鑰：未提供"
  echo "  若這個 runtime 需要認證，請用 --key 傳入，或寫進 .env 的 ENDPOINT_API_KEY。"
fi
echo

# ── 步驟 1：相容性探針（先驗證，後接線）───────────────────
info "步驟 1／3：相容性探針"
echo
PROBE_ARGS=("$URL")
[[ -n "$KEY" ]] && PROBE_ARGS+=(--api-key "$KEY")

set +e
bash "$PROBE_SH" "${PROBE_ARGS[@]}"
PROBE_STATUS=$?
set -e

echo
if [[ $PROBE_STATUS -eq 2 ]]; then
  fail "無法判定 —— 連不上這個 runtime。"
  echo "  沒有做任何變更。這不是「它壞掉」，是「這次測不出來」："
  echo "  先確認網址、tunnel 是否還開著、服務是否還在跑，再重試。"
  exit 2
elif [[ $PROBE_STATUS -ne 0 ]]; then
  fail "探針未通過 —— **不做任何變更**。"
  echo "  先修好未通過的項目。上層會踩到的洞就是那些，接上去只是把洞搬進來。"
  exit 1
fi
ok "相容性探針通過"

if [[ "$MODE" == "check" ]]; then
  echo
  info "（--check：只驗證，未做任何變更）"
  exit 0
fi

# ── 步驟 2：寫入設定 ─────────────────────────────────────
echo
info "步驟 2／3：寫入設定（以資料庫為準，不是 .env）"
run_state set-openai --url "$URL" --key "$KEY"
SET_STATUS=$?
if [[ $SET_STATUS -ne 0 ]]; then
  fail "寫入失敗，未變更任何設定。"
  exit 1
fi

# ── 步驟 3：重啟並讀回確認 ───────────────────────────────
echo
info "步驟 3／3：重啟 open-webui 並讀回確認"
$COMPOSE restart open-webui >/dev/null
wait_for_webui || warn "容器未在時限內回報健康，請看：docker compose logs --tail=40 open-webui"
echo
show_and_conclude on
exit $?