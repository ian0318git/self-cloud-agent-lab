#!/usr/bin/env bash
# 突變測試：把切 A 的修正**故意弄壞**，確認 test_endpoint_apikey_broadcast_fixes.py 真的會叫。
#
#   bash scripts/test_endpoint_apikey_broadcast_fixes_mutants.sh <已修補的樹>
#   bash scripts/test_endpoint_apikey_broadcast_fixes_mutants.sh <已修補的樹> --pristine <原始樹>
#
# ── 這台為什麼存在（2026-10-01，D-076）────────────────────────────────
# 切 A 的驗證器是家族裡**唯一沒有突變台的一支**。它一直只被「雙向驗證」證明過
# ——對修補後的樹要過、對原始樹要失敗——而雙向只證明它有**分辨力**，不證明它
# 抓得住**每一件它宣稱要抓的事**。
#
# 這件事在 D-076 變成硬需求：D-070 曾經合法地編輯過這支驗證器（它的定位錨從被
# 刪掉的 `APIKEY:` 分支搬到 `TUNNEL ACQUIRED:`），而 D-076 要把切 B 驗證器裡那個
# 釘住它的 sha 更新到編輯後的版本。**重 pin 等於替「編輯過的它仍然可信」背書**
# （D-074 第七節的原話）。背書要有量測撐著，不是一句話。
#
# 為什麼需要這一步（D-024 第八節，家族共用）：一組「永遠通過」的測試和「永遠
# 失敗」的測試一樣沒用。**唯一能證明測試有效的方法，是讓它面對一個已知的錯誤，
# 然後看它有沒有叫。**
#
# ── 和家族其他台一樣：不問「紅不紅」，問「哪一句叫」────────────────
# 每一條突變**指名它該被哪一句話抓到**（`期望` 欄），而那句話是驗證器印出來的
# **失敗訊息本身**。用訊息而不是編號是刻意的：訊息沒有另外維護的標籤可以漂移。
#
# `=NOFAIL` 是守門：這一條**不准**讓驗證器失敗。
#
# ── 植入的位置：AST 界定的函式範圍 ＋ 該範圍內唯一 ──────────────
# **錨點一律單行。** 表格是用 `IFS='|' read` 逐列解析的，`read` 一次只讀一行。
#
# ── 這台**沒有**涵蓋的（明講，不要讀成涵蓋了）────────────────────
# 檢查 B 有兩條路徑。第一條（`APIKEY:` 分支已退場）是安裝樹走的那條，由 B1 涵蓋。
# 第二條（分支還在）只有**早於第四刀的樹**走得到，由 P1 涵蓋，而 P1 需要 `--pristine`。
#
# 第二條路徑的另一個斷言——「`TUNNEL ACQUIRED:` 分支必須 break」——**沒有突變涵蓋**。
# 原因是格式限制不是判斷：那個 `break` 獨占一行，而它在 run_boot 裡出現 3 次
# （單行錨點不唯一），要唯一就得用多行錨點，而多行錨點正是上面那條規矩擋掉的東西。
# **它是被斷言了、但沒有被突變測過的一條。** 要補得先改這個表格格式。
#
# 用法：bash scripts/test_endpoint_apikey_broadcast_fixes_mutants.sh <已修補的樹> [--pristine <原始樹>]
#
# 結束碼：0 = 每條突變都被指定的那句話抓到、守門的都沒叫、對照組通過
#         1 = 有突變沒被抓到（驗證器有洞）、或對照組失敗（驗證器壞了）
#         2 = 用法錯誤
#         3 = 突變清單本身寫壞了（錨點在範圍內不唯一、欄位數不對）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERIFIER="$SCRIPT_DIR/test_endpoint_apikey_broadcast_fixes.py"

# **不要寫 .pyc。** 家族筆記（test_endpoint_topic_secret_mutants.sh:52-56）記過
# 這個坑：CPython 對 .pyc 的有效性檢查只比對原始檔的 mtime（**秒**）與大小，
# 同長度、同一秒的兩次寫入會命中前一次的 .pyc，突變根本沒被執行，而突變台
# 把它報成「抓到」。
export PYTHONDONTWRITEBYTECODE=1

