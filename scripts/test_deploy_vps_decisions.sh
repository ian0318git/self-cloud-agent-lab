#!/usr/bin/env bash
# 測 scripts/deploy-vps-decisions.sh。
#
# 為什麼這支要存在：那支模組裡有兩個判斷，錯了都不會有症狀 ——
# 「綁定位址該用什麼」與「閘門的結束碼 2 要讀成什麼」。前者回錯一個字，
# 整個堆疊就在公網上而所有輸出都正常；後者回錯一個字，就是把「我沒查到」
# 講成「我查過了」。兩者都沒有第二次機會。
#
# 所以每個判準都要有「該過的過」與「該擋的擋」（D-016／D-024），而且斷言
# 盯的是**可區辨的字串**（D-026 第五節）—— 例如閘門的 rc=1 與 rc=2 在形狀
# 上都是「非 0」，只檢查「有沒有非 0」是測不出兩者處置不同的。
#
# 用法：bash scripts/test_deploy_vps_decisions.sh
# 結束碼：0 = 全過；1 = 有案例沒過

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DECISIONS="$SCRIPT_DIR/deploy-vps-decisions.sh"
source "$DECISIONS"

PASS=0
FAILED=0

check() {   # <名稱> <期望> <實際>
  local name="$1" want="$2" got="$3"
  if [[ "$got" == "$want" ]]; then
    PASS=$((PASS + 1))
    ok "$name"
  else
    FAILED=$((FAILED + 1))
    fail "$name —— 期望「$want」，得到「$got」"
  fi
}

# ═══════════════════════════════════════════════════════════
# default_bind_addr —— 整個部署最重要的一個字
# ═══════════════════════════════════════════════════════════
check "綁定：非 Codespaces 的機器一律 loopback" "127.0.0.1" "$(default_bind_addr false)"
check "綁定：沒有明講時也是 loopback（預設必須是安全的那一邊）" "127.0.0.1" "$(default_bind_addr)"
check "綁定：Codespaces 上才用 0.0.0.0" "0.0.0.0" "$(default_bind_addr true)"
# 「差不多是 true」的字串不該被當成 true —— 這條擋的是寬鬆比對。
check "綁定：只有字面上的 true 才算（1 不算）" "127.0.0.1" "$(default_bind_addr 1)"

# ── resolve_bind_addr：整個部署真正的對外介面 ───────────────
# 下面每一條都對應一個**實際會發生**的處境，不是為了湊覆蓋率。第一條尤其
# 重要：它是這支腳本存在的理由，而它的第一個版本（寫在進入點裡、沒有測試）
# 就是错在這裡 —— 全新 VPS 的 .env 由 `cp .env.example .env` 產生，於是
# `.env` 裡「本來就」有 0.0.0.0，照單全收就把洞原封不動地留下了。
rb() { resolve_bind_addr "$1" "$2" "$3" | tr '\n' ' '; }

check "解析：全新 VPS（.env 由範本複製，裡面是 0.0.0.0）必須被改掉" \
      "127.0.0.1 0.0.0.0 " "$(rb 0.0.0.0 0 false)"
check "解析：.env 沒有這個鍵 —— 套預設，但沒覆蓋掉什麼，不該警告" \
      "127.0.0.1  " "$(rb '' 0 false)"
check "解析：使用者明講 127.0.0.1 —— 尊重，且不警告" \
      "127.0.0.1  " "$(rb 127.0.0.1 0 false)"
# 綁特定網卡不是 fail-open，不該被「順手改正」——那會關掉他的區網存取。
check "解析：使用者綁在 10.0.0.5 —— 尊重（這不是萬用位址）" \
      "10.0.0.5  " "$(rb 10.0.0.5 0 false)"
check "解析：--expose 一律 0.0.0.0" "0.0.0.0  " "$(rb 127.0.0.1 1 false)"
# --expose 蓋掉一個本來就是 0.0.0.0 的值時，不該說「已改成 0.0.0.0」。
check "解析：--expose 且 .env 本來就是 0.0.0.0 —— 沒有覆蓋，不警告" \
      "0.0.0.0  " "$(rb 0.0.0.0 1 false)"
check "解析：Codespaces 上 0.0.0.0 是對的，不該被改掉也不該警告" \
      "0.0.0.0  " "$(rb 0.0.0.0 0 true)"
