#!/usr/bin/env bash
# 讀取／設定 RAG 嵌入引擎與模型（DECISIONS.md D-013）。
#
# 用法：
#   bash scripts/set-embedding.sh                              顯示目前設定（唯讀）
#   bash scripts/set-embedding.sh ollama qwen3-embedding:0.6b  套用並重建 open-webui
#
# ── 為什麼不直接用環境變數或 Admin UI 之外的捷徑 ──────────────
#
# 1. **環境變數無效。** Open WebUI 在首次開機時把設定播種進 config 表，
#    之後「資料庫既有值一律優先」—— models/config.py 的 seed_defaults()
#    docstring 自己寫著 "Existing DB values take precedence over defaults"，
#    而它只補「資料庫還沒有的鍵」。rag.embedding_engine 那列已經存在，
#    所以改 .env 再重建容器不會有任何作用（D-013）。
#
# 2. **本腳本改的就是 Admin UI 改的那張表。** Admin UI 走 HTTP API
#    （POST /api/v1/retrieval/embedding/update），該端點需要管理員身分；
#    這裡改的是它最終寫入的同一個 config 表、同一個鍵。差別只在少了
#    HTTP 層，因此**改完必須重啟**（見下一點）。
#
# 3. **為什麼一定要重啟。** 那個 API 端點除了寫資料庫，還會重建行程內的
#    request.app.state.ef 與 EMBEDDING_FUNCTION。main.py 只在啟動時
#    （lifespan）讀一次 rag.embedding_engine 來建這兩個物件，
#    因此從外部改資料庫無法觸及執行中的行程 —— 重啟是唯一讓它重建的方法。
#
# 為什麼不建一個管理員帳號來呼叫 API：scripts/verify-first-admin.sh 用的是
# 寫死在 repo 裡的密碼。為了改兩個設定值而在這套堆疊上開一個永久 admin，
# 與本專案的安全取向（D-003 不發布 Ollama 埠、D-012 鎖註冊）相衝。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose
load_env

# 只列這幾個鍵：其餘 rag.* 可能含 API key，不該出現在輸出裡。
READ_KEYS=(
  'rag.embedding_engine'
  'rag.embedding_model'
  'rag.embedding_batch_size'
  'rag.embedding_concurrent_requests'
  'rag.enable_async_embedding'
  'rag.ollama.base_url'
)

if ! $COMPOSE ps --status running --services 2>/dev/null | grep -qx open-webui; then
  fail "open-webui 未在執行中。請先執行：bash scripts/up.sh"
  exit 1
fi

# 在容器內讀設定。用參數傳入鍵名，避免在 shell 裡拼 Python 字串。
read_config() {
  $COMPOSE exec -T open-webui python3 - "${READ_KEYS[@]}" <<'PY'
import json, sqlite3, sys
keys = sys.argv[1:]
con = sqlite3.connect('/app/backend/data/webui.db')
found = dict(con.execute(
    f"select key, value from config where key in ({','.join('?' * len(keys))})", keys
))
for k in keys:
    raw = found.get(k)
    if raw is None:
        print(f'{k}\t（不存在）')
        continue
    # value 欄位宣告為 JSON，但 SQLite 是動態型別：整數會被原樣存成
    # INTEGER，讀回來就是 int，直接 json.loads(int) 會 TypeError。
    # 只有字串／bytes 才需要（也才能）解析。
    val = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    print(f'{k}\t{val}')
PY
}

# 既有的知識庫／文件數。換模型會讓既有向量失效，必須先擋下來。
count_documents() {
  $COMPOSE exec -T open-webui python3 - <<'PY'
import sqlite3
con = sqlite3.connect('/app/backend/data/webui.db')
tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
total = 0
for t in ('knowledge', 'document', 'file'):
    if t in tables:
        total += con.execute(f'select count(*) from "{t}"').fetchone()[0]
print(total)
PY
}

