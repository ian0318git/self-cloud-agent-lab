#!/usr/bin/env python3
"""以「繁體 vs 簡體」的對照實驗，測出嵌入模型對繁體中文的鑑別力。

為什麼要自己量，而不是查排行榜：
  MTEB / C-MTEB 的分數幾乎都來自簡體中文的資料集，而本專案的文件是繁體。
  「簡體訓練的模型能不能處理繁體」這個問題，排行榜沒有回答。
  本專案已經兩次把推論寫成結論（見 D-011），不應該再靠推論決定這件事。

實驗設計：
  同一組題目寫成兩份 —— 一份全繁體、一份全簡體，語意完全平行。
  兩份各自做檢索，比較準確率。**落差就是答案。**
  若繁體那份的準確率明顯低於簡體那份，代表模型對繁體有偏差。
  只測繁體而不測簡體是沒有意義的：準確率低可能是題目太難，
  有對照組才能把「模型偏差」和「題目太難」分開。

題目的難度刻意調高：
  每一題的四份文件**主題完全相同**，只差一個屬性
  （商務艙 vs 標準艙、住院 vs 門診、三奈米 vs 五奈米）。
  隨機猜的期望值是 25%。只會比對主題、不會比對屬性的模型，
  準確率會貼著 25% —— 那正是 RAG 最常見的失敗模式。
  題目若太簡單，所有模型都會滿分，這個實驗就變成無效的假通過。

用法（見 scripts/rag_probe.sh，需在容器內執行）：
  python3 rag_probe.py bge-m3 nomic-embed-text
"""

import json
import math
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")

# 部分嵌入模型需要指令前綴，查詢與文件用的還不一樣。
# 少了前綴不至於壞掉，但會明顯拉低分數 —— 而那個低分會被誤讀成
# 「這個模型不好」。因此前綴必須跟著模型走，並且在輸出中顯示出來。
PREFIXES = {
    "nomic-embed-text": ("search_query: ", "search_document: "),
    "nomic-embed-text-v2-moe": ("search_query: ", "search_document: "),
    "multilingual-e5": ("query: ", "passage: "),
    "multilingual-e5-large": ("query: ", "passage: "),
    "multilingual-e5-small": ("query: ", "passage: "),
    "multilingual-e5-base": ("query: ", "passage: "),
}
NO_PREFIX = ("", "")


def prefixes_for(model: str) -> tuple:
    """回傳 (查詢前綴, 文件前綴)。找不到就回傳無前綴。"""
    base = model.split(":")[0]
    return PREFIXES.get(base, NO_PREFIX)


