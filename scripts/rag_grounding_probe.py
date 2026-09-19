#!/usr/bin/env python3
"""RAG 接地探針 —— 回答第二階段 RAG 真正該問的問題。

    「檢索到的東西，模型真的拿來用嗎？還是它只是照樣生成一個像樣的答案？」

這支探針**必須在 open-webui 容器內執行**（需要 open_webui 套件與向量資料庫）。

── 它走的是誰的程式碼 ──────────────────────────────────────────

檢索那一段走的是**應用程式自己的函式**：

  · Config.get_many()                     —— 讀設定，與 get_all_models 同一條路
  · get_retrieval_config()                —— 應用程式自己建 RetrievalConfig 的函式
  · get_embedding_function()              —— main.py 啟動時建 EMBEDDING_FUNCTION 用的同一個
  · query_collection()                    —— 聊天流程實際呼叫的檢索函式
  · VECTOR_DB_CLIENT                      —— factory.py 選出的向量後端（本堆疊為 Chroma）
  · rag_template() + get_source_context() —— 應用程式自己的 RAG 提示組裝

**邊界（很重要，不要把它讀成比實際更多）：** 攝入這一段，本探針用的是
應用程式自己的切塊器類別與參數、自己的嵌入函式、自己的向量客戶端，但
**由本探針組裝**，不是走 `/api/v1/files` 那條 HTTP 路徑。也就是說：這一支
證明得了「這條鏈在你的設定下能動」，證明不了「上傳 PDF 的那條路也能動」——
後者要真的上傳一份才知道。

**為什麼不直接呼叫 /api/v1/files：** 那條路需要一個已登入的使用者。本專案
刻意不在 repo 裡寫死管理員密碼（D-003、D-012），而資料庫目前有 0 個使用者。
所以這裡測「機制」，帳號那一步留給人。

── 三種失敗是不同的東西，必須分開報 ────────────────────────────

  檢索失敗   —— 對的 chunk 根本沒被撈出來。問題在嵌入模型或切塊。
  接地失敗   —— chunk 撈到了，模型卻沒用它。問題在模型或提示。
  查無此物失敗 —— 文件裡沒有的東西，模型照樣生一個具體答案。

前兩者分開報，是因為修法完全不同。分不出來的話，「RAG 壞了」會變成一種
沒有可行動性的說法。

── 結束碼（比照 D-016）──────────────────────────────────────

  0  全部通過
  1  有項目**未通過** —— 端點有回應、有結論，就是不對
  2  **無法判定** —— 連不上，或連得上但沒在時限內講完。這不是失敗
  3  **探針自己壞掉** —— 這不是 RAG 的結論，是這支腳本的缺陷

「連不上」與「太慢」在結束碼上同樣是 2，但**訊息必須分開講**。
把太慢講成連不上，會叫人去查網路 —— 而真正該做的是拉長時限或換模型。

用法（容器內）：
    python3 rag_grounding_probe.py --model qwen3:4b
    python3 rag_grounding_probe.py --model qwen3:4b --timeout 900
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid

# 這支腳本被 `docker compose cp` 到 /tmp 再執行，所以 sys.path[0] 是 /tmp，
# 而不是應用程式所在的 /app/backend。容器的工作目錄是 /app/backend，
# 但那只對 `python3 -` 這種從 stdin 讀的形式有效。
_BACKEND = os.getenv("OPEN_WEBUI_BACKEND", "/app/backend")
if os.path.isdir(os.path.join(_BACKEND, "open_webui")):
    sys.path.insert(0, _BACKEND)

# ── 狀態 ────────────────────────────────────────────────
PASS = "通過"
FAIL = "未通過"
UNKNOWN = "無法判定"
SKIP = "未執行"

OK_MARK = "\033[0;32m✓\033[0m"
BAD_MARK = "\033[0;31m✗\033[0m"
UNK_MARK = "\033[0;33m?\033[0m"
DOT_MARK = "\033[0m·\033[0m"

# ── 測試文件 ────────────────────────────────────────────
# 全部是**虛構**的。這一點是設計的一部分，不是偷懶：如果問的是真實法規
# （例如「颱風停班停課的風速標準」），模型可以從自己的參數記憶答對，
# 而我們就分不出「它讀了文件」與「它本來就知道」。
#
# 所以每一個要驗的值都是捏造的，只存在於這份文件裡。模型若答對，
# 只可能來自檢索到的內容。
#
# 文件刻意寫成多個段落（有 markdown 標題，切塊器會用到），且三個要驗的
# 事實分散在**不同段落**——這樣才驗得到檢索是不是真的挑對了段落，
# 而不是把整份文件一股腦塞進 context。
DOCUMENT = """# 颱風期間出勤管理辦法