show_config() {
  echo "── 目前設定（讀自 config 表）──────────────"
  while IFS=$'\t' read -r k v; do
    printf '  %-38s %s\n' "$k" "$v"
  done < <(read_config)
  echo
}

show_config

# ── 唯讀模式 ────────────────────────────────────────────
if [[ $# -eq 0 ]]; then
  info "未指定引擎與模型 —— 僅顯示目前設定，未做任何變更。"
  echo "  要套用請執行："
  echo "    bash scripts/set-embedding.sh ollama qwen3-embedding:0.6b"
  exit 0
fi

if [[ $# -ne 2 ]]; then
  fail "參數數量不對：需要「引擎 模型」兩個。"
  echo "  例：bash scripts/set-embedding.sh ollama qwen3-embedding:0.6b"
  exit 1
fi

ENGINE="$1"
MODEL="$2"

case "$ENGINE" in
  ollama|openai|azure_openai) ;;
  '')
    fail "引擎不可為空字串 —— 空字串代表 SentenceTransformers，"
    fail "而 D-013 已決定不使用它（模型不存在時會安靜地降級 RAG）。"
    fail "若確定要用，請從 Admin UI 設定，不要用這支腳本。"
    exit 1
    ;;
  *)
    fail "不認得的引擎：$ENGINE"
    echo "  可用：ollama、openai、azure_openai（空字串 = SentenceTransformers，不建議）"
    exit 1
    ;;
esac

# ── 前置檢查：模型必須已經在 ollama 裡 ──────────────────
# 這正是 D-013 選 ollama 引擎的理由：模型是可列舉的，可以在跑之前檢查。
# SentenceTransformers 那條路是「首次使用時才在容器內下載」，而且會安靜地
# 降級 RAG —— 這裡就是要把那種失敗擋在門外。
if [[ "$ENGINE" == "ollama" ]]; then
  installed_raw="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  want="$(normalize_model "$MODEL")"
  found=0
  while IFS= read -r line; do
    if [[ -n "$line" && "$(normalize_model "$line")" == "$want" ]]; then
      found=1
      break
    fi
  done <<<"$installed_raw"

  if [[ $found -eq 0 ]]; then
    fail "ollama 裡沒有模型 $MODEL。"
    echo "  請先下載（否則 Open WebUI 會在第一次檢索時才失敗）："
    echo "    bash scripts/pull-model.sh $MODEL"
    exit 1
  fi
  ok "模型 $MODEL 已在 ollama 中"
fi

# ── 前置檢查：不能有既有文件 ────────────────────────────
docs="$(count_documents)"
if [[ "$docs" != "0" ]]; then
  fail "資料庫裡已經有 $docs 筆文件／知識庫。"
  fail "換嵌入模型會讓既有向量全部失效，必須重新嵌入 —— 這是不可逆的一步（D-008）。"
  echo "  若確定要換，請先自行備份並清空文件，再執行本腳本。"
  exit 1
fi
ok "目前沒有任何文件 —— 換模型是安全的（D-008 要求在上傳前決定）"

# ── 寫入 ────────────────────────────────────────────────
info "寫入設定：引擎 $ENGINE、模型 $MODEL"
$COMPOSE exec -T open-webui python3 - "$ENGINE" "$MODEL" <<'PY'
import json, sqlite3, sys, time
engine, model = sys.argv[1], sys.argv[2]
con = sqlite3.connect('/app/backend/data/webui.db')
now = int(time.time())
for key, value in (('rag.embedding_engine', engine), ('rag.embedding_model', model)):
    # value 欄位是 JSON：字串要存成 "ollama"（含引號），不是 ollama。
    # 存錯的話讀取端 json.loads 會失敗或拿到非預期的型別。
    con.execute(
        'insert into config (key, value, updated_at) values (?, ?, ?) '
        'on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at',
        (key, json.dumps(value), now),
    )
con.commit()
print('  已寫入', con.total_changes, '列')
PY

# ── 重啟（讓 app 重建 EMBEDDING_FUNCTION）───────────────
info "重啟 open-webui（main.py 只在啟動時讀一次嵌入設定）..."
$COMPOSE restart open-webui >/dev/null

# 首次開機的預算比照 docker-compose.yml 的 healthcheck：start_period 180s
# 加 5×15s。這裡給到 300s，並在過程中回報，不要讓它看起來像卡住。
DEADLINE=$((SECONDS + 300))
while (( SECONDS < DEADLINE )); do
  state="$($COMPOSE ps --format '{{.Health}}' open-webui 2>/dev/null || true)"
  if [[ "$state" == "healthy" ]]; then
    ok "open-webui 已就緒（$((300 - (DEADLINE - SECONDS))) 秒）"
    break
  fi
  sleep 5
done

if [[ "${state:-}" != "healthy" ]]; then
  fail "open-webui 在 300 秒內未變成 healthy（目前：${state:-未知}）。"
  echo "  請看日誌：docker compose logs --tail=40 open-webui"
  exit 1
fi

echo
show_config

# ── 驗證 ────────────────────────────────────────────────
# 讀回來的值必須與寫入的一致。DB 寫入成功不代表 app 讀到的是新值。
failures=0
while IFS=$'\t' read -r k v; do
  case "$k" in
    rag.embedding_engine) [[ "$v" == "$ENGINE" ]] || { fail "讀回 $k = $v，預期 $ENGINE"; failures=$((failures+1)); } ;;
    rag.embedding_model)  [[ "$v" == "$MODEL"  ]] || { fail "讀回 $k = $v，預期 $MODEL";  failures=$((failures+1)); } ;;
  esac
