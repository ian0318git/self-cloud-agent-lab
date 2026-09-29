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
| 修補後的產生器撐得過一次真的 `endpoint boot` | **未驗證** —— 補丁是對模擬的 ntfy 驗證的，不是對真的 Kaggle 跑 |
| kernel **還在跑的時候**讀不讀得到日誌 —— 新的網址讀取器實際上賴以為生的前提 | **未量測** —— 唯一一次讀取是對**已結束**的 kernel 做的（D-068） |
| 佈署的儀器在推測解碼（MTP）之下仍然誠實 | **已知衝突** —— 見下面「一個已知會讓儀器說謊的方法」，以及 D-061 |

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
| `endpoint stop` | 停掉這一台。**它會告訴你是三種情況裡的哪一種** —— 見[讀不到 kernel 時的 `endpoint stop`](#讀不到-kernel-時的-endpoint-stop) |
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

那把金鑰最後會住在**三個**地方，而只有其中一個會自我修復：

| 位置 | 它是什麼 |
|---|---|
| `~/.config/endpoint/endpoint-config.yaml` → `identity.api_key` | **真本**。每次 boot 都把它的複本烘進推上 Kaggle 的 notebook —— **明文**。 |
| `.env` → `ENDPOINT_API_KEY` | **硬拷貝**，給 `connect-endpoint.sh` 與探針讀。 |
| Open WebUI 的資料庫 → `openai.api_keys` | **硬拷貝**，與 `openai.api_base_urls` 同索引對齊。 |

只有 CLI 會從過期的值裡恢復 —— 它會重讀活著那台引擎的 `GET /v1/apikey`。
另外兩份永遠不會。它們只會安靜地過期，然後很久以後以 401 的樣子出現，
而那時沒有任何線索指回這裡。

```bash
bash scripts/rotate-endpoint-key.sh --check   # 三處還一致嗎？
bash scripts/rotate-endpoint-key.sh           # 一次換掉三處
```

`--check` 只印**指紋，絕不印金鑰**、不改任何東西，三處不一致時回 `2` ——
所以它能當閘門用。

要在 boot **之前**輪替，不是之後。輪替是本機動作：它不碰 Kaggle。舊金鑰在已經
推上去的那個 notebook 裡仍然是活的，直到下一次 boot 把它換掉 —— 而那個
notebook 雖然是私有的，它仍然是別人伺服器上的明文。

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
bash scripts/rotate-endpoint-key.sh --check     # 三個金鑰持有點一致嗎？
bash scripts/rotate-endpoint-key.sh             # 一次輪替三處
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
- **金鑰以明文長存 Kaggle 伺服器。** 每次 boot 都會把它烘進推上去的
  notebook。那個 notebook 是**私有**的，所以這不是公開外洩——但任何持有該
  帳號的人都看得到，而 CLI 自己永遠不會輪替它。唯一會輪替的是
  `scripts/rotate-endpoint-key.sh`，而且輪替要等到**下一次** boot 才生效。
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

### 一個已知會讓儀器說謊的方法

上面那張表裡不是每一項都只是「還沒輪到」。有一個改動是**已知會弄壞儀器本身**的，
所以值得單獨點名：

**推測解碼用的 draft head**（現在的例子是 Gemma 4 的 MTP drafter）。ollama 的
`DRAFT` 指令會在目標模型旁邊多載一個小模型，而
[有 drafter 時 `/api/ps` 會低估記憶體](https://github.com/ollama/ollama/issues/17951)
—— 4.4 GB 的模型回報 315 MB。

這件事落在本專案的一個特定位置上。`scripts/deploy_smoke_probe.py` 的 GPU 判準是
一條**比例**測試（`size_vram == size`），而它印出來當證據的是**絕對**數字。
比例撐得過「數字是錯的」，所以 D-056 稱為唯一執行面證據的那一層，會繼續說
「整顆模型都在 GPU 上」，同時顯示一個不是那個模型的數字。另外兩個儀器不是答錯，
是**不再回答**：模型落在 `model_gb_estimate()` 那六格表之外時，磁碟閘門印
`unknown`（人就是那個閘門），主機記憶體警告則落到「跳過檢查」。

**本專案不使用 MTP。** 它裝得起來 —— ollama 0.34.2 支援 —— 但它拉高的是
每秒 token 數，而我們的瓶頸是 token **數量**（D-054），而且沒有 T4 的數字。
完整記錄（含還沒驗的部分）在 DECISIONS.md **D-061**。

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

## 修補 notebook 產生器

`endpoint boot` 跑的筆記本不是你能編輯的那種。它跑的是已安裝的 `endpoint-vps`
套件裡的 `master_build_notebook.py`，由它**產生**筆記本再推上去 —— 所以產出的
筆記本有缺陷時，產生器是唯一能改的地方。

兩個缺陷是從一次「健康卻死亡」的 kernel log 裡讀出來的
（[D-050](../DECISIONS.md)）：

| 缺陷 | 它做了什麼 |
|---|---|
| `signal()` 把每一行 HF 下載進度都發布出去 | 628 秒內 629 則，而額度是約 60 則、之後每 5 秒 1 則（每個 IP）。桶子見底，夾著網址的一次性 `TUNNEL ACQUIRED` 被 429 丟掉 —— 而 `signal()` 對此完全沉默。 |
| `check_kill_signals()` 用 `/raw?since=5m` 卻沒有 `poll=1` | 那是阻塞式訂閱，不是輪詢。`boot` 會在新 kernel 啟動前約 20 秒發布一則 `KILL`，所以每次開機都會重播前一台的 kill 訊號 —— 並在 32 分鐘後對著健康的引擎擊發。 |

兩者必須一起修，因為它們共用同一個額度：筆記本的同一個 IP 既發布訊號、也輪詢
kill 主題。

```bash
bash scripts/apply-endpoint-ntfy-fixes.sh --dry-run   # 只檢查，不動檔案
bash scripts/apply-endpoint-ntfy-fixes.sh             # 套用
bash scripts/apply-endpoint-ntfy-fixes.sh --revert    # 還原
```

腳本會擋下三種情況：目標雜湊不是補丁所依據的版本、原始檔竟然**通過**行為驗證
（代表已經沒有東西可修）、以及套用後雜湊不符（會自動還原）。套用前先備份，
套用後再驗一次。行為驗證是 `scripts/test_endpoint_ntfy_fixes.py`，可以指向任何
產生器：它驅動的是產生器**實際吐出的字串**，跑在模擬的 ntfy token bucket 與假
時鐘上。**它對原始檔是預期要失敗的** —— 那個失敗本身就是缺陷的展示。

**這動的是 package manager 目錄裡的檔案。** 重裝或升級 `endpoint-vps` 就會把
修正蓋掉；升級後請重跑一次。

## 讀不到 kernel 時的 `endpoint stop`

2026-09-26，`endpoint stop` 在 GPU session **還活著、還在燒額度**的時候回報
「No running kernel found.」並以 0 結束（[D-060](../DECISIONS.md)）。最後是直接
呼叫 `endpoint.core.send_kill_signal` 才停掉的。

成因不是「少了一個檢查」。`get_kernel_status()` 的**每一條**失敗路徑都回
`"offline"` —— 沒憑證、非 200、網路例外、狀態字不認得 —— 而 `run_stop` 把
`"offline"` 當成「沒有在跑」這個**事實**。也就是說，呼叫端唯一信任的那個值，
全部由「問不到」生產。這與 [D-051](../DECISIONS.md) 是同一種病：把「讀不到」
摺進「不在」，然後在摺進去的那一支上安靜地什麼都不做。

**在非互動 shell 底下這是常態，不是邊緣情況。** Kaggle 權杖住在你的 shell rc
檔裡，而非互動 shell 不會讀它。`get_kaggle_token()` 回 `None`，於是每一次都
讀不到狀態。

修補之後，結束碼會說出三種情況裡的哪一種：

| 結束碼 | 意思 |
|---|---|
| `0` | 確認停好了，或確認本來就沒有在跑。 |
| `2` | **kill 訊號送出去了，但狀態從頭到尾讀不到** —— 無法確認已終止。請在拿得到權杖的 shell 裡跑 `endpoint status`，或看 Kaggle 網頁。 |
| `1` | 確定的失敗 —— 設定不存在（沒跑過 `endpoint init`）。 |

用 `2` 而不是 `0` 或 `1`，沿用探針已有的約定（[D-018](../DECISIONS.md)）：
1 是確定的失敗、2 是**無法判定**。`endpoint stop && echo "停好了"` 不會再騙人。

狀態未知時，它刻意**不**做兩件事：

- **不呼叫 Kaggle 的取消路徑。** 那條路徑開頭就是 `kaggle kernels delete -y`，
  會連 kernel 的版本歷史一起刪掉。對一個你讀不到的狀態花掉一個不可逆的動作，
  形狀就是錯的 —— 而且它需要同一批剛剛才失敗的憑證，本來也不可能成功。kill
  訊號就夠了：notebook 收到就結束，而 2026-09-26 真的把 GPU 停下來的就是它。
- **不安靜地往好的方向假設。** 讀不到的狀態，`endpoint status` 會印黃色的
  `unknown (could not check)`，而不是把它塗成紅的、假裝確認過 `offline`。

```bash
bash scripts/apply-endpoint-stop-fixes.sh --dry-run   # 只檢查，不動任何檔案
bash scripts/apply-endpoint-stop-fixes.sh             # 套用（兩個檔案）
bash scripts/apply-endpoint-stop-fixes.sh --revert    # 還原
```

這個補丁橫跨**兩個**檔案（`endpoint/core.py` 與 `endpoint/commands.py`），所以
腳本釘四個雜湊、兩個檔案備份在同一個時間章節下、一起還原。而兩個檔案狀態**不
一致**時它會拒絕動作：一份橫跨兩檔的 diff 不可能只套上其中一半，那個狀態代表
有一次套用被中斷、或有人手動編輯過，而猜測該補哪一半，比請你先還原更危險。

**這動的是 package manager 目錄裡的檔案。** 重裝或升級 `endpoint-vps` 就會把
修正蓋掉；升級後請重跑一次。

> **這件事沒有被確立。** Kaggle 的 API 到底會不會回 `offline` 這個字，兩個方向
> 都沒有查證過 —— 它不在 SDK 的 `KernelWorkerStatus` 列舉裡，所以這個補丁不依
> 賴它。修正本身做過行為驗證（對原始檔與修補後各跑一次），也在非互動 shell 裡
> 真的跑過 `endpoint stop`；但那一跑只可能走到**讀不到**那條路，因為那是非互動
> shell 唯一有的路。要確認**活著的** kernel 真的停掉，仍然需要一次 boot，而那
> 會花掉 GPU 額度。

## 引擎明文發布出去的那把 API 金鑰

`_startup()` 裡有一行

```python
_broadcast(f"APIKEY:{_get_api_key()}")
```

而 `_broadcast` 會把 `STATUS: [<session>] <msg>` POST 到一條**由 Kaggle 使用者
名稱推導出名字的 ntfy 主題**（`endpoint/core.py`）—— 那是公開帳號。所以每一次
boot 都把自己的 API 金鑰，以明文，發布到一條**任何知道帳號名稱的人都能重算出
來**的通道上（[D-067](../DECISIONS.md)）。2026-09-28 **整條鏈實測過**，不是推論。
（主詞是過去式：切 C 之後主題名不再由帳號推導，見下面的專節。）

```bash
bash scripts/apply-endpoint-apikey-broadcast-fixes.sh --dry-run   # 只檢查，不動任何檔案
bash scripts/apply-endpoint-apikey-broadcast-fixes.sh             # 套用
bash scripts/apply-endpoint-apikey-broadcast-fixes.sh --revert    # 還原
```

這次只有一個檔案 `engine/engine.py`，而它是**生產者**：notebook 產生器讀
`REPO_ROOT/engine/engine.py`、base64 嵌進去，kernel 開機時解出來執行。所以改
安裝目錄裡的這一份，就等於改了下一次 boot 會跑的東西。補丁**刪掉那一行，不替換
成別的東西**；`STARTING...` 與 `WAITING FOR MODEL...` 照常發布，那條主題上的
生命週期訊號不變。

刪掉這則廣播**不會**卡住 boot，而這一點是**斷言出來的、不是假設的**：`run_boot`
的 600 秒等待迴圈只在 `TUNNEL ACQUIRED` 分支 `break`，`APIKEY:` 分支只
`continue`。上游哪天改成會離開迴圈，驗證器就會紅 —— 因為那時這個刪除的代價
會是一次完整的逾時。

> **這件事沒有被確立 —— 在把這條主題當成安全之前請先讀完。**
> 這一刀移除的是一個環節，不是整條鏈。`GET /v1/apikey` 仍在免認證清單裡，所以
> 任何人只要拿到 tunnel 網址，仍能用**一次不帶權杖的請求**把金鑰讀回去。這一刀
> 移除的是**唯一一個完全不需要先知道網址的環節**。要關掉整條鏈，得把網址從公開
> 主題上拿下來 —— **那件事已經做完了**（`scripts/apply-endpoint-tunnel-url-privacy.sh`，
> [D-068](../DECISIONS.md)，見下一節）—— 再讓主題的**名字**不再由公開帳號推導，
> 而**那件事也已經做完了**（`scripts/apply-endpoint-topic-secret.sh`，
> [D-069](../DECISIONS.md)，見下面第三節）。剩下的是 `GET /v1/apikey` 本身。
> 現在要拿到網址得先有 Kaggle 憑證，而要重算出主題得先有一個**從不離開這台機器**
> 的秘密 —— 那就是這兩刀全部的意義。
>
> 而且它只有離線驗證。烘驗把引擎原始碼從產生出來的 notebook 裡**解碼回來**，
> 在修補版上找到 0 則 `APIKEY:` 廣播、在原始版上找到 1 則；但**沒有真的 boot
> 過**，那會花掉 GPU 額度。這次新確立的只是：**產物裡不再含有那則廣播**。

## 網址現在從哪裡來

步驟 3 寫的是 `endpoint base-url`。這一節說那個網址現在從哪裡來，以及為什麼換了。

**它原本走的那條通道是公開的。** 開機訊號發到一條 ntfy 主題，而主題的**名字**當時是由
Kaggle 使用者名稱推導出來的（`endpoint/core.py`）—— 那是公開帳號。所以 tunnel 網址
以明文躺在一條**任何知道帳號名稱的人都能重算出來**的通道上；而拿到那個網址，就足以
用**一次不帶權杖的請求**從 `GET /v1/apikey` 把金鑰讀回去（[D-067](../DECISIONS.md)）。
這一刀把網址從那條主題上拿下來，改由 **kernel log** 提供，而讀日誌需要 **Kaggle 權杖**。
主題的**名字**在這一刀之後仍是公開帳號的函式 —— 那是下一刀，見下面第三節。

它比聽起來的改動小，因為**網址本來就已經在 kernel log 裡了**：產生器的 `signal()`
是**先 print、後 POST**。補丁改的是「發布」，不是「寫入」。

```python
print(f'TUNNEL ACQUIRED: {tunnel_url}', flush=True)   # 網址 → 只進 kernel log
signal('TUNNEL ACQUIRED:', topic_only=True)           # 值為空 → 只進主題
```

主題照樣會收到一則 `TUNNEL ACQUIRED:` —— 只是冒號後面什麼都沒有。`boot` 等的就是
這個訊號、沒有別的，所以**它照樣代表成功**；差別只在內容沒了。

```bash
bash scripts/apply-endpoint-tunnel-url-privacy.sh --dry-run   # 只檢查，不動任何檔案
bash scripts/apply-endpoint-tunnel-url-privacy.sh             # 套用
bash scripts/apply-endpoint-tunnel-url-privacy.sh --revert    # 還原
```

⚠️ **順序不是可選的。** 這個補丁是對「已經套了 ntfy 修正**與** stop 修正」的樹建的
—— `apply-endpoint-ntfy-fixes.sh` 打的是**同一個** notebook 產生器，
`apply-endpoint-stop-fixes.sh` 打的是**同一組** `core.py` 與 `commands.py`。
順序是 **ntfy 修正 → stop 修正 → 本補丁**；而且對前兩支任一執行 `--revert` 會
**無聲地**把本補丁拆掉 —— 本補丁的雜湊閘門要等到**下一次**跑它才會發現。

**三個狀態，而其中兩個不是同一件事。** 讀取器回 `found`、`absent`（讀到了、日誌裡
**還沒有**網址）或 `unknown`（根本讀不到）。把 `absent` 塌進 `unknown` —— 或更糟，
塌進「沒有東西在跑」—— 正是 `endpoint stop` 有過的那個缺陷（D-060），也正是這裡
把它們分成兩個值的原因。**三種情況下 `boot` 都算成功**；`absent` 與 `unknown`
各印各的句子，而且都指向 `endpoint base-url`。

想不 boot 就對著真 API 確認任何一項：

```bash
python3 scripts/probe_kernel_log_url.py            # 0 找到了 / 1 還沒有 / 2 讀不到 / 3 用法錯誤
```

它只印狀態碼、布林與計數 —— **永不印日誌內容、網址、權杖或主題**。網址只以
**單向指紋**的形式出現，所以兩次執行可以比對而不揭露任何一次。**請把 tunnel 網址
當成憑證**：它活著的時候，任何拿著它的人都能碰到那台引擎。

> **這件事沒有被確立。** 讀取器只被指向過**已結束**的 kernel。
> `ListKernelSessionOutput` 供應的是一份持久化的 blob，對**跑動中**的 kernel 可能
> 行為不同 —— 而那正好是一次剛開完機的 kernel 所處的狀態。所以「boot 之後能自己
> 拿到網址」是**沒有量過的**；退路與以前一樣，就是幾分鐘後再 `endpoint base-url`。
> 第二個缺口：唯一那份樣本的日誌**沒有**被砍頭，但若 Kaggle 哪天改成只回尾部，
> 網址（在那份樣本中位於 76.5%）可能會掉出窗外 —— 探針**刻意拒絕用猜的**，因為
> 一個用猜格式的「有沒有被截斷」判斷，會在**它最該抓到的那個情況上**回「沒有」。
> 第三：讀日誌需要一個**非互動 shell 也找得到**的權杖，所以它該住在
> `~/.kaggle/kaggle.json`（權限 `600`，含 `username` 與 `key`），而不是 shell 的
> rc 檔。最後，這個補丁只有離線驗證 —— **沒有真的 boot 過**，那會花掉 GPU 額度。

## 主題名不再是一個公開帳號的函式

前面兩刀把主題上的**秘密**清空了。這一刀改的是**誰能碰到那條主題**
（[D-069](../DECISIONS.md)）。

主題名不是通道識別碼，它是**通行憑證**：ntfy 不做任何認證，所以知道名字**就等於**
有權讀它、也有權在上面發布。而在切 C 之前，那個名字是 `sha256(kaggle_username)[:12]`
—— 一個公開帳號的函式，任何人都能一行算出來。那買到三件事，沒有一件需要秘密：
**讀生命週期**、**偽造 `KILL`**、以及依勘查的洪水分析**延遲**真的 `KILL`。

現在名字由一個**從不離開這台機器**的秘密推導。產生器烘進 notebook 的是**推導出來的
名字**；引擎只從 `LLM_SIGNAL_TOPIC`／`LLM_CONTROL_TOPIC` 讀，**從不重算**。所以：

> **秘密的爆炸半徑是這台機器。主題的爆炸半徑是這台機器＋Kaggle＋kernel log。**

後半段要緊，也正是主題仍被當成憑證的理由：它會落在 Kaggle 上的 private notebook、
落在 `/tmp/endpoint-engine-output/endpoint_setup.ipynb`（**會留存** —— 9/28 的檔還在）、
以及 kernel log。輸出目錄現在以 `0700` 建立，就是為了這個。

**順序很重要，而且這是這裡最容易搞錯的一點。** 寫入秘密的當下**什麼都不會變** ——
還在跑的程式碼推導的仍是舊名字。真正切換主題的時刻，是補丁落地的那一刻。所以：

```bash
# 1. 先確認沒有 kernel 在跑 —— endpoint status（要 Kaggle 權杖）
bash scripts/set-endpoint-topic-secret.sh            # 秘密就位
bash scripts/apply-endpoint-topic-secret.sh --dry-run # 檢查，不做變更
bash scripts/apply-endpoint-topic-secret.sh           # 套用 —— 這就是切換的那一刻
```

反過來做的話，還在跑的 kernel 會**變聾**：它聽的是舊名字，所以沒有 `stop`、也沒有
`KILL` 到得了它，而它會繼續燒 GPU 配額。**60 分鐘閒置逾時不是復原路徑** —— 它殺的是
引擎行程，不是 notebook。復原只有 `endpoint kill-all --yes`（它**從不建構 `Config`**，
所以不受秘密影響）或 Kaggle UI。

**沒有形狀正確的秘密時，腳本拒絕執行，而且這件事沒有 `--force`。** 那個閘門正是讓
「補丁已套但沒有秘密」這個狀態不可達的東西，因為那是個 P0：`boot` 會**先殺掉健康的
kernel、再失敗**。所有碰到主題的路徑都改成**大聲失敗**，而不是安靜地退回舊推導。

**一個不會被備份的秘密。** 設定腳本不建備份，就地覆寫。推導是單向的，所以舊名字也
推不回來 —— 而它**不需要**推回來，因為它本來就是公開帳號的函式。代價是
**沒有 Kaggle 憑證時就沒有無憑證的復原路徑**；另一個出口是 Kaggle UI。

**一個 lab 一個秘密。** 推導裡的領域分隔字串不綁前綴也不綁帳號，所以同一個秘密用在
兩個 lab 會導出同一條主題。而且這個推導是**雜湊、不是 KDF**：知道主題名的人握有一個
**離線驗證器**，可以用雜湊速度測試候選秘密。這就是形狀被強制成 32 個十六進位字元
（128 bits）而不是憑感覺的理由 —— 它是**熵的代理量，不是熵的測量**。

**只有離線驗證。** 烘驗真的產生一份 notebook，斷言推導出的主題在裡面、canary 秘密
不在；十三道檢查的驗證器餵假物件驅動兩份推導；突變台證明每一道判準都有牙齒。
**沒有真的 boot 過** —— 那會花掉 GPU 額度 —— 所以「新主題上的 `KILL` 真的到得了」
**沒有量過**。這裡沒有任何東西證明真 boot 的行為。

⚠️ 套用之後，**切 B** 的 apply 腳本的 `state_of` 會對共用的三個檔案回
`unknown:<hash>`，而它的訊息不會提到切 C。**記下來，不改它。**
而**切 C 的原始雜湊逐字等於切 B 的修補後雜湊**，所以 **B 必須先套**。

## 疑難排解

| 症狀 | 可能原因 |
|---|---|
| 探針回傳 2 | 端點不可達——tunnel 斷了、notebook 停了，或網址過期。這**不是**「runtime 壞掉」。 |
| 探針回傳 1 | 端點有回應，但形狀不是 OpenAI。看是哪一項沒過。 |
| `connect-endpoint.sh` 說「不做任何變更」 | 這是刻意設計。先把沒過的項目修好。 |
| UI 裡沒有模型 | 設定對了但抓取失敗。看 open-webui 日誌。 |
| 本來正常，約 12 小時後停了 | Kaggle session 上限。重啟 endpoint 再重接。 |
| `check-egress.sh` 說有啟用中的外部端點 | 接上之後這是預期的——接上就是這個意思。確認那是你自己的。 |
| kernel log 出現 `SHUTDOWN SIGNAL RECEIVED` 但沒人下 `stop` | 未修補的 kill 開關在重播它自己開機前約 20 秒發布的 `KILL`。套用補丁。 |
| `boot` 成功但 tunnel 網址一直沒出現 | 未修補的限流把額度花在下載進度上。用 `scripts/apply-endpoint-ntfy-fixes.sh --verify` 確認。 |
| `apply-endpoint-ntfy-fixes.sh` 說雜湊不符而拒絕 | `endpoint-vps` 升級了。先確認上游是否已修；否則針對新檔案重建補丁。 |
| `endpoint stop` 說「No running kernel found.」但 runtime 還在答話 | 未修補的狀態讀取：`get_kernel_status()` 把每一條失敗都叫成 `offline`。套用 `scripts/apply-endpoint-stop-fixes.sh`。（修補後這會變成結束碼 2 與一句明講的「無法確認」。） |
| `endpoint stop` 回傳 2 | 這是刻意設計，不是失敗。kill 訊號已送出；狀態讀不到，因為這個 shell 沒有 Kaggle 權杖。改在拿得到權杖的 shell 裡用 `endpoint status` 確認。 |
| `apply-endpoint-stop-fixes.sh` 拒絕，說兩個檔案不一致 | 上一次套用被中斷，或有人手動改過其中一個。先用 `--revert` 還原再重跑。 |
| 某次 boot 沒有出現 `API key registered with engine` | 套過 `scripts/apply-endpoint-apikey-broadcast-fixes.sh` 之後的正常現象 —— 那行訊息來自被刪掉的廣播。金鑰仍會走 HTTP 註冊；這行不見了不是故障。 |
| `apply-endpoint-apikey-broadcast-fixes.sh` 警告連結數 | `engine.py` 是由 uv 快取硬連結出來的，而 `patch` 會打斷那些連結。對產生器無害（它讀的就是你改的那一份），但重裝會把快取裡的副本還原 —— 升級後請重跑本腳本。 |
| `endpoint status` 印出 `unknown (could not check)` | 補丁正在作用：`endpoint status` 也需要權杖，而這個 shell 沒有。是黃色不是紅色 —— 讀不到的狀態不等於確認過的 `offline`。 |
| endpoint 明明是活的，`connect-endpoint.sh` 或探針卻拿到 401 | `.env` 拿著**過期的硬拷貝**——它不會自己更新。跑 `scripts/rotate-endpoint-key.sh --check`。 |
| `rotate-endpoint-key.sh --check` 回傳 2 | 這是刻意設計：三個持有點不一致，而不一致在出事之前都是無聲的。跑一次不帶 `--check` 的，讓它們一致。 |
| `rotate-endpoint-key.sh` 回傳 2，說 open-webui 沒在跑 | 這是刻意設計。只輪替讀得到的兩個，會留下第三個拿著**死金鑰、且沒有任何症狀**。把堆疊開起來再跑一次。 |
| `rotate-endpoint-key.sh` 回傳 3 | 那個值在該檔案裡不是恰好一筆 `myth-` 金鑰。腳本只印**數量、不印值**——自己看一眼那個檔案再重跑。 |
| `boot` 成功，但說網址還沒出現在 kernel log 裡 | 這是預期行為，不是故障。tunnel 起來了，日誌還沒跟上。過一分鐘用 `endpoint base-url` 取。 |
| ntfy 主題上再也沒有帶著 tunnel 網址的訊息 | 套過 `scripts/apply-endpoint-tunnel-url-privacy.sh` 之後的正常現象 —— 網址現在在 kernel log 裡，而主題上那則 `TUNNEL ACQUIRED:` 是**刻意留空值**的。用 `endpoint base-url`。 |
| `endpoint base-url` 說讀不到 kernel log | 兩句不同的訊息、兩個不同的原因：**沒有憑證**（這個 shell 看不到 Kaggle 權杖）或 **Kaggle 拒絕**。看清楚印的是哪一句。前者的解法是 `~/.kaggle/kaggle.json` —— **非互動 shell 不會讀 shell 的 rc 檔**，而 cron 與大多數腳本都跑在非互動 shell 裡。 |
| 對 ntfy 或 stop 補丁執行 `--revert` 之後，網址就不來了 | 這是預期行為 —— 那兩支補丁打的正是本補丁的三個檔案。照順序重套：ntfy 修正 → stop 修正 → `apply-endpoint-tunnel-url-privacy.sh`。雜湊閘門要等到下一次跑才會發現，所以出事當下不會有任何警告。 |
| `apply-endpoint-tunnel-url-privacy.sh` 說雜湊不符而拒絕 | 不是 `endpoint-vps` 升級了，就是 ntfy／stop 修正沒套（它們是本補丁的建構基準）。先對那兩支跑 `--verify`。 |
| `boot`／`stop`／`watch` 大聲失敗說沒有可用的 `signal.topic_secret` | 套用 `scripts/apply-endpoint-topic-secret.sh` 之後的預期行為 —— 補丁落地了，但秘密從沒設過。**在沒有 kernel 在跑時**執行 `scripts/set-endpoint-topic-secret.sh`。這正是設計**要的**失敗：另一個選項是安靜地退回那條公開可算的舊名字。 |
| `set-endpoint-topic-secret.sh` 拒絕，說有個 kernel 是 `running` | 預期行為。現在改主題會讓那個 kernel **變聾**，而它會繼續燒 GPU 配額。先 `endpoint stop`（或 `endpoint kill-all --yes`），再重跑。`--force` 可以覆寫這個檢查。 |
| `set-endpoint-topic-secret.sh` 警告查不到 kernel 狀態 | **「查不到」不等於「沒有東西在跑」** —— 那是 D-060 的形狀。通常是這個 shell 沒有 Kaggle 憑證。先自己用 `endpoint status` 確認一次。 |
| `set-endpoint-topic-secret.sh` 警告狀態字不在已知清單裡 | 預期行為。Kaggle 回了一個不在 `running`／`queued`／`pending`／`complete`／`error` 裡的字。只有 **positively** 代表「kernel 結束了」的那兩個字才准放行，其餘一律當成「查不到」處理 —— 因為反過來的那個錯（把它讀成「沒有在跑」）正是這道閘門存在的理由（D-060）。用 `endpoint status` 確認。 |
| 已經有 kernel 在跑，而主題已經被改掉了 | 它再也收不到訊號了。`endpoint kill-all --yes` **不建構 `Config`**，所以仍然有效，Kaggle UI 也是。**60 分鐘閒置逾時救不了你** —— 它殺的是引擎行程，不是 notebook。 |
| `apply-endpoint-topic-secret.sh` 拒絕，並指名 ntfy／stop／tunnel-url 三支腳本 | 預期行為 —— 切 C 疊在**切 B 之上**，所以它的原始雜湊逐字等於 B 的修補後雜湊。先照順序把那幾支套上去。 |
| 套用切 C 之後 ntfy 主題就安靜了 | 預期行為，不是失敗 —— kernel 現在發布到**新名字**上。先確認你套用的時候真的沒有 kernel 在跑（見上）；若有，那就是個孤兒。 |
| `set-endpoint-topic-secret.sh --check` 回 2 | 預期行為：沒有可用的秘密，而套補丁之後每一條碰到主題的路徑都會失敗。拿掉 `--check` 再跑一次。 |

接上之後，`bash scripts/check-egress.sh` 會把你的 runtime 列在「啟用中」。
那是正確且預期的。這支探針存在的目的，是讓那成為一個**決定**，而不是意外。
