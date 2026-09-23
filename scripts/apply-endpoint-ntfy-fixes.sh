#!/usr/bin/env bash
# 把 ntfy 兩項修正套用到 endpoint CLI 的 notebook 產生器。
#
#   scripts/apply-endpoint-ntfy-fixes.sh              # 套用（先備份、後驗證）
#   scripts/apply-endpoint-ntfy-fixes.sh --dry-run    # 只檢查，不動任何檔案
#   scripts/apply-endpoint-ntfy-fixes.sh --verify     # 對現況跑行為驗證
#   scripts/apply-endpoint-ntfy-fixes.sh --revert     # 還原成最新的一份備份
#   scripts/apply-endpoint-ntfy-fixes.sh --target F   # 改為操作 F（給測試用）
#
# 為什麼是補丁：master_build_notebook.py 屬於 endpoint-vps，由 uv tool 安裝在
# 家目錄底下，不在本 repo 內。筆記本是它產出的，所以它是唯一要改的地方；而
# 補丁是唯一能讓這個修正「可重現、可稽核、可回退」的形式。
#
# **這支腳本動的是 package manager 目錄裡的檔案。** `uv tool upgrade
# endpoint-vps` 或任何重裝都會把修正蓋掉 —— 升級後請重跑一次。腳本認得
# 原始檔的雜湊，所以升級後會直接拒絕，不會把補丁硬套到新版上。
#
# 兩個方向的驗證是刻意的：修補前**必須**驗證失敗（否則就沒有東西可修），
# 修補後**必須**通過。兩邊都過代表這支腳本在檢查空氣。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$SCRIPT_DIR/endpoint-ntfy-fixes.patch"
VERIFIER="$SCRIPT_DIR/test_endpoint_ntfy_fixes.py"

# 補丁是對這個版本建的。上游一換，這裡就會擋下來。
PRISTINE_SHA=1d927451d80f40d13d252ef3a10a4c21838b21660f63c47349798197a07f7e67
PATCHED_SHA=b416bd476226a58de35cc69c1242bae2897655a41137a784dcc754d3e6c14f86

TARGET=""
MODE=apply

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) MODE=dry ;;
        --verify)  MODE=verify ;;
        --revert)  MODE=revert ;;
        --target)  TARGET="${2:-}"; shift ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
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
    hit="$(find "$base" -path '*/site-packages/scripts/master_build_notebook.py' \
             -print -quit 2>/dev/null || true)"
    [[ -n "$hit" ]] || die "找不到產生器。請用 --target 指定，或設 ENDPOINT_TOOL_DIR。"
    printf '%s\n' "$hit"
}

# 在暫存樹裡重現補丁預期的目錄形狀（-p1 需要 scripts/ 這一層），
# 讓 --dry-run 與實際套用走同一條路徑、用同一個嚴格度。
stage_copy() {
    local src="$1" dst="$2"
    mkdir -p "$dst/scripts"
    cp "$src" "$dst/scripts/master_build_notebook.py"
}

apply_patch_to() {
    # $1 = 暫存樹根
    ( cd "$1" && patch -p1 --fuzz=0 < "$PATCH" )
}

gen="$(find_target)"
[[ -f "$gen" ]] || die "目標不存在：$gen"
[[ -f "$PATCH" ]] || die "找不到補丁：$PATCH"

echo "目標：$gen"
echo "雜湊：$(sha "$gen")"
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
    exec python3 "$VERIFIER" "$gen"
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
if python3 "$VERIFIER" "$gen" >/dev/null 2>&1; then
    die "原始版竟然通過了行為驗證 —— 補丁已無意義或驗證失效，停手。"
fi
python3 "$VERIFIER" "$gen" | grep -E '^    [✗✓]|^  →' || true
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
staged_sha="$(sha "$stage/scripts/master_build_notebook.py")"
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

apply_patch_to "$(dirname "$(dirname "$gen")")" >/dev/null

final="$(sha "$gen")"
if [[ "$final" != "$PATCHED_SHA" ]]; then
    cp "$backup" "$gen"
    die "套用後雜湊不符（實得 $final）。已自動還原。"
fi

echo "✓ 已套用"
echo

echo "--- 修補後：行為驗證（必須通過）---"
python3 "$VERIFIER" "$gen" || {
    echo
    echo "✗ 套用後驗證未過 —— 已保留備份，可用下面這行還原：" >&2
    echo "    cp '$backup' '$gen'" >&2
    exit 1
}

echo
echo "還原方式： $0 --revert"
echo "升級 endpoint-vps 之後要重跑這支腳本（重裝會蓋掉修正）。"
