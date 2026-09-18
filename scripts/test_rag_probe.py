#!/usr/bin/env python3
"""rag_probe.py 的離線測試：不需要 Ollama、不需要網路、不需要容器。

為什麼要測評分邏輯本身：
  這個探針的用途是「用數字決定要不要採用某個嵌入模型」。如果評分邏輯錯了，
  它會回報一組看起來很合理的數字，而我們會照著做決定 —— 這是本專案已經
  吃過兩次虧的失敗模式（D-011）。所以「量測工具本身」必須先被量測。

做法：把 embed() 換成建構出來的假向量，讓答案已知，再檢查評分是否
      得到那個已知答案。三種假模型分別代表三種真實結果：
        perfect      —— 完全正確的模型
        topic_only   —— 只比對主題、不會分辨屬性的模型（RAG 最常見的失敗）
        simp_biased  —— 簡體很準、繁體全錯的模型（本探針存在的理由）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rag_probe as r  # noqa: E402

FAILURES = []


def check(condition, label):
    if condition:
        print(f"  ✓ {label}")
    else:
        print(f"  ✗ {label}")
        FAILURES.append(label)


# ── 測試資料的反查表 ───────────────────────────────────────
# role: 0=查詢 1=正解 2=干擾項
def _index():
    idx = {}
    for i, item in enumerate(r.ITEMS):
        for variant in ("trad", "simp"):
            v = item[variant]
            idx[v["q"]] = (variant, i, 0)
            idx[v["correct"]] = (variant, i, 1)
            for d in v["distractors"]:
                idx[d] = (variant, i, 2)
    return idx


IDX = _index()
N = len(r.ITEMS)
_PREFIXES = ("search_query: ", "search_document: ", "query: ", "passage: ")


def _strip(text):
    for p in _PREFIXES:
        if text.startswith(p):
            return text[len(p):]
    return text


def make_stub(mode):
    """建構假向量。維度配置：前 N 維是主題訊號，後面幾維是角色訊號。

    主題訊號讓同一個題目的四份文件彼此靠近（模擬「主題相同」），
    角色訊號才是決定名次的關鍵（模擬「屬性不同」）。
    """

    def stub(model, texts):
        vectors = []
        for text in texts:
            key = _strip(text)
            found = IDX.get(key)
            vec = [0.0] * (N + 4)
            if found is None:
                # 不在題庫裡的字串 —— 回傳零向量，讓它在評分中被忽略
                vectors.append(vec)
                continue

            variant, i, role = found
            vec[i] = 0.3  # 主題訊號：同題共用

            if mode == "perfect":
                # 查詢與正解同方向 → 相似度 1.0；干擾項在另一個方向
                vec[N] = 1.0 if role in (0, 1) else 0.0
                if role == 2:
                    vec[N + 1] = 1.0
            elif mode == "topic_only":
                # 只有主題訊號，屬性或然；正解系統性落後干擾項
                vec[N] = 1.0 if role == 0 else (0.0 if role == 1 else 2.0)
            elif mode == "simp_biased":
                if variant == "simp":
                    vec[N] = 1.0 if role in (0, 1) else 0.0
                    if role == 2:
                        vec[N + 1] = 1.0
                else:
                    vec[N] = 1.0 if role == 0 else (0.0 if role == 1 else 2.0)
            else:
                raise ValueError(f"未知的假模型模式：{mode}")

            vectors.append(vec)
        return vectors

    return stub


def run_with(mode):
    original = r.embed
    r.embed = make_stub(mode)
    try:
        results = {}
        for variant in ("trad", "simp"):
            _, stats = r.evaluate("fake-model", variant, "", "")
            results[variant] = stats
        return results
    finally:
        r.embed = original


# ── 開始 ───────────────────────────────────────────────────
print("rag_probe.py 離線測試")

print("\n[1] cosine 相似度")
check(abs(r.cosine([1, 0], [1, 0]) - 1.0) < 1e-9, "同向量 = 1.0")
check(abs(r.cosine([1, 0], [0, 1])) < 1e-9, "正交向量 = 0.0")
check(abs(r.cosine([1, 0], [-1, 0]) + 1.0) < 1e-9, "反向向量 = -1.0")
check(r.cosine([0, 0], [1, 0]) == 0.0, "零向量不除以零")
check(
    abs(r.cosine([3, 4], [6, 8]) - 1.0) < 1e-9,
    "未正規化的向量仍得到正確的 cosine（Ollama 不保證正規化）",
)

print("\n[2] 指令前綴對應")
check(r.prefixes_for("nomic-embed-text") == ("search_query: ", "search_document: "),
      "nomic-embed-text 有前綴")
check(r.prefixes_for("nomic-embed-text:latest") == ("search_query: ", "search_document: "),
      "帶 tag 的模型名也能對到（切成 : 前的部分）")
check(r.prefixes_for("multilingual-e5-large") == ("query: ", "passage: "),
      "e5 家族用 query/passage")
check(r.prefixes_for("bge-m3") == ("", ""), "bge-m3 不需要前綴")
check(r.prefixes_for("某個沒聽過的模型") == ("", ""), "未知模型回退為無前綴")

print("\n[3] 題庫自我檢查（雙向）")
try:
    r._check_test_data()
    check(True, "原始題庫通過檢查")
except SystemExit:
    check(False, "原始題庫通過檢查")

_saved = r.ITEMS[0]["trad"]["correct"]
r.ITEMS[0]["trad"]["correct"] = "台灣高鐵商務車廂台北至左營的票价为 2450 元。"
try:
    r._check_test_data()
    check(False, "注入簡體字必須被抓到")
except SystemExit:
    check(True, "注入簡體字必須被抓到")
finally:
    r.ITEMS[0]["trad"]["correct"] = _saved

_saved_simp = r.ITEMS[0]["simp"]["q"]
r.ITEMS[0]["simp"]["q"] = r.ITEMS[0]["trad"]["q"]  # 兩份寫成一樣 = 漏改
try:
    r._check_test_data()
    check(False, "繁體與簡體完全相同必須被抓到")
except SystemExit:
    check(True, "繁體與簡體完全相同必須被抓到")
finally:
    r.ITEMS[0]["simp"]["q"] = _saved_simp

print("\n[4] 完美模型 → 應全對且無落差")
perfect = run_with("perfect")
check(perfect["trad"]["top1"] == 1.0, f"繁體 Top-1 = 100%（實得 {perfect['trad']['top1']:.0%}）")
check(perfect["simp"]["top1"] == 1.0, "簡體 Top-1 = 100%")
check(abs(perfect["trad"]["mrr"] - 1.0) < 1e-9, "MRR = 1.00")
check(perfect["trad"]["margin"] > 0, "正解領先幅度為正")

print("\n[5] 只比對主題的模型 → 應貼近随機，且分數必須低")
topic = run_with("topic_only")
check(topic["trad"]["top1"] == 0.0,
      f"繁體 Top-1 = 0%（實得 {topic['trad']['top1']:.0%}）—— 這是最關鍵的一項："
      "評分若無法區分好模型與壞模型，這個探針就沒有用")
check(topic["trad"]["top3"] == 0.0, "Top-3 也是 0%")
check(topic["trad"]["margin"] < 0, "正解領先幅度為負（被干擾項壓過）")

print("\n[6] 簡體偏差模型 → 落差必須大到觸發偏差判讀")
biased = run_with("simp_biased")
gap = biased["simp"]["top1"] - biased["trad"]["top1"]
check(gap == 1.0, f"落差 = 100%（實得 {gap:+.0%}）")
check(biased["trad"]["top1"] == 0.0, "繁體全錯")
check(biased["simp"]["top1"] == 1.0, "簡體全對")

print("\n[7] 判讀邏輯：同樣是「繁體低分」，原因不同就該給不同結論")
# 直接呼叫 rag_probe 的 verdict_for，不另外抄一份。
# 抄一份等於沒測 —— 真正在跑的那份可以壞掉，測試照樣通過。


def v(trad, simp):
    return r.verdict_for(trad, simp)[0]


check("足夠" in v(1.0, 1.0), "好模型 → 足夠")
check("偏差" in v(0.0, 1.0), "簡體全對、繁體全錯 → 判為偏差")
check(
    "偏差" in v(0.5, 1.0),
    "簡體很好、繁體一半 → 判為偏差（這才是最典型的偏差樣態）",
)
check("隨機" in v(0.0, 0.0), "兩邊都爛 → 判為隨機，而非偏差")
check(
    "隨機" in v(0.0, 0.25),
    "落差 2 題但簡體本身也爛 → 判為隨機，"
    "不可把「模型不好」誤診成「簡體偏差」",
)
check("人工判讀" in v(0.75, 0.875), "小幅落差 → 交由人工判讀")
check("足夠" in v(0.875, 1.0), "差距 1 題但繁體已達標 → 足夠")

# 落差必須真的傳回來，不能只回傳字串
check(r.verdict_for(0.5, 1.0)[1] == 0.5, "verdict_for 一併回傳落差")

print()
if FAILURES:
    print(f"✗ {len(FAILURES)} 項未通過：")
    for f in FAILURES:
        print(f"    • {f}")
    sys.exit(1)
print("✓ rag_probe.py 離線測試全數通過")
