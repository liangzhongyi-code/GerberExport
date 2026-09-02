"""
C4 對話框白名單測試（對應 spec: operability「對話框白名單制」/ TD-5）。

這是本專案最危險的失敗模式所在。若腳本對未知對話框盲目送出 Enter，
可能誤觸「是，覆蓋」而造成不可回復的資料遺失——而且當下不會有人發現。

因此安全模型是白名單制：**預設一律視為未知**，只有明確列出的才處理。
下面第一條測試盯的就是這個預設值。
"""

import pytest

from lib import dialog_guard as dg
from lib.config import DialogRule

OVERWRITE = DialogRule(
    title_like="*已存在*", action="Cancel", result_status="FAILED_TARGET_EXISTS"
)
LICENSE = DialogRule(
    title_like="AccuMark 授權*", action="Close", result_status="FAILED_TIMEOUT"
)


def dlg(title="某個視窗", text="", buttons=("確定", "取消")):
    return dg.DialogInfo(title=title, text=text, buttons=tuple(buttons))


# ── 安全預設 ─────────────────────────────────────────────────────────


def test_empty_whitelist_treats_everything_as_unknown():
    """空白名單是最嚴格的合法狀態：任何對話框都停機。"""
    v = dg.match_whitelist(dlg(), ())
    assert v.known is False


def test_unmatched_dialog_is_unknown():
    v = dg.match_whitelist(dlg(title="磁碟區已滿"), (OVERWRITE, LICENSE))
    assert v.known is False


def test_unknown_verdict_carries_halt_status():
    v = dg.match_whitelist(dlg(), ())
    assert v.result_status == "HALTED_UNKNOWN_DIALOG"


def test_unknown_verdict_has_no_action():
    """
    未知時不能給出任何可執行的動作。呼叫端若照著按，等於白名單白做了。
    """
    v = dg.match_whitelist(dlg(), ())
    assert not v.action


# ── 匹配 ─────────────────────────────────────────────────────────────


def test_matching_rule_is_recognised():
    v = dg.match_whitelist(dlg(title="檔案已存在"), (OVERWRITE,))
    assert v.known is True
    assert v.action == "Cancel"
    assert v.result_status == "FAILED_TARGET_EXISTS"


def test_glob_matches_prefix():
    v = dg.match_whitelist(dlg(title="AccuMark 授權即將到期"), (LICENSE,))
    assert v.known is True
    assert v.action == "Close"


def test_first_matching_rule_wins():
    """規則順序有意義，使用者可以把更精確的規則排在前面。"""
    specific = DialogRule(
        title_like="檔案已存在 - 重要", action="No", result_status="FAILED_TARGET_EXISTS"
    )
    v = dg.match_whitelist(dlg(title="檔案已存在 - 重要"), (specific, OVERWRITE))
    assert v.action == "No"


def test_match_is_case_insensitive():
    """使用者手寫規則時大小寫常常對不上，不該因此漏掉。"""
    rule = DialogRule(
        title_like="*OVERWRITE*", action="Cancel", result_status="FAILED_TARGET_EXISTS"
    )
    assert dg.match_whitelist(dlg(title="Confirm overwrite?"), (rule,)).known is True


def test_exact_title_without_wildcard():
    rule = DialogRule(title_like="確認", action="No", result_status="FAILED_TIMEOUT")
    assert dg.match_whitelist(dlg(title="確認"), (rule,)).known is True
    assert dg.match_whitelist(dlg(title="確認刪除"), (rule,)).known is False


def test_verdict_records_which_rule_matched():
    """日誌要能說明是哪一條規則生效，否則使用者無從調整。"""
    v = dg.match_whitelist(dlg(title="檔案已存在"), (LICENSE, OVERWRITE))
    assert v.rule_index == 1


# ── 危險動作絕不會出現 ───────────────────────────────────────────────


