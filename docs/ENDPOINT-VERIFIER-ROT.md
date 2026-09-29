# endpoint 安全驗證器的已知鏽

**現勘：2026-09-30。** 對象是 uv 安裝的 `endpoint-vps 0.1.2`：

```
~/.local/share/uv/tools/endpoint-vps/lib/python3.13/site-packages
```

## 這份文件為什麼存在

endpoint 的安全工作是一疊刀做出來的，每一刀的驗證器都是**對著「當時那棵樹」**寫的。
後面的刀會合法地搬動前面的刀釘住的檔案。所以今天六支驗證器有四支失敗 —— 而**這四支的
失敗全部是過期的釘值或過期的定位字串，沒有一個是活著的缺陷**。

沒有這份清單，下一個讀者分不出「已知的鏽」與「新的損壞」。那是這份清單唯一的用途。

**而「樹沒有壞」不是這裡的判斷，是一個可以自己跑的檢查。** 樹上四個受影響的檔，
`sha256` 全部落在 `scripts/` 底下那 35 個釘值裡，而且各自落在自己那條鏈的**末端**
—— 見〈疊放鏈〉。紅的是驗證器，不是樹。

**它不管的事：不修。** 這些驗證器是「當時檢查了什麼」的記錄。把驗證器改到會過，正是
D-033 的教訓 —— 改規則讓紅變綠，跟它要抓的蟲是同一件事的鏡像。要重新取得覆蓋，是
**新寫一支**、或明講地更新舊的並記下為什麼，不是把它改成綠的。

## 一眼看盡

| # | 驗證器 | 吃什麼參數 | 結束碼 | 判定 |
|---|---|---|---|---|
| 1 | `test_endpoint_apikey_broadcast_fixes.py` | site-packages 根 | **0** | 通過（**非空過**，見下） |
| 2 | `test_endpoint_apikey_selfheal.py` | site-packages 根 | **0** | 通過（**非空過**，見下） |
| 3 | `test_endpoint_apikey_selfheal_mutants.sh` | 已套第四刀的樹 | **0** | 14/14 全過 |
| 4 | `test_endpoint_tunnel_url_privacy.py` | site-packages 根 | 1 | **鏽**：1 項 |
| 5 | `test_endpoint_topic_secret.py` | site-packages 根 | 1 | **鏽**：6 項 |
| 6 | `test_endpoint_topic_secret_mutants.sh` | 已套 C 的樹 | 1 | **被擋住**（不是自身鏽，見下） |
| 7 | `test_endpoint_stop_fixes.py` | site-packages 根 | 1 | **鏽**：2 項無法執行 |
| 8 | `test_endpoint_ntfy_fixes.py` | **產生器檔案**，不是樹根 | 1 | **鏽**：1 項無法執行 |

**第 8 支的參數跟其他五支不一樣，這點值得單獨講。** 它要的是
`site-packages/scripts/master_build_notebook.py`（產生器原始碼），不是 site-packages 根目錄。
拿樹根去餵它會得到一個 `IsADirectoryError` 的 Python traceback、結束碼 1 —— **看起來像
失敗，其實是用錯介面**。這一輪就是這樣先被騙了一次，記在這裡。

那三支通過的，**不是空過**：把第 1、2 支指向 `tmp/precut-a/`（切 A 之前的樹）會得到
結束碼 1。一支對「修補前」與「修補後」都過的驗證器等於什麼都沒檢查 —— 這一點兩支
自己的檔頭都寫了，這裡是照著驗。

**`tmp/precut-a/` 是工作區的暫存樹，`tmp/` 不進版（`.gitignore:17`）、也不會永遠留著。**
它清掉之後，上面那個反向測試要重建才跑得動 —— 而「用套用腳本的 `--revert` 重建一棵
等價的樹」這條路，**這一輪沒有驗過**，所以不寫成處方。**已經驗過的是那個結論**
（兩支對修補前後的結束碼不同），不是那條路徑會一直在。

## 疊放鏈

各套用腳本自己說明的順序是：**ntfy → stop → 切 B → 切 C**（`apply-endpoint-topic-secret.sh:392`、
`apply-endpoint-tunnel-url-privacy.sh:191,199,207`）。切 A 與第四刀動的是 `engine/engine.py`。

把六支套用腳本的釘值排開，**四條鏈各自都是連續的** —— 後一支的 `PRISTINE` 逐字等於
前一支的 `PATCHED`，中間沒有缺口：

