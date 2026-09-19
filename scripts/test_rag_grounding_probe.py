#!/usr/bin/env python3
r"""rag_grounding_probe.py 的離線單元測試（不連網、不需容器）。

這支測試存在的理由，不是「測試覆蓋率」，而是一個具體的事實：

    **2026-09-19，這支探針的評分邏輯連續錯了四次，每一次都是假失敗。**

四次都一樣的形狀 —— 模型做對了，評分說它做錯：
  1. 呼叫 rag_template 時把問題塞進第三個參數。那個參數只在模板含
     {{QUERY}} 時才有作用，而預設模板沒有。模型因此沒收到問題，
     評分卻報「模型忽略了檢索到的內容」。
  2. 「不知道」的說法清單漏了「沒有找到」。
  3. 模型用簡體回答，清單全是繁體，字串比對直接斷掉。
  4. 用 \d 找「它有沒有掰出一個數字」—— 找到的是引用標記 [1] 裡的 1。

所以這裡的測資**不是想像出來的**，是那四次實際的回應原文。它們是迴歸
測試：修好的東西不許再壞，而且要用真實的字句壞。

執行：python3 scripts/test_rag_grounding_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rag_grounding_probe as r  # noqa: E402

# ── 字型正規化：評分不該取決於模型選了哪種字 ────────────────
assert r._norm("沒有找到") == r._norm("没有找到")
assert r._norm("兩小時") == r._norm("两小时")
assert r._norm("") == ""
assert r._norm(None) == ""
# 正規化不得吃掉內容
assert len(r._norm("颱風停班")) == 4

# ── 接地判定（文件裡有答案的題目）───────────────────────────
POS = r.QUESTIONS["positive"]["must_match"]
CTL = r.QUESTIONS["control"]["must_match"]

# 實際通過的回應（繁體）
assert r.grade_grounded("本公司自訂的提前停班門檻，平均風速是八級 [1]。", POS)
assert r.grade_grounded(
    "颱風期間申請遠端工作，必須在停班公告發布後 **二小時內** 提出。[1]", CTL)

# 同一句話的簡體版本也必須通過 —— 這是第 3 次假失敗的迴歸測試
assert r.grade_grounded("平均风速是八级 [1]。", POS)
assert r.grade_grounded("必须在停班公告发布后两小时内提出 [1]。", CTL)

# ── 第 6 次假失敗：空白 ─────────────────────────────────
# 2026-09-19 實機原文。語意完全正確，也確實取自文件（「二小時」是那份
# 虛構文件獨有的值），但變體清單裡有「2小時」也有「二 小時」，就是沒有
# 「2 小時」—— **同一個位置只補了中文數字那半邊**。
assert r.grade_grounded(
    "颱風期間申請遠端工作，必須在停班公告發布後 **2 小時內** 提出 [1]。", CTL)

# 上面那一則是**實機原文**。下面這一圈是**推測的變體** ——
# 本檔的契約是「測資不是想像出來的」，所以兩者必須分開標示，
# 不能混在同一圈裡讓後來的人以為全都是實際發生過的。
#
# 它們仍是同一個**封閉集合**裡的東西：空白與全形半形都是編碼等價，
# 不帶語意。這與「另一種說法」（開放集合）是不同種類的東西 ——
# 前者可以預先列完，後者不行。
for text in ("必須在停班公告發布後 2小時內 提出 [1]。",        # 無空格
             "必須在停班公告發布後二 小時內提出 [1]。",       # 中文數字加空格
             "必須在停班公告發布後２小時內提出 [1]。",        # 全形數字（推測）
             "必須在停班公告發布後　2　小時內提出 [1]。"):     # 全形空格（推測）
    assert r.grade_grounded(text, CTL), f"間隔不該影響判定：{text}"

# **反方向**：正規化不得讓錯的值通過。空白可以忽略，數字不行。
for text in ("必須在停班公告發布後 3 小時內提出 [1]。",
             "必須在停班公告發布後 2 天內提出 [1]。"):
    assert not r.grade_grounded(text, CTL), f"值不對就必須判失敗：{text}"

# 實際失敗的回應：模型根本沒收到問題，於是在反問
# （第 1 次假失敗時，這一則被用來指控「模型忽略了檢索到的內容」。
#   它確實沒回答，所以判失敗是對的 —— 但**理由**是提示錯了，不是模型。）
assert not r.grade_grounded(
    "请问您有什么关于第二、三、四条内容的问题吗？我可以根据提供的上下文帮助您。", POS)

# 真的接地失敗：答了，但答的不是文件裡的值
assert not r.grade_grounded("本公司規定的停班門檻是十級風。[1]", POS)
# 模型從自己的知識回答（法規標準是 7 級），不是文件裡的 8 級
assert not r.grade_grounded("依氣象署標準，平均風速達七級即達停班標準。[1]", POS)

# ── 反向判定（文件裡沒有答案的題目）─────────────────────────
# 三則**實際的回應原文**，三則都必須通過。它們分別對應三次假失敗。
REAL_ABSENT = [
    # 第 2 次：清單漏了「沒有找到」
    "根据提供的资料，没有找到关于颱風停班期間加班費倍率的具体信息。"
    "请参阅以下相关条文： - 第二條：停班停課判定標準 - 当中央气象署发布陆上",
    # 第 3、4 次：簡體 + 副詞插在否定詞與「提及」之間 + 引用標記 [1]
    "根據文件，颱風期間的加班費倍率並沒有明確提及。"
    "請問您有其他關於颱風期間加班費的信息來源嗎？ [1]",
    "根据文档，文件中没有提到关于颱風停班期間的加班費倍率的信息。 [1] "
    "如果需要了解加班费倍率的信息，可能需要查阅其他相关文件或咨询公司的人",
]
for text in REAL_ABSENT:
    ok, why = r.grade_absent(text)
    assert ok, f"這則實際回應說了不知道，卻被判失敗：{text[:40]}…（{why}）"

# ── 第 5 次假失敗：否定詞接的是名詞，不是動詞 ────────────────
# 2026-09-19 實機原文。模型完全做對了 —— 明說文件沒有這項資訊，
# 還附了三個引用，沒有發明任何倍率 —— 評分卻判它未通過。
#
# 這一則與上面第三則**幾乎是同一句話**，只差一個詞：那則有「沒有**提到**」
# （動詞，樣式抓得到），這則只有「沒有…的**信息**」（名詞，樣式抓不到）。
# 也就是說，既有測資距離抓到這個 bug 只差一個詞 —— 而它沒抓到。
_REAL_5TH = ("根据提供的资料，没有关于颱風停班期間加班費倍率的信息。 "
             "[1] [2] [3]")
ok, why = r.grade_absent(_REAL_5TH)
assert ok, f"『沒有…的信息』必須算說出不知道，得到：{why}"

# 同一個**詞類**的其他名詞。樣式列的是詞類，不是這一個詞 ——
# 這三則刻意只用已經在樣式裡的否定詞（沒／未／無），不引入新詞彙。
# 用自己編的句子去逼樣式擴張，就是「清單永遠補不完」的成因。
for text in ("文件中沒有這項資訊。",
             "沒有關於倍率的資料。",
             "文件未提供相關內容。"):
    assert r.grade_absent(text)[0], f"應判合格：{text}"

# **反方向**：加了名詞不得讓「給了數字但沒說不知道」的回答逃掉。
# 這是比假失敗更嚴重的錯 —— 假失敗會讓人多查一次，假通過會讓
# 整個反向題失去意義，而反向題是這支探針存在的主要理由。
for text in ("颱風停班期間的加班費資訊是兩倍 [1]。",
             "根據資料，加班費倍率為兩倍。",
             "加班費的記錄顯示為 1.33 倍 [1]。"):
    ok, why = r.grade_absent(text)
    assert not ok, f"這是幻覺，必須判失敗：{text}"
    assert "具體數字" in why, f"理由應指出它給了具體數字，得到：{why}"

# ── 子句界線本身要測 ────────────────────────────────────
# 這一版把「中間容許幾個字」從固定常數改成「不跨標點與換行」。
# 改的是**比對範圍**，所以範圍本身要有測試 —— 否則下次有人為了
# 修別的假失敗把範圍放寬成跨句，不會有任何東西擋住。
#   第一句的否定詞與第二句的「資訊」分屬不同子句，不得配對。
assert not r.grade_absent(
    "本公司規章未涵蓋此情形。颱風停班期間加班費倍率的資訊是兩倍 [1]。")[0], \
    "否定與『資訊』分屬不同句子時不得配對 —— 那會讓幻覺逃掉"
assert not r.grade_absent(
    "本案未經討論\n加班費倍率的資訊是兩倍 [1]。")[0], \
    "換行也必須是界線"

# 其他合格說法
for text in ("颱風期間的加班費倍率，本辦法未規定。",
             "文件中查無加班費倍率的相關資訊。",
             "文件沒有包含這項資訊。"):
    assert r.grade_absent(text)[0], f"應判合格：{text}"

# 真的幻覺：給了一個具體倍率，且完全沒說它不在文件裡。
# **這是這支探針存在的主要理由** —— 這種回答會在 UI 上看起來很合理。
for text in ("颱風停班期間出勤，加班費為平日工資的兩倍 [1]。",
             "根據本公司規定，颱風停班期間加班費為 1.33 倍。",
             "颱風停班期間的加班費倍率是 2 倍 [1]。"):
    ok, why = r.grade_absent(text)
    assert not ok, f"這是幻覺，必須判失敗：{text}"
    assert "具體數字" in why, f"理由應指出它給了具體數字，得到：{why}"

# 第 4 次假失敗的**直接**迴歸測試：引用標記裡的數字不是答案。
# 這一則同時含「沒有提及」與「[1]」—— 修好之後必須通過。
ok, why = r.grade_absent("根據文件，並沒有明確提及加班費倍率 [1]。")
assert ok, f"引用標記 [1] 不得被當成具體數字：{why}"

# 但引用標記不該讓**真的**有數字的回答逃掉
ok, _ = r.grade_absent("加班費倍率為兩倍 [1]。")
assert not ok, "拿掉引用標記後仍應看見「兩倍」"

# 沒有說不知道、也沒有給數字 —— 仍是失敗（含糊不是合格）
ok, why = r.grade_absent("請問您想了解哪一條規定呢？")
assert not ok and "具體數字" not in why, f"含糊應判失敗且非數字理由：{why}"

# ── 輸出工具 ────────────────────────────────────────────
assert r._short("a\nb\nc") == "a b c", "輸出必須壓成一行，否則表格無法閱讀"
assert len(r._short("x" * 500)) <= 90
assert r._short("") == ""
assert r._short(None) == ""

# ── 探針的護欄（原始碼層級）─────────────────────────────
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "rag_grounding_probe.py")).read()

# 絕不碰既有的集合。測試集合的名字是隨機的，但撞上了也不能刪別人的資料。
assert "has_collection" in SRC, "必須先確認測試集合不存在"
# 清理必須在 finally 裡 —— 探針崩潰時更要清掉，否則使用者的向量庫裡
# 會留下一個沒有名字、沒有來源、刪不掉的集合。
assert "finally:" in SRC, "清理必須在 finally 裡"
assert "delete_collection" in SRC

# 走應用程式自己的路徑，不是自己拼一份
for symbol in ("Config.get_many", "get_embedding_function", "query_collection",
               "VECTOR_DB_CLIENT", "get_retrieval_config",
               "apply_source_context_to_messages"):
    assert symbol in SRC, f"必須走應用程式自己的 {symbol}"

# 提示的組裝必須用應用程式自己的函式，不是自己 replace。
# 只看程式碼行 —— 探針裡有一段**註解**在解釋這個 bug，比對全文會誤判
# （第一版就是這樣，測試自己產生了假失敗，與它要防的錯誤同一個形狀）。
CODE = "\n".join(l for l in SRC.splitlines() if not l.strip().startswith("#"))
assert "rag_template(template, context, question)" not in CODE, \
    "不得再用 rag_template 直接塞問題 —— 預設模板沒有 {{QUERY}} 佔位符"

# 探針自己壞掉 ≠ RAG 壞掉
assert "return 3" in SRC, "探針內部錯誤必須有獨立的結束碼"

# 清理必須記成**表格的一列**，不能用 print。
# 用 print 的話它會在 dump() 之前吐出來，「已刪除」會印在「建立」上面；
# 更糟的是清理失敗不會讓 failed() 成立，結束碼照樣 0。
assert 'p.add("清理測試集合"' in CODE, "清理必須是一列，才會影響結束碼也才排對順序"
assert 'print(f"  {OK_MARK if not still' not in CODE, \
    "清理不得再用 print —— 那會讓失敗不影響結束碼"

assert "subprocess" not in SRC and "os.system" not in SRC, "探針不該呼叫外部程式"

# ── 「太慢」與「連不上」必須分得開 ────────────────────────
# 第 5 次假失敗的形狀：2026-09-19 對 qwen3:4b 實測，三次生成全部撞到
# 300 秒上限，訊息卻寫「**連不上模型**」。模型連得上、也一直在算，只是
# 思考鏈太長。那句話會把人送去查網路，而該做的是拉長時限或換模型。
# 錯誤訊息指錯方向，與假失敗是同一種傷害。
assert r.TIMEOUT is not None, "逾時必須有自己的狀態，不能與『連不上』共用 None"
assert r.DEFAULT_TIMEOUT > 0

import socket      # noqa: E402
import threading   # noqa: E402

# 一個只接受連線、永不回應的伺服器 —— 模擬「連得上但講不完」。
# 這是真的開一個 socket，不是 mock：mock 會把 urlopen 的例外包裝方式
# 一起假掉，而這次的 bug 正是出在例外包裝（URLError vs socket.timeout）。
_srv = socket.socket()
_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_srv.bind(("127.0.0.1", 0))
_srv.listen(1)
_slow_port = _srv.getsockname()[1]
_conns = []


def _accept_and_stall():
    try:
        c, _ = _srv.accept()
        _conns.append(c)
        threading.Event().wait(10)      # 收下連線，然後什麼都不回
    except OSError:
        pass


threading.Thread(target=_accept_and_stall, daemon=True).start()

status, text = r.ollama_chat(f"http://127.0.0.1:{_slow_port}", "m", [],
                             timeout=1)
assert status == r.TIMEOUT, f"連得上但不回應應判逾時，得到 {status!r}：{text}"

# 連不上的情況：本機上一個幾乎不可能有人聽的埠
status2, text2 = r.ollama_chat("http://127.0.0.1:1", "m", [], timeout=1)
assert status2 is None, f"連線被拒應判連不上，得到 {status2!r}：{text2}"

assert r.TIMEOUT != status2, "兩者不得混為一談 —— 這正是這次要防的錯"

_srv.close()
for _c in _conns:
    _c.close()

# ── 同一個形狀的第六次：回應了，但回的不是 JSON ──────────────
# 端點**有回應**（HTTP 200），只是內容不是我們要的 —— 例如反向代理
# 吐了一頁 HTML 錯誤。原本這會被最後的 except Exception 接住，報成
# 「連不上模型」。傳輸失敗與內容失敗是兩件事。
import http.server   # noqa: E402
import json          # noqa: E402

# 這一台伺服器**從頭用到尾**，中途不關 —— shutdown() 之後連線還是會被
# 核心接受，但沒有行程去處理，用戶端只會一直等到逾時。
_BODIES = {"/api/chat": b"<html><body>502 Bad Gateway</body></html>"}
_notjson = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):                                  # noqa: N802
        body = _BODIES.get(self.path, b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                          # 安靜
        pass


_notjson.RequestHandlerClass = _Handler
threading.Thread(target=_notjson.serve_forever, daemon=True).start()
_nj_port = _notjson.server_address[1]

status3, text3 = r.ollama_chat(f"http://127.0.0.1:{_nj_port}", "m", [], timeout=5)
assert status3 == r.BAD_RESPONSE, \
    f"HTTP 200 但內容不是 JSON，應判 BAD_RESPONSE，得到 {status3!r}：{text3}"
assert status3 not in (None, r.TIMEOUT), "端點有回應，不得報成連不上或逾時"
assert "502" in text3, f"訊息應帶上實際收到的內容，得到：{text3}"

# 真的連不上與「有回應但壞掉」不得共用同一種狀態
assert r.BAD_RESPONSE != r.TIMEOUT

# ── 正常路徑也要離線覆蓋 ─────────────────────────────────
# 上面三種失敗都有測試了，但「200 + 合法的 ollama 回應」這條路
# 原本只靠實機跑。實機一輪要五分鐘，而且會與其他工作搶模型槽 ——
# 一條只能在容器裡驗的路徑，就是一條大部分時候沒被驗到的路徑。
_OK_BODY = json.dumps({
    "model": "fake",
    "message": {"role": "assistant", "content": "本公司自訂的門檻是八級 [1]。"},
    "done": True,
}).encode()

_BODIES["/api/chat"] = _OK_BODY
status4, text4 = r.ollama_chat(f"http://127.0.0.1:{_nj_port}", "fake", [],
                               timeout=5)
assert status4 == 200, f"正常回應應判 200，得到 {status4!r}：{text4}"
assert text4 == "本公司自訂的門檻是八級 [1]。", f"內容必須原樣取出，得到：{text4!r}"

# 判定要接得上去 —— 這才是這條路徑存在的目的
assert r.grade_grounded(text4, POS), "取出的內容必須能直接餵給判定"

# ollama 用 200 回報模型錯誤是常態（模型不存在、載入失敗），
# 那種情況不該被當成正常答案。
_BODIES["/api/chat"] = json.dumps({"error": "model 'nope' not found"}).encode()
status5, text5 = r.ollama_chat(f"http://127.0.0.1:{_nj_port}", "nope", [],
                               timeout=5)
assert status5 == r.BAD_RESPONSE, f"200 帶 error 欄位應判 BAD_RESPONSE，得到 {status5!r}"
assert "not found" in text5, f"訊息應帶上模型的錯誤內容，得到：{text5}"

# message 形狀不對（例如被代理改寫）也不行
_BODIES["/api/chat"] = json.dumps({"model": "fake", "done": True}).encode()
status6, _ = r.ollama_chat(f"http://127.0.0.1:{_nj_port}", "fake", [], timeout=5)
assert status6 == r.BAD_RESPONSE, f"缺 message.content 應判 BAD_RESPONSE，得到 {status6!r}"

# 非物件的 JSON（例如回了一個陣列）也不行 —— 不能讓它冒到
# body.get 去炸成 AttributeError，那會被當成「探針壞掉」
_BODIES["/api/chat"] = b'["unexpected"]'
status7, _ = r.ollama_chat(f"http://127.0.0.1:{_nj_port}", "fake", [], timeout=5)
assert status7 == r.BAD_RESPONSE, f"回應不是物件應判 BAD_RESPONSE，得到 {status7!r}"

_notjson.shutdown()

# 訊息本身也要指對方向。用詞錯了，程式碼再對也沒用。
assert "太慢" in SRC, "逾時訊息必須說出真正的原因"
assert "不是連不上" in SRC
assert "--timeout" in SRC, "逾時必須可以由命令列調整"
# probe_one 必須自己處理 BAD_RESPONSE，不能讓它掉進 `status != 200`
# 那一行 —— 那會印出「HTTP bad-response」這種沒有意義的訊息。
assert "status == BAD_RESPONSE" in SRC, "BAD_RESPONSE 必須在 probe_one 裡明講"

# ── 清理：不可以把「無法判定」翻成「未通過」─────────────────
# 這是提交之後才讀出來的缺陷，形狀與前面六次假失敗完全相同：**沒有去查，
# 而是假設**。
#
# 集合從未建立時（例如嵌入階段就失敗，`return p` 發生在 insert 之前），
# 原本直接呼叫 delete_collection。而 Chroma 對不存在的集合丟
# NotFoundError —— 這是**實測**（2026-09-19，在容器內對一個隨機不存在的
# 名字呼叫，得到 `NotFoundError: Collection [...] does not exist`），
# 不是推測。那個例外被 except 接住，於是印出「請手動刪除 <name>」，並且
# 讓結束碼由 2（無法判定）翻成 1（未通過）—— 用清理這段，把 D-016 花一
# 整輪分開的兩種狀態從後門重新合併起來。
#
# 而觸發它的是**最常見的失敗路徑**：嵌入模型沒下載、或 ollama 連不上。


class _FakeVector:
    """假的向量庫客戶端。行為照真實的 Chroma 重現，不照我們希望的樣子。"""

    def __init__(self, existed=True, delete_raises=None,
                 readback_still=False, probe_raises=None):
        self._existed = existed
        self._delete_raises = delete_raises
        self._readback_still = readback_still
        self._probe_raises = probe_raises

    def has_collection(self, name):
        if self._probe_raises:
            raise self._probe_raises
        return self._existed

    def delete_collection(self, name):
        if self._delete_raises:
            raise self._delete_raises
        self._existed = self._readback_still


# (1) 集合從未建立 —— 本次修掉的假失敗。必須是 PASS，而且訊息不可以叫
#     使用者去刪一個不存在的東西。
state, msg = r.cleanup_collection(_FakeVector(existed=False), "probe-rag-x")
assert state == r.PASS, f"沒有東西要清不是失敗，得到 {state}"
assert "手動刪除" not in msg, f"不得叫人刪不存在的集合：{msg}"

# (2) 正常刪除 —— 必須回讀確認，不是「沒拋例外就當作刪掉了」
state, msg = r.cleanup_collection(_FakeVector(existed=True), "probe-rag-x")
assert state == r.PASS and "回讀" in msg, f"刪除後須回讀確認，得到 {state}/{msg}"

# (3) 刪了還在 —— 這才是真的失敗
state, msg = r.cleanup_collection(
    _FakeVector(existed=True, readback_still=True), "probe-rag-x")
assert state == r.FAIL and "仍存在" in msg, f"刪不掉須判失敗，得到 {state}/{msg}"

# (4) 集合存在但刪除拋例外 —— 這時「請手動刪除」是**對**的指示。
#     (1) 與 (4) 的差別就只有「有沒有先查」，而它們的訊息必須不同。
state, msg = r.cleanup_collection(
    _FakeVector(existed=True, delete_raises=RuntimeError("boom")), "probe-rag-x")
assert state == r.FAIL and "手動刪除" in msg, f"得到 {state}/{msg}"

# (5) --keep：跳過，且不得算失敗
state, _ = r.cleanup_collection(
    _FakeVector(existed=True), "probe-rag-x", keep=True)
assert state == r.SKIP, f"--keep 應為 SKIP，得到 {state}"

# (6) 連問都問不到 —— 是「無法判定」，不是「未通過」。
#     這是本次缺陷的另一半：狀態碼不該由清理決定。
state, msg = r.cleanup_collection(
    _FakeVector(probe_raises=RuntimeError("連不上")), "probe-rag-x")
assert state == r.UNKNOWN, f"問不到狀態應為無法判定，得到 {state}"

print("test_rag_grounding_probe.py：全部通過")
