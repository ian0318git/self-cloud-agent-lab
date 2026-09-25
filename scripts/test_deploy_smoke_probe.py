#!/usr/bin/env python3
"""deploy_smoke_probe.py 的離線單元測試（不連網、不需容器、不需 ollama）。

這組測試守的是**部署當下唯一那個會說「好了」的判斷**。它答錯的兩個方向
代價不對稱：

  · 假成功（壞掉的堆疊被講成好的）—— 使用者就這麼用下去了，而 Open WebUI
    的錯誤訊息不會指向真正的成因。
  · 假失敗（好的堆疊被講成壞的）—— D-016：叫人去查一個不存在的問題。

而最貴的一條是**那個陷阱本身**（D-027 第十一節，同一個形狀在本專案出現過
六次）：`qwen3:4b` 是推理模型，thinking 的 token 也計入 `eval_count`，所以
一個太小的 `num_predict` 可以整個被 thinking 吃掉、回傳空的 content ——
那與「部署壞了」在輸出上無法區分。這裡的測試因此要求：

  1. 四條斷言**各自**都有一個「該擋的擋」測資（少一條就少一道防線）
  2. 「讀不到」與「答錯了」走**不同**的判定（None vs False），而且訊息
     必須**可區辨**（D-026 第五節）—— 只檢查「有沒有非 0」測不出這件事
  3. `done_reason=length` 即使 content 非空也**是**失敗

執行：python3 scripts/test_deploy_smoke_probe.py
"""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deploy_smoke_probe as p  # noqa: E402


# ── 造測資的輔助 ────────────────────────────────────────


def _resp(content="ok", done_reason="stop", eval_count=5):
    """ollama /api/chat 的回應形狀（只留判定會讀的欄位）。"""
    r = {"message": {"role": "assistant", "content": content}}
    if done_reason is not None:
        r["done_reason"] = done_reason
    if eval_count is not None:
        r["eval_count"] = eval_count
    return r


def _ps(name="qwen3:4b", ctx=8192, extra=None):
    m = {"name": name, "model": name}
    if ctx is not None:
        m["context_length"] = ctx
    models = [m]
    if extra:
        models.extend(extra)
    return {"models": models}


def _ps_gpu(name="qwen3:4b", size=4000000000, vram=4000000000):
    """ollama /api/ps 的形狀：size 是總量，size_vram 是住在 VRAM 裡的那部分。

    傳 None 代表那個欄位**不存在** —— 用來測「讀不到」，不是測「值是 0」。
    這兩件事在判準上是不同的結論，所以要能分開造出來。
    """
    m = {"name": name, "model": name}
    if size is not None:
        m["size"] = size
    if vram is not None:
        m["size_vram"] = vram
    return {"models": [m]}


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _assert_reason(reason, must, mustnot=()):
    for s in must:
        _assert(s in reason, "訊息裡應該有 %r，實際是：%s" % (s, reason))
    for s in mustnot:
        _assert(s not in reason, "訊息裡**不該**有 %r，實際是：%s" % (s, reason))


# ═══════════════════════════════════════════════════════════
# smoke_verdict —— 四條斷言，每一條都要有自己的守衛
# ═══════════════════════════════════════════════════════════


def test_smoke_happy_path():
    ok, reason = p.smoke_verdict(_resp(), 32)
    _assert(ok is True, "正常的回應應該判過，得到 %r" % (ok,))
    _assert_reason(reason, ["done_reason=stop", "eval_count=5", "上限 32"])


def test_smoke_boundary_one_below_the_cap():
    # eval_count == num_predict - 1 是「跑得完」的最後一格，不可以被擋。
    ok, _ = p.smoke_verdict(_resp(eval_count=31), 32)
    _assert(ok is True, "31 < 32 應該判過，得到 %r" % (ok,))


def test_smoke_trap_partial_answer_with_length_is_a_failure():
    """**最重要的一條。** content 非空、看起來像個答案，但被切斷了。

    這正是「被切斷的生成被當成完整的量測」在煙霧測試裡的形狀：一個
    num_predict 太小的請求會回一段看起來很合理、其實沒講完的話。
    """
    ok, reason = p.smoke_verdict(_resp(content="The", done_reason="length", eval_count=32), 32)
    _assert(ok is False, "done_reason=length 必須判**失敗**，得到 %r" % (ok,))
    # 與下面 empty-content 那條必須講不同的話 —— 兩者成因不同、處置不同。
    _assert_reason(reason, ["沒有自然結束", "不是 'stop'"])