check "解析：Codespaces 但使用者明講 loopback —— 尊重" \
      "127.0.0.1  " "$(rb 127.0.0.1 0 true)"
# IPv6 的萬用位址同樣是萬用位址。
check "解析：IPv6 的 :: 也算萬用位址" "127.0.0.1 :: " "$(rb '::' 0 false)"
check "解析：[::] 也算萬用位址" "127.0.0.1 [::] " "$(rb '[::]' 0 false)"

# bind_value_is_wildcard 本身
check "萬用位址：0.0.0.0" "yes" "$(bind_value_is_wildcard 0.0.0.0)"
check "萬用位址：::" "yes" "$(bind_value_is_wildcard '::')"
check "萬用位址：127.0.0.1 不是" "no" "$(bind_value_is_wildcard 127.0.0.1)"
check "萬用位址：空字串不是（那是「沒設」，不是「開放到全部」）" \
      "no" "$(bind_value_is_wildcard '')"
# 只判斷整個值，不做子字串比對 —— 否則 10.0.0.0.1 之類會被誤判。
check "萬用位址：不做子字串比對（10.0.0.0.1 不是）" "no" "$(bind_value_is_wildcard 10.0.0.0.1)"

# ═══════════════════════════════════════════════════════════
# bind_is_wildcard —— 不依賴 `ip` 的那個檢查
# ═══════════════════════════════════════════════════════════
WILD='3000/tcp -> 0.0.0.0:3000'
LOOP='3000/tcp -> 127.0.0.1:3000'
check "綁定判讀：0.0.0.0 是萬用位址" "yes" "$(bind_is_wildcard "$WILD")"
check "綁定判讀：127.0.0.1 不是萬用位址" "no" "$(bind_is_wildcard "$LOOP")"
check "綁定判讀：IPv6 的 :: 也算萬用位址" "yes" "$(bind_is_wildcard '3000/tcp -> [::]:3000')"
# 讀不到 `docker port` 的輸出時**不可以**回 "no" —— 那正是 fail-open 的形狀。
# 「不知道」與「沒綁在萬用位址」在這裡處置完全不同（呼叫端把 unknown 當不安全）。
check "綁定判讀：讀不到時回 unknown，不是 no" "unknown" "$(bind_is_wildcard '')"

# ═══════════════════════════════════════════════════════════
# bind_addr_scope ／ port_bind_scope —— 「不是 0.0.0.0」不等於 loopback
# ═══════════════════════════════════════════════════════════
# 這一組守的是 check-exposure.sh 原本那個 fail-open。它只認得萬用位址，其餘
# 一律印「僅綁在 loopback」，而那個推論有兩個錯：
#
#   · 綁在特定網卡上（這個 lab 就是 192.168.44.128）時，那句話**與事實相反** ——
#     同一層網路的裝置直接連得到，不經過 tunnel，也不經過 Cloudflare Access。
#   · 綁在**公開位址**上時，它會回「未發現對外暴露」，而機器其實對 Internet
#     開著。那是把「我沒檢查」講成「沒問題」。
#
# 最貴的一條是「公開位址被判成 public，不是 private」—— 那個字判錯，整套
# 安全檢查就是空的，而所有輸出照常。
check "位址分類：127.0.0.1 是 loopback" "loopback" "$(bind_addr_scope 127.0.0.1)"
check "位址分類：::1 是 loopback" "loopback" "$(bind_addr_scope '::1')"
# docker port 對 IPv6 會寫成 [::1]:3000 —— 方括號要在分類之前脫掉。
check "位址分類：[::1]（docker port 的 IPv6 寫法）也是 loopback" \
      "loopback" "$(bind_addr_scope '[::1]')"
check "位址分類：0.0.0.0 是萬用" "wildcard" "$(bind_addr_scope 0.0.0.0)"
check "位址分類：[::] 也是萬用" "wildcard" "$(bind_addr_scope '[::]')"
check "位址分類：這個 lab 的 192.168.44.128 是私有" \
      "private" "$(bind_addr_scope 192.168.44.128)"