# ── 題庫 ────────────────────────────────────────────────────
# 台灣用語優先（奈米而非纳米、資訊而非信息），因為台灣用語本身
# 就是繁體文件的特徵之一，順帶測出模型對這類詞彙的掌握。
ITEMS = [
    {
        "id": "thsr-fare",
        "trad": {
            "q": "高鐵商務車廂從台北到左營的票價是多少？",
            "correct": "台灣高鐵商務車廂台北至左營的票價為 2450 元。",
            "distractors": [
                "台灣高鐵標準車廂台北至左營的票價為 1490 元。",
                "台灣高鐵商務車廂台北至台中的票價為 1120 元。",
                "台灣高鐵標準車廂台北至台中的票價為 700 元。",
            ],
        },
        "simp": {
            "q": "高铁商务车厢从台北到左营的票价是多少？",
            "correct": "台湾高铁商务车厢台北至左营的票价为 2450 元。",
            "distractors": [
                "台湾高铁标准车厢台北至左营的票价为 1490 元。",
                "台湾高铁商务车厢台北至台中的票价为 1120 元。",
                "台湾高铁标准车厢台北至台中的票价为 700 元。",
            ],
        },
    },
    {
        "id": "nhi-copay",
        "trad": {
            "q": "全民健保住院的部分負擔上限是多少？",
            "correct": "全民健康保險住院的部分負擔，單次住院上限為 41000 元。",
            "distractors": [
                "全民健康保險門診的部分負擔，單次上限為 300 元。",
                "全民健康保險住院的部分負擔，單次住院上限為 21000 元。",
                "全民健康保險的保險費率為 5.17%。",
            ],
        },
        "simp": {
            "q": "全民健保住院的部分负担上限是多少？",
            "correct": "全民健康保险住院的部分负担，单次住院上限为 41000 元。",
            "distractors": [
                "全民健康保险门诊的部分负担，单次上限为 300 元。",
                "全民健康保险住院的部分负担，单次住院上限为 21000 元。",
                "全民健康保险的保险费率为 5.17%。",
            ],
        },
    },
    {
        "id": "tsmc-node",
        "trad": {
            "q": "台積電三奈米製程在哪一年量產？",
            "correct": "台積電的三奈米製程於 2022 年進入量產。",
            "distractors": [
                "台積電的五奈米製程於 2020 年進入量產。",
                "台積電的七奈米製程於 2018 年進入量產。",
                "台積電的兩奈米製程預計於 2025 年進入量產。",
            ],
        },
        "simp": {
            "q": "台积电三纳米制程在哪一年量产？",
            "correct": "台积电的三纳米制程于 2022 年进入量产。",
            "distractors": [
                "台积电的五纳米制程于 2020 年进入量产。",
                "台积电的七纳米制程于 2018 年进入量产。",
                "台积电的两纳米制程预计于 2025 年进入量产。",
            ],
        },
    },
    {
        "id": "typhoon-closure",
        "trad": {
            "q": "颱風來襲時，什麼情況下會停止上班上課？",
            "correct": "颱風期間，若該縣市風力預測達停班停課標準，會由縣市政府宣布停止上班上課。",
            "distractors": [
                "颱風期間，若該縣市雨量預測達淹水警戒，會由縣市政府宣布停止上班上課。",
                "颱風期間，若該縣市風力預測達停班停課標準，僅有學校會停止上課，公務機關仍須上班。",
                "颱風期間的停班停課標準由中央氣象署統一宣布，縣市政府不得自行決定。",
            ],
        },
        "simp": {
            "q": "台风来袭时，什么情况下会停止上班上课？",
            "correct": "台风期间，若该县市风力预测达停班停课标准，会由县市政府宣布停止上班上课。",
            "distractors": [
                "台风期间，若该县市雨量预测达淹水警戒，会由县市政府宣布停止上班上课。",
                "台风期间，若该县市风力预测达停班停课标准，仅有学校会停止上课，公务机关仍须上班。",
                "台风期间的停班停课标准由中央气象署统一宣布，县市政府不得自行决定。",
            ],
        },
    },
    {
        "id": "passport-renewal",
        "trad": {
            "q": "護照效期剩下多久以內可以申請換發？",
            "correct": "護照效期不足六個月時，可以申請換發新護照。",
            "distractors": [
                "護照效期不足一年時，可以申請換發新護照。",
                "護照簽證頁用完時，可以申請換發新護照。",
                "護照遺失時，應先向警察機關報案，再申請補發。",
            ],
        },
        "simp": {
            "q": "护照效期剩下多久以内可以申请换发？",
            "correct": "护照效期不足六个月时，可以申请换发新护照。",
            "distractors": [
                "护照效期不足一年时，可以申请换发新护照。",
                "护照签证页用完时，可以申请换发新护照。",
                "护照遗失时，应先向警察机关报案，再申请补发。",
            ],
        },
    },
    {
        "id": "scooter-licence",
        "trad": {
            "q": "普通重型機車駕照可以騎乘多大的機車？",
            "correct": "普通重型機車駕照可以騎乘排氣量 50 至 250 毫升的機車。",
            "distractors": [
                "大型重型機車駕照可以騎乘排氣量 250 毫升以上的機車。",
                "普通輕型機車駕照可以騎乘排氣量 50 毫升以下的機車。",
                "普通重型機車駕照須年滿十八歲才能考領。",
            ],
        },
        "simp": {
            "q": "普通重型机车驾照可以骑乘多大的机车？",
            "correct": "普通重型机车驾照可以骑乘排气量 50 至 250 毫升的机车。",
            "distractors": [
                "大型重型机车驾照可以骑乘排气量 250 毫升以上的机车。",
                "普通轻型机车驾照可以骑乘排气量 50 毫升以下的机车。",
                "普通重型机车驾照须年满十八岁才能考领。",
            ],
        },
    },
    {
        "id": "rent-deposit",
        "trad": {
            "q": "租房子時，押金依法最多可以收幾個月？",
            "correct": "依土地法規定，租屋押金不得超過兩個月的租金。",
            "distractors": [
                "依土地法規定，租屋押金不得超過一個月的租金。",
                "依土地法規定，租屋定金不得超過兩個月的租金。",
                "依土地法規定，租屋押金應於租約到期後立即無息返還。",
            ],
        },
        "simp": {
            "q": "租房子时，押金依法最多可以收几个月？",
            "correct": "依土地法规定，租屋押金不得超过两个月的租金。",
            "distractors": [
                "依土地法规定，租屋押金不得超过一个月的租金。",
                "依土地法规定，租屋定金不得超过两个月的租金。",
                "依土地法规定，租屋押金应于租约到期后立即无息返还。",
            ],
        },
    },
    {
        "id": "income-tax-deduction",
        "trad": {
            "q": "綜合所得稅的標準扣除額是多少？",
            "correct": "綜合所得稅的標準扣除額，單身者為 131000 元。",
            "distractors": [
                "綜合所得稅的標準扣除額，已婚合併申報者為 262000 元。",
                "綜合所得稅的薪資所得特別扣除額上限為 218000 元。",
                "綜合所得稅的列舉扣除額包含捐贈、保險費與醫療費。",
            ],
        },
        "simp": {
            "q": "综合所得税的标准扣除额是多少？",
            "correct": "综合所得税的标准扣除额，单身者为 131000 元。",
            "distractors": [
                "综合所得税的标准扣除额，已婚合并申报者为 262000 元。",
                "综合所得税的薪资所得特别扣除额上限为 218000 元。",
                "综合所得税的列举扣除额包含捐赠、保险费与医疗费。",
            ],
        },
    },
]

