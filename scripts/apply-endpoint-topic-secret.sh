#!/usr/bin/env bash
# 把 ntfy 主題名的推導輸入從公開的 Kaggle 帳號換成本機的秘密（切 C）。
#
#   scripts/apply-endpoint-topic-secret.sh            # 套用（先備份、後驗證）
#   scripts/apply-endpoint-topic-secret.sh --dry-run  # 只檢查，不動任何檔案
#   scripts/apply-endpoint-topic-secret.sh --verify   # 對現況跑行為驗證
#   scripts/apply-endpoint-topic-secret.sh --revert   # 還原成最新的一份備份
#   scripts/apply-endpoint-topic-secret.sh --target D # 改為操作 D（D 是 site-packages 目錄）
#
# 為什麼是補丁：五個目標有四個不在本 repo 內 —— `endpoint/core.py`、
# `endpoint/commands.py` 與 `endpoint/data/` 底下那兩個隨套件出貨的檔案由 uv tool
# 安裝在家目錄底下，`scripts/master_build_notebook.py` 是產生 Kaggle notebook 的
# 產生器。補丁是唯一能讓這個修正「可重現、可稽核、可回退」的形式。
#
# **這支腳本動的是 package manager 目錄裡的檔案。** `uv tool upgrade endpoint-vps`
# 或任何重裝都會把修正蓋掉 —— 升級後請重跑一次。腳本認得原始檔的雜湊，所以升級
# 後會直接拒絕，不會把補丁硬套到新版上。
#
# ⚠️ **套用順序：ntfy-fixes → stop-fixes → tunnel-url-privacy（切 B）→ 本補丁（切 C）。**
# 本補丁釘的「原始版雜湊」＝ **切 B 套用後**的狀態，所以它只能在 B 之上套用。
# 而 B、stop-fixes、ntfy-fixes 打的是同一組檔案 —— **先還原本補丁，才能還原它們**；
# 直接對前幾支跑 `--revert` 會**無聲地**拆掉這一刀（它們認的是自己的 patched 雜湊，
# 還原後就回到它們眼中的 pristine）。這是補丁互相疊加時固有的風險，不是本腳本能
# 擋掉的；要擋只能靠順序。B 的腳本自己也有同一段警語。
#
# 這個補丁橫跨**五個檔案**，所以比姊妹作多兩件事：釘十個雜湊；五個檔案狀態不一致
# 時直接停手，不試著補完 —— 那個狀態只會來自中斷的套用或手動編輯，而一份橫跨
# 五檔的 diff 本來就不會只套上其中一部分。
#
# **硬閘門：本機設定裡必須已經有一個形狀正確的 `signal.topic_secret`。**
# 補丁一落地，CLI 就改用由秘密推導出來的主題名；沒有秘密就推導不出來，boot／stop／
# watch 會全部大聲失敗（刻意的：寧可拒絕，也不要靜默退回舊的、由公開帳號推導的
# 名字）。這條閘門**離線、精確、不是代理量** —— 它讀的就是 Config 會讀的那個檔案，
# 所以「補丁已套、秘密不存在」這個災難視窗在構造上關得掉。
# **刻意沒有 --force。** 一個能繞過它的開關會把這個 P0 原樣打開，而 `--dry-run`
# 已經提供了「先看看套不套得上」這條無害的路。
#
# 活性只是**警告**，不是閘門：`endpoint status` 要憑證與網路，而本腳本是離線的。
# 離線拿得到的只有快取目錄，那代表「上次 boot 成功」而**不是**「現在有 kernel 在
# 跑」—— 把代理量當事實正是 D-051／D-060 的形狀。但套用之後主題名就換了，還在跑的
# 那個 kernel 聽的是舊名字，**它會繼續燒 GPU 配額**。真正的復原只有
# `endpoint kill-all --yes`（它不建構 Config，走 Kaggle REST）或 Kaggle UI；
# **60 分鐘閒置逾時不是復原路徑** —— 它殺的是引擎行程，notebook 本體還在
# `while True: time.sleep(60)`。
#
# 兩個方向的驗證是刻意的：修補前**必須**驗證失敗（否則就沒有東西可修），
# 修補後**必須**通過。兩邊都過代表這支腳本在檢查空氣。
#
# 結束碼：0 ＝ 成功（含 --dry-run 空跑完成）；1 ＝ 檢查失敗（雜湊不符、驗證未過、
# context 不合）；2 ＝ 前置條件不成立（沒有可用的秘密）或參數不認識。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 載入原因只有一個：usage_text()，也就是 `--help` 要印的檔頭。在頂層載入是
# 刻意的 —— 惰性載入會安靜地印出空白、而且結束碼 0。理由與姊妹作相同，那邊
# 有完整的說明。
source "$SCRIPT_DIR/lib.sh"

