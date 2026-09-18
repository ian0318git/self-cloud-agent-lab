# self-cloud-agent-lab

在**免費雲端額度內**驗證一套 self-hosted AI 平台的可行性：
自己執行 LLM、讀自己的資料、透過 MCP 使用工具、並讓 agent 自主完成工作。

> **本專案的定位是「驗證沙箱」，不是常駐服務。**
> 原因見下方「為什麼不是常駐服務」一節 —— 這是最重要的前提，請先讀。

---

## 目錄

- [為什麼不是常駐服務](#為什麼不是常駐服務)
- [架構](#架構)
- [快速開始](#快速開始)
- [驗證清單](#驗證清單)
- [額度管理](#額度管理)
- [第二階段](#第二階段)
- [疑難排解](#疑難排解)

---

## 為什麼不是常駐服務

GitHub Codespaces 免費額度的實際換算：

| 項目 | 免費額度 | 換算後的真實可用量 |
|---|---|---|
| Compute | 120 core-hours/月 | 2-core 機器消耗 2 core-hours/小時 → **僅 60 真實小時/月** |
| Storage | 15 GB-month | **codespace 存在期間就計費，停止狀態照算** |
| Idle timeout | 預設 30 分鐘 | 閒置即自動停止 |
| Port 可見性 | **預設 private** | 需手動改為 public |

**60 小時 ÷ 30 天 ≈ 每天 2 小時。** 若嘗試 24/7 常駐，
額度會在 **2.5 天內耗盡**，接著 $0 spending limit 會直接中斷環境。

儲存額度的計算方式是：

```
GB-month = (佔用 GB × 存在小時) ÷ 730
```

因此要讓一個 codespace 24/7 存在整月，總佔用必須 ≤ 15 GB。
關鍵陷阱：`docker compose down` 或停止 codespace **都不會停止儲存計費**，
只有**刪除整個 codespace** 才會。

**結論**：本專案適合用來驗證「這套架構能不能跑、模型夠不夠聰明」，
驗證完就刪除。若要建立真正常駐的服務，需要另尋宿主（見 [第二階段](#第二階段)）。

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
| **Qwen3 4B** | 預設模型（2.5 GB，支援 256K context 與 tool calling） |
| **Open WebUI** | 網頁聊天介面 + 內建 RAG + 原生 MCP 支援 |

---

## 快速開始

### 在 Codespaces 上

1. 在 GitHub 上開啟此 repo → **Code** → **Codespaces** → **Create codespace on main**
2. 選擇 **2-core / 8GB** 機器（**不要選 4-core，會讓額度消耗變成 4 倍**）
3. 等待建立完成 —— `postCreateCommand` 會自動執行 `scripts/up.sh`，
   包含下載模型（首次約需數分鐘）
4. 開啟 **PORTS** 面板 → 點擊 **3000** 埠的網址
5. 註冊第一個帳號（**它會自動成為管理員**）
6. 登入後，將 `.env` 的 `ENABLE_SIGNUP` 改為 `false`，再執行：
   ```bash
   docker compose up -d open-webui
   ```

### 在本機

需求：Docker 與 Compose v2。**至少 8GB RAM**（本機若只有 4GB 請改用 `qwen3:1.7b`）。

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

開啟 <http://localhost:3000>。

### 指令一覽

| 指令 | 用途 |
|---|---|
| `bash scripts/up.sh` | 啟動堆疊 + 確保模型存在（冪等） |
| `bash scripts/down.sh` | 停止容器，**保留**模型與對話紀錄 |
| `bash scripts/down.sh --purge` | 停止容器並**刪除**所有 volume |
| `bash scripts/pull-model.sh` | 單獨重試模型下載 |
| `bash scripts/status.sh` | 顯示容器狀態、模型清單、記憶體用量 |

---

## 驗證清單

本專案**沒有自動化測試** —— 這是一個以「驗證可行性」為目的的 POC，
且核心行為（模型推論品質、agent 工具呼叫穩定性）本質上需要人工判斷。
以下為手動驗證步驟。

### 第一階段：基礎堆疊

- [ ] **容器健康** — `bash scripts/status.sh` 顯示兩個容器皆為 `running`，
      且 `open-webui` 的 healthcheck 為 `healthy`
- [ ] **模型就緒** — `status.sh` 的模型清單中出現 `qwen3:4b`
- [ ] **推論正常** — 在 Open WebUI 中送出「用三句話解釋什麼是 MCP」，
      約 10–30 秒內取得合理回答
- [ ] **串流正常** — 回答是逐字浮現，而非停頓後一次出現
- [ ] **記憶體未爆** — 對話進行中，`status.sh` 的 `Mem` 一行顯示
      available 仍有餘裕（未被 swap 吃光）
- [ ] **模型卸載** — 靜置超過 `OLLAMA_KEEP_ALIVE` 後，
      `docker stats` 顯示 ollama 容器記憶體明顯下降
- [ ] **重啟存活** — `bash scripts/down.sh && bash scripts/up.sh` 後，
      **模型不需重新下載**，且先前的對話紀錄仍在

### 第二階段：RAG

- [ ] 在 Open WebUI 的 **Workspace → Knowledge** 建立知識庫
- [ ] 上傳一份 PDF 或 Markdown 文件
- [ ] 對該知識庫提問，確認回答引用了文件內容而非憑空生成
- [ ] **反例測試**：詢問一個文件中**不存在**的細節，
      確認模型回答「不知道」而非編造

### 第二階段：MCP

- [ ] 在 **Admin Settings → External Tools** 新增一個 MCP server
      （Type 選 **MCP (Streamable HTTP)**，不是 OpenAPI）
- [ ] 確認工具出現在對話的 tools 清單中，且帶有 `server:mcp:` 前綴
- [ ] 觸發一次工具呼叫，確認**模型自行決定**使用工具（而非被明確指示）
- [ ] **多輪測試**：需要連續呼叫兩次以上工具的任務，
      確認 4B 模型能維持流程不中斷

> **本階段最關鍵的待驗證假設**：4B 等級的模型能否穩定執行多輪 tool calling。
> 若失敗率過高，需改用 `qwen3:8b` 並接受記憶體壓力，或改用更大的機器。

---

## 額度管理

### 查看用量

<https://github.com/settings/billing> — 會有兩個獨立的進度條：

- **Compute** — 120 core-hours
- **Storage** — 15 GB-month

### 降低消耗的做法

| 做法 | 效果 |
|---|---|
| 選 2-core 而非 4-core | 額度消耗減半 |
| 工作結束即 `bash scripts/down.sh` | 停止 compute 計費 |
| 用 `gh codespace stop -c <name>` 停止 codespace | 停止 compute 計費 |
| **刪除** codespace（非僅停止） | 停止**儲存**計費 |
| 關閉編輯器分頁 | 避免被判定為活躍而持續計費 |

### 重要提醒

執行中的程序、終端機輸出、或已開啟的埠流量**都會讓 codespace 被判定為活躍**，
即使你人不在電腦前。最保險的做法永遠是手動停止。

---

## 第二階段

Open WebUI **自 v0.6.31 起原生支援 MCP**，且內建 agentic mode 的工具已涵蓋
原計畫多數需求：

| 原計畫需求 | Open WebUI 內建工具 |
|---|---|
| RAG 文件知識庫 | `query_knowledge_bases` / `search_knowledge_files` / `view_knowledge_file` |
| 記憶 | `search_memories` / `add_memory` |
| 網路檢索 | `search_web` / `fetch_url` |
| 筆記 / 對話歷史 | `search_notes` / `write_note` / `search_chats` |

**因此 LangGraph 暫不引入** —— 僅在出現「需要自訂多步驟 workflow 或明確狀態機」
的需求時才評估。理由與完整取捨見 [`DECISIONS.md`](DECISIONS.md) 的 D-005。

### 啟用 MCP

`ENABLE_MCP=true` 已預設開啟。接著：

1. **Admin Settings → External Tools** → **+**
2. Type 選 **MCP (Streamable HTTP)**
3. 填入 Server URL 與認證方式
4. 儲存

> `stdio` 類的本地 MCP server（如 Claude Desktop 用的那些）無法直連，
> 需透過 **MCPO proxy** 橋接。

---

## 疑難排解

### `WEBUI_SECRET_KEY 未設定` 導致啟動失敗

這是刻意的設計，不是 bug。執行 `bash scripts/up.sh` 會自動產生；
或手動 `cp .env.example .env` 後填入任意 32 bytes 十六進位字串。

**不要**為了繞過而留空 —— 那會導致第二階段的 MCP 工具在每次容器重建後
出現 `Error decrypting tokens`。

### 模型下載中斷

```bash
bash scripts/pull-model.sh
```

Ollama 支援中斷續傳。

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

## 授權

個人 POC 專案。
