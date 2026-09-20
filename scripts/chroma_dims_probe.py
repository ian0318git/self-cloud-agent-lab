#!/usr/bin/env python3
"""第三階段 item 2 的探針：mem0 的 Chroma 後端與本專案的 embeddings 對不對得上？

README 第三階段第 2 項是個 [open]，上面寫著：

  「mem0 的 Chroma 後端預設 **1536 維**（OpenAI 大小）。本專案的嵌入模型是
    `qwen3-embedding:0.6b`，量到 **1024 維**（D-013）。照預設值跑會在寫入時
    以 shape mismatch 失敗。`embedding_dims` 與集合的維度都必須釘在 1024。」

**這段敘述從來沒有被量過。** 這支探針存在的理由就是去量它 —— 而量出來的結果
與敘述不符，見下。

### 量到的四件事（2026-09-20，mem0ai 2.1.0 / chromadb 1.5.9）

1. **`1536` 不在這條路徑上。** `ChromaDbConfig` 的欄位是
   `collection_name / client / path / host / port / api_key / tenant` ——
   **沒有任何維度欄位**。1536 是 mem0 的 *OpenAI* embedder 與其他向量庫
   （pgvector、milvus、redis…）的預設，不是 Chroma 的，也不是 ollama 的。
   所以 README 說「把 `embedding_dims` 釘在 1024」在這條路徑上**無處可釘**。

2. **mem0 的 ollama embedder 宣告 `512`，而且那個值是死的。**
   `OllamaEmbedding.__init__` 寫 `self.config.embedding_dims = ... or 512`，
   但 `embed()` 直接把 ollama 回的向量原樣傳出去，從不截斷也不補齊。
   實測：宣告 512、實際回傳 1024 維。

3. **真正的機制是「第一次寫入鎖定維度」。** 集合建立時沒有維度、`metadata`
   是 `None`；寫入第一批向量之後維度才定下來，之後寫入不同長度會被擋：
   `InvalidArgumentError: Collection expecting embedding with dimension of 1024, got 512`。
   這個失敗是**吵的**，而且訊息可辨識。

4. **真正危險的是換模型，不是換維度。** 本機 ollama 裡的
   `qwen3-embedding:0.6b` 與 `bge-m3:latest` **都是 1024 維**。所以
   「維度檢查」對「embedding 模型被換掉」**完全沒有防禦力** —— 而 Chroma
   不會有任何抱怨，檢索只是安靜地回錯文件。實測：同一段文字在兩個模型下的
   cosine 是 **-0.02**，而**同一個模型下兩段不相干文字**的 cosine 是 **0.40**。
   也就是說：跨模型的「同一句話」比同模型內的「兩句不相干的話」還要遠。

第 4 點是 D-016 說的那種最難發現的失敗：**沒有例外、沒有錯誤訊息、答案看起來
正常，只是錯的。** 而 README 原本提議的防線（比對維度）正好擋不住它。

### 因此這支探針的判準

C1~C6 是**重新量測**：把上面四件事變成可重跑的斷言，上游改了就立刻叫。
C7 是**建設性的那一半**：示範一個維度以外的偵測方式確實可行 ——
在建立集合時把 embedding 模型名寫進 collection metadata，開集合時比對。
實測這個記錄**活得過** mem0 的 `create_col()`（它只傳 name 與
`embedding_function`），而且對既有集合用不同 metadata 再 `get_or_create`
**不會蓋掉**原值（也不丟例外）—— 這讓它可以是權威來源。

### 為什麼評分邏輯要獨立成純函式

本專案慣例（scripts/test_langgraph_tools_probe.py、D-016）：**評分邏輯本身要被
實測過**。所以 `grade()`、`observations()`、`cosine()`、`guard_verdict()`、
`dimension_error_recognised()` 只讀一般資料結構、不 import mem0/chroma。
mem0 與 chromadb 的 import 全部延後到 `run_probe()` 裡面。

用法（通常由 scripts/verify-chroma-dims.sh 呼叫）：
    python3 chroma_dims_probe.py --model qwen3-embedding:0.6b \
        --alt-model bge-m3:latest --ollama-url http://ollama:11434

結束碼：0 = 通過（C1~C7 全過）
        1 = 未通過（某一條判準沒過 —— 見輸出，通常是上游變了）
        2 = 無法判定（連不上、模型沒下載 —— 不是判準的問題）
        3 = 探針自己壞掉（D-018：這不是受測對象的問題）
"""

