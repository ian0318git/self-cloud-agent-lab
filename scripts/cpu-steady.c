/* 機器穩定度探針：一個 vCPU 每秒能做多少固定工作量。
 *
 *   gcc -O2 -o /tmp/cpu-steady scripts/cpu-steady.c
 *   for c in 0 1 2 3; do taskset -c $c /tmp/cpu-steady alu 45 > alu-$c.csv & done; wait
 *   for c in 0 1 2 3; do taskset -c $c /tmp/cpu-steady mem 45 > mem-$c.csv & done; wait
 *
 * 輸出是 CSV：`cpu,已過秒數,該秒的工作量`。alu 的工作量是迭代數，mem 是掃過的
 * 位元組數 —— 兩者單位不同，**只能各自跟自己比**。
 *
 * 為什麼要有這一支（2026-09-30，D-071）。吞吐量基準線連續兩輪被探針自己的穩定度
 * 規則拒絕，而慢樣本的時間全部落在 llama-server 的解碼迴圈裡：沒有重載、沒有
 * prefill、恆等式誤差 <0.1 秒、宿主 steal = 0.00%、沒有換頁、dmesg 沒有熱節流。
 * 那些觀測排除了 VM 內部所有講得出來的嫌疑犯，卻沒有指出**往哪裡找**。
 *
 * 這一支把問題從推論堆疊裡拉出來：不含 ollama、不含容器、不含網路，只有一個純
 * 算術迴圈（alu，受時脈限制）與一個純記憶體順掃迴圈（mem，受頻寬限制）。
 *
 * **判讀靠的是兩個變體之間的差別，不是任何一個的絕對值。** 若某個東西在偷走
 * CPU 時間（有別的行程在排隊），被偷的秒數裡兩種迴圈都做不了事，兩者的變異會
 * **成比例**。若某個東西在壓低時脈（電源／散熱管理），執行緒一直在跑、只是跑得
 * 慢，於是受時脈限制的 alu 掉很多，受 DRAM 頻寬限制的 mem 幾乎不掉。
 * 2026-09-30 量到的就是後者：alu CV 24–25%、mem CV 7–9%。
 *
 * 兩種變體**分開跑，不要同時跑** —— 四條執行緒已經吃滿 4 個 vCPU，同時跑就是
 * 八條在同一批核心上排隊，量到的會是排隊而不是平台。
 *
 * **這一支沒有自動化測試。** 手動驗證方式是：編譯要無警告（`-Wall` 會抓出
 * `%llu` 配 `uint64_t` 那類問題），並且每次跑都要檢查輸出的第一欄 ——
 * `sched_getcpu()` 若不等於 `taskset` 指定的號碼，釘選失效，那一輪的數字不能用。
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdint.h>
#include <inttypes.h>
#include <sched.h>

static double now_mono(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "用法: %s alu|mem 秒數\n", argv[0]);
        return 2;
    }
    const int is_mem = (strcmp(argv[1], "mem") == 0);
    const double secs = atof(argv[2]);
    if (secs <= 1.0) {
        fprintf(stderr, "秒數太小\n");
        return 2;
    }

    /* 64 MiB：遠大於 L3 的單一 instance，順掃一趟是典型的頻寬負載。
     * 對齊到 2 的次方，索引可以用 & 遮罩。 */
    const size_t N = (64u * 1024u * 1024u) / sizeof(uint64_t);
    uint64_t *buf = NULL;
    if (is_mem) {
        buf = malloc(N * sizeof(uint64_t));
        if (buf == NULL) {
            fprintf(stderr, "malloc 失敗\n");
            return 2;
        }
        for (size_t i = 0; i < N; i++)
            buf[i] = (uint64_t)i * 2654435761u + 1u;
    }

    uint64_t acc = 0x9e3779b97f4a7c15ull;
    unsigned long long work = 0;      /* alu: 迭代數；mem: 掃過的位元組數 */
    const double t0 = now_mono();
    double next = t0 + 1.0;
    const double end = t0 + secs;

    for (;;) {
        if (is_mem) {
            uint64_t s = 0;
            for (size_t i = 0; i < N; i++)
                s += buf[i];
            acc ^= s;
            work += (unsigned long long)N * sizeof(uint64_t);
        } else {
            for (int i = 0; i < 1000000; i++) {
                acc = acc * 6364136223846793005ull + 1442695040888963407ull;
                acc ^= acc >> 29;
            }
            work += 1000000ull;
        }

        const double t = now_mono();
        if (t >= next) {
            /* CPU 編號每次重印：釘選失效或被搬動時，這裡會直接說出來。 */
            printf("%d,%.3f,%llu\n", sched_getcpu(), t - t0, work);
            fflush(stdout);
            work = 0;
            next = t + 1.0;
        }
        if (t >= end)
            break;
    }

    /* acc 必須逃逸，否則 -O2 會把整個迴圈刪掉，量到的就是一個空迴圈。 */
    fprintf(stderr, "acc=%" PRIu64 "\n", acc);
    return 0;
}
