# self-cloud-agent-lab

**一套留在他自己邊界之內的私有 AI 堆疊。**

你自己的模型、你自己的文件、你自己的工具 —— 從免費沙盒一路量到租來的 GPU。

[English](README.md) · [繁體中文](README.zh-TW.md)

> **量出來的，不是假設出來的。**
> 這個專案記下什麼能用、什麼會壞、要付多少，以及什麼到現在還只是推估。

---

## 為什麼需要這個專案

把公司的合約貼進代管 AI 服務，你就已經把它交出去了。那改成在自己的機器上跑？
你得先有硬體 —— 一個 7–8B 的模型就要**約 8 GB 的 VRAM** 才肯開口。

這兩條路之間的落差不是技術問題，是**沒有人在你投入之前，告訴你自架到底要付
多少** —— 多少錢、每秒多少 token、要花幾個小時。產品頁寫「production-ready」，
不會寫「CPU VPS 上 5 token/s」—— 那比你打字還慢。

多數指南教你怎麼把零件接起來。這一份量的是接起來之後**實際上會發生什麼** ——
**成本、tokens/s、建置時間，以及失敗。**

**從小處開始。量它。把失敗留著。只有在證據說值得的時候才放大。**

---

## 這是什麼

一套完整的私有堆疊 —— 推論、對話、RAG、工具呼叫 —— 背後的網路設計讓
**沒有任何東西暴露在外**（[D-003](DECISIONS.md)）。

| 元件 | 角色 |
|---|---|
| **Ollama** | 本機推論引擎。**不對外發布** —— 只有 Docker 網路內搆得到。 |
| **Open WebUI** | 聊天介面、內建 RAG、原生支援 MCP。 |
| **mcp-test-server** | 透過 MCP 做工具呼叫。屬於第二階段。 |
| **cloudflared** | *選用*，預設關閉。**對外**建立的 tunnel —— 不開任何入站埠。 |

**只有一個埠**被送出來 —— `3000`；其他每一個服務都留在 Docker 網路裡。
資料是你的、模型是你的，帳單也是你的 —— 而帳單正是型錄不會寫的那一段。

---

## 現況