import argparse
import json
import math
import re
import sys
import tempfile

# ── 結束碼（D-018 的約定，全專案一致）──────────────────
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INDETERMINATE = 2
EXIT_BROKEN = 3

# D-013 在 open-webui 容器內端到端量到的維度。**改嵌入模型就要改這個值，
# 而且必須重新嵌入** —— 這正是 C5/C6 在講的那件事，不要只改數字。
EXPECTED_DIM = 1024

# README 第 2 項指名的那個數字。留成常數是為了讓「它到底在不在這條路徑上」
# 是一個可以被斷言的問題，而不是散落在註解裡的一句話。
README_CLAIMED_DIM = 1536

# Chroma 擋下維度不符時，訊息裡的固定片段。
# 刻意用完整的片語而不是 `dimension` 這種單字：D-024 第九節的教訓是
# 「用會回傳你期待答案的查法，就會得到你期待的答案」—— `dimension`
# 也會命中語意相反的訊息。這裡用片語，讓它只命中這一種失敗。
DIM_MISMATCH_PHRASE = "expecting embedding with dimension of"

# 兩個模型中，用來量跨模型不相容的那句話。刻意與檢索用的文件不同，
# 這樣「同一段文字」與「不相干的文字」是兩組真正不同的輸入。
SAMPLE_TEXT = "這台伺服器的記憶體是十六 GB"

# 檢索示範用的文件集。其中只有最後一則與查詢相關 ——
# 相關性要一眼看得出來，否則「回錯文件」無法判定。
DOCS = (
    "貓咪喜歡吃魚",
    "汽車需要定期保養輪胎",
    "股票市場今天下跌三百點",
    "下雨天要記得帶傘",
    "這台伺服器的記憶體是十六 GB",
)
QUERY = "我想買一台新的伺服器，記憶體越大越好"
EXPECTED_DOC = DOCS[-1]

# 集合的 metadata 用哪個鍵記模型名。抽成常數是因為 C7 的守門邏輯要跟它對齊。
MODEL_METADATA_KEY = "embedding_model"
DIM_METADATA_KEY = "embedding_dim"


# ══════════════════════════════════════════════════════════
# 純函式區 —— 這一段是 test_chroma_dims_probe.py 的受測對象
#
# 只讀一般資料結構（dict / list / 數字 / 字串），不 import mem0 也不 import
# chromadb，所以離線測試不需要安裝任何相依。
# ══════════════════════════════════════════════════════════


def cosine(a, b):
    """兩向量的 cosine 相似度。長度不同或長度為 0 時回 None。

    回 None 而不是 0.0 是刻意的：0.0 是一個**合法**的相似度值
    （正交），把它拿來當「算不出來」的哨兵會讓兩種完全不同的情況
    在輸出上長得一樣。
    """
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return dot / (na * nb)


def dimension_error_recognised(error_type, error_module, message):
    """這個例外是不是「維度不符」那一種？

    三個條件都要成立。只看訊息會把別人的錯誤訊息誤認成這一種；
    只看型別名稱則會在 chromadb 改用它自己的例外類別時失效。
    """
    if not error_type or not message:
        return False
    if "chromadb" not in str(error_module or ""):
        return False
    return DIM_MISMATCH_PHRASE in str(message).lower()


