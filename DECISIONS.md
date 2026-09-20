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
**狀態**：**已複核（2026-09-19）** —— 第一階段已完成，且本決策的延後條件已被援引。
見文末更正。

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

> **2026-09-19 更正：這一項已經有答案，不再是未知數。**
> D-011 實測多輪 tool calling **通過**，因此本行原寫的「最需要實測的假設」已失效。
> README 對應的「本階段最關鍵的未知數」一併移除（D-014 決策 5）。
>
> **這則更正延遲了近一天，原因值得記錄。** D-011 內文早已明寫
> 「**D-005 的未知已解答**：多輪 tool calling 成立」，也就是答案當時就寫下來了 ——
> 但**答案沒有被寫回提出問題的那份文件**，於是 D-005 單獨讀起來仍在說「待驗證」。
> 這與 D-014 是同一種失效：下了結論，卻沒有回頭更新作出該宣稱的地方。
> 只讀 D-005 的人會被誤導，而文件之間不會互相提醒。
>
> **但 D-011 的答案有適用範圍，而那個範圍決定了第三階段。** 那份證據是在
> **Open WebUI 自己的 tool calling 流程**中取得的。LangGraph 是透過 `bind_tools` /
> Ollama 整合的原生 function calling 呼叫工具，那是**完全不同的程式路徑** ——
> 因此「多輪 tool calling 會通過」這個結論**不會自動轉移到第三階段**，
> 必須在 agent 框架內重新量測。這正是 D-014 在講的失效模式：
> 在某組條件下量到的結果，被記成結論，然後套用到它從未在那組條件下量過的地方。
>
> **狀態變更的理由**：本決策把 LangGraph 延後到「出現需要自訂多步驟 workflow 或
> 明確狀態機的需求時」。該條件已由第三階段（狀態機）**刻意援引**，而非被悄悄假設掉，
> 因此本決策進入複核 —— 這是延後條件的成立，不是對本決策的推翻。
> 第三、四階段的規劃與六個未驗證項見 README「第三與第四階段 —— 計畫中，尚未量測」。
>
> **D-007 不受影響**：`langgraph` **函式庫**是 MIT 可自由使用；
> `langgraph-server` / `langgraph-api` 商業容器映像檔為 Elastic License 2.0，
> 仍不採用。

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
**狀態**：已決定並已套用（2026-09-19）
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
- **已套用（2026-09-19）**：`bash scripts/set-embedding.sh ollama qwen3-embedding:0.6b`
  —— 讀回驗證通過，且從 open-webui 容器實際取得一個 1024 維向量（端到端）。
  此處原本寫「尚未做」，套用後沒有同步更新；本行於同日更正

---

## D-014：事實正確性必須機械檢查，且事實性問題不得依賴模型內部知識

**日期**：2026-09-19
**狀態**：已量測（本機 docker，非 codespace）；`/no_think` 的效果未確認（見第五節）

**背景**：Phase 2 的主軸是 MCP。2026-09-19 對 qwen3:4b 提問「用三句話解釋 MCP 是什麼」，
得到錯誤答案。追查後發現這**不是新問題** —— D-011 已在一天前記錄過兩次 MCP 幻覺。
真正的問題是：**那個教訓從來沒有變成判準。**

### 一、D-011 發現了幻覺，但沒把它寫成檢查

D-011 的原文：

> **第二次獨立的幻覺證據**：1.7b 說 MCP 是「多選題」，4b 說是 "Microsoft Client Protocol"。
> 兩顆模型、兩種不同的錯法、都講得理直氣壯、格式都完美。
> **這是 Agent 平台最危險的失敗模式：看起來對的錯答案。**

而 `scripts/verify_api.py` 的測試 5（第 424–481 行），題目正是：

```python
prompt = "用一句話說明什麼是 MCP。"
```

它的判定只有三項 —— 有沒有簡化字、有沒有複述題目、有沒有被截斷。
**沒有一項檢查答案的內容是否為真。**

因此：一個把 MCP 定義錯的模型，只要用繁體字寫、不複述題目、沒被截斷，就會通過。
`verify.sh` 會印出「✓ 驗證完成，關鍵項目全數通過」。

**D-011 是在人眼看測試輸出時發現幻覺的，不是測試自己判出來的。**
結論留在散文裡，沒有變成判準 —— 於是下一個人跑同一支測試，拿到的仍然是綠燈。

### 二、README 把這個缺口寫成了前提

README「Verification checklist」開頭：

> This project has **no automated tests**. ... its core behaviour — inference quality,
> MCP tool-calling reliability — is **inherently a human judgement call**.

前半句與事實不符（`test_rag_probe.py` 57 項、`verify_api.py` 5 項、
`test_verify_api.py`、`verify_lock_signup_script.py` 都在版控裡）。
後半句才是問題：「答案裡有沒有出現 Model Context Protocol」**可以機械判定**，
但一旦把它歸類成「人類判斷」，就沒有人會去判。

**那是一個判斷，不是一個事實** —— 而它決定了後面所有事的走向。

### 三、新增量測工具

`scripts/ask_probe.py` + `scripts/ask_probe.sh`，與 `rag_probe` 同一種形式
（送入容器執行，因為 D-003 不發布 11434 埠）。兩個設計要點：

**1. 機械評分。** 每題附 `expect_all` 字串清單，由程式判定，人眼只負責覆核
程式判出來的結果。理由見第一節。

**2. 對照題。** 每顆受測模型都要另外回答一題「早於所有候選模型訓練截止」的題目。
只問「MCP 是什麼」而得到錯答案，無法區分：

| | 假設 | 處置 |
|---|---|---|
| (a) | 這顆模型不可靠，什麼都答錯 | 換模型 |
| (b) | 訓練資料早於 MCP 發布（2024-11），根本沒見過 | 換同級模型沒有幫助 |

**沒有對照題就無法區分，而兩者的處置相反。**

### 四、量測結果

本機 docker（3.8GB）、Ollama 0.34.2、`num_predict` 1024、未給系統提示：

| | qwen3:4b | qwen2.5:3b |
|---|---|---|
| MCP | ✗ `response` 為空（見第五節） | ✗ 「MCP 是一個由**阿里云**提供的模型」 |
| **HTTP（對照題）** | ✗ `response` 為空（見第五節） | **✓ 正確** |
| 總牆鐘 | 264–293 s | 7–15 s |
| 生成速率 | 3.78–3.97 tok/s | 6.38–6.85 tok/s |
| 結束原因 | `length`（截斷） | `stop`（自然結束） |
| 簡體字 | — | 16 次／12 種 |

**對照題把變因隔離出來了。** qwen2.5:3b 同一顆模型、同一組條件：HTTP 答對、MCP 答錯。
所以問題**不是「這顆模型不可靠」**（那 HTTP 也該錯），而是**「MCP 不在它的知識裡」**。

它把 MCP 說成「阿里云提供的模型」—— Qwen 是阿里雲的，所以它從最接近的既有概念編了一個。

> **推論：換模型不解決這個問題。** 這個級距裡任何訓練截止早於 2024-11 的模型都不知道
> MCP。（此為推論而非實測，見「已知風險」。）

### 五、qwen3:4b 的 response 為空：已確認，且取得直接證據

本次量測中，qwen3:4b 兩題都以 `done_reason: length` 結束、`response` 長度為 0。
原本列為待確認（因為下判定的是同日剛寫的 `ask_probe.py`，它也可能讀錯欄位），
已用獨立執行確認。原始欄位傾印：

```
頂層欄位：['context', 'created_at', 'done', 'done_reason', 'eval_count',
           'eval_duration', 'load_duration', 'model', 'prompt_eval_cached_count',
           'prompt_eval_count', 'prompt_eval_duration', 'response', 'thinking',
           'total_duration']
done_reason      : length
eval_count       : 256
response 長度    : 0
thinking 欄位存在: True
thinking 長度    : 437
```

**`response` 確實是空的，不是工具讀錯欄位。** 256 個 token 全數落在 `thinking`，
`done_reason: length` 表示還沒想完就撞上上限。這同時確認了三件 D-011 已記載的事：
thinking 被隔離在獨立欄位、模型需要大量 token 才能想完、以及**思考內容預設是簡體**
（用户、让、解释、什么、网络、协议、认证）—— 而提問是繁體。

**但這次傾印給出了比預期更強的東西。** `thinking` 的開頭是：

> 嗯，用户让我用三句话解释MCP是什么。首先得确定MCP具体指什么，因为缩写可能有多种含义。
> **常见的MCP有微软的MCP认证、机械臂的MCP、或者网络协议中的MCP。**不过用户没给上下文，得选最普遍的。

**模型在內部推理裡列舉 MCP 的可能含義 —— 而 Model Context Protocol 不在其中。**
它列的是「微軟的 MCP 認證」「機械臂的 MCP」「網路協定中的 MCP」。

這是目前為止最直接的證據：不是「它答錯了」，而是**它在自己的知識裡搜尋過，沒找到**。
第四節的「知識不存在」因此不只是從錯誤答案反推的推論，而是模型自己顯露出來的。

**仍未確認的是 `/no_think` 的效果。** `ask_probe.py` 沒有輸出 `thinking` 欄位的長度，
因此無法判斷加了前綴的那兩次，1024 個 token 究竟落在 `thinking` 還是別處 ——
**這是工具本身的缺口，應補上，否則下一次仍然無法判讀。**

另外注意 D-011 是把 `/no_think` **接在提示結尾**（`verify_api.py:348`），
本次放在**開頭**。擺放位置可能是變因，但目前的證據不足以判定。

### 六、速度：20 倍差距來自 thinking

| 模型 | 推理模型 | 同題牆鐘 | 速率 |
|---|---|---|---|
| qwen3:4b | 是 | 264–293 s | 3.78–3.97 tok/s |
| qwen2.5:3b | 否 | 7–15 s | 6.38–6.85 tok/s |

**README 的「10–30 秒」標準做得到 —— 只要不用 thinking 模型。**

D-011 決策「維持 thinking 開啟」是因為三種關閉方式全部失效。那個決策本身沒有錯，
但它**沒有把 20 倍延遲算成成本**。本決策補上這一項，不推翻 D-011。

**速度校正**：D-011 在 8GB Codespace 量到 4.5–5.1 tok/s；本決策在本機 3.8GB
（有 swap）量到 3.78–4.90 tok/s。**兩者落在同一區間** —— 推論速率由 2 vCPU 決定，
不由記憶體決定。因此 D-011 的「單題 30–110 秒」在 8GB 上同樣成立，不是本機的假象。

### 決策

1. **事實正確性必須機械檢查。** 任何「模型知不知道 X」的驗證都要附 `expect_all`
   字串清單，且**必須包含一題對照題**，用來隔離「模型不可靠」與「知識不存在」。
   加進 `scripts/verify.sh`。

2. **事實性問題不得依賴模型的內部知識。** D-011 的結論升級為判準 ——
   **RAG 是 Phase 2 的前提，不是 Phase 2 的一個功能。**
   本決策提供了機制：不是模型不乖，是知識不存在，且無法靠換模型修補。

3. **README 的「no automated tests / inherently a human judgement call」必須刪除。**
   它與事實不符，而且那個前提正是缺口成立的原因。

4. **Phase 1 驗收的「10–30 秒」必須改。** 現行文字與 D-011 實測的 30–110 秒直接
   衝突，而這個衝突在兩份文件並存一整天後才被發現。改為反映實測，並註明 thinking
   是主因。

5. **Phase 2 MCP 的「critical unknown」必須移除。** D-011 已驗證多輪 tool calling
   通過，README 卻仍把「4B 模型能否多輪 tool calling」列為未知。

### 如何套用

- `verify.sh` 增加事實正確性測試（決策 1）
- README 依決策 3、4、5 改寫
- 對照題必須隨時間更新：**任何「晚於候選模型訓練截止」的事實都需要對照題**，
  否則答錯的原因無法判讀

### 更正 D-011

不推翻，補一項成本：

- D-011 的模型比較表顯示 qwen3:1.7b 快 2.1 倍、tool calling 也通過。但**它同樣把
  MCP 說成「多選題」**。這反而**加強**了本決策的結論：換模型不解決知識問題。
- D-011 的「維持 thinking 開啟」沒有計入 20 倍延遲（見第六節）。決策不變，
  但成本要記在帳上。

### 已知風險

- 對照題只有一題（HTTP），單題不足以代表「模型的知識整體」
- **qwen2.5:3b 的 tool calling 完全未測。** 它速度快、對照題通過，但 D-005 的前提
  是多輪 tool calling，本決策沒有驗證它 —— 不可用來支持「換成 qwen2.5:3b」
- 繁體輸出：本次未給系統提示，qwen2.5:3b 輸出大量簡體（16 次）。D-011 已證實系統
  提示可以修正，但本決策未重測
- 「訓練截止早於 2024-11 的模型都不知道 MCP」是**從訓練截止推論的，不是逐一實測**。
  本專案的標準是實測（見 D-011「共同根源」），這一項是例外，故明記於此
- qwen3:4b 的空白回應已確認成因，但**加 `/no_think` 前綴的那兩次，token 流向何處
  仍未確認**（`ask_probe.py` 不輸出 `thinking` 長度，見第五節）
