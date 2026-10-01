# self-cloud-agent-lab

[English](README.md) | **繁體中文**

一套私有 AI 堆疊 —— 你自己的模型、你自己的文件、你自己的工具 —— 跑在
**你自己的邊界之內**，從免費沙盒一路擴到租來的 GPU。

## 為什麼需要這個專案

把公司的合約貼進 ChatGPT，你就已經把它交出去了。那改成在自己的機器上跑？
你得先有硬體 —— 一個 7–8B 的模型就要**約 8 GB 的 VRAM** 才肯開口。

這兩條路之間的落差不是技術問題，是**沒有人在你投入之前，告訴你自架到底要付多少**
—— 多少錢、每秒多少 token、要花幾個小時。產品頁寫「production-ready」，不會寫
「CPU VPS 上 5 token/s」—— 那比你打字還慢。

所以這個 repo 把它量出來。這裡的每一句主張，不是量過的，就是明白標示「沒有量過」；
失敗的那些，留在原地。

## 這是什麼

一套完整的私有堆疊 —— **Ollama** 推論、**Open WebUI** 對話與 RAG、**MCP** 工具
—— 背後的網路設計讓**沒有任何東西暴露在外**：Ollama 只在 Docker 網路內搆得到
（[D-003](DECISIONS.md)），對外存取走**由內而外**建立的隧道，不開任何入站埠。

資料是你的、模型是你的，帳單也是你的 —— 而帳單正是型錄不會寫的那一段，
所以下面每一步都附上量到的價格。

## 這條路

三步，從免費走到有能力。第三步停住了，那也是量出來的。

| | 階段 | 買到什麼 | 狀態 |
|---|---|---|---|
| **1** | **免費沙盒** —— GitHub Codespaces | 用 $0 證明架構可行 | ✅ 可運作 |
| **2** | **自己的 VPS** —— `deploy-vps.sh` | 一個真的是你的、而且真的常駐的實例 | ✅ 腳本端到端驗證過 |
| **3** | **GPU** —— 第二個、更大的模型 | 14B–70B 跑 20–60 token/s（**推估**），而不是 7B 的 5–10 | ⚠️ 端到端不可用 |

**第一步免費，而它是沙盒是量出來的，不是偏好。** 免費額度大約買到**一天 2 小時**；
24/7 跑下去整個月會在**約 2.5 天**內用完，而且**只要 codespace 還存在**（就算停掉）
儲存空間照樣計費。用它驗證，然後刪掉它（[D-001](DECISIONS.md)）。

**第二步是完成的那一步。** 佈署腳本已經端到端驗證過。**沒有**驗證過的是機器本身：
還沒有真的租過任何一台 VPS，所以特定方案的 token/s 數字，是從量到的校準點**推估**
出來的，並且標明是推估（[D-028](DECISIONS.md)、[D-047](DECISIONS.md)）。

**第三步是今天停住的地方**，而且兩條路都停 —— GPU 的 compose overlay 從沒在真的
GPU 硬體上跑過；Kaggle 那條則讀不回自己的 tunnel 網址，**那是 Kaggle 的性質，
不是 GPU 的**（自己的 VPS 用固定主機名，沒有這一步）。兩者都寫在下面。

如果你是來判斷「自架到底值不值得」的：第一步不用錢，而它會回答你。
如果你是來佈署的：第二步已經準備好了。

---

## 現況

