#!/usr/bin/env bash
# 機械驗證 mcp-test-server：從 open-webui 容器的視角打 MCP handshake。
#
# 為什麼要從 open-webui 容器打：驗證的是「Open WebUI 將要走的那條路」——
# 同一個 ai-net、同一個 URL。從主機打什麼都證明不了：主機根本不在那條
# 路上（compose 刻意不發布 mcp-test-server 的埠，D-003）。
#
# 驗證四步：initialize → notifications/initialized → tools/list →
# tools/call（echo 與 roll_die 各一次）。四步全過，在
# Admin → External Tools 新增 URL 後就該看得到這兩個工具。
#
# 三個實測踩過、寫進這支腳本的坑（mcp 1.30.0）：
#   1. server 的回應是 SSE 格式（Accept 帶 text/event-stream 才會被接受，
#      只收 application/json 會拿到 406）—— parse() 兩種都吃。
#   2. notifications/initialized 按規範不回 body，只回 202 ——
#      對它解析 body 會拿到空字串而爆炸。
#   3. initialize 回傳 Mcp-Session-Id，後續請求必須帶著 ——
#      不帶會被當成另一個（或無效的）session。
#
# 用法：bash scripts/verify-mcp-server.sh
#
# 結束碼：0 = 四步全過
#         1 = 未通過（server 有回應但內容不符合預期，見訊息）
#         2 = 無法判定（容器未執行、或連不上 —— 見訊息）
#         3 = 探針自己壞掉（D-018 的約定：這不是 server 的問題）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose

URL="${MCP_URL:-http://mcp-test-server:8000/mcp}"

info "目標：$URL（從 open-webui 容器的視角）"

for NAME in open-webui mcp-test-server; do
  if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    fail "$NAME 容器未在執行中 —— 先執行 bash scripts/up.sh 與 docker compose up -d mcp-test-server"
    exit 2
  fi
done

# URL 用參數傳進容器內的 python（不要拼進程式碼），輸出與結束碼原樣保留。
# `|| rc=$?` 是為了在 set -e 之下留住結束碼再分類（見腳本尾的 case）。
rc=0
$COMPOSE exec -T open-webui python3 - "$URL" <<'PY' || rc=$?
import json
import sys
import urllib.error
import urllib.request

url = sys.argv[1]


def parse(body):
    """SSE 或純 JSON 都吃：抽 data: 行再 json.loads。"""
    text = body.decode()
    lines = [l for l in text.splitlines() if l.startswith("data:")]
    if lines:
        return json.loads("\n".join(l[5:].lstrip() for l in lines))
    return json.loads(text)


def post(payload, session=None, parse_body=True):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
        return r.status, r.headers.get("Mcp-Session-Id"), (parse(body) if parse_body else None)


def check(cond, label):
    print(("通過 " if cond else "未通過 ") + label)
    if not cond:
        sys.exit(1)


try:
    status, session, init = post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "verify-mcp-server", "version": "0.1"},
        },
    })
    check(status == 200 and "result" in init, "initialize（serverInfo：%s）"
          % init.get("result", {}).get("serverInfo", {}))

    # 通知不回 body，只驗 HTTP 狀態
    status_n, _, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                          session, parse_body=False)
    check(status_n in (200, 202), "notifications/initialized")

    status2, _, resp2 = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    names = sorted(t["name"] for t in resp2.get("result", {}).get("tools", []))
    check(status2 == 200 and names == ["echo", "roll_die"], "tools/list（echo、roll_die）")

    status3, _, resp3 = post({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hello-mcp"}},
    }, session)
    echo_text = "".join(c.get("text", "") for c in resp3.get("result", {}).get("content", []))
    check(status3 == 200 and echo_text == "hello-mcp", "tools/call echo")

    status4, _, resp4 = post({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "roll_die", "arguments": {"sides": 6}},
    }, session)
    roll = "".join(c.get("text", "") for c in resp4.get("result", {}).get("content", []))
    check(status4 == 200 and roll.isdigit() and 1 <= int(roll) <= 6, "tools/call roll_die")
except urllib.error.URLError as e:
    print("無法判定：連不上 %s（%s）" % (url, e.reason))
    sys.exit(2)
except urllib.error.HTTPError as e:
    print("未通過：HTTP %s" % e.code)
    sys.exit(1)
except Exception as e:  # noqa: BLE001 —— 探針的任何意外都是「自己壞掉」，不是 server 的錯
    print("探針自己壞掉：%s %s" % (type(e).__name__, e))
    sys.exit(3)
PY

case $rc in
  0) ok "MCP handshake 四步全過 —— Admin → External Tools 可以填 $URL 了" ;;
  1) fail "MCP handshake 未通過（見上方輸出）"; exit 1 ;;
  2) fail "無法判定（見上方輸出）"; exit 2 ;;
  3) fail "探針自己壞掉（見上方輸出）—— 這不是 server 的問題，是這支腳本該修"; exit 3 ;;
  *) fail "意外的結束碼 $rc"; exit 3 ;;
esac
