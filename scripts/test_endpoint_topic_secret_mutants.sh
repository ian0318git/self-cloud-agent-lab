#!/usr/bin/env bash
# 突變測試：把切 C 的推導**故意弄壞**，確認 test_endpoint_topic_secret.py 真的會叫。
#
#   bash scripts/test_endpoint_topic_secret_mutants.sh <已套 C 的樹>
#
# 為什麼需要這一步（D-024 第八節，同樣的理由再來一次）：一組「永遠通過」的
# 測試和「永遠失敗」的測試一樣沒用。把受測程式碼修好、測試跟著變綠，這件事
# 本身不證明測試有在檢查任何東西。**唯一能證明測試有效的方法，是讓它面對
# 一個已知的錯誤，然後看它有沒有叫。**
#
# ── 這一台和家族裡其他台不同的地方：它比對的是**哪一條**叫 ────
# 家族裡其他的突變台問的是「測試有沒有變紅」。這裡不夠。切 C 的驗證器有一條
# D11 會把**套用腳本裡手寫的十個雜湊**拿去比對受測的樹 —— 任何突變都會讓它紅。
# 所以「紅了」在這裡沒有資訊量：每一條突變都會讓它紅，包括寫壞的那種。
#
# 因此每一條突變都**指名**它該被哪一條檢查抓到（`D4`），而守門的突變指名
# 哪一條**不准**叫（`=D6`）。D11 的噪音於是無害，而「這條斷言有牙齒」變成
# 逐條可證的。這比「全部變紅」強得多：某一條突變若**只**讓 D11 紅，它會被
# 正確地報成沒抓到，而不是混進「抓到」的名額裡。
#
# ── 植入的位置：AST 界定的函式範圍 ＋ 該範圍內唯一 ──────────
# 目標字串常常在檔案裡出現不只一次（兩個產生器呼叫點逐字相同）。所以突變
# 指定一個**外層函式名**，字串只要在那個函式的行範圍內恰好出現一次即可。
# `.sh` 沒有 AST，範圍寫 `-`，唯一性回退成整檔。
#
# 欄位格式：標籤|T:樹內路徑 或 S:腳本目錄檔名|範圍|原字串|取代字串|期望
#   期望 `D4`  = 這一條**必須**讓 D4 叫（有牙齒）
#   期望 `=D6` = 這一條**不准**讓 D6 叫（守門：D6 不該被無關的改動驚動）
#
# 用法：bash scripts/test_endpoint_topic_secret_mutants.sh <已套 C 的樹>
#
# 結束碼：0 = 每條突變都被指定它的那一條抓到、每個守門的都沒叫、對照組通過
#         1 = 有突變沒被抓到（驗證器有洞）、或對照組失敗（驗證器壞了）
#         2 = 用法錯誤
#         3 = 突變清單本身寫壞了（欄位數不對）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -ne 1 ]]; then
    echo "用法：bash scripts/test_endpoint_topic_secret_mutants.sh <已套 C 的樹>" >&2
    echo "  那棵樹是 apply-endpoint-topic-secret.sh 的產物（或 --dry-run 用的同一棵）。" >&2
    exit 2
