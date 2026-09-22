#!/usr/bin/env bash
# RAG 四項的 **HTTP 路徑** 驗證：把 rag_http_probe.py 送進 open-webui 容器執行。
#
# 為什麼要繞這一段：docker-compose.yml 刻意不對外發布 Ollama 的 11434 埠
# （見 DECISIONS.md D-003），主機端連不到 Ollama。容器內則同時有應用程式
# 自己的 HTTP 服務（127.0.0.1:8080）與網路 —— 探針打的是**應用程式自己的
# API**，走的是 UI 走的那條路。
#
# 與 rag-verify.sh 的分工：
#   rag-verify.sh      走應用程式自己的**函式**，不需要帳號，分得出
#                      「檢索挑錯段落」與「模型沒用檢索到的內容」。
#   rag-http-verify.sh 走應用程式自己的**HTTP API**，需要一組 API 金鑰，
#                      證明「上傳文件那條路」也能動，但看不到 chunk 排名。
#   兩支一起跑，才涵蓋 README「Phase 2: RAG」那四項的兩半。
#
# ── 關於 API 金鑰（很重要）──────────────────────────────────
#
# 金鑰放 .env，變數名 WEBUI_API_KEY：
#
#   WEBUI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
#
# 取得方式：登入 Open WebUI → 左下角頭像 → 設定 → 帳號 → API 金鑰 → 建立。
#
# **但這個功能預設是關閉的。** 2026-09-20 實查本堆疊的資料庫：
# config 表 `auth.enable_api_keys = false`、api_key 表 0 筆，而 routers/auths.py
# 的 _check_api_key_permission 在它為 false 時一律回 403。所以要先去
# **管理員控制台 → 設定 → 驗證 → API 金鑰** 把它打開
# （直接網址 http://localhost:3000/?settings=admin:authentication）。
#
# 那個開關改的是 config 表，與 D-013 同一個形狀：**改 .env 沒有用**，
# 第一次開機播種之後資料庫的值一律優先。好消息是它每次請求都重讀，
# 開關按下去就生效，不必重建容器。
#
# 這支腳本**不把金鑰放進命令列**。`docker compose exec -e KEY=...` 會把它
# 寫進主機端 `docker compose` 的 argv，同機器上任何人 `ps` 都看得到。這裡
# 改用 stdin 餵給容器內的 python（`printf` 是 bash 內建，不產生行程，因此
# 也不會出現在 ps 裡）。金鑰不會被印出來，`--json` 的輸出裡也沒有。
#
# 用法：
#   bash scripts/rag-http-verify.sh                      使用 .env 的 OLLAMA_MODEL
#   bash scripts/rag-http-verify.sh --model qwen2.5:3b   指定模型
#   bash scripts/rag-http-verify.sh --timeout 900        單次生成的時限（秒）
#   bash scripts/rag-http-verify.sh --json               機器可讀輸出
#
# 結束碼：0 全部通過、1 有項目未通過、2 無法判定（金鑰未設定／連不上／太慢）、
#         3 探針自己壞掉。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PROBE_LOCAL="$PROJECT_ROOT/scripts/rag_http_probe.py"
SHARED_LOCAL="$PROJECT_ROOT/scripts/rag_grounding_probe.py"
PROBE_REMOTE=/tmp/rag_http_probe.py
SHARED_REMOTE=/tmp/rag_grounding_probe.py
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --model=*) MODEL="${1#--model=}"; shift ;;
    --timeout) ARGS+=("--timeout" "$2"); shift 2 ;;
    --timeout=*) ARGS+=("--timeout" "${1#--timeout=}"); shift ;;
    --json) ARGS+=("--json"); shift ;;
    --keep) ARGS+=("--keep"); shift ;;
    -h|--help)
      # 印出檔頭那段註解。判準是**內容**（一直印到第一行非註解、非空行為止），
      # 不是行號 —— 行號會在下次有人補一行說明時默默錯掉，而錯掉的 --help 比
      # 沒有 --help 更糟。
      #
      # 這裡原本是一段自己寫的 awk（`NR>1 && /^#/ …; NR>1 {exit}`），判準看起來
      # 一樣，但它在**第一個空行**就停 —— 檔頭中間若有空行就會截斷，而且它連
      # 檔頭與程式碼之間那個空行都不印。那是 test_usage_text.sh 的 C 節抓到的
      # （它與對該檔獨立推導出來的檔頭逐字比對），不是我看出來的。
      usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      fail "不認得的參數：$1"
      echo "  用法：bash scripts/rag-http-verify.sh [--model <模型>] [--timeout <秒>] [--json] [--keep]"
      exit 1 ;;
  esac
done

require_docker
detect_compose
load_env

# 模型一律走位置參數優先、其次 .env（理由同 rag-verify.sh：load_env 會
# source .env，把命令列的環境變數覆蓋掉 —— D-011 記錄過這個根因）。
MODEL="${MODEL:-${OLLAMA_MODEL:-}}"
if [[ -z "$MODEL" ]]; then
  fail "沒有指定模型，且 .env 裡也沒有 OLLAMA_MODEL。"
  echo "  請用：bash scripts/rag-http-verify.sh --model qwen2.5:3b"
  exit 1
fi