| 階段 | 狀態 |
|---|---|
| **第一階段** —— 本機堆疊 | ✅ **可運作。** 端到端驗證過。 |
| **第二階段** —— RAG 與 MCP | ✅ **可運作。** 應用程式自己的程式路徑與 HTTP 路徑**兩條**都驗證過。 |
| **第三階段** —— agent 執行環境 | 📐 **量過了，但沒有實作。** |
| **第四階段** —— 持久化儲存 | 📄 **只是一份提案** —— [`PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html)。 |
| **GPU 端點** | ⚠️ **可用 —— 但有一步要人工。** |

**「量過了，但沒有實作」在這裡是精確的說法。** 截至 2026-09-21，第三階段六個
未知數都已量測，其中**有一項的前提沒有通過量測**。一項通過從來只代表
「試下一項不再是白費力氣」。

數字盡可能都是量到的。沒有量到的，明白標示為推估。
工程紀錄見 [`DECISIONS.md`](DECISIONS.md)。

---

## 架構

第一階段，也就是這個 repo 實作出來的部分：

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
│   │   (not published)         │        │     │
│   └───────────────────────────┼───────┘     │
│                               │             │
│                      port forward :3000     │
└───────────────────────────────┼─────────────┘
                                ▼
                        Browser (Open WebUI)
```

而在那道邊界之外，選用：

```
                        Browser
                            │
                            ▼
             outbound tunnel (no inbound port)
                            │
                            ▼
                       GPU Endpoint
                       Larger models
```

### 設計原則

* **預設私有** —— Ollama 從不直接對外發布。
* **由內而外** —— 對外存取走 tunnel，而不是開入站埠。
* **證據優先於假設** —— 能量的就記下來，不用估的。
* **逐步放大** —— 先驗證架構，再為更多基礎設施付錢。

---

## 快速開始

### 1. Codespaces —— 從這裡開始

從 `main` 建立 Codespace，選 **2 cores / 8 GB RAM**。
**不要選 4-core —— 那會讓額度消耗變成四倍。**

devcontainer 會自動把堆疊起起來，包含下載模型。
打開埠 **3000**、註冊第一個帳號（**它會自動變成管理者**），
然後把註冊鎖起來：

```bash
bash scripts/lock-signup.sh
```

預設模型是 **Qwen3 4B**（2.5 GB、256K context、支援 tool calling）。

> **Codespaces 是沙盒，這是量出來的，不是偏好。** 免費額度大約買到**一天 2
> 小時**；24/7 跑下去整個月會在**約 2.5 天**內用完，而且**只要 codespace 還
> 存在**（就算停掉）儲存空間照樣計費。用它驗證，然後刪掉它
> （[D-001](DECISIONS.md)）。

### 2. 在本機

需要 Docker 與 Compose v2，**至少 8 GB 記憶體**。4 GB 的機器請在 `.env` 設
`OLLAMA_MODEL=qwen3:1.7b`。

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

打開 <http://localhost:3000>。

### 3. 在自己的 VPS 上

Docker 與 Compose v2 **必須已經裝好** —— `deploy-vps.sh` 會檢查、講出它找到的
是三種原因裡的哪一種，然後停下來。**它不會替你安裝。**

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/deploy-vps.sh --model qwen3:8b --num-ctx 16384
```

**機器上有 GPU 的話，它不吃旗標** —— 唯一的控制點是 `.env` 的 `OLLAMA_GPU`
（`auto`／`on`／`off`）：

```bash
printf 'OLLAMA_GPU=on\n' >> .env
bash scripts/deploy-vps.sh --model qwen3:14b --num-ctx 16384
```

腳本會掛上 `docker-compose.gpu.yml`，然後確認模型**真的**跑在 GPU 上 ——
**要拿到結束碼 `0` 就得通過這一關。**

> ⚠️ **那個 overlay 從沒在真的、租來的 GPU 硬體上跑過。** 第一次就當它是實驗。
> [硬體規劃](docs/HANDBOOK.zh-TW.md#硬體規劃)說該租哪一台。

還沒有真的租過任何一台 VPS，所以特定方案的 token/s 數字，是從量到的校準點
**推估**出來的，並且標明是推估（[D-047](DECISIONS.md)）。
佈署腳本本身**是**端到端驗證過的（[D-028](DECISIONS.md)）。

### 4. GPU 端點

第二個、更大的模型，可以在 Kaggle 這類 GPU 執行環境上開起來。這條流程能走到
一個健康、正在跑的端點，也能串流真的 token。剩下的限制是
**自動讀回 tunnel 網址** —— Kaggle 的 API 在 session 跑動中只回一份**空的**
kernel log，那是 **Kaggle 的性質，不是 GPU 的**。

**目前網址是由人從 notebook 的輸出讀出來**，交給連線腳本：

```bash
uv tool install endpoint-vps   # 套件叫 endpoint-vps，執行檔叫 endpoint
endpoint init                  # 互動式：使用者名稱、kernel slug、預設模型
endpoint -g boot               # GPU T4 ×2 —— 然後從 notebook 讀出網址
```

> **兩件事沒注意到的話會賠掉一晚。** `-g` 要在 `boot` **前面**。而 `scripts/`
> 裡那些 notebook 產生器的補丁**必須在第一次 boot 之前、而且照順序**套完 ——
> Kaggle 帳號也要先過手機驗證。[實作手冊](docs/HANDBOOK.zh-TW.md#kaggle-實作手冊)
> 兩件都寫了。

⚠️ **這條路上 `boot` 現在回結束碼 `2`，而不是宣稱成功。** 那是刻意的，不是
故障：引擎是活的，而 endpoint 從沒被接線，因為網址讀不回來
（[D-073](DECISIONS.md)）。流程見 [`ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md)；
三個提案選項裡為什麼有兩個被劃掉，見 [D-075](DECISIONS.md)。