@pytest.mark.parametrize("verdict_action", ["Yes", "OK", "確定", "Confirm"])
def test_dangerous_actions_are_rejected_at_the_guard(verdict_action):
    """
    設定層已經擋過一次危險動作，這裡是第二道。
    多一道的理由：白名單是唯一擋在「誤觸覆蓋」前面的東西。
    """
    rule = DialogRule(
        title_like="*", action=verdict_action, result_status="FAILED_TIMEOUT"
    )
    with pytest.raises(dg.UnsafeActionError, match=verdict_action):
        dg.match_whitelist(dlg(), (rule,))


# ── 給使用者看的描述 ─────────────────────────────────────────────────


def test_description_contains_everything_needed_to_extend_the_whitelist():
    """
    停機時記錄的資訊，必須正好是使用者擴充白名單所需要的全部：
    標題、內文、每一顆按鈕的文字。
    """
    d = dlg(
        title="AccuMark 提示",
        text="此 model 含有錯誤，是否繼續？",
        buttons=("是", "否", "取消"),
    )
    desc = dg.describe(d)
    for token in ("AccuMark 提示", "此 model 含有錯誤", "是", "否", "取消"):
        assert token in desc


def test_description_survives_missing_text():
    desc = dg.describe(dlg(title="只有標題", text="", buttons=()))
    assert "只有標題" in desc


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "標題第一行\n標題第二行"),
        ("text", "第一行\n第二行\r\n第三行"),
    ],
)
def test_description_is_single_line(field, value):
    """
    日誌一筆一行。標題與內文都要壓平——只測內文的話，
    標題那一路的 split/join 被拿掉也驗不出來。
    """
    d = dlg(**{field: value})
    desc = dg.describe(d)
    assert "\n" not in desc
    assert "\r" not in desc


# ── 前景視窗檢查（偵測注入） ─────────────────────────────────────────


def test_no_dialog_means_all_clear():
    v = dg.check_foreground(lambda: None, (OVERWRITE,))
    assert v is None


def test_known_dialog_returns_its_verdict():
    v = dg.check_foreground(lambda: dlg(title="檔案已存在"), (OVERWRITE,))
    assert v.known is True


def test_unknown_dialog_returns_unknown_verdict_with_description():
    v = dg.check_foreground(
        lambda: dlg(title="沒看過的視窗", text="內容", buttons=("是",)), ()
    )
    assert v.known is False
    assert "沒看過的視窗" in v.description
    assert "是" in v.description


def test_detector_failure_is_treated_as_unknown():
    """
    偵測本身出錯時，寧可當成「有未知視窗」停機，也不要假設畫面是乾淨的。
    這是安全方向的預設。
    """

    def broken():
        raise RuntimeError("UIA 掛了")

    v = dg.check_foreground(broken, (OVERWRITE,))
    assert v is not None
    assert v.known is False
    assert "UIA 掛了" in v.description


# ── 標題／內文／按鈕為 None（D0b blocker）───────────────────────────
#
# pywinauto 對某些視窗的 window_text() 會回 None 而不是空字串。守衛在
# describe() 裡直接對它 .split()，於是「安全停機」變成一份 traceback：
# 停是停了，但日誌裡沒有使用者拿去擴充白名單所需的任何資訊，而且
# 呼叫端拿到的是例外而不是判定，連 HALTED_UNKNOWN_DIALOG 都記不到。


def test_none_title_does_not_crash_and_still_halts():
    """None 標題視為空標題，仍走「未知 → 停機」那條路，而不是拋例外。"""
    v = dg.check_foreground(lambda: dlg(title=None), (OVERWRITE,))
    assert v is not None
    assert v.known is False
    assert v.result_status == "HALTED_UNKNOWN_DIALOG"
    assert not v.action


def test_none_title_is_described_as_untitled():
    """日誌要看得出「這個視窗沒有標題」，而不是留一段空白讓人猜。"""
    desc = dg.describe(dg.DialogInfo(title=None))
    assert "(無標題)" in desc


