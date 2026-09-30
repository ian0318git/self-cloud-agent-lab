#!/usr/bin/env bash
# 讓 endpoint boot 不再宣稱一件它沒做到的事。
#
#   scripts/apply-endpoint-boot-honest-outcome.sh              # 套用（先備份、後驗證）
#   scripts/apply-endpoint-boot-honest-outcome.sh --dry-run    # 只檢查，不動任何檔案
#   scripts/apply-endpoint-boot-honest-outcome.sh --verify     # 對現況跑行為驗證
#   scripts/apply-endpoint-boot-honest-outcome.sh --revert     # 還原成最新的一份備份
#   scripts/apply-endpoint-boot-honest-outcome.sh --target F   # 改為操作 F（給測試用）
#
# 為什麼是補丁：endpoint/commands.py 屬於 endpoint-vps，由 uv tool 安裝在家目錄
# 底下，不在本 repo 內。補丁是唯一能讓這個修正「可重現、可稽核、可回退」的形式。
# 姊妹作是 apply-endpoint-tunnel-url-privacy.sh（切 B，動的是同一支檔案），以及
# apply-endpoint-stop-fixes.sh（CLI 本體的另一處）。
#
# **這支腳本動的是 package manager 目錄裡的檔案。** `uv tool upgrade endpoint-vps`
# 或任何重裝都會把修正蓋掉 —— 升級後請重跑一次。腳本認得原始檔的雜湊，所以升級後
# 會直接拒絕，不會把補丁硬套到新版上。
#
# ── 這一刀修的不是「網址拿不到」，是 boot 對這件事的說法 ──────────
# 2026-09-30 量的（D-072 第四節）：kernel 在跑的時候，ListKernelSessionOutput 回
# 200 但 log 是**空的**；網址要等 session 結束才出現，那時隧道已經沒了。於是
# run_boot 裡 `if success: if tunnel_url:` 這一層整個沒進去，而 boot 仍然印出
# 「Endpoint IS ONLINE」、仍然 exit 0。快取在 boot 開頭就被清了，所以跑完之後
# proxy 不知道隧道在哪、快取也不知道 —— **endpoint 是連不上的。**
#
# 同一條路上有兩句話是假的：「not in the kernel log yet」（那個 yet 永遠不會到）
# 和「Read it in a moment with: endpoint base-url」（那條指令走的就是這條讀不到
# 的路）。這支補丁把它們換成量到的事實，並讓成功路徑把「沒接線」講出來、然後
# 用 exit 2 收尾。
#
# **為什麼是 2 不是 1**：`sys.exit(1)` 在這支檔案裡已經專屬「kernel 沒起來」——
# 它會印 "Boot failed"、去爬 notebook 的 cell 錯誤、叫讀者開 Kaggle。共用它會把
# 讀者送去追不存在的錯誤。run_stop 已經立了 2 表示「沒有到達確定的好狀態」
# （commands.py:1574，"Exiting 2 (cannot determine)"），migrate-kaggle-token.sh
# 的 --check 也對 unknown 回 2。這是同一家族：kernel 起來了，接線沒有。
#
# ── 一個必須講清楚的後果 ────────────────────────────────────
# 因為網址目前**每一次** boot 都讀不到，這支補丁會讓 `endpoint boot` **每一次**都
# exit 2。這是預期效果，不是退化：只要切 B 沒解，endpoint 在 boot 之後就真的是沒
# 接線的，錯的是那個 0。本 repo 沒有任何腳本在程式上吃 boot 的離開碼（grep 過），
# 所以這個改變不會弄壞自家的工具鏈。
#
# ── 兩個方向的驗證是刻意的 ──────────────────────────────────
# 修補前**必須**驗證失敗（否則就沒有東西可修），修補後**必須**通過。
# 兩邊都過代表這支腳本在檢查空氣。驗證器另有突變測試在 D-073 那一輪跑過
# （八條突變，每條指名該被哪一個檢查編號抓到），但**沒有留成腳本** ——
# 這一點在 DECISIONS.md D-073 裡有記。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 載入原因只有一個：usage_text()，也就是 `--help` 要印的檔頭。
#
# **載入在頂層而不是在 handler 裡是刻意的。** lib.sh 就在同一層目錄，少了它的話
# 頂層載入會用「No such file」大聲失敗；改成在 handler 裡惰性載入則會**安靜地印出
# 空白、而且結束碼 0** —— test_usage_text.sh 的 C-對照組示範的就是這個假成功。
# 一個會安靜回報成功的 --help，比一個壞掉的 --help 更難發現。
source "$SCRIPT_DIR/lib.sh"

PATCH="$SCRIPT_DIR/endpoint-boot-honest-outcome.patch"
VERIFIER="$SCRIPT_DIR/test_endpoint_boot_honest_outcome.py"

COMMANDS_REL="endpoint/commands.py"

# 補丁是對這個版本建的。上游一換，這裡就會擋下來。
PRISTINE_SHA=abd6d3c9986daebd775431568459f39405838657ca0a9b08e182d61e9a8f7096
PATCHED_SHA=bcd0f287062a9603e273215bf18efdf7c379175c402179be608683f36a30a81d

TARGET=""
MODE=apply

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) MODE=dry ;;
        --verify)  MODE=verify ;;
        --revert)  MODE=revert ;;
        --target)  TARGET="${2:-}"; shift ;;
        -h|--help) usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "不認識的參數：$1" >&2; exit 2 ;;
    esac
    shift
done