fi
SRC_TREE="$1"
[[ -d "$SRC_TREE" ]] || { echo "找不到目錄：$SRC_TREE" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# **不要寫 .pyc。** 突變是用字串取代植入的，而 CPython 對 .pyc 的有效性檢查
# 只比對「原始檔的 mtime（**秒**）與大小」。兩條連續的突變若長度相同又落在
# 同一秒內，第二次寫入就會命中第一次留下的 .pyc —— 那個突變**根本沒有被
# 執行**，測試當然通過，而突變台把它報成「抓到」。實測：`return 1` → `return 2`
# 這種同長度取代，在同一秒內被完全忽略。一個會謊報「抓到」的突變台是
# fail-open —— 比沒有突變台更糟，因為它讓「全部抓到」這句話變成假的。
export PYTHONDONTWRITEBYTECODE=1

# ── 突變清單 ─────────────────────────────────────────────
MUTANTS=(
  # ── 推導本身 ──
  "M1 缺席時回退帳號（fail-open）|T:endpoint/core.py|topic_secret_of|raise ValueError(TOPIC_SECRET_HELP)|return str(config.kaggle_username)|D4"
  "M2 空字串走退路（產生器端）|T:scripts/master_build_notebook.py|require_topic_secret|raise ValueError(TOPIC_SECRET_HELP)|return \"0\" * 32|D4"
  "M3 CLI 端改回帳號推導|T:endpoint/core.py|signal_topic|digest = topic_digest(topic_secret_of(self))|digest = topic_digest(self.kaggle_username)|D3"
  "M4 只拿掉 CLI 這邊的 .lower()|T:endpoint/core.py|signal_topic|return f\"{self.topic_prefix}-{digest}\".lower()|return f\"{self.topic_prefix}-{digest}\"|D3"
  "M5 upload 呼叫點留在舊推導|T:scripts/master_build_notebook.py|build_upload_notebook|signal_topic = compute_signal_topic(require_topic_secret(cfg), topic_prefix)|signal_topic = compute_signal_topic(username, topic_prefix)|D5"
  "M6 手寫的 -control 被改|T:scripts/master_build_notebook.py|build_notebook|CONTROL_TOPIC    = '{signal_topic}-control'|CONTROL_TOPIC    = '{signal_topic}-ctl'|D3"
  # ── 輸出與例外路徑 ──
  "M7 run_watch 又印主題|T:endpoint/commands.py|run_watch|console.header(\"WATCHING SIGNAL CHANNEL\")|console.header(f\"WATCHING {config.signal_topic}\")|D6"
  "M8 send_kill_signal 又插值例外物件|T:endpoint/core.py|send_kill_signal|type(e).__name__|e|D6"
  "M9 把秘密本身烘進 notebook|T:scripts/master_build_notebook.py|compute_signal_topic|digest = hashlib.sha256((TOPIC_SECRET_DOMAIN + secret).encode()).hexdigest()[:12]|digest = secret|D7"
  "M10 丟掉領域分隔字串（行為靜默）|T:endpoint/core.py|-|TOPIC_SECRET_DOMAIN = \"endpoint-signal-v1:\"|TOPIC_SECRET_DOMAIN = \"\"|D3"
  # ── 設定腳本 ──
  "M11 空跑印出指紋以外的東西|S:set-endpoint-topic-secret.sh|-|echo \"sha256:\$NEW_FP\"|echo \"sha256:\$NEW_FP \$NEW_V\"|D12"
  # ── M12／M13 是電池實測抓到的兩個真 bug，不是假想出來的 ──
  # 兩個缺陷互相掩護：`((SECRET_OK))` 在 `set -u` 下先死於 unbound variable，
  # 所以**沒有人**活著看到極性也是反的。淨效果是套用腳本根本無法套用，
  # 而唯一的症狀是一行看不懂的 shell 錯誤。它們現在各佔一個突變名額。
  "M12 秘密閘門的極性反了（有秘密也拒絕）|S:apply-endpoint-topic-secret.sh|-|SECRET_OK=1|SECRET_OK=0|D13"
  "M13 閘門不呼叫探測（讀了沒填過的變數）|S:apply-endpoint-topic-secret.sh|-|    secret_probe|    true|D13"
)

# ── 守門：這些改動**不准**讓指定的那一條叫 ────────────────
GUARDS=(
  "G1 純文字訊息改寫（標頭換句話說）|T:endpoint/commands.py|run_watch|console.header(\"WATCHING SIGNAL CHANNEL\")|console.header(\"WATCHING THE SIGNAL CHANNEL\")|=D6"
  "G2 說明文字提到 kaggle_username（D1 必須看 AST 不是看字串）|T:endpoint/core.py|topic_secret_of|three_quotes|three_quotes|=D1"
  "G3 處方訊息改寫但兩個關鍵字都留著|T:endpoint/core.py|-|Run: scripts/set-endpoint-topic-secret.sh|Fix it with: scripts/set-endpoint-topic-secret.sh|=D4"
)

# G2 的目標含三引號，寫在陣列裡會把 shell 的引號規則弄得一團亂，
# 所以在這裡用 python 組出來 —— 字面值仍然一目了然。
G2_OLD='"""Return the validated channel secret, or raise.'
G2_NEW='"""Validates the channel secret (was the kaggle_username digest), or raise.'
GUARDS[1]="G2 說明文字提到 kaggle_username（D1 必須看 AST 不是看字串）|T:endpoint/core.py|topic_secret_of|${G2_OLD}|${G2_NEW}|=D1"

# ── 欄位數檢查 ───────────────────────────────────────────
# 每一條都必須**恰好**五個 `|`。多寫一個就會被切錯，而且症狀是**靜默**的：
# 原字串被截短、取代字串從 `|` 開始，植入的是一段壞掉的程式碼 —— 測試當然
# 會失敗，於是它被算進「抓到」，但它守不住任何判準。
BADFIELDS=""
for m in "${MUTANTS[@]}" "${GUARDS[@]}"; do
    pipes="${m//[^|]/}"
    [[ ${#pipes} -eq 5 ]] || BADFIELDS+="  ${m%%|*}"$'\n'
done
if [[ -n "$BADFIELDS" ]]; then
    echo "突變清單有條目的 \`|\` 不是剛好五個 —— 欄位會被切錯，植入的是壞掉的程式碼：" >&2
    printf '%s' "$BADFIELDS" >&2
    exit 3
fi

# ── 每次突變都跑在一份完整副本上 ─────────────────────────
# 樹要整棵複製：驗證器會讀 engine/engine.py、endpoint/data/ 底下兩個出貨檔案，
# 少一個它會判定用法錯誤（exit 2）而不是回報失敗。
# 腳本目錄也要複製：D12 驅動的是**這份副本裡的**設定腳本（M11 改的就是它），
# 而且這樣才不會碰到你真正的 $HOME。
prepare() {
    rm -rf "$WORK/tree" "$WORK/scripts"
    cp -a "$SRC_TREE" "$WORK/tree"
    mkdir -p "$WORK/scripts"
    # 三個驗證器都要：C 的 D8 會跑 B 的，而 B 的會先確認 A 的逐位元沒變。
    # 少一個，被鏈住的那一條就會報「找不到兄弟檔」而紅 —— 那是**假的紅**。
    for f in lib.sh test_endpoint_topic_secret.py set-endpoint-topic-secret.sh \
             apply-endpoint-topic-secret.sh endpoint-topic-secret.patch \
             test_endpoint_tunnel_url_privacy.py test_endpoint_apikey_broadcast_fixes.py; do
        [[ -f "$SCRIPT_DIR/$f" ]] && cp -a "$SCRIPT_DIR/$f" "$WORK/scripts/$f"
    done
    # 清掉暫存樹裡可能夾帶的 .pyc，理由同上面的 PYTHONDONTWRITEBYTECODE。
    find "$WORK/tree" "$WORK/scripts" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    return 0
}

inject() {  # inject <檔案> <範圍> <原字串> <取代字串>
    python3 - "$1" "$2" "$3" "$4" 2>&1 >/dev/null <<'PY'
import ast, io, subprocess, sys

path, scope, old, new = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
src = io.open(path, encoding="utf-8").read()
lines = src.splitlines(keepends=True)

lo, hi = 1, len(lines)
if scope != "-":
    if not path.endswith(".py"):
        print("SCOPE_ON_NON_PY", file=sys.stderr)
        sys.exit(1)
    found = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and n.name == scope
    ]
    if len(found) != 1:
        print("SCOPE_NOT_UNIQUE:%d" % len(found), file=sys.stderr)
        sys.exit(1)
    lo, hi = found[0].lineno, found[0].end_lineno

hits = []
start = 0
while True:
    i = src.find(old, start)
    if i < 0:
        break
    if lo <= src.count("\n", 0, i) + 1 <= hi:
        hits.append(i)
    start = i + 1

if not hits:
    # 「找不到」與「不唯一」要分開講。合併成一句會誤導：目標字串裡多寫了一個
    # `|` 時欄位會被切錯，症狀是**找不到**，但訊息若說「不唯一」，就會去翻
    # 原始碼找重複，而該改的是清單那一行。
    print("NOT_FOUND" if src.count(old) == 0 else "NOT_IN_SCOPE:%d" % src.count(old),
          file=sys.stderr)
    sys.exit(1)
if len(hits) > 1:
    print("NOT_UNIQUE:%d" % len(hits), file=sys.stderr)
    sys.exit(1)

mutated = src[:hits[0]] + new + src[hits[0] + len(old):]

# 植入之後還要再過一關，因為「測試變紅」不等於「有斷言守住」。這一關擋的是
# **假抓到**，它有兩張臉，而且都不會讓任何一條斷言叫起來：
#   NO_OP       取代沒有改變語意（`if slack < -0:` 就是 `if slack < 0:`）。
#               它殺不掉任何東西，卻在報告裡佔一個「抓到」的名額 —— 更糟的是
#               它讓一份「全部抓到」看起來很完整。`ast.dump` 不比對註解與
#               空白，所以只改註解的那種也會在這裡現形。
#   BROKEN_CODE 植入後根本不是合法程式。測試當然會失敗，但那是模組匯入不了，
#               不是判準被守住。
# 兩者都只說明**這條突變寫壞了**，原始碼沒有問題。
if path.endswith(".py"):
    try:
        before = ast.dump(ast.parse(src))
        after = ast.dump(ast.parse(mutated))
    except SyntaxError as e:
        print("BROKEN_CODE:%s" % e.msg, file=sys.stderr)
        sys.exit(1)
    if before == after:
        print("NO_OP", file=sys.stderr)
        sys.exit(1)
elif path.endswith(".sh"):
    # shell 的等價性在這裡判不了（那要真的比語意），所以只驗語法。走 stdin
    # 而不是暫存檔：`bash -n` 的訊息就會是乾淨的 `line N`。
    p = subprocess.run(["bash", "-n"], input=mutated, capture_output=True, text=True)
    if p.returncode != 0:
        last = (p.stderr.strip().splitlines() or ["bash -n 失敗"])[-1]
        print("BROKEN_CODE:%s" % last, file=sys.stderr)
        sys.exit(1)

io.open(path, "w", encoding="utf-8").write(mutated)
PY
}

ran_fully() {  # 驗證器有沒有跑到印總結那一行
    grep -qE '^  → ' <<<"$1"
}

verdict() {  # verdict <標籤> <期望> <輸出> <結束碼> —— 回 0 表示「符合期望」
    local expect="$1" out="$2" rc="$3" want
    # **先確認它真的跑完了。** 驗證器回 2 是用法錯誤（檔案不在、樹不完整），
    # 而「跑不起來」在只看「那條判準有沒有叫」的判讀下，長得跟「守門守住了」
    # 一模一樣 —— 一個跑不起來的驗證器會讓**每一個**守門都報綠燈。
    # 這正是 D-051／D-060 的形狀：把「問不到」讀成「沒事」。實測踩到過：
    # 把突變台從別的目錄跑起來時 $SCRIPT_DIR 指錯，副本裡沒有驗證器，
    # 於是 12 個突變 + 4 個守門全部「通過」。
    if [[ "$rc" -eq 2 ]] || ! ran_fully "$out"; then
        echo "RUN_FAILED" >&2
        return 2
    fi
    if [[ "$expect" == =* ]]; then
        want="${expect#=}"
        # 守門：指定的那一條**不准**出現在失敗清單裡。
        grep -qE "^✗ (check )?${want}[: ]" <<<"$out" && return 1
        return 0
    fi
    grep -qE "^✗ (check )?${expect}[: ]" <<<"$out"
}

run_one() {  # run_one <欄位> <must_catch>
    local entry="$1" mode="$2"
    local label file scope old new expect
    label="${entry%%|*}";   entry="${entry#*|}"
    file="${entry%%|*}";    entry="${entry#*|}"
    scope="${entry%%|*}";   entry="${entry#*|}"
    old="${entry%%|*}";     entry="${entry#*|}"
    new="${entry%%|*}";     expect="${entry#*|}"

    prepare
    local target
    case "$file" in
        T:*) target="$WORK/tree/${file#T:}" ;;
        S:*) target="$WORK/scripts/${file#S:}" ;;
        *)   echo "  檔案前綴認不得：$file" >&2; return 4 ;;
    esac
    if [[ ! -f "$target" ]]; then
        echo "MISSING_FILE:$target" >&2; return 4
    fi

    local msg
    if ! msg="$(inject "$target" "$scope" "$old" "$new")"; then
        case "$msg" in
          NOT_FOUND)
            echo "植入失敗：$label —— 目標字串在 $file 裡**找不到**（原始碼改了，要更新這支腳本）" >&2 ;;
          NOT_IN_SCOPE:*)
            echo "植入失敗：$label —— 目標字串在 $file 裡有，但不在 ${scope}() 的行範圍內" >&2 ;;
          NOT_UNIQUE:*)
            echo "植入失敗：$label —— 目標字串在 ${scope}() 內出現 ${msg#NOT_UNIQUE:} 次（要挑更長的目標）" >&2 ;;
          SCOPE_NOT_UNIQUE:*)
            echo "植入失敗：$label —— ${scope} 在 $file 裡定義了 ${msg#SCOPE_NOT_UNIQUE:} 次" >&2 ;;
          SCOPE_ON_NON_PY)
            echo "植入失敗：$label —— .sh 沒有 AST，範圍要寫 \`-\`" >&2 ;;
          NO_OP)
            echo "植入失敗：$label —— 取代**沒有改變語意**，這條突變殺不掉任何斷言" >&2 ;;
          BROKEN_CODE:*)
            echo "植入失敗：$label —— 植入後不是合法程式（${msg#BROKEN_CODE:}）" >&2 ;;
          *)
            echo "植入失敗：$label（$msg）" >&2 ;;
        esac
        return 4
    fi

    local out rc=0
    out="$(cd "$WORK" && python3 scripts/test_endpoint_topic_secret.py tree 2>&1)" || rc=$?

    local v=0
    verdict "$expect" "$out" "$rc" || v=$?

    if [[ "$v" -eq 2 ]]; then
        echo "  ✗ 驗證器沒跑完：$label —— 這一條的結果**不可採信**（結束碼 $rc）"
        echo "      「跑不起來」和「守門守住了」在輸出上長得一樣。先修驗證器或這支腳本。"
        grep -vE '^\s*$' <<<"$out" | head -3 | sed 's/^/      /'
        return 1
    fi

    if [[ "$v" -eq 0 ]]; then
        local how="抓到"
        [[ "$mode" == guard ]] && how="守住了"
        printf '  \033[0;32m✓\033[0m %s  %s（期望 %s，驗證器結束碼 %d）\n' "$how" "$label" "$expect" "$rc"
        return 0
    fi

    if [[ "$mode" == guard ]]; then
        echo "  ✗ 守門破了：$label —— $expect 不該叫卻叫了（驗證器對無關的改動過度敏感）"
    else
        echo "  ✗ 漏掉！$label —— 期望 $expect 叫，但它沒叫"
        grep -E '^✗ ' <<<"$out" | head -4 | sed 's/^/      /'
    fi
    return 1
}

