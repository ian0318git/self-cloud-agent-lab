#!/usr/bin/env bash
# 產生（或輪替）ntfy 主題名所依據的本機秘密 —— 切 C，D-069。
#
#   scripts/set-endpoint-topic-secret.sh            # 產生／輪替（會要你打 yes 確認）
#   scripts/set-endpoint-topic-secret.sh --check    # 只報告現況，不改任何東西
#   scripts/set-endpoint-topic-secret.sh --dry-run  # 產生一個並印指紋，但**不寫入**
#   scripts/set-endpoint-topic-secret.sh --yes      # 不問確認
#   scripts/set-endpoint-topic-secret.sh --force    # 活性查得到時仍然繼續
#   scripts/set-endpoint-topic-secret.sh --stdin    # 從 stdin 讀一行當新值（不產生）
#
# ── 為什麼需要這支腳本 ────────────────────────────────────
# 主題名以前是 `sha256(公開的 Kaggle 帳號)[:12]`，所以任何知道帳號的人都能推導
# 出來，而主題上有明文的生命週期文字 —— 那是可以**偽造 KILL**、可以讀生命週期、
# 可以依洪水分析**延遲**真 KILL 的一條通道。切 C 把推導輸入換成本機的秘密。
#
# 秘密**只到本機**：產生器烘進 notebook 的是**推導出來的主題字面值**，引擎只從
# `LLM_SIGNAL_TOPIC`／`LLM_CONTROL_TOPIC` 讀，**從不重算**。但主題**是**通行憑證，
# 它會離開本機到 Kaggle 上的 private notebook、`/tmp/endpoint-engine-output/` 底下的
# notebook、以及 kernel log。所以正確的說法是：
#   **秘密的爆炸半徑是本機；主題的爆炸半徑是本機＋Kaggle＋kernel log。**
#
# ── 形狀：恰為 32 個小寫十六進位字元 ──────────────────────
# 這不是形式主義。推導是**雜湊、不是 KDF**，所以任何人一旦知道主題名，就握有一個
# **離線驗證器**，可以用雜湊速度測試候選秘密 ——「好記的秘密」是真的弱。32 hex
# ＝128 bits，與 rotate-endpoint-key.sh 對金鑰的紀律一致。形狀在這裡強制，
# 而 read／write 的路徑上也都驗。
#
# ── 順序很重要（最容易誤解的一點）────────────────────────
# 秘密寫進去的**當下什麼都不會變** —— 舊的程式碼還在推導舊的主題。真正切換主題的
# 時刻是**套用切 C 的補丁**（`apply-endpoint-topic-secret.sh`）。所以正確順序是
#
#   1. 先確認**沒有 kernel 在跑**
#   2. 跑本腳本（秘密就位）
#   3. 套用切 C
#
# 反過來的話，補丁一落地、還在跑的那個 kernel 就聽不到任何訊號了 —— 它聽的是舊
# 名字。**它會繼續燒 GPU 配額。**
#
# ── 活性閘門（**列舉安全字，其餘一律警告**）────────────────
# 它會用套件自己的 `get_kernel_status`（要 Kaggle 憑證與網路）問一次。三態：
#   · running／queued／pending → **拒絕**，用 --force 才繼續
#   · complete／error → 繼續（只有這兩個字 positively 代表 kernel 結束了）
#   · **其餘全部**（unknown、任何認不得的狀態字、查不到）→ **警告後繼續**
# 界線畫在「列舉安全字」而不是「列舉危險字」，是因為兩個方向的錯代價不對稱：
# 把「問不到」當成「有東西在跑」是猜，而且會讓這支腳本在離線機器上根本不能用；
# 但把「問不到」當成「沒有東西在跑」**正是 D-060 的形狀**。所以只有 positively
# 代表「結束了」的字才放行 —— 誤判成警告只是吵，誤判成放行會讓一個還在跑的
# kernel 變聾並繼續燒 GPU 配額。它只印狀態字，**不印帳號、不印網址、不印主題**。
#
# ── 它刻意不做的事 ───────────────────────────────────────
#   · **不印秘密。** 全程只印 sha256 前 12 碼（指紋）。指紋足以確認「腳本讀到的
#     就是你剛設的那個」，但推不回秘密。
#   · **不接受從命令列傳值。** argv 是 `ps` 看得到的。它只自己產生，或從 stdin 讀
#     （`--stdin`）。沒有互動提示讀秘密這種事 —— 那會進 shell 歷史與終端機回捲。
#   · **不建備份。** 就地覆寫（寫 .tmp 再 `replace`），所以被換掉的秘密在本機
#     **不可回復**。要能退回請自己在執行**之前**備份 yaml。
#   · **不碰 Kaggle。** 不 push、不改那個 kernel；這是本機的動作。
#   · **CLI 不會自動產生秘密。** 這是刻意的：一個「沒設定就自動生一個」的路徑會讓
#     你以為一切正常，而實際上每次 boot 都換一條通道。
#
# 結束碼：0 ＝ 完成（或 --check 有可用的秘密）
#         1 ＝ 寫入失敗
#         2 ＝ 無法判定／無法完成（config 不存在、活性查得到在跑、--check 沒有秘密）
#              → **沒有改任何東西**
#         3 ＝ 這支腳本自己的前提壞了（那個鍵在檔案裡不是恰好一筆、或結構認不得）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 載入原因只有一個：usage_text()，也就是 `--help` 要印的檔頭。在頂層載入是
# 刻意的 —— 惰性載入會安靜地印出空白、而且結束碼 0。
source "$SCRIPT_DIR/lib.sh"

