# 貢獻指南

這個 repo 在量測一套私有 AI 堆疊，並留下證據。對它本身的修改，適用同一套標準。

Issue 與 pull request 可以用**英文或繁體中文**書寫。

## 唯一的規則

**量出來的，不是假設出來的。** 這個 repo 裡的每一句話都屬於五種之一，而且會說出自己是哪一種：

| 種類 | 意思 |
| --- | --- |
| **Measured** —— 已量測 | 在跑動的系統上觀察到的 |
| **Verified** —— 已驗證 | 走應用程式路徑確認過的 |
| **Projected** —— 推估 | 預期如此，但還沒有量 |
| **Proposed** —— 提案 | 只有設計 |
| **Known limitation** —— 已知限制 | 已理解、並記載下來的限制或失效 |

**沒有量過的數字，就標明沒有量過。** 這適用於 PR 說明、issue 留言與程式註解 ——
一句你指不出出處的話，該刪掉，不是該講軟一點。

## 這個 repo 怎麼開發

直接 commit 到 `main` 到 **2026-10-01**（含）為止；從同一天起也走 issue 與 pull
request —— 交界那天兩者都有。這個轉變是刻意的。

沒有為更早的工作補開任何 pull request。那些工作一直在它原來的地方 —— commit 歷史與
決策記錄裡 —— 而**一份沒有發生過的 review 紀錄，不值得偽造**。

這裡的 pull request 以 **draft** 開啟，經 review 後才合併。

## 動手之前

先讀 issue 裡的 **Constraints from existing decisions** 欄位（不能踩到的既有決定）。
[`DECISIONS.md`](DECISIONS.md) 是決策記錄，而且只會更長；沒有人會在動手前讀完它，這
就是那一欄要跟著任務跑的原因。如果你的改動會與某一條衝突，先在 issue 裡說出來 ——
**推翻一條決定是允許的，而推翻本身也是一條決定**。

## 驗證

寫出你跑的那道指令，以及它印了什麼。

* 若這項改動沒有自動化測試，寫下 **no test coverage**（無測試覆蓋），並描述你手動
  怎麼檢查。目前沒有 CI（`gh api
  repos/ian0318git/self-cloud-agent-lab/actions/workflows` 回報 0 個 workflow），
  所以不會有別的東西替你接住它。
* `scripts/` 裡的 verifier 不是常規測試套件：有幾個**對 pristine tree 刻意失敗**，
  而那個失敗本身就是證明。看到紅色之前，先讀那支 verifier 的檔頭。
* 操作指令與閘門在 [`docs/HANDBOOK.zh-TW.md`](docs/HANDBOOK.zh-TW.md)。

## 文件成對

長篇文件成對新增 —— `X.md` 與 `X.zh-TW.md` —— **改一份就等於要改另一份。** 只加一份、
沒加另一份的改動，就是沒做完的改動。

`git ls-files '*.zh-TW.*'` 列出的是已經存在的配對。**它看不出哪一份文件少了另一半**
—— 那是 review 的工作。

## Commit

`type(scope): 說明` —— type 用英文（`feat`、`fix`、`docs`、`test`、`refactor`、
`chore`），說明可以用任一種語言。屋裡的寫法看 `git log --oneline`，它很一致。

## 授權

這個 repo 自己的東西 —— compose 檔、腳本、文件 —— 是 MIT。那**不涵蓋**它拉下來的
映像檔：尤其是 Open WebUI，它是 BSD-3 風格**外加一條品牌條款**。見
[`README.zh-TW.md`](README.zh-TW.md)。

## AI 協作

這個 repo 有部分是以 AI 助理開發的，有些 issue 與 pull request 由它開啟。它們同樣受
上面所有規則約束 —— 每句話標示種類、每個驗證寫出指令 —— 並且與任何其他改動一樣，
**合併前由人類 review**。
