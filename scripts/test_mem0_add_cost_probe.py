#!/usr/bin/env python3
"""mem0_add_cost_probe.py 的離線單元測試（不連網、不需容器、不需 mem0/ollama）。

這組測試對應兩條既有教訓：

1. **評分邏輯本身要被實測過**（README 第一階段清單、D-016）。一個只跑過
   「輸出為空」的評分器，在第一次真正跑到之前沒有任何防線。
2. **假失敗比漏報更糟**（D-016），而**漏報比誤報更難發現**（D-024 第八節）。
   所以這裡**兩邊都驗**：每一條判準 C1~C5 都有一個「該過的過」與一個
   「該擋的擋」測資。

還有一條是 D-026 第五節直接點名的：**突變測試抓到的最後一個洞，是斷言只
檢查了判準的編號，沒有檢查它講的是哪一種失敗。** 所以這裡每個「該擋的擋」
測資都斷言**可區辨的字串**（例如 C4 的「thinking 是空的」與「無法確認它
計入」是兩種不同的失敗），而不是只斷言 "C4" 有出現。

執行：python3 scripts/test_mem0_add_cost_probe.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mem0_add_cost_probe as p  # noqa: E402


def _evidence_ok():
    """2026-09-20 在真環境量到的形狀（見 D-027）。這是對照組。"""
    return {
        # exit_code() 只認「跑滿全部」的輪次 —— 所以一份「正常」的證據
        # 必須帶著它，否則每一條通過的測試都會被讀成「沒跑滿」。
        "sections_run": list(p.ALL_SECTIONS),
        "meta": {
            "model": "qwen3:4b",
            "embed_model": "qwen3-embedding:0.6b",
            "mem0_options": {"temperature": 0.1, "num_predict": 2000, "top_p": 0.1},
            "full_ctx": 16384,
            "context_length_before": 4096,
        },
        "truncation": {
            "truncated": True,
            "dropped": 5404,
            "default_pec": 4096,
            "full_pec": 9500,
            "reason": "同樣的 messages：mem0 的 options 只評估了 4096 個 token…",
            "full_ctx_took_effect": True,
        },
        "canary": {
            "head_seen": False,
            "tail_seen": True,
            "asymmetric": True,
            "answer_usable": True,
            "head_marker": "HEADMARKABC123456789",
            "tail_marker": "TAILMARKXYZ987654321",
            "reason": "開頭看不到、結尾看得到…",
            # 第二次呼叫是**位置對照**：兩個 marker 對調，所以看到的是
            # 換過來的那一個。少了這一格，C3 就只剩一半 —— 詳見 grade()。
            "reversed": {
                "head_seen": True,
                "tail_seen": False,
                "asymmetric": True,
                "answer_usable": True,
                "reason": "這一端看得到、另一端看不到 —— 與位置對調之後相符…",
            },
        },
        "thinking": {
            "thinking_present": True,
            "counts_toward_eval": True,
            "saved_tokens": 180,
            "reason": "thinking 有 700 個字元…",
        },
        "calls": {"first_add_chat": 1, "first_add_embed": 2},
        "first_add": {
            "memories": 3,
            "wall": 640.0,
            "eval_count": 2000,
            "prompt_eval_count": 4096,
            "eval_duration": 630.0,
            "prompt_eval_duration": 8.0,
        },
        "second_add": {"memories": 0, "wall": 610.0, "eval_count": 2000,
                       "prompt_eval_count": 4200},
        # **這裡原本寫著 `"prompt_grew": 104` —— 一個發明出來的數字。**
        # 沒有任何判準讀它，所以它從來沒被發現是假的；而它之所以是假的，
        # 是因為它用 `prompt_eval_count` 相減算出來的，而那兩個數字都被
        # 截斷到同一個上限（見 prompt_chars()）。長度差只留在攔截到的
        # messages 裡。
        "duplicate": {"second_add_chat": 1,
                      "prompt_chars_first": 21000,
                      "prompt_chars_second": 21360,
                      "prompt_grew_chars": 360},
        "cost_split": {
            "total": 638.0,
            "timings": {"llm_eval": 638.0, "embed": 1.2},
            "shares": {"llm_eval": 0.998, "embed": 0.002},
        },
    }


def _set(evidence, path, value):
    """把 evidence 裡某個路徑的值換掉，回傳同一個 dict（方便連鎖呼叫）。"""
    node = evidence
    keys = path.split(".")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return evidence


def _fails(evidence, must_mention):
    """跑 grade()，要求它不通過，而且問題訊息裡要出現 must_mention。"""
    passed, problems = p.grade(evidence)
    assert not passed, "預期會擋下來，結果通過了：%r" % (problems,)
    joined = " || ".join(problems)
    assert must_mention in joined, (
        "有擋下來，但講的不是預期的那種失敗。\n"
        "  預期訊息要含：%r\n  實際訊息：%s" % (must_mention, joined)
    )
    return problems


# ── truncation_verdict ──────────────────────────────────


def test_truncation_detects_shortfall():
    v = p.truncation_verdict(4096, 9500)
    assert v["truncated"] is True
    assert v["dropped"] == 5404
    # 訊息要講出兩個數字，人才有辦法自己去查。
    assert "4096" in v["reason"] and "9500" in v["reason"]


def test_truncation_reports_intact():
    v = p.truncation_verdict(9500, 9500)
    assert v["truncated"] is False
    assert v["dropped"] == 0


def test_truncation_refuses_to_call_reversed_result_intact():
    """預設評估得**比**給足 num_ctx 還多 —— 這是非預期，不可以當成「沒截斷」。

    這一條是 D-016 的形狀：把「量到奇怪的東西」講成「沒問題」，
    就是假失敗的鏡像 —— 假通過。
    """
    v = p.truncation_verdict(9500, 4096)
    assert v["truncated"] is None, "非預期的方向不可以回 False"
    assert "非預期" in v["reason"]


def test_truncation_missing_counter_is_indeterminate():
    assert p.truncation_verdict(None, 9500)["truncated"] is None
    assert p.truncation_verdict(4096, None)["truncated"] is None
    assert p.truncation_verdict(0, 0)["truncated"] is None


# ── canary_reading ──────────────────────────────────────


def test_control_group_effect_confirms_requested_ctx():
    assert p.control_group_effect(16384, 16384) is True


def test_control_group_effect_rejects_different_ctx():
    """B 要求 16384、/api/ps 卻說 4096 —— 對照組不成立，這是假通過的入口。"""
    assert p.control_group_effect(4096, 16384) is False


def test_control_group_effect_unreadable_is_unknown_not_false():
    """讀不到 /api/ps 是「無法判定」。回 False 會讓 C2 擋下一個
    其實沒問題的環境 —— 那是假失敗（D-016）。"""
    assert p.control_group_effect(None, 16384) is None


def test_control_group_effect_zero_is_unknown_not_false():
    """`if not ctx_after_full` 與 `if ctx_after_full is None` 在這裡分岔：
    寫成後者的話，0 會走到 `0 == 16384` → False → 假失敗。
    這一條把那個分岔釘住。"""
    assert p.control_group_effect(0, 16384) is None


def test_memories_written_counts_results_not_keys():
    """這是實際犯過的錯：`len(add() 的回傳值)` 量到的是 **key 的數量**，
    永遠是 1。「抽出 1 則記憶」看起來完全合理，所以不會有人發現。"""
    assert p.memories_written({"results": ["a", "b", "c"]}) == 3


def test_memories_written_zero_is_a_real_answer():
    assert p.memories_written({"results": []}) == 0


def test_memories_written_unknown_shape_is_none_not_zero():
    """`{"results": "..."}` 這種形狀要回 None（不知道），不是 0（沒抽到）。
    合併的話，「上游改了回傳格式」會看起來像「這次沒抽到記憶」。"""
    assert p.memories_written({"results": "not a list"}) is None
    assert p.memories_written({}) is None
    assert p.memories_written(None) is None


def test_memories_written_accepts_a_bare_list():
    """舊版回傳 list。上游改回去時要還能數，而不是靜默變成 None。"""
    assert p.memories_written(["a", "b"]) == 2


def test_truncated_length_matches_the_measured_points():
    """真公式的在實際量到的那些 num_ctx 上的值。

    2050 是這一整節的起點：mem0 的抽取 prompt 在預設 context 下被砍到
    2050，而它是 `4096/2 + 2`，不是 `4096 − 2000`。
    """
    assert p.truncated_length(4096) == 2050
    assert p.truncated_length(2048) == 1026
    assert p.truncated_length(16384) == 8194


def test_truncated_length_does_not_depend_on_num_predict():
    """**這一條是那次錯誤假設的回歸測試。**

    舊公式是 `num_ctx − num_predict − 46`，它在 num_predict=2000、num_ctx=4096
    時**剛好**也給 2050，所以看起來被驗證過。真正拆穿它的是固定 num_ctx 去變
    num_predict 的那個實驗：16／1000／2000 三次量到的都是 2050。

    這裡把那個實驗的結果寫成斷言：函式的簽名裡**沒有** num_predict 這個參數，
    所以傳不進去 —— 用 TypeError 把「不小心加回去」擋在門口。
    """
    assert p.truncated_length(4096) == 2050
    try:
        p.truncated_length(4096, num_predict=2000)
    except TypeError:
        pass
    else:
        raise AssertionError("truncated_length 竟然認得 num_predict —— "
                             "那個公式已經被實測推翻了")


def test_truncated_length_keeps_num_keep_tokens():
    """公式裡的 num_keep 是「先保留開頭幾個 token」的那一項。

    少了它（夾成 0）算出來的 2048 跟 2050 只差兩個 token —— 看起來完全
    正常，但 2050 正是 D-027 記下來的那個數字，不能少算那兩個。
    """
    assert p.truncated_length(4096, num_keep=4) == 2050
    # num_keep 越大，保留越多、砍完越長。
    assert p.truncated_length(4096, num_keep=100) > p.truncated_length(4096, num_keep=4)
    # 預設值就是 4（api/types.go:1143）—— 不給參數時要跟明給一樣。
    assert p.truncated_length(4096) == p.truncated_length(4096, num_keep=4)


def test_truncated_length_survives_degenerate_contexts():
    """探針跑幾十分鐘，最後才因為除以零或負數炸掉等於整輪白費。"""
    assert p.truncated_length(1) == 0
    assert p.truncated_length(0) == 0
    for ctx in (2, 3, 5, 8, 16, 64):
        assert 0 < p.truncated_length(ctx) < ctx, ctx


def test_min_ctx_required_is_one_more_than_the_prompt():
    """門檻是 `len(tokens) ≧ num_ctx`，所以「進得去」的最小值就是 +1。

    少加那個 1，`num_ctx = prompt_tokens` 會剛好落在被砍的那一側，
    而算出來的數字看起來完全正常。
    """
    assert p.min_ctx_required(8047) == 8048
    assert p.min_ctx_required(0) == 1
    # 那個 1 不是「大概留一點」：它正好是夾縫測試夾出來的那一格。
    assert p.min_ctx_required(8047) > 8047


def test_min_ctx_required_ignores_any_notion_of_generation_headroom():
    """舊版會把 num_predict 加回去（`... + num_predict`），因為它以為生成
    的空間是從 prompt 的預算裡扣的。現在知道不是，所以那個參數不該存在。"""
    try:
        p.min_ctx_required(8047, num_predict=2000)
    except TypeError:
        pass
    else:
        raise AssertionError("min_ctx_required 竟然認得 num_predict —— "
                             "留給生成的空間不會從 prompt 的預算裡扣")


def test_truncation_limit_verdict_matches():
    r = p.truncation_limit_verdict(4096, 2050)
    assert r["matches"] is True, r
    assert r["expected"] == 2050, r
    assert "相符" in r["reason"], r["reason"]


def test_truncation_limit_verdict_flags_a_disagreement():
    """量到的跟閉式解不同 → 上游變了。訊息要講**不要套這條式子**，
    不能只說「不一樣」—— 下一個讀的人要能立刻知道該怎麼辦。"""
    r = p.truncation_limit_verdict(4096, 2096)
    assert r["matches"] is False, r
    assert "不要" in r["reason"], r["reason"]
    assert "2096" in r["reason"] and "2050" in r["reason"], r["reason"]


def test_truncation_limit_verdict_refuses_when_nothing_was_cut():
    """**這一條守的是假失敗（D-016）。**

    prompt 沒被截斷時，prompt_eval_count 是它的完整長度，一定不等於
    預測的砍完長度。若不特別擋掉，「prompt 準備得太短」這個失誤會長成
    「公式錯了」—— 而公式其實好好的，會被誤修。
    """
    r = p.truncation_limit_verdict(2048, 7000)
    assert r["matches"] is None, r
    assert "沒有截斷" in r["reason"], r["reason"]


def test_truncation_limit_verdict_without_a_reading():
    r = p.truncation_limit_verdict(4096, None)
    assert r["matches"] is None, r
    assert r["expected"] == 2050, r


def test_threshold_bracket_holds_at_p_and_p_plus_one():
    """夾縫測試成立時的樣子：num_ctx=P 砍、num_ctx=P+1 完整。

    用真的量到的形狀當例子：一份 700 個 token 的小 prompt。
    """
    P = 700
    r = p.threshold_bracket_verdict(P, p.truncated_length(P), P)
    assert r["consistent"] is True, r
    assert r["cut_as_predicted"] is True
    assert r["above_untouched"] is True
    assert str(P + 1) in r["reason"], r["reason"]


def test_threshold_bracket_rejects_the_half_of_num_ctx_rule():
    """**這一條是「上限大約是 num_ctx 的一半」那條規則的否證。**

    若門檻是 num_ctx/2，num_ctx=P+1 時 P ≧ (P+1)/2 仍然成立 → 還是會砍。
    所以夾縫測試的第二次會量到「被砍」，而不是原封不動。
    這是 C2 的 A/B 做不到的事：4096 砍、16384 不砍，對兩種規則都成立。
    """
    P = 700
    r = p.threshold_bracket_verdict(P, p.truncated_length(P), p.truncated_length(P + 1))
    assert r["consistent"] is False, r
    assert r["above_untouched"] is False, r
    assert "門檻不是 num_ctx" in r["reason"], r["reason"]


def test_threshold_bracket_rejects_a_constant_offset_rule():
    """門檻 = num_ctx − c 的規則：只要 c ≧ 1，num_ctx=P+1 就容不下 P。

    這正是舊公式的形狀（c 是 num_predict + 邊界）。它會讓第二次量到
    「被砍」—— 跟一半的規則一樣被夾縫擋掉。
    """
    P = 700
    c = 46
    r = p.threshold_bracket_verdict(P, p.truncated_length(P),
                                    p.truncated_length(P + 1 - c))
    assert r["consistent"] is False, r
    assert "門檻不是 num_ctx" in r["reason"], r["reason"]


def test_threshold_bracket_flags_a_threshold_below_p():
    """num_ctx=P 就已經完整進去了 → 門檻比 P 小，這一節的前提不成立。"""
    P = 700
    r = p.threshold_bracket_verdict(P, P, P)
    assert r["consistent"] is False, r
    assert r["cut_as_predicted"] is False, r


def test_threshold_bracket_without_readings_is_indeterminate():
    """讀不到數字是「無法判定」，不是「規則錯了」—— 兩者的結束碼不同
    （D-018：2 是環境、1 是判準）。合併成同一個 False 會製造假失敗。"""
    for a, b in ((None, 700), (700, None), (None, None)):
        r = p.threshold_bracket_verdict(700, a, b)
        assert r["consistent"] is None, (a, b, r)
        assert "無法判定" in r["reason"], r["reason"]


def test_canary_head_dropped_tail_kept():
    r = p.canary_reading("I can see TAILMARKXYZ987654321", "HEADMARKABC", "TAILMARKXYZ987654321")
    assert r["head_seen"] is False
    assert r["tail_seen"] is True
    assert r["asymmetric"] is True
    # 斷言**可區辨**的片語，不是「開頭」這種兩個分支都會出現的字。
    # 寫突變清單時發現原本的寫法擋不住「把兩個方向對調」的突變。
    assert "模型自己的指令" in r["reason"]


def test_canary_both_seen_is_not_asymmetric():
    r = p.canary_reading("HEADMARKABC and TAILMARKXYZ", "HEADMARKABC", "TAILMARKXYZ")
    assert r["asymmetric"] is False
    assert r["head_seen"] and r["tail_seen"]


def test_canary_neither_seen_is_indeterminate():
    """**「兩端都看不到」是「不知道」，不是「對稱」。**

    它同時容納兩種相反的世界：模型看不到（截斷吃掉整個系統提示詞），與
    模型沒有照做。原本這裡回 asymmetric=False，把一個不確定的觀測講成一個
    確定的結果 —— run2 就是這樣把「沒有量到」印成「量到：兩端都看不到」。
    """
    r = p.canary_reading("I cannot see any markers", "HEADMARKABC", "TAILMARKXYZ")
    assert r["asymmetric"] is None, r
    assert r["head_seen"] is False and r["tail_seen"] is False


def test_canary_no_answer_is_indeterminate():
    r = p.canary_reading(None, "H", "T")
    assert r["asymmetric"] is None
    assert r["head_seen"] is None


def test_canary_reversed_direction_is_reported_as_unusual():
    """反過來（開頭看得到、結尾看不到）要講出來，不是靜靜地算通過。"""
    r = p.canary_reading("HEADMARKABC", "HEADMARKABC", "TAILMARKXYZ")
    assert r["asymmetric"] is True
    assert r["tail_seen"] is False
    assert "比較不尋常" in r["reason"]


def test_canary_reversed_layout_says_the_same_thing():
    """**位置對調之後，句子講的還是同一件事。**

    這是 2026-09-21 的 C3 重測當場抓到的：判定欄位全對（`grade()` 要的
    `rev.head_seen is True and rev.tail_seen is False` 完全吻合），但印出來
    的那句話是「context 是從**結尾**被丟掉的。這比較不尋常」—— **與事實
    相反**。原因是它照 marker 的**標籤**下判斷，而在鏡像那一次，`head` 標籤
    坐在**結尾**。

    這一條測的就是修法本身：兩次排列講的是同一個 context 的同一端，所以
    句子必須一致。少了它，「標籤 vs 位置」這個混淆可以整個回來而沒有人
    知道 —— 因為欄位是對的。
    """
    def claim(reason):
        """句子裡「主張」的那一段 —— 括號裡的標籤註記不算。"""
        return reason.split("（看得到的 marker")[0]

    # 第一次：head 標籤在開頭。開頭看不到、結尾看得到。
    first = p.canary_reading("TAILMARKXYZ", "HEADMARKABC", "TAILMARKXYZ",
                             head_first=True)
    # 第二次：對調。head 標籤在結尾 —— 所以「head 看得到」指的是結尾看得到。
    rev = p.canary_reading("HEADMARKABC", "HEADMARKABC", "TAILMARKXYZ",
                           head_first=False)
    assert claim(first["reason"]) == claim(rev["reason"]), \
        (first["reason"], rev["reason"])
    assert "模型自己的指令" in rev["reason"]
    assert "比較不尋常" not in rev["reason"]
    # 回傳值仍然報**標籤**（那是觀測到的原始事實）；只有句子講位置。
    assert rev["head_seen"] is True and rev["tail_seen"] is False
    # 註記要指名**看得到**的那一個標籤 —— 那正是人工核對時要對的字。
    assert "是 head" in rev["reason"], rev["reason"]
    assert "是 tail" in first["reason"], first["reason"]


def test_canary_reversed_layout_end_dropped_is_unusual():
    """鏡像裡真正該喊「不尋常」的是另一半 —— 開頭看得到、結尾看不到。

    對調之後要得到這個結果，看到的是坐在**開頭**的 `tail` 標籤。
    """
    rev = p.canary_reading("TAILMARKXYZ", "HEADMARKABC", "TAILMARKXYZ",
                           head_first=False)
    assert rev["head_seen"] is False and rev["tail_seen"] is True
    assert "比較不尋常" in rev["reason"]
    assert "從**結尾**被丟掉" in rev["reason"]


def test_canary_layout_does_not_change_symmetric_readings():
    """對稱的兩種結果與位置無關 —— 它們講的是「有沒有截斷」，不是哪一端。"""
    for hf in (True, False):
        both = p.canary_reading("HEADMARKABC TAILMARKXYZ", "HEADMARKABC",
                                "TAILMARKXYZ", head_first=hf)
        assert both["asymmetric"] is False, (hf, both)
        neither = p.canary_reading("nothing here", "HEADMARKABC",
                                   "TAILMARKXYZ", head_first=hf)
        assert neither["asymmetric"] is None, (hf, neither)


# ── prompt_chars ────────────────────────────────────────

def test_prompt_chars_sums_message_content():
    call = {"params": {"messages": [
        {"role": "system", "content": "abcde"},
        {"role": "user", "content": "123"},
    ]}}
    assert p.prompt_chars(call) == 8


def test_prompt_chars_unreadable_is_none_not_zero():
    """**讀不到回 None，不回 0。**

    `0` 是一個合法的長度（空 prompt）。把「讀不到」併進去，就會讓儀器失效
    長得像一個量測結果 —— 這正是第十節那一串缺陷的共同形狀。
    """
    assert p.prompt_chars({}) is None
    assert p.prompt_chars({"params": {}}) is None
    assert p.prompt_chars({"params": {"messages": []}}) is None
    assert p.prompt_chars({"params": {"messages": [{"role": "user"}]}}) is None
    assert p.prompt_chars({"params": {"messages": ["not a dict"]}}) is None


def test_prompt_chars_empty_contents_is_zero():
    """真的量到 0 的時候要回 0 —— 那是量測結果，不是讀不到。"""
    call = {"params": {"messages": [{"role": "user", "content": ""}]}}
    assert p.prompt_chars(call) == 0


# ── canary_answer_usable／canary_verdict ─────────────────
# run2 的失敗：兩次 canary 都停在 num_predict（ollama 日誌：
# `eval time = ... / 2000 tokens` 兩次），而「模型沒講完」被讀成了
# 「兩端都看不到」。這一組測試就是要讓那個讀法不可能再發生。


def _chat(content, eval_count):
    """_chat() 回傳值的縮影 —— 只留這幾條判準會用到的欄位。"""
    return {"content": content, "eval_count": eval_count, "thinking_chars": 0}


def test_canary_answer_usable_accepts_a_finished_answer():
    ok, why = p.canary_answer_usable(_chat('{"markers": []}', 40), 2000)
    assert ok is True, why


def test_canary_answer_usable_rejects_exhausted_budget():
    """停在 num_predict 上＝被**預算**切斷，不是自己講完 —— 沒有證據。

    而且回覆非空**不能**讓它變回可用：推理模型的 thinking 也佔這個預算，
    所以「有回覆」與「講完了」是兩件事。
    """
    ok, why = p.canary_answer_usable(_chat('{"markers": []}', 2000), 2000)
    assert ok is False, why
    # 要講出是哪一個數字讓它不成立，不是只說「不能用」。
    assert "2000" in why and "num_predict" in why, why


def test_canary_answer_usable_rejects_empty_content():
    """thinking 不進 content：生了一堆 token 但回覆是空的，也沒有答案。"""
    ok, why = p.canary_answer_usable(_chat("   \n", 12), 2000)
    assert ok is False, why
    assert "空" in why, why


def test_canary_answer_usable_is_indeterminate_without_counts():
    """讀不到數字是「無法判定」，不是「不能用」—— 兩者的結束碼不同（D-018）。

    把 None 當成 False 會製造假失敗（D-016）；這裡反過來，把「讀不到」
    當成「可用」會製造**假成功**，那更糟。
    """
    for chat, num_predict in ((None, 2000), ({}, 2000), ({"content": None}, 2000),
                              ({"content": "x", "eval_count": None}, 2000),
                              ({"content": "x", "eval_count": 5}, None)):
        ok, why = p.canary_answer_usable(chat, num_predict)
        assert ok is None, (chat, num_predict, ok, why)


def test_canary_verdict_unfinished_never_says_both_invisible():
    """**這一條是 run2 的迴歸測試。**

    沒講完的時候，理由裡不可以出現看起來像量測結果的話，而要明說
    「沒有量到」；asymmetric 必須是 None，不是 False。
    """
    r = p.canary_verdict(_chat("", 2000), "HEADMARKABC", "TAILMARKXYZ", 2000)
    assert r["asymmetric"] is None, r
    assert r["answer_usable"] is False, r
    assert "沒有量到" in r["reason"], r["reason"]
    assert "兩端都看不到" not in r["reason"], r["reason"]


def test_canary_verdict_passes_through_when_usable():
    r = p.canary_verdict(_chat("I can see TAILMARKXYZ987654321", 40),
                         "HEADMARKABC", "TAILMARKXYZ987654321", 2000)
    assert r["answer_usable"] is True, r
    assert r["tail_seen"] is True and r["asymmetric"] is True, r


# ── parse_sections ──────────────────────────────────────


def test_parse_sections_default_is_everything():
    want, err = p.parse_sections(None)
    assert err is None
    assert want == p.ALL_SECTIONS


def test_parse_sections_reorders_to_dependency_order():
    """順序照 ALL_SECTIONS，不照使用者打的 —— 那是依賴順序。"""
    want, err = p.parse_sections("add,C3")
    assert err is None, err
    assert want == ("C3", "add"), want


def test_parse_sections_rejects_unknown_name():
    """**不靜默忽略。** 打錯字而它安靜地不跑，整輪會變成「什麼都沒量到卻
    什麼都沒抱怨」—— 那是這個功能最危險的失敗方式。"""
    want, err = p.parse_sections("C3,C9")
    assert want == (), want
    assert "C9" in err and "不認識" in err, err


def test_parse_sections_rejects_empty():
    want, err = p.parse_sections("  ,  ")
    assert want == (), want
    assert "空" in err, err


# ── exit_code ───────────────────────────────────────────


def test_exit_code_pass_and_fail():
    e = _evidence_ok()
    assert p.exit_code(e, True) == p.EXIT_PASS
    assert p.exit_code(e, False) == p.EXIT_FAIL


def test_exit_code_broken_when_canary_not_measured():
    """儀器壞掉是 3（這支腳本該修），**不是 1（上游變了）**。

    run2 差一點就是這個下場：C3 的生成預算被 thinking 吃光，而它會被
    回報成「判準沒過，重讀 D-027」。
    """
    e = _set(_evidence_ok(), "canary.answer_usable", False)
    assert p.exit_code(e, False) == p.EXIT_BROKEN
    assert p.EXIT_BROKEN == 3


def test_exit_code_indeterminate_when_canary_unreadable():
    e = _set(_evidence_ok(), "canary.answer_usable", None)
    assert p.exit_code(e, False) == p.EXIT_INDETERMINATE


def test_exit_code_partial_run_is_never_pass():
    """只跑一部分**永遠不算通過** —— 就算傳進來的 passed 是 True。"""
    e = _set(_evidence_ok(), "sections_run", ["C3"])
    assert p.exit_code(e, True) == p.EXIT_INDETERMINATE


def test_exit_code_missing_sections_run_is_indeterminate():
    """讀不到 sections_run 就當沒跑滿 —— fail-closed，不是預設全跑。"""
    e = _evidence_ok()
    del e["sections_run"]
    assert p.exit_code(e, True) == p.EXIT_INDETERMINATE


def test_exit_code_fatal_is_indeterminate():
    e = _set(_evidence_ok(), "fatal", "連不上 ollama")
    assert p.exit_code(e, False) == p.EXIT_INDETERMINATE


# ── thinking_verdict ────────────────────────────────────


def test_thinking_counts_toward_eval():
    v = p.thinking_verdict(700, 2000, 0, 1820)
    assert v["thinking_present"] is True
    assert v["counts_toward_eval"] is True
    assert v["saved_tokens"] == 180


def test_thinking_absent_is_reported():
    v = p.thinking_verdict(0, 2000, 0, 2000)
    assert v["thinking_present"] is False
    assert v["counts_toward_eval"] is False


def test_thinking_present_but_no_eval_drop_is_not_confirmed():
    """thinking 有內容但 eval_count 沒降 —— 不可以回 True。"""
    v = p.thinking_verdict(700, 2000, 700, 2000)
    assert v["thinking_present"] is True
    assert v["counts_toward_eval"] is False
    assert v["saved_tokens"] is None


def test_thinking_without_control_is_indeterminate():
    """沒有 think=False 的對照（例如模型不支援），不可以宣稱確認。"""
    v = p.thinking_verdict(700, 2000, None, None)
    assert v["thinking_present"] is True
    assert v["counts_toward_eval"] is None
    assert "無法確認" in v["reason"]


# ── call_summary / cost_split ───────────────────────────


def test_call_summary_counts_by_kind():
    calls = [
        {"kind": "embed", "wall": 0.5},
        {"kind": "chat", "wall": 10.0, "prompt_eval_count": 4096, "eval_count": 2000},
        {"kind": "embed", "wall": 0.2},
    ]
    s = p.call_summary(calls)
    assert s["chat"] == 1
    assert s["embed"] == 2
    assert s["chat_prompt_tokens"] == 4096
    assert s["chat_eval_tokens"] == 2000
    assert abs(s["chat_wall"] - 10.0) < 1e-9


def test_call_summary_handles_empty():
    s = p.call_summary([])
    assert s["chat"] == 0 and s["embed"] == 0


def test_call_summary_counts_every_call_not_just_the_last():
    """兩次呼叫要算成 2，token 要相加 —— 不是只留最後一次的值。

    這一條是為 C5 而寫的：C5 問的是「第二次 add 有沒有再發一次呼叫」，
    如果計數器只記得住一次，那個問題永遠會得到「有」這個答案。
    兩次的 token 刻意給不同值，加總與取最後一筆才分得出來。
    """
    calls = [
        {"kind": "chat", "wall": 10.0, "prompt_eval_count": 4096, "eval_count": 2000},
        {"kind": "chat", "wall": 9.0, "prompt_eval_count": 4200, "eval_count": 1500},
    ]
    s = p.call_summary(calls)
    assert s["chat"] == 2
    assert s["chat_eval_tokens"] == 3500
    assert s["chat_prompt_tokens"] == 8296
    assert abs(s["chat_wall"] - 19.0) < 1e-9


def test_cost_split_shares_sum_to_one():
    c = p.cost_split({"llm_eval": 90.0, "embed": 10.0})
    assert abs(c["total"] - 100.0) < 1e-9
    assert abs(sum(c["shares"].values()) - 1.0) < 1e-9


def test_cost_split_empty_is_zero_not_crash():
    c = p.cost_split({})
    assert c["total"] == 0.0
    assert c["shares"] == {}


# ── grade：該過的過 ─────────────────────────────────────


def test_grade_passes_on_measured_shape():
    passed, problems = p.grade(_evidence_ok())
    assert passed, "實測形狀應該要通過，卻被擋：%r" % (problems,)
    assert problems == []


# ── grade：該擋的擋（每一條都要有）──────────────────────


def test_grade_blocks_when_call_count_is_not_one():
    _fails(_set(_evidence_ok(), "calls.first_add_chat", 2),
           "README 第 3 項說一次")


def test_grade_blocks_when_not_truncated():
    """沒截斷是好事，但它代表 README 的危險敘述要重寫 —— 所以判準要叫。"""
    _fails(_set(_evidence_ok(), "truncation.truncated", False),
           "沒有被截斷")


def test_grade_blocks_when_truncation_indeterminate():
    _fails(_set(_evidence_ok(), "truncation.truncated", None),
           "沒有被截斷")


def test_grade_blocks_when_control_group_ctx_did_not_take_effect():
    """B 沒跑在 FULL_CTX 時，A/B 會被截成同一長度 → a == b → 判準會回
    「沒有截斷」。那是假通過，所以對照組失效必須自己是一條判準。"""
    e = _set(_evidence_ok(), "truncation.full_ctx_took_effect", False)
    _fails(e, "沒有生效")


def test_grade_allows_unknown_control_group_ctx():
    """讀不到 /api/ps 是「無法判定」，不是「沒生效」。
    把 None 當成 False 會是假失敗（D-016）—— 所以這一條要**過**。"""
    e = _evidence_ok()
    e["truncation"]["full_ctx_took_effect"] = None
    passed, problems = p.grade(e)
    assert passed, "讀不到 num_ctx 不該擋下來，結果擋了：%r" % (problems,)


def test_grade_blocks_when_canary_symmetric():
    e = _set(_evidence_ok(), "canary.asymmetric", False)
    _fails(e, "開頭看不到、結尾看得到")


def test_grade_blocks_when_canary_head_visible():
    """開頭看得到＝被砍的是結尾 —— 危險性質不同，要擋。"""
    e = _evidence_ok()
    e["canary"]["head_seen"] = True
    e["canary"]["tail_seen"] = False
    _fails(e, "開頭看不到、結尾看得到")


def test_grade_distinguishes_not_measured_from_contradicted():
    """「沒有量到」與「量到反例」不可以是同一句話 —— 前者是探針該修
    （結束碼 3），後者是上游變了（結束碼 1）。訊息必須自己講出差別。"""
    e = _evidence_ok()
    e["canary"].update({"answer_usable": False, "asymmetric": None,
                        "head_seen": None, "tail_seen": None,
                        "reason": "這一節沒有量到 —— 生成停在 2000 個 token…"})
    passed, problems = p.grade(e)
    assert not passed
    assert "沒有量到" in problems[0], problems
    # 沒有把它講成方向判準的失敗 —— 那會讓人去重讀上游，而該修的是探針。
    assert not any("開頭看不到、結尾看得到" in m for m in problems), problems


def test_grade_blocks_when_thinking_absent():
    _fails(_set(_evidence_ok(), "thinking.thinking_present", False),
           "thinking 是空的")


def test_grade_blocks_when_thinking_not_confirmed():
    """這一條與上一條是**不同的失敗**，訊息不可以一樣。

    突變測試（D-026 第五節）抓到的正是這種洞：只斷言 "C4" 有出現的話，
    把 thinking_present 的判斷改成常數也會過。
    """
    e = _set(_evidence_ok(), "thinking.counts_toward_eval", False)
    _fails(e, "無法確認它計入 eval_count")


def test_grade_blocks_when_second_add_makes_no_call():
    """第二次 add 變成 0 次呼叫＝上游把去重移到 LLM 之前了。

    那是第四階段會想做的事，所以它發生時要有人講話，不能靜靜地繼續。
    """
    _fails(_set(_evidence_ok(), "duplicate.second_add_chat", 0),
           "去重移到 LLM 之前")


def test_grade_blocks_on_empty_evidence():
    passed, problems = p.grade({})
    assert not passed
    assert "沒有收到任何證據" in problems[0]


def test_grade_names_missing_sections_instead_of_claiming_a_verdict():
    """整節沒量到時，不可以讓各條判準各自喊「沒有被截斷」「thinking 是空的」
    —— 那些是**與事實相反**的敘述（實際上沒量到，只是 None 被拼進句子）。
    要一次講清楚缺少哪幾節，並明說這不是判準沒過。"""
    e = _evidence_ok()
    del e["canary"]
    passed, problems = p.grade(e)
    assert not passed
    assert "缺少這幾節的量測結果" in problems[0], problems
    assert "C3 canary" in problems[0], problems
    assert "不是判準沒過" in problems[0], problems
    # 沒有把它講成 C3 的判準失敗。
    assert not any("開頭看不到、結尾看得到" in m for m in problems), problems


# ── observations：非致命，但不可以是空的 ────────────────


def test_observations_empty_evidence_returns_nothing():
    """空證據回空清單 —— 這一條擋的是「無條件塞一則說明」。

    chroma_dims_probe 的 observations() 一開始就是這樣壞的（D-026 第二節）。
    """
    assert p.observations({}) == []


def test_observations_reports_the_numbers():
    notes = p.observations(_evidence_ok())
    joined = " ".join(notes)
    assert "qwen3:4b" in joined
    assert "4096" in joined          # 當下生效的 num_ctx
    assert "5404" in joined          # 截斷幅度
    assert "tok/s" in joined         # 速率
    assert notes, "有證據時 observations 不可以是空的"


def test_observations_survives_partial_evidence():
    """只有一半的證據也要能印，不可以丟例外。"""
    e = _evidence_ok()
    for k in ("first_add", "second_add", "cost_split", "canary", "thinking"):
        e.pop(k, None)
    notes = p.observations(e)
    assert isinstance(notes, list)


def test_observations_second_add_does_not_name_an_unobserved_cause():
    """**第五處。** 這一則原本寫著差別「來自 Phase 1 檢索回來的既有記憶」。

    那是一個**沒有觀測到的成因**，而且與這一輪的資料衝突：Phase 1 檢索的是
    向量庫，而這一輪抽出 0 筆、向量庫是空的。它之所以讀起來通順，是因為
    它是一個**合理的**故事 —— 而合理的錯故事比明顯的錯更難發現。

    這一條同時釘住兩半：不可以指名那個成因，不可以再出現相減的
    `prompt_eval_count` 差值。
    """
    notes = p.observations(_evidence_ok())
    second = [n for n in notes if "第二次 add" in n]
    assert second, notes
    joined = " ".join(second)
    assert "Phase 1" not in joined, joined
    # 要印的是真的量到的：兩次攔截下來的 messages 長度。
    assert "21000" in joined and "21360" in joined, joined
    assert "+360" in joined, joined


def test_observations_second_add_declines_when_messages_unreadable():
    """讀不到攔截到的 messages 時要說「無從比較」，**不是**說沒有變長。"""
    e = _evidence_ok()
    for k in ("prompt_chars_first", "prompt_chars_second", "prompt_grew_chars"):
        e["duplicate"].pop(k, None)
    second = [n for n in p.observations(e) if "第二次 add" in n]
    assert second, e
    assert "無從比較" in second[0], second[0]


def test_duplicate_never_derives_growth_from_prompt_eval_count():
    """`prompt_grew_chars` 只能來自 messages 的字元數。

    兩次 `prompt_eval_count` 都被截斷到同一個上限，相減永遠是 0 ——
    用它算出來的「成長」是儀器的天花板，不是量測結果。這一條刻意讓兩次
    的 `prompt_eval_count` **相同**，而 messages 長度不同：錯的算法在這裡
    會給 0，對的算法給 40。
    """
    call1 = {"kind": "chat", "params": {"messages": [{"content": "x" * 100}]},
             "prompt_eval_count": 2050}
    call2 = {"kind": "chat", "params": {"messages": [{"content": "x" * 140}]},
             "prompt_eval_count": 2050}
    dup = p.duplicate_evidence(call1, call2)
    assert dup["prompt_grew_chars"] == 40, dup
    assert dup["prompt_chars_first"] == 100 and dup["prompt_chars_second"] == 140
    # 而那兩個 prompt_eval_count 相減是 0 —— 那個 0 不是「沒有變長」。
    assert call2["prompt_eval_count"] - call1["prompt_eval_count"] == 0


def test_duplicate_omits_the_cells_when_unreadable():
    """**讀不到就不放那一格**，不是放 0 —— 兩者在 JSON 裡必須分得出來。"""
    dup = p.duplicate_evidence({}, {})
    assert dup == {}, dup
    # 只有一邊讀得到也不給：單邊的長度撐不起「變長了」這個主張。
    one = p.duplicate_evidence({"params": {"messages": [{"content": "abc"}]}}, {})
    assert one == {}, one


# ── measure_ctx_law() 的接線（用假的 ollama）─────────────────
# 上面測的是純函式，這裡測**接線**：參數順序、解包的個數、哪一種情況該把
# None 傳進去。接線錯了在真機上要十幾分鐘、外加一次模型重載才會發現，
# 而這裡可以用好幾種「假規則」各跑一次。
#
# 五條覆蓋的是同一個問題的不同面：**C2b 在什麼情況下會給出那個數字，
# 在什麼情況下不會。** 給錯的時機比算錯更難發現 —— 算錯會留下一個
# 對不上的數字，而「在不該給的時候給了」留下的是所有欄位都好看的輸出。


class _FakeOllama:
    """依給定的規則回報 prompt_eval_count。

    `law(tokens, ctx) -> prompt_eval_count`。`round_ctx` 模擬「ollama 把
    num_ctx 向上取整到某個下限」—— 那會讓 P 與 P+1 變成同一個 context。
    """

    def __init__(self, law, round_ctx=None):
        self.law = law
        self.round_ctx = round_ctx
        self.ctx = 4096

    def chat(self, model, messages, options, **kw):
        toks = sum(len(m["content"]) for m in messages) // 4
        ctx = options.get("num_ctx", 4096)
        if self.round_ctx:
            ctx = max(ctx, self.round_ctx)
        self.ctx = ctx
        return {"wall": 1.0, "prompt_eval_count": self.law(toks, ctx),
                "eval_count": 1, "prompt_eval_duration": 1.0,
                "eval_duration": 1.0, "load_duration": 1.0,
                "thinking_chars": 0, "content": "{}"}


def _run_ctx_law(law, round_ctx=None, read_ps=True):
    """跑一次 measure_ctx_law()，回傳它寫進 evidence 的那一節。

    一定要還原被換掉的模組層函式：測試是**照名字排序**跑的，污染會散到
    別的測試上，而且症狀會看起來完全無關。
    """
    fake = _FakeOllama(law, round_ctx)
    saved = (p._client, p._chat, p._ps_context_length)
    p._client = lambda args: fake
    p._chat = lambda client, model, messages, options, **kw: fake.chat(
        model, messages, options)
    p._ps_context_length = (lambda client, model: fake.ctx) if read_ps else (
        lambda client, model: None)
    try:
        ev = {"truncation": {"full_pec": 8047}}
        p.measure_ctx_law(ev, type("A", (), {"model": "m"})())
        return ev["ctx_law"]
    finally:
        p._client, p._chat, p._ps_context_length = saved


def _true_law(toks, ctx):
    return p.truncated_length(ctx) if toks >= ctx else toks


def _half_law(toks, ctx):
    return p.truncated_length(ctx) if toks > ctx // 2 else toks


def _offset_law(toks, ctx):
    return p.truncated_length(ctx) if toks >= ctx - 46 else toks


def test_ctx_law_wiring_under_the_true_rule():
    """真規則下，那一節要一路走到「num_ctx 至少開到 8048」。"""
    law = _run_ctx_law(_true_law)
    assert law["consistent"] is True, law
    assert law["min_ctx_for_extraction"] == 8048, law
    assert len(law["bracket_points"]) == 3, law
    assert [c["matches"] for c in law["cut_points"]] == [True, True], law


def test_ctx_law_wiring_rejects_the_half_rule():
    """門檻 = num_ctx/2 時**不可以**給出那個數字。

    這一條比對應的純函式測試多守一件事：接線真的把 P+1 那次的量測
    傳進去了（傳錯一格的話這裡會過關）。
    """
    law = _run_ctx_law(_half_law)
    assert law["consistent"] is False, law
    assert law["min_ctx_for_extraction"] is None, law
    assert law["bracket"]["above_untouched"] is False, law


def test_ctx_law_wiring_rejects_the_offset_rule():
    """門檻 = num_ctx − c（舊公式的形狀）也一樣要擋掉。"""
    law = _run_ctx_law(_offset_law)
    assert law["consistent"] is False, law
    assert law["min_ctx_for_extraction"] is None, law


def test_ctx_law_wiring_rounded_num_ctx_is_indeterminate():
    """**這一條守的是假失敗。**

    ollama 若把 num_ctx 向上取整到一個下限，P 與 P+1 會變成同一個
    context、兩次都不會被砍 —— 判讀會說「門檻不是 num_ctx」，而真正
    發生的事情是「我們要求的 num_ctx 沒有被照做」。兩者的處置完全
    不同：前者要重新推規則，後者只要把 BRACKET_PROMPT_CTX 調大。
    """
    law = _run_ctx_law(_true_law, round_ctx=2048)
    assert law["consistent"] is None, law
    assert law["min_ctx_for_extraction"] is None, law
    assert [pt["took_effect"] for pt in law["bracket_points"][1:]] == [False, False], law


def test_ctx_law_wiring_survives_unreadable_ps():
    """讀不到 /api/ps 是「無法確認」，不是「沒生效」—— 量測照樣成立。

    合併這兩者的話，任何一個讀不到 /api/ps 的環境都會拿不到那個數字
    （而且在 C2 那邊會變成假失敗，D-016）。
    """
    law = _run_ctx_law(_true_law, read_ps=False)
    assert law["consistent"] is True, law
    assert law["min_ctx_for_extraction"] == 8048, law


class _RecordingOllama:
    """記下每一次 _chat 的 options，並回報固定的 prompt_eval_count。

    C2 的 A 段與 B 段用**不同的 options**（B 的 num_predict 刻意壓小）。
    這個假 client 存在的唯一理由，是讓「A 送出的必須是 mem0 那一份、一個字
    都不改」變成測得出來的斷言 —— 那是這一節最重要的一條不變式，而它與
    「B 可以壓小」長得非常像。
    """

    def __init__(self, default_pec=2050, full_pec=8047):
        self.calls = []
        self.default_pec = default_pec
        self.full_pec = full_pec
        self.ctx = 4096

    def chat(self, model, messages, options, **kw):
        self.calls.append(dict(options))
        full = "num_ctx" in options
        self.ctx = options.get("num_ctx", 4096)
        return {"wall": 1.0,
                "prompt_eval_count": self.full_pec if full else self.default_pec,
                "eval_count": 1, "prompt_eval_duration": 1.0,
                "eval_duration": 1.0, "load_duration": 1.0,
                "thinking_chars": 0, "content": "{}"}


def _run_truncation(fake):
    """跑一次 measure_truncation()，回傳 (evidence 的那一節, 假 client)。

    跟 _run_ctx_law() 一樣必須還原模組層函式：測試照名字排序跑，漏還原
    會污染別的測試，而且症狀看起來完全無關。

    `_mem0_messages` 也要換掉 —— 真的那個會 `import mem0.configs.prompts`，
    而 mem0 只裝在探針映像裡，這支測試是在**主機上**跑的。這裡測的是
    「options 怎麼被送出去」，不是 mem0 的提示詞長什麼樣，所以換掉它不影響
    這一節要問的問題。
    """
    saved = (p._client, p._chat, p._unload, p._ps_lookup,
             p._ps_context_length, p._mem0_messages)
    p._client = lambda args: fake
    p._chat = lambda client, model, messages, options, **kw: fake.chat(
        model, messages, options)
    p._unload = lambda client, model: True
    p._ps_lookup = lambda client, model: (True, fake.ctx)
    p._ps_context_length = lambda client, model: fake.ctx
    p._mem0_messages = lambda: [
        {"role": "system", "content": "S" * 400},
        {"role": "user", "content": "U" * 40},
    ]
    try:
        ev = {}
        p.measure_truncation(ev, type("A", (), {"model": "m"})())
        return ev["truncation"], fake
    finally:
        (p._client, p._chat, p._unload, p._ps_lookup,
         p._ps_context_length, p._mem0_messages) = saved


def test_truncation_sends_mem0_options_verbatim_for_a():
    """A 段必須送出 mem0 那份 options，一個字都不改。

    A 量的是「mem0 實際遇到什麼」。任何為了省時間而動 A 的念頭（先前就
    發生過一次：把 A 的 num_predict 壓成 16）都會讓它量的變成別的東西
    （D-014）。B 段壓小 num_predict 是刻意的 —— 這條測試存在的理由就是
    它與那件事長得一樣，而分不出兩者的測試等於沒有測試。
    """
    v, fake = _run_truncation(_RecordingOllama())
    assert fake.calls[0] == p.MEM0_LLM_OPTIONS, fake.calls[0]
    assert "num_ctx" not in fake.calls[0], fake.calls[0]
    assert v["default_options"] == p.MEM0_LLM_OPTIONS, v["default_options"]


def test_truncation_reduces_num_predict_for_b_only():
    """B 段壓小 num_predict，其餘與 mem0 一致，而且 evidence 要老實記下來。

    記下 full_options 是為了讓「B 不是跑在 mem0 的 options 下」這件事留在
    證據裡 —— 不寫的話，日後讀 evidence 的人會以為 A、B 只差 num_ctx。
    """
    v, fake = _run_truncation(_RecordingOllama())
    a_opts, b_opts = fake.calls
    assert b_opts["num_ctx"] == p.FULL_CTX, b_opts
    assert b_opts["num_predict"] == p.B_CROSSCHECK_NUM_PREDICT, b_opts
    assert b_opts["num_predict"] != a_opts["num_predict"], b_opts
    assert b_opts["temperature"] == a_opts["temperature"], b_opts
    assert b_opts["top_p"] == a_opts["top_p"], b_opts
    assert v["full_options"] == b_opts, v["full_options"]


def test_truncation_verdict_survives_the_reduced_b_budget():
    """壓小 num_predict 之後，C2 的判準還是照樣成立。

    這一條防的是「改動把判準弄壞了卻沒人發現」：B 的 prompt_eval_count
    不管生成幾個 token 都該是完整的 prompt 長度。
    """
    v, _ = _run_truncation(_RecordingOllama())
    assert v["truncated"] is True, v
    assert v["dropped"] == 8047 - 2050, v


def test_probe_revision_fingerprints_the_source_on_disk():
    """指紋要對得上**磁碟上那一份**，不然它只是一個裝飾用的字串。

    會寫這條是因為 D-027 真的吃過這個虧：run2 跑完之後探針被改過，而
    「那組數字是哪一版跑的」只能靠一個殘留的 .pyc 做 bytecode 比對才答得
    出來。指紋的**唯一**用途就是回答那個問題，所以它必須是可核對的。
    """
    import hashlib
    r = p.probe_revision()
    src = open(p.__file__, "rb").read()
    assert r["sha256_16"] == hashlib.sha256(src).hexdigest()[:16], r
    assert r["bytes"] == len(src), r
    assert len(r["sha256_16"]) == 16, r
    assert all(c in "0123456789abcdef" for c in r["sha256_16"]), r
    # mtime 是可讀的本地時間字串（不是 epoch、不是空字串）——它只是給人看的。
    assert len(r["mtime"]) == 19 and r["mtime"][4] == "-", r


def test_probe_revision_changes_when_the_source_changes():
    """**指紋不可以是常數。** 這是這條測試唯一真正要擋的東西。

    一個寫死的「修訂版」比沒有更糟：它會讓每一次執行都宣稱自己是同一版，
    而那個宣稱永遠不會有人去查。用一個內容不同、但同樣大小的假檔案來驗
    —— 只改內容不改長度，是為了確認它算的是**內容**，不是長度。
    """
    import hashlib
    import tempfile
    real = p.Path
    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "mem0_add_cost_probe.py")
        src = open(p.__file__, "rb").read()
        with open(fake, "wb") as f:
            # 同樣的長度、不同的內容。**不能**只把開頭換成 '#'：這個檔案本來
            # 就以 '#!' 開頭，那會寫出一份位元組完全相同的檔案，測試就變成
            # 在驗「一樣的東西有一樣的雜湊」——恆真，什麼都擋不到。
            assert src[:1] == b"#", src[:1]
            f.write(b"!" + src[1:])
        assert os.path.getsize(fake) == len(src)
        p.Path = lambda *a, **k: real(fake)
        try:
            r = p.probe_revision()
        finally:
            p.Path = real
    assert r["bytes"] == len(src), r
    assert r["sha256_16"] != hashlib.sha256(src).hexdigest()[:16], \
        "同樣長度但內容不同，指紋卻一樣 —— 它算的不是內容"


def test_probe_revision_reports_an_error_instead_of_raising():
    """讀不到自己也要有結果（一個帶 error 的 dict），不是例外。

    這一格會進 evidence 與 JSON；讓它在唯讀檔系統或打包環境下把整輪炸掉，
    等於為了一個**觀察**欄位犧牲掉全部量測。"""
    real = p.Path

    def boom(*a, **k):
        raise OSError("模擬讀不到")

    p.Path = boom
    try:
        r = p.probe_revision()
    finally:
        p.Path = real
    assert "error" in r, r
    assert "sha256_16" not in r, r


def test_environment_records_the_probe_revision():
    """指紋要**進 evidence** —— 算得再準，沒傳下去就等於沒有。

    這條擋的是「讀得到、但沒有往下傳」：D-027 的那次鑑識之所以要動用
    bytecode 比對，正是因為當時沒有任何一格記著「是誰跑的」。而一個只存在
    於函式回傳值裡的指紋，跟沒有是一樣的。
    """
    class _Fake:
        def list(self):
            return {"models": [{"model": "qwen3:4b"},
                               {"model": "qwen3-embedding:0.6b"}]}

    saved = (p._client, p._ps_context_length)
    p._client = lambda args: _Fake()
    p._ps_context_length = lambda client, model: 4096
    try:
        ev = {}
        args = type("A", (), {"model": "qwen3:4b",
                              "embed_model": "qwen3-embedding:0.6b",
                              "add_max_tokens": None, "ctx": None})()
        ok = p.measure_environment(ev, args)
    finally:
        p._client, p._ps_context_length = saved
    assert ok is True, ev
    assert "probe" in ev["meta"], ev["meta"]
    probe = ev["meta"]["probe"]
    assert len(probe.get("sha256_16", "")) == 16, probe
    assert probe.get("bytes"), probe


def test_thinking_verdict_marks_a_capped_run_as_a_lower_bound():
    """D-027 實測到的那一組：**「開」那一側停在 num_predict 上限。**

    沒有這一條，探針會把 `256 − 72 = 184` 講成真值，而那個 256 是預算切出來
    的 —— 放寬上限只會讓差值更大。**被切斷的生成不能拿來當完整的量測。**
    """
    v = p.thinking_verdict(892, 256, 0, 72, num_predict_on=256, num_predict_off=256)
    assert v["counts_toward_eval"] is True, v
    assert v["saved_tokens"] == 184, v
    assert v["saved_is_lower_bound"] is True, v
    assert "下界" in v["reason"], v["reason"]
    assert "256" in v["reason"], v["reason"]


def test_thinking_verdict_is_a_point_value_when_neither_side_is_capped():
    """兩側都沒撞上限時，差值才是點值 —— 而且**不可以**多講一句下界。

    這是「該過的要過」那一半：一個永遠說「下界」的判定跟永遠說「真值」的
    判定一樣沒用，因為它不再區辨任何東西。
    """
    v = p.thinking_verdict(500, 200, 0, 72, num_predict_on=256, num_predict_off=256)
    assert v["saved_tokens"] == 128, v
    assert v["saved_is_lower_bound"] is False, v
    assert "下界" not in v["reason"], v["reason"]


def test_thinking_verdict_without_num_predict_says_it_did_not_check():
    """**沒查 ≠ 查過且不是。** 呼叫端沒給 num_predict 時，這一格必須是
    None（不知道），不是 False（斷言它是點值）。"""
    v = p.thinking_verdict(892, 256, 0, 72)
    assert v["saved_is_lower_bound"] is None, v
    assert v["saved_tokens"] == 184, v  # 差值照算，只是不宣稱它是真值


def test_thinking_verdict_a_capped_non_decrease_is_not_a_refutation():
    """「沒有下降」加上「被切斷」不可以讀成反例 —— 那只是沒有量到。

    這與 C3 的那個洞同型：一個不確定的觀測被當成一個肯定的結果。
    """
    v = p.thinking_verdict(892, 256, 400, 256, num_predict_on=256, num_predict_off=256)
    assert v["counts_toward_eval"] is False, v
    assert "上限" in v["reason"], v["reason"]
    assert "不可以當成反例" in v["reason"], v["reason"]


def test_grade_blocks_when_the_position_control_is_not_a_mirror():
    """位置對照組沒有呈現鏡像 → C3 不可以通過。

    提示詞的字面裡就寫著 `HEADMARK`／`TAILMARK`。少了這條，第一次量到的
    「尾端看得到」排除不掉「模型只是照著問題的字面回答、與位置無關」。
    """
    e = _set(_evidence_ok(), "canary.reversed.head_seen", False)
    passed, problems = p.grade(e)
    assert passed is False, problems
    assert any("對照組" in m and "鏡像" in m for m in problems), problems
    # 要跟「第一次就錯了」分得開 —— 兩者的處置不同。
    assert not any("開頭看不到、結尾看得到" in m for m in problems), problems


def test_grade_says_not_measured_when_only_the_control_was_unusable():
    """只有對照組沒量到，也要講「沒有量到」，不是講判準沒過。"""
    e = _set(_evidence_ok(), "canary.reversed.answer_usable", None)
    passed, problems = p.grade(e)
    assert passed is False, problems
    assert any("沒有量到" in m for m in problems), problems
    assert any("對照" in m for m in problems), problems


def test_exit_code_indeterminate_when_the_control_group_is_missing():
    """對照組整個缺席 → 2（這一輪不完整），**不是 0**。

    這是 fail-closed：只看第一次的話，一個沒量到的對照組會讓整輪走到 0，
    而那個 0 會被讀成「item 3 通過」。
    """
    e = _evidence_ok()
    del e["canary"]["reversed"]
    assert p.exit_code(e, True) == p.EXIT_INDETERMINATE, e["canary"]


def test_exit_code_broken_when_the_control_was_cut_off():
    """對照組被生成預算切斷 → 3（儀器該修），不是 1（上游變了）。"""
    e = _set(_evidence_ok(), "canary.reversed.answer_usable", False)
    assert p.exit_code(e, False) == p.EXIT_BROKEN, e["canary"]


def _fake_memory(resp):
    """攔截層只碰這四個屬性 —— 用 stub 就測得起來，不必連 ollama。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        llm=SimpleNamespace(client=SimpleNamespace(chat=lambda **kw: resp)),
        embedding_model=SimpleNamespace(
            client=SimpleNamespace(embed=lambda **kw: None)),
    )


