#!/usr/bin/env bash
# 把 Kaggle 憑證從「只有互動 shell 讀得到」搬到「非互動 shell 也讀得到」。
#
# ── 為什麼需要一支腳本，而不是「貼一行指令」 ──────────────────
# 那把憑證原本只活在 ~/.bashrc 的一個 export 裡，而 ~/.bashrc 開頭就是
# 「非互動就 return」的守衛。於是每一個非互動的呼叫端 —— 腳本、排程、任何
# 不經過你的終端機的 endpoint 指令 —— 都是**沒有憑證**的，而它們失敗的方式
# 是「查不到」而不是「報錯」。那正是 D-060 的形狀：把「問不到」讀成「沒事」。
#
# ── 兩個消費者，兩個不同的檔案（這一點很容易記錯）────────────
# 「搬到 kaggle.json」只對一半。實測（kaggle 2.2.4）：
#
#   endpoint 自己的 REST —— core.py 的 get_kaggle_token()
#     讀 KAGGLE_API_TOKEN 環境變數，**否則** ~/.kaggle/kaggle.json 的 key 欄。
#     它把那個值當 Bearer 用。切 B 讀 kernel log、get_kernel_status 都走這條。
#
#   kaggle CLI —— kagglesdk 的 get_access_token_from_env()
#     讀 KAGGLE_API_TOKEN 環境變數，**否則** ~/.kaggle/access_token（或 .txt）。
#     **它不看 kaggle.json 的 access-token 欄。** 而 CLI 是 `endpoint kill-all`
#     的實作 —— 也就是孤兒 kernel 唯一的復原路徑。
#
# 所以兩個檔都要寫、值相同。只寫一個，就會有一條路徑安靜地沒有憑證。
#
# ── 它刻意不做的事 ───────────────────────────────────────────
#   · **不印值。** 全程只印 sha256 前 12 碼（指紋）。指紋足以確認「兩處是不是
#     同一把」，但推不回那把值。
#   · **不接受從命令列傳值。** argv 是 `ps` 看得到的；值只從 rc 檔讀。
#   · **不動 ~/.bashrc**，除非你明確要 --remove-rc。
#   · **不覆寫既有憑證。** 目標檔已存在且值不同時停下來（回 2），請你自己決定
#     怎麼處置它。本腳本不建秘密備份，也不把別人的憑證蓋掉。值相同的重跑是
#     安全的（它認得「已經搬過了」）。
#   · **不碰 Kaggle。** 不 boot、不 push、不殺 kernel、不改任何設定。
#
# ── 「不知道」不等於「沒事」（D-060）────────────────────────
# 驗證分兩層，而它們的性質不一樣：
#
#   離線層（檔案在不在、權限、兩處的值是否相同）—— **精確**，可以當閘門。
#   線上層（兩個消費者真的能認證嗎）—— 要網路，回三態 ok／rejected／unknown。
#
# unknown（網路不通、逾時、讀不到）**不會**被印成成功，也不會被印成失敗；
# 它就是「不知道」，而 --check 對它回 2。把 unknown 讀成「沒事」是本專案
# 反覆咬人的那個病，這裡不重犯。
#
# ── 順序：先搬家，再刪 rc ────────────────────────────────────
# --remove-rc 有一道**離線且精確**的硬閘門：兩個目標檔都必須存在、權限必須是
# 0600、且值必須與 rc 那一行相同。沒有這一關，「先搬家、後刪行」就只是口頭
# 約定 —— 而刪掉最後一份可讀的憑證卻沒有新的，會讓互動 shell 也一起失去憑證。
#
# ⚠️ --remove-rc 建的備份**是那把值的明文副本**。~/.bashrc 通常是 0644，所以
#    `cp -p` 會連寬鬆的權限一起複製 —— 本腳本會把它 chmod 600，並且在收尾
#    訊息裡提醒你處置它。
#
# 用法：
#   bash scripts/migrate-kaggle-token.sh --dry-run    只印計畫，什麼都不寫
#   bash scripts/migrate-kaggle-token.sh              搬家（重跑是安全的）
#   bash scripts/migrate-kaggle-token.sh --check      只驗證現況，不改任何東西
#   bash scripts/migrate-kaggle-token.sh --remove-rc  備份後刪掉 rc 那一行
#
# 結束碼：0 ＝ 完成且驗證全綠（或 --check 全綠，rc 殘留只是警告）
#         1 ＝ 寫入失敗，**或**寫入成功但驗證**確定**失敗（401／403）
#         2 ＝ 無法判定／無法完成 → 沒有改任何東西
#         3 ＝ 這支腳本自己的前提壞了（rc 裡不是恰好一筆、值的形狀不合）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