def guard_verdict(metadata, expected_model, expected_dim):
    """純函式：集合的 metadata → (是否安全, 原因)。

    這是 C7 的核心 —— 「維度檢查」的替代品。規則很簡單：
    建立集合時把模型名與維度寫進去，開集合時比對。不比對就別用。

    回傳的 reason 一律是可以直接印給人看的字串，講「為什麼不安全」。
    """
    if metadata is None:
        return False, (
            "集合沒有任何 metadata —— 認不出它是哪個 embedding 模型建的。"
            "在只有一種模型、且從未換過的情況下這無害，但一旦換過模型，"
            "維度相同時不會有任何徵兆（見 C5/C6）。"
        )

    recorded_model = metadata.get(MODEL_METADATA_KEY)
    if recorded_model is None:
        return False, (
            "集合的 metadata 沒有 %s 這個鍵 —— 認不出是哪個模型建的。"
            % MODEL_METADATA_KEY
        )
    if recorded_model != expected_model:
        return False, (
            "集合是用 %r 建的，但現在設定的模型是 %r —— "
            "**不要直接寫入**。同維度的不同模型產生的向量不可互比，"
            "而 Chroma 不會有任何抱怨。" % (recorded_model, expected_model)
        )

    recorded_dim = metadata.get(DIM_METADATA_KEY)
    if recorded_dim is not None and recorded_dim != expected_dim:
        return False, (
            "集合記錄的維度是 %r，目前預期 %r —— 不一致。"
            % (recorded_dim, expected_dim)
        )
    return True, "集合記錄的模型與維度都與目前設定一致（%s=%s）" % (
        MODEL_METADATA_KEY,
        recorded_model,
    )