def test_smoke_empty_content_with_length_is_a_failure():
    ok, reason = p.smoke_verdict(_resp(content="", done_reason="length", eval_count=32), 32)
    _assert(ok is False, "空 content 必須判失敗，得到 %r" % (ok,))
    _assert_reason(
        reason,
        ["生成被 num_predict=32 切斷了", "被切斷的生成不能當成完整的量測"],
        # 這句是 empty+stop 那條在講的，兩者不可以混。
        mustnot=["模型沒有回答任何東西"],
    )


def test_smoke_empty_content_with_stop_says_something_different():
    ok, reason = p.smoke_verdict(_resp(content="", done_reason="stop"), 32)
    _assert(ok is False, "空 content 必須判失敗，得到 %r" % (ok,))
    _assert_reason(
        reason,
        ["模型沒有回答任何東西"],
        mustnot=["生成被 num_predict"],
    )


def test_smoke_whitespace_only_content_counts_as_empty():
    ok, _ = p.smoke_verdict(_resp(content="   \n  "), 32)
    _assert(ok is False, "只有空白的 content 應該與空的同等處置，得到 %r" % (ok,))


def test_smoke_missing_done_reason_is_indeterminate_not_failure():
    """主要的那道防護套不上時，不可以講成「判準沒過」。

    欄位不存在是**儀器的狀態**（這個 ollama 版本太舊），不是模型的狀態。
    回 False 會讓一個好的部署被報成壞的 —— D-016 的假失敗。
    """
    ok, reason = p.smoke_verdict(_resp(done_reason=None), 32)
    _assert(ok is None, "done_reason 缺席應該是 None，得到 %r" % (ok,))
    _assert_reason(reason, ["主要的那道防護套不上"])


def test_smoke_missing_eval_count_is_indeterminate():
    ok, reason = p.smoke_verdict(_resp(eval_count=None), 32)
    _assert(ok is None, "讀不到 eval_count 應該是 None，得到 %r" % (ok,))
    _assert_reason(reason, ["無法判斷生成是不是被切斷的"])


def test_smoke_zero_eval_count_is_a_failure():
    ok, reason = p.smoke_verdict(_resp(content="ok", eval_count=0), 32)
    _assert(ok is False, "eval_count=0 必須判失敗，得到 %r" % (ok,))
    _assert_reason(reason, ["沒有生成任何 token"])


def test_smoke_stop_but_hitting_the_cap_is_a_contradiction():
    # 兩條斷言互相矛盾時，那本身就是要講出來的資訊，不是挑一條相信。
    ok, reason = p.smoke_verdict(_resp(content="ok", done_reason="stop", eval_count=32), 32)
    _assert(ok is False, "stop 卻撞到上限應該判失敗，得到 %r" % (ok,))
    _assert_reason(reason, ["這兩者矛盾"])


def test_smoke_unreadable_shapes_are_indeterminate():
    for bad, want in [
        ("不是 dict", "回應不是 JSON 物件"),
        ({}, "沒有 message 物件"),
        ({"message": "不是 dict"}, "沒有 message 物件"),
        ({"message": {"content": None}}, "message.content 不是字串"),
        ({"message": {}}, "message.content 不是字串"),
    ]:
        ok, reason = p.smoke_verdict(bad, 32)
        _assert(ok is None, "%r 應該是 None，得到 %r" % (bad, ok))
        _assert_reason(reason, [want])