# 只收「與繁體字形不同」的簡體專用字。
#
# 這份清單踩過一次坑：第一版把「遺失」「護照」「騎乘」整串寫進來，
# 於是 失、照、乘 也被當成簡體字 —— 但它們在繁體裡就是這樣寫的。
# 結果是自我檢查會把正確的繁體題庫判成錯誤。清單的每個字都必須
# 逐字確認過「繁體用的是別的字」，不能靠整串詞彙直覺帶入。
# 同理，「准」（標準／标准）「台」「里」「云」「后」這類
# 兩體皆合法的字不可納入 —— 那會造成大量誤判。
SIMPLIFIED_ONLY = set(
    "铁务车厢从价营湾标负担数单为门诊疗险费积电纳产于进两预计"
    "风来袭时么况会课该县测达标仅学关须决护内请换发个签证券页"
    "遗应报补车驾骑气轻满岁领规过约后无还综税额并资举赠与医疗费"
)


def _check_test_data():
    """題庫本身錯了，實驗結果就沒有意義 —— 所以在跑之前先驗題庫。

    這不是形式主義：繁體/簡體兩份是手寫的，很容易在複製貼上時漏改，
    結果是「兩份其實都是繁體」或「繁體那份混進簡體字」，
    而實驗照跑不誤、照樣印出一組看起來合理的數字。
    """
    problems = []

    for item in ITEMS:
        for variant in ("trad", "simp"):
            if variant not in item:
                problems.append(f"{item['id']}：缺少 {variant} 版本")
                continue
            v = item[variant]
            texts = [v["q"], v["correct"]] + v["distractors"]
            if len(v["distractors"]) != 3:
                problems.append(f"{item['id']}/{variant}：干擾項應為 3 份")
            for text in texts:
                if len(text) < 10:
                    problems.append(f"{item['id']}/{variant}：文字過短，檢索無意義：{text!r}")

            # 繁體版本不得出現簡體字
            if variant == "trad":
                hits = sorted(set("".join(texts)) & SIMPLIFIED_ONLY)
                if hits:
                    problems.append(
                        f"{item['id']}/trad：繁體版本出現簡體字 {''.join(hits)}"
                    )

        # 兩份必須真的不同 —— 相同就代表漏改，實驗等於只測了一份
        t_texts = [item["trad"]["q"]] + item["trad"]["distractors"]
        s_texts = [item["simp"]["q"]] + item["simp"]["distractors"]
        for a, b in zip(t_texts, s_texts):
            if a == b:
                problems.append(f"{item['id']}：繁體與簡體版本完全相同：{a[:20]}…")

    if problems:
        print("✗ 題庫自我檢查失敗，實驗不予執行：")
        for p in problems:
            print(f"    • {p}")
        sys.exit(1)


