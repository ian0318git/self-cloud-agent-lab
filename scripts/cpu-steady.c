/* 機器穩定度探針：一個 vCPU 每秒能做多少固定工作量，以及 barrier 會放大多少。
 *
 *   gcc -O2 -Wall -pthread -o /tmp/cpu-steady scripts/cpu-steady.c
 *
 *   # 個別 vCPU 的產能（先後跑，不要同時跑）
 *   for c in 0 1 2 3; do taskset -c $c /tmp/cpu-steady alu 45 > alu-$c.csv & done; wait
 *   for c in 0 1 2 3; do taskset -c $c /tmp/cpu-steady mem 45 > mem-$c.csv & done; wait
 *
 *   # barrier 放大：N 條執行緒每步會合一次（釘選由程式自己做，不用 taskset）
 *   /tmp/cpu-steady step 4 45 > step-4.csv
 *   /tmp/cpu-steady step 1 45 > step-1.csv
 *   /tmp/cpu-steady step 4 45 nopin > step-4-nopin.csv   # 不釘選
 *
 * 輸出是 CSV。
 *   alu / mem ：`cpu,已過秒數,該秒的工作量`。alu 是迭代數、mem 是掃過的位元組
 *               數 —— 單位不同，**只能各自跟自己比**。
 *   step      ：`cpu,已過秒數,步數,Σmax(ms),Σt0(ms),Σt1(ms),…`。每步每條執行緒
 *               的工作時間都單獨累加，所以「整步被最慢的那條拖住多少」可以從
 *               `Σmax ÷ (各執行緒總時間的平均)` 直接算出來。
 *
 * 為什麼要有 alu 與 mem 兩支（2026-09-30，D-071）。吞吐量基準線連續兩輪被探針
 * 自己的穩定度規則拒絕，而慢樣本的時間全部落在 llama-server 的解碼迴圈裡：沒有
 * 重載、沒有 prefill、恆等式誤差 <0.1 秒、宿主 steal = 0.00%、沒有換頁、dmesg
 * 沒有熱節流。那些觀測排除了 VM 內部所有講得出來的嫌疑犯，卻沒有指出**往哪裡找**。
 *
 * 這一支把問題從推論堆疊裡拉出來：不含 ollama、不含容器、不含網路。
 *
 * **判讀靠的是變體之間的差別，不是任何一個的絕對值。**
 *
 *   alu 對 mem —— 分辨「被偷時間」與「被壓時脈」。若某個東西在偷走 CPU 時間
 *   （有別的行程在排隊），被偷的秒數裡兩種迴圈都做不了事，兩者的變異會**成比
 *   例**。若某個東西在壓低時脈（電源／散熱管理），執行緒一直在跑、只是跑得慢，
 *   於是受時脈限制的 alu 掉很多，受 DRAM 頻寬限制的 mem 幾乎不掉。
 *   2026-09-30 量到的就是後者：alu CV 23–24%、mem CV 6.7–8.8%。
 *
 *   step 對 alu —— 這是 llama.cpp 解碼路徑的模型。每條執行緒做定量工作，然後
 *   全部在 barrier 上會合，於是**整步的耗時由最慢的那條執行緒決定（max，不是
 *   平均）**。若平台的抖動是「整個封裝一起快一起慢」，max 與平均同步升降，
 *   barrier 不改變變異；若是「某一條 vCPU 單獨被壓低」，barrier 就會把那條的
 *   損失攤到每一步上。兩者的差別就是 step 模式要量出來的東西。
 *
 *   step 釘選 對 step nopin —— **這是「我的模型」與「llama.cpp」之間唯一的
 *   差別**。llama.cpp 的執行緒不釘選，所以 guest 排程器可以把兩條放到同一顆
 *   vCPU 上；那條就慢一倍，而 barrier 讓**整步**都用它的速度 —— 這一條會躲過
 *   先前所有的排除證據，因為 `steal` 量的是 hypervisor 拿走的時間，**不是
 *   guest 內部的擠壓**。所以 nopin 模式除了量放大率，還會直接數「有幾步是
 *   兩條以上擠在同一顆 vCPU 上」（每一輪的 stderr 末行）。擠壓步數若是 0，
 *   這個版本當場消滅；若很多而放大率仍然是 1.1 倍，那也表示它解釋不了 2.3×。
 *
 * 三種模式**分開跑，不要同時跑** —— 四條執行緒已經吃滿 4 個 vCPU，同時跑就是
 * 更多條在同一批核心上排隊，量到的會是排隊而不是平台。
 *
 * **這一支沒有自動化測試。** 手動驗證方式是：
 *   1. 編譯要無警告（`-Wall` 會抓出 `%llu` 配 `uint64_t` 那類問題）。
 *   2. 每次跑都要檢查輸出的第一欄 —— `sched_getcpu()` 若不等於指定的號碼，
 *      釘選失效，那一輪的數字不能用。step 模式的釘選由程式自己做，失敗會直接
 *      在 stderr 出聲並讓那一輪作廢，不會安靜地量到沒釘選的數字。
 *      **`nopin` 的那一輪例外**：它本來就不釘選，第一欄會漂移，那是預期行為而
 *      非失效；判斷改看 stderr 末行的「擠在同一顆 vCPU 的步數」。
 *   3. `acc` 必須逃逸（印到 stderr），否則 `-O2` 會把整個迴圈刪掉，
 *      量到的就是一個空迴圈。
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <stdint.h>
#include <inttypes.h>
#include <sched.h>
#include <pthread.h>

#define MAXT 8

static double now_mono(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

/* ──────────────────────────────────────────────────────────────────────
 * alu / mem：單執行緒，逐秒回報
 * ────────────────────────────────────────────────────────────────────── */