def test_smoke_default_budget_covers_a_reasoning_model():
    """**這一條是這支探針 2026-09-21 實際踩過的坑，不是預防性的。**

    原本的預設 `num_predict=32` 配上 `think: false`，而 `think: false` 只是把
    推理搬進 `content`、並沒有關掉它。於是 32 個 token 全部被推理吃掉，
    `done_reason=length` —— 判成「上游變了」（結束碼 1），但真正壞掉的是
    這支探針自己的參數。**一個假失敗比漏掉更糟（D-016）。**

    實測那個 prompt 要 152 個 token 的推理才回答，所以預設預算必須高於它，
    而且要留餘裕（推理長度會隨模型與版本變動）。把預算調回一個註定不夠的
    值時，這條會響。
    """
    _assert(
        p.DEFAULT_NUM_PREDICT > p.MEASURED_REASONING_TOKENS,
        "預設 num_predict=%d 沒有超過實測的推理長度 %d —— 生成會在回答之前"
        "就被切斷" % (p.DEFAULT_NUM_PREDICT, p.MEASURED_REASONING_TOKENS),
    )
    # 餘裕也要有，不然推理長一點的模型就翻車。
    _assert(
        p.DEFAULT_NUM_PREDICT >= 2 * p.MEASURED_REASONING_TOKENS,
        "預設 num_predict=%d 對實測的 %d 個推理 token 餘裕不足"
        % (p.DEFAULT_NUM_PREDICT, p.MEASURED_REASONING_TOKENS),
    )
    # 上限仍然必須存在，否則第 4 條斷言（沒撞到上限）就沒有意義了。
    _assert(
        p.DEFAULT_NUM_PREDICT < 100000,
        "預設 num_predict 大到等於沒有上限，第 4 條斷言會失去意義",
    )
    # **上面兩條錨在 MEASURED_REASONING_TOKENS 上，所以那個常數本身要被守住。**
    # 這是突變測試抓出來的：第一版的突變是把量到的長度調小，而它**存活了** ——
    # 因為把量測調小只會讓「預算 > 量測」更容易成立，兩條斷言都還是綠的。
    # 一個可以被悄悄歸零的錨點等於沒有錨點，所以給它一個合理範圍。
    _assert(
        50 <= p.MEASURED_REASONING_TOKENS <= 2000,
        "實測推理長度 %d 落在合理範圍外 —— 這個常數是上面兩條斷言的錨，"
        "改動它之前要重新量一次，不是改數字讓測試變綠"
        % p.MEASURED_REASONING_TOKENS,
    )


# ═══════════════════════════════════════════════════════════
# ctx_verdict —— 一個設了卻沒生效的開關
# ═══════════════════════════════════════════════════════════


def test_ctx_match():
    ok, reason = p.ctx_verdict(_ps(ctx=8192), "qwen3:4b", 8192)
    _assert(ok is True, "相符應該判過，得到 %r" % (ok,))
    _assert_reason(reason, ["context_length=8192"])


def test_ctx_mismatch_is_false_and_names_both_numbers():
    """不符時必須同時講出**實際值**與**要求值**。

    只講「num_ctx 沒有生效」的話，讀的人沒辦法判斷是差一點還是差很多，
    也不知道該不該去查。而且這條失敗的結束碼是 2 不是 1（見 run_ctx），
    所以訊息要自己說清楚「堆疊是好的、只是這個設定沒生效」。
    """
    ok, reason = p.ctx_verdict(_ps(ctx=4096), "qwen3:4b", 8192)
    _assert(ok is False, "不符應該判 False，得到 %r" % (ok,))
    _assert_reason(reason, ["context_length=4096", "不是要求的 8192", "沒有生效"])
    _assert_reason(reason, ["堆疊本身是好的"], mustnot=[])


def test_ctx_model_not_loaded_is_indeterminate():
    # 「模型不在清單裡」不等於「num_ctx 錯了」—— 這是量不到，不是量到反例。
    ok, reason = p.ctx_verdict(_ps(name="llama3:8b"), "qwen3:4b", 8192)
    _assert(ok is None, "模型不在清單裡應該是 None，得到 %r" % (ok,))
    _assert_reason(reason, ["不在 /api/ps 的清單裡"])
    _assert_reason(reason, ["量不到"], mustnot=["沒有生效"])


def test_ctx_missing_context_length_is_indeterminate():
    ok, reason = p.ctx_verdict(_ps(ctx=None), "qwen3:4b", 8192)
    _assert(ok is None, "讀不到 context_length 應該是 None，得到 %r" % (ok,))
    _assert_reason(reason, ["讀不到 context_length"])


def test_ctx_unreadable_response_is_indeterminate():
    for bad, want in [
        ("不是 dict", "回應不是 JSON 物件"),
        ({}, "沒有 models 清單"),
        ({"models": "不是 list"}, "沒有 models 清單"),
        ({"models": []}, "不在 /api/ps 的清單裡"),
    ]:
        ok, reason = p.ctx_verdict(bad, "qwen3:4b", 8192)
        _assert(ok is None, "%r 應該是 None，得到 %r" % (bad, ok))
        _assert_reason(reason, [want])


