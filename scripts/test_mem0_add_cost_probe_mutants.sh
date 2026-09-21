#!/usr/bin/env bash
# 突變測試：把 mem0_add_cost_probe.py 的評分器**故意弄壞**，確認
# test_mem0_add_cost_probe.py 真的會叫。
#
# 為什麼需要這一步（D-024 第八節，同樣的理由再來一次）：一組「永遠通過」的
# 測試和「永遠失敗」的測試一樣沒用。把受測程式碼修好、測試跟著變綠，這件事
# 本身不證明測試有在檢查任何東西。**唯一能證明測試有效的方法，是讓它面對
# 一個已知的錯誤，然後看它有沒有叫。**
#
# 上一次（item 2）跑這一步時抓到了一個真缺口：斷言只檢查了判準的編號，
# 沒檢查它講的是哪一種失敗，所以「把 None 的情況推進另一個分支」的突變
# 不會叫。D-026 第五節記的就是這件事。
#
# **這一次寫清單時〈在跑之前〉就又抓到一次**：`canary_reading` 的斷言原本
# 寫 `"開頭" in reason`，但反轉方向的訊息裡也有「開頭」兩個字 —— 所以
# 「把兩個方向對調」的突變會穿過去。斷言改成 `"模型自己的指令"` 才擋得住。
# 這一條留著當例子：突變清單要在寫測試的**同時**想，不是事後補。
#
# **第一次跑的時候有三個突變沒被抓到，而三個是同一種錯**：探針裡有好幾處
# `reason` 是**多行字串串接**，我只改了第一行，可區辨的那句話留在第二行，
# 所以突變其實**沒有改變任何行為**。一個沒改變行為的突變，從外面看起來
# 跟「測試有洞」一模一樣 —— 這正是突變測試要逼你面對的東西：倖存的突變
# 要先懷疑突變本身，再懷疑測試。改目標時要挑**帶著那句話的那一行**。
#
# **第二次跑之前又補了一條**：多行的突變目標要用 `$'...'`（ANSI-C 引號），
# 一般的雙引號不解析 `\n`。用雙引號寫的話，送進去的是字面上的反斜線加 n，
# 在檔案裡的出現次數是 0 —— 而「植入失敗」的訊息長得跟「測試有洞」很像，
# 一不小心就會去改探針，但該改的是這支腳本。
#
# **同一輪又遇到 no-op 突變，但成因是新的**：`len(pts) < 2` → `< 1` 原本
# 會讓單點走到 `pts[0], pts[-1]` 而除以零；後來加了 `c1 == c0` 的防護之後，
# 單點改走防護那條路，回傳的形狀與原本**完全一樣**，只有訊息不同 ——
# 於是這個突變靜默地失去了意義。兩件事都要記：
#   · 加了一道防護，可能讓別的突變變成 no-op —— 加完要重跑整份清單
#   · 形狀相同、只有訊息不同的兩條路徑，測試必須斷言**訊息**
#     （這正是 D-026 第五節那條，這次是第二次踩到）
#
# **軸換掉之後再一次**：E1/E2 把上限的自變數從 num_ctx 改成
# num_ctx − num_predict，`min_ctx_for()` 多了一個必填參數、`return 0` 的
# 分支被改成夾在 0 —— 三條突變的目標字串因此**在原始碼裡消失**。
# 這一次腳本自己叫了（NOT_FOUND 分支），沒有靜默通過；那個分支就是為了
# 這一刻存在的。留下的教訓：**清單綁的是原始碼的字面，不是行為**，
# 所以任何一次改動評分函式都要重跑，不能只看測試有沒有紅。
#
# **第四次是整組換掉，而原因是這一輪最重要的那件事**：上面那次「軸換掉」
# 本身就是錯的 —— 新的軸（num_ctx − num_predict）與被推翻的公式一起被
# 撤掉了，`ctx_prompt_limit()`／`min_ctx_for()` 整組刪除，改成閉式解
# `truncated_length()` 加兩個核對函式。所以這份清單不是「補幾條」，
# 是**整段重寫**。
#
# 記下這一條的形狀：突變清單會**跟著錯的假設一起長大**。上一輪那六條
# 「上限關係：…」每一條都在守那個錯公式的內部一致性（斜率、截距、
# 取整方向），守得很紮實 —— 而它們守的東西是錯的。突變測試證明的是
# 「測試盯得住實作」，不是「實作盯得住現實」。這兩件事要分開看，
# 否則一份 41/41 的突變報告會變成信心的來源，而不是懷疑的起點。
#
# **第五次是行為刻意改變，而清單裡有一條因此失效。** run2 讓
# `canary_reading` 的「兩端都看不到」從 `asymmetric: False` 改成 `None`
# —— 那不是改字面，是改語意。舊的突變「C3：不看 tail 是否看得到」綁在
# `if canary.get(...)` 那一行上，而那一行現在是 `elif`，於是植入失敗。
#
# 這次要記的是**處理方式**：植入失敗的突變不可以直接刪掉。那一條守的
# 意圖（「不能只看 asymmetric、不看 tail」）在新結構下**仍然成立**，所以
# 是把它移到新的字面（`elif ...`），而不是讓它消失。刪掉一條守著真行為
# 的突變，換來的是一份漂亮的 55/55 報告 —— 那正是上面那段在警告的事。
#
# 而且這一次是**在同一個 session 裡**踩到的：改完評分函式立刻重跑，腳本
# 自己叫了。第四次的教訓是「任何一次改動評分函式都要重跑」，這次是它
# 第一次照著做，也第一次證明那條教訓是有用的。
#
# **第六次是同一個缺陷的第二個位置，外加補上「誰跑的」。** 修完 C3 之後
# 往旁邊看，發現 `thinking_verdict()` 有**一模一樣**的洞：它的輸入沒有
# `num_predict`，所以分不出「模型講完了」與「被預算切斷」，於是把
# `256 − 72 = 184` 講成真值 —— 而那個 256 正是上限。新增五條守它（三態的
# 下界／點值／沒查、以及「被切斷的沒下降不可以當反例」）。同時補上
# `probe_revision()` 的四條 —— 那守的是**這份清單自己的前提**：run2 跑完
# 之後探針被改過，而「那組數字是哪一版跑的」當時只能靠一個殘留的 .pyc 做
# bytecode 比對才答得出來。**一份突變報告如果講不出它守的是哪一版，
# 它守的東西就沒有歸屬。**
#
# 做法：把探針複製到暫存目錄、用字串取代植入突變、從那份副本跑測試。
# 原始檔從頭到尾不被修改。每個突變都是「把一道判準換成 `if False`」或
# 「換成一個看起來差不多、語意不同的寫法」。
#
# 用法：bash scripts/test_mem0_add_cost_probe_mutants.sh
#
# 結束碼：0 = 每個突變都被抓到，且對照組通過
#         1 = 有突變沒被抓到（測試有洞）、或對照組失敗（測試壞了）

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$SCRIPT_DIR/mem0_add_cost_probe.py"
TEST="$SCRIPT_DIR/test_mem0_add_cost_probe.py"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# **不要寫 .pyc。** 突變是用字串取代植入的，而 CPython 對 .pyc 的有效性
# 檢查只比對「原始檔的 mtime（**秒**）與大小」。兩條連續的突變若長度相同
# 又落在同一秒內，第二次寫入就會命中第一次留下的 .pyc —— 那個突變**根本
# 沒有被執行**，測試當然通過，而突變台把它報成「抓到」。
# 實測：`return 1` → `return 2` 這種同長度取代，在同一秒內被完全忽略。
# 一個會謊報「抓到」的突變台是 fail-open —— 比沒有突變台更糟，因為它讓
# 「全部抓到」這句話變成假的。
export PYTHONDONTWRITEBYTECODE=1

