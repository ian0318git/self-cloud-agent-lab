#!/usr/bin/env bash
# 切換四個「功能型」內建工具群組的開關 —— 不經 UI。
#
# 為什麼需要這支腳本：ollama 的 chat template 會把工具 schema 直接渲染進
# system prompt，所以**工具數目就是 prompt 長度**。本堆疊量到的實例是
# 37 顆工具 = 24,307 字元 = 約 4,937 tokens，而 num_ctx 只有 4096 ——
# 於是 ollama 從中間截斷（limit=2050 prompt=4937 keep=4 new=2050），
# 模型根本看不到問題，回傳空字串，done=1、error=null、沒有任何錯誤訊息。
# 完整推導見 DECISIONS.md D-022。
#
# 這四個開關控制其中 21 顆：
#   memories.enable     8 顆（search_memories, add_memory, update_memory, ...）
#   notes.enable        4 顆（search_notes, view_note, write_note, ...）
#   automations.enable  5 顆（create_automation, list_automations, ...）
#   calendar.enable     4 顆（search_calendar_events, create_calendar_event, ...）
#
# ⚠  **關掉它們不會讓工具歸零。** 另外 14 顆只受模型 meta.builtinTools
#    控制，環境變數與資料庫的開關對它們完全無效：
#      time(2) user_input(1) knowledge(7) chats(2) tasks(2)
#    這點在腳本輸出裡會再講一次，以免出現「都關了怎麼還有工具」的誤解。
#
# 為什麼不直接寫 sqlite：value 欄位是 JSON 型別，手寫 SQL 得自己處理
# 序列化。走 Config.upsert 與 UI 是**同一條路徑**（本專案慣例：
# use the application's own code path）。已實測可從容器內 import。
#
# 為什麼不用重啟：models/config.py 檔頭寫著 "Reads are direct DB lookups"
# —— 每次讀都是直接查資料庫，沒有記憶體快取。所以改完下一則訊息就生效。
# （UI 的瀏覽器分頁可能還顯示舊值，那只是前端快取，重新整理即可。）
#
# 用法：
#   bash scripts/toggle-builtin-tools.sh --check   # 只看狀態（預設）
#   bash scripts/toggle-builtin-tools.sh --off     # 關掉四個
#   bash scripts/toggle-builtin-tools.sh --on      # 開回四個
#
# 結束碼：0 = 狀態符合要求（--check 一律 0）
#         1 = 未達成（寫入後回讀不符預期 —— 見訊息）
#         2 = 無法判定（open-webui 容器未執行）
#         3 = 探針自己壞掉（D-018：這不是設定值的問題）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_docker
detect_compose

MODE="--check"
case "${1:-}" in
  --check | --off | --on) MODE="$1" ;;
  "") ;;
  *)
    fail "未知參數：$1（可用：--check / --off / --on）"
    exit 3
    ;;
esac

if ! docker ps --format '{{.Names}}' | grep -qx open-webui; then
  fail "open-webui 容器未在執行中 —— 先執行 bash scripts/up.sh"
  exit 2
fi

info "模式：$MODE"

# 模式用參數傳進容器（不要拼進程式碼），輸出與結束碼原樣保留。
# `|| rc=$?` 是為了在 set -e 之下留住結束碼再分類（見腳本尾的 case）。
rc=0
$COMPOSE exec -T open-webui python3 - "$MODE" <<'PY' || rc=$?
import asyncio
import os
import sys

MODE = sys.argv[1]
KEYS = ('memories.enable', 'notes.enable', 'automations.enable', 'calendar.enable')

# 不受這四個開關控制的工具群組。寫在這裡是為了讓輸出自己說明白 ——
# 否則「關了四顆開關卻還有 16 顆工具」看起來像腳本沒生效。
NOT_GATED = {'time': 2, 'user_input': 1, 'knowledge': 7, 'chats': 2, 'tasks': 2}
MCP_TOOLS = 2

try:
    os.chdir('/app/backend')
    from open_webui.models.config import Config
except Exception as e:
    print('探針自己壞掉：無法載入 Config（%s: %s）' % (type(e).__name__, e))
    sys.exit(3)


def snapshot():
    return asyncio.run(Config.get_many(*KEYS))


def show(values, label):
    print(label)
    for key in KEYS:
        print('  %-22s %s' % (key, values.get(key)))


try:
    before = snapshot()
    show(before, '目前狀態：')

    if MODE == '--check':
        after = before
    else:
        target = MODE == '--on'
        asyncio.run(Config.upsert({key: target for key in KEYS}))
        after = snapshot()
        print('')
        show(after, '寫入後回讀資料庫：')

        # 回讀驗證 —— 沒有回讀的寫入就是靜默失敗
        bad = [k for k in KEYS if bool(after.get(k)) is not target]
        if bad:
            print('')
            print('未通過：回讀後仍與預期不符的鍵：%s' % ', '.join(bad))
            sys.exit(1)

    on = [k for k in KEYS if after.get(k)]
    print('')
    print('四個開關目前開啟 %d 個%s' % (len(on), ('：' + ', '.join(on)) if on else '（全部關閉）'))
    print('')
    print('不受這四個開關控制的工具（仍會被渲染進 prompt）：')
    for name in sorted(NOT_GATED):
        print('  %-12s %d 顆' % (name, NOT_GATED[name]))
    print('  %-12s %d 顆' % ('小計', sum(NOT_GATED.values())))
    print('加上 MCP %d 顆 = 共 %d 顆（關掉前是 37 顆）'
          % (MCP_TOOLS, sum(NOT_GATED.values()) + MCP_TOOLS))
except Exception as e:  # noqa: BLE001 —— 探針的意外都是「自己壞掉」，不是設定值的錯
    print('探針自己壞掉：%s %s' % (type(e).__name__, e))
    sys.exit(3)
PY

case $rc in
  0)
    ok "狀態確認完成"
    if [[ "$MODE" == "--check" ]]; then
      info "要關掉請執行：bash scripts/toggle-builtin-tools.sh --off"
    fi
    ;;
  1)
    fail "切換未達成（見上方輸出）"
    exit 1
    ;;
  2)
    fail "無法判定（見上方輸出）"
    exit 2
    ;;
  3)
    fail "探針自己壞掉（見上方輸出）—— 這不是設定值的問題，是這支腳本該修"
    exit 3
    ;;
  *)
    fail "意外的結束碼 $rc"
    exit 3
    ;;
esac