die() { echo "✗ $*" >&2; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

find_target() {
    if [[ -n "$TARGET" ]]; then
        printf '%s\n' "$TARGET"
        return
    fi
    local base="${ENDPOINT_TOOL_DIR:-$HOME/.local/share/uv/tools/endpoint-vps}"
    local hit
    hit="$(find "$base" -path "*/site-packages/$COMMANDS_REL" -print -quit 2>/dev/null || true)"
    [[ -n "$hit" ]] || die "找不到 commands.py。請用 --target 指定，或設 ENDPOINT_TOOL_DIR。"
    printf '%s\n' "$hit"
}

# 在暫存樹裡重現補丁預期的目錄形狀（-p1 需要 endpoint/ 這一層），
# 讓 --dry-run 與實際套用走同一條路徑、用同一個嚴格度。
stage_copy() {
    local src="$1" dst="$2"
    mkdir -p "$dst/endpoint"
    cp "$src" "$dst/$COMMANDS_REL"
}

apply_patch_to() {
    # $1 = 暫存樹根（或安裝根）
    ( cd "$1" && patch -p1 --fuzz=0 < "$PATCH" )
}

# 硬連結預警。patch 會打斷連結，而 uv 快取裡的那幾份會留在舊內容。
link_report() {
    local f="$1" n
    n="$(stat -c '%h' "$f" 2>/dev/null || echo '?')"
    echo "  連結數：$n"
    if [[ "$n" =~ ^[0-9]+$ ]] && (( n > 1 )); then
        echo "  ⚠️  這是有 $n 條連結的檔案 —— patch 會打斷它們，"
        echo "      同 inode 的其他路徑會留在舊內容："
        find "$HOME" -xdev -samefile "$f" -not -path "$f" \
             -printf '        %p\n' 2>/dev/null || true
        echo "      重裝 endpoint-vps 會從那些副本還原，所以升級後請重跑本腳本。"
    fi
}

root_of() { printf '%s\n' "$(dirname "$(dirname "$1")")"; }

gen="$(find_target)"
[[ -f "$gen" ]] || die "目標不存在：$gen"
[[ -f "$PATCH" ]] || die "找不到補丁：$PATCH"

echo "目標：$gen"
echo "雜湊：$(sha "$gen")"
link_report "$gen"
echo

# --------------------------------------------------------------------------
# --revert
# --------------------------------------------------------------------------
if [[ "$MODE" == revert ]]; then
    latest="$(ls -1t "$gen".bak-* 2>/dev/null | head -1 || true)"
    [[ -n "$latest" ]] || die "找不到備份（$gen.bak-*），無法還原。"
    cp "$latest" "$gen"
    echo "✓ 已從 $(basename "$latest") 還原"
    echo "  雜湊：$(sha "$gen")"
    exit 0
fi

# --------------------------------------------------------------------------
# --verify：只跑行為驗證，不動檔案
# --------------------------------------------------------------------------
if [[ "$MODE" == verify ]]; then
    exec python3 "$VERIFIER" "$(root_of "$gen")"
fi

# --------------------------------------------------------------------------
# 前置檢查：兩個方向都必須成立
# --------------------------------------------------------------------------
current="$(sha "$gen")"

if [[ "$current" == "$PATCHED_SHA" ]]; then
    echo "這份檔案已經是修補後的版本，沒有事要做。"
    exit 0
fi

if [[ "$current" != "$PRISTINE_SHA" ]]; then
    die "目標既不是已知的原始版，也不是修補後的版本。
  預期原始版雜湊：$PRISTINE_SHA
  實得：          $current
  endpoint-vps 可能已升級 —— 請確認上游是否已自行修正，或重建補丁。"
fi

echo "--- 修補前：行為驗證（必須失敗，否則沒有東西可修）---"
if python3 "$VERIFIER" "$(root_of "$gen")" >/dev/null 2>&1; then
    die "原始版竟然通過了行為驗證 —— 補丁已無意義或驗證失效，停手。"
fi
python3 "$VERIFIER" "$(root_of "$gen")" | grep -E '^    [✗✓]|^  →' || true
echo

# --------------------------------------------------------------------------
# 空跑：同一條路徑、同一個嚴格度
# --------------------------------------------------------------------------
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
stage_copy "$gen" "$stage"

echo "--- 空跑（--fuzz=0）---"
if ! apply_patch_to "$stage" >/dev/null 2>&1; then
    die "補丁套不上去（context 不合）。沒有動到任何檔案。"
fi
staged_sha="$(sha "$stage/$COMMANDS_REL")"
[[ "$staged_sha" == "$PATCHED_SHA" ]] \
    || die "套用後的雜湊不符：預期 $PATCHED_SHA，實得 $staged_sha"
echo "✓ 套用後雜湊與驗證過的版本一致"
echo

if [[ "$MODE" == dry ]]; then
    echo "空跑完成，沒有動到任何檔案。"
    exit 0
fi

# --------------------------------------------------------------------------
# 實際套用
# --------------------------------------------------------------------------
backup="$gen.bak-$(date +%Y%m%d-%H%M%S)"
cp "$gen" "$backup"
echo "已備份 → $(basename "$backup")"

apply_patch_to "$(root_of "$gen")" >/dev/null

final="$(sha "$gen")"
if [[ "$final" != "$PATCHED_SHA" ]]; then
    cp "$backup" "$gen"
    die "套用後雜湊不符（實得 $final）。已自動還原。"
fi

echo "✓ 已套用"
echo

echo "--- 修補後：行為驗證（必須通過）---"
python3 "$VERIFIER" "$(root_of "$gen")" || {
    echo
    echo "✗ 套用後驗證未過 —— 已保留備份，可用下面這行還原：" >&2
    echo "    cp '$backup' '$gen'" >&2
    exit 1
}

echo
echo "還原方式： $0 --revert"
echo "升級 endpoint-vps 之後要重跑這支腳本（重裝會蓋掉修正）。"
