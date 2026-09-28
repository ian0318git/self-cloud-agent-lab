#!/usr/bin/env bash
# 把 tunnel 網址從公開 ntfy 主題上取下來的修正套用到 endpoint 本體（切 B）。
#
#   scripts/apply-endpoint-tunnel-url-privacy.sh            # 套用（先備份、後驗證）
#   scripts/apply-endpoint-tunnel-url-privacy.sh --dry-run  # 只檢查，不動任何檔案
#   scripts/apply-endpoint-tunnel-url-privacy.sh --verify   # 對現況跑行為驗證
#   scripts/apply-endpoint-tunnel-url-privacy.sh --revert   # 還原成最新的一份備份
#   scripts/apply-endpoint-tunnel-url-privacy.sh --target D # 改為操作 D（D 是 site-packages 目錄）
#
# 為什麼是補丁：三個目標都不在本 repo 內 —— `endpoint/core.py` 與
# `endpoint/commands.py` 由 uv tool 安裝在家目錄底下，`scripts/master_build_notebook.py`
# 是產生 Kaggle notebook 的產生器。補丁是唯一能讓這個修正「可重現、可稽核、
# 可回退」的形式。
#
# **這支腳本動的是 package manager 目錄裡的檔案。** `uv tool upgrade
# endpoint-vps` 或任何重裝都會把修正蓋掉 —— 升級後請重跑一次。腳本認得原始檔
# 的雜湊，所以升級後會直接拒絕，不會把補丁硬套到新版上。
#
# ⚠️ **套用順序：ntfy-fixes → stop-fixes → 本補丁。** 本補丁的三個目標裡有兩個
# （core.py、commands.py）已經被 stop-fixes 改過，第三個（master_build_notebook.py）
# 已經被 ntfy-fixes 改過，所以下面釘的「原始版雜湊」其實是**那些修正之後**的狀態。
# 這代表 —— **先還原本補丁，才能還原那兩支**。直接跑
# `apply-endpoint-stop-fixes.sh --revert` 會把本補丁在 core.py／commands.py 上的
# 改動一起蓋掉，而且不會有任何警告（它認的是它自己的 patched 雜湊，還原後就回到
# 它眼中的 pristine）。這是這個 repo 裡補丁互相疊加時固有的風險，不是本腳本
# 能擋掉的；要擋只能靠順序。
#
# 這個補丁橫跨**三個檔案**，所以比姊妹作多三件事：釘六個雜湊；三個檔案備份在
# 同一個時間章節下、一起還原（只還原一部分會留下一個拼接過的樹）；而**三個檔案
# 狀態不一致時直接停手**，不試著補完。那個狀態只會來自中斷的套用或手動編輯，
# 而一份橫跨三檔的 diff 本來就不會只套上其中一部分 —— 猜測要補哪一邊，比請人
# 還原後重跑更危險。
#
# 兩個方向的驗證是刻意的：修補前**必須**驗證失敗（否則就沒有東西可修），
# 修補後**必須**通過。兩邊都過代表這支腳本在檢查空氣。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 載入原因只有一個：usage_text()，也就是 `--help` 要印的檔頭。在頂層載入是
# 刻意的 —— 惰性載入會安靜地印出空白、而且結束碼 0。理由與姊妹作相同，那邊
# 有完整的說明。
source "$SCRIPT_DIR/lib.sh"

PATCH="$SCRIPT_DIR/endpoint-tunnel-url-privacy.patch"
VERIFIER="$SCRIPT_DIR/test_endpoint_tunnel_url_privacy.py"

NB_REL="scripts/master_build_notebook.py"
CORE_REL="endpoint/core.py"
COMMANDS_REL="endpoint/commands.py"

# 補丁是對這個版本建的。上游一換，這裡就會擋下來。
# （core.py 與 commands.py 的「原始版」＝ stop-fixes 套用後的狀態。）
NB_PRISTINE_SHA=b416bd476226a58de35cc69c1242bae2897655a41137a784dcc754d3e6c14f86
NB_PATCHED_SHA=a044901445ea5fd59dcb59e91e7a37c394b26dc952ad646c48b8bdddfd370cd8
CORE_PRISTINE_SHA=2e8692b1c199c3ca442f6da92d77be24f8104c5bec207b0da5c0679bc9581251
CORE_PATCHED_SHA=8c8afbb9e3da2b03eb6ca2adf9152321409d442a737faddce36fbe6b926ebc86
COMMANDS_PRISTINE_SHA=d8b7cd8140be63a2027f3f18563f0b7da683b9e2b556183ec8ce0f01b10e14a7
COMMANDS_PATCHED_SHA=8d6f2adeb8d070b080b88b04f60bc37ff528ffa859e5b518286a2ba962eb91d3

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
    [[ -n "$hit" ]] || die "找不到 endpoint 套件。請用 --target 指定，或設 ENDPOINT_TOOL_DIR。"
    # .../site-packages/endpoint/commands.py → 往上兩層就是 site-packages 根。
    printf '%s\n' "$(dirname "$(dirname "$hit")")"
}

