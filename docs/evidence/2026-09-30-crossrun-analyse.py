# 2026-09-30-crossrun-analyse.py
#
# 指令：python3 tmp/reps/analyse.py
# 時間：2026-09-30 01:26:00 +1000（來源檔的 mtime）
# 結束碼：0
# 牆上時間：數秒
# 引用：DECISIONS.md D-071 §十一（2026-09-30 追加）
# 註  ：跨輪分析腳本。**這是儀器，不是輸出** —— 放在這裡是因為第十一節記了「它的三張表無法從版本庫重推」這個缺口。
#     **已知缺陷（2026-09-30 搬遷時發現）：** `SOURCES` 把 ISO 隔離輪也收進去，而 ISO 只有 3 組條件且全在 `ctx=4096`，會讓「所有輪都有的條件」塌成 **1 組**。第九節補記之二的表是 **6 組** —— 那是**排除 ISO** 之後的五輪交集。要重現那張表，把 `SOURCES` 裡 ISO 那一行拿掉即可，其餘不動。
#     路徑以搬遷前的 `tmp/` 佈局為準；`tmp/` 清掉之後那三行要改指這裡的新檔名。
#     本批 53 個逐字檔裡，**只有這一個的來源自己以 `#` 開頭**（`#!`），所以只有它的說明區塊不能用前綴判斷邊界 —— 見本檔最後一行。
#
# 上面開頭連續的 `#` 行是這次搬遷加上去的說明 —— **但原檔自己的第一行也是 `#` 開頭（`#!`）**，所以不能只看前綴判斷邊界。說明區塊到上面那一行為止，其後就是原檔，一個位元組都沒少。
#!/usr/bin/env python3
"""跨輪中位數一致性分析（(b) 的驗證）。讀 tmp/reps/*.json 與既有三輪。

用法：python3 tmp/reps/analyse.py
"""
import json
import statistics as st
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
REPS = Path(__file__).resolve().parent

# (標籤, 路徑, 是否為「第一條件可比」的完整輪)
SOURCES = [
    ("R1", BASE / "tmp/throughput-baseline-2026-09-29.json"),
    ("ISO", BASE / "tmp/iso-4096.json"),
    ("R3", BASE / "tmp/throughput-baseline-rerun.json"),
]
for tag in "bcdefg":
    p = REPS / f"rep-{tag}.json"
    if p.exists() and p.stat().st_size > 0:
        SOURCES.append((f"r{tag}", p))


def key(c):
    return (c["model"], c["num_ctx"], c["num_predict"])


def load(path):
    try:
        d = json.loads(Path(path).read_text())
    except Exception:
        return None
    out = {}
    for c in d["conditions"]:
        smp = [x for x in c["samples"] if x["usable"]["usable"]]
        if not smp:
            continue
        out[key(c)] = {
            "label": c["label"],
            "dec": [x["decode_tps"] for x in smp],
            "cv": (c.get("summary") or {}).get("cv_pct"),
        }
    return out


def q(v, p):
    s = sorted(v)
    i = (len(s) - 1) * p
    lo = int(i)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def main():
    runs = [(t, load(p)) for t, p in SOURCES]
    runs = [(t, d) for t, d in runs if d]
    print(f"讀到 {len(runs)} 輪：" + "、".join(t for t, _ in runs))
    if len(runs) < 2:
        return

    # 只取所有輪都有的條件
    common = set(runs[0][1])
    for _, d in runs[1:]:
        common &= set(d)
    common = sorted(common)
    print(f"全部輪都有的條件：{len(common)} 組\n")

    print("=" * 100)
    print("① 每組的中位數，逐輪（t/s）")
    print("=" * 100)
    tags = [t for t, _ in runs]
    print(f"{'條件':<38}" + "".join(f"{t:>9}" for t in tags) + f"{'極差%':>9}")
    print("-" * 100)
    spreads = {}
    for k in common:
        meds = [st.median(d[k]["dec"]) for _, d in runs]
        spr = (max(meds) - min(meds)) / st.median(meds) * 100
        spreads[k] = spr
        print(f"{runs[0][1][k]['label']:<38}"
              + "".join(f"{m:>9.3f}" for m in meds) + f"{spr:>8.1f}%")

    print()
    print("=" * 100)
    print("② 每組的中位數 vs 該組輪內樣本 CV")
    print("=" * 100)
    print(f"{'條件':<38} {'輪間極差%':>10} {'輪內CV中位%':>12} {'倍數':>8}")
    print("-" * 100)
    for k in common:
        cvs = [d[k]["cv"] for _, d in runs if d[k]["cv"] is not None]
        m = st.median(cvs) if cvs else float("nan")
        r = m / spreads[k] if spreads[k] else float("inf")
        print(f"{runs[0][1][k]['label']:<38} {spreads[k]:>9.1f}% {m:>11.1f}% {r:>7.1f}×")

    print()
    print("=" * 100)
    print(f"③ 候選統計量的跨輪穩定度（比值 vs 第 1 輪，{len(runs)} 輪合計）")
    print("=" * 100)
    STATS = [
        ("中位數 (b 的定義)", st.median),
        ("平均數", st.mean),
        ("最快樣本 (max)", max),
        ("第 75 百分位", lambda v: q(v, 0.75)),
        ("上緣3個平均", lambda v: st.mean(sorted(v)[-3:])),
    ]
    print(f"{'統計量':<22} {'離1中位%':>10} {'最壞%':>9} {'極差':>9}")
    print("-" * 100)
    for name, fn in STATS:
        devs = []
        base = runs[0][1]
        for _, d in runs[1:]:
            for k in common:
                devs.append(abs(fn(d[k]["dec"]) / fn(base[k]["dec"]) - 1))
        print(f"{name:<22} {st.median(devs)*100:>9.2f}% {max(devs)*100:>8.2f}% "
              f"{(max(devs)-min(devs))*100:>8.2f}%")

    print()
    print("=" * 100)
    print("④ 「多輪中位數一致」要訂多寬才成立 —— 每組的 |比值-1|")
    print("=" * 100)
    devs = []
    for k in common:
        ds = []
        base = runs[0][1][k]["dec"]
        for _, d in runs[1:]:
            ds.append(abs(st.median(d[k]["dec"]) / st.median(base) - 1) * 100)
        devs.append((max(ds), runs[0][1][k]["label"]))
    for thr in (3, 5, 10, 15, 20):
        ok = sum(1 for v, _ in devs if v <= thr)
        print(f"  門檻 ≤{thr:>3}%：{ok}/{len(devs)} 組通過")
    print(f"\n  最壞的一組：{max(devs)[1]}  {max(devs)[0]:.1f}%")


if __name__ == "__main__":
    main()