| 階段 | 狀態 |
|---|---|
| **第一階段** —— 本機堆疊：Codespaces 上的 Ollama + Open WebUI | ✅ **可運作。** 端到端驗證過。 |
| **第二階段** —— RAG 與 MCP 工具 | ✅ **可運作。** RAG 那四項在應用程式自己的程式路徑與 HTTP 路徑**兩條**都通過；MCP 工具呼叫已驗證。 |
| **第三階段** —— agent 執行環境（LangGraph） | 📐 **量過了，但沒有實作。** 截至 2026-09-21，六個未知數都已量測，其中**有一項的前提沒有通過量測**。一項通過從來只代表「試下一項不再是白費力氣」。 |
| **第四階段** —— 持久化儲存 | 📄 **一份提案**，不是程式碼。[`docs/PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html) |
| **`endpoint`** —— 在租來的 GPU 上跑第二個、更大的模型 | ⚠️ **端到端不可用。** 見下。 |

### `endpoint` 這條線目前不會動

[`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md) 講的是在不改動上層任何一行的前提下，
接上**第二個 LLM 執行環境**——跑在別人的 GPU 上。引擎本身起得來：2026-09-30 那次
真的 boot 走到

```
→ ENGINE HEALTHY → Endpoint IS ONLINE
```

**但 boot 讀不回 tunnel 的網址**，而它下游的每一件事都掛在那個網址上 —— 就緒等待、
註冊、印出端點。同一次 boot 量到的結果是

```
→ AVAILABLE MODELS: []
```

這個缺口還沒解；誠實的幾個選項、以及其中兩個為什麼被劃掉，記在
[D-073](DECISIONS.md) 與 [D-075](DECISIONS.md)。那條路現在回**結束碼 2**，
而不是宣稱成功。

**其他東西都不依賴它。** Codespaces 堆疊、第一到第三階段、以及 OpenAI 協定探針，
沒有它照樣運作。

---

## 怎麼讀這個 repo

這裡有兩個貫穿全部的原則：

- **沒有量過的數字，就標明沒有量過。** 有幾個長年被當成事實的數字，等到有人真的去查
  才發現沒有出處；那些更正被留著，不是悄悄修掉。
- **一份寫著「這裡有鏽」的文件是在盡它的職責。** 已知並記載的限制收集在
  [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md)，紅的驗證器也在裡面。

每一個設計選擇，都在 **[`DECISIONS.md`](DECISIONS.md)** 有一條編號、有日期的記錄 ——
記著決定了什麼、推翻了什麼、以及什麼會讓它改變。這份 README 說「量過」的時候，
量測就在那裡。

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