# ── 對照組先跑：不動任何東西，測試必須通過 ──────────────
# 沒有這一步，下面「每個突變都被抓到」可能只是因為測試**永遠失敗**。
info "對照組（無突變）"
if ! python3 "$TEST" >/dev/null 2>&1; then
  fail "對照組就失敗了 —— test_mem0_add_cost_probe.py 本身有問題，先修它"
  exit 1
fi
ok "對照組通過（測試不是永遠失敗）"
echo

# 突變清單：label|原字串|取代字串
# 每一條都對應一個判準或一個純函式的判斷。原字串必須在檔案裡唯一出現一次。
MUTANTS=(
  # ── truncation_verdict（C2 的核心）──
  "截斷：把 >= 也當成有落差|    if full_pec > default_pec:|    if full_pec >= default_pec:"
  "截斷：丟掉的數量算錯|            \"dropped\": full_pec - default_pec,|            \"dropped\": default_pec,"
  "截斷：反轉的結果講成沒問題|        \"非預期，先查清楚再下結論，不要當成沒截斷。\"|        \"數值正常。\""
  "截斷：只檢查其中一個計數器|    if not default_pec or not full_pec:|    if not default_pec and not full_pec:"
  # ── canary_reading（C3 的核心）──
  "canary：head 一律算看到|    head_seen = head_marker in text|    head_seen = True"
  "canary：單向截斷不算數|            \"asymmetric\": False,|            \"asymmetric\": None,"
  # **第六次清單對不上原始碼，而這次的原因就是那個修法本身**：這一條原本
  # 綁在 `if tail_seen and not head_seen:` 上，而那一行已經被改成讀**位置**
  # （`if end_seen and not begin_seen:`）—— 綁標籤的條件整條不見了，因為
  # 綁標籤就是那個 bug。它叫了 NOT_FOUND，沒有靜默通過。
  "canary：兩個方向對調|    if end_seen and not begin_seen:|    if False:"
  "canary：反過來的情況不特別講|            \"這比較不尋常，值得再看一眼。\"|            \"\""
  # ── canary：run2 的迴歸（「沒有量到」不可以長得像量測結果）──
  # 這一組守的是 run2 抓到的那個洞：模型沒講完（生成停在 num_predict），
  # 而判定把它讀成了「量到：兩端都看不到」。
  $'canary：兩端都看不到又講成對稱|            "head_seen": False,\n            "tail_seen": False,\n            "asymmetric": None,|            "head_seen": False,\n            "tail_seen": False,\n            "asymmetric": False,'
  "canary：預算被切斷也當成可用|    if eval_count >= num_predict:|    if False:"
  "canary：空回覆也當成可用|    if not str(chat.get(\"content\") or \"\").strip():|    if False:"
  "canary：讀不到計數講成可用|        return None, (\"讀不到 eval_count|        return True, (\"讀不到 eval_count"
  "canary：沒講完也照樣判方向|    if usable is not True:|    if False:"
  # ── grade：沒量到與量到反例要分開講 ──
  "判準：分不出沒量到與量到反例|    if canary.get(\"answer_usable\") is not True:|    if False:"
  # ── C3 的位置對照組：沒有它，「尾端看得到」排除不掉「照著字面回答」──
  "C3：不看位置對照組|    elif rev.get(\"head_seen\") is not True or rev.get(\"tail_seen\") is not False:|    elif False:"
  "C3：對照組沒量到也當成判準沒過|    if rev.get(\"answer_usable\") is not True:|    if False:"
  # ── canary：標籤 vs 位置 ──
  # 這一組守的是 2026-09-21 重測當場抓到的東西：欄位全對，但**句子說反了
  # 方向** —— 因為它照 marker 的標籤下判斷，而在鏡像那一次，`head` 標籤
  # 坐在結尾。這一組的兩條突變都不動任何一個回傳欄位，只動句子。
  "canary：忽略排版，照標籤下判斷|    begin_seen = head_seen if head_first else tail_seen|    begin_seen = head_seen"
  $'canary：位置映射整個對調|    begin_seen = head_seen if head_first else tail_seen\n    end_seen = tail_seen if head_first else head_seen|    begin_seen = tail_seen if head_first else head_seen\n    end_seen = head_seen if head_first else tail_seen'
  # ── prompt_chars／duplicate_evidence：長度只能從 messages 量 ──
  # 「讀不到」與「量到 0」在 JSON 裡必須分得出來；而兩次被截到同一個上限
  # 的 prompt_eval_count 相減永遠是 0 —— 那個 0 是儀器的天花板。
  $'prompt_chars：讀不到回 0 而不是 None|        return None\n    total = 0|        return 0\n    total = 0'
  "prompt_chars：只算第一則訊息|    for m in msgs:|    for m in msgs[:1]:"
  $'prompt_chars：沒有 content 的訊息當成空字串|        if content is None:\n            return None|        if content is None:\n            content = ""'
  "duplicate：用 prompt_eval_count 相減|        out[\"prompt_grew_chars\"] = c2 - c1|        out[\"prompt_grew_chars\"] = (chat2.get(\"prompt_eval_count\") or 0) - (chat1.get(\"prompt_eval_count\") or 0)"
  $'duplicate：讀不到也放 0|    if c1 is not None and c2 is not None:\n        out["prompt_chars_first"] = c1\n        out["prompt_chars_second"] = c2\n        out["prompt_grew_chars"] = c2 - c1|    out["prompt_chars_first"] = c1 or 0\n    out["prompt_chars_second"] = c2 or 0\n    out["prompt_grew_chars"] = (c2 or 0) - (c1 or 0)'
  # ── observations：第五處 ──
  "觀察：第二次 add 指名一個沒觀測到的成因|                    \"—— 兩次都被截斷到同一個上限，所以 prompt_eval_count \"|                    \"—— 差別來自 Phase 1 檢索回來的既有記憶，所以 prompt_eval_count \""
  "觀察：讀不到長度時說沒有變長|            grew = (\"讀不到攔截到的 messages（%r／%r），所以**無從比較**\"|            grew = (\"讀不到攔截到的 messages（%r／%r），所以沒有變長\""
  # ── exit_code：順序就是規格 ──
  "結束碼：儀器壞掉講成判準沒過|    if any(s is False for s in states):|    if False:"
  "結束碼：對照組缺席也算通過|    if canary and any(s is None for s in states):|    if False:"
  "結束碼：沒跑滿也算通過|    if sections is None or tuple(sections) != tuple(ALL_SECTIONS):|    if False:"
  # ── probe_revision：一次執行要講得出自己是誰跑的 ──
  # 這一組守的是 D-027 的鑑識成本：run2 跑完之後探針被改過，而「那組數字是
  # 哪一版跑的」只能靠一個殘留的 .pyc 做 bytecode 比對才答得出來。
  "修訂版：指紋寫成常數|        \"sha256_16\": hashlib.sha256(data).hexdigest()[:16],|        \"sha256_16\": \"0123456789abcdef\","
  "修訂版：指紋算的是長度不是內容|        \"sha256_16\": hashlib.sha256(data).hexdigest()[:16],|        \"sha256_16\": hashlib.sha256(str(len(data)).encode()).hexdigest()[:16],"
  "修訂版：讀不到自己就讓整輪炸掉|    except OSError as e:|    except ValueError as e:"
  "修訂版：算了卻不記進 evidence|    meta[\"probe\"] = probe_revision()|    meta[\"probe\"] = {}"
  # ── parse_sections：不靜默忽略打錯的節名 ──
  "分段：不照依賴順序|    return tuple(s for s in ALL_SECTIONS if s in want), None|    return tuple(want), None"
  "分段：打錯的節名靜默忽略|    if unknown:|    if False:"
  # ── thinking_verdict（C4 的核心）──
  "thinking：不檢查 eval 有沒有下降|    counts_toward = thinking_present and saved > 0|    counts_toward = thinking_present"
  "thinking：一律當成有 thinking|    thinking_present = thinking_chars_on > 0|    thinking_present = thinking_chars_on >= 0"
  "thinking：省下的 token 算錯|    saved = eval_count_on - eval_count_off|    saved = eval_count_off"
  "thinking：沒有對照也講成確認|            \"只知道 thinking 有 %d 個字元，無法確認它有沒有計入 eval_count\"|            \"只知道 thinking 有 %d 個字元\""
  # ── thinking_verdict：撞到 num_predict 上限時只能講下界 ──
  # 這一組是 C3 那個洞的**第二個位置**：run2 的 C4 也有一次生成停在
  # num_predict 上（eval_count 正好 256），而舊版會把 256−72=184 講成真值。
  "thinking：撞到上限也當成真值|    lower_bound = (cut_on or cut_off) if checked else None|    lower_bound = False"
  "thinking：開那一側不看上限|    cut_on = num_predict_on is not None and eval_count_on >= num_predict_on|    cut_on = False"
  "thinking：沒查就當成查過|    checked = num_predict_on is not None or num_predict_off is not None|    checked = True"
  "thinking：切斷了卻不講下界|            reason += (\" 但**開著 thinking 的那一次停在 num_predict 上限（%d）**\"|            reason += (\"\""
  "thinking：被切斷的沒下降當成反例|        if cut_on or cut_off:|        if False:"
  # ── observations：最常被讀的那一行也不能留同樣的陷阱 ──
  "觀察：撞到上限不講|            capped = \"（**撞到 num_predict=%s 的上限，是被切斷的**）\" % cap|            capped = \"\""
  # ── call_summary（C1／C5 的依據）──
  "呼叫計數：chat 最多算一次|            summary[\"chat\"] += 1|            summary[\"chat\"] = 1"
  "呼叫計數：token 只留最後一次|            summary[\"chat_eval_tokens\"] += c.get(\"eval_count\") or 0|            summary[\"chat_eval_tokens\"] = c.get(\"eval_count\") or 0"
  # ── cost_split ──
  "成本佔比：分母用最大值|    total = sum(v for v in timings.values() if isinstance(v, (int, float)))|    total = max(v for v in timings.values() if isinstance(v, (int, float)))"
  "成本佔比：回傳秒數而非比例|            shares[k] = v / total|            shares[k] = v"
  # ── control_group_effect（C2 的對照組是否成立）──
  # 這一條的利害關係跟別人不一樣：它守的是**假通過**。B 沒生效時 A/B 會被
  # 截成同一個長度，truncation_verdict 會回「沒有截斷」—— 結論正好相反。
  # 多行的目標要用 $'...'（ANSI-C 引號）：一般的雙引號**不解析 \n**，
  # 送進去的是字面上的反斜線加 n，count 會是 0 而不是 1。這裡踩過一次。
  $'對照組：0 被當成沒生效|    if not ctx_after_full:\n        return None|    if ctx_after_full is None:\n        return None'
  "對照組：一律說生效|    return ctx_after_full == full_ctx|    return True"
  # ── C2/A 的 options 必須是 mem0 那一份（2026-09-20 新增）──
  # C2/B 為了省時間把 num_predict 壓小（見 B_CROSSCHECK_NUM_PREDICT 的說明），
  # 而「A 也可以順手壓一下吧」是這個改動最自然的下一步 —— 先前真的發生過
  # 一次，而 A 一旦被改，量的就不是 mem0 實際遇到的東西（D-014）。
  # 這一條與 B 的那個改動只差一個字，所以它需要自己的突變守著。
  $'C2/A 也壓小 num_predict|    opts = dict(MEM0_LLM_OPTIONS)\n    total_chars = sum(len(m["content"]) for m in messages)|    opts = dict(MEM0_LLM_OPTIONS, num_predict=B_CROSSCHECK_NUM_PREDICT)\n    total_chars = sum(len(m["content"]) for m in messages)'
  # ── memories_written（實際犯過的錯）──
  # 這一條守的是「看起來完全合理的錯數字」，最難發現的一種。
  "記憶數：數 key 而不是數 results|        results = add_result.get(\"results\")|        results = add_result"
  $'記憶數：形狀不對時回 0|    if not isinstance(results, list):\n        return None|    if not isinstance(results, list):\n        return 0'
  # ── truncated_length（C2b 的閉式解）──
  # 這一組守的是「那個 2050 是怎麼來的」。舊版以為它來自 num_predict，
  # 而 4096 − 2000 − 46 剛好也等於 2050 —— 所以這些突變的數字只差幾個
  # token，看起來全都很正常。
  "截斷長度：忘了保留 num_keep|    num_keep = max(0, min(num_keep, num_ctx - 1))|    num_keep = 0"
  "截斷長度：不夾在至少一個|    return num_ctx - max((num_ctx - num_keep) // 2, 1)|    return num_ctx - (num_ctx - num_keep) // 2"
  $'截斷長度：退化的 context 不擋|    if num_ctx <= 1:\n        return 0|    if False:\n        return 0'
  "截斷長度：除以二寫成除以四|    return num_ctx - max((num_ctx - num_keep) // 2, 1)|    return num_ctx - max((num_ctx - num_keep) // 4, 1)"
  # ── min_ctx_required（C2b 的行動結論）──
  # 少了那個 1，`num_ctx = prompt_tokens` 會剛好落在被砍的那一側，
  # 而算出來的數字看起來完全正常。這是整份探針最貴的一個位元。
  "門檻：忘了加一|    return prompt_tokens + 1|    return prompt_tokens"
  # ── truncation_limit_verdict（核對閉式解）──
  "核對：沒截斷也當成不符|    if measured_pec > num_ctx:|    if False:"
  "核對：不符時說成相符|        \"matches\": False,|        \"matches\": True,"
  "核對：沒有數字時不擋|    if not measured_pec:|    if False:"
  # ── threshold_bracket_verdict（C2b 的夾縫測試）──
  # 這一組守的是「要開多大」那個數字的**正當性**。夾縫鬆掉一格，
  # num_ctx/2 與 num_ctx−c 這兩條替代規則就都會通過 —— 而它們在 C2 的
  # A/B 兩個點上跟真規則完全無法區分。
  "夾縫：P 那一側不檢查|    cut_as_predicted = at_ctx_pec == expected_cut|    cut_as_predicted = True"
  "夾縫：P+1 那一側不檢查|    above_untouched = above_ctx_pec == prompt_tokens|    above_untouched = True"
  "夾縫：一端成立就說成立|    if cut_as_predicted and above_untouched:|    if cut_as_predicted or above_untouched:"
  "夾縫：讀不到也往下判|    if not at_ctx_pec or not above_ctx_pec:|    if False:"
  "夾縫：不成立時不講門檻|        \"reason\": \"夾縫測試不成立（%s）—— 門檻不是 num_ctx，\"|        \"reason\": \"夾縫測試不成立（%s）——\""
  # ── grade：C1~C5 各一條 ──
  # 少了這道，整節沒量到時各條判準會各自喊出**與事實相反**的敘述。
  "缺節：不說缺少哪幾節|    if missing:|    if False:"
  "C1：不檢查呼叫數|    if first_add_chat != README_CLAIMED_CALLS:|    if False:"
  "C2：截斷的判斷反過來|    if trunc.get(\"truncated\") is not True:|    if trunc.get(\"truncated\") is not False:"
  # 下面兩條的分工：前者拿掉整道判準，後者把「無法判定」（None）也當成
  # 「沒生效」。後者若穿過去，讀不到 /api/ps 的環境會被判未通過 —— 假失敗。
  "C2：不檢查對照組|    if trunc.get(\"full_ctx_took_effect\") is False:|    if False:"
  "C2：無法判定也當成沒生效|    if trunc.get(\"full_ctx_took_effect\") is False:|    if trunc.get(\"full_ctx_took_effect\") is not True:"
  # 這一條針對的是**帶著可區辨字串的那一行**：「沒有生效」在檔案裡出現兩次
  # （grade 的訊息、measure_truncation 的列印），所以目標字串要拉長到唯一。
  # D-026 第五節記的就是這個形狀 —— 斷言要盯的字串，必須真的在被改的那行上。
  "C2：對照組的訊息講成別的事情|C2：對照組要求的 num_ctx=%s 沒有生效（/api/ps 說 %s）—— |C2：對照組要求的 num_ctx=%s 不符（/api/ps 說 %s）—— "
  "C3：不看 tail 是否看得到|    elif canary.get(\"asymmetric\") is not True or canary.get(\"tail_seen\") is not True:|    elif canary.get(\"asymmetric\") is not True:"
  "C4：thinking 缺席時不講話|    if think.get(\"thinking_present\") is not True:|    if False:"
  "C4：不確認 thinking 計入|    elif think.get(\"counts_toward_eval\") is not True:|    elif False:"
  "C5：不檢查第二次呼叫|    if second_add_chat != README_CLAIMED_CALLS:|    if False:"
  "空證據當成通過|        return False, [\"沒有收到任何證據 —— 探針沒有跑到判準那一步\"]|        return True, []"
  # ── observations ──
  # 這一條對應 item 2 真的犯過的錯：observations() 無條件塞一則說明，
  # 空證據時回傳非空清單（D-026 第二節）。
  $'observations：空證據不早退|    if not evidence:\n        return notes|    if False:\n        return notes'
)