done < <(read_config)

# 端到端：從 open-webui 容器真的對 ollama 要一個向量。
# 這證明的不只是「設定寫進去了」，而是「這條路真的能動」——
# 兩者失敗的原因完全不同，而 D-013 選 ollama 就是為了讓前者能擋住後者。
if [[ "$ENGINE" == "ollama" ]]; then
  info "從 open-webui 容器對 ollama 實際取得一個向量..."
  if $COMPOSE exec -T -e "MODEL=$MODEL" open-webui python3 - <<'PY'
import json, os, sys, urllib.request
model = os.environ['MODEL']
req = urllib.request.Request(
    'http://ollama:11434/api/embeddings',
    data=json.dumps({'model': model, 'prompt': '颱風停班停課的標準'}).encode(),
    headers={'Content-Type': 'application/json'},
)
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        vec = json.load(r).get('embedding')
except Exception as exc:
    print(f'  取得向量失敗：{exc}')
    sys.exit(1)
if not vec:
    print('  回應中沒有 embedding 欄位')
    sys.exit(1)
print(f'  維度 {len(vec)}，前三個分量 {[round(x, 4) for x in vec[:3]]}')
PY
  then
    ok "端到端可用：open-webui → ollama → $MODEL"
  else
    fail "無法從 open-webui 取得向量 —— 設定看起來對，但這條路實際不通。"
    failures=$((failures+1))
  fi
fi

echo
if [[ $failures -gt 0 ]]; then
  fail "套用未完成（$failures 項未通過）。"
  exit 1
fi

ok "嵌入設定已套用並驗證"
cat <<'EOF'

後續：
  • 現在才**可以**開始上傳文件 —— 在那之前換模型是免費的，之後不是（D-008）
  • qwen3-embedding 官方建議查詢加 'Instruct: <任務>\nQuery: ' 前綴。
    本腳本未設定它。要套用請在 .env 設 RAG_EMBEDDING_QUERY_PREFIX
    （這個變數是純 os.getenv，不進資料庫，所以環境變數對它有效）
  • 還原方式：bash scripts/set-embedding.sh ollama <舊模型>
EOF