PRISTINE=""
if [[ $# -eq 1 ]]; then
    TREE="$1"
elif [[ $# -eq 3 && "$2" == "--pristine" ]]; then
    TREE="$1"; PRISTINE="$3"
else
    echo "用法：$0 <已修補的樹> [--pristine <原始樹>]" >&2
    exit 2
fi

[[ -d "$TREE/engine" && -d "$TREE/endpoint" ]] || {
    echo "✗ 這不是一棵樹（缺 engine/ 或 endpoint/）：$TREE" >&2; exit 2; }
if [[ -n "$PRISTINE" ]]; then
    [[ -d "$PRISTINE/engine" && -d "$PRISTINE/endpoint" ]] || {
        echo "✗ --pristine 不是一棵樹：$PRISTINE" >&2; exit 2; }
fi

ENGINE="engine/engine.py"
COMMANDS="endpoint/commands.py"

# 突變表：標籤|T:樹內路徑|範圍|原字串|取代字串|期望
#   期望 = 一句必須出現的失敗訊息；`=NOFAIL` = 這一條不准讓驗證器失敗。
MUTATIONS=(
  # ── A：外洩的位元組（切 A 砍掉的就是這一行）──
  "A1 把被切 A 刪掉的那則廣播加回去|T:${ENGINE}|_startup|    _broadcast(\"STARTING...\")|    _broadcast(\"STARTING...\"); _broadcast(f\"APIKEY:{_get_api_key()}\")|有 1 則發布含明文金鑰"
  # ── B 第一條路徑：分支已退場，驗訊號兩端皆不存在（D-070 新寫的那一段）──
  "B1 只拆一半：訊號的一端還在|T:${COMMANDS}|run_boot|        _last_status_print = 0.0|        _last_status_print = 0.0; _legacy = \"APIKEY:\"|分支不見了，但訊號的另一端還在"
  # ── C：路由與它的輔助函式（D-070 說「斷言不變、只有理由變了」——這裡驗那句話）──
  "C1 路由改名（等於刪掉）|T:${ENGINE}|-|@app.get(\"/v1/apikey\")|@app.get(\"/v1/apikey-x\")|找不到 GET /v1/apikey 路由"
  "C2 路由還在，但不回傳金鑰|T:${ENGINE}|-|    return {\"key\": _get_api_key()}|    return {\"key\": None}|但已經不回傳金鑰了"
  # ── 守門 ──
  "G1 只加一個註解（語意不變）|T:${ENGINE}|_startup|    _broadcast(\"STARTING...\")|    _broadcast(\"STARTING...\")  # 語意相同|  =NOFAIL"
)

# B 第二條路徑（分支還在）只有早於第四刀的樹走得到。
# 錨點裡的 `→` 在原始檔裡是**一個字面的反斜線接 `u2192`**（Python 字串裡的轉義），
# 不是箭頭字元。在 bash 雙引號裡要寫 `\\u2192` 才會得到那一個反斜線 —— 寫成
# `\\\\u2192` 會多一層，錨點就 0 次（這一條第一次跑就是這樣錯的，od -c 才看出來）。
PRISTINE_MUTATIONS=(
  "P1 上游讓 APIKEY: 分支 return|T:${COMMANDS}|run_boot|                        console.print(\"  [dim]\\u2192 API key registered with engine[/]\")|                        console.print(\"  [dim]\\u2192 API key registered with engine[/]\"); return None|APIKEY: 分支會離開等待迴圈"
)

for row in "${MUTATIONS[@]}" "${PRISTINE_MUTATIONS[@]}"; do
    if [[ "$(tr -cd '|' <<<"$row" | wc -c)" != "5" ]]; then
        echo "✗ 突變清單寫壞了（這列不是六欄）：$row" >&2
        exit 3
    fi
done

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 在 `範圍` 內把 `原字串` 換成 `取代字串`，要求範圍內恰好出現一次。
# `範圍` 為 `-` 表示整檔。
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

# 對照組。**兩棵樹的期望相反**：修補後的樹必須過，原始樹必須失敗（那是被示範
# 的蟲本身）。兩邊都過或兩邊都失敗，後面的每一條「抓到」都是假的。
echo "對照組 ①：已修補的樹必須通過  $TREE"
if ! python3 "$VERIFIER" "$TREE" >/dev/null 2>&1; then
    echo "✗ 對照組①失敗 —— 這棵樹本身就跑不過驗證器，突變測試無從談起。" >&2
    python3 "$VERIFIER" "$TREE" >&2 || true
    exit 1
fi
echo "✓ 對照組①通過"
echo

out="$(python3 "$VERIFIER" "$TREE" 2>&1)" && rc=0 || rc=$?
if [[ -n "$PRISTINE" ]]; then
    echo "對照組 ②：原始樹必須失敗，而且**只**該失敗在檢查 A  $PRISTINE"
    pout="$(python3 "$VERIFIER" "$PRISTINE" 2>&1)" && prc=0 || prc=$?
    if [[ "$prc" == "0" ]]; then
        echo "✗ 對照組②失敗 —— 驗證器對原始樹也過，它在檢查空氣。" >&2
        exit 1
    fi
    # 原始樹上檢查 B、C 是守衛，本來就會過；紅的只能是 A。紅了別的就要先查清楚。
    if grep -qF "分支不見了，但訊號的另一端還在" <<<"$pout" \
       || grep -qF "TUNNEL ACQUIRED: 分支沒有 break" <<<"$pout"; then
        echo "✗ 對照組②失敗 —— 原始樹上除了檢查 A 之外還有別的紅，先查清楚再跑突變。" >&2
        exit 1
    fi
    echo "✓ 對照組②通過（紅的只有檢查 A）"
    echo
fi

pass=0 fail=0

# 去前導與尾隨空白。**只用在 `期望` 欄** —— 其他五欄的空白是內容的一部分
# （`原字串` 少了縮排就對不上），去掉了會把錨點弄壞。
# 這條第一次跑就咬到自己：表格為了對齊寫成 `|  =NOFAIL`，於是 `expect` 是
# `"  =NOFAIL"`，不等於 `=NOFAIL`，守門那一列被當成「該抓到卻沒抓到」而報紅。
trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

run_row() {
    local row="$1" base="$2" tree_label="$3"
    local label target scope old new expect rel mutant err out rc
    IFS='|' read -r label target scope old new expect <<<"$row"
    expect="$(trim "$expect")"
    rel="${target#T:}"

    mutant="$work/m"
    rm -rf "$mutant"; mkdir -p "$mutant"
    cp -r "$base/engine" "$base/endpoint" "$mutant/"

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
            grep -E '^    ✗|^    →' <<<"$out" | sed 's/^/      /' || true
            fail=$((fail + 1))
        fi
    elif grep -qF "$expect" <<<"$out"; then
        echo "✓ [$label] 被「$expect」抓到  ($tree_label)"
        pass=$((pass + 1))
    elif [[ "$rc" == "0" ]]; then
        echo "✗ [$label] 沒抓到 —— 驗證器對這個破壞無感  ($tree_label)"
        fail=$((fail + 1))
    else
        echo "✗ [$label] 是紅了，但不是指名的那一句（預期含「$expect」）  ($tree_label)"
        grep -E '^    ✗|^    →' <<<"$out" | sed 's/^/      /' || true
        fail=$((fail + 1))
    fi
}

for row in "${MUTATIONS[@]}"; do
    run_row "$row" "$TREE" "修補樹"
done

if [[ -n "$PRISTINE" ]]; then
    for row in "${PRISTINE_MUTATIONS[@]}"; do
        run_row "$row" "$PRISTINE" "原始樹"
    done
else
    echo "⚠️  未提供 --pristine —— 跳過 ${#PRISTINE_MUTATIONS[@]} 條（檢查 B 的第二條路徑）。"
    echo "    **那條路徑這一輪沒有被突變測到。** 建原始樹的作法見 D-076 或"
    echo "    docs/ENDPOINT-VERIFIER-ROT.md：把安裝樹複製出來，用 patch -R -p1 --fuzz=0"
    echo "    照 D-073→第四刀→C→B→stop→A→ntfy 的逆序反套，終點是四個具名雜湊。"
fi

echo
if (( fail > 0 )); then
    echo "✗ $fail 條沒過、$pass 條過 —— 驗證器有洞或突變清單有誤。"
    exit 1
fi

echo "✓ ${#MUTATIONS[@]} 條全過：每條破壞都被指名的那一句抓到，守門的都沒叫。"
if [[ -n "$PRISTINE" ]]; then
    echo "  外加 ${#PRISTINE_MUTATIONS[@]} 條走檢查 B 第二條路徑（原始樹）。"
fi