static int run_loop(int is_mem, double secs)
{
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
    free(buf);
    return 0;
}

/* ──────────────────────────────────────────────────────────────────────
 * step：N 條執行緒，每步做定量工作後在 barrier 上會合
 *
 * 每一步分兩道 barrier：
 *   工作 → barrier#1（所有工作時間都已發布）→ tid0 讀取並累加
 *        → barrier#2（讀完才准進下一步）→ 檢查是否收工
 *
 * 兩道是為了讓「讀取」不會與「下一輪的寫入」重疊 —— 單一緩衝就夠，不必輪替。
 * 收工的決定寫在 barrier#2 之前，所以所有人看到的是同一個值，不會有人卡在
 * 一道再也不會被釋放的 barrier 上。
 * ────────────────────────────────────────────────────────────────────── */

typedef struct {
    int nthreads;
    int pin;                /* 0 = 不釘選（模擬 llama.cpp：執行緒可被搬動） */
    long work;              /* 每一步的 LCG 迭代數 */
    double t0, end;
    volatile int stop;
    volatile long total;    /* 整輪的步數（只有 tid0 寫） */
    volatile long collide;  /* 兩條以上執行緒落在同一個 vCPU 的步數（只有 tid0 寫） */
    int cpu[MAXT];          /* 本步各執行緒所在的 vCPU（各自寫自己那格） */
    double t[MAXT];         /* 本步每條執行緒自己的工作時間（秒） */
    double acc_max;         /* 每秒累加：每一步的 max（只有 tid0 寫） */
    double acc_each[MAXT];  /* 每秒累加：第 i 條執行緒的總工作時間（只有 tid0 寫） */
    long steps;             /* 該秒的步數（只有 tid0 寫） */
    uint64_t acc[MAXT];     /* 各自的逃逸值，避免迴圈被最佳化掉 */
    pthread_barrier_t bar;
} step_ctx;

/* tid 用參數傳，**不從共用的 ctx 讀** —— 在建立迴圈裡寫 `c.tid = i` 再
 * pthread_create 是競態：執行緒可能在新值寫入之後才開始跑，於是兩條執行緒
 * 拿到同一個 tid，兩條都去釘同一個 vCPU，而其中一個 vCPU 空著。
 * 那種錯誤在輸出上只表現為「某些秒的數字怪怪的」，看不出原因。 */
typedef struct {
    step_ctx *c;
    int tid;
} step_arg;

static void *step_thread(void *arg)
{
    const step_arg *sa = arg;
    step_ctx *c = sa->c;
    const int me = sa->tid;

    if (c->pin) {
        cpu_set_t set;
        CPU_ZERO(&set);
        CPU_SET(me, &set);
        if (pthread_setaffinity_np(pthread_self(), sizeof(set), &set) != 0) {
            /* 釘選失敗就讓整輪作廢 —— 安靜地量一組沒釘選的數字比不量更糟。 */
            fprintf(stderr, "✗ 釘選失敗：thread %d\n", me);
            c->stop = 1;
            return NULL;
        }
    }

    uint64_t acc = 0x9e3779b97f4a7c15ull * (uint64_t)(me + 1);
    double next = c->t0 + 1.0;

    for (;;) {
        const double a = now_mono();
        for (long i = 0; i < c->work; i++) {
            acc = acc * 6364136223846793005ull + 1442695040888963407ull;
            acc ^= acc >> 29;
        }
        const double b = now_mono();
        c->t[me] = b - a;
        /* 工作**做完之後**才取樣：這一步我實際跑在哪個 vCPU 上。不釘選時
         * 這個值會漂移，而「有兩條落在同一顆」正是要看的東西。 */
        c->cpu[me] = sched_getcpu();

        pthread_barrier_wait(&c->bar);

        if (me == 0) {
            double mx = 0.0;
            for (int i = 0; i < c->nthreads; i++) {
                if (c->t[i] > mx)
                    mx = c->t[i];
                c->acc_each[i] += c->t[i];
            }
            c->acc_max += mx;
            c->steps++;
            c->total++;

            /* 這一步有沒有兩條執行緒擠在同一顆 vCPU 上。有＝這一步的 max
             * 註定被那條慢一倍（或更多）的執行緒決定 —— 這正是 barrier
             * 假設在真系統上唯一還沒被排除的版本。 */
            int seen = 0, dup = 0, j;
            for (j = 0; j < c->nthreads; j++) {
                unsigned bit = 1u << (unsigned)(c->cpu[j] & 31);
                if (seen & (int)bit) { dup = 1; break; }
                seen |= (int)bit;
            }
            if (dup)
                c->collide++;

            const double t = now_mono();
            if (t >= next) {
                printf("%d,%.3f,%ld,%.3f", sched_getcpu(), t - c->t0, c->steps,
                       c->acc_max * 1e3);
                for (int i = 0; i < c->nthreads; i++)
                    printf(",%.3f", c->acc_each[i] * 1e3);
                printf("\n");
                fflush(stdout);
                c->steps = 0;
                c->acc_max = 0.0;
                for (int i = 0; i < c->nthreads; i++)
                    c->acc_each[i] = 0.0;
                next = t + 1.0;
            }
            if (t >= c->end)
                c->stop = 1;
        }

        pthread_barrier_wait(&c->bar);

        if (c->stop)
            break;
    }

    c->acc[me] = acc;
    fprintf(stderr, "thread %d acc=%" PRIu64 "\n", me, acc);
    return NULL;
}

