#!/usr/bin/env bash
# 輪替 endpoint 的持久 API 金鑰（`myth-` 開頭、會被烘進 Kaggle notebook 的那把）。
#
# ── 為什麼需要一支腳本，而不是「改一個地方」 ──────────────
# 那把金鑰有**三個**持有點，而且它們的性質不一樣：
#
#   1. ~/.config/endpoint/endpoint-config.yaml 的 identity.api_key
#      **真本**。每次 boot 由 commands.py 放進環境變數，master_build_notebook.py
#      讀它、烘成明文進 notebook。CLI 只在它為空時才產生，所以它不會自己輪替。
#   2. 專案 .env 的 ENDPOINT_API_KEY
#      給 scripts/connect-endpoint.sh 與 probe-openai.sh／probe_openai.py 讀的
#      **硬拷貝**。
#   3. Open WebUI 資料庫 config 表的 openai.api_keys
#      WebUI 那條連線用的**硬拷貝**，與 openai.api_base_urls 同索引對齊。
#
# **只有第 1 個的擁有者（CLI）會自我修復** —— 它會從活著的引擎 `/v1/apikey`
# 把當下的金鑰抓回來，所以它永遠跟得上。第 2、3 兩份不會：它們只會安靜地變成
# 一把死金鑰，然後在很久以後以 401 的樣子出現，而那個時候沒有人會想到是這裡。
# 那正是本專案一直在抓的那種病，所以換的時候三處要一起換。
#
# ── 順序：先全部讀完，再開始寫 ────────────────────────────
# 這支腳本在動任何一個位元組**之前**會先把三處都讀出來（＝ connect-endpoint.sh
# 的「先驗證，後接線」）。理由不是潔癖：只換到一半的狀態裡面，有一個持有點
# 拿著死金鑰，而它沒有任何症狀。與其留下那種狀態，不如什麼都不要改。
#
# ── 它刻意不做的事 ───────────────────────────────────────
#   · **不印金鑰。** 全程只印 sha256 前 12 碼（指紋）—— 指紋足以比對「三處是
#     不是同一把」，但推不回金鑰。這是本專案對憑證的一貫規矩。
#   · **不碰 Kaggle。** 不 push、不改那個 kernel；這是本機的動作。
#   · **不建備份。** 它是就地把舊值覆寫掉（寫 .tmp 再 `replace`），所以被換掉的
#     金鑰在本機**不可回復** —— 要能退回，請在執行**之前**自己備份 yaml 與 .env。
#     收尾訊息會把這件事講清楚。它以前聲稱「舊金鑰還留在備份裡」，那句話不成立
#     （2026-09-28 修正，見 DECISIONS.md 的 D-066 第六節）。
#
# ── 時間差（最容易誤解的一點）────────────────────────────
# 換完的當下，新金鑰**還沒有在任何地方生效**，而舊金鑰**也還沒有失效**：
#
#   · Kaggle 上那個 kernel 裡烘的仍是舊值。要等**下一次 boot** 推上新 notebook
#     才會換掉，在那之前它仍是「伺服器上那把明文金鑰」。
#   · 它現在沒在跑（runtime 停著），所以「舊的還能用」在實務上不成立 ——
#     但那是因為沒有東西在服務，不是因為舊金鑰死了。
#
# 所以正確的順序是**先輪替、後 boot**，而這件事必須由人記住：這支腳本不會替你
# 阻止一次太早的 boot。
#
# 用法：
#   bash scripts/rotate-endpoint-key.sh --check   只比對三處是否一致，不改任何東西
#   bash scripts/rotate-endpoint-key.sh           輪替（會要你打 yes 確認）
#   bash scripts/rotate-endpoint-key.sh --yes     輪替（不問）
#
# 結束碼：0 = 完成（或 --check 三處一致）
#         1 = 寫入失敗
#         2 = 無法判定／無法完成（例如堆疊沒開、WebUI 那一列認不得）→ **沒有改任何東西**
#         3 = 這支腳本自己的前提壞了（例如那個值在檔案裡不是恰好一筆）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose

