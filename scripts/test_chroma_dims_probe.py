#!/usr/bin/env python3
"""chroma_dims_probe.py 的離線單元測試（不連網、不需容器、不需 mem0/chromadb）。

這組測試對應兩條既有教訓：

1. **評分邏輯本身要被實測過**（README 第一階段清單、D-016）。一個只跑過
   「輸出為空」的評分器，在第一次真正跑到之前沒有任何防線。
2. **假失敗比漏報更糟**（D-016），而**漏報比誤報更難發現**（D-024 第八節）。
   所以這裡**兩邊都驗**：每一條判準 C1~C7 都有一個「該擋的擋」測資 ——
   把對應的欄位改成壞值，`grade()` 必須叫。少了那一半，評分器會在最需要
   它的那一刻才第一次運作。

還有一條是 D-024 第九節直接點名的：**用會回傳你期待答案的查法，就會得到你
期待的答案。** 所以 `dimension_error_recognised()` 有一個測資專門餵它一句
**含有 `dimension` 這個字、但語意相反**的訊息，確認它不會放行。

執行：python3 scripts/test_chroma_dims_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chroma_dims_probe as p  # noqa: E402


def _evidence_ok():
    """2026-09-20 在真環境量到的形狀（見 D-026）。這是對照組。"""
    return {
        "expected_dim": 1024,
        "embedder": {
            "model": "qwen3-embedding:0.6b",
            "vendor_dims": 1024,
            "declared_dims": 512,
            "vector_len": 1024,
        },
        "alt_model": {"model": "bge-m3:latest", "vendor_dims": 1024},
        "chroma_cfg": {
            "fields": [
                "collection_name", "client", "path", "host", "port",
                "api_key", "tenant",
            ],
            "dim_fields": [],
        },
        "collection": {
            "dim_before_write": None,
            "dim_after_write": 1024,
            "metadata_before_write": None,
            "metadata_after_write": None,
        },
        "mismatch": {
            "raised": True,
            "attempted_dim": 512,
            "error_type": "InvalidArgumentError",
            "error_module": "chromadb.errors",
            "message": "Collection expecting embedding with dimension of 1024, got 512",
        },
        "guard": {
            "metadata_survives": True,
            "mem0_adopts": True,
            "overwrite_rejected": True,
            "fires": True,
        },
        "cross_model": {
            "same_model_dims": 1024,
            "alt_model_dims": 1024,
            "top1_same_model": p.EXPECTED_DOC,
            "top1_cross_model": "汽車需要定期保養輪胎",
            "cos_same_text_across_models": -0.0216,
            "cos_within_model_different_texts": 0.4036,
        },
    }


def _fails(evidence, must_mention=None):
    """輔助：斷言 grade() 會擋，且問題描述裡提到某個字串。

    回傳問題清單，讓呼叫端可以再進一步檢查。
    """
    passed, problems = p.grade(evidence)
    assert passed is False, "這組證據應該要被擋下來，卻通過了"
    assert problems, "被擋下來卻沒有任何問題描述 —— report 會印不出原因"
    for one in problems:
        assert isinstance(one, str) and one.strip(), "問題描述必須是可直讀的字串"
    if must_mention is not None:
        assert any(must_mention in x for x in problems), (
            "問題描述裡沒有提到 %r，實際為：%r" % (must_mention, problems)
        )
    return problems


def _set(evidence, path, value):
    """依 'a.b' 路徑設定欄位（複製一份，不動原物件）。"""
    import copy

    out = copy.deepcopy(evidence)
    head, _, tail = path.partition(".")
    out[head][tail] = value
    return out


# ══════════════════════════════════════════════════════════
# 對照組：量到的形狀必須通過
# ══════════════════════════════════════════════════════════

_passed, _problems = p.grade(_evidence_ok())
assert _passed is True, "對照組沒過，問題：%r" % (_problems,)
assert _problems == []
assert isinstance(p.observations(_evidence_ok()), list)

# 空證據不可被當成通過
assert p.grade({})[0] is False
assert p.grade(None)[0] is False
assert p.observations({}) == []


# ══════════════════════════════════════════════════════════
# C1：本專案的嵌入模型真的是 1024 維
# ══════════════════════════════════════════════════════════

_fails(_set(_evidence_ok(), "embedder.vendor_dims", 768), "C1")
# 經過 mem0 拿到的長度也要對 —— 這一條抓的是「mem0 在中間動手腳」
_fails(_set(_evidence_ok(), "embedder.vector_len", 512), "C1")
_fails(_set(_evidence_ok(), "embedder.vendor_dims", None), "C1")


# ══════════════════════════════════════════════════════════
# C2：mem0 的 chroma 設定沒有維度欄位
# ══════════════════════════════════════════════════════════

# 上游哪天新增了維度欄位，這條要叫 —— 代表 README 的處方變得可行了
_fails(_set(_evidence_ok(), "chroma_cfg.dim_fields", ["embedding_dims"]), "C2")


# ══════════════════════════════════════════════════════════
# C3：README 指的 1536 不在這條路徑上，且宣告值是死的
# ══════════════════════════════════════════════════════════

# 宣告值變成 README 說的那個數字 → 上游改了預設
_fails(_set(_evidence_ok(), "embedder.declared_dims", 1536), "C3")

# 讀不到宣告值 → 屬性形狀變了
_fails(_set(_evidence_ok(), "embedder.declared_dims", None), "C3")

# **關鍵的一條**：宣告值等於實際向量長度時，就分不出「宣告值恰好等於真實
# 維度」與「宣告值真的被拿去截斷」。這時必須叫 —— 否則上游開始拿它截斷
# 向量時，這一條會靜默地繼續通過。
_fails(_set(_evidence_ok(), "embedder.declared_dims", 1024), "C3")


# ══════════════════════════════════════════════════════════
# C4：維度由第一次寫入鎖定，不符時會被擋
# ══════════════════════════════════════════════════════════

_fails(_set(_evidence_ok(), "collection.dim_before_write", 1024), "C4")
_fails(_set(_evidence_ok(), "collection.dim_after_write", 512), "C4")

# 沒有丟出例外 —— 比原本的假設更危險的那種結果
_fails(_set(_evidence_ok(), "mismatch.raised", False), "沒有丟出任何例外")

# 丟了例外但形狀認不出來。**這與「沒丟例外」是兩件不同的事**，
# 訊息必須分辨得出來，否則會把「探針的判準過期了」誤讀成「受測對象變了」。
_fails(_set(_evidence_ok(), "mismatch.error_module", "builtins"), "認不出來")
_fails(_set(_evidence_ok(), "mismatch.message", "something else entirely"), "認不出來")


# ══════════════════════════════════════════════════════════
# C5：替代模型維度相同 → 維度檢查擋不住換模型
# ══════════════════════════════════════════════════════════

# **這兩條必須分辨得出來**，這是突變測試實測抓到的洞（第一版只斷言「C5
# 有叫」，於是 `if alt.get("vendor_dims") is None:` 被換成 `if False:` 之後
# 測試照樣通過 —— 因為它落進了下面那個「維度不同」的分支）。
# 「量不到」是量測儀器的問題，「維度不同」是世界變了，兩者的處置完全不同。
_fails(_set(_evidence_ok(), "alt_model.vendor_dims", None), "量不到")
# 兩個模型維度不同其實是好消息，但 D-026 的結論要改 —— 必須叫
_fails(_set(_evidence_ok(), "alt_model.vendor_dims", 768), "不同")


# ══════════════════════════════════════════════════════════
# C6：跨模型的空間不可互比（無閾值的序關係）
# ══════════════════════════════════════════════════════════

_fails(_set(_evidence_ok(), "cross_model.cos_same_text_across_models", None), "C6")
_fails(_set(_evidence_ok(), "cross_model.cos_within_model_different_texts", None), "C6")

# 序關係反過來（跨模型反而更近）→ 不可互比的結論不成立
_fails(_set(_evidence_ok(), "cross_model.cos_same_text_across_models", 0.9), "C6")

# **相等也要叫**。這一條抓的是 `<` 被寫成 `<=` 之類的邊界漂移 ——
# 相等時「沒有比較遠」，那就不該算通過。
_equal = _set(_evidence_ok(), "cross_model.cos_same_text_across_models", 0.4036)
_fails(_equal, "C6")


# ══════════════════════════════════════════════════════════
# C7：維度以外的偵測方式確實可行
# ══════════════════════════════════════════════════════════

for _field in ("metadata_survives", "mem0_adopts", "overwrite_rejected", "fires"):
    _fails(_set(_evidence_ok(), "guard." + _field, False), "C7")


# ══════════════════════════════════════════════════════════
# dimension_error_recognised()：三條件缺一不可
# ══════════════════════════════════════════════════════════

_ok = ("InvalidArgumentError", "chromadb.errors",
       "Collection expecting embedding with dimension of 1024, got 512")
assert p.dimension_error_recognised(*_ok) is True

# 大小寫不影響
assert p.dimension_error_recognised(
    "InvalidArgumentError", "chromadb.errors",
    "COLLECTION EXPECTING EMBEDDING WITH DIMENSION OF 1024, GOT 512") is True

# 模組不對 → 不是 chromadb 丟的，不算
assert p.dimension_error_recognised(
    "InvalidArgumentError", "builtins", _ok[2]) is False
assert p.dimension_error_recognised(_ok[0], None, _ok[2]) is False

# **D-024 第九節那一條**：訊息裡有 `dimension` 這個字，但語意相反。
# 用 `"dimension" in message` 當判準的寫法會在這裡放行 —— 這正是要抓的洞。
assert p.dimension_error_recognised(
    "ValueError", "chromadb.errors",
    "dimension check disabled; wrote 512 into 1024 collection") is False

assert p.dimension_error_recognised(None, None, None) is False
assert p.dimension_error_recognised("E", "chromadb.errors", "") is False
assert p.dimension_error_recognised("E", "chromadb.errors", None) is False


# ══════════════════════════════════════════════════════════
# guard_verdict()：維度以外的偵測方式
# ══════════════════════════════════════════════════════════

_meta = {"embedding_model": "qwen3-embedding:0.6b", "embedding_dim": 1024}

_ok_v, _reason = p.guard_verdict(_meta, "qwen3-embedding:0.6b", 1024)
assert _ok_v is True and isinstance(_reason, str)

# 模型名不符 —— 這是 C5/C6 那個失敗的唯一防線，必須叫
_bad_v, _bad_reason = p.guard_verdict(_meta, "bge-m3:latest", 1024)
assert _bad_v is False, "模型名不符時守門邏輯必須叫"
assert "bge-m3:latest" in _bad_reason and "qwen3-embedding:0.6b" in _bad_reason

# 沒有 metadata／沒有鍵 → 不算安全（認不出來就別用）
assert p.guard_verdict(None, "m", 1024)[0] is False
assert p.guard_verdict({}, "m", 1024)[0] is False

# 記錄的維度與預期不符
assert p.guard_verdict(
    {"embedding_model": "m", "embedding_dim": 512}, "m", 1024)[0] is False

# 只有模型名、沒有維度 → 仍算安全（維度是加分項，不是必要條件）
assert p.guard_verdict({"embedding_model": "m"}, "m", 1024)[0] is True


# ══════════════════════════════════════════════════════════
# cosine()：把「算不出來」與「相似度為 0」分開
# ══════════════════════════════════════════════════════════

assert abs(p.cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
assert abs(p.cosine([1.0, 0.0], [0.0, 1.0]) - 0.0) < 1e-9
assert abs(p.cosine([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-9

# 正交回 0.0（合法的相似度），維度不符回 None —— 兩者不可混為一談
assert p.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
assert p.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) is None
assert p.cosine([], []) is None
assert p.cosine(None, [1.0]) is None
assert p.cosine([0.0, 0.0], [1.0, 1.0]) is None


# ══════════════════════════════════════════════════════════
# observations()：對殘缺證據不可爆炸
# ══════════════════════════════════════════════════════════

for _partial in (
    {},
    {"embedder": {}, "alt_model": {}, "collection": {}, "cross_model": {}},
    {"cross_model": {"top1_same_model": "x"}},
    _evidence_ok(),
):
    _notes = p.observations(_partial)
    assert isinstance(_notes, list)
    for _n in _notes:
        assert isinstance(_n, str) and _n.strip()

# 檢索結果正確與否要看得出來（這是給人看的操作面後果）
_notes = p.observations(_evidence_ok())
assert any("檢索 top1" in n for n in _notes)
assert any("錯誤" in n for n in _notes), "換模型回錯文件時，觀察段必須寫出來"

print("✓ chroma_dims_probe.py 純函式測試全數通過（C1~C7 每條都有『該擋的擋』測資）")