caught=0
missed=()

# 植入器寫成檔案，而不是每次在 `if` 裡塞一個 heredoc —— bash 解析不了
# `if ! VAR="$(cmd <<'PY' ... PY)"` 那種組合（實際踩過，錯誤訊息只說
# 「syntax error near unexpected token `then'」，不會告訴你是heredoc）。
cat > "$WORK/inject.py" <<'PY'
import ast, io, os, subprocess, sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
src = io.open(path, encoding="utf-8").read()
n = src.count(old)
if n != 1:
    # 兩種原因要分開講。合併成一句「不是唯一字串」會誤導：多行的目標
    # 用雙引號寫（不解析 \n）時其實是**找不到**，但訊息會說「不是唯一」，
    # 於是會去翻原始碼找重複，而該改的是這支腳本裡的引號。
    print("NOT_FOUND" if n == 0 else "NOT_UNIQUE:%d" % n, file=sys.stderr)
    sys.exit(1)
mutated = src.replace(old, new, 1)

# 植入之後還要再過一關，因為「測試變紅」不等於「有斷言守住」。這一關擋的
# 是**假抓到**，它有兩張臉，而且都不會讓任何一條斷言叫起來：
#   NO_OP       取代沒有改變語意（`if slack < -0:` 就是 `if slack < 0:`）。
#               它殺不掉任何東西，卻在報告裡佔一個「抓到」的名額 —— 更糟
#               的是它讓一份「全部抓到」看起來很完整。`ast.dump` 不比對
#               註解與空白，所以只改註解的那種也會在這裡現形。
#   BROKEN_CODE 植入後根本不是合法程式。測試當然會失敗，但那是模組匯入
#               不了，不是判準被守住。真的混進來過：目標行裡的 `||` 被
#               當成欄位分隔符，以及雙引號裡的字面 `\n`。
# 兩者都只說明**這條突變寫壞了**，原始碼沒有問題。
ext = os.path.splitext(path)[1]
if ext == ".py":
    try:
        before = ast.dump(ast.parse(src))
        after = ast.dump(ast.parse(mutated))
    except SyntaxError as e:
        print("BROKEN_CODE:%s" % e.msg, file=sys.stderr)
        sys.exit(1)
    if before == after:
        print("NO_OP", file=sys.stderr)
        sys.exit(1)