## 第一條：適用範圍

本辦法適用於本公司全體正職員工、約聘人員與實習生，自公告日起實施。
派駐海外據點之人員依當地法令辦理，不適用本辦法。

## 第二條：停班停課判定標準

當中央氣象署發布陸上颱風警報，且預測工作地所在行政區之風力達停班停課
標準時，由人事部門統一公告。本公司為預留員工通勤時間，自訂之提前停班
門檻為平均風速八級，較法規標準提前一級。

## 第三條：通勤安全

停班期間，員工不得以任何理由要求到班。主管若因業務需要指派人員出勤，
應先取得事業單位主管簽核，並提供必要之交通工具。

## 第四條：遠端工作之申請

颱風期間如需改採遠端工作，應於停班公告發布後二小時內，透過人資系統
提出申請，經直屬主管核准後生效。未經核准而自行遠端工作者，該日以
事假計。

## 第五條：出勤紀錄之處理

因停班而無法出勤之日數，不計入個人之差勤異常紀錄，亦不影響全勤
獎金之計算。人事部門應於次月五日前完成系統註記。

## 第六條：本辦法之修訂

本辦法由人事部門擬訂，經總經理核定後公告實施，修訂時亦同。
"""

# 要驗的三個問題。每一個都是機械可判的。
#
# 正向題與對照題的答案是文件裡的**捏造值**；反向題的答案**不在文件裡**，
# 而且刻意挑一個「模型一定有能力掰出一個具體數字」的問題。
QUESTIONS = {
    "positive": {
        "q": "本公司自訂的提前停班門檻，平均風速是幾級？",
        # 機械判準：答案必須出現「八」或「8」。這是文件獨有的值。
        "must_match": [["八", "8"]],
        "chunk_hint": "提前停班",
        "label": "正向題（文件有答案，必須接地）",
    },
    "control": {
        "q": "颱風期間申請遠端工作，必須在停班公告發布後多久內提出？",
        # 對照題：換一段、換一個值。答對才證明檢索挑對了段落，
        # 而不是只撈到剛好那一段。
        #
        # 「二 小時」這一項已經拿掉 —— 空白現在由 _norm 處理，清單不必
        # 再列舉間隔。這一項原本存在，而模型給的是「2 小時」（阿拉伯
        # 數字加空格），清單裡沒有 —— 同一個位置只補了中文數字那半邊。
        # 這就是列舉的問題：不是漏了，是**只補了想到的那一半**。
        "must_match": [["二小時", "兩小時", "2小時"]],
        "chunk_hint": "遠端工作",
        "label": "對照題（另一個段落，驗檢索是否挑對）",
    },
    "negative": {
        "q": "颱風停班期間的加班費倍率是多少？",
        # 反向題：文件從頭到尾沒提「加班費」。
        # 合格 = 明白說出文件沒有這項資訊（可以另外補充常識，那不算錯）。
        # 不合格 = 給出一個具體倍率而不說明它不在文件裡。
        #
        # ⚠ 這裡換過兩次做法，兩次都是**假失敗**，都是我的評分邏輯錯：
        #
        #   第一次：一份「不知道」的說法清單漏了「沒有找到」，
        #           於是模型明明說了不知道，卻被判成發明答案。
        #   第二次：改成清單加法後，模型說「並沒有**明確**提及」——
        #           否定詞與「提及」之間插了一個副詞，字串比對就斷了。
        #
        # 清單式比對永遠補不完。所以改成**樣式**：否定詞後面容許幾個字的
        # 副詞，再出現「提及／說明／記載／…」。下面的 _norm 先把字型
        # 正規化，因此這裡只寫簡體樣式。
        "label": "反向題（文件沒有答案，不得發明）",
    },
}

# 「它說了不知道」的樣式。在 _norm 之後比對，所以寫簡體。
#
# ⚠ 這是**開放清單**，已經補過四次，每次都是實機跑出來的假失敗：
#   1. 初版只列了「提及」類動詞
#   2. 漏了「沒有**找到**」
#   3. 模型用簡體回答（改用 _norm 之後比對解決，不是靠補字）
#   4. 「没有关于…的**信息**」—— 否定詞接的是**名詞**，不是動詞
#
# 第 4 次（2026-09-19 實機，模型答「根据提供的资料，没有关于…加班費
# 倍率的信息」）證明了一件事：把清單換成樣式**並沒有**解決「清單補不完」，
# 只是把補的單位從詞換成了詞類。所以這裡同時列動詞與名詞，並且 ——
# 這條才是重點 —— **每次補，都要把那次實際的回應原文加進
# `test_rag_grounding_probe.py`**。沒有那一步，補字只是在等下一次。
_ABSENT_RE = re.compile(
    r"[没未无]"                       # 否定詞
    # 中間容許的距離 = **同一個子句內**，不是固定字數。
    # 原本寫 {0,10}，於是「没有关于台风停班期间加班费倍率的**信息**」
    # （中間 15 字）抓不到 —— 那個 10 是憑感覺訂的常數，它自己成了
    # 第二個假失敗。標點才是真正的界線（子句邊界），所以靠標點。
    r"[^。！？!?，,；;、\n]*"          # 不跨標點、不跨換行（頓號也是標點）
    r"(提及|提到|说明|记载|规定|包含|找到|发现|提供|依据"      # 動詞
    r"|相关|这项|这方面"                                     # 指示詞
    r"|信息|资讯|讯息|资料|内容|记录|描述|数据|答案)"          # 名詞
    r"|查无|不在文件|未见|不含|无法从|无法在|超出.{0,6}(范围|文件)"
)

# 模型可能用繁體或簡體回答 —— 同一句話在兩種字型下是不同的字串，
# 而「它剛好選了哪一種」與答案對不對無關。評分若取決於這件事，
# 就會產生第三種假失敗。（實際發生過：正向題答繁體、反向題答簡體。）
_VARIANTS = str.maketrans({
    "沒": "没", "資": "资", "訊": "讯", "規": "规", "辦": "办", "載": "载",
    "記": "记", "項": "项", "這": "这", "無": "无", "說": "说", "關": "关",
    "係": "系", "從": "从", "題": "题", "條": "条", "內": "内", "對": "对",
    "與": "与", "為": "为", "會": "会", "個": "个", "們": "们", "來": "来",
    "時": "时", "過": "过", "開": "开", "實": "实", "際": "际", "應": "应",
    "該": "该", "產": "产", "專": "专", "業": "业", "樣": "样", "發": "发",
    "現": "现", "資": "资", "料": "料", "颱": "台", "風": "风", "費": "费",
    "倍": "倍", "率": "率", "條": "条", "辦": "办", "法": "法", "夠": "够",
    "兩": "两", "點": "点", "鐘": "钟", "級": "级", "間": "间", "門": "门",
    "檻": "槛", "速": "速", "均": "均", "自": "自", "訂": "订", "準": "准",
})


# 全形 → 半形。與字型、空白同一個層次：**編碼**，不是說法。
# 中文文本裡的阿拉伯數字常常是全形的（「２小時」），而它與「2小時」
# 的意思完全相同。屬同一個封閉集合（Unicode 全形區 U+FF01–U+FF5E），
# 不是開放清單。
_FULLWIDTH = str.maketrans(
    {chr(c): chr(c - 0xFEE0) for c in range(0xFF01, 0xFF5F)}
)


def _norm(text):
    """把異體字與空白正規化，讓比對與「模型選了哪種寫法」無關。

    空白為什麼要在這裡拿掉：實機跑出「必須在停班公告發布後 **2 小時內**
    提出」，而變體清單裡有「2小時」也有「二 小時」—— 同一個位置，中文
    數字那版預期了空格、阿拉伯數字那版沒有。那不是判斷，是隨手列舉，
    而列舉永遠少一個。

    **換行不拿掉** —— 它是 `_ABSENT_RE` 的子句界線（見該處說明）。
    """
    return re.sub(r"[ \t　]+", "",
                  (text or "").translate(_VARIANTS).translate(_FULLWIDTH))


class Probe:
    def __init__(self):
        self.rows = []

    def add(self, name, state, detail=""):
        self.rows.append((name, state, detail))

    def failed(self):
        return any(s == FAIL for _, s, _ in self.rows)

    def unknown(self):
        return any(s == UNKNOWN for _, s, _ in self.rows)

    def dump(self, as_json=False):
        if as_json:
            print(json.dumps(
                [{"name": n, "state": s, "detail": d} for n, s, d in self.rows],
                ensure_ascii=False, indent=2))
            return
        for name, state, detail in self.rows:
            mark = {PASS: OK_MARK, FAIL: BAD_MARK,
                    UNKNOWN: UNK_MARK, SKIP: DOT_MARK}[state]
            line = f"  {mark} {name:<34} {state}"
            if detail:
                line += f"  {detail}"
            print(line)


def _short(text, n=90):
    """把回應壓成一行。換行會讓表格無法閱讀。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def _matches(text, groups):
    """每一組是「可接受的寫法」，任一命中即可。比對前先正規化字型。"""
    flat = _norm(text)
    return all(any(_norm(alt) in flat for alt in group) for group in groups)