check "位址分類：10.x 是私有" "private" "$(bind_addr_scope 10.0.0.5)"
check "位址分類：172.16.x 是私有" "private" "$(bind_addr_scope 172.16.3.9)"
# 172.15 與 172.32 不在 RFC1918 裡 —— 邊界要盯，不然「差不多像」就會被放進去。
check "位址分類：172.15.x 不在 RFC1918（算公開）" "public" "$(bind_addr_scope 172.15.3.9)"
check "位址分類：172.32.x 不在 RFC1918（算公開）" "public" "$(bind_addr_scope 172.32.3.9)"
# CGNAT 從外面連不到，與 RFC1918 同類；但 100.63 與 100.128 不在 100.64/10 裡。
check "位址分類：100.64.x（CGNAT）是私有" "private" "$(bind_addr_scope 100.64.0.1)"
check "位址分類：100.128.x 不在 CGNAT 段（算公開）" "public" "$(bind_addr_scope 100.128.0.1)"
# **這一條是整個分類存在的理由。**
check "位址分類：公開 IPv4 是 public，不是 private" "public" "$(bind_addr_scope 203.0.113.5)"
check "位址分類：公開 IPv6 是 public" "public" "$(bind_addr_scope '2001:db8::1')"
check "位址分類：[fd00::1]（ULA，docker port 的寫法）是私有" \
      "private" "$(bind_addr_scope '[fd00::1]')"
check "位址分類：[fe80::1]（link-local）是私有" \
      "private" "$(bind_addr_scope '[fe80::1]')"
check "位址分類：空的回 unknown" "unknown" "$(bind_addr_scope '')"
# 認不出來的東西不可以被歸進安全的那一類（fail-closed）。
check "位址分類：認不出來的回 unknown" "unknown" "$(bind_addr_scope 'some-hostname')"

# ── port_bind_scope：整個埠的綁定，多條取最寬的 ──
check "整體綁定：只有 loopback" "loopback" \
      "$(port_bind_scope '3000/tcp -> 127.0.0.1:3000')"
check "整體綁定：萬用位址" "wildcard" \
      "$(port_bind_scope '3000/tcp -> 0.0.0.0:3000')"
check "整體綁定：IPv6 的 [::]" "wildcard" \
      "$(port_bind_scope '3000/tcp -> [::]:3000')"
check "整體綁定：這個 lab 現在的綁定是私有（不是 loopback）" "private" \
      "$(port_bind_scope '3000/tcp -> 192.168.44.128:3000')"
check "整體綁定：公開位址" "public" \
      "$(port_bind_scope '3000/tcp -> 203.0.113.5:3000')"
# 多條綁定取**最寬**的 —— 只要有一條公開，其餘是 loopback 也救不回來。
check "整體綁定：多條取最寬（有公開就是公開）" "public" \
      "$(port_bind_scope $'3000/tcp -> 127.0.0.1:3000\n3000/tcp -> 203.0.113.5:3000')"
check "整體綁定：多條取最寬（有私有就不是 loopback）" "private" \
      "$(port_bind_scope $'3000/tcp -> 127.0.0.1:3000\n3000/tcp -> 10.0.0.5:3000')"
check "整體綁定：多條取最寬（順序反過來也一樣）" "public" \
      "$(port_bind_scope $'3000/tcp -> 203.0.113.5:3000\n3000/tcp -> 127.0.0.1:3000')"
# 讀不到、以及解析不出位址，都不可以被當成 loopback —— 那正是原本那個
# fail-open 的形狀（把「我不知道」講成「沒問題」）。
check "整體綁定：讀不到 → unknown，不是 loopback" "unknown" "$(port_bind_scope '')"
check "整體綁定：解析不出位址 → unknown，不是 loopback" "unknown" \
      "$(port_bind_scope '3000/tcp')"
# 解析出一個認不得的位址也一樣：unknown 在縮減裡不可以被 loopback 吃掉。
check "整體綁定：認不出來的位址 → unknown，不是 loopback" "unknown" \
      "$(port_bind_scope '3000/tcp -> some-hostname:3000')"