def grade(evidence, expected_dim=EXPECTED_DIM):
    """純函式：證據 → (passed, problems)。

    problems 每一項都是可以直接印給人看的字串 —— 講「哪裡沒過」，
    不是只說「沒過」。

    每一條判準的失敗訊息都刻意寫成「與 D-026 記錄的不符」，而不是
    「壞掉了」：這支探針量的是**上游的行為**，它變了不代表誰壞了，
    代表 item 2 的結論要重讀。
    """
    problems = []

    if not evidence:
        return False, ["沒有收到任何證據 —— 探針沒有跑到判準那一步"]

    emb = evidence.get("embedder") or {}
    alt = evidence.get("alt_model") or {}
    cfg = evidence.get("chroma_cfg") or {}
    col = evidence.get("collection") or {}
    mismatch = evidence.get("mismatch") or {}
    guard = evidence.get("guard") or {}
    cross = evidence.get("cross_model") or {}

    # ── C1：本專案的嵌入模型真的是 1024 維 ──────────────────
    # D-013 的重新確認。兩個來源都要對：直接問 ollama，以及經過 mem0 的
    # embedder 拿到的那個向量。後者多驗了一層 —— mem0 有沒有在中間動過手腳。
    if emb.get("vendor_dims") != expected_dim:
        problems.append(
            "C1：%s 直接向 ollama 取得的維度是 %r，預期 %d（D-013）。"
            "換了嵌入模型就要重新嵌入既有資料，不能只改設定。"
            % (emb.get("model"), emb.get("vendor_dims"), expected_dim)
        )
    if emb.get("vector_len") != expected_dim:
        problems.append(
            "C1：經過 mem0 的 OllamaEmbedding 拿到的向量長度是 %r，預期 %d —— "
            "兩者不同代表 mem0 在中間對向量做了處理。"
            % (emb.get("vector_len"), expected_dim)
        )

    # ── C2：mem0 的 chroma 設定沒有維度欄位 ─────────────────
    # 這條不是在挑毛病，是在確認 README 第 2 項的處方有沒有地方可下。
    # 哪天上游加了這個欄位，這條會叫 —— 那時「釘住維度」就真的可行了。
    if cfg.get("dim_fields"):
        problems.append(
            "C2：mem0 的 ChromaDbConfig 出現了維度相關欄位 %r —— "
            "與 D-026 記錄的「chroma 設定裡無處可釘」不符。"
            "上游若新增了這個欄位，README 第 2 項的處方要重新評估。"
            % (cfg.get("dim_fields"),)
        )

    # ── C3：README 指的 1536 不在這條路徑上，且宣告值是死的 ──
    declared = emb.get("declared_dims")
    if declared == README_CLAIMED_DIM:
        problems.append(
            "C3：mem0 的 ollama embedder 宣告值變成了 %d（README 說的那個數字）—— "
            "與 D-026 記錄的 512 不符，上游改了預設值。"
            % README_CLAIMED_DIM
        )
    if declared is None:
        problems.append(
            "C3：讀不到 OllamaEmbedding 的 embedding_dims 宣告值 —— "
            "屬性形狀變了，探針的假設要更新。"
        )
    elif emb.get("vector_len") is not None and emb.get("vector_len") != declared:
        # 宣告值與實際長度不同 → 宣告值是死的（沒有拿去截斷）。
        # 這**正是**D-026 記錄的行為，所以這裡通過。
        pass
    else:
        problems.append(
            "C3：宣告值 %r 與實際向量長度 %r 相等 —— 無法區分「宣告值恰好等於"
            "真實維度」與「宣告值真的被拿去截斷」。"
            "若上游開始拿它截斷向量，這裡會是第一個該重讀的地方。"
            % (declared, emb.get("vector_len"))
        )

    # ── C4：維度由第一次寫入鎖定，不符時會被擋 ──────────────
    if col.get("dim_before_write") is not None:
        problems.append(
            "C4：空集合在第一次寫入前就報出了維度 %r —— "
            "與 D-026 記錄的「寫入前無維度」不符。"
            % (col.get("dim_before_write"),)
        )
    if col.get("dim_after_write") != expected_dim:
        problems.append(
            "C4：寫入 %d 維向量後，集合回報的維度是 %r —— 不符。"
            % (expected_dim, col.get("dim_after_write"))
        )
    if not mismatch.get("raised"):
        problems.append(
            "C4：對 1024 維的集合寫入不同維度的向量，**沒有丟出任何例外** —— "
            "與 D-026 記錄的「會被擋」不符。這比原本的假設更危險："
            "寫入會靜默通過或靜默失敗。"
        )
    elif not dimension_error_recognised(
        mismatch.get("error_type"), mismatch.get("error_module"), mismatch.get("message")
    ):
        problems.append(
            "C4：維度不符確實丟了例外，但形狀認不出來 —— 型別 %r、模組 %r。"
            "D-026 記的判準是 chromadb 的 InvalidArgumentError 帶 %r。"
            % (
                mismatch.get("error_type"),
                mismatch.get("error_module"),
                DIM_MISMATCH_PHRASE,
            )
        )

    # ── C5：替代模型的維度相同 → 維度檢查擋不住換模型 ────────
    if alt.get("vendor_dims") is None:
        problems.append("C5：量不到替代模型 %r 的維度。" % (alt.get("model"),))
    elif alt.get("vendor_dims") != emb.get("vendor_dims"):
        problems.append(
            "C5：替代模型 %r 是 %r 維，與本專案的 %r 維**不同** —— "
            "與 D-026 記錄的「兩者同為 1024 維」不符。"
            "這其實是好消息（維度檢查就能擋下換模型），但 D-026 的結論要改。"
            % (alt.get("model"), alt.get("vendor_dims"), emb.get("vendor_dims"))
        )

    # ── C6：跨模型的空間不可互比（用無閾值的序關係判定）──────
    # 刻意不比「相似度低於某個門檻」—— 門檻會製造假失敗（D-016）。
    # 改比一個不需要門檻的序關係：
    #   跨模型的「同一句話」  <  同模型內的「兩句不相干的話」
    # 若前者比較遠，這兩個空間就不可能是在同一個座標系裡。
    cross_same = cross.get("cos_same_text_across_models")
    within_diff = cross.get("cos_within_model_different_texts")
    if cross_same is None or within_diff is None:
        problems.append("C6：跨模型或同模型的 cosine 算不出來（維度不符或向量為空）。")
    elif not cross_same < within_diff:
        problems.append(
            "C6：跨模型同一段文字的 cosine 是 %.4f，同模型內不相干文字的 cosine 是 %.4f —— "
            "前者**沒有**比較遠，與 D-026 記錄的不可互比不符。"
            "（若數字接近 1，代表兩個模型其實相容，那 D-026 的警告要放寬。）"
            % (cross_same, within_diff)
        )

    # ── C7：維度以外的偵測方式確實可行 ──────────────────────
    if not guard.get("metadata_survives"):
        problems.append(
            "C7：我們在建立集合時寫入的 metadata 沒有活過 mem0 的 create_col() —— "
            "D-026 提議的守門方式不成立，要換一種。"
        )
    if not guard.get("mem0_adopts"):
        problems.append(
            "C7：mem0 的 ChromaDB 沒有沿用我們預先建立的集合（讀不到它的 metadata）。"
        )
    if not guard.get("overwrite_rejected"):
        problems.append(
            "C7：對既有集合用不同 metadata 再 get_or_create，原值被蓋掉了 —— "
            "這個記錄不再是權威來源，守門邏輯會失效。"
        )
    if not guard.get("fires"):
        problems.append(
            "C7：模型名不符時守門邏輯**沒有叫** —— 那它擋不住 C5/C6 那個失敗。"
        )

    return (not problems), problems