- 第五節的 thinking 原文取自**未加前綴**的那一次，與第四節表格的條件不完全相同
- 以上全部在**本機 docker（3.8GB）** 完成，非 codespace

---

## D-015：可搬移性是架構約束，不是未來的重構工作

**日期**：2026-09-19
**狀態**：已檢查並已修正（部分項目刻意不修，見決策 4）

**背景**：本專案目前跑在 GitHub Codespaces 的免費額度內，用 qwen3:4b 驗證。
但長期目標不是「在 Codespaces 上跑小模型」，而是**一套可搬移的私有 AI 平台** ——
等到有足夠 CPU/RAM/GPU 的 VPS，就把整套搬過去、換上更大的本地模型，
像使用 ChatGPT 一樣使用它，而資料、RAG、MCP、Agent、Memory 與工作流程
都自己控制。

**為什麼這件事現在就要處理**：可搬移性不是「以後再重構」的題目。綁死在
POC 環境上的假設，會在搬家的那一刻同時失效，而那正是最不適合除錯的時候。
本決策把可搬移性升格為**架構約束**：現在做的每個選擇，都要能回答
「搬到 VPS 時這一行會怎樣」。

**檢查結果**（2026-09-19，逐項對照長期目標）：

| 項目 | 結果 |
|---|---|
| Ollama 11434 不對外發布 | ✅ 已符合（D-003） |
| Cloudflare Tunnel 保留為對外方式 | ✅ 已符合（走 profile，對外撥接、不需 inbound 埠） |
| 模型可替換 | ✅ `OLLAMA_MODEL` 是唯一提到模型名的地方 |
| 上層功能不需重寫 | ✅ Open WebUI 的狀態全在具名 volume 內 |
| 不綁死 8GB 機器 | ⚠️ **違反** —— 資源限制寫死在 compose |
| 對外暴露 | 🔴 **違反** —— 3000 埠綁 0.0.0.0，在 VPS 上繞過 Cloudflare Access |
| 搬遷路徑 | ❌ **完全沒有記載** |

### 決策

1. **資源限制一律可設定，不寫死。** `OLLAMA_MAX_LOADED_MODELS` 與
   `OLLAMA_NUM_PARALLEL` 原為寫死的 `1`（為 8GB 機器調的），改為走 `.env`，
   預設值不變。理由不只是方便：寫死會讓「換 VPS 只要改 .env」變成
   「換 VPS 要改 compose」，而後者讓改動散進版控、難以回溯。

2. **對外埠的綁定位址可設定，且必須機械檢查。** 新增 `WEBUI_BIND_ADDR`
   （預設 `0.0.0.0`，維持 Codespaces 現行行為）。同時新增
   `scripts/check-exposure.sh`。

   **為什麼要機械檢查，而不是寫在文件裡就好**：同一份 compose 在 Codespaces
   安全、在 VPS 危險 —— 差別不在檔案，在**機器在哪**。一個在 Codespaces 上
   反覆驗證過「沒問題」的設定，會在搬家那一刻變成問題，而搬家的時候沒有人
   會回頭懷疑一個用了很久、從沒出過事的設定。這正是 D-014 的形狀：
   **條件變了，結論沒跟著變。**

3. **`check-exposure.sh` 必須區分「對 Internet 開放」與「只有區網可達」。**
   它讀執行中容器的**實際綁定**（`docker port`，非 compose 宣告），
   並檢查本機有無公開位址。一個在安全情境下也照樣報紅的檢查最後會被忽略，
   而被忽略的檢查等於沒有檢查（同 `SIMPLIFIED_HINTS` 的假失敗教訓）。

4. **驗證工具與 Ollama 原生 API 的綁定，刻意不修。** `verify_api.py` 與
   `ask_probe.py` 呼叫 `/api/generate` 並讀取 `thinking`、`done_reason`、
   `eval_count`、`load_duration` —— 這些欄位 vLLM 沒有。
   換 runtime 就得重寫這兩支。**這是被接受的成本，不是被忽略的缺陷**：
   上層功能（Open WebUI、RAG、MCP、Memory、Agent）不受影響，
   而驗證工具本來就是針對當下 runtime 寫的。現在為一個還不存在的 vLLM
   做抽象化，正是要避免的過度工程。

5. **換 runtime 走 Open WebUI 的 OpenAI 相容連線，不是改本專案。**
   Open WebUI 用 `OLLAMA_BASE_URL` 連 Ollama，但用 OpenAI 相容連線連 vLLM。
   兩者可並存，因此搬遷可以是漸進的：兩邊都跑、比較、再移除不要的。
   本專案不需為此改任何東西 —— 這是「不綁死」的具體證據。

6. **README 新增「搬到 VPS」一節**（中英同步），記載可原樣帶走的部分、
   兩個必經步驟、以及四項真正帶不走的成本。

### 已知風險

- **`WEBUI_BIND_ADDR` 的預設值仍是 `0.0.0.0`。** 這是刻意的 ——
  改預設會影響 Codespaces 目前的埠轉送行為，而那需要在 Codespaces 上實測
  （本機測不到）。**因此預設值目前對 VPS 是不安全的**，靠第 1 步與
  `check-exposure.sh` 攔截。這是本決策唯一「知道有問題但留著」的地方。
- `check-exposure.sh` 只檢查容器綁在哪，**不檢查防火牆**。雲端安全群組與
  主機防火牆是使用者的責任，腳本結尾已明講。
- 本檢查針對的是**部署形態**，不是程式碼品質。上層功能的實際可搬移性
  （例如把 volume 複製到新機器後 Open WebUI 是否真的正常）**尚未實測** ——
  要等真的有 VPS 才能驗。在那之前，「可以原樣帶走」是查證架構後的判斷，
  不是實測結果。

---

## D-016：評分方式必須與問句對齊，且「無法判定」不得併入「未通過」

**日期**：2026-09-19
**狀態**：已實測並已修正（qwen2.5:3b 驗證修正成立；qwen3:4b 的結果見第五節）

**背景**：D-014 決策 1 要求事實正確性必須機械檢查，且必須附一題對照題。
該決策於 2026-09-19 實作進 `scripts/verify_api.py` 的測試 5（commit `225baab`）。
同日實測 `bash scripts/verify.sh 5`，**這套評分在第一次真正跑到時就抓到自己的缺陷**。

### 一、發現 1：對照題假失敗，工具據此叫人換模型

`qwen2.5:3b` 對對照題的回答：

```
題目：用一句話說明什麼是 HTTP。
輸出：HTTP 是一組 rules 和約定，用於網際網路傳輸超連結檔案。
✗ 事實不正確 —— 缺少關鍵字串：
    hypertext transfer protocol／超文本傳輸協定／超文本传输协议／超文本傳送協定
```

**這個答案是正確的。** 它只是沒有把縮寫的全名拼出來。工具於是印出：

```
事實正確性 : 兩題皆不正確 → 模型整體不可靠（對照題也不過，這才是換模型的訊號）
```

**根據一個假失敗，建議使用者換模型。**

根因是**問句與評分不對稱**：問句「用一句話說明 X 是什麼」是開放題，
答對不必出現全名；但 `expect_all` 比對的正是全名。這與 D-014 自己
在 `expect_all` 註解裡寫下的警告（「假失敗比漏報更糟」）是同一件事 ——
**警告寫下來了，卻在同一個檔案裡被違反。**

而且這個缺陷是**對稱的**：目標題也會因為答對而沒拼全名，
被判成「知識不存在」——那會反過來誤導我們去建一個不需要的 RAG。

### 二、發現 2：「無法判定」被併入「未通過」

`verify_api.py` 的 `tri()` 早已寫明五種狀態必須分得開
（未執行／執行失敗／無法判定／通過／未通過），並註明
「把它們併成『未通過』會讓人以為功能退步；併成『無法判定』則會掩蓋真正的錯誤。
這兩個缺陷都是 2026-09-18 實測時自己製造出來的。」

但整體結論那段是 `tri()` 管不到的地方：`control_ok is None`（無法判定）
會落到 `else` 分支，印出**「有關鍵項目未通過，第二階段需調整模型或策略」**。
`qwen3:4b` 兩題都回傳空的 `response`，走的就是這條路 ——
於是「測不出來」被講成「該換模型」，而兩者的處置**正好相反**。

### 三、決策

1. **問句改為直接問全名**（「MCP 的英文全名是什麼？」），讓問句與評分對齊。
   目標題與對照題必須維持**同一個形狀** —— 問法一旦不對稱，
   隔離「模型不可靠」與「知識不存在」的能力就沒了，而那是對照題唯一的用途。
2. **無法判定獨立成一條路徑**，不再落入「未通過」，並在輸出中直接說明
   最常見的成因（`response` 為空＝thinking 吃光 `num_predict` 額度）。
3. **結束碼改為三分**：`0` = 關鍵項目全數通過、`1` = 有關鍵項目未通過、
   `2` = 無法判定。`verify.sh` 據此顯示不同訊息。
4. **部分執行（`verify.sh 5`）不再一律回 0** —— 跑過的項目若無法判定則回 2。
   一律回 0 會讓下一個讀結束碼的人或腳本把它當成通過，
   正是本專案一再吃虧的假通過形狀（同 VERIFY_ONLY 那次）。

### 四、修正後的實測（qwen2.5:3b）

| 題 | 修正前 | 修正後 |
|---|---|---|
| http（對照題） | ✗ 假失敗 | **✓ 事實正確** ——「HTTP 的英文全名是 HyperText Transfer Protocol。」 |
| mcp（目標題） | ✗ | ✗（真陽性保留） |

判讀輸出：**「目標題不正確、對照題正確 → 知識不存在，非模型不可靠（D-014）」** ——
這是**第一次由工具機械產出 D-014 的結論**；D-014 當初是靠人眼讀 raw output 得到的。

值得注意的是，目標題三次執行給了**三個不同的錯答案**：

| 來源 | qwen2.5:3b 的 MCP 答案 |
|---|---|
| D-014（人眼判讀） | MCP 是一個由阿里云提供的模型 |
| 本次第一次執行 | MCP是指微控制器平台（Microcontroller Platform） |
| 本次第二次執行 | MCP 的英文全名是 Master Control Panel |

**錯得穩定（永遠答錯），但錯法每次不同。** 這是「瞎猜」的形狀，
不是持有某個特定錯誤觀念 —— 也就不能指望靠提示詞修正。
這同時印證了為什麼問句必須讓關鍵字比對成立：模型答對時會拼出全名，
答錯時會拼出**另一個聽起來同樣合理的全名**，兩者用同一個字串清單就能分開。

### 五、未解的問題

- **`qwen3:4b` 仍然無法判定，窄問句沒有改善它。** 新問句的答案只有 12 tokens
  （`qwen2.5:3b` 實測），理論上留給 thinking 的壓力較小，但 2026-09-19 實測
  `qwen3:4b` 仍然兩題都把 1024 tokens 的 `num_predict` 額度用盡、`response` 全空
  （MCP 331.9s / 3.09 tok/s、HTTP 266.1s / 3.85 tok/s）。
  **問句變窄並沒有讓它少想**；該調的是 `num_predict` 或測法，**不是換模型** ——
  這正是決策 2 要分開的兩件事。

  這一輪同時驗證了新的結束碼路徑：`verify.sh 5 qwen3:4b` 回傳 **2**，
  `verify.sh` 顯示「部分測試無法判定」而非「失敗」—— 修正前它會回 0。

- **但答案在 `thinking` 欄位裡。** HTTP 對照題的 thinking 開頭是
  「HTTP 是 HyperText Transfer Protocol 的縮寫。所以，英文全名應該是…」——
  **模型知道答案**，只是永遠沒走到作答那一步。MCP 的 thinking 則仍是列舉
  「MCP 在不同上下文中可能有不同的全稱…在技術或商業領域中，MCP 有幾個常…」。

  這指向一個具體的改進：`response` 為空時改判 `thinking`。D-014 第五節
  當初正是**用人眼讀 thinking** 才得到結論的 —— 那一步可以機械化。
  **尚未實作。** 注意 thinking 是**較弱**的證據：模型可能在 thinking 中列出
  正解，作答時卻選了別的。

- **本輪的時間數字不可與上一輪比較。** 執行期間 `Swap` 累積到 2.1Gi（共 3.8Gi）、
  `available` 只剩 102Mi，機器在換頁。同一題由 272.2s 變成 331.9s 是環境造成的，
  不是問句造成的。**內容判定仍有效，時間比較無效。**

- 本次修正**沒有改變** D-014 的核心結論（MCP 知識不存在 → RAG 是前提）。
  它改變的是**取得那個結論的方式**：從人眼讀輸出，變成機械判定。

---

## D-017：OpenAI-compatible API 是 Runtime 的替換點，但接上它之前必須先關掉它的預設值

**日期：** 2026-09-19
**狀態：** 已實作並實測

### 一、背景

長期目標是「可自架、資料由公司自己控制」的平台，且 LLM Runtime 必須可替換
（Ollama / Endpoint / vLLM），上層的 Open WebUI / RAG / MCP / Agent 不得
知道底下是哪一個。

