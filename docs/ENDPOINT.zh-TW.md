# 接上 GPU Runtime（Kaggle + Endpoint）

這份文件說明如何把**第二個 LLM Runtime**——跑在別人 GPU 上的較大模型——
接上平台，而且不改上層任何一行程式碼。

English: [`ENDPOINT.md`](ENDPOINT.md)

---

## 為什麼需要這份文件

整個架構的價值，取決於這句話是否成立：

> 上層只說 OpenAI 協定，不知道底下是哪一個 runtime。

這句話在沒有被機械檢查之前一文不值，所以它被檢查了——用
`scripts/probe-openai.py`，它**只說 OpenAI 協定**、不使用任何廠商 SDK。
任何能通過它的 runtime 都能接手這個平台；任何需要為它改上層程式碼的都不能。

**而且已經有一個 runtime 通過了。** Ollama 本身就提供 OpenAI-compatible
API（`/v1`），所以整條路徑今天就能走完，不需要 Kaggle 帳號：

```
$ bash scripts/probe-openai.sh
  ✓ GET /v1/models               通過  4 個模型
  ✓ POST /v1/chat/completions    通過  18.3s，回應 好
  ✓ 串流（SSE）                   通過  3 個 chunk，收到 [DONE]
  結論：通過 —— 只換 --base-url，不改任何上層程式碼。
```

**Kaggle 並不特別。它只是一個 `--base-url`。**

## 還沒驗證的部分

明列出來，因為整個專案都建立在「不要把『介面可以』誤認成『跑得動』」之上：

| 主張 | 狀態 |
|---|---|
| 通過探針的 runtime 能接手上層 | **已驗證**（對 Ollama `/v1`） |
| Kaggle + Endpoint 能跑較大的 GGUF | **未驗證** —— 需要你的 Kaggle 帳號 |
| 速度、可用 VRAM、2×T4 裝得下多大的模型 | **未量測** |
| tunnel 能否撐過一整個 session | **未量測** |

介面相容不等於承載能力。探針刻意不宣稱後者——它自己的輸出就寫明了這件事。

## 使用條款：Kaggle 是驗證工具，不是產品環境

**這一節原本不存在，是個真空 —— 而它決定的正是「這條路能不能拿去給公司用」。**

