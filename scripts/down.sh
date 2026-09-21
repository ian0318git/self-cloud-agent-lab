#!/usr/bin/env bash
# 停止堆疊。
#
# 預設行為：停止容器，但「保留」volume（模型與對話紀錄不會遺失）。
#   用途：當日工作結束，隔天再開。
#
# --purge  ：一併刪除 volume。下次啟動需重新下載模型。
#   用途：專案驗證完畢、準備刪除整個 codespace 之前。
#
# 注意：volume 即使容器停止仍會佔用 codespace 磁碟，
#       而 Codespaces 的儲存額度是「codespace 存在期間」就計費（含停止狀態）。
#       要真正停止儲存計費，必須刪除整個 codespace，不只是停止容器。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

detect_compose
load_env

PURGE=false
case "${1:-}" in
  --purge) PURGE=true ;;
  "")      ;;
  *)       fail "未知參數：$1（可用：--purge）"; exit 1 ;;
esac

if [[ "$PURGE" == true ]]; then
  warn "即將停止容器並刪除所有 volume（模型、對話紀錄、RAG 知識庫都會消失）"
  read -r -p "確定要繼續嗎？輸入 yes 確認：" reply
  if [[ "$reply" != "yes" ]]; then
    info "已取消"
    exit 0
  fi
  $COMPOSE --profile '*' down -v --remove-orphans
  ok "容器與 volume 已刪除（含被停用 profile 的）"
else
  # **`--profile '*'` 是必要的，`--remove-orphans` 不夠。**
  # 這裡原本寫著「不加 --remove-orphans，停用 profile 的 cloudflared 就會留在
  # 背景繼續把服務對外」—— 那句話是**錯的**：加了也一樣不會被清掉（實測，
  # 見 D-029）。compose 對 orphan 的定義是「compose 檔裡**沒有定義**的服務」，
  # 而被 profile 停用的服務仍然有定義。
  #
  # `'*'` 是把所有 profile 都納入這次 down 的作用範圍 —— 那才是「把整個堆疊
  # 停掉」真正的意思。少了它，一次 `down.sh` 之後 cloudflared 還在對外服務，
  # 而使用者以為已經收工了。
  $COMPOSE --profile '*' down --remove-orphans
  ok "容器已停止（含被停用 profile 的；volume 保留，模型無需重新下載）"
  info "若要一併清除 volume：bash scripts/down.sh --purge"
fi

cat <<'EOF'

提醒：
  • docker compose down 只停止容器，codespace 仍在運作並繼續消耗額度。
    要停止消耗，請在 GitHub 網頁或 CLI 停止 codespace：
      gh codespace stop -c <codespace-name>
  • 停止中的 codespace 仍會佔用儲存額度。若本階段驗證已結束，
    建議直接刪除整個 codespace（請先確認資料已備份）。
EOF
