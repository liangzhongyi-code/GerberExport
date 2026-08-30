"""
uia.py 裡「必須在真實 Windows 上才驗得出來」的那一小塊。

TD-3 把 uia.py 隔離成唯一碰真實環境的模組，其餘七個模組都是純函式、
完整可測。代價是 uia.py 自己一行測試都沒有——而它有 714 行。

這個空白讓一個 blocker 活到了實機執行：find_foreground_window() 呼叫
pywinauto.win32functions.GetForegroundWindow，但 pywinauto 0.6.9 裡
**沒有這個屬性**。四支對話框探測腳本（2a/2b/2c/2d）因此全部跑不起來，
而那正是交付點 1 的主要內容。

靜態掃描抓不到它：屬性是在執行期才解析的。單元測試也抓不到：把
pywinauto 換成假物件的話，假物件要嘛有這個屬性（測試變綠但現實是紅的），
要嘛沒有（測到的是假物件的行為，不是 pywinauto 的）。

只有真的去呼叫才算數。所以這裡放的是最小的一組：不需要 AccuMark，
不需要特定視窗，只需要一台跑著 Windows 的機器——桌面上永遠有前景視窗。
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="需要真實的 Windows 視窗系統"
)

from lib import uia  # noqa: E402


def _needs_pywinauto():
    try:
        uia._load_pywinauto()
    except uia.PywinautoMissingError:
        pytest.skip("這台機器沒有 pywinauto")


# ── 前景視窗控制代碼 ─────────────────────────────────────────────────


def test_foreground_handle_actually_works():
    """
    這是本檔存在的理由。原本的實作在這裡直接拋 AttributeError，
    而四支對話框探測全部走這條路。
    """
    handle = uia.foreground_handle()
    assert isinstance(handle, int)
    assert handle != 0, "桌面上永遠有前景視窗，抓不到表示 API 呼叫本身壞了"


def test_foreground_handle_does_not_need_pywinauto():
    """
    取控制代碼只是一次 user32 呼叫。繞過 pywinauto 的內部模組，
    正是為了不再被它的版本差異絆倒。
    """
    import ctypes  # noqa: PLC0415

    assert uia.foreground_handle() == ctypes.windll.user32.GetForegroundWindow()


# ── 錯誤路徑（注入，不需要真的弄掉前景視窗）─────────────────────────


def test_zero_handle_is_reported_as_not_found():
    """
    鎖屏或切換桌面時 GetForegroundWindow 會回 0。那要變成一句看得懂的
    指示，不是 pywinauto 深處拋出來的例外。
    """
    with pytest.raises(uia.WindowNotFoundError) as exc:
        uia.find_foreground_window(handle_fn=lambda: 0)
    assert "最上層" in str(exc.value) or "前景" in str(exc.value)


def test_wrapping_failure_is_reported_as_not_found():
    """控制代碼有效但包不成控制項時，一樣要走同一條錯誤路徑。"""

    def bogus():
        return 999999999

    with pytest.raises(uia.WindowNotFoundError):
        uia.find_foreground_window(handle_fn=bogus)


# ── 列出視窗：探測失敗時的唯一線索 ───────────────────────────────────


def test_list_top_windows_finds_something():
    """
    找不到目標時要靠這個列出候選給使用者挑。它自己壞掉的話，
    使用者就只剩「找不到」三個字，沒有任何下一步。
    """
    _needs_pywinauto()
    titles = uia.list_top_windows()
    assert isinstance(titles, tuple)
    assert len(titles) > 0, "桌面上至少有工作列"