CFG="$HOME/.config/endpoint/endpoint-config.yaml"

# 套件的 venv python。用它是為了讓 `from endpoint.core import …` 找得到
# requests／yaml —— 系統的 python3 不一定有，而 PATH 上那個 `endpoint` 指令
# 的 shebang 指的就是這裡。
TOOL_DIR="${ENDPOINT_TOOL_DIR:-$HOME/.local/share/uv/tools/endpoint-vps}"
VENV_PY="$TOOL_DIR/bin/python3"
[[ -x "$VENV_PY" ]] || VENV_PY="$TOOL_DIR/bin/python"
[[ -x "$VENV_PY" ]] || VENV_PY=""

MODE="set"
ASSUME_YES=false
FORCE=false
FROM_STDIN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)   MODE=check ;;
        --dry-run) MODE=dry ;;
        --yes|-y)  ASSUME_YES=true ;;
        --force)   FORCE=true ;;
        --stdin)   FROM_STDIN=true ;;
        -h|--help) usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "未知的參數：$1（可用：--check、--dry-run、--yes、--force、--stdin、-h）"; exit 2 ;;
    esac
    shift
done

# ── 讀取：只回狀態，不回值 ───────────────────────────────
# 用 python 而不是 grep：要分辨「這個鍵在不在」、「它在不在 signal: 底下」、
# 「它的值是引號字串還是被 YAML 讀成整數」，而 YAML 不是 grep 能可靠解析的東西。
# 三個路徑的優先序與 core.py 的 CONFIG_PATHS 相同 —— 但本腳本只**寫**第一個。
READ_PY="$(cat <<'PY'
import hashlib, re, sys
from pathlib import Path

p = Path(sys.argv[1])
if not p.exists():
    print("missing\t")
    sys.exit(0)

raw = p.read_text(encoding="utf-8")
ANY = re.compile(r"^[ \t]*topic_secret[ \t]*:", re.M)
CHILD = re.compile(r"^[ \t]+topic_secret[ \t]*:[ \t]*(.*?)[ \t]*$", re.M)
n_any, n_child = len(ANY.findall(raw)), len(CHILD.findall(raw))

if n_any == 0:
    print("absent\t")
    sys.exit(0)
if n_any != 1 or n_child != 1:
    print("foreign\t%d 行 topic_secret（其中 %d 行是縮排的子鍵）—— 不是本腳本管的形狀"
          % (n_any, n_child))
    sys.exit(0)