# ═══════════════════════════════════════════════════════════
# exposure_gate_verdict —— 0／1／2／3 是四個不同的處置
# ═══════════════════════════════════════════════════════════
check "閘門：0 是通過" "pass" "$(exposure_gate_verdict 0 strict)"
check "閘門：1 是發現暴露，要擋（strict）" "block" "$(exposure_gate_verdict 1 strict)"
check "閘門：2 是無法判定，strict 之下要中止" "indeterminate" "$(exposure_gate_verdict 2 strict)"
check "閘門：2 在 lenient 之下降級成警告" "warn" "$(exposure_gate_verdict 2 lenient)"
# 兩者的差別就是這支函式存在的理由：同一個 2，部署要停、日常入口要繼續。
check "閘門：lenient 不會把 1 也放過" "block" "$(exposure_gate_verdict 1 lenient)"
check "閘門：非 0/1/2 是腳本自己壞了，不是環境" "broken" "$(exposure_gate_verdict 3 strict)"
check "閘門：沒有給 mode 時預設是 strict（安全的那一邊）" "indeterminate" "$(exposure_gate_verdict 2)"

# ═══════════════════════════════════════════════════════════
# nat_fronted_environment —— 這台的可見位址推論不出對外可達性
# ═══════════════════════════════════════════════════════════
# 四大雲的 sys_vendor／product_name 實測值。
check "NAT：AWS EC2" "AWS EC2" "$(nat_fronted_environment 'Amazon EC2' 'm5.large')"
check "NAT：Google Cloud" "Google Cloud" \
  "$(nat_fronted_environment 'Google' 'Google Compute Engine')"
check "NAT：Azure" "Azure 或 WSL2" \
  "$(nat_fronted_environment 'Microsoft Corporation' 'Virtual Machine')"
check "NAT：Oracle Cloud" "Oracle Cloud" \
  "$(nat_fronted_environment 'OracleCloud' 'Standard PC')"
# 這台機器（VMware）與一般實體機都**不該**命中 —— 它們的 `ip` 讀得到真相。
check "NAT：VMware 不算（本機就是這個）" "" \
  "$(nat_fronted_environment 'VMware, Inc.' 'VMware Virtual Platform')"
check "NAT：實體機不算" "" "$(nat_fronted_environment 'Dell Inc.' 'PowerEdge R740')"
check "NAT：讀不到 DMI 時不算（無知不是命中）" "" "$(nat_fronted_environment '' '')"
# **這一條守的是假失敗。** Surface 筆電的 sys_vendor 也是 "Microsoft
# Corporation"，只比對廠商的話，一台跑 Ubuntu 的 Surface 會被誤判成雲主機。
check "NAT：Surface 不算（廠商對但產品不對）" "" \
  "$(nat_fronted_environment 'Microsoft Corporation' 'Surface Laptop 5')"

# ═══════════════════════════════════════════════════════════
# wide_bind_verdict —— 綁在萬用位址時算不算對外開放
# ═══════════════════════════════════════════════════════════
# 參數：<is_codespace> <has_public> <ip_available> <nat_env>
check "萬用綁定：Codespaces" "codespace" \
  "$(wide_bind_verdict true false true '')"
check "萬用綁定：有公開位址 → 暴露" "exposed" \
  "$(wide_bind_verdict false true true '')"
check "萬用綁定：讀不到 ip → 無法判定" "unknown" \
  "$(wide_bind_verdict false false false '')"
check "萬用綁定：1:1 NAT 的雲 → 無法判定" "nat_fronted" \
  "$(wide_bind_verdict false false true 'AWS EC2')"
# 這一條是**極性沒有被翻轉**的證明：只有私有位址、讀得到 ip、又不是 1:1
# NAT —— 在這個組合下「不到 Internet」是推論得到的。翻掉它的話，每一台
# 在 NAT 後的家庭網路機器都會變成失敗（D-016）。
check "萬用綁定：只有私有位址且判定得出來 → lan_only（不是 unknown）" "lan_only" \
  "$(wide_bind_verdict false false true '')"
# 順序就是規格：Codespaces 跑在 Azure 上，所以 nat_env 也會命中 ——
# 順序顛倒的話，一個正確的 Codespaces 部署會變成「無法判定」。
check "萬用綁定：Codespaces 優先於 NAT 判定" "codespace" \
  "$(wide_bind_verdict true false true 'Azure 或 WSL2')"
# 有公開位址勝過一切不確定 —— 確定的事要先講。
check "萬用綁定：有公開位址優先於 NAT 判定" "exposed" \
  "$(wide_bind_verdict false true true 'AWS EC2')"
