# 原始輸出（evidence）

這裡放**執行過的探針／腳本的逐字輸出**，用來支撐 `DECISIONS.md` 與 `README.md` 裡的
數字。會進版控，是因為貼在對話裡或 `/tmp` 裡的證據重開就沒了 —— 而本專案的規矩是
**數字要能追回它的來源**（`DECISIONS.md` 的證據表就是為此存在的）。

## 規矩

1. **逐字。** 只移除 ANSI 色碼與外層工具自己的結束頁腳；**不改數字、不刪段落、不重排**。
   每個檔案開頭的註解區塊必須寫明：指令、時間、結束碼、牆上時間、以及引用它的決策條號。
2. **不改寫成「好看的版本」。** 失敗的輸出也照存 —— 一份只留成功樣本的證據目錄，
   跟沒有證據是同一個問題的另一面。
3. **不進版的東西不進來。** `.env` 與任何金鑰（`.gitignore` 已擋 `.env*`）；若輸出裡
   意外含有敏感字樣，寧可整份不存。
4. **檔名**：`YYYY-MM-DD-<探針或腳本>-<可辨識的條件>.txt`。副檔名用 `.txt`，**不用
   `.log`** —— `.gitignore` 忽略 `*.log`，存成 `.log` 會被靜默忽略。

## 目前有的

| 檔案 | 是什麼 | 引用它的地方 |
|---|---|---|
| `2026-09-23-throughput-qwen3-8b.txt` | 吞吐量探針 `--quick`（8b，`num_ctx` 8192／16384），exit 2、14m36s | `DECISIONS.md` D-049 §3／§5；`README.md`〈Hardware sizing → The baseline〉 |
| `2026-09-23-throughput-qwen3-4b.txt` | 同上，**4b**（讓 4b／8b 變成同一把尺的那一輪），exit 2、3m46s | `DECISIONS.md` D-047 §1／§5、D-049 §4／§5；`README.md` 同節 |
| `2026-09-26-pull-peak-measurement.txt` | 量測協定 A：在**獨立** volume 對 `qwen3:8b` 真拉一次（1184 筆樣本／1183 秒），量下載期間的磁碟峰值。倍數 **1.000**，兩個獨立估計法一致，exit 0 | `DECISIONS.md` D-058 §六／§七；`scripts/deploy-vps-decisions.sh` 的常數區 |
| `2026-09-26-pull-preallocation-diagnostic.txt` | 上面那一跑的**儀器驗證**：同一時刻 `st_size` 5,225,377,718 B 對上 `st_blocks*512` 732,086,272 B | `DECISIONS.md` D-058 §六；`scripts/measure-pull-peak.sh` 檔頭 |

上面兩份是**同一次比較的兩半**：同一支探針、同一組條件，只有模型名不同。合起來的結論
（搶 CPU 約 20%、等效頻寬跨模型大小 13.1–19.7 GB/s）寫在 `DECISIONS.md` D-047 第一節
的補記之二。

下面兩份則是**同一支儀器的兩半**，而且**順序不能顛倒**：診斷先證明了「用 `st_size`
量佔用量會得到一個假的答案」（ollama 把 blob 稀疏地預先配置到完整大小，所以表觀值
在下載的下一秒就等於成品），取樣器才據此改成讀 `st_blocks * 512`，量測才成立。少了
前者，後者那份「峰值等於最終」會被讀成一個好消息，而不是一個被修正過的假象。

---

## 2026-09-30 追加：D-071 的那一批（55 份）

### 為什麼現在才進來

`DECISIONS.md` D-071 第十二節原本寫著「**這一批原始產物沒有進 `docs/evidence/`**」，
理由是那條決策還在「未拍板」的狀態（第九節）。**這次搬進來不是因為那個狀態變了。**

搬進來的理由是**保存**：這批產物一直只存在於 `tmp/`，而 `tmp/` 是要清的。清掉之後
D-071 第二節到第九節的每一個數字都只剩下條目裡的轉述，沒有任何東西可以回推。

> **補記（2026-09-30）：`tmp/` 已經清了。** 上面那句「是要清的」現在是過去式。
> 清點 87 個檔，全部有歸宿：**51 個**與本目錄的檔案逐位元組相同、**21 個**（19 個
> `.err` ＋ 2 個 `.exit`）的內容在兩份彙整裡、**2 個**另行收進版控（見 `DECISIONS.md`
> D-071 第十二節）、**13 個**是空檔／標記／可由套用腳本與產生器重建的暫存樹。
> **0 個無家可歸。**

