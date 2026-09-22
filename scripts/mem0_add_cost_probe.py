#!/usr/bin/env python3
"""第三階段 item 3 的探針：mem0 的 add() 到底多花多少？

README 第三階段第 3 項是個 [open]，上面寫著：

  「mem0 的寫入路徑會先跑一次 LLM 把事實抽出來再存。那個呼叫是**額外**的，
    不包含在生成答案裡。照目前量到的速率（3.8–6.9 tok/s，D-011／D-014），
    這可能是第四階段最大的單筆成本 —— 大到應該在設計記憶之前就單獨計時，
    而不是事後才發現。」

「每輪多一次 LLM 呼叫」這句話**從來沒有被量過**。這支探針存在的理由就是去量它。

### 先講結論的形狀：那句話是對的，但它問錯了問題

讀原始碼（mem0ai 2.1.0）就會發現「一次」在**結構上**成立：同步路徑
`add()` → `_add_to_vector_store()` 分成五個 Phase，其中只有 Phase 2
（`memory/main.py:956`）會呼叫 `self.llm.generate_response()`，註解自己就寫著
`# Phase 2: LLM extraction (single call)`。其他 `generate_response` 的出處是
procedural memory（要明示 `memory_type` 才會走）與非同步版本的鏡像。

**但「一次」不等於「便宜」，也不等於「那是一次有效的呼叫」。** 這支探針真正
量到的是後面那兩件事：

1. **那次呼叫的 prompt 有 33,653 字元是固定開銷，而且它進不去。**
   `ADDITIVE_EXTRACTION_PROMPT`（`configs/prompts.py:468`）本身就有 33,653 個
   字元，而 mem0 的 OllamaLLM 送出的 `options` **從頭到尾沒有 `num_ctx`**
   （`llms/ollama.py:129-134` 只送 temperature／num_predict／top_p）。
   沒有 `num_ctx` 就是吃 ollama 的預設值 4096。

   ollama 的截斷規則是兩段式，**而且跟 `num_predict` 完全無關**
   （`llm/llama_server.go:288-330`，v0.34.2）：

       len(tokens) <= num_ctx − 1   → 原封不動
       len(tokens)  > num_ctx − 1   → 砍到 num_ctx − max((num_ctx − nKeep)/2, 1)
                                      （nKeep 預設 4 → num_ctx/2 + 2）

   mem0 那份 prompt 是 8047 個 token，8047 > 4095，所以被砍到 2050 ——
   **5997 個 token 沒有進到模型裡**。要讓它不被砍，`num_ctx ≥ 8048`。

   ⚠ **這裡原本寫的是錯的公式**（`num_ctx − num_predict − 邊界`，邊界 = 46），
   而且它通過了一次「看起來有分辨力」的實測。

   錯在哪：`4096 − 2000 − 46 = 2050`，跟真公式算出來的 2050 **數字剛好
   一樣**。而我的實驗是「固定 num_predict、變 num_ctx」，在那個方向上兩個
   公式都預測 2050 —— **那次實驗根本沒有分辨力**，我卻把它的結果讀成分辨
   （D-024 第九節）。

   真正拆穿它的是反過來的那個實驗：**固定 num_ctx=4096、變 num_predict**。
   舊公式預測 4034 → 3050 → 2050，實測是

       num_predict=16   → 2050
       num_predict=1000 → 2050
       num_predict=2000 → 2050

   斜率 0。舊公式只有在最後一點（num_predict=2000）才對上，而那一點正是
   它當初被「驗證」的那一點。諷刺的是那份臨時腳本自己把斜率算成 0、下一行
   卻仍舊寫出「以最後一點算邊界 = 46」—— 因為它就是照著那個假設寫的。

   同一個實驗可以看起來有分辨力、也可以沒有，差別只在**有沒有去算另一邊
   預測什麼**：那個被讀成分辨的版本裡，舊公式預測 4034、真公式預測 2050，
   兩者分得很開 —— 但我只算了舊公式那一邊，看到量到的 2050 就收工了。
   一個數字不可能同時「符合預測」又「區分預測」，除非兩個預測本來就不同。

   ⚠⚠ **更該記下來的是：正確的公式本來就已經在紀錄裡了。**
   **D-023 第五節**（`7362ac4`，2026-09-20，**早於**上面這個錯誤）寫著同一
   條規則 —— 觸發條件 `> num_ctx − 1`、砍到 `num_ctx − max((num_ctx −
   nKeep)/2, 1)`、`numKeep` 預設 4 —— 讀自同一份原始碼，而且對著同一行
   `limit=2050` 的日誌核對過。它甚至自己寫出缺哪一項證據：

       我**沒有**用第二組 `num_ctx` 實測過（沒有真的設 8192 再確認門檻是
       8,191）。**所以公式若被我讀錯一層，上表會跟著錯。**

   那正是 C2b 的夾縫檢驗在做的事。所以這一節有一半是在**補一個已經被指名
   過的缺口**，不是在發現新東西。錯的不是紀錄，是我從一行日誌反推公式、
   沒有先去看已經寫下來的東西。

   教訓有兩層。一是**實驗的設計方向決定了它能分辨什麼**：變一個在公式裡
   有係數的變數才叫檢驗，變一個兩邊都預測同樣結果的變數只是重述假設。
   二是**先查紀錄，再推公式** —— 從一行日誌推出來的公式會剛好對上那一行，
   而紀錄會連「我還沒驗證哪一段」一起告訴你。

2. **被丟掉的是開頭，也就是模型自己的指令。**
   `_parse_response()`（`llms/ollama.py:43-90`）只回傳
   `response.message.content` —— ollama 回傳的 `prompt_eval_count`、
   `eval_count`、`eval_duration`、以及 qwen3 的 `thinking` 全部被丟棄。
   所以「花了多少」在 mem0 的介面上是**看不到的**，只能從外面包住
   `llm.client.chat` 才拿得到。這支探針就是這樣做的。

### 為什麼是包住 client，而不是自己重送一份 prompt

如果探針自己組一份「類似的」prompt 去問 ollama，那量到的是**探針的 prompt**，
不是 mem0 實際送出的那一份 —— 這是 D-024 第九節的教訓
（「用會回傳你期待答案的查法，就會得到你期待的答案」）。包住
`llm.client.chat` 拿到的是**同一個 dict**：同樣的 messages、同樣的 options，
連 `format=json` 時追加的那句 `Please respond with valid JSON only.` 都在裡面。

### 判準（C1~C7）與觀察

C1~C7 全部是**無閾值的機械判準** —— 不是「時間有沒有超過幾秒」這種會隨機器
飄移的門檻，而是兩個量之間的序關係或計數：

  C1  一次 add() 只發出一次 chat 呼叫（README 的「一次」在結構上成立）
  C2  同一份 messages，mem0 的 options 評估的 token 數**少於**明確給足
      num_ctx 時 —— 少掉的就是被丟棄的
  C3  截斷吃的是**開頭**（`llama_server.go` 的預測）：等長的 canary 分別放在
      系統提示詞的開頭與結尾，在 mem0 的 options 下**尾端看得到、開頭看不
      到**。兩端都看不到**不算通過** —— 那是「沒有量到」，不是量到反例
      （run2 就是這樣：模型的生成被 num_predict 切斷，兩端都沒講到。
      見 canary_answer_usable() 與 exit_code()）
  C4  thinking 被算進 eval_count 卻被 mem0 丟掉：mem0 的 options 下
      thinking 非空，且關掉 think 之後 eval_count 明顯下降
  C5  Phase 5 的雜湊去重發生在 Phase 2 的 LLM 呼叫**之後**，所以同樣的內容
      寫兩次仍然付兩次 LLM 呼叫 —— 去重省不到那個呼叫
  C6  抽取 prompt 沒有超過推導用的上界（`EXTRACTION_PROMPT_TOKENS_BOUND`）
  C7  生成是模型**自己停下來的**（`done_reason=stop`），不是被預算切斷的

**C6／C7 只在預算是推導來的時候成立**（`--ctx`，見 D-037）：它們盯的是那條
推導的兩個前提，不是上游的行為。`--add-max-tokens`（含 `--quick` 翻出來的
200）是重現 D-027 條件的受控實驗，那時預算太小是設計，不判。

C2b 不是判準，是 C2 問完之後必然接著要問的問題：**那要開多大才不截斷？**

答案是一個**門檻**，不是一條要外推的線：`num_ctx ≥ prompt 的 token 數 + 1`。
所以這裡量的是門檻本身 —— 用**小 prompt** 把它夾在 1 個 token 之內：

  1. 先量出這份 prompt 的 token 數 P（在寬鬆的 num_ctx 下，不會被砍）
  2. num_ctx = P   → 預期被砍，砍完剩 `truncated_length(P)`
  3. num_ctx = P+1 → 預期原封不動（prompt_eval_count == P）

夾縫只有 1 個 token，所以「門檻是 num_ctx − 1，還是差某個別的常數」
當場分開。用**小** prompt 是因為這個判準與長度無關（它是一個比較，
不是一條斜率），小 prompt 幾十秒就夠。真實尺寸那一端由 C2 的對照組蓋住：
8047 個 token 在 num_ctx=16384 下完整進去 —— 如果規則是「一律砍到一半」，
那裡會量到 8194 而不是 8047。

再量兩個點確認**砍下去剩多少**（`num_ctx/2 + 2`）。這個數字寫成
`truncated_length()` —— 一個**閉式解**，不是拿量測點擬合出來的直線。
差別在方向：擬合出來的線，兩個點就定得出來，量測只是在確認自己的假設；
閉式解在任何 num_ctx 上都先算得出預測值，量測只能去**核對**它。
上一版就是反過來做的 —— 拿兩個點擬出一條線、再拿那條線反推「要開多大」，
回一個看起來很精確卻答錯問題的 num_ctx，而且沒有任何一步會擋下它。

### A 與 B 的 num_predict 刻意不對稱

C2 的兩次呼叫送出的 options **不一樣**，而且這個不對稱是刻意的：

  A  **一個字都不改** —— 送的就是 `MEM0_LLM_OPTIONS`，也就是 mem0 的
     OllamaLLM 實際送出的那一份。A 量的是「mem0 實際遇到什麼」，改了它
     量的就不是實況（D-014）。
  B  `num_ctx=16384`，而 `num_predict` 壓成 `B_CROSSCHECK_NUM_PREDICT`（32）。

B 唯一的主張是「同一份 prompt 在夠大的 num_ctx 下會被評估幾個 token」——
那是 **prompt eval 階段**的產物。`num_predict` 是生成端的參數，兩者在原始碼
裡完全不相交（它既不在 `truncated_length()` 的推導裡，也不在
`llama_server.go` 的截斷分支裡）。

**這不是先前那個錯誤的重演。** 先前錯的是壓小 **A** 的 num_predict；B 是
反事實對照組，不是實況。而且「num_predict 不影響 prompt 的評估數」正是
E4 量到的事：固定 num_ctx=4096、只變 num_predict（16／1000／2000），三次的
prompt_eval_count 都是 2050 —— **斜率 0** —— 那也正是拆穿舊公式的實驗。

成本差多少：生成的速率由**當下的 context 長度**決定，不是 num_ctx。實測在
~8000 token 的 context 下生成是 0.18~0.26 tok/s，所以 2000 個 token 要
~2.3 小時；32 個只要 ~2 分鐘。B 的 prompt eval 本身約 8 分鐘 —— 省下來的
全部是與主張無關的生成。

（列印出來的 options 要老實寫：A 是「mem0 的 options」，B 不是。）

時間、token 數、速率一律放在「觀察」段，因為那些是**這台機器的**數字，
不是判準（D-014：在某組條件下量到的結果不可以套用到別組條件）。

### 為什麼評分邏輯要獨立成純函式

本專案慣例（scripts/test_chroma_dims_probe.py、D-016）：**評分邏輯本身要被
實測過**。所以 `truncation_verdict()`、`control_group_effect()`、
`truncated_length()`、`min_ctx_required()`、`truncation_limit_verdict()`、
`threshold_bracket_verdict()`、`canary_reading()`、`canary_answer_usable()`、
`canary_verdict()`、`thinking_verdict()`、`call_summary()`、`cost_split()`、
`grade()`、`exit_code()`、`parse_sections()`、`observations()` 只讀一般資料結構，
**不在 module 頂層 import mem0／chromadb／ollama**。那些 import 全部延後到
`run_probe()` 裡面，離線測試才有辦法在沒有網路、沒有這些套件的情況下跑。

唯一的例外是 `probe_revision()` —— 它讀的是磁碟上自己的原始碼。它仍然是
可離線測試的（不碰網路、不碰那三個套件），而它存在的理由見它的 docstring：
**一次執行如果講不出自己是誰跑的，它的數字就沒有歸屬。**

`control_group_effect()` 與 `threshold_bracket_verdict()` 是刻意抽出來的：
這兩個判斷**非對即錯，而且錯的方向是假通過** —— 對照組沒生效時 C2 會回
「沒有截斷」，夾縫測試不成立時「要開多大」的那個數字會照算。這種判斷
不能只活在連網的程式碼裡，離線測試碰不到就等於沒有防線。

用法（通常由 scripts/verify-mem0-add-cost.sh 呼叫）：
    python3 -u mem0_add_cost_probe.py --model qwen3:4b \
        --embed-model qwen3-embedding:0.6b --ollama-url http://ollama:11434 \
        --ctx 16384

`--ctx` 是 ollama 生效的 num_ctx：給了它就**推導**真實 add() 的生成預算
（`max_tokens = num_ctx − 抽取 prompt 的上界`），而不是挑一個數字 —— 這個
組合不會觸發 context shift，而且 num_ctx 變大時預算自己跟著變大（D-037）。
不給它就是 mem0 的實況（`num_predict=2000`，抽取很可能回傳 0 筆）。

結束碼：0 = 通過（C1~C7 全過）
        1 = 未通過（某一條判準沒過 —— 見輸出，通常是上游變了）
        2 = 無法判定（連不上、模型沒下載、**沒跑滿全部** —— 不是判準的問題）
        3 = 探針自己壞掉（D-018：這不是受測對象的問題；`--ctx` 推不出預算
            也是走這個碼，那是用法問題，不是判準也不是環境）

⚠ **這支探針會跑好幾分鐘的生成。** 在沒有 GPU 的 VM 上，qwen3:4b 的生成速率
是每秒數個 token 的等級，而 mem0 的 `num_predict` 預設是 2000。用
`--add-max-tokens` 可以把真實 add() 那兩次縮短（機制判準 C2~C4 不受影響，
因為它們量的是 prompt 處理）。**中途中斷探針不會取消 ollama 端已經開始的
生成** —— 它會繼續跑完並佔住 `OLLAMA_NUM_PARALLEL` 的槽位，讓下一次執行
看起來像卡住。要中斷請連 ollama 的請求一起想辦法，或等它跑完。
"""

import argparse
import json
import random
import string
import sys
import tempfile
import time
from pathlib import Path

# ── 結束碼（D-018 的約定，全專案一致）──────────────────
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INDETERMINATE = 2
EXIT_BROKEN = 3

# mem0 的 OllamaLLM 實際送出的 options（llms/ollama.py:129-134）。
# 刻意抄成常數而不是「記得要對齊」：這是被量測的**條件**，它變了，量到的
# 數字就不能跟 D-027 的比。top_k 不在裡面 —— mem0 的 config 有 top_k 欄位，
# 但 build options 時漏掉沒送，所以它對 ollama 是死的。
MEM0_LLM_OPTIONS = {"temperature": 0.1, "num_predict": 2000, "top_p": 0.1}

# 抽取 prompt 的 token 數**上界** —— 拿它把生成預算從 num_ctx 推導出來
# （D-036 第十節立的規矩、D-037 的實作）。
#
# mem0 的 OllamaLLM 只送上面那份 options，**不送 num_ctx**。後果有兩層，
# 第二層是 D-036 才量出來的：
#
#   1. context 由 ollama 的預設決定 → 抽取 prompt 可能被截斷（D-027）
#   2. **生成預算固定 2,000** → 模型話沒講完就被切斷，抽取回傳 0 筆。
#      D-036：num_ctx 不動、只把預算開到 8,000，同一份 prompt 就抽出
#      2 則記憶（`done_reason=stop`、`eval_count=5,605`）。
#
# 第 2 層的處方**不是「換一個比較大的數字」**——那只是把 2,000 的錯換成下
# 一個數字，下一個模型／下一份 prompt 會再犯一次（D-036 第十節）。真正的
# 條件是「大到模型自己停」，而在 num_ctx 有限之下，能給的最大值是**算**得
# 出來的：
#
#     預算 = num_ctx −（抽取 prompt 最多佔多少）
#
# 只要實測的 prompt 不超過這個界，`prompt + 預算 ≤ num_ctx` 就恆成立 ——
# **context shift 在結構上不可能發生**（D-035 第三節：生成超過
# `num_ctx − prompt_tokens` 會讓 llama-server 從 prompt 中段丟掉一整塊）。
# num_ctx 調大，預算自己跟著變大。這是**不變式**，不是一個挑出來的值 ——
# 也就是它與魔數的差別。
#
# 8,192 是**界，不是量測值**：抽取 prompt 實測 8,052（D-027）、8,100
# （D-034）、8,167（D-036 的 add 2），取到下一個 2 的次方。取大是安全方向
# ——界太小會讓預算太大（可能 shift，由判準 C6 擋），界太大只會讓預算偏小
# （生成被切斷，由判準 C7 擋）。**界過期的那一天，處方是更新這個界並記錄
# 新的觀測，不是把判準放寬。**
EXTRACTION_PROMPT_TOKENS_BOUND = 8192

