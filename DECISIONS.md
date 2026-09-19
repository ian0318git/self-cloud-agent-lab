# 架構決策記錄

本檔記錄 POC 過程中做出的關鍵決策與其理由。每項決策都附上「推翻了什麼」，
以便日後回頭檢視時能理解當時的取捨。

---

## D-001：Codespaces 定位為「驗證沙箱」，而非常駐主機

**日期**：2026-09-18
**狀態**：已決定

**背景**：原始構想是「利用免費的 VPS / Cloud environment 建立 self-hosted AI platform」，
並以 GitHub Codespaces 作為執行環境。

**查證結果**：

| 項目 | 免費額度 | 實際可用量 |
|---|---|---|
| Compute | 120 core-hours/月 | 2-core 機器 → **60 真實小時/月（約每天 2 小時）** |
| Storage | 15 GB-month | **codespace 存在期間就計費，停止狀態照算** |
| Idle timeout | 預設 30 分鐘 | 人不在即自動停止 |
| Port 可見性 | 預設 private | 需手動改為 public |

**決策**：Codespaces 僅用於「開機驗證 → 關機刪除」的短期驗證。
不將其作為 24/7 常駐服務的宿主。

**理由**：以 24/7 運作計算，60 小時的額度會在 **2.5 天內耗盡**，
之後 $0 spending limit 會直接中斷環境。這與「self-hosted 常駐平台」的目標
在架構上不相容。

**推翻了什麼**：原計畫將 Codespaces 視為免費 VPS 的前提。

---

## D-002：第一階段模型選用 qwen3:4b

**日期**：2026-09-18
**狀態**：已決定

**背景**：原始構想指定「Qwen 3B/7B」。

**查證結果**：Qwen3 系列**沒有 3B 與 7B 尺寸**。實際為
`0.6b / 1.7b / 4b / 8b / 14b / 30b / 32b / 235b`。

**決策**：預設模型為 `qwen3:4b`（2.5 GB）。

**理由**：8GB RAM 的記憶體預算如下 ——

```
OS + Docker          ~1.0 GB
Open WebUI           ~1.0 GB
qwen3:4b + KV cache  ~3.0 GB
──────────────────────────────
合計                 ~5.0 GB   ← 留有約 3GB 餘裕
```

若改用 `qwen3:8b`（5.2 GB），總計逼近 7.5 GB，
在 8GB 機器上與 Open WebUI 併跑有 OOM 風險。

**備註**：4b 支援 256K context，且具備 tool calling 能力，足以驗證第二階段的 MCP 流程。

**推翻了什麼**：原計畫的「3B/7B」模型尺寸設定。

---

## D-003：不對外發布 Ollama 的 11434 埠

**日期**：2026-09-18
**狀態**：已決定

**背景**：原始 docker-compose.yml 將 ollama 的 11434 埠發布到主機。

**決策**：移除該埠發布，僅透過 compose 內部網路 `ai-net` 提供服務。

**理由**：**Ollama 沒有任何認證機制**。任何能觸及該埠的人都可以：
- 讀取模型清單
- 刪除已下載的模型
- 推送任意模型（含可能的惡意 model file）

Open WebUI 已可透過 `http://ollama:11434` 直達，對外發布沒有任何功能上的必要，
只是白白擴大攻擊面。

**除錯替代方案**：`docker compose exec ollama ollama list`

**推翻了什麼**：原 compose 檔的 `ports: - "11434:11434"`。

---

## D-004：強制要求 WEBUI_SECRET_KEY，不允許留空

**日期**：2026-09-18
**狀態**：已決定

**背景**：Open WebUI 在未設定 `WEBUI_SECRET_KEY` 時會自動產生一組並存於資料卷中，
表面上「可以運作」。

**決策**：compose 檔使用 `${WEBUI_SECRET_KEY:?...}` 語法，
未設定時**直接拒絕啟動**，並由 `scripts/up.sh` 自動產生 32 bytes 隨機值。

**理由**：2026 年的 Open WebUI 以該金鑰加密儲存 MCP 的 OAuth token。
金鑰未固定時，每次容器重建都會出現 `Error decrypting tokens`，
導致 MCP 工具連線失效 —— 這是一個**延遲出現、難以診斷**的故障。
在第二階段才發現會耗費大量除錯時間。

這符合專案原則：**不允許靜默失敗**。

---

## D-005：第二階段優先使用 Open WebUI 原生能力，暫不引入 LangGraph

**日期**：2026-09-18
**狀態**：待第一階段驗證後複核

**背景**：原計畫第二階段為「RAG + MCP + LangGraph」。

**查證結果**：Open WebUI **自 v0.6.31 起原生支援 MCP**
（Admin → External Tools → 選 `MCP (Streamable HTTP)`），
且 agentic mode 的內建工具已涵蓋原計畫的多數需求：

> **2026-09-19 更正**：本文原寫「`ENABLE_MCP=true`」，**那個環境變數不存在**。
> 官方文件與後端原始碼（v0.11.3）皆查無此變數，唯一相關的是握手逾時
> `MCP_INITIALIZE_TIMEOUT`。MCP 的啟用是新增連線並寫入資料庫，與環境變數無關 ——
> 與 D-012 補記發現的 `ENABLE_SIGNUP` 是同一類錯誤：把「資料庫驅動的設定」
> 誤當成「環境變數驅動的設定」。

| 原計畫需求 | Open WebUI 內建工具 |
|---|---|
| RAG 文件知識庫 | `query_knowledge_bases` / `search_knowledge_files` / `view_knowledge_file` |
| 記憶 | `search_memories` / `add_memory` |
| 網路檢索 | `search_web` / `fetch_url` |
| 筆記 | `search_notes` / `write_note` |
| 對話歷史 | `search_chats` / `view_chat` |

