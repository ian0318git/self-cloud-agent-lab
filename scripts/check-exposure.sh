#!/usr/bin/env bash
# 機械檢查「誰能從外面連到這個堆疊」。
#
# 為什麼需要這支腳本，而不是把注意事項寫在文件裡就好：
#   同一份 docker-compose.yml 在 Codespaces 上是安全的，在自己的 VPS 上是
#   危險的 —— 差別不在檔案，在**機器在哪**。也就是說，一個在 Codespaces
#   上反覆驗證過「沒問題」的設定，會在搬家的那一刻變成問題；而搬家的時候
#   沒有人會回頭懷疑一個已經用了很久、從沒出過事的設定。
#
#   這正是 D-014 記下的失效形狀：**條件變了，結論沒跟著變。**
#   所以這件事要機械檢查 —— 靠人記得去讀文件的檢查，等於沒有檢查。
#
# 檢查兩件事：
#   1. Ollama 的 11434 **不得**發布到主機。Ollama 沒有任何認證機制，
#      任何能觸及該埠的人都可以讀取、刪除、推送模型（見 D-003）。
#   2. Open WebUI 的 3000 若綁在 0.0.0.0 且**不在 Codespaces 上**，就是
#      對整個 Internet 開放 —— 而且會**繞過** Cloudflare Access。Access
#      保護的是經過 tunnel 的那條路，不是這個埠。
#
# 讀的是**執行中容器的實際綁定**（docker port），不是 compose 檔的宣告。
# 兩者可能不一致（改了 .env 但沒重建容器），而前者才是真正發生的事。
#
# 用法：
#   bash scripts/check-exposure.sh
#
# 結束碼：0 = 未發現暴露；1 = 發現暴露；2 = **無法判定**
#
# 2 有兩種來源，而兩者都不能被讀成「安全」：
#   · 容器未執行 —— 沒有綁定可讀
#   · 綁在 0.0.0.0，但**判定不出這台機器有沒有公開位址**：讀不到 `ip`，
#     或這台是 1:1 NAT 的雲主機（在那種機器上 `ip` 只會讀到私有位址）。
#     兩者在 2026-09-21 之前都會走到「只有私有位址」那條分支然後回 0 ——
#     那是把「我讀不到」講成「沒有」，也就是 fail-open。

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$(dirname "${BASH_SOURCE[0]}")/deploy-vps-decisions.sh"

require_docker
detect_compose
load_env

# GitHub Codespaces 會設定這個變數。它決定 0.0.0.0 是安全還是危險，
# 因為 Codespaces 的埠預設為私有，需通過 GitHub 認證才連得到。
IS_CODESPACE="${CODESPACES:-false}"

echo "── 對外暴露檢查 ──────────────────────────────"
if [[ "$IS_CODESPACE" == "true" ]]; then
  echo "  環境：GitHub Codespaces（埠預設為私有，需通過 GitHub 認證）"
else
  echo "  環境：非 Codespaces（埠的可達性取決於這台機器的防火牆與公開 IP）"
fi
echo

# 收集每個容器的實際埠綁定。容器沒跑就取不到 —— 那是「無法判定」，
# 不是「安全」，兩者絕不可混為一談。
declare -A BINDINGS=()
ANY_RUNNING=false

for svc in ollama open-webui cloudflared; do
  cid="$($COMPOSE ps -q "$svc" 2>/dev/null || true)"
  [[ -z "$cid" ]] && continue
  ANY_RUNNING=true

  # 只取執行中的容器；docker port 對已停止的容器回空。
  ports="$(docker port "$cid" 2>/dev/null || true)"
  if [[ -z "$ports" ]]; then
    BINDINGS["$svc"]="（未發布任何埠）"
  else
    BINDINGS["$svc"]="$ports"
  fi
done

if [[ "$ANY_RUNNING" == "false" ]]; then
  fail "沒有任何容器在執行，無法判定暴露狀況。"
  echo "  這不是「安全」，是「不知道」。請先啟動堆疊：bash scripts/up.sh"
  exit 2
