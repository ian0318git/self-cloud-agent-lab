#!/usr/bin/env bash
# 突變測試：把 chroma_dims_probe.py 的評分器**故意弄壞**，確認
# test_chroma_dims_probe.py 真的會叫。
#
# 為什麼需要這一步（D-024 第八節，同樣的理由再來一次）：一組「永遠通過」的
# 測試和「永遠失敗」的測試一樣沒用。把受測程式碼修好、測試跟著變綠，這件事
# 本身不證明測試有在檢查任何東西。**唯一能證明測試有效的方法，是讓它面對
# 一個已知的錯誤，然後看它有沒有叫。**
#
# 上一次跑這一步時抓到了一個真缺口（加總比對改成子字串時測試不會叫）。
# 這支腳本存在的目的就是再抓一次。
#
# 做法：把探針複製到暫存目錄、用字串取代植入突變、從那份副本跑測試。
# 原始檔從頭到尾不被修改。每個突變都是「把一道判準換成 `if False`」或
# 「換成一個看起來差不多、語意不同的寫法」。
#
# 用法：bash scripts/test_chroma_dims_probe_mutants.sh
#
# 結束碼：0 = 每個突變都被抓到，且對照組通過
#         1 = 有突變沒被抓到（測試有洞）、或對照組失敗（測試壞了）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/chroma_dims_probe.py"
TEST="$SCRIPT_DIR/test_chroma_dims_probe.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 對照組先跑：不動任何東西，測試必須通過 ──────────────
# 沒有這一步，下面「每個突變都被抓到」可能只是因為測試**永遠失敗**。
info "對照組（無突變）"
if ! python3 "$TEST" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_chroma_dims_probe.py 本身有問題，先修它"
  exit 1
fi
ok "對照組通過（測試不是永遠失敗）"
echo

# 突變清單：label|原字串|取代字串
# 每一條都對應一個判準或一個正規化步驟。原字串必須在檔案裡唯一出現一次。
MUTANTS=(
  # ── C1 ──
  "C1 廠商端維度|if emb.get(\"vendor_dims\") != expected_dim:|if False:"
  "C1 經 mem0 的向量長度|if emb.get(\"vector_len\") != expected_dim:|if False:"
  # ── C2 ──
  "C2 chroma 設定出現維度欄位|if cfg.get(\"dim_fields\"):|if False:"
  # ── C3 ──
  "C3 宣告值變成 1536|if declared == README_CLAIMED_DIM:|if False:"
  "C3 讀不到宣告值|if declared is None:|if False:"
  "C3 宣告值等於實際長度（分不出有沒有被截斷）|elif emb.get(\"vector_len\") is not None and emb.get(\"vector_len\") != declared:|elif True:"
  # ── C4 ──
  "C4 寫入前就有維度|if col.get(\"dim_before_write\") is not None:|if False:"
  "C4 寫入後維度|if col.get(\"dim_after_write\") != expected_dim:|if False:"
  "C4 沒丟例外|if not mismatch.get(\"raised\"):|if False:"
  "C4 例外形狀認不出來|elif not dimension_error_recognised(|elif False and not dimension_error_recognised("
  # ── C5 ──
  "C5 量不到替代模型|if alt.get(\"vendor_dims\") is None:|if False:"
  "C5 兩模型維度不同|elif alt.get(\"vendor_dims\") != emb.get(\"vendor_dims\"):|elif False:"
  # ── C6 ──
  "C6 cosine 算不出來|if cross_same is None or within_diff is None:|if False:"
  "C6 序關係|elif not cross_same < within_diff:|elif False:"
  "C6 序關係邊界 < 寫成 <=|elif not cross_same < within_diff:|elif not cross_same <= within_diff:"
  # ── C7 ──
  "C7 metadata 存活|if not guard.get(\"metadata_survives\"):|if False:"
  "C7 mem0 沿用集合|if not guard.get(\"mem0_adopts\"):|if False:"
  "C7 metadata 蓋不掉|if not guard.get(\"overwrite_rejected\"):|if False:"
  "C7 模型不符時會叫|if not guard.get(\"fires\"):|if False:"
  # ── 純函式 ──
  "維度錯誤訊息改用子字串比對|return DIM_MISMATCH_PHRASE in str(message).lower()|return \"dimension\" in str(message).lower()"
  "維度錯誤不檢查模組|if \"chromadb\" not in str(error_module or \"\"):|if False:"
  "cosine 不比對長度|if not a or not b or len(a) != len(b):|if not a or not b:"
  "守門不比對模型名|if recorded_model != expected_model:|if False:"
  "守門不比對記錄的維度|if recorded_dim is not None and recorded_dim != expected_dim:|if False:"
)

caught=0
missed=()

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  cp "$PROBE" "$WORK/chroma_dims_probe.py"
  cp "$TEST" "$WORK/test_chroma_dims_probe.py"

  if ! python3 - "$WORK/chroma_dims_probe.py" "$old" "$new" <<'PY'
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

  if python3 "$WORK/test_chroma_dims_probe.py" >/dev/null 2>&1; then
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