def test_ctx_matches_a_name_the_user_left_untagged():
    # 使用者打 `qwen3`、ollama 回 `qwen3:latest` —— 這是同一個模型。
    # lib.sh 的註解記著一次實際發生的假失敗（rag_probe.sh 對已下載的
    # bge-m3 報「尚未下載」），成因就是拿未正規化的名字去比對。
    ok, _ = p.ctx_verdict(_ps(name="qwen3:latest"), "qwen3", 8192)
    _assert(ok is True, "未帶 tag 的名字應該正規化後相符，得到 %r" % (ok,))


def test_ctx_picks_the_right_entry_among_several():
    extra = [{"name": "llama3:8b", "context_length": 2048}]
    ok, reason = p.ctx_verdict(_ps(ctx=8192, extra=extra), "qwen3:4b", 8192)
    _assert(ok is True, "應該挑中 qwen3:4b 那一筆，得到 %r" % (ok,))
    _assert_reason(reason, ["context_length=8192"])


# ═══════════════════════════════════════════════════════════
# gpu_verdict —— 「我們說它在 GPU 上」與「它真的在」是兩件事
# ═══════════════════════════════════════════════════════════


def test_gpu_fully_resident_is_true():
    ok, reason = p.gpu_verdict(_ps_gpu(size=4000000000, vram=4000000000), "qwen3:4b")
    _assert(ok is True, "size_vram 等於 size 就是整顆在 GPU 上，得到 %r" % (ok,))
    _assert_reason(reason, ["4000000000"])


def test_gpu_zero_vram_is_false_not_indeterminate():
    """**這個檔案裡最重要的一條。**

    這是 #83 要消滅的那個靜默失敗：我們掛了 device reservation、告訴使用者
    這台機器在用 GPU，而 ollama 一個位元組都沒放上去。它必須是 False（→ 結束碼
    1），不可以混進 None（→ 結束碼 2）被當成「環境無從判定」放過去。
    """
    ok, reason = p.gpu_verdict(_ps_gpu(size=4000000000, vram=0), "qwen3:4b")
    _assert(ok is False, "size_vram=0 是失敗，不是無法判定，得到 %r" % (ok,))


def test_gpu_zero_vram_reason_carries_the_remediation():
    """判準要能自己說出下一步 —— 否則它只是把人丟在原地。"""
    _, reason = p.gpu_verdict(_ps_gpu(size=4000000000, vram=0), "qwen3:4b")
    _assert_reason(
        reason,
        ["docker-compose.gpu.yml", "COMPOSE_FILE", "nvidia-smi -L", "OLLAMA_GPU=off"],
    )


def test_gpu_partial_offload_is_indeterminate():
    """部分卸載是**真的量測**，不是判準沒過 —— 比照 run_ctx 對 NOT_APPLIED 的推理。"""
    ok, reason = p.gpu_verdict(_ps_gpu(size=4000000000, vram=1000000000), "qwen3:4b")
    _assert(ok is None, "部分卸載不是失敗，得到 %r" % (ok,))
    _assert_reason(reason, ["25%"])


def test_gpu_one_byte_off_is_still_partial_not_full():
    """邊界：差一個位元組就不是「整顆在 GPU 上」，但也不是 CPU。"""
    ok, _ = p.gpu_verdict(_ps_gpu(size=4000000000, vram=3999999999), "qwen3:4b")
    _assert(ok is None, "少一個位元組仍然是部分卸載，得到 %r" % (ok,))


def test_gpu_one_byte_on_is_partial_not_cpu():
    """另一邊的邊界：只放上去一個位元組**不算**「整顆跑在 CPU 上」。"""
    ok, _ = p.gpu_verdict(_ps_gpu(size=4000000000, vram=1), "qwen3:4b")
    _assert(ok is None, "放上去一點點不是 size_vram=0，得到 %r" % (ok,))


def test_gpu_model_not_loaded_is_indeterminate():
    ok, reason = p.gpu_verdict(_ps_gpu(name="llama3:8b"), "qwen3:4b")
    _assert(ok is None, "模型不在清單裡是「量不到」，得到 %r" % (ok,))


def test_gpu_missing_size_vram_is_indeterminate_not_cpu():
    """欄位不存在 ≠ 值是 0。

    舊版 ollama 不送 size_vram 時把它讀成 0，就會**對著好好的 GPU 佈署大喊
    「它跑在 CPU 上」** —— 一個會誤報的守衛比沒有守衛更糟。
    """
    ok, reason = p.gpu_verdict(_ps_gpu(size=4000000000, vram=None), "qwen3:4b")
    _assert(ok is None, "讀不到 size_vram 是「量不到」，不是 0，得到 %r" % (ok,))
    _assert_reason(reason, [], mustnot=["跑在 CPU"])


