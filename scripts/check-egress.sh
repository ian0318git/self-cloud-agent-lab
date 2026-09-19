#!/usr/bin/env bash
# 檢查「資料可能流向哪些外部服務」，必要時把 OpenAI 官方 API 那個洞關掉。
#
# ── 為什麼需要這支腳本（2026-09-19 實測）──────────────────
# 本專案的目標是「資料由公司自己控制」，但實測 Open WebUI 的資料庫裡是：
#
#     openai.enable        = true
#     openai.api_base_urls = ["https://api.openai.com/v1"]
#
# 也就是這個堆疊**預設就接上了 OpenAI 官方 API，而且是開著的**。
# 成因是兩個預設值相加：
#   · ENABLE_OPENAI_API    預設 'True'
#   · OPENAI_API_BASE_URLS 預設 ''，而空字串是 fallback 不是關閉，
#                          會被填成 ['https://api.openai.com/v1']
#
# 這與 ENABLE_MCP（不存在的變數）、ENABLE_SIGNUP（只第一次開機有效）是同一類：
# **看起來是關的，其實不是。**
#
# ── 為什麼不能只改 .env ─────────────────────────────────
# 因為環境變數只是「第一次開機時的種子」。config.py 的 Config.seed_defaults
# 寫得很明白："Existing DB values take precedence over defaults."
# 資料庫已經有值，改 .env 再重啟不會有任何效果 —— 這正是 lock-signup.sh
# 的舊版踩過的坑（讀 .env 判斷，於是回報了一個假成功）。
# 所以這支腳本**以資料庫的真實內容為準**。
#
# 用法：
#   bash scripts/check-egress.sh            掃描並列出（唯讀）
#   bash scripts/check-egress.sh --check    同上，適合放進部署流程
#   bash scripts/check-egress.sh --json     輸出 JSON，給其他工具吃
#   bash scripts/check-egress.sh --fix      關掉 openai.enable，重啟並回讀確認
#
# 結束碼：0 = 沒有啟用中的外部端點；1 = 有；2 = 無法判定

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose

MODE="scan"
PROBE="$PROJECT_ROOT/scripts/egress_probe.py"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE="scan" ;;
    --scan)  MODE="scan" ;;
    --json)  MODE="json" ;;
    --fix)   MODE="fix" ;;
    -h|--help)
      sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) fail "未知的參數：$1"; exit 2 ;;
  esac
  shift
done

if [[ ! -f "$PROBE" ]]; then
  fail "找不到 $PROBE"
  exit 2
fi

if ! $COMPOSE ps --status running --services 2>/dev/null | grep -qx open-webui; then
  fail "open-webui 未在執行中 —— 探針要讀它的資料庫。"
  echo "  請先啟動堆疊：bash scripts/up.sh"
  exit 2
fi

# 探針用 stdin 送進容器執行，不在容器裡留下暫存檔。
# 讀的是容器內真實的資料庫路徑。
run_probe() {
  $COMPOSE exec -T open-webui python3 - "$@" < "$PROBE"
}

if [[ "$MODE" != "fix" ]]; then
  set +e
  if [[ "$MODE" == "json" ]]; then
    run_probe scan --json
  else
    run_probe scan
  fi
  STATUS=$?
  set -e
  exit $STATUS
fi

# ── --fix ────────────────────────────────────────────────────
echo "修復模式：把 openai.enable 關掉。"
echo

# 先做一次唯讀掃描，確認真的需要修 —— 若本來就沒有啟用中的端點，
# 就不該重啟容器去打擾一個正常運作的服務。
set +e
run_probe scan >/dev/null 2>&1
BEFORE=$?
set -e

if [[ $BEFORE -eq 2 ]]; then
  fail "無法判定目前狀態，未做任何變更。"
  exit 2
fi
if [[ $BEFORE -eq 0 ]]; then
  ok "沒有啟用中的外部端點，不需要修復。"
  exit 0
fi

info "寫入資料庫：openai.enable = false"

set +e
$COMPOSE exec -T open-webui python3 - <<'PY'
import json, sqlite3, sys, time

DB = "/app/backend/data/webui.db"
try:
    db = sqlite3.connect(DB)
    db.execute("BEGIN")
    row = db.execute(
        "SELECT value FROM config WHERE key = 'openai.enable'").fetchone()
    before = row[0] if row else "(不存在)"
    # 值是以 JSON 存的 TEXT；json.dumps(False) == 'false'。
    db.execute(
        "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        ("openai.enable", json.dumps(False), int(time.time())))
    db.commit()
    after = db.execute(
        "SELECT value FROM config WHERE key = 'openai.enable'").fetchone()[0]
    print(f"  openai.enable: {before} → {after}")
except sqlite3.Error as exc:
    print(f"  寫入失敗：{exc}", file=sys.stderr)
    sys.exit(1)
PY
DB_STATUS=$?
set -e

if [[ $DB_STATUS -ne 0 ]]; then
  fail "資料庫寫入失敗，未做任何變更。"
  exit 1
fi

# Open WebUI 在記憶體裡快取設定，寫資料庫不會即時生效 —— 必須重啟。
# 這一步不能省，否則「改了但其實沒生效」正是本專案一直在防的假成功。
echo
info "重啟 open-webui 讓設定生效（記憶體中的快取不會自動更新）..."
$COMPOSE restart open-webui >/dev/null

info "等待服務就緒..."
READY=0
for _ in $(seq 1 60); do
  if $COMPOSE exec -T open-webui python3 -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/health', timeout=3).status==200 else 1)" \
      >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 3
done

echo
if [[ $READY -eq 0 ]]; then
  warn "容器在 180 秒內未回報健康。設定已寫入，但服務狀態未知。"
  echo "  查看日誌：docker compose logs --tail=40 open-webui"
fi

set +e
run_probe scan >/dev/null 2>&1
AFTER=$?
set -e

echo
if [[ $AFTER -eq 0 ]]; then
  ok "已確認：沒有啟用中的外部端點。"
  echo
  echo "界線說明：這裡驗證的是**資料庫裡的值**。Open WebUI 沒有未認證的"
  echo "          端點會揭露 openai.enable，所以「服務實際載入的值」無法"
  echo "          在沒有管理員帳號的情況下讀到。容器健康 + 資料庫值正確"
  echo "          是本腳本做得到的全部。"
  exit 0
else
  fail "重啟後仍有啟用中的外部端點（可能是 openai.enable 以外的項目）。"
  echo "  請看上方清單逐項處理：bash scripts/check-egress.sh"
  exit 1
fi