RC="$HOME/.bashrc"
CFG="$HOME/.config/endpoint/endpoint-config.yaml"
KDIR="$HOME/.kaggle"
KJSON="$KDIR/kaggle.json"
KTOK="$KDIR/access_token"

# rc 裡那一行的**前綴**樣式。值不在樣式裡 —— 它只用來定位與計數。
RE_RC='^[[:space:]]*export[[:space:]]+KAGGLE_API_TOKEN='
# 值的形狀。夠寬以容納未來長度變化，夠窄以擋掉「整行連尾端註解一起被讀進來」
# 那一類錯誤 —— 那種錯誤會安靜地寫進檔案，然後在很遠的地方以 401 出現。
RE_TOK='^[A-Za-z0-9_][A-Za-z0-9_.-]*$'

MODE="migrate"
ASSUME_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   MODE="dry" ;;
    --check)     MODE="check" ;;
    --remove-rc) MODE="remove-rc" ;;
    --yes|-y)    ASSUME_YES=true ;;
    -h|--help) usage_text "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) fail "未知的參數：$1（可用：--dry-run、--check、--remove-rc、--yes、-h）"; exit 2 ;;
  esac
  shift
done

# ── 小工具 ───────────────────────────────────────────────
fp()   { printf '%s' "$1" | sha256sum | cut -c1-12 | sed 's/^/sha256:/'; }
mode() { stat -c '%a' "$1" 2>/dev/null || printf '----'; }

# 從 rc 讀出那把值。**不是恰好一筆就失敗** —— 兩筆代表前提不成立，而在那種
# 狀態下任何取代動作都是猜的（rotate-endpoint-key.sh 立的同一條紀律）。
# 回 0 ＝ 恰好一筆；1 ＝ 沒有；2 ＝ 不只一筆。
read_rc_token() {
  local n
  n="$(grep -cE "$RE_RC" "$RC" 2>/dev/null || true)"
  [[ "${n:-0}" -eq 1 ]] || { [[ "${n:-0}" -eq 0 ]] && return 1 || return 2; }
  grep -m1 -E "$RE_RC" "$RC" | sed 's/^[^=]*=//' | tr -d '\042\047' | tr -d '\r'
}

# ── 找一個有 yaml 的 python ───────────────────────────────
# config 是 YAML，用正規表示式讀縮排與引號的組合會在某個寫法上安靜地讀錯。
PY=""
for c in "${ENDPOINT_TOOL_DIR:-$HOME/.local/share/uv/tools/endpoint-vps}/bin/python3" python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import yaml' >/dev/null 2>&1; then PY="$c"; break; fi
done

need_python() {
  [[ -n "$PY" ]] && return 0
  fail "找不到一個有 yaml 的 python —— 讀不出 config 裡的 kaggle_username。"
  echo "  試過：${ENDPOINT_TOOL_DIR:-$HOME/.local/share/uv/tools/endpoint-vps}/bin/python3 與 PATH 上的 python3"
  exit 2
}

# ── 讀 config 裡的帳號（只回值，永不印）────────────────────
read_username() {
  "$PY" - "$CFG" <<'PYEOF'
import sys, pathlib, yaml
d = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
v = ((d.get("identity") or {}).get("kaggle_username")) or ""
sys.stdout.write(str(v).strip())
PYEOF
}

# ── 比對既有的 kaggle.json（只印判定字，永不印值）──────────
# 期待值走 stdin、帳號走 argv —— 兩者都不進 argv 的是那把值。
json_state() {   # <路徑> <帳號>；期望的值在 stdin
  "$PY" -c '
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); want_user = sys.argv[2]
want_key = sys.stdin.read().strip()
if not p.exists():
    print("absent"); sys.exit(0)
try:
    d = json.loads(p.read_text())