# ── 判定（抽成函式，才能離線測試）────────────────────────
#
# 這幾段邏輯連續錯了**六次**，全都是**假失敗**。把它們從 probe_one 裡
# 抽出來獨立測試，是這一輪唯一學到的教訓的具體落實：評分邏輯比被評分的
# 東西更需要被測。
#
# 六次裡有四次是同一種病：**評分取決於模型選了哪一種寫法**——
# 繁體還是簡體、中文數字還是阿拉伯數字、中間有沒有空格。每一次的修法
# 都一樣：**正規化，不要列舉**。

def grade_grounded(text, must_match):
    """文件裡有答案的題目：答出捏造值才算通過。"""
    return _matches(text, must_match)


def grade_absent(text):
    """文件裡沒有答案的題目：回傳 (是否合格, 理由)。

    合格 = 明白說出文件沒有這項資訊。另外補充常識不算錯 ——
    應用程式的模板本來就允許「說明這不在文件中，再用自己的知識回答」。
    """
    flat = _norm(text)
    if _ABSENT_RE.search(flat):
        return True, "明白指出文件未提及"

    # ⚠ 引用標記 [1] 裡面有數字。第一版直接搜 \d，於是
    #   「並沒有明確提及… [1]」被判成「給了具體數字」——
    #   一個自己加的引用註腳，被當成幻覺的證據。先把它拿掉再找數字。
    without_citations = re.sub(r"\[\d+\]", "", flat)
    looks_specific = bool(
        re.search(r"\d", without_citations)
        or re.search(r"[一二两三四五六七八九十]+(倍|成|小时|天|日|元|块)", without_citations)
    )
    if looks_specific:
        return False, "未說明文件沒有這項，卻給了具體數字"
    return False, "未說明文件沒有這項資訊"