這個架構的**全部價值**建立在一句話上：「上層只說 OpenAI 協定，不知道底下是誰。」
而本專案已經吃過太多次「文件寫了、沒有人驗」的虧（D-014、D-016），所以這次
先做的是：把那句話變成一支可以跑的探針，再去接第二個 runtime。

### 二、發現：一個「資料不出公司」的堆疊，預設接上了 OpenAI 官方 API

在動手改 `docker-compose.yml` 之前先查證 Open WebUI 實際怎麼決定端點，結果
在**本專案自己的資料庫**裡量到：

```
openai.enable         = true
openai.api_base_urls  = ["https://api.openai.com/v1"]
openai.api_keys       = [""]
```

成因是兩個預設值相加（`backend/open_webui/config.py`）：

| 設定 | 預設 | 效果 |
|---|---|---|
| `ENABLE_OPENAI_API` | `'True'` | OpenAI 連線**開著** |
| `OPENAI_API_BASE_URLS` | `''` | 空字串是 **fallback 不是關閉**，被填成 `['https://api.openai.com/v1']` |

兩者相加：**模型選單裡會出現 OpenAI 的模型。** 而 `OPENAI_API_KEY` 是很多
開發機上**全域匯出**的環境變數 —— 一旦有，任何人就能選中 `gpt-4o`，公司資料
在**沒有任何人在這個專案裡設定過**的情況下送出去。

這與 `ENABLE_MCP`（不存在的變數，D-012）、`ENABLE_SIGNUP`（只第一次開機有效，
D-012）是同一類：**看起來是關的，其實不是。**

### 三、發現二：改 `.env` 對現有部署沒有用

原本的修法是「在 compose 把 `ENABLE_OPENAI_API` 預設成 false」。查證後發現
那**只對未來的全新資料庫有效**：

```
# backend/open_webui/models/config.py
def seed_defaults(defaults: dict) -> None:
    """Insert keys that don't yet exist in the DB.
    ...
    Existing DB values take precedence over defaults.     ← 關鍵
    """
```

函式叫 `seed_`、docstring 明寫「Existing DB values take precedence」。

**這是 `lock-signup.sh` 舊版踩過的坑的完全相同的形狀** —— 那次是「讀 `.env`
判斷，於是回報了一個假成功」。差別只在這次我是在**寫** `.env` 而不是讀它，
但結果一樣：一個看起來像修好了、實際上沒有作用的動作。

因此最後的做法是：**compose 的預設值只負責「下一次」，真正的修復以資料庫為準，
而且改完要重啟、回讀、再確認。**

### 四、決策

1. **`ENABLE_OPENAI_API` 在 compose 的預設值改為 `false`。** 這不是保守，
   是修錯。要接自架 runtime 的人必須明寫 `ENABLE_OPENAI_API=true` 與非空的
   `OPENAI_API_BASE_URLS` —— 兩個都要，少了後者就會落回 `api.openai.com`。
2. **新增 `scripts/check-egress.sh` + `scripts/egress_probe.py`：「資料可能流向
   哪些外部服務」必須是機械可查的。** 不寫死清單，而是掃描「值是指向**公開**
   位址的 URL」的設定項 —— 這樣 Open WebUI 將來新增第三方服務時它仍然有效。
   指向 localhost / 私有網段 / docker service name 的一律不列（那些沒有離開
   這台機器；報出來只會製造噪音，而被噪音淹沒的檢查等於沒有檢查）。
3. **`--fix` 必須回讀確認。** 寫入 → 重啟（記憶體快取不會自動更新）→ 重新掃描。
   少任何一步都會變成假成功。
4. **新增 `scripts/probe-openai.sh` + `scripts/probe_openai.py`：只說 OpenAI 協定。**
   不使用任何廠商 SDK、不碰任何 Ollama 專屬端點。判準因此很簡單：**任何能通過
   它的 runtime 都能接手這個平台；任何需要為它改上層程式碼的 runtime 都不能。**
   附帶一條測試斷言：原始碼裡不得出現 `import openai` / `import ollama` /
   `requests.` —— 用了廠商 SDK，驗到的就是那個 SDK 的相容性，不是協定的相容性。

### 五、實測

**統一介面（優先驗證項 1–3）：** 因為 Ollama 本身也提供 OpenAI-compatible API，
不需要 Kaggle 帳號就能先把介面這一層釘死。

```
$ bash scripts/probe-openai.sh                      # 指向 http://ollama:11434/v1
  ✓ GET /v1/models               通過  4 個模型
  ✓ POST /v1/chat/completions    通過  18.3s，回應 好
  ✓ 串流（SSE）                   通過  3 個 chunk，收到 [DONE]
  · 未帶金鑰應被拒絕               未執行  未提供金鑰
  · POST /v1/embeddings（資訊）    未執行  HTTP 501（不提供嵌入）
  結論：通過 —— 只換 --base-url，不改任何上層程式碼。   EXIT=0
```

`/v1/embeddings` 回 501 是**正確的判定而非缺陷**：`qwen2.5:3b` 是聊天模型，
本來就不提供嵌入。探針把它列為「資訊」而不計入失敗 —— 因為本專案的嵌入引擎
本來就可以與聊天引擎是不同的 runtime（D-013）。

**外部端點（優先驗證項 5）：**

```
$ bash scripts/check-egress.sh --check
  ✗ 啟用中  openai.api_base_urls
       https://api.openai.com/v1
       開關：openai.enable = True                    EXIT=1

$ bash scripts/check-egress.sh --fix
  寫入資料庫：openai.enable = false
    openai.enable: true → false
  重啟 open-webui 讓設定生效...
  ✓ 已確認：沒有啟用中的外部端點。                     EXIT=0
```

`--fix` 之後複查：`/health` 回 200、`ollama.enable=true` 完好、
`probe-openai.sh` 仍通過 —— **只關掉了 OpenAI 那條路，沒有連帶弄壞 Ollama。**
掃描另外列出 11 個「已設定但關著」的外部端點（mistral OCR、firecrawl、bing、
perplexity、image generation 等），它們現在不是風險，但只要有人打開開關或
填入金鑰就會變成風險。

**離線測試：** 新增 `test_egress_probe.py`、`test_probe_openai.py`，
連同既有三套共五套全部通過。

寫測試的過程當場抓到一個真實缺陷：`_is_key_name` 只比對 `api_key`，
認不出複數的 `openai.api_keys` —— **而那是它最該認出來的一個**。已修。

第二個缺陷是在讀真實輸出時發現的：`_gate_for` 取「前兩段」當前綴，於是
`audio.stt.openai.api_base_url` 的閘門被報成 `audio.stt.deepgram.api_key`
—— 把 openai 的端點說成由 deepgram 的金鑰管制。修法是從欄位名砍掉結尾
還原子系統。**一支叫人「自己覆核」的探針報錯閘門，比不報還糟。**

### 六、未解的問題

- **優先驗證項 4 完全未驗證：Kaggle + Endpoint 能否實際跑較大的 GGUF 模型。**
  這需要使用者自己的 Kaggle 帳號與 `kgat_` 權杖，在本次執行環境中無法進行。
  介面相容（已驗）**不等於跑得動** —— 速度、可用 VRAM、模型能否載入、T4×2 的
  16GB×2 在 tensor split 下實際能承載多大的 GGUF，全部還是未知。
  **這是目前最大的一塊空白，而它正是「小模型 → 大型本地 LLM」這條路徑的關鍵。**
- `check-egress.sh` 的界線：它讀的是**資料庫裡的值**。Open WebUI 沒有未認證的
  端點會揭露 `openai.enable`（實測 `/api/config` 只回 features/oauth/status 等），
  所以「服務實際載入的值」在沒有管理員帳號的情況下讀不到。容器健康 + 資料庫值
  正確是它做得到的全部 —— 這一點寫在腳本輸出裡，不讓它假裝更多。
- 它回答的是「設定上允許資料去哪裡」，**不是「資料實際去了哪裡」**。要看實際
  流向需要網路層的觀察（`docker compose logs` 只看得到請求，看不到被拒絕的）。
- `--fix` 只關 `openai.enable`，**沒有清掉 `openai.api_base_urls` 裡那個
  `api.openai.com`**。這是刻意的（保留設定、只關閘門），但意味著若有人日後把
  `openai.enable` 打開，它會若無其事地接回 OpenAI。
- 尚未實作（承 D-016）：`response` 為空時改判 `thinking` 欄位。

### 七、後續修正（同日，實作 endpoint 接線時）

**7.1 先質疑自己上一輪的驗證。**

`--fix` 的流程是「寫資料庫 → 讀資料庫確認」。那**可能是循環論證** ——
讀自己剛寫進去的東西，然後宣稱成功。這正是本專案反覆指控的錯誤形狀，
只是換了一件衣服。所以先查證 `openai.*` 到底以什麼為準：

```python
@classmethod
def persistent_enabled_for(cls, key: str) -> bool:
    if not cls.PERSISTENT_ENABLED:
        return False
    if key.startswith('oauth.') and not cls.OAUTH_PERSISTENT_ENABLED:
        return False
    return True                       # ← openai.* 落在這裡

@staticmethod
async def get(key, default=None):
    if not Config.persistent_enabled_for(key):
        return Config.default_value(key, default)    # 讀 env
    ...                                              # ← openai.* 走這條：讀 DB
```

`openai.*` 是持久的，**資料庫是權威來源** —— 上一輪的修法是對的。

**7.2 但「原始碼說會這樣」不算證明。用誘餌實證。**

在 `ai-net` 網路上起一個受控的 HTTP 伺服器（借 open-webui 映像檔的 Python），
把 `openai.api_base_urls` 指向它、`openai.enable=true`，重啟，然後問
**應用程式自己的讀取路徑**：

```
=== 應用程式自己的讀取路徑（Config.get_many）回傳 ===
  openai.enable = True
  openai.api_base_urls = ['http://egress-canary:9999/v1']
  openai.api_keys = ['canary-token']
```

這是 `get_all_models` 用的**同一個函式**，它讀出的是誘餌網址。資料庫是
權威來源 —— 這下是實測，不是推論。

**7.3 但誘餌一個請求都沒收到。**

這才是真正的收穫。重啟後誘餌的請求數仍然是 1（我自己 curl 的那次）。
原因是 Open WebUI **延遲抓取**：要有人在已登入的狀態下打開模型清單，
才會對設定的端點發出請求。

所以「設定生效」與「模型抓到了」是**兩件事**，而只有前者是腳本證明得了的。
`connect-endpoint.sh` 的讀回確認把這句話明寫在輸出裡，不讓它假裝更多。

**7.4 探針自己有同一個缺陷（D-016 的形狀，再次出現）。**

實作接線腳本時，用誘餌當「連得上但不符合協定」的測試對象，結果探針印出

```
✗ GET /v1/models   未通過  HTTP 404
結論：無法判定 —— 端點拒絕。          ← 這兩行互相矛盾
```

回傳 2。於是 `connect-endpoint.sh` 叫使用者去查網路 —— **而網路根本沒問題**。
一個明確回答「我沒有這個路徑」的端點是**有結論**的，不是「測不出來」。

成因是 `probe()` 把非 200 也標成 `aborted`，而 `main()` 先判斷 `aborted`
才判斷 `p.failed()`，於是 FAIL 被吞掉。**這正是 D-016 修過的同一件事，
發生在我自己寫的檔案裡** —— 那次是「對照題答對卻被判錯」，這次是
「端點有回應卻被說成連不上」。

判準已收斂為一句：**只有連不上（無 HTTP 回應）才是「無法判定」；
端點有回應就是有結論。** 迴歸測試用替換 `_request` 的方式釘住它，不需網路。

**7.5 新增工具。**

| 檔案 | 作用 |
|---|---|
| `scripts/connect-endpoint.sh` | 接上一個 runtime：**先驗證、後接線、再回讀**。探針未通過就拒絕變更 |
| `scripts/runtime_state.py` | 讀寫 runtime 設定。**讀值走應用程式自己的 `Config.get_many`**，不讀自己剛寫的資料列 |
| `scripts/test_runtime_state.py` | 離線測試（金鑰不外洩、拒接清單、讀值路徑） |
| `docs/ENDPOINT.md` + `.zh-TW.md` | Kaggle + Endpoint 的完整接線指南 |

`runtime_state.py` 有一個刻意的防呆：**拒絕接上 `api.openai.com`**。
本專案的目標是「資料由公司自己控制」，接上它與該目標直接衝突，所以它必須是
明示的例外，而不是一個不小心就會滑進去的預設值。

**7.6 新發現的架構張力：Quick Tunnel 沒有 Access。**

`endpoint` 用 cloudflared **Quick Tunnel**（`*.trycloudflare.com`）暴露引擎，
**沒有 Cloudflare Access 政策**，保護只有 API 金鑰。這與本專案自己的規則
（D-012：對外要走 Tunnel **+ Access**）直接衝突；Ollama 是刻意不暴露的
（D-003），而 GPU runtime 剛好相反 —— 它**就是**公開的。

這個張力沒有在這一輪解決，只是被記錄下來。它會在上傳第一份公司文件時
變成必須解決的問題。

