#!/usr/bin/env bash
# 突變測試：把第四刀的修正**故意弄壞**，確認 test_endpoint_apikey_selfheal.py 真的會叫。
#
#   bash scripts/test_endpoint_apikey_selfheal_mutants.sh <已套第四刀的樹>
#   bash scripts/test_endpoint_apikey_selfheal_mutants.sh --live
#
# 為什麼需要這一步（D-024 第八節，同樣的理由再來一次）：一組「永遠通過」的
# 測試和「永遠失敗」的測試一樣沒用。把受測程式碼修好、測試跟著變綠，這件事
# 本身不證明測試有在檢查任何東西。**唯一能證明測試有效的方法，是讓它面對
# 一個已知的錯誤，然後看它有沒有叫。**
#
# ── 這一台和家族裡其他台不同的地方：它不問「紅不紅」，問「哪一句叫」──
# 切 C 的突變台得處理「套用腳本裡手寫的雜湊會被任何突變驚動」的噪音，所以它
# 用 `D4` 這種檢查編號指名。第四刀的驗證器**沒有雜湊檢查**，所以每個突變只會
# 驚動它該驚動的那一條 —— 但「紅了」在這裡仍然不夠：一個把六條檢查全部寫成
# `return False` 的驗證器也會全紅，而且紅得一模一樣。
#
# 所以每一條突變**指名它該被哪一句話抓到**（`期望` 欄），而那句話是驗證器印
# 出來的**失敗訊息本身**。用訊息而不是編號是刻意的：訊息沒有另外維護的標籤
# 可以漂移；驗證器改壞了，這裡就對不上，這台會報「沒抓到」而不是放行。
#
# `=NOFAIL` 是守門：這一條**不准**讓驗證器失敗。用來證明檢查是照語意判斷、
# 不是照字面 —— 例如把免認證清單的成員換個順序，語意完全相同，A 不該叫。
#
# ── 植入的位置：AST 界定的函式範圍 ＋ 該範圍內唯一 ──────────────
# 目標字串常常在檔案裡出現不只一次：`api_key = config.api_key or …` 在修補後的
# commands.py 裡就有兩份（兩個自癒點逐字相同）。所以突變指定一個**外層函式名**，
# 字串只要在那個函式的行範圍內恰好出現一次即可。`範圍` 寫 `-` 表示整檔。
#
# **錨點一律單行。** 這不是風格偏好：表格是用 `IFS='|' read` 逐列解析的，而
# `read` 一次只讀一行 —— 多行的錨點會在欄位邊界上被**安靜地截斷**，然後突變
# 植入一個殘缺的字串。單行 ＋ 範圍是這個格式唯一安全的組合。
#
# 用法：bash scripts/test_endpoint_apikey_selfheal_mutants.sh <已套第四刀的樹>
#
# 結束碼：0 = 每條突變都被指定的那句話抓到、每個守門的都沒叫、對照組通過
#         1 = 有突變沒被抓到（驗證器有洞）、或對照組失敗（驗證器壞了）
#         2 = 用法錯誤
#         3 = 突變清單本身寫壞了（錨點在範圍內不唯一、欄位數不對）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFIER="$SCRIPT_DIR/test_endpoint_apikey_selfheal.py"

# **不要寫 .pyc。** 家族筆記（test_endpoint_topic_secret_mutants.sh:52-56）記過
# 這個坑：CPython 對 .pyc 的有效性檢查只比對原始檔的 mtime（**秒**）與大小，
# 同長度、同一秒的兩次寫入會命中前一次的 .pyc，突變根本沒被執行，而突變台
# 把它報成「抓到」。這裡的驗證器只做 AST 剖析、不 import，所以本來就不會產生
# .pyc —— 但還是明文關掉，因為「本來就不會」正是那種會在改版後悄悄失效的前提。
export PYTHONDONTWRITEBYTECODE=1