# ── 應用程式的物件 ──────────────────────────────────────

def _load_app():
    """載入應用程式自己的模組。這行會執行 Config.configure(defaults=...)。"""
    import open_webui.config  # noqa: F401
    from open_webui.models.config import Config
    from open_webui.retrieval.utils import get_embedding_function, query_collection
    from open_webui.retrieval.vector.factory import VECTOR_DB_CLIENT
    from open_webui.routers.retrieval import get_retrieval_config
    from open_webui.utils.middleware import (
        apply_source_context_to_messages,
        get_source_context,
    )
    from open_webui.utils.task import rag_template
    return {
        "Config": Config,
        "get_embedding_function": get_embedding_function,
        "query_collection": query_collection,
        "VECTOR_DB_CLIENT": VECTOR_DB_CLIENT,
        "get_retrieval_config": get_retrieval_config,
        "get_source_context": get_source_context,
        "apply_source_context_to_messages": apply_source_context_to_messages,
        "rag_template": rag_template,
    }


EMBED_KEYS = (
    "rag.embedding_engine",
    "rag.embedding_model",
    "rag.ollama.base_url",
    "rag.ollama.api_key",
    "rag.openai.api_base_url",
    "rag.openai.api_key",
    "rag.azure_openai.base_url",
    "rag.azure_openai.api_key",
    "rag.azure_openai.api_version",
    "rag.embedding_batch_size",
    "rag.enable_async_embedding",
    "rag.embedding_concurrent_requests",
)