---

## 證據

這個 repo 把主張背後的原始證據留著。

```text
docs/
├── evidence/                  # 逐字的探針輸出，有日期
├── ENDPOINT.md                # GPU 端點這條線
├── HANDBOOK.md                # 佈署與操作手冊
├── ENDPOINT-VERIFIER-ROT.md   # 已知的鏽：紅的驗證器，以及為什麼
└── PHASE4-STORAGE-PROPOSAL.html
```

每一份文件都有英文與中文兩版。**改一份就要改另一份。**
這個專案刻意區分這五種說法：

* **已量測** —— 在跑動的系統上觀察到的
* **已驗證** —— 走應用程式路徑確認過的
* **推估** —— 預期如此，但還沒有量
* **提案** —— 只有設計
* **已知限制** —— 已理解、並記載下來的限制或失效

---

## 關鍵工程決策

**[`DECISIONS.md`](DECISIONS.md) 裡有 76 條編號、有日期的記錄** —— 記著決定了
什麼、推翻了什麼、以及什麼會讓它改變。幾個例子：

* **[D-001](DECISIONS.md)** —— Codespaces 是量出來的沙盒，非常駐主機。
* **[D-003](DECISIONS.md)** —— Ollama 從不直接對外發布。
* **[D-047](DECISIONS.md)** —— 未租用基礎設施的效能數字是推估。
* **GPU 佈署與本機堆疊分開**，所以 endpoint 壞掉不會影響核心系統。

有兩個原則貫穿全部：

* **沒有量過的數字，就標明沒有量過。** 有幾個長年被當成事實的數字，等到有人
  真的去查才發現沒有出處；那些更正被留著，不是悄悄修掉。
* **一份寫著「這裡有鏽」的文件是在盡它的職責。** 已知並記載的限制 ——
  紅的驗證器也在裡面 —— 收集在
  [`ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md)。

---

## 已知限制

* **Kaggle 那條 endpoint 流程仍然需要一步人工讀 tunnel 網址**，
  而且在該路上 `boot` 會回結束碼 `2`。
* **GPU 的 compose overlay 沒有在真的、租來的 GPU 硬體上驗證過。**
* **VPS 的效能數字仍是推估** —— 還沒有租過任何一台 VPS。
* **LangGraph agent 執行環境量過了，但沒有實作。**
* **持久化儲存仍然只是一份提案。**

---

## 文件地圖

| 你在找什麼 | 去哪裡 |
|---|---|
| 怎麼跑 —— 每個指令、每道閘門、每個坑 | [`docs/HANDBOOK.zh-TW.md`](docs/HANDBOOK.zh-TW.md) |
| 為什麼事情是現在這個樣子 | [`DECISIONS.md`](DECISIONS.md) |
| GPU 端點這條線 | [`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md) |
| 已知的鏽，記下來而不是藏起來 | [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md) |
| 主張背後的原始證據 | [`docs/evidence/`](docs/evidence/) |

**手冊是這份文件的長版** —— 同樣的材料，全部攤開來寫，包括還沒收掉的部分。

---

## 授權

本專案自己的程式碼 —— compose 檔、腳本、文件 —— 採 **[MIT 授權](LICENSE)**。

**這不涵蓋它拉下來的容器映像檔。** Ollama 與 mcpo 是 MIT；Qwen3 模型是
Apache-2.0；**Open WebUI *不是* MIT** —— 它是 BSD-3 風格**外加一條品牌條款**，
對一人用的 POC 無關緊要，但哪天你用它服務超過 50 個人就會有關。
完整對照表與條款本身在[手冊裡](docs/HANDBOOK.zh-TW.md#授權)。

---

> **少蓋一點。多量一點。把證據留著。**
