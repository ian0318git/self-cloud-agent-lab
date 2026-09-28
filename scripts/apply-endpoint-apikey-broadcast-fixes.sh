#!/usr/bin/env bash
# 讓 Kaggle notebook 不再把 API 金鑰發布到公開的 ntfy 主題上。
#
#   scripts/apply-endpoint-apikey-broadcast-fixes.sh              # 套用（先備份、後驗證）
#   scripts/apply-endpoint-apikey-broadcast-fixes.sh --dry-run    # 只檢查，不動任何檔案
#   scripts/apply-endpoint-apikey-broadcast-fixes.sh --verify     # 對現況跑行為驗證
#   scripts/apply-endpoint-apikey-broadcast-fixes.sh --revert     # 還原成最新的一份備份
#   scripts/apply-endpoint-apikey-broadcast-fixes.sh --target F   # 改為操作 F（給測試用）
#
# 為什麼是補丁：engine/engine.py 屬於 endpoint-vps，由 uv tool 安裝在家目錄底下，
# 不在本 repo 內；而它是 notebook 的**生產者** —— 產生器把整個檔案 base64 嵌進
# notebook（master_build_notebook.py:331 讀 REPO_ROOT/engine/engine.py，而安裝環境
# 下 REPO_ROOT 就是 site-packages）。補丁是唯一能讓這個修正「可重現、可稽核、
# 可回退」的形式。姊妹作是 apply-endpoint-ntfy-fixes.sh（改產生器）與
# apply-endpoint-stop-fixes.sh（改 CLI 本體）。
#
# **這支腳本動的是 package manager 目錄裡的檔案。** `uv tool upgrade endpoint-vps`
# 或任何重裝都會把修正蓋掉 —— 升級後請重跑一次。腳本認得原始檔的雜湊，所以升級後
# 會直接拒絕，不會把補丁硬套到新版上。
#
# 多一層的是硬連結：engine.py 由 uv 快取硬連結而來，`patch` 以「暫時檔 ＋ rename」
# 寫入會**打斷那條連結**。功能上沒有影響（產生器讀的就是我們改的這一份），但快取
# 裡的那幾份會留在舊內容，**下次重裝會把它們還原回去**。所以空跑時會把連結數與同
# inode 的其他路徑印出來當預警。這裡**刻意不自動修**：改快取等於動 uv 的私有狀態，
# 而它的處方與「升級後重跑本腳本」完全相同。
#
# 兩個方向的驗證是刻意的：修補前**必須**驗證失敗（否則就沒有東西可修），
# 修補後**必須**通過。兩邊都過代表這支腳本在檢查空氣。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 載入原因只有一個：usage_text()，也就是 `--help` 要印的檔頭。
#
# **載入在頂層而不是在 handler 裡是刻意的。** lib.sh 就在同一層目錄，少了它的話
# 頂層載入會用「No such file」大聲失敗；改成在 handler 裡惰性載入則會**安靜地印出
# 空白、而且結束碼 0** —— test_usage_text.sh 的 C-對照組示範的就是這個假成功。
# 一個會安靜回報成功的 --help，比一個壞掉的 --help 更難發現。
source "$SCRIPT_DIR/lib.sh"

PATCH="$SCRIPT_DIR/endpoint-apikey-broadcast-fixes.patch"
VERIFIER="$SCRIPT_DIR/test_endpoint_apikey_broadcast_fixes.py"

ENGINE_REL="engine/engine.py"

# 補丁是對這個版本建的。上游一換，這裡就會擋下來。
PRISTINE_SHA=871b4cab9bcca6bb4860c171c3987b8796374e3bacfb35ff04937fe70ae4475b
PATCHED_SHA=68dfa5a26614e1606c9f489c2288b0ea0071c6a4fc891a524a635e69f99e653e

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
    hit="$(find "$base" -path "*/site-packages/$ENGINE_REL" -print -quit 2>/dev/null || true)"
    [[ -n "$hit" ]] || die "找不到引擎。請用 --target 指定，或設 ENDPOINT_TOOL_DIR。"
    printf '%s\n' "$hit"
}

# 在暫存樹裡重現補丁預期的目錄形狀（-p1 需要 engine/ 這一層），
# 讓 --dry-run 與實際套用走同一條路徑、用同一個嚴格度。
stage_copy() {
    local src="$1" dst="$2"
    mkdir -p "$dst/engine"
    cp "$src" "$dst/$ENGINE_REL"
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
staged_sha="$(sha "$stage/$ENGINE_REL")"
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