async def build_embedding_function(app):
    """照 main.py 啟動時的做法，從資料庫的設定建出嵌入函式。

    刻意**重讀一次設定**而不是沿用 app.state 的物件：這支探針是在另一個
    行程裡跑的，沒有那個 app.state。讀的是同一張表、同一批鍵、同一個函式。
    """
    cfg = await app["Config"].get_many(*EMBED_KEYS)
    engine = cfg.get("rag.embedding_engine")
    url = {
        "openai": cfg.get("rag.openai.api_base_url"),
        "ollama": cfg.get("rag.ollama.base_url"),
        "azure_openai": cfg.get("rag.azure_openai.base_url"),
    }.get(engine)
    key = {
        "openai": cfg.get("rag.openai.api_key"),
        "ollama": cfg.get("rag.ollama.api_key"),
        "azure_openai": cfg.get("rag.azure_openai.api_key"),
    }.get(engine)

    fn = app["get_embedding_function"](
        engine,
        cfg.get("rag.embedding_model"),
        embedding_function=None,          # 引擎不是 SentenceTransformers 時不使用
        url=url,
        key=key,
        embedding_batch_size=cfg.get("rag.embedding_batch_size"),
        azure_api_version=(
            cfg.get("rag.azure_openai.api_version") if engine == "azure_openai" else None
        ),
        enable_async=cfg.get("rag.enable_async_embedding"),
        concurrent_requests=cfg.get("rag.embedding_concurrent_requests"),
    )
    return fn, cfg


def split_document(text, config):
    """應用程式自己的切塊器，用資料庫裡的參數。

    沒有呼叫 save_docs_to_vector_db：那個函式會無條件解參考
    request.app.state.ef 與 request.app.state.main_loop，要呼叫它就得
    捏造一個假的 request 物件。與其假造執行環境，這裡直接用同一個
    切塊器類別、同一組參數。
    """
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError:                                   # pragma: no cover
        from langchain.text_splitter import RecursiveCharacterTextSplitter
    from langchain_core.documents import Document

    docs = [Document(page_content=text, metadata={"name": "probe-doc.md"})]

    if config.ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER:
        try:
            from langchain_text_splitters import MarkdownHeaderTextSplitter
            md = MarkdownHeaderTextSplitter(
                headers_to_split_on=[("#", "h1"), ("##", "h2")],
                strip_headers=False,
            )
            split = md.split_text(text)
            if split:
                docs = split
        except Exception:                                  # noqa: BLE001
            # 標題切塊失敗就退回整份文件再切 —— 與應用程式一致的地方是
            # 後面那一段，這裡失敗不該讓整場實驗中止。
            pass

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        add_start_index=True,
    )
    return splitter.split_documents(docs)


# ── 生成 ────────────────────────────────────────────────

TIMEOUT = "timeout"        # 連得上，但沒在時限內講完
BAD_RESPONSE = "bad-response"   # 回應了，但回的不是我們要的東西
DEFAULT_TIMEOUT = 300      # 秒。qwen2.5:3b 兩輪實測 41–94 秒（範圍與成因見
                           # D-018）。300 是對照**最慢**那輪訂的，不是最快
                           # 那輪 —— 看著快的那輪訂會對健康的堆疊誤報逾時。
                           # 但**思考型模型**（qwen3 系列）仍會超出，見 --timeout。