check "萬用綁定：有公開位址優先於讀不到 ip" "exposed" \
  "$(wide_bind_verdict false true false '')"

# ═══════════════════════════════════════════════════════════
# fresh_install_verdict —— 看的是「有沒有人類的資料」
# ═══════════════════════════════════════════════════════════
check "全新安裝：沒有帳號也沒有對話 → fresh" "fresh" "$(fresh_install_verdict 0 0 0)"
check "全新安裝：有帳號 → occupied" "occupied" "$(fresh_install_verdict 3 0 0)"
check "全新安裝：有對話（沒有帳號，例如只剩匯入的）→ occupied" "occupied" "$(fresh_install_verdict 0 7 0)"
check "全新安裝：有資料但 --keep-data → kept" "kept" "$(fresh_install_verdict 3 12 1)"
# 沒有資料時 keep 不該改變結論 —— 「繼續」在空的機器上就是「全新」。
check "全新安裝：空的機器加 --keep-data 仍然是 fresh" "fresh" "$(fresh_install_verdict 0 0 1)"

# ═══════════════════════════════════════════════════════════
# disk_verdict —— 唯一會硬擋的資源
# ═══════════════════════════════════════════════════════════
check "磁碟：遠高於需求 → sufficient" "sufficient" "$(disk_verdict 97 9)"
check "磁碟：剛好等於需求 → sufficient（門檻是 <，不是 <=）" "sufficient" "$(disk_verdict 9 9)"
check "磁碟：少 1GB → insufficient" "insufficient" "$(disk_verdict 8 9)"
check "磁碟：讀不到可用空間 → unknown，不是 insufficient" "unknown" "$(disk_verdict '' 9)"
check "磁碟：模型大小未知所以需求未知 → unknown" "unknown" "$(disk_verdict 97 '')"
check "磁碟：非數字的輸入不會被當成 0" "unknown" "$(disk_verdict '很多' 9)"

# ═══════════════════════════════════════════════════════════
# ram_verdict —— 只警告，不硬擋
# ═══════════════════════════════════════════════════════════
check "記憶體：充足 → ok" "ok" "$(ram_verdict 16000 4900)"
check "記憶體：剛好 → ok" "ok" "$(ram_verdict 4900 4900)"
check "記憶體：不足 → tight" "tight" "$(ram_verdict 3800 4900)"
check "記憶體：讀不到 → unknown" "unknown" "$(ram_verdict '' 4900)"
check "記憶體：非數字不會被當成 0（0 會變成 tight 這個假失敗）" "unknown" "$(ram_verdict 'lots' 4900)"

# ═══════════════════════════════════════════════════════════
# model_gb_estimate / disk_need_gb / ram_warn_mb
# ═══════════════════════════════════════════════════════════
check "模型大小：qwen3:4b 查得到" "2.5" "$(model_gb_estimate qwen3:4b)"
check "模型大小：表格外的模型 → unknown（不用 tag 猜）" "unknown" "$(model_gb_estimate llama3:70b)"
check "模型大小：空字串 → unknown" "unknown" "$(model_gb_estimate '')"

check "磁碟需求：2×模型 + 4GB" "9" "$(disk_need_gb 2.5)"
check "磁碟需求：模型大小未知 → 空（呼叫端據此走 unknown）" "" "$(disk_need_gb unknown)"

# KV cache 那一項是**估算值**，所以它只餵警告。這個數字同時也是那條警告
# 講得出「為什麼」的來源：8192 × 36KiB/1024 = 288MB。
check "記憶體需求：模型 2.5GB + 8k context 的 KV + 2GB 基底" "4896" "$(ram_warn_mb 2.5 8192)"
check "記憶體需求：模型大小未知 → 空" "" "$(ram_warn_mb unknown 8192)"