# ── Ollama ─────────────────────────────────────────────────


def _post(path, payload):
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def embed(model, texts):
    """回傳每段文字的向量。失敗時拋出例外，由呼叫端轉成看得懂的訊息。"""
    data = _post("/api/embed", {"model": model, "input": texts})
    vectors = data.get("embeddings")
    if not vectors or len(vectors) != len(texts):
        raise ValueError(
            f"回應的向量數量不符（要求 {len(texts)}，取得 "
            f"{len(vectors) if vectors else 0}）"
        )
    return vectors


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── 評分 ────────────────────────────────────────────────────


def evaluate(model, variant, q_prefix, d_prefix):
    """回傳 (明細, 統計)。明細是每題的排名與領先幅度。"""
    details = []

    for item in ITEMS:
        v = item[variant]
        docs = [v["correct"]] + v["distractors"]

        q_vec = embed(model, [q_prefix + v["q"]])[0]
        d_vecs = embed(model, [d_prefix + d for d in docs])

        sims = [cosine(q_vec, dv) for dv in d_vecs]
        ranked = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)
        rank = ranked.index(0) + 1  # 正確文件（索引 0）的名次，1 為最佳

        # 領先幅度：正確文件領先最強干擾項多少。
        # 只看排名會浪費資訊 —— 8 題的準確率解析度很粗，
        # 幅度是連續值，即使名次相同也能看出差距在拉開還是拉近。
        best_distractor = max(sims[1:])
        margin = sims[0] - best_distractor

        details.append({"id": item["id"], "rank": rank, "margin": margin})

    n = len(details)
    top1 = sum(1 for d in details if d["rank"] == 1) / n
    top3 = sum(1 for d in details if d["rank"] <= 3) / n
    mrr = sum(1 / d["rank"] for d in details) / n
    mean_margin = sum(d["margin"] for d in details) / n

    return details, {
        "top1": top1,
        "top3": top3,
        "mrr": mrr,
        "margin": mean_margin,
    }


def verdict_for(trad_top1, simp_top1):
    """把兩個 Top-1 準確率轉成一句判讀。

    抽成獨立函式是為了讓測試能直接呼叫它。原本這段邏輯寫在 probe() 裡，
    測試只能另外抄一份 —— 而抄一份等於沒測：真正在跑的那份可以壞掉，
    測試照樣通過。本專案的離線測試因此改為呼叫同一個函式。

    判讀順序有意義：先看「落差」再看「繁體本身好不好」。
    一個模型若繁體 0%、簡體 100%，兩種說法都成立 ——
    「不能用於繁體」正確但沒有資訊量，「簡體偏差」才指出原因，
    而原因決定了下一步（先繁轉簡再嵌入？還是直接換模型？）。
    """
    gap = simp_top1 - trad_top1

    # 「偏差」必須附帶「簡體表現良好」的條件。
    # 否則一個兩邊都爛的模型（繁體 0%、簡體 25%）也會被說成簡體偏差 ——
    # 那是把「模型本身不好」誤診成「簡體訓練造成的偏差」，
    # 而兩者的處置完全不同（後者可靠繁簡轉換救回，前者不能）。
    if trad_top1 >= 0.875 and gap <= 0.125:
        return "繁體鑑別力足夠，且未見簡體偏差", gap
    if gap >= 0.25 and simp_top1 >= 0.75:
        return ("繁體明顯弱於簡體 —— 存在簡體偏差。"
                "可考慮先繁轉簡再嵌入，或改用多語言模型"), gap
    if trad_top1 <= 0.25:
        return "繁體表現貼近隨機（25%）—— 這個模型不能用於繁體文件", gap
    return "介於之間，需人工判讀（見上方明細）", gap