**7.7 尚未驗證（新增）。**

- **`GET /v1/apikey` 是否能在無權杖下存取。** endpoint 的文件提到這條路由。
  若它不需認證，那麼光是 tunnel 網址就足以取得金鑰。**本專案未驗證。**
- 誘餌實驗證明的是「設定被讀到」，**不是「模型抓到了」**。後者需要登入，
  只有使用者開一次 UI 才算數。
- Kaggle 的實際額度與硬體**未量測**（且不應寫死 —— 政策會變，P100 已於
  2026-09-15 退役）。
- **Kaggle 的條款本文與 Acceptable Use Policy 都沒有直接讀到**，只有官方
  Q&A 的轉引（見第八節）。

### 八、使用條款：Kaggle 不能是產品路徑（2026-09-19 補記）

**這一節補的是一個真空。** 第四到第七節寫了 Kaggle 怎麼接、怎麼驗、有什
麼風險，但**從頭到尾沒有一句話提到它的使用條款** —— 而條款決定的正是這
條路「能不能拿去給公司用」。

Kaggle 的[使用條款](https://www.kaggle.com/terms)把服務限定為**個人、非
商業**用途。官方 Q&A 引述的原文是：

> You will only use the Services for your own internal, personal,
> non-commercial use, and not on behalf of or for the benefit of any
> third party.

本專案的長期目標是「讓小公司建內部助理」—— 那是商業用途。所以要把兩種
用途分開：

| 用途 | Kaggle |
|---|---|
| 驗證大模型跑得動、量速度與可用 VRAM | **適合** —— 這正是 D-017 用它的理由 |
| 產品路徑：公司內部助理的執行環境 | **不適合** —— 條款限定個人、非商業 |

**這不改變任何技術結論。** 探針的結果、runtime 替換點的存在、接線腳本，
全部照舊。它改變的是**定位**：Kaggle 是驗證工具，產品路徑的終點仍然是
VPS + GPU + vLLM。

**證據的邊界**（本專案的規矩：連「沒查到什麼」也要寫出來）：

- 引文來自 **Kaggle 官方 Q&A 對條款的引述**，2026-09-19 查閱。
- **條款本文沒有直接讀到。** `kaggle.com/terms` 是 JS 渲染的頁面，抓取只
  回傳標題、沒有條文內文 —— 這是**轉引**，不是與原文核對。
- **Acceptable Use Policy（`kaggle.com/aup`）的本文同樣沒有取得。** 所以
  本節**不能**宣稱它對「把 notebook 當伺服器」有什麼具體規定，已列為
  未解（第六節）。
- **本節不是法律意見。** 要拿去商用之前，請自己讀過條款，或問法務。

已同步寫進 `docs/ENDPOINT.md` 與 `docs/ENDPOINT.zh-TW.md`，那兩份原本
也都沒有這一節。

---

## D-018：RAG 的驗證必須拆成「檢索」與「接地」兩項，且評分邏輯自身必須可測

**日期：** 2026-09-19
**狀態：** 已實作並實測

### 一、背景

README 的「Phase 2: RAG」是四項 UI 動作：建立知識庫、上傳文件、問文件裡
的事、**問文件裡沒有的事**。四項都要一個已登入的使用者。

但這一階段真正未知的東西不是 UI，而是這一句：

> 檢索到的段落，模型真的拿來用了嗎？還是它只是一個接得通順的生成器？

這個問題對小模型尤其尖銳。RAG 的整個價值建立在「答案來自文件」之上；
一個 3B/4B 模型完全可以在無視 context 的情況下生出一個看起來很合理的
答案，而**在 UI 上兩者長得一模一樣**。

### 二、為什麼沒有用帳號

資料庫目前有 **0 個使用者**，而註冊是鎖住的（D-012）。要開帳號就得先決定
一組密碼，而 `scripts/verify-first-admin.sh` 用的是**寫死在 repo 裡**的密碼
—— 在正式堆疊上開一個永久 admin 只為了跑測試，與本專案的安全取向
（D-003、D-012）相衝。

所以本階段改成：**測機制，帳號那一步留給人**。這也讓探針可以在任何人
註冊之前就回答「這個設定到底行不行」。

### 三、走的是應用程式自己的程式碼

這支探針不是「模擬 RAG」，它呼叫的是應用程式自己的函式：

| 環節 | 用的函式 | 為什麼是這一個 |
|---|---|---|
| 讀設定 | `Config.get_many` | 與 `get_all_models` 同一條路徑 |
| 切塊參數 | `get_retrieval_config` | 應用程式自己建 `RetrievalConfig` 的函式 |
| 嵌入 | `get_embedding_function` | `main.py` 啟動時建 `EMBEDDING_FUNCTION` 用的同一個 |
| 檢索 | `query_collection` | **聊天流程實際呼叫的檢索函式** |
| 向量庫 | `VECTOR_DB_CLIENT` | `factory.py` 依 `VECTOR_DB` 選出的後端（本堆疊為 Chroma） |
| 訊息組裝 | `apply_source_context_to_messages` | 應用程式自己把 context 併進訊息的地方 |

`apply_source_context_to_messages` 的函式體內**完全沒用到 `request`**，
所以傳 `None` 是誠實的，不是假造環境。（相對地，`save_docs_to_vector_db`
會無條件解參考 `request.app.state.ef` 與 `main_loop` —— 要用它就得捏造一個
假的 request 物件。與其假造執行環境，攝入那一段改用同一組函式自己組。)

### 四、邊界

**攝入這一段不是 `/api/v1/files` 那條 HTTP 路徑。** 它用的是應用程式自己的
切塊器類別與參數、自己的嵌入函式、自己的向量客戶端，但**由探針組裝**。

所以它證明的是「這條鏈在目前設定下能動」，**不是**「上傳 PDF 的那條路也
能動」。後者要真的上傳一份、而且需要帳號。這句話寫在探針自己的輸出裡。

### 五、測試文件是虛構的 —— 這是設計，不是偷懶

如果問的是真實法規（例如颱風停班停課的風速標準），模型可以從參數記憶
答對，而我們就分不出「它讀了文件」與「它本來就知道」。

所以要驗的值全部是捏造的，只存在於那份測試文件裡。三個要驗的事實分散在
**不同段落**，這樣才驗得到檢索是不是真的挑對段落，而不是把整份文件一股腦
塞進 context。

### 六、實測（qwen2.5:3b）

```
✓ 嵌入設定      引擎 ollama、模型 qwen3-embedding:0.6b
✓ 切塊          6 塊（chunk_size=1000、overlap=100）
✓ 嵌入文件      6 個向量，維度 1024
✓ 正向題·檢索   目標段落排在第 [1] 名（共 3 塊）
✓ 正向題·判定   本公司自訂的提前停班門檻，平均風速是八級 [1]。
✓ 對照題·檢索   目標段落排在第 [1] 名（共 3 塊）
✓ 對照題·判定   颱風期間申請遠端工作，必須在停班公告發布後 **二小時內** 提出。[1]
✓ 反向題·判定   根据文档，文件中没有提到关于…加班費倍率的信息。 [1]
```

結論 0（全部通過）。檢索三題都把目標段落排在第 1，接地完全正確，
反向題明白說出文件未提及並附上引用。

#### 生成時間：同一個堆疊、同一個模型，最慢與最快差了 2.3 倍

| 輪次 | 正向題 | 對照題 | 反向題 | 生成合計 |
|---|---|---|---|---|
| A | 73.9s | 66.8s | 94.4s | 235.1s（整輪實測 ~280s） |
| B | 51.7s | 40.6s | 41.0s | 133.3s |
| C | 53.2s | 53.0s | 45.2s | 151.4s |
| D | 55.2s | 42.4s | 41.9s | 139.5s |

四輪同一天、同一個堆疊、同一組問句、同一個模型。B、C、D 之間只差評分
邏輯的修改，**生成時間不受它影響**。

**一次生成的範圍是 41–94 秒，不是一個定值**（實測 40.6s ~ 94.4s，
樣本數 12）。開頭那一輪（A）明顯最慢，後面三輪都落在 40–55 秒。

整輪 = 生成合計 + 固定開銷（嵌入、模型載入、檢索）。A 輪的固定開銷是
**約 45 秒**（280 − 235）—— 只有一輪量到整輪時間，所以這個 45 秒是
單一觀測值，不是平均。

這一節的數字換過三次，每次都是同一種錯的變體：

1. 最初寫「約 45–50 秒」—— **憑印象填的**，根本沒量。
2. 改成「67–94 秒」—— 量了，但**只量了 A 那一輪就把單輪數字當成範圍**。
   這比第 1 種更難發現：它看起來像有憑有據，實際上樣本數是 1。
3. 現在這張表 —— 四輪並列，並且把「生成合計」與「整輪」分開欄位，
   因為原本那張表的「整輪」欄位**兩列用的不是同一個量尺**（A 是整輪
   實測、B 是生成合計），看起來卻像可以直接比。**單位不一致的表格，
   比沒有表格更危險** —— 它會讓人做出一個基於錯誤比較的決定。

第 4 次修的不是這張表，是**引用這張表的那一行 README** —— 數字一字
不改，只是它原本掛在 `bash scripts/rag-verify.sh`（模型取自 `.env`
的 `qwen3:4b`）旁邊，而它是用 `--model qwen2.5:3b` 量出來的。詳見
第七節 C 的第三種。**這一張表的每個數字都對，錯的是它被貼在哪裡。**

「量過一次」不等於「知道它的範圍」，而兩者在文字上長得一模一樣。
**單輪數字寫成範圍，是假資訊最難抓的一種。**

**變化主因**：`OLLAMA_MAX_LOADED_MODELS=1`（見下）讓嵌入模型與生成模型
必須互相卸載，每次載入實測約 15 秒。**這個解釋我沒有單獨驗證過** ——
15 秒是載入的實測值，但輪次之間的差距（最大 53 秒）不完全是載入造成的，
剩下的部分未知。B、C 兩輪接近而 A 最慢，與「A 跑在閒置之後」相符，
但這是**同一個未驗證解釋的補充，不是新證據**。

**對 `DEFAULT_TIMEOUT` 的意義**：300 秒必須對照**最慢**那輪訂，不是最快
那輪。若看著 B 輪的 52 秒去訂（例如設 120 秒），A 輪的 94 秒就會誤報逾時
—— 而且會誤報成「太慢」，把一個健康的堆疊說成有問題。這是第七節 B 表
第 1 列（思考型模型逾時被寫成「連不上」）的另一個入口。

#### 同一個堆疊、換 qwen3:4b —— 全部逾時

```
✓ 建立測試集合 / 嵌入設定 / 切塊 / 嵌入文件 / 寫入向量庫
✓ 正向題·檢索   目標段落排在第 [1] 名（共 3 塊）
? 正向題·生成   無法判定
✓ 對照題·檢索   目標段落排在第 [1] 名（共 3 塊）
? 對照題·生成   無法判定
✓ 反向題·檢索   3 個 chunk
? 反向題·生成   無法判定
```

結論 2（無法判定）。**檢索全對，但三次生成全部撞到 300 秒上限。**

這件事本身是結果，不是缺陷：qwen3 是思考型模型，在純 CPU 上光是思考鏈
就超過 300 秒。`qwen3:4b` 目前載入的 context 是 **4096**（見下），
`ollama ps` 顯示 100% CPU。

**但當時的訊息寫的是「連不上模型」—— 那是錯的。** 模型連得上、也一直
在算，只是講不完。這句話會把人送去查網路、重啟容器，而該做的是拉長
時限或換一個不需要思考鏈的模型。

**這是「錯誤的成因把人送去修錯的子系統」的第 1 次**（第七節 B 表第 1 列），
而且這次是在它被寫進任何文件之前就抓到。
修法：

- `ollama_chat` 把逾時獨立成 `TIMEOUT` 狀態，不再與「連不上」共用 `None`。
  連線階段的逾時被 `urlopen` 包在 `URLError.reason` 裡、讀取階段的逾時
  直接拋 `socket.timeout` —— **兩種都要認**，只認一種會讓連線逾時繼續
  被報成連不上。
- 逾時訊息直說「模型連得上，但 N 秒內沒講完 —— 這是太慢，不是連不上」。
- 新增 `--timeout`（預設 300 秒）與對應的包裝腳本參數。
- 離線測試**真的開一個只接受連線、永不回應的 socket** 來驗這兩條路徑
  **會分開**。不用 mock —— mock 會把 `urlopen` 的例外包裝方式一起假掉，
  而這次的 bug 正好出在例外包裝。

#### 這次沒有證明的東西：context 只有 4096

`num_ctx` 在 open-webui 裡**只出現在型別驗證清單**，沒有任何地方設定它
—— 也就是說 open-webui 不送 `num_ctx`，由 ollama 用自己的模型預設值。
本堆疊 qwen3:4b 載入時是 **4096**。

我們的測試文件只有 478 字元，整份塞進去約 **765 token**，離 4096 很遠，
所以這次的通過**沒有**壓力測試到 context 上限。用同一組參數估算：