fi

for svc in ollama open-webui cloudflared; do
  [[ -v BINDINGS["$svc"] ]] || continue
  echo "  $svc："
  while IFS= read -r line; do
    [[ -n "$line" ]] && echo "    $line"
  done <<<"${BINDINGS[$svc]}"
done
echo

EXPOSED=0

# ── 檢查 1：Ollama 11434 絕不可發布 ─────────────────────
if [[ -v BINDINGS[ollama] ]] && grep -q "11434" <<<"${BINDINGS[ollama]}"; then
  fail "Ollama 的 11434 已發布到主機！"
  echo "  Ollama 沒有任何認證機制 —— 任何能連上這個埠的人都可以讀取、"
  echo "  刪除、推送模型。請移除 docker-compose.yml 中 ollama 服務的 ports。"
  EXPOSED=1
else
  ok "Ollama 11434 未對外發布（符合 D-003）"
fi

# ── 檢查 2：Open WebUI 3000 的綁定位址 ──────────────────
# 「綁在 0.0.0.0」本身不等於「對 Internet 開放」—— 還要看這台機器有沒有
# 公開位址。在 NAT 後的家庭網路裡，0.0.0.0 只代表區網可達。
#
# 這個區分不是吹毛求疵：一個在安全情境下也照樣報紅的檢查，最後會被忽略 ——
# 而一個被忽略的檢查等於沒有檢查。本專案已經吃過太多次「假失敗讓人開始
# 懷疑一個其實沒問題的設定」的虧。所以寧可多寫幾行把兩種情況分開。
#
# **但「讀不到公開位址」與「沒有公開位址」是兩件事**，而下面那個
# HAS_PUBLIC=false 分不出來。兩個實際會發生的情況：
#   · 精簡的 VPS image 沒有 `ip` 指令 —— 兩條管線都是空的
#   · AWS／GCP／Azure／Oracle：NIC 上**永遠**只有私有位址，公開 IP 是
#     1:1 NAT 在前面。`ip` 讀得到、但讀到的永遠是 10.x
# 兩者原本都會落到「只有私有位址（在 NAT 後）」那條分支，然後回結束碼 0。
# 那是 fail-open —— 把「我讀不到」講成「沒有」。所以這兩個狀態要能被
# 分辨出來，並且走「無法判定」。
IP_AVAILABLE=true
if ! command -v ip >/dev/null 2>&1; then
  IP_AVAILABLE=false
  PUBLIC_V4=""
  PUBLIC_V6=""
else
  PUBLIC_V4="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' \
    | cut -d/ -f1 \
    | grep -vE '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|127\.|169\.254\.)' || true)"
  PUBLIC_V6="$(ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' \
    | cut -d/ -f1 | grep -vE '^(fc|fd|fe80)' || true)"
fi
HAS_PUBLIC=false
[[ -n "$PUBLIC_V4$PUBLIC_V6" ]] && HAS_PUBLIC=true

# 1:1 NAT 的雲主機。讀 /sys（I/O）在這裡，判斷本身在
# deploy-vps-decisions.sh 的 nat_fronted_environment() 裡（那裡可測）。
DMI_VENDOR="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)"
DMI_PRODUCT="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
NAT_ENV="$(nat_fronted_environment "$DMI_VENDOR" "$DMI_PRODUCT")"

print_fix() {
  echo
  echo "  修正：在 .env 設定"
  echo "      WEBUI_BIND_ADDR=127.0.0.1"
  echo "  然後重建容器："
  echo "      bash scripts/down.sh && bash scripts/up.sh"
  echo "  對外存取一律走 Cloudflare Tunnel + Access。"
}

INDETERMINATE=0