def test_none_title_does_not_match_a_real_rule():
    """空標題不能誤中「*已存在*」這類規則，否則會對不明視窗按 Cancel。"""
    v = dg.match_whitelist(dlg(title=None), (OVERWRITE, LICENSE))
    assert v.known is False


def test_none_text_is_safe():
    desc = dg.describe(dg.DialogInfo(title="標題", text=None))
    assert "標題" in desc
    assert "\n" not in desc


def test_none_button_text_is_safe_and_other_buttons_survive():
    """
    某一顆按鈕讀不到文字，不該讓整段描述掛掉——其他按鈕的文字
    正是使用者擴充白名單要抄的東西。
    """
    desc = dg.describe(dg.DialogInfo(title="標題", buttons=(None, "確定")))
    assert "確定" in desc


def test_none_buttons_tuple_is_safe():
    desc = dg.describe(dg.DialogInfo(title="標題", buttons=None))
    assert "標題" in desc


def test_everything_none_still_produces_a_single_line_description():
    d = dg.DialogInfo(title=None, text=None, buttons=None)
    desc = dg.describe(d)
    assert "(無標題)" in desc
    assert "\n" not in desc


def test_check_foreground_with_all_none_dialog_halts_with_description():
    """三個欄位全 None 是最壞情況，也要能安全停機並留下描述。"""
    v = dg.check_foreground(
        lambda: dg.DialogInfo(title=None, text=None, buttons=None), (OVERWRITE,)
    )
    assert v.known is False
    assert "(無標題)" in v.description


# ── 預期完成對話框（design §2.3、TD-4）──────────────────────────────
#
# ZIP 任務會等一個「Process Complete」對話框。它不是白名單項目——白名單
# 的動作只有 Cancel／Close／No，是「遇到不認識的東西要怎麼退」；完成對話框
# 則是「這一步本來就該按的那個鈕」，OK 由流程在確認檔案穩定後自己按。
# 守衛只負責認出它、不給動作；判定順序：完成對話框 → 白名單 → 未知。

COMPLETE = "*Process Complete*"


def test_expected_completion_is_recognised():
    v = dg.check_foreground(
        lambda: dlg(title="Process Complete"),
        (OVERWRITE,),
        expected_completion=COMPLETE,
    )
    assert v is not None
    assert v.completion is True


def test_completion_verdict_is_not_unknown():
    """完成訊號不能被當成未知視窗，否則每個 ZIP 任務都會在終點停機。"""
    v = dg.check_foreground(
        lambda: dlg(title="Process Complete"), (), expected_completion=COMPLETE
    )
    assert v.known is True
    assert v.result_status != "HALTED_UNKNOWN_DIALOG"


def test_completion_verdict_carries_no_action():
    """
    按 OK 是流程在「訊號到了 + 檔案穩定」之後自己做的事，不是守衛的動作。
    守衛若回傳任何動作，呼叫端照著按就等於在檔案還沒寫完時關掉對話框。
    """
    v = dg.check_foreground(
        lambda: dlg(title="Process Complete"), (), expected_completion=COMPLETE
    )
    assert not v.action
    assert not v.result_status


def test_completion_is_not_a_whitelist_hit():
    v = dg.check_foreground(
        lambda: dlg(title="Process Complete"), (), expected_completion=COMPLETE
    )
    assert v.rule_index == -1


def test_completion_beats_whitelist():
    """
    判定順序：先比完成對話框，再比白名單。使用者若在白名單放了一條
    「*」之類的寬鬆規則，完成對話框不能被它攔走變成 Cancel——那會取消
    掉一次本來成功的匯出。
    """
    catch_all = DialogRule(
        title_like="*", action="Cancel", result_status="FAILED_TIMEOUT"
    )
    v = dg.check_foreground(
        lambda: dlg(title="Process Complete"),
        (catch_all,),
        expected_completion=COMPLETE,
    )
    assert v.completion is True
    assert not v.action


