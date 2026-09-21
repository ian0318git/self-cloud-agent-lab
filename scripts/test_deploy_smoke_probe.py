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
        "%d/%d 全過 —— 四條斷言各有守衛，且「讀不到」與「答錯了」走不同的判定"
        % (len(tests), len(tests))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