v = CHILD.findall(raw)[0]
if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
    v = v[1:-1]

if not v:
    print("invalid\t空字串")
elif not re.fullmatch(r"[0-9a-fA-F]*", v):
    print("invalid\t不是十六進位（長度 %d）" % len(v))
elif v != v.lower():
    print("invalid\t有大寫（長度 %d）" % len(v))
elif len(v) != 32:
    print("invalid\t長度 %d，要 32" % len(v))
else:
    print("present\tsha256:" + hashlib.sha256(v.encode()).hexdigest()[:12])
PY
)"

# ── 寫入：就地在文字層 upsert，不用 yaml.dump ────────────
# 用 yaml.safe_load ＋ yaml.dump 會**摧毀所有註解與鍵序**，而這個檔案裡有一堆
# 說明。所以走文字層（與 rotate-endpoint-key.sh 的 HOST_PY 同型）：值只從 stdin
# 進來，寫 .tmp 再 `replace`，權限一律收到 0600。
WRITE_PY="$(cat <<'PY'
import hashlib, os, re, stat, sys
from pathlib import Path

p = Path(sys.argv[1])
new = sys.stdin.read().strip()
if not re.fullmatch(r"[0-9a-f]{32}", new):
    print("新值形狀不對（這是腳本自己壞掉）", file=sys.stderr)
    sys.exit(1)

raw = p.read_text(encoding="utf-8")
ANY = re.compile(r"^[ \t]*topic_secret[ \t]*:", re.M)
LINE = re.compile(r"^([ \t]+)topic_secret[ \t]*:[^\n]*$", re.M)
n_any, n_line = len(ANY.findall(raw)), len(LINE.findall(raw))

if n_any == 0:
    sig = re.compile(r"^signal[ \t]*:[ \t]*(#.*)?$", re.M)
    hits = list(sig.finditer(raw))
    if len(hits) != 1:
        print("找不到唯一的頂層 `signal:` 行（找到 %d 個）—— 不敢猜要插在哪裡"
              % len(hits), file=sys.stderr)
        sys.exit(3)
    at = hits[0].end()
    action = "inserted"
    out = raw[:at] + '\n  topic_secret: "%s"' % new + raw[at:]
elif n_any == 1 and n_line == 1:
    action = "replaced"
    out = LINE.sub(lambda m: '%stopic_secret: "%s"' % (m.group(1), new), raw, count=1)
else:
    print("topic_secret 在這個檔案裡不是恰好一筆縮排的子鍵（%d / %d）"
          % (n_any, n_line), file=sys.stderr)
    sys.exit(3)

before = stat.S_IMODE(p.stat().st_mode)
tmp = p.with_name(p.name + ".tmp")
tmp.write_text(out, encoding="utf-8")
os.chmod(tmp, 0o600)          # 一律收緊；「爆炸半徑是本機」只在這裡成立
tmp.replace(p)
print("sha256:%s\t%s\t%04o\t%04o"
      % (hashlib.sha256(new.encode()).hexdigest()[:12], action, before, 0o600))
PY
)"

# ── 活性：查得到且在跑就拒絕，查不到就警告 ───────────────
# 只印一個狀態字。不印帳號、不印網址、不印主題。
LIVE_PY="$(cat <<'PY'
import sys
try:
    from endpoint.core import Config, get_kernel_status
except Exception as e:
    print("unavailable\t" + type(e).__name__)
    sys.exit(0)
try:
    print("ok\t" + str(get_kernel_status(Config())))
except Exception as e:
    print("unknown\t" + type(e).__name__)
PY
)"

probe_config() {
    local line
    line="$(python3 -c "$READ_PY" "$CFG")"
    CFG_STATE="${line%%$'\t'*}"
    CFG_NOTE="${line#*$'\t'}"
    CFG_FP=""
    if [[ "$CFG_STATE" == present ]]; then
        CFG_FP="$CFG_NOTE"
    fi
    return 0
}

