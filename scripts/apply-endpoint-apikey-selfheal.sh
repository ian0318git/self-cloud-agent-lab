#!/usr/bin/env bash
# 第四刀：`/v1/apikey` 離開免認證清單，而 CLI 的自癒改讀本機真本（D-070）。
#
#   scripts/apply-endpoint-apikey-selfheal.sh              # 套用（先備份、後驗證）
#   scripts/apply-endpoint-apikey-selfheal.sh --dry-run    # 只檢查，不動任何檔案
#   scripts/apply-endpoint-apikey-selfheal.sh --verify     # 對現況跑行為驗證
#   scripts/apply-endpoint-apikey-selfheal.sh --revert     # 還原成最新的一份備份
#   scripts/apply-endpoint-apikey-selfheal.sh --target F   # 改為操作 F（給測試用）
#
# 為什麼是補丁：engine/engine.py 與 endpoint/commands.py 都屬於 endpoint-vps，
# 由 uv tool 安裝在家目錄底下，不在本 repo 內；而 engine.py 是 notebook 的
# **生產者** —— 產生器把整個檔案 base64 嵌進 notebook。補丁是唯一能讓這個修正
# 「可重現、可稽核、可回退」的形式。姊妹作是 apply-endpoint-apikey-broadcast-fixes.sh
# （切 A）、apply-endpoint-tunnel-url-privacy.sh（切 B）、apply-endpoint-topic-secret.sh
# （切 C）。
#
# **這一支是家族裡第一個動兩個檔案的補丁。** 單檔補丁不需要的守衛因此多了一個：
# 兩個檔案必須**同時**是原始版或**同時**是修補版。一份被套用、另一份沒有，是這類
# 修正最危險的狀態 —— 免認證清單被關掉了，而自癒還在走那條已經走不通的路，外表
# 看不出來。這種樹會被直接拒絕，不會被「套用到剩下那一半」。
#
# **這支腳本動的是 package manager 目錄裡的檔案。** `uv tool upgrade endpoint-vps`
# 或任何重裝都會把修正蓋掉 —— 升級後請重跑一次。腳本認得兩個檔案在兩個狀態下的
# 雜湊，所以升級後會直接拒絕，不會把補丁硬套到新版上。
#
# 多一層的是硬連結：這兩個檔案由 uv 快取硬連結而來，`patch` 以「暫時檔 ＋ rename」
# 寫入會**打斷那條連結**。功能上沒有影響，但快取裡的那幾份會留在舊內容，**下次
# 重裝會把它們還原回去**。所以空跑時會把連結數與同 inode 的其他路徑印出來當預警。
# 這裡**刻意不自動修**：改快取等於動 uv 的私有狀態，而它的處方與「升級後重跑本
# 腳本」完全相同。
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

PATCH="$SCRIPT_DIR/endpoint-apikey-selfheal.patch"
VERIFIER="$SCRIPT_DIR/test_endpoint_apikey_selfheal.py"

# 補丁涵蓋的兩個檔案，以及它們在兩個狀態下的雜湊。
# 補丁是對這兩個版本建的。上游一換，這裡就會擋下來。
FILES=(engine/engine.py endpoint/commands.py)
PRISTINE=(
    68dfa5a26614e1606c9f489c2288b0ea0071c6a4fc891a524a635e69f99e653e
    19b8129ee4c9b7f644bae3ec2f5e4c038b2886f128bb838b27a0b55bb86b0eb5
)
PATCHED=(
    1930dda18ce87e8e0461994fbdd10b99dd23942fd7dd82b561f600aa6c4cbafd
    abd6d3c9986daebd775431568459f39405838657ca0a9b08e182d61e9a8f7096
)

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