def ollama_chat(base_url, model, messages, timeout=DEFAULT_TIMEOUT):
    """直接對 Ollama 要一次生成。

    messages 是**應用程式自己的函式**組出來的（見 probe_one），不是這裡拼的。

    回傳 (status, text)，status 有五種：
      200           成功
      int           HTTP 錯誤（有回應，只是拒絕）
      None          **連不上** —— 連線被拒、DNS 失敗、沒有這個服務
      TIMEOUT       **連得上，只是來不及講完**
      BAD_RESPONSE  **連得上、也回應了，但回的不是可用的答案**

    **這四種失敗必須分開講。** 2026-09-19 對 qwen3:4b 實測時，三次生成
    全部撞到 300 秒上限，訊息卻寫「連不上模型」—— 模型明明連得上、也一直
    在算，只是思考鏈太長。那句話會把人送去查網路，而該做的是拉長時限或
    換一個不需要思考鏈的模型。

    同一天稍後在**這支函式自己身上**發現同一形狀的第二個：`json.load`
    失敗原本會被最後的 `except Exception` 接住，也報成「連不上」。可是
    端點明明回應了 —— 只是回的不是 JSON（例如反向代理吐了 HTML 錯誤頁）。
    傳輸失敗與內容失敗是兩件事，所以下面把它們分開處理。
    """
    import socket
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    # ── 第一段：連線與傳輸。這裡的失敗才是「連不上」。──
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        # urlopen 會把連線階段的逾時包進 URLError，讀取階段的逾時則直接
        # 拋 socket.timeout。兩種都要認出來，否則連線逾時又會被報成連不上。
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return TIMEOUT, f"{timeout} 秒內沒有回應"
        return None, str(exc)
    except (socket.timeout, TimeoutError):
        return TIMEOUT, f"{timeout} 秒內沒有回應"
    except Exception as exc:                               # noqa: BLE001
        return None, str(exc)

    # ── 第二段：內容。走到這裡代表**端點有回應**，再壞都不是「連不上」。──
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:                               # noqa: BLE001
        return BAD_RESPONSE, f"{exc}｜{_short(raw.decode('utf-8', 'replace'), 60)}"

    if not isinstance(body, dict):
        return BAD_RESPONSE, f"回應不是物件：{_short(str(body), 60)}"
    if body.get("error"):
        return BAD_RESPONSE, _short(str(body["error"]), 80)

    message = body.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return BAD_RESPONSE, f"回應沒有 message.content：{_short(str(body), 60)}"
    return 200, content


# ── 主流程 ──────────────────────────────────────────────

async def run(model, keep=False, timeout=DEFAULT_TIMEOUT):
    p = Probe()
    app = _load_app()

    collection = f"probe-rag-{uuid.uuid4().hex[:12]}"
    vector = app["VECTOR_DB_CLIENT"]

    # 絕不碰既有的集合。名字是隨機的，但如果真的撞上了，那是別人的資料。
    if vector.has_collection(collection):
        p.add("建立測試集合", FAIL, f"{collection} 已存在 —— 中止，不碰既有資料")
        return p
    p.add("建立測試集合", PASS, collection)

    embedding_fn, embed_cfg = await build_embedding_function(app)
    engine = embed_cfg.get("rag.embedding_engine")
    emb_model = embed_cfg.get("rag.embedding_model")
    p.add("嵌入設定", PASS, f"引擎 {engine}、模型 {emb_model}")

    config = await app["get_retrieval_config"]()
    k = await app["Config"].get("rag.top_k")

    try:
        # ── A 攝入 ──────────────────────────────────────
        chunks = split_document(DOCUMENT, config)
        if not chunks:
            p.add("切塊", FAIL, "切不出任何 chunk")
            return p
        p.add("切塊", PASS,
              f"{len(chunks)} 塊（chunk_size={config.CHUNK_SIZE}、"
              f"overlap={config.CHUNK_OVERLAP}）")

        texts = [c.page_content for c in chunks]
        try:
            vectors = await embedding_fn(texts)
        except Exception as exc:                           # noqa: BLE001
            p.add("嵌入文件", UNKNOWN, f"無法取得向量：{_short(str(exc), 80)}")
            return p
        if not vectors or len(vectors) != len(texts):
            p.add("嵌入文件", FAIL, f"取得 {len(vectors or [])} 個向量，預期 {len(texts)}")
            return p
        p.add("嵌入文件", PASS, f"{len(vectors)} 個向量，維度 {len(vectors[0])}")

        vector.insert(collection, [
            {
                "id": str(uuid.uuid4()),
                "text": t,
                "vector": v,
                "metadata": {"name": "probe-doc.md", "source": "probe-doc.md"},
            }
            for t, v in zip(texts, vectors)
        ])
        p.add("寫入向量庫", PASS, f"{type(vector).__name__}")

        # ── B 逐題：檢索 → 生成 → 判定 ───────────────────
        answers = {}
        for key, spec in QUESTIONS.items():
            probe_obj = await probe_one(
                app, vector, collection, embedding_fn, k, model,
                embed_cfg, key, spec, timeout=timeout,
            )
            for row in probe_obj.rows:
                p.rows.append(row)
            answers[key] = probe_obj.answer

        return p
    finally:
        # 清理一定要發生。留一個 probe-* 集合在正式向量庫裡，
        # 會變成使用者知識庫裡一個沒有名字、沒有來源、刪不掉的東西。
        #
        # 這裡用 p.add **而不是 print**，有兩個理由：
        #  1. print 會在 dump() 之前就吐出來，於是「清理測試集合：已刪除」
        #     會印在「建立測試集合」**上面**。在一支以輸出為證據的工具裡，
        #     順序本身就是證據，反過來讀是錯的。
        #  2. 記成一列之後，清理**失敗**才會讓 failed() 成立。原本清理失敗
        #     只印一行訊息、結束碼照樣 0 —— 那等於在說「全部通過」，卻把
        #     一個刪不掉的集合留在使用者的向量庫裡。
        if keep:
            p.add("清理測試集合", SKIP, f"--keep：保留 {collection}（請自行刪除）")
        else:
            try:
                vector.delete_collection(collection)
                still = vector.has_collection(collection)
                p.add("清理測試集合",
                      FAIL if still else PASS,
                      f"刪除後仍存在：{collection}" if still
                      else "已刪除（回讀確認）")
            except Exception as exc:                       # noqa: BLE001
                p.add("清理測試集合", FAIL,
                      f"失敗：{_short(str(exc), 60)} —— 請手動刪除 {collection}")