report_config() {
    case "$CFG_STATE" in
        present) echo "  現在：$CFG_FP" ;;
        absent)  echo "  現在：沒有 signal.topic_secret 這個鍵" ;;
        missing) echo "  現在：$CFG 不存在" ;;
        *)       echo "  現在：不可用 —— $CFG_NOTE" ;;
    esac
}

# ── 讀完才開始寫：先確認每個前提 ─────────────────────────
if [[ ! -f "$CFG" ]]; then
    fail "找不到 $CFG —— endpoint 還沒在這台機器上設定過。"
    echo "  處方：endpoint init"
    exit 2
fi

probe_config
if [[ "$CFG_STATE" == foreign ]]; then
    fail "$CFG：$CFG_NOTE"
    echo "  在那個狀態下**任何取代動作都是猜的** —— 不動任何東西。"
    echo "  處方：自己看一眼那個檔案，把 signal 底下多出來的 topic_secret 收成一筆。"
    exit 3
fi

if [[ "$MODE" == check ]]; then
    echo "── ntfy 主題秘密（--check，沒有改任何東西）──"
    report_config
    if [[ "$CFG_STATE" == present ]]; then
        echo
        ok "有可用的秘密。"
        exit 0
    fi
    echo
    fail "沒有可用的秘密。切 C 套用後，boot／stop／watch 會全部大聲失敗。"
    echo "  處方：scripts/set-endpoint-topic-secret.sh"
    exit 2
fi

# ── 產生新值（或從 stdin 讀）────────────────────────────
if [[ "$FROM_STDIN" == true ]]; then
    # 只讀**一行**。值從 argv 進來的話 `ps` 看得到，所以 stdin 是唯一的來源。
    IFS= read -r NEW_V || { fail "讀不到 stdin —— --stdin 需要一行新值"; exit 2; }
else
    if command -v openssl >/dev/null 2>&1; then
        NEW_V="$(openssl rand -hex 16)"
    else
        NEW_V="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
fi
[[ "$NEW_V" =~ ^[0-9a-f]{32}$ ]] || { fail "產生的新值形狀不對 —— 這是腳本自己壞掉"; exit 3; }

NEW_FP="$(printf '%s' "$NEW_V" | sha256sum | cut -c1-12)"

if [[ "$MODE" == dry ]]; then
    # 空跑**只印指紋**。不印值，也不印推導出來的主題名。
    echo "sha256:$NEW_FP"
    echo "（空跑，沒有寫入 $CFG）"
    exit 0
fi

echo "── ntfy 主題秘密 ──────────────────────────────────"
report_config
echo "  寫入後：sha256:$NEW_FP"
echo

# 「沒能確認沒有東西在跑」有兩個來源，**處方相同**：查不到（沒有憑證、非 200、
# 網路錯誤、探針根本跑不起來），或是查到了但那個狀態字我不認得。兩者的共同點是
# 「不知道」，而**不知道不是「沒事」**。
liveness_unknown() {
    warn "$1 —— **這不等於沒有東西在跑**。"
    echo "  這一條**刻意不阻擋**：活性是盡力而為的警告，不是硬門（設計 §五）——"
    echo "  硬門是下面那個秘密，離線且精確。但「不知道」不等於「沒事」：先自己確認"
    echo "  一次 endpoint status。若套用後才發現有孤兒：endpoint kill-all --yes"
    echo "  （不建構 Config）或 Kaggle UI。"
}