def test_whitelist_still_applies_when_completion_does_not_match():
    v = dg.check_foreground(
        lambda: dlg(title="檔案已存在"), (OVERWRITE,), expected_completion=COMPLETE
    )
    assert v.completion is False
    assert v.known is True
    assert v.action == "Cancel"


def test_unknown_when_neither_completion_nor_whitelist_matches():
    v = dg.check_foreground(
        lambda: dlg(title="磁碟區已滿"), (OVERWRITE,), expected_completion=COMPLETE
    )
    assert v.completion is False
    assert v.known is False
    assert v.result_status == "HALTED_UNKNOWN_DIALOG"


def test_no_expected_completion_means_completion_title_is_unknown():
    """
    沒宣告完成對話框的任務（DXF 走 files 模式）看到「Process Complete」
    也該停機——那不是它在等的東西，出現就代表有狀況。
    """
    v = dg.check_foreground(lambda: dlg(title="Process Complete"), (OVERWRITE,))
    assert v.completion is False
    assert v.known is False


def test_completion_match_is_glob_and_case_insensitive():
    """沿用白名單的 fnmatchcase + lower()，使用者手寫樣式時大小寫常對不上。"""
    v = dg.check_foreground(
        lambda: dlg(title="AccuMark - PROCESS COMPLETE"),
        (),
        expected_completion=COMPLETE,
    )
    assert v.completion is True


def test_completion_description_is_logged():
    v = dg.check_foreground(
        lambda: dlg(title="Process Complete", buttons=("OK",)),
        (),
        expected_completion=COMPLETE,
    )
    assert "Process Complete" in v.description
    assert "OK" in v.description


def test_none_title_is_never_a_completion_signal():
    """
    無標題視窗絕不能被當成完成訊號：那會讓流程對一個不明視窗按 OK，
    正是 TD-5 要杜絕的路徑。就算樣式寬鬆到「*」也一樣。
    """
    v = dg.check_foreground(lambda: dlg(title=None), (), expected_completion="*")
    assert v.completion is False
    assert v.known is False


def test_empty_completion_pattern_means_no_expectation():
    """空樣式等於沒宣告，不能讓空標題與空樣式互相匹配成完成訊號。"""
    v = dg.check_foreground(lambda: dlg(title=""), (), expected_completion="")
    assert v.completion is False
    assert v.known is False


def test_no_dialog_is_still_all_clear_with_expected_completion():
    assert dg.check_foreground(lambda: None, (), expected_completion=COMPLETE) is None


def test_detector_failure_is_still_unknown_with_expected_completion():
    """get_fg 壞掉時的原錯誤不能被完成對話框的比對蓋掉。"""

    def broken():
        raise RuntimeError("UIA 掛了")

    v = dg.check_foreground(broken, (), expected_completion=COMPLETE)
    assert v.known is False
    assert v.completion is False
    assert "UIA 掛了" in v.description


def test_match_completion_is_a_pure_function():
    assert dg.match_completion(dlg(title="Process Complete"), COMPLETE) is True
    assert dg.match_completion(dlg(title="檔案已存在"), COMPLETE) is False
    assert dg.match_completion(dlg(title="Process Complete"), None) is False


def test_classify_orders_completion_before_whitelist_before_unknown():
    """classify 是 check_foreground 背後的純函式，三條路各走一次。"""
    catch_all = DialogRule(
        title_like="*", action="Close", result_status="FAILED_TIMEOUT"
    )
    done = dg.classify(dlg(title="Process Complete"), (catch_all,), COMPLETE)
    listed = dg.classify(dlg(title="檔案已存在"), (OVERWRITE,), COMPLETE)
    unknown = dg.classify(dlg(title="???"), (OVERWRITE,), COMPLETE)
    assert (done.completion, done.known) == (True, True)
    assert (listed.completion, listed.known, listed.action) == (False, True, "Cancel")
    assert (unknown.completion, unknown.known) == (False, False)
