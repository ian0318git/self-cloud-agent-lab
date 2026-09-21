#!/usr/bin/env bash
# 突變測試：把 langgraph_tools_probe.py 的評分器**故意弄壞**，確認
# test_langgraph_tools_probe.py 真的會叫。
#
# 為什麼需要這一步：一組「永遠通過」的測試和「永遠失敗」的測試一樣沒用。
# 把受測程式碼修好、測試跟著變綠，這件事本身不證明測試有在檢查任何東西 ——
# 也可能它根本沒被执行到。**唯一能證明測試有效的方法，是讓它面對一個已知
# 的錯誤，然後看它有沒有叫。**
#
# 這不是形式主義。這支腳本第一次跑就抓到 test_langgraph_tools_probe.py 的
# 一個真缺口（C4 加總比對改成子字串時測試不會叫），補上測資後才全數攔下。
# 也就是說：少了這一步，那份測試會帶著一個洞進到下一個階段。
#
# 做法：把探針複製到暫存目錄、用字串取代植入突變、從那份副本跑測試。
# 原始檔從頭到尾不被修改。每個突變都是「把一道判準換成 `if False`」或
# 「換成一個看起來差不多、語意不同的寫法」。
#
# 用法：bash scripts/test_langgraph_tools_probe_mutants.sh
#
# 結束碼：0 = 每個突變都被抓到，且對照組通過
#         1 = 有突變沒被抓到（測試有洞）、或對照組失敗（測試壞了）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/langgraph_tools_probe.py"
TEST="$SCRIPT_DIR/test_langgraph_tools_probe.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# **不要寫 .pyc。** 突變是用字串取代植入的，而 CPython 對 .pyc 的有效性
# 檢查只比對「原始檔的 mtime（**秒**）與大小」。兩條連續的突變若長度相同
# 又落在同一秒內，第二次寫入就會命中第一次留下的 .pyc —— 那個突變**根本
# 沒有被執行**，測試當然通過，而突變台把它報成「抓到」。
# 實測：`return 1` → `return 2` 這種同長度取代，在同一秒內被完全忽略。
# 一個會謊報「抓到」的突變台是 fail-open —— 比沒有突變台更糟，因為它讓
# 「全部抓到」這句話變成假的。
export PYTHONDONTWRITEBYTECODE=1

# ── 對照組先跑：不動任何東西，測試必須通過 ──────────────
# 沒有這一步，下面「每個突變都被抓到」可能只是因為測試**永遠失敗**。
info "對照組（無突變）"
if ! python3 "$TEST" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_langgraph_tools_probe.py 本身有問題，先修它"
  exit 1
fi
ok "對照組通過（測試不是永遠失敗）"
echo

# 突變清單：label|原字串|取代字串
# 每一條都對應一個判準或一個正規化步驟。原字串必須在檔案裡唯一出現一次。
MUTANTS=(
  "C2 孤兒呼叫（call_id 鏈）|if call[\"call_id\"] not in turn[\"results\"]:|if False:"
  "C2 反向（回傳沒有呼叫）|for call_id in turn[\"results\"]:|for call_id in []:"
  "C1 自主呼叫|if not any(is_roll_call(c) for c in all_calls):|if False:"
  "C3 連續兩次呼叫|if len(last_rolls) < min_rolls_last_turn:|if False:"
  "C4 加總比對改子字串|if expected not in numbers:|if str(expected) not in final:"
  "C4 空回答|if not final:|if False:"
  "工具回傳不可當最終答案|or call_id is not None:|:"
  "回傳值可解析|if value is None:|if False:"
  "工具名稱比對改硬等於|ROLL_DIE in str(name)|name == ROLL_DIE"
  "result_text 正規化清單|if isinstance(content, (list, tuple)):|if False:"
  "roll_values 改成抓第一個數字|out.append(int(stripped) if _INT_RE.fullmatch(stripped) else None)|out.append(int(_INT_RE.search(stripped).group()) if _INT_RE.search(stripped) else None)"
)

caught=0
missed=()