**決策**：第一階段先以 Open WebUI 原生 MCP + Knowledge 驗證。
LangGraph 延後引入，僅在出現「需要自訂多步驟 workflow 或明確狀態機」的需求時才評估。

**理由**：在額度受限的環境中，每一層額外元件都是額外的記憶體開銷與維護成本。
先驗證既有能力是否足夠，可避免過早最佳化。

**已知限制**（2026-09-18 查證）：

- **僅支援 Streamable HTTP** —— 不支援 stdio、不支援 SSE，這是刻意的設計
  （瀏覽器與多租戶安全考量）。
- **MCP server 僅限管理員設定**，非管理員只能新增 OpenAPI server。
- **`stdio` 類的本地 MCP server**（Claude Desktop 用的那種）需透過
  [**mcpo**](https://github.com/open-webui/mcpo)（4,379★, MIT）橋接為 OpenAPI。
- **OAuth 2.1 工具無法設為模型預設值** —— 需要互動式重導向。
- 官方文件仍將 OpenAPI（經由 mcpo）列為多數部署情境的「偏好路徑」。

**待驗證**：4b 等級的模型能否穩定執行多輪 tool calling —— 這是本階段最需要實測的假設。

---

## D-007：不採用 `langgraph-server` 商業容器映像檔

**日期**：2026-09-18
**狀態**：已決定

**查證結果**：`langgraph-server` / `langgraph-api` 生產容器映像檔採用
**Elastic License 2.0**。在生產環境自架需要授權金鑰
（Plus 方案以上的 `LANGSMITH_API_KEY`，或 `LANGGRAPH_CLOUD_LICENSE_KEY`）；
沒有金鑰會在啟動時直接拋出 `INVALID_LICENSE`。

**決策**：
- **`langgraph` 函式庫本身是 MIT，可自由使用** —— 若日後確實需要，
  應將它以函式庫形式嵌入自己的服務，或使用 `langgraph dev`
- **不採用商業版 server 映像檔**

**理由**：本專案的前提是「完全免費」。引入一個啟動時就需要付費授權的元件，
會直接違反這個前提，而且是在部署階段才會發現 —— 屬於高成本的延遲失敗。

**與 D-005 的關係**：這是支持「第二階段先不引入 LangGraph」的**第二個獨立理由**。
D-005 的理由是「既有能力可能已經足夠」，本項的理由是「商業版元件的授權成本」。
兩者獨立成立。

---

## D-008：RAG 嵌入模型需在上傳文件前決定

**日期**：2026-09-18
**狀態**：**已由 D-013 取代（2026-09-19）** —— 本條的模型選擇（`nomic-embed-text`）
與其「已知風險」的描述都有誤，實測結果見 D-013。本條保留作為當時的推理記錄，
以及「上傳文件前必須決定」這個**仍然有效**的結論。

**查證結果**：Open WebUI 內建 RAG 的預設嵌入模型為
`sentence-transformers/all-MiniLM-L6-v2` —— **僅支援英文**、384 維、
CPU 執行、約 500MB RAM。向量儲存於內嵌的 ChromaDB。

**問題**：本專案的使用者文件預期包含**繁體中文**。
英文嵌入模型對中文文件的檢索品質會顯著低落，
這會直接影響第二階段 RAG 驗證結果的有效性 ——
若未察覺，可能誤判為「RAG 不可行」，而實際上只是嵌入模型選錯。

**決策**：
- 第二階段上傳任何文件**之前**，先切換至多語言嵌入模型：
  `RAG_EMBEDDING_ENGINE=ollama` + `RAG_EMBEDDING_MODEL=nomic-embed-text`
- **切換嵌入模型需要重新嵌入所有既有文件**，因此這是一旦開始上傳就難以回頭的決定

**已知風險**：有回報指出較大的嵌入模型會讓 RAM 從 2GB 暴增至 14GB。
在 8GB 的 codespace 上，這與 Ollama 主模型直接競爭記憶體。
需在實測時以 `scripts/status.sh` 密切監控。

---

## D-009：儲存額度的計費基準存疑，採保守策略

**日期**：2026-09-18
**狀態**：待實測確認

**背景**：GitHub 官方文件描述儲存計費為
「the amount of disk space the codespace or prebuild occupies」，
字面上指向「實際佔用」。但社群回報中有說法指其計費的是
**整個 32GB 的 volume 配置量**，而非實際用量。

**影響**：這個差異很大 ——

| 計費基準 | 單一 codespace 存活一個月的消耗 | 對照 15 GB-month 額度 |
|---|---|---|
| 實際佔用（假設 ~9GB） | 9 GB-month | 可存活整月，有餘裕 |
| 完整配置（32GB） | 32 GB-month | **約兩週即耗盡，比 compute 更早** |

**決策**：無法從文件確認，因此採保守策略 ——
**假設最壞情況（32GB 配置量計費）**，並在 README 的
[Codespaces 的坑](README.zh-TW.md#codespaces-的坑)一節中
明確標示此為未定事項。

**行動**：實際使用時應在第一週就檢查
<https://github.com/settings/billing> 的儲存進度條，
以實測數據取代文件推論。這個數字會直接決定
「codespace 可以保留多久才需要刪除重建」的操作節奏。

---

## D-010：本專案採 MIT，但需知悉 Open WebUI 的品牌條款

**日期**：2026-09-18
**狀態**：已決定

**決策**：本專案自身的程式碼（compose 檔、腳本、文件）採 **MIT 授權**。

**背景 —— 相依元件的授權各不相同**（2026-09-18 以 GitHub API 查證）：

| 元件 | 授權 |
|---|---|
| Ollama | MIT |
| mcpo | MIT |
| LangGraph（函式庫） | MIT |
| Qwen3 模型 | Apache-2.0 |
| **Open WebUI** | **Open WebUI License（非 MIT）** |

**Open WebUI 授權的關鍵限制**（第 4 條）：禁止變更、移除、遮蔽或替換任何
「Open WebUI」品牌識別，**除非**該部署在任何滾動 30 天期間內終端使用者
不超過 **50 人**，或已取得事前書面許可。

**評估**：

- 對本 POC **完全不構成影響** —— 單一使用者，且本專案未修改介面
- 但在 compose 檔中引用映像檔**不會**使該授權延伸到本 repo，
  因此本 repo 採 MIT 是恰當的：MIT 涵蓋的是本 repo 內的程式碼
- 此限制僅在「以 Open WebUI 為基礎打造超過 50 人的產品」時才生效

**記錄理由**：這是一個**不會在開發階段浮現、只會在商業化階段才撞到**的限制。
先記錄下來，避免日後產品化時才發現需要重新設計或取得授權。

**同時釐清**：LangGraph 的 MIT 指的是**函式庫**；
其**商業版容器映像檔**為 Elastic License 2.0，這是兩件不同的事（見 D-007）。

---

## D-006：不設定容器的硬性記憶體上限

**日期**：2026-09-18
**狀態**：已決定

**背景**：曾考慮為 open-webui 設定 `mem_limit` 以保護 ollama 的記憶體。

**決策**：不設定。改以 `OLLAMA_MAX_LOADED_MODELS=1` 與 `OLLAMA_NUM_PARALLEL=1`
控制 Ollama 端的記憶體用量，並提供 `scripts/status.sh` 供人工監控。

**理由**：若 open-webui 觸及硬性上限，會被 OOM killer 終止；
由於 `restart: unless-stopped`，它會反覆重啟形成 crash loop，
反而比讓核心自行調度更難診斷。在 POC 階段，
可觀測性優先於隔離性。

---

## D-011：4b 模型在本環境的最佳配置是「維持 thinking 開啟」

**日期**：2026-09-18
**狀態**：已驗證（實測資料，非推論）

**背景**：D-005 標記了第二階段最大的未知 ——「4b 等級模型能否做多輪 tool
calling」。同時假設 thinking 模式會吃掉大量 token 與時間，因而嘗試關閉它。
兩者都以 `scripts/verify_api.py` 對 Ollama API 直接實測（不經 Open WebUI）。

**實測環境**：GitHub Codespace 2-core / 8GB、Ollama 0.34.2、qwen3:4b。

**已驗證的結論**：

| 項目 | 結果 |
|---|---|
| 生成速度 | 4.5–5.1 tok/s（多次測試一致） |
| 單輪 tool calling | 通過 —— 正確產生 `get_weather({"city": "台北"})` |
| **多輪 tool calling** | **通過 —— 收斂成「台北現在天氣多雲，溫度28度。」** |
| 關閉 thinking | **沒有可用方式** |
| 繁體中文控制 | 未能完成驗證（見下） |

**D-005 的未知已解答**：多輪 tool calling 成立，第二階段可直接進行。

**三種關閉 thinking 的方式全部失效**：

1. 提示詞加 `/no_think` → **HTTP 500**（Ollama 0.34.2 直接拒絕）
2. API `think: false` → **假象**。thinking 欄位確實清空（225 字 → 0 字），
   但模型仍在思考，內容改從 `response` 流出：252 tokens 的內心獨白、沒有
   作答、耗時 71s。對照組（thinking 開啟）169 tokens / 54s，答案正確。
   **它關掉的是標籤，不是行為 —— 更慢、更貴、更糟。**
3. `raw` 加 ChatML 預填已關閉的思考區塊 → **HTTP 500**

**決策**：不關閉 thinking。維持 Ollama 預設 —— 它會把思考隔離在獨立的
`thinking` 欄位，`response` 保持乾淨。消費端只要正確忽略 `thinking` 欄位
即可，Open WebUI 原生行為即是如此。

**繁體中文的實際狀況**：
- 模型會**鏡像使用者的語言** —— 繁體提問得到繁體回答（多輪 tool calling
  的最終答案即為繁體）
- 但模型的**思考內容預設是簡體**（用户、说、这个）
- 因此一旦思考外流（如 `think: false` 的情形），輸出就會被簡體污染
- 系統提示詞「嚴禁使用簡體字」有效，但無法阻止思考階段的簡體

**繁體中文控制在建議配置下仍未完成驗證**：最後一次嘗試把 `num_predict`
提高到 1024（512 會讓模型還在思考就被截斷，只得到「無法判定」），模型端
回 **HTTP 500：`an error was encountered while running the model:
unexpected EOF`** —— llama-server 在生成途中結束。**原因未定**：可能是
記憶體不足、context 配置、或 Ollama 0.34.2 自身的問題，本專案沒有足夠
證據區分，不應臆測。這是待辦事項，不是已知結論。

實務影響有限：多輪 tool calling（真正要做的事）已產出乾淨的繁體答案，
且 thinking 預設被隔離在獨立欄位，Open WebUI 的顯示不受污染。

**已推翻的假設**（曾提出且已證偽，記錄以免重蹈）：
- ~~記憶體不足導致模型無法載入~~ —— `ps` 證實模型確實載入（RSS 3.0 GB）
- ~~swap 抖動~~ —— 本環境**完全沒有 swap**（`Swap: 0B`）
- ~~`think: false` 可關閉 thinking~~ —— 見上，只是把思考搬家

**三個判準缺陷（本專案自己犯的，已修正並補上迴歸測試）**：
- 用「token 數是否提早收斂」判斷 thinking 是否關閉 —— 間接指標，誤判
- 用「thinking 欄位是否為空」判斷 —— 仍是間接指標，再次誤判
- `_guard` 讓「測試拋出例外」與「測試回傳 None」共用同一個值，導致
  HTTP 500 被顯示成「無法判定（輸出為空）」—— 原因完全錯誤。已以
  `FAILED` 哨兵區分。**注意 `FAILED` 是物件，`bool()` 為真**，因此
  `critical` 的計算改為明確比對，否則「執行失敗」會被當成「通過」。

**共同根源**：三次都是**把兩個不同的狀態併成同一個值**，或**用代理指標
取代直接證據**。症狀都是「顯示的結論與事實不符」，而且都不會拋例外 ——
這正是本專案明令禁止的靜默失敗。真正的判準是**答案對不對、快不快**，
不是任何單一欄位或單一函式的傳回值。

**模型比較（2026-09-18，同一組測試 3/4）**：

| | qwen3:4b | qwen3:1.7b |
|---|---|---|
| 單輪 tool call | 通過（121 tok / 24.4s） | 通過（129 tok / 11.9s） |
| 多輪 tool call | 通過（409 tok / 90.5s） | 通過（185 tok / 17.9s） |
| 生成速率 | 4.90 tok/s | 10.3–10.8 tok/s |
| 執行後可用記憶體 | 992 Mi | 2.1 Gi |
| 繁體輸出（無系統提示） | **正確** | **簡體** |
| 繁體輸出（有系統提示） | 未測 | **正確**（單句樣本） |

多輪 tool calling 在 1.7b 上同樣成立，速率快 2.1 倍；測試 4 的牆鐘更從
90.5 秒降到 17.9 秒（5×，因為它同時也少講了很多話）。記憶體多留 1.1 GiB，
對之後要放嵌入模型（RAG）是實質好處。

**但 1.7b 不會鏡像使用者的語言** —— 繁體提問得到簡體回答：

    4b   ：台北現在天氣多雲，溫度28度。
    1.7b ：台北目前的天气是多云，气温28摄氏度。

**系統提示詞可以強制繁體**：測試 5 在 1.7b 上通過 —— 在系統提示「你必須
一律使用繁體中文回答，嚴禁使用簡體字」之下，輸出為

    MCP是多選題，考生需從多個選項中選擇正確答案。

整句為繁體（從、個、選、題、項、擇、確 皆為繁體字形）。關鍵對照：**同一
顆模型、同樣的繁體提問**，沒有系統提示時輸出簡體（見上表），加上系統提示
後輸出繁體 —— 差別就在系統提示。379 tokens / 36.1s，完整跑完未截斷。

**保留**：這是單一一句話的樣本，長輸出是否會飄回簡體未測。因此不建議只
依賴模型的配合 —— 見下方「建議的強制手段」。

**第二次獨立的幻覺證據**：1.7b 說 MCP 是「多選題」，4b 說是 "Microsoft
Client Protocol"。兩顆模型、兩種不同的錯法、都講得理直氣壯、格式都完美。
**這是 Agent 平台最危險的失敗模式：看起來對的錯答案。** 沒有檢索就不要讓
模型回答事實性問題 —— RAG 不是第二階段的一個功能，是它的前提。

**尚未驗證**：1.7b 的推理能力。本測試只有單一工具的簡單情境，較複雜的
多工具／多步任務未測，不可從這兩項推論。

**附帶修正**：本次比較暴露了簡化字清單的漏收（`气`、`摄` 不在清單中），
以及 `云` 這類「繁體亦合法但簡體中另有其字」的盲點。已擴充清單並新增
`AMBIGUOUS_HINTS` —— 後者只提出警告、不影響判定，因為逕判會大量誤判
（「台灣」「方面」「只有」皆為合法繁體）。

**剩餘限制**：
- 一個簡單問答約需 30–110 秒（thinking 佔絕大部分）
- 對互動式使用偏慢；批次／非同步的工作流程則可接受
- 本結論僅適用於 2-core CPU；4-core 預期約兩倍速度

---

## D-012：對外連線採 Cloudflare Tunnel + Access，且必須早於 RAG

**日期**：2026-09-18
**狀態**：已決定（**程式與文件已完成，尚未實地驗證** —— 見文末）

**背景**：使用者提出「可以使用 Cloudflare + Open WebUI 來完成安全連線嗎？
要不然我的電腦連 Open WebUI 的資料就不安全了」。

這個問題問得比它表面上看起來更關鍵。第一階段整個堆疊裡**沒有任何私有資料** ——
只有模型與空的對話紀錄，所以傳輸安全在當時確實不重要。**會改變這件事的是 RAG**：
一旦上傳自己的文件，瀏覽器到模型之間的那段路就開始承載值得保護的內容。

**查證結果**：

| 事實 | 影響 |
|---|---|
| Codespaces 的埠**預設 private**，需 GitHub 認證才連得到 | 這已經是一個有效的身分驗證層，不是「沒有防護」 |
| GitHub 會終結 TLS，因此它看得到明文 | 與 Cloudflare 的取捨相同，只是換一個第三方 |
| cloudflared 由容器**對外**建立連線 | **不需要發布任何埠**，也繞過了 Codespaces 埠轉送的可得性問題 |
| Tunnel 的身分存在於 Cloudflare，不在 codespace | codespace 重建後**連回同一個主機名** —— 這對短命環境是決定性優點 |
| Cloudflare Access 免費層涵蓋 50 位使用者 | 個人使用完全在免費範圍內 |
| **Quick Tunnel（`*.trycloudflare.com`）沒有 Access 政策** | **等同公開，不可承載私有資料** |
| tunnel token 可重新導向該主機名 | token 外洩 = tunnel 控制權外洩，只放 `.env` |
| Cloudflare 會終結 TLS | 它看得到明文，與任何反向代理相同 |

**決策**：

1. 對外連線採 **Cloudflare Tunnel + Access**，做成 compose 的 `tunnel` profile，
   **預設關閉** —— 沒啟用就不會被建立。
2. **不強制**使用者走 tunnel。文件同時列出三個選項（Codespaces 私有埠／
   Cloudflare Tunnel + Access／Tailscale），並明說「留在預設私有埠」是有效答案。
3. `ENABLE_SIGNUP` 的關閉做成腳本 `scripts/lock-signup.sh`，不只是一行註解提醒。
4. **順序固定：安全層先於 RAG。** 見下方理由。

**理由**：

- **為什麼不強制 tunnel**：使用者的實際威脅模型是「我的資料在傳輸中被誰看到」。
  若他只從已登入 GitHub 的瀏覽器存取，預設的私有埠就已經滿足這個需求，
  多架一層只是多一個第三方（Cloudflare）看到明文。**安全措施要對應真實威脅，
  不是越多越好** —— 加一個會看到明文的仲介，可能讓整體更差而不是更好。
- **為什麼順序不能顛倒**：安全層是「保護既有資料」的機制。若先上傳文件、
  之後才補安全層，那份文件在補上之前的每一次傳輸都是未受保護的，
  而且**已經來不及**。這是不可逆的順序，不是偏好問題。
- **為什麼 `lock-signup` 要做成腳本**：`ENABLE_SIGNUP=true` 期間，任何能觸及
  Open WebUI 的人都能自行註冊，且**第一位註冊者成為管理員**。這種「一行設定
  決定整個系統歸屬」的項目，靠 `.env.example` 裡的註解提醒是不夠的 ——
  註解會被略過，腳本會回報結果。

**推翻了什麼**：

- 推翻了「安全連線是第二階段後期再處理的事」。它必須在 RAG **之前**，
  理由是順序不可逆，而不是因為它比較重要。
- 推翻了「要安全就一律上 tunnel」的直覺。多一層不等於更安全 ——
  Cloudflare 一樣看得到明文。真正的判準是「誰坐在傳輸路徑上」。
- 推翻了「把 codespace 埠改成 public」這個沒被說出口的選項。使用者從未授權
  這件事，本決策也不採用；`README` 只在疑難排解中說明埠可見性的操作方式。

**實作**：

| 檔案 | 內容 |
|---|---|
| `docker-compose.yml` | `cloudflared` 服務，掛 `profiles: ["tunnel"]` |
| `.env.example` | `CLOUDFLARE_TUNNEL_TOKEN`、`COMPOSE_PROFILES=tunnel`、五步設定說明 |
| `scripts/up.sh` | profile 啟用但 token 為空時，**在啟動前**擋下 |
| `scripts/status.sh` | 顯示 cloudflared 是否執行、日誌是否出現註冊成功訊息 |
| `scripts/lock-signup.sh` | 以 HTTP 讀取**真實**狀態；開著時透過 configs API 關閉，並讀回確認 |
| `README.md` / `README.zh-TW.md` | 「Securing remote access／對外連線的安全性」整節 |

**踩到的坑（三個，都已修正）**：

1. `docker-compose.yml` 原本想在 cloudflared 上用 `${CLOUDFLARE_TUNNEL_TOKEN:?...}`
   做必填驗證。**這是錯的** —— compose 會對**整個檔案**做變數插值，
   **即使該 profile 沒有啟用**也會觸發，結果是沒用 tunnel 的人反而起不來。
   驗證因此移到 `scripts/up.sh`。

2. **`lock-signup.sh` 原本會無聲地失敗 —— 而且第一次的修正修在錯的層。**
   最初的診斷是 compose 的變數優先序「shell 環境 > `.env` 檔」：`load_env`
   會用 `set -a` 把 `.env` 的舊值匯出到 shell，所以「改 `.env` → 重啟」
   會用舊值 `true` 重建容器。當時的修正是在改完檔案後 `export
   ENABLE_SIGNUP=false`，讓兩個來源一致。

   **這個修正方向沒錯，但打在錯的層。** 2026-09-19 以三次開機實測
   （`scripts/verify-lock-signup.sh`）證明：**改 `.env` + 重建容器之後，
   註冊仍然是開的**。真正的執法點讀的是**資料庫**（`auths.py` →
   `Config.get('ui.enable_signup')`），而環境變數只在資料庫「沒有」該 key
   時才生效 —— 第一次開機就已經寫進去了。DB 贏過 shell，也贏過檔案，
   所以 shell／檔案的優先序之爭從頭到尾都不重要。

   舊版還有第二個假成功路徑：它的冪等檢查讀的是 `.env` 而不是真實狀態，
   所以 `.env` 是 `false` 但實際開著時，它會印「已是 false，無需變更」就結束，
   連重啟都不做。

   **修正**：整支腳本改成以 HTTP 讀到的真實狀態為準，並在變更後讀回確認，
   見下方「2026-09-19 補記」。

3. **停用 profile 不等於停掉服務。** 使用者把 `COMPOSE_PROFILES=tunnel` 註解掉
   之後，已建立的 cloudflared 容器並不會自動停止，會繼續把服務對外 ——
   「我已經把 tunnel 關掉了」於是成為錯誤的認知。修正：`up.sh` 與 `down.sh`
   都加上 `--remove-orphans`。這在別的專案是清理動作，在這裡是安全措施。

**已離線驗證**（不需要 docker，因此可以當場做完）：

- `docker-compose.yml` 的 YAML 結構、`cloudflared` 的 profile 正確、未發布任何埠
- 沒有任何服務發布 Ollama 的 11434 埠（D-003 仍然成立）
- `lock-signup.sh` 的 `.env` 寫入邏輯，以六個案例實測：一般情況、已註解、
  完全沒有該鍵、重複套用（冪等，不會產生第二行）、行尾有註解、CRLF 換行
  —— 六者皆正確收斂為 `ENABLE_SIGNUP=false`
- 所有 shell 腳本 `bash -n` 通過；`scripts/test_verify_api.py` 全數通過

**未驗證（重要）**：

**需要 docker 才能執行的部分完全沒有跑過。** 原因：實作時 codespace 已依 D-001
刪除，且沒有可用的網域與 tunnel token。以下幾點**不可視為已驗證**：

- `cloudflared` 在本 repo 的 compose 設定下能否正常連上 Cloudflare
- `COMPOSE_PROFILES` 經 `load_env` 匯出後，各腳本是否都正確看見該 profile
- `status.sh` 對 cloudflared 日誌的判斷字串是否與實際輸出相符
- `--remove-orphans` 是否確實清掉停用 profile 後的殘留容器
- 「shell 環境 > `.env`」這條優先序規則來自 Docker 官方文件，**未在本機實測**
  （本機沒有 compose 外掛）。不過修正後的寫法不依賴這條規則 ——
  檔案與環境變數被設成同一個值，兩個來源一致。

下次有 codespace 時，**應先驗證這五項再進行 RAG**。這與 D-011 的教訓一致：
本專案已經兩次把「推論」寫成「結論」，不應再來第三次。

---

### 2026-09-19 補記：`lock-signup.sh` 實測結果（本機 docker，非 codespace）

使用者指示「先確認 lock-signup 是否真的有效」。查證方式是**實測，不是讀原始碼** ——
受測映像 `ghcr.io/open-webui/open-webui@sha256:1a6399d2…`（`:main`，2026-09-19 拉取），
docker 29.1.3，拋棄式容器與 volume，只綁 `127.0.0.1`。

**實驗一：`.env` 這條路有效嗎？**（`scripts/verify-lock-signup.sh`，三次開機共用同一 volume）

| 開機 | `ENABLE_SIGNUP` | volume | 觀測到的 `features.enable_signup` |
|---|---|---|---|
| 1 | `true` | 全新 | `true` |
| 2 | **`false`** | **同一個** | **`true`** ← `lock-signup.sh` 的情境 |
| 3 | `true` | 同一個 | `true` |

**結論：`.env` 的 `ENABLE_SIGNUP` 在 volume 建立後無效。** 改檔案、重建容器，
註冊仍然是開的。

**實驗二：那什麼才有效？**（`scripts/signup_control_probe.py`，13 項全數通過）

| 機制 | 實測結果 |
|---|---|
| **A. 第一位註冊者自動上鎖** | 成立。第一位註冊者取得 `role=admin`、使用者總數為 1，開關自動轉 `false`（`auths.py` 在 `get_num_users() == 1` 時 upsert） |
| **B. `POST /api/v1/configs/import`** | 成立，且**真的在執法**：開關為 `true` 時第二位註冊者被放行（HTTP 200），為 `false` 時**實際回傳 403** |
| **B 的安全性** | **部分更新，不是整份覆蓋。** import 前後設定項數目 398 → 398，只動傳入的 key（`configs.py` 是 `Config.upsert`） |

機制 B 的關鍵一步是**實際送出註冊請求撞它**，而不是只看 `/api/config` 的旗標。
旗標好看不等於端點在管 —— 要證明鎖得住，就得真的去撞。

**修正後的 `scripts/lock-signup.sh`**：

- 一律以 HTTP 讀到的真實狀態為準（`GET /api/config`，未認證端點，回傳的就是
  執法點讀的同一個值）。**不再讀 `.env` 來判斷狀態** —— 那正是舊版出錯的地方。
- 開著時：登入管理員 → 呼叫 configs API → **讀回確認**。HTTP 200 只代表請求被
  接受，不代表值真的變了，所以一律以重新讀到的值為準。
- 誠實的結束碼：`0` 已確認關閉／`1` 開著或關不掉／`2` 連不上。連不上時**不會**
  宣稱成功。
- 不再呼叫 `load_env`，因此不會產生 golden key 等副作用。
- `.env` 仍會被同步為 `false`，但僅對「將來的全新資料庫」有效，文件中已明確標示。

**實測涵蓋**（`scripts/verify_lock_signup_script.py`，全新容器與同容器重跑各一輪）：

| 情境 | 預期 | 結果 |
|---|---|---|
| 開著時 `--check` | 結束碼 1、明講開啟、**不誤報成功** | ✓ |
| 開著時 `--check` | 不改變任何狀態 | ✓ |
| 開著時 `--yes`（帶帳密） | 結束碼 0、有讀回確認、狀態確實變 false | ✓ |
| 已關閉時 `--yes` | 結束碼 0、**不做多餘的登入** | ✓ |
| 已關閉時 `--check` | 結束碼 0 | ✓ |
| **獨立驗證：實際送註冊請求** | **HTTP 403** | ✓ |
| 連不上時 `--check` | 結束碼 2、不宣稱成功 | ✓ |

**附帶風險：把 `.env.example` 改成 `false` 會不會把人鎖在外面？**
（`scripts/verify-first-admin.sh`）

既然 `.env` 的值只在第一次開機生效，那麼「第一次開機」這個唯一有效的時機就必須
是對的。舊版 `.env.example` 是 `true`，改成 `false` 之後就產生一個新的失敗模式：
**全新安裝若被 `false` 擋住，使用者會沒有任何帳號可登入，也就沒有任何方法改回來。**

原始碼顯示不會擋（`auths.py` 對「還沒有任何使用者」另走一條分支，只檢查
`enable_login_form`，其預設為 `True`，與 `enable_signup` 無關）。但這正是
「讀起來很合理、錯了卻會讓人完全進不去」的那種地方，所以照樣實測：

| 觀測點 | 結果 |
|---|---|
| `features.enable_signup`（全新 volume、`ENABLE_SIGNUP=false`） | `false` |
| `POST /api/v1/auths/signup` | **HTTP 200** |
| 回應中的 `role` | **`admin`** |

**結論：`ENABLE_SIGNUP=false` 不會擋住第一位管理員，`.env.example` 可以安全地預設
`false`。** 而且第一位註冊者仍然是 admin —— 這件事只有在「全新資料庫」時成立；
已有使用者之後 `enable_signup` 就開始擋人，那正是它應該做的事。

**這一項仍然要說清楚**：以上驗證是在**本機 docker** 上完成的，不是 codespace。
D-001 的 codespace 已刪除，所以「在 codespace 裡跑起來會如何」仍未驗證；
但這些行為只與 Open WebUI 的 API 有關，與執行環境無關。

**同時確認了一個文件錯誤**：`ENABLE_MCP` **不存在於 Open WebUI**。官方文件與
後端原始碼皆無此變數，MCP 是在 Admin → External Tools 新增連線（寫進資料庫），
不是環境變數。本 repo 有五處斷言它存在，已一併更正。

**方法論上的教訓（第三次了）**：這次的答案與第一次的診斷**不同**。
第一次說「是 compose 優先序」，第二次說「是資料庫持久化」。兩者都讀得出來，
但只有實測能分辨哪一個才是真正在起作用的那一層。D-011 的教訓是「不要推論」，
這次補上的是更精確的版本：**推論出一個「合理的原因」不代表找到了原因。**

---

## D-013：嵌入引擎採 ollama，模型採 qwen3-embedding:0.6b

**日期**：2026-09-19
**狀態**：已決定（引擎與模型），套用方式待執行
**推翻了什麼**：D-008 選定的 `nomic-embed-text` —— 那是未經量測的選擇

### 問題

D-008 指出嵌入模型必須在上傳文件前決定，但當時的選擇來自「有回報說」而非量測。
本專案已經吃過三次「推論出一個合理的原因不代表找到了原因」的虧（D-011、D-012），
所以這次把引擎與模型都變成可量測的問題。

**而且問題是現在進行式。** 實測資料庫現況（本機 docker，2026-09-19）：

```
rag.embedding_engine = ""                                        ← SentenceTransformers（預設）
rag.embedding_model  = "sentence-transformers/all-MiniLM-L6-v2"  ← 僅支援英文
```

也就是說，在做出這個決定之前，這套堆疊**正以英文模型服務繁體中文文件**。

### 量測工具

`scripts/rag_probe.py`（由 `scripts/rag_probe.sh` 送進 open-webui 容器執行）。
8 題繁體／簡體對照題，每題 1 正解 + 3 個「**主題相同、只差一個屬性**」的干擾項。
後者是關鍵：只比對主題的模型在這種題目上會系統性選錯，而那正是 RAG 最常見的失敗模式。
評分邏輯本身另有離線測試 `scripts/test_rag_probe.py`（57 項全過）——
量測工具本身必須先被量測，否則它會回報一組看起來很合理的數字，而我們會照著做決定。

### 引擎：ollama

同一顆模型（`qwen3-embedding:0.6b`）、同一份工作量（32 次呼叫、80 段文字）：

| 引擎 | 繁體 Top-1 | 簡體 Top-1 | 判讀 | 時間 |
|---|---|---|---|---|
| **ollama** | 100% | 75% | 足夠，無簡體偏差 | 40.0s（2.0 段/秒） |
| st（SentenceTransformers） | 100% | 75% | 足夠，無簡體偏差 | 79.5s（1.0 段/秒） |

**品質四項（Top-1／Top-3／MRR／判讀）完全相同**，只有平均領先幅度差在小數第三位
（ollama 是量化權重、st 是 fp32，這是預期內的差異）。

決定性理由不是速度，是**失敗模式**：

- **ollama**：模型是可列舉的。`rag_probe.sh` 在跑之前就用 `ollama list` 檢查，
  缺了直接失敗並叫使用者去 pull。
- **st**：模型在容器內首次使用時才抓。Open WebUI 原始碼對此的註解是
  *"Deferred so a missing local model degrades RAG instead of crashing boot."*
  —— 官方選擇了「安靜地降級 RAG」，而不是「開機時就失敗」。

**一個抓不到模型的嵌入引擎，會讓 RAG 安靜地回傳爛結果，而不是告訴你它壞了。**
這比慢一倍難查得多，而本專案已經被靜默失敗咬過兩次（D-011）。

「安靜」不只是設計哲學，是實測過的：模型下載在日誌上**完全看不見**，
因為 HuggingFace 的進度條在 stderr 非 TTY 時會自動關閉（`disable=None`）。
我因此一度把下載時間誤讀成編碼時間 —— 見下方「量測的極限」第 2 點。

附帶好處：ollama 的模型記憶體活在**獨立容器**（有自己的 restart policy），
st 的則活在網頁伺服器行程內。Open WebUI 官方文件本身也建議用外部引擎
把嵌入模型移出 webui 行程。

### 模型：qwen3-embedding:0.6b

| 模型 | 繁體 Top-1 | 簡體 Top-1 | 落差 | 繁體 MRR | 磁碟 | 上下文 |
|---|---|---|---|---|---|---|
| **qwen3-embedding:0.6b** | **100%**（8/8） | 75%（6/8） | −25% | **1.00** | **639 MB** | 32768 |
| bge-m3 | 88%（7/8） | 75%（6/8） | −12% | 0.94 | 1.2 GB | 8192 |

兩者的判讀都是「繁體鑑別力足夠，且未見簡體偏差」。
失敗的題目**不一樣**，所以不是哪一顆有系統性弱點：

| | 繁體失敗題 | 簡體失敗題 |
|---|---|---|
| qwen3-embedding:0.6b | 無 | `typhoon-closure`、`income-tax-deduction` |
| bge-m3 | `income-tax-deduction` | `typhoon-closure`、`scooter-licence` |

**「落差」的方向要說清楚**：落差 = 簡體 − 繁體。負值代表繁體分數**高於**簡體，
也就是沒有簡體偏差 —— 這正是本探針要偵測的東西。bge-m3 的落差「比較小」（−12%）
**不是優點**，那只是因為它的繁體比較低，不是因為它比較中立。

### 量測的極限（這一節比上面的數字重要）

1. **N=8，一題就是 12.5%。** qwen 與 bge-m3 的差別是**一題**（合計 14/16 vs 13/16）。
   探針自己就會印出這句警告。**一題的差距不足以判定優劣** ——
   所以「qwen 比較準」不是我能在這裡下的結論，兩者都在同一個解析度格點上。

2. **計時在這個環境不可重現。** 同一顆模型、同一份工作量，兩次量測差了一倍：

   | 模型 | 第一次 | 第二次 |
   |---|---|---|
   | qwen3-embedding:0.6b | 40.0s | 20.1s |
   | bge-m3 | 14.2s | 22.2s |

   兩者的範圍**完全重疊**。第一次量測顯示 bge-m3 快 2.8 倍，重測之後那個優勢就消失了。
   2 vCPU 的 Codespace 上，單次計時不足以排序任何東西。
   （st 引擎最初量到的 296.9s 也是同一個坑：其中約 217s 是模型下載，乾淨的重測是 79.5s。
   我先把 7.4 倍報了出去，重測後才發現是 2.0 倍。）

3. **品質是確定性的，速度不是。** 每顆模型跑兩次，分數與失敗題目**完全相同**。
   所以品質不需要重測，而速度必須 —— 這件事要寫下來，因為下一次做類似量測的人
   會直接踩上去。

### 決策

**引擎 `ollama`、模型 `qwen3-embedding:0.6b`。**

速度和品質都無法分辨兩顆模型的情況下，決定性的是唯一一個確定性的差異：
**qwen3-embedding:0.6b 的磁碟佔用是 bge-m3 的一半**（639MB vs 1.2GB）。
D-009 已記錄本專案對儲存額度採保守策略，而這套堆疊跑在免費額度上。

**必須說清楚：這不是「qwen 比較好」，而是「量不出 bge-m3 好在哪，而它貴一倍」。**
若日後語料變大、或實際使用中發現繁體檢索品質不足，bge-m3 是**已經量過的**替代方案
（換模型需重新嵌入所有既有文件，見 D-008）。

### 如何套用（這裡有個陷阱）

**光在 `.env` 設 `RAG_EMBEDDING_ENGINE` 不會生效。** 這次是從原始碼確認的，
不只是沿用 D-012 的結論：

- `models/config.py` 的 `Config.get()`：**資料庫有值就用資料庫，沒有才用 env 預設**。
- 開機時的 `seed_registered_defaults()` → `Config.seed_defaults()`，其 docstring 明寫
  *"Insert keys that don't yet exist in the DB. **Existing DB values take precedence
  over defaults.**"* —— 只補「資料庫還沒有」的鍵，不覆蓋既有的。

`rag.embedding_engine` 與 `rag.embedding_model` 兩列**已經存在**於 `config` 表
（該表共 345 個鍵），所以改 `.env` 再重建容器不會有任何作用。

正確順序：

1. `bash scripts/pull-model.sh qwen3-embedding:0.6b` —— 把模型放進 ollama
2. 從 **Admin → Settings → Documents → Embedding** 改引擎與模型
   （或改 `config` 表中那兩個鍵；管理 API 走的是同一條 `Config.upsert()` 路徑）
3. **在改完之前不要上傳任何文件** —— 換模型需重新嵌入所有既有文件（D-008）

**唯一可用環境變數控制的相關設定是 `RAG_EMBEDDING_QUERY_PREFIX`**（以及
`..._CONTENT_PREFIX`）：它們是純 `os.getenv`，不進資料庫，每次行程啟動時讀取。
qwen3-embedding 官方建議查詢加上 `Instruct: <任務>\nQuery: ` 前綴，本次量測**沒有加**，
所以實測分數是「不加前綴」的保守值 —— 要套用就設這個變數。

### 更正 D-008

1. **`nomic-embed-text` 的選擇是未經量測的。** 本決策以量測取代它。
2. **「較大的嵌入模型會讓 RAM 從 2GB 暴增至 14GB」是 `jinaai/jina-embeddings-v3`
   的個案，不是嵌入模型的通性。** 該數字出自 Open WebUI Discussion #10014 的
   一位使用者，而 jina-v3 是 570M 參數、Jina 自家的 on-prem cheat sheet 標示約 3GB VRAM。
   把它寫成一般性風險，會讓日後的人無謂地害怕 bge-m3（566.70M 參數）——
   而實測顯示 bge-m3 在 8GB 環境上運作正常，兩次都跑完、沒有 OOM。
   但**它不是沒有代價的**：載入後 ollama 容器佔 1.29GiB，主機合計已用 2.6–2.9Gi / 3.8Gi
   （同一時間 open-webui 只佔 60–125MiB）。作為對照，qwen3-embedding:0.6b 場次
   跑完主機是 1.6Gi。這個差距在 2 vCPU / 8GB 的環境上不算小，
   是支持「選一半大小的模型」的另一個理由。

   真正該留下的一般性教訓是另一半：**SentenceTransformers 引擎把模型載進 webui
   行程，每個 uvicorn worker 各載一份**。這才是官方建議改用外部引擎的理由，
   也是本決策選 ollama 的原因之一。

### 已知風險

- 探針只測了 8 題短句，**沒有**測長文件、多語言混雜、或大規模語料的行為
- 上表的分數是「不加 query 前綴」的保守值（見上方套用說明）
- 嵌入模型一旦定案並開始上傳文件就難以回頭（D-008）
- 以上驗證在**本機 docker** 完成，非 codespace（D-001 的 codespace 已刪除）
- **尚未做**：把設定實際改過去。本決策只到「決定」為止