| 檔案 | 鏈（每一格是「哪一刀：結果」） |
|---|---|
| `engine/engine.py` | 起 `871b4cab` → 切A `68dfa5a2` → **第四刀 `1930dda1`** |
| `endpoint/commands.py` | 起 `618a8f00` → stop `d8b7cd81` → 切B `8d6f2ade` → 切C `19b8129e` → **第四刀 `abd6d3c9`** |
| `endpoint/core.py` | 起 `c482400a` → stop `2e8692b1` → 切B `8c8afbb9` → **切C `e3d7fe21`** |
| `scripts/master_build_notebook.py` | 起 `1d927451` → ntfy `b416bd47` → 切B `a0449014` → **切C `e29a23aa`** |

**樹上實得 —— 四個檔都落在自己那條鏈的末端：**

| 檔案 | 實得 | 是誰的 `PATCHED` |
|---|---|---|
| `engine/engine.py` | `1930dda1…` | **第四刀的** |
| `endpoint/commands.py` | `abd6d3c9…` | **第四刀的** |
| `endpoint/core.py` | `e3d7fe21…` | 切 C 的 |
| `scripts/master_build_notebook.py` | `e29a23aa…` | 切 C 的 |

**這是這份文件最重要的一張表。** `scripts/` 底下所有 64 位十六進位常數（35 個）去重
之後，**樹上的四個檔全部落在這個集合裡**，而且是各自那條鏈的末端。**沒有一個檔漂移。**

**所以樹是對的，紅的是驗證器。** 這句話就是「鏽」與「損壞」的分野，而它不是推論
—— 把四個檔的 `sha256` 拿去對那 35 個常數就是它全部的證據。D-070 也**獨立**記下了
同一組數字：「安裝樹的兩個檔案雜湊與本節釘住的 `PATCHED` 值逐位元相符 —— `engine.py`
`1930dda1…`、`commands.py` `abd6d3c9…`」。

**第四刀不是偷偷走過去的。** `apply-endpoint-apikey-selfheal.sh:55-64` 有完整的雙向
釘值，它的 `PRISTINE` 正是切 A 與切 C 的 `PATCHED`（`68dfa5a2`、`19b8129e`）——
它是一個閘門，而且它**過了**。會壞掉的是**前面幾刀的驗證器**：它們釘的是「那一刀
剛套完的樣子」，而鏈已經往前走了。

**（第四刀內部的定位方式是另一回事** —— 那是它的補丁怎麼描述自己要改哪幾行，與
**套用腳本釘不釘 sha** 無關。這兩件事在這一輪被混為一談過一次，所以在這裡分開寫。）

## 只有兩個根因

### 根因一：驗證器釘在鏈的中間，而鏈往前走了

第四刀改了 `engine/engine.py` 與 `endpoint/commands.py`，於是那些**釘在第四刀之前**
的檢查就全部失效 —— 不是釘錯了，是**釘在過去**：

- 釘住 `engine.py` ＝ `68dfa5a2`（切 A 修補後）的檢查 → 樹已經是 `1930dda1`
- 釘住 `commands.py` ＝ `19b8129e`（切 C 修補後）的檢查 → 樹已經是 `abd6d3c9`
- 釘住別支驗證器**檔案本身**的 sha、而那支驗證器後來被合法編輯過的檢查 → 失效
  （第四刀讓 `test_endpoint_apikey_broadcast_fixes.py` 必須改：它原本錨在 `APIKEY:`
  分支上，而第四刀把那個分支刪了）

**這三種都不是「值錯了」。** 每一個釘值在寫下的當天都是對的，而且今天仍然**正確地
描述著「那一刀剛套完的樣子」** —— 只是樹已經不是那個樣子了。要讓它們變綠就得改數字，
而**改數字就是重寫歷史**：那個釘值記錄的是「這一刀套用之後我量到什麼」。改掉它，
D-067～D-070 的驗證紀錄就不再是當時的紀錄，而會變成一份**對著今天的樹回頭校準過**的
文件。這是「不修」在這一類上最具體的理由 —— 比原則更硬。

**受影響：第 4 支的檢查 A、第 5 支的 D8／D9／D11／D13×3。** D13 的三項不是獨立缺陷 ——
切 C 的閘門因為 `commands.py` 的釘值對不上而拒絕套用，閘門根本沒跑到能回答問題的地方。
閘門拒絕時自己也把原因印出來了：

```
✗ endpoint/commands.py 既不是已知的原始版，也不是修補後的版本。
  預期原始版雜湊：8d6f2ade…
  實得：          abd6d3c9…
```

### 根因二：定位字串／假命名空間被後來的刀拿走