# ═══════════════════════════════════════════════════════════
# ctx_meets_mem0 —— 門檻的 +1 就是 D-027 的結論本身
# ═══════════════════════════════════════════════════════════
check "context：8192 足以讓 mem0 的抽取 prompt 完整進去" "yes" "$(ctx_meets_mem0 8192)"
# 8,100 是實測到最大的那個 prompt；門檻是「prompt tokens + 1」，所以 8,101
# 是**包含端點**。這一條與下一條合起來把邊界夾死 —— 只測 8192 測不出 off-by-one。
check "context：8101 剛好達到門檻（含端點）" "yes" "$(ctx_meets_mem0 8101)"
check "context：8100 差一個 token 就不夠（會砍掉 1 個 token）" "no" "$(ctx_meets_mem0 8100)"
check "context：4096 是 ollama 的舊預設，不夠" "no" "$(ctx_meets_mem0 4096)"
check "context：非數字 → no（不 throw、不當成夠大）" "no" "$(ctx_meets_mem0 '')"

# ═══════════════════════════════════════════════════════════
# ctx_holds_through_generation —— 第二個門檻（D-035）
#
# **這一組的每一條都必須與上面那一組對得起來：** 這個函式存在的理由就是
# 「過得了第一個門檻、過不了第二個」的那個區間（8,101～10,100）不是空的，
# 而 8192 就坐在裡面。所以除了夾邊界，還要**明確斷言 8192 是 yes→no 的
# 分界兩側**，否則這個函式可以被換成 ctx_meets_mem0 而測試全過。
# ═══════════════════════════════════════════════════════════
check "生成期間：16384（現在的預設）待得住" "yes" "$(ctx_holds_through_generation 16384)"
check "生成期間：10101 剛好達到門檻（含端點）" "yes" "$(ctx_holds_through_generation 10101)"
check "生成期間：10100 差一個 token 就不夠" "no" "$(ctx_holds_through_generation 10100)"
# 這一條是這個函式的存在理由：8192 **過得了** ctx_meets_mem0，卻**過不了**這裡。
# 兩句放在一起，才證明這是兩個不同的門檻而不是同一個寫了兩次。
check "生成期間：8192 進得去（第一個門檻）" "yes" "$(ctx_meets_mem0 8192)"
check "生成期間：8192 但待不住（第二個門檻）—— 這條就是 D-035" "no" "$(ctx_holds_through_generation 8192)"
check "生成期間：非數字 → no（不 throw、不當成夠大）" "no" "$(ctx_holds_through_generation '')"
check "生成期間：門檻常數 = 8,100 + 2,000 + 1" "10101" "$MEM0_ADD_HOLD_CTX"

# ═══════════════════════════════════════════════════════════
# num_ctx_verdict —— 參數錯誤要走 3，不是 2
# ═══════════════════════════════════════════════════════════
check "num_ctx：正常值" "ok 8192" "$(num_ctx_verdict 8192)"
check "num_ctx：非整數" "not_integer" "$(num_ctx_verdict 8k)"
check "num_ctx：空字串" "not_integer" "$(num_ctx_verdict '')"
check "num_ctx：512 是下限（含端點）" "ok 512" "$(num_ctx_verdict 512)"
check "num_ctx：511 太小" "too_small" "$(num_ctx_verdict 511)"
check "num_ctx：超過上限" "too_large" "$(num_ctx_verdict 1048577)"

# ═══════════════════════════════════════════════════════════
# env_upsert —— 改的是使用者唯一的設定檔，所以每一條都要測
# ═══════════════════════════════════════════════════════════
upsert() { printf '%s' "$2" | env_upsert "$1" "$3"; }

# 命令替換會吃掉結尾的換行，而**結尾有沒有換行對 .env 是有意義的**：
# 少了它，下一個鍵會黏在同一行上（`K=vNEXT=w`），而且 source 之後看起來
# 只是少了一個變數。所以碰到結尾的案例改用哨兵字元把結尾擋住再比。
upsert_raw() { printf '%s' "$2" | env_upsert "$1" "$3"; printf '⟨END⟩'; }

# 1. 既有鍵就地改值，位置不變（其他行原封不動）
check "env：既有鍵就地改值" \
  $'A=1\nWEBUI_BIND_ADDR=127.0.0.1\nB=2' \
  "$(upsert WEBUI_BIND_ADDR $'A=1\nWEBUI_BIND_ADDR=0.0.0.0\nB=2\n' 127.0.0.1)"

# 2. 沒有這個鍵就附加在最後
check "env：沒有的鍵附加到最後" \
  $'A=1\nWEBUI_BIND_ADDR=127.0.0.1' \
  "$(upsert WEBUI_BIND_ADDR $'A=1\n' 127.0.0.1)"

