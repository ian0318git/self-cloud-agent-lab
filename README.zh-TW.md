# self-cloud-agent-lab

[English](README.md) | **繁體中文**

在**免費雲端額度內**驗證一套 self-hosted AI 平台的可行性：
自己執行 LLM、讀自己的資料、透過 MCP 使用工具、並讓 agent 自主完成工作。

> **本專案的定位是「驗證沙箱」，不是常駐服務。**
> 原因見下方第一節 —— 這是開始之前最需要理解的一件事。

---

## 目錄

- [為什麼不是常駐服務](#為什麼不是常駐服務)
- [搬到 VPS](#搬到-vps)
- [硬體規劃](#硬體規劃)
- [架構](#架構)
- [快速開始](#快速開始)
- [Kaggle 實作手冊](#kaggle-實作手冊)
- [對外連線的安全性](#對外連線的安全性)
- [驗證清單](#驗證清單)
- [額度管理](#額度管理)
- [第二階段](#第二階段)
- [第三與第四階段 —— 計畫中；六項已量測](#第三與第四階段--計畫中六項已量測)
- [Codespaces 的坑](#codespaces-的坑)
- [疑難排解](#疑難排解)

---

## 為什麼不是常駐服務

GitHub Codespaces 免費額度換算成真實數字：

| 資源 | 免費額度 | 換算後的真實可用量 |
|---|---|---|
| Compute | 120 core-hours/月 | 2-core 消耗 2 core-hours/小時 → **僅 60 真實小時/月** |
| Storage | 15 GB-month | **codespace 存在期間就計費 —— 包含停止狀態** |
| Idle timeout | 預設 30 分鐘 | 最大 240 分鐘 |
| 埠可見性 | **預設 private** | 需手動改為 public |

**60 小時 ÷ 30 天 ≈ 每天 2 小時。** 若嘗試 24/7 常駐，
整月份額會在**約 2.5 天內耗盡**，接著 `$0` spending limit 會完全封鎖環境。

儲存額度的計算方式：

```
GB-month = (佔用 GB × 存在小時) ÷ 730
```

因此要讓一個 codespace 24/7 存在整月，總佔用必須 ≤ 15 GB。
**陷阱**：`docker compose down` 或停止 codespace **都不會停止儲存計費**，
只有**刪除整個 codespace** 才會。

**結論**：本專案用來回答「這套架構能不能跑、模型夠不夠聰明」，
驗證完就刪除。真正常駐的部署需要不同的宿主，見[第二階段](#第二階段)。

---

## 搬到 VPS

Codespaces 是**測試台**，不是終點。目標是一套可搬移的私有 AI 平台：
等到有足夠 CPU/RAM/GPU 的 VPS，就把這套堆疊搬過去、換上更大的本地模型，
像使用 ChatGPT 一樣使用它 —— 而資料、RAG、MCP、Agent、Memory 與工作流程
都掌握在自己手上。

本節記錄那次搬遷實際上涉及什麼，讓 POC 保持是 POC，而不會悄悄變成陷阱。
**2026-09-19 已依這個目標檢查過架構**，結果如下，並標明哪些能原樣帶走。

### 一行指令

```bash
git clone <這個 repo> && cd self-cloud-agent-lab
bash scripts/deploy-vps.sh --model qwen3:4b --num-ctx 16384
```

這就是整個佈署。它會關掉那個埠、下載模型、起堆疊、驗證堆疊真的生成得出
東西，並確認 context 長度真的生效了。底下幾節是它在做什麼、以及為什麼 ——
想手工做的時候讀，或者腳本裡有東西需要改的時候讀。

**兩個前提，寫出來是因為腳本兩個都不提供。** Docker 已經安裝而且執行中
（含 Compose v2），以及 repo 已經在這台機器上 —— 上面那行 `git clone` 就是
第二個，而它**刻意不在**腳本裡面。`deploy-vps.sh` 兩個都會檢查，然後
**停下來**；它不會在你的主機上跑任何安裝程式。Docker 不在或連不上時，它會
講出它查到的是三個原因裡的哪一個 —— 沒裝、裝了但 daemon 沒在跑、在跑但你
沒有權限 —— 並印出那個原因的處方，因為那三個的處方是三件不同的事（D-063）。

**順序本身就是安全論證，不是實作細節。** 不能拿 `up.sh` 當佈署路徑：它的
預設值是為 Codespaces 調的，那裡「發布一個埠」是私密的；同一組預設值在
VPS 上則是 fail-open。底下每一段都是因為這個差別才存在。

`deploy-vps.sh` **只支援全新安裝**。Open WebUI 的資料庫裡已經有帳號或對話
時，它會拒絕繼續，而且它永遠不搬資料（見本節最後的「搬既有資料」）。

### 可以原樣帶走的部分

| 項目 | 為什麼能存活 |
|---|---|
| `docker-compose.yml` | 三個容器加一個 bridge 網路 —— 沒有任何 Codespaces 專屬的東西。第三個 `mcp-test-server` 是第二階段的臨時設施，清單驗完就刪（`docker-compose.yml:196`）。 |
| Open WebUI 的狀態 | 對話、Knowledge、MCP 連線、使用者、設定全都存在 `open_webui_storage` volume 裡。複製 volume，資料就過去了。 |
| 模型選擇 | `.env` 的 `OLLAMA_MODEL` 是**給腳本用的**對話模型名字（`up.sh` 拿它去拉、驗證腳本拿它去跑）。`docker-compose.yml` **完全沒有讀它**，也沒有讀 `EMBEDDING_MODEL` —— 那個檔案裡根本沒有 `OLLAMA_MODEL` 這一行（`grep -n OLLAMA_ docker-compose.yml`，2026-09-23 查證）。嵌入模型也不在 `.env` 裡生效：它在 Open WebUI 自己的 `config` 表，要用 `scripts/set-embedding.sh` 改，而且**必須重啟**，因為行程在啟動時只建一次嵌入函式（D-013）。你實際對話用的模型則是**每一則對話在介面上選**，或由模型預設釘住。這兩者都在資料庫裡、跟著 volume 走 —— 所以「換更大的模型」＝`.env` 一行 ＋ 一次 pull ＋ 介面上選一次。 |
| MCP / RAG / Memory / Agent | 全都是 Open WebUI 在資料庫裡的設定，不在本專案裡。跟著 volume 走。 |
| Cloudflare Tunnel | `cloudflared` 走 compose profile，而且是**對外**撥接。不需要任何 inbound 埠 —— 這正是它在 VPS 上同樣正確的原因。 |

### 第 1 步（必須）：關掉那個埠

**這件事要先做。** `ports: "3000:8080"` 綁的是 `0.0.0.0` —— 在 Codespaces 上
無害（埠預設私有、需 GitHub 認證），但在有公開 IP 的 VPS 上，這會把
Open WebUI 放到整個 Internet 上。它之所以是危險而不只是不整齊，是因為
**它會繞過 Cloudflare Access**：Access 守的是 tunnel 那條路，不是這個埠。
掃到 3000 的人根本不會碰到 Access。

```bash
# 在 .env 裡
WEBUI_BIND_ADDR=127.0.0.1
```

然後重建容器，並且**機械驗證** —— 不是讀設定檔：

```bash
bash scripts/down.sh && bash scripts/up.sh
bash scripts/check-exposure.sh
```

`check-exposure.sh` 讀的是**執行中容器的實際綁定**（`docker port`），
不是 compose 檔的宣告；它判讀的是**每個埠綁在哪一類位址上** —— loopback、
萬用、私有、或公開 —— 而不是只問「有沒有用萬用位址」。所以綁在這台機器
自己的區網位址上，它會回報成**「區網可達，不是 loopback」**：那不是對
Internet 暴露，但它也不經過 tunnel、不經過 Cloudflare Access，碰得到那個
位址的人直接就能開到登入頁。這一種是**靠你的路由器擋住的，不是靠這套堆疊**
—— 綁在公開位址上則是真正的暴露，而它會被照實回報。

**這支檢查答不了什麼：** 它讀的是**埠**，而 Cloudflare Tunnel **不發布任何埠**
—— 那正是它叫 tunnel 的原因。所以「沒有開著的埠」不等於「連不到」。
tunnel 開著的時候，這台主機正在服務你的主機名，而 `check-exposure.sh`
會回報「沒有任何介面暴露」。那不是它錯了，是它答的是另一個問題。
**tunnel 本身的狀態由 `bash scripts/status.sh` 回報**，它讀的是**容器**、
不是設定檔 —— 為什麼這個區分是關鍵，見下面的「要關掉 tunnel」。

**手工做的時候有個陷阱**，而這正是腳本要在呼叫 `load_env` **之前**寫
`.env` 的原因：`docker compose` 解析**shell 環境變數**的順序在 `.env` 之前，
而 `load_env` 會把它 source 進來的東西全部 **export**。所以先改 `.env` 再跑
`up.sh`，修好的值會被讀到，但**已經 export 的舊值仍然勝出** —— compose 繼續
用 `0.0.0.0`，而且沒有任何東西會告訴你。在 `load_env` 之後才修 `.env` 是
無效動作。要驗證請用 `docker port open-webui`，永遠不要用檔案。

同時要維持 11434 不發布（D-003）。Ollama 完全沒有認證機制，
這支腳本也會一併檢查。

### 第 2 步：放大模型與資源限制

`.env` 裡有五個值是為 2 核心、8GB 調的。它們是第一個該調大的地方：

```bash
OLLAMA_MODEL=qwen3:70b          # 或這台 VPS 裝得下的任何模型
OLLAMA_CONTEXT_LENGTH=16384     # 見底下 —— 4096 會截斷抽取 prompt，而 8192 也不夠
OLLAMA_MAX_LOADED_MODELS=3      # 必須 >= OLLAMA_NUM_PARALLEL；多載一顆就多一份權重
OLLAMA_NUM_PARALLEL=4           # 吞吐提升明顯；多個請求共用一次模型載入 —— 但它會把 KV cache 乘上去（見底下）
OLLAMA_KEEP_ALIVE=-1            # 常駐；重載一次要數十秒
```

`OLLAMA_CONTEXT_LENGTH` 與其他幾個不同：它**不是免費的**。它吃 KV cache 的
記憶體（3B 等級的模型大約每 token 36 KiB；那個數字是由 Qwen2.5-3B 的架構
推得的**估算值，本堆疊未實測**，而且會隨模型的層數／head 數變動），也吃
**時間** —— 在本堆疊上，8,192 開到 16,384 讓同一份工作**慢了 45.5%**（D-035）。
（*那是牆上的**總量**，量得可靠；至於一次執行裡「prefill 佔多少、生成佔多少」
是另一個命題，而它已經不成立了 —— **錯 6.8%～8.2%**，因為 `eval_duration` 是一個
**減出來的餘數**（`total − load − prompt_eval`），所以第一次以後的每一次 prefill
都會被它吃掉、被貼上「生成」的標籤。D-036 第五節問，D-041 第五節答。
那次「分成兩段」本身也查清楚了：它**不是**第二個請求 —— 每一個 `add()` 從頭到尾
只有一個 `POST /api/chat`，取消與重送都發生在同一個請求裡面，是 ollama 自己的行為，
所以每一個走 ollama 的長生成都會遇到（D-041 第十節）。*）
儘管如此，預設值仍然值得調高的理由：**記憶抽取會被無聲截斷。**
一次 `mem0` 的 `add()` 送出的抽取 prompt 實測是 8,052 與 8,100 個 token，
而 ollama 的 4096 預設會把它砍到 2,050 —— prompt 尾端的指示被丟掉，抽取回傳
零筆事實，而且**不報錯**（D-027）。

**「大到 prompt 進得去」是兩個門檻，而且明顯的那一個不夠。** 第一個是
**生成之前**的截斷觸發點 `prompt_tokens > num_ctx − 1`，所以 ≥ 8,101 能讓
prompt **進得去**。但 ollama 是用 `--context-shift --keep 4` 起 llama-server
的：生成只要超過 `num_ctx − prompt_tokens`，llama-server 就會**從 prompt
中段丟掉一整塊**再繼續生成。`num_ctx=8192` 之下那個餘裕只有 **140** 個
token，兩次 `add()` 都撞到了（D-035）。真正該滿足的是
`num_ctx > prompt_tokens + num_predict` —— 這裡約 10,052，所以要用
**16384**。`deploy-vps.sh` 會在相關的那一步解釋這件事。

**但調高仍舊是必要、不是充分。** 兩組對照組都在 2026-09-22 跑完：`num_ctx=8192`
之下 prompt 進得去，但生成途中被 shift 掏空；`num_ctx=16384` 之下它整段生成
期間都保持完整（`context shift` 次數：**0**），而抽取**仍然回傳零筆**，生成也
**仍然停在 `num_predict=2000`**。所以截斷與生成期間的 shift **都不是**把抽取
清空的原因 —— 兩個都被排除了。

**第三組對照組確認了剩下的那個候選。** `num_ctx` 不動（16,384），只改
`num_predict` —— mem0 預設的 **2,000 → 8,000**：第一次 `add()` 抽出 **2 則
記憶**，`done_reason=stop`、`eval_count` 5,605（D-036）。**生成預算就是成因**，
而 2,000 那一輪說明了為什麼：在那個上限下，預算全部被 `thinking` 吃掉，答案
根本還沒開始生（`thinking_chars=7796` 對上 `eval_count=2000`，D-034）。

所以要佈署的是一組配對：context 用 **16384**，而 `max_tokens` **大到模型自己
停下來** —— 不是再定一個魔數。判準是 `done_reason == "stop"`；這個模型對這份
prompt 的自然長度是 5,605 個 token，任何低於它的固定上限都會**靜默地**截斷，
而截斷出來的結果讀起來像「沒有事實可抽」。

**「預算」那一半現在寫下來了 —— 寫成推導，不是常數（D-037）。** 探針從它
拿到的 context 推出預算：

```
max_tokens = num_ctx − EXTRACTION_PROMPT_TOKENS_BOUND   # bound = 8192
```

不變式就是它與「8,000 這個數字」的差別：只要 prompt 不超過上界，
`prompt + 預算 ≤ num_ctx`，所以生成期間的 **context shift 結構上不可能發生**
—— 而且 `num_ctx` 調大時預算自己跟著變大。兩條硬判準盯著這條推導的兩個前提，
它們是「過期的推導不能靜默通過」的理由：**C6** 在這一輪實測的
`prompt_eval_count` 超過上界時失敗（處方是**更新上界**），**C7** 在生成不是以
`done_reason=stop` 結束時失敗（處方是**把 `num_ctx` 開大**）。預算是被刻意
覆寫時（`--add-max-tokens`、`--quick`）兩條都不判 —— 那時預算小是**實驗**，
不是缺陷。

**這件事沒有佈署，文件也不該讀起來像佈署了。** 這個 repo 裡沒有任何地方在
生產環境呼叫 mem0：`grep -rn 'import mem0'` 只命中探針，`mcp-server/` 沒有
記憶工具，open-webui 的 memory 層整個沒開。存在的是**推導 ＋ 會叫的判準** ——
第四階段的 agent 會沿用那份 config 樣板，而佈署路徑（`deploy-vps.sh`）仍然
只寫 context 那一半。

**驗證它有生效不是可有可無的步驟**，因為這個變數的**名字**在二進位檔裡被
驗證過，遠早於有任何東西證明它真的有用（D-028）。要從**已載入的模型**讀
回來，不是從設定檔：主機上沒有 `curl`（ollama 刻意不發布任何埠），所以要在
容器裡跑 —— `deploy-vps.sh` 會替你做這件事並回報它讀到的值。

### 搬既有資料

`deploy-vps.sh` 只做全新安裝，而且它會**明講**而不是做一半。搬一個已經有
資料的安裝是另一份程序：停掉兩邊的堆疊、複製 `open_webui_storage` 與
`ollama_models` 兩個 volume、把目標起來，然後在動任何設定之前先確認帳號數
與對話數。腳本會把這段提醒印出來，而不是用猜的。

### 真正帶不走的部分

這些是真的搬遷成本。列出來是為了不要在搬的過程中才發現 ——
與 D-014 存在的理由相同。

1. **換嵌入模型就必須重新嵌入所有文件。** 上方第二階段與 D-013 已提過，
   但搬遷時它會變成關鍵路徑：**在你上傳真正在意的語料之前**先決定嵌入模型。

2. **驗證工具說的是 Ollama 的原生 API，不是 OpenAI 的。**
   `verify_api.py` 與 `ask_probe.py` 呼叫 `/api/generate`，並讀取只有 Ollama
   才有的欄位 —— `thinking`、`done_reason`、`eval_count`、`load_duration`。
   vLLM 提供的是 `/v1/chat/completions`，沒有對應欄位。
   **把 runtime 換成 vLLM，意味著要重寫這兩支腳本**，不只是改一個 URL。

3. **換 runtime 是 Open WebUI 的設定變更，不是本專案的。**
   Open WebUI 透過 `OLLAMA_BASE_URL` 跟 Ollama 講話，但它是透過
   **OpenAI 相容連線**（管理員控制台 → 設定 → 連線）連到 vLLM。
   兩者可以並存，所以這次搬遷可以是漸進的 —— 兩邊都跑、比較、再移除不要的。
   本專案不需要為此改任何東西。

4. **D-001 的 8GB 記憶體預算不再是限制。** 那正是搬遷的目的，但這也意味著
   好幾個決策背後的理由（只載入一顆模型、單一平行請求、因為其他方式都失效
   所以維持 thinking 開啟）是針對**這台機器**的。要重新檢查，而不是繼承 ——
   特別是 D-011 的「維持 thinking 開啟」，當 GPU 讓「每題 264–293 秒」
   變得無關緊要時，值得重新看一次。

---

## 硬體規劃

這套堆疊要多少硬體、每一種配置買到什麼、以及租來的機器能多接近託管助理。
**實測的都標了實測，其餘全部是推估，也標了推估。** 推估用的規則寫在下面，
沒列到的硬體可以自己代進去算。

### 基準線：這台機器是什麼、跑出什麼

| | |
|---|---|
| CPU | Intel i7-1260P（Alder Lake 筆電版）的 4 vCPU 切片 |
| RAM | 主機 15 GiB；ollama 容器**沒有**記憶體上限 |
| GPU | 無 |
| Runtime | `OLLAMA_NUM_PARALLEL=1`（單槽）、`OLLAMA_CONTEXT_LENGTH=16384`、`OLLAMA_MAX_LOADED_MODELS=2`、`OLLAMA_KEEP_ALIVE=-1`（後兩者自 2026-09-23 起，D-049） |

2026-09-22/23 實測，逐輪日誌都留著：

| 量 | qwen2.5:3b（1.9 GB） | qwen3:4b（2.5 GB） |
|---|---|---|
| 生成 | 5–6 token/s | 2.7–4.8 token/s |
| prompt 前處理（prefill） | 19–24 token/s | 19–24 token/s |
| 冷啟動載入 | — | 71.8 秒 |
| 暖重載 | — | 3.3 秒 |

三個光看數字看不出來的後果：

1. **prefill 是「每輪」的稅，不是「每題」的稅。** 1,880 個 token 的 prompt 在這台
   要 **93.5 秒**。每一次工具呼叫都是一次往返，而每一次往返都要重算一個**更大**的
   prompt —— 所以一個呼叫三次工具的任務，這筆稅要付四次。
2. **推理模型會把四秒的答案變成十分鐘。** 一輪丟三顆骰子生成了 **2,966 個 token、
   花了 620 秒**；三次工具呼叫本身是瞬間的。那些 token 絕大多數是 `thinking`。
   慢的不是工具往返，是模型在那裡想。
3. **單槽就是全部排隊。** `OLLAMA_NUM_PARALLEL=1` 之下，第二個聊天室不是變慢，
   是**在第一個跑完之前根本不會動**。當一個答案要十分鐘，那十分鐘就是整台機器。
   這是換大機器時第一個該調大的數字，而且它不犧牲任何模型品質。

**預設模型已於 2026-09-23 更換**（`.env`：`qwen3:4b` → `qwen3:8b`（5.2 GB），
外加 `OLLAMA_KEEP_ALIVE=-1` 與 `OLLAMA_MAX_LOADED_MODELS=2` —— D-049）。所以上面那張
表是 **4b 系列**，而這件事要緊，因為**速率是模型的函數**。同一支探針改量
`qwen3:8b`、每個條件一個樣本：

| 條件 | decode | prefill* |
|---|---|---|
| `num_ctx=8192` | 3.77 t/s | 76.7 t/s |
| `num_ctx=16384` | 3.55 t/s | 76.4 t/s |
| `num_ctx=16384`、`num_predict=128` | 2.89 t/s | 56.4 t/s |
| `num_ctx=16384`、`num_predict=512` | 3.12 t/s | 73.0 t/s |

\* **prefill 那一欄不可引用。** 這支探針沒有控制 prefix cache 的重用，它自己也這樣
說（同一支儀器報過 306～14,710 t/s）；本輪 ollama 日誌顯示真實的冷 prefill 約
**25 t/s**。decode 那一欄不受影響。這一輪是 `--quick`（每條件一個樣本），所以依
探針自己的設計它**不是基準線**（exit 2，「沒有嘗試建立基準線」），只是一次**有條件
的讀數**。

這一輪量到兩件舊系列沒有的成本：16k context 之下模型**常駐 7.9 GB**（`ollama ps`、
100% CPU），而磁碟上是 5.2 GB —— 差額就是 KV cache；而探針的 KV 斜率是
**146 KiB/token 的上界**（只有兩點，其中約 23 KiB/token 不是 KV）。量測期間
`vmstat` 顯示沒有換頁、`id` 是 0%、四顆 vCPU 上的執行佇列 5–6 —— 所以這些數字
**略偏保守**。

**兩組數字現在是同一把尺了 —— 4b 也用同樣的方式量過。** 同一支探針、同一組條件、
只換模型名（`--quick -- --models qwen3:4b --ctx 8192 16384`，exit 2、3m46s、
**全程零換頁**）：

| 條件 | 4b | 8b | 4b／8b | 4b 等效頻寬 | 8b 等效頻寬 |
|---|---|---|---|---|---|
| `num_ctx=8192` | 5.93 t/s | 3.77 t/s | 1.57× | 13.8 GiB/s | 18.4 GiB/s |
| `num_ctx=16384` | **5.96** | **3.55** | **1.68×** | **13.9 GiB/s** | **17.3 GiB/s** |
| `num_ctx=16384`、`num_predict=128` | 5.72 | 2.89 | 1.98× | 13.3 | 14.1 |
| `num_ctx=16384`、`num_predict=512` | 5.23 | 3.12 | 1.68× | 12.2 | 15.2 |

三件事就此定下來。**聊天搶 CPU 的代價約 20%**：同一個 4b 在真實聊天裡是 4.78 t/s
（D-046 證據表），乾淨單請求是 5.96 t/s。**剩下的差距不是吵雜**：兩組乾淨讀數之間
仍差 1.24× —— 4b 只快 1.68×，而它小了 2.09×，也就是下一節那條規則**系統性高估
小模型**。**等效頻寬不是這台機器的一個數字**：它隨模型大小落在 13.1–19.7 GB/s，
引用時要**連模型大小一起說**。D-047 第一節的校準值因此寫成 12–20 GB/s 而不是 12
（D-049 第四／五節）。

兩輪的**逐字輸出**（除移除 ANSI 色碼外未編輯）已存進版控：`docs/evidence/`
（`2026-09-23-throughput-qwen3-4b.txt` 與 `…-8b.txt`）；那些轉錄要守的規矩寫在
`docs/evidence/README.md`。

### 預測速度的那條規則

```
生成速度 ≈ 有效記憶體頻寬 ÷ 模型檔大小
```

每生成一個 token 就要把模型的權重讀過一次，所以決定速度的是**記憶體頻寬**，
不是核心數。用這台校準：2.5 GB 跑 **5.96 token/s（乾淨單請求）⇒ 等效約 14.9 GB/s**；
同一顆在**聊天搶 CPU** 時掉到 4.78 token/s（約 12 GB/s），而 8B 的檔案跑到約
18.6 GB/s —— 所以這**不是一個數字，是一個範圍：跨模型大小 13.1–19.7 GB/s**
（D-047 第一節把它記成 12–20 GB/s；兩組數字都在上一節的表裡）。
雙通道 DDR5 桌機的標示值是 ~77 GB/s，所以這個 VM 切片只拿到它宿主的一小部分。
下面的推估一律假設**跑得到標示頻寬的 60%**；要信之前先量自己的機器。
（prefill 是算力綁定、行為不同 —— 這也是 GPU 對它的改善遠大於對生成的原因。）

### 一顆模型要多少記憶體

| 模型級別 | Q4_K_M 檔案 | ＋ 16k context 的 KV cache | 現實最低需求 |
|---|---|---|---|
| 3–4B | 1.9–2.5 GB *(實測)* | ~0.6 GB | 4 GB |
| 7–8B | ~5 GB | ~1.2 GB | 8 GB |
| 14B | ~9 GB | ~2 GB | 16 GB |
| 32B | ~20 GB | ~4 GB | 24 GB（很緊） |
| 70B | ~43 GB | ~8 GB | 48 GB |
| 120B MoE | ~60–65 GB | ~2 GB（同時作用的層較少） | 80 GB |

KV cache 那一欄是從 **D-022 的「3B 級模型約 36 KiB/token」**換算的 ——
那是從 Qwen2.5-3B 架構**推得**的估算值，不是在本堆疊上量到的。由此有兩件事
很容易做錯：把 `OLLAMA_CONTEXT_LENGTH` 開大是**按比例**吃記憶體的，而 mem0 抽取
需要的 16384（見上方第 2 步）不是免費的。

### 各種機器買到什麼

| 硬體 | 有效頻寬 | 3–4B | 8B | 14B | 32B | 70B |
|---|---|---|---|---|---|---|
| 本實驗機（4 vCPU VM） | **12–20 GB/s** *(實測；隨模型大小變動)* | **5.2–6.0** *(實測，乾淨單請求)* | **3.5** *(實測)* | — | — | — |
| 桌機，DDR5 雙通道 | ~46 GB/s | 20–30 | 8–10 | 5 | 2 | — |
| 裸機，12 通道 DDR5（EPYC） | ~275 GB/s | 100+ | 40–60 | 25–30 | 12–15 | 5–6 |
| 1× 24 GB GPU（RTX 4090/3090） | ~600 GB/s | 100+ | 60–100 | 40–60 | 20–30 | 放不下 |
| 1× 48 GB GPU（L40S/A6000） | ~520 GB/s | 100+ | 60–100 | 40–60 | 25–35 | 10–15 |
| 1× 80 GB GPU（A100/H100） | ~1,200–2,000 GB/s | 100+ | 60–100 | 40–60 | 50–80 | 25–45 |

除了標實測的那一列，其餘都是**推估**（上面那條規則、60% 效率），當成數量級看，
不要當成承諾。`—` 代表放不進系統記憶體，或慢到沒辦法對話。

有兩列值得讀兩次。**純 CPU 的雲端 VPS 通常比桌機更差**，因為它是共用記憶體匯流排
的切片 —— 而這正是這個工作負載唯一需要的資源。以及**單張 24 GB GPU 是第一種
質變的配置**：14B 在那裡從「要等」變成「可以對話」。

### VPS 配置

| | A — 純 CPU | B — 單張 24 GB GPU **（甜蜜點）** | C — 80 GB GPU |
|---|---|---|---|
| 規格 | 8 vCPU / 32 GB / 200 GB NVMe | 8 vCPU / 32–64 GB / 1× 24 GB / 200 GB NVMe | 16 vCPU / 128 GB / 1× 80 GB / 500 GB NVMe |
| 跑得動 | 7–8B，5–10 token/s | 14B 40–60；32B 20–30 token/s | 70B 25–45 token/s |
| 幾個人用 | 1 個人，而且要很有耐心 | 1–5 人順暢 | 5–15 人 |
| 成本量級（2026） | 約 $40–120/月 | 約 $250–650/月 | 約 $1,100–2,200/月 |

成本只是**數量級** —— GPU 租金會變動，而「先租小時數」才是誠實的試法。
有一筆帳要算清楚：**在這個價位上，自架不會比 API 便宜。** 你買的是資料路徑，
不是價格。這個理由本身是對的，而且值得明講 —— 因為反過來做（為了省錢去租
80 GB GPU）是不會成立的。

**上面那些 token/s 數字全部是在 x86_64 上量的。** 它們是這張表裡唯一與 CPU
架構有關的一欄，而這個 repo 在 ARM（Graviton／Ampere／Oracle ARM）上**一次
都沒有跑過**。映像檔是 multi-arch，所以 ARM 機器**會**起來、**會**服務 ——
只是用一個沒有人量過的速度。`scripts/deploy-vps.sh` 佈署時會把這件事講出來，
然後繼續跑，因為「沒量過」不是「壞掉」（D-057）。

搬到 GPU 機型時容易漏掉的事：

1. **機器要有 NVIDIA container runtime。** 裝置保留區本身不必再手加了：
   `scripts/deploy-vps.sh` 會自己偵測 GPU，並把帶著
   `deploy.resources.reservations.devices` 的 `docker-compose.gpu.yml` 接上去。
   runtime 到位後 ollama 映像檔會自己認到 GPU。開關是 `.env` 的 `OLLAMA_GPU`
   —— `auto`（預設）／`on`／`off`。**有硬體但沒有 runtime 時腳本會停下來**，
   不會靜默退回 CPU。而且佈署要等到「真的跑一次推論 ＋ 讀 `/api/ps` 確認
   `size_vram == size`」才算完成 —— 裝置看得到、runtime 有註冊，兩者都還只是
   宣告面。
2. **VRAM 是硬牆。** 權重加 KV cache 一旦超過，ollama 會把層卸載到系統記憶體，
   速度是**崩掉**（常常是差 10–50 倍，不是差 20%）。要照「模型**加 context**」估，
   不是只估模型。佈署腳本在估算超過偵測到的 VRAM 時會警告（不擋）——
   **估不出來時也一樣警告**（表外模型、或沒給 `--model-gb`），
   不會在一個空白數字旁邊印綠色 OK。
3. **磁碟速度會出現在第一句話上。** 這台冷啟動載入 2.5 GB 要 71.8 秒；
   70B 那一級是一次 43 GB 的讀取。
4. **絕對不要發布 11434。** ollama 完全沒有認證機制 —— 在有公開 IP 的機器上，
   下方「對外連線的安全性」不是選配。

### 租一台 VPS 能多接近 ChatGPT？

這其實是兩個問題，答案不一樣。

**功能面** —— 工具、對自己文件的 RAG、MCP、多使用者、OpenAI 相容端點 ——
這裡**已經有了**，而且原樣搬到 VPS。硬體決策不會改變這一層。

**模型面**是你租不掉的那一半。開源權重模型的天花板低於前沿模型，而差距正好出現在
工作室會注意到的地方：長鏈多步推理、照著複雜指令走、以及**知道自己不知道**。
按級別誠實地說：

- **7–8B**：摘要、抽取、分類、翻譯沒問題。它在**沒有工具的時候會自己編一個工具
  結果** —— 本堆疊 2026-09-23 實測：同一個聊天室出現三塊捏造的 `<tool_response>`，
  而 MCP 端**一個 session 都沒有**。只要「答錯很貴」，就需要護欄。
  （這筆的正式紀錄還沒寫；原始日誌留著。）
- **14B–32B**：一個能幹的初階助理，而且它記得你給它的每一份資料。對單人或小型
  工作室來說，這一級才是「接近 ChatGPT 的功能」這句話可以成立的地方。
- **70B 與 100B+ MoE**：明顯更好，但仍然不是 ChatGPT —— 而在 C 級的成本上，
  值得先拿 API 對照過，再決定要不要簽月租。

**實務建議：配置 B。** 14B–32B 跑 20–60 token/s，是「媒介不再是問題」的那條線。
先租小時數，把你最難的真實任務跑一遍，再決定上一級值不值得 3–4 倍的月費。
並且留著 OpenAI 相容連線（見 compose 註解）當逃生口：敏感的自己跑、其餘走 API ——
這個混合做法比任何一個極端都更便宜也更好。

### 「敏感資料」實際上要求什麼

決定敏感與否的是**什麼東西離開**，不是模型跑在哪裡。

- `ENABLE_OPENAI_API=false` 在這裡是預設值，**因為上游的預設值會把資料送去
  OpenAI** —— 見 `docker-compose.yml` 的註解。維持關閉。
- 網頁搜尋與外部工具就是「把內容送出去」的那兩個功能。敏感的語料上必須關掉；
  內網的本地 MCP 工具（例如 `mcp-test-server`）完全是另一回事。
- 本來就在本地的：嵌入模型（`bge-m3`、`qwen3-embedding:0.6b`）、對話存在
  `open_webui_storage` volume（`webui.db` 就是整個安裝）。
- **仍然會出去的，而且最容易忘：** 第一次開機會從 Hugging Face 下載嵌入模型，
  `ollama pull` 會連 `registry.ollama.ai`。**在資料進來之前**先把要用的模型全部
  拉好。
- 兩個 volume 都要備份。掉了 `webui.db` 就等於掉了所有對話、文件與設定。

---

## 架構

第一階段（本 repo 已實作）：

```
┌─────────────────────────────────────────────┐
│  GitHub Codespace (2-core / 8GB / 32GB)     │
│                                             │
│   ┌───────────────────────────────────┐     │
│   │  Docker network: ai-net           │     │
│   │                                   │     │
│   │  ┌──────────┐      ┌───────────┐  │     │
│   │  │  ollama  │◄─────│ open-webui│  │     │
│   │  │  :11434  │      │   :8080   │  │     │
│   │  └──────────┘      └─────┬─────┘  │     │
│   │   （不對外發布）          │        │     │
│   └───────────────────────────┼───────┘     │
│                               │             │
│                        埠轉發 :3000         │
└───────────────────────────────┼─────────────┘
                                ▼
                        瀏覽器（Open WebUI）
```

| 元件 | 角色 |
|---|---|
| **Ollama** | 本地 LLM 推論引擎 |
| **Qwen3 4B** | 預設模型（2.5 GB，256K context，支援 tool calling） |
| **Open WebUI** | 聊天介面 + 內建 RAG + 原生 MCP 支援 |
| **cloudflared** | *選用*，預設關閉。對外連線用的 outbound tunnel，見[對外連線的安全性](#對外連線的安全性) |

> `docker compose up -d` 還會起**第三個**容器：`mcp-test-server`。它沒有宣告
> profile，所以不像 `cloudflared` 那樣是選用的。它屬於第二階段的圖，見下面的
> [東西跑在哪裡（第二階段）](#東西跑在哪裡第二階段)。

---

## 快速開始

### 在 Codespaces 上

1. 在 GitHub 上開啟此 repo → **Code** → **Codespaces** → **Create codespace on main**
2. 選擇 **2-core / 8GB** 機器（**不要選 4-core —— 會讓額度消耗變成 4 倍**）
3. 等待建立完成 —— `postCreateCommand` 會自動執行 `scripts/up.sh`，
   包含下載模型（首次約需數分鐘）
4. 開啟 **PORTS** 面板 → 點擊 **3000** 埠的網址
5. 註冊第一個帳號（**會自動成為管理員**）
6. 登入後立刻**鎖住註冊**：
   ```bash
   bash scripts/lock-signup.sh
   ```

### 在本機

需求：Docker 與 Compose v2。**至少 8GB RAM**
（若只有 4GB，請改用 `qwen3:1.7b`）。

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

開啟 <http://localhost:3000>。

### 指令一覽

| 指令 | 用途 |
|---|---|
| `bash scripts/deploy-vps.sh --model M --num-ctx N [--model-gb G]` | **在全新 VPS 上一鍵佈署。** **前提是 Docker 與 Compose v2 已經安裝而且執行中** —— 它會檢查、講出查到的是三個原因裡的哪一個、然後停下來；它不安裝（D-063）。先安全地寫 `.env`**再**載入它、起堆疊、以真實埠綁定過閘門、下載兩個模型，然後證明堆疊生成得出東西、且 `num_ctx` 真的生效。只做全新安裝 —— 資料庫已有帳號或對話時會拒絕。**`--model-gb G`** 用來明講內建表不認識的模型要多少磁碟；不給的話磁碟閘門會說「不知道」，而它**不會從 tag 猜**（D-058） |
| `bash scripts/deploy-vps.sh --dry-run` | 同樣的前置檢查與 `.env` 差異，但不寫任何東西。它印的磁碟數字**只含無條件需要的那一份** —— 這次會拉的 compose 映像，加上系統餘裕。模型刻意**不在**那個數字裡：模型在不在要等容器起來才問得到，所以那一半會在下載前**再確認一次**（D-058） |
| `bash scripts/deploy-vps.sh --expose` | 刻意把 Open WebUI 發布在 `0.0.0.0`，並把閘門降級成警告。**這會繞過 Cloudflare Access** —— 它是給真的在邊緣有過濾的機器用的，不是方便旗標 |
| `bash scripts/up.sh` | 啟動堆疊 + 確保模型存在（冪等） |
| `bash scripts/down.sh` | 停止容器，**保留**模型與對話紀錄 |
| `bash scripts/down.sh --purge` | 停止容器並**刪除**所有 volume |
| `bash scripts/pull-model.sh` | 單獨重試模型下載 |
| `bash scripts/status.sh` | 容器狀態、模型清單、記憶體用量 |
| `bash scripts/check-exposure.sh` | 哪些埠真的能從外面連到 —— 讀執行中容器的實際綁定，不是讀設定檔 |
| `bash scripts/verify.sh` | 第二階段前置驗證：生成速度、tool calling、thinking、事實正確性（含對照題）、繁中輸出（約 15 分鐘）。結束碼：`0` 關鍵項目全過、`1` 有項目未通過、`2` **無法判定** —— 那不是失敗（D-016） |
| `bash scripts/verify.sh 2` | 同上，但只跑第 2 項（關閉 thinking，約 1 分鐘） |
| `bash scripts/verify.sh 3,4 qwen3:1.7b` | 用指定模型跑指定項目（模型比較用；模型只能走參數，環境變數會被 `.env` 覆蓋） |
| `bash scripts/ask_probe.sh qwen3:4b qwen2.5:3b` | 模型的答案**事實是否正確**，外加速度。每道事實題都附一道對照題（每題約 1–2 分鐘） |
| `bash scripts/rag-verify.sh` | **第二階段 RAG 的機械驗證**：檢索有沒有挑對段落、模型有沒有真的用它、文件沒寫的東西它會不會照樣發明。走應用程式自己的檢索函式。模型取自 `.env` 的 `OLLAMA_MODEL`，而**範例檔的預設是 `qwen3:4b`（思考型）—— 照這一行原樣跑，三次生成會全部撞到 300 秒上限，至少 15 分鐘之後得到「無法判定」**（2026-09-19 實測）。想要會亮綠燈的跑法請看下一行。結束碼同上，另加 `3` = 探針自己壞掉（D-018） |
| `bash scripts/rag-verify.sh --model qwen2.5:3b` | 同上，指定模型。**建議的跑法**：非思考型模型，三次生成各 41–94 秒，整輪約 2–5 分鐘（四輪 12 個樣本實測，D-018）。模型必須是參數——`.env` 會覆蓋環境變數 |
| `bash scripts/rag-verify.sh --timeout 900` | 拉長單題時限，給思考型模型用。「太慢」與「連不上」會分開講 —— 兩者要修的東西不同 |
| `bash scripts/rag-http-verify.sh` | **同樣四項 RAG，改走真正的 HTTP 路徑** —— 建立知識庫、上傳文件、問文件裡的事、問文件裡沒有的事 —— 走 `/api/v1/files` 與 `/api/chat/completions`，而不是應用程式的函式。需要 `.env` 裡有一組 `WEBUI_API_KEY`（而那又需要先打開 **管理員控制台 → 設定 → 驗證 → API 金鑰**）。它補上 `rag-verify.sh` 的盲點，也有自己的盲點：看不到撈回了哪些 chunk。結束碼 `0`/`1`/`2`/`3`（D-019） |
| `bash scripts/verify-throughput.sh` | **這台機器實際解碼多快** —— 七個條件的矩陣，每個量**七次**（約 35 分鐘），外加兩組 `num_ctx` 下的 prompt 上限。只有在重複彼此一致時才印出基準線；**不一致時 exit `1` 並指出是哪幾個條件在吵**。離散度看的是變異係數而不是全距 —— 全距會隨樣本數變大，七次取樣下舊的「全距 15%」實際上等於 5.55%（D-032）。這就是仍未完成的那次重打（D-031、D-032） |
| `bash scripts/check-egress.sh` | **資料可能流向哪些外部服務** —— 以資料庫的實際值為準，列出「啟用中」的外部端點 |
| `bash scripts/check-egress.sh --fix` | 關掉 `openai.enable`，重啟容器，並回讀確認（D-017） |
| `bash scripts/probe-openai.sh [URL]` | 任何 OpenAI-compatible runtime 的相容性探針。預設指向 Ollama 的 `/v1` |
| `bash scripts/connect-endpoint.sh --url URL --check` | 接上 GPU runtime **之前**先驗證相容性，不做任何變更 |
| `bash scripts/connect-endpoint.sh --url URL` | 接上一個 OpenAI-compatible runtime（先驗證、後接線、再回讀確認） |
| `bash scripts/connect-endpoint.sh --status` | 目前接的是哪一個 runtime |
| `bash scripts/connect-endpoint.sh --disconnect` | 切回只有 Ollama |
| `bash scripts/rotate-endpoint-key.sh --check` | 比對那把持久 endpoint 金鑰住的**三個**地方 —— config yaml（真本，boot 時烘進 notebook）、`.env`、Open WebUI 的資料庫 —— **只印指紋，不印金鑰**。不改任何東西；不一致時回 `2`（所以能當閘門用） |
| `bash scripts/rotate-endpoint-key.sh` | 把三個地方**一次**換成同一把新的（會先全部讀完才動手）。要打 `yes` 確認，`--yes` 可略過。只換兩個會留下一個拿著死金鑰、**且沒有任何症狀**的持有點，所以只要有讀不到的持有點，整支就停下來（回 `2`） |
| `bash scripts/apply-endpoint-ntfy-fixes.sh --dry-run` | 檢查 endpoint 產生器裡的兩項 ntfy 修正是否需要、以及補丁還套不套得上。不做任何變更 |
| `bash scripts/apply-endpoint-ntfy-fixes.sh` | 對那個產生器套補丁（備份 → 雜湊關卡 → 套用 → 重驗）。`--revert` 還原。**動的是 package manager 的檔案：升級 `endpoint-vps` 就會蓋掉** |
| `python3 scripts/test_endpoint_ntfy_fixes.py FILE` | 上面那支的行為驗證 —— 驅動產生器**實際吐出的字串**，跑在模擬的 ntfy token bucket 上。**對原始檔是預期要失敗的**，那個失敗就是缺陷的展示（D-050） |
| `bash scripts/apply-endpoint-stop-fixes.sh --dry-run` | 檢查 `endpoint stop` 的靜默 no-op 還在不在、以及補丁還套不套得上。不做任何變更 |
| `bash scripts/apply-endpoint-stop-fixes.sh` | 對 **CLI 本體**套補丁 —— **兩個檔案**（`core.py` ＋ `commands.py`），所以釘四個雜湊、共用一個備份章節。兩個檔案狀態不一致時會拒絕。`--revert` 一次還原兩個。**動的是 package manager 的檔案：升級 `endpoint-vps` 就會蓋掉** |
| `python3 scripts/test_endpoint_stop_fixes.py ROOT` | 上面那支的行為驗證 —— 用 AST 把 `get_kernel_status`／`run_stop`／`run_boot` 的清舊迴圈切出來，餵假物件驅動（不碰網路、不寫快取）。**對原始樹是預期要失敗的**（D-060） |
| `bash scripts/apply-endpoint-apikey-broadcast-fixes.sh --dry-run` | 檢查引擎是否還把 API 金鑰廣播到那條公開的 ntfy 主題、以及補丁還套不套得上。不做任何變更 |
| `bash scripts/apply-endpoint-apikey-broadcast-fixes.sh` | 停掉那則廣播 —— 一個檔案 `engine/engine.py`，它是 notebook 產生器 **base64 嵌入的生產者**（`master_build_notebook.py:331`）。`--revert` 還原。**動的是 package manager 的檔案：升級 `endpoint-vps` 就會蓋掉** |
| `python3 scripts/test_endpoint_apikey_broadcast_fixes.py ROOT` | 上面那支的行為驗證 —— 對捕獲的傳輸層**真的執行** `_startup()`，斷言送出的 POST 內容不含金鑰，外加「刪掉廣播不會卡住 boot」的 AST 守衛。**對原始檔是預期要失敗的**（D-067）。它自己的檔頭被第四刀合法編輯過，D-076 因此重 pin 了它的雜湊 —— 而重 pin 是靠量測撐著，不是靠改數字 |
| `bash scripts/test_endpoint_apikey_broadcast_fixes_mutants.sh TREE [--pristine TREE2]` | 上面的驗證器真的有在被執行 —— **5 條突變，每一條都指名「必須抓到它的那一條檢查」**，外加 1 條**不准叫**的守門，以及（給了 `--pristine` 時）1 條只有**早於切 A 的樹**走得到的突變：檢查 B 的第二條路徑。沒給 `--pristine` 時它**會出聲講**「這條路徑這一輪沒被測到」，不是靜靜跳過。結束碼 `0` 全抓到、`1` 有漏、`2` 用法錯誤、`3` 表格寫壞。**它有一條明講沒涵蓋的斷言**（`TUNNEL ACQUIRED:` 分支必須 `break` —— 那個 `break` 在 `run_boot` 裡不唯一，單行錨點寫不出來），寫在檔頭（D-076） |
| `bash scripts/apply-endpoint-tunnel-url-privacy.sh --dry-run` | 檢查 tunnel 網址是否還發布到那條公開的 ntfy 主題上、以及補丁還套不套得上。不做任何變更 |
| `bash scripts/apply-endpoint-tunnel-url-privacy.sh` | 把那個網址從主題上拿下來 —— 這次是**三個檔案**（`scripts/master_build_notebook.py`、`endpoint/core.py`、`endpoint/commands.py`），所以釘六個雜湊、共用一個備份章節。網址不再被發布，改成從**Kaggle kernel log** 讀回來，而讀它需要 Kaggle 憑證。⚠ **順序有關係：ntfy 修正 → stop 修正 → 這一支**，而對前兩支任一執行 `--revert` 會**無聲地**把這一支拆掉。`--revert` 還原。**動的是 package manager 的檔案：升級 `endpoint-vps` 就會蓋掉** |
| `python3 scripts/test_endpoint_tunnel_url_privacy.py ROOT` | 上面那支的行為驗證 —— 用假物件驅動新的日誌讀取器，把 `found`／`absent`／`unknown` 釘成彼此互異（後兩者塌在一起正是 D-060 的缺陷），斷言 `get_tunnel_url` 已經沒有呼叫點，並讀產生器吐出的字串。它還會**重算切 A 驗證器的雜湊**：這一刀如果碰過它，那一項就會紅。**對原始樹是預期要失敗的**（D-068） |
| `python3 scripts/probe_kernel_log_url.py` | 對真的 Kaggle API 問：日誌裡到底有沒有一個讀得出來的 tunnel 網址 —— 這是切 B 賴以成立的前提。**永不印日誌內容、網址、權杖或主題**：只印狀態碼、布林、計數，以及網址的**單向指紋**（供兩次執行比對而不揭露任何一次）。結束碼 `0` 找到了、`1` 讀到了但還沒有網址、`2` 根本讀不到、`3` 用法錯誤 |
| `bash scripts/set-endpoint-topic-secret.sh` | 產生（或輪替）ntfy 主題名所依據的**本機秘密**。**永不印值** —— 只印 `sha256:<12>` 指紋 —— 且**永不接受從命令列傳值**（argv 是 `ps` 看得到的）；它自己產生，或用 `--stdin` 從 stdin 讀一行。**不建備份**，所以被換掉的秘密在這台機器上不可回復；復原路徑是 `endpoint kill-all --yes` 或 Kaggle UI。**有 kernel 還在跑時不要執行**（順序見下）。`--check` 只報告不改（沒有可用秘密時回 `2`，所以能當閘門用）；`--dry-run` 印一個指紋，什麼都不寫 |
| `bash scripts/apply-endpoint-topic-secret.sh --dry-run` | 檢查主題是否還由**公開的 Kaggle 帳號**推導、補丁還套不套得上、以及**這台機器上有沒有一個形狀正確的 `signal.topic_secret`**。不做任何變更。今天對真安裝目錄是**預期要回 `1`** 的 —— 切 C 疊在**切 B 之上**，而 B 還沒套 |
| `bash scripts/apply-endpoint-topic-secret.sh` | 讓主題不再由公開帳號推導 —— 這次是**五個檔案**（`endpoint/core.py`、`endpoint/commands.py`、`scripts/master_build_notebook.py`，外加兩個隨套件出貨的資料檔 `endpoint/data/endpoint-config.example.yaml` 與 `endpoint/data/endpoint.1`），所以釘**十個**雜湊、共用一個備份章節。主題變成一個本機秘密的函式，因此它不再被印到任何地方。⚠ **它的秘密閘門正是讓「補丁已套但沒有秘密」這個狀態不可達的東西 —— 形狀不對就拒絕執行，所以**沒有** `--force`。** ⚠ **順序有關係：B 必須先套** —— 切 C 的原始雜湊逐字等於切 B 的修補後雜湊。`--revert` 還原。**動的是 package manager 的檔案：升級 `endpoint-vps` 就會蓋掉** |
| `python3 scripts/test_endpoint_topic_secret.py ROOT` | 上面那支的行為驗證 —— **十三道檢查（D1–D13）**，用 AST 把推導切出來、**餵假物件 exec 而不是 import 套件**，所以跑的是**被測的那棵樹**。它釘住黃金值、兩個隨套件出貨的資料檔、切 B 的驗證器（`e8e2259e…`）與 `engine/engine.py`（`68dfa5a2…`），並且把秘密閘門**透過一棵反向套用過的原始樹**驅動起來。**對原始樹是預期要失敗的：23 項**（D-069） |
| `bash scripts/test_endpoint_topic_secret_mutants.sh TREE` | 上面的驗證器真的有在被執行 —— **13 條突變，每一條都指名「必須抓到它的那一條檢查」**（D11 在每一條上都會紅，所以「變紅了」本身不帶資訊），外加 3 條**不准叫**的守門。其中兩條突變是電池實測抓到的**真** bug，不是假想出來的。結束碼 `0` 全抓到、`1` 有漏、`2` 用法錯誤、`3` 有一條**根本沒植入**（清單過期**不是**驗證器的洞，訊息也這樣講）（D-069） |
| `bash scripts/apply-endpoint-boot-honest-outcome.sh --dry-run` | 檢查 boot 那兩句被實測推翻的處方、以及「沒接線卻 exit 0」還在不在，以及補丁還套不套得上。不做任何變更 |
| `bash scripts/apply-endpoint-boot-honest-outcome.sh` | 讓 boot 不再宣稱一件它沒做到的事 —— 一個檔案 `endpoint/commands.py`，**每一處改動都在 `run_boot` 這一個函式內**（把修補前後每個函式的 AST dump 出來比對得到的，不是讀 diff 看出來的）。原本：隧道起來但網址讀不到時，它印出「等一下就有了」與「用 `endpoint base-url` 讀」（後者走的就是那條讀不到的路），而 `_register_with_proxy` 從未執行、proxy 從未被通知 —— boot 卻仍印 `Endpoint IS ONLINE` 並 `exit 0`。現在：講明 endpoint **沒有接線**，並以 **exit 2** 收尾（`1` 仍專屬「kernel 沒起來」）。⚠ **這只修說法，不修缺陷** —— 網址仍然拿不到（切 B 未解），所以**從此每一次 boot 都會 exit 2**，那是預期效果。⚠ **順序有關係：它的原始雜湊是「A／B／C 與第四刀都套好之後」的狀態** —— 前面任一支 `--revert` 之後，這一支會**拒絕執行**（大聲拒絕，不是靜默）。`--revert` 還原。**動的是 package manager 的檔案：升級 `endpoint-vps` 就會蓋掉** |
| `python3 scripts/test_endpoint_boot_honest_outcome.py ROOT` | 上面那支的行為驗證 —— **五道檢查（A–E）**，用 AST 剖析而**不 import**（import 會讀使用者的真設定，而套用腳本每一輪要跑它兩次）。A 釘住兩句假處方必須消失；B 是**正對照**（分支仍要出聲，堵住「把整個分支刪掉讓 A 變綠」）；C 是 **D-033 護欄**（`success = True` 必須仍是無條件、且是字面上的 `True` —— 改成 `False` 等於把為真的事實改成假的失敗）；D 釘住收尾必須有條件、且在成功路徑上；E 釘住兩個離開碼各守本分。**對原始檔是預期要失敗的：A、D、E 三項**（D-073） |
| `bash scripts/lock-signup.sh` | 驗證註冊是否真的關著；若開著，透過設定 API 關閉 |
| `bash scripts/lock-signup.sh --check` | 只驗證，不做變更。註冊開著時結束碼非 0 |

接上 GPU runtime（Kaggle + Endpoint，或 VPS + vLLM）另有一份專門的指南：
[`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md) · [`docs/ENDPOINT.md`](docs/ENDPOINT.md)。
Kaggle 的實際操作手冊在[下面](#kaggle-實作手冊)。

### 證據腳本

這幾支之所以存在，是因為本專案已經兩次交出「回報成功但其實沒作用」的修正。
它們會真的開容器、真的打端點，觀察真實行為 —— 動到它們涵蓋的東西時請重跑。

| 指令 | 證明了什麼 |
|---|---|
| `bash scripts/verify-lock-signup.sh` | `ENABLE_SIGNUP` 在第一次開機之後就是無效的 —— 三次開機共用一個 volume（約 25 分鐘） |
| `python3 scripts/signup_control_probe.py URL` | 真正控制註冊的是哪條路徑，並以實際打端點驗證 |
| `python3 scripts/verify_lock_signup_script.py URL .` | `lock-signup.sh` 真的關得掉、可重複執行、且失敗時會吵 |
| `bash scripts/verify-first-admin.sh` | `ENABLE_SIGNUP=false` 不會擋住你建立第一位管理員 |
| `bash scripts/verify-langgraph-tools.sh` | 工具呼叫跨得過換到 LangGraph 的 code path，且判準是 `call_id` 鏈而不是「答案讀起來對」 |
| `bash scripts/test_langgraph_tools_probe_mutants.sh` | 上面的評分器真的有在被執行 —— 對它自己的 11 道判準做突變，每一個都必須被抓到 |
| `bash scripts/verify-chroma-dims.sh` | mem0 會沿用預先建立的 ChromaDB 集合，而不是跟它對抗；以及 embedding 維度以哪個 metadata 鍵為準（D-026） |
| `bash scripts/test_chroma_dims_probe_mutants.sh` | 上面的評分器真的有在被執行 —— 對它自己的 24 道判準做突變，每一個都必須被抓到 |
| `bash scripts/verify-mem0-add-cost.sh` | mem0 的 `add()` 確切只多付一次 LLM 呼叫，而且在預設 `num_ctx` 下那一次讀不到自己的指令（D-027）。**此後三組對照組把這個問題收掉了**：生成之前的截斷（D-034）與生成期間的 context shift（D-035）都被**排除**，而**生成預算被正面確認就是成因**（D-036）—— `num_ctx` 維持 16,384，`num_predict` 由 2,000 開到 8,000 之後抽出 2 則記憶，原本是 0 則，且 `done_reason=stop`。這支腳本現在會讀容器的 `OLLAMA_CONTEXT_LENGTH` 並以 `--ctx` 交給探針，所以預算是**推導**出來的（`num_ctx − 8192`）而不是寫死的，C6／C7 則是那條推導兩個前提的哨兵（D-037）。那兩條**在分段的 `--sections add` 輪次裡也照判** —— 缺節的提早返回原本會把它們一起吞掉，於是哨兵在唯一會被用到的組態裡是安靜的（D-040）。**2026-09-23 那一輪真的跑了**（Stage B）：推導預算 8,192 之下第一次 `add()` 抽出 **2 則記憶**、`done_reason=stop`、`prompt token=8052`，而判準段沒有 C6／C7 的問題行 —— 那是那個修法**唯一在輸出上看得出來的時機**（D-040 第七節的結果段）。**同一晚接著跑完整五節**（Stage C，`num_ctx=4096` —— 閘門自己開的處方）：`rc=0`、**C1~C7 全過**、54 分鐘，而且每一個量得到的值都**逐位重現 D-027**（`2050`／`8047`、夾縫 `687`／`346`／`687`、canary 頭端看不到、thinking ≥185 個 token）。它另外買到兩件事：ollama 日誌的 `prompt=` 給出了**真實** `add()` 的長度（8,052 與 8,100），與探針**重組**的 8,047 **同一輪並存** —— 第一次量到那個 5 個 token 的差距（D-042 第四節原本把它假設掉了）；以及兩次 `add()` 之間那個 `+48` 逐位重現，所以那個沒有解釋的差異是**穩定的**，不是雜訊（D-044）。⚠ **這一支的完整五節在出貨設定下跑不起來**：C2／C3 的前提是「伺服器預設值小到會截斷」，而 16,384 讓它不成立，所以不帶 `--sections` 的完整輪次**每次都會回 2**（那是設計，不是壞掉）；`num_ctx` 調到 **8,048** 以下才是那兩條判準有定義的組態 —— 8,048 就是 C2 自己的 `min_ctx_for_extraction`，也就是探針**重組**那份 prompt（8,047）＋ 1，那才是 C2 真正送出去的物件 —— 而那道閘門的門檻是 8,101，中間 **8,048～8,100** 會穿過去並回一個**假失敗 1**（兩半、兩半都是假的：8,048～8,052 是真實 `add()` 的 prompt **還在被砍**而 C2 的儀器短了 5 個 token 看不到；8,053 之後才是前提單純不成立）；同一個常數也往另一邊錯 —— 某一輪的第一份抽取 prompt 只要到 8,101 個 token，前提其實還成立，閘門卻回 2。這是**界**而不是現行缺陷（那份 prompt 的實測最大值是 8,100），但兩邊合起來才是完整的形狀（D-043） |
| `python3 scripts/test_mem0_add_cost_probe.py` | 上面那些判定在離線時就有區辨力 —— 不需要 Docker、不需要 ollama —— 包括 `#58` 換掉的那個四態截斷主張（D-036 第 11.1 節） |
| `bash scripts/test_mem0_add_cost_probe_mutants.sh` | 上面的評分器真的有在被執行 —— 對它自己的 109 道判準做突變，每一個都必須被抓到 |
| `bash scripts/test_probe_traceability.sh` | **即時 bind-mount** 進容器的探針，可以被證明在執行前後是同一個檔案 —— 探針沒有烤進映像（四個腳本都這樣掛它，例如 `verify-mem0-add-cost.sh` 的 `-v "$PROBE:/probe/mem0_add_cost_probe.py:ro"`），所以映像標籤不涵蓋它，而它中途被換掉時產出的「通過」追不回任何一個修訂版 —— 追回那件事曾經只能靠一個殘留的 `.pyc`（D-027 第十節、D-036 第 11.2 節） |
| `bash scripts/test_lib_running.sh` | `lib.sh` 裡取代掉管線的那幾個輔助 —— `line_in_list` 與騎在它上面的三個述詞（`container_running` / `service_running` / `model_in_ollama`），以及 `first_line` —— 意思跟它們取代掉的形式一樣（整行相等、空名稱＝不在、glob 字元是字面值、表頭不是模型），而且**在那些形式答錯的輸入上答對**：`set -o pipefail` 之下 `docker ps … \| grep -qx` 會死於 SIGPIPE，於是回報「容器沒有在跑」而它明明在跑。這份檔案的控制組就是拿舊形式對同一個假 docker 跑，看著它誤報（D-038） |
| `bash scripts/test_verify_mem0_add_cost_guard.sh` | 上面那個守衛真的**接上去了**，而且是**四支**會即時掛載探針的腳本（`verify-mem0-add-cost` / `-chroma-dims` / `-langgraph-tools` / `-phase3-runtime`）：`changed` 蓋得過 `--sections` 的結束碼 2，而真正的 2 不會被誤報；雜湊讀不到時只警告不失敗（D-016）；「結束碼 2 是預期的」這句在 rc=3 時不會再印出來；以及讀得到的 `OLLAMA_CONTEXT_LENGTH` 會以 `--ctx` 抵達探針，讀不到時是**完全不給旗標**加一則警告（D-037、D-038 第六節） |
| `bash scripts/test_ollama_log_corroboration.sh` | 伺服器端日誌的旁證會把「儀器壞掉」與「這一輪沒有截斷」講成兩句不同的話。它的涵蓋檢查以前正是下面那個缺陷最壞的例子 —— `set -o pipefail` 之下 `printf \| grep -q` 的形式死於 SIGPIPE，於是**偏偏在涵蓋範圍最大的時候**回報「涵蓋不到」。現在它是 shell 的模式比對，而判準被講成它真正的樣子：**讀者離開之後，生產者還會不會再寫**（D-034，於 D-038 第七節更正） |
| `bash scripts/test_usage_text.sh` | 每一支腳本的 `--help` 只印**它自己的檔頭**、不印別的，而且「印到哪裡停」的判準是**內容**（一直印到第一行非註解、非空行）而不是位置。原本用位置決定的 9 支裡：**4 支把程式碼當說明印出來**（`source …`／`require_docker`）、**1 支在截斷**（`deploy-vps.sh` 的 110 行檔頭只印了 18 行）、**4 支只是剛好對** —— 而「剛好對」才是危險的那一半：它不會失敗，所以也不會有人說什麼。C 節在暫存專案根裡（接假 docker）真的跑 11 支的 `--help`，與**獨立推導**出來的檔頭（另一種算法）逐字比對；D 節掃描寫死行號有沒有回來、以及有沒有一個 `usage_text` 呼叫搆不到定義 —— 這次改動的第一版就是在 `lib.sh` 載入之前呼叫它，結果**什麼都不印、結束碼還是 0**（D-039） |
| `bash scripts/deploy-vps.sh --dry-run` | 一次佈署會寫哪些東西進 `.env`，以及這台機器的磁碟／記憶體夠不夠 —— 不變更任何東西。D-058 之後它印的磁碟數字**只含無條件需要的那一份**（映像 ＋ 餘裕），所以模型那一份會另外講，而表外模型會被**指名為不知道**，不會被混進那個數字裡 |
| `bash scripts/test_deploy_vps_decisions.sh` | 綁定位址政策、暴露閘門的四個狀態、資源門檻、`.env` 寫入器、架構三層與**兩階段的磁碟閘門**，每一項都有「該過的過」與「該擋的擋」—— 245 個案例。架構那幾條讀的是**真實抓下來的 `docker manifest inspect` JSON**，包含兩個坑：單一平台的 manifest **完全沒有** `architecture` 欄位（讀成「沒有」會擋掉一台容器已經起來的機器），以及 `arm` 不可以誤中 `arm64`（D-057）。磁碟那幾條釘住兩個「往寬鬆那邊錯」的方向：表外模型必須維持 `unknown`、**絕不可以回一個數字**（D-055 §3(2) 的紅線），以及明確傳進來的空字串 `will_download` 要算成「會下載」，不是重跑（D-058） |
| `bash scripts/test_deploy_vps_decisions_mutants.sh` | 上面的評分器真的有在被執行 —— 對三個 bash 模組與煙霧探針做 121 道突變，每一個都必須被抓到。架構那組最貴的兩條：`arm64 → verified`（宣稱這個 lab 在 ARM 上驗證過）與 `不等時回 native`（讓 qemu 模擬執行**整個靜默**）。磁碟那組的三條骨幹：表外模型靜默回一個數字、映像清單讀不到被當成「沒有東西要拉」，以及第二階段從 `exit 1` 退化成 `return`（呼叫端會把它變成「無法判定」）（D-058） |
| `bash scripts/verify-phase3-runtime.sh` | `recursion_limit` 算的是 super-step（+1）、太緊的 limit 會在工作全部做完之後才中止、以及持久化的 job store 在 1 秒預設寬限下仍會安靜地丟掉到期的 job（D-030） |
| `bash scripts/test_phase3_runtime_probe.py` | 上面那些判定在離線時就有區辨力 —— 不需要 Docker、不需要 langgraph —— 而且每個邊界都是獨立一個案例 |
| `bash scripts/test_phase3_storage_decisions.sh` | 儲存判定分得出 named volume、bind mount 與容器的暫存目錄，而且 `unknown` 永遠不會被讀成 durable |
| `bash scripts/test_phase3_mutants.sh` | 上面三個評分器真的有在被執行 —— 對它們自己的 23 道判準做突變，每一個都必須被抓到 |
| `python3 scripts/test_deploy_smoke_probe.py` | 佈署後煙霧測試的四條斷言各自會咬人，包含它存在的理由本身：被 token 上限切斷的生成讀起來像成功 |
| `bash scripts/test_profile_lifecycle.sh` | 殘留容器的**差集**兩個方向都不能錯 —— 要回報被停用 profile 留下的那個，**且**不能砍掉還在服務的容器 —— 外加空清單守衛與對外連線的四態（D-029） |
| `bash scripts/verify-throughput.sh` | 七個條件的解碼速率矩陣、**兩組** `num_ctx` 下的截斷上限實測、以及按模型分組的 KV cache 斜率 —— 而且當七次重複彼此不一致時，它**拒絕把任何數字叫做基準線**（D-031、D-032、D-033）。它的 prefill 欄是刻意標成不可引用的：探針控制不了 prefix cache 的重用，所以它報 306–14,710 t/s，而真實的冷 prefill 約 25 t/s。**D-033 查出基準線不只是「還沒量到」，是「到不了」**：長 prompt 那一臂的取樣次數被寫死成 2，而穩定度規則要求 ≥ 5，所以判定對**任何**可能的執行都是 `unstable` —— D-031 與 D-033 兩輪在跑之前就註定失敗，而「機器吵」只講對了一半。**這一條已經修好**（那一臂現在和其他臂取一樣多的樣本；D-033 第五節），它讓判準**可滿足、不等於被滿足**：真正吵的那 5 個條件完全沒被碰到，所以下一輪仍然可能合理地失敗 |
| `bash scripts/test_throughput_probe.py` | 上限公式、截斷判定、「上限會不會隨 `num_predict` 移動」的判定、以及 KV 分組，每一個都會咬人 —— 259 項斷言，不碰 Docker。**它最後一節與上面每一條都不同類**：它把一個 stub client 餵進**真的 `measure()`**，斷言的是**探針自己組出來的那份矩陣**。上面測的全是法官，那一節測的是**法官實際會拿到的那個案子**（D-033）。裡面有一條專案**從來沒觀測過**的 `num_keep`，因為一個在每個已觀測輸入上都等價的化簡，靠觀測是殺不掉的 |
| `bash scripts/test_throughput_probe_mutants.sh` | 上面四個判定真的被操到 —— 73 條針對自身準則的突變，每一條都必須被抓到。其中有兩條守的是**接線**而不是判定：長 prompt 臂的取樣次數改回寫死的 `2`，以及它的一般化形態（取樣器自己靜默夾住次數）。突變台自己的輸出也是一句斷言，所以它也被檢查：準則不只要被取代**弄壞**，還要壞在**有斷言會叫**的地方 —— 一個 no-op 的取代、被切錯的欄位、或植入後語法就不合法，三者都會讓測試「失敗」，但什麼都沒守住（D-032） |

---

## Kaggle 實作手冊

這裡是**照著做**的操作手冊：從一個空的瀏覽器，做到一個能用的 GPU runtime。
**為什麼**、**安全取捨**、以及**疑難排解表**在
[`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md) —— 這一節不重複它們。

**你會得到**：第二個 LLM runtime，跑在 Kaggle 的免費 GPU 上（T4 ×2，約可到 70B
參數），以 OpenAI-compatible 端點的形式暴露出來。它上面的每一層只看到一個
`--base-url` 的差別。
**代價**：不用錢。它消耗 Kaggle 的 GPU 額度（30 小時/週），而且 runtime 會自己消失。

> **已經設好一部分了？** 兩條指令告訴你現在站在哪裡：`endpoint doctor`（設定讀得到
> 嗎？）與 `bash scripts/apply-endpoint-ntfy-fixes.sh --verify`（notebook 產生器補丁
> 還在嗎？）。下面第 1–4 步是一次性的。

### 第 1 步 —— 申請 Kaggle 帳號（一次）

1. 到 <https://www.kaggle.com> 註冊（Google 或 email）。
2. **完成手機驗證** —— <https://www.kaggle.com/settings> → *Phone Verification*。

   **這是第一次最常見的卡點。** 沒有驗證過的手機，Kaggle 不給你 API 存取、不給
   GPU 加速器、也不給 Internet 開關 —— 而且它**不會當場報錯**，是之後才失敗，
   長成「boot 跑完但網址一直不出現」。
3. TPU 另外需要 *persona/identity* 驗證。**T4 ×2 不需要。**

### 第 2 步 —— 取得 API 權杖（一次）

<https://www.kaggle.com/settings> → **API** → **Create New Token**。

- 開頭是 `kgat_`。舊式的 *username + key* 組合**已經不被接受**。
- 放進你的 shell rc 檔 —— **絕不進這個 repo**：

  ```bash
  # ~/.bashrc 或 ~/.zshrc
  export KAGGLE_API_TOKEN='kgat_xxxxxxxxxxxxxxxx'
  ```

> **那個權杖是一份憑證。** 本 repo 的 `.gitignore` 涵蓋 `.env`，**涵蓋不到你的
> shell rc 檔**。貼到任何地方之前先想一想。

然後**開一個新的終端機**。許多發行版的 `.bashrc` 對非互動 shell 會提早 return，
所以 `bash -c '...'` 看不到那個變數，即使你的提示字元看得到。

### 第 3 步 —— 安裝 CLI 並設定（一次）

```bash
uv tool install endpoint-vps   # 套件叫 endpoint-vps，執行檔叫 endpoint
endpoint init                  # 互動式：Kaggle 使用者名稱、kernel slug、預設模型
endpoint doctor                # 確認設定真的被讀到了
```

`endpoint --help` 會列出所有加速器與指令。`init` 與 `boot` **都是互動式的**，
不能接進排程、CI 或任何非互動的工具裡。

### 第 4 步 —— 在**第一次 boot 之前**修補 notebook 產生器

這一項是本 repo 專屬的，而且不是選配。`endpoint boot` 跑的 notebook 是已安裝的
套件**產生**出來的，而那個產生器有兩個缺陷（D-050），會讓一次健康的開機看起來
像死掉的。

```bash
bash scripts/apply-endpoint-ntfy-fixes.sh --dry-run   # 只檢查，不動任何檔案
bash scripts/apply-endpoint-ntfy-fixes.sh             # 備份 → 雜湊關卡 → 套用 → 重驗
bash scripts/apply-endpoint-ntfy-fixes.sh --verify    # 驗行為，不只是驗雜湊
```

跳過它的症狀是：**boot 成功，但 tunnel 網址一直不出現**，而 kernel 會在約
32 分鐘後自殺。

### 第 5 步 —— 起飛

```bash
endpoint -g boot
```

- **`-g` 要放在 `boot` 前面。** `endpoint boot --gpu` 會失敗。
- `-g` = **GPU T4 ×2**。其他：`endpoint boot`（CPU）、`endpoint -t boot`（TPU v5e-8）。
- **它會問你要佈署哪個模型。** 那是刻意的；`--no-watch` 是「不要串流狀態」，
  **不是**「不要問問題」。
- 大概要 **5～10 分鐘**：Kaggle 開機、建 `llama.cpp`、載入模型。

它跑的期間，用瀏覽器打開 Kaggle：

```
https://www.kaggle.com/code/<你的帳號>/<你的-kernel-slug>
```

這些是 `boot` 透過 Kaggle API 設定的 —— 你應該不需要自己按任何東西。它們是
出問題時要去看的地方：

| 在哪裡 | 應該要是 |
|---|---|
| 右上 **Accelerator** | **GPU T4 ×2** |
| **Settings → Internet** | **On** —— 沒有網路就沒有 tunnel |
| **Input → Datasets** | 掛著模型資料集 |

cell 輸出出現 **`TUNNEL ACQUIRED`**，就是網址到手了。

### 第 6 步 —— 拿網址與金鑰

```bash
endpoint base-url
```

你要兩樣東西：一個結尾是 `trycloudflare.com` 的網址（後面加 `/v1`），以及 API
金鑰。把金鑰放到本 repo 腳本會讀的地方 —— **`.env`，它已被 gitignore**：

```bash
ENDPOINT_API_KEY=<貼在這裡>
```

那把金鑰最後會住在**三個**地方，而只有其中一個會自我修復。config yaml
（`~/.config/endpoint/endpoint-config.yaml`）是真本，每次 `boot` 都會把它的複本
——**明文**——烘進推上 Kaggle 的 notebook。repo 的 `.env` 與 Open WebUI 的資料庫
拿的是**硬拷貝**，沒有任何東西會更新它們；它們只會安靜地過期，然後很久以後以
401 的樣子出現。

```bash
bash scripts/rotate-endpoint-key.sh --check   # 三處還一致嗎？
bash scripts/rotate-endpoint-key.sh           # 一次換掉三處
```

要在 boot **之前**輪替，不是之後。輪替是本機動作 —— 它不碰 Kaggle。舊金鑰在
已經推上去的那個 notebook 裡仍然是活的，直到下一次 boot 把它換掉；而那個
notebook 雖然是私有的，它仍然是別人伺服器上的明文。

### 第 7 步 —— 接線**之前**先驗證

```bash
bash scripts/connect-endpoint.sh --url https://<tunnel>.trycloudflare.com/v1 --check
```

`--check` 只跑相容性探針，**不做任何變更**。這個順序就是重點：先接再測的話，
失敗時你已經把平台唯一的模型來源指向一個壞掉的服務了 —— 而 Open WebUI 不會
因此抗議，它只會顯示一個空的模型清單。

### 第 8 步 —— 接上

```bash
bash scripts/connect-endpoint.sh --url https://<tunnel>.trycloudflare.com/v1
```

腳本會先跑探針，**未通過就拒絕繼續**；它把 `openai.enable` /
`openai.api_base_urls` / `openai.api_keys` 寫進**資料庫** —— 不是 `.env`，那在
第一次開機之後就是無效的（D-017）—— 重啟 Open WebUI，然後用**應用程式自己的
`Config.get_many` 路徑**讀回設定，而不是回頭讀自己剛寫的那一列。

### 第 9 步 —— 只有你能做的那一步

**打開 <http://localhost:3000>，看模型選單。**

Open WebUI 對設定的端點是**延遲抓取** —— 只有在已登入的使用者打開模型清單時
才會發出請求。2026-09-19 用誘餌服務驗證過：重啟後 `openai.enable=true` 且指向
誘餌，**在有人要求模型清單之前，一個請求都沒有到達**。所以腳本證明得了
「**設定**指向你的 runtime」，證明不了「**模型**真的抓到了」。那只有你親眼看到
才算。

沒出現的話：`docker compose logs --tail=50 open-webui`。

### 第 10 步 —— 收工

```bash
endpoint stop
```

**要看它的結束碼，不是只看它的輸出。** `0` 是停好了（或確認本來就沒有在跑）；
**`2` 是 kill 訊號送出去了、但狀態從頭到尾讀不到** —— 而在非互動 shell 底下這
是常態：Kaggle 權杖住在你的 shell rc 檔裡，非互動 shell 不會讀它。`stop` 以前
會把這種情況叫做「No running kernel found.」並以 0 結束，而 GPU 繼續燒；現在不
會了（[D-060](DECISIONS.md)，以及 `scripts/apply-endpoint-stop-fixes.sh`）。
拿到 `2` 之後要確認，就在有權杖的 shell 裡跑 `endpoint status`，或看 Kaggle 網頁。

`endpoint kill-all` 會終止**帳號上所有在跑的 Kaggle kernel** —— 當你有一台手動
開的 notebook 正在默默吃額度時它很有用，而那也正是它是散彈槍而不是步槍的原因。

### 會咬人的地方

| 風險 | 它實際上是什麼 |
|---|---|
| 額度 | GPU T4 ×2 = **30 小時/週**、單次 session 約 **12 小時**、**閒置 60 分鐘會自己關機**釋放 GPU。而且只算**成功的推論** —— 用 `/v1/models` 輪詢**不會**續命。 |
| 網址每次都不一樣 | Quick Tunnel 的網址是隨機的。那是隱蔽性，**不是認證**：任何知道網址**和金鑰**的人都能用你的 GPU。不要在任何東西上寫死它。 |
| 升級 `endpoint-vps` 會蓋掉第 4 步 | 那個補丁動的是 package manager 的檔案。每次升級後重跑 `--verify`。 |
| `endpoint status` 說 `offline` | 它會說謊 —— 狀態檔說 kernel 死了，runtime 其實還在答話。改成去探端點。 |
| `boot` 成功但沒有網址 | 幾乎都是沒套補丁的限流（第 4 步），或 Internet 開關沒開。 |
| `GET /v1/apikey` | 上游文件提到這條路由。**是否需要認證，本專案尚未驗證** —— 若不需要，光有網址就足以取得金鑰。啟動後自己用 `curl` 驗一次。 |

**使用條款。** Kaggle 的條款把服務限定為個人、非商業用途。那讓 Kaggle 成為
「**量測大模型能跑到什麼程度**」的正確工具，而**不是**公司內部助理的產品路徑。
完整的判讀 —— 包括那些條款裡哪些查證過、哪些沒有 —— 在
[`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md)。

---

## 對外連線的安全性

### 先看資料會往哪裡**出去**

安全性有兩個方向，而本專案原本只寫了其中一個。「誰能連進來」是埠與
Cloudflare Access 的問題（見下方）；「**資料會流向哪裡**」是另一個問題，
而且在 2026-09-19 實測之前，本專案一直是錯的：

```
$ bash scripts/check-egress.sh --check
  ✗ 啟用中  openai.api_base_urls
       https://api.openai.com/v1
       開關：openai.enable = True                    EXIT=1
```

Open WebUI 的 `ENABLE_OPENAI_API` 預設是 `True`，而 `OPENAI_API_BASE_URLS`
的預設空字串**不是「關閉」而是 fallback**，會被填成 `https://api.openai.com/v1`。
兩者相加：一個以「資料不出公司」為目標的堆疊，**預設就接上了 OpenAI 官方 API
而且是開著的**。而 `OPENAI_API_KEY` 是很多開發機上全域匯出的環境變數 ——
一旦有，`gpt-4o` 就會出現在模型選單裡。

修復：`bash scripts/check-egress.sh --fix`（寫入 → 重啟 → 回讀確認）。
**注意改 `.env` 對現有部署沒有用** —— 環境變數只是第一次開機的種子，
之後 Open WebUI 以資料庫為準（與 `ENABLE_SIGNUP` 完全相同的形狀）。
決策與實測見 [`DECISIONS.md`](DECISIONS.md) D-017。

這支探針回答的是「**設定上允許**資料去哪裡」，不是「資料實際去了哪裡」。

---

**第一階段沒有私有資料，所以下面這一節還不重要。會改變這件事的是 RAG** ——
一旦你上傳自己的文件，瀏覽器與模型之間的傳輸就成為值得保護的一環。
請在**上傳任何文件之前**完成。決策記錄見 [`DECISIONS.md`](DECISIONS.md) D-012。

未經認證的 Open WebUI 不只是聊天視窗。能觸及它的人可以讀取已儲存的所有對話、
使用你的模型，而且 —— 若 `ENABLE_SIGNUP` 仍是 `true` —— **自行註冊帳號**，
其中第一位註冊者會成為管理員。

### 三選一

| 方案 | 身分驗證 | 需要網域 | 說明 |
|---|---|---|---|
| **Codespaces 私有埠**（預設） | GitHub 帳號 | 不需要 | 零設定。只有你、且經過 GitHub 認證才連得到。**這是基準，而且已經是開啟的。** |
| **Cloudflare Tunnel + Access** | Email OTP / Google / GitHub SSO | 需要 | 固定主機名，codespace 重建後仍可沿用。Zero Trust 免費層可涵蓋 50 位使用者。 |
| **Tailscale** | 裝置 | 不需要 | 驗證層級是「裝置」而非「使用者」；若你的用戶端都能加入同一個 tailnet 則最簡單。 |

如果你永遠只從「已登入 GitHub 的瀏覽器」存取，預設的私有埠就已經足夠，
這節可以不用再看。Tunnel 的價值在於：**好記的主機名**、
codespace 重建後仍然有效，或是**從非 GitHub 認證的裝置存取**。

### Cloudflare Tunnel + Access

Tunnel 由 `cloudflared` 容器**對外**建立連線，因此**完全不發布任何埠** ——
與 [D-003](DECISIONS.md) 中 Ollama 不對外發布是同一個思路。也因為 tunnel 的身分
存在於 Cloudflare 而非 codespace，短命的環境在每次重建後都會連回**同一個主機名**。

1. **Cloudflare Zero Trust → Networks → Tunnels → Create a tunnel**
2. 新增 **Public hostname**，Service 指向 `http://open-webui:8080`
   （是**容器內**的 8080，**不是**對外的 3000）
3. 複製 tunnel token（`eyJ…` 開頭）填到 `.env` 的 `CLOUDFLARE_TUNNEL_TOKEN`
4. **Access → Applications** 建立政策，例如只允許你自己的 email
5. 取消 `.env` 中 `COMPOSE_PROFILES=tunnel` 的註解，然後：
   ```bash
   bash scripts/up.sh
   ```

`cloudflared` 掛在 `tunnel` 這個 compose profile 下，**沒啟用就不會被建立**。
若 profile 開了但 token 是空的，`bash scripts/up.sh` 會在啟動前擋下來 ——
否則這個錯誤只會以看不懂的訊息出現在容器日誌裡。

**確認是否生效：**

```bash
bash scripts/status.sh                      # 顯示 cloudflared 是否已註冊
docker compose logs --tail=20 cloudflared   # 應出現 "Registered tunnel connection"
```

**要關掉 tunnel：** 把 `COMPOSE_PROFILES` 註解回去，執行 `bash scripts/up.sh`。
停用 profile **不會**停止已經在跑的容器 —— 而這裡有個容易搞錯的地方：
**`--remove-orphans` 也清不掉它。** compose 對 orphan 的定義是「compose 檔裡
**沒有定義**的服務」，而被 profile 停用的服務**仍然有定義**，所以那個旗標
再怎麼加都不會動它（實測三種寫法：`up -d --remove-orphans`、
`down --remove-orphans`、不加旗標的 `down`，三者**都不動**）。因此 `up.sh`
改用**差集**清除殘留容器 —— 「`config --services` 說目前作用中的服務」對比
「實際存在的容器身上的 service 標籤」；`down.sh` 則加上 `--profile '*'`，
讓「把堆疊停掉」真的等於整個堆疊。少了這一段，「我已經把 tunnel 關掉了」
會是個錯誤的認知，而服務其實仍連得到 —— 且 `status.sh` 還會一邊印出
「未啟用」。

現在若 profile 已關、`cloudflared` 卻還在跑，`status.sh` 會用紅字說出來，
而不是回報成關閉。

> ⚠️ **絕對不要用 Quick Tunnel（`*.trycloudflare.com`）承載私有資料。**
> Quick Tunnel **沒有附掛任何 Access 政策**，任何知道網址的人都連得到，
> 實質上等於公開。它只適合沒有損失的展示。本 repo 完全不使用。

> ⚠️ **tunnel token 等同該 tunnel 的控制權。** 持有它的人可以把你的主機名
> 指到他自己控制的伺服器。它只該放在 `.env`（已被 gitignore），不做他想。

> **這條路不保護什麼：** Cloudflare 會終結 TLS，因此它在傳輸過程中看得到明文，
> 這與任何反向代理相同。若這不可接受，請改用 Codespaces 私有埠或 Tailscale ——
> 那兩條路上沒有第三方坐在中間。

### 鎖住註冊

不論選哪一個方案，都先做這件事：

```bash
bash scripts/lock-signup.sh
```

它會用 HTTP 讀出**真實**狀態；若註冊是開著的，就透過設定 API 關閉，並讀回確認。
既有帳號不受影響。

> **`.env` 裡的 `ENABLE_SIGNUP` 管不到這件事。** 它只在**第一次開機**、Open WebUI
> 把設定寫進資料庫時被讀取；之後資料庫贏過一切，改 `.env`（甚至重建容器）**完全沒有效果**。
> 這是**實測**得到的，不是推論 —— 同一個 volume 開機三次、每次翻轉該環境變數，
> 開關始終不變。完整證據見 `DECISIONS.md` D-012。
>
> 實務上你在跑到這一步之前就已經受保護了：Open WebUI 在**第一個**帳號建立時會
> 自動關閉註冊。這支腳本的功能是**確認**這件事，並在它被重新打開時修正。
>
> `.env.example` 預設 `ENABLE_SIGNUP=false`。這**不會**把你鎖在外面 —— 已實測
> 確認全新安裝仍可建立第一位管理員（HTTP 200，且該帳號為 `admin`）。見 D-012。

---

## 驗證清單

本專案**有**自動化測試 —— `scripts/test_rag_probe.py`（57 項檢查）、
`scripts/test_verify_api.py`、`scripts/test_ask_probe.py`、
`scripts/test_egress_probe.py`、`scripts/test_probe_openai.py`、
`scripts/test_runtime_state.py`、`scripts/test_rag_grounding_probe.py` 與
`scripts/test_rag_http_probe.py` ——
離線單元測試：六支給探針、一支給 runtime 設定讀寫、一支給 RAG 評分，
而那個評分在 2026-09-19 **連續錯了四次**（四次全是**假失敗**；迴歸測資
用的是模型實際的回應原文，一字未改）。它的測試也釘住「模型太慢」與
「模型連不上」的分別 —— 這一項探針自己也弄錯過一次，把慢的模型報成
連不上的（D-018）。另有
`scripts/verify.sh`（5 項對實際 API 的測試）。
`test_ask_probe.py` 直到 D-016 才存在：在那之前它的評分邏輯從未被實測過，
而**第一次**實際跑到就抓到它自己的假失敗。本節原本寫的是相反的句子，
而把那個句子留在這裡是有意義的：一旦「推論品質本質上需要人工判斷」被寫成前提，
就再也沒有任何檢查去看答案**是否為真**。`verify_api.py` 第 5 項問模型
MCP 是什麼，卻只檢查**形式** —— 回應非空、未洩漏提示、未被截斷。
當模型回答「MCP 是阿里雲提供的模型」時，每一項檢查都通過。
見 `DECISIONS.md` D-014。

以下步驟仍以人工執行。但**事實正確性不能靠人工** ——
那是 `scripts/ask_probe.sh` 的職責。

### 第一階段：基礎堆疊

- [ ] **容器健康** —— `bash scripts/status.sh` 顯示三個容器皆為 `running` 且
      `healthy`（`ollama`、`open-webui`、`mcp-test-server`）
- [ ] **模型就緒** —— 模型清單中出現 `qwen3:4b`
- [ ] **推論正常** —— 送出一道你能用眼睛核對答案的題目。
      問 **HTTP，不要問 MCP**：本項原本寫「用三句話解釋什麼是 MCP」，
      而 `qwen3:4b` 對那題的答案是**自信地錯誤**，
      於是「取得了合理的回答」會在答案是假的時候照樣通過（D-014）
- [ ] **事實靠機械檢查，不靠肉眼** ——
      `bash scripts/ask_probe.sh qwen3:4b` 需**跑完** `mcp` 題與其
      `http` 對照題。對照題正是重點：少了它，就無法區分
      「這顆模型不可靠」與「這顆模型根本沒見過 MCP」——
      而這兩者的處置完全相反（D-014）。
      `mcp` 題**預期會失敗** —— 那是發現，不是缺陷，不要為了讓它變綠而調設定
- [ ] **評分邏輯本身被實測過** —— 一個只跑過「輸出為空」的模型的檢查，
      等於沒被測過。`verify_api.py` 的評分是改用 `qwen2.5:3b` 才第一次真正跑到，
      而**第一次跑就抓到檢查自己的假失敗**：`http` 對照題答對了，
      只因沒拼出縮寫全名而被判錯（D-016）。
      **沒產出過判定的測試，不算測過的測試**
- [ ] **速度是量出來的，不是假設的** —— 每題約需 **30–110 秒**；
      `qwen3:4b` 開著 thinking 時為 **264–293 秒**。此處原本寫的
      「10–30 秒」對不上任何一次實測，卻與 D-011 的數字直接衝突並存了
      一整天後才被發現。主因是 thinking，不是模型大小 ——
      同級模型關掉 thinking 只需 **7–15 秒**（D-011、D-014）
- [ ] **串流正常** —— 回答逐字浮現，而非停頓後一次出現
- [ ] **記憶體未爆** —— 對話進行中，`status.sh` 的 `Mem` 一行仍有餘裕
      （未被 swap 吃光）
- [ ] **模型卸載** —— 靜置超過 `OLLAMA_KEEP_ALIVE` 後，`docker stats`
      顯示 ollama 容器記憶體明顯下降
- [ ] **重啟存活** —— `bash scripts/down.sh && bash scripts/up.sh` 後，
      模型**不需重新下載**，且先前的對話紀錄仍在

### 第二階段：上傳文件之前

- [ ] 註冊確實已關閉 —— 執行 `bash scripts/lock-signup.sh --check`（結束碼 0 代表已確認
      關閉；它**不會**去讀 `.env`，因為那並不是控制這件事的地方）
- [ ] 已決定對外連線方式，且對該方式的取捨有意識
      （見[對外連線的安全性](#對外連線的安全性)；留在預設的私有埠也是有效答案）
- [ ] 已確認**沒有**使用 Quick Tunnel

### 第二階段：RAG

- [ ] 在 **工作區 → 知識庫**（英文介面是 Workspace → Knowledge）建立知識庫
- [ ] 上傳一份 PDF 或 Markdown 文件
- [ ] 對該知識庫提問，確認回答引用了文件內容而非憑空生成
- [ ] **反例測試**：詢問一個文件中**不存在**的細節，
      確認模型回答「不知道」而非編造

> **這四項可以機械驗證，而且不需要帳號。**
> `bash scripts/rag-verify.sh` 用一份**虛構**文件跑完這四項，走的是應用程式
> 自己的檢索函式（`query_collection`、`get_embedding_function`、
> `VECTOR_DB_CLIENT`、`apply_source_context_to_messages`）。之要用虛構內容，
> 是關鍵：問真實法規的話，模型可以從自己的記憶答對，那就什麼都證明不了。
>
> 它把**檢索與接地分開報**——「對的段落根本沒被撈到」和「撈到了但模型忽略」
> 是兩種病，修法不同。見 D-018。
>
> **它證明不了什麼：** 攝入用的是應用程式自己的切塊器、嵌入函式與向量客戶端，
> 但**由探針組裝** —— 不是走 `/api/v1/files` 那條 HTTP 路徑。那條路需要一個
> 已登入的使用者，而本資料庫目前有**零個使用者**：註冊是鎖住的（D-012），
> 所以建立第一個管理員只有你能做。有了帳號之後，上面四項請在 UI 裡實際做
> 一次；這支腳本是讓你在做之前就知道該預期什麼。
>
> **這個缺口現在有對應的腳本了：** `bash scripts/rag-http-verify.sh` 用
> `/api/v1/knowledge/create`、`/api/v1/files/` 與 `/api/chat/completions`
> 跑完同樣四項 —— 也就是 UI 自己走的那條路 —— 金鑰從 `.env` 讀。
> 它同時補上另一支的盲點，也有自己的盲點：**它看不到撈回了哪些 chunk**，
> 所以在它眼裡「對的段落沒被撈到」與「撈到了卻被忽略」分不開。
> 兩支都跑；單獨一支都不是完整答案（D-019）。
>
> **本堆疊的 API 金鑰預設是關閉的。** 2026-09-20 查資料庫：
> `auth.enable_api_keys = false` 且 `api_key` 表 0 筆，所以帳號頁的
> 「建立」不是不存在，就是回應 `403 API_KEY_CREATION_NOT_ALLOWED`。
> 請先到 **管理員控制台 → 設定 → 驗證 → API 金鑰** 打開
> （`http://localhost:3000/?settings=admin:authentication`）。
> 它和其他 Open WebUI 設定一樣住在資料庫裡 —— **改 `.env` 不會生效**（D-013）。
>
> **它也沒有壓到 context 上限。** 探針的文件只有 478 字元（約 765 token），
> 而本堆疊載入模型時是 **`num_ctx` 4096** —— Open WebUI 從來不設這個值。
> 在 `chunk_size=1000` 之下，撈回六個滿塊約 6700 token，超出的部分會被
> **靜默丟棄**，而被丟掉的正是排名最後的那些段落。請把「通過」讀成
> 「這條鏈能動」，**不要**讀成「RAG 對真實文件也成立」（D-018）。

> **非英文文件的注意事項**：Open WebUI 的預設嵌入模型是
> `sentence-transformers/all-MiniLM-L6-v2` —— **僅支援英文**、384 維、
> 約 500MB RAM。若文件是中文或其他非英文語言，檢索品質會很差，
> 必須改用多語言嵌入模型。候選模型已在本堆疊上實測過，
> 決定（見 `DECISIONS.md` D-013）是 **`ollama` 引擎 + `qwen3-embedding:0.6b`**。
>
> **請從 Admin UI 設定 —— 改 `.env` 沒有用。** Open WebUI 在首次開機時
> 把設定寫進 config 表，之後資料庫的既有值一律優先，所以改 `.env` 的
> `RAG_EMBEDDING_ENGINE` 再重建容器完全不會生效（這是從原始碼確認的，
> 不是推論 —— 見 D-013）。請用
> **管理員控制台 → 設定 → 文件 → 嵌入**（英文介面是 Documents → Embedding）。
>
> 這個決定有兩支腳本配合：`bash scripts/set-embedding.sh` 讀取／設定該項
> 設定，而且**送出後會回讀確認**（走 Admin UI 需要帳號，這支不用）；
> `bash scripts/rag_probe.sh` 對候選模型做繁體／簡體對照檢索實驗。
> 上面那支 `bash scripts/rag-verify.sh` 回答的則是最後選定的模型
> **到底會不會接地**。
>
> 模型要先用 `bash scripts/pull-model.sh qwen3-embedding:0.6b` 放進 ollama。
> **日後更換嵌入模型需要重新嵌入所有文件**，因此請在上傳前決定。
>
> 你可能看過的「RAM 從 2GB 暴增至 14GB」是 `jina-embeddings-v3` 的個案，
> 不是較大嵌入模型的通性 —— 見 D-013。

### 第二階段：MCP

- [ ] 在 **管理員控制台 → 設定 → 外掛功能**（英文介面是 Integrations）的
      「工具」區塊新增 MCP server：`External Tool Servers` 右側的 **＋**
      （**類型必須是 MCP Streamable HTTP**，不是 OpenAPI）
- [ ] 確認工具出現在對話的 tools 清單中，且帶有 `server:mcp:` 前綴
- [ ] 觸發一次工具呼叫，確認模型**自行決定**使用工具（而非被明確指示）
- [ ] **多輪測試**：需要連續呼叫兩次以上工具的任務，
      確認 4B 模型能維持流程不中斷

> **狀態（2026-09-20）：最後兩項已驗證通過，見 D-023。**
> 在四個功能開關關閉的狀態下（工具 37 → 16，
> `bash scripts/toggle-builtin-tools.sh --off`），模型從一句
> 「幫我丟一顆骰子」就**自己決定**呼叫 `roll_die` —— 沒提到任何工具名稱、
> 也沒被指示要用工具。接著一題兩輪的任務，它在**同一次回應裡發出三個
> 連續的工具呼叫**（兩個 `roll_die` 加一個 `echo`），最終答案正確加總了
> 兩筆真實回傳：5 + 6 = 11。
>
> 這份證據是機械的，不是「讀起來像真的」。模型發出的每個 `function_call`
> 都自帶一個 `call_id`，而工具結果會以**同一個 `call_id`** 掛在
> `function_call_output` 上。這條鏈存在對話紀錄與 MCP server 的
> `CallToolRequest` 日誌裡，所以**移除任何臨時探針都不影響它**；它也是
> 分辨「真的呼叫了工具」與「掰了一個看起來合理的數字」的唯一方法 ——
> D-023 第六節就記錄了一輪模型在**完全沒有 `function_call`** 的情況下
> 回答「6」。
>
> **收尾刻意還沒做。** 第三階段必須**透過 LangGraph 的程式路徑**重新量測
> 工具呼叫（D-011 的結果不會跨越那條界線轉移），而這個 server 就是現成的
> 測試對象。那次量測現在已經做完並通過了（2026-09-20，D-024），
> 所以**原本的理由已經消滅**。**測試對象仍然保留**，但理由換了一個：
> 它是第三階段 agent 唯一能呼叫的工具，item 2–6 每一項都會用到它。
> 管理介面那筆連線則隨時可以移除 —— LangGraph 這條路徑完全不經過 Open WebUI。

> **現成的測試 server**：`docker compose up -d --build mcp-test-server` 會起一個
> 只活在 ai-net 內網的 Streamable HTTP MCP server（不發布埠，D-003），提供
> `echo` 與 `roll_die` 兩個工具——前者驗證最基本的工具呼叫，後者讓
> 「擲兩次骰子並加總」成為現成的多輪測試題。新增時 URL 填
> `http://mcp-test-server:8000/mcp`、驗證選「無」。加之前可先跑
> `bash scripts/verify-mcp-server.sh`——它從 open-webui 容器的視角打完整
> handshake，機械確認這條路全通。驗證完第二階段後，刪掉 compose 裡的
> `mcp-test-server` 服務與 `mcp-server/` 目錄即可。

> **這已經不是未知數了。** D-011 已驗證本堆疊的多輪 tool calling **會通過**，
> 因此把「4B 等級模型到底能不能做這件事」列為本階段最關鍵的未知數，
> 是過期的資訊（D-014）。
>
> **但這個結果的適用範圍比字面上窄，而那個差別決定了第三階段。**
> 那份證據是在 **Open WebUI 自己的 tool calling 流程**中取得的。
> LangGraph 是透過 `bind_tools` / Ollama 整合的原生 function calling
> 呼叫工具 —— 那是**完全不同的程式路徑**。因此 D-011 的通過結果
> **不會自動轉移**，必須在 agent 框架內重新量測，第三階段才能依賴它。
> 這正是 D-014 在講的失效模式：在某組條件下量到的結果，被記成結論，
> 然後套用到它從未在那組條件下量過的地方。

---

## 額度管理

### 查看用量

<https://github.com/settings/billing> —— 兩個獨立的進度條：

- **Compute** —— 120 core-hours
- **Storage** —— 15 GB-month

### 降低消耗的做法

| 做法 | 效果 |
|---|---|
| 選 2-core 而非 4-core | 額度消耗減半 |
| 工作結束即 `bash scripts/down.sh` | 停止 compute 計費 |
| `gh codespace stop -c <name>` | 停止 compute 計費 |
| **刪除** codespace（非僅停止） | 停止**儲存**計費 |
| 關閉編輯器分頁 | 避免被判定為活躍 |

### 重要提醒

執行中的程序、終端機輸出、已開啟埠的流量**都會被判定為活躍** ——
**即使你人不在電腦前**。手動停止永遠是最保險的做法。

當任一額度耗盡且未綁定付款方式時，在每月重置之前
**無法建立或恢復任何 codespace**。

---

## 第二階段

Open WebUI **自 v0.6.31 起原生支援 MCP**，且內建 agentic mode 的工具
已涵蓋原計畫多數需求：

| 原計畫需求 | Open WebUI 內建工具 |
|---|---|
| RAG 文件知識庫 | `query_knowledge_bases` / `search_knowledge_files` / `view_knowledge_file` |
| 記憶 | `search_memories` / `add_memory` |
| 網路檢索 | `search_web` / `fetch_url` |
| 筆記 / 對話歷史 | `search_notes` / `write_note` / `search_chats` |

**第二階段因此不引入 LangGraph** —— D-005 把它延後到「出現需要自訂多步驟
workflow 或明確狀態機的需求時」才評估。那個條件現在正被刻意援引於第三階段，
因為第三階段按定義就是狀態機；見下方
[第三與第四階段](#第三與第四階段--計畫中六項已量測)，完整取捨見
[`DECISIONS.md`](DECISIONS.md) 的 D-005。

> **授權警告（已查證）**：`langgraph-server` / `langgraph-api` 生產容器映像檔
> 採用 **Elastic License 2.0**。在生產環境自架需要授權金鑰
> （Plus 方案以上的 `LANGSMITH_API_KEY`，或 `LANGGRAPH_CLOUD_LICENSE_KEY`）；
> 沒有金鑰會在啟動時拋出 `INVALID_LICENSE`。**`langgraph` 函式庫本身是 MIT
> 且免費** —— 請在你自己的服務中使用它，或使用 `langgraph dev`，
> 避開商業版 server 映像檔。

### 東西跑在哪裡（第二階段）

第一階段的圖是兩個長時間執行的容器。第二階段加了第三個，以及一層**不是服務**的
驗證層。下面這張圖是從 compose 檔與探針腳本讀出來的，不是憑印象畫的 —— 圖上
每一句在項目符號裡都有出處：

```
┌──────────────────────────────────────────────────────────┐
│  Docker network: ai-net                                  │
│                                                          │
│  long-running (docker compose up -d):                    │
│   ┌──────────┐         ┌───────────────┐                 │
│   │  ollama  │◄────────│  open-webui   │                 │
│   │  :11434  │         │ :8080 → :3000 │                 │
│   └────▲─────┘         └───────┬───────┘                 │
│        │                       │                         │
│        │  MCP (Streamable HTTP)│                         │
│        │                       ▼                         │
│        │               ┌───────────────┐                 │
│        │               │ mcp-test-srv  │                 │
│        │               │   :8000/mcp   │                 │
│        │               │ echo,roll_die │                 │
│        │               └───────────────┘                 │
│        │                                                 │
│  one-off (docker run --rm, per experiment):              │
│   ┌────┴─────────────────────┐                           │
│   │    probe container       │                           │
│   │    mem0 + ChromaDB       │                           │
│   │    + history.db          │                           │
│   └──────────────────────────┘                           │
└──────────────────────────────────────────────────────────┘
```

- **`mcp-test-server` 只為了一張清單而存在。** 它從 `./mcp-server` 建起來，用
  Streamable HTTP 在 `http://mcp-test-server:8000/mcp` 提供兩個工具 —— `echo`
  與 `roll_die`。它不發布任何埠，所以只有 `ai-net` 上的 `open-webui` 碰得到
  （`docker-compose.yml:197-200`，與 ollama 不發布 `:11434` 同一個思路，D-003）。
  compose 的註解寫著：驗證完第二階段 MCP 清單後，刪掉這個服務與 `mcp-server/`
  目錄即可（`docker-compose.yml:196`）。
- **它不是選用的；`cloudflared` 才是。** `mcp-test-server` 沒有宣告 profile，所以
  `scripts/up.sh:76` 那句樸素的 `docker compose up -d --wait --remove-orphans`
  會把它一起起起來，並且等它的 healthcheck。那個 healthcheck 只檢查「埠有在聽」
  —— 這是刻意的：streamable-http 的 `GET /mcp` 不會回 `200`，用 HTTP 狀態碼去驗
  會製造假失敗（`docker-compose.yml:202-204`）。
- **MCP 連線本身是資料庫狀態，不是設定。** 它沒有環境變數，是在介面上新增、存進
  Open WebUI 的資料庫 —— 見[啟用 MCP](#啟用-mcp)。
- **`:8080 → :3000` 是唯一發布的埠。** Open WebUI 在容器內聽 `:8080`，compose 把
  它發布到主機的 `:3000`，綁在 `${WEBUI_BIND_ADDR:-0.0.0.0}`（`docker-compose.yml:88`）。
- **探針層不屬於佈署面上的一員。** 它是第二階段那些宣稱的查核方式：每個探針都是
  `docker run --rm --network <project>_ai-net` 的一次性執行，映像檔當場用 heredoc
  建起來（`docker build -t … -f -`，所以磁碟上沒有 `Dockerfile`），探針的 `.py`
  以唯讀方式掛進去。目前兩個映像檔：`…-mem0-probe`（mem0 + ChromaDB）與
  `…-langgraph-probe`（tool calling，以及第三階段那些執行期問題）。
- **ChromaDB 與 `history.db` 就在探針容器裡面**（嵌入式），所以它們跟著 `--rm`
  一起消失（D-027 第七節）。需要讀回前一次呼叫寫了什麼的實驗，得在一次執行內
  完成。
- **第二階段沒有改變的：** `qwen3-embedding:0.6b` 與聊天模型由同一個 `ollama`
  容器提供，`:11434` 依然不發布，`cloudflared` 依然在 `tunnel` profile 後面。

### 啟用 MCP

**沒有 `ENABLE_MCP` 這個環境變數** —— 它不存在於 Open WebUI（已查證官方文件與
後端原始碼），設了只會讓你以為 MCP 已經開啟。MCP 的啟用方式是新增一條連線，
設定會存進資料庫：

1. **管理員控制台 → 設定 → 外掛功能**（英文介面是 Integrations）
2. 捲到「工具」區塊，在 `External Tool Servers` 右側按 **＋**
3. **類型那格的 `OpenAPI` 要點它** —— 它是**切換鈕**，不是唯讀標籤；
   點一下會變成 `MCP Streamable HTTP`
4. 填入 URL 與驗證方式（本 repo 的測試 server 選「無」）
5. 儲存

> 這一節的路徑以**本堆疊實測的 Open WebUI v0.11.3** 為準。舊版文件寫的
> 「Admin Settings → External Tools」在這個版本不存在 —— 分頁本身叫
> Integrations，繁中翻譯成「外掛功能」；`External Tool Servers` 這個標題
> 的繁中翻譯是空的，所以它會以英文顯示。而「類型」那一格長得像唯讀標籤，
> 是整條路徑最容易卡住的地方。

**存檔之後要重新載入整頁（F5）。** 沒按之前，剛剛新增的 MCP 工具不會出現在
對話的工具選單裡，而且它**不會自己出現** —— 再等也不會。這是前端 store 的
殘留，不是連線失敗，知道原因才不會往錯的方向除錯：

- `routes/(app)/+layout.svelte:187` 只在 layout 掛載時把工具載進 `$tools`
  store，**就那麼一次**。
- `Chat.svelte:990` 只在 **`$tools` 是空的時候**才重抓 —— 而空陣列在
  JavaScript 裡是 truthy，所以 store 一旦有任何內容，這個條件就對整個
  session 關上了。
- 在對話之間切換是前端路由：layout 不會重新掛載，所以上面兩條路徑都不會重跑。

整頁重新載入會重新掛載 layout、重跑 `setTools()`，`server:mcp:<id>` 的項目
才會出現。**如果重新載入後還是沒有**，那才是連線本身的問題 —— 回管理員控制台
看，握手失敗的 server 會在清單載入時以 toast 回報。（下面〈驗證 MCP server〉
可以在完全不開 UI 的情況下直接驗握手。）

只有管理員能新增 MCP server，且原生只支援 **Streamable HTTP** 傳輸 ——
stdio／SSE 的 server 需要用 [mcpo](https://github.com/open-webui/mcpo) 轉接。
`WEBUI_SECRET_KEY` 必須設定（本堆疊會自動產生），否則使用 OAuth 的 MCP 工具
每次容器重建都會出現 "Error decrypting tokens" 而失效。

**已知限制：**

- **僅支援 Streamable HTTP** —— 不支援 stdio、不支援 SSE。
  這是刻意的設計（瀏覽器與多租戶安全考量）。
- **MCP server 僅限管理員設定。** 非管理員只能新增 OpenAPI server。
- **stdio 類的 server**（Claude Desktop 用的那種）需要透過
  [**mcpo**](https://github.com/open-webui/mcpo) proxy 橋接為 OpenAPI。
- OAuth 2.1 工具**無法**設為模型預設值 —— 需要互動式重導向。

---

## 第三與第四階段 —— 計畫中；六項已量測

> **狀態：大部分仍是計畫。** 本節存在的目的是在動手**之前**把設計寫下來，
> 並讓它所依賴的假設明顯到可以被測試。以下每一項都標記為 **[已查證]**
> （2026-09-19 對文件／原始碼查證，或之後實測）或 **[未驗證]**
> （尚無任何量測支持）。
>
> **2026-09-21 起，六項全部已量測**（D-024、D-026、D-027、D-030）——
> 工具呼叫確實跨得過 code path 的改變、mem0 確實會沿用預先建立的 ChromaDB
> 集合、mem0 的抽取呼叫確實為一份它送不完的 prompt 付了錢、迴圈防護的通則
> 量到了、排程器的失敗模式也量到了。**第 5 項的前提沒有通過量測** ——
> 見下方第 5 項。每一項通過只代表「現在去做下一項不再是白費力氣」，
> **不代表第三階段可行**。

本節是對 **D-005** 的複核。D-005 把 LangGraph 延後到「出現需要自訂多步驟
workflow 或明確狀態機的需求時」才評估。那個條件現在是被**刻意援引**的，
不是被悄悄假設掉的 —— 第三階段按定義就是狀態機。D-005 的狀態是
「待第一階段驗證後複核」，而第一階段已經完成。**D-007 不變且不受影響：**
`langgraph` **函式庫**是 MIT；`langgraph-server` / `langgraph-api`
商業容器映像檔是 Elastic License 2.0，本專案不採用。

### 規劃的堆疊

| 層 | 選擇 | 備註 |
|---|---|---|
| 狀態機 | `langgraph`（函式庫） | MIT —— 以函式庫形式嵌入自家服務，絕不使用 ELv2 的 server 映像檔 |
| 模型對接 | `langchain-ollama` | 綁定 `qwen3:4b` / `qwen2.5:3b`，待第一階段 benchmark 定案後決定 |
| 工具橋接 | `langchain.mcp` → `MCPAdapter` | **[已查證]** —— 見下方更正 |
| 迴圈防護 | `recursion_limit` **逐圖設定**，不是 5 | **[已量測]** 第 4 項 —— 5 太緊 |
| 記憶層 | `mem0` + ChromaDB | **[已驗證]** 第 2、3 項 |
| 排程器 | `APScheduler`，單一 process 內 | **[已量測]** 第 6 項 —— 持久化是契約，不是換一個 store |
| 多步流程 | LangGraph Plan-and-Execute | Plan → Execute → Check → Retry/Summarize |
| 多代理 | LangGraph Supervisor（tool-based） | `langgraph-supervisor-py` 列為可選，不一開始引入 |

**關於工具橋接的更正。** 原規劃說「`langchain-mcp-adapters` 若仍採用則固定版本」。
先去查是對的直覺 —— **它已經不是官方推薦路徑了。** 自 LangChain 2026-09-03
的發布起，MCP 已內建於主套件的 `langchain.mcp` 命名空間，且 `MCPAdapter`
「取代了獨立的 `langchain-mcp-adapters` 套件」。安裝方式為
`pip install "langchain[mcp]"`，需要 `langchain[mcp]>=1.4.0`。
`MultiServerMCPClient.get_tools()` 對應到 `MCPAdapter.list_tools()`，
而傳輸方式現在是**從目標推斷**，不再用 `transport` 鍵明確指定。
注意 `langchain.mcp` 仍在 **beta** —— 匯入時會拋出 `LangChainBetaWarning`，
API 仍可能變動，因此選定後應固定版本。

### 決定這套能不能成立的未驗證項

以下每一項在撰寫時都是本專案尚未做過的量測。排序即為應該解決的順序 ——
因為若前一項失敗，後面的都是白做工。**六項現在都量過了**（第 5 項的前提
沒有通過 —— 動手前先讀過）。第 3 項自己的速率記在 D-027，那些數字
**並不**等於重打了基準線。重打已於 2026-09-21 嘗試過，**沒通過它自己的
穩定性準則**（D-031）—— 所以仍未完成，而且記錄上每一個速率不是來自舊
VM，就是來自一輪不穩到不該留下的量測。

第 4~6 項可以用這一行重跑：

```bash
bash scripts/verify-phase3-runtime.sh          # 三項全跑，人看的
bash scripts/verify-phase3-runtime.sh --json   # 機器可讀，走 stdout
```

1. **[已驗證] tool calling 換了程式路徑之後還成立嗎？** —— **成立。**
   2026-09-20 實測（D-024），指令為
   `bash scripts/verify-langgraph-tools.sh --model qwen2.5:3b`。D-011 證明的是
   **Open WebUI 流程**下的多輪 tool calling；LangGraph 透過 `bind_tools`
   與 Ollama 整合的原生 function calling 綁定工具 —— 那是不同的實作。
   這次重新量測時，**除了 code path 以外的東西全部固定住**（同一個模型、
   同樣 2 顆工具、同樣的 `num_ctx`、同樣的兩道題），所以結果可以歸因於
   code path 本身：

   - 模型**自己**決定呼叫 `roll_die` —— 問句從頭到尾沒有提任何工具名稱。
   - 第二輪發出兩次 `roll_die`，並根據兩筆真實回傳的 **2** 回答 **4**。
     因為兩顆骰子回傳同一個值，那個 `4` 不可能是覆述任何一顆 ——
     它只能來自相加。
   - 三層互相獨立的證據一致：`call_id` 鏈（3 次呼叫、3 筆對應回傳、
     沒有孤兒）、對真實回傳的加總比對、以及 MCP server 日誌
     （探針容器發出 `CallToolRequest` × 3 —— 這層完全不經過模型）。

   **一個要帶進後續設計的差異：這條路徑上一次只發一個工具呼叫，不是一批。**
   Open WebUI 那三次呼叫相隔約 10 ms（同一次回應）；LangGraph 的兩次
   相隔 **7.4 秒** —— 三次獨立的 LLM 往返。以這台機器解碼的個位數 tok/s
   （D-031 與 D-033 都量到個位數，兩次都不是基準線 —— D-033 發現
   判準在結構上**不可能被滿足**，該缺陷已修）來看，
   在 LangGraph 上「多一步」的代價是**多一次 LLM 往返**，不是多一個 token。
   這與 item 3、item 4 直接相關。這目前只有一次觀測 ——
   還不能歸因於是 LangGraph、langchain 或取樣的哪一個造成。

   **沒有量到、不要放大解讀：** 這次的 prompt 只有 213–364 tokens，離 D-022
   定出的 4,095 截斷觸發線極遠 —— 所以這次**不能**證明第三階段可以繼續用
   `num_ctx=4096`。一旦接上 mem0、更多工具與更長的歷史，prompt 就會長回去
   （D-014）。而且 n=1。

2. **[已驗證] Chroma 不會拒收本專案的嵌入向量 —— 但換模型是無聲的。**
   2026-09-20 實測（D-026），指令為 `bash scripts/verify-chroma-dims.sh`。
   **原本的預測是錯的，而量下去之後發現了更糟的事。** 三處更正：

   - **這條路徑上根本沒有 1536。** `ChromaDbConfig` 的欄位是
     `collection_name / client / path / host / port / api_key / tenant` ——
     **沒有任何維度欄位**。1536 是 mem0 的 *OpenAI* embedder、以及其他向量庫
     （pgvector、Milvus、Redis…）的預設，不是 Chroma 的。所以
     「把 `embedding_dims` 釘住」在這條路徑上**無處可釘**。
   - **mem0 的 ollama embedder 宣告 512，而那個值是死的。**
     `OllamaEmbedding.__init__` 寫 `embedding_dims = … or 512`，但 `embed()`
     把 ollama 回的向量原樣傳出去。實測：宣告 512、回傳 1024。
   - **維度是第一次寫入鎖定的，不是預設值決定的。** 新集合報不出維度、
     `metadata` 是 `None`；第一次 insert 之後才定下來。之後寫入不同長度
     **確實會被大聲擋下**：`InvalidArgumentError: Collection expecting
     embedding with dimension of 1024, got 512` —— 但那正是維度檢查本來
     就抓得到的情況。

   **真正危險的是預測的反面。** 這台機器上的兩個嵌入模型 ——
   `qwen3-embedding:0.6b` 與 `bge-m3:latest` —— **都是 1024 維**。
   所以維度檢查對「模型被換掉」**完全沒有防禦力**，而 Chroma 不會有任何
   抱怨：檢索只是回錯文件。實測：同一句話在兩個模型下的 cosine 是
   **0.0074**；同一個模型下兩句不相干的話是 **0.3308**。跨模型的「同一句話」
   比同模型內的「兩句不相干的話」還要遠 —— 這兩個空間不在同一個座標系裡，
   而整條堆疊沒有一處會說出來。

   **該改成這樣做：** 建立集合時把嵌入模型名寫進 collection 的 `metadata`，
   開集合時比對。實測可行 —— 這筆記錄**活得過** mem0 的 `create_col()`
   （它只傳 name 與 `embedding_function`）、mem0 的 `ChromaDB` 會沿用我們
   預先建立的集合，而且對既有集合用不同 metadata 再
   `get_or_create_collection` **既不丟例外也不覆蓋** —— 這讓它成為權威來源。
   探針裡的 `guard_verdict()` 是參考實作。

   **沒有量到：** mem0 `add()` 路徑的任何事。那條路會先跑一次 LLM 抽取事實
   再存，屬於第 3 項。這支探針直接驅動向量庫與 embedder，因此不需要
   API 金鑰、也不需要 LLM。

3. **[已驗證] mem0 確實要多付一次 LLM 呼叫；在預設 `num_ctx` 下那一次根本
   讀不到自己的指令；而且它預設的生成預算小到抽取講不完。** 2026-09-20 實測（D-027），指令為
   `bash scripts/verify-mem0-add-cost.sh`。上面那段話在**結構上**是對的，
   而量下去之後比它聽起來更糟：

   - **一次 —— 靠攔截確認，不是靠讀原始碼。** 探針包住 `llm.client.chat`，
     所以拿到的是 mem0 送出的**同一個 dict**：同樣的 messages、同樣的
     options，連 `format=json` 追加的那句 `Please respond with valid JSON
     only.` 都在裡面。一次 `add()` → 一次 chat 呼叫。Phase 5 的雜湊去重
     跑在 Phase 2 的抽取**之後**，所以同樣的內容寫兩次仍然付兩次。
   - **抽取 prompt 本身是 33,653 個字元的固定開銷** ——
     `ADDITIVE_EXTRACTION_PROMPT`，`configs/prompts.py:468` —— 而 mem0 的
     `OllamaLLM` **從頭到尾沒有送 `num_ctx`**（`llms/ollama.py:129-134`
     只送 temperature／num_predict／top_p），所以吃 ollama 的預設值 4096。
   - **在那個預設值下，8,047 個 token 裡有 5,997 個進不到模型** —— 而且
     被丟掉的是**開頭**，也就是模型自己的指令。用等長的 canary 分別放在
     系統提示詞的頭與尾確認：尾端看得到，開頭看不到 —— 而且把兩個 canary
     **對調位置**之後，看得到的仍然是同一端，這排除了「模型只是照著被問到
     的標記名稱回報」。
     *（這一句在量到之前就寫好了，而第一次量測回來的是一片空白 —— 模型的
     回覆在 `num_predict` 被切斷，探針把它讀成了「兩端都看不到」。壞掉的
     是儀器，不是這個斷言；上面這一句是修好之後重測的結果，D-027 第十節。）*
   - **要不被截斷，`num_ctx` 必須大於 prompt 自己的 token 數。** 規則是
     `num_ctx ≥ prompt token 數 + 1`。它是一個**門檻**，不是外推 —— 用小
     prompt 把它夾在 1 個 token 之內量出來的：`num_ctx = P` 時被砍到
     `truncated_length(P)`，`num_ctx = P + 1` 時原封不動。同一個夾縫也否定
     了兩條替代規則（門檻 = `num_ctx/2`、門檻 = `num_ctx − c`）—— 這兩條在
     舊讀法唯一賴以成立的那一點上，與真規則完全無法區分。**不要把 `8048`
     當成「mem0 的數字」帶著走**：它是這條規則套用在**探針重組出來的那一份**
     prompt（8,047 個 token）上的結果。同一輪裡 ollama 自己的日誌對兩次真實
     的 `add()` 寫的是 `prompt=8052` 與 `prompt=8100`，所以真實的 `add()`
     要 `≥ 8101`。
   - **……但那只是兩個門檻裡的第一個，而且明顯的那一個不夠。** 上面那條規則
     管的是**生成開始之前**的截斷。ollama 是用 `--context-shift --keep 4` 起
     llama-server 的，所以把 context 用滿的生成**不會停** —— llama-server 會
     **從 prompt 中段丟掉一整塊**再繼續生成（D-035；日誌行
     `slot context shift, n_keep = 4, n_left = 8187, n_discard = 4093`）。
     `num_ctx=8192` 配 8,052 的 prompt 只剩 **140** 個 token 的餘裕，兩次真實
     的 `add()` 都撞到了。真正成立的是
     `num_ctx > prompt_tokens + num_predict` —— 8,100 + 2,000 + 1 = **10,101**
     —— 所以佈署要用 **16384**。**這條觸發規則的證據很弱，而且這裡就標明它弱**：
     它是從**同一個 `num_ctx`** 的兩個觀測配出來的，16,384 是它在第二組上的
     第一次檢驗，那次預測 0 次 shift、實測也是 0 次。那叫一致，不叫證明 ——
     所以上面那個 `+1` 是保守取值，不是量出來的邊界。
   - **砍的形狀由伺服器日誌獨立確認，不經過探針。** `docker logs ollama` 裡
     每一條截斷紀錄都寫著 `limit=2050 prompt=... keep=4 new=2050`，緊接著的
     slot 行寫 `n_keep = 4` —— 也就是保留前 `numKeep = 4` 個 token、保留尾段、
     丟掉中間。這與原始碼和夾縫給出的是同一條規則，而且是伺服器針對真實請求
     回報的。
   - **thinking 被計費、然後被丟掉。** `_parse_response()`
     （`llms/ollama.py:43-90`）只回傳 `message.content` —— `eval_count`、
     `prompt_eval_count`、以及 qwen3 的 `thinking` 全部被丟棄，所以 mem0
     自己的介面答不出「它花了多少」。這就是為什麼必須從外面包住 client。

   **更正這一項的上一版。** 截斷規則**本來就已經有記錄了**：**D-023 第五節**
   寫著 token 數超過 `num_ctx − 1` 時砍到
   `num_ctx − max((num_ctx − numKeep)/2, 1)`，`numKeep = 4` —— 讀自同一份
   原始碼、對著同一行 `limit=2050` 日誌核對過。D-023 第五節甚至寫出它缺哪
   一項證據（「我沒有用第二組 `num_ctx` 實測過」），而那正是上面的夾縫檢驗；
   重跑它就是補上那個缺口。中間有一版用了**別的**公式
   （`num_ctx − num_predict − 46`），它剛好對上唯一被拿去核對的那一行日誌，
   而「驗證」它的那個實驗裡兩個公式預測同一個數字。見 D-027 第四節。

   **第三組對照組給了答案（D-036）。** `num_ctx` 維持 16,384、`num_predict`
   由 2,000 開到 8,000，其他什麼都沒動：第一次 `add()` 抽出 **2 則記憶**，
   而 D-035 在 2,000 那一輪是 **0 則** —— 且 `done_reason=stop`、
   `eval_count` 5,605，意思是它**沒有被切斷**。**抽取是真的會發生，成因就是
   生成預算。** 由此得到的規則不是一個數字：模型是自己停下來的，而任何低於
   模型實際所需的固定 `max_tokens` 都會**靜默地**截斷 —— 截斷出來的結果讀起
   來像「沒有事實可抽」，mem0 自己的 2,000 就是這樣被誤讀了三輪。

   那一輪第二次 `add()` 也是 0 則，而**它是另一種 0**：`done_reason=stop`、
   `eval_count` 1,236，而且它的內容已經被第一次寫進去了 —— 所以「沒有新東西
   可抽」是**正確**答案。在舊儀器上這兩種 0 分不出來，而這正是
   `done_reason` 必須在**開跑之前**補上的全部理由（commit `d4a8bf8`，在開跑
   前就推上去了，所以數字追得回那個修訂版）。這一輪另外確立三件事：

   - **「預算開大一定更貴」是錯的。** add 1 由 2,367 秒變 6,076 秒（×2.57），
     但 add 2 由 2,430 秒變成 **1,361 秒（×0.56，反而更快）** —— 它生成
     1,236 個 token 就自己停了，不是被切在 2,000。付費的是**用掉的**預算，
     不是**允許的**預算，所以把上限拉高在這一輪是零成本。
   - **有一件事被觀測到但沒有被解釋，記成未知。** `num_predict=8000` 之下
     ollama 把每一次生成切成**兩個 slot task**（把 prompt ＋ 已生成的部分
     重新 prefill 一次），所以 `prompt_eval_duration` 已經不再指涉它名字說的
     那一段。總量仍然與牆上時間對得起來（5,527.7 + 531.7 + 7.5 = 6,066.9 秒
     對 6,075.8 秒），**所以總量成立 —— 但 D-030 之後每一個成本論述背後的
     「prefill／生成」切分都要重新檢查。**
   - **`grep 'shift'` 會被旗標本身騙。** 那份日誌裡 7 個命中全部是
     llama-server 啟動時把 `--context-shift` 印在參數 dump 裡。判定要靠在
     **事件**那一行（`slot context shift, …`，不存在）以及 `n_discard`／
     `n_left`（也完全不存在，不只是沒數），不是靠關鍵字次數 —— 那種次數連
     啟動橫幅都滿足得了。

   **那組配對裡「context」那一半已經佈署了，「預算」那一半還沒有。**
   `deploy-vps.sh` 會寫入 `OLLAMA_CONTEXT_LENGTH=16384`（D-035 第八節），
   但 mem0 的 `max_tokens` 是 library 層的設定，**這個堆疊裡沒有任何地方
   寫它** —— 所以佈署起來的實例仍然用 2,000 的預設值抽取，抽取仍然是空的。
   那是下一個動作，而且它不是另一個魔數：開到模型自己停下來
   （`done_reason == "stop"`）為止。

   **這一項的儀器「已知會印出假句子」，在這一輪真的印出來了** ——
   「兩次都被截斷到同一個上限，所以 `prompt_eval_count` 看不出這個差別」
   —— 印在一行自己的文字寫著 `prompt token=8167` 對 8052、而 `num_ctx=16384`
   根本沒有截斷的地方。D-035 第六節把它記成「找到但沒修」；這一輪給了它觀測
   實例，它現在是一個四態判定（`truncated`／`not_truncated`／`disagree`／
   `unknown`）加上探針的修訂版守衛，見 `#58`（D-036 第十一節）。

   **重打基準線已於 2026-09-21 嘗試，沒過 —— 下面沒有任何一個速率是基準線。**
   這台 VM 於 2026-09-20 從 2 vCPU / 3.8 GB 加大為 4 vCPU / 16 GB，時點在
   D-024 **寫完之後**（D-024 的 commit 是 18:02，重開機是 18:15），所以在那
   之前記錄上的每一個速率描述的都是已經不存在的機器。
   `bash scripts/verify-throughput.sh` 在新 VM 上跑了完整矩陣，結果 **exit 1**：
   有兩個條件各重複三次，三次之間自己就差了 16.0% 與 26.7%，超過 15% 的上限。
   在那種雜訊下「沒有差別」與「量不出差別」長得一模一樣，所以這一輪記成
   **失敗**，不是「勉強算過」。

   這一輪確立了什麼、又沒有確立什麼（D-031）：

   - 條件真的對得上 D-014 的只有 `qwen2.5:3b` + `num_predict=1024` 那一臂，
     而且它的生成 3/3 都撞到上限（`matched`），所以這個對照至少是良置的：
     **6.59 t/s**，落在舊 VM 的 6.38–6.85 之內。但它自己的離散度是 15.7%
     —— **儀器的雜訊比要看的效應還大**。
   - `qwen3:4b` 量到 5.74–5.87 t/s，對上 D-014 的 3.78–3.97，但**兩者不能比**：
     D-014 沒有記錄 `num_ctx`，而上面第 3 項已經證明 `num_ctx` 決定 prompt
     被不被截斷 —— 也就是決定那個速率是在哪個條件下量的。
   - **沒有任何 CPU 擴充的結論。** 這裡沒有「4 vCPU 讓它快了 48%」這回事，
     也沒有量到任何記憶體頻寬。「沒有明顯變快」目前最直接的解釋是
     **CPU 拓樸**（guest 把 4 個 vCPU 報成 4 顆單核，宿主是大小核混合），
     而那個解釋也還沒被檢驗。

   這一輪同時做出了儀器：`scripts/throughput_probe.py`（純判定函式 + 量測）、
   它的 247 項離線斷言、一份 71 條突變的突變台，以及 `verify-throughput.sh`。
   儀器裡抓到並修掉八個缺陷；值得知道的列在下面的 Evidence 表。

   **接著換了估計量（D-032）：七次取樣，看變異係數。** 下一輪每個條件取
   **七個**樣本，離散度判定改成變異係數（樣本標準差 / 中位數，`<= 8.9%`），
   不再用全距。這是**估計量的修正，不是把標準放寬**：全距是樣本數的函數
   （`E[全距] = d₂(n)·σ`，`d₂(3) = 1.6926`、`d₂(7) = 2.7044`），所以七次
   取樣還沿用舊的「全距 15%」，實際上是 `CV ≤ 5.55%` —— 比 D-031 失敗的那道
   門檻**嚴了 59%**，而那是數出來的，不是機器量出來的。`8.9%` 是舊門檻的
   換算值（`15 / 1.6926 = 8.86`）。個別資料集可能因此過、也可能因此不過，
   而且**這一換不保證下一輪會過** —— 七個樣本還是散，那就是機器真的這麼吵，
   那也是一個結果。

4. **[已量測] `recursion_limit=5` 確實太緊 —— 而且「為什麼」的猜測是對的。**
   2026-09-21 實測（D-030），指令為 `bash scripts/verify-phase3-runtime.sh`。
   通則是 `所需 limit = super-step 數 + 1`，在九個拓樸上成立（linear 1/3/5，
   以及扇出寬度 1/2/4 × 輪數 1/2），每一個都用二分搜尋找「跑得完的最小
   limit」。**上限算的是 super-step，不是節點數** —— 寬度 4 的扇出有 6 個
   節點但只有 3 個 super-step，而把寬度從 1 掃到 6，最小 limit 停在
   `[4, 4, 4, 4, 4]` 不動。這是機械證據。預設值是 **10007**（讀 langgraph
   原始碼，不是二分搜尋），所以 `5` 是**刻意的窄化**。

   **那個 +1 花在收尾。** 當 `limit = 節點數` 時，每個節點都跑完了、
   `next` 也空了 —— 圖已經沒有下一步可走 —— 卻仍然拋
   `GraphRecursionError`。守衛是在工作**全部做完之後**才響的。這也表示
   那個例外型別**分辨不出**「計畫正常但守衛太緊」與「圖真的失控」——
   而 D-024 量過，這條路徑上多一步等於多一次 LLM 往返（當時是 7.4 秒）。
   中斷之後救不救得回來，則完全取決於 checkpointer：有的話，中斷點落在
   乾淨的節點邊界，同一個 `thread_id` 放大 limit 就能續跑；沒有的話，
   `get_state` 只會回 `ValueError: No checkpointer set`，成果沒了。

   `5` 允許 4 個 super-step，所以直線的 Plan → Execute → Check（3 步）塞得下，
   而第一次重試塞不下。**但最後這一句是外推，不是量測** —— 那個
   Plan-and-Execute 圖還沒被建出來，它的步數是從**有量到的**通則推的。
   量到的東西支持「5 太緊」，不支持「它剛好跑三步」。所以要**逐圖**設守衛，
   再把它實際跑了幾步讀回來：

   ```python
   app.get_state(cfg).metadata["step"]      # langgraph 自己的計數器，不是你的
   ```

   （`metadata["step"]` 需要 checkpointer 與 `thread_id`。**不要**數節點執行
   次數 —— 在扇出上那數的是另一件事，而且它會讀成判定不成立。）

5. **[已推翻] 沒有第二套向量庫要維運。** 原本的前提是「兩套儲存要備份、
   遷移、保持一致」。2026-09-21 實測（D-030）：mem0 的 ChromaDB 位於
   `Path(workdir) / "chroma"`，而 `workdir` 的預設值是 `tempfile.mkdtemp()`
   （`mem0_add_cost_probe.py:1931,2048`），容器又是 `docker run --rm`。
   **它從來沒有變成一個 volume。** 沒有東西可以備份、沒有東西可以遷移、
   沒有任何一對東西可以不一致。這一項要回答的問題因此改變了：不是
   *怎麼維運兩套*，而是**要不要讓它落地，落地的話放哪**。

   這個區分不是文字遊戲。「兩套儲存要備份」會讓下一個人去規劃備份與一致性
   的工作 —— 而那些工作在一個每次跑完就消失的目錄上全是白做的。這就是突變台
   裡這一條價值最高的原因。

   真正存在的那一套很小：Open WebUI 自己的 `vector_db` 量到 **7 MB**，
   在那個 2.2 GB 的 volume 裡。**磁碟確實是吃緊的資源**（97 GB 用了 81%）——
   但壓力在別的地方：**16.8 GB 可回收的映像**，向量庫只是它的 1/2400。
   順序是反的，所以判定寫成三段而不是兩段：`ok` / `reclaim-first`
   （放不下，但**可回收的量比要新增的量還大** → 先清再放）/ `blocked`。
   中間那一段才是發現；少了它，「81% 滿」與「滿了」會被讀成同一件事。

6. **[已量測] `APScheduler` 在 process 內會丟排程 —— 而持久化的 store 也會，
   安靜地，在一個沒人會去設的預設值下。** 2026-09-21 實測（D-030）。
   README 原本那句話成立：在真的重啟直譯器之後，`MemoryJobStore` 裡剩
   **0** 個 job、`SQLAlchemyJobStore` 剩 **1** 個。

   **持久化是必要條件，不是充分條件。** 一個在停機期間到期的 job，逾期超過
   `misfire_grace_time` 就會被當成 misfire 丟掉，而那個預設值是 **1 秒**。
   把逾期固定在約 2.2 秒、只改寬限：default → dropped、1 → dropped、
   2 → dropped、3 → ran、10 → ran。另一次獨立的停機掃描結果一致
   （0.3 / 0.8 秒的跑得起來，1.5 / 3.0 秒的被丟掉）。所以邊界是
   *逾期 > 寬限*，不是某個固定時長。

   **它危險的地方在於它與成功無法分辨。** `date` 觸發器跑完會自我移除，
   所以「跑過了、被移除了」與「逾期被丟掉了」在 store 裡**長得一模一樣**。
   任何對 job store 的檢視都分不出這兩者 —— 只有外部的紀錄可以。

   另外兩個代價也都是量到的。**換 store 不是 drop-in：** `MemoryJobStore`
   收得下 lambda，`SQLAlchemyJobStore` 直接拋 `ValueError: This Job cannot be
   serialized since the reference to its callable … could not be determined`
   —— 所以每一個排程的函式都必須是模組層級、可 pickle 的，而這是對程式碼
   組織方式的限制，必須在決定持久化**之前**知道。還有 `run_date` 必須是
   `datetime`，給 float 時間戳會得到
   `TypeError: Unsupported type for run_date: float`。

---

## Codespaces 的坑

已查證、容易浪費數小時的問題：

| 坑 | 說明 |
|---|---|
| **永遠沒有 GPU** | Codespaces 的 GPU 機器類型已於 **2025-08-29 下架**。所有推論都是 CPU-only。 |
| **推論很慢** | 3B–4B Q4 模型在共享 vCPU 上約為個位數 tokens/sec —— 舊的 2 vCPU 機器與現在的 4 vCPU 機器都量到這個數量級（D-014、D-031、D-033）。Open WebUI 預設的 300 秒 HTTP timeout 可能被長回答觸發。 |
| **模型下載幾乎不需要額外磁碟 —— 已量測** | 下載需要容納正在進來的資料，所以接近全滿的 volume 仍可能下載失敗 —— 有時是靜默失敗。**2026-09-26 量測**：在獨立 volume 裡對 `qwen3:8b`（5,225,422,848 B）真拉一次，每秒取樣 —— 1184 筆樣本、1183 秒 —— 峰值**實際佔用**從未超過模型的最終大小（瞬時超額 **0 B**；兩個獨立估計法都給 **1.000**）。原因是 blob **就是**成品，而 ollama 對它做稀疏預先配置，所以任何時刻都不會同時存在壓縮檔與解壓檔兩份。這一列原本寫的「約 2 倍」不只是沒有根據 —— 它是**錯的**，那個常數已經刪掉。重跑時若模型已存在也一樣不需要額外空間，而佈署閘門會**問機器**，不再假設一定會下載。（D-058；`docs/evidence/2026-09-26-pull-peak-measurement.txt`） |
| **可能先耗盡的是儲存** | 各方說法不一致：儲存究竟計費「實際使用」還是「32GB 配置量」。若是後者，一個 codespace 存活一個月就是 32 GB-month，對上 15 GB-month 的額度，會在**約兩週**內耗盡 —— 比 compute 更早。請盯緊帳單頁面。 |
| **`localhost` 不是主機** | 在 devcontainer 內，要連到主機服務需用 `host.docker.internal`（Linux 上需加 `--add-host=host.docker.internal:host-gateway`），或像本 repo 一樣把 Ollama 做成 compose 服務，以 `http://ollama:11434` 存取。 |
| **不存在 Ollama 的 devcontainer feature** | 沒有 `ghcr.io/devcontainers/features/ollama` 這個東西。所有做法都是透過 `onCreateCommand` 安裝、做成 compose 服務、或指向主機實例。本 repo 採用 compose 服務做法。 |

---

## 疑難排解

### 啟動失敗並顯示 `WEBUI_SECRET_KEY 未設定`

這是刻意的設計，不是 bug。`bash scripts/up.sh` 會自動產生金鑰；
或執行 `cp .env.example .env` 後填入任意 32 bytes 十六進位字串。

**不要**為了繞過而留空 —— 那會導致第二階段的 MCP 工具在每次容器重建後
出現 `Error decrypting tokens`。

### 模型下載中斷

```bash
bash scripts/pull-model.sh
```

Ollama 支援中斷續傳 —— **本專案沒有量測過這個宣稱**（D-058 的協定 D 沒有跑，見該條的
無測試聲明）。有量到的是它磁碟那一面：一次下載的峰值就是模型的最終大小、不多佔，
而「模型已經在」的重跑則完全不需要額外空間。佈署閘門會把這兩種情況分開，不再假設
一定會下載（D-058）。

### 回答速度很慢

在 2-core 的機器上這是預期行為。可嘗試：

- 改用更小的模型：修改 `.env` 的 `OLLAMA_MODEL=qwen3:1.7b`，再執行 `bash scripts/up.sh`
- 縮短 `OLLAMA_KEEP_ALIVE`（代價是每次對話需重新載入模型）

### 容器一直重啟 / 被 OOM killer 終止

記憶體不足。依序嘗試：

1. 確認 `.env` 的 `OLLAMA_MODEL` 不是 `qwen3:8b` 或更大
2. `docker stats` 找出是哪個容器在吃記憶體
3. 縮短 `OLLAMA_KEEP_ALIVE` 讓模型更快釋放

### 找不到 3000 埠的網址

在 Codespaces 中，埠**預設為 private**，只有在該埠有流量時才會出現在
**PORTS** 面板。若沒看到，手動加入：

1. **PORTS** 面板 → **Add Port** → 輸入 `3000`
2. 右鍵該埠 → **Port Visibility** → 依需求選擇
   （維持 private 即可，只有你能存取）

---

## 關於模型命名的補充

原始構想指定「Qwen 3B/7B」。這兩個尺寸在 **Qwen3** 中不存在：

| 系列 | 可用尺寸 |
|---|---|
| **Qwen3** | 0.6b · 1.7b · 4b · 8b · 14b · 30b · 32b · 235b |
| **Qwen2.5** | 0.5b · 1.5b · **3b** · **7b** · 14b · 32b · 72b |

Qwen2.5 **確實有** 3B 與 7B —— 若原本指的是這個，`qwen2.5:3b`（1.9 GB）
是 `qwen3:4b` 之外的合理選擇。本 repo 預設採 Qwen3，
因為它較新且完全採 Apache-2.0（Qwen2.5 的 3B 與 72B 使用 Qwen 授權）。

---

## 授權

本專案自身的程式碼 —— compose 檔、腳本、文件 —— 採 **[MIT 授權](LICENSE)**。

**這不涵蓋它所拉取的容器映像檔。** 各相依元件有其各自的授權：

| 元件 | 授權 | 備註 |
|---|---|---|
| [Ollama](https://github.com/ollama/ollama) | MIT | |
| [Open WebUI](https://github.com/open-webui/open-webui) | [Open WebUI License](https://github.com/open-webui/open-webui/blob/main/LICENSE) | ⚠️ **不是 MIT。** BSD-3 風格**外加品牌條款** —— 見下方說明 |
| [mcpo](https://github.com/open-webui/mcpo) | MIT | 第二階段 |
| [LangGraph](https://github.com/langchain-ai/langgraph) 函式庫 | MIT | `langgraph-server` **容器**為 Elastic License 2.0 —— 見 [D-007](DECISIONS.md) |
| Qwen3 模型 | Apache-2.0 | Qwen2.5 的 3B 與 72B 改用 Qwen 授權 |

### Open WebUI 的品牌條款

Open WebUI 授權的第 4 條禁止變更、移除、遮蔽或替換任何「Open WebUI」品牌識別
—— 包括名稱、標誌與視覺標識 —— **除非**符合以下情形：

1. 該部署在任何滾動的 30 天期間內，**終端使用者不超過 50 人**，或
2. 已取得著作權人的事前書面許可，或
3. （授權中載明的其他條件）

**這對個人 POC 完全不構成影響** —— 只有一個使用者，且本專案根本沒有修改介面。
只有在日後想以 Open WebUI 為基礎打造服務超過 50 人的產品時才會相關。
在你對「重新品牌化」這件事產生期待之前，值得先知道。

另需說明：在 compose 檔中單純引用一個映像檔，並不會讓該映像檔的授權
延伸到本 repo —— 上方的 MIT 授權涵蓋的是**本 repo 內**的程式碼。