if [[ -v BINDINGS[open-webui] ]] && grep -q "3000" <<<"${BINDINGS[open-webui]}"; then
  # 只要有任何一行綁在萬用位址，就是對外開放。
  wide="$(grep -E '\-> (0\.0\.0\.0|\[::\]|::):' <<<"${BINDINGS[open-webui]}" || true)"

  if [[ -z "$wide" ]]; then
    ok "Open WebUI 3000 僅綁在 loopback（對外只能經由 tunnel 抵達）"
  else
    # 判定順序與每一條分支的結論都在 wide_bind_verdict() 裡（那裡可測）。
    case "$(wide_bind_verdict "$IS_CODESPACE" "$HAS_PUBLIC" "$IP_AVAILABLE" "$NAT_ENV")" in
      codespace)
        warn "Open WebUI 綁在 0.0.0.0 —— 在 Codespaces 上這是可接受的"
        echo "    （那裡的埠預設為私有，需通過 GitHub 認證才連得到）。"
        echo "    但這個設定**不能**直接帶到自己的 VPS。" ;;

      exposed)
        fail "Open WebUI 綁在 0.0.0.0，且這台機器**有公開位址** —— 等於對 Internet 開放！"
        echo
        echo "  這台機器的公開位址："
        [[ -n "$PUBLIC_V4" ]] && while IFS= read -r ip; do echo "      $ip"; done <<<"$PUBLIC_V4"
        [[ -n "$PUBLIC_V6" ]] && while IFS= read -r ip; do echo "      $ip（IPv6）"; done <<<"$PUBLIC_V6"
        echo "  只要防火牆沒擋，任何人都連得到你的 Open WebUI。更麻煩的是："
        echo "  它會**繞過** Cloudflare Access —— Access 保護的是經過 tunnel 的"
        echo "  那條路，不是這個埠。掃到 3000 的人不經過它。"
        print_fix
        EXPOSED=1 ;;

      unknown)
        warn "Open WebUI 綁在 0.0.0.0，但這台機器上讀不到 \`ip\` 指令 —— **無法判定**有沒有公開位址。"
        echo "    這不是「安全」，是「不知道」。在 VPS 上這通常就是對 Internet 開放。"
        echo "    要自己確認：這台機器的對外 IP 是什麼、防火牆有沒有擋 3000。"
        print_fix
        INDETERMINATE=1 ;;

      nat_fronted)
        warn "Open WebUI 綁在 0.0.0.0，而這台看起來是 $NAT_ENV —— **無法判定**有沒有公開位址。"
        echo "    這種機器的 NIC 上**永遠**只有私有位址（公開 IP 是 1:1 NAT 在前面），"
        echo "    所以「只讀到 10.x」推論不出「不到 Internet」。"
        echo "    要自己確認：雲端控制台的安全性群組有沒有放行 3000。"
        print_fix
        INDETERMINATE=1 ;;

      *)
        warn "Open WebUI 綁在 0.0.0.0，但這台機器只有私有位址（在 NAT 後）。"
        echo "    目前只有同一個區網的裝置連得到 —— 不到 Internet。"
        echo "    **這是靠外部環境擋住的，不是靠這個堆疊。** 換到有公開 IP 的"
        echo "    VPS 時，同一個設定就會變成真的對外開放。搬到 VPS 時請一併改。"
        print_fix ;;
    esac
  fi
else
  warn "Open WebUI 目前沒有發布 3000 埠 —— 請確認這是你要的。"
fi

echo
# 順序是刻意的：1 是**確定**的暴露，2 是「不確定」。確定的那一個要先講，
# 否則一台同時踩到兩者的機器會收到比較弱的訊息。
if [[ $EXPOSED -eq 1 ]]; then
  fail "發現對外暴露。請依上方指示修正。"
  exit 1
fi
if [[ $INDETERMINATE -eq 1 ]]; then
  fail "無法判定是否對外暴露 —— 請依上方指示自行確認。"
  exit 2
fi

ok "未發現對外暴露。"
cat <<'EOF'

注意：這支腳本檢查的是「容器把埠綁在哪」，不是「防火牆有沒有擋」。
      它無法得知雲端廠商的安全群組或主機防火牆設定 ——
      在 VPS 上，這兩者仍然是你的責任。
EOF
