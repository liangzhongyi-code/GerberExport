"""
把 config.controls 翻譯成實際的 UIA 呼叫（design.md §2.3 的 UI 層）。

這是 batch_export 唯一會碰到真實 AccuMark 的地方。orchestrator 只認得
這裡的方法名稱，所以開發機上用替身就能把整條流程跑完——這一層自己則
只能靠 dry-run 與目標機驗收。

三件刻意留在 orchestrator 而不做在這裡的事：

  * **選取的讀回驗證**（TD-9）。這裡只負責「選」與「讀」，判斷交給流程層，
    否則流程層漏掉那一步時測試照樣是綠的。
  * **完成訊號的判定**。這裡只回報「前景有什麼視窗」。
  * **什麼時候按完成對話框的 OK**。要等檔案穩定，那是 completion 的事。

視窗抓到之後會快取：每個任務重找一次要多花好幾秒，而 AccuMark 主視窗
在整批期間不會換。反過來說，使用者中途把 AccuMark 關掉再開，快取就失效
了——那時操作會拋 UiaError，訊息會說是哪個控制項，使用者重跑即可。
"""

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from . import uia
from .dialog_guard import DialogInfo

DEFAULT_WINDOW_TIMEOUT_SEC = 10.0
DEFAULT_CONTROL_TIMEOUT_SEC = 2.0

# 匯出精靈的對話框要等它自己跳出來，比一般控制項久一點。
DEFAULT_DIALOG_TIMEOUT_SEC = 15.0


