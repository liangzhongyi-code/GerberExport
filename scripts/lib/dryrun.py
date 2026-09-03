"""
`--dry-run`：只定位、不操作，回報缺哪幾個（TD-10）。

把「人讀 700 個節點的探測報告」變成「程式列出缺項清單」。也是驗證
pywinauto 看不看得見 AccuMark 的最快方法——若一個控制項都找不到，表示
介面是自繪的、UIA 無能為力，那是唯一能讓整個方案翻船的未知數。

**這個模組只收兩個函式：找視窗、找控制項。** 不收 ops、不收操作介面——
「絕不 Invoke／SetValue／Select」因此是結構上的保證，而不是靠自律。
"""

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

WINDOW_KEY = "window"

GROUP_TITLES = {
    "explorer": "AccuMark Explorer（ZIP 任務）",
    "dcu": "Data Conversion Utility（AAMA／ASTM 任務）",
}


@dataclass(frozen=True)
class CheckResult:
    group: str
    name: str
    strategy: str
    value: str
    found: bool
    detail: str = ""
    # 視窗沒找到時，底下的項目根本沒被查過。把它們算進「定位不到」會讓
    # 使用者以為每個設定都要改，其實只要把程式打開——兩者的下一步完全不同。
    checked: bool = True

    @property
    def is_window(self) -> bool:
        return self.name == WINDOW_KEY


def _spec_text(spec) -> Tuple[str, str]:
    return str(getattr(spec, "strategy", "?")), str(getattr(spec, "value", "?"))


def check_group(
    group: str,
    specs: Mapping[str, Any],
    *,
    find_window_fn: Callable[[Any], Any],
    resolve_fn: Callable[[Any, Any], Any],
) -> Tuple[CheckResult, ...]:
    """
    檢查一組控制項。視窗找不到時，底下每一項都標成「未檢查」而不是
    「找不到」——它們可能好端端的，只是沒有視窗可以找。把兩者混為一談，
    使用者會以為要改十個設定，其實只要把那個視窗打開。
    """
    window_spec = specs[WINDOW_KEY]
    strategy, value = _spec_text(window_spec)

    try:
        window = find_window_fn(window_spec)
    except Exception as exc:  # noqa: BLE001 — 任何定位失敗都只是一筆結果
        head = CheckResult(group, WINDOW_KEY, strategy, value, False, str(exc))
        rest = [
            CheckResult(g, n, s, v, False, "視窗沒找到，這一項沒有檢查", checked=False)
            for g, n, s, v in (
                (group, name, *_spec_text(spec))
                for name, spec in specs.items()
                if name != WINDOW_KEY
            )
        ]
        return (head,) + tuple(rest)

    results = [CheckResult(group, WINDOW_KEY, strategy, value, True)]
    for name, spec in specs.items():
        if name == WINDOW_KEY:
            continue
        s, v = _spec_text(spec)
        try:
            resolve_fn(window, spec)
        except Exception as exc:  # noqa: BLE001
            # 一個失敗不能中斷其餘：使用者要的是完整缺項清單，
            # 不是「第一個壞掉的東西」然後再跑一次看下一個。
            results.append(CheckResult(group, name, s, v, False, str(exc)))
        else:
            results.append(CheckResult(group, name, s, v, True))
    return tuple(results)


def check_controls(
    controls,
    groups: Sequence[str],
    *,
    find_window_fn: Callable[[Any], Any],
    resolve_fn: Callable[[Any, Any], Any],
) -> Tuple[CheckResult, ...]:
    """依序檢查每一組。某一組整個失敗，其餘照常檢查。"""
    out = []
    for group in groups:
        specs = getattr(controls, group)
        out.extend(
            check_group(
                group, specs, find_window_fn=find_window_fn, resolve_fn=resolve_fn
            )
        )
    return tuple(out)


# ── 選取狀態 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelectionCheck:
    """
    Explorer 的 model 清單能不能讀出「使用者框選了哪些」。

    這決定了日常操作的形狀：讀得到就是「框選再雙擊」，設定檔完全不用碰；
    讀不到就得在 config.json 裡列出 model 名稱，換 model 時要編輯檔案。

    刻意跟 CheckResult 分開：控制項找不找得到是「設定對不對」，這一項是
    「能用哪一種用法」——後者讀不到並不代表有東西壞掉。
    """

    supported: bool
    items: Tuple[str, ...] = ()
    error: str = ""


def check_selection(list_ctrl, read_fn: Callable[[Any], Any]) -> SelectionCheck:
    """
    問清單一次「你目前選了哪些」。

    read_fn 回傳的東西要有 supported／items／error 三個屬性（uia.read_selection
    就是這個形狀）。它自己炸掉也不讓 dry-run 失敗——這是輔助資訊。
    """
    try:
        probe = read_fn(list_ctrl)
    except Exception as exc:  # noqa: BLE001
        return SelectionCheck(supported=False, error=str(exc))
    return SelectionCheck(
        supported=bool(getattr(probe, "supported", False)),
        items=tuple(getattr(probe, "items", ()) or ()),
        error=str(getattr(probe, "error", "") or ""),
    )