# ── 金鑰 ────────────────────────────────────────────────
# 缺少金鑰是**設定還沒完成**，不是「RAG 未通過」。所以要退出碼 2（無法判定），
# 不是 1 —— 把它報成失敗，等於叫人去查一個沒壞的系統。
if [[ -z "${WEBUI_API_KEY:-}" ]]; then
  fail "無法判定：.env 裡沒有 WEBUI_API_KEY。"
  echo "  這不是 RAG 的結論 —— 是這項驗證還缺一個前置條件。"
  echo
  echo "  步驟 0（2026-09-20 實查本堆疊的資料庫：auth.enable_api_keys = false，"
  echo "  且 api_key 表 0 筆。**API 金鑰這個功能目前是關閉的**，直接去帳號頁"
  echo "  會找不到「建立」；就算找到也會得到 403 API_KEY_CREATION_NOT_ALLOWED。"
  echo "  這與 D-013 同一個形狀：設定值存在資料庫，改 .env 沒有用。）"
  echo "     管理員控制台 → 設定 → 驗證 → 「API 金鑰」開關打開"
  echo "     直接網址：http://localhost:3000/?settings=admin:authentication"
  echo
  echo "  1. 左下角頭像 → 設定 → 帳號 → API 金鑰 → 建立"
  echo "  2. 把金鑰寫進 .env，加一行："
  echo "         WEBUI_API_KEY=<你的金鑰>"
  echo "  3. 重跑這支腳本"
  echo
  echo "  金鑰不需要貼到任何對話裡，本腳本也不會把它印出來。"
  exit 2
fi

# ── 前置檢查 ────────────────────────────────────────────
for svc in ollama open-webui; do
  if ! service_running "$svc"; then
    fail "$svc 未在執行中。請先啟動堆疊：bash scripts/up.sh"
    exit 1
  fi
done

# 生成用的模型必須在。嵌入模型不在這裡檢查 —— 那是探針要報的結論之一。
installed_raw="$($COMPOSE exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
found=0
while IFS= read -r line; do
  if [[ -n "$line" && "$(normalize_model "$line")" == "$(normalize_model "$MODEL")" ]]; then
    found=1
    break
  fi
done <<<"$installed_raw"

if [[ $found -eq 0 ]]; then
  fail "ollama 裡沒有模型 $MODEL。"
  echo "  請先下載：bash scripts/pull-model.sh $MODEL"
  exit 1
fi
ok "生成模型 $MODEL 已在 ollama 中"

# 這裡**不先驗金鑰有效性**。驗了只會多一次往返，而且驗不過的話還是得跑
# 探針才知道是哪一種（401？權限不足？服務沒起來？）—— 那正是探針第一步
# 在做的事，交給它報，訊息才會一致。
if ! wait_for_webui 60; then
  fail "無法判定：open-webui 在 60 秒內沒有回應 /health。"
  echo "  請看：$COMPOSE logs --tail=50 open-webui"
  exit 2
fi

echo
info "複製探針腳本至 open-webui 容器..."
# 一次複製兩個檔：rag_http_probe.py 會 import rag_grounding_probe，
# 為的是**共用**那份捏造文件、題庫與評分樣式 —— 抄一份過來等於保證
# 兩份會分岔，而分岔的那一天，兩支腳本會對同一個回應給出不同答案。
for pair in "$PROBE_LOCAL:$PROBE_REMOTE" "$SHARED_LOCAL:$SHARED_REMOTE"; do
  if ! $COMPOSE cp "${pair%%:*}" "open-webui:${pair##*:}"; then
    fail "無法複製 ${pair%%:*} 進容器。請確認 open-webui 仍在執行中。"
    exit 2
  fi
done

info "開始驗證（模型 $MODEL，會實際上傳、嵌入並生成兩次，請勿中斷）..."
echo

# 暫時關閉 errexit，才能取得結束碼再自行判斷。
# `printf` 是 bash 內建 → 金鑰不經過任何子行程的 argv。見檔頭說明。
set +e
printf '%s\n' "$WEBUI_API_KEY" | $COMPOSE exec -T open-webui \
  python3 -u "$PROBE_REMOTE" --model "$MODEL" "${ARGS[@]}"
STATUS=$?
set -e

# 清理容器內的暫存檔；失敗不影響結果，但會講一聲。
# 這裡的 rm 只刪暫存**腳本**。測試知識庫與上傳文件的清理在 Python 端，
# 而且是在 finally 裡做的 —— 它必須在探針自己崩潰時也發生。
if ! $COMPOSE exec -T open-webui rm -f "$PROBE_REMOTE" "$SHARED_REMOTE" >/dev/null 2>&1; then
  warn "無法刪除容器內的暫存腳本（不影響結果）"
fi

echo
case $STATUS in
  0) ok "RAG HTTP 路徑驗證通過" ;;
  1) fail "RAG HTTP 路徑驗證未通過 —— 請看上方標示「未通過」的項目。" ;;
  2) warn "無法判定 —— 金鑰未設定、連不上，或有東西沒在時限內跑完。這不是失敗。" ;;
  3) fail "探針本身執行失敗 —— 這**不是** RAG 的結論。" ;;
  *) fail "探針異常結束（結束碼 $STATUS）" ;;
esac

exit $STATUS