# 「明確給足」的對照組 context 大小。要大到讓 33,653 字元的系統提示詞加
# 使用者訊息都進得去（約 9~10k token），所以 16384 有餘。
FULL_CTX = 16384

# C2/B 的 num_predict —— **刻意不用 mem0 的 2000**。
#
# B 唯一的主張是「同一份 prompt 在夠大的 num_ctx 下會被評估幾個 token」，
# 那是 **prompt eval 階段**的產物；num_predict 是生成端的參數，兩者在原始碼
# 裡完全不相交（num_predict 不在 truncated_length() 的推導裡，也不在
# llama_server.go 的截斷分支裡）。留著 2000 只是讓 B 多生成 2000 個 token，
# 而那些 token 對 B 的主張沒有任何貢獻。
#
# **這不是先前那個錯誤的重演。** 先前錯的是把 **A** 的 num_predict 壓小：
# A 量的是「mem0 實際遇到什麼」，改了它 A 就不是 mem0 的實況（D-014）。
# B 是反事實對照組，不是實況。而且「num_predict 不影響 prompt 的評估數」
# 正是 **E4** 量到的事 —— 固定 num_ctx=4096、只變 num_predict
# （16／1000／2000），三次的 prompt_eval_count 都是 2050，**斜率 0** ——
# 那也正是拆穿舊公式的實驗。
#
# 成本差多少：生成的速率由**當下的 context 長度**決定（不是 num_ctx）。
# 2026-09-20 實測，在 ~8000 token 的 context 下生成是 **0.18~0.26 tok/s**，
# 所以 2000 個 token 要 ~2.3 小時；32 個 token 只要 ~2 分鐘。B 的 prompt
# eval 本身約 8 分鐘 —— 省下來的全部是與主張無關的生成。
B_CROSSCHECK_NUM_PREDICT = 32

# 確認「砍下去剩多少」用的 prompt。它要**明顯大於**那些量測點的 num_ctx，
# 否則量到的會是「沒有截斷」，那就不是上限。30000 字元 ≈ 7.1k token，
# 比 4096 大得多。這份 prompt 的實際 token 數不重要 —— 被截斷時
# prompt_eval_count 就等於砍完的長度。
CTX_LAW_PROMPT_CHARS = 30000

# 砍下去剩多少的兩個量測點（num_ctx）。預期值不是擬合出來的，是
# `truncated_length()` 這個**閉式解**算出來的：2048 → 1026、4096 → 2050。
# 選這兩個值是因為它們要能區分兩個都「看起來很合理」的公式 ——
# 真公式在 2048 給 1026，而「num_ctx − num_predict − 邊界」在同一個點會給 2。
CUT_FORMULA_CTX = (2048, 4096)

# 夾縫測試（bracket）用的小 prompt。它只要「token 數多到不會被模板與 BOS
# 這類固定開銷蓋過去」就夠 —— 門檻是一個**比較**（len(tokens) ≧ num_ctx），
# 不是一條斜率，所以跟 prompt 多長無關。用小的理由是成本：夾縫測試要三次
# 呼叫，每次換 num_ctx 就得重新載入模型。
# 真實尺寸那一端由 C2 的對照組蓋住（8,047 個 token 在 num_ctx=16384 下
# 完整進去）—— 如果規則是「一律砍到一半」，那裡會量到 8,194 而不是 8,047。
BRACKET_PROMPT_CHARS = 3000

# 夾縫測試第一次呼叫用的 num_ctx，唯一的用途是**量出 P**（那份小 prompt 的
# token 數）。量出來的 P 會拿去當後兩次的 num_ctx，所以這個值不必精確，
# 但**必須明顯大於 P** —— 太接近的話第一次自己就被截斷，量到的 P 就是假的。
BRACKET_PROMPT_CTX = 8192

# C4 需要真的生成一點東西才看得到 thinking。
THINKING_NUM_PREDICT = 256

# C3 的 canary 呼叫要不要讓模型推理。**這不是「把條件調鬆」，是拆掉一個
# 混淆變項。**
#
# C3 問的是「截斷吃哪一端」——那是 **prompt eval 端**的性質。模型能不能
# **回報**它看到什麼，是**生成端**的性質。run2 就是在這裡把兩者混成了一個：
# 生成預算被 thinking 吃光，於是「模型沒講完」被讀成了「模型看不到」。
#
# think=False 只影響生成端，不影響截斷 —— 截斷只看 num_ctx 與 token 數
# （D-027；已由固定 num_ctx、只變 num_predict 的 E4 實測：斜率 0）。
# 理由與 C2/B 壓小 num_predict 的那一段完全相同：拿掉一個與主張無關的
# 變項，而不是把量測放寬。
#
# 留著 num_predict=2000 不動（那是 mem0 的值），並且**記錄 eval_count**
# ——若模型還是沒講完，canary_answer_usable() 會讓這一節回報「沒有量到」，
# 而不是回報一個方向。
CANARY_THINK = False

# README 第 3 項的那句話。留成常數是為了讓「一次」是一個可以被斷言的數字。
README_CLAIMED_CALLS = 1

# 全部的量測節。順序就是依賴順序：C2 的對照組會把 context 載成 16384，
# 而 C3 必須先卸載才量得到預設值下的截斷（見 measure_canary 的說明）。
# C1／C5 放最後是因為它最貴（真的跑兩次 add()）。
ALL_SECTIONS = ("C2", "C2b", "C3", "C4", "add")


# ─────────────────────────────────────────────────────────
# 純函式 —— 只讀一般資料結構，不 import 任何重套件
# ─────────────────────────────────────────────────────────


def truncation_verdict(default_pec, full_pec):
    """兩個 prompt_eval_count 的比較 → 有沒有被截斷、丟掉多少。

    這是 C2 的核心，而且它**不需要知道 ollama 的預設 num_ctx 是多少**：
    同一份 messages 送兩次，唯一差別是第二次明確給了足夠大的 num_ctx。
    兩次的 prompt_eval_count 不同，就代表第一次有東西沒被評估到。

    `prompt_eval_count` 是 ollama 自己回報的「實際評估了幾個 token」，
    不經過我們的解讀 —— 這是選它當判準的理由。

    回傳 dict(truncated, dropped, reason)：
      truncated  True / False / None（None = 無法判定）
    """
    if not default_pec or not full_pec:
        return {
            "truncated": None,
            "dropped": None,
            "reason": "其中一次沒有回報 prompt_eval_count（%r / %r）—— 無法比較"
            % (default_pec, full_pec),
        }
    if full_pec > default_pec:
        return {
            "truncated": True,
            "dropped": full_pec - default_pec,
            "reason": "同樣的 messages：mem0 的 options 只評估了 %d 個 token，"
            "明確給足 num_ctx 時評估了 %d 個 —— 有 %d 個 token 沒有進到模型裡。"
            % (default_pec, full_pec, full_pec - default_pec),
        }
    if full_pec == default_pec:
        return {
            "truncated": False,
            "dropped": 0,
            "reason": "兩者評估的 token 數相同（%d）—— 在 mem0 的預設下沒有截斷。"
            % default_pec,
        }
    return {
        "truncated": None,
        "dropped": None,
        "reason": "預設的評估數（%d）反而**多於**給足 num_ctx 的（%d）—— "
        "非預期，先查清楚再下結論，不要當成沒截斷。"
        % (default_pec, full_pec),
    }


def control_group_effect(ctx_after_full, full_ctx):
    """B 段要求的 num_ctx 有沒有真的生效 → True / False / None。

    抽成純函式是因為這個判斷**非對即錯、且錯的方向很危險**：B 若沒生效，
    A/B 會落在同一個 context 下被截成同一個長度，truncation_verdict 就會
    回「沒有截斷」—— 結論正好相反，而且是**假通過**（比漏報嚴重）。

    三段語意，不要合併：
      True  —— /api/ps 回報的就是我們要求的數字
      False —— 回報了、但不等於我們要求的（對照組不成立，C2 要擋）
      None  —— 讀不到 /api/ps。那是**無法判定**，不是「沒生效」；
               當成沒生效會製造假失敗（D-016）。
    """
    if not ctx_after_full:
        return None
    return ctx_after_full == full_ctx


def memories_written(add_result):
    """mem0 的 add() 回傳什麼 → 抽出幾則記憶（讀不出來時回 None）。

    `Memory.add()` 回傳的是 **`{"results": [...]}`（dict），不是 list**。
    所以 `len(回傳值)` 量到的是 **key 的數量 —— 永遠是 1**，不是抽出幾則。
    這個數字看起來完全合理（「抽出 1 則記憶」），所以錯了不會有人發現。

    None 與 0 在這裡是**不同的意思**，不可以合併：
      0    —— 讀到了，就是沒抽出東西
      None —— 形狀不是預期的，不知道
    把 None 併進 0 會讓「上游改了回傳格式」看起來像「這次沒抽到記憶」。
    """
    if isinstance(add_result, dict):
        results = add_result.get("results")
    else:
        results = add_result
    if not isinstance(results, list):
        return None
    return len(results)


def prompt_chars(call):
    """這一次呼叫送出的 messages 共幾個字元（讀不到就 None）。

    **為什麼要有這一支：** 在 num_ctx=4096 的年代，兩次 `add()` 的
    `prompt_eval_count` 都是 2050 —— 因為兩次都被截斷到**同一個上限**，所以
    那個欄位看不出兩份 prompt 的長度差。截斷前的差別只留在被攔截下來的
    `params["messages"]` 裡（那是 mem0 送出的同一個 dict，見
    `_install_recorder()`）。run2 的日誌則從另一側顯示同一件事：兩次真實的
    add() prompt 是 8052 與 8100 個 token。

    **但那個理由只在會被截斷的 ctx 下成立。** ctx 夠大時 prompt 完整進去，
    `prompt_eval_count` 反而**看得出**差別（D-036 量到 8,052 與 8,167）。
    所以這一支的用途從「唯一能看出差別的管道」變成「與 prompt_eval_count
    互相印證的獨立管道」—— 兩個都印，讓它們彼此對照，見
    `truncation_claim_verdict()`。

    **它量的是字元數，不是 token 數** —— 這支探針沒有 tokenizer。所以它
    證明的是「第二份比較長」，不是「長了幾個 token」。

    讀不到回 None，**不回 0**：`0` 是一個合法的長度（空 prompt），把
    「讀不到」併進去，就會讓儀器失效長得像一個量測結果。
    """
    msgs = (call.get("params") or {}).get("messages")
    if not isinstance(msgs, list) or not msgs:
        return None
    total = 0
    for m in msgs:
        if not isinstance(m, dict):
            return None
        content = m.get("content")
        if content is None:
            return None
        if not isinstance(content, str):
            content = str(content)
        total += len(content)
    return total


def duplicate_evidence(chat1, chat2):
    """兩次 add() 的 prompt 長度對照 —— **只從攔截到的 messages 量**。

    **在會被截斷的 ctx 下**，`prompt_eval_count` 不可以拿來相減：兩次都被截斷
    到**同一個上限**，那個差值永遠是 0，而那個 0 是儀器的天花板，不是量測結果
    （見 prompt_chars()）。run2 的 ollama 日誌從另一側顯示同一件事：兩次真實的
    add() prompt 是 8052 與 8100 個 token，差了 48 —— 而 `prompt_eval_count`
    兩次都報 2050。

    **在不會被截斷的 ctx 下這個理由消失**，`prompt_eval_count` 反而看得出差別
    （D-036：8,052 與 8,167）。所以這一支不是「唯一」的管道，而是**與它獨立的
    第二個管道** —— 兩者互相印證。它們一致時結論更強；不一致時
    `truncation_claim_verdict()` 會回 `disagree`，那是要人工看的訊號。

    **讀不到就整格不放**（不是放 0）。在 JSON 裡「沒有這一格」與「這一格是
    0」必須長得不一樣，否則讀的人分不出「沒有變長」與「沒有量」。

    抽成純函式是為了讓它可測：`measure_add()` 要連上 ollama 才跑得動，
    而這一格的算式是這一節唯一有主張的地方。
    """
    out = {}
    c1, c2 = prompt_chars(chat1), prompt_chars(chat2)
    if c1 is not None and c2 is not None:
        out["prompt_chars_first"] = c1
        out["prompt_chars_second"] = c2
        out["prompt_grew_chars"] = c2 - c1
    return out