except Exception:
    print("unreadable"); sys.exit(0)
if not isinstance(d, dict):
    print("unreadable"); sys.exit(0)
print("same" if (d.get("username") == want_user and d.get("key") == want_key) else "diff")
' "$1" "$2"
}

# ── 兩個線上探針：三態 ────────────────────────────────────
# 刻意先把 KAGGLE_API_TOKEN 從環境拿掉 —— 否則互動 shell 裡那個 export 會
# 讓探針「通過」，而它證明的其實是環境變數還在，不是檔案可用。
probe_rest() {
  env -u KAGGLE_API_TOKEN "$PY" - 2>/dev/null <<'PYEOF' || printf 'unknown probe-error'
import sys
try:
    import requests
    from endpoint.core import Config, get_kaggle_token
except Exception:
    print("unknown import-error"); sys.exit(0)
t = get_kaggle_token()
if not t:
    print("nokey"); sys.exit(0)
# 打的是**切 B 自己用的那個端點**，不是 /api/v1/account。這不是偏好問題：
# 2026-09-29 實測，/api/v1/account 帶一把壞 Bearer 與**完全不帶**都回 404 ——
# 它對憑證有沒有效**沒有訊號**，拿它當探針只會製造假安心。api.kaggle.com 這個
# 帶壞 Bearer 回 403，才是有訊號的那一個。順帶：200 也正好是切 B 的前提
# （讀得到 kernel log）。
try:
    owner, slug = Config().kernel_id.split("/", 1)
except Exception:
    print("unknown no-kernel-id"); sys.exit(0)
try:
    r = requests.post(
        "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernelSessionOutput",
        json={"userName": owner, "kernelSlug": slug},
        headers={"Authorization": "Bearer %s" % t, "Content-Type": "application/json"},
        timeout=15)
except Exception:
    print("unknown network"); sys.exit(0)
if r.status_code == 200:
    print("ok")
elif r.status_code in (401, 403):
    print("rejected %d" % r.status_code)
else:
    print("unknown %d" % r.status_code)
PYEOF
}

# CLI 的輸出可能含帳號，所以它從頭到尾只活在這個函式的區域變數裡，永不外流。
probe_cli() {
  local rc=0 out=""
  out="$(env -u KAGGLE_API_TOKEN PATH="$HOME/.local/bin:$PATH" \
         kaggle kernels list --mine --csv 2>&1)" || rc=$?
  if [[ "$rc" -eq 0 ]]; then printf 'ok'
  elif [[ "$out" == *"Authentication required to call the Kaggle API."* ]]; then printf 'rejected %d' "$rc"
  elif [[ "$rc" -eq 127 ]]; then printf 'unknown no-cli'
  else printf 'unknown %d' "$rc"; fi
}

# 印一句人話。三態各有各的句子 —— **unknown 不借用成功或失敗的句子**。
say_probe() {   # <名稱> <三態字串>
  case "$2" in
    ok)        ok   "  $1：可以認證（ok）" ;;
    nokey)     fail "  $1：讀不到值（nokey）—— 檔案在，但內容是空的" ;;
    rejected*) fail "  $1：**被拒絕**（$2）—— 憑證不對或已失效" ;;
    unknown*)  warn "  $1：**不知道**（$2）—— 這不等於失敗，也不等於成功" ;;
    *)         warn "  $1：無法判定的回應（$2）" ;;
  esac
}

# 三態 → 整體判定。0 全綠；1 確定壞；2 有一格不知道。
verdict_of() {   # <三態字串…>
  local v bad=0 unk=0
  for v in "$@"; do
    case "$v" in
      ok) ;; nokey|rejected*) bad=1 ;;
      *) unk=1 ;;
    esac
  done
  [[ "$bad" -eq 1 ]] && { printf '1'; return; }
  [[ "$unk" -eq 1 ]] && { printf '2'; return; }
  printf '0'
}

# ════════════════════════════════════════════════════════════
# 讀（在動任何位元組之前，先全部讀完）
# ════════════════════════════════════════════════════════════
need_python

[[ -r "$RC" ]] || { fail "讀不到 $RC"; exit 2; }
[[ -r "$CFG" ]] || { fail "讀不到 $CFG —— 沒有它讀不出 kaggle_username"; exit 2; }