# 安裝根：兩個檔案共同的那一層（site-packages）。
#
# --target 兩種都收：給目錄就當安裝根，給檔案就取它的安裝根。單檔補丁的姊妹作
# 收的是檔案路徑，既有的測試腳本沿用那個形狀；兩種都認才不會有人在這裡踩空。
find_target() {
    if [[ -n "$TARGET" ]]; then
        if [[ -d "$TARGET" ]]; then
            printf '%s\n' "$TARGET"
        else
            # .../site-packages/engine/engine.py -> .../site-packages
            printf '%s\n' "$(dirname "$(dirname "$TARGET")")"
        fi
        return
    fi
    local base="${ENDPOINT_TOOL_DIR:-$HOME/.local/share/uv/tools/endpoint-vps}"
    local hit
    hit="$(find "$base" -path "*/site-packages/${FILES[0]}" -print -quit 2>/dev/null || true)"
    [[ -n "$hit" ]] || die "找不到引擎。請用 --target 指定，或設 ENDPOINT_TOOL_DIR。"
    # .../site-packages/engine/engine.py -> .../site-packages
    printf '%s\n' "$(dirname "$(dirname "$hit")")"
}

# 在暫存樹裡重現補丁預期的目錄形狀（-p1 需要 engine/ 與 endpoint/ 這兩層），
# 讓 --dry-run 與實際套用走同一條路徑、用同一個嚴格度。
stage_copy() {
    local src="$1" dst="$2" rel
    for rel in "${FILES[@]}"; do
        mkdir -p "$dst/$(dirname "$rel")"
        cp "$src/$rel" "$dst/$rel"
    done
}

apply_patch_to() {
    # $1 = 暫存樹根（或安裝根）
    ( cd "$1" && patch -p1 --fuzz=0 < "$PATCH" )
}

# 硬連結預警。patch 會打斷連結，而 uv 快取裡的那幾份會留在舊內容。
link_report() {
    local f="$1" n
    n="$(stat -c '%h' "$f" 2>/dev/null || echo '?')"
    echo "  $(basename "$f") 連結數：$n"
    if [[ "$n" =~ ^[0-9]+$ ]] && (( n > 1 )); then
        echo "  ⚠️  這是有 $n 條連結的檔案 —— patch 會打斷它們，"
        echo "      同 inode 的其他路徑會留在舊內容："
        find "$HOME" -xdev -samefile "$f" -not -path "$f" \
             -printf '        %p\n' 2>/dev/null || true
        echo "      重裝 endpoint-vps 會從那些副本還原，所以升級後請重跑本腳本。"
    fi
}

root="$(find_target)"
[[ -d "$root" ]] || die "目標不存在：$root"
[[ -f "$PATCH" ]] || die "找不到補丁：$PATCH"
[[ -f "$VERIFIER" ]] || die "找不到驗證器：$VERIFIER"

echo "目標：$root"
for i in "${!FILES[@]}"; do
    f="$root/${FILES[$i]}"
    [[ -f "$f" ]] || die "目標檔案不存在：$f"
    echo "  ${FILES[$i]}  雜湊：$(sha "$f")"
done
for i in "${!FILES[@]}"; do
    link_report "$root/${FILES[$i]}"
done
echo

# --------------------------------------------------------------------------
# --revert：兩個檔案一起還原
# --------------------------------------------------------------------------
if [[ "$MODE" == revert ]]; then
    for rel in "${FILES[@]}"; do
        f="$root/$rel"
        latest="$(ls -1t "$f".bak-* 2>/dev/null | head -1 || true)"
        [[ -n "$latest" ]] || die "找不到備份（$f.bak-*），無法還原。"
        cp "$latest" "$f"
        echo "✓ $rel 已從 $(basename "$latest") 還原"
        echo "  雜湊：$(sha "$f")"
    done
    exit 0
fi

# --------------------------------------------------------------------------
# --verify：只跑行為驗證，不動檔案
# --------------------------------------------------------------------------
if [[ "$MODE" == verify ]]; then
    exec python3 "$VERIFIER" "$root"
fi

# --------------------------------------------------------------------------
# 前置檢查：兩個檔案必須同時在同一邊
# --------------------------------------------------------------------------
declare -a state=()
for i in "${!FILES[@]}"; do
    cur="$(sha "$root/${FILES[$i]}")"
    if   [[ "$cur" == "${PATCHED[$i]}"  ]]; then state+=(patched)
    elif [[ "$cur" == "${PRISTINE[$i]}" ]]; then state+=(pristine)
    else state+=(unknown)
    fi
