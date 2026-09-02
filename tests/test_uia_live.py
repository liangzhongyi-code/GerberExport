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


# ══════════════════════════════════════════════════════════════════════
# 期二操作函式（D3）：以記事本為靶
# ══════════════════════════════════════════════════════════════════════
#
# 這一段驗的是「pywinauto 0.6.9 的 pattern 介面真的長這樣」——屬性名稱、
# 回傳型別、哪個控制項有哪個 pattern。這些都是執行期才知道的事，靜態掃描
# 與假物件都測不到（見本檔開頭）。
#
# 靶選記事本：每台 Windows 都有、Win32 標準控制項、UIA 樹小而穩定。
# 全程只用 UIA pattern：SetValue 寫進文字區不會動到游標與焦點；選單只
# 「定位」不展開；任何按鈕都不按；結束一律 kill，不會觸發存檔對話框。

import contextlib  # noqa: E402
import subprocess  # noqa: E402
from types import SimpleNamespace  # noqa: E402

# 同時容許中英文介面。這台開發機是中文介面（標題「未命名 - 記事本」），
# 目標機不確定，所以兩種都要能過。
NOTEPAD_TITLE_RE = r".*記事本|.*Notepad"

# 中文記事本的選單項目 Name 是「檔案(F)」，帶快速鍵括號——不是「檔案」。
# 這正是 TD-10 預期的語系落差；依序嘗試，任一存在即可。
FILE_MENU_NAMES = ("檔案(F)", "檔案", "File")

SAMPLE_TEXT = "批次匯出測試"


def _spec(strategy, value):
    """
    刻意用 SimpleNamespace 而不是 config.Control：操作函式的契約是鴨子型別
    （任何有 .strategy 與 .value 的物件），這裡順便證明它不綁死在某個類別。
    """
    return SimpleNamespace(strategy=strategy, value=value)


