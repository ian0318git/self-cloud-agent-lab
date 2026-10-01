#!/usr/bin/env bash
# 第三階段 item 5（第二套向量庫的維運面）的純函式判定。
#
# 這個檔案**只定義函式**，被 source 進來用，不自己執行任何東西。
# 它不呼叫 docker、不讀檔、不碰網路 —— 所有 I/O 都在 verify-phase3-runtime.sh，
# 判定留在這裡，所以判定可以在沒有 docker 的機器上被離線測試。
#
# 為什麼 item 5 需要判定函式而不是一段報告：
# HANDBOOK 對這一項的敘述是「兩套儲存要備份、遷移、保持一致」。量測之後那句話
# **不成立** —— 第二套（mem0 的 ChromaDB）住在 `--rm` 容器裡的暫存目錄，
# 從來沒有變成一個 volume，所以沒有東西可以備份、可以遷移、可以不一致。
# 而真正在吃磁碟的東西跟向量庫無關。這兩件事都需要一個會**區分**的判定，
# 否則下一個人還是會照著那句話去規劃。（D-024 第八節）

# store_durability_verdict <mount_kind>
#
#   mount_kind ∈ named-volume | bind-mount | container-tmp | unknown
#
# 回傳三個字之一：
#   durable-restart-only —— 容器重啟還在，但 `docker compose down -v` 會刪掉
#   durable              —— 容器重啟與 down -v 都活得下來
#   ephemeral            —— 跟容器一起消失；**沒有維運面可言**
#
# 第三種是 item 5 量到的實情，也是最容易被誤讀的一種：
# 「它不在 volume 清單裡」不等於「它很安全」，而是「它根本不存在於任何
# 備份策略的守備範圍內」。把它當成 durable 來規劃，會在第一次真的需要
# 那些向量時才發現它們早就沒了。
store_durability_verdict() {
  case "${1:-unknown}" in
    named-volume)
      echo "durable-restart-only" ;;
    bind-mount)
      echo "durable" ;;
    container-tmp)
      echo "ephemeral" ;;
    *)
      echo "unknown" ;;
  esac
}

# store_durability_reason <mount_kind>
store_durability_reason() {
  case "$(store_durability_verdict "${1:-unknown}")" in
    durable-restart-only)
      echo "在 named volume 裡：容器重啟活得下來，但 down -v 會刪掉它（D-029 的 down -v 語意）。" ;;
    durable)
      echo "在 bind mount 裡：容器重啟與 down -v 都不影響，備份就是備份那個路徑。" ;;
    ephemeral)
      echo "在容器的暫存目錄裡：隨 --rm 容器一起消失。沒有東西可以備份、遷移或保持一致 —— 這一項的維運面前提不成立。" ;;
    *)
      echo "掛載型態不明，無法判定持久性。" ;;
  esac
}

# disk_pressure_verdict <used_pct> <avail_mb> <new_store_mb> <reclaimable_mb>
#
# 回傳 verdict 與一行理由（用 `|` 分隔，方便呼叫端切）。
#
#   ok          —— 直接放得下
#   reclaim-first —— 放不下，但**可回收的量比要新增的量還大**：先清再放
#   blocked     —— 放不下而且沒東西可清
#
# `reclaim-first` 是這一項真正的發現。磁碟 81% 看起來很緊，但 18.32GB 的映像裡
# 有 16.46GB 是可回收的，而向量庫本身是 6.1MB —— 順序反了：
# 該處理的是映像，不是儲存。
disk_pressure_verdict() {
  local used_pct="${1:-}" avail_mb="${2:-}" new_mb="${3:-}" reclaim_mb="${4:-}"
  local n
  for n in "$used_pct" "$avail_mb" "$new_mb" "$reclaim_mb"; do
    [[ "$n" =~ ^[0-9]+$ ]] || { echo "unknown|數值無法解析（收到 '$n'）"; return 0; }
  done
  if (( avail_mb >= new_mb )); then
    echo "ok|可用 ${avail_mb}MB 放得下 ${new_mb}MB。"
    return 0
  fi
  if (( reclaim_mb >= new_mb )); then
    echo "reclaim-first|可用只有 ${avail_mb}MB < ${new_mb}MB，但有 ${reclaim_mb}MB 可回收（> 要新增的量）—— 先回收再放。"
    return 0
  fi
  echo "blocked|可用 ${avail_mb}MB < ${new_mb}MB，且可回收只有 ${reclaim_mb}MB，兩者相加仍不足以放入。"
}

# store_role_verdict <mount_kind> <size_bytes>
#
# 把「這個 store 值不值得為它做維運」講清楚。item 5 的原始顧慮是兩套儲存要
# 保持一致；量到的事實是那第二套不存在，而存在的那一套是 6.1MB。
# 一個 6.1MB 的東西不需要一套備份策略，它需要被**知道在哪裡**。
store_role_verdict() {
  local kind="${1:-unknown}" size="${2:-}"
  [[ "$size" =~ ^[0-9]+$ ]] || { echo "unknown|大小無法解析（收到 '$size'）"; return 0; }
  if [[ "$(store_durability_verdict "$kind")" == "ephemeral" ]]; then
    echo "not-a-store|這是暫存目錄，不是儲存：它不參與備份、遷移或一致性，因為它每次跑完就沒了。"
    return 0
  fi
  if (( size < 104857600 )); then   # < 100 MiB
    echo "negligible|$(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B")：比同一個 volume 裡的快取小兩三個數量級，維運成本不是來自它。"
    return 0
  fi
  echo "material|$(numfmt --to=iec "$size" 2>/dev/null || echo "${size}B")：值得納入備份與遷移。"
}