- **第 7 支的檢查 B、C**：`NameError: name 'topic_secret_of' is not defined`。
  切 C 讓 `run_stop()` 多了這個依賴（樹上 `commands.py:60` 匯入、`:1529` 使用），
  而驗證器 exec 那段程式碼時的**假命名空間沒有跟著更新** —— 它裡面 `topic_secret_of`
  出現 **0 次**（已查）。**B、C 兩項因此從來沒有執行過**，不是執行後失敗。
- **第 8 支的節流檢查**：`StopIteration`。它用兩個標記夾出產生器吐出的 `signal()`，
  結束標記是 `def broadcast(url)`。**那個函式被切 B 整段刪掉了**（`DECISIONS.md:10171`、
  `:10273`），所以結束標記找不到。樹上的產生器已經沒有 `broadcast` 這個字（已查）。

**這兩項都是「大聲的」失敗** —— 驗證器把例外接住、印出「無法執行」、並列進未過清單。
不是靜默跳過。這是它們可以被信任的地方：**紅，而且說得出為什麼紅。**

## 逐項

### 第 1、2、3 支：通過

`test_endpoint_apikey_broadcast_fixes.py`、`test_endpoint_apikey_selfheal.py` 都過，
而且對 `tmp/precut-a/`（切 A 之前的樹）都會失敗 —— 所以它們仍然在檢查東西。
後者是**第四刀自己的**驗證器，而第四刀是最後一刀，沒有更後面的刀來推離它的釘值；
它的檔頭也記著它為什麼刻意用 AST 切片而不是 import。

`test_endpoint_apikey_selfheal_mutants.sh` 對安裝樹 **14/14 全過**：每一條故意弄壞的
突變都被指名的那一句抓到，兩個守門的都沒叫。**這是這一疊裡最健康的一支。**

### 第 4 支：`test_endpoint_tunnel_url_privacy.py` —— 鏽 1 項

```
✗ 切 A 的驗證器被改過了 —— 它的獨立性已經沒了
  → 預期 05a3e7ac41064f8023f8aacfcc923c24f670b9efb2fce722b5e353ca24e8884d
  → 實得 dd5c22faa736f0755f7a1b88aae608cfaef6e777de04501b85acce339a0e4913
```

檢查 A 釘的是**切 A 那支驗證器檔案的 sha**，用來確認「這支驗證器沒有被動過，所以它
對切 A 的判斷仍然獨立」。第四刀**必須**編輯那支驗證器（它的定位錨從 `APIKEY:` 分支
搬到 `TUNNEL ACQUIRED:`），於是這個釘子過期。**根因一。**

### 第 5 支：`test_endpoint_topic_secret.py` —— 鏽 6 項

```
✗ D8:  cut-B verifier now exits 1
✗ D9:  engine.py drifted from the cut-A value (1930dda1…)
✗ D11: endpoint/commands.py pin does not describe this tree (abd6d3c9…)
✗ D13: the gate did not recognise a good secret
✗ D13: --dry-run exited 1
✗ D13: the gate accepted a malformed secret
```

- **D8** 是把第 4 支當子程序跑（`test_endpoint_topic_secret.py` 會叫它），所以 D8 紅
  是**第 4 支紅的下游**，不是第二個缺陷。
- **D9／D11** 就是根因一那兩個檔。
- **D13 ×3** 是切 C 閘門被 D11 擋住的下游。閘門拒絕套用時，那三項問的三個問題
  （有沒有認出好秘密、空跑回不回 0、壞秘密會不會被拒）都無從回答，於是全部記成失敗。
  **它們不是三個獨立的缺陷。**
- 這支的其他檢查（推導、形狀、core.py 的釘值）**是過的** —— 因為 `core.py` 沒被第四刀動過。

### 第 6 支：`test_endpoint_topic_secret_mutants.sh` —— 被擋住

它把第 5 支當**控制組**跑（`:321`），控制組沒有跑完就退出（`:322–325`）。它**自己
沒有任何手寫的 sha 釘值**（已查，`grep` 40 位以上十六進位為空），所以它的紅是**被第 5
支的鏽擋住**，不是自己鏽了。

**但「把第 5 支的鏽解掉之後它會不會過」這一輪沒有驗，不能斷言。** 它的突變表是對著
切 C 當時的樹寫的，而 `commands.py` 已經被第四刀動過 —— 判斷它是否仍然有效，
需要真的解掉鏽再跑一次。

### 第 7 支：`test_endpoint_stop_fixes.py` —— 鏽 2 項

```
✗ 檢查 B 無法執行：NameError: name 'topic_secret_of' is not defined
✗ 檢查 C 無法執行：NameError: name 'topic_secret_of' is not defined
```