@contextlib.contextmanager
def _notepad():
    """
    啟動一個乾淨的記事本，離開時一律 kill。

    kill 而不是關視窗：文字區被 SetValue 過之後，正常關閉會跳「要儲存嗎」
    對話框，而那個對話框只能靠實體輸入或 Invoke 按鈕才關得掉——兩者都是
    這組測試不該做的事。TerminateProcess 沒有這個問題。
    """
    _needs_pywinauto()
    proc = subprocess.Popen(["notepad.exe"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


# 記事本本身開得很快，但這一整檔會連續啟動、砍掉二十幾個 process，其中一條
# 測試還同時開兩個。實測在那條之後偶爾會超過 10 秒才等到視窗——系統仍在回收
# 前一批 process。這個數字只影響「失敗要等多久」，不影響成功時的速度，所以
# 寧可放寬：偶發的紅燈會讓人開始不信任這組測試，那比多等幾秒糟得多。
NOTEPAD_WAIT_SEC = 30


def _open_notepad(proc):
    """
    以 process 縮小搜尋範圍。這台機器實測 `.*記事本|.*Notepad` 同時匹配到
    多個視窗（其他桌面上的記事本、Notepad++），不加 process 就會歧義。
    """
    return uia.find_window_by_spec(
        _spec("title_re", NOTEPAD_TITLE_RE), timeout_sec=NOTEPAD_WAIT_SEC, process=proc.pid
    )


def _edit_of(win):
    return uia.resolve(win, _spec("control_type", "Edit"))


# ── find_window_by_spec ──────────────────────────────────────────────


def test_find_window_by_spec_finds_notepad():
    with _notepad() as proc:
        win = _open_notepad(proc)
        label = uia.window_label(win)
        assert "記事本" in label or "Notepad" in label, label
        assert win.element_info.control_type == "Window"


def test_find_window_by_spec_only_accepts_title_re():
    """視窗只能用標題正規式找；其他策略是給子控制項用的，混用要立刻被擋。"""
    with pytest.raises(uia.UiaError) as exc:
        uia.find_window_by_spec(_spec("name", "AccuMark"), timeout_sec=0)
    assert "title_re" in str(exc.value)


def test_find_window_by_spec_reports_not_found_with_pattern():
    """
    找不到時要說「找什麼、沒找到」，讓 dry-run 直接把這句印給使用者。
    timeout 0 = 只試一次，測試不必空等。
    """
    _needs_pywinauto()
    pattern = "絕對不存在的視窗標題 9f3c1e"
    with pytest.raises(uia.WindowNotFoundError) as exc:
        uia.find_window_by_spec(_spec("title_re", pattern), timeout_sec=0)
    assert pattern in str(exc.value)


def test_find_window_by_spec_reports_ambiguity_with_titles():
    """
    兩個記事本同時開著，不帶 process 條件就必然歧義。歧義是「找不到唯一
    的視窗」，要走 WindowNotFoundError 這條路，但訊息必須列出撞到的標題，
    否則使用者只會看到一句「找不到」而視窗明明就在眼前。
    """
    with _notepad() as first, _notepad() as second:
        _open_notepad(first)
        _open_notepad(second)
        with pytest.raises(uia.WindowNotFoundError) as exc:
            uia.find_window_by_spec(_spec("title_re", NOTEPAD_TITLE_RE), timeout_sec=0)
        message = str(exc.value)
        assert isinstance(exc.value, uia.WindowAmbiguousError)
        assert "記事本" in message or "Notepad" in message, message


# ── resolve ──────────────────────────────────────────────────────────


def test_resolve_finds_edit_by_control_type():
    """config 預填用的就是 control_type=Edit（export_to_path），這條路一定要通。"""
    with _notepad() as proc:
        edit = _edit_of(_open_notepad(proc))
        assert edit.element_info.control_type == "Edit"


def test_resolve_finds_edit_by_auto_id():
    """記事本文字區的控制項 ID 固定是 15，UIA 把它曝光成 AutomationId。"""
    with _notepad() as proc:
        ctrl = uia.resolve(_open_notepad(proc), _spec("auto_id", "15"))
        assert ctrl.element_info.control_type == "Edit"


def test_resolve_finds_child_by_index():
    """
    index 支援兩種寫法：純整數是「第 n 個子控制項」；探測報告輸出的
    `Edit#0` 是「同層第 n 個該型別」，使用者照抄報告也要能用。
    """
    with _notepad() as proc:
        win = _open_notepad(proc)
        first = uia.resolve(win, _spec("index", "0"))
        expected = win.children()[0]
        assert (first.element_info.control_type, first.element_info.name) == (
            expected.element_info.control_type,
            expected.element_info.name,
        )
        typed = uia.resolve(win, _spec("index", "Edit#0"))
        assert typed.element_info.control_type == "Edit"


def test_resolve_finds_file_menu_by_name():
    """
    只定位、不展開。任一語系的名稱找到即可；三個都找不到才算失敗，
    並把三個都列出來讓人一眼看出是語系問題。
    """
    with _notepad() as proc:
        win = _open_notepad(proc)
        found = None
        for name in FILE_MENU_NAMES:
            try:
                found = uia.resolve(win, _spec("name", name))
                break
            except uia.ControlNotFoundError:
                continue
        assert found is not None, f"三個名稱都找不到：{FILE_MENU_NAMES}"
        assert found.element_info.control_type == "MenuItem"


def test_control_not_found_error_names_spec_and_window():
    """
    dry-run 的整個價值就在這句訊息：缺哪個、用什麼條件找的、在哪個視窗下。
    少任何一項，使用者就得回頭翻 700 個節點的探測報告。
    """
    with _notepad() as proc:
        win = _open_notepad(proc)
        with pytest.raises(uia.ControlNotFoundError) as exc:
            uia.resolve(win, _spec("name", "這個控制項不存在 7a2b"))
        message = str(exc.value)
        assert "name" in message
        assert "這個控制項不存在 7a2b" in message
        assert uia.window_label(win) in message
        assert isinstance(exc.value, RuntimeError)


def test_resolve_rejects_unknown_strategy():
    with _notepad() as proc:
        win = _open_notepad(proc)
        with pytest.raises(uia.UiaError) as exc:
            uia.resolve(win, _spec("xpath", "//Edit"))
    assert "xpath" in str(exc.value)


# ── set_value / read_value / read_text ───────────────────────────────


def test_set_value_round_trips_through_value_pattern():
    """ValuePattern 寫進去、ValuePattern 讀回來，游標與焦點全程不動。"""
    with _notepad() as proc:
        edit = _edit_of(_open_notepad(proc))
        uia.set_value(edit, SAMPLE_TEXT)
        assert uia.read_value(edit) == SAMPLE_TEXT
        assert uia.read_text(edit) == SAMPLE_TEXT


def test_read_text_does_not_mistake_control_name_for_empty_content():
    """
    實測抓到的陷阱：記事本文字區的 window_text() 回的是控制項 Name
    「文字編輯器」，不是內容。read_text 只能在 pattern **不存在**時才退到
    window_text()，內容是空字串就該回空字串——否則 Results 窗格為空時
    會讀到它的標籤，完成偵測就會誤判成「有結果」。
    """
    with _notepad() as proc:
        edit = _edit_of(_open_notepad(proc))
        assert uia.read_text(edit) == ""


def test_read_text_falls_back_to_window_text_when_no_pattern():
    """頂層視窗沒有 Value／Text pattern，退到 window_text() 就是標題。"""
    with _notepad() as proc:
        text = uia.read_text(_open_notepad(proc))
        assert "記事本" in text or "Notepad" in text, text


def test_read_value_rejects_control_without_value_pattern():
    with _notepad() as proc:
        with pytest.raises(uia.UiaError) as exc:
            uia.read_value(_open_notepad(proc))
    assert "Value" in str(exc.value)


def test_set_value_rejects_control_without_value_pattern():
    with _notepad() as proc:
        with pytest.raises(uia.UiaError) as exc:
            uia.set_value(_open_notepad(proc), "x")
    assert "Value" in str(exc.value)


# ── invoke / select_single / read_selected_names 的錯誤路徑 ──────────
#
# 記事本沒有清單也沒有可安全按的按鈕，所以這裡只驗「沒有 pattern 時
# 拒絕得乾淨」——這也是 dry-run 對 AccuMark 最常見的回報內容。


def test_invoke_rejects_control_without_invoke_pattern():
    """文字區沒有 InvokePattern（實測），不會有東西被按到。"""
    with _notepad() as proc:
        with pytest.raises(uia.UiaError) as exc:
            uia.invoke(_edit_of(_open_notepad(proc)))
    assert "Invoke" in str(exc.value)


def test_read_selected_names_rejects_control_without_selection():
    """
    讀不到選取狀態必須是錯誤，不能回空 tuple：TD-9 的讀回驗證若把
    「讀不到」當成「0 項」，錯誤訊息會指向選取而不是指向 pattern。
    """
    with _notepad() as proc:
        with pytest.raises(uia.UiaError) as exc:
            uia.read_selected_names(_edit_of(_open_notepad(proc)))
    assert "Selection" in str(exc.value)


def test_select_single_reports_missing_item_with_spec():
    with _notepad() as proc:
        edit = _edit_of(_open_notepad(proc))
        with pytest.raises(uia.ControlNotFoundError) as exc:
            uia.select_single(edit, "A-1234")
    assert "A-1234" in str(exc.value)


def test_menu_invoke_reports_missing_root_item_without_opening_anything():
    """
    第一層就找不到時，什麼都不該展開；訊息要列出選單列上實際有的項目，
    這是語系落差（File vs 檔案(F)）最快的診斷方式。
    """
    with _notepad() as proc:
        win = _open_notepad(proc)
        with pytest.raises(uia.ControlNotFoundError) as exc:
            uia.menu_invoke(win, [_spec("name", "這個選單不存在 5d1e")])
        message = str(exc.value)
        assert "這個選單不存在 5d1e" in message
        assert any(name in message for name in FILE_MENU_NAMES), message


# ══════════════════════════════════════════════════════════════════════
# 排序與比對邏輯：鴨子型別假物件
# ══════════════════════════════════════════════════════════════════════
#
# TD-3 拒絕的是「用 mock 假裝整個 UIA」——那會把假設寫死成測試。這裡的
# 假物件不假裝 UIA：它們只提供操作函式會碰的那幾個屬性，用來驗證**我們
# 自己的**邏輯——比對是否不分大小寫、讀回不一致是否拋錯、展開／選取／
# 收合的順序、失敗時有沒有把選單收回去。pattern 本身的真實行為由上面的
# 記事本測試負責。


def _no_pattern_error():
    from pywinauto.uia_defines import NoPatternInterfaceError  # noqa: PLC0415

    return NoPatternInterfaceError()


class _Fake:
    """最小的控制項替身：element_info、children/descendants、iface_*。"""

    def __init__(self, control_type, name="", auto_id="", children=(), patterns=None, log=None):
        self.element_info = SimpleNamespace(
            name=name, automation_id=auto_id, control_type=control_type,
            class_name="Fake", enabled=True,
        )
        self._children = list(children)
        self._patterns = dict(patterns or {})
        self.log = log if log is not None else []

    # ── pywinauto 介面的最小子集 ──
    def window_text(self):
        return self.element_info.name

    def class_name(self):
        return "Fake"

    def _matches(self, kwargs):
        title = kwargs.get("title")
        ctype = kwargs.get("control_type")
        return (title is None or self.element_info.name == title) and (
            ctype is None or self.element_info.control_type == ctype
        )

    def children(self, **kwargs):
        return [c for c in self._children if c._matches(kwargs)]

    def descendants(self, **kwargs):
        return [d for d in self.iter_descendants() if d._matches(kwargs)]

    def iter_descendants(self, **kwargs):
        for c in self._children:
            yield c
            yield from c.iter_descendants()

    def __getattr__(self, attr):
        if attr.startswith("iface_"):
            if attr in self._patterns:
                return self._patterns[attr]
            raise _no_pattern_error()
        raise AttributeError(attr)

    def get_selection(self):
        if "iface_selection" not in self._patterns:
            raise _no_pattern_error()
        return [SimpleNamespace(name=n, rich_text=n) for n in self._patterns["iface_selection"].names()]


class _FakeValue:
    def __init__(self, log, stubborn=False):
        self.CurrentValue = ""
        self._log = log
        self._stubborn = stubborn

    def SetValue(self, text):
        self._log.append(f"SetValue:{text}")
        if not self._stubborn:
            self.CurrentValue = text


class _FakeInvoke:
    def __init__(self, log, label):
        self._log, self._label = log, label

    def Invoke(self):
        self._log.append(f"Invoke:{self._label}")


class _FakeExpandCollapse:
    def __init__(self, log, label, on_expand=None):
        self._log, self._label, self._on_expand = log, label, on_expand

    def Expand(self):
        self._log.append(f"Expand:{self._label}")
        if self._on_expand:
            self._on_expand()

    def Collapse(self):
        self._log.append(f"Collapse:{self._label}")


class _FakeSingleSelectList:
    """一個清單的選取狀態；Select 任一項會清掉其他項（UIA 單選語意）。"""

    def __init__(self, log):
        self._log = log
        self.selected = []
        self.value = None  # 供下拉選單的 ValuePattern 讀回

    def item_pattern(self, label):
        state = self

        class _SelectionItem:
            def Select(self):
                state._log.append(f"Select:{label}")
                state.selected = [label]
                if state.value is not None:
                    state.value.CurrentValue = label

        return _SelectionItem()

    def names(self):
        return list(self.selected)


def _fake_list(log, item_names):
    state = _FakeSingleSelectList(log)
    items = [
        _Fake("ListItem", name=n, patterns={"iface_selection_item": state.item_pattern(n)}, log=log)
        for n in item_names
    ]
    return _Fake("List", name="Source File Name", children=items, patterns={"iface_selection": state}, log=log), state


def test_set_value_detects_readback_mismatch():
    """
    SetValue 對唯讀或自繪欄位可能「成功」卻沒寫進去（COM 不報錯）。
    不讀回比對的話，路徑欄沒改到、匯出就寫進上一次的資料夾。
    """
    log = []
    ctrl = _Fake("Edit", name="Destination Path", patterns={"iface_value": _FakeValue(log, stubborn=True)})
    with pytest.raises(RuntimeError) as exc:
        uia.set_value(ctrl, r"C:\temp")
    message = str(exc.value)
    # 比對 repr 而不是原始字串：實作刻意用 !r 格式化，因為「實際讀到的是
    # 空字串」與「實際讀到的是三個空白」在訊息裡必須看得出差別，而那正是
    # 自繪欄位最常見的兩種失敗樣子。repr 會把反斜線轉義成 \\。
    assert repr(r"C:\temp") in message and "''" in message, message


def test_select_single_matches_loosely_but_reports_original_name():
    """比對不分大小寫、去頭尾空白；回報與選取用的是清單裡的原名。"""
    log = []
    list_ctrl, state = _fake_list(log, ["A-1234 ", "a-1235"])
    item = uia.select_single(list_ctrl, " a-1234")
    assert item.element_info.name == "A-1234 "
    assert log == ["Select:A-1234 "]
    assert state.selected == ["A-1234 "]


def test_select_single_rejects_duplicate_names():
    """兩個同名項目無法決定選哪個，寧可拒絕也不能隨便選一個。"""
    log = []
    list_ctrl, _ = _fake_list(log, ["A-1234", "a-1234 "])
    with pytest.raises(uia.UiaError):
        uia.select_single(list_ctrl, "A-1234")
    assert log == [], "拒絕時不能已經選了其中一個"


def test_select_single_lists_available_items_when_missing():
    log = []
    list_ctrl, _ = _fake_list(log, ["A-1234", "A-1235"])
    with pytest.raises(uia.ControlNotFoundError) as exc:
        uia.select_single(list_ctrl, "B-9999")
    message = str(exc.value)
    assert "B-9999" in message and "A-1234" in message and "Source File Name" in message


def test_read_selected_names_reflects_selection_pattern():
    log = []
    list_ctrl, _ = _fake_list(log, ["A-1234 ", "A-1235"])
    assert uia.read_selected_names(list_ctrl) == ()
    uia.select_single(list_ctrl, "A-1235")
    assert uia.read_selected_names(list_ctrl) == ("A-1235",)


def test_set_combo_expands_selects_collapses_then_reads_back():
    """TD-9：File Type 一定要真的切到指定格式，順序與讀回都要對。"""
    log = []
    inner, state = _fake_list(log, ["AAMA", "ASTM"])
    value = _FakeValue(log)
    state.value = value
    combo = _Fake(
        "ComboBox", name="File Type", children=[inner],
        patterns={"iface_expand_collapse": _FakeExpandCollapse(log, "File Type"), "iface_value": value},
    )
    uia.set_combo(combo, "astm")
    assert log == ["Expand:File Type", "Select:ASTM", "Collapse:File Type"]


def test_set_combo_reports_readback_mismatch_and_still_collapses():
    log = []
    inner, _ = _fake_list(log, ["AAMA", "ASTM"])
    stuck = _FakeValue(log)
    stuck.CurrentValue = "AAMA"  # 選了 ASTM 但值沒變
    combo = _Fake(
        "ComboBox", name="File Type", children=[inner],
        patterns={"iface_expand_collapse": _FakeExpandCollapse(log, "File Type"), "iface_value": stuck},
    )
    with pytest.raises(uia.UiaError) as exc:
        uia.set_combo(combo, "ASTM")
    assert "ASTM" in str(exc.value) and "AAMA" in str(exc.value)
    assert "Collapse:File Type" in log, "讀回失敗也要把下拉收回去"


def _fake_menu(log, leaf_name="Export Zip"):
    """MenuBar → File（展開後才長出子項）→ Export Zip（可 Invoke）。"""
    leaf = _Fake("MenuItem", name=leaf_name, patterns={"iface_invoke": _FakeInvoke(log, leaf_name)})
    file_item = _Fake("MenuItem", name="File")
    file_item._patterns["iface_expand_collapse"] = _FakeExpandCollapse(
        log, "File", on_expand=lambda: file_item._children.append(leaf)
    )
    bar = _Fake("MenuBar", name="Application", children=[file_item])
    return _Fake("Window", name="AccuMark Explorer", children=[bar])


def test_menu_invoke_expands_each_level_then_invokes_last():
    log = []
    win = _fake_menu(log)
    uia.menu_invoke(win, [_spec("name", "File"), _spec("name", "Export Zip")])
    assert log == ["Expand:File", "Invoke:Export Zip"]


def test_menu_invoke_collapses_opened_menus_on_failure():
    """
    子項找不到時，已展開的選單必須收回去：留一個開著的選單在畫面上，
    下一個任務的定位會撞到它，而且使用者會以為是自己誤點的。
    """
    log = []
    win = _fake_menu(log, leaf_name="Something Else")
    with pytest.raises(uia.ControlNotFoundError) as exc:
        uia.menu_invoke(win, [_spec("name", "File"), _spec("name", "Export Zip")], popup_timeout_sec=0)
    assert "Export Zip" in str(exc.value)
    assert log == ["Expand:File", "Collapse:File"]
