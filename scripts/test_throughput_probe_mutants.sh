#!/usr/bin/env bash
# 突變測試：把吞吐量探針的判準**故意弄壞**，確認離線測試會叫。
#
# 為什麼需要（D-024 第八節、D-026 第五節）：一組「永遠通過」的測試和
# 「永遠失敗」的測試一樣沒用。唯一能證明測試有效的方法，是讓它面對一個
# 已知的錯誤，然後看它有沒有叫。
#
# 這份清單裡每一條守的東西，以及**為什麼它值得一條突變**：
#
#   `cap_respected_verdict` —— **整份清單裡最重要的一條。** 開發這支探針時，
#     我自己的臨時腳本把 `num_predict` 放在請求頂層，ollama 靜默忽略它，
#     生成於是没有上限、跑了 2,876 個 token。那種失敗不報錯，只會一直跑。
#     這一條判準就是為了讓它不可能再發生 —— 弄壞它，所有速率都會變成
#     在「上限其實沒生效」的條件下量的，而沒人看得出來。
#
#   `rate` 的「量不到」—— 回 0.0 會被讀成「很慢」，回 inf 會被讀成「很快」，
#     兩者都是把「沒有資料」講成「有一個值」。
#
#   `sample_usable` 的冷樣本 —— 重新載入後的第一個樣本量到 5.65 t/s，
#     同組其餘是 6.20／6.82。放它進去就是讓一個不同條件的樣本冒充同組。
#
#   `summarize` 的分母 —— 離散度除以**中位數**。改成除以最大值會讓
#     每一組看起來都更穩，門檻 15% 就不再是 15%。
#
#   `baseline_verdict` 的 D-014 —— 只有一個條件時回 `single_condition`。
#     這一條被弄壞，就等於把「單一條件下的結果不外推」這條規則取消，
#     而那正是這次重打基準線的理由。
#
#   `truncation_limit` 的公式 —— `num_ctx − max((num_ctx − num_keep)/2, 1)`，
#     而且**只在 prompt 塞不下時才套用**。這個公式錯了兩次，兩次都是靠
#     離線測試有洞才活下來的：先是「預算 = ctx − num_predict」（它的期望值
#     2,096 就是它自己算出來的），後是化簡式 `num_ctx/2 + 2`（專案裡每一個
#     觀測的 num_keep 都剛好是 4，所以沒有任何觀測會抗議它）。
#
#   `reservation_verdict` —— 整份探針裡唯一能**否證**那個公式的地方。
#     它拿觀測到的平台去對公式：兩者不合、或平台會隨 num_predict 移動，
#     都必須讓這一輪失敗。上一版在三條出路裡有一條回 `independent`，而
#     `_all_ok` 接受 `independent` —— 也就是說，這一臂真的偵測到上游變更
#     的時候，這一輪照樣回 0。
#
#   `_all_ok` 的截斷閘門 —— 原本讀 `out["truncation"]`，一個**從來沒有人
#     設過**的鍵（截斷是存在條件裡的 `long_cond["truncation"]`），所以判定
#     永遠不成立。它同時寫成封鎖清單，漏掉一個 verdict 值就 fail-open。
#
#   `kv_slope_verdict` 的「ctx 變大、記憶體沒變大」—— 那代表這支儀器量的
#     不是 KV cache（可能是別的欄位），此時**不可以**給出 bytes/token。
#
#   `cpu_topology_verdict` —— 本機的 4 個 vCPU 被報成 4 個獨立 socket。
#     這是「2 vCPU 換到 4 vCPU 竟然沒變快」的候選解釋，不能弄丟。
#
#   `parse_kv_buffer_bytes` —— 抓不到回 None，不回 0。回 0 會被讀成
#     「KV cache 是 0」，而那是**一個假的事實**（日誌格式變了就是這個下場）。
#
# 做法：把模組與測試複製到暫存目錄、用字串取代植入突變，從副本跑測試。
# 原始檔從頭到尾不被修改。
#
# 用法：bash scripts/test_throughput_probe_mutants.sh
#
# 結束碼：0 = 每個突變都被抓到，且對照組通過
#         1 = 有突變沒被抓到（測試有洞）、或對照組失敗（測試壞了）
#         3 = 找不到受測檔案

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/throughput_probe.py"
PROBE_TEST="$SCRIPT_DIR/test_throughput_probe.py"