class UiaOps:
    """orchestrator 要的那一組方法，全部走 lib.uia。"""

    def __init__(
        self,
        config,
        *,
        window_timeout_sec: float = DEFAULT_WINDOW_TIMEOUT_SEC,
        control_timeout_sec: float = DEFAULT_CONTROL_TIMEOUT_SEC,
        dialog_timeout_sec: float = DEFAULT_DIALOG_TIMEOUT_SEC,
    ):
        self._config = config
        self._explorer_specs = config.controls.explorer
        self._dcu_specs = config.controls.dcu
        self._zip_dialog = config.zip.complete_dialog
        self._labels = config.dxf.file_type_labels
        self._window_timeout = window_timeout_sec
        self._control_timeout = control_timeout_sec
        self._dialog_timeout = dialog_timeout_sec
        self._explorer = None
        self._dcu = None

    # ── 視窗 ─────────────────────────────────────────────────────────

    def explorer(self):
        if self._explorer is None:
            self._explorer = uia.find_window_by_spec(
                self._explorer_specs["window"], timeout_sec=self._window_timeout
            )
        return self._explorer

    def dcu(self):
        if self._dcu is None:
            self._dcu = uia.find_window_by_spec(
                self._dcu_specs["window"], timeout_sec=self._window_timeout
            )
        return self._dcu

    def connect(self) -> None:
        """
        啟動時確認兩個視窗都在。

        刻意在建立任何資料夾或狀態檔之前呼叫：AccuMark 沒開就中止，而且
        什麼都沒留下——使用者不會在桌面看到一個空的輸出資料夾，以為跑過了。
        """
        self.explorer()
        if self._config.dxf_formats:
            self.dcu()

    def _explorer_ctrl(self, name: str):
        return uia.resolve(self.explorer(), self._explorer_specs[name], self._control_timeout)

    def _dcu_ctrl(self, name: str):
        return uia.resolve(self.dcu(), self._dcu_specs[name], self._control_timeout)

    # ── 清單查詢 ─────────────────────────────────────────────────────

    def available_models(self, fmt: str) -> Tuple[str, ...]:
        if fmt == "ZIP":
            return uia.list_item_names(self._explorer_ctrl("model_list"))
        return uia.list_item_names(self._dcu_ctrl("source_list"))

    def selected_models(self) -> Tuple[str, ...]:
        """models: "SELECTED" 用的：使用者在 Explorer 裡框選了哪些。"""
        return uia.read_selected_names(self._explorer_ctrl("model_list"))

    # ── ZIP：Explorer → File → Export Zip ────────────────────────────

    def explorer_select(self, model: str) -> None:
        uia.select_single(self._explorer_ctrl("model_list"), model)

    def explorer_selection(self) -> Tuple[str, ...]:
        return uia.read_selected_names(self._explorer_ctrl("model_list"))

    def export_zip(self, model: str, dest: Path) -> None:
        """
        走完整個精靈：選單 → 「Export To」選資料夾 → 匯出畫面按 OK。

        匯出畫面上的元件選項（Model Notes、Measure Charts…）**一個都不碰**。
        使用者建檔時已經設好，腳本改了它們，產出的內容就跟他手動匯出的不一樣，
        而檔名完全相同——看不出來的那一種錯誤。
        """
        uia.menu_invoke(
            self.explorer(),
            [self._explorer_specs["menu_file"], self._explorer_specs["menu_export_zip"]],
        )

        export_to = uia.find_window_by_spec(
            self._explorer_specs["export_to_dialog"], timeout_sec=self._dialog_timeout
        )
        uia.set_value(
            uia.resolve(export_to, self._explorer_specs["export_to_path"], self._control_timeout),
            str(dest),
        )
        uia.invoke(
            uia.resolve(export_to, self._explorer_specs["export_to_ok"], self._control_timeout)
        )

        screen = uia.find_window_by_spec(
            self._explorer_specs["export_screen"], timeout_sec=self._dialog_timeout
        )
        uia.invoke(
            uia.resolve(screen, self._explorer_specs["export_screen_ok"], self._control_timeout)
        )

    # ── DXF：Data Conversion Utility ─────────────────────────────────

    def dcu_set_format(self, fmt: str) -> None:
        uia.set_combo(self._dcu_ctrl("file_type"), self._labels[fmt])

    def dcu_select(self, model: str) -> None:
        uia.select_single(self._dcu_ctrl("source_list"), model)

    def dcu_selection(self) -> Tuple[str, ...]:
        return uia.read_selected_names(self._dcu_ctrl("source_list"))

    def dcu_set_destination(self, dest: Path) -> None:
        uia.set_value(self._dcu_ctrl("destination_path"), str(dest))

    def dcu_run(self, model: str, fmt: str) -> None:
        uia.invoke(self._dcu_ctrl("run_button"))

    def dcu_results_text(self) -> str:
        """completion: "results_text" 模式用。讀不到就回空字串。"""
        try:
            return uia.read_text(self._dcu_ctrl("results"))
        except uia.UiaError:
            return ""

    # ── 守衛 ─────────────────────────────────────────────────────────

    def _known_windows(self) -> Sequence[Any]:
        return [w for w in (self._explorer, self._dcu) if w is not None]

    def foreground_dialog(self) -> Optional[DialogInfo]:
        """
        AccuMark 自己彈出來的視窗，沒有就 None。

        **不是**看系統的前景視窗：使用者一邊跑批次一邊用瀏覽器是核心需求，
        拿前景視窗當偵測對象的話，他切去別的程式就會把整批停掉。
        """
        popups = uia.popup_windows(self._known_windows())
        if not popups:
            return None
        window = popups[0]
        return DialogInfo(
            title=uia.window_title(window),
            text="",
            buttons=uia.button_names(window),
        )

    def _completion_window(self):
        pattern = self._zip_dialog.title_like.lower()
        for window in uia.popup_windows(self._known_windows()):
            title = (uia.window_title(window) or "").lower()
            if title and fnmatchcase(title, pattern):
                return window
        return None

    def dismiss_completion(self) -> None:
        """
        按完成對話框的 OK。

        重新找一次而不是沿用守衛看到的那個：中間隔了整段等待檔案穩定的
        時間，那個 wrapper 可能已經失效。找不到就拋——對話框還開著卻按不掉，
        下一個任務一定會撞到它，靜默跳過只會把問題往後推。
        """
        window = self._completion_window()
        if window is None:
            raise uia.UiaError(
                f"找不到標題符合 {self._zip_dialog.title_like!r} 的完成對話框，無法按下"
                f" {self._zip_dialog.ok_button!r}。它可能已經被別的方式關掉了"
            )
        button = uia.resolve(
            window,
            _NameSpec(self._zip_dialog.ok_button),
            self._control_timeout,
        )
        uia.invoke(button)


class _NameSpec:
    """完成對話框的 OK 鈕只有名稱、沒有進 controls，這裡臨時包一個 spec。"""

    strategy = uia.STRATEGY_NAME

    def __init__(self, value: str):
        self.value = value
