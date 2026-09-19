# self-cloud-agent-lab

[English](README.md) | **繁體中文**

在**免費雲端額度內**驗證一套 self-hosted AI 平台的可行性：
自己執行 LLM、讀自己的資料、透過 MCP 使用工具、並讓 agent 自主完成工作。

> **本專案的定位是「驗證沙箱」，不是常駐服務。**
> 原因見下方第一節 —— 這是開始之前最需要理解的一件事。

---

## 目錄

- [為什麼不是常駐服務](#為什麼不是常駐服務)
- [架構](#架構)
- [快速開始](#快速開始)
- [對外連線的安全性](#對外連線的安全性)
- [驗證清單](#驗證清單)
- [額度管理](#額度管理)
- [第二階段](#第二階段)
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
| `bash scripts/up.sh` | 啟動堆疊 + 確保模型存在（冪等） |
| `bash scripts/down.sh` | 停止容器，**保留**模型與對話紀錄 |
| `bash scripts/down.sh --purge` | 停止容器並**刪除**所有 volume |
| `bash scripts/pull-model.sh` | 單獨重試模型下載 |
| `bash scripts/status.sh` | 容器狀態、模型清單、記憶體用量 |
| `bash scripts/verify.sh` | 第二階段前置驗證：生成速度、tool calling、thinking、繁中輸出（約 10 分鐘） |
| `bash scripts/verify.sh 2` | 同上，但只跑第 2 項（關閉 thinking，約 1 分鐘） |
| `bash scripts/verify.sh 3,4 qwen3:1.7b` | 用指定模型跑指定項目（模型比較用；模型只能走參數，環境變數會被 `.env` 覆蓋） |
| `bash scripts/lock-signup.sh` | 驗證註冊是否真的關著；若開著，透過設定 API 關閉 |
| `bash scripts/lock-signup.sh --check` | 只驗證，不做變更。註冊開著時結束碼非 0 |

### 證據腳本

這幾支之所以存在，是因為本專案已經兩次交出「回報成功但其實沒作用」的修正。
它們會真的開容器、真的打端點，觀察真實行為 —— 動到它們涵蓋的東西時請重跑。

| 指令 | 證明了什麼 |
|---|---|
| `bash scripts/verify-lock-signup.sh` | `ENABLE_SIGNUP` 在第一次開機之後就是無效的 —— 三次開機共用一個 volume（約 25 分鐘） |
| `python3 scripts/signup_control_probe.py URL` | 真正控制註冊的是哪條路徑，並以實際打端點驗證 |
| `python3 scripts/verify_lock_signup_script.py URL .` | `lock-signup.sh` 真的關得掉、可重複執行、且失敗時會吵 |
| `bash scripts/verify-first-admin.sh` | `ENABLE_SIGNUP=false` 不會擋住你建立第一位管理員 |

---

## 對外連線的安全性

**第一階段沒有私有資料，所以這一節還不重要。會改變這件事的是 RAG** ——
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
停用 profile **不會**停止已經在跑的容器 —— `up.sh` 與 `down.sh` 都帶了
`--remove-orphans`，才會真正把殘留的 `cloudflared` 移除。少了這個旗標，
「我已經把 tunnel 關掉了」會是個錯誤的認知，而服務其實仍連得到。

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

本專案**沒有自動化測試**。這是可行性 POC，其核心行為 ——
推論品質、MCP tool calling 的穩定性 —— 本質上需要人工判斷。
以下步驟以手動方式執行。

### 第一階段：基礎堆疊

- [ ] **容器健康** —— `bash scripts/status.sh` 顯示兩個容器皆為 `running`，
      且 `open-webui` 為 `healthy`
- [ ] **模型就緒** —— 模型清單中出現 `qwen3:4b`
- [ ] **推論正常** —— 在 Open WebUI 中送出「用三句話解釋什麼是 MCP」，
      約 10–30 秒內取得合理回答
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

- [ ] 在 **Workspace → Knowledge** 建立知識庫
- [ ] 上傳一份 PDF 或 Markdown 文件
- [ ] 對該知識庫提問，確認回答引用了文件內容而非憑空生成
- [ ] **反例測試**：詢問一個文件中**不存在**的細節，
      確認模型回答「不知道」而非編造

> **非英文文件的注意事項**：Open WebUI 的預設嵌入模型是
> `sentence-transformers/all-MiniLM-L6-v2` —— **僅支援英文**、384 維、
> 約 500MB RAM。若文件是中文或其他非英文語言，檢索品質會很差，
> 必須改用多語言嵌入模型，透過 `RAG_EMBEDDING_ENGINE=ollama` 與
> `RAG_EMBEDDING_MODEL=nomic-embed-text` 切換。
> **日後更換嵌入模型需要重新嵌入所有文件**，因此請在上傳前決定。
> 另需注意：有回報指出較大的嵌入模型會讓 RAM 從 2GB 暴增至 14GB ——
> 在小機器上請預留餘裕。

### 第二階段：MCP

- [ ] 在 **Admin Settings → External Tools** 新增 MCP server
      （Type 必須選 **MCP (Streamable HTTP)**，不是 OpenAPI）
- [ ] 確認工具出現在對話的 tools 清單中，且帶有 `server:mcp:` 前綴
- [ ] 觸發一次工具呼叫，確認模型**自行決定**使用工具（而非被明確指示）
- [ ] **多輪測試**：需要連續呼叫兩次以上工具的任務，
      確認 4B 模型能維持流程不中斷

> **本階段最關鍵的未知數**：4B 等級的模型能否穩定執行多輪 tool calling。
> 若失敗率過高，需改用 `qwen3:8b` 並接受記憶體壓力，或改用更大的機器。

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

**因此 LangGraph 暫不引入** —— 僅在出現「需要自訂多步驟 workflow 或
明確狀態機」的需求時才評估。完整取捨見
[`DECISIONS.md`](DECISIONS.md) 的 D-005。

> **授權警告（已查證）**：`langgraph-server` / `langgraph-api` 生產容器映像檔
> 採用 **Elastic License 2.0**。在生產環境自架需要授權金鑰
> （Plus 方案以上的 `LANGSMITH_API_KEY`，或 `LANGGRAPH_CLOUD_LICENSE_KEY`）；
> 沒有金鑰會在啟動時拋出 `INVALID_LICENSE`。**`langgraph` 函式庫本身是 MIT
> 且免費** —— 請在你自己的服務中使用它，或使用 `langgraph dev`，
> 避開商業版 server 映像檔。

### 啟用 MCP

**沒有 `ENABLE_MCP` 這個環境變數** —— 它不存在於 Open WebUI（已查證官方文件與
後端原始碼），設了只會讓你以為 MCP 已經開啟。MCP 的啟用方式是新增一條連線，
設定會存進資料庫：

1. **Admin Settings → External Tools** → **+**
2. Type 選 **MCP (Streamable HTTP)**
3. 填入 Server URL 與認證方式
4. 儲存

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

## Codespaces 的坑

已查證、容易浪費數小時的問題：

| 坑 | 說明 |
|---|---|
| **永遠沒有 GPU** | Codespaces 的 GPU 機器類型已於 **2025-08-29 下架**。所有推論都是 CPU-only。 |
| **推論很慢** | 3B–4B Q4 模型在 2 個共享 vCPU 上約為個位數 tokens/sec。Open WebUI 預設的 300 秒 HTTP timeout 可能被長回答觸發。 |
| **下載需要約 2 倍磁碟** | 模型下載需要同時容納壓縮檔與解壓後的檔案，因此接近全滿的 volume 會下載失敗 —— 有時是靜默失敗。 |
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