**所以：這次搬遷不等於第九節的 (a)／(b) 已經選邊。** 那個取捨仍然留給人，條目裡
原本怎麼寫就還是怎麼寫。把「東西放進了證據目錄」讀成「決定做了」，是誤讀。

### 兩條規矩的例外（此批適用，規矩本身不變）

**規矩 4 的例外：結構化資料保留原生副檔名。** 規矩 4 要求 `.txt` 的理由**只有一條**
—— `.gitignore` 忽略 `*.log`。`.json` 與 `.csv` **不在**忽略清單裡，沒有「靜默不進版」
的風險。而 JSON 一旦加上 `#` 標頭就不再是 JSON，會摧毀 D-071 第十一節自己指出並記下
的那個缺口所要求的「可重推」性質。所以：**`.json`／`.csv` 逐位元組原樣，不加標頭**；
它們的「指令／時間／結束碼／牆上時間／引用」記在本檔下面的對照表裡。

**規矩 1 的例外：兩份彙整檔不是單一指令的逐字稿。** `probe-exit-codes` 與
`acc-escape-values` 各是把多個小檔併成一份（原本是 2 個 7 位元組的 `.exit`，加上 19 個
stderr —— 其中 16 個只有 23–25 位元組，另外 3 個是 `step-*` 的，51／225／230 位元組）。
與其留 21 個近乎空的檔案，不如併起來並**在檔頭明說這是彙整**。兩份的檔頭都寫了原本
幾個檔、內容動了什麼（只去首尾空白、補檔名與對齊，沒有一個字被改寫）。

### 「拿掉開頭連續的 `#` 行就是原檔」—— 這條可以逐位元組驗

這一條是本目錄唯一能自動檢查的性質，所以寫出來：對 53 個有標頭的逐字檔，**把開頭
連續的 `#` 行整段拿掉之後剩下的位元組，與原檔完全相同**（唯一差異是已移除的 ANSI
色碼）。標頭與內容之間**不留空行**，就是為了讓這句話字面上成立。

**一個例外：** `2026-09-30-crossrun-analyse.py` 的來源自己第一行就是 `#!`（shebang），
光看前綴分不出說明與本文的邊界。它自己的檔頭最後一行有寫這件事 —— **53 個裡就只有
這一個**。

### 6 份沒有標頭的 JSON：對照表

時間欄取自**來源檔的 mtime**（搬遷時一併帶到副本上）。`mtime` **不是版控的一部分**
—— clone 之後會變成 checkout 的時間 —— 所以**以這張表為準**，mtime 只是工作區裡的
方便。

這 6 個時間與**另外兩條獨立路徑**互相印證：前三輪（`r1`／`iso`／`r3`）與 D-071 第二節
表上獨立記下的收工時間相符（`rep-*` 三輪在條目裡只記了範圍「01:24–03:07」，沒有逐輪
收工時間）；六輪**全部**與從 session 逐字稿撈出來的監看輸出相符。mtime 來自檔案系統、
逐字稿來自工具呼叫記錄 —— 兩者沒有共同的來源。

四道指令各對應兩、三個檔，先列出來，下表用代號引用：

```
r1     bash scripts/verify-throughput.sh --json > tmp/throughput-baseline-2026-09-29.json 2> …err
iso    bash scripts/verify-throughput.sh --json -- --models qwen3:4b --ctx 4096 --repeats 7 \
         --length-predict 128 --long-prompt-repeats 1 --overflow-prompt-repeats 1 \
         --reservation-predict 1 > tmp/iso-4096.json 2> …err; echo "EXIT=$?" > tmp/iso-4096.exit
r3     bash scripts/verify-throughput.sh --json > tmp/throughput-baseline-rerun.json 2> …err; \
         echo "EXIT=$?" > tmp/throughput-baseline-rerun.exit
b/c/d  for tag in b c d; do bash scripts/verify-throughput.sh --json -- \
         --models qwen3:4b qwen2.5:3b --ctx 4096 8192 --length-predict 128 \
         > "tmp/reps/rep-$tag.json" 2> "tmp/reps/rep-$tag.log"; done
```