elif ext == ".sh":
    # shell 的等價性在這裡判不了（那要真的比語意），所以只驗語法。
    # 走 stdin 而不是暫存檔：`bash -n` 的訊息就會是乾淨的 `line N`，
    # 不必把一個臨時路徑印進錯誤訊息裡。
    p = subprocess.run(["bash", "-n"], input=mutated,
                       capture_output=True, text=True)
    if p.returncode != 0:
        last = (p.stderr.strip().splitlines() or ["bash -n 失敗"])[-1]
        print("BROKEN_CODE:%s" % last, file=sys.stderr)
        sys.exit(1)

io.open(path, "w", encoding="utf-8").write(mutated)
PY

# 每一條都必須**恰好**兩個 `|`（label|old|new）。多寫一個就會被切錯，而且
# 症狀是**靜默**的：`old` 被截短、`new` 從 `|` 開始，植入的是一段壞掉的
# 程式碼 —— 測試當然會失敗，於是它被算進「抓到」，但它守不住任何判準。
# 這一條真的抓到過一條：目標行裡的 `||` 被當成了分隔符。
BADFIELDS=""
for m in "${MUTANTS[@]}"; do
  pipes="${m//[^|]/}"                      # 只留 `|`，數它有幾個
  [[ ${#pipes} -eq 2 ]] || BADFIELDS+="  ${m%%|*}"$'\n'
done
if [[ -n "$BADFIELDS" ]]; then
  fail "突變清單有條目的 \`|\` 不是剛好兩個 —— 欄位會被切錯，植入的是壞掉的程式碼："
  printf '%s' "$BADFIELDS"
  exit 3
fi

for entry in "${MUTANTS[@]}"; do
  label="${entry%%|*}"
  rest="${entry#*|}"
  old="${rest%%|*}"
  new="${rest#*|}"

  cp "$PROBE" "$WORK/mem0_add_cost_probe.py"
  cp "$TEST" "$WORK/test_mem0_add_cost_probe.py"

  INJECT_MSG="$(python3 "$WORK/inject.py" "$WORK/mem0_add_cost_probe.py" \
                "$old" "$new" 2>&1 >/dev/null)" && : || {
    case "$INJECT_MSG" in
      NOT_FOUND)
        fail "植入失敗：$label —— 目標字串在 $PROBE 裡**找不到**"
        fail "  最常見的原因：多行的目標寫在一般雙引號裡，bash 不解析 \\n。" \
             "改用 \$'...'（ANSI-C 引號）。" ;;
      NOT_UNIQUE:*)
        fail "植入失敗：$label —— 目標字串在 $PROBE 裡出現 ${INJECT_MSG#NOT_UNIQUE:} 次"
        fail "  原始碼改了，這條突變對不上（要更新這支腳本，不是更新探針）" ;;
      NO_OP)
        fail "植入失敗：$label —— 取代**沒有改變語意**，這條突變殺不掉任何斷言"
        fail "  這不是「很弱的突變」，是沒有突變。掃描會把它算進「抓到」，但它是空的" ;;
      BROKEN_CODE:*)
        fail "植入失敗：$label —— 植入之後**根本不是合法程式**（${INJECT_MSG#BROKEN_CODE:}）"
        fail "  測試會變紅，但那是跑不起來，不是判準守住了。原始碼沒問題，是這條突變寫壞了" ;;
      *)
        fail "植入失敗：$label（$INJECT_MSG）" ;;
    esac
    missed+=("$label（植入失敗）")
    continue
  }

  if python3 "$WORK/test_mem0_add_cost_probe.py" >/dev/null 2>&1; then
    fail "漏掉！$label —— 評分器壞成這樣，測試還是通過了"
    missed+=("$label")
  else
    ok "抓到  $label"
    caught=$((caught + 1))
  fi
done

echo
total="${#MUTANTS[@]}"
if [[ "${#missed[@]}" -gt 0 ]]; then
  fail "有 ${#missed[@]} 個突變沒被抓到，共 $total 個 —— 測試有洞："
  for m in "${missed[@]}"; do fail "  • $m"; done
  exit 1
fi

ok "$caught/$total 個突變全數被抓到，且對照組通過 —— 評分邏輯的每一道判準都有測試守著"