rc_tok=""; rc_rc=0
rc_tok="$(read_rc_token)" || rc_rc=$?
case "$rc_rc" in
  0) ;;
  # rc 那一行已經清掉，是 --check 的正常終態（不是錯誤）。其餘模式沒有來源，
  # 不能動 —— 但 --check 仍要能回答「現在的狀態對不對」。
  1) [[ "$MODE" == "check" ]] || { fail "「$RC」裡沒有那一行 —— 可能已經搬過、或從來沒有過。"; exit 2; } ;;
  2) fail "「$RC」裡有**不只一筆** KAGGLE_API_TOKEN —— 前提不成立，取代動作會是猜的。" ; exit 3 ;;
esac
if [[ "$rc_rc" -eq 0 ]]; then
  [[ "$rc_tok" =~ $RE_TOK ]] || { fail "從「$RC」讀到的值形狀不合（可能連尾端註解一起讀進來）—— 沒有改任何東西。"; exit 3; }
fi

# 要比對的「權威值」。正常來自 rc；rc 已經清乾淨時（清理之後的正常狀態）就用
# access_token 當基準 —— 這樣「兩個目標檔是否一致」在 rc 刪掉之後仍然驗得到。
ref="$rc_tok"
if [[ "$rc_rc" -ne 0 && -e "$KTOK" ]]; then
  ref="$(tr -d '\r\n' < "$KTOK" 2>/dev/null || true)"
fi

user="$(read_username)"
[[ -n "$user" ]] || { fail "「$CFG」的 identity.kaggle_username 是空的 —— 沒有它填不了 kaggle.json。"; exit 2; }

# 其他的憑證來源（只數行數，不讀值）。若它們存在，互動 shell 會拿到與檔案
# **不同**的憑證 —— 那是一種「兩邊都說自己對」的狀態，值得先講出來。
other_src="$(grep -cE '^[[:space:]]*export[[:space:]]+KAGGLE_(USERNAME|KEY)=' "$RC" 2>/dev/null || true)"

if [[ -n "$ref" ]]; then
  json_st="$(printf '%s' "$ref" | json_state "$KJSON" "$user")"
else
  json_st="unreadable"
fi
if [[ -e "$KTOK" ]]; then
  if [[ -n "$ref" && "$(tr -d '\r\n' < "$KTOK" 2>/dev/null || true)" == "$ref" ]]; then tok_st="same"; else tok_st="diff"; fi
else
  tok_st="absent"
fi

fp_rc="$(fp "$rc_tok")"
# ════════════════════════════════════════════════════════════

# ── --dry-run：只印計畫 ──────────────────────────────────
if [[ "$MODE" == "dry" ]]; then
  echo "── 來源 ──"
  echo "  $RC 的那一行 : 1 筆，$fp_rc"
  echo "  $CFG 的帳號 : 讀得到（不印）"
  [[ "${other_src:-0}" -gt 0 ]] && warn "  ⚠️ $RC 裡另有 $other_src 行 KAGGLE_USERNAME／KAGGLE_KEY —— 互動 shell 會拿到與檔案不同的憑證"
  echo
  echo "── 目標（會建立 $KDIR 為 0700）──"
  echo "  $KTOK  →  值相同（${tok_st}）"
  echo "  $KJSON →  帳號＋值相同（${json_st}）"
  echo
  if [[ "$json_st" == "diff" || "$json_st" == "unreadable" || "$tok_st" == "diff" ]]; then
    warn "目標已存在且**值不同** —— 搬家會停下來（回 2），不覆寫。"
  elif [[ "$json_st" == "same" && "$tok_st" == "same" ]]; then
    info "兩個目標都已經是同一把值 —— 重跑不會改任何東西（idempotent）。"
  else
    info "會寫入上面標成 absent 的那些檔（0600）。"
  fi
  exit 0
fi

