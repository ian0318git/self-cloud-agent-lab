#!/usr/bin/env bash
# 「這一輪的數字是哪一版探針跑的？」—— 純判定，被 source 進來用。
#
# 這個檔案**只定義函式**，被 source 時不做任何事：不呼叫 docker、不讀檔、
# 不寫檔、不碰網路。所有輸入都由參數傳入，所有輸出都走 stdout。
# I/O 留在呼叫端（verify-mem0-add-cost.sh），判定留在這裡 —— 所以判定可以
# 在沒有 docker 的機器上被離線測試（scripts/test_probe_traceability.sh）。
#
# ── 為什麼需要這個判定 ────────────────────────────────────────────────
#
# 四支 verify-*.sh 都把探針**從工作樹即時 bind-mount** 進容器，不是烤進映像：
#
#   verify-mem0-add-cost.sh:298   -v "$PROBE:/probe/mem0_add_cost_probe.py:ro"
#   verify-chroma-dims.sh:132     -v "$PROBE:/probe/chroma_dims_probe.py:ro"
#   verify-langgraph-tools.sh:141 -v "$PROBE:/probe/langgraph_tools_probe.py:ro"
#   verify-phase3-runtime.sh:225  -v "$PROBE:/probe/phase3_runtime_probe.py:ro"
#
# 這樣設計是對的（改評分邏輯不必重建映像），但它有兩個後果：
#
#   1. **映像的雜湊標籤蓋不到探針。** verify-mem0-add-cost.sh 用
#      `lab.req-sha256` 標記映像，而那個標籤只涵蓋 requirements-mem0.txt
#      ——`--rebuild` 對探針毫無作用。
#   2. **探針的修訂版＝容器啟動當下的工作樹。** 跑完之後才發現工作樹被改過，
#      就再也分不出那一輪跑的是舊碼還是新碼。
#
# 而探針**只在啟動時讀一次自己的雜湊**（probe_revision()，寫進 evidence
# 的 meta.probe.sha256_16）。所以：跑完之後拿工作樹去比對，對不上時，
# 是「跑的是舊碼」還是「跑完才被改」，事後分不出來。
#
# 這一條不是假想的。D-027 的鑑識就是這樣：run2 跑完之後探針被改過，而
# 「那組數字是哪一版跑的」最後只能靠一個殘留的 .pyc 做 bytecode 比對才答
# 得出來。一份跑了好幾小時、要寫進 DECISIONS.md 的數字，不該只能靠這種
# 方式追回它的歸屬。
#
# 這個守衛把那個沉默的失敗變成一聲響：**跑之前與跑之後各算一次雜湊。**
#
# ── 為什麼是兩個函式而不是一個 ────────────────────────────────────────
#
# 「雜湊有沒有變」與「這一輪的結束碼該是什麼」是兩個判斷。分開，是因為
# 第二個是**決定性的**：它決定 `changed` 蓋不蓋過 `--sections` 的 2。
# 把那個 if 留在呼叫端的 case 裡，就沒有任何測試盯得住它。

# probe_stable_verdict <跑之前的 sha256> <跑之後的 sha256>
#
# 回傳三個字之一：
#   stable  —— 兩次相同，這一輪的數字可以追回一個修訂版
#   changed —— 兩次不同，這一輪的數字對不上任何一個修訂版
#   unknown —— 至少有一次算不出來（空字串），**沒有主張**
#
# **空字串是「算不出來」，不是「變了」。** 這兩件事混成一件就會製造假失敗
# （讀不到檔就說儀器被換掉）—— 而 D-016 說得很清楚：一個會誤報的檢查最後
# 會被忽略，被忽略的檢查等於沒有檢查。所以 unknown 是**獨立的第三態**，
# 呼叫端對它只警告不失敗。
#
# 注意 `unknown` 與 `stable` 也**不可以**混為一談：把「沒守到」讀成「守住了」
# 是反方向的同一種錯，而它更難發現 —— 因為報告看起來一切正常。
probe_stable_verdict() {
  # 空字串＝算不出來。拆成兩行而不是 `-z "$1" || -z "$2"`：突變清單的目標
  # 行不可以含有 `|`（見 scripts/test_deploy_vps_decisions_mutants.sh 開頭）。
  if [[ -z "$1" ]]; then printf 'unknown'; return 0; fi
  if [[ -z "$2" ]]; then printf 'unknown'; return 0; fi
  if [[ "$1" == "$2" ]]; then printf 'stable'; return 0; fi
  printf 'changed'
}

# probe_guard_verdict <探針自己的結束碼> <probe_stable_verdict 的輸出>
#
# 回傳這一輪**有效**的結束碼。只有一種情況會改寫它：
#
#   changed → 3（無條件，蓋過探針自己回的 0/1/2）
#
# 為什麼無條件：
#   · 探針回 0（全過）時更不能放過 —— 一份追不回修訂版的「通過」是假的通過，
#     而那正是這個守衛最想擋的東西。
#   · 探針回 2（--sections 的分段重跑）時，`changed` 比它更嚴重：分段重跑是
#     **設計**（只跑一部分永遠不算通過），而儀器被換掉是儀器壞了。
#     D-018 的 3 就是這個意思：不是受測對象的問題。
#
#   stable / unknown → 原封不動。unknown 只警告，不改結束碼（D-016）。
#
# **unknown 不可以被當成 stable 或 changed 來處理** —— 它不改結束碼，所以
# 呼叫端必須自己把那聲警告印出來，否則它就變成靜默。
probe_guard_verdict() {
  if [[ "$2" == "changed" ]]; then printf '3'; return 0; fi
  printf '%s' "$1"
}
