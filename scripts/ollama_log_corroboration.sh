#!/usr/bin/env bash
# 讀 ollama **伺服器端**的日誌，找出「這一輪」的截斷紀錄。
#
# 為什麼獨立成一個檔案：這是 item 3 唯一不經過探針的證據來源，而它讀的
# 東西（`docker logs`）在這台機器上有兩個**無聲的**坑，兩個都實測過：
#
#   1. `--since` 不能用。三種寫法裡兩種是無聲的錯：
#      · `--since 4m`、`--since 2026-09-20T09:39:00Z` → 這個 docker 版本解析
#        不了，**靜默回 0 行**（不報錯）。於是「日誌裡沒有截斷紀錄」變成
#        一句假話，然後照著這句假話去查一個不存在的矛盾。
#      · `--since <不帶時區的時間戳>` → 被當成**本地時間**解析，而日誌內容
#        是 UTC。host 在 AEST（UTC+10）時窗口被平移十小時，把**十小時前**
#        的截斷紀錄算成這一輪的旁證 —— 那是假證據，比沒有證據糟。
#
#   2. **日誌檔中段有一處損壞**（實測落在 08:12:36 附近）。往回讀超過某個
#      距離就會失敗，而失敗時 docker **不報錯**：它退回「從檔頭往前讀」並停
#      在損壞處。症狀是 `docker logs | wc -l` 凍結在 41992 不再增加，`| tail -1`
#      卻拿得到最新的行 —— 同一份輸出裡有兩個不同的時間點。（「行數差值」正是
#      踩到這個：基準是凍結的。）
#      實測這個距離：`--tail 6000`／`6500` 正常（尾端是最新的行），
#      `--tail 7000` 已經讀到容器啟動的殘骸，`--tail 7500` 明確退回檔頭。
#      所以預設取 6000，並留了下面的涵蓋範圍檢查當安全網。
#
#      2026-09-20 又踩到一次，而且形狀更難看：`--tail 20000` 回了 9286 行、
#      **最後一行停在 08:12**（當時已經 12:40），同一時間 `--tail 5000` 的
#      尾端是正確的。所以症狀不只是「退回檔頭」，而是**回一個看起來很合理
#      的舊窗口** —— 行數夠多、開頭也很久以前，不核對尾端時間根本看不出來
#      （那次就是照著它去查一個不存在的「日誌空白」，查了半小時）。
#      下面那個 pre_anchor 涵蓋檢查是唯一擋得住這種窗口的東西。
#
# 所以：用 `--tail N`（N 留在讀得回來的範圍內）＋**驗證涵蓋範圍**＋自己比對
# 時間戳（UTC 對 UTC，不靠 docker 解析）。涵蓋範圍不成立時要大聲說「旁證
# 不可用」，**不能**印一句「沒有截斷紀錄」了事 —— 那正是這裡要防的假話。
#
# 使用者要把 `docker` 放在 PATH 上；測試會用假的 `docker` 覆蓋它，
# 見 scripts/test_ollama_log_corroboration.sh。
#
# ollama_log_corroboration <開跑前最後一行> <開跑UTC時間> [尾巴行數]
#
# 回傳：0 = 有涵蓋且找到這一輪的截斷（已列出）
#       1 = 有涵蓋、這一輪沒有截斷紀錄
#       2 = 涵蓋不到 → **旁證不可用**（不是「沒有截斷」）
#       3 = 日誌格式變了（有截斷行但找不到 time= 欄位）→ 旁證失效

ollama_log_corroboration() {
  local pre_anchor="$1" run_start="$2" tail_n="${3:-6000}"
  local window n_all n_ts run_trunc

  window="$(docker logs --tail "$tail_n" ollama 2>&1 || true)"

  # 涵蓋範圍的證明。少了這道，下面「沒有截斷紀錄」可能只是因為視窗
  # 根本沒蓋到這一輪 —— 而那個結論會**正好相反**。
  if [[ -z "$pre_anchor" || -z "$window" ]] \
     || ! printf '%s\n' "$window" | grep -qF -- "$pre_anchor"; then
    warn "旁證不可用 —— 讀到的日誌區段涵蓋不到開跑的時間點（見這支腳本開頭的兩個坑）。"
    warn "  讀到的最後一行：$(printf '%s\n' "$window" | tail -1 | cut -c1-60)"
    warn "  開跑前最後一行：$(printf '%s' "$pre_anchor" | cut -c1-60)"
    warn "  → 這一輪有沒有截斷**只看探針的量測**，不要從日誌推論。"
    return 2
  fi

  printf '  視窗涵蓋：%s ～ %s\n' \
    "$(printf '%s\n' "$window" | head -1 | cut -c1-26)" \
    "$(printf '%s\n' "$window" | tail -1 | cut -c1-26)"

  n_all="$(printf '%s\n' "$window" | grep -c 'truncating input prompt' || true)"
  if [[ "$n_all" -eq 0 ]]; then
    warn "這一輪的 ollama 日誌裡沒有截斷紀錄 —— 若 C2 說有截斷，這兩者矛盾，要查清楚"
    return 1
  fi

  # 有截斷行、卻一行都沒有 `time=` 欄位 → 日誌格式變了。此時「這一輪沒有
  # 截斷」是錯的（那些行可能就在這一輪裡）。錯的方向是少報證據，比假證據
  # 好一點，但仍然不可以是無聲的。
  n_ts="$(printf '%s\n' "$window" | grep -cE '(^| )time=[0-9]{4}-' || true)"
  if [[ "$n_ts" -eq 0 ]]; then
    warn "旁證失效 —— 日誌裡有 $n_all 行截斷紀錄，但一行都找不到 time= 欄位（格式變了）。"
    warn "  → 不要把「時間戳篩不掉」讀成「這一輪沒有截斷」。"
    return 3
  fi

  # 時間戳是 UTC（容器 TZ 實測為 UTC），與 `date -u` 同格式，所以字串比較
  # 就是時間比較。ts 取前 19 字（到秒），去掉小數與 Z。
  run_trunc="$(printf '%s\n' "$window" | awk -v t="$run_start" '
    /truncating input prompt/ {
      ts = ""
      for (i = 1; i <= NF; i++) if ($i ~ /^time=/) { ts = substr($i, 6, 19); break }
      if (ts >= t) print
    }')"

  if [[ -n "$run_trunc" ]]; then
    printf '%s\n' "$run_trunc" | sed 's/^/  /'
    ok "ollama 自己記了 $(printf '%s\n' "$run_trunc" | wc -l) 次截斷 —— 與判準 C2 互相印證"
    return 0
  fi

  warn "這一輪的 ollama 日誌裡沒有截斷紀錄 —— 若 C2 說有截斷，這兩者矛盾，要查清楚"
  return 1
}
