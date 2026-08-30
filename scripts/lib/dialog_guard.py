"""
對話框守衛（TD-5）。

這是本專案最危險的失敗模式所在。匯出過程中可能彈出檔案覆蓋確認、授權
警告、model 損毀提示。若腳本盲目送出 Enter，有機率誤觸「是，覆蓋」而
造成不可回復的檔案遺失——而且當下不會有人發現。

在誤判成本不對稱的場合，安全預設必須偏向不作為：
  * 跑錯而停機 → 花五分鐘看日誌，把新對話框加進白名單
  * 盲按 Enter 而覆蓋掉版型檔 → 可能重做一整天的工作

因此採白名單制：**預設一律視為未知**，只有明確列出的才處理。
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


class UnsafeActionError(RuntimeError):
    """白名單裡出現了會造成變更的動作。"""


@dataclass(frozen=True)
class DialogInfo:
    title: str
    text: str = ""
    buttons: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    known: bool
    action: str
    result_status: str
    description: str = ""
    rule_index: int = -1


def describe(dialog: DialogInfo) -> str:
    """
    把對話框壓成一行文字寫進日誌。

    內容刻意涵蓋標題、內文與每一顆按鈕——這正好是使用者擴充白名單
    所需要的全部資訊，讓「遇到未知情況」自然演化成「白名單長大一點」，
    而不需要開發者介入。
    """
    parts = ["標題=" + " ".join(dialog.title.split())]
    if dialog.text:
        parts.append("內文=" + " ".join(dialog.text.split()))
    if dialog.buttons:
        parts.append("按鈕=" + "/".join(b.strip() for b in dialog.buttons))
    return "  ".join(parts)


def _unknown(description: str) -> Verdict:
    return Verdict(
        known=False,
        action="",  # 未知時不給任何可執行的動作
        result_status=HALT_STATUS,
        description=description,
    )


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


def check_foreground(
    detect_fn: Callable[[], Optional[DialogInfo]],
    rules: Sequence[DialogRule],
) -> Optional[Verdict]:
    """
    看看畫面上有沒有擋路的對話框。回傳 None 代表一切乾淨。

    偵測本身出錯時回傳「未知」而不是 None——寧可停機，也不要假設
    畫面是乾淨的然後繼續按下去。這是安全方向的預設。
    """
    try:
        dialog = detect_fn()
    except Exception as exc:  # noqa: BLE001 — 任何偵測失敗都當成有狀況
        return _unknown(f"對話框偵測失敗：{exc}")

    if dialog is None:
        return None
    return match_whitelist(dialog, rules)