| 檢索回傳 | 提示大小 | 對 4096 的意義 |
|---|---|---|
| 整份 478 字文件 | ~765 token | 綽綽有餘（本次實測） |
| 3 塊各 1000 字 | ~3500 token | 勉強 |
| 6 塊各 1000 字 | ~6700 token | **會靜默截斷** |

`chunk_size=1000` × Top-K 一旦超過約 3500 字，超出 4096 的部分會被
**靜默丟掉**，而被丟掉的通常是排在後面的段落 —— 使用者看到的會是
「模型漏答了文件裡的某一條」，而不是任何錯誤訊息。

**這件事還沒有實測，只是估算。** 要驗它得用一份真正的長文件，而那就
需要帳號（見第二節）。在那之前，**不要**把這次的通過讀成「RAG 對真實
文件也成立」。

#### 順帶量到的：嵌入模型與生成模型在互相排擠

`docker-compose.yml` 對 ollama 設了 `OLLAMA_MAX_LOADED_MODELS=1`
—— **一次只能有一個模型常駐**。而 RAG 天然需要兩個：一個嵌入模型
（`qwen3-embedding:0.6b`）與一個生成模型。

ollama 日誌如實顯示它們在交替：

```
2026-09-19T11:14:01  載入 06507c7b（qwen3-embedding:0.6b）
2026-09-19T11:14:14  載入 5ee4f07c（生成模型）   ← 13 秒後就換手
2026-09-19T11:15:21  載入 06507c7b（嵌入）      ← 又換回來
2026-09-19T11:15:26  載入 5ee4f07c（生成）      ← 5 秒後再換
```

實測其中一次載入耗時約 **15 秒**（11:14:14→11:14:29）。也就是說每一題
問答，除了生成本身，還要付一次模型重載的代價。

**這件事有多貴，本次沒有量。** 要量得把每次請求的耗時與載入事件對齊，
那是另一件事。這裡只記錄確定的部分：**這個設定與 RAG 的形狀相衝**，
而 `OLLAMA_MAX_LOADED_MODELS=2` 是候選解法 —— 但兩個模型同時常駐會多吃
記憶體，4GB 以下的機器不一定吃得下。要改之前先在目標機器上量記憶體。

#### 最後一輪綠燈，但它沒有測到剛修的東西

D 輪 `EXIT=0`，11 項全過。但**對照題這次模型寫的是「二小時內」** ——
而修正前的變體清單本來就有這一項。也就是說：

- 讓第 6 次假失敗的那個寫法（「2 小時」），**D 輪沒有再出現**。
- D 輪是靠**舊有的**變體過的，不是靠新加的空白正規化過的。

所以「空白正規化」目前的證據只有一項：測試檔裡那則**實機原文**的迴歸
測資。那不是零，但**也不是「實機重現驗證」**。兩者在報告裡長得很像，
差別是後者真的跑過。

**這是探針的性質，不是缺陷**：模型的措辭是取樣的，一輪三次生成就是三個
樣本。某個特定的假失敗會不會再現，取決於模型那天怎麼講話。可以確定的
只有一件事：**再現時它會被抓到**，因為那則原文已經在迴歸測資裡。

要把它變成實機重現驗證，得跑到模型再講一次同樣的話 —— 那可能要很多輪。
這裡選擇**誠實標示，而不是宣稱已經驗過**。

### 七、假失敗 —— 錯的永遠不是 RAG

這一節是這個決策裡最重要的部分。錯的形狀不只一種，但共同點只有一句：
**RAG 本身沒壞，壞的是「我說它壞了」。**

原本這一節只記「評分邏輯錯了四次」，用一條全球流水號編下來。寫到後面的
時候流水號已經沒有意義了 —— 它把不同種類的錯混在一起數，而且**每次
新增就要全部重編**，連帶所有指回來的引用一起改。

**改成按類型分組，每組算自己的次數。** 這樣新增一次錯誤只需要動一組，
而且「這一類已經錯了幾次」本身就是要看的資訊。

#### A. 評分邏輯錯 —— 六次，全部是假失敗

探針寫完之後，**六次**執行回報未通過，而每一次**錯的都是評分邏輯，不是
RAG**：

| # | 症狀 | 真正的原因 |
|---|---|---|
| 1 | 判「模型忽略了檢索到的內容」 | 我呼叫 `rag_template(tpl, ctx, question)`，以為第三個參數會把問題放進提示。**它不會** —— 它只替換模板裡存在的 `{{QUERY}}`，而預設模板只有 `{{CONTEXT}}`。問題是**使用者訊息**，不是模板的一部分。模型收到文件、沒收到問題，於是反問「請問您想問哪一條？」 |
| 2 | 判「未說明文件沒有這項資訊」 | 「不知道」的說法清單漏了「**沒有找到**」。模型明明說了。 |
| 3 | 判「未說明文件沒有這項資訊」 | 模型用**簡體**回答，清單全是繁體，字串比對直接斷掉。 |
| 4 | 判「卻給了具體數字」 | 用 `\d` 找「有沒有掰出數字」——找到的是**引用標記 `[1]` 裡的 1**。一個自己加的註腳被當成幻覺的證據。 |
| 5 | 判「未說明文件沒有這項資訊」 | 模型答「根据提供的资料，没有关于…加班費倍率**的信息**」—— 完全正確。樣式卻抓不到，**兩個獨立的原因各自都足以讓它漏掉**：<br>**(a)** 樣式要求否定詞後面接**動詞**（提及／提到／說明…），而這裡接的是**名詞**「信息」。<br>**(b)** 中間隔了 15 個字，超過樣式憑感覺訂的 `{0,10}`。<br>只修 (a) 或只修 (b) 都不會過 —— 第一次修就是只補了名詞，測試仍然失敗。 |
| 6 | 判「未答出文件裡的捏造值」 | 模型答「必須在停班公告發布後 **2 小時內** 提出」—— 語意完全正確，也確實取自文件（「二小時」是那份虛構文件獨有的值）。變體清單裡有「2小時」也有「二 小時」，**就是沒有「2 小時」** —— 同一個位置，中文數字那半邊預期了空格、阿拉伯數字那半邊沒有。 |

六次都是同一個形狀：**模型做對了，評分說它做錯。** 而這正是本專案在
D-014 與 D-016 已經記錄過兩次的形狀 —— 第三次與第四次發生在我當天新寫的
檔案裡，**第五次發生在我剛寫完「前四次」那一節之後、同一輪工作之內，
第六次發生在我剛修完第五次、跑下一輪驗證的時候。**

#### 六次裡有五次是同一種病

把這六次排開，會看到一個比「假失敗」更精確的診斷：

| # | 表面上的差異 | 病 |
|---|---|---|
| 3 | 繁體 vs 簡體 | 評分取決於**字型** |
| 5 | 動詞「提及」vs 名詞「信息」 | 評分取決於**詞性** |
| 5 | 中間隔 2 字 vs 隔 15 字 | 評分取決於**距離** |
| 6 | 「2 小時」vs「2小時」vs「二 小時」 | 評分取決於**空白** |
| 6' | 「２小時」vs「2小時」（**這一項沒有實測到**，是看到第 6 次之後順著同一個封閉集合補的） | 評分取決於**全形／半形** |

**這些全都不是「模型答錯了」，是「評分讀不懂對的答案」。** 而它們的修法
只有一句話：**正規化，不要列舉。**

- 字型 → 轉成同一種（`_VARIANTS`）
- 空白、全形 → 拿掉／轉半形（空白收斂 + `_FULLWIDTH`）
- 詞性 → 列**詞類**（動詞＋名詞），不是列詞
- 距離 → 用**標點**當界線，不用數到幾

**列舉與正規化的差別，在於它們怎麼失敗。**

列舉是「想到一個補一個」：補了「沒有找到」、補了中文數字那半邊的空格、
補了動詞 —— 每一次都只補了**當時想到的那一半**，所以每一次都還會再漏。

正規化是「把不帶語意的差異全部消掉」。做完之後，**在那個維度上不可能
再有下一種寫法**，因為它已經不是一個維度了。

這條原則在別的地方也生效：`_ABSENT_RE` 現在不跨標點，所以「否定在一句、
肯定在另一句」不可能誤配 —— 那不是補了一個特例，是那個維度不存在了。

**第 5 次有一段特別值得記的細節。** 測試檔裡早就有一則幾乎相同的測資：

```
根据文档，文件中没有提到关于颱風停班期間的加班費倍率的信息。 [1]
```

它通過了 —— 因為它有「沒有**提到**」那個動詞。實機這次跑出來的是同一句
話**拿掉動詞、只留名詞**。也就是說，既有測資距離抓到這個 bug 只差一個詞，
而它沒抓到。

這說明「用實際回應原文當測資」有一個邊界：**它防的是「同一個句子再錯
一次」，不是「同一個句型換個詞」**。要防後者，測資得刻意覆蓋同一句話的
不同詞性版本 —— 但這又回到「清單永遠補不完」。**沒有完美的解，只有
明確知道自己在防哪一種。**

**第 1 次的代價最大**：它不只誤判，還**指著模型罵**。輸出寫著「模型忽略了
檢索到的內容」，把一個提示組裝的錯誤說成模型的缺陷。如果當時沒有去讀模型
實際說了什麼就接受那個結論，整個第二階段會被導向「換更大的模型」這個
錯誤的方向。

**具體的修正：**

- 提示改用 `apply_source_context_to_messages`（應用程式自己的函式），
  不再自己拼。
- 「不知道」的判定從**字串清單**改成**樣式**。清單永遠補不完，而
  「並沒有**明確**提及」這種插了副詞的寫法一定會再出現。
  **但這個修法本身被第 5 次推翻了兩次**：把清單換成樣式，只是把「補的
  單位」從詞換成詞類，並沒有解決補不完。而且樣式裡「否定詞與關鍵詞
  之間 0~10 字」是憑感覺訂的常數 —— 第 5 次的句子隔了 15 個字。
  現在改成靠**標點**當界線（不跨子句、不跨換行），因為「同一個子句」
  才是這個比對真正的語意範圍，字數從來不是。
- 比對前先做**字型正規化**。評分不該取決於模型剛好選了繁體還是簡體。
- 找數字前先**移除引用標記**。
- 判定邏輯抽成 `grade_grounded` / `grade_absent`，離線可測。
- 探針自己崩潰給**獨立的結束碼 3** —— 「探針壞掉」不是「RAG 壞掉」，
  報成後者會叫人去修一個沒壞的系統。

**每一條判定都要附上它判定所依據的文字。** 沒有這句話，一行「未通過」
就是在要求人相信一個看不見的結論。加上之後，多數假失敗在輸出的第一眼
就看得出來 —— **第 5 次就是這樣抓到的**：輸出把模型的原話印在「未通過」
旁邊，一看就知道它答對了。

#### B. 訊息／診斷錯 —— 三次，錯誤的成因把人送去修錯的子系統

這一類不在上面那張表裡，因為它不是評分錯，是**說法錯**：

| 訊息寫的 | 真正發生的事 |
|---|---|
| 「**連不上模型**：timed out」 | qwen3:4b **連得上、也一直在算**，只是思考鏈超過 300 秒。這是太慢，不是連不上。（詳見第六節） |
| 「**連不上模型**：Expecting value…」 | 端點回了 HTTP 200，只是內容不是 JSON（例如反向代理吐了一頁 HTML 錯誤）。**傳輸失敗與內容失敗是兩件事**，卻給了同一個說法。 |
| 註解：「Python 的 stdout **整塊緩衝**」 | 與緩衝無關。真正原因是 `run()` 收集完才 `dump()`。見下面 D 節。 |

三次的傷害一樣 —— 都把負責修的人送去修錯的子系統。第 2 次是**我在修完
第 1 次之後、同一天內立刻犯的**，而且是在自己寫的靜態自審裡才發現；
第 3 次則是修完前兩次之後，寫在**程式碼註解**裡。

#### C. 量測錯 —— 三次，一次比一次像真的

（完整表格在第六節，這裡只記形狀。）

- 「一次生成約 45–50 秒」—— **憑印象填的**，根本沒量。
- 改成「67–94 秒」—— 量了，但**只量一輪就把單輪數字當成範圍**。
- 「約 2–5 分鐘；三次生成各 41–94 秒」—— 數字**完全正確**（四輪 12 個
  樣本），但**掛在錯的指令上**。

第三種最難發現，因為前兩種至少還是「數字本身的問題」。第三種的數字
經得起任何複查 —— 有樣本、有範圍、有表格。問題不在數字，在**它旁邊那
一行指令**：

    指令是 `bash scripts/rag-verify.sh`          ← 模型取自 .env
    數字是 `--model qwen2.5:3b` 量出來的          ← 非思考型模型

而 `.env.example` 出貨的預設是 `qwen3:4b` —— 一個思考型模型。所以照著
README 第一列原樣跑的人，會得到**至少 15 分鐘**（三次各 300 秒上限）
和一句「無法判定」，而說明上寫的是 2–5 分鐘。誤差 5 倍以上，方向還是
最糟的那一邊：**它會讓人以為堆疊壞了。**

2026-09-19 這輪就是照那一行跑的，才當場撞到。修法是把時間數字移到
產生它的那一行，並讓第一列直接說出它實際會發生什麼。