# 每一條都必須**恰好**兩個 `|`（label|old|new）。多寫一個就會被切錯，而且
# 症狀是**靜默**的：`old` 被截短、`new` 從 `|` 開始，植入的是一段壞掉的
# 程式碼 —— 測試當然會失敗，於是它被算進「抓到」，但它守不住任何判準。
# 這一條真的抓到過一條：目標行裡的 `||` 被當成了分隔符。
BADFIELDS=""
for m in "${MUTANTS[@]}"; do
  pipes="${m//[^|]/}"                      # 只留 `|`，數它有幾個
  [[ ${#pipes} -eq 2 ]] || BADFIELDS+="  ${m%%|*}"$'\n'
done
if [[ -n "$BADFIELDS" ]]; then
  fail "突變清單有條目的 \`|\` 不是剛好兩個 —— 欄位會被切錯，植入的是壞掉的程式碼："
  printf '%s' "$BADFIELDS"
  exit 3
fi

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  cp "$PROBE" "$WORK/langgraph_tools_probe.py"
  cp "$TEST" "$WORK/test_langgraph_tools_probe.py"

  INJECT_MSG="$(python3 - "$WORK/langgraph_tools_probe.py" "$old" "$new" 2>&1 >/dev/null <<'PY'
import ast, io, os, subprocess, sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
n = src.count(old)
if n != 1:
    # 「找不到」與「不唯一」要分開講。合併成一句會誤導：目標字串裡多寫了
    # 一個 `|` 時欄位會被切錯，症狀是**找不到**，但訊息若說「不唯一」，
    # 就會去翻原始碼找重複，而該改的是清單那一行。
    print("NOT_FOUND" if n == 0 else "NOT_UNIQUE:%d" % n, file=sys.stderr)
    sys.exit(1)
mutated = src.replace(old, new, 1)

# 植入之後還要再過一關，因為「測試變紅」不等於「有斷言守住」。這一關擋的
# 是**假抓到**，它有兩張臉，而且都不會讓任何一條斷言叫起來：
#   NO_OP       取代沒有改變語意（`if slack < -0:` 就是 `if slack < 0:`）。
#               它殺不掉任何東西，卻在報告裡佔一個「抓到」的名額 —— 更糟
#               的是它讓一份「全部抓到」看起來很完整。`ast.dump` 不比對
#               註解與空白，所以只改註解的那種也會在這裡現形。
#   BROKEN_CODE 植入後根本不是合法程式。測試當然會失敗，但那是模組匯入
#               不了，不是判準被守住。真的混進來過：目標行裡的 `||` 被
#               當成欄位分隔符，以及雙引號裡的字面 `\n`。
# 兩者都只說明**這條突變寫壞了**，原始碼沒有問題。
ext = os.path.splitext(path)[1]
if ext == ".py":
    try:
        before = ast.dump(ast.parse(src))
        after = ast.dump(ast.parse(mutated))
    except SyntaxError as e:
        print("BROKEN_CODE:%s" % e.msg, file=sys.stderr)
        sys.exit(1)
    if before == after:
        print("NO_OP", file=sys.stderr)
        sys.exit(1)
elif ext == ".sh":
    # shell 的等價性在這裡判不了（那要真的比語意），所以只驗語法。
    # 走 stdin 而不是暫存檔：`bash -n` 的訊息就會是乾淨的 `line N`，
    # 不必把一個臨時路徑印進錯誤訊息裡。
    p = subprocess.run(["bash", "-n"], input=mutated,
                       capture_output=True, text=True)
    if p.returncode != 0:
        last = (p.stderr.strip().splitlines() or ["bash -n 失敗"])[-1]
        print("BROKEN_CODE:%s" % last, file=sys.stderr)
        sys.exit(1)

io.open(path, "w", encoding="utf-8").write(mutated)
PY
)" && : || {
    case "$INJECT_MSG" in
      NOT_FOUND)
        fail "植入失敗：$label —— 目標字串在 $PROBE 裡**找不到**"
        fail "  兩個常見原因：(1) 原始碼改了，這條突變對不上（要更新這支腳本）" \
             "；(2) 目標字串裡有 \`|\`，欄位被切錯了（目標行必須不含 \`|\`）" ;;
      NOT_UNIQUE:*)
        fail "植入失敗：$label —— 目標字串在 $PROBE 裡出現 ${INJECT_MSG#NOT_UNIQUE:} 次"
        fail "  原始碼改了，這條突變對不上（要更新這支腳本，不是更新探針）" ;;
      NO_OP)
        fail "植入失敗：$label —— 取代**沒有改變語意**，這條突變殺不掉任何斷言"
        fail "  這不是「很弱的突變」，是沒有突變。掃描會把它算進「抓到」，但它是空的" ;;
      BROKEN_CODE:*)
        fail "植入失敗：$label —— 植入之後**根本不是合法程式**（${INJECT_MSG#BROKEN_CODE:}）"
        fail "  測試會變紅，但那是跑不起來，不是判準守住了。原始碼沒問題，是這條突變寫壞了" ;;
      *)
        fail "植入失敗：$label（$INJECT_MSG）" ;;
    esac
    missed+=("$label（植入失敗）")
    continue
  }

  if python3 "$WORK/test_langgraph_tools_probe.py" >/dev/null 2>&1; then
    fail "漏掉！$label —— 評分器壞成這樣，測試還是通過了"
    missed+=("$label")
  else
    ok "抓到  $label"
    caught=$((caught + 1))
  fi
done

echo
total="${#MUTANTS[@]}"
if [[ "${#missed[@]}" -gt 0 ]]; then
  fail "有 ${#missed[@]} 個突變沒被抓到，共 $total 個 —— 測試有洞："
  for m in "${missed[@]}"; do fail "  • $m"; done
  exit 1
fi

ok "$caught/$total 個突變全數被抓到，且對照組通過 —— 評分邏輯的每一道判準都有測試守著"
