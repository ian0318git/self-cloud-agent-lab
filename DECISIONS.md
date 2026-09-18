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
（`ENABLE_MCP=true`，Admin → External Tools → 選 `MCP (Streamable HTTP)`），
且 agentic mode 的內建工具已涵蓋原計畫的多數需求：

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
**狀態**：待第二階段實測後確認

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

**剩餘限制**：
- 一個簡單問答約需 30–110 秒（thinking 佔絕大部分）
- 對互動式使用偏慢；批次／非同步的工作流程則可接受
- 本結論僅適用於 2-core CPU；4-core 預期約兩倍速度