Kaggle 的[使用條款](https://www.kaggle.com/terms)把服務限定為**個人、
非商業**用途。官方 Q&A 引述的原文是：

> You will only use the Services for your own internal, personal,
> non-commercial use, and not on behalf of or for the benefit of any
> third party.

本專案的長期目標是「讓小公司建內部助理」——那是商業用途，兩者不相容。
所以要把兩種用途分開看：

| 用途 | Kaggle 適不適合 |
|---|---|
| 驗證「大模型跑得動」、量速度與可用 VRAM | **適合** —— 這正是本文件的目的 |
| 產品路徑：公司內部助理的執行環境 | **不適合** —— 條款限定個人、非商業 |

產品路徑的終點是 VPS + GPU + vLLM（見最後一節）。**Kaggle 是抵達之前的
一次量測，不是那條路的一部分。**

**這一段的可信度界線**（本專案的規矩：說法要附證據，也要附它的邊界）：

- 上面的引文來自 **Kaggle 官方 Q&A 對條款的引述**，2026-09-19 查閱。
- **條款本文沒有直接讀到。** `kaggle.com/terms` 是 JS 渲染的頁面，抓取只
  回傳標題、沒有條文內文 —— 所以這是**轉引**，不是與原文核對。
- **Acceptable Use Policy（`kaggle.com/aup`）的本文同樣沒有取得**，所以
  這裡**不能**宣稱它對「把 notebook 當伺服器」有什麼具體規定。
- **本節不是法律意見。** 要拿去商用之前，請自己讀過條款，或問法務。

---

## 前置條件

- **通過手機驗證**的 Kaggle 帳號（API 與 GPU／Internet 存取都需要）
- 執行 CLI 的機器上有 Python 3.12+
- `endpoint` CLI：

```bash
uv tool install endpoint-vps   # 之後直接打 endpoint boot
# 套件叫 endpoint-vps，執行檔叫 endpoint —— `uvx endpoint-vps` 會失敗
# （訊息是 "not provided by package"）
# 不想安裝的一次性用法：uvx --from endpoint-vps endpoint boot
# 或 pip install endpoint-vps / pipx install endpoint-vps
```

## 步驟 1 —— Kaggle 權杖

到 <https://www.kaggle.com/settings> 產生**新的**權杖，開頭是 `kgat_`。
舊式的 username/key 組合已不再被接受。

```bash
export KAGGLE_API_TOKEN='kgat_xxxxxxxxxxxxxxxx'
```

把那行放進 shell 的 rc 檔讓它存活，或改寫 `~/.kaggle/kaggle.json`。

> **這個權杖是一份憑證。** 它不屬於本專案，也絕不該進版控。本 repo 的
> `.gitignore` 涵蓋 `.env`，但涵蓋不到你的 shell rc 檔。貼到任何地方之前
> 先想一想。

接著跑設定精靈：

```bash
endpoint init
```

它會寫出 `~/.config/endpoint/endpoint-config.yaml`，並詢問你的 Kaggle
使用者名稱、kernel slug 與預設模型。

## 步驟 2 —— 啟動

```bash
endpoint -g boot        # T4 x2
```

其他目標：`endpoint boot`（CPU）、`endpoint -t boot`（TPU v5e-8）、
`endpoint boot --p100`。

**`init` 與 `boot` 都是互動式的。** `boot` 的 argparse 只有 `--no-watch`、
`--p100`、`--community` —— **沒有 `--model`**，模型是當場問的。這兩步都
不能接進排程、CI 或任何非互動的工具裡。`--no-watch` 是「不要串流狀態」，
**不是**「不要問問題」。

之後可用：

| 指令 | 做什麼 |
|---|---|
| `endpoint status` | 目前那台的狀態、tunnel 網址、已佈署的模型 |
| `endpoint base-url` | 只取網址，附可直接用的 curl 範例 |
| `endpoint doctor` | 系統診斷 —— 先確認設定真的被讀到了 |
| `endpoint models` / `upload` / `settings` | 列出或上傳模型；看或改引擎參數 |
| `endpoint logs` / `watch` | 引擎日誌（SSE），或狀態訊號串流 |
| `endpoint stop` | 停掉這一台 |
| `endpoint kill-all` | **終止帳號上所有在跑的 Kaggle kernel** |

> `kill-all` 的範圍是**整個帳號**，不是這一台。這既是它有用的原因——例如
> 你另外手動開了一台 notebook 正在默默吃 GPU 額度——也是它是一把掃射的槍、
> 而不是一把步槍的原因。

**不要把額度或硬體寫死進任何東西。** Kaggle 會改這些政策（P100 已於
2026-09-15 退役），而且每個帳號不同。`endpoint` 回報什麼，那個才是真的。

## 步驟 3 —— 取得網址與金鑰

```bash
endpoint base-url
```

你要兩樣東西：一個 `https://xxxx.trycloudflare.com` 結尾是 `/v1` 的網址，
以及 API 金鑰。CLI 會自動設定 `ENDPOINT_API_KEY`；金鑰也可以用
`GET /v1/apikey` 取得。

把金鑰放到本 repo 腳本會讀的地方——**`.env`，它已被 gitignore**：

```bash
ENDPOINT_API_KEY=<貼在這裡>
```

## 步驟 4 —— 接線**之前**先驗證

```bash
bash scripts/connect-endpoint.sh --url https://xxxx.trycloudflare.com/v1 --check
```

`--check` 只跑相容性探針，**不做任何變更**。這個順序就是重點：先接再測的話，
失敗時你已經把平台唯一的模型來源指向一個壞掉的服務了——而 Open WebUI
不會因此抗議，它只會顯示一個空的模型清單。

## 步驟 5 —— 接上

```bash
bash scripts/connect-endpoint.sh --url https://xxxx.trycloudflare.com/v1
```

腳本會：

1. 跑相容性探針，**未通過就拒絕繼續**
2. 把 `openai.enable` / `openai.api_base_urls` / `openai.api_keys` 寫進
   **資料庫**——不是寫進 `.env`，那在第一次開機之後就是無效的（D-017）
3. 重啟 Open WebUI
4. 用**應用程式自己的 `Config.get_many` 路徑**讀回設定——那是
   `get_all_models` 用的同一條路徑——而不是回頭讀自己剛寫的那一列，
   那樣只是自我證明

搭配指令：

```bash
bash scripts/connect-endpoint.sh --status       # 目前接的是什麼
bash scripts/connect-endpoint.sh --disconnect   # 切回只有 Ollama
```

## 步驟 6 —— 只有你能做的那一步

**打開 <http://localhost:3000>，看模型選單。**

這不是可選的，腳本也無法代勞。Open WebUI 對設定的端點是**延遲抓取**——
只有在已登入的使用者打開模型清單時才會發出請求。2026-09-19 用誘餌服務
驗證過：重啟後 `openai.enable=true` 且指向誘餌，**在有人要求模型清單之前，
一個請求都沒有到達**。

所以腳本證明得了「應用程式讀到的設定指向你的 runtime」，證明不了
「模型真的抓到了」。那只有你親眼看到才算。

若模型沒有出現，代表端點可達但抓取失敗——看
`docker compose logs --tail=50 open-webui`。

---

## 安全性：tunnel 是最弱的一環

以上談的是能力。這一節談的是你接受了什麼。

`endpoint` 透過 **cloudflared Quick Tunnel**（`*.trycloudflare.com`）暴露
引擎。那不是具名 tunnel，而且**沒有任何 Cloudflare Access 政策**。
存在的保護只有 API 金鑰。

這與本專案自己對外連線的規則**直接衝突**：[D-012](../DECISIONS.md) 說對外
存取要走 Cloudflare Tunnel **+ Access**，絕不是一個裸的公開端點。Ollama
是刻意不對外暴露的（[D-003](../DECISIONS.md)）。GPU runtime 剛好相反：
它**就是**公開的，這是它的構造方式。

具體來說：

- **任何知道 tunnel 網址與金鑰的人都能用你的 GPU。** 網址是隨機的，
  那是隱蔽性，不是認證。
- **Quick Tunnel 的網址每次啟動都會變。** 不要在任何東西上寫死它。
- **確認 `GET /v1/apikey` 是否能在沒有權杖的情況下存取。** endpoint 的文件
  提到這條路由。若它不需認證，那麼光是 tunnel 網址就足以取得金鑰——
  啟動後請自己用 `curl` 驗一次。**本專案尚未驗證這件事。**
- Kaggle notebook 有**單次 session 上限**（撰寫時約 12 小時）與每週 GPU
  額度。runtime 會消失。請據此設計：把它當成一個會來來去去的加速器，
  而不是唯一的模型來源。
- `endpoint` 有提供選用的 Cloudflare Worker proxy，具備 per-IP 限流與
  雜湊化的金鑰。那比裸 tunnel 實在好得多。要讓敏感資料經過之前，先考慮它。

**實務原則：** POC 階段、平台上還沒有私有資料時，Quick Tunnel 是可以接受的
交換。但在你上傳公司文件之前，either 把 GPU 搬進自己家裡（見下方 VPS + GPU
+ vLLM），either 在它前面放一個具名 tunnel 加 Access。

---

## 探針**不**量測什麼

探針回答「上層能不能對這個 runtime 運作」。它**不**回答「夠不夠快／夠不夠大」。
那些請自己量：

| 問題 | 怎麼量 |
|---|---|
| 每秒 token 數 | 計時一次真實生成，不是一個字的回答 |
| 更大的 GGUF 裝得下嗎 | 試了才知道——2×T4 各 16GB VRAM，而 tensor split 會改變結果 |
| 能不能撐過長時間 session | 讓它跑幾個小時 |
| 實際可用的 context 長度 | `LLM_CONTEXT_LEN` 對上 KV cache 的實際成本 |

模型本身是一個變數，不是架構的一部分。改它——`.env` 的 `OLLAMA_MODEL` 給腳本用，
Open WebUI 的模型選單給對話用——
不是程式碼變更。

---

## 之後換到 VPS + GPU

這就是整件事的目的。上層完全不動：

```bash
bash scripts/connect-endpoint.sh --disconnect
bash scripts/connect-endpoint.sh --url http://your-vps:8000/v1
```

路上有兩件事要弄對：

- **把 runtime 綁在私有網路上，不要綁 `0.0.0.0`。** 在有公開 IP 的 VPS 上，
  綁 `0.0.0.0` 的推論服務就是對全世界開放——而且它會**繞過 Cloudflare
  Access**，跟 3000 埠是同一個形狀。用 `scripts/check-exposure.sh` 檢查。
- **除非你真的要搬，否則嵌入引擎留在原處。** 換掉嵌入模型會讓既有向量
  全部失效（[D-013](../DECISIONS.md)）。聊天可以搬到 GPU，嵌入留著不動。
  探針把嵌入分成獨立一項，正是為了這個原因。

## 疑難排解

| 症狀 | 可能原因 |
|---|---|
| 探針回傳 2 | 端點不可達——tunnel 斷了、notebook 停了，或網址過期。這**不是**「runtime 壞掉」。 |
| 探針回傳 1 | 端點有回應，但形狀不是 OpenAI。看是哪一項沒過。 |
| `connect-endpoint.sh` 說「不做任何變更」 | 這是刻意設計。先把沒過的項目修好。 |
| UI 裡沒有模型 | 設定對了但抓取失敗。看 open-webui 日誌。 |
| 本來正常，約 12 小時後停了 | Kaggle session 上限。重啟 endpoint 再重接。 |
| `check-egress.sh` 說有啟用中的外部端點 | 接上之後這是預期的——接上就是這個意思。確認那是你自己的。 |

接上之後，`bash scripts/check-egress.sh` 會把你的 runtime 列在「啟用中」。
那是正確且預期的。這支探針存在的目的，是讓那成為一個**決定**，而不是意外。