def probe(model):
    q_prefix, d_prefix = prefixes_for(model)
    prefix_note = "無" if not q_prefix else f"{q_prefix!r} / {d_prefix!r}"

    print(f"\n{'=' * 62}")
    print(f"模型：{model}")
    print(f"指令前綴（查詢 / 文件）：{prefix_note}")
    print(f"{'=' * 62}")

    results = {}
    for variant, label in (("trad", "繁體"), ("simp", "簡體")):
        try:
            details, stats = evaluate(model, variant, q_prefix, d_prefix)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            print(f"  ✗ {label}：HTTP {exc.code}：{body}")
            if exc.code == 404:
                print(f"    提示：模型可能尚未下載 —— docker compose exec ollama "
                      f"ollama pull {model}")
            return None
        except Exception as exc:  # noqa: BLE001 - 這裡刻意廣抓，確保失敗可見
            print(f"  ✗ {label}：{type(exc).__name__}：{exc}")
            return None

        results[variant] = stats
        wrong = [d["id"] for d in details if d["rank"] != 1]

        print(f"\n  【{label}】Top-1 {stats['top1']:.0%}　"
              f"Top-3 {stats['top3']:.0%}　MRR {stats['mrr']:.2f}　"
              f"平均領先幅度 {stats['margin']:+.3f}")
        if wrong:
            print(f"    未排在第一的題目：{'、'.join(wrong)}")
        else:
            print("    全部題目皆排在第一")

    verdict, gap = verdict_for(results["trad"]["top1"], results["simp"]["top1"])
    print(f"\n  ── 繁體 vs 簡體 ──")
    print(f"     簡體 Top-1 {results['simp']['top1']:.0%}　"
          f"繁體 Top-1 {results['trad']['top1']:.0%}　"
          f"落差 {gap:+.0%}")
    print(f"     判讀：{verdict}")
    print(f"     注意：N={len(ITEMS)}，一題即 12.5% —— 這個解析度只能看出"
          f"明顯差距，\n           小幅差異（1 題）不足以判定優劣。")

    return results


def main():
    models = sys.argv[1:]
    if not models:
        print("用法：python3 rag_probe.py <模型> [<模型> ...]")
        print("例：  python3 rag_probe.py bge-m3 nomic-embed-text")
        return 2

    _check_test_data()
    print(f"題庫自我檢查通過（{len(ITEMS)} 題，每題 1 正解 + 3 干擾，"
          f"干擾項與正解主題相同、僅差一個屬性）")
    print(f"隨機猜測的期望 Top-1 = 25%")

    summary = {}
    for model in models:
        summary[model] = probe(model)

    ok_models = {m: r for m, r in summary.items() if r}
    if len(ok_models) > 1:
        print(f"\n{'=' * 62}")
        print("總結")
        print(f"{'=' * 62}")
        print(f"{'模型':<28}{'繁體Top-1':>10}{'簡體Top-1':>10}{'落差':>8}")
        for model, r in ok_models.items():
            gap = r["simp"]["top1"] - r["trad"]["top1"]
            print(f"{model:<28}{r['trad']['top1']:>9.0%}"
                  f"{r['simp']['top1']:>10.0%}{gap:>+8.0%}")

    failed = [m for m, r in summary.items() if not r]
    if failed:
        print(f"\n✗ 未能完成：{'、'.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