# ── 搬家 ─────────────────────────────────────────────────
if [[ "$MODE" == "migrate" ]]; then
  if [[ "$json_st" == "unreadable" ]]; then
    fail "「$KJSON」存在但不是可解析的 JSON —— 沒有改任何東西。"
    echo "  請你自己看它一眼（或先移開），再跑一次。本腳本不覆寫既有的憑證檔。"
    exit 2
  fi
  if [[ "$json_st" == "diff" || "$tok_st" == "diff" ]]; then
    fail "目標已存在且**值不同** —— 沒有改任何東西。"
    echo "  本腳本不覆寫既有憑證、也不建秘密備份。請你自己決定怎麼處置它"
    echo "  （例如先 mv 到別的地方），再跑一次。"
    exit 2
  fi

  tmp_a=""; tmp_b=""
  cleanup() { [[ -n "$tmp_a" ]] && rm -f "$tmp_a"; [[ -n "$tmp_b" ]] && rm -f "$tmp_b"; return 0; }
  trap cleanup EXIT

  umask 077
  mkdir -p "$KDIR"
  chmod 700 "$KDIR"

  wrote=0
  if [[ "$tok_st" != "same" ]]; then
    tmp_a="$(mktemp "$KDIR/.access_token.XXXXXX")"
    printf '%s' "$rc_tok" > "$tmp_a"
    mv -f "$tmp_a" "$KTOK"; tmp_a=""
    chmod 600 "$KTOK"
    ok "寫入 $KTOK（0600）"
    wrote=$((wrote + 1))
  fi
  if [[ "$json_st" != "same" ]]; then
    tmp_b="$(mktemp "$KDIR/.kaggle.json.XXXXXX")"
    printf '%s' "$rc_tok" | "$PY" -c '
import json, sys
json.dump({"username": sys.argv[1], "key": sys.stdin.read().strip()},
          open(sys.argv[2], "w"))
' "$user" "$tmp_b"
    mv -f "$tmp_b" "$KJSON"; tmp_b=""
    chmod 600 "$KJSON"
    ok "寫入 $KJSON（0600，含 username）"
    wrote=$((wrote + 1))
  fi
  [[ "$wrote" -eq 0 ]] && info "兩個目標都已經是同一把值 —— 沒有寫任何東西（重跑是安全的）。"
fi

# ════════════════════════════════════════════════════════════
# 驗證（搬家與 --check 共用）
# ════════════════════════════════════════════════════════════
bad=0
echo
echo "── 離線層（精確，可以當閘門）──"
if [[ -e "$KDIR" && "$(mode "$KDIR")" == "700" ]]; then ok "  $KDIR 權限 700"
else fail "  $KDIR 權限是 $(mode "$KDIR")（要 700）"; bad=1; fi

for f in "$KTOK" "$KJSON"; do
  if [[ ! -e "$f" ]]; then fail "  缺少 $f"; bad=1
  elif [[ "$(mode "$f")" != "600" ]]; then fail "  $f 權限是 $(mode "$f")（要 600）"; bad=1
  else ok "  $(basename "$f") 權限 600"; fi
done

if [[ -e "$KTOK" && -e "$KJSON" ]]; then
  st="$(tr -d '\r\n' < "$KTOK" | json_state "$KJSON" "$user")"
  case "$st" in
    same) ok   "  兩處的值相同（$(fp "$(tr -d '\r\n' < "$KTOK")")）" ;;
    diff) fail "  兩處的值**不同** —— 一定有一條路徑會拿到錯的憑證"; bad=1 ;;
    *)    fail "  kaggle.json 不是可解析的 JSON（$st）"; bad=1 ;;
  esac
  if [[ "$rc_rc" -eq 0 ]]; then
    if [[ "$(tr -d '\r\n' < "$KTOK")" == "$rc_tok" ]]; then ok "  與 $RC 那一行相同"
    else fail "  與 $RC 那一行**不同**"; bad=1; fi
  fi
fi

if [[ "${other_src:-0}" -gt 0 ]]; then
  warn "  ⚠️ $RC 裡另有 $other_src 行 KAGGLE_USERNAME／KAGGLE_KEY"
fi

if [[ "$rc_rc" -eq 0 ]]; then
  warn "  $RC 那一行還在（世界可讀的檔案裡有明文憑證）—— 要清掉請跑 --remove-rc"
fi

echo
echo "── 線上層（要網路；unknown ≠ 失敗）──"
r_rest="$(probe_rest)"
r_cli="$(probe_cli)"
say_probe "endpoint REST（切 B 讀 kernel log 走這條）" "$r_rest"
say_probe "kaggle CLI（endpoint kill-all 走這條）    " "$r_cli"