# ── 對照組先跑：不動任何東西，每一條判準都必須通過 ──────────
# 沒有這一步，下面「每條都被指定它的那一條抓到」可能只是因為驗證器**永遠
# 紅**：期望 `D4` 叫的斷言，在一個永遠紅的驗證器上全部成立。
echo "對照組（無突變）"
prepare
control_rc=0
control_out="$(cd "$WORK" && python3 scripts/test_endpoint_topic_secret.py tree 2>&1)" || control_rc=$?
if ! ran_fully "$control_out"; then
    echo "對照組根本沒跑起來（結束碼 $control_rc）—— 在修好之前，下面每一個綠燈都是假的："
    grep -vE '^\s*$' <<<"$control_out" | head -3 | sed 's/^/  /'
    exit 1
fi
if grep -qE '^✗ ' <<<"$control_out"; then
    echo "對照組就失敗了 —— 驗證器對未突變的樹不通過，先修它："
    grep -E '^✗ ' <<<"$control_out" | sed 's/^/  /'
    exit 1
fi
echo "  ✓ 對照組通過（驗證器不是永遠紅）"
echo

echo "── 突變：每條都必須被它指名的那一條抓到 ──"
caught=0; missed=(); broken=()
for m in "${MUTANTS[@]}"; do
    v=0; run_one "$m" mutant || v=$?
    case "$v" in
        0) caught=$((caught + 1)) ;;
        4) broken+=("${m%%|*}") ;;   # 沒植入 ≠ 沒抓到，見下面的分流
        *) missed+=("${m%%|*}") ;;
    esac