# 在暫存樹裡重現補丁預期的目錄形狀（-p1 需要 endpoint/ 與 scripts/ 這兩層），
# 讓 --dry-run 與實際套用走同一條路徑、用同一個嚴格度。
stage_copy() {
    local src="$1" dst="$2"
    mkdir -p "$dst/endpoint" "$dst/scripts"
    cp "$src/$NB_REL" "$dst/$NB_REL"
    cp "$src/$CORE_REL" "$dst/$CORE_REL"
    cp "$src/$COMMANDS_REL" "$dst/$COMMANDS_REL"
}

apply_patch_to() {
    # $1 = 暫存樹根（或安裝根）
    ( cd "$1" && patch -p1 --fuzz=0 < "$PATCH" )
}

state_of() {
    # $1=檔案 $2=原始版雜湊 $3=修補後雜湊 → pristine | patched | unknown:<雜湊>
    local s
    s="$(sha "$1")"
    if   [[ "$s" == "$2" ]]; then printf 'pristine\n'
    elif [[ "$s" == "$3" ]]; then printf 'patched\n'
    else printf 'unknown:%s\n' "$s"
    fi
}

revert_trio() {
    local nb="$1" core="$2" commands="$3"
    shopt -s nullglob
    local baks=("$nb".bak-*)
    shopt -u nullglob
    ((${#baks[@]})) || die "找不到備份（$nb.bak-*），無法還原。"

    local stamp f ok
    # 從最新的章節往回找，只接受**三個檔案都齊**的那一組。
    while read -r stamp; do
        [[ -n "$stamp" ]] || continue
        ok=1
        for f in "$nb" "$core" "$commands"; do
            [[ -f "$f.bak-$stamp" ]] || ok=0
        done
        ((ok)) || continue
        for f in "$nb" "$core" "$commands"; do
            cp "$f.bak-$stamp" "$f"
        done
        echo "✓ 已從章節 $stamp 還原三個檔案"
        echo "  master_build_notebook.py  $(sha "$nb")"
        echo "  core.py                   $(sha "$core")"
        echo "  commands.py               $(sha "$commands")"
        echo
        echo "注意：這只還原本補丁。core.py 與 commands.py 會回到 stop-fixes 套用後的"
        echo "      狀態（本補丁的「原始版」），master_build_notebook.py 會回到"
        echo "      ntfy-fixes 套用後的狀態。"
        exit 0
    done < <(printf '%s\n' "${baks[@]}" | sed 's/.*\.bak-//' | sort -ru)

    die "每一份備份都湊不齊三個檔案 —— 不敢只還原一部分。"
}

root="$(find_target)"
nb="$root/$NB_REL"
core="$root/$CORE_REL"
commands="$root/$COMMANDS_REL"
[[ -f "$nb" ]]       || die "目標不存在：$nb"
[[ -f "$core" ]]     || die "目標不存在：$core"
[[ -f "$commands" ]] || die "目標不存在：$commands"
[[ -f "$PATCH" ]]    || die "找不到補丁：$PATCH"

echo "目標：$root"
echo "  master_build_notebook.py  $(sha "$nb")"
echo "  core.py                   $(sha "$core")"
echo "  commands.py               $(sha "$commands")"
echo

# --------------------------------------------------------------------------
# --revert
# --------------------------------------------------------------------------
if [[ "$MODE" == revert ]]; then
    revert_trio "$nb" "$core" "$commands"
fi

# --------------------------------------------------------------------------
# --verify：只跑行為驗證，不動檔案
# --------------------------------------------------------------------------
if [[ "$MODE" == verify ]]; then
    exec python3 "$VERIFIER" "$root"
fi

# --------------------------------------------------------------------------
# 前置檢查：三個檔案都必須落在已知的兩個版本上，而且狀態一致
# --------------------------------------------------------------------------
nb_state="$(state_of "$nb" "$NB_PRISTINE_SHA" "$NB_PATCHED_SHA")"
core_state="$(state_of "$core" "$CORE_PRISTINE_SHA" "$CORE_PATCHED_SHA")"
cmds_state="$(state_of "$commands" "$COMMANDS_PRISTINE_SHA" "$COMMANDS_PATCHED_SHA")"

if [[ "$nb_state" == unknown:* ]]; then
    die "master_build_notebook.py 既不是已知的原始版，也不是修補後的版本。
  預期原始版雜湊：$NB_PRISTINE_SHA
  實得：          ${nb_state#unknown:}
  endpoint-vps 可能已升級，或 ntfy-fixes 尚未套用（本補丁疊在它上面）——
  請先跑 scripts/apply-endpoint-ntfy-fixes.sh，或確認上游是否已自行修正。"
fi

if [[ "$core_state" == unknown:* ]]; then
    die "core.py 既不是已知的原始版，也不是修補後的版本。
  預期原始版雜湊：$CORE_PRISTINE_SHA
  實得：          ${core_state#unknown:}
  endpoint-vps 可能已升級，或 stop-fixes 尚未套用（本補丁疊在它上面）——
  請先跑 scripts/apply-endpoint-stop-fixes.sh，或確認上游是否已自行修正。"
fi

if [[ "$cmds_state" == unknown:* ]]; then
    die "commands.py 既不是已知的原始版，也不是修補後的版本。
  預期原始版雜湊：$COMMANDS_PRISTINE_SHA
  實得：          ${cmds_state#unknown:}
  endpoint-vps 可能已升級，或 stop-fixes 尚未套用（本補丁疊在它上面）——
  請先跑 scripts/apply-endpoint-stop-fixes.sh，或確認上游是否已自行修正。"
fi

if [[ "$nb_state" == patched && "$core_state" == patched && "$cmds_state" == patched ]]; then
    echo "三個檔案都已經是修補後的版本，沒有事要做。"
    exit 0
fi

# 全 pristine 或全 patched 之外，只要有一個不是 patched，就得是 pristine。
if [[ "$nb_state" != "$core_state" || "$core_state" != "$cmds_state" ]]; then
    die "三個檔案的狀態不一致（notebook：$nb_state、core.py：$core_state、commands.py：$cmds_state）。
  這只會來自一次中斷的套用，或手動編輯 —— 一份橫跨三個檔案的 diff 不會只
  套上其中一部分。請先還原成一致的狀態再重跑：
      $0 --revert
  若 --revert 說找不到完整的備份章節，重新安裝一次即可：
      uv tool install --force endpoint-vps"
fi

# 到這裡三個檔案都是原始版，所以反向閘門的結論是有意義的。
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
for pair in "$NB_REL:$NB_PATCHED_SHA" "$CORE_REL:$CORE_PATCHED_SHA" "$COMMANDS_REL:$COMMANDS_PATCHED_SHA"; do
    rel="${pair%%:*}"
    want="${pair#*:}"
    got="$(sha "$stage/$rel")"
    [[ "$got" == "$want" ]] || die "$rel 套用後的雜湊不符：預期 $want，實得 $got"
done
echo "✓ 三個檔案套用後的雜湊都與驗證過的版本一致"
echo

if [[ "$MODE" == dry ]]; then
    echo "空跑完成，沒有動到任何檔案。"
    exit 0
fi

# --------------------------------------------------------------------------
# 實際套用
# --------------------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
nb_bak="$nb.bak-$STAMP"
core_bak="$core.bak-$STAMP"
cmds_bak="$commands.bak-$STAMP"
cp "$nb" "$nb_bak"
cp "$core" "$core_bak"
cp "$commands" "$cmds_bak"
echo "已備份 → $(basename "$nb_bak")、$(basename "$core_bak")、$(basename "$cmds_bak")"

apply_patch_to "$root" >/dev/null

mismatch=""
[[ "$(sha "$nb")" == "$NB_PATCHED_SHA" ]]             || mismatch="master_build_notebook.py"
[[ "$(sha "$core")" == "$CORE_PATCHED_SHA" ]]         || mismatch="$mismatch core.py"
[[ "$(sha "$commands")" == "$COMMANDS_PATCHED_SHA" ]] || mismatch="$mismatch commands.py"
if [[ -n "$mismatch" ]]; then
    # 三個都還原 —— 只還原不合的那個會留下一個拼接過的樹。
    cp "$nb_bak" "$nb"
    cp "$core_bak" "$core"
    cp "$cmds_bak" "$commands"
    die "套用後雜湊不符（$mismatch）。已把三個檔案都還原。"
fi

echo "✓ 已套用"
echo

echo "--- 修補後：行為驗證（必須通過）---"
python3 "$VERIFIER" "$root" || {
    echo
    echo "✗ 套用後驗證未過 —— 已保留備份，可用下面這行還原：" >&2
    echo "    cp '$nb_bak' '$nb' && cp '$core_bak' '$core' && cp '$cmds_bak' '$commands'" >&2
    exit 1
}

echo
echo "還原方式： $0 --revert"
echo "升級 endpoint-vps 之後要重跑這支腳本（重裝會蓋掉修正）。"