# 3. **重複的生效行收斂成恰好一行。** 這是這支函式存在的理由：.env 的重複鍵
#    是最後一行生效，所以「我改過了」可以與事實相反，而且是無聲的。
check "env：重複的生效行收斂成一行（在第一個的位置）" \
  $'A=1\nWEBUI_BIND_ADDR=127.0.0.1\nB=2' \
  "$(upsert WEBUI_BIND_ADDR $'A=1\nWEBUI_BIND_ADDR=0.0.0.0\nB=2\nWEBUI_BIND_ADDR=0.0.0.0\n' 127.0.0.1)"

# 4. 註解掉的行不算生效 —— 原樣留著，另外附加一行。
#    少了這一條，一個「把註解行也當成設定」的實作也會全綠，而那會讓
#    `.env.example` 裡那一堆說明用的註解被吃掉。
check "env：註解掉的行不算生效（且原樣保留）" \
  $'# WEBUI_BIND_ADDR=0.0.0.0\nA=1\nWEBUI_BIND_ADDR=127.0.0.1' \
  "$(upsert WEBUI_BIND_ADDR $'# WEBUI_BIND_ADDR=0.0.0.0\nA=1\n' 127.0.0.1)"

# 5. 前綴相同的鍵不可以被誤配
check "env：前綴相同的鍵不受影響" \
  $'WEBUI_BIND_ADDR_EXTRA=z\nWEBUI_BIND_ADDR=127.0.0.1' \
  "$(upsert WEBUI_BIND_ADDR $'WEBUI_BIND_ADDR_EXTRA=z\n' 127.0.0.1)"

# 6. 值裡面有 = 要完整保留
check "env：值裡的 = 完整保留" \
  'URL=a=b=c' \
  "$(upsert URL $'URL=x\n' 'a=b=c')"

# 7. 空的 .env（.env.example 被清空之類）也要能寫，而且結尾要有換行
check "env：空檔案，且結尾有換行" \
  $'K=v\n⟨END⟩' \
  "$(upsert_raw K '' v)"

# 8. 最後一行沒有換行時：不可以吃掉它，也不可以黏在一起（見 upsert_raw）
check "env：沒有結尾換行的檔案（會補上換行）" \
  $'A=1\nWEBUI_BIND_ADDR=127.0.0.1\n⟨END⟩' \
  "$(upsert_raw WEBUI_BIND_ADDR $'A=1\nWEBUI_BIND_ADDR=0.0.0.0' 127.0.0.1)"

# 8b. 純粹路過的行也要保持結尾換行（這是第三個輸出點，前兩條蓋不到）
check "env：純路過不改值的檔案也以換行結尾" \
  $'A=1\nK=v\n⟨END⟩' \
  "$(upsert_raw K $'A=1\n' v)"

# 9. **冪等** —— 部署失敗後會重跑，而它改的是唯一的設定檔。
#    這條也守著「位置不變」：第二次跑不可以把那一行搬到別的地方。
once="$(upsert WEBUI_BIND_ADDR $'A=1\nWEBUI_BIND_ADDR=0.0.0.0\nB=2\nWEBUI_BIND_ADDR=0.0.0.0\n' 127.0.0.1)"
check "env：跑兩次的結果與跑一次相同（冪等）" "$once" "$(upsert WEBUI_BIND_ADDR "$once" 127.0.0.1)"

# 9b. 冪等也要在「附加」那條路徑上成立（第二次跑不該再附加一行）
check "env：附加之後再跑一次不會多一行" \
  $'A=1\nK=v' \
  "$(upsert K "$(upsert K $'A=1\n' v)" v)"

# 10. 帶入的 key 是另一個 key 的前綴時，也不可以誤傷
check "env：key 本身是別的鍵的前綴時只改自己" \
  $'A=1\nAB=127.0.0.1' \
  "$(upsert AB $'A=1\nAB=x\n' 127.0.0.1)"

echo
if [[ "$FAILED" -gt 0 ]]; then
  fail "$FAILED 個案例沒過，$PASS 個過"
  exit 1
fi
ok "$PASS/$PASS 全過 —— 綁定預設、閘門四態、資源門檻與 .env 的每一條邊界都有測試守著"