PATCH="$SCRIPT_DIR/endpoint-topic-secret.patch"
VERIFIER="$SCRIPT_DIR/test_endpoint_topic_secret.py"

# 五個目標，順序固定：清單順序就是狀態訊息與備份章節的順序。
RELS=(
    "endpoint/core.py"
    "endpoint/commands.py"
    "scripts/master_build_notebook.py"
    "endpoint/data/endpoint-config.example.yaml"
    "endpoint/data/endpoint.1"
)

# 補丁是對這個版本建的。上游一換，這裡就會擋下來。
# （「原始版」＝ ntfy-fixes ＋ stop-fixes ＋ 切 B 都套用後的狀態。）
CORE_PRISTINE_SHA=8c8afbb9e3da2b03eb6ca2adf9152321409d442a737faddce36fbe6b926ebc86
CORE_PATCHED_SHA=e3d7fe21240013320dd3ce7928d3501fd7b56e59bc782119fe9a8dbe65c1fbc0
COMMANDS_PRISTINE_SHA=8d6f2adeb8d070b080b88b04f60bc37ff528ffa859e5b518286a2ba962eb91d3
COMMANDS_PATCHED_SHA=19b8129ee4c9b7f644bae3ec2f5e4c038b2886f128bb838b27a0b55bb86b0eb5
NB_PRISTINE_SHA=a044901445ea5fd59dcb59e91e7a37c394b26dc952ad646c48b8bdddfd370cd8
NB_PATCHED_SHA=e29a23aa2479789571a2a4d8c18371fd79d41b5e7001a7bfd7b1e082da166f27
EXCONF_PRISTINE_SHA=001f849f12d8a65d30f915e53aabe5e8b1591750aadd2f7249ec68782a2a6126
EXCONF_PATCHED_SHA=52981380f673867457d22ed285ebe5076a144bc86c8817ec599fe63467af660f
MANPAGE_PRISTINE_SHA=3830953c70b855a3a5db5da0cadd89a8f51d85e2118cd57b4c41a9b0c8d99fb0
MANPAGE_PATCHED_SHA=ed791ff378140d304d562c846c734cab8425bff4727efe4a91c5e4ae008a4d4d