def _fake_response(**over):
    from types import SimpleNamespace
    base = dict(
        message=SimpleNamespace(thinking="想", content="[]"),
        prompt_eval_count=8100, eval_count=8000,
        prompt_eval_duration=6.057e11, eval_duration=1.7446e12,
        load_duration=1.02e10, done_reason="length",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_recorder_captures_ollamas_done_reason():
    """**這一輪的判準就繫在這一格上。**

    「生成 8,000 個 token」與「被預算切在 8,000」在輸出上長得一樣，分開它們
    的只有 done_reason。攔截層漏掉它，整輪就只能靠 eval_count 推論 —— 而
    推論與觀測的差別正是這一節在爭的東西。
    """
    memory = _fake_memory(_fake_response(done_reason="length"))
    calls = p._install_recorder(memory)
    memory.llm.client.chat(model="m", messages=[], options={})
    assert len(calls) == 1, calls
    assert calls[0]["done_reason"] == "length", calls[0]
    assert calls[0]["eval_count"] == 8000, calls[0]
    assert calls[0]["content_chars"] == 2, calls[0]


def test_recorder_survives_a_response_without_done_reason():
    """舊版 client 沒有這個欄位時要記成 None 而**不是爆掉**。

    爆掉的那一輪會什麼都量不到（而且是在跑了幾小時之後才發現）；記成 None
    的那一輪至少量得到 token 數，generation_stop_verdict 也會明講它是推的。
    """
    memory = _fake_memory(_fake_response(done_reason=None))
    calls = p._install_recorder(memory)
    memory.llm.client.chat(model="m", messages=[], options={})
    assert calls[0]["done_reason"] is None, calls[0]
    # 而且這個時候 verdict 要落在「推論」那一支，不可以宣稱是 ollama 說的。
    v, s = p.generation_stop_verdict(calls[0]["eval_count"], 8000,
                                     calls[0]["done_reason"])
    assert v == "capped", (v, s)
    assert "從 eval_count 推的" in s, s


def test_generation_stop_verdict_prefers_ollamas_own_statement():
    """done_reason 是觀測，eval_count 是推論 —— 兩個都在時以觀測為準。"""
    v, s = p.generation_stop_verdict(8000, 8000, "stop")
    # eval_count == cap 看起來像撞到上限，但 ollama 說它自己停了 → 矛盾。
    # **這一格不挑一個相信**：那是有話要說的情況。
    assert v == "disagree", (v, s)
    assert "對不起來" in s, s

    v, s = p.generation_stop_verdict(7999, 8000, "stop")
    assert v == "stopped", (v, s)
    assert "done_reason=stop" in s and "自己停下來" in s, s

    v, s = p.generation_stop_verdict(8000, 8000, "length")
    assert v == "capped", (v, s)
    assert "撞到 num_predict" in s, s


def test_generation_stop_verdict_falls_back_to_inference_and_says_so():
    """沒有 done_reason 時只能推論 —— 而**推論要標成推論**。"""
    v, s = p.generation_stop_verdict(8000, 8000, None)
    assert v == "capped", (v, s)
    assert "從 eval_count 推的" in s, s

    v, s = p.generation_stop_verdict(1234, 8000, None)
    assert v == "stopped", (v, s)
    assert "推得沒有撞到上限" in s, s


def test_generation_stop_verdict_refuses_to_guess_without_a_cap():
    """沒有上限可比就沒有推論可言 —— 回 unknown，不是回「沒撞到」。

    這一格是這支函式的 fail-closed 面：`cap` 讀不到時說「沒有撞到上限」，
    等於把「不知道」講成「好消息」，而這一整條線的病都是這一種。
    """
    v, s = p.generation_stop_verdict(2000, None, None)
    assert v == "unknown", (v, s)
    assert s == "", s

    v, s = p.generation_stop_verdict(None, 2000, None)
    assert v == "unknown", (v, s)

    # bool 是 int 的子類別 —— 不可以被當成 token 數收下。
    v, _ = p.generation_stop_verdict(True, 1, None)
    assert v == "unknown", v


def test_generation_stop_verdict_ignores_a_done_reason_it_does_not_know():
    """ollama 還有別種 done_reason（load／unload）—— 不認識的就不要假裝懂。"""
    v, s = p.generation_stop_verdict(500, 8000, "load")
    # 推論還在，所以仍然給得出答案，但**不可以**宣稱那是 ollama 說的。
    assert v == "stopped", (v, s)
    assert "done_reason=stop" not in s, s
    v, s = p.generation_stop_verdict(None, None, "load")
    assert v == "unknown", (v, s)


def test_observations_says_when_the_add_generation_hit_the_cap():
    """觀察段也不可以把被切斷的生成講成生成完了。

    這一格是**最常被讀的輸出**，而 `eval_count=2000` 單獨看就是「模型生成
    了 2000 個 token」。要講出它同時等於 `num_predict`。
    """
    e = _evidence_ok()
    e["first_add"]["eval_count"] = 2000  # == mem0_options 的 num_predict
    assert any("撞到 num_predict" in n for n in p.observations(e)), \
        [n for n in p.observations(e)]

    e2 = _evidence_ok()
    e2["first_add"]["eval_count"] = 800  # 沒撞到
    assert not any("撞到 num_predict" in n for n in p.observations(e2)), \
        [n for n in p.observations(e2)]


def test_truncation_claim_verdict_ctx_large_says_not_truncated():
    """D-036 的處境：num_ctx 16,384 ≥ 下限 8,048 → **沒有截斷**。

    這是這一支存在的理由：同一句話在 ctx 4096 之下是真的、在 16384 之下是假
    的，而它印在輸出裡最常被讀的那一行。
    """
    v, s = p.truncation_claim_verdict(16384, 8048, 8052, 8167)
    assert v == "not_truncated", (v, s)
    assert "這兩次都沒有被截斷" in s, s
    assert "8052" in s and "8167" in s, s
    # 原本那句斷言**一個字都不可以再出現**，包括「引它來說它錯了」的寫法 ——
    # 這一行的讀者是用眼睛掃、用 grep 找的。
    assert "都被截斷到同一個上限" not in s, s
    assert "看不出" not in s, s


def test_truncation_claim_verdict_ctx_small_keeps_the_original_sentence():
    """ctx 4096 < 下限 8,048 → 原句成立（兩次都砍到 2050）。"""
    v, s = p.truncation_claim_verdict(4096, 8048, 2050, 2050)
    assert v == "truncated", (v, s)
    assert "都被截斷到同一個上限" in s and "看不出這個差別" in s, s


def test_truncation_claim_verdict_disagrees_when_the_counts_differ():
    """判準說有截斷，兩個 prompt_eval_count 卻不相等 → **不挑一個相信**。

    兩次都被砍到同一個上限，那兩個數就必須相等（同一個 num_ctx 下
    truncated_length() 是同一個數）。不相等就是判準與觀測打架。
    """
    v, s = p.truncation_claim_verdict(4096, 8048, 2050, 3000)
    assert v == "disagree", (v, s)
    assert "打架" in s and "2050" in s and "3000" in s, s


def test_truncation_claim_verdict_refuses_without_the_lower_bound():
    """缺 min_ctx_for_extraction（門檻沒夾準）→ **不主張**。

    fail-closed 的那一面：讀不到下限時說「沒有被截斷」，等於把「不知道」講成
    好消息。

    **注意不可以用 "沒有被截斷" 當「不該出現」的字串** —— 這一句裡有
    「有沒有被截斷」，而它含那五個字。断言的對象要指到**可分辨的句子**
    （D-026 第五節）；第一版就是這樣寫錯，對正確的程式碼回報失敗。
    """
    v, s = p.truncation_claim_verdict(16384, None, 8052, 8167)
    assert v == "unknown", (v, s)
    assert "沒有主張" in s, s
    assert "都被截斷到同一個上限" not in s, s
    assert "這兩次都沒有被截斷" not in s, s


def test_truncation_claim_verdict_refuses_without_ctx():
    v, s = p.truncation_claim_verdict(None, 8048, 8052, 8167)
    assert v == "unknown", (v, s)
    assert "沒有主張" in s, s


def test_truncation_claim_verdict_does_not_treat_true_as_a_number():
    """`True` 是 `int` 的子類 —— 不可以被當成 ctx=1。"""
    v, s = p.truncation_claim_verdict(True, 8048, 8052, 8167)
    assert v == "unknown", (v, s)
    assert "沒有主張" in s, s


def test_truncation_claim_verdict_at_the_boundary():
    """下限是「不被截斷」的**最小值**，所以 ctx == 下限算沒有截斷。"""
    v, _ = p.truncation_claim_verdict(8048, 8048, 8052, 8167)
    assert v == "not_truncated", v
    v, _ = p.truncation_claim_verdict(8047, 8048, 2050, 2050)
    assert v == "truncated", v


def test_truncation_claim_verdict_still_decides_without_the_counts():
    """有截斷、但讀不到 prompt_eval_count → 仍然判得出來，只是不假裝驗過一致性。"""
    v, s = p.truncation_claim_verdict(4096, 8048, None, None)
    assert v == "truncated", (v, s)
    assert "都被截斷到同一個上限" in s, s


def test_observations_drops_the_truncation_claim_when_ctx_is_large():
    """端到端：`observations()` 在 ctx 夠大時不可以再說「兩次都被截斷」。"""
    e = _evidence_ok()
    e["meta"]["context_length_before"] = 16384
    e["ctx_law"] = {"min_ctx_for_extraction": 8048}
    e["first_add"]["prompt_eval_count"] = 8052
    e["second_add"]["prompt_eval_count"] = 8167
    notes = p.observations(e)
    second = [n for n in notes if "第二次 add（內容相同）" in n]
    assert len(second) == 1, notes
    assert "這兩次都沒有被截斷" in second[0], second[0]
    assert "看不出這個差別" not in second[0], second[0]


def test_observations_keeps_the_truncation_claim_when_ctx_is_small():
    """反向：ctx 不夠大時那句話仍然是對的 —— 修法不是把它刪掉。"""
    e = _evidence_ok()
    e["meta"]["context_length_before"] = 4096
    e["ctx_law"] = {"min_ctx_for_extraction": 8048}
    e["first_add"]["prompt_eval_count"] = 2050
    e["second_add"]["prompt_eval_count"] = 2050
    notes = p.observations(e)
    second = [n for n in notes if "第二次 add（內容相同）" in n]
    assert len(second) == 1, notes
    assert "都被截斷到同一個上限" in second[0], second[0]


# ── 推導式生成預算（D-037）──────────────────────────────
#
# 這一組盯的是 D-036 第十節立的規矩：「不要把它寫成另一個魔數」。
# 魔數的症狀不是「數字不對」，而是**它在下一個模型／下一份 prompt 上安靜地
# 不對** —— 所以這裡驗的是「值的來歷」（推導 vs 覆寫 vs 預設）與「前提被違反
# 時會不會叫」，不只是數字本身。


def _evidence_derived(ctx=16384, first=None, second=None):
    """D-036 第三輪的形狀：預算由 num_ctx 推導，兩次 add() 都自己停下來。"""
    e = _evidence_ok()
    _, source, note = p.budget_resolution(None, ctx)
    e["meta"].update({
        "budget_source": source,
        "ctx_arg": ctx,
        "budget_note": note,
        "add_max_tokens_effective": p.extraction_budget(ctx),
        "context_length_before": ctx,
    })
    e["first_add"].update({"prompt_eval_count": 8052, "eval_count": 5605,
                           "done_reason": "stop"})
    e["second_add"].update({"prompt_eval_count": 8167, "eval_count": 1236,
                            "done_reason": "stop"})
    if first:
        e["first_add"].update(first)
    if second:
        e["second_add"].update(second)
    return e


def test_extraction_budget_derives_and_follows_num_ctx():
    """預算是**算**出來的：num_ctx 變大，它跟著變大。

    這一條是「推導」與「魔數」的分界線。魔數在任何 num_ctx 下都回同一個
    值，於是 num_ctx 一大，它就變成新的 2,000（D-036 第十節）。
    """
    assert p.extraction_budget(16384) == 8192, p.extraction_budget(16384)
    assert p.extraction_budget(32768) == 24576, p.extraction_budget(32768)
    assert p.extraction_budget(32768) > p.extraction_budget(16384)
    # 界線：剛好多 1 個 token 就推得出 1，等於界則推不出來。
    assert p.extraction_budget(8193) == 1, p.extraction_budget(8193)
    assert p.extraction_budget(8192) is None, p.extraction_budget(8192)


def test_extraction_budget_refuses_instead_of_returning_zero():
    """推不出來時回 **None，不是 0**。

    0 在 mem0 的 config 裡會被 truthiness 吃掉（`--add-max-tokens 0` 現行
    就是靜默地什麼都不做），所以「算不出來」若寫成 0，會變成「用 mem0 的
    預設 2,000」—— 「安靜地不對」正是這一整條線要消滅的失敗方式。

    `True` 是刻意的案例：`isinstance(True, int)` 是 True，但它是旗標不是
    大小 —— 與 `generation_stop_verdict()` 的同一個慣例（那裡也擋 bool）。
    """
    for bad in (None, 0, -1, -8192, True, False, 1.5, "16384", 8191, 8101, []):
        got = p.extraction_budget(bad)
        assert got is None, "extraction_budget(%r) 回了 %r，應該是 None" % (bad, got)
        assert got != 0, bad


def test_budget_resolution_precedence():
    """優先序：`--add-max-tokens` > `--ctx` 推導 > mem0 的預設。

    第一條不是形式主義：`--quick` 就是翻譯成 `--add-max-tokens 200`，它是
    重現 D-027 條件的受控實驗。推導值若蓋掉它，一個已文件化的旗標會變成
    沒作用 —— 而且是靜默的。
    """
    eff, src, _ = p.budget_resolution(2000, 16384)
    assert (eff, src) == (2000, "overridden"), (eff, src)
    eff, src, note = p.budget_resolution(None, 16384)
    assert (eff, src) == (8192, "derived_from_ctx"), (eff, src)
    assert "16384" in note and "8192" in note, note
    eff, src, _ = p.budget_resolution(None, None)
    assert (eff, src) == (None, "mem0_default"), (eff, src)


def test_budget_resolution_underivable_is_not_filed_as_the_default():
    """推不出來與「本來就用預設」**不可以混成同一格**。

    兩者的 `effective` 都是 None，但只有後者是承諾（量 mem0 的實況）。
    來源字串是唯一分得開它們的地方 —— 混掉的話，「推導失敗」會偽裝成
    「這一輪刻意不覆寫」。`main()` 會在更前面就擋掉，這裡驗的是這條界線
    本身存在。
    """
    eff, src, note = p.budget_resolution(None, 0)
    assert eff is None and src == "underivable", (eff, src)
    assert src != "mem0_default", src
    assert "推不出" in note, note


def test_ctx_arg_error_blocks_a_value_that_is_not_a_num_ctx():
    """**根本不像 num_ctx 的值**要當場擋下來（用法錯誤 → 結束碼 3）。

    擋的是「0 或負數」這種不是 context 的值，不是「太小」—— 見下一條。
    """
    assert p.ctx_arg_error(None) is None, "沒給 --ctx 不是錯誤"
    assert p.ctx_arg_error(16384) is None, "推得出來就不是錯誤"

    for bad in (0, -1, -8192, True):
        msg = p.ctx_arg_error(bad)
        assert msg, "--ctx %r 應該被擋下來" % (bad,)
        assert "--ctx" in msg, msg


def test_ctx_arg_error_allows_a_ctx_that_is_merely_too_small():
    """**「太小、推不出預算」不是用法錯誤** —— 它是 D-027 的受控條件。

    這一條守著一個**文件化的流程**：C2／C3 的前提是「伺服器預設值小到會
    截斷」，而 `verify-mem0-add-cost.sh` 的訊息就叫使用者把
    `OLLAMA_CONTEXT_LENGTH` 調回 4096 再跑。那時 4096 會被腳本當成合法的
    num_ctx 傳進來（`num_ctx_verdict` 說 ok），若這裡把它擋成 3，照著文件
    做的人會被告知儀器壞了 —— 一個**假失敗**，而 D-016 說假失敗比漏報更糟。

    推不出預算是**事實**：探針照跑、記成 `budget_source=underivable`、
    不覆寫（量 mem0 的實況），並且在輸出裡講出來。
    """
    for small in (4096, 8192, 8101, 512):
        assert p.ctx_arg_error(small) is None, (
            "--ctx %r 不該被當成用法錯誤 —— 那是受控條件的形狀" % (small,))
        # 但預算仍然推不出來，而且來源要分得出來（不是 mem0_default）。
        eff, src, note = p.budget_resolution(None, small)
        assert eff is None and src == "underivable", (small, eff, src)
        assert "沒有推導" in note, note


def test_budget_note_is_printed_for_every_state_except_the_plain_default():
    """要講出來的狀態有三種（覆寫／推導／推不出來），只有預設不講。"""
    derived = {"budget_source": "derived_from_ctx", "budget_note": "由 num_ctx 推導…"}
    assert p.budget_note_to_print(derived) == "由 num_ctx 推導…"
    assert p.budget_note_to_print({"budget_source": "overridden",
                                   "budget_note": "覆寫…"}) == "覆寫…"
    assert p.budget_note_to_print({"budget_source": "underivable",
                                   "budget_note": "沒有推導…"}) == "沒有推導…"
    assert p.budget_note_to_print({"budget_source": "mem0_default"}) is None
    # 來歷不明（舊 evidence 沒有這個鍵）就不要替它講話。
    assert p.budget_note_to_print({}) is None
    assert p.budget_note_to_print(None) is None


def test_effective_num_predict_prefers_the_resolved_value():
    """解析出來的值蓋過 mem0 的 options —— config、輸出、判準吃同一個數字。

    這一條擋的是「輸出與事實相反」：D-035 第七節教訓 3 的形狀（三處各自
    讀一次來源，遲早會漂移）。
    """
    meta = {"add_max_tokens_effective": 8192,
            "mem0_options": {"num_predict": 2000}}
    assert p.effective_num_predict(meta) == 8192, p.effective_num_predict(meta)


def test_effective_num_predict_falls_back_to_mem0s_default():
    """沒有覆寫也沒有推導時，回的是 mem0 的預設（不是 None）。"""
    assert p.effective_num_predict({"mem0_options": {"num_predict": 2000}}) == 2000
    assert p.effective_num_predict({}) is None, "沒有 mem0_options 時應該回 None"
    assert p.effective_num_predict(None) is None


def test_grade_passes_when_the_derived_budget_held():
    """D-036 第三輪的實測形狀：兩個前提都成立 → 放行。"""
    passed, problems = p.grade(_evidence_derived())
    assert passed, problems


def test_grade_blocks_when_the_bound_went_stale():
    """前提 1（界要成立）：prompt 超過推導用的界 → C6，而且處方是**更新界**。

    這一條是那個界（8192）的哨兵。沒有它，界過期會表現成「預算看起來還是
    算出來的」—— 而不變式已經不成立了（prompt + 預算 > num_ctx）。
    """
    problems = _fails(_evidence_derived(first={"prompt_eval_count": 8300}),
                      "超過推導用的界 8192")
    c6 = [x for x in problems if x.startswith("C6")]
    assert len(c6) == 1, problems
    assert "8300" in c6[0], c6[0]
    assert "更新界" in c6[0], c6[0]
    # 處方不可以是「把預算調大」—— 那正是 D-036 第十節禁止的那件事。
    assert "不是把預算調大" in c6[0], c6[0]


def test_grade_blocks_when_the_bound_cannot_be_checked():
    """讀不到 prompt_eval_count → C6 不算過（fail-closed）。

    「讀不到」與「沒超過」在輸出上長得一樣，而這一條是界的哨兵 ——
    讀不到就放行，等於把哨兵撤掉。
    """
    e = _evidence_derived()
    del e["first_add"]["prompt_eval_count"]
    del e["second_add"]["prompt_eval_count"]
    problems = _fails(e, "讀不到 prompt_eval_count")
    assert any(x.startswith("C6") for x in problems), problems


def test_grade_blocks_when_generation_was_cut_by_the_derived_budget():
    """前提 2（預算要夠）：撞到上限 → C7，而且處方是**開大 num_ctx**。

    這是這一整條線的核心症狀（D-036：`eval_count=2,000` 讀起來像「講完了」，
    其實是被切斷）。預算是由 num_ctx 推導的，所以「不夠」的意思是
    num_ctx 不夠 —— 把上限直接調大就是把界調鬆，下一個模型再犯一次。
    """
    problems = _fails(_evidence_derived(first={"eval_count": 8192,
                                               "done_reason": "length"}),
                      "不是模型自己停下來的")
    c7 = [x for x in problems if x.startswith("C7")]
    assert len(c7) == 1, problems
    assert "把 num_ctx 開大" in c7[0], c7[0]
    assert "不是把上限直接調大" in c7[0], c7[0]


def test_grade_blocks_when_the_second_add_was_cut():
    """**兩次 add() 都算** —— 第二次的 prompt 比較長（mem0 會帶上既有歷史）。

    只看第一次的話，最長的那一份 prompt 沒人守 —— 而它才是界與預算的
    真正壓力點。
    """
    problems = _fails(_evidence_derived(second={"eval_count": 8192,
                                                "done_reason": "length"}),
                      "第二次")
    assert any(x.startswith("C7") for x in problems), problems


def test_grade_ignores_the_budget_criteria_when_overridden():
    """`--add-max-tokens`（含 `--quick`）是受控實驗：那時預算太小是**設計**。

    這一條擋的是「把一個有用的對照組關掉」—— D-027 的比對條件就是靠它
    重現的（`num_predict=2000`）。
    """
    e = _evidence_ok()
    e["meta"]["add_max_tokens_effective"] = 200
    e["meta"]["budget_source"] = "overridden"
    e["first_add"].update({"prompt_eval_count": 8052, "eval_count": 200,
                           "done_reason": "length"})
    e["second_add"].update({"prompt_eval_count": 8167, "eval_count": 200,
                            "done_reason": "length"})
    passed, problems = p.grade(e)
    assert passed, problems
    assert not [x for x in problems if x.startswith(("C6", "C7"))], problems


def test_grade_ignores_the_budget_criteria_on_a_bare_run():
    """裸跑探針（沒有覆寫、沒有 `--ctx`）也不判那兩條。

    那一輪量的是 mem0 的實況 —— 抽取回傳 0 筆是**預期結果**，不是缺陷。
    """
    e = _evidence_ok()
    e["meta"]["budget_source"] = "mem0_default"
    e["first_add"].update({"prompt_eval_count": 8052, "eval_count": 2000,
                           "done_reason": "length"})
    passed, problems = p.grade(e)
    assert passed, problems
    assert not [x for x in problems if x.startswith(("C6", "C7"))], problems


def test_observations_states_where_the_budget_came_from():
    """觀察段要能讓讀的人**自己驗算**那個數字是怎麼來的。"""
    notes = p.observations(_evidence_derived())
    line = [n for n in notes if n.startswith("生成預算 num_predict=")]
    assert len(line) == 1, notes
    assert "8192" in line[0], line[0]
    assert "16384" in line[0], line[0]


def test_observations_stays_quiet_about_the_default_budget():
    """用 mem0 預設的那一輪沒有別的話要說 —— 不印那一行。

    （印「num_predict=2000（來源：mem0_default）」會讓每一輪都多一行解釋
    一個沒有改變的東西，而真正要看的那一輪反而被稀釋。）
    """
    e = _evidence_ok()
    e["meta"]["budget_source"] = "mem0_default"
    notes = p.observations(e)
    assert not [n for n in notes if n.startswith("生成預算 num_predict=")], notes


def test_observations_says_out_loud_when_no_budget_could_be_derived():
    """推不出來的那一輪**要講** —— 不然「沒有推導」與「有推導」長得一樣。

    這一條就是「不靜默」在輸出端的那一半：ctx 太小時不會有 C6／C7 盯著
    （那兩條只在推導成立時判），所以觀察段是唯一會講話的地方。
    """
    e = _evidence_ok()
    _, src, note = p.budget_resolution(None, 4096)
    e["meta"].update({"budget_source": src, "ctx_arg": 4096, "budget_note": note})
    lines = [n for n in p.observations(e) if n.startswith("生成預算 num_predict=")]
    assert len(lines) == 1, p.observations(e)
    assert "沒有推導" in lines[0] and "2000" in lines[0], lines[0]


def test_observations_flags_a_ctx_mismatch():
    """推導用的 num_ctx 與 ollama 當下生效的值不一致時要講。

    不一致＝這個預算不是用生效的值算的（常見成因：改過 .env 但沒重啟），
    而不變式是**對生效的那個 num_ctx** 說的。
    """
    e = _evidence_derived()
    e["meta"]["context_length_before"] = 4096      # 生效的是 4096
    notes = p.observations(e)
    mismatch = [n for n in notes if "不一致" in n]
    assert len(mismatch) == 1, notes
    assert "16384" in mismatch[0] and "4096" in mismatch[0], mismatch[0]

    # 相同時不印（上面 _evidence_derived() 的預設就是相同）。
    assert not [n for n in p.observations(_evidence_derived()) if "不一致" in n]


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
    print("%d/%d 全過" % (len(tests), len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