三種量測錯的共同點：**數字的可信度與它所屬的脈絡是分開的兩件事**，
而人只會查前者。「有憑有據」不等於「據的是這件事」。

#### D. 診斷錯 —— 而且是在修完上面全部之後立刻犯的

（已列在 B 節的表裡，這裡記它的特殊之處。）

寫 `rag-verify.sh` 時觀察到「整輪輸出要到結束才一次吐出來」。我把它歸因於
**Python 的 stdout 整塊緩衝**，加了 `python3 -u`，並把這個解釋寫成註解。

**觀察是對的，成因是錯的。** 真正的原因在探針自己：`run()` 用 `p.add()`
收集所有列，`dump()` 由呼叫端在 `run()` 回傳**之後**才印 —— 整輪本來就
什麼都不會印，與 stdio 緩衝無關。`-u` 治不了它。

這與第 5、6 次是同一個形狀，但這次**寫在程式碼註解裡**。註解比執行期
訊息更耐久：下一個人會照著它去調 Python 的緩衝設定，發現沒用，然後
不再相信這支腳本的註解。

#### E. 清理自己製造的假失敗 —— 提交之後重讀程式碼才看出來

（這一項既不是實機跑出來的，也不是離線測試抓到的，是提交後重讀發現的。
放在最後，因為它與 A 節的六次是**同一個病**，卻發生在我用來防那種病的
機制**裡面**。）

集合從未建立時（例如嵌入階段就失敗，`return p` 在 insert 之前），`finally`
直接呼叫 `delete_collection`。而 Chroma 對不存在的集合丟 `NotFoundError`
—— 這是實測，在容器內對一個隨機不存在的名字呼叫：

    NotFoundError: Collection [probe-nonexistent-9e16d894] does not exist

那個例外被 `except` 接住，於是三件事同時發生：

1. 印出「請手動刪除 `<name>`」—— 叫使用者去刪一個不存在的東西；
2. 記成 `未通過` —— 而實際上沒有任何東西被留下來；
3. **結束碼由 2（無法判定）翻成 1（未通過）。**

第 3 點才是重點：D-016 用一整輪把「無法判定」與「未通過」分開，而清理
這段從後門把它們合併回去。觸發它的是**最常見的失敗路徑** —— 嵌入模型
沒下載、或 ollama 連不上 —— 也就是最需要那兩種狀態分得開的時候。

形狀與 A 節六次完全相同：**沒有去查，而是假設**。修法也相同，先問
`has_collection` 再決定要做什麼。抽成 `cleanup_collection()` 是為了讓
**訊息本身**能離線迴歸測試 —— 這段錯的從頭到尾都是訊息。

修正在**真實 Chroma** 上驗過四條分支（不是只餵假物件）：

| 情境 | 修正前 | 修正後（實測） |
|---|---|---|
| 集合從未建立 | `未通過`「請手動刪除」 | `通過`「沒有需要清理的集合（從未建立）」 |
| 真的建立再刪除 | `通過` | `通過`「已刪除（回讀確認）」，回讀 `False` |
| 刪了還在 | `未通過` | `未通過`「刪除後仍存在」 |
| 連不上向量庫 | `未通過` | `無法判定` |

第三、四列才是這次修法的界線：**正規化不等於放寬**。「沒有東西可清」
是通過，「清不掉」還是失敗，「問不到」是無法判定 —— 三者不同。

#### 這個清單為什麼會繼續變長

不是因為不小心，是因為**形狀是結構性的**。這份工作大半時間在做
「看一個輸出，然後說它是什麼意思」，而「它是什麼意思」永遠比
「輸出長怎樣」更容易講得比證據多。

擋得住它的不是小心，是機制。這個決策裡的每一條修正都是同一件事 ——
**讓說法必須附帶證據**：

| 機制 | 擋住了什麼 | **沒擋住什麼** |
|---|---|---|
| 判定邏輯離線可測，測資用實際回應原文 | A 的第 1–4 次 | **A 的第 5、6 次** |
| 每一條判定附上它依據的文字 | A 全部（第 5、6 次都是這樣一眼看出來的） | — |
| **正規化，不要列舉** | A 的第 3、5、6 次 —— 同一個病 | — |
| 逾時／壞回應／連不上拆成三種狀態 | B 的前兩次 | — |
| 清理記成表格的一列，讓它影響結束碼 | 清理靜默失敗 | **清理自己製造的假失敗**（E 節） |
| 數字要能被第二輪複現 | C 的前兩次 | **C 的第三次** —— 它的數字經得起複現 |
| **實機跑，不是只跑離線測試** | **A 的第 5、6 次** | — |

**最後一列是這一輪學到的，也是這張表存在的理由。** A 的第 5、6 次**都
不是**離線測試抓到的 —— 都是實機跑抓到的，而且是連續兩輪各抓到一次。
離線測試裡早有一則與第 5 次幾乎相同的測資，只差一個詞（動詞 vs 名詞），
而那個詞正好讓它通過。

所以兩者防的不是同一件事：

- **離線測試**防的是「**同一句話**再錯一次」。快、免費、每次改動都跑。
- **實機跑**防的是「**同一個句型換個詞**」。慢（一輪約 2–5 分鐘）、
  要佔用模型槽，但它看到的是模型真的會講出來的話。

不能互相取代。而且 —— 這一節本身就是證據 —— **如果只看離線測試綠燈就
提交，第 5 次會直接進到 repo 裡。**

**D 節（註解裡的錯成因）是唯一沒有機制可擋的** —— 只能靠「不確定就說
不確定」。所以那條註解的修正版直接把「這不是緩衝問題」寫進去，
而不是只是刪掉錯的說法：留下一個空洞，下一個人還是會自己填一個。

### 八、決策

1. **RAG 的驗證拆成「檢索」與「接地」兩項分開報。** 兩者是不同的病，
   修法不同；合併成一句「RAG 壞了」沒有可行動性。
2. **測試文件一律虛構。** 用真實法規就分不出「讀了文件」與「本來就知道」。
3. **評分邏輯必須離線可測，且迴歸測資用模型實際的回應原文。**
   `scripts/test_rag_grounding_probe.py` 裡的測資就是那六次假失敗的原文，
   一字未改。（第 6' 次的迴歸測資是**推測的變體**，在測試檔裡另外標示，
   不與實際原文混在一起 —— 那份檔案的契約是「測資不是想像出來的」。）

   **但要清楚它的邊界**：實際原文防的是「**同一句話**再錯一次」，不是
   「**同一個句型換個詞**」—— 第 5 次換了詞性（動詞→名詞）、第 6 次換了
   空白，兩次都通過了既有的測資。所以
   **`test_rag_grounding_probe.py` 綠燈不等於可以提交**，仍要實機跑一輪。
   這一條是這一輪用兩次真的假失敗換來的。

4. **評分邏輯只做兩件事：正規化，或列詞類。不得列舉寫法。**
   六次評分假失敗裡有五次是同一種病 —— 評分取決於模型剛好選了哪一種
   表示法（字型、空白、全形、詞性、距離）。每一次「補一個變體」的修法
   都只補到當時想到的那一半，所以每一次都還會再漏。
   **判準：如果某個差異不帶語意，就在 `_norm` 裡把它消掉（那個維度從此
   不存在）；如果某個差異帶語意，就列詞類。兩者都不做的話，下一個變體
   一定會來。**
5. **探針不得碰既有的向量集合**，清理放在 `finally`，且探針自己壞掉要有
   獨立的結束碼。
   `finally` 擋得住例外與 Ctrl-C（`SIGINT` → `KeyboardInterrupt`），
   **擋不住 `SIGTERM`／`SIGKILL`** —— 那兩種情況下 `finally` 不會執行，
   集合會留在向量庫裡。留著的集合名字開頭是 `probe-rag-`，看得出來源，
   可以放心刪除。要確認有沒有殘留，**用應用程式自己的
   `VECTOR_DB_CLIENT.client.list_collections()`**，不要直接讀
   `chroma.sqlite3` —— 直接讀 sqlite 在本堆疊上得到過與事實不符的
   結果（讀到 0 個，但當時集合確實存在），而一個會說謊的檢查比沒有
   檢查更糟。

   **同一天還犯了一個同形狀的錯，換了媒介：** 要停掉一個跑太久的探針時，
   在**容器內**執行 `kill -TERM <主機 PID>` —— 容器有獨立的 PID 命名空間，
   那個 PID 在裡面不存在，`kill` 靜默失敗，而用來確認的
   `ls /proc/<pid>` 也照樣失敗，於是把「殺掉了」印了出來。實際上那支
   探針還活著，並且與新開的一輪**同時搶唯一的模型槽**（見上面
   `OLLAMA_MAX_LOADED_MODELS=1`），差一點讓新的一輪產生假逾時。
   確認的方式必須是**用會反映真實狀態的來源重掃一次**，不是看命令有沒有
   回報成功。
6. **第一階段的 `rag.template` 不需要改。** 實測顯示預設模板足以讓 3B 模型
   正確接地並附引用 —— 在動它之前先有證據說它不夠。
7. **「太慢」與「連不上」必須分開講**，即使結束碼同為 2。逾時獨立成
   `TIMEOUT` 狀態，訊息直說原因。這一條不限於本探針 —— 任何把
   「沒有回應」與「回應太慢」混為一談的訊息，都會把人送去修錯的東西。
8. **預設生成模型維持 `qwen2.5:3b`（非思考型）。** qwen3:4b 在這台純 CPU
   機器上跑不完一題 RAG；要用它必須同時拉長 `--timeout` 並接受等待。
9. **context 上限（4096）列為已知未驗證項**，在真實長文件跑過之前，
   不得宣稱 RAG 對真實文件成立。

---

## D-019：RAG 的 HTTP 路徑驗證與「函式路徑」是兩支腳本，兩者各有一個盲點

**日期：** 2026-09-20
**狀態：** 已實作；離線測試通過，**成功路徑尚未實機跑過**（見第五節）

### 一、背景

D-018 把「檢索」與「接地」分開了，但它自己在文件裡留了一句邊界：

> 攝入這一段……**由本探針組裝**，不是走 `/api/v1/files` 那條 HTTP 路徑。

那句話是誠實的，也是當時唯一能誠實的說法 —— 那條路需要一個已登入的使用者，
而資料庫當時有 0 個使用者。這一輪把那個缺口關掉：有了管理員帳號，就可以
用一組 API 金鑰走**應用程式自己的 HTTP API**，也就是 UI 自己走的那條路。

### 二、為什麼是「再加一支」而不是「改那一支」

兩支腳本各自看得到對方看不到的東西，而**兩邊的盲點都不是缺陷，是媒介的
限制**：

| | `rag-verify.sh`（函式路徑） | `rag-http-verify.sh`（HTTP 路徑） |
|---|---|---|
| 攝入 | 探針自己呼叫切塊器／嵌入函式／向量客戶端 | `POST /api/v1/files/`，應用程式自己處理 |
| 需要帳號 | 不需要 | 需要 API 金鑰 |
| 看得到 chunk 排名 | **看得到** | **看不到** |
| 證明「上傳文件那條路」 | 證明不了 | **證明得了** |

HTTP 那一支看不到排名，是因為**非串流的 chat completion 回應不含
`sources`** —— 引用來源是走 WebSocket 事件到前端的（讀 `middleware.py`：
非串流處理函式那一段完全沒有 `sources` 的處理，串流那段的 `sources` 是
工具呼叫的引用標記，不是 RAG 檢索結果）。所以「對的段落沒撈到」與
「撈到了卻被忽略」在 HTTP 這一側分不開。**兩支一起跑，才是完整的一句話。**

### 三、三個實查（讀執行中的原始碼，不是推論）確認的陷阱

1. **`files` 在請求 body 的頂層，不是 `metadata` 裡。**
   `main.py` 用 `form_data.get('files')` 組出內部的 metadata，然後
   `form_data['metadata'] = metadata` **覆寫**掉呼叫者送來的 metadata。
   照著「`middleware.py` 讀的是 `metadata.files`」去組請求的話，那份
   metadata 會被**靜默丟掉**：HTTP 200、答案通順、完全沒有檢索。
   正向題於是失敗，而它看起來像模型不聽話。
   **這是本輪最容易犯、也最難察覺的錯**，所以它有一條專屬的斷言
   （`test_rag_http_probe.py`：`assert "files" in body` 且
   `assert "metadata" not in body`）。

2. **文件必須「處理完」才加得進知識庫。**
   `/knowledge/{id}/file/add` 會擋 `file.data` 不存在的檔案
   （`FILE_NOT_PROCESSED`），而上傳預設是**背景**處理。所以要用
   `?process=true&process_in_background=false`。副產品是：**加得進去，
   本身就是「處理完了」的機械證據** —— 不需要再去讀 `data` 欄位。

3. **`chat_id` 要給 `temporary:` 開頭。** 不給的話伺服器自己生一個 uuid，
   而那是「會被存檔」的形式（`utils/chat_id.py` 的 `is_saved_chat_id`）——
   測試會在使用者的聊天紀錄裡留下垃圾。

