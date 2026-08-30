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