# 這支刻意**不呼叫 load_env**：它會 cd 到專案根、沒有 .env 就從 .env.example 生
# 一個、還會寫入 WEBUI_SECRET_KEY。輪替不該有那些副作用 —— 而且它要的是那個
# 檔案的**原始那一行**，不是載入後的環境變數。
CFG="$HOME/.config/endpoint/endpoint-config.yaml"
ENVF="$PROJECT_ROOT/.env"

# 同一組 ERE 同時給 bash（grep -c 計數、sed 取值）與 python（re.findall）用，
# 所以只能寫**兩邊都認得**的語法。`[ \t]` 是量出來的答案，不是偏好：
#   · `[[:space:]]` —— python 的 re 在字元類別裡不支援（`FutureWarning: Possible
#     nested set`，而且配對數是 **0**；bash 這邊會過）。用它的話兩邊會給出
#     不同的答案，而那種不一致正是這支腳本最不該有的東西。
#   · `[ \t]` —— GNU grep／sed 的 bracket 內會把 `\t` 展開成 tab（實測：開頭是
#     tab 的行配對得到），python 的 class 內本來就支援。兩邊一致。
# 行尾的 `$` 在 sed 的 `s///` 裡是「行尾」，與 grep -E 的語意相同（都逐行看）。
RE_YAML='^[ \t]*api_key:[ \t]*(myth-[0-9a-f]{16})[ \t]*$'
RE_ENV='^ENDPOINT_API_KEY=(myth-[0-9a-f]{16})$'

MODE="rotate"
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)   MODE="check" ;;
    --yes|-y)  ASSUME_YES=true ;;
    -h|--help) usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知的參數：$1（可用：--check、--yes、-h）"; exit 2 ;;
  esac
  shift
done

# ── 讀「恰好一筆」 ───────────────────────────────────────
# 回的若不是恰好一筆，代表這支腳本的前提不成立（檔案被改過、有兩把金鑰…），
# 而那個狀態下**任何取代動作都是猜的**。所以這裡只回值，判斷留給呼叫端 ——
# 呼叫端要負責印處方（D-056 §四：硬擋不附處方就只是把問題丟回給人）。
read_unique() {   # <檔案> <ERE 含一個捕取組>
  local f="$1" re="$2" n
  n="$(grep -cE "$re" "$f" 2>/dev/null || true)"
  [[ "${n:-0}" -eq 1 ]] || return 1
  sed -nE "s/$re/\1/p" "$f"
}

if [[ ! -f "$CFG" ]]; then
  fail "找不到 $CFG —— endpoint 還沒在這台機器上設定過。"
  echo "  處方：endpoint init"
  exit 2
fi
if [[ ! -f "$ENVF" ]]; then
  fail "找不到 $ENVF —— 這個 lab 還沒啟動過。"
  echo "  處方：bash scripts/up.sh"
  exit 2
fi

if ! YAML_V="$(read_unique "$CFG" "$RE_YAML")"; then
  fail "$CFG：identity.api_key 不是「恰好一筆 myth- 金鑰」的狀態 —— 不動任何東西。"
  echo "  現在的樣子（只回數量，不印值）：符合格式的行數 $(grep -cE "$RE_YAML" "$CFG" 2>/dev/null || true)"
  echo "  處方：先自己看一眼那個檔案，或讓 CLI 重新產生（把該行清空後跑 endpoint boot）。"
  exit 3
fi
if ! ENV_V="$(read_unique "$ENVF" "$RE_ENV")"; then
  fail "$ENVF：ENDPOINT_API_KEY 不是「恰好一筆 myth- 金鑰」的狀態 —— 不動任何東西。"
  echo "  現在的樣子（只回數量，不印值）：符合格式的行數 $(grep -cE "$RE_ENV" "$ENVF" 2>/dev/null || true)"
  echo "  處方：先自己看一眼 .env 的那一行。"
  exit 3