for f in "$PROBE" "$PROBE_TEST"; do
  if [[ ! -r "$f" ]]; then
    echo "[3] 找不到受測檔案：$f" >&2
    exit 3
  fi
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ok()   { printf '  ✓ %s\n' "$*"; }
info() { printf '\n── %s ──\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*"; }

restore() {
  cp "$PROBE"      "$WORK/throughput_probe.py"
  cp "$PROBE_TEST" "$WORK/test_throughput_probe.py"
}

cat > "$WORK/inject.py" <<'PY'
import io, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
n = src.count(old)
if n != 1:
    # 「找不到」與「不唯一」要分開講。合併成一句會誤導：目標字串裡多寫了
    # 一個 `|` 時欄位會被切錯，症狀是**找不到**，但訊息若說「不唯一」，
    # 就會去翻原始碼找重複，而該改的是清單那一行。
    print("NOT_FOUND" if n == 0 else "NOT_UNIQUE:%d" % n, file=sys.stderr)
    sys.exit(1)
io.open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
PY

echo "======================================================================"
echo "吞吐量探針突變測試"

# ── 對照組先跑：不動任何東西，測試必須通過 ──────────────
# 沒有這一步，下面「每個突變都被抓到」可能只是因為測試**永遠失敗**。
restore
info "對照組（無突變）"
if ! python3 "$WORK/test_throughput_probe.py" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_throughput_probe.py 本身有問題，先修它"
  exit 1
fi
ok "對照組通過（測試不是永遠失敗）"

# 突變清單：label|原字串|取代字串
# 原字串必須在 throughput_probe.py 裡**唯一**出現一次，而且**不含 `|`**。
MUTANTS=(
  # ══════════════════════════════════════════════════════════
  # 上限有沒有生效 —— 本探針存在的理由
  # ══════════════════════════════════════════════════════════
  # 超過上限不再判 ignored → 開發時燒掉十幾分鐘的那個 bug 回來
  # 這兩條的目標現在**橫跨三行**，因為 `if eval_count > num_predict:` 在
  # `long_generation_verdict` 裡也出現了一次 —— 兩行的目標會 NOT_UNIQUE，
  # 而突變台正確地拒絕猜（植入失敗 > 改錯地方）。第三行的 `"ignored"` 與
  # 那裡的 `"void"` 才是區辨點。bash 的雙引號不展開 `\n`，所以用 $'...'。
  $'上限被忽略不再判 ignored|    if eval_count > num_predict:\n        return {\n            "verdict": "ignored",|    if False:\n        return {\n            "verdict": "ignored",'
  # 只超過 1 個 token 不算 → 上限「差不多有生效」被當成有生效
  $'超過上限的邊界放寬 1 個 token|    if eval_count > num_predict:\n        return {\n            "verdict": "ignored",|    if eval_count > num_predict + 1:\n        return {\n            "verdict": "ignored",'
  # length 與數量對不起來時硬判 respected → fail-open
  "length 但數量不符仍判 respected|    if done_reason == \"length\" and eval_count != num_predict:|    if False:"
  # 訊息不再點出 options → 下一個人不知道要改哪裡
  "ignored 的訊息不再點出 options|                f\"必須放在 options 裡）。這一輪的速率不可信。\"|                f\"這一輪的速率不可信。\""

  # ══════════════════════════════════════════════════════════
  # rate：量不到不可以被講成一個值
  # ══════════════════════════════════════════════════════════
  # 量不到回 0.0 → 被讀成「很慢」，而那是編造出來的事實。
  # （拿掉 count<=0 的守衛，函式就會落到最後一行算 0/duration = 0.0）
  "rate 量不到時回 0.0 而不是 None|    if count <= 0 or duration_ns <= 0:|    if duration_ns <= 0:"
  # 時長 0 不再擋 → ZeroDivisionError，或更糟：被呼叫端吞掉
  "rate 不擋 duration=0|    if count <= 0 or duration_ns <= 0:|    if count <= 0:"
  # 負時長放行 → 負速率會拉低中位數
  "rate 放行負時長|    if count <= 0 or duration_ns <= 0:|    if count <= 0 or duration_ns < 0:"

  # ══════════════════════════════════════════════════════════
  # 冷樣本與穩定度
  # ══════════════════════════════════════════════════════════
  # 冷樣本放行 → 一個不同條件的樣本冒充同組成員
  "冷樣本被當成可用樣本|    if load > COLD_LOAD_THRESHOLD_NS:|    if False:"
  # 門檻降回 0 → **這就是原本的 bug**：ollama 對每個請求都回非零的
  # load_duration（1.1～4.3 毫秒），所以整份矩陣會被判成全冷、全部排除，
  # 症狀是「沒有任何條件量到速率」。一個永遠說「不」的判準。
  "冷樣本門檻降回 0（原本的 bug）|COLD_LOAD_THRESHOLD_NS = 100_000_000|COLD_LOAD_THRESHOLD_NS = 0"
  # 門檻高到不可能達到 → 真正的重新載入也放行，一個 5.65 t/s 的樣本混進
  # 6.20／6.82 那一組。這是反方向：永遠說「是」。
  "冷樣本門檻高到擋不住真的載入|COLD_LOAD_THRESHOLD_NS = 100_000_000|COLD_LOAD_THRESHOLD_NS = 10**18"
  # 讀不到 load_duration 也放行 → 不知道冷不冷就敢用
  "load_duration 讀不到仍算可用|        return {\"usable\": False, \"reason\": \"unreadable\", \"message\": \"load_duration 讀不到，無法判斷這是不是冷樣本。\"}|        return {\"usable\": True, \"reason\": \"ok\", \"message\": \"load_duration 讀不到，當成可用。\"}"
  # 離散度的分母改成最大值 → 每一組看起來都更穩
  "離散度改用最大值當分母|    spread = (max(clean) - min(clean)) / med * 100.0 if len(clean) > 1 else 0.0|    spread = (max(clean) - min(clean)) / max(clean) * 100.0 if len(clean) > 1 else 0.0"
  # 門檻 15% 改成 20% → 一個明顯不穩的條件被講成穩定
  "穩定門檻放寬到 20%|STABILITY_TOLERANCE_PCT = 15.0|STABILITY_TOLERANCE_PCT = 20.0"
  # 空集合回 median=0 → 「沒有資料」被講成「速率 0」
  "空集合的中位數回 0.0|        return {\"n\": 0, \"min\": None, \"median\": None, \"max\": None, \"spread_pct\": None, \"stability\": \"unknown\"}|        return {\"n\": 0, \"min\": None, \"median\": 0.0, \"max\": None, \"spread_pct\": None, \"stability\": \"unknown\"}"

  # ══════════════════════════════════════════════════════════
  # D-014：一個條件不是基準線
  # ══════════════════════════════════════════════════════════
  # 單一條件也放行 → 這次重打基準線的理由被取消
  "單一條件被判成可用基準線|    if len(usable) < min_conditions:|    if False:"
  # 門檻預設從 2 降到 1 → 同上，但走另一條路
  "基準線門檻預設降到 1|def baseline_verdict(conditions: list[dict[str, Any]], min_conditions: int = 2) -> dict[str, Any]:|def baseline_verdict(conditions: list[dict[str, Any]], min_conditions: int = 1) -> dict[str, Any]:"
  # noisy 不再擋 → 不穩的數字照樣被當基準線
  "noisy 條件不再擋下基準線判定|    if noisy:|    if False:"
  # 速率 0 也算一個條件 → 用「量不到」湊到條件數
  "速率 0 也算一個已量測條件|    usable = [c for c in conditions if _is_num(c.get(\"rate\")) and c[\"rate\"] > 0]|    usable = [c for c in conditions if _is_num(c.get(\"rate\"))]"

  # ══════════════════════════════════════════════════════════
  # 截斷規則：切完剩多長、什麼時候才切
  # ══════════════════════════════════════════════════════════
  # 上限改回 num_ctx → 被切掉的 prompt（2050/4096）被判成完整
  "上限改回 num_ctx（被切的 prompt 會漏掉）|    return max(num_ctx - room, 0)|    return max(num_ctx, 0)"
  # num_keep 寫死 4 → 實作了化簡式而不是完整公式。**專案裡每一個觀測的
  # num_keep 都剛好是 4**，所以只有刻意換一個 num_keep 的測試殺得掉它 ——
  # 這一條就是「為什麼期望值不能從公式反推」的突變版。
  "num_keep 寫死 4（化簡式冒充完整公式）|    room = max((num_ctx - num_keep) // 2, 1)|    room = max((num_ctx - 4) // 2, 1)"
  # 塞得下卻剛好等於上限時不再判被切 → 保留臂的真實資料（4098 @ ctx 8192）
  # 會落到下面的「塞得下」而出門，一個被切掉的 prompt 被講成完整。
  $'等於上限不再判被切（被切的 prompt 變完整）|    if prompt_eval_count == limit:\n        return {\n            "verdict": "truncated",\n            "limit": limit,\n            "message": (\n                f"prompt_eval_count={prompt_eval_count} 正好是被切之後的長度 {limit}"|    if False:\n        return {\n            "verdict": "truncated",'
  # 邊界寫成「不大於」→ 剛好填滿 context 被放行成完整。那個輸入依
  # D-027 第三節的夾縫（687 → 346）一定被切，所以看到它就代表規則變了，
  # 放行等於把「上游變了」講成「prompt 完整」。
  "剛好填滿 context 被放行成完整|    if prompt_eval_count < num_ctx:|    if prompt_eval_count <= num_ctx:"
  # 溢出臂「沒撞到上限」不再判 unknown → 把「這次沒量到」講成結論
  "溢出臂沒撞到上限仍給結論|        if prompt_eval_count < limit:|        if False:"
  # num_keep 讀不到也照算 → 用一個猜來的值決定 prompt 有沒有被切
  "num_keep 讀不到仍照算|    if not _is_int(prompt_eval_count) or not _is_int(num_ctx) or not _is_int(num_keep):|    if not _is_int(prompt_eval_count) or not _is_int(num_ctx):"
  # 算不出正的上限時硬算 → 一個負數或 0 的「上限」拿去比對
  "算不出正上限時硬算|    if limit <= 0:|    if False:"

  # ══════════════════════════════════════════════════════════
  # 否證：拿觀測到的平台去對公式（本探針唯一能抓上游變更的地方）
  # ══════════════════════════════════════════════════════════
  # 平台與公式不合仍放行 → 上游改了截斷規則，這一輪照樣被當基準線
  "平台與公式不合仍放行|        if observed != limit:|        if False:"
  # 「prompt 沒撞到上限」被講成「上限與 num_predict 無關」= 把「沒量到」講成結論。
  # 這一條殺的是**循環論證**：舊版拿「count < 預算」當成沒溢出的證據，
  # 但截斷正是把 count 壓到預算以下的原因。
  "沒撞到上限被講成 independent（循環論證）|        if observed < limit:|        if False:"
  # 斜率不檢查 → 「上限不動」也會被講成「上限隨 num_predict 移動」
  "保留規則不檢查斜率|    if all(abs(d + 1.0) <= 0.15 for d in deltas):|    if True:"
  # 反方向：上限真的隨 num_predict 移動（斜率 −1）時永遠判不成立
  "上限隨 num_predict 移動卻判不成立|    if all(abs(d + 1.0) <= 0.15 for d in deltas):|    if False:"
  # 斜率不是 −1（上限會動但不成規律）回 independent → **fail-open**：
  # 這一臂存在的目的正是偵測「上限會動」，而它偵測到時這一輪照樣回 0。
  "上限會動卻回報 independent（fail-open）|        \"verdict\": \"moves_with_num_predict\",|        \"verdict\": \"independent\","

  # ══════════════════════════════════════════════════════════
  # _all_ok 的閘門：這一輪到底能不能當基準線
  # ══════════════════════════════════════════════════════════
  # 讀一個不存在的鍵 → 長 prompt 臂真的被截斷時，這一輪照樣回 0
  "截斷閘門讀不存在的鍵（原本的缺陷）|        tr = c.get(\"truncation\")|        tr = out.get(\"truncation\")"
  # 允許清單改回封鎖清單 → `unexpected`（上游變了）被放行
  "截斷閘門寫成封鎖清單|        if tr is not None and tr.get(\"verdict\") != \"intact\":|        if tr is not None and tr.get(\"verdict\") == \"truncated\":"
  # 保留臂的允許清單改回封鎖 → `limit_mismatch` 與 `moves_with_num_predict` 放行
  "保留臂閘門寫成封鎖清單|    if (out.get(\"reservation\") or {}).get(\"verdict\") != \"independent\":|    if (out.get(\"reservation\") or {}).get(\"verdict\") == \"reserves_output\":"

  # ══════════════════════════════════════════════════════════
  # KV cache 與日誌解析
  # ══════════════════════════════════════════════════════════
  # 斜率不再講明含非 KV 項 → 讀者把一個**上界**當成 KV 成本做容量規劃
  "斜率不再講明只能當上界|            f\"所以這只能當 KV 成本的**上界**\"|            f\"\""
  # ctx 變大、記憶體沒變大時仍然給出 bytes/token → 給一個假的 KV 成本
  "斜率 ≤0 仍給出 bytes/token|    if slope <= 0:|    if False:"
  # 少於兩點也硬算 → 單點求斜率
  "少於兩點仍硬算斜率|    if len(pts) < 2:|    if False:"
  # 反序輸入沒排序 → 斜率正負號錯，KV 成本變成負的
  "取樣點不排序（反序輸入會算錯）|    pts = sorted(pts, key=lambda p: p[\"context_length\"])|    pts = pts"
  # 日誌抓不到時不再擋 → 「格式變了」變成當掉或拿到舊值
  "KV 日誌抓不到時不再擋|    if not hits:|    if False:"
  # 單位表接反 → KV 成本差 1024² 倍，而那個數字看起來完全正常
  "MiB 與 GiB 的換算接反|\"MiB\": 1024**2, \"GiB\": 1024**3|\"MiB\": 1024**3, \"GiB\": 1024**2"
  # 多筆時取第一筆 → 拿到的是第一次載入的舊值
  "KV 多筆取第一筆而不是最後一筆|    value, unit = hits[-1]  # 取最後一筆：最後一次載入的|    value, unit = hits[0]"
  # 取樣點不按模型分組 → `size` 的差量到的是**模型之間的權重差**，不是 KV。
  # 這一條是本輪實際踩到的：兩個模型混在一起排序，斜率算出 -188439 B/token。
  # 危險的不是那個負號（負的很顯眼），而是換一組權重接近的模型時，同一個
  # 缺陷會吐出一個**看起來完全合理的正數**。
  "KV 取樣點不按模型分組（混合多模型）|groups.setdefault(str(p.get(\"model\") or \"（未標示模型）\"), []).append(p)|groups.setdefault(\"all\", []).append(p)"
  # 只有一個模型時也把數字拿掉 → 分組等於把整個功能關掉
  "KV 只有一個模型時不給單一數字|    if len(per_model) == 1:|    if False:"
  # 多模型時自己發明一個單一數字 → 平均或總和都是不存在的量
  $'KV 多模型時發明一個單一數字|"verdict": verdict,\n        "bytes_per_token": None,|"verdict": verdict,\n        "bytes_per_token": 12345,'
  # 量不到的模型從訊息裡消失 → 用沉默把一個模型刪掉
  "KV 量不到的模型從訊息裡消失|            parts.append(f\"{m}：{v['message']}\")|            parts.append(\"\")"
  # 單一模型的判定被壓成 unknown → 「這個儀器量的不是 KV」與「點不夠」
  # 被講成同一件事，而這兩句話要採取的行動完全不同
  "KV 單一模型的 anomalous 被壓成 unknown|{**only, \"per_model\": per_model,|{**only, \"verdict\": \"unknown\", \"per_model\": per_model,"

  # ══════════════════════════════════════════════════════════
  # 失敗的分類與中斷 —— 第二次完整量測買到的
  # ══════════════════════════════════════════════════════════
  # timeout 被回報成「準則不成立」（1）→ 那個假事實回來。
  # 目標**必須橫跨兩行**：Python 的字典字面值允許重複的鍵，而**後者勝出**，
  # 所以把 `"exit": EXIT_FAIL` 塞在真正的 exit 之前是沒有效果的（我第一版
  # 就是這樣寫的，突變因此「漏掉」—— 但漏掉的不是測試，是突變本身）。
  # 單行寫不出來是因為 bash 的雙引號不展開 `\n`，所以用 $'...' 的 ANSI-C 引法。
  $'timeout 被回報成準則不成立（1）|"kind": "timeout",\n            "exit": EXIT_UNMEASURED,|"kind": "timeout",\n            "exit": EXIT_FAIL,'
  # HTTPError 不再優先於 URLError → 「ollama 回了 500」被講成「連不上」
  "HTTPError 不再優先於 URLError 判定|    if isinstance(exc, urllib.error.HTTPError):|    if False:"
  # URLError 包住的 timeout 不再拆出來 → 兩種要去做不同事的情況混成一句
  "URLError 包住的 timeout 被講成連不上|not isinstance(getattr(exc, \"reason\", None), TimeoutError)|True"
  # 探針自己壞掉被降級成「量不到」→ 下一個人不會去讀 traceback
  "探針自己壞了被降級成量不到|\"exit\": EXIT_BROKEN,|\"exit\": EXIT_UNMEASURED,"
  # 中斷過的矩陣仍被判成基準線 → 殘缺的矩陣可以下結論（D-014 失效）
  "中斷過的矩陣仍被判成基準線|    if out.get(\"measurement_error\"):|    if False:"
  # ══════════════════════════════════════════════════════════
  # 長生成對照組 —— num_predict=1024 不代表產出了 1024 個 token
  # ══════════════════════════════════════════════════════════
  # 提早講完也算對照 → 一個標著 1024、實際 130 的樣本會被寫成 D-014 的對照
  "長生成提早講完也算對照|    if eval_count < num_predict:|    if False:"
  # 超過上限（上限被忽略）也算對照 → 上限失效的那一輪被當成正常
  $'長生成超過上限也算對照|    if eval_count > num_predict:\n        return {\n            "verdict": "void",|    if False:\n        return {\n            "verdict": "void",'
  # 只要有一個樣本好就整組算過 → 不同生成長度的樣本被平均在一起
  "長生成有一個樣本壞掉仍算整組過|    bad = [v for v in verdicts if v[\"verdict\"] != \"matched\"]|    bad = []"
  # 沒有可用樣本時當成 matched → 「沒量到」被講成「量到了」
  "長生成沒有樣本時當成 matched|    if not verdicts:|    if False:"
  # _all_ok 不再看長生成 → 提早停的那一輪照樣被當成基準線
  $'基準線的守衛不看長生成|        lg = c.get("long_generation")\n        if lg is not None and lg.get("verdict") != "matched":\n            return False|        lg = None'
  # 新外殼的 conditions 不是可迭代的空清單 → 第一次 append 就炸
  "新外殼的 conditions 是 None|\"conditions\": [],|\"conditions\": None,"

  # ══════════════════════════════════════════════════════════
  # 機器的形狀
  # ══════════════════════════════════════════════════════════
  # 本機的拓樸被判成一般 → 「4 個 vCPU 是 4 個 socket」這個線索消失
  "單 socket／多 socket 不再區分|    if len(set(physical_ids)) == len(physical_ids) and len(physical_ids) > 1 and all(s == 1 for s in set(siblings) or [1]):|    if False:"
  # 只有一個 vCPU 也判成 one_socket_per_cpu → 講了一句沒有內容的話
  "單一 vCPU 也判成 one_socket_per_cpu|    if len(set(physical_ids)) == len(physical_ids) and len(physical_ids) > 1 and all(s == 1 for s in set(siblings) or [1]):|    if len(set(physical_ids)) == len(physical_ids):"
  # 執行緒數少於 vCPU 不再提 → 「多出來的核沒用」這個解釋消失
  "執行緒數少於 vCPU 不再判 partial|    if n_threads >= nproc:|    if True:"
)

# 先擋重複的 label。**這不是潔癖**：改過一次判準之後，舊突變的目標字串
# 就不存在了，而新突變常常沿用同一個標題 —— 於是清單裡同時有一條舊的
# （植入失敗）和一條新的（正常），輸出看起來像「有一條沒抓到」，
# 但其實是清單有兩條同名。我自己踩過，所以讓它自己講出來。
#
# 取 label 要**直接對陣列元素**做，不要 `printf '%s\n' | cut -d'|' -f1`。
# 目標字串可以橫跨多行（見清單裡 $'...' 的用法），逐行切會把那些行也當成
# 獨立的「label」：我的兩條 KV 突變都含有一行 `"verdict": "ignored",`，
# 於是這個檢查報了兩條**不存在**的重複。反過來說，真正重複的 label 在
# 一堆片段裡也可能被淹掉 —— 一個會誤報又會漏報的檢查。
# `"${m%%|*}"` 在 bash 裡就是「元素中第一個 | 之前的部分」，與下面迴圈
# 取 label 用的是同一個規則，兩者不會再各自解讀一次。
DUPES="$(for m in "${MUTANTS[@]}"; do printf '%s\n' "${m%%|*}"; done | sort | uniq -d)"
if [[ -n "$DUPES" ]]; then
  fail "突變清單有重複的 label："
  printf '  %s\n' $DUPES
  fail "同名的突變會讓「有一條植入失敗」看起來像測試有洞 —— 請改標題或刪掉過期的那條"
  exit 3
fi

CAUGHT=0
MISSED=()

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  restore

  INJECT_MSG="$(python3 "$WORK/inject.py" "$WORK/throughput_probe.py" "$old" "$new" 2>&1 >/dev/null)" && : || {
    case "$INJECT_MSG" in
      NOT_FOUND)
        fail "植入失敗：$label —— 目標字串在 throughput_probe.py 裡**找不到**"
        fail "  兩個常見原因：(1) 原始碼改了，這條突變對不上（要更新這支腳本）" \
             "；(2) 目標字串裡有 \`|\`，欄位被切錯了（目標行必須不含 \`|\`）" ;;
      NOT_UNIQUE:*)
        fail "植入失敗：$label —— 目標字串出現 ${INJECT_MSG#NOT_UNIQUE:} 次（必須唯一）"
        fail "  原始碼改了，這條突變對不上（要更新這支腳本，不是更新原始碼）" ;;
      *)
        fail "植入失敗：$label（$INJECT_MSG）" ;;
    esac
    MISSED+=("$label（植入失敗）")
    continue
  }

  # 測試必須**失敗**才算抓到。
  if python3 "$WORK/test_throughput_probe.py" >/dev/null 2>&1; then mut_rc=0; else mut_rc=1; fi

  if [[ "$mut_rc" -eq 0 ]]; then
    fail "漏掉！$label —— 判準壞成這樣，測試還是通過了"
    MISSED+=("$label")
  else
    ok "$label"
    CAUGHT=$((CAUGHT + 1))
  fi
done

echo
echo "======================================================================"
if (( ${#MISSED[@]} > 0 )); then
  echo "抓到 $CAUGHT/${#MUTANTS[@]}，漏掉 ${#MISSED[@]} 條："
  for m in "${MISSED[@]}"; do echo "  ✗ $m"; done
  echo
  echo "漏掉代表**測試有洞**，不是突變寫錯（除非是「植入失敗」）。"
  exit 1
fi
echo "全部抓到：$CAUGHT/${#MUTANTS[@]} 條突變，每條都讓測試失敗。"
exit 0