def format_selection(check: SelectionCheck) -> list:
    """三種結論，三種下一步。"""
    lines = ["", "model 清單能不能讀出「你框選了哪些」", "-" * 56]

    if check.supported and check.items:
        shown = "、".join(check.items[:5]) + ("…" if len(check.items) > 5 else "")
        lines.append(f"  [OK]  可以讀取，目前選了 {len(check.items)} 項：{shown}")
        lines.append("        以後在 Explorer 框選要處理的 model 再雙擊就好，")
        lines.append("        設定檔完全不用改。")
        return lines

    if check.supported:
        lines.append("  [OK]  可以讀取，但你現在沒有框選任何項目。")
        lines.append("        功能是可用的——跑批次前記得先在 Explorer 框選。")
        return lines

    lines.append("  [缺]  讀不到選取狀態。")
    if check.error:
        lines.append(f"        原因：{check.error}")
    lines.append("        這不是壞掉，只是要換一種用法：在 config.json 的")
    lines.append('        models 欄位填明確清單，例如 ["A-1234", "A-1235"]，')
    lines.append("        換 model 時編輯那一行。把這個畫面回報給我，我幫你填。")
    return lines


def missing(results: Sequence[CheckResult]) -> Tuple[CheckResult, ...]:
    """真的查過、但找不到的。這些才是要改設定的項目。"""
    return tuple(r for r in results if r.checked and not r.found)


def unchecked(results: Sequence[CheckResult]) -> Tuple[CheckResult, ...]:
    """因為所屬視窗沒找到而沒查成的。把視窗弄出來就能查了。"""
    return tuple(r for r in results if not r.checked)


def exit_code(results: Sequence[CheckResult]) -> int:
    """沒驗完就不能說沒問題——未檢查一樣算失敗。"""
    return 1 if missing(results) or unchecked(results) else 0


def format_results(results: Sequence[CheckResult]) -> list:
    """
    畫面輸出。每一項都印——找到的那些同樣重要：使用者要看見「大部分都對上了，
    只差兩個」，而不是只看到一串錯誤然後以為整個方案沒救。
    """
    lines = []
    for group in dict.fromkeys(r.group for r in results):
        lines.append("")
        lines.append(GROUP_TITLES.get(group, group))
        lines.append("-" * 56)
        for r in (x for x in results if x.group == group):
            mark = "[OK]  " if r.found else "[缺]  "
            lines.append(f"  {mark}{r.name:<18}{r.strategy:<14}{r.value}")
            if r.detail:
                lines.append(f"          └ {r.detail}")

    gone = missing(results)
    skipped = unchecked(results)
    lines.append("")
    lines.append("=" * 56)
    if not gone and not skipped:
        lines.append(f"全部 {len(results)} 個控制項都定位得到，可以開始跑批次。")
        return lines

    if gone:
        lines.append(f"{len(results)} 個控制項裡有 {len(gone)} 個定位不到：")
        for r in gone:
            lines.append(f"  {r.group}.{r.name}    （{r.strategy} = {r.value}）")
    if skipped:
        groups = "、".join(
            dict.fromkeys(GROUP_TITLES.get(r.group, r.group) for r in skipped)
        )
        lines.append(f"另有 {len(skipped)} 個項目未檢查（{groups} 的視窗沒找到）。")
    lines.append("")
    lines.extend(_advice(results, gone, skipped))
    return lines


def _advice(
    results: Sequence[CheckResult],
    gone: Sequence[CheckResult],
    skipped: Sequence[CheckResult] = (),
) -> list:
    """
    缺項的意義完全取決於「缺多少」，所以建議要分情況給。

    全缺 = 介面可能是自繪的（方案翻船）；缺一群 = 那個視窗沒開；
    缺幾個 = 語系或版本差異，改設定檔就好。三者的下一步天差地遠。
    """
    if len(gone) + len(skipped) == len(results):
        # 三種可能依「常見程度」排序，不是依嚴重程度。把最嚇人的排前面，
        # 使用者會以為方案沒救而放棄，實際上前兩種五分鐘就能解決。
        #
        # 語系那一條特別重要：AccuMark 有中文版，中文版連視窗標題都可能
        # 不是 `AccuMark Explorer`——外觀與「介面自繪」一模一樣，但一個是
        # 改十幾行設定，一個是整個方案要換做法。
        return [
            "**一個都定位不到。** 三種可能，由常見到罕見：",
            "",
            "  1. AccuMark 與 DCU 沒開著，或被最小化了。",
            "     —— 打開它們再跑一次。",
            "",
            "  2. 這台的 AccuMark 是中文（或其他語系）介面，連視窗標題都跟",
            "     設定檔裡填的英文名稱對不上。",
            "     —— 這只要改設定檔就好。請雙擊 1_執行探測.bat，它會列出",
            "        畫面上所有視窗的實際標題，把那份結果回報即可。",
            "",
            "  3. AccuMark 的介面是自繪的，Windows 的無障礙介面看不見它。",
            "     —— 這一種才需要改用別的做法。請把這整個畫面回報。",
        ]

    window_gone = [r for r in gone if r.is_window]
    if not window_gone and skipped:
        window_gone = [r for r in results if r.is_window and not r.found]
    if window_gone:
        names = "、".join(GROUP_TITLES.get(r.group, r.group) for r in window_gone)
        return [
            f"有視窗沒找到（{names}）。先確認那個程式開著、視窗沒有最小化，",
            "再跑一次。視窗底下的項目要等視窗找到了才檢查得到。",
        ]

    return [
        "視窗都找到了，只是有幾個控制項對不上——多半是介面語系或版本差異",
        "（例如選單顯示「檔案」而不是 File）。",
        "請把這個畫面連同 probe-output 資料夾回報，我改設定檔即可，不用改程式。",
    ]