async def probe_one(app, vector, collection, embedding_fn, k, model,
                    embed_cfg, key, spec, timeout=DEFAULT_TIMEOUT):
    """一題：檢索 → 組提示 → 生成 → 機械判定。"""
    p = Probe()
    p.answer = None
    label = spec["label"]
    question = spec["q"]

    # ── 檢索（應用程式自己的函式）──────────────────────
    try:
        result = await app["query_collection"](
            None,                      # request=None：不走 hybrid search（本堆疊未啟用）
            [collection],
            [question],
            embedding_fn,
            k,
        )
    except Exception as exc:                               # noqa: BLE001
        p.add(label, UNKNOWN, f"檢索失敗：{_short(str(exc), 80)}")
        return p

    docs = (result or {}).get("documents") or [[]]
    docs = docs[0] if docs else []
    metas = (result or {}).get("metadatas") or [[]]
    metas = metas[0] if metas else []
    if not docs:
        p.add(f"{label}·檢索", FAIL, "檢索回傳 0 個 chunk")
        return p

    # 對的段落有沒有被撈出來？這是「檢索」這半的判準，與模型無關。
    if key == "negative":
        # 反向題本來就不該撈到含有答案的段落 —— 沒有可檢查的目標。
        p.add(f"{label}·檢索", PASS, f"{len(docs)} 個 chunk（本題無數目標段落）")
    else:
        hit = [i for i, d in enumerate(docs) if spec["chunk_hint"] in d]
        if hit:
            p.add(f"{label}·檢索", PASS,
                  f"目標段落排在第 {[i + 1 for i in hit]} 名（共 {len(docs)} 塊）")
        else:
            p.add(f"{label}·檢索", FAIL,
                  f"撈回的 {len(docs)} 塊裡沒有「{spec['chunk_hint']}」那段"
                  f" —— 這是檢索問題，不是模型的問題")
            return p

    # ── 組訊息（應用程式自己的函式）────────────────────
    #
    # ⚠ 這裡曾經寫錯，而且是那種「看起來很合理」的錯：一開始我呼叫的是
    #   rag_template(template, context, question)，以為第三個參數會把問題
    #   放進提示裡。**它不會。** rag_template 只替換模板裡實際存在的
    #   {{QUERY}} / [query] 佔位符，而預設模板只有 {{CONTEXT}}。
    #   問題是**使用者訊息**，不是模板的一部分。
    #
    #   症狀是模型回過頭來問「請問您想問第二、三、四條的哪一條？」——
    #   它收到了文件，沒收到問題。當時我的評分邏輯把這判成
    #   「模型忽略了檢索到的內容」，也就是說：**它指著模型罵，但錯的是提示。**
    #
    #   現在改成呼叫應用程式自己組訊息的那個函式。它不需要 request
    #   （整個函式體內沒用到），所以傳 None 是誠實的，不是假造環境。
    sources = [{"document": docs, "metadata": metas,
                "source": {"name": "probe-doc.md", "type": "file", "id": "probe"}}]
    messages = await app["apply_source_context_to_messages"](
        None,
        [{"role": "user", "content": question}],
        sources,
        question,
    )

    # ── 生成 ───────────────────────────────────────────
    base_url = embed_cfg.get("rag.ollama.base_url") or "http://ollama:11434"
    t0 = time.time()
    status, text = ollama_chat(base_url, model, messages, timeout=timeout)
    elapsed = time.time() - t0

    if status == TIMEOUT:
        # 這是**慢**，不是連不上。訊息必須這樣寫，否則會被拿去查網路。
        p.add(f"{label}·生成", UNKNOWN,
              f"模型連得上，但 {timeout} 秒內沒講完 —— 這是太慢，不是連不上。"
              f"請用 --timeout 拉長，或改用非思考型模型")
        return p
    if status is None:
        p.add(f"{label}·生成", UNKNOWN, f"連不上模型：{_short(text, 70)}")
        return p
    if status == BAD_RESPONSE:
        # 端點**有回應**，只是回的不能算答案。這是有結論的失敗，不是
        # 無法判定 —— 依 D-016，「無法判定」只保留給沒有回應的情況。
        p.add(f"{label}·生成", FAIL, f"模型回應無法解析：{_short(text, 70)}")
        return p
    if status != 200:
        p.add(f"{label}·生成", FAIL, f"HTTP {status} {_short(text, 70)}")
        return p

    p.answer = text

    # ── 判定（機械）────────────────────────────────────
    # **每一個判定都要附上它判定所依據的文字。** 沒有這句話，一行
    # 「未通過」就是在要求人相信一個看不見的結論 —— 而本專案已經吃過
    # 兩次假失敗（D-014、D-016），兩次都是評分邏輯自己錯。
    snapshot = f"{elapsed:.1f}s｜{_short(text, 70)}"

    if key == "negative":
        ok, reason = grade_absent(text)
    else:
        ok = grade_grounded(text, spec["must_match"])
        reason = "答出文件裡的捏造值" if ok else "未答出文件裡的捏造值"

    p.add(f"{label}·判定", PASS if ok else FAIL, f"{reason}｜{snapshot}")
    return p