if [[ "$FORCE" != true ]]; then
    echo "── 活性（列舉安全字，其餘一律警告）──"
    if [[ -z "$VENV_PY" ]]; then
        liveness_unknown "找不到套件的 venv python（$TOOL_DIR/bin/python），探針跑不起來"
    else
        live="$("$VENV_PY" -c "$LIVE_PY" 2>/dev/null || printf 'unknown\t%s\n' "執行失敗")"
        live_state="${live%%$'\t'*}"
        live_word="${live#*$'\t'}"
        # **列舉安全字，不列舉危險字。** 這裡本來寫的是 `ok:*` → 綠燈，而
        # `get_kernel_status` 的 docstring 第一句就是 `"unknown" is not "offline"`：
        # 它的每一條失敗路徑（沒有憑證、非 200、網路錯誤、**認不得的狀態字**）都回
        # "unknown"。所以那個 glob 恰好把所有「問不到」讀成「沒有在跑」—— 就是下面
        # 警告文字自己在講的 D-060 形狀。文字是對的，控制流沒有照著做。
        # 反過來之後，complete／error 以外的**每一個**字都會走到警告，包括未來新增
        # 的狀態字。D12(f) 與突變 M14 釘住這個極性。
        case "$live_state:$live_word" in
            ok:running|ok:queued|ok:pending)
                fail "Kaggle 上有一個 kernel 的狀態是「$live_word」—— 拒絕改動。"
                echo "  改主題會讓那個 kernel 聽不到任何訊號（它聽的是舊名字），"
                echo "  而它會繼續燒 GPU 配額。**60 分鐘閒置逾時不是復原路徑** ——"
                echo "  它只殺引擎行程，notebook 本體還在跑。"
                echo "  處方：先 endpoint stop（或 endpoint kill-all --yes），再跑本腳本。"
                echo "  真的要現在改：--force。"
                exit 2 ;;
            ok:complete|ok:error)
                ok "狀態是「$live_word」—— 那個 kernel 已經結束了，可以改。" ;;
            ok:*)
                liveness_unknown "狀態字「$live_word」不在已知清單裡（結束態只有 complete／error）" ;;
            *)
                liveness_unknown "查不到 kernel 狀態（$live_word），通常是這個 shell 沒有 Kaggle 憑證" ;;
        esac
    fi
    echo
fi

if [[ "$ASSUME_YES" != true ]]; then
    echo "即將把秘密換成上面指紋的那一個。"
    echo "  · **舊值不可回復** —— 這支腳本就是地覆寫，不建備份。"
    echo "  · 寫入的當下**什麼都不會變**；真正切換主題的時刻是套用切 C 的補丁。"
    echo "  · 收尾時會把 $CFG 的權限收成 0600。"
    read -r -p "確定要繼續嗎？輸入 yes 確認：" reply || {
        fail "沒有互動輸入可用 —— 已取消（要跳過確認請用 --yes）。"
        exit 2
    }
    if [[ "$reply" != "yes" ]]; then info "已取消"; exit 0; fi
fi

# ── 寫入 ────────────────────────────────────────────────
if ! out="$(printf '%s' "$NEW_V" | python3 -c "$WRITE_PY" "$CFG")"; then
    fail "寫入失敗 —— $CFG 沒有被改動（或已還原）。"
    exit 1
fi
IFS=$'\t' read -r W_FP W_ACTION W_BEFORE W_AFTER <<< "$out"

ok "已寫入（$W_ACTION）：$W_FP"
if [[ "$W_BEFORE" != "$W_AFTER" ]]; then
    echo "  權限：$W_BEFORE → $W_AFTER（收緊了）"
fi

cat <<'EOF'

接下來（照這個順序，不能換）：

  1. 確認沒有 kernel 在跑：endpoint status
  2. 套用切 C：
         scripts/apply-endpoint-topic-secret.sh --dry-run
         scripts/apply-endpoint-topic-secret.sh
     **這一步才是真正切換主題的時刻。** 在那之前一切照舊。

要注意的兩件事：

  · `endpoint` 存設定時會用 yaml.dump 重寫這個檔案，**那會摧毀所有註解**
    （值不會掉）。如果註解對你重要，自己留一份。
  · 要退回舊的主題名，只能還原切 C 的補丁（`apply-endpoint-topic-secret.sh
    --revert`）；秘密本身沒有備份。主題名沒有備份也**不需要** —— 推導是
    單向的，舊名字推不回來，而那個舊名字本來就是「公開帳號的函式」。
EOF
