#!/usr/bin/env bash
# Compose profile 的生命週期判斷。**只有純函式，不做任何 I/O。**
#
# 為什麼要有這支：`--remove-orphans` 一直被當成「停用 profile 之後的清理機制」，
# 而它**不是**（實測，見 DECISIONS.md D-029）。被 profile 停用的服務在 compose
# 的定義下仍然「存在於 compose 檔裡」，所以它不是 orphan —— 加了旗標也一樣
# 不會被動到。實際後果是 fail-open：cloudflared 還連著 Cloudflare 邊緣、
# 主機名還在服務，而 `status.sh` 說「未啟用」、`check-exposure.sh` 看不到它。
# 使用者因此得到「我已經把 tunnel 關掉了」這個**錯誤的認知**。
#
# 判斷這種殘留需要一對事實，兩者都來自 docker，而「誰該被移除」是純粹的
# 集合運算 —— 那部分住在這裡，可以被離線測試與突變測試盯著。
#
# **被指定為突變目標的行不可以含有 `|`**（見 test_deploy_vps_decisions_mutants.sh
# 開頭的說明），所以下面刻意不用 `||`。改這支時要維持這個性質。

# 給定兩份清單，印出**必須移除的容器名**（每行一個）：
#   $1 = 目前**作用中**的服務（`docker compose config --services`）
#   $2 = 專案裡**實際存在**的容器，每行「容器名<TAB>服務名」
#        （`docker compose ps -a --format '{{.Name}}{{"\t"}}{{.Label "com.docker.compose.service"}}'`）
#
# 差集即殘留：在 compose 檔裡有定義、但不在目前作用中設定裡的服務。
stale_containers() {
  local active="${1:-}" pairs="${2:-}" name svc

  # **作用中的服務清單是空的，就什麼都不動。**
  # 兩個理由，第二個是決定性的：
  #   1. 一份「一個服務都沒有」的 compose 檔幾乎不存在，所以這條很少誤放過
  #   2. 但 `config --services` **失敗**時也會得到空字串，而「失敗」與「真的
  #      沒有服務」在下游長得一模一樣。少了這道守衛，compose 檔任何一個語法
  #      錯誤都會讓差集變成「全部」，然後把整個堆疊砍掉（實測：故意弄壞的
  #      compose 檔，`config --services` 回 1 且沒有任何輸出）。
  # 誤刪一個正在服務的容器，比漏清一個殘留容器貴得多（D-016）。
  if [[ -z "$active" ]]; then
    return 0
  fi

  while IFS=$'\t' read -r name svc; do
    if [[ -z "$name" ]]; then
      continue
    fi
    # 沒有 service 標籤的容器不碰。它可能是 `docker run` 手動建的、或 compose
    # v1 留下來的 —— 我們無法判斷它屬不屬於這個專案，而「不確定」在這裡的
    # 正確處置是留著（同上：誤刪比漏清貴）。
    if [[ -z "$svc" ]]; then
      continue
    fi
    # `-x` 是整行比對，`-F` 是固定字串。**兩個都必要**，各自對應一個真實的
    # 誤判：少了 `-x`，服務 `ollama` 會匹配到容器服務 `ollama-extra`（然後
    # 那個容器就會被留下來繼續跑）；少了 `-F`，服務名裡的 `.` 會被當成正則
    # 的萬用字元（`ollama.` 會匹配 `ollamaX`）。
    #
    # 寫成 `if` 的條件而不是 `grep ... ; if [[ $? ]]`：lib.sh 有 `set -e`，
    # 而**不匹配時 grep 回 1** —— 當成普通指令跑的話，第一個非殘留的容器
    # 就會讓整個腳本結束。`if` 的條件不受 `set -e` 管。
    if grep -qxF "$svc" <<<"$active"; then
      continue
    fi
    printf '%s\n' "$name"
  done <<<"$pairs"
}

# 由兩個事實決定對外連線的**實際**狀態：
#   $1 = profile 有沒有開（.env 的 COMPOSE_PROFILES 含 tunnel）→ yes／no
#   $2 = cloudflared 容器有沒有在跑（問 docker，不是問設定檔）→ yes／no
#
# 四態裡只有 `stale` 是危險的：設定檔說關了，容器說還開著。
# 原本 status.sh 只看 $1，所以那一態根本不存在於它的輸出裡 —— 它會把
# `stale` 印成「未啟用」。
tunnel_state_verdict() {
  local profile="${1:-no}" running="${2:-no}"
  if [[ "$profile" == "yes" ]]; then
    if [[ "$running" == "yes" ]]; then
      printf 'active'
    else
      printf 'enabled-but-down'
    fi
  else
    if [[ "$running" == "yes" ]]; then
      printf 'stale'
    else
      printf 'off'
    fi
  fi
}