static int run_step(int nthreads, double secs, int pin)
{
    step_ctx c;
    step_arg sa[MAXT];
    pthread_t th[MAXT];

    memset(&c, 0, sizeof(c));
    c.nthreads = nthreads;
    c.pin = pin;
    /* 每步約 2 ms：夠長到量得準，夠短到一秒有幾百步可看分布。 */
    c.work = 1000000;
    c.t0 = now_mono();
    c.end = c.t0 + secs;

    if (pthread_barrier_init(&c.bar, NULL, (unsigned)nthreads) != 0) {
        fprintf(stderr, "barrier 初始化失敗\n");
        return 2;
    }
    int started = 0;
    for (int i = 0; i < nthreads; i++) {
        sa[i].c = &c;
        sa[i].tid = i;
        if (pthread_create(&th[i], NULL, step_thread, &sa[i]) != 0) {
            fprintf(stderr, "thread %d 建立失敗\n", i);
            c.stop = 1;   /* 已經跑起來的會在下一次 barrier 之後看到並收工 */
            break;
        }
        started++;
    }
    for (int i = 0; i < started; i++)
        pthread_join(th[i], NULL);
    pthread_barrier_destroy(&c.bar);

    if (started != nthreads) {
        fprintf(stderr, "✗ 只起來 %d/%d 條 —— 這一輪作廢\n", started, nthreads);
        return 2;
    }
    if (c.total == 0) {
        fprintf(stderr, "✗ 一步都沒有完成 —— 這一輪沒有有效資料\n");
        return 1;
    }
    fprintf(stderr, "完成 %ld 步\n", c.total);
    fprintf(stderr, "釘選=%s，兩條以上擠在同一顆 vCPU 的步數 = %ld / %ld (%.1f%%)\n",
            pin ? "是" : "否", c.collide, c.total,
            c.total ? 100.0 * (double)c.collide / (double)c.total : 0.0);
    return 0;
}

/* ────────────────────────────────────────────────────────────────────── */

static void usage(const char *argv0)
{
    fprintf(stderr,
            "用法: %s alu|mem 秒數\n"
            "      %s step 執行緒數 秒數 [nopin]\n"
            "\n"
            "  alu         受時脈限制的算術迴圈\n"
            "  mem         受 DRAM 頻寬限制的順掃迴圈（64 MiB）\n"
            "  step        N 條執行緒每步過一次 barrier（預設由程式自己釘選）\n"
            "  nopin       不釘選 —— 執行緒可被搬動，模擬 llama.cpp 的執行緒\n",
            argv0, argv0);
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        usage(argv[0]);
        return 2;
    }

    if (strcmp(argv[1], "step") == 0) {
        if (argc < 4) {
            usage(argv[0]);
            return 2;
        }
        const int n = atoi(argv[2]);
        const double secs = atof(argv[3]);
        if (n < 1 || n > MAXT) {
            fprintf(stderr, "執行緒數必須在 1–%d 之間\n", MAXT);
            return 2;
        }
        if (secs <= 1.0) {
            fprintf(stderr, "秒數太小\n");
            return 2;
        }
        /* 第四個參數 `nopin` ＝ 不釘選，模擬 llama.cpp 的執行緒（它不釘）。 */
        const int pin = !(argc >= 5 && strcmp(argv[4], "nopin") == 0);
        if (argc >= 5 && strcmp(argv[4], "nopin") != 0) {
            fprintf(stderr, "不認識的參數：%s（只認 nopin）\n", argv[4]);
            return 2;
        }
        return run_step(n, secs, pin);
    }

    const int is_mem = (strcmp(argv[1], "mem") == 0);
    if (!is_mem && strcmp(argv[1], "alu") != 0) {
        usage(argv[0]);
        return 2;
    }
    const double secs = atof(argv[2]);
    if (secs <= 1.0) {
        fprintf(stderr, "秒數太小\n");
        return 2;
    }
    return run_loop(is_mem, secs);
}