vd="$(verdict_of "$r_rest" "$r_cli")"
[[ "$bad" -eq 1 ]] && vd=1
echo
case "$vd" in
  0) ok   "驗證全綠：非互動 shell 讀得到憑證，兩個消費者都通。" ;;
  1) fail "驗證**失敗**：有東西確定是壞的（見上）。" ;;
  2) warn "驗證**不完整**：離線層精確且通過，但線上層有一格是「不知道」。" ;;
esac

# ── --check 到這裡結束 ───────────────────────────────────
if [[ "$MODE" == "check" ]]; then
  [[ "$vd" == "0" && "$bad" -eq 0 ]] && exit 0
  exit "$vd"
fi

# ════════════════════════════════════════════════════════════
# --remove-rc
# ════════════════════════════════════════════════════════════
if [[ "$MODE" == "remove-rc" ]]; then
  # 硬閘門：先確認新的兩份**真的在**，才准刪掉舊的那一份。
  if [[ "$bad" -eq 1 || ! -e "$KTOK" || ! -e "$KJSON" ]]; then
    fail "拒絕刪除 $RC 那一行：新的兩份還不算站穩（見上面的離線層）。"
    echo "  刪掉最後一份可讀的憑證卻沒有新的，會讓互動 shell 也一起失去憑證。"
    exit 2
  fi
  if [[ "$rc_rc" -ne 0 ]]; then
    fail "「$RC」裡現在不是恰好一筆 KAGGLE_API_TOKEN（狀態碼 $rc_rc）—— 沒有改任何東西。"
    exit 2
  fi

  if [[ "$ASSUME_YES" != true ]]; then
    echo
    echo "即將："
    echo "  1. 把 $RC 備份成 $RC.bak-<時間> 並 chmod 600（**它是那把值的明文副本**）"
    echo "  2. 從 $RC 刪掉**恰好那一行**（其餘一個位元組都不動）"
    if [[ -t 0 ]]; then
      printf '要繼續請打 yes：'
      read -r reply
      [[ "$reply" == "yes" ]] || { info "已取消"; exit 0; }
    else
      fail "沒有互動輸入可用 —— 已取消（要跳過確認請用 --yes）。"
      exit 2
    fi
  fi

  bak="$RC.bak-$(date +%Y%m%d-%H%M%S)"
  cp -p "$RC" "$bak"
  chmod 600 "$bak"          # cp -p 會把 0644 一起帶過來，而那是秘密的副本
  n_before="$(wc -l < "$RC")"
  # ⚠️ sed 的位址**要斜線**（/RE/d）。寫成裸樣式是 awk 的語法，sed 會把 `^`
  # 當成指令而吐 `unknown command` —— 那不是「沒事」，只是一句話都沒說就停住。
  # RE_RC 裡不含 `/`，所以直接夾起來是安全的。
  if ! sed -i -E "/${RE_RC}/d" "$RC"; then
    fail "sed 刪除失敗 —— $RC **沒有改動**（備份仍在 $bak）"
    exit 1
  fi
  n_after="$(wc -l < "$RC")"

  if ! bash -n "$RC" 2>/dev/null; then
    cp -p "$bak" "$RC"
    fail "改完之後 $RC 語法檢查不通過 —— **已從備份還原**，沒有改動。"
    exit 1
  fi
  if [[ "$((n_before - n_after))" -ne 1 ]] || [[ "$(grep -cE "$RE_RC" "$RC" || true)" -ne 0 ]]; then
    cp -p "$bak" "$RC"
    fail "刪掉的不是恰好一行（$n_before → $n_after）—— **已從備份還原**，沒有改動。"
    exit 1
  fi

  ok "已刪掉 $RC 那一行（$n_before → $n_after），語法檢查通過"
  echo
  warn "備份在 $bak（0600）—— **它就是那把值的明文副本**。"
  echo "  確認幾天沒事之後請自己刪掉它：rm -f '$bak'"
  echo "  開一個**新的**終端機之後，KAGGLE_API_TOKEN 應該是 unset，而 endpoint 仍要能動。"
fi

exit "$vd"