fi

# ── 第 3 處：WebUI 的資料庫 ──────────────────────────────
# 走容器，不直接讀 volume：open_webui_storage 是**具名 volume**，在主機上是
# /var/lib/docker/volumes/... 底下 root 才讀得到的路徑。
#
# 金鑰**只經 stdin** 進容器。`python3 - <金鑰>` 或 `docker exec -e` 都會把它放進
# argv／行程環境，而 argv 是 `ps` 看得到的 —— 這條規矩本專案已經踩過一次。
WEBUI_PY="$(cat <<'PY'
import hashlib, json, re, sqlite3, sys

mode = sys.argv[1] if len(sys.argv) > 1 else "read"
lines = sys.stdin.read().split("\n")
old = lines[0] if lines else ""
new = lines[1] if len(lines) > 1 else ""

DB, KEY = "/app/backend/data/webui.db", "openai.api_keys"

def fp(v):
    return "sha256:" + hashlib.sha256(v.encode()).hexdigest()[:12] if v else "(空)"

def kind(v):
    if v == "":
        return "空字串（未設定）"
    if re.fullmatch(r"myth-[0-9a-f]{16}", v):
        return "myth- 形式"
    return "**不是 myth- 形式**"

con = sqlite3.connect(DB)
row = con.execute("select value from config where key=?", (KEY,)).fetchone()
if row is None:
    print("資料庫裡沒有 openai.api_keys 這一列", file=sys.stderr)
    sys.exit(2)
keys = json.loads(row[0] if isinstance(row[0], str) else json.dumps(row[0]))
if not isinstance(keys, list) or not keys:
    print("openai.api_keys 不是非空陣列", file=sys.stderr)
    sys.exit(2)

if mode == "read":
    # 只回「筆數|第 0 筆的指紋|第 0 筆的種類」，不印任何值。
    print("%d|%s|%s" % (len(keys), fp(keys[0]), kind(keys[0])))
    sys.exit(0)

if mode != "write":
    print("認不得的模式：%s" % mode, file=sys.stderr)
    sys.exit(3)

# 一筆：那就是 endpoint 這條連線，直接換。
# 多筆：只換「等於 config yaml 那一把」的，一筆都沒對上就拒絕 —— 覆盖一個
#       不知道是什麼的東西，比停在這裡糟。
if len(keys) == 1:
    before = kind(keys[0])
    keys = [new]
elif old and old in keys:
    before = "與 config yaml 相同的那幾筆"
    keys = [new if v == old else v for v in keys]
else:
    print("openai.api_keys 有 %d 筆，且沒有一筆等於 config yaml 的金鑰 —— 拒絕覆蓋"
          % len(keys), file=sys.stderr)
    sys.exit(2)

con.execute("update config set value=? where key=?", (json.dumps(keys), KEY))
con.commit()
print("原本：%s" % before)
print("現在：%s" % fp(new))
PY
)"

webui_read() {
  printf '\n\n' | $COMPOSE exec -T open-webui python3 -c "$WEBUI_PY" read 2>/dev/null
}

# service_running 來自 lib.sh（不開子行程的整行比對）。
if ! service_running open-webui; then
  fail "open-webui 未在執行中 —— 第 3 個持有點（WebUI 的資料庫）現在讀不到。"
  echo "  只換另外兩處會留下一個拿著**死金鑰**的持有點，而它沒有任何症狀。"
  echo "  處方：bash scripts/up.sh 之後再跑一次。"
  exit 2
fi

WEBUI_RAW="$(webui_read)"
if [[ -z "$WEBUI_RAW" ]]; then
  fail "讀不到 WebUI 的 openai.api_keys —— 不動任何東西。"
  echo "  處方：docker compose logs --tail=40 open-webui"
  exit 2