def truncated_length(num_ctx, num_keep=4):
    """被截斷時，prompt 會被砍到剩幾個 token —— ollama 0.34.2 的閉式解。

    來源是 `llm/llama_server.go:288-330`：

        fullPromptLimit := s.options.NumCtx - 1
        if len(tokens) <= fullPromptLimit {
            return prompt, nil            # 原封不動
        }
        limit := contextShiftPromptLimit(s.options.NumCtx, nKeep)

    而 `contextShiftPromptLimit()` 是

        numCtx - max((numCtx-numKeep)/2, 1)

    `num_keep` 的預設是 4（`api/types.go:1143`）。

    **`num_predict` 完全不在這條式子裡。** 這是最容易誤認的一項，因為
    mem0 的 num_predict=2000 讓 `4096 − 2000 = 2096` 看起來很接近實際量到的
    2050；而 2050 真正的來歷是 `4096/2 + 2`。

    這裡刻意寫成**閉式解而不是拿量測點去擬合**：擬合出來的直線，兩個點就
    定得出來，而「兩個點定出來的線」沒有任何多餘的約束力 —— 上一版就是
    這樣把一個錯的公式扶正的。閉式解在任何 num_ctx 上都算得出預測值，
    所以量測只能去**核對**它，不能反過來定義它。

    除以 2 在 Go 是整數除法（兩個運算元都是 int），所以這裡用 `//`。
    """
    if num_ctx <= 1:
        return 0
    num_keep = max(0, min(num_keep, num_ctx - 1))
    return num_ctx - max((num_ctx - num_keep) // 2, 1)


def min_ctx_required(prompt_tokens):
    """要讓 prompt_tokens 個 token 全部進得去，num_ctx 至少要多少。

    判準是 `len(tokens) ≧ num_ctx`（見 truncated_length 的說明），所以
    「不被截斷」等於 `prompt_tokens < num_ctx`，最小值就是 +1。

    寫成函式而不是在呼叫端寫 `+1`，是因為那個 1 就是**這個結論的全部**：
    少加它，`num_ctx = prompt_tokens` 會剛好落在被砍的那一側，而算出來的
    數字看起來完全正常。附帶一提，這個結論**跟 num_predict 無關** ——
    留給生成的空間不會從 prompt 的預算裡扣。
    """
    return prompt_tokens + 1


def extraction_budget(num_ctx, bound=EXTRACTION_PROMPT_TOKENS_BOUND):
    """在這個 num_ctx 之下、**不會觸發 context shift** 的生成預算 → int 或 None。

    回 None 的情況（三種，全部都是「推導不出來」，**不是「0」**）：
      · num_ctx 不是整數（None、字串、浮點數）
      · num_ctx 是 bool —— `True` 會被 isinstance 放行，但它是旗標不是大小
      · num_ctx ≤ 界 —— 連 prompt 自己佔的空間都不夠，推不出正的預算

    **回 None 時不可以當 0 用。** 0 在 mem0 的 config 裡會被 truthiness 吃掉
    （`--add-max-tokens 0` 現行就是這樣：它靜默地什麼都不做），於是「算不出
    來」會變成「用 mem0 的預設 2,000」—— 那正是這一整條線要消滅的失敗方式。

    這支與 `min_ctx_required()` 是同一組的兩面：
      · `min_ctx_required(prompt)` —— prompt 要多大才進得去
      · `extraction_budget(ctx)` —— 這個 ctx 留給生成多少才不會 shift
    """
    if isinstance(num_ctx, bool) or not isinstance(num_ctx, int):
        return None
    if num_ctx <= 0:
        return None
    budget = num_ctx - bound
    return budget if budget > 0 else None


def budget_resolution(add_max_tokens, ctx):
    """(`--add-max-tokens`, `--ctx`) → `(生效的預算或 None, 來源, 給人看的說明)`。

    純函式：吃兩個數字，不吃 args —— 所以可以離線測試（與 `parse_sections`
    同樣的分工）。**優先序就是這個函式的分支順序**：

      `--add-max-tokens N`  →  N                 `overridden`
      `--ctx N`             →  `num_ctx − 界`    `derived_from_ctx`
      都沒有                →  None（不寫 config）`mem0_default`

    `--add-max-tokens` 永遠贏，而且**刻意不讓推導值從同一條通道進來** ——
    腳本的 `--quick` 就是翻譯成 `--add-max-tokens 200`，若推導值也走那裡，
    `--quick` 會被靜默吃掉（一個已經文件化的旗標變成沒作用）。它是重現
    D-027 條件的**受控實驗**，不可以被蓋掉。

    `mem0_default` 那一格回 None 是**有意義的**：不寫 config ＝ 讓 mem0
    用它自己的 2,000，那才是「mem0 的實況」（裸跑探針時的承諾）。
    """
    if add_max_tokens:
        return add_max_tokens, "overridden", "--add-max-tokens %s 覆寫" % add_max_tokens
    if ctx is None:
        return None, "mem0_default", "沒有覆寫、也沒有給 --ctx → 用 mem0 自己的預設"
    derived = extraction_budget(ctx)
    if derived is None:
        # **不是錯誤，是一個要講出來的結果。** ctx 小到 prompt 上界就吃掉
        # 全部空間（D-027 的受控條件：4096）。這時不覆寫、量 mem0 的實況，
        # 而來源字串與這一句話就是「有沒有推導」的證據。
        return None, "underivable", (
            "num_ctx=%s 推不出正的預算（抽取 prompt 的上界 %d 就吃掉全部）——"
            "**這一輪沒有推導，不覆寫，量的是 mem0 的實況 num_predict=2000**"
            % (ctx, EXTRACTION_PROMPT_TOKENS_BOUND))
    return derived, "derived_from_ctx", (
        "由 num_ctx=%s 推導（%s − %d 的 prompt 上界）—— 這個組合不會觸發 "
        "context shift" % (ctx, ctx, EXTRACTION_PROMPT_TOKENS_BOUND))


def ctx_arg_error(ctx):
    """`--ctx` 的值**根本不像一個 num_ctx** 時回一句話，否則回 None。

    抽成純函式是為了讓「用法錯誤要擋下來」可以被離線測試 —— `main()` 那一行
    只負責印出來（與 `parse_sections` 同樣的分工）。

    **只擋「不是 context 的值」**（非正整數）。「太小、推不出預算」（≤ 界）
    **不算用法錯誤** —— 那正是 D-027 那個受控條件的形狀
    （`OLLAMA_CONTEXT_LENGTH=4096`，prompt 本來就會被截斷），而腳本自己就叫人
    這樣跑（`verify-mem0-add-cost.sh` 的 C2／C3 訊息）。那時推不出預算是
    **事實**，不是錯誤：探針照跑、老實記成 `budget_source=underivable`、並且
    在輸出裡講出來 —— 不覆寫，量的就是 mem0 的實況。

    把它擋成 3 會製造一個**假失敗**：照著文件把 num_ctx 調回 4096 驗截斷的
    人，會被告知儀器壞了（D-016：假失敗比漏報更糟）。
    """
    if ctx is None:
        return None
    if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0:
        return "--ctx 必須是正整數：%r" % (ctx,)
    return None


def budget_note_to_print(meta):
    """要印給人看的預算說明 → 字串；沒有話要說時回 None。

    三種狀態都要講：覆寫與推導要讓人**驗算**那個數字，而**推不出來**
    （ctx 太小）要讓人知道**這一輪沒有推導**（不覆寫 ＝ 量 mem0 的實況，
    抽取預期是空的）。只有「本來就用 mem0 的預設」那一格沒有別的話要說 ——
    每一輪都多一行解釋一個沒有改變的東西，會把真正要看的那一輪稀釋掉。

    `budget_source` 缺席（舊 evidence）時也回 None：不知道來歷就不要替它講話。
    """
    if not meta or meta.get("budget_source") in (None, "mem0_default"):
        return None
    return meta.get("budget_note")


def effective_num_predict(meta):
    """這一輪**真正送進 mem0** 的生成上限 → int 或 None。

    None ＝ 沒有覆寫，用 mem0 自己的預設（`meta.mem0_options.num_predict`）。

    為什麼要有這支：**三個地方要同一個數字** —— `measure_add()` 寫 config、
    `observations()` 印生效值、`grade()` 的 C7 拿它當判準的 `cap`。先前有兩
    處各自 `or` 一次，那是因為當時只有一個來源（`--add-max-tokens`）；推導
    進來之後它們就會漂移，而漂移的症狀正是 D-035 第七節教訓 3：輸出印著
    「mem0 預設，沒有覆寫」，而 mem0 其實收到 8,192。**config 與判準由同一個
    函式餵，是結構上不可能漂移，不是靠自律。**

    `or` 是刻意的：0 在這裡不是合法的上限（`extraction_budget()` 不會回 0，
    `--add-max-tokens 0` 現行也已經被忽略）。
    """
    if not meta:
        return None
    return (meta.get("add_max_tokens_effective")
            or (meta.get("mem0_options") or {}).get("num_predict"))


def truncation_limit_verdict(num_ctx, measured_pec, num_keep=4):
    """量到的 prompt_eval_count 是否等於閉式解預測的長度（C2b 的核對）。

    回傳 dict(expected, measured, matches, reason)；matches 三段語意：
      True  —— 相符
      False —— 量到了，但跟閉式解算的不同（上游變了，別套這條式子）
      None  —— 無法核對：沒回報數字，或**這一輪根本沒有截斷**

    最後那一種要單獨講。prompt 沒被截斷時 prompt_eval_count 是它的完整
    長度，一定不等於預測的砍完長度 —— 若不擋掉，一次「prompt 準備得太短」
    的失誤會長成「公式錯了」，那是假失敗（D-016）。
    """
    expected = truncated_length(num_ctx, num_keep)
    if not measured_pec:
        return {
            "expected": expected,
            "measured": measured_pec,
            "matches": None,
            "reason": "num_ctx=%d：這次沒有回報 prompt_eval_count —— 無法核對。"
            % num_ctx,
        }
    if measured_pec > num_ctx:
        return {
            "expected": expected,
            "measured": measured_pec,
            "matches": None,
            "reason": "num_ctx=%d：評估了 %d 個 token，比 num_ctx 還多 —— "
            "這一輪沒有截斷，不能拿來核對砍完的長度。"
            % (num_ctx, measured_pec),
        }
    if measured_pec == expected:
        return {
            "expected": expected,
            "measured": measured_pec,
            "matches": True,
            "reason": "num_ctx=%d：量到 %d，閉式解也算 %d —— 相符。"
            % (num_ctx, measured_pec, expected),
        }
    return {
        "expected": expected,
        "measured": measured_pec,
        "matches": False,
        "reason": "num_ctx=%d：量到 %d，但閉式解算的是 %d（差 %d）—— "
        "**不要**把這條式子套在這個版本上，先查清楚它改成什麼了。"
        % (num_ctx, measured_pec, expected, measured_pec - expected),
    }


def threshold_bracket_verdict(prompt_tokens, at_ctx_pec, above_ctx_pec, num_keep=4):
    """夾縫測試：截斷的門檻是不是正好 `prompt_tokens`（也就是 num_ctx）。

    兩次呼叫的 prompt **完全相同**，只有 num_ctx 差 1：
      at_ctx_pec    —— num_ctx = prompt_tokens     預期被砍，剩 truncated_length(P)
      above_ctx_pec —— num_ctx = prompt_tokens + 1 預期原封不動，等於 P

    夾縫只有一個 token，所以它當場分開幾個「講起來都通」的規則：
      · 門檻 = num_ctx / 2  → num_ctx=P+1 時 P ≧ (P+1)/2 仍成立 → 也會砍 ✗
      · 門檻 = num_ctx − c  → 要不砍得要求 P < P+1−c，即 c ≦ 0
      · 門檻 = num_ctx − num_predict − c → num_predict=2000 時差得更遠 ✗

    所以這是唯一一個能把「要開多大」講死的量測。C2 的 A/B 兩次做不到：
    「4096 砍、16384 不砍」對上面**每一條**規則都成立。

    回傳 dict(prompt_tokens, expected_cut, consistent, reason)；
    consistent 三段語意，None 是「量測不成立」而不是「規則錯了」。
    """
    expected_cut = truncated_length(prompt_tokens, num_keep)
    if not at_ctx_pec or not above_ctx_pec:
        return {
            "prompt_tokens": prompt_tokens,
            "expected_cut": expected_cut,
            "at_ctx_pec": at_ctx_pec,
            "above_ctx_pec": above_ctx_pec,
            "cut_as_predicted": None,
            "above_untouched": None,
            "consistent": None,
            "reason": "兩次呼叫有一次沒有回報 prompt_eval_count（%r / %r）—— "
            "夾縫測試不成立，無法判定。" % (at_ctx_pec, above_ctx_pec),
        }

    cut_as_predicted = at_ctx_pec == expected_cut
    above_untouched = above_ctx_pec == prompt_tokens

    if cut_as_predicted and above_untouched:
        return {
            "prompt_tokens": prompt_tokens,
            "expected_cut": expected_cut,
            "at_ctx_pec": at_ctx_pec,
            "above_ctx_pec": above_ctx_pec,
            "cut_as_predicted": True,
            "above_untouched": True,
            "consistent": True,
            "reason": "num_ctx=P（%d）砍到 %d、num_ctx=P+1（%d）完整留下 %d —— "
            "門檻正好是 num_ctx，這份 prompt 要開到 P+1 = %d。"
            % (prompt_tokens, at_ctx_pec, prompt_tokens + 1, above_ctx_pec,
               prompt_tokens + 1),
        }

    wrong = []
    if not cut_as_predicted:
        wrong.append("num_ctx=P 量到 %d，閉式解預期 %d" % (at_ctx_pec, expected_cut))
    if not above_untouched:
        wrong.append("num_ctx=P+1 量到 %d，預期原封不動的 %d"
                     % (above_ctx_pec, prompt_tokens))
    return {
        "prompt_tokens": prompt_tokens,
        "expected_cut": expected_cut,
        "at_ctx_pec": at_ctx_pec,
        "above_ctx_pec": above_ctx_pec,
        "cut_as_predicted": cut_as_predicted,
        "above_untouched": above_untouched,
        "consistent": False,
        "reason": "夾縫測試不成立（%s）—— 門檻不是 num_ctx，"
        "「要開多大」那個數字不能照算，先查清楚。" % "；".join(wrong),
    }


def canary_reading(answer, head_marker, tail_marker, head_first=True):
    """模型回報看到哪些 marker → 截斷是從哪一端吃掉 context 的。

    這是 C3。兩次呼叫的 prompt **長度完全相同**，只有 marker 的位置不同，
    所以不需要任何門檻值：只要兩端的可見性不一樣，就是單向截斷。

    marker 用隨機字串，模型猜不到 —— 沒有看到卻答對的機率可以忽略。

    **`head_first` 不是在描述結果，是在還原座標。** `head`／`tail` 只是
    兩個標籤，它們**在哪一端**由呼叫端決定（`_canary_messages()`）：第二次
    呼叫把兩者對調，所以那一次「head 標籤看得到」指的是**結尾**看得到。
    少了這個參數，這一支只能照標籤下判斷，於是在鏡像那一次說出與事實相反
    的方向 —— 2026-09-21 的 C3 重測就是這樣：鏡像明明成立了，輸出卻寫著
    「context 是從**結尾**被丟掉的。這比較不尋常」。**那句話手上的資訊
    只夠它說「哪個標籤看得到」。**

    映回位置之後，兩次排列講的是同一個 context 的同一端，所以 reason
    **必然相同** —— 那正是對照組該有的樣子（見
    test_canary_reversed_layout_says_the_same_thing）。

    **這一支只讀文字，所以它只能回答「回覆裡有什麼」。** 它無法分辨
    「模型講完了、兩端都沒看到」與「模型根本沒講完」—— 後者請走
    `canary_verdict()`，那裡才會先檢查回覆能不能當證據。

    三個回傳值要分清楚（D-018 的三態）：True 是**看到方向**、False 是
    **確定的對稱結果**（只有「兩端都看到」算）、None 是**不知道**。
    """
    if answer is None:
        return {
            "head_seen": None,
            "tail_seen": None,
            "asymmetric": None,
            "reason": "沒有拿到模型的回答 —— 無法判定",
        }
    text = str(answer)
    head_seen = head_marker in text
    tail_seen = tail_marker in text
    if head_seen and tail_seen:
        # 唯一一種**確定的**對稱結果：兩端都看得到 → 這份 prompt 沒被截斷。
        return {
            "head_seen": True,
            "tail_seen": True,
            "asymmetric": False,
            "reason": "兩端都看得到 —— 這一份 prompt 沒有被截斷",
        }
    if not head_seen and not tail_seen:
        # **「兩端都看不到」是「不知道」，不是「對稱」。**
        #
        # 它同時容納兩種完全相反的世界：模型看不到（截斷吃掉了整個系統提示
        # 詞），與模型沒有照做。這兩者在這裡是**同一筆觀測** —— 所以它推不出
        # 任何方向。原本這裡回 asymmetric=False，等於把一個不確定的觀測
        # 講成一個確定的結果；run2 就是這樣把「沒有量到」印成「量到：兩端都
        # 看不到」的（那一次的成因見 canary_answer_usable()）。
        #
        # 方向要靠**兩次排列的差異**才定得出來：一端看得到、另一端看不到。
        # 只有一端確定看得到時，才有資訊。
        return {
            "head_seen": False,
            "tail_seen": False,
            "asymmetric": None,
            "reason": "兩端都看不到 —— 這既不是「沒有截斷」也不是「兩端都被"
                      "砍掉」：模型沒看到、與模型沒有照做，在這裡是同一筆觀測。"
                      "**沒有方向可言。**",
        }
    # 映回位置再說。`begin_seen`／`end_seen` 是**座標**，`head_seen`／
    # `tail_seen` 是**標籤** —— 回傳值仍然報標籤（那是觀測到的原始事實），
    # 但句子講的是位置（那才是主張）。
    begin_seen = head_seen if head_first else tail_seen
    end_seen = tail_seen if head_first else head_seen
    visible_label = "tail" if head_first else "head"
    if end_seen and not begin_seen:
        reason = (
            "開頭看不到、結尾看得到 —— context 是從**開頭**被丟掉的，"
            "意思是被砍掉的正是模型自己的指令。"
            "（看得到的 marker 落在結尾，是 %s）" % (visible_label,)
        )
    else:
        reason = (
            "開頭看得到、結尾看不到 —— context 是從**結尾**被丟掉的。"
            "這比較不尋常，值得再看一眼。"
            "（看得到的 marker 落在開頭，是 %s）" % (visible_label,)
        )
    return {
        "head_seen": head_seen,
        "tail_seen": tail_seen,
        "asymmetric": True,
        "reason": reason,
    }


def canary_answer_usable(chat, num_predict):
    """這次 canary 的回覆能不能當證據 → (True／False／None, 為什麼)。

    **這一條是 run2 逼出來的，而它是一個真的否定結果。**

    run2 的兩次 canary 都量到 prompt_eval_count=2050（截斷確實發生了），
    但兩端都看不到 marker。ollama 的日誌顯示**兩次生成都停在 2000 個
    token** —— 正好是 mem0 的 `num_predict`。也就是說模型從來沒有講完：
    qwen3 這類推理模型會先產生 thinking，而 thinking 也佔 `eval_count`。

    在那個狀態下 `content` 可能是空的、或是一段還沒講到 marker 的內容。
    **兩者都不是「模型看不到 marker」的證據** —— 而舊的 `canary_reading`
    把「回覆裡沒有 marker」一律讀成一個確定的答案，於是「沒有量到」被印成
    了「量到：兩端都看不到」。那是 D-016 那一類錯誤（假失敗比漏掉更糟）
    的鏡像：**這裡是假成功**，一個不確定的觀測被當成確定的事實。

    回傳 None 是「讀不到足以判斷的數字」，那是儀器問題，不是模型問題
    —— 與 threshold_bracket_verdict() 的 None 同一個道理。
    """
    if not chat:
        return None, "沒有這次呼叫的紀錄"
    if chat.get("content") is None:
        return None, "沒有拿到回覆內容（content 是 None）"
    eval_count = chat.get("eval_count")
    if eval_count is None or num_predict is None:
        return None, ("讀不到 eval_count（%r）或 num_predict（%r）—— 無法判斷"
                      "模型是講完了還是被預算切斷" % (eval_count, num_predict))
    if eval_count >= num_predict:
        return False, ("生成停在 %d 個 token，正好是 num_predict 的上限（%d）"
                       "—— 模型是被**預算**切斷的，不是自己講完的。"
                       "推理模型的 thinking 也佔這個預算，所以"
                       "「有回覆」不等於「講完了」。" % (eval_count, num_predict))
    if not str(chat.get("content") or "").strip():
        return False, ("模型只生了 %d 個 token 且回覆是空的（%d 未達上限）"
                       "—— thinking 不進 content，所以沒有可讀的答案"
                       % (eval_count, num_predict))
    return True, "生成 %d 個 token、未達上限 %d，回覆非空" % (eval_count, num_predict)


def canary_verdict(chat, head_marker, tail_marker, num_predict, head_first=True):
    """C3 的判定入口：**先問這則回覆能不能當證據，再問它看到什麼。**

    兩步刻意分開，而且第一步是**必要**的：`canary_reading` 只讀得到文字，
    它分辨不出「模型講完了、兩端都沒看到」與「模型根本沒講完」。run2 的
    失敗就是後者被讀成了前者。

    `head_first` 要一路傳到底 —— 它是座標，不是裝飾；預設 True 只服務
    既有呼叫端（見 canary_reading 的說明）。

    回傳的 dict 比 canary_reading() 多一個 `answer_usable`：
      True  → 照 canary_reading() 的結果走
      False → 這一節**沒有量到**（asymmetric=None），原因在 reason 裡
      None  → 連判斷可用性的數字都讀不到（asymmetric=None）
    """
    usable, why = canary_answer_usable(chat, num_predict)
    if usable is not True:
        return {
            "head_seen": None,
            "tail_seen": None,
            "asymmetric": None,
            "answer_usable": usable,
            "reason": "這一節沒有量到 —— %s。（「沒有量到」不等於「量到沒"
                      "有」：兩者在 D-018 下是不同的結束碼。）" % why,
        }
    r = canary_reading(chat.get("content"), head_marker, tail_marker, head_first)
    r["answer_usable"] = True
    return r


def thinking_verdict(thinking_chars_on, eval_count_on, thinking_chars_off,
                     eval_count_off, num_predict_on=None, num_predict_off=None):
    """thinking 有沒有被算進成本 → C4。

    mem0 的 `_parse_response()` 只留 `message.content`，thinking 被丟掉；
    但 ollama 的 `eval_count`／`eval_duration` 是**含 thinking 的**。
    所以如果 thinking 非空且關掉之後 eval_count 掉下來，那表示
    「看不到的 token」是實際付過錢的。

    **`num_predict` 是為了分辨「講完了」與「被預算切斷」。** 少了它，
    一個撞到上限的 `eval_count` 會被當成模型自己停在那裡，於是差值被當成
    真值 —— 而它其實只是**下界**。這與 C3 是同一個缺陷（run2 的 canary
    也是停在 `num_predict` 上，被讀成「兩端都看不到」）：**被切斷的生成
    不能拿來當完整的量測。** 兩側任一邊被切斷，差值就只會更小，不會更大。

    回傳 dict(thinking_present, counts_toward_eval, saved_tokens,
    saved_is_lower_bound, reason)。
    """
    if thinking_chars_on is None or eval_count_on is None:
        return {
            "thinking_present": None,
            "counts_toward_eval": None,
            "saved_tokens": None,
            "saved_is_lower_bound": None,
            "reason": "沒有拿到 mem0 options 那次呼叫的資料 —— 無法判定",
        }
    thinking_present = thinking_chars_on > 0
    if thinking_chars_off is None or eval_count_off is None:
        return {
            "thinking_present": thinking_present,
            "counts_toward_eval": None,
            "saved_tokens": None,
            "saved_is_lower_bound": None,
            "reason": "沒有拿到 think=False 那次呼叫的資料 —— "
            "只知道 thinking 有 %d 個字元，無法確認它有沒有計入 eval_count"
            % thinking_chars_on,
        }
    saved = eval_count_on - eval_count_off

    # 「撞到上限」＝ eval_count 剛好等於 num_predict。用 >= 而不是 ==：
    # 真的量到比上限多一個 token 的話，那更該被當成被切斷，不是被當成正常。
    cut_on = num_predict_on is not None and eval_count_on >= num_predict_on
    cut_off = num_predict_off is not None and eval_count_off >= num_predict_off
    # 三態，不是二態：True＝確知是下界、False＝兩側都查過且都不是下界
    # （點值）、None＝**沒查**（呼叫端沒給 num_predict）。把「沒查」講成
    # False 就是在斷言一個點值 —— 那正是這一條要修的那個錯。
    checked = num_predict_on is not None or num_predict_off is not None
    lower_bound = (cut_on or cut_off) if checked else None

    counts_toward = thinking_present and saved > 0
    if not thinking_present:
        reason = "mem0 的 options 下 thinking 是空的 —— 這次沒有隱藏成本"
    elif counts_toward:
        reason = (
            "thinking 有 %d 個字元，關掉 think 之後 eval_count 從 %d 降到 %d "
            "—— %d 個 token 是花在模型看得到、但 mem0 丟掉的推理上。"
            % (thinking_chars_on, eval_count_on, eval_count_off, saved)
        )
        if cut_on:
            reason += (" 但**開著 thinking 的那一次停在 num_predict 上限（%d）**"
                       "—— 它是被預算切斷的，不是自己講完的，**所以 %d 是下界**，"
                       "放寬上限只會更大。" % (num_predict_on, saved))
        if cut_off:
            reason += (" 但**關掉 thinking 的那一次也停在 num_predict 上限（%d）**"
                       "—— 這一側同樣不完整，**%d 是下界**。"
                       % (num_predict_off, saved))
    else:
        reason = (
            "thinking 有 %d 個字元，但關掉 think 之後 eval_count 沒有下降"
            "（%d → %d）—— 兩者的關係與預期不符，先查清楚。"
            % (thinking_chars_on, eval_count_on, eval_count_off)
        )
        if cut_on or cut_off:
            reason += (" **而且有一側停在 num_predict 上限（開=%s、關=%s）**"
                       "—— 這個「沒有下降」發生在被切斷的生成上，"
                       "不可以當成反例。"
                       % (cut_on, cut_off))
    return {
        "thinking_present": thinking_present,
        "counts_toward_eval": counts_toward,
        "saved_tokens": saved if counts_toward else None,
        "saved_is_lower_bound": lower_bound if counts_toward else None,
        "reason": reason,
    }


def call_summary(calls):
    """依 kind 統計呼叫次數與 token。

    `calls` 是記錄器累積的 list，每一項是一個 dict（見 _install_recorder）。
    這是 C1 與 C5 的依據：**數呼叫**，不是猜呼叫。
    """
    summary = {"total": len(calls or []), "chat": 0, "embed": 0,
               "chat_prompt_tokens": 0, "chat_eval_tokens": 0, "chat_wall": 0.0}
    for c in calls or []:
        kind = c.get("kind")
        if kind == "chat":
            summary["chat"] += 1
            summary["chat_prompt_tokens"] += c.get("prompt_eval_count") or 0
            summary["chat_eval_tokens"] += c.get("eval_count") or 0
            summary["chat_wall"] += c.get("wall") or 0.0
        elif kind == "embed":
            summary["embed"] += 1
    return summary


def cost_split(timings):
    """add() 的牆上時間花在哪裡。

    刻意**不是判準**：這台機器的秒數不該拿來當通過與否的依據（D-014）。
    但「LLM 呼叫的生成時間是否大於所有嵌入加起來」是一個不需要門檻的序關係，
    放在觀察段給人看。

    `timings` 是 dict，值為秒數。回傳各項與總和、以及佔比。
    """
    if not timings:
        return {"total": 0.0, "shares": {}}
    total = sum(v for v in timings.values() if isinstance(v, (int, float)))
    shares = {}
    for k, v in timings.items():
        if isinstance(v, (int, float)) and total > 0:
            shares[k] = v / total
    return {"total": total, "shares": shares}


def budget_premise_problems(evidence):
    """C6／C7 —— 推導式生成預算的兩個前提（D-037）。回傳 problems 清單。

    **只吃 add 節與 meta**，所以 add 跑了就判得出來，哪怕別的節沒跑 —— 而那
    正是 D-037 的處方（`--sections add`）。這兩條原本寫在 `grade()` 的尾端，
    於是 `grade()` 開頭那個「有整節沒量到就提早返回」會順手把它們一起吞掉：
    **哨兵在唯一會被用到的那個組態裡是安靜的**。2026-09-22 的 #66 就是這樣跑
    的 —— C6／C7 一次都沒有被判定，只有觀察段那幾行能看。原本那段註解寫著
    「上面那個 missing 檢查（`calls` 缺席）已經先返回了，所以走到這裡一定拿
    得到 first_add」：前半是對的，但它只說明了「這裡拿得到 first_add」，沒說
    「這裡一定會被執行到」—— 提早返回不只發生在 add 缺席時。

    add 節真的沒跑時回空清單，**不**逐條喊「讀不到」：那正是上面那個 missing
    檢查要防的「與事實相反的敘述」（沒量到 ≠ 沒過）。缺節訊息已經講過了。

    這兩條問的**不是**「上游變了沒」，而是「我們自己這條推導的前提還成不
    成立」，所以它們是硬判準，而 `truncation_claim_verdict()` 那類 claim 判讀
    只進觀察段：claim 問的是世界的變化（給人看的訊號），前提問的是**我們算出
    來的數字對不對**。界過期 ＝ 預算沒有依據，而它的症狀是「安靜地抽出 0
    筆」—— 靜默通過正是 D-036 花了四小時才買到的教訓。

    **只在預算是推導來的時候成立。** `--add-max-tokens N`（含 `--quick` 翻出來
    的 200）是重現 D-027 條件的受控實驗：那時「預算不夠」是**設計**，讓它失敗
    會把一個有用的對照組關掉。
    """
    if not evidence.get("calls"):
        return []
    meta = evidence.get("meta") or {}
    if meta.get("budget_source") != "derived_from_ctx":
        return []

    problems = []
    cap = effective_num_predict(meta)
    ctx_arg = meta.get("ctx_arg")
    adds = [("add() 第一次", evidence.get("first_add") or {}),
            ("第二次", evidence.get("second_add") or {})]
    adds = [(label, a) for label, a in adds if a]

    # C6：**界要成立** —— 這一輪的 prompt 沒有超過界。取兩次之中大的那一個：
    # 第二次的 prompt 比較長（mem0 會把既有歷史一起送），界要蓋住的是最大的
    # 那一份。
    pecs = [(label, a["prompt_eval_count"]) for label, a in adds
            if isinstance(a.get("prompt_eval_count"), int)
            and not isinstance(a.get("prompt_eval_count"), bool)]
    if not pecs:
        problems.append(
            "C6：讀不到 prompt_eval_count，無法確認推導用的界（%d）還成立。"
            "這一條是那個界的哨兵，讀不到就不算過 —— 見 D-037。"
            % EXTRACTION_PROMPT_TOKENS_BOUND)
    elif max(v for _, v in pecs) > EXTRACTION_PROMPT_TOKENS_BOUND:
        worst_label, worst = max(pecs, key=lambda kv: kv[1])
        problems.append(
            "C6：抽取 prompt 量到 %r 個 token（%s，兩次是 %s），**超過推導用"
            "的界 %d** —— 預算 %s 是用一個**過期的界**算出來的，所以它不再"
            "保證不會觸發 context shift（不變式是 prompt ≤ 界 ⇒ "
            "prompt + 預算 ≤ num_ctx=%s）。"
            "處方是**更新界並記錄這次的觀測**（EXTRACTION_PROMPT_TOKENS_"
            "BOUND），不是把預算調大 —— 調大就是把 D-036 第十節那條規矩"
            "再犯一次。見 D-037。"
            % (worst, worst_label,
               "、".join("%s=%s" % kv for kv in pecs),
               EXTRACTION_PROMPT_TOKENS_BOUND, cap, ctx_arg))

    # C7：**預算要夠** —— 模型是自己停下來的，不是被這個上限切斷的。
    verdicts = [(label, generation_stop_verdict(
        a.get("eval_count"), cap, a.get("done_reason"))) for label, a in adds]
    bad = [(label, s or "讀不到判準需要的觀測（eval_count／done_reason）")
           for label, (v, s) in verdicts if v != "stopped"]
    if not verdicts or bad:
        readings = "、".join("%s：%s" % kv for kv in bad) or "兩次 add() 都沒有觀測"
        problems.append(
            "C7：預算是從 num_ctx=%s 推導出來的 %s，而生成**不是模型自己停"
            "下來的**（%s）—— 也就是說模型需要的比我們允許的多。"
            "處方是**把 num_ctx 開大**（預算 = num_ctx − %d 的 prompt 上界，"
            "會自己跟著走），**不是把上限直接調大** —— 後者就是魔數，下一個"
            "模型／下一份 prompt 會再犯一次。見 D-037。"
            % (ctx_arg, cap, readings, EXTRACTION_PROMPT_TOKENS_BOUND))

    return problems


def grade(evidence):
    """純函式：證據 → (passed, problems)。

    problems 每一項都是可以直接印給人看的字串 —— 講「哪裡沒過」，
    不是只說「沒過」。

    每一條的失敗訊息都刻意寫成「與 README 第 3 項的說法不符」或
    「與原始碼讀出來的行為不符」，而不是「壞掉了」：這支探針量的是
    **上游的行為**，它變了不代表誰壞了，代表 item 3 的結論要重讀。
    """
    problems = []

    if not evidence:
        return False, ["沒有收到任何證據 —— 探針沒有跑到判準那一步"]

    # 有一整節沒量到時，**不要**在下面各條判準裡各喊一次。那些訊息會變成
    # 「抽取 prompt 沒有被截斷」「thinking 是空的」這種**與事實相反的敘述**
    # ——實際上根本沒量到，只是 `%s` 把 None 拼進了句子。輸出是最容易被
    # 相信的東西，講相反的話比不講話糟。這裡一次講清楚，並明說這不是判準。
    missing = [name for name, key in
               (("C2 截斷", "truncation"), ("C3 canary", "canary"),
                ("C4 thinking", "thinking"), ("C1／C5 add()", "calls"))
               if not evidence.get(key)]
    if missing:
        # **但 C6／C7 還是要判。** 它們只吃 add 節與 meta，而 add 節有跑的時候
        # （`--sections add`，也就是 D-037 的處方）這一輪的 C2／C3／C4 缺席並
        # 不影響那兩條能不能判。少了這一行，哨兵在唯一會被用到的組態裡是安靜
        # 的 —— 見 budget_premise_problems() 的 docstring。
        return False, [
            "缺少這幾節的量測結果：%s —— 探針沒有跑到那裡，通常是環境問題"
            "（見上面的 fatal），**不是判準沒過**。" % "、".join(missing)
        ] + budget_premise_problems(evidence)

    calls = evidence.get("calls") or {}
    trunc = evidence.get("truncation") or {}
    canary = evidence.get("canary") or {}
    think = evidence.get("thinking") or {}
    dup = evidence.get("duplicate") or {}

    # ── C1：一次 add() 只發一次 chat 呼叫 ───────────────────
    # README 說「多一次 LLM 呼叫」。這句話要嘛對要嘛不對，而且是可以數的。
    first_add_chat = calls.get("first_add_chat")
    if first_add_chat != README_CLAIMED_CALLS:
        problems.append(
            "C1：第一次 add() 發出了 %r 次 chat 呼叫，README 第 3 項說一次。"
            "呼叫數變了就是成本模型變了 —— 重讀 D-027 再決定怎麼改 README。"
            % (first_add_chat,)
        )

    # ── C2：抽取 prompt 被 ollama 的預設 num_ctx 截斷 ──
    # 訊息裡必須點出成因是**哪一項**：mem0 不送 num_ctx，所以起作用的是
    # ollama 的預設值。寫成「context 太小」會把讀的人帶去調錯的地方
    # （mem0 的 config、或 num_predict —— 後者與截斷無關，見 D-027）。
    if trunc.get("truncated") is not True:
        problems.append(
            "C2：抽取 prompt 沒有被截斷（判定＝%r）。%s "
            "這代表 ollama 的預設 num_ctx 容得下 33,653 字元的系統提示詞，"
            "或 mem0 開始送 num_ctx 了 —— 兩種都要重讀 D-027 再改文件。"
            % (trunc.get("truncated"), trunc.get("reason"))
        )

    # C2 的對照組必須真的成立，否則上面那個「沒有截斷」是假的：B 若沒跑在
    # FULL_CTX，兩次都會被截到同一個長度，a == b，比較就失去意義。
    # `is False`（而不是 falsy）是刻意的：讀不到 /api/ps 時是 None，
    # 那叫「無法判定」，不是「沒生效」—— 當成沒生效會是假失敗（D-016）。
    if trunc.get("full_ctx_took_effect") is False:
        problems.append(
            "C2：對照組要求的 num_ctx=%s 沒有生效（/api/ps 說 %s）—— "
            "A/B 兩次其實跑在同一個 context 下，這一節的比較不成立，"
            "「沒有截斷」的結論不可信。"
            % (FULL_CTX, trunc.get("context_length_after_full"))
        )

    # ── C3：被丟掉的是開頭（模型自己的指令）────────────────
    # **「沒有量到」與「量到反例」要分開講。** 前者是儀器問題（結束碼 3），
    # 後者才是判準沒過（結束碼 1）。合併成一句會讓「這支腳本該修」讀起來
    # 像「上游變了」—— 而 run2 的 C3 正是前者（見 canary_answer_usable）。
    #
    # **兩次呼叫都要看。** 第二次是把兩個 marker 對調位置（tail 在前、
    # head 在後），它是一組**位置對照**：如果可見性真的由位置決定，那第二
    # 次看到的應該換成另一個 marker。少了這一條，第一次的「尾端看得到」
    # 排除不掉一個很具體的可能 —— 提示詞裡的字面就寫著 `HEADMARK`／
    # `TAILMARK`，模型有可能抓著字面回報，與位置無關。
    rev = canary.get("reversed") or {}
    not_measured = []
    if canary.get("answer_usable") is not True:
        not_measured.append("第一次（head 在前）：%s" % (canary.get("reason"),))
    if rev.get("answer_usable") is not True:
        not_measured.append("第二次（tail 在前，位置對照）：%s" % (rev.get("reason"),))
    if not_measured:
        problems.append(
            "C3：canary **沒有量到** —— %s 這是探針自己的問題，不是上游變了："
            "沒有給推理模型足以講完的生成預算。main() 會回結束碼 3。"
            % ("；".join(not_measured),)
        )
    elif canary.get("asymmetric") is not True or canary.get("tail_seen") is not True:
        problems.append(
            "C3：canary 的位置對照沒有呈現「開頭看不到、結尾看得到」"
            "（head_seen=%r, tail_seen=%r）。%s "
            "截斷的方向決定這個危險的性質：砍掉結尾只是少一則記憶，"
            "砍掉開頭是模型根本沒看到抽取規則。"
            % (canary.get("head_seen"), canary.get("tail_seen"), canary.get("reason"))
        )
    elif rev.get("head_seen") is not True or rev.get("tail_seen") is not False:
        problems.append(
            "C3：canary 的**位置對照組**沒有呈現鏡像 —— 把兩個 marker 對調"
            "位置之後，看到的應該換成另一個（head_seen=%r, tail_seen=%r）。%s "
            "這一條擋的是「模型回報的 marker 與位置無關」：提示詞的字面裡就"
            "寫著 HEADMARK／TAILMARK，少了這條對照，第一次量到的「尾端看得到」"
            "就排除不掉「模型只是照著問題的字面回答」。"
            % (rev.get("head_seen"), rev.get("tail_seen"), rev.get("reason"))
        )

    # ── C4：thinking 被算進成本，卻被 mem0 丟掉 ──────────────
    if think.get("thinking_present") is not True:
        problems.append(
            "C4：mem0 的 options 下 thinking 是空的。%s "
            "若上游改成預設不推理，這一條的隱藏成本就消失 —— 那是好事，"
            "但要記得回來把 D-027 的成本結論下修。" % (think.get("reason"),)
        )
    elif think.get("counts_toward_eval") is not True:
        problems.append(
            "C4：thinking 非空，但無法確認它計入 eval_count。%s" % (think.get("reason"),)
        )

    # ── C5：重複寫入仍然付一次完整的 LLM 呼叫 ────────────────
    # 這是設計事實，不是 bug：Phase 5 的雜湊去重在 Phase 2 之後。
    # 它之所以是判準，是因為「上游把去重移到 LLM 之前」會讓它變成假的 ——
    # 而那正好是第四階段會想做的事。
    second_add_chat = dup.get("second_add_chat")
    if second_add_chat != README_CLAIMED_CALLS:
        problems.append(
            "C5：第二次 add()（內容與第一次完全相同）發出了 %r 次 chat 呼叫，"
            "預期 1 次。若變成 0 次，代表上游把去重移到 LLM 之前了 —— "
            "第四階段的成本估算可以下修，重讀 D-027。"
            % (second_add_chat,)
        )

    # ── C6／C7：推導式生成預算的兩個前提 ─────────────────────
    # 這一輪跑滿了所有的節，所以上面那個 missing 檢查沒有提早返回 —— 兩條
    # 哨兵照判。它們的實作與理由都在 budget_premise_problems()（分段輪次要
    # 靠它才判得到，見那個函式的 docstring）。
    problems.extend(budget_premise_problems(evidence))

    return (len(problems) == 0), problems


def exit_code(evidence, passed):
    """這一輪的結束碼 —— 純函式。**順序就是它的規格**，每一步都有理由。

    D-018 的四個碼：0 通過、1 判準沒過（通常代表上游變了）、2 無法判定
    （環境問題，或**沒跑滿**）、3 探針自己壞掉（這支腳本該修）。

    順序刻意是「**先問這一輪算不算一次完整的量測，再問判準**」：一個儀器
    壞掉的量測、或一個只跑了一部分的輪次，都不可以走到 1 —— 那會把它說成
    「上游變了」，而實際上這一輪根本沒有結果。run2 就是這樣：C3 的儀器
    壞掉，而它差一點被回報成「判準沒過」。
    """
    if evidence.get("fatal"):
        return EXIT_INDETERMINATE

    # C3 有**兩次**呼叫（位置對照），任一次沒量到都算儀器問題 —— 只看第一
    # 次的話，一個沒量到的對照組會讓整輪走到 0。而對照組沒量到時，判準
    # 本身是 fail 的（grade 會記一筆），於是「儀器壞掉」會被講成「判準沒過」，
    # 正是這一段開頭要防的那件事。
    canary = evidence.get("canary") or {}
    rev = canary.get("reversed") or {}
    states = [canary.get("answer_usable"), rev.get("answer_usable")]
    if any(s is False for s in states):
        # 儀器問題，不是受測對象的問題 → 3：這支腳本該修
        return EXIT_BROKEN
    if canary and any(s is None for s in states):
        # 連判斷可用性的數字都讀不到 → 2。對照組整個缺席也算讀不到：
        # 那個時候 evidence 是不完整的，不是「判準沒過」。
        return EXIT_INDETERMINATE

    sections = evidence.get("sections_run")
    if sections is None or tuple(sections) != tuple(ALL_SECTIONS):
        # 沒跑滿，**或無法確認跑滿** —— 兩者都不可以是通過，也不該說成判準
        # 沒過。讀不到就當沒跑滿，是刻意的：這裡要 fail-closed。
        return EXIT_INDETERMINATE
    if not evidence.get("calls"):
        return EXIT_INDETERMINATE
    return EXIT_PASS if passed else EXIT_FAIL


def generation_stop_verdict(eval_count, cap, done_reason):
    """這次生成是**模型自己停下來的**，還是**被 num_predict 切斷的**？

    回 `(verdict, sentence)`，verdict ∈ `{"stopped", "capped", "disagree",
    "unknown"}`。

    為什麼要有這支：「生成 8,000 個 token」與「被預算切在 8,000」是兩件完全
    不同的事，而**它們在輸出上長得一模一樣**。這一條是 D-027 第七節那個陷阱
    的同一種病，只是換了一個欄位。

    兩個來源，強度不同，所以**分開講**：
      · `done_reason` —— ollama 自己的陳述（`"stop"` 自己停、`"length"` 撞到
        上限）。這是觀測。
      · `eval_count >= cap` —— 我們的推論。`cap` 缺席時它無從推起。
    兩者都在而且**互相矛盾**時回 `"disagree"`：那是有話要說的情況，不是
    挑一個相信的時候。
    """
    n = eval_count if isinstance(eval_count, int) and not isinstance(eval_count, bool) \
        else None
    c = cap if isinstance(cap, int) and not isinstance(cap, bool) else None

    inferred = None
    if n is not None and c is not None:
        inferred = "capped" if n >= c else "stopped"

    if done_reason in ("stop", "length"):
        stated = "capped" if done_reason == "length" else "stopped"
        if inferred is not None and inferred != stated:
            return "disagree", (
                "**ollama 與 token 數對不起來**：done_reason=%r 說「%s」，"
                "但 eval_count=%s 對 num_predict=%s 推得「%s」。"
                "兩個都不採信 —— 這一格要人工看。"
                % (done_reason,
                   "撞到上限" if stated == "capped" else "自己停的",
                   n, c,
                   "撞到上限" if inferred == "capped" else "自己停的")
            )
        if stated == "capped":
            if c is not None:
                return "capped", (
                    "（**ollama 回的 done_reason=length —— 撞到 num_predict=%s "
                    "的上限，是被切斷的**）" % c
                )
            return "capped", "（**ollama 回的 done_reason=length —— 是被切斷的**）"
        return "stopped", (
            "（ollama 回的 done_reason=stop —— 模型**自己停下來的**，"
            "不是被預算切斷的）"
        )

    if inferred == "capped":
        return "capped", (
            "（**撞到 num_predict=%s 的上限，是被切斷的**"
            "；這是從 eval_count 推的，這一輪沒有讀到 done_reason）" % c
        )
    if inferred == "stopped":
        return "stopped", (
            "（eval_count=%s < num_predict=%s，**推得沒有撞到上限**"
            "；這一輪沒有讀到 done_reason）" % (n, c)
        )
    return "unknown", ""


def truncation_claim_verdict(ctx, min_ctx_for_extraction, pec_first, pec_second):
    """「兩次都被截斷到同一個上限，所以 prompt_eval_count 看不出差別」——
    **這一輪**這句話成立嗎？

    回 (verdict, sentence)，verdict ∈ {"truncated", "not_truncated", "disagree",
    "unknown"}。

    為什麼要問：那句話寫於 num_ctx=4096 的年代。當時兩次 add 的 prompt 都被砍到
    2050，所以相減永遠是 0，那句話是真的。但在 num_ctx=16384 之下 prompt
    8,052／8,167 完整進去，`prompt_eval_count` 反而**看得出**那個差別 ——
    同一句話從真變成假，而它印在輸出裡最常被讀的那一行（D-035 第六節；D-036
    第七節拿到了觀測實例：那句話在同一行裡被自己的 8,167 反駁）。

    判準用既有的 evidence，不新增管線：

      ctx                      meta.context_length_before（/api/ps 讀到的生效值）
      min_ctx_for_extraction   ctx_law.min_ctx_for_extraction（= prompt_tokens + 1）
                               只在門檻夾準（consistent）時才有 —— 沒有就回 unknown

    **內在一致性檢查**：兩次都被截斷到同一個上限，那兩個 prompt_eval_count 就
    **必須相等**（同一個 num_ctx 下 truncated_length() 是同一個數）。不相等而
    判準說「有截斷」，那就是判準與觀測打架 —— 回 disagree，不挑一個相信。
    """
    def _int(x):
        return x if isinstance(x, int) and not isinstance(x, bool) else None

    c = _int(ctx)
    need = _int(min_ctx_for_extraction)
    p1 = _int(pec_first)
    p2 = _int(pec_second)

    if c is None or need is None:
        return "unknown", (
            "（**沒有主張「有沒有被截斷」**：生效的 num_ctx=%r、不被截斷的下限=%r "
            "—— 兩者都要有才推得出來，缺一個就什麼都不說。"
            "讀不到不等於沒有截斷。）" % (ctx, min_ctx_for_extraction))

    truncated = c < need

    if truncated:
        if p1 is not None and p2 is not None and p1 != p2:
            return "disagree", (
                "（**判準與觀測打架**：num_ctx=%d < 不被截斷的下限 %d，所以兩次"
                "應該都被砍到同一個長度；但 prompt_eval_count 是 %d 與 %d，"
                "不相等。同一組算式不會給兩個答案 —— 要人工看。）"
                % (c, need, p1, p2))
        return "truncated", (
            "—— num_ctx=%d 小於不被截斷的下限 %d，所以兩次都被截斷到同一個"
            "上限（%s），prompt_eval_count 看不出這個差別。"
            % (c, need, "兩次相同" if p1 == p2 else "見上"))

    # `not_truncated` 這一句**不可以引用那句話來說它錯了**。
    # 「原本那句『兩次都被截斷到同一個上限，所以看不出差別』是不成立的」讀起來
    # 像是在更正，但它把假話**原封不動印在輸出裡** —— 而這一行的讀者是用眼睛
    # 掃、用 grep 找的（D-036 教訓 5 就是 grep 被關鍵字騙）。更正留在 docstring
    # 與 DECISIONS.md，輸出只講成立的東西。
    if p1 is not None and p2 is not None:
        detail = "prompt_eval_count = %s 與 %s，差別看得出來" % (p1, p2)
    else:
        detail = "（沒有讀到 prompt_eval_count，所以無從比對）"
    return "not_truncated", (
        "—— **這兩次都沒有被截斷**（num_ctx=%d ≥ 下限 %d）：%s。"
        % (c, need, detail))


def observations(evidence):
    """非致命觀察 —— 印給人看，不影響通過與否。

    時間、token、速率全部放在這裡，因為它們是**這台機器的**數字。
    判準用的是序關係與計數，不是秒數：秒數會隨機器飄移，拿它當判準
    就會變成 D-016 說的那種假失敗。
    """
    notes = []
    if not evidence:
        return notes

    meta = evidence.get("meta") or {}
    calls = evidence.get("calls") or {}
    trunc = evidence.get("truncation") or {}
    canary = evidence.get("canary") or {}
    think = evidence.get("thinking") or {}
    dup = evidence.get("duplicate") or {}
    split = evidence.get("cost_split") or {}
    add1 = evidence.get("first_add") or {}
    add2 = evidence.get("second_add") or {}

    notes.append(
        "條件：模型=%s、嵌入=%s、mem0 的 options=%s、對照 num_ctx=%s"
        % (meta.get("model"), meta.get("embed_model"),
           meta.get("mem0_options"), meta.get("full_ctx"))
    )

    # 冷啟動的結果要出現在觀察段，不能只躺在 evidence 裡。C2／C3／C1
    # 都是在「不指定 num_ctx」的段落量的，而那個段落的意義取決於這件事 ——
    # 前提沒成立時，上面那些數字的意思就變了，讀的人有權知道。
    cold = evidence.get("cold_start") or {}
    if cold:
        notes.append(
            "冷啟動（量之前卸載模型）：%s"
            % "、".join("%s=%s" % (k, v) for k, v in sorted(cold.items()))
        )

    # 呼叫結構：這是「一次」的操作面後果。
    notes.append(
        "呼叫結構（第一次 add）：chat=%s、embed=%s —— 其中 Phase 1 的檢索嵌入"
        "與 Phase 3 的批次嵌入是分開的兩次 embed，不是 LLM。"
        % (calls.get("first_add_chat"), calls.get("first_add_embed"))
    )
    if add1:
        # **生成預算從哪裡來的** —— D-036 第十節：「不要把它寫成另一個魔數」。
        # 所以這一行要讓讀的人自己驗算得出來：推導式（附 num_ctx 與界）、
        # 覆寫、還是 mem0 的預設。預設那一輪沒有別的話要說，不印。
        budget_note = budget_note_to_print(meta)
        if budget_note:
            notes.append("生成預算 num_predict=%s —— %s"
                         % (effective_num_predict(meta), budget_note))
        # 推導的**輸入**與 ollama 實際生效的值是不是同一個。只在不同時印：
        # 不一致就代表「推導用的 num_ctx 不是生效的 num_ctx」，那個預算就
        # 沒有依據（常見成因：改過 .env 但 ollama 沒重啟）。
        # **這是觀察，不是判準** —— 純 `--sections add` 的環境段跑在模型載入
        # **之前**，`/api/ps` 常常是空的，設成判準會製造假失敗。
        ctx_arg = meta.get("ctx_arg")
        live = meta.get("context_length_before")
        if (isinstance(ctx_arg, int) and not isinstance(ctx_arg, bool)
                and isinstance(live, int) and not isinstance(live, bool)
                and ctx_arg != live):
            notes.append(
                "⚠ 推導用的 num_ctx=%s 與 ollama 當下生效的 %s **不一致** —— "
                "這個預算不是用生效的值算出來的。" % (ctx_arg, live))
        # **撞到 num_predict 要當場講。** 這一行是最常被讀的輸出，而
        # `eval_count=2000` 在沒有註記時讀起來像「模型生成完了」。它其實
        # 是「被預算切斷」，也就是說「一次抽取要多久」在這裡是下界
        # （D-027 第七節）。既然這一條的判準都在防「拿被切斷的生成當完整
        # 量測」，輸出本身也不該留一個同樣的陷阱。
        # 判準放在 generation_stop_verdict 裡（可離線測試），這裡只負責印。
        # **不要在這裡另外寫一份「有沒有撞到上限」的判斷** —— 兩份判斷會漂移，
        # 而漂移的那一天，輸出與判準會各說各話（D-035 第六節就是這種病）。
        cap = effective_num_predict(meta)
        stop_verdict, capped = generation_stop_verdict(
            add1.get("eval_count"), cap, add1.get("done_reason"))
        notes.append(
            "第一次 add：llm 生成的 token=%s、prompt token=%s、牆上=%.1f 秒%s"
            % (add1.get("eval_count"), add1.get("prompt_eval_count"),
               add1.get("wall") or 0.0, (" " + capped) if capped else "")
        )
        if stop_verdict == "stopped" and add1.get("done_reason") == "stop":
            # 這一則只在**有觀測**時印：模型自己停下來是這一輪最想知道的事，
            # 而它同時也是「預算假設」的否證條件 —— 值得單獨一行。
            notes.append(
                "**生成沒有被預算切斷**（done_reason=stop）—— 也就是說，"
                "%s 個 token 是模型自己認為講完了，不是我們叫它停的。"
                % add1.get("eval_count")
            )
    if add1 and add1.get("eval_count") and add1.get("eval_duration"):
        notes.append(
            "生成速率（ollama 自己算的，不含載入）：%.2f tok/s"
            % (add1["eval_count"] / add1["eval_duration"])
        )

    # 截斷的實際幅度。
    if trunc.get("dropped") is not None:
        notes.append(
            "截斷幅度：mem0 預設評估 %s 個 token，給足 num_ctx 是 %s 個，"
            "差距 %s 個 token。%s"
            % (trunc.get("default_pec"), trunc.get("full_pec"),
               trunc.get("dropped"), trunc.get("reason"))
        )
    # 要開多大的 num_ctx —— 第四階段直接要用的數字。
    law = evidence.get("ctx_law") or {}
    if law.get("bracket_points"):
        notes.append(
            "截斷門檻（夾縫測試）：%s（量測點：%s）"
            % (law["bracket"].get("reason"),
               "、".join("num_ctx=%s→%s" % (p.get("num_ctx"), p.get("prompt_eval_count"))
                         for p in law["bracket_points"]))
        )
    if law.get("cut_points"):
        notes.append(
            "砍完剩多少：%s"
            % "；".join("num_ctx=%s→%s" % (c.get("num_ctx"), c.get("measured"))
                        for c in law["cut_points"])
        )
    if law.get("min_ctx_for_extraction"):
        notes.append(
            "抽取 prompt 是 %s 個 token → num_ctx 至少 %s 才不會被截斷。"
            "**這仍然是下限**：它只保證這一份 prompt 進得去，"
            "沒有留給後續輪次長大的空間。"
            % (law.get("extraction_prompt_tokens"), law["min_ctx_for_extraction"])
        )
    elif law and law.get("consistent") is not True:
        notes.append(
            "門檻沒夾準（consistent=%s）—— 不給「要開多大」的數字（不猜）。"
            % law.get("consistent")
        )

    if canary.get("head_marker"):
        notes.append(
            "canary：head=%s（看到=%r）、tail=%s（看到=%r）—— %s"
            % (canary.get("head_marker"), canary.get("head_seen"),
               canary.get("tail_marker"), canary.get("tail_seen"),
               canary.get("reason"))
        )

    if think.get("reason"):
        notes.append("thinking：%s" % (think.get("reason"),))

    # 重複寫入。**這一則原本指名的成因是錯的**（D-027 第十節）：它說差別來自
    # 「Phase 1 檢索回來的既有記憶」，但 Phase 1 檢索的是**向量庫**，而這一輪
    # 抽出 0 筆、向量庫是空的。原始碼給的預測是 `## Last k Messages`
    # （mem0 把上一次的訊息寫進 history DB，第二次 add() 再讀回來）—— 但那是
    # **預測，不是這支探針的觀測**，所以它不寫進這一則。
    #
    # 這裡只印量得到的：被攔截下來的 messages 有幾個字元。
    #
    # **「兩次都被截斷到同一個上限」不可以寫死。** 那句話在 num_ctx=4096 的
    # 年代是真的，但 ctx 夠大時 prompt 完整進去，prompt_eval_count 反而**看
    # 得出**差別 —— 同一個字串從真變成假（D-035 第六節發現，D-036 第七節拿到
    # 觀測實例：那句話在同一行裡被它自己的 8,167 反駁）。判準在
    # truncation_claim_verdict()（可離線測試），這裡只負責印。
    if add2:
        grew = ""
        c1, c2 = dup.get("prompt_chars_first"), dup.get("prompt_chars_second")
        d = dup.get("prompt_grew_chars")
        claim_verdict, claim = truncation_claim_verdict(
            (evidence.get("meta") or {}).get("context_length_before"),
            (evidence.get("ctx_law") or {}).get("min_ctx_for_extraction"),
            (add1 or {}).get("prompt_eval_count"),
            add2.get("prompt_eval_count"),
        )
        if c1 is not None and c2 is not None and d is not None:
            grew = ("攔截到的 messages：第一次 %d 字元、第二次 %d 字元（%+d）%s"
                    % (c1, c2, d, (" " + claim) if claim else ""))
        else:
            grew = ("讀不到攔截到的 messages（%r／%r），所以**無從比較**"
                    "兩次的長度。%s" % (c1, c2, claim))
        notes.append(
            "第二次 add（內容相同）：chat=%s 次、prompt token=%s、牆上=%.1f 秒 —— %s"
            % (dup.get("second_add_chat"), add2.get("prompt_eval_count"),
               add2.get("wall") or 0.0, grew)
        )

    # 成本歸屬：序關係，不是門檻。
    if split.get("total"):
        notes.append(
            "add() 的牆上時間歸屬：總計 %.1f 秒，其中 LLM 生成 %.1f 秒（%.0f%%）、"
            "嵌入 %.1f 秒（%.0f%%）。"
            % (split["total"],
               split["timings"].get("llm_eval", 0.0),
               100 * split["shares"].get("llm_eval", 0.0),
               split["timings"].get("embed", 0.0),
               100 * split["shares"].get("embed", 0.0))
        )

    if not notes:
        notes.append("沒有可報告的觀察 —— 探針可能沒有跑到量測那一步")
    return notes


# ─────────────────────────────────────────────────────────
# 以下需要真的連上 ollama —— import 一律延後到這裡
# ─────────────────────────────────────────────────────────


def _install_recorder(memory):
    """把 mem0 的 llm.client.chat 與 embedding_model.client.embed 包起來。

    為什麼要包：mem0 的 `_parse_response()` 只回傳 `message.content`，
    ollama 回報的 token 數與時間全部被丟掉（`llms/ollama.py:43-90`）。
    要拿到那些數字，唯一的辦法是在 mem0 與 ollama 之間插一層。

    記錄的是**同一個 dict**：mem0 組出來的 params 原封不動傳下去，
    所以量到的是 mem0 實際送出的那一份，不是探針自己重組的。
    """
    calls = []

    raw_chat = memory.llm.client.chat

    def chat(**params):
        t0 = time.monotonic()
        resp = raw_chat(**params)
        wall = time.monotonic() - t0
        message = getattr(resp, "message", None)
        calls.append({
            "kind": "chat",
            "params": params,
            "wall": wall,
            "prompt_eval_count": getattr(resp, "prompt_eval_count", None),
            "eval_count": getattr(resp, "eval_count", None),
            "prompt_eval_duration": (getattr(resp, "prompt_eval_duration", 0) or 0) / 1e9,
            "eval_duration": (getattr(resp, "eval_duration", 0) or 0) / 1e9,
            "load_duration": (getattr(resp, "load_duration", 0) or 0) / 1e9,
            "thinking_chars": len(getattr(message, "thinking", None) or ""),
            "content_chars": len(getattr(message, "content", None) or ""),
            "content": getattr(message, "content", None),
            # **ollama 自己說它為什麼停的。** 少了這一格，「生成 2,000 個
            # token」只能靠 `eval_count == num_predict` **推論**是不是撞到
            # 上限；有了它，那是 ollama 的陳述而不是我們的推論，而且兩者
            # 不一致時可以當場看出來（見 generation_stop_verdict）。
            "done_reason": getattr(resp, "done_reason", None),
        })
        return resp

    memory.llm.client.chat = chat

    raw_embed = memory.embedding_model.client.embed

    def embed(**params):
        t0 = time.monotonic()
        resp = raw_embed(**params)
        inp = params.get("input")
        n = len(inp) if isinstance(inp, list) else 1
        calls.append({
            "kind": "embed",
            "wall": time.monotonic() - t0,
            "n_inputs": n,
        })
        return resp

    memory.embedding_model.client.embed = embed
    return calls


def _client(args):
    from ollama import Client
    return Client(host=args.ollama_url)


def _mem0_messages():
    """組出 mem0 在 `_add_to_vector_store()` 會送出的 messages。

    這一段是**重組**，不是攔截 —— 用在需要對照組（明確給足 num_ctx、
    換 canary 位置）的地方，那些呼叫不是 mem0 發的。真正要量 mem0 的
    那次是包住 client 拿到的（見 _install_recorder）。
    """
    from mem0.configs.prompts import (
        ADDITIVE_EXTRACTION_PROMPT,
        generate_additive_extraction_prompt,
    )

    messages = [
        {"role": "system", "content": ADDITIVE_EXTRACTION_PROMPT},
        {
            "role": "user",
            "content": generate_additive_extraction_prompt(
                existing_memories=[],
                new_messages=[
                    {"role": "user", "content": "我剛把 VM 換成 16GB / 4 vCPU。"},
                    {"role": "assistant", "content": "了解。"},
                ],
                last_k_messages=[],
                custom_instructions=None,
            ),
        },
    ]
    # `llms/ollama.py:120-127`：format=json 時 mem0 會在最後一則使用者訊息
    # 後面追加這句。少了它，量到的就不是 mem0 送出的那一份。
    messages[-1]["content"] += "\n\nPlease respond with valid JSON only."
    return messages


def _chat(client, model, messages, options, fmt="json", think=None):
    """送一次 chat，回傳統一的 dict。"""
    kwargs = {"model": model, "messages": messages, "options": options}
    if fmt:
        kwargs["format"] = fmt
    if think is not None:
        kwargs["think"] = think
    t0 = time.monotonic()
    resp = client.chat(**kwargs)
    wall = time.monotonic() - t0
    message = getattr(resp, "message", None)
    return {
        "wall": wall,
        "prompt_eval_count": getattr(resp, "prompt_eval_count", None),
        "eval_count": getattr(resp, "eval_count", None),
        "prompt_eval_duration": (getattr(resp, "prompt_eval_duration", 0) or 0) / 1e9,
        "eval_duration": (getattr(resp, "eval_duration", 0) or 0) / 1e9,
        "load_duration": (getattr(resp, "load_duration", 0) or 0) / 1e9,
        "thinking_chars": len(getattr(message, "thinking", None) or ""),
        "content": getattr(message, "content", None),
    }


def _ps_lookup(client, model):
    """(讀得到 /api/ps 嗎, 那個模型當下生效的 context_length)。

    兩件事要分開回報。`_ps_context_length()` 讀不到時回 None，但
    「模型沒載入」與「/api/ps 讀不到」**都會**走到那個 None —— 前者是觀察
    結果，後者是儀器失效。在「卸載成功了嗎」這種前提檢查上，把兩者混成
    同一個 None 會製造假通過：儀器壞掉被讀成「已卸載」。
    """
    try:
        models = client.ps().models
    except Exception:
        return False, None
    for m in models:
        if getattr(m, "model", None) == model:
            return True, getattr(m, "context_length", None)
    return True, None


def _ps_context_length(client, model):
    """讀 /api/ps 回報的 context_length —— 這是**當下生效的** num_ctx。

    不是模型的最大 context，是這次載入實際配置的。mem0 不送 num_ctx，
    所以這裡看到的就是 ollama 的預設值。讀不到就回 None（不當成失敗）。
    需要區分「讀不到」與「沒載入」時用 _ps_lookup()。
    """
    return _ps_lookup(client, model)[1]


def _unload(client, model):
    """把模型卸載，讓下一次**不指定 num_ctx** 的請求從預設值重新載入。

    為什麼要管這件事：截斷的上限只看 `num_ctx`（見 truncated_length()），
    所以「不指定 num_ctx 時拿得到多大的上限」取決於**當下載入的是哪一個
    context**。C2 的對照組會故意載入 16384；在那個 context 下，mem0 那份
    8047 個 token 的抽取 prompt 一個 token 都不會被丟掉 —— C2 會回報
    「沒有截斷」，那是假通過。

    **實測（2026-09-20，ollama/ollama:latest）：不會漏。** 伺服器日誌顯示，
    context 載成 16384 之後，下一個**不帶 num_ctx** 的請求讓 llama-server
    重新初始化成 4096：

        load_model: initializing, n_ctx_slot = 16384   ← num_ctx=16384 那次
        load_model: initializing, n_ctx_slot = 4096    ← 緊接著不帶 num_ctx
        slot operator(): new prompt, n_ctx_slot = 4096, task.n_tokens = 3148

    那**為什麼還要卸載**？因為「不會漏」是這台機器這個版本的行為，不是
    規格；而這一節要的不是「運氣好」，是「前提成立而且被記下來」。卸載
    之後，`/api/ps` 讀到的就一定等於接下來那次請求用的，這個等號才是
    量測的立足點。它同時把「前一個 section 留下什麼」這個變數整個拿掉 ——
    沒有它，C2／C3 的結論會隨執行順序改變（D-014）。

    卸載用 `/api/generate` 只帶 model 與 keep_alive=0。**不能**用 /api/chat
    送一句話來卸載：推理模型會開始生成一整段 thinking，而那個請求沒有
    num_predict，於是整支探針卡在那裡（實測踩過）。

    回傳 True / False / None：
      True  —— 確認卸載了（/api/ps 讀得到、而且它不在裡面）
      False —— 送了卸載請求，但它還在（或請求本身失敗）
      None  —— 讀不到 /api/ps，**無法確認**；這不是「已卸載」。
    """
    try:
        client.generate(model=model, keep_alive=0)
    except Exception:
        return False
    readable, ctx = _ps_lookup(client, model)
    if not readable:
        return None
    return ctx is None


def _require_cold_start(client, model, evidence, section):
    """量一個「不指定 num_ctx」的段落之前，先把模型卸載並記下結果。

    回傳卸載的確認狀態（True / False / None），並寫進 evidence ——
    **這是前提，不是裝飾**：沒有它，「A 是在預設 context 下量的」只是一句
    沒人驗證過的話，而它錯了會讓 C2 給出相反的結論（假通過）。
    `_unload()` 的說明裡記了實測結果（這一版的 ollama 會重新載入，不沿用）
    —— 即使如此，被檢查過的前提與被假設的前提在證據上仍然不一樣。

    狀態是三值的，而且三種都要分開講：None 是「讀不到 /api/ps」，
    不是「卸載失敗」，更不是「已卸載」。把它讀成後者，就是儀器壞掉被
    當成量測成立 —— 那正是這一支要防的事。
    """
    state = _unload(client, model)
    (evidence.setdefault("cold_start", {}))[section] = state
    if state is False:
        print("     ⚠ 卸載失敗 —— 模型還在，這一節可能是在前一段留下的 context 下量的",
              flush=True)
    elif state is None:
        print("     ? 讀不到 /api/ps，無法確認這一節是不是從預設 context 開始",
              flush=True)
    return state


def _canary_messages(target_chars, head_first=True):
    """組一份夠長、只有 canary 位置不同的 prompt。

    **指令必須放在最後。** 這是這個設計的關鍵：如果指令寫在開頭，而截斷正好
    砍掉開頭，那模型連「請回報 marker」都看不到 —— 量到的會是「模型不照做」，
    不是「marker 被砍掉」。把指令放進使用者訊息（永遠在最後、截斷砍不到），
    模型才有能力回報它看到什麼。這也正好對上 mem0 的實況：系統提示詞在前、
    使用者訊息在後。

    兩次呼叫的填充長度相同，只有 marker 的位置不同 —— 所以「一端看得到、
    另一端看不到」只能是位置造成的，不會是長度效應。

    **頭端 marker 必須比 `numKeep` 長。** 截斷保留的是「前 `numKeep` 個
    token 加尾段」，`numKeep` 預設 4（D-023 第五節、`llama_server.go`）——
    也就是說**頭端的前 4 個 token 其實留著**。所以放在最前面的 marker 會
    被留下一個碎片。判準是「模型有沒有回報**完整**的 marker 字串」
    （`HEADMARK` + 12 個隨機字元，20 個字元遠超 4 個 token），碎片湊不出
    完整字串，所以 `head_seen` 仍然正確地是 False —— **但這是設計依賴的
    條件，不是巧合**：如果 marker 短到能塞進 4 個 token，這一節會回報
    「頭端看得到」，而那是假的。12 個隨機字元同時擋掉另一件事：模型猜不到。
    """
    alphabet = string.ascii_uppercase + string.digits
    head = "HEADMARK" + "".join(random.choice(alphabet) for _ in range(12))
    tail = "TAILMARK" + "".join(random.choice(alphabet) for _ in range(12))

    unit = " the quick brown fox jumps over the lazy dog."
    filler = unit * max(1, target_chars // len(unit))

    body = f"{head}\n{filler}\n{tail}" if head_first else f"{tail}\n{filler}\n{head}"

    user = (
        "The message above is one long block of text. It may contain one or two "
        "marker strings: they start with HEADMARK or TAILMARK and are followed by "
        "12 random characters. List EXACTLY the marker strings you can actually "
        "see, and nothing else. "
        'Reply with JSON only: {"markers": ["..."]}'
    )
    return [
        {"role": "system", "content": body},
        {"role": "user", "content": user},
    ], head, tail


def measure_ctx_law(evidence, args):
    """C2b：門檻在哪、砍完剩多少 —— 那 num_ctx 要開多大才不截斷？

    觀測量還是 `prompt_eval_count`：**被截斷時它就等於砍完的長度**（實測
    num_ctx=4096 → 2050，ollama 日誌同一件事寫成 limit=2050）。所以不必
    讀日誌，也不必猜 tokenizer 怎麼切。

    兩件事分開量，因為它們是**兩種不同形狀的敘述**，要用不同的方式驗：

      (a) 門檻：`len(tokens) ≧ num_ctx` 就砍。這是一個比較、不是斜率，
          所以用一份**小 prompt** 把門檻夾在 1 個 token 之內 —— 先量出
          P，再用 num_ctx=P 與 num_ctx=P+1 各送一次。判讀見
          threshold_bracket_verdict()。
      (b) 砍完剩多少：`truncated_length(num_ctx)` 這個閉式解。拿兩個
          num_ctx 去**核對**它，而不是拿這兩個點去擬合出一條線 ——
          擬合出來的線永遠會通過它自己的兩個點。

    **num_predict 一律用 mem0 真正的值（2000），不壓小。** 這裡是整份
    探針最容易被「省時間」害到的地方：壓小 num_predict 之後數字還是很
    漂亮，但「這是在 mem0 的條件下量到的」那句話就變成假的，而結論會被
    拿去套在 num_predict=2000 上。舊版正是壓成 16 去量，然後把結論外推
    到 mem0 的設定 —— 那是一個跨條件的推論（D-014），而它剛好是錯的。
    成本其實不高：`format=json` 加一句短問題，實測每次只生成兩百多個
    token，不是 2000。

    三件事會讓這一節量出假的關係，都寫在這裡：
      · prompt 太短 → 沒有截斷 → 量到的是 prompt 長度，不是上限
      · num_ctx 沒生效 → 下一次呼叫會沿用前一次的 KV cache → 兩個點同值
      · 前一次呼叫的 prompt 還在 → ollama 會沿用快取，數字偏大
    所以每一輪都重新確認 num_ctx 生效（control_group_effect）。
    """
    client = _client(args)
    unit = " the quick brown fox jumps over the lazy dog."

    def _ask(messages, ctx, note):
        """送一次、印一行、回報；(回應, /api/ps 讀到的 num_ctx, 有沒有生效)。"""
        r = _chat(client, args.model, messages,
                  dict(MEM0_LLM_OPTIONS, num_ctx=ctx))
        after = _ps_context_length(client, args.model)
        took = control_group_effect(after, ctx)
        flag = {True: "", False: "  ✗ num_ctx 沒生效", None: "  ? 讀不到"}[took]
        print("     %-14s num_ctx=%-6d → prompt_eval_count=%-6s（%.0f 秒）%s"
              % (note, ctx, r["prompt_eval_count"], r["wall"], flag), flush=True)
        return r, after, took

    def _filler(chars):
        return unit * max(1, chars // len(unit))

    # ── (a) 夾縫測試 ────────────────────────────────────
    small = [
        {"role": "system", "content": "HEAD\n" + _filler(BRACKET_PROMPT_CHARS)},
        {"role": "user", "content": 'Reply with JSON only: {"ok": true}'},
    ]
    print("  夾縫測試：小 prompt %d 字元，用 num_ctx=P 與 P+1 各送一次"
          % sum(len(m["content"]) for m in small), flush=True)

    base, _, _ = _ask(small, BRACKET_PROMPT_CTX, "量 P（寬鬆）")
    p_tokens = base["prompt_eval_count"]
    bracket_points = [{"num_ctx": BRACKET_PROMPT_CTX, "prompt_eval_count": p_tokens,
                       "role": "measure-P"}]

    # 兩種情況會讓 P 不是 P：沒回報數字，或第一次自己就被砍了。
    # 後者比對得出來 —— 被砍的話量到的會剛好是閉式解的預測值。
    if not p_tokens:
        bracket = {
            "prompt_tokens": None, "expected_cut": None, "consistent": None,
            "reason": "第一次沒有回報 prompt_eval_count —— 量不出 P，夾縫測試無法進行。",
        }
    elif p_tokens >= truncated_length(BRACKET_PROMPT_CTX):
        bracket = {
            "prompt_tokens": p_tokens, "expected_cut": None, "consistent": None,
            "reason": "小 prompt 在 num_ctx=%d 下自己就被砍了（量到 %d）—— P 不是"
            "真的 P，夾縫測試的兩端會跟著錯。把 BRACKET_PROMPT_CTX 調大。"
            % (BRACKET_PROMPT_CTX, p_tokens),
        }
    else:
        at_r, at_ctx, at_took = _ask(small, p_tokens, "num_ctx = P")
        above_r, above_ctx, above_took = _ask(small, p_tokens + 1, "num_ctx = P+1")
        bracket_points += [
            {"num_ctx": p_tokens, "prompt_eval_count": at_r["prompt_eval_count"],
             "ctx_after": at_ctx, "took_effect": at_took, "role": "at-P"},
            {"num_ctx": p_tokens + 1, "prompt_eval_count": above_r["prompt_eval_count"],
             "ctx_after": above_ctx, "took_effect": above_took, "role": "above-P"},
        ]
        # num_ctx 沒生效的那一次不能當成「量到 num_ctx=P 的結果」—— 它量的是
        # 別的 context。傳 None 進判讀函式（＝讀不到），讓它回「無法判定」
        # 而不是回「門檻不是 num_ctx」：前者要人去看，後者會指向錯的方向。
        #
        # 這一格不是理論上的顧慮：夾縫只有 1 個 token，ollama 若把 num_ctx
        # 向上取整到某個最小值（或夾到下限），P 與 P+1 會變成同一個 context，
        # 兩次都不會被砍 —— 判讀會說「門檻不是 num_ctx」，而真正發生的事情
        # 是「我們要求的 num_ctx 沒有被照做」。`took is None`（讀不到 /api/ps）
        # **不算**沒生效，數字照用，理由同 control_group_effect() 的說明。
        bracket = threshold_bracket_verdict(
            p_tokens,
            at_r["prompt_eval_count"] if at_took is not False else None,
            above_r["prompt_eval_count"] if above_took is not False else None)

    # ── (b) 砍完剩多少（核對閉式解，不是擬合）────────────
    # 這份 prompt 要**遠大於**每一個量測點的 num_ctx，否則量到的是「沒截斷」，
    # 那就不是上限。30000 字元 ≈ 7.1k token，比 4096 大得多。
    long_messages = [
        {"role": "system", "content": "HEAD\n" + _filler(CTX_LAW_PROMPT_CHARS)},
        {"role": "user", "content": 'Reply with JSON only: {"ok": true}'},
    ]
    print("  砍完剩多少：長 prompt %d 字元，核對 truncated_length(num_ctx)"
          % sum(len(m["content"]) for m in long_messages), flush=True)

    cuts = []
    for ctx in CUT_FORMULA_CTX:
        r, after, took = _ask(long_messages, ctx, "核對閉式解")
        # 同上：num_ctx 沒生效的那一次核對的是別的 context，不能算數。
        v = truncation_limit_verdict(
            ctx, r["prompt_eval_count"] if took is not False else None)
        v["num_ctx"] = ctx
        v["ctx_after"] = after
        v["took_effect"] = took
        cuts.append(v)
        print("       %s" % v["reason"], flush=True)

    law = {
        "bracket": bracket,
        "bracket_points": bracket_points,
        "bracket_prompt_chars": sum(len(m["content"]) for m in small),
        "cut_points": cuts,
        "cut_prompt_chars": sum(len(m["content"]) for m in long_messages),
        "num_predict": MEM0_LLM_OPTIONS["num_predict"],
    }
    law["consistent"] = (
        True if bracket.get("consistent") is True and all(c.get("matches") is True for c in cuts)
        else (False if bracket.get("consistent") is False
              or any(c.get("matches") is False for c in cuts)
              else None)
    )
    evidence["ctx_law"] = law
    print("  → %s" % bracket["reason"], flush=True)

    # ── 給第四階段的那個數字 ─────────────────────────────
    # prompt 的 token 數用 C2 的對照組量到的值 —— 那是**真的那份抽取
    # prompt**，不是這一節的填充。門檻沒夾準就不給這個數字：`+1` 這一步
    # 的正當性完全來自那個夾縫，沒有它，「8048」只是另一個看起來很精確
    # 的猜測。
    full_pec = (evidence.get("truncation") or {}).get("full_pec")
    if full_pec:
        law["extraction_prompt_tokens"] = full_pec
        if law["consistent"] is True:
            need = min_ctx_required(full_pec)
            law["min_ctx_for_extraction"] = need
            print("     抽取 prompt 是 %s 個 token → num_ctx 至少開到 %d。"
                  % (full_pec, need), flush=True)
        else:
            law["min_ctx_for_extraction"] = None
            print("     ? 門檻沒夾準（consistent=%s）—— 不給「要開多大」的數字，"
                  "不猜。已知的是 C2 量到的兩端：%s 個 token 在預設的 num_ctx 下"
                  "進不去、在 %d 下進得去，門檻落在中間某處。"
                  % (law["consistent"], full_pec, FULL_CTX), flush=True)


def probe_revision():
    """這支探針**自己的**修訂版指紋。

    為什麼需要：D-027 的 run2 跑完之後，探針為了修 C3 的儀器缺陷又被改過，
    於是「那組數字是哪一版跑出來的」變成一個只能靠記憶回答的問題 —— 這個
    檔案當時還沒進 git（新檔），也沒留下舊版，最後是靠容器外一個殘留的
    `.pyc` 做 bytecode 比對，才把歸屬釘住。

    **一次執行如果講不出自己是誰跑的，它的數字就沒有歸屬。** 這裡記三個
    唯讀的事實（雜湊、大小、mtime）—— 它**不參與 grade()，也不影響任何
    判準**，只是讓下一個看到這些數字的人不必再做一次鑑識。

    它是在行程啟動**之後**才讀檔的，所以中途改檔案並不會改變已記下的值
    （python 在啟動時就把原始碼讀進記憶體了）—— 除非改動正好落在啟動與
    這一行之間那幾毫秒，而那不是這支工具設計要防的事。
    """
    import hashlib
    try:
        p = Path(__file__).resolve()
        data = p.read_bytes()
        st = p.stat()
    except OSError as e:
        return {"error": "讀不到自己的原始碼（%s）" % e}
    return {
        "sha256_16": hashlib.sha256(data).hexdigest()[:16],
        "bytes": len(data),
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
    }


def measure_environment(evidence, args):
    """讀環境：模型在不在、當下生效的 num_ctx 是多少、**這支探針是哪一版**。"""
    client = _client(args)
    meta = evidence.setdefault("meta", {})
    meta["model"] = args.model
    meta["embed_model"] = args.embed_model
    meta["mem0_options"] = dict(MEM0_LLM_OPTIONS)
    meta["full_ctx"] = FULL_CTX
    # 放在連線檢查**之前**：連不上 ollama 的那一輪，也該留下「是誰跑的」。
    meta["probe"] = probe_revision()
    if args.add_max_tokens:
        meta["add_max_tokens_overridden"] = args.add_max_tokens

    # **生成上限只解析這一次**，三個使用點（config、輸出、判準）都讀 meta。
    # 放在連線檢查之前，理由與 probe 修訂版相同：連不上的那一輪也該留下
    # 「這一輪本來要用什麼預算跑」。細節與優先序見 budget_resolution()。
    effective, source, why = budget_resolution(args.add_max_tokens, args.ctx)
    meta["budget_source"] = source
    meta["budget_note"] = why
    meta["ctx_arg"] = args.ctx
    if effective is not None:
        meta["add_max_tokens_effective"] = effective

    try:
        names = [m.get("model") or m.get("name") for m in client.list().get("models", [])]
    except Exception as e:
        evidence["fatal"] = "連不上 ollama（%s）—— 這是環境問題，不是判準的問題" % e
        return False

    for want in (args.model, args.embed_model):
        if not any(n == want or (n or "").split(":")[0] == want.split(":")[0] for n in names):
            evidence["fatal"] = "ollama 裡沒有 %s —— 先 pull 再跑" % want
            return False

    meta["context_length_before"] = _ps_context_length(client, args.model)
    return True


def measure_truncation(evidence, args):
    """C2：同一份 messages，mem0 的 options vs 明確給足 num_ctx。

    **A 用的是 mem0 真正的 options，一個字都不改**（先前的版本把 num_predict
    壓成 16 以省時間，那量到的就不是 mem0 遇到的東西），而且先卸載模型讓它
    從預設 context 開始。兩件事都跟截斷的條件有關 —— 條件是
    `len(tokens) ≧ num_ctx`（見 truncated_length()）：

    少了卸載，A 會在「前一次留下的 context」下量；少了真實的 num_predict，
    A 送出的 options 就不是 mem0 送出的那一份。兩種都會讓 C2 的結論與 mem0
    的實況不符（D-014：在甲條件下量到的，不可以套到乙條件）。

    附帶一提：num_predict 其實**不影響**截斷（它是生成端的參數），所以壓小
    它不會改變上限 —— 但那是現在讀了原始碼才知道的事，而當時正是「壓小它
    應該會改變上限」這個假設讓錯誤的公式通過了一次沒有分辨力的實驗。
    條件對齊不是為了那個公式，是為了不必再依賴任何關於它的假設。
    """
    client = _client(args)
    messages = _mem0_messages()
    opts = dict(MEM0_LLM_OPTIONS)
    total_chars = sum(len(m["content"]) for m in messages)
    print("  重組的 messages：%d 字元（system=%d、user=%d）"
          % (total_chars, len(messages[0]["content"]), len(messages[1]["content"])),
          flush=True)

    # A 必須從**預設 context** 開始 —— 見 _require_cold_start() 的說明。
    _require_cold_start(client, args.model, evidence, "C2/A")
    print("  A：mem0 的 options（不含 num_ctx），options=%s …" % opts, flush=True)
    a = _chat(client, args.model, messages, opts)
    ctx_after_a = _ps_context_length(client, args.model)
    print("     prompt_eval_count=%s（%.1f 秒）、之後的 num_ctx=%s"
          % (a["prompt_eval_count"], a["wall"], ctx_after_a), flush=True)

    # B 的 num_predict 與 A 不同 —— 理由寫在 B_CROSSCHECK_NUM_PREDICT 的
    # 常數說明裡。印出來的 options 要老實寫，不能標成「mem0 的 options」。
    b_opts = dict(opts, num_ctx=FULL_CTX, num_predict=B_CROSSCHECK_NUM_PREDICT)
    print("  B：同一份 messages、num_ctx=%d（會觸發重新載入）、"
          "num_predict 壓成 %d（B 量的是 prompt eval 的結果，生成幾個 token "
          "不影響它 —— 見常數說明）…"
          % (FULL_CTX, B_CROSSCHECK_NUM_PREDICT), flush=True)
    b = _chat(client, args.model, messages, b_opts)
    ctx_after_b = _ps_context_length(client, args.model)
    print("     prompt_eval_count=%s（%.1f 秒）、之後的 num_ctx=%s"
          % (b["prompt_eval_count"], b["wall"], ctx_after_b), flush=True)

    v = truncation_verdict(a["prompt_eval_count"], b["prompt_eval_count"])
    v["default_pec"] = a["prompt_eval_count"]
    v["full_pec"] = b["prompt_eval_count"]
    v["messages_chars"] = total_chars
    v["default_options"] = opts
    v["full_options"] = b_opts
    # 兩次呼叫**之後**各讀一次 /api/ps。這裡有兩件事要分開看。
    v["context_length_after_default"] = ctx_after_a
    v["context_length_after_full"] = ctx_after_b
    baseline_ctx = (evidence.get("meta") or {}).get("context_length_before")
    print("  → %s" % v["reason"], flush=True)

    # (1) A 之後的 num_ctx 是否偏離進來的基準。偏離了代表 ollama 是**為了
    #     塞進 prompt 而放大 context** —— 那也是一種「沒截斷」，但成因與
    #     「預設值本來就夠大」不同，結論要分清楚。
    #     比對對象是 **A 之前的基準**，不是 B：B 故意送不同的 num_ctx，
    #     拿 B 當基準的話這個警告每次都會響，而每次都響的警告等於沒有警告。
    if ctx_after_a and baseline_ctx and ctx_after_a != baseline_ctx:
        print("     ⚠ A 之後的 num_ctx 由 %s 變成 %s —— ollama 可能是為了塞進"
              " prompt 而放大 context，判讀時要把成因寫清楚"
              % (baseline_ctx, ctx_after_a), flush=True)

    # (2) B 的 num_ctx 有沒有真的生效。判斷本身在 control_group_effect()
    #     （純函式、離線測試覆蓋得到），這裡只負責把結果印出來。
    #     這一條**必須進判準**：若 B 其實也跑在預設值，兩次都會被截到同一個
    #     長度，a == b，truncation_verdict 會回「沒有截斷」—— 結論正好相反。
    effect = control_group_effect(ctx_after_b, FULL_CTX)
    v["full_ctx_took_effect"] = effect
    if effect is None:
        print("     ? 讀不到 B 之後的 num_ctx —— 無法確認對照組是否成立"
              "（讀不到不等於沒生效）", flush=True)
    elif effect is False:
        print("     ✗ B 要求的 num_ctx=%d 沒有生效（/api/ps 說 %s）——"
              " 對照組不成立，這一節的比較不成立" % (FULL_CTX, ctx_after_b), flush=True)

    evidence["truncation"] = v


def measure_canary(evidence, args):
    """C3：截斷是從哪一端吃掉的。

    兩次呼叫的 prompt 長度完全相同，只有 marker 的位置不同。
    用 FULL_CTX 來跑並不合適 —— 那就沒有截斷了。這裡刻意用 **mem0 的
    options、不給 num_ctx**，讓截斷在同樣的條件下發生。

    **而且要先卸載。** 這一節排在 C2 後面，而 C2 的對照組剛把 context 載成
    16384 —— 那時的上限是 16384/2 + 2 = 8194，跟這裡的 prompt 長度是
    **同一個數量級**，會不會截斷會取決於 tokenizer 怎麼切，而不是取決於
    mem0 的行為。C3 的結論就會隨執行順序改變（D-014）。

    更嚴重的是 C2 的 A：mem0 真正的抽取 prompt 是 8047 個 token，在 8194
    之下**一個都不會被丟掉** —— C2 會回報「沒有截斷」，而那是假通過。
    （這一段原本寫的數字是照錯誤公式算的 14338，結論一樣、方向一樣，
    連「會假通過」都是同一個 —— 但錯的數字會讓下一個查的人對不上日誌。）
    兩個地方都靠卸載才站得住。

    **生成端要讓模型講得完（think=False）。** run2 的兩次呼叫都在
    num_predict=2000 被切斷，於是「模型沒講完」被讀成了「兩端都看不到」。
    C3 問的是 prompt eval 端的方向，回報則是生成端的事 —— 讓生成端可靠
    不會動到主張（截斷只看 num_ctx，見 CANARY_THINK 的說明）。
    而「有沒有講完」由 canary_answer_usable() 事後檢查，不靠假設。
    """
    client = _client(args)
    opts = dict(MEM0_LLM_OPTIONS)
    _require_cold_start(client, args.model, evidence, "C3/canary")

    # 填充量要明顯大於預設 context 下那個 2050 的上限，才能真的截到；
    # 但也不能大到讓 tokenizer 白做工。判準是「兩端可見性是否不同」，
    # 不是「剛好截掉幾個 token」，所以只要夠長就好。
    filler = 60000
    messages, head, tail = _canary_messages(filler, head_first=True)
    print("  canary prompt：%d 字元（head 在前），options=%s、think=%s …"
          % (sum(len(m["content"]) for m in messages), opts, CANARY_THINK), flush=True)
    chat_head_first = _chat(client, args.model, messages, opts, think=CANARY_THINK)

    messages2, head2, tail2 = _canary_messages(filler, head_first=False)
    print("  canary prompt：%d 字元（tail 在前）…"
          % sum(len(m["content"]) for m in messages2), flush=True)
    chat_tail_first = _chat(client, args.model, messages2, opts, think=CANARY_THINK)

    # head_first=True 時 head 在開頭；head_first=False 時 head 在結尾。
    # 兩種排列都跑，是為了排除「模型只是剛好漏看某個字串」。
    #
    # eval_count 與 thinking 字元**一起印出來**：run2 就是少了這一格，
    # 才讓「模型沒講完」與「模型看不到」在輸出上長得一模一樣。
    r1 = canary_verdict(chat_head_first, head, tail, opts["num_predict"],
                        head_first=True)
    r2 = canary_verdict(chat_tail_first, head2, tail2, opts["num_predict"],
                        head_first=False)
    for label, chat, r in (("head 在前", chat_head_first, r1),
                           ("tail 在前", chat_tail_first, r2)):
        print("  %s：prompt_eval_count=%s、eval_count=%s（上限 %d、"
              "thinking=%d 字元）→ %s"
              % (label, chat["prompt_eval_count"], chat["eval_count"],
                 opts["num_predict"], chat["thinking_chars"], r["reason"]),
              flush=True)

    # 判準用第一次（head 在開頭、tail 在結尾）—— 那是最貼近 mem0 實況的
    # 排列：系統提示詞（指令）在前、使用者訊息在後。
    evidence["canary"] = dict(r1)
    evidence["canary"]["head_marker"] = head
    evidence["canary"]["tail_marker"] = tail
    evidence["canary"]["reversed"] = r2
    evidence["canary"]["prompt_chars"] = sum(len(m["content"]) for m in messages)
    evidence["canary"]["eval_count"] = chat_head_first["eval_count"]
    evidence["canary"]["thinking_chars"] = chat_head_first["thinking_chars"]
    evidence["canary"]["num_predict"] = opts["num_predict"]
    evidence["canary"]["think"] = CANARY_THINK


def measure_thinking(evidence, args):
    """C4：mem0 的 options 下 thinking 是否存在、是否計入 eval_count。

    這一節把 num_predict 壓成 THINKING_NUM_PREDICT —— **與 C2／C3 不同**，
    但這裡是對的：C4 量的是**生成段**（thinking 算不算進 eval_count），
    num_predict 是生成端唯一會影響它的參數。它不影響截斷（截斷只看
    num_ctx，見 truncated_length()），所以這裡壓小它不會動到別的東西 ——
    但印出來的 options 還是要老實寫，不能標成「mem0 的 options」了事。

    也不需要冷啟動：C4 的結論不依賴當下是哪一個 context（prompt 被截到
    多長，generation 的計數都一樣），而它前面的 C3 已經卸載過，
    所以這裡本來就跑在預設值下。
    """
    client = _client(args)
    messages = _mem0_messages()
    opts = dict(MEM0_LLM_OPTIONS, num_predict=THINKING_NUM_PREDICT)

    print("  mem0 的 options，但 num_predict 壓成 %d（只為了縮短生成；"
          "C4 量的是生成段，不是上限）…" % THINKING_NUM_PREDICT, flush=True)
    on = _chat(client, args.model, messages, opts)
    print("     eval_count=%s、thinking=%d 字元（%.1f 秒）"
          % (on["eval_count"], on["thinking_chars"], on["wall"]), flush=True)

    print("  同樣的呼叫，think=False …", flush=True)
    try:
        off = _chat(client, args.model, messages, opts, think=False)
        print("     eval_count=%s、thinking=%d 字元（%.1f 秒）"
              % (off["eval_count"], off["thinking_chars"], off["wall"]), flush=True)
    except Exception as e:
        # 不支援 think 的模型會在這裡失敗 —— 那不算判準沒過。
        print("     think=False 不受支援（%s）—— 這一條只能部分判定" % e, flush=True)
        off = {"eval_count": None, "thinking_chars": None}

    # num_predict 一定要傳進去：少了它，一個撞到上限的 eval_count 會被當成
    # 「模型自己停在那裡」，差值就被講成真值。實測就是這樣 —— 「開」那一側
    # 的 eval_count 正好是 256＝THINKING_NUM_PREDICT（D-027）。
    v = thinking_verdict(on["thinking_chars"], on["eval_count"],
                         off["thinking_chars"], off["eval_count"],
                         num_predict_on=opts["num_predict"],
                         num_predict_off=opts["num_predict"])
    v["eval_count_on"] = on["eval_count"]
    v["eval_count_off"] = off["eval_count"]
    v["num_predict"] = opts["num_predict"]
    v["thinking_chars_on"] = on["thinking_chars"]
    v["content_chars_on"] = len(on["content"] or "")
    evidence["thinking"] = v
    print("  → %s" % v["reason"], flush=True)


def measure_add(evidence, args, workdir):
    """C1／C5：真的跑兩次 add()，數呼叫、量成本。

    這裡**不重組 prompt** —— mem0 自己組、自己送，探針只在
    llm.client.chat 外面記一筆。所以量到的是實況。

    mem0 不送 num_ctx，所以它拿到的是 ollama 當下**預設**的 context。
    這一節排在 C2／C3 後面，那兩節剛把 context 載成 16384／4096 ——
    沒有先卸載的話，量到的會是「上一個請求留下的 context」下的 add()，
    而 D-027 的結論正是關於這個預設值。所以要卸載，並且把卸載的結果
    記進 evidence（卸載不掉時要大聲說，不能安靜地量）。

    注意 `--add-max-tokens` 會走 config 的 max_tokens、也就是 mem0 送出
    真正的 num_predict —— 它改變的是生成段，不是截斷上限（後者只看
    num_ctx）。但預設（不給這個參數）仍然是 mem0 的實況，成本數字要跟
    D-027 比就只能用預設的那一輪。

    **`--ctx` 走同一條 config 路徑，但值是用 `extraction_budget()` 從
    num_ctx 推導出來的**（D-037）：`max_tokens = num_ctx − prompt 上界`，
    所以它「大到模型自己停」而不會大到觸發 context shift。兩個前提各有一
    條判準盯著（C6：界還成立、C7：預算真的夠 —— 見 `grade()`），而那兩條
    **只在預算是推導來的時候成立**，覆寫是受控實驗，不判。
    """
    from mem0 import Memory

    # 真的 add() 之前先冷啟動 —— 見上面的說明。
    _require_cold_start(_client(args), args.model, evidence, "C1/add")

    cfg = {
        "llm": {"provider": "ollama", "config": {
            "model": args.model, "ollama_base_url": args.ollama_url}},
        "embedder": {"provider": "ollama", "config": {
            "model": args.embed_model, "ollama_base_url": args.ollama_url}},
        "vector_store": {"provider": "chroma", "config": {
            "collection_name": "item3_probe",
            "path": str(Path(workdir) / "chroma")}},
        "history_db_path": str(Path(workdir) / "history.db"),
    }
    # 寫進 config 的只有「明確指定」的那兩種（--add-max-tokens 覆寫、或
    # --ctx 推導）；`mem0_default` **不寫**，讓 mem0 用它自己的值 —— 那是
    # 裸跑探針時的承諾（量 mem0 的實況）。判斷讀的是 meta 而不是 args：
    # 解析只在 measure_environment 做一次，這裡跟著同一個來源，就不會出現
    # 「config 寫了 8,192、輸出卻印『mem0 預設，沒有覆寫』」。
    effective = (evidence.get("meta") or {}).get("add_max_tokens_effective")
    if effective:
        cfg["llm"]["config"]["max_tokens"] = effective

    memory = Memory.from_config(cfg)
    calls = _install_recorder(memory)

    conversation = [
        {"role": "user", "content": "我剛把 VM 換成 16GB / 4 vCPU，之前只有 3.8GB。"},
        {"role": "assistant", "content": "了解，那接下來跑 8b 模型應該沒問題。"},
    ]

    print("  第一次 add()（infer=True，mem0 的預設）…", flush=True)
    t0 = time.monotonic()
    first = memory.add(conversation, user_id="item3")
    wall1 = time.monotonic() - t0
    n_chat1 = sum(1 for c in calls if c["kind"] == "chat")
    n_embed1 = sum(1 for c in calls if c["kind"] == "embed")
    print("     → 抽出 %d 則記憶；chat=%d、embed=%d、牆上 %.1f 秒"
          % (memories_written(first), n_chat1, n_embed1, wall1), flush=True)

    chat1 = [c for c in calls if c["kind"] == "chat"]
    embed1 = [c for c in calls if c["kind"] == "embed"]
    a1 = chat1[-1] if chat1 else {}
    evidence["first_add"] = {
        "memories": memories_written(first),
        "wall": wall1,
        "eval_count": a1.get("eval_count"),
        "prompt_eval_count": a1.get("prompt_eval_count"),
        "eval_duration": a1.get("eval_duration"),
        "prompt_eval_duration": a1.get("prompt_eval_duration"),
        "load_duration": a1.get("load_duration"),
        "thinking_chars": a1.get("thinking_chars"),
        "content_chars": a1.get("content_chars"),
        "done_reason": a1.get("done_reason"),
    }
    evidence["calls"] = {
        "first_add_chat": n_chat1,
        "first_add_embed": n_embed1,
    }
    # 牆上時間歸屬。llm_eval 用 ollama 自己回報的 eval + prompt_eval 時間，
    # 不是 stopwatch —— 那兩個數字不含排隊與載入，比牆上時間乾淨。
    timings = {
        "llm_eval": (a1.get("eval_duration") or 0) + (a1.get("prompt_eval_duration") or 0),
        "embed": sum(c["wall"] for c in embed1),
    }
    split = cost_split(timings)
    split["timings"] = timings
    split["add_wall"] = wall1
    evidence["cost_split"] = split

    # ── 第二次：**完全相同的內容** ────────────────────────
    # 這不是「再寫一則」——是重複寫同一則。Phase 5 的雜湊去重會擋掉它，
    # 但去重在 Phase 2 的 LLM 呼叫之後，所以這次的 LLM 呼叫照樣發生。
    print("  第二次 add()（內容與第一次完全相同）…", flush=True)
    t0 = time.monotonic()
    second = memory.add(conversation, user_id="item3")
    wall2 = time.monotonic() - t0
    chat2 = [c for c in calls if c["kind"] == "chat"][n_chat1:]
    print("     → 抽出 %d 則記憶；chat=%d、牆上 %.1f 秒"
          % (memories_written(second), len(chat2), wall2), flush=True)

    b1 = chat2[-1] if chat2 else {}
    evidence["second_add"] = {
        "memories": memories_written(second),
        "wall": wall2,
        "eval_count": b1.get("eval_count"),
        "prompt_eval_count": b1.get("prompt_eval_count"),
        "content_chars": b1.get("content_chars"),
        "done_reason": b1.get("done_reason"),
    }
    evidence["duplicate"] = {"second_add_chat": len(chat2)}

    # **不可以用 `prompt_eval_count` 相減來量「prompt 有沒有變長」——除非先
    # 確定兩次都沒有被截斷。**
    #
    # 在 ctx 4096 之下那兩個數字都是 2050（兩次都被截斷到**同一個上限**），
    # 所以相減永遠是 0。一個名叫「prompt 變長了」的欄位會永遠回報「沒有變長」，
    # 而那個 0 是儀器的天花板，不是量測結果。**但這個禁令是有條件的**：ctx
    # 夠大時兩個數字是 8,052 與 8,167，相減得到的 115 是**有效**的（D-036）。
    # 所以判準是「先問有沒有被截斷」—— 那是 truncation_claim_verdict() 的工作，
    # 不是這裡。
    #
    # 這一支仍然走獨立管道（`prompt_chars()`，從攔截到的 messages 量），理由
    # 從「唯一可行」變成「與 prompt_eval_count 互相印證」：兩個管道一致時結論
    # 更強，不一致時看得出來。
    #
    # （原本這裡的註解說「第二次的 prompt 應該更長（Phase 1 檢索到第一次寫入的
    # 記憶）」，那個括號裡的成因**是錯的** —— Phase 1 檢索的是向量庫。真正會
    # 變長的是 `## Last k Messages`：mem0 把上一次的訊息寫進 history DB，第二次
    # add() 再讀回來。**那是一條可查的線索，不是這支探針的觀測**，所以它不住在
    # evidence 裡。）
    evidence["duplicate"].update(duplicate_evidence(a1, b1))
    return memory


def parse_sections(spec):
    """`--sections` 的字串 → (節名 tuple, 錯誤訊息或 None)。

    **刻意不靜默忽略不認識的名字。** 打錯一個字（`C3` 打成 `c3`）而它安靜地
    不跑，整輪就會變成「什麼都沒量到卻什麼都沒抱怨」—— 那是這個功能最危險
    的失敗方式。寧可擋下來。

    回傳的順序**照 ALL_SECTIONS，不照使用者打的順序**：那是依賴順序
    （C2 的對照組會把 context 載成 16384，C3 必須在它之後並先卸載）。
    """
    if spec is None:
        return tuple(ALL_SECTIONS), None
    want = [s.strip() for s in spec.split(",") if s.strip()]
    if not want:
        return (), "空的分段清單 —— 給 %s 這種形式" % ",".join(ALL_SECTIONS)
    unknown = [s for s in want if s not in ALL_SECTIONS]
    if unknown:
        return (), "不認識的節名：%s（可用的：%s）" % (
            "、".join(unknown), "、".join(ALL_SECTIONS))
    return tuple(s for s in ALL_SECTIONS if s in want), None


def run_probe(args, want=None):
    """跑指定的量測節，回傳 evidence。want 預設是全部。"""
    evidence = {}
    if want is None:
        want, _ = parse_sections(args.sections)
    evidence["sections_run"] = list(want)
    if want != tuple(ALL_SECTIONS):
        print("※ 分段重跑：只跑 %s（%d／%d 節）—— 這一輪**不算通過**，"
              "結束碼會是 2。"
              % ("、".join(want), len(want), len(ALL_SECTIONS)), flush=True)
    workdir = args.workdir or tempfile.mkdtemp(prefix="item3-")

    print("── 環境 ─────────────────────────────────", flush=True)
    if not measure_environment(evidence, args):
        print("  ✗ %s" % evidence.get("fatal"), flush=True)
        return evidence
    meta = evidence["meta"]
    print("  模型=%s、嵌入=%s" % (args.model, args.embed_model), flush=True)
    print("  當下生效的 num_ctx（/api/ps）= %s" % meta.get("context_length_before"),
          flush=True)
    probe = meta.get("probe") or {}
    print("  探針修訂版：sha256[:16]=%s、%s 位元組、mtime %s"
          % (probe.get("sha256_16", probe.get("error", "?")), probe.get("bytes", "?"),
             probe.get("mtime", "?")), flush=True)
    # **這一輪真正要動的變數，在跑那兩次 add() 之前就印出來。** 真實 add() 的
    # 生成上限若沒送到，整輪會用 mem0 的預設值跑完幾個小時，而輸出裡沒有
    # 任何一行長得不一樣 —— 那正是 D-035 第七節教訓 3 的形狀（第一次跑忘了
    # --json，重跑 11 分鐘才換到一個欄位）。寧可開跑前十秒發現。
    eff_cap = effective_num_predict(meta)
    print("  真實 add() 的生成上限 num_predict=%s（來源：%s）"
          % (eff_cap, meta.get("budget_source")), flush=True)
    budget_note = budget_note_to_print(meta)
    if budget_note:
        print("    %s" % budget_note, flush=True)

    if "C2" in want:
        print("\n── C2：抽取 prompt 是否被截斷 ─────────────", flush=True)
        measure_truncation(evidence, args)

    if "C2b" in want:
        print("\n── C2b：num_ctx 要開多大才不截斷 ─────────", flush=True)
        measure_ctx_law(evidence, args)

    if "C3" in want:
        print("\n── C3：截斷是從哪一端 ────────────────────", flush=True)
        measure_canary(evidence, args)

    if "C4" in want:
        print("\n── C4：thinking 是否計入成本 ─────────────", flush=True)
        measure_thinking(evidence, args)

    if "add" in want:
        print("\n── C1／C5：真的跑兩次 add() ──────────────", flush=True)
        measure_add(evidence, args, workdir)

    return evidence


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="第三階段 item 3 的探針：mem0 的 add() 到底多花多少？",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--model", default="qwen3:4b",
                   help="mem0 的 LLM 模型（預設 qwen3:4b，對齊 .env 的 OLLAMA_MODEL）")
    p.add_argument("--embed-model", default="qwen3-embedding:0.6b",
                   help="mem0 的嵌入模型（預設對齊 D-013 量到的 1024 維）")
    p.add_argument("--ollama-url", default="http://ollama:11434",
                   help="ollama 的位址 —— mem0 的 OllamaLLM 預設是 None，"
                        "不給就會退回 127.0.0.1:11434 而連不上")
    p.add_argument("--workdir", default=None,
                   help="chroma 與 history.db 的落地位置（預設用暫存目錄）")
    p.add_argument("--add-max-tokens", type=int, default=None,
                   help="覆寫 mem0 的 max_tokens（預設 2000）。縮小可以讓"
                        "真實 add() 快很多；機制判準 C2~C4 不受影響"
                        "（截斷只看 num_ctx，不看 num_predict，見 D-027）")
    p.add_argument("--ctx", type=int, default=None,
                   help="ollama 生效的 num_ctx。給了就**推導**真實 add() 的生成"
                        "預算（max_tokens = num_ctx − %d 的 prompt 上界），"
                        "而不是挑一個數字 —— 這個組合不會觸發 context shift，"
                        "而且 num_ctx 變大時預算自己跟著變大（D-037）。"
                        "推導只在 --add-max-tokens 沒給的時候生效；"
                        "num_ctx ≤ 界時推不出正的預算，直接以 3 結束。"
                        % EXTRACTION_PROMPT_TOKENS_BOUND)
    p.add_argument("--json", action="store_true", help="額外輸出 JSON")
    p.add_argument("--sections", default=None,
                   help="只跑這幾節（逗號分隔，全部＝%s）。**分段重跑用**："
                        "某一節的儀器壞掉、修好之後只想重跑那一節。"
                        "沒跑滿全部時結束碼是 2（無法判定），不會是 0 —— "
                        "「只跑了一部分」永遠不算通過。"
                        % ",".join(ALL_SECTIONS))
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    want, err = parse_sections(args.sections)
    if err:
        # 用法錯誤 → 3（不是環境、也不是判準）。argparse 的 p.error() 會走 2，
        # 而 2 在這支探針的意思是「無法判定（環境）」—— 那會誤導。
        print("--sections：%s" % err, file=sys.stderr)
        return EXIT_BROKEN
    ctx_err = ctx_arg_error(args.ctx)
    if ctx_err:
        # 同一個原則：用法錯誤 → 3。判斷本身在純函式裡（可離線測試），
        # 這裡只負責印。
        print(ctx_err, file=sys.stderr)
        return EXIT_BROKEN
    try:
        evidence = run_probe(args, want)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\n探針自己壞掉：%s" % e, file=sys.stderr)
        return EXIT_BROKEN

    print("\n── 判準 ─────────────────────────────────", flush=True)
    passed, problems = grade(evidence)
    for p in problems:
        print("  ✗ %s" % p, flush=True)
    if passed:
        print("  ✓ C1~C7 全過", flush=True)

    print("\n── 觀察 ─────────────────────────────────", flush=True)
    for n in observations(evidence):
        print("  · %s" % n, flush=True)

    if args.json:
        print("\n── JSON ─────────────────────────────────", flush=True)
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), flush=True)

    # 結束碼的規則是純函式（exit_code），理由與順序都寫在那裡。
    return exit_code(evidence, passed)


if __name__ == "__main__":
    sys.exit(main())
