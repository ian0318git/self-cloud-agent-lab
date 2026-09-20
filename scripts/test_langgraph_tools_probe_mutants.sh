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

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  cp "$PROBE" "$WORK/langgraph_tools_probe.py"
  cp "$TEST" "$WORK/test_langgraph_tools_probe.py"

  if ! python3 - "$WORK/langgraph_tools_probe.py" "$old" "$new" <<'PY'
import io, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
if src.count(old) != 1:
    sys.exit(1)  # 找不到，或不是唯一 —— 兩種都會讓突變失去意義
io.open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
PY
  then
    fail "植入失敗：$label —— 突變目標在 $PROBE 裡不是唯一字串"
    fail "  這代表原始碼改了，這條突變已經對不上（要更新這支腳本，不是更新探針）"
    missed+=("$label（植入失敗）")
    continue
  fi

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