fi
IFS='|' read -r WEBUI_N WEBUI_FP WEBUI_KIND <<< "$WEBUI_RAW"

echo "── endpoint 持久 API 金鑰：三個持有點 ─────────────────"
echo "  1. config yaml（真本，boot 時烘進 notebook）"
printf '     %s\n' "$(printf '%s' "$YAML_V" | sha256sum | cut -c1-12 | sed 's/^/sha256:/')"
echo "  2. .env（connect-endpoint.sh／probe 讀的硬拷貝）"
printf '     %s\n' "$(printf '%s' "$ENV_V" | sha256sum | cut -c1-12 | sed 's/^/sha256:/')"
echo "  3. Open WebUI 資料庫（WebUI 連線用的硬拷貝）"
echo "     $WEBUI_FP（$WEBUI_N 筆，$WEBUI_KIND）"
echo

YAML_FP="$(printf '%s' "$YAML_V" | sha256sum | cut -c1-12)"
ENV_FP="$(printf '%s' "$ENV_V" | sha256sum | cut -c1-12)"
W_FP="${WEBUI_FP#sha256:}"

if [[ "$YAML_FP" == "$ENV_FP" && "$YAML_FP" == "$W_FP" ]]; then
  ok "三處一致。"
else
  warn "三處**不一致** —— 這不會有任何症狀，直到某一條路突然 401。"
  [[ "$YAML_FP" != "$ENV_FP" ]] && echo "     · .env 與 config yaml 不同"
  [[ "$YAML_FP" != "$W_FP" ]]    && echo "     · WebUI 資料庫與 config yaml 不同"
fi
echo

if [[ "$MODE" == "check" ]]; then
  echo "（--check：沒有改任何東西）"
  # 不一致回 2（無法判定），一致回 0 —— 這樣它才能當閘門用。
  # 它刻意**不**被 up.sh／deploy-vps.sh 呼叫：報告變成閘門就會變成假失敗（D-016）。
  if [[ "$YAML_FP" == "$ENV_FP" && "$YAML_FP" == "$W_FP" ]]; then exit 0; else exit 2; fi
fi

if [[ "$ASSUME_YES" != true ]]; then
  echo "即將把三個持有點一起換成新的一把。"
  echo "  · **舊金鑰不會立刻失效** —— Kaggle 上那個 kernel 仍是舊值，"
  echo "    要等下一次 boot 推上新 notebook 才換掉。"
  read -r -p "確定要繼續嗎？輸入 yes 確認：" reply
  if [[ "$reply" != "yes" ]]; then info "已取消"; exit 0; fi
fi

# 產生新值。格式與 CLI 自己產的一致（myth- ＋ 8 bytes 十六進位）。
if command -v openssl >/dev/null 2>&1; then
  NEW_V="myth-$(openssl rand -hex 8)"
else
  NEW_V="myth-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
[[ "$NEW_V" =~ ^myth-[0-9a-f]{16}$ ]] || { fail "產生的新金鑰格式不對 —— 這是腳本自己壞掉"; exit 3; }

# ── 寫入。任何一步失敗都說清楚「哪幾處已經寫了」──
HOST_PY="$(cat <<'PY'
import hashlib, os, pathlib, re, stat, sys

path, pat = sys.argv[1], sys.argv[2]
new = sys.stdin.read().strip()
f = pathlib.Path(path)
t = f.read_text()
hits = re.findall(pat, t, re.M)
if len(hits) != 1:
    print("預期恰好 1 筆，實際 %d 筆" % len(hits), file=sys.stderr)
    sys.exit(1)
old = hits[0]
if t.count(old) != 1:
    print("同一個值在檔案裡出現 %d 次（預期 1）—— 取代會波及別的行" % t.count(old), file=sys.stderr)
    sys.exit(1)
