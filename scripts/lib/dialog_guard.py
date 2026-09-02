"""
對話框守衛（TD-5）。

這是本專案最危險的失敗模式所在。匯出過程中可能彈出檔案覆蓋確認、授權
警告、model 損毀提示。若腳本盲目送出 Enter，有機率誤觸「是，覆蓋」而
造成不可回復的檔案遺失——而且當下不會有人發現。

在誤判成本不對稱的場合，安全預設必須偏向不作為：
  * 跑錯而停機 → 花五分鐘看日誌，把新對話框加進白名單
  * 盲按 Enter 而覆蓋掉版型檔 → 可能重做一整天的工作

因此採白名單制：**預設一律視為未知**，只有明確列出的才處理。

判定順序（design.md §2.3）：
  1. 預期完成對話框（ZIP 任務等的「Process Complete」）→ 完成訊號，不給動作
  2. 白名單 → 回該條規則的動作（限 Cancel／Close／No）
  3. 其餘 → 未知，停機

完成對話框刻意不放進白名單：白名單是「遇到不認識的東西要怎麼退」，
完成對話框是「這一步本來就該按的那個鈕」。OK 由流程在確認檔案穩定後
自己按，守衛只負責認出它。

pywinauto 對某些視窗的標題／內文／按鈕文字會回 None。守衛一律把 None
當空字串處理——停機是要留下使用者能拿去擴充白名單的資訊，不是 traceback。
"""

from dataclasses import dataclass
# 用 fnmatchcase 而不是 fnmatch：後者會經過 os.path.normcase，
# 在 Windows 上自動不分大小寫、在 Linux 上卻分——同一份規則在不同平台
# 行為不同，而且測試在 Windows 上根本驗不出大小寫處理是否正確。
# 這裡明確 lower() 兩邊，行為就與平台無關了。
from fnmatch import fnmatchcase
from typing import Callable, Optional, Sequence, Tuple

from .config import VALID_ACTIONS, DialogRule

HALT_STATUS = "HALTED_UNKNOWN_DIALOG"
UNTITLED = "(無標題)"
UNLABELLED_BUTTON = "(無文字)"


class UnsafeActionError(RuntimeError):
    """白名單裡出現了會造成變更的動作。"""


@dataclass(frozen=True)
class DialogInfo:
    """
    三個欄位都可能是 None：pywinauto 對某些視窗讀不到文字時就回 None，
    而不是空字串。
    """

    title: Optional[str]
    text: Optional[str] = ""
    buttons: Optional[Tuple[Optional[str], ...]] = ()


@dataclass(frozen=True)
class Verdict:
    """
    known=False              → 未知，停機（action 為空）
    known=True, completion   → 預期的完成對話框，不帶動作，流程自己決定何時按 OK
    known=True, 非 completion → 白名單命中，照 action 退場
    """

    known: bool
    action: str
    result_status: str
    description: str = ""
    rule_index: int = -1
    completion: bool = False


def _flatten(value: Optional[str]) -> str:
    """None 視為空字串；換行與連續空白壓成單一空格，日誌一筆一行。"""
    return " ".join((value or "").split())


def describe(dialog: DialogInfo) -> str:
    """
    把對話框壓成一行文字寫進日誌。

    內容刻意涵蓋標題、內文與每一顆按鈕——這正好是使用者擴充白名單
    所需要的全部資訊，讓「遇到未知情況」自然演化成「白名單長大一點」，
    而不需要開發者介入。

    任何欄位是 None 都不能讓這裡掛掉：這個函式是在停機路徑上被呼叫的，
    它一掛，停機就從「留下線索」變成「留下 traceback」。
    """
    parts = ["標題=" + (_flatten(dialog.title) or UNTITLED)]
    text = _flatten(dialog.text)
    if text:
        parts.append("內文=" + text)
    if dialog.buttons:
        labels = [_flatten(b) or UNLABELLED_BUTTON for b in dialog.buttons]
        parts.append("按鈕=" + "/".join(labels))
    return "  ".join(parts)