PRISTINE_SHAS=(
    "$CORE_PRISTINE_SHA"
    "$COMMANDS_PRISTINE_SHA"
    "$NB_PRISTINE_SHA"
    "$EXCONF_PRISTINE_SHA"
    "$MANPAGE_PRISTINE_SHA"
)
PATCHED_SHAS=(
    "$CORE_PATCHED_SHA"
    "$COMMANDS_PATCHED_SHA"
    "$NB_PATCHED_SHA"
    "$EXCONF_PATCHED_SHA"
    "$MANPAGE_PATCHED_SHA"
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

die()    { echo "✗ $*" >&2; exit 1; }
refuse() { echo "✗ $*" >&2; exit 2; }
sha()    { sha256sum "$1" | cut -d' ' -f1; }

find_target() {
    if [[ -n "$TARGET" ]]; then
        printf '%s\n' "$TARGET"
        return
    fi
    local base="${ENDPOINT_TOOL_DIR:-$HOME/.local/share/uv/tools/endpoint-vps}"
    local hit
    hit="$(find "$base" -path "*/site-packages/${RELS[1]}" -print -quit 2>/dev/null || true)"
    [[ -n "$hit" ]] || die "找不到 endpoint 套件。請用 --target 指定，或設 ENDPOINT_TOOL_DIR。"
    # .../site-packages/endpoint/commands.py → 往上兩層就是 site-packages 根。
    printf '%s\n' "$(dirname "$(dirname "$hit")")"
}

# 在暫存樹裡重現補丁預期的目錄形狀（-p1 需要 endpoint/、endpoint/data/ 與
# scripts/ 這三層），讓 --dry-run 與實際套用走同一條路徑、用同一個嚴格度。
stage_copy() {
    local src="$1" dst="$2" rel
    for rel in "${RELS[@]}"; do
        mkdir -p "$dst/$(dirname "$rel")"
        cp "$src/$rel" "$dst/$rel"
    done
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

# ── 秘密閘門（離線、精確）────────────────────────────────────────
# 讀的就是 Config 會讀的那三個路徑，順序也一樣（`CONFIG_PATHS`：使用者設定 →
# repo 根 → 套件 data/）。回報每一個路徑的狀態，並記下第一個命中的。
#
# 為什麼用 Python 而不是 grep：YAML 不是可以用 grep 可靠解析的東西（引號、縮排、
# 行內註解、`topic_secret` 出現在別的路徑底下），而這條閘門必須**精確**才站得住。
# **不印值**：形狀不對的值只印長度與不合法的方式，指紋也只用來確認「腳本讀到的
# 就是你剛設的那個」，比照 rotate-endpoint-key.sh 只印 sha256:<12> 的既有紀律。
secret_probe() {
    local out row status path note
    out="$(python3 - "$root" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
paths = [
    Path.home() / ".config" / "endpoint" / "endpoint-config.yaml",
    root / "endpoint-config.yaml",
    root / "endpoint" / "data" / "endpoint-config.yaml",
]

VALID = re.compile(r"[0-9a-f]{32}\Z")


def why(v):
    """不合法的方式，不含值本身。"""
    if v is None:
        return "沒有這個鍵"
    s = str(v)
    if not s:
        return "空字串"
    if not re.fullmatch(r"[0-9a-fA-F]*", s):
        return f"不是十六進位（長度 {len(s)}）"
    if s != s.lower():
        return f"有大寫（長度 {len(s)}）"
    return f"長度 {len(s)}，要 32"


for p in paths:
    if not p.exists():
        print(f"missing\t{p}\t檔案不存在")
        continue
    try:
        import yaml

        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001 —— 任何讀取失敗都要回報，不是猜
        print(f"unreadable\t{p}\t{type(e).__name__}")
        continue
    node = data
    for key in ("signal", "topic_secret"):
        node = node.get(key) if isinstance(node, dict) else None
    secret = str(node or "")
    if VALID.match(secret):
        fp = hashlib.sha256(secret.encode()).hexdigest()[:12]
        print(f"present\t{p}\tsha256:{fp}")
    else:
        print(f"invalid\t{p}\t{why(node)}")
PY
)"

    SECRET_OK=0
    SECRET_PATH=""
    SECRET_FP=""
    SECRET_REPORT=()
    while IFS=$'\t' read -r status path note; do
        [[ -n "$status" ]] || continue
        SECRET_REPORT+=("$status|$path|$note")
        # CONFIG_PATHS 的順序就是優先序：第一個命中的檔案就是生效的那個。
        # 命名是 `SECRET_OK`＝「有可用的秘密」，所以 1 才是成功。這裡曾經相反
        # （初始 1、命中時 0），而 `secret_probe` 又**從來沒有被呼叫過** ——
        # 兩個缺陷互相掩護：`((SECRET_OK))` 在 `set -u` 下先炸成
        # 「unbound variable」，於是誰也沒發現它的極性也是反的。結果是這支
        # 腳本**永遠套用不了**，而症狀只是一行看不懂的 shell 錯誤。
        if [[ "$status" == present && -z "$SECRET_PATH" ]]; then
            SECRET_OK=1
            SECRET_PATH="$path"
            SECRET_FP="$note"
        fi
    done <<<"$out"
}

# 這四個全域在**函式之外**初始化是刻意的：`set -u` 之下，一個沒被賦值的
# `SECRET_OK` 會在 `((SECRET_OK))` 那一行炸掉，而那個症狀（一行 shell 錯誤）
# 讀起來完全不像「秘密閘門壞了」。初始化擺在這裡，就再也沒有那個狀態。
SECRET_OK=0
SECRET_PATH=""
SECRET_FP=""
SECRET_REPORT=()

secret_report() {
    local row status path note
    for row in "${SECRET_REPORT[@]}"; do
        status="${row%%|*}"
        row="${row#*|}"
        path="${row%%|*}"
        note="${row#*|}"
        case "$status" in
            present) printf '  ✓ %s\n           %s\n' "$path" "$note" ;;
            missing) printf '  · %s —— %s\n' "$path" "$note" ;;
            *)       printf '  ✗ %s —— %s\n' "$path" "$note" ;;
        esac
    done
}

