# 這顆 kernel 什麼都不做，只做一件事：證明「它還活著」。
#
# 它被推上 Kaggle 當一顆**私有、CPU-only** 的 kernel，跑約 12 分鐘。用途是讓
# `scripts/probe_kernel_files_live.py` 在它**跑動中**輪詢 Kaggle 的檔案 API，
# 回答切 B 選項 B 唯一還沒量的前提：
#
#     引擎的檔案，在 session 跑動中拿不拿得到？
#
# 為什麼要自己寫一顆，而不是拿 endpoint 的 boot 來問：boot 要 GPU 配額，而這個
# 問題問的是 **Kaggle 的檔案 API 在 session 生命週期裡的行為** —— 與 GPU、與
# endpoint 都無關。CPU kernel 便宜到可以忽略，所以前提可以先在這裡量掉，
# 不必等那一趟昂貴的 boot。
#
# ── 這顆 kernel 的設計，每一項都是為了讓「拿不到」與「還沒寫」分得開 ──
#
#   t+0    立刻寫 probe_out/t0.txt。**關鍵**：任何在起始後一分鐘以上的輪詢，
#          只要 API 是活的就該看到它。看不到 = 門沒開，不是「還沒寫」。
#   t+5m   再寫 probe_out/t5.txt。用來分辨「檔案清單是快照」還是「會長大」。
#   每分鐘 印一次心跳，且 flush=True —— 讓 log 本身也有時間軸，可與檔案清單對照。
#   t+11m  正常結束。
#
# 全部寫在 /kaggle/working 底下，那是 Kaggle 的 session 輸出目錄，也是
# endpoint 的引擎寫 logs/cf_engine.log 的地方（engine/engine.py:191-193）。
#
# 這顆 kernel 不含任何機密，也不連網，輸出只有時間戳與它自己寫的檔案。

import datetime
import os
import time

OUT = "/kaggle/working/probe_out"


def stamp(msg: str) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"[FILES-PROBE] {now} {msg}", flush=True)


def write(name: str, body: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(body)
    stamp(f"WROTE probe_out/{name}")


stamp("BOOT")
write("t0.txt", "written at t+0\n")

MINUTES = 11
MARKER_AT = 5

for minute in range(1, MINUTES + 1):
    time.sleep(60)
    stamp(f"MINUTE {minute} of {MINUTES}")
    if minute == MARKER_AT:
        write("t5.txt", f"written at t+{MARKER_AT}m\n")

stamp("DONE")