def test_gpu_missing_size_is_indeterminate():
    ok, _ = p.gpu_verdict(_ps_gpu(size=None, vram=0), "qwen3:4b")
    _assert(ok is None, "沒有 size 就無從算比例，得到 %r" % (ok,))


def test_gpu_zero_size_is_indeterminate():
    """size=0 讓比例失去意義（0/0）—— 不可以除出一個看起來合理的百分比。"""
    ok, _ = p.gpu_verdict(_ps_gpu(size=0, vram=0), "qwen3:4b")
    _assert(ok is None, "size=0 時比例無意義，得到 %r" % (ok,))


def test_gpu_bools_are_not_numbers():
    """bool 是 int 的子類：True == 1，不擋的話會被當成位元組數。"""
    ok, _ = p.gpu_verdict(_ps_gpu(size=True, vram=True), "qwen3:4b")
    _assert(ok is None, "bool 不該被當成整數，得到 %r" % (ok,))


def test_gpu_unreadable_shapes_are_indeterminate():
    bad_shapes = [
        None,
        [],
        "not a dict",
        {},
        {"models": None},
        {"models": "nope"},
        {"models": [None]},
        {"models": [{"name": "qwen3:4b", "size": "big", "size_vram": 0}]},
        {"models": [{"name": "qwen3:4b", "size": 8, "size_vram": "0"}]},
    ]
    for bad in bad_shapes:
        ok, reason = p.gpu_verdict(bad, "qwen3:4b")
        _assert(ok is None, "讀不懂的形狀應該是 None（%r），得到 %r" % (bad, ok))
        _assert(reason, "每一種讀不到都要有自己的說法（%r）" % (bad,))


def test_gpu_matches_a_name_the_user_left_untagged():
    """與 ctx_verdict 同一條名字規則，否則使用者寫 qwen3:4b、ollama 回 qwen3:4b 也會對不上。"""
    ok, _ = p.gpu_verdict(_ps_gpu(name="qwen3:latest"), "qwen3")
    _assert(ok is True, "應該沿用 normalize_model 的規則，得到 %r" % (ok,))


def test_gpu_picks_the_right_entry_among_several():
    ps = _ps_gpu(size=4000000000, vram=0)
    ps["models"].append({"name": "bge-m3:latest", "size": 1, "size_vram": 1})
    ok, reason = p.gpu_verdict(ps, "qwen3:4b")
    _assert(ok is False, "應該挑中 qwen3:4b 那一筆（它在 CPU 上），得到 %r" % (ok,))
    _assert_reason(reason, ["qwen3:4b"])


# ═══════════════════════════════════════════════════════════
# normalize_model —— 與 lib.sh 的同一條規則
# ═══════════════════════════════════════════════════════════


def test_normalize_model():
    cases = [
        ("bge-m3", "bge-m3:latest"),
        ("bge-m3:latest", "bge-m3:latest"),
        ("qwen3:4b", "qwen3:4b"),
        # 只有「最後一個 : 在最後一個 / 之後」才是 tag ——
        # 這個是 registry 的埠號，是名字的一部分，不可以被當成 tag。
        ("localhost:5000/foo", "localhost:5000/foo:latest"),
        ("localhost:5000/foo:v1", "localhost:5000/foo:v1"),
    ]
    for given, want in cases:
        got = p.normalize_model(given)
        _assert(got == want, "normalize_model(%r) 應該是 %r，得到 %r" % (given, want, got))


# ═══════════════════════════════════════════════════════════
# run_smoke / run_ctx：結束碼的對應（把 _request 換掉，不連網）
# ═══════════════════════════════════════════════════════════