secret_gate() {
    # **閘門自己探測。** 呼叫端曾經要記得先跑 `secret_probe`，而它忘了 ——
    # 一個「讀了沒被填過的變數」的閘門會拒絕對的樹、放行錯的樹，而兩者都
    # 只表現成一行難以理解的訊息。把探測綁進閘門，那個狀態就不存在了。
    secret_probe
    echo "--- 秘密閘門（離線、精確）---"
    secret_report
    if ((SECRET_OK)); then
        echo "✓ 找到可用的 signal.topic_secret（$SECRET_FP）"
        local user_cfg="$HOME/.config/endpoint/endpoint-config.yaml"
        if [[ "$SECRET_PATH" != "$user_cfg" ]]; then
            echo "  注意：秘密不在使用者設定裡（$user_cfg）。"
            echo "        產生器收到的是 CLI 解析出來的那份（Config().path），"
            echo "        放在使用者設定裡最不會有意外。"
        fi
        echo
        return 0
    fi
    echo "✗ 沒有任何路徑提供可用的 signal.topic_secret。"
    echo
    echo "  補丁一落地，CLI 就改用由這個秘密推導出來的主題名。沒有秘密就推導不出來 ——"
    echo "  boot／stop／watch 會全部大聲失敗。那是刻意的：寧可拒絕，也不要靜默退回舊的、"
    echo "  由公開 Kaggle 帳號推導的名字。所以這裡先拒絕，讓那個狀態不可達。"
    echo
    echo "  先產生一個（它只印指紋，不印值）："
    echo "      scripts/set-endpoint-topic-secret.sh"
    echo
    echo "  然後再跑一次本腳本。"
    echo
    return 1
}

# 活性：**只是警告**。離線拿得到的只有快取目錄，那代表「上次 boot 成功」而不是
# 「現在有 kernel 在跑」—— 把代理量當事實正是 D-051／D-060 的形狀。
liveness_warning() {
    local cache="${TMPDIR:-/tmp}/.endpoint_cache_dir/tunnel"
    echo "--- 活性（盡力而為，不是閘門）---"
    echo "  套用之後主題名就換了。還在跑的那個 kernel 聽的是**舊名字** ——"
    echo "  它不會收到任何訊號，會繼續燒 GPU 配額直到你處理它。"
    if [[ -e "$cache" ]]; then
        echo "  ! 快取目錄裡有 tunnel 網址：上一次 boot 至少跑到了取得網址那一步。"
    else
        echo "  · 快取目錄裡沒有 tunnel 網址：上次 stop 成功、或從來沒 boot 過。"
    fi
    echo "    兩者都只是**代理量**，都無法回答「現在有沒有 kernel 在跑」。"
    echo "  → 真正該做的是先跑 endpoint status（要 Kaggle 憑證）。"
    echo "  → 若套用後才發現有孤兒：endpoint kill-all --yes（不建構 Config）或 Kaggle UI。"
    echo "    **60 分鐘閒置逾時不是復原路徑** —— 它只殺引擎行程，notebook 還在跑。"
    echo
}

