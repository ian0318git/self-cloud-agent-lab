# self-cloud-agent-lab

[English](README.md) | **繁體中文**

一套跑在**免費雲端額度之內**的自架 AI 平台 —— 你自己的模型、你自己的資料、
走 MCP 的工具，以及一個能自主工作的 agent。

這個 repo 存在的目的，是誠實回答一個問題：**這個架構站得住嗎，而模型夠聰明嗎？**
這裡的每一句主張，不是量過的，就是明白標示「沒有量過」。主張失敗的地方，失敗被留下來。

> **這是一個驗證沙箱，不是常駐服務。**
> Codespaces 免費額度大約買到**一天 2 小時**。24/7 跑下去，整個月的額度會在
> **大約 2.5 天**內用完 —— 而且**只要 codespace 還存在**（就算停掉）儲存空間照樣計費。
> 算式在[手冊裡](docs/HANDBOOK.zh-TW.md#為什麼不是常駐服務)。

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

而且**推論一次都沒有跑過。** 這個缺口還沒解；誠實的幾個選項、以及其中兩個為什麼被
劃掉，記在 [D-073](DECISIONS.md) 與 [D-075](DECISIONS.md)。那條路現在回**結束碼 2**，
而不是宣稱成功。

**其他東西都不依賴它。** Codespaces 堆疊、第一到第三階段、以及 OpenAI 協定探針，
沒有它照樣運作。

---

## 為什麼要做這個

最初的問題是：一套完全自架的 AI 平台 —— 自己的模型、自己的文件、自己的工具 ——
能不能只靠免費額度跑起來。答案後來的形狀是「**架構可以，常駐不行**」，
而兩半都寫下來了，沒有四捨五入掉。

專案的定位是**驗證沙箱**：一個用來確認設計站不站得住、然後就把它刪掉的地方。
這個定位是 [D-001](DECISIONS.md) —— 決策記錄裡的第一條，也是決定其他每一條的那一條。

這裡有兩個貫穿全部的原則：

- **沒有量過的數字，就標明沒有量過。** 有幾個長年被當成事實的數字，等到有人真的去查
  才發現沒有出處；那些更正被留著，不是悄悄修掉。
- **一份寫著「這裡有鏽」的文件是在盡它的職責。** 已知並記載的限制收集在
  [`docs/ENDPOINT-VERIFIER-ROT.md`](docs/ENDPOINT-VERIFIER-ROT.md)，紅的驗證器也在裡面。

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
