#!/usr/bin/env bash
# 實測：ENABLE_SIGNUP=false 時，還能不能建立「第一位」管理員？
#
# 為什麼要問這個：.env.example 現在預設 ENABLE_SIGNUP=false。如果這個值會擋住
# 第一位管理員的註冊，那新使用者第一次啟動就會**被鎖在自己的系統外面** ——
# 沒有帳號可以登入，也就沒有任何方法把它改回來。這是本專案最嚴重的一種失敗：
# 安全設定把人鎖在外面。
#
# 原始碼顯示不會（auths.py 對「還沒有任何使用者」另走一條分支，只檢查
# enable_login_form，而它預設為 True）。但這正是那種「讀起來很合理、
# 錯了卻會讓人完全進不去」的地方，所以要實測。
#
# 用法：bash scripts/verify-first-admin.sh [--keep]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker

IMAGE="ghcr.io/open-webui/open-webui:main"
CONTAINER="owui-first-admin-probe"
VOLUME="owui-first-admin-probe-data"
PORT="${PROBE_PORT:-18082}"
SECRET="probe-secret-not-a-real-key"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

cleanup() {
  if [[ $KEEP -eq 1 ]]; then
    warn "--keep：保留容器 $CONTAINER 與 volume $VOLUME"
    return
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  fail "找不到映像檔 $IMAGE。請先：docker pull $IMAGE"
  exit 1
fi

echo "── 第一位管理員可否建立（ENABLE_SIGNUP=false）──"
echo "映像檔：$IMAGE"
echo

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker volume rm "$VOLUME" >/dev/null 2>&1 || true
docker volume create "$VOLUME" >/dev/null

info "以 ENABLE_SIGNUP=false 啟動全新容器（全新 volume）..."
docker run -d --name "$CONTAINER" \
  -p "127.0.0.1:$PORT:8080" \
  -e "WEBUI_SECRET_KEY=$SECRET" \
  -e "ENABLE_SIGNUP=false" \
  -v "$VOLUME:/app/backend/data" \
  "$IMAGE" >/dev/null

ready=0
for _ in $(seq 1 100); do
  if curl -sf -m 3 "http://127.0.0.1:$PORT/api/config" >/dev/null 2>&1; then
    ready=1
    break
  fi
  if ! container_running "$CONTAINER"; then
    fail "容器啟動失敗。最後 20 行日誌："
    docker logs --tail 20 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2
    exit 1
  fi
  sleep 3
done

if [[ $ready -ne 1 ]]; then
  fail "等待逾時，讀不到 /api/config"
  exit 1
fi
echo

# 觀測點一：對外顯示的開關狀態
observed="$(curl -s -m 10 "http://127.0.0.1:$PORT/api/config" \
  | python3 -c 'import json,sys; print(str(json.load(sys.stdin)["features"]["enable_signup"]).lower())' 2>/dev/null || true)"
info "features.enable_signup = ${observed:-（讀不到）}"

# 觀測點二：真的送一次註冊請求，看端點的反應
status="$(curl -s -o /tmp/first_admin_resp.json -w '%{http_code}' -m 30 \
  -X POST "http://127.0.0.1:$PORT/api/v1/auths/signup" \
  -H 'Content-Type: application/json' \
  -d '{"name":"First Admin","email":"first-admin@example.com","password":"Probe-first-admin-1234"}')"
info "POST /api/v1/auths/signup → HTTP $status"

echo
if [[ "$status" == "200" ]]; then
  role="$(python3 -c 'import json;print(json.load(open("/tmp/first_admin_resp.json")).get("role",""))' 2>/dev/null || true)"
  rm -f /tmp/first_admin_resp.json
  ok "結論：ENABLE_SIGNUP=false 不會擋住第一位管理員，而且他成為 $role。"
  echo "  所以 .env.example 預設 false 是安全的 —— 新使用者不會被鎖在自己系統外面。"
  echo
  echo "  （注意：這只在「資料庫是全新的」時成立。已有使用者之後，"
  echo "    ENABLE_SIGNUP 就開始擋人 —— 那正是它應該做的事。）"
  exit 0
fi

rm -f /tmp/first_admin_resp.json
if [[ "$status" == "403" ]]; then
  fail "結論：ENABLE_SIGNUP=false **會**擋住第一位管理員。"
  cat <<'EOF'

  這是嚴重問題：全新安裝會沒有任何帳號可登入，也無從改回來。
  .env.example 必須改回 ENABLE_SIGNUP=true，並且要另外找方法關閉註冊。
EOF
  exit 1
fi

fail "結論：無法判定（非預期的 HTTP $status）。請保留上方輸出。"
exit 1