def main():
    ap = argparse.ArgumentParser(description="RAG 接地探針")
    ap.add_argument("--model", required=True, help="用來生成回答的模型")
    ap.add_argument("--json", action="store_true", help="以 JSON 輸出")
    ap.add_argument("--keep", action="store_true",
                    help="保留測試集合（除錯用；正常情況下不要用）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"單次生成的時限秒數（預設 {DEFAULT_TIMEOUT}）。"
                         "思考型模型需要更長")
    args = ap.parse_args()

    print("=" * 62)
    print(" RAG 接地探針 —— 檢索到的內容，模型真的用了嗎？")
    print("=" * 62)
    print()

    try:
        probe = asyncio.run(run(args.model, keep=args.keep, timeout=args.timeout))
    except Exception as exc:                               # noqa: BLE001
        # **探針自己壞掉，不是 RAG 壞掉。** 這兩件事必須分開：把它報成
        # 「未通過」會叫人去修一個沒壞的系統（D-016 的形狀）。
        import traceback
        traceback.print_exc()
        print()
        print("─" * 62)
        print(" 結論：**探針本身執行失敗** —— 這不是 RAG 的結論。")
        print(f"       {type(exc).__name__}: {exc}")
        print("       請把上方 traceback 當成缺陷回報，不要先去改設定。")
        return 3

    print()
    probe.dump(as_json=args.json)

    if args.json:
        return 1 if probe.failed() else (2 if probe.unknown() else 0)

    print()
    print("─" * 62)
    if probe.failed():
        print(" 結論：**未通過** —— 上面標「未通過」的就是結論，不是無法判定。")
        print("       檢索與接地是兩件事，看是哪一項沒過再決定修什麼。")
        return 1
    if probe.unknown():
        print(" 結論：**無法判定** —— 有東西連不上，或有項目沒在時限內跑完。")
        print("       這不是失敗，也還不是 RAG 的結論 —— 先看上面每一列寫的")
        print("       是哪一種：連不上就查服務，太慢就拉 --timeout 或換模型。")
        return 2
    print(" 結論：通過 —— 檢索挑對了段落，模型也真的用了它，且沒有發明。")
    print()
    print(" 邊界：本探針證明的是這條鏈在目前設定下能動。**沒有**證明")
    print("       上傳 PDF 的那條 HTTP 路徑也能動 —— 那需要一個帳號。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