if old == new:
    print("sha256:" + hashlib.sha256(new.encode()).hexdigest()[:12])
    sys.exit(0)
mode = stat.S_IMODE(f.stat().st_mode)
tmp = f.with_name(f.name + ".tmp")
tmp.write_text(t.replace(old, new))
os.chmod(tmp, mode)
tmp.replace(f)
print("sha256:" + hashlib.sha256(new.encode()).hexdigest()[:12])
PY
)"

rotate_file() {   # <檔案> <ERE>
  local out
  if ! out="$(printf '%s' "$NEW_V" | python3 -c "$HOST_PY" "$1" "$2")"; then
    return 1
  fi
  printf '%s' "$out"
}

DONE=()

if rotate_file "$CFG" "$RE_YAML" >/dev/null; then
  DONE+=("config yaml"); ok "1. config yaml 已換"
else
  fail "1. config yaml 寫入失敗 —— 沒有做後面的步驟。"
  exit 1
fi
if rotate_file "$ENVF" "$RE_ENV" >/dev/null; then
  DONE+=(".env"); ok "2. .env 已換"
else
  fail "2. .env 寫入失敗 —— 已經寫過：${DONE[*]}；**還沒寫**：WebUI 資料庫。"
  exit 1
fi

if printf '%s\n%s\n' "$YAML_V" "$NEW_V" | $COMPOSE exec -T open-webui python3 -c "$WEBUI_PY" write; then
  DONE+=("WebUI 資料庫"); ok "3. Open WebUI 資料庫已換"
else
  fail "3. WebUI 資料庫寫入失敗 —— 已經寫過：${DONE[*]}；**還沒寫**：WebUI 資料庫。"
  echo "  那兩個現在拿著新金鑰、WebUI 拿著舊的。處方：修好之後重跑一次這支腳本"
  echo "  （它是冪等的：已換過的那兩處會再換一次，不會壞）。"
  exit 1
fi

# ── 本機快取 ─────────────────────────────────────────────
# ~/.cache/endpoint/apikey 由 CLI 的 register 路徑先讀、讀不到才去問 /v1/apikey
# （core.py 的 load_cached_apikey）。留著舊值會讓下一次 register 拿著死金鑰去註冊。
CACHE="$HOME/.cache/endpoint/apikey"
if [[ -e "$CACHE" ]]; then
  rm -f "$CACHE"
  ok "已清除本機金鑰快取（$CACHE）"
else
  info "本機金鑰快取不存在（正常 —— boot 時就會清）"
fi

echo
ok "三個持有點都換好了（指紋 $(printf '%s' "$NEW_V" | sha256sum | cut -c1-12 | sed 's/^/sha256:/')）"
cat <<'EOF'

接下來（照這個順序）：

  1. 下一次 boot **之前**不需要做別的事 —— 但要知道 Kaggle 上那個 kernel
     仍是舊金鑰，而它是**明文**。要讓舊的失效，就得 boot 一次，
     讓新的 notebook 推上去。
  2. boot 之後，WebUI 那條連線用的就是新金鑰了（三處已經一致）。
     如果連線有問題，先跑：
         bash scripts/connect-endpoint.sh --status
     它會顯示 WebUI 現在指向誰。

**舊金鑰沒有副本 —— 這支腳本是就地覆寫，不建備份。**
  · 它在本機那三個持有點已經被蓋掉，**不可回復**。
  · 它還活著的地方只有一個：Kaggle 上**已經推上去的那份 notebook**（明文）。
    下一次 boot 推上新的一份時，它才真正失效。
  · 所以：**從現在到下一次 boot 之間，是沒有退路的。** 若你需要能退回，
    請自己在執行這支腳本**之前**備份 yaml 與 .env。
  · 那些路徑若存在 *.bak-*，是**以前手動**留下的，**不是這支腳本建的**，
    內容也不見得是剛剛被換掉的那把。
EOF