### 四、API 金鑰預設是關閉的 —— 這與 D-013 同一個形狀

2026-09-20 查本堆疊的資料庫：

```
config 表 auth.enable_api_keys = false
api_key 表 0 筆
```

而 `routers/auths.py` 的 `_check_api_key_permission` 在它為 false 時
**一律回 403**，不因身分是 admin 而放行。所以照著「頭像 → 設定 → 帳號 →
API 金鑰 → 建立」走的人，會找不到按鈕，或得到一句
`API_KEY_CREATION_NOT_ALLOWED`，然後合理地懷疑是自己哪裡做錯。

開啟的位置是 **管理員控制台 → 設定 → 驗證 → API 金鑰**
（`?settings=admin:authentication`）。與 D-013 相同的部分是：**它住在
config 表，改 `.env` 沒有用**。與 D-013 不同的部分是：它每次請求都重讀，
所以開關按下去就生效，不必重建容器。

**決策：不為它寫一支「幫你打開」的腳本。** `set-embedding.sh` 那個先例
是合理的（嵌入模型是這個專案自己的技術選擇），但「開放 API 金鑰」是一個
**安全屬性的變更** —— 它決定這個實例允不允許程式化存取。這種開關應該由
人在看得到的介面上按下，而不是被一支測試腳本順手打開。

### 五、本輪驗到什麼、**沒**驗到什麼

驗過的（皆為實機或對真實元件的測試）：

- 離線單元測試 11 組全過（`bash scripts/test_rag_http_probe.py`），
  包含「files 放錯位置」與「金鑰不外洩」兩條專屬斷言。
- **multipart 的編碼**：把探針組出來的那份 body 餵給**應用程式自己用的
  那顆剖析器**（Starlette → python-multipart，在容器內跑），欄位名、
  檔名、內容位元組數全部相符。這不是字串包含的那種驗證。
- **金鑰錯誤的路徑**：對真的 open-webui 發一次 401，確認它被判為
  「無法判定」而不是「未通過」，且**沒有建立任何東西**（清理那一列是
  「沒有需要清理的東西」）。
- 缺少金鑰時的行為：結束碼 2，訊息直說這是設定未完成。

**沒驗到的：成功路徑一次都沒跑過。** 因為本堆疊裡還沒有任何 API 金鑰
（見第四節），而建立金鑰只有人能按。所以現在的狀態是：

> 探針的每一行程式碼都至少被執行過一次 —— 但**是在假的 HTTP 傳輸上**
> （`test_rag_http_probe.py` 的罐頭回應）。對**真的伺服器**，只有
> 「金鑰錯誤 → 提早返回」那一小段跑過；建立知識庫、上傳、加檔案、
> 生成、清理這幾段，對真伺服器一次都沒跑過。

這一條寫在這裡，是因為本專案吃過太多次同一種虧：**一段從未被執行到的
程式碼，與一段執行過且正確的程式碼，在原始碼裡長得一模一樣。**
在金鑰被填進 `.env` 並實際跑到綠燈之前，README 與本檔都不得宣稱這條路
「已驗證」。

### 六、教訓

1. **兩支腳本各自的盲點要寫進檔頭，不要只寫「互補」。** 「看不到 chunk
   排名」是一個會改變結論的解讀限制 —— 少了它，正向題失敗時會被讀成
   「HTTP 路徑壞了」，而實際上可能只是那個 3B 模型沒用 context。
2. **「呼叫沒拋例外」與「事情真的做了」是兩件事** —— 這條在本專案出現
   過至少三次（lock-signup、connect-endpoint、D-018 的集合清理）。這裡
   的落實方式：加進知識庫之後**回讀**知識庫的檔案清單，刪除之後**回讀**
   確認 404，而不是相信 HTTP 200。
3. **清理不得改變結論。** 金鑰錯的那條路徑上，清理必須回報「沒有需要
   清理的東西」而**不是**「請手動刪除某個 id」—— D-018 的
   `cleanup_collection` 就是因為把 `NotFoundError` 當成失敗，從後門把
   「無法判定」翻成了「未通過」。

---

## D-020：Open WebUI 的後端日誌預設是靜音的，而且缺 `message_id` 會靜默跳過工具執行

**日期**：2026-09-20
**狀態**：已決定（並更正一條我先前的錯誤結論）

### 一、起因

README 的 Phase 2 MCP 清單有兩項待驗：模型**自主**呼叫工具、以及**多輪**
工具呼叫。使用者從 UI 送出測試句後回報「過了 10 分鐘沒反應」。

### 二、第一個發現：`open_webui.utils.middleware` 的 `log.*` 到不了日誌

追查過程中我在 `utils/middleware.py` 的 `process_chat_payload()` 開頭插入
一個 `log.error()` —— 那個函式**一定**會被呼叫（整個對話流程都經過它）。
**它完全沒有出現在日誌裡。**

原因：

```python
log = logging.getLogger(__name__)   # open_webui.utils.middleware
```

日誌 handler 只掛在 uvicorn / httpx 這些第三方 logger 上，`open_webui.*`
的記錄會往 root logger 傳遞，而 root 沒有掛 handler，於是**整串消失**。
這解釋了為什麼日誌裡只看得到 `uvicorn.protocols...` 與 `httpx._client...`
的訊息，以及為什麼這套堆疊「看起來從來沒有錯誤」。

**為什麼這條比 MCP 本身更值得記**：它讓「日誌裡沒有錯誤」這個觀察**失去
證據力**。本專案的核心原則是「不允許靜默失敗」，而這裡的靜默失敗發生在
**工具鏈**層級 —— 不是某段程式碼忘了檢查錯誤，而是**錯誤根本沒有出口**。

**推翻了什麼**：我在同一次調查中，先後兩次根據「日誌裡沒有例外」推論
「所以不是例外造成的」。那兩個推論**都不成立**。第一次我以為例外被
`log.debug` 吞掉（等級太低），於是把那一行改成 `log.error` —— 改成
`log.error` 之後仍然什麼都看不到，我才發現問題不在等級，而在 handler。

**落實方式**：本次調查全部改用 `print(..., flush=True)` 才取得進展。
日後凡是需要在 Open WebUI 後端插探針，**一律用 `print`，不要用 `log`**；
若要看它自己的日誌，必須先確認 logger 有掛 handler。

### 三、第二個發現：缺 `message_id` 會靜默跳過所有工具執行

```python
async def get_event_emitter_and_caller(metadata):
    event_emitter = None
    if metadata.get('chat_id') and metadata.get('message_id'):
        event_emitter = await get_event_emitter(metadata)
```

而 `streaming_chat_response_handler` 的結構是：

```python
if event_emitter:
    ...            # 完整處理：工具執行迴圈、事件推播、狀態
else:
    # Fallback to the original response
    async for data in original_generator:
        yield data          # 原樣轉發
```

**沒有 `event_emitter` 時，模型回應被原封不動轉發給客戶端** ——
包含 `finish_reason: tool_calls`。工具**從來沒有被執行**，而且：

- HTTP 200
- 不報錯
- 不留任何日誌
- 模型也正常回答了

**`chat_id` 與 `message_id` 兩者缺一不可**，少一個就整條工具路徑消失。

### 四、我犯的錯，以及它為什麼是這一條的核心

我一開始用純 HTTP 打 `/api/chat/completions` 做重現，**只送了 `chat_id`，
沒送 `message_id`**。於是我觀察到「模型產生正確的工具呼叫、open-webui
不執行」，並據此**向外下了「這是 Open WebUI 的 bug」的結論**，還查了
上游 PR（#24106，CLOSED 未合併）來佐證。

補上 `message_id` 之後，MCP server 立刻收到 `CallToolRequest`：

```
Processing request of type ListToolsRequest
Processing request of type CallToolRequest     ← 執行了
```

**我把「我的重現少送一個欄位」誤判成「程式的缺陷」。** 而且我還拿著
一個真實存在、但**與此無關**的上游 PR 當佐證 —— 這讓錯誤的結論看起來
更有根據，比單純的猜測更危險。

**教訓**：重現失敗時，**先懷疑重現，再懷疑被測物**。判準是「我的重現
與真實路徑差在哪」，而不是「哪個上游 PR 支持我的推論」。

### 五、MCP 工具呼叫本身：已驗證會動

用**應用程式自己的認證路徑**（`open_webui.utils.auth.create_token` ＋
`/api/chat/completions`，帶齊 `chat_id` + `message_id`）實測：

| 環節 | 結果 |
|---|---|
| MCP 連線（initialize／initialized） | 通過 |
| 工具探索（`tools/list`） | 通過，回傳 `echo`、`roll_die` |
| 模型自主產生工具呼叫 | 通過（`mcp-test_roll_die` args=`{"sides": 20}`、`mcp-test_echo` args=`{"text": "hello"}`） |
| 工具實際執行 | **通過**，server 收到 `CallToolRequest` |
| 最終答案文字 | **未驗證** |

最後一列要說清楚：有 `event_emitter` 時，輸出是走 **socket.io 推播**給
使用者房間，**不走 HTTP 串流**。所以我的 HTTP 客戶端收到的是空的 ——
這不是失敗，是走錯介面。要把最終答案讀出來，得開一個真正的 socket 連線，
或讓對話存進資料庫再回讀（本次嘗試的兩筆對話因為 chat 資料列不存在而
沒有落地）。

**所以 README 的兩項清單在本次調查後仍然是 `[open]`** —— 工具鏈已驗證，
但「模型算出的加總是否正確」還沒有證據。

### 六、附帶量到的效能數字

同一台機器（2 核、qwen2.5:3b）：

| 項目 | 實測值 |
|---|---|
| 產出速率 | **2.9 tok/s** |
| 提示處理 | **~15 tok/s**（邊際） |
| KV cache 重用 | **有效** —— 相同前綴第二次重算 0.1 秒（首次 37.7 秒） |
| 兩句測試的牆鐘時間 | 6.6 秒 / 53.3 秒（模型已暖） |

另外，使用者第一次嘗試的日誌顯示**兩個併發的 ollama 請求**（對話 +
標題生成），而 `OLLAMA_NUM_PARALLEL=1` 會讓它們**序列化**：第二個請求
的 2m29s **大部分是排隊等待，不是推論**。這與「卡住」的體感直接相關。

---

## D-021：同一個標題、兩套工具伺服器 —— 一套在伺服器端，一套在瀏覽器端

**日期**：2026-09-20
**狀態**：已決定（並更正 D-020 期間我對那條 toast 字串的錯誤假設）

### 一、起因

使用者回報網頁跳出：「無法連線至 http://mcp-test-server:8000/mcp
OpenAPI 工具伺服器」。而同一時間，MCP server 是健康的，從 open-webui
容器連它也完全正常（`initialize` 成功、`tools/list` 回 `echo`、`roll_die`）。
**server 沒問題，是有一筆設定在打一個永遠打不到的地方。**

### 二、這裡有兩套長得幾乎一樣的東西

| | 管理員控制台 → 設定 → 外掛功能 → 工具 | 個人 頭像 → 設定 → 工具 |
|---|---|---|
| 標題 | `External Tool Servers` | **`External Tool Servers`（同一個字）** |
| 支援型別 | OpenAPI **與 MCP (Streamable HTTP)** | **只有 OpenAPI** |
| 存在哪 | `config` 表 `tool_server.connections` | `user` 表 `settings.ui.toolServers` |
| **誰去連** | **open-webui 容器** | **使用者的瀏覽器** |
| 寫入端點 | `/api/v1/configs/tool_servers` | `/api/v1/users/user/settings/update` |

前端兩處的識別方式：管理員那支呼叫
`saveSettings({TOOL_SERVER_CONNECTIONS})`；個人那支呼叫
`saveSettings({toolServers, terminalServers})`，且唯讀 `$settings.toolServers`。
兩支的**標題字串完全相同**，只有說明文字不同 —— 個人那支寫的是
"Connect to your own **OpenAPI** compatible external tool servers." 與
"CORS must be properly configured by the provider to allow requests from
Open WebUI."（CORS 云云正是「由瀏覽器發起」的線索）。

### 三、為什麼那筆永遠連不上（三重原因，任一都足以致命）

本堆疊實測（使用者 `ian`，role=admin）：

1. 瀏覽器所在的主機**解析不到** `mcp-test-server` ——
   `getent hosts mcp-test-server` 無回應，`curl` 回
   `Could not resolve host`。那是 compose 網路內的服務名。
2. 該埠**沒有對外發布**（`docker compose port mcp-test-server 8000` →
   `invalid IP:0`）—— 這是 D-003 刻意的。
3. 就算前兩項都通了也還是不會動：那筆的 `type` 是 **`openapi`**，
   它會去抓 `<url>/openapi.json`，而 `/mcp` 是 MCP 端點，
   不提供 OpenAPI spec。

### 四、更正：那條 toast 的「OpenAPI」不是通用字串

追查之初我假設 i18n 字串 `Failed to connect to {{URL}} OpenAPI tool server`
是無條件套用的通用字串，所以「OpenAPI」三個字沒有意義。**這個假設是錯的。**
它在打包檔裡只有兩個呼叫點，兩處都只讀 `$settings.toolServers` ——
也就是**只屬於個人直接連線**那條路。字面上的 OpenAPI 就是那筆的 `type`。

