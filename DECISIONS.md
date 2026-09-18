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