| 檔案 | 指令 | 時間（來源 mtime） | 結束碼 | 牆上時間 | 引用 |
|---|---|---|---|---|---|
| `2026-09-29-throughput-baseline-r1.json` | `r1` | 2026-09-29 22:14:00 | 未留存¹ | 未留存² | D-071 §一、§二、§三、§九補記之二、§十 |
| `2026-09-29-throughput-isolation-4096.json` | `iso` | 2026-09-29 22:52:28 | 1³ | 未留存² | D-071 §二、§九補記之二 |
| `2026-09-30-throughput-baseline-r3.json` | `r3` | 2026-09-30 00:04:47 | 1³ | 未留存² | D-071 §一、§二、§三、§十 |
| `2026-09-30-throughput-repeat-b.json` | `b/c/d` | 2026-09-30 02:03:19 | 1 | 38 分 10 秒 | D-071 §九（補記之二）、§十 |
| `2026-09-30-throughput-repeat-c.json` | `b/c/d` | 2026-09-30 02:35:41 | 0 | 32 分 22 秒 | 同上 |
| `2026-09-30-throughput-repeat-d.json` | `b/c/d` | 2026-09-30 03:07:42 | 0 | 32 分 01 秒 | 同上 |

¹ 那一輪的結束碼被指令尾端的 `echo` 蓋掉了（D-071 第十節自己記下的一個錯誤）。
JSON 裡 `baseline.verdict` 是 `unstable`，與真正的結束碼 1 相符。
² 探針的報告不含牆上時間，外層也沒有記時。
³ 逐字見 `2026-09-30-throughput-probe-exit-codes.txt`。

**這 6 個檔沒有「拿掉標頭就是原檔」那條性質可用**（它們根本沒有標頭），所以另外把
大小與 `sha256` 記在這裡 —— 這是它們唯一的外部完整性錨點：

| 檔案 | 大小 | sha256 |
|---|---|---|
| `2026-09-29-throughput-baseline-r1.json` | 37205 B | `68ed998d3b41db6f207ae3fed0f52233aadb45e8819f1c3f1306185269e0a787` |
| `2026-09-29-throughput-isolation-4096.json` | 16438 B | `a25a02f6245f3e654a55e9c6bf2e8e8d49c3617f9b161ef4fb7cf8d9b54ec2f2` |
| `2026-09-30-throughput-baseline-r3.json` | 37547 B | `59c39642dfa39b42df0eba493b93d6e4cc3e9037a9ef1c2bfe9496ce4a3ee8e0` |
| `2026-09-30-throughput-repeat-b.json` | 32649 B | `802a867514e77627d5c88013a9b571343e27530f84255dc44c129e131d797a27` |
| `2026-09-30-throughput-repeat-c.json` | 32399 B | `a37635809f6ccb6d82ab1005db200c98e15fa8c2c2339f5de67c54f3ec28425d` |
| `2026-09-30-throughput-repeat-d.json` | 32405 B | `64c9f2d68bef2d1891f28aec9c4668b7efc7ff8cf20b63ea4d32743dc1a6d4c2` |

搬遷當場把這 6 個與 `tmp/` 的來源各算過一次，**6/6 相同**。

**`rep-a` 缺號是有原因的**，不是漏存：那一輪少了 `bash`，而 `scripts/verify-throughput.sh`
沒有可執行位，死在 `Permission denied`，什麼都沒產出。`2026-09-30-throughput-repeat-a.txt`
留著就是為了讓下一個看到缺號的人不必再猜。

### 其餘 49 份