B 是「`run_stop()` 在狀態 unknown 時的行為」，C 是「不得回歸（既有的三種『沒事可做』
與 running）」。**兩項都沒有執行**。同一支的其他七項（活性閘門把六種回應映到狀態）
全部通過。**根因二。**

### 第 8 支：`test_endpoint_ntfy_fixes.py` —— 鏽 1 項

```
✗ 節流檢查無法執行：StopIteration:
  A 只有開機前那則 KILL      ✓ 不服從
  B 開機後才發布的 KILL      ✓ 服從並終止
  C 取水位被 429            ✓ 重試 2 次、沒有往下走
```

kill 那側的三項全過；節流那側找不到結束標記 `def broadcast(url)`。**根因二。**

## 怎麼分辨「已知的鏽」與「新的損壞」

重跑一次（指令見下），拿輸出跟上面比對。規則：

1. **已知的鏽講的是「找不到」或「對不上」。** 錯誤訊息裡會指名一個具體的東西：一個
   sha、一個 `NameError` 的名字、一個找不到的標記。它描述的是**驗證器與樹的關係**。
2. **新的損壞講的是「行為不對」。** 錯誤訊息會說某個斷言不成立 —— 閘門放行了不該放行
   的、某個推導得到錯的值、某個計數不等於預期。它描述的是**樹本身**。
3. **數目不一樣就是有事。** 第 4 支應該恰好 1 項，第 5 支恰好 6 項，第 7 支恰好 2 項
   （而且必須是「無法執行」），第 8 支恰好 1 項。**比表上多，就是新的。**
4. **這一輪沒驗過的事，不要當成驗過了。** 例如第 6 支解掉鏽之後會不會過。

**真正的判準是**：這四支的紅都指向「驗證器跟不上樹」，而不是「樹壞了」。**如果哪天
某支紅了、而它的訊息在說樹的行為不對，那份清單幫不了你 —— 那是真的。**

## 重跑指令

```bash
SP=~/.local/share/uv/tools/endpoint-vps/lib/python3.13/site-packages

# 前五支吃樹根
for t in test_endpoint_apikey_broadcast_fixes test_endpoint_apikey_selfheal \
         test_endpoint_stop_fixes test_endpoint_topic_secret \
         test_endpoint_tunnel_url_privacy; do
  python3 "scripts/$t.py" "$SP" >/dev/null 2>&1
  printf '%-42s %s\n' "$t" "$?"
done

# 突變測試吃「已套該刀的樹」—— 安裝樹就是
bash scripts/test_endpoint_apikey_selfheal_mutants.sh "$SP" | tail -1
bash scripts/test_endpoint_topic_secret_mutants.sh "$SP" | tail -1

# 這一支吃產生器檔案，不是樹根
python3 scripts/test_endpoint_ntfy_fixes.py "$SP/scripts/master_build_notebook.py"
```

## 無測試聲明

**這份文件沒有自動化測試，因為它不是程式，是一份觀測記錄。** 它的正確性來自每一個
數字都寫了來源：結束碼與失敗訊息是 2026-09-30 當場對安裝樹跑出來的（不是轉述）；
「四個檔都落在鏈的末端」是拿它們的實得 `sha256` 去比對 `scripts/` 底下**全部** 64 位
十六進位常數得到的（`grep -rhoE '\b[0-9a-f]{64}\b' scripts/ | sort -u`，35 個，四個檔
都在裡面）；第四刀動的是哪兩個檔，是從 `apply-endpoint-apikey-selfheal.sh:52` 的
`FILES=` 讀出來的，它的雙向釘值在同一支腳本的 `:55-64`；「切 B 刪掉了 `broadcast`」
是 `grep` 產生器（0 命中）加上 `DECISIONS.md:10171`、`:10273` 兩處記載互相印證的。

**這一輪更正過一次判讀。** 初稿寫「`engine.py` 與 `commands.py` 不是任何一個釘值」——
那是**錯的**。成因是把「這一刀的補丁**內部**怎麼定位要改的行」讀成了「**套用腳本**
有沒有釘 sha」，而那是兩件事。實際上第四刀的 `PRISTINE`／`PATCHED` 就是那兩個檔的
釘值，而樹正好等於它的 `PATCHED`。**更正之後結論反而更強**：樹不但沒有漂移，還通過了
第四刀那道雙檔雙向的閘門 —— 而那道閘門會在上游一換時擋下來。

**這一輪沒有修改任何驗證器、補丁、或套用腳本** —— 沒有動到 `scripts/` 底下任何一個檔。
上面每一個結束碼與訊息，都可以用〈重跑指令〉那一段原樣重現。