### 五、它不會擋住 MCP，因為是兩個不同的 store

打包檔實測：`setTools()`（管理員工具）設的是 `Lh`，`setToolServers()`
（個人直接連線）設的是 `bf`。**不同 store。** 所以那筆壞掉的只會把
`$toolServers` 清成空陣列，`$tools` 不受影響 —— MCP 的 `echo`／`roll_die`
照樣進得了對話的工具選單。

`MessageInput.svelte` 的可見度條件正是把兩者分開算的：
`showToolsButton = ($tools ?? []).length > 0 || ($toolServers ?? []).length > 0`。

### 六、為什麼管理員特別容易踩到

`+layout` 設定分頁的可見度條件是：

```
role === "admin" || (role === "user" && permissions.features.direct_tool_servers)
```

而後端 `routers/users.py` 對 `ui.toolServers` 的處置是
「**非** admin 且沒有 `features.direct_tool_servers` 權限才 pop 掉」。
本堆疊的 `user.permissions.features.direct_tool_servers = false`、
`direct.enable = false`。也就是說：

- **非管理員**使用者：看不到那個分頁，就算硬送也會被後端刪掉。
- **管理員**：分頁看得見、後端不刪、於是留存。

**一個功能被關掉了，但關它的機制只對非管理員生效。**

### 七、處置

在 個人 設定 → 工具 移除那筆 `mcp-test-server`（type: openapi）即可。
**管理員控制台那筆 MCP 連線要留著** —— 那才是實際在用的那條路。

**已於 2026-09-20 執行完畢**，走的是 UI 存檔的同一條路
（`POST /api/v1/users/user/settings/update`，帶完整的 `ui` 物件）：

- 備份：`/tmp/webui-backup/user-settings-20260920-120038.json`
- `ui.toolServers`：1 筆 → `[]`
- 併存確認：`ui.params.tool_approval_mode`（`"full"`）、`ui.version`、
  `ui.terminalServers` 均未受影響 —— 這一點必須檢查，因為
  `models/users.py:732` 是 `user_settings.update(updated)` 的**淺合併**，
  只送 `{"ui": {"toolServers": []}}` 會把整個 `ui` 蓋掉。
- 管理員那筆 MCP 連線未動，事後重測 `tools/list` 仍回 `echo`、`roll_die`。

瀏覽器端要**重新載入**才會生效（`$settings` 是上次載入時抓進記憶體的）。

### 八、教訓

**同名的兩個 UI，不代表同一個東西。** 判斷「這個設定是誰在用」的時候，
問的不是「它叫什麼」，而是「**哪個行程會去連它**」—— 容器連得到、
瀏覽器連不到，這一條就足以分辨。

---

## D-022：加大機器不等於加大 context —— 截斷的來源是 `num_ctx`，不是 RAM

**日期**：2026-09-20
**狀態**：已決定（並更正 D-020 調查期間我對「第二次呼叫為何暴增」的錯誤假設）

### 一、起因

使用者在 UI 送出的每一句話，助手都回空白：`content = '""'`、`done = 1`、
`error = null`。**系統認為自己「成功完成」了。** 而同一個 chat、同一句話，
我用 HTTP + WebSocket 重現卻完全正常（8.1 秒、工具被呼叫、答案正確）。

既然同一條程式路徑一邊過一邊不過，差別只可能在**送出的內容**。

### 二、量到的數字

暫時在 `utils/payload.py:convert_payload_openai_to_ollama` 與
`utils/middleware.py:execute_tool_call` 插入探針（先備份、`ast.parse`
驗證語法後才重啟），量到：

| | 使用者的 UI | 我的重現 |
|---|---|---|
| 送出的工具數 | **37** | 2 |
| 工具定義大小 | **24,307 字元** | 493 字元 |
| ollama 收到的 prompt | **4,937 token** | 219 / 257 token |
| ollama 的截斷紀錄 | **`limit=2050 prompt=4937 keep=4 new=2050`** | 無 |
| 第二次呼叫的 `n_tokens` | 2,050（被砍到上限） | 257 |
| 最終答案 | 空白 | 「你丟了一顆六面骰子，結果是 4。」 |

多出來的 35 個工具來自 `config` 表四個全開的開關：`memories.enable`、
`notes.enable`、`automations.enable`、`calendar.enable`（再加上知識庫、
聊天、核心工具）。名稱可在探針輸出中逐一核對。

### 三、根因：工具是 prompt 的一部分，不是額外欄位

`ollama show qwen2.5:3b --modelfile` 的對話模板開頭就是：

```
{{- if .Tools }}
# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{{- range .Tools }}
{"type": "function", "function": {{ .Function }}}
{{- end }}
</tools>
```

**工具定義會被渲染進 system prompt。** 這解釋了算術：24,307 字元 ÷ 4,937
token ≈ **4.9 字元/token**，正是 JSON schema 的密度。那 4,937 個 token
絕大多數就是這 37 個工具的定義。

而 `num_ctx` 是 4096 —— `ollama show qwen2.5:3b --modelfile` 顯示該模型
**沒有任何 `PARAMETER` 行**（`grep -c '^PARAMETER'` = 0），所以走 ollama
的預設值。塞不進去時 ollama 只保留**前 4 個 token + 最後 2,050 個**，中間
全丟 —— 而中間裝的是工具定義與對話。**模型在第二次呼叫時根本沒看到問題**，
所以吐出空字串、而且 `error = null`（它沒有失敗，它只是沒東西可答）。

### 四、為什麼「只加大 VM」不會過

`num_ctx` 與機器大小**完全無關**。把 VM 從 3.8GB 加到 32GB，`num_ctx`
還是 4096，截斷上限還是 2,050，4,937 個 token 照樣被砍掉中間。

**RAM 的角色是「讓你能把 `num_ctx` 開大」，不是「自動讓 context 變大」。**
兩件事要一起做，只做後者等於沒做。

| `num_ctx` | 截斷上限 | 4,937 塞得進去嗎 |
|---|---|---|
| 4096（目前預設） | 2,050 | ❌ |
| 8192 | 4,096 或 6,146 | ⚠️ 取決於截斷規則，可能不夠 |
| **16384** | **8,192 或 14,338** | ✅ 兩種規則都夠 |

**建議 16384 而非 8192**：本堆疊只觀察到一個數據點（`n_ctx=4096` →
`limit=2050`），無法確定 ollama 是「砍一半」還是「扣掉輸出保留額」。
16384 在兩種規則下都安全，不需要先確定是哪一種。

模型本身支援到 32768（`ollama show` 的 `context length`），瓶頸不是模型。

### 五、`num_ctx` 的三條設定路徑（逐字驗證過）

1. **Open WebUI 進階參數**（單一模型、最省事）
   模型選單 → 該模型 → 進階參數 → `num_ctx` = `16384`。
   已驗證前端確實有此欄位：`grep -roE '"?num_ctx"?' /app/build/_app/immutable/`
   在 `chunks/0uv-gfwg.js` 與 `nodes/2.DY2zKRtP.js` 都有命中。
   這個值會經 `payload.py` 的 `options` 原樣傳給 ollama。

2. **環境變數 `OLLAMA_CONTEXT_LENGTH`**（整個 ollama 的預設）
   已驗證本堆疊的 ollama 0.34.2 binary 認得這個名字：
   `grep -a -o -E "OLLAMA_[A-Z_]{3,}" /usr/bin/ollama` 有 `OLLAMA_CONTEXT_LENGTH`。
   加進 `docker-compose.yml` 的 ollama 服務後重啟。

3. **Modelfile `PARAMETER num_ctx 16384`**（模型層級、最持久）
   由於 qwen2.5:3b 目前沒有任何 `PARAMETER` 行，加一行不會與既有值衝突。
   以 `ollama create` 產生新模型後，在 Open WebUI 指向它。

**KV cache 的代價**（**估算值，本堆疊未實測**）：層數與 KV head 數取自
Qwen2.5-3B 的架構（36 層、2 個 KV head、head_dim 128、f16），
每 token ≈ 2 × 36 × 2 × 128 × 2 B ≈ **36 KiB**。

| `num_ctx` | KV cache（估） | 加權重 1.9GB 後（估） |
|---|---|---|
| 4096 | ~151 MB | ~2.1 GB |
| 16384 | **~604 MB** | **~2.5 GB** |

8GB 機器：模型 2.5GB + Open WebUI ~1GB + OS/docker ~1GB ≈ 4.5GB，放得下。
本堆疊的 3.8GB（`available` 只剩 286MB）放不下 —— 這與「現在跑不動」
的觀察一致。

記憶體仍然吃緊時，binary 裡另有一個槓桿：`OLLAMA_KV_CACHE_TYPE`
（量化 KV cache）。**本堆疊尚未實測其效果與品質影響**，僅記錄它存在。

### 六、三個坑

1. **`num_ctx` 才是開關，不是 RAM。** 加大機器而沒改 `num_ctx`，行為
   一模一樣 —— 這正是本條目要記下來的東西。
2. **不要順手把 `OLLAMA_NUM_PARALLEL` 拉高。** 日誌顯示
   `n_ctx_slot = 4096`（在 `NUM_PARALLEL=1` 下等於 `num_ctx`），所以
   `num_ctx` 是**每個 slot** 的量。拉到 4 的話 KV cache 是**乘 4**
   （16k × 4 ≈ 2.4GB），不是把 context 分掉。這是「機器變大反而更容易
   OOM」的常見走法。
3. **速度仍是瓶頸。** 量到 `prompt processing, n_tokens = 512,
   progress = 0.25, 23 tokens/秒` —— 2,050 個 token 要 89 秒，而且
   `OLLAMA_KEEP_ALIVE=5m` 讓模型反覆卸載重載（日誌可見
   `llama-server started in 6.38 seconds`）。加大機器會快幾倍，但純 CPU
   跑 3B 模型、每次處理 5,000 token，一來一回仍以「分鐘」計。

### 七、兩條路，以及為什麼先走免費的那條

| | 做法 | 成本 | 結果 |
|---|---|---|---|
| **A** | 關掉記憶／筆記／自動化／日曆 → 工具 37 → 2 | 免費 | prompt 4,937 → ~250 token，**現在的機器就能過** |
| **B** | 加大 VM **且** 設 `num_ctx=16384` | 機器錢 | 4,937 塞得進去，37 個工具全留 |

**建議先走 A**，理由不只是省錢：

- README 第二階段要驗的是「模型會不會**自主**呼叫 MCP 工具」與「多輪
  加總是否正確」。掛 35 個用不到的工具不會讓這個驗證更有說服力，
  只會讓它跑不動。
- 就算換了大機器，每一則訊息都送 5,000 個 token 的無用工具定義，
  仍然白白拖慢每一次回應。
- **先驗過 MCP 再決定要不要為那四個功能付機器錢。** 順序反過來的話，
  失敗時會分不清是 MCP 的問題還是 context 的問題。

### 八、診斷方法上會誤導人的兩個地方（我實際踩了）

1. **`POST /api/chat/completions` 回 `null` 是正常的，不是錯誤。**
   Open WebUI 把事件走 WebSocket 送，HTTP 就回 4 個位元組的 `null`。
   我一度把 `body=b'null'` 判成「請求失敗」，據此往下追了很久的錯誤方向。
   `main.py:1690` 的註解只在「沒有 chat_id/message_id」時才會改成
   丟 `HTTPException`；有 chat_id 時走的是這條靜默路。
   **要診斷就得聽 WebSocket，不能只看 HTTP。**
2. **「日誌裡沒有瀏覽器的 WebSocket 連線」不能推論它沒連上。**
   `socket/main.py:406` 的 `connect` 處理器**沒有任何 log 語句**。
   對照組：我自己那條成功的連線也沒留下任何紀錄。**沒有日誌 ≠ 沒有連線。**

還有一個純量測的教訓：探針要印**訊息組成**（`role:長度`）與**工具大小**，
不要只印總量。我最初只追「第二次呼叫為什麼多 4,666 個 token」，
一路假設是巨型工具結果、巨型錯誤訊息、ExceptionGroup —— 全部錯。
探針印出 `toolchars=24307` 的那一行才是答案。

### 九、推翻了什麼

- 推翻了「**加大 VM 就能跑得動**」這個直覺。加大 VM 只解除記憶體限制，
  不解除 `num_ctx` 限制。
- 推翻了我在 D-020 調查期間的假設：「第二次呼叫暴增是因為工具回傳了
  約 18,000 字元的錯誤訊息／`ExceptionGroup`」。**探針明確顯示沒有任何
  訊息超過 1,500 字元**；增加的是工具定義，不是訊息。
- 推翻了「`body=b'null'` 代表請求失敗」。那是 WebSocket 路徑的正常形狀。

### 十、教訓

**問「這台機器夠不夠大」之前，先問「這個數字是被什麼決定的」。**
`num_ctx` 是設定值，不是硬體值 —— 硬體只決定你能把它設到多大。
把「跑不動」直接翻譯成「機器太小」，會讓人花錢買到一個**完全沒有改變
行為**的升級。