done

# 全修補 = 沒事做；全原始 = 可以套用；其餘（含半套用）= 拒絕。
want_patched=""; want_pristine=""
for _ in "${FILES[@]}"; do want_patched+="patched "; want_pristine+="pristine "; done
want_patched="${want_patched% }"; want_pristine="${want_pristine% }"

if [[ "${state[*]}" == "$want_patched" ]]; then
    echo "這兩個檔案都已經是修補後的版本，沒有事要做。"
    exit 0
fi

# 半套用 = 最危險的狀態，直接拒絕
if [[ "${state[*]}" != "$want_pristine" ]]; then
    die "兩個檔案不在同一個狀態上，拒絕套用。
  這是最危險的狀態：免認證清單若已關閉，而自癒還在走那條走不通的路，
  外表看不出來。請先用 --revert 還原，或手動確認後再處理。
  狀態：${FILES[0]}=${state[0]}、${FILES[1]}=${state[1]}
  預期原始版雜湊：${PRISTINE[0]} / ${PRISTINE[1]}
  實得：          $(sha "$root/${FILES[0]}") / $(sha "$root/${FILES[1]}")"
fi

echo "--- 修補前：行為驗證（必須失敗，否則沒有東西可修）---"
if python3 "$VERIFIER" "$root" >/dev/null 2>&1; then
    die "原始版竟然通過了行為驗證 —— 補丁已無意義或驗證失效，停手。"
fi
python3 "$VERIFIER" "$root" | grep -E '^    [✗✓]|^  →' || true
echo

# --------------------------------------------------------------------------
# 空跑：同一條路徑、同一個嚴格度
# --------------------------------------------------------------------------
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
stage_copy "$root" "$stage"

echo "--- 空跑（--fuzz=0）---"
if ! apply_patch_to "$stage" >/dev/null 2>&1; then
    die "補丁套不上去（context 不合）。沒有動到任何檔案。"
fi
for i in "${!FILES[@]}"; do
    staged_sha="$(sha "$stage/${FILES[$i]}")"
    [[ "$staged_sha" == "${PATCHED[$i]}" ]] \
        || die "${FILES[$i]} 套用後的雜湊不符：預期 ${PATCHED[$i]}，實得 $staged_sha"
    echo "✓ ${FILES[$i]} 套用後雜湊與驗證過的版本一致"
done
echo

if [[ "$MODE" == dry ]]; then
    echo "空跑完成，沒有動到任何檔案。"
    exit 0
fi

# --------------------------------------------------------------------------
# 實際套用：兩個檔案先各備份，任何一個雜湊不符就一起還原
# --------------------------------------------------------------------------
stamp="$(date +%Y%m%d-%H%M%S)"
declare -a backups=()
for rel in "${FILES[@]}"; do
    b="$root/$rel.bak-$stamp"
    cp "$root/$rel" "$b"
    backups+=("$b")
    echo "已備份 → $(basename "$b")"
done

rollback() {
    local why="$1"
    for i in "${!FILES[@]}"; do cp "${backups[$i]}" "$root/${FILES[$i]}"; done
    die "$why。兩個檔案都已自動還原。"
}

apply_patch_to "$root" >/dev/null || rollback "patch 執行失敗"

for i in "${!FILES[@]}"; do
    final="$(sha "$root/${FILES[$i]}")"
    [[ "$final" == "${PATCHED[$i]}" ]] \
        || rollback "套用後 ${FILES[$i]} 雜湊不符（實得 $final）"
done

echo "✓ 已套用（兩個檔案）"
echo

echo "--- 修補後：行為驗證（必須通過）---"
python3 "$VERIFIER" "$root" || {
    echo
    echo "✗ 套用後驗證未過 —— 已保留備份，可用下面這行還原：" >&2
    for b in "${backups[@]}"; do
        echo "    cp '$b' '${b%.bak-*}'" >&2
    done
    exit 1
}

echo
echo "還原方式： $0 --revert"
echo "升級 endpoint-vps 之後要重跑這支腳本（重裝會蓋掉修正）。"