| 檔案 | 有幾份 | 是什麼 | 引用它的地方 |
|---|---|---|---|
| `2026-09-30-throughput-{baseline-r1,isolation-4096,baseline-r3,repeat-a,repeat-b,repeat-c,repeat-d}.txt` | 7 | 上表各輪的探針報告（`.err`／`.log`），逐字 | 同上表 |
| `2026-09-30-cpu-steady-alu-separate-cpu{0..3}.csv` | 4 | `cpu-steady.c alu`，一次一顆 vCPU | D-071 §五、§九補記之二 |
| `2026-09-30-cpu-steady-mem-separate-cpu{0..3}.csv` | 4 | 同上，`mem`。**指令列未留存**，是依同輪形式重建的 | D-071 §五、§七 |
| `2026-09-30-cpu-steady-alu-simultaneous-cpu{0,1}.csv` | 2 | 與 `mem` 同一秒跑（alu 吃 cpu0／1） | D-071 §七 |
| `2026-09-30-cpu-steady-mem-simultaneous-cpu{2,3}.csv` | 2 | 同上（mem 吃 cpu2／3） | D-071 §七 |
| `2026-09-30-cpu-steady-alu-4x-cpu{0..3}.csv` | 4 | 四條同時跑 `alu`，無 barrier | D-071 §八（「四臂」之一） |
| `2026-09-30-cpu-steady-mem-4x-cpu{0..3}.csv` | 4 | 四條同時跑 `mem`，頻寬飽和 | D-071 §八（「四臂」之一） |
| `2026-09-30-cpu-steady-mem-long600s-cpu{0..3}.csv` | 4 | 長曝露 600 秒（各 595 筆） | D-071 §五、§七 |
| `2026-09-30-cpu-steady-alu-0309-cpu{0..3}.csv` | 4 | 09-30 03:09 重跑的 `alu`。**必須與 `-separate-`（同夜 00:40）配對讀** —— 補記之二那 18.4% 是兩者的差，單看一組沒有意義 | D-071 §九（補記之二） |
| `2026-09-30-cpu-steady-step4-pinned.csv` | 1 | `step 4` 釘選。**這是同名檔的第二次寫入**，稍早第一次的數字已被覆蓋、無法再重推，D-071 因此不引用它們 | D-071 §八 |
| `2026-09-30-cpu-steady-step4-nopin.csv` | 1 | `step 4` 不釘選，模擬 llama.cpp 的執行緒。第八節用它排除「guest 內部擠壓」那一版 | D-071 §八 |
| `2026-09-30-cpu-steady-step1-pinned.csv` | 1 | 單執行緒對照 | **沒有任何一節引用**（探索期的產物，原樣留存） |
| `2026-09-30-cpu-steady-alu-control-cpu{0..3}.csv` | 4 | 控制組 | **沒有任何一節引用**（同上） |
| `2026-09-30-cpu-steady-acc-escape-values.txt` | 1 | 上面各輪 stderr 的彙整：`acc` 的逃逸值。證明的是「迴圈真的執行了」，不是任何一個數字 | D-071 §十一（手動驗證第 3 條） |
| `2026-09-30-throughput-probe-exit-codes.txt` | 1 | 兩個 `.exit` 檔的彙整 | D-071 §一、§二 |
| `2026-09-30-vmware-toolbox-stat-speed-90s.txt` | 1 | 90 次取樣**全部 `2496 MHz`，一次都沒動** —— 這是標稱值，不是即時時脈。列在這裡是當**反例** | D-071 §五、§十二 |
| `2026-09-30-throughput-contamination-log.txt` | 1 | 我自己造成的量測污染逐筆記錄，寫在量測進行中 | D-071 §九補記之二、§十 |
| `2026-09-30-crossrun-analyse.py` | 1 | 跨輪分析腳本。**這是儀器，不是輸出** | D-071 §十一（2026-09-30 追加） |
| `2026-09-30-crossrun-analyse-asis-1condition.txt` | 1 | 上面那支**原樣**跑出來的結果：只有 1 組共同條件 | D-071 §九補記之二、§十一 |
| `2026-09-30-crossrun-analyse-noiso-6conditions.txt` | 1 | 同一次重跑，**唯一改動**是把 `SOURCES` 裡的 ISO 那一行拿掉：六組條件的輪間極差 **22.1%–48.0%**，與第九節補記之二一字不差 | D-071 §九補記之二、§十一 |

**`analyse.py` 的已知缺陷：** 它的 `SOURCES` 把 ISO 隔離輪也收進去，而 ISO 只有 3 組
條件且全在 `ctx=4096`，會讓「所有輪都有的條件」塌成 **1 組**。第九節補記之二的表是
**6 組** —— 那是**排除 ISO** 之後的五輪交集，也就是上面倒數第二份檔案的來源。要重現
那張表，把那支腳本 `SOURCES` 裡的 ISO 那一行拿掉即可，其餘不動。腳本檔頭有同樣的記載。