done

echo
echo "── 守門：這些改動不准讓指定的那一條叫 ──"
gcaught=0; gmissed=()
for g in "${GUARDS[@]}"; do
    v=0; run_one "$g" guard || v=$?
    case "$v" in
        0) gcaught=$((gcaught + 1)) ;;
        4) broken+=("${g%%|*}") ;;
        *) gmissed+=("${g%%|*}") ;;
    esac
done

echo
total="${#MUTANTS[@]}"; gtotal="${#GUARDS[@]}"
rc=0
# 「沒植入」要跟「沒抓到」分開講。合併的話訊息會說「驗證器有洞」—— 但植入
# 失敗時驗證器根本沒被問到，那是**這支腳本自己的清單過期了**，要改的是上面
# 那一行，不是驗證器。實測踩到過：M12／M13 對 .sh 寫了函式範圍，注入器
# 只回得出 SCOPE_ON_NON_PY，而報告把它們列成「驗證器有洞」。
if [[ "${#broken[@]}" -gt 0 ]]; then
    echo "✗ 有 ${#broken[@]} 條突變**根本沒植入** —— 這不是驗證器的洞，是清單或原始碼過期了："
    for b in "${broken[@]}"; do echo "  • $b"; done
    rc=3
fi
if [[ "${#missed[@]}" -gt 0 ]]; then
    echo "✗ 有 ${#missed[@]} 個突變沒被抓到，共 $total 個 —— 驗證器有洞："
    for m in "${missed[@]}"; do echo "  • $m"; done
    rc=1
fi
if [[ "${#gmissed[@]}" -gt 0 ]]; then
    echo "✗ 有 ${#gmissed[@]} 個守門被誤觸，共 $gtotal 個 —— 驗證器對無關的改動太敏感："
    for g in "${gmissed[@]}"; do echo "  • $g"; done
    rc=1
fi
if [[ "$rc" -eq 0 ]]; then
    echo "✓ ${caught}/${total} 個突變全數被指定的檢查抓到，且 ${gcaught}/${gtotal} 個守門未被誤觸"
    echo "  （D11 在每個突變上都會紅：它比對的是套用腳本里手寫的雜湊，突變必然使它們失效。"
    echo "    刻意如此 —— 它證明十個釘值真的在描述這棵樹。）"
fi
exit "$rc"