| 元件 | 角色 |
|---|---|
| **Ollama** | 本機推論引擎。**不對外發布** —— 只有 `ai-net` 裡面搆得到（[D-003](DECISIONS.md)）。 |
| **Qwen3 4B** | 預設模型：2.5 GB、256K context、支援 tool calling。 |
| **Open WebUI** | 聊天介面、內建 RAG、原生支援 MCP。 |
| **cloudflared** | *選用*，預設關閉。**對外**建立的 tunnel —— 不開任何入站埠。見[對外連線的安全性](docs/HANDBOOK.zh-TW.md#對外連線的安全性)。 |
| **mcp-test-server** | 跟著 `docker compose up -d` 一起起來；它沒有宣告 profile，所以不像 `cloudflared` 那樣是選用的。屬於第二階段。 |

**只有一個埠**被送出來 —— `3000`。三個容器，一道門。

---

## 快速開始

### 在 Codespaces 上

1. **Code → Codespaces → Create codespace on main**
2. 選 **2-core / 8GB** 的機器。*不要選 4-core —— 那會讓額度消耗變成四倍。*
3. 等它建好。`postCreateCommand` 會自動跑 `scripts/up.sh`，包含下載模型。
4. **PORTS** 面板 → 打開埠 **3000** 的網址。
5. 註冊第一個帳號 —— **它會自動變成管理者。**
6. 在做任何事之前先把註冊鎖起來：
   ```bash
   bash scripts/lock-signup.sh
   ```

### 在本機

需要 Docker 與 Compose v2，**至少 8 GB 記憶體**（4 GB 的機器請在 `.env` 設
`OLLAMA_MODEL=qwen3:1.7b`）。

```bash
git clone https://github.com/ian0318git/self-cloud-agent-lab.git
cd self-cloud-agent-lab
bash scripts/up.sh
```

然後打開 <http://localhost:3000>。

> **要佈署到一台全新的 VPS？** `bash scripts/deploy-vps.sh --model M --num-ctx N`
> 一個指令做完。它**要求 Docker 與 Compose v2 已經裝好並在跑** —— 它會檢查、
> 講出它找到的是三種原因裡的哪一種，然後停下來。它不會替你安裝。
> 選機器看[搬到 VPS](docs/HANDBOOK.zh-TW.md#搬到-vps)，
> 規格看[硬體規劃](docs/HANDBOOK.zh-TW.md#硬體規劃)。

---

## 疑難排解

| 症狀 | 原因與處理 |
|---|---|
| 啟動失敗並顯示 **`WEBUI_SECRET_KEY 未設定`** | 這是刻意的，不是 bug。`bash scripts/up.sh` 會自動產生金鑰。**不要**為了繞過去而留空 —— 那會讓第二階段的 MCP 工具在每次容器重建後以 `Error decrypting tokens` 失敗。 |
| **模型下載中斷** | `bash scripts/pull-model.sh`。Ollama 宣稱可以續傳，**本專案沒有量測過這個宣稱**。有量的是磁碟那一面：一次 pull 的峰值就是模型的最終大小，不會更多（[D-058](DECISIONS.md)）。 |
| **回應非常慢** | 2 個 vCPU 上這是預期行為 —— 個位數 token/s。換小一點的模型，或縮短 `OLLAMA_KEEP_ALIVE`。 |
| **容器一直重啟／被 OOM killer 殺掉** | 記憶體不夠。確認 `OLLAMA_MODEL` 不是 `qwen3:8b` 或更大；用 `docker stats` 看是哪個容器；縮短 `OLLAMA_KEEP_ALIVE`。 |
| **Codespaces 裡找不到埠 3000 的網址** | 埠**預設是 private**，而且要有流量才會出現在面板上。在 PORTS 面板手動加入 `3000`。 |

[手冊的疑難排解那節](docs/HANDBOOK.zh-TW.md#疑難排解)有完整版本，
還有一份很容易讓人耗掉好幾個小時的 Codespaces 坑清單。

---

## 文件地圖

| 你在找什麼 | 去哪裡 |
|---|---|
| 怎麼跑 —— 每個指令、每道閘門、每個坑 | **[`docs/HANDBOOK.zh-TW.md`](docs/HANDBOOK.zh-TW.md)** |
| *為什麼*事情是現在這個樣子 —— 76 條編號決策 | **[`DECISIONS.md`](DECISIONS.md)** |
| 第二個 GPU 執行環境 | [`docs/ENDPOINT.zh-TW.md`](docs/ENDPOINT.zh-TW.md) |
| 主張背後的逐字輸出 | [`docs/evidence/`](docs/evidence/) —— 62 份探針輸出 |
| 已知的鏽：紅的驗證器，以及為什麼 | [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md) |
| 第四階段的儲存，以提案的形式 | [`docs/PHASE4-STORAGE-PROPOSAL.html`](docs/PHASE4-STORAGE-PROPOSAL.html) |

**手冊是這份文件的長版** —— 同樣的材料，全部攤開來寫，包括還沒收掉的部分。
它有英文與中文兩份；**改一份就要改另一份。**

---

## 授權

本專案自己的程式碼 —— compose 檔、腳本、文件 —— 採 **[MIT 授權](LICENSE)**。

**這不涵蓋它拉下來的容器映像檔。** Ollama 與 mcpo 是 MIT；Qwen3 模型是 Apache-2.0；
**Open WebUI *不是* MIT** —— 它是 BSD-3 風格**外加一條品牌條款**，
對一人用的 POC 無關緊要，但哪天你用它服務超過 50 個人就會有關。
完整對照表與條款本身在[手冊裡](docs/HANDBOOK.zh-TW.md#授權)。