def observations(evidence):
    """非致命觀察 —— 印給人看，不影響通過與否。

    檢索的 top1 刻意放在這裡而不是判準裡：五份文件裡挑一份是有可能碰巧挑對的，
    把它算成失敗就是 D-016 那種「假失敗」。真正的判準是 C6 那個無閾值的序關係，
    這裡只是把它操作上的後果攤開來給人看。
    """
    notes = []
    if not evidence:
        return notes
    cross = evidence.get("cross_model") or {}
    emb = evidence.get("embedder") or {}
    alt = evidence.get("alt_model") or {}
    col = evidence.get("collection") or {}

    notes.append(
        "維度：%s=%r（宣告 %r）、%s=%r、預期 %d"
        % (
            emb.get("model"),
            emb.get("vendor_dims"),
            emb.get("declared_dims"),
            alt.get("model"),
            alt.get("vendor_dims"),
            EXPECTED_DIM,
        )
    )
    notes.append(
        "集合維度：寫入前 %r、寫入後 %r、metadata %r"
        % (
            col.get("dim_before_write"),
            col.get("dim_after_write"),
            col.get("metadata_after_write"),
        )
    )
    if cross.get("top1_same_model") is not None:
        same_ok = cross.get("top1_same_model") == EXPECTED_DOC
        cross_ok = cross.get("top1_cross_model") == EXPECTED_DOC
        notes.append(
            "檢索 top1：同模型 %r（%s）、換模型 %r（%s）"
            % (
                cross.get("top1_same_model"),
                "正確" if same_ok else "**錯誤**",
                cross.get("top1_cross_model"),
                "正確" if cross_ok else "**錯誤，且沒有任何錯誤訊息**",
            )
        )
    if cross.get("cos_same_text_across_models") is not None:
        notes.append(
            "cosine：跨模型同一句話 %.4f、同模型兩句不相干的話 %.4f"
            % (
                cross["cos_same_text_across_models"],
                cross["cos_within_model_different_texts"],
            )
        )
    if cross.get("cos_same_text_across_models") is not None and \
       cross.get("cos_within_model_different_texts") is not None:
        if cross["cos_same_text_across_models"] < cross["cos_within_model_different_texts"]:
            notes.append(
                "→ 跨模型的「同一句話」比同模型內的「兩句不相干的話」還要遠："
                "兩個空間不在同一個座標系裡。"
            )
    return notes


# ══════════════════════════════════════════════════════════
# 連網區 —— 需要 mem0 / chromadb / ollama
#
# 這一區刻意**不在模組頂層 import**：頂層匯入會讓 test_chroma_dims_probe.py
# 在沒有這些套件的環境下直接失敗，而評分邏輯必須能離線測試（D-016）。
# ══════════════════════════════════════════════════════════


