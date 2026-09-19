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
- [架構](#架構)
- [快速開始](#快速開始)
- [對外連線的安全性](#對外連線的安全性)
- [驗證清單](#驗證清單)
- [額度管理](#額度管理)
- [第二階段](#第二階段)
- [第三與第四階段 —— 計畫中，尚未量測](#第三與第四階段--計畫中尚未量測)
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

### 可以原樣帶走的部分

| 項目 | 為什麼能存活 |
|---|---|
| `docker-compose.yml` | 兩個容器加一個 bridge 網路，沒有任何 Codespaces 專屬的東西。 |
| Open WebUI 的狀態 | 對話、Knowledge、MCP 連線、使用者、設定全都存在 `open_webui_storage` volume 裡。複製 volume，資料就過去了。 |
| 模型選擇 | `.env` 的 `OLLAMA_MODEL` 是**唯一**提到模型名稱的地方，腳本與 compose 都讀這個變數。換更大的模型是一行的事。 |
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
不是 compose 檔的宣告；它也會看這台機器有沒有公開位址，來區分
「對 Internet 開放」與「只有區網可達」。注意後者是**靠你的路由器擋住的，
不是靠這套堆疊** —— 同一個設定一落到公開 IP 上，就會變成真的暴露。

同時要維持 11434 不發布（D-003）。Ollama 完全沒有認證機制，
這支腳本也會一併檢查。

### 第 2 步：放大模型與資源限制

`.env` 裡有三個值是為 2 核心、8GB 調的。它們是第一個該調大的地方，
而且三個都已經是可設定的 —— 不需要改 compose：

```bash
OLLAMA_MODEL=qwen3:70b          # 或這台 VPS 裝得下的任何模型
OLLAMA_MAX_LOADED_MODELS=3
OLLAMA_NUM_PARALLEL=4           # 吞吐提升明顯；多個請求共用一次模型載入
OLLAMA_KEEP_ALIVE=-1            # 常駐；重載一次要數十秒
```

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
   **OpenAI 相容連線**（Admin → Settings → Connections）連到 vLLM。
   兩者可以並存，所以這次搬遷可以是漸進的 —— 兩邊都跑、比較、再移除不要的。
   本專案不需要為此改任何東西。

4. **D-001 的 8GB 記憶體預算不再是限制。** 那正是搬遷的目的，但這也意味著
   好幾個決策背後的理由（只載入一顆模型、單一平行請求、因為其他方式都失效
   所以維持 thinking 開啟）是針對**這台機器**的。要重新檢查，而不是繼承 ——
   特別是 D-011 的「維持 thinking 開啟」，當 GPU 讓「每題 264–293 秒」
   變得無關緊要時，值得重新看一次。

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
| `bash scripts/check-exposure.sh` | 哪些埠真的能從外面連到 —— 讀執行中容器的實際綁定，不是讀設定檔 |
| `bash scripts/verify.sh` | 第二階段前置驗證：生成速度、tool calling、thinking、事實正確性（含對照題）、繁中輸出（約 15 分鐘）。結束碼：`0` 關鍵項目全過、`1` 有項目未通過、`2` **無法判定** —— 那不是失敗（D-016） |
| `bash scripts/verify.sh 2` | 同上，但只跑第 2 項（關閉 thinking，約 1 分鐘） |
| `bash scripts/verify.sh 3,4 qwen3:1.7b` | 用指定模型跑指定項目（模型比較用；模型只能走參數，環境變數會被 `.env` 覆蓋） |
| `bash scripts/ask_probe.sh qwen3:4b qwen2.5:3b` | 模型的答案**事實是否正確**，外加速度。每道事實題都附一道對照題（每題約 1–2 分鐘） |
| `bash scripts/rag-verify.sh` | **第二階段 RAG 的機械驗證**：檢索有沒有挑對段落、模型有沒有真的用它、文件沒寫的東西它會不會照樣發明。走應用程式自己的檢索函式。模型取自 `.env` 的 `OLLAMA_MODEL`，而**範例檔的預設是 `qwen3:4b`（思考型）—— 照這一行原樣跑，三次生成會全部撞到 300 秒上限，至少 15 分鐘之後得到「無法判定」**（2026-09-19 實測）。想要會亮綠燈的跑法請看下一行。結束碼同上，另加 `3` = 探針自己壞掉（D-018） |
| `bash scripts/rag-verify.sh --model qwen2.5:3b` | 同上，指定模型。**建議的跑法**：非思考型模型，三次生成各 41–94 秒，整輪約 2–5 分鐘（四輪 12 個樣本實測，D-018）。模型必須是參數——`.env` 會覆蓋環境變數 |
| `bash scripts/rag-verify.sh --timeout 900` | 拉長單題時限，給思考型模型用。「太慢」與「連不上」會分開講 —— 兩者要修的東西不同 |
| `bash scripts/check-egress.sh` | **資料可能流向哪些外部服務** —— 以資料庫的實際值為準，列出「啟用中」的外部端點 |
| `bash scripts/check-egress.sh --fix` | 關掉 `openai.enable`，重啟容器，並回讀確認（D-017） |
| `bash scripts/probe-openai.sh [URL]` | 任何 OpenAI-compatible runtime 的相容性探針。預設指向 Ollama 的 `/v1` |
| `bash scripts/connect-endpoint.sh --url URL --check` | 接上 GPU runtime **之前**先驗證相容性，不做任何變更 |
| `bash scripts/connect-endpoint.sh --url URL` | 接上一個 OpenAI-compatible runtime（先驗證、後接線、再回讀確認） |
| `bash scripts/connect-endpoint.sh --status` | 目前接的是哪一個 runtime |
| `bash scripts/connect-endpoint.sh --disconnect` | 切回只有 Ollama |
| `bash scripts/lock-signup.sh` | 驗證註冊是否真的關著；若開著，透過設定 API 關閉 |
| `bash scripts/lock-signup.sh --check` | 只驗證，不做變更。註冊開著時結束碼非 0 |

接上 GPU runtime（Kaggle + Endpoint，或 VPS + vLLM）另有一份專門的指南：
[`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md) · [`docs/ENDPOINT.md`](docs/ENDPOINT.md)。

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

本專案**有**自動化測試 —— `scripts/test_rag_probe.py`（57 項檢查）、
`scripts/test_verify_api.py`、`scripts/test_ask_probe.py`、
`scripts/test_egress_probe.py`、`scripts/test_probe_openai.py`、
`scripts/test_runtime_state.py` 與 `scripts/test_rag_grounding_probe.py` ——
離線單元測試：五支給探針、一支給 runtime 設定讀寫、一支給 RAG 評分，
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

- [ ] **容器健康** —— `bash scripts/status.sh` 顯示兩個容器皆為 `running`，
      且 `open-webui` 為 `healthy`
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

- [ ] 在 **Workspace → Knowledge** 建立知識庫
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
> **Admin → Settings → Documents → Embedding**。
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
[第三與第四階段](#第三與第四階段--計畫中尚未量測)，完整取捨見
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

## 第三與第四階段 —— 計畫中，尚未量測

> **狀態：這是計畫，不是結果。** 本節沒有任何一項跑過。它存在的目的是
> 在動手**之前**把設計寫下來，並讓它所依賴的假設明顯到可以被測試。
> 以下每一項都標記為 **[已查證]**（2026-09-19 對文件／原始碼查證）或
> **[未驗證]**（尚無任何量測支持）。

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
| 迴圈防護 | `recursion_limit=5` | 見 **[未驗證]** 第 4 項 |
| 記憶層 | `mem0` + ChromaDB | **[未驗證]** 第 2、3 項 |
| 排程器 | `APScheduler`，單一 process 內 | **[未驗證]** 第 6 項 |
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

以下每一項都是本專案尚未做過的量測。排序即為應該解決的順序 ——
因為若前一項失敗，後面的都是白做工。

1. **[未驗證] tool calling 換了程式路徑之後還成立嗎？** D-011 證明的是
   **Open WebUI 流程**下的多輪 tool calling。LangGraph 透過 `bind_tools`
   與 Ollama 整合的原生 function calling 綁定工具 —— 那是不同的實作。
   **在任何東西蓋上去之前先重新量測**；見上方第二階段 MCP 的註記與 D-014。

2. **[未驗證] Chroma 預設會拒收本專案的嵌入向量。** mem0 的 Chroma 後端
   預設為 **1536 維**（OpenAI 的尺寸）。本專案的嵌入模型是
   `qwen3-embedding:0.6b`，實測為 **1024 維**（D-013）。維持預設值會在
   寫入時以 shape mismatch 失敗。`embedding_dims` 與 collection 的維度
   都必須固定為 1024 —— 而且日後不能更換嵌入模型而不重新嵌入，
   理由與上方第二階段所述相同。

3. **[未驗證] mem0 每輪對話要多付一次 LLM 呼叫，而這裡只有 2 vCPU。**
   mem0 的寫入路徑會先跑一次 LLM 抽取事實，再存進向量庫。那次呼叫是
   **額外於**生成答案的。以本專案實測的速率（3.8–6.9 tok/s，D-011／D-014）
   來看，這可能是第四階段最大的一筆成本 —— 大到應該在設計記憶層**之前**
   單獨計時，而不是設計完才發現。

4. **[未驗證] `recursion_limit=5` 對 Plan-and-Execute 很可能太緊。**
   這個上限算的是 graph 的 super-step，而 Plan → Execute → Check → Retry
   只要發生一次重試就可能超過 5。應該針對每個 graph 刻意設定防護值，
   而不是沿用一個全域數字 —— 一個在正常運作時就會觸發的上限，
   最後只會被調高到失去意義。

5. **[未驗證] 8GB 上的第二套向量庫。** Open WebUI 本身已為 Knowledge
   維護一套儲存；ChromaDB 會是第二套。D-001 的 8GB 預算
   （OS 1.0 + Open WebUI 1.0 + 模型 3.0 ≈ 5.0GB）從未把它算進去。

6. **[未驗證] `APScheduler` 在 process 內，重啟就丟掉排程。**
   放在 process 內是為了省掉一個 Redis 容器 —— 那正是它的目的 ——
   但沒有持久化的 job store，每次容器重啟都會安靜地丟掉排程。
   在依賴排程之前，先決定它是否必須挺過重啟。

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