def _unknown(description: str) -> Verdict:
    return Verdict(
        known=False,
        action="",  # 未知時不給任何可執行的動作
        result_status=HALT_STATUS,
        description=description,
    )


def _completion(dialog: DialogInfo) -> Verdict:
    # 不給 action、不給 result_status：按 OK 的時機（訊號到了 + 檔案穩定）
    # 與最終狀態（SUCCESS 或逾時）都由流程決定，守衛只回報「看到了」。
    return Verdict(
        known=True,
        action="",
        result_status="",
        description=describe(dialog),
        completion=True,
    )


def match_completion(dialog: DialogInfo, title_like: Optional[str]) -> bool:
    """
    純函式：這個對話框是不是本任務宣告的完成對話框？

    比對方式與白名單一致（glob、不分大小寫）。兩個刻意的「不算」：
      * 沒宣告樣式（None 或空白）→ 永遠不算，DXF 任務本來就沒有完成對話框
      * 視窗沒有標題 → 永遠不算，就算樣式寬鬆到「*」也一樣——
        對一個不明視窗按 OK 正是 TD-5 要杜絕的路徑
    """
    if not title_like or not title_like.strip():
        return False
    title = dialog.title or ""
    if not title.strip():
        return False
    return fnmatchcase(title.lower(), title_like.lower())


def match_whitelist(
    dialog: DialogInfo, rules: Sequence[DialogRule]
) -> Verdict:
    """
    純函式：這個對話框在白名單裡嗎？

    標題以 glob 比對且不分大小寫——使用者手寫規則時大小寫常常對不上，
    不該因此漏掉一條本來寫對的規則。順序有意義，先匹配的先贏，
    所以更精確的規則可以排在前面。
    """
    title = dialog.title or ""
    for index, rule in enumerate(rules):
        # 設定層已經擋過危險動作，這裡是第二道。多一道的理由是：
        # 白名單是唯一擋在「誤觸覆蓋」前面的東西。
        if rule.action not in VALID_ACTIONS:
            raise UnsafeActionError(
                f"白名單第 {index + 1} 條的 action 是 {rule.action!r}，"
                f"只接受 {list(VALID_ACTIONS)}。"
                "允許按下「是／確定」等於拆掉整個安全模型"
            )
        if fnmatchcase(title.lower(), rule.title_like.lower()):
            return Verdict(
                known=True,
                action=rule.action,
                result_status=rule.result_status,
                description=describe(dialog),
                rule_index=index,
            )
    return _unknown(describe(dialog))


def classify(
    dialog: DialogInfo,
    rules: Sequence[DialogRule],
    expected_completion: Optional[str] = None,
) -> Verdict:
    """
    純函式：完成對話框 → 白名單 → 未知，依這個順序判定。

    完成對話框排在白名單前面，是因為使用者可能在白名單放一條寬鬆規則
    （例如「*」）；若讓它先攔到完成對話框，會對一次本來成功的匯出按 Cancel。
    """
    if match_completion(dialog, expected_completion):
        return _completion(dialog)
    return match_whitelist(dialog, rules)


def check_foreground(
    detect_fn: Callable[[], Optional[DialogInfo]],
    rules: Sequence[DialogRule],
    expected_completion: Optional[str] = None,
) -> Optional[Verdict]:
    """
    看看畫面上有沒有擋路的對話框。回傳 None 代表一切乾淨。

    expected_completion 是本任務宣告的完成對話框標題樣式（ZIP 任務為
    config 的 zip.complete_dialog.title_like），沒有就給 None。

    偵測本身出錯時回傳「未知」而不是 None——寧可停機，也不要假設
    畫面是乾淨的然後繼續按下去。這是安全方向的預設。
    """
    try:
        dialog = detect_fn()
    except Exception as exc:  # noqa: BLE001 — 任何偵測失敗都當成有狀況
        return _unknown(f"對話框偵測失敗：{exc}")

    if dialog is None:
        return None
    return classify(dialog, rules, expected_completion)