def ollama_embed(url, model, text):
    """直接向 ollama 要一個向量。**不經過 mem0** —— C1 要的是廠商端的真相。"""
    import urllib.request

    req = urllib.request.Request(
        url.rstrip("/") + "/api/embed",
        data=json.dumps({"model": model, "input": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode())
    vectors = payload.get("embeddings") or []
    if not vectors:
        raise ValueError("ollama 對 %r 回傳了空的 embeddings" % model)
    return vectors[0]


def measure_embedder(evidence, args):
    """M1/M3：本專案的嵌入模型，經 ollama 與經 mem0 兩條路各量一次。"""
    from mem0.configs.embeddings.base import BaseEmbedderConfig
    from mem0.embeddings.ollama import OllamaEmbedding

    emb = evidence["embedder"]
    emb["model"] = args.model
    emb["vendor_dims"] = len(ollama_embed(args.ollama_url, args.model, SAMPLE_TEXT))

    # mem0 的建構子會呼叫 _ensure_model_exists()，模型不存在時會自己 pull。
    # 這裡不擋 —— verify-chroma-dims.sh 已經先檢查過，重複擋只是多一層。
    mem0_emb = OllamaEmbedding(
        BaseEmbedderConfig(model=args.model, ollama_base_url=args.ollama_url)
    )
    emb["declared_dims"] = mem0_emb.config.embedding_dims
    emb["vector_len"] = len(mem0_emb.embed(SAMPLE_TEXT))


def measure_chroma_config(evidence):
    """M2：mem0 的 chroma 設定到底有哪些欄位。"""
    from mem0.configs.vector_stores.chroma import ChromaDbConfig

    fields = list(ChromaDbConfig.model_fields.keys())
    evidence["chroma_cfg"] = {
        "fields": fields,
        "dim_fields": [
            f for f in fields if "dim" in f.lower() or "embedding" in f.lower()
        ],
    }


def measure_collection(evidence, args, workdir):
    """M4：集合的維度從哪裡來、不符時長什麼樣子。"""
    import chromadb
    from chromadb.config import Settings

    from mem0.vector_stores.chroma import ChromaDB

    client = chromadb.Client(
        Settings(
            persist_directory=workdir,
            is_persistent=True,
            anonymized_telemetry=False,
        )
    )
    vector = ollama_embed(args.ollama_url, args.model, SAMPLE_TEXT)

    store = ChromaDB(collection_name="dim-probe", client=client)

    # 寫入前：空集合報不報得出維度？metadata 又是什麼？
    # 兩個都要用量的，不能寫死 —— 寫死的話「上游開始在建立時就填 metadata」
    # 這件事永遠不會被發現，而 C7 的守門邏輯正是建立在「建立時填、之後沿用」
    # 這個行為上。
    before = store.collection.get(limit=1, include=["embeddings"])
    emb_before = before.get("embeddings")
    dim_before = None
    if emb_before is not None and len(emb_before) > 0:
        dim_before = len(emb_before[0])
    metadata_before = store.collection.metadata

    store.insert(vectors=[vector], payloads=[{"t": "seed"}], ids=["seed"])

    after = store.collection.get(limit=1, include=["embeddings"])
    evidence["collection"] = {
        "dim_before_write": dim_before,
        "dim_after_write": len(after["embeddings"][0]),
        "metadata_before_write": metadata_before,
        "metadata_after_write": store.collection.metadata,
    }

    # 換一個長度不同的向量寫進去（模擬換了嵌入模型）—— 預期會被擋。
    wrong = [0.01] * (len(vector) // 2)
    try:
        store.insert(vectors=[wrong], payloads=[{"t": "wrong"}], ids=["wrong"])
        evidence["mismatch"] = {
            "raised": False,
            "attempted_dim": len(wrong),
            "error_type": None,
            "error_module": None,
            "message": None,
        }
    except Exception as e:  # noqa: BLE001 —— 這裡**就是要**接住它來記錄形狀
        evidence["mismatch"] = {
            "raised": True,
            "attempted_dim": len(wrong),
            "error_type": type(e).__name__,
            "error_module": type(e).__module__,
            "message": str(e),
        }


def measure_guard(evidence, args, workdir):
    """C7：維度以外的偵測方式可不可行。

    四件事要成立，缺一不可：
      1. 我們在建立集合時寫的 metadata 活得過 mem0 的 create_col()
      2. mem0 的 ChromaDB 讀得到它（沿用我們建的集合）
      3. 對既有集合用不同 metadata 再 get_or_create 蓋不掉原值
      4. 模型名不符時，guard_verdict() 真的會叫
    """
    import chromadb
    from chromadb.config import Settings

    from mem0.vector_stores.chroma import ChromaDB

    client = chromadb.Client(
        Settings(
            persist_directory=workdir,
            is_persistent=True,
            anonymized_telemetry=False,
        )
    )
    declared = {MODEL_METADATA_KEY: args.model, DIM_METADATA_KEY: EXPECTED_DIM}
    client.create_collection(
        name="guard-probe", embedding_function=None, metadata=declared
    )

    # (2) mem0 沿不沿用我們建的集合
    store = ChromaDB(collection_name="guard-probe", client=client)
    adopted = dict(store.collection.metadata or {})
    mem0_adopts = adopted == declared

    # (1) metadata 活下來了嗎
    metadata_survives = adopted.get(MODEL_METADATA_KEY) == args.model and adopted.get(
        DIM_METADATA_KEY
    ) == EXPECTED_DIM

    # (3) 用不同 metadata 再 get_or_create，蓋不蓋得掉
    client.get_or_create_collection(
        name="guard-probe",
        embedding_function=None,
        metadata={MODEL_METADATA_KEY: args.alt_model, DIM_METADATA_KEY: EXPECTED_DIM},
    )
    after = dict(client.get_collection("guard-probe").metadata or {})
    overwrite_rejected = after.get(MODEL_METADATA_KEY) == args.model

    # (4) 模型名不符時會不會叫
    ok_same, _ = guard_verdict(after, args.model, EXPECTED_DIM)
    ok_diff, _ = guard_verdict(after, args.alt_model, EXPECTED_DIM)
    fires = (not ok_diff) and ok_same

    evidence["guard"] = {
        "metadata_survives": metadata_survives,
        "mem0_adopts": mem0_adopts,
        "overwrite_rejected": overwrite_rejected,
        "fires": fires,
        "declared": declared,
        "adopted": adopted,
        "after_overwrite_attempt": after,
    }


def measure_cross_model(evidence, args, workdir):
    """M5/C6：同維度的兩個模型，空間可不可互比。"""
    import chromadb
    from chromadb.config import Settings

    emb_dims = evidence["embedder"].get("vendor_dims")
    alt_dims = len(ollama_embed(args.ollama_url, args.alt_model, SAMPLE_TEXT))
    evidence["alt_model"] = {"model": args.alt_model, "vendor_dims": alt_dims}

    doc_vectors = [ollama_embed(args.ollama_url, args.model, d) for d in DOCS]
    q_same = ollama_embed(args.ollama_url, args.model, QUERY)
    q_cross = ollama_embed(args.ollama_url, args.alt_model, QUERY)

    client = chromadb.Client(
        Settings(
            persist_directory=workdir,
            is_persistent=True,
            anonymized_telemetry=False,
        )
    )
    col = client.get_or_create_collection(name="cross-probe", embedding_function=None)
    col.add(
        ids=[str(i) for i in range(len(DOCS))],
        embeddings=doc_vectors,
        metadatas=[{"d": d} for d in DOCS],
    )
    r_same = col.query(query_embeddings=[q_same], n_results=1)
    r_cross = col.query(query_embeddings=[q_cross], n_results=1)

    same_text_a = ollama_embed(args.ollama_url, args.model, SAMPLE_TEXT)
    same_text_b = ollama_embed(args.ollama_url, args.alt_model, SAMPLE_TEXT)
    other_text_a = ollama_embed(args.ollama_url, args.model, DOCS[0])

    evidence["cross_model"] = {
        "same_model_dims": emb_dims,
        "alt_model_dims": alt_dims,
        "top1_same_model": r_same["metadatas"][0][0]["d"],
        "top1_cross_model": r_cross["metadatas"][0][0]["d"],
        "cos_same_text_across_models": cosine(same_text_a, same_text_b),
        "cos_within_model_different_texts": cosine(same_text_a, other_text_a),
    }


def run_probe(args):
    """跑完 M1~M5，回傳 (evidence, meta)。例外往上丟，由 main() 分類。"""
    evidence = {
        "expected_dim": EXPECTED_DIM,
        "embedder": {},
        "alt_model": {},
        "chroma_cfg": {},
        "collection": {},
        "mismatch": {},
        "guard": {},
        "cross_model": {},
    }
    meta = {}

    with tempfile.TemporaryDirectory(prefix="chroma-dims-") as workdir:
        print("── M1/M3：嵌入模型維度（ollama 直量 vs 經過 mem0）")
        measure_embedder(evidence, args)
        print(
            "   %s：ollama 直量 %r 維、mem0 embed() 回傳 %r 維、mem0 宣告 %r"
            % (
                args.model,
                evidence["embedder"].get("vendor_dims"),
                evidence["embedder"].get("vector_len"),
                evidence["embedder"].get("declared_dims"),
            )
        )

        print("── M2：mem0 的 ChromaDbConfig 欄位")
        measure_chroma_config(evidence)
        print("   %s" % (evidence["chroma_cfg"].get("fields"),))
        print(
            "   含維度語意的欄位：%s"
            % (evidence["chroma_cfg"].get("dim_fields") or "（無）")
        )

        print("── M4：集合維度怎麼決定、不符時長什麼樣")
        measure_collection(evidence, args, workdir)
        col = evidence["collection"]
        print(
            "   寫入前維度 %r、寫入後 %r、metadata %r"
            % (col.get("dim_before_write"), col.get("dim_after_write"), col.get("metadata_after_write"))
        )
        mm = evidence["mismatch"]
        if mm.get("raised"):
            print("   寫入 %r 維 → %s: %s" % (mm["attempted_dim"], mm["error_type"], mm["message"]))
        else:
            print("   寫入 %r 維 → **沒有丟出例外**" % (mm.get("attempted_dim"),))

        print("── M5：同維度的兩個模型能不能互比")
        measure_cross_model(evidence, args, workdir)
        cm = evidence["cross_model"]
        print(
            "   %s %r 維、%s %r 維"
            % (args.model, cm.get("same_model_dims"), args.alt_model, cm.get("alt_model_dims"))
        )
        print("   同模型檢索 top1：%r" % cm.get("top1_same_model"))
        print("   換模型檢索 top1：%r" % cm.get("top1_cross_model"))
        print(
            "   跨模型同句 cosine %.4f、同模型異句 cosine %.4f"
            % (
                cm.get("cos_same_text_across_models") or 0.0,
                cm.get("cos_within_model_different_texts") or 0.0,
            )
        )

        print("── C7：維度以外的偵測方式可不可行")
        measure_guard(evidence, args, workdir)
        g = evidence["guard"]
        print(
            "   metadata 存活 %s、mem0 沿用 %s、蓋不掉 %s、模型不符時會叫 %s"
            % (
                g.get("metadata_survives"),
                g.get("mem0_adopts"),
                g.get("overwrite_rejected"),
                g.get("fires"),
            )
        )

    return evidence, meta


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="第三階段 item 2 的探針")
    parser.add_argument("--model", default="qwen3-embedding:0.6b")
    parser.add_argument(
        "--alt-model",
        default="bge-m3:latest",
        help="用來示範「同維度但不可互比」的另一個模型",
    )
    parser.add_argument("--ollama-url", default="http://ollama:11434")
    parser.add_argument("--json", action="store_true", help="額外印出正規化證據")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    print("嵌入模型：%s" % args.model)
    print("替代模型：%s" % args.alt_model)
    print("ollama：%s" % args.ollama_url)
    print("預期維度：%d（D-013）" % EXPECTED_DIM)
    print()

    try:
        evidence, _ = run_probe(args)
    except ImportError as e:
        print("探針自己壞掉：匯入失敗（%s: %s）" % (type(e).__name__, e))
        print("  相依版本見 scripts/requirements-mem0.txt —— 不要放寬，先確認是哪一個套件變了。")
        return EXIT_BROKEN
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        text = str(e)
        # 連不上、模型不存在 → 無法判定（不是判準沒過）。其餘一律當成探針壞掉。
        markers = (
            "ConnectError",
            "ConnectionError",
            "ConnectTimeout",
            "URLError",
            "not found",
            "Connection refused",
            "All connection attempts failed",
        )
        if any(m in name or m in text for m in markers):
            print("無法判定：%s: %s" % (name, text))
            return EXIT_INDETERMINATE
        print("探針自己壞掉：%s: %s" % (name, text))
        return EXIT_BROKEN

    if args.json:
        print()
        print("── 正規化證據 ──────────────────────────────")
        print(json.dumps(evidence, ensure_ascii=False, indent=2, default=str))
        print()

    passed, problems = grade(evidence)

    print()
    print("── 判準 ────────────────────────────────────")
    if passed:
        print("通過：C1~C7 全過 —— 見下方「與 README 的差異」段。")
    else:
        print("未通過，問題如下：")
        for p in problems:
            print("  • %s" % p)
    print()
    print("── 觀察（不影響判定）───────────────────────")
    for note in observations(evidence):
        print("  • %s" % note)

    return EXIT_PASS if passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