class _FakeRequest:
    """照順序回傳預錄好的 (status, parsed)；同時記下每次的 payload。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, path, payload, timeout):
        self.calls.append((path, payload))
        if not self.replies:
            raise AssertionError("_request 被呼叫的次數超出預期")
        return self.replies.pop(0)


@contextlib.contextmanager
def _patched(fake):
    real = p._request
    p._request = fake
    try:
        yield fake
    finally:
        p._request = real


def test_run_smoke_pass():
    with _patched(_FakeRequest((200, _resp()))):
        rc, _ = p.run_smoke("qwen3:4b", 32, 5)
    _assert(rc == p.EXIT_PASS, "正常應該回 0，得到 %d" % rc)


def test_run_smoke_failure_is_one():
    with _patched(_FakeRequest((200, _resp(content="", done_reason="length", eval_count=32)))):
        rc, _ = p.run_smoke("qwen3:4b", 32, 5)
    _assert(rc == p.EXIT_FAIL, "模型答錯應該是 1，得到 %d" % rc)


def test_run_smoke_model_not_pulled_is_environment_not_failure():
    """**模型的問題與環境的問題要走不同的結束碼。**

    「模型還沒拉」不是「模型答錯了」—— 一個是 pull 沒成功，一個是上游
    行為變了。混成同一個碼，部署腳本就會印出錯的診斷（D-018）。
    """
    fake = _FakeRequest((404, {"error": "model 'qwen3:4b' not found, try pulling it first"}))
    with _patched(fake):
        rc, reason = p.run_smoke("qwen3:4b", 32, 5)
    _assert(rc == p.EXIT_INDETERMINATE, "模型沒拉應該是 2，得到 %d" % rc)
    _assert_reason(reason, ["找不到模型"])


def test_run_smoke_cannot_reach_ollama_is_environment():
    with _patched(_FakeRequest((None, {"error": "Connection refused"}))):
        rc, reason = p.run_smoke("qwen3:4b", 32, 5)
    _assert(rc == p.EXIT_INDETERMINATE, "連不上應該是 2，得到 %d" % rc)
    _assert_reason(reason, ["連不上 ollama", "環境問題，不是模型問題"])


def test_run_smoke_other_http_error_is_broken():
    # 400/500 不是「模型答錯」，是請求本身有問題 —— 那是探針的狀態。
    with _patched(_FakeRequest((500, {"error": "internal"}))):
        rc, reason = p.run_smoke("qwen3:4b", 32, 5)
    _assert(rc == p.EXIT_BROKEN, "HTTP 500 應該是 3，得到 %d" % rc)
    _assert_reason(reason, ["這不是「模型答錯」"])


def test_run_smoke_http_error_is_broken_not_a_model_failure():
    """4xx/5xx 是「請求本身有問題」，不是「模型答錯了」。

    這條以前測的是「只有錯誤訊息提到 think 才重試」。那個重試分支跟著
    `think` 一起拿掉了（見探針檔頭），所以這裡改測那個分支底下的**判斷
    本身**：任何 HTTP 錯誤都回 3（探針壞了），而不是 1（上游變了）——
    回 1 會叫人去查模型，但該查的是這支探針送的請求。
    """
    fake = _FakeRequest((400, {"error": "invalid options"}))
    with _patched(fake):
        rc, reason = p.run_smoke("qwen3:4b", 512, 5)
    _assert(len(fake.calls) == 1, "不該重試，實際呼叫了 %d 次" % len(fake.calls))
    _assert(rc == p.EXIT_BROKEN, "應該是 3，得到 %d" % rc)
    _assert(rc != p.EXIT_FAIL, "不可以是 1 —— 那會叫人去查模型，但模型沒事")
    _assert_reason(reason, ["不是「模型答錯」"])


def test_run_smoke_sends_the_guards_that_make_the_judgement_possible():
    """請求本身就帶著防護，少了任何一個判定都會失去意義。"""
    fake = _FakeRequest((200, _resp()))
    with _patched(fake):
        p.run_smoke("qwen3:4b", 512, 5)
    _, payload = fake.calls[0]
    # **不可以送 `think`。** 原本送 `think: false`，理由是「拿掉推理那個
    # 變項」—— 實測是錯的：它只是把推理從 `thinking` 搬到 `content`，預算
    # 照樣被吃掉（eval_count 兩者都是 152），而且讓「content 非空」這條
    # 斷言可以被推理前言單獨滿足。不送才是應用程式真正的請求形狀。
    _assert(
        "think" not in payload,
        "不該送 think —— 它不會關掉推理，只會把推理搬進 content，"
        "讓「content 非空」這條斷言失去意義",
    )
    _assert(payload.get("stream") is False, "必須關掉 streaming，否則讀到的是一串 NDJSON")
    _assert(payload["options"].get("num_predict") == 512, "num_predict 必須照傳入的值送")
    _assert(payload["options"].get("temperature") == 0, "temperature=0 才可重現")


def test_run_ctx_mismatch_is_two_not_one():
    """**這條是刻意的設計決定，不是順手寫的。**

    num_ctx 沒生效時回 2 而不是 1：部署本身成功了、堆疊可以用，那是一個
    真實的量測結果，不是判準沒過（D-018 的 1 是「上游變了」）。但也絕對
    不是 0 —— 一個設了卻沒生效的伺服器端開關，正是這個專案一直踩到的
    「看起來已經套用、其實沒有」。
    """
    with _patched(_FakeRequest((200, _ps(ctx=4096)))):
        rc, reason = p.run_ctx("qwen3:4b", 8192, 5)
    _assert(rc == p.EXIT_INDETERMINATE, "num_ctx 沒生效應該是 2，得到 %d" % rc)
    _assert(rc != p.EXIT_FAIL, "不可以是 1 —— 1 的意思是「上游變了」")
    _assert(rc != p.EXIT_PASS, "更不可以是 0")
    _assert_reason(reason, ["沒有生效"])


def test_run_ctx_pass_and_environment():
    with _patched(_FakeRequest((200, _ps(ctx=8192)))):
        rc, _ = p.run_ctx("qwen3:4b", 8192, 5)
    _assert(rc == p.EXIT_PASS, "相符應該回 0，得到 %d" % rc)

    with _patched(_FakeRequest((None, {"error": "Connection refused"}))):
        rc, _ = p.run_ctx("qwen3:4b", 8192, 5)
    _assert(rc == p.EXIT_INDETERMINATE, "連不上應該是 2，得到 %d" % rc)

    with _patched(_FakeRequest((500, {"error": "internal"}))):
        rc, _ = p.run_ctx("qwen3:4b", 8192, 5)
    _assert(rc == p.EXIT_BROKEN, "HTTP 500 應該是 3，得到 %d" % rc)


def test_run_gpu_sends_its_own_load_before_reading_ps():
    """**順序與內容都是判準的一部分。**

    這支探針刻意自己觸發一次載入，不靠前一個 section 留下的殘留 —— 殘留取決於
    `.env` 的 OLLAMA_KEEP_ALIVE，而判準不可以取決於一個可設定的值。所以這裡
    逐項檢查：先 chat、再 ps，且 chat 帶著 keep_alive 與 1 個 token 的預算。
    """
    fake = _FakeRequest((200, _resp()), (200, _ps_gpu()))
    with _patched(fake):
        rc, _ = p.run_gpu("qwen3:4b", 5)

    _assert(rc == p.EXIT_PASS, "全在 GPU 上應該回 0，得到 %d" % rc)
    paths = [c[0] for c in fake.calls]
    _assert(paths == ["/api/chat", "/api/ps"], "順序必須是先載入再讀，實際是 %r" % paths)

    payload = fake.calls[0][1]
    _assert(
        payload.get("keep_alive") == p.GPU_CHECK_KEEP_ALIVE,
        "必須自己指定 keep_alive（%r）—— 不指定的話模型可能在讀 /api/ps 前就被"
        "卸載，判定會變成「量不到」而不是它真正的位置" % p.GPU_CHECK_KEEP_ALIVE,
    )
    # **刻意不拿常數跟自己比**：上面那條只證明「有送」，常數本身被改壞時它還是過。
    # 這裡管的是值：0 會在回應後立刻卸載（判定永遠是「量不到」），負數是永久保留
    # （這次檢查會改動機器的持久狀態，不只是量測）。
    keep = p.GPU_CHECK_KEEP_ALIVE
    _assert(
        isinstance(keep, str) and keep not in ("", "0") and not keep.startswith("-"),
        "keep_alive 必須是一個有限的正持續時間（讓模型撐過讀 /api/ps 那一刻），"
        "拿到 %r" % (keep,),
    )
    _assert(
        payload["options"].get("num_predict") == 1,
        "載入用的生成只該給 1 個 token：這裡要的是載入，不是答案",
    )
    _assert(payload.get("stream") is False, "必須關掉 streaming，否則讀到的是一串 NDJSON")


def test_run_gpu_on_cpu_is_one_not_two():
    """**#83 的核心斷言在結束碼上的那一半。**

    我們掛了 device reservation、告訴使用者這台在用 GPU，而 ollama 全部放在
    CPU。這不是「環境無從判定」（2），是我們自己的宣稱失效 —— 必須是 1，
    否則部署腳本會把它當成「量不到」放過去，靜默就回來了。
    """
    with _patched(_FakeRequest((200, _resp()), (200, _ps_gpu(size=4000000000, vram=0)))):
        rc, reason = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_FAIL, "跑在 CPU 上必須是 1，得到 %d" % rc)
    _assert(rc != p.EXIT_INDETERMINATE, "絕對不可以是 2 —— 那正是要消滅的靜默")
    _assert_reason(reason, ["docker-compose.gpu.yml"])


def test_run_gpu_partial_is_two():
    """部分卸載是真實量測，不是判準沒過 —— 比照 run_ctx 對 NOT_APPLIED 的推理。"""
    with _patched(_FakeRequest((200, _resp()), (200, _ps_gpu(size=4000000000, vram=1)))):
        rc, _ = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_INDETERMINATE, "部分卸載應該是 2，得到 %d" % rc)
    _assert(rc != p.EXIT_FAIL, "不可以是 1 —— GPU 確實在用")


def test_run_gpu_environment_and_broken_are_distinguished():
    """與 run_smoke 同一套分類：模型不在 → 2，其他 HTTP 錯誤 → 3。"""
    with _patched(_FakeRequest((None, {"error": "Connection refused"}))):
        rc, _ = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_INDETERMINATE, "連不上應該是 2，得到 %d" % rc)

    with _patched(_FakeRequest((404, {"error": "model 'qwen3:4b' not found"}))):
        rc, _ = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_INDETERMINATE, "模型沒拉應該是 2，得到 %d" % rc)

    with _patched(_FakeRequest((500, {"error": "internal"}))):
        rc, _ = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_BROKEN, "HTTP 500 應該是 3，得到 %d" % rc)


def test_run_gpu_unreadable_ps_is_indeterminate():
    """載入成功但 /api/ps 讀不到 —— 不可以退化成「假通過」。"""
    with _patched(_FakeRequest((200, _resp()), (200, {"models": "nope"}))):
        rc, _ = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_INDETERMINATE, "讀不到 /api/ps 應該是 2，得到 %d" % rc)
    _assert(rc != p.EXIT_PASS, "讀不到絕對不可以是 0")

    with _patched(_FakeRequest((200, _resp()), (500, {"error": "boom"}))):
        rc, _ = p.run_gpu("qwen3:4b", 5)
    _assert(rc == p.EXIT_BROKEN, "/api/ps 回 500 應該是 3，得到 %d" % rc)


# ═══════════════════════════════════════════════════════════
# main：參數錯誤走 3（不是 2 —— 2 的意思是「環境」）
# ═══════════════════════════════════════════════════════════


def test_main_argument_errors_exit_broken():
    cases = [
        [],
        ["smoke"],
        ["ctx"],
        ["ctx", "qwen3:4b"],
        ["不知名的section", "qwen3:4b"],
        ["smoke", "qwen3:4b", "不是數字"],
        ["smoke", "qwen3:4b", "0"],
        ["smoke", "qwen3:4b", "-1"],
        ["ctx", "qwen3:4b", "不是數字"],
    ]
    for argv in cases:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = p.main(["probe"] + argv)
        _assert(
            rc == 3,
            "參數錯誤 %r 應該回 3（不是 2 —— 2 是「環境無法判定」），得到 %d" % (argv, rc),
        )
        _assert(err.getvalue().strip() != "", "參數錯誤應該印出用法：%r" % (argv,))


def test_main_prints_the_verdict_and_returns_the_exit_code():
    out = io.StringIO()
    with _patched(_FakeRequest((200, _resp()))):
        with contextlib.redirect_stdout(out):
            rc = p.main(["probe", "smoke", "qwen3:4b"])
    _assert(rc == p.EXIT_PASS, "應該回 0，得到 %d" % rc)
    _assert("[PASS]" in out.getvalue(), "輸出應該有 PASS 標記：%r" % out.getvalue())


def main():
    tests = [
        (n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)
    ]
    failed = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed.append((name, str(e)))
            print("✗ %s\n    %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            failed.append((name, "非斷言例外：%r" % (e,)))
            print("✗ %s\n    非斷言例外：%r" % (name, e))
        else:
            print("✓ %s" % name)
    print()
    if failed:
        print("%d/%d 失敗" % (len(failed), len(tests)))
        return 1
    print(
        "%d/%d 全過 —— 四條斷言各有守衛，且「讀不到」與「答錯了」走不同的判定；"
        "GPU 那組另外守著「我們宣稱的事沒有發生」不可以被歸類成「量不到」"
        % (len(tests), len(tests))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