if [[ $# -ne 1 ]]; then
    echo "用法：$0 <已套第四刀的樹>  或  $0 --live" >&2
    exit 2
fi

if [[ "$1" == "--live" ]]; then
    TREE="$(python3 - <<'PY'
import subprocess
from pathlib import Path
base = Path.home() / ".local/share/uv/tools/endpoint-vps"
hit = subprocess.run(
    ["find", str(base), "-path", "*/site-packages/endpoint/commands.py", "-print", "-quit"],
    capture_output=True, text=True,
).stdout.strip()
print(str(Path(hit).parent.parent) if hit else "")
PY
)"
    [[ -n "$TREE" ]] || { echo "✗ 找不到安裝樹" >&2; exit 2; }
else
    TREE="$1"
fi

[[ -d "$TREE/engine" && -d "$TREE/endpoint" ]] || {
    echo "✗ 這不是一棵安裝樹（缺 engine/ 或 endpoint/）：$TREE" >&2; exit 2; }

ENGINE="engine/engine.py"
COMMANDS="endpoint/commands.py"
BYPASS5='    bypass = {"/health", "/metrics", "/tunnel", "/docs", "/openapi.json"}'

# F1 的新字串同時含單引號與雙引號，寫進單引號的表格列會把 shell 的引號規則
# 弄得一團亂，所以在這裡先組出來 —— 字面值仍然一目了然。（家族筆記：切 C 的
# G2 因為三引號做了同一件事。）
F1_NEW='                "Authentication denied — run '"'"'endpoint register'"'"' for a fresh key."'

# 突變表：標籤|T:樹內路徑|範圍|原字串|取代字串|期望
#   期望 = 一句必須出現的失敗訊息；`=NOFAIL` = 這一條不准讓驗證器失敗。
MUTATIONS=(
  # ── A：免認證清單 ──
  "A1 把 /v1/apikey 放回免認證清單|T:${ENGINE}|-|${BYPASS5}|${BYPASS5%?}, \"/v1/apikey\"}|is still in the auth bypass set"
  "A2 清單被削成只剩 /v1/apikey|T:${ENGINE}|-|${BYPASS5}|    bypass = {\"/v1/apikey\"}|is still in the auth bypass set"
  # ── B：路由本體 ──
  "B1 路由改名（等於刪掉）|T:${ENGINE}|-|@app.get(\"/v1/apikey\")|@app.get(\"/v1/apikey-x\")|route was deleted"
  # ── C：自癒讀真本 ──
  "C1 _register_with_proxy 改回讀快取|T:${COMMANDS}|_register_with_proxy|    api_key = config.api_key or load_cached_apikey() or \"\"|    api_key = load_cached_apikey() or \"\"|_register_with_proxy does not read config.api_key first"
  "C2 run_register 改回讀快取|T:${COMMANDS}|run_register|    api_key = config.api_key or load_cached_apikey() or \"\"|    api_key = load_cached_apikey() or \"\"|run_register does not read config.api_key first"
  "C3 自癒又走回 tunnel|T:${COMMANDS}|_register_with_proxy|    save_cached_apikey(api_key)|    _probe = client.get_api_key(); save_cached_apikey(api_key)|still called from commands.py"
  # ── D：缺金鑰要大聲 ──
  "D1 缺金鑰時又開始嘗試|T:${COMMANDS}|_register_with_proxy|        console.warn(\"Proxy registration: no local API key configured.\")|        for _attempt in range(4): pass|still attempts work"
  "D2 缺金鑰時靜默返回|T:${COMMANDS}|_register_with_proxy|        console.warn(\"Proxy registration: no local API key configured.\")|        pass|returns silently"
  # ── E：訊號兩端 ──
  # `_API_KEY: str | None = None` 是更直觀的錨點，但它含 `|` —— 那是這份表格的
  # 欄位分隔字元，寫進去會同時弄壞欄位計數與錨點本身。改錨在同一段的另一行。
  "E1 引擎又開始發 APIKEY: 訊號|T:${ENGINE}|-|_API_KEY_LOCK = threading.Lock()|_APIKEY_SIGNAL = \"APIKEY:x\"; _API_KEY_LOCK = threading.Lock()|engine.py still references the APIKEY: signal"
  "E2 取用端又回來讀 APIKEY: 訊號|T:${COMMANDS}|_register_with_proxy|    api_key = config.api_key or load_cached_apikey() or \"\"|    api_key = config.api_key or load_cached_apikey() or \"\"; _s = \"APIKEY:x\"|commands.py still references the APIKEY: signal"
  # ── F：401 處方 ──
  "F1 401 提示又指向 endpoint register|T:${COMMANDS}|-|                \"Authentication denied — the key this machine holds is not the \"|${F1_NEW}|still points at"
  "F2 401 提示不給任何處方|T:${COMMANDS}|-|                \"  Re-run 'endpoint boot' so the engine is rebuilt with the local key.\"|                \"  Good luck.\"|names no workable remedy"

  # ── 守門：不准讓驗證器失敗 ──
  "G1 免認證清單換順序（語意相同）|T:${ENGINE}|-|${BYPASS5}|    bypass = {\"/metrics\", \"/health\", \"/openapi.json\", \"/docs\", \"/tunnel\"}|=NOFAIL"
  "G2 加了含 APIKEYS 但非訊號的字串|T:${ENGINE}|-|_API_KEY_LOCK = threading.Lock()|_APIKEY_HINT = \"APIKEYS for the admin panel\"; _API_KEY_LOCK = threading.Lock()|=NOFAIL"
)

# ── 欄位數檢查 ───────────────────────────────────────────
# 每一條都必須**恰好**六欄。多寫一個 `|` 就會被切錯，而且症狀是**靜默**的：
# 錨點變成半截字串、找不到、於是報「突變清單寫壞了」—— 或者更糟，找到了一個
# 剛好也唯一的半截字串。所以在跑之前先驗欄位數。
for row in "${MUTATIONS[@]}"; do
    if [[ "$(tr -cd '|' <<<"$row" | wc -c)" != "5" ]]; then
        echo "✗ 突變清單寫壞了（這列不是六欄）：$row" >&2
        exit 3
    fi
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 在 `範圍` 內把 `原字串` 換成 `取代字串`，要求範圍內恰好出現一次。
# `範圍` 為 `-` 表示整檔。範圍用行號界定：從 `def 名字(` 的那一行，到檔案結尾或
# 下一個衝到第 0 欄的 `def `/`class `。
mutate() {
    python3 - "$1" "$2" "$3" "$4" <<'PY'
import sys
from pathlib import Path

path, scope, old, new = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

if scope == "-":
    lo, hi = 0, len(lines)
else:
    needles = (f"def {scope}(", f"async def {scope}(")
    lo = next((i for i, l in enumerate(lines) if l.startswith(needles)), None)
    if lo is None:
        sys.exit(f"找不到函式 {scope}")
    hi = next(
        (i for i in range(lo + 1, len(lines))
         if lines[i].startswith(("def ", "async def ", "class "))),
        len(lines),
    )

body = "".join(lines[lo:hi])
n = body.count(old)
if n != 1:
    sys.exit(f"錨點在 {scope} 範圍內出現 {n} 次（要恰好 1 次）：{old[:60]!r}")

lines[lo:hi] = [body.replace(old, new, 1)]
path.write_text("".join(lines), encoding="utf-8")
PY
}

# 對照組：未突變的樹必須通過。這台若連這個都過不了，後面每一條「抓到」都是假的。
echo "對照組：未突變的樹  $TREE"
if ! python3 "$VERIFIER" "$TREE" >/dev/null 2>&1; then
    echo "✗ 對照組失敗 —— 這棵樹本身就跑不過驗證器，突變測試無從談起。" >&2
    python3 "$VERIFIER" "$TREE" >&2 || true
    exit 1
fi
echo "✓ 對照組通過"
echo

pass=0 fail=0

for row in "${MUTATIONS[@]}"; do
    IFS='|' read -r label target scope old new expect <<<"$row"
    rel="${target#T:}"

    mutant="$work/m"
    rm -rf "$mutant"; mkdir -p "$mutant"
    cp -r "$TREE/engine" "$TREE/endpoint" "$mutant/"

    if ! err="$(mutate "$mutant/$rel" "$scope" "$old" "$new" 2>&1)"; then
        echo "✗ 突變清單寫壞了：[$label] $err" >&2
        exit 3
    fi

    out="$(python3 "$VERIFIER" "$mutant" 2>&1)" && rc=0 || rc=$?

    if [[ "$expect" == "=NOFAIL" ]]; then
        if [[ "$rc" == "0" ]]; then
            echo "✓ [$label] 守門：沒有被驚動"
            pass=$((pass + 1))
        else
            echo "✗ [$label] 守門失敗：不該叫卻叫了"
            grep -E '^    ✗|^    -' <<<"$out" | sed 's/^/      /' || true
            fail=$((fail + 1))
        fi
    elif [[ "$rc" == "0" ]]; then
        echo "✗ [$label] 沒抓到 —— 驗證器對這個破壞無感"
        fail=$((fail + 1))
    elif grep -qF "$expect" <<<"$out"; then
        echo "✓ [$label] 被「$expect」抓到"
        pass=$((pass + 1))
    else
        echo "✗ [$label] 是紅了，但不是指名的那一句（預期含「$expect」）"
        grep -E '^    ✗|^    -' <<<"$out" | sed 's/^/      /' || true
        fail=$((fail + 1))
    fi
done

echo
if (( fail > 0 )); then
    echo "✗ $fail 條沒過、$pass 條過 —— 驗證器有洞或突變清單有誤。"
    exit 1
fi

echo "✓ ${#MUTATIONS[@]} 條全過：每條破壞都被指名的那一句抓到，守門的都沒叫。"