revert_all() {
    local first="${root}/${RELS[0]}"
    shopt -s nullglob
    local baks=("$first".bak-*)
    shopt -u nullglob
    ((${#baks[@]})) || die "找不到備份（$first.bak-*），無法還原。"

    local stamp rel ok
    # 從最新的章節往回找，只接受**五個檔案都齊**的那一組。
    while read -r stamp; do
        [[ -n "$stamp" ]] || continue
        ok=1
        for rel in "${RELS[@]}"; do
            [[ -f "$root/$rel.bak-$stamp" ]] || ok=0
        done
        ((ok)) || continue
        for rel in "${RELS[@]}"; do
            cp "$root/$rel.bak-$stamp" "$root/$rel"
        done
        echo "✓ 已從章節 $stamp 還原五個檔案"
        for rel in "${RELS[@]}"; do
            printf '  %-45s %s\n' "$rel" "$(sha "$root/$rel")"
        done
        echo
        echo "注意：這只還原本補丁。core.py 與 commands.py 會回到切 B ＋ stop-fixes 套用"
        echo "      後的狀態（本補丁的「原始版」）。**要還原那幾支，必須先還原本補丁。**"
        echo "      還原後主題名就回到由公開 Kaggle 帳號推導的那個 —— 秘密留著不影響。"
        exit 0
    done < <(printf '%s\n' "${baks[@]}" | sed 's/.*\.bak-//' | sort -ru)

    die "每一份備份都湊不齊五個檔案 —— 不敢只還原一部分。"
}

root="$(find_target)"
[[ -d "$root" ]] || die "目標不存在：$root"
for rel in "${RELS[@]}"; do
    [[ -f "$root/$rel" ]] || die "目標不存在：$root/$rel"
done
[[ -f "$PATCH" ]]    || die "找不到補丁：$PATCH"
[[ -f "$VERIFIER" ]] || die "找不到驗證器：$VERIFIER"

echo "目標：$root"
for rel in "${RELS[@]}"; do
    printf '  %-45s %s\n' "$rel" "$(sha "$root/$rel")"
done
echo

# --------------------------------------------------------------------------
# --revert
# --------------------------------------------------------------------------
if [[ "$MODE" == revert ]]; then
    revert_all
fi

# --------------------------------------------------------------------------
# --verify：只跑行為驗證，不動檔案
# --------------------------------------------------------------------------
if [[ "$MODE" == verify ]]; then
    exec python3 "$VERIFIER" "$root"
fi

# --------------------------------------------------------------------------
# 前置檢查：五個檔案都必須落在已知的兩個版本上，而且狀態一致
# --------------------------------------------------------------------------
STATES=()
for i in "${!RELS[@]}"; do
    STATES+=("$(state_of "$root/${RELS[$i]}" "${PRISTINE_SHAS[$i]}" "${PATCHED_SHAS[$i]}")")
done

for i in "${!RELS[@]}"; do
    if [[ "${STATES[$i]}" == unknown:* ]]; then
        die "${RELS[$i]} 既不是已知的原始版，也不是修補後的版本。
  預期原始版雜湊：${PRISTINE_SHAS[$i]}
  實得：          ${STATES[$i]#unknown:}
  endpoint-vps 可能已升級，或前面幾支補丁還沒套（本補丁疊在切 B 之上）——
  請先依序跑 apply-endpoint-ntfy-fixes.sh、apply-endpoint-stop-fixes.sh、
  apply-endpoint-tunnel-url-privacy.sh，或確認上游是否已自行修正。"
    fi
done

all_patched=1
for s in "${STATES[@]}"; do
    [[ "$s" == patched ]] || all_patched=0
done
if ((all_patched)); then
    echo "五個檔案都已經是修補後的版本，沒有事要做。"
    exit 0
fi

# 全 pristine 或全 patched 之外，只要有一個不是 patched，就得全部一致。
first_state="${STATES[0]}"
for i in "${!RELS[@]}"; do
    if [[ "${STATES[$i]}" != "$first_state" ]]; then
        echo "✗ 五個檔案的狀態不一致：" >&2
        for j in "${!RELS[@]}"; do
            printf '    %-45s %s\n' "${RELS[$j]}" "${STATES[$j]}" >&2
        done
        die "這只會來自一次中斷的套用，或手動編輯 —— 一份橫跨五個檔案的 diff 不會只
  套上其中一部分。請先還原成一致的狀態再重跑：
      $0 --revert
  若 --revert 說找不到完整的備份章節，重新安裝一次即可：
      uv tool install --force endpoint-vps"
    fi
done

# --------------------------------------------------------------------------
# 秘密閘門：--dry-run 只報告，實際套用是硬擋
# --------------------------------------------------------------------------
if [[ "$MODE" == dry ]]; then
    if secret_gate; then
        :
    else
        echo "⚠️  空跑仍然繼續：--dry-run 不動任何檔案。"
        echo "   但**實際套用會被上面這條閘門拒絕**，直到秘密就位。"
        echo
    fi
else
    secret_gate || refuse "拒絕套用：沒有可用的 signal.topic_secret（沒有動到任何檔案）。"
fi

liveness_warning

# 到這裡五個檔案都是原始版，所以反向閘門的結論是有意義的。
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
for i in "${!RELS[@]}"; do
    got="$(sha "$stage/${RELS[$i]}")"
    [[ "$got" == "${PATCHED_SHAS[$i]}" ]] || die "${RELS[$i]} 套用後的雜湊不符：預期 ${PATCHED_SHAS[$i]}，實得 $got"
done
echo "✓ 五個檔案套用後的雜湊都與驗證過的版本一致"
echo

if [[ "$MODE" == dry ]]; then
    echo "空跑完成，沒有動到任何檔案。"
    exit 0
fi

# --------------------------------------------------------------------------
# 實際套用
# --------------------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
echo "已備份 →"
for rel in "${RELS[@]}"; do
    cp "$root/$rel" "$root/$rel.bak-$STAMP"
    printf '  %s.bak-%s\n' "$rel" "$STAMP"
done

apply_patch_to "$root" >/dev/null

mismatch=""
for i in "${!RELS[@]}"; do
    [[ "$(sha "$root/${RELS[$i]}")" == "${PATCHED_SHAS[$i]}" ]] || mismatch="$mismatch ${RELS[$i]}"
done
if [[ -n "$mismatch" ]]; then
    # 五個都還原 —— 只還原不合的那些會留下一個拼接過的樹。
    for rel in "${RELS[@]}"; do
        cp "$root/$rel.bak-$STAMP" "$root/$rel"
    done
    die "套用後雜湊不符（$mismatch）。已把五個檔案都還原。"
fi

echo "✓ 已套用"
echo

echo "--- 修補後：行為驗證（必須通過）---"
python3 "$VERIFIER" "$root" || {
    echo
    echo "✗ 套用後驗證未過 —— 已保留備份，可用下面這行還原：" >&2
    printf '    %s --revert\n' "$0" >&2
    exit 1
}

echo
echo "⚠️  主題名**從這一刻起**才切換。若剛剛有 kernel 在跑，它聽的是舊名字、"
echo "    不會收到任何訊號 —— 先確認：endpoint status。"
echo "還原方式： $0 --revert"
echo "升級 endpoint-vps 之後要重跑這支腳本（重裝會蓋掉修正）。"
