"""
期零：目標機環境檢查。

在任何實際工作開始之前，確認這台機器跑得動後面的東西。
整個技術棧建立在「目標機能用 pywinauto」這個前提上，而這件事
五分鐘就能驗證，卻決定後續所有任務是否白做。

判斷邏輯全部集中在 evaluate()——純函式，不碰真實環境，因此可以
在開發機完整測試各種組合，不需要真的去弄壞 Python 安裝。
"""

import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

MIN_PYTHON: Tuple[int, int] = (3, 8)

ONLINE_INSTALL = "pip install pywinauto"
OFFLINE_INSTALL = "pip install --no-index --find-links=wheels pywinauto"


def configure_stdout() -> None:
    """
    cp950 主控台下印出非 cp950 字元會拋 UnicodeEncodeError，
    腳本會在使用者眼前直接崩潰。errors='replace' 保證最壞情況
    只是幾個字變成問號，而不是整支掛掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


@dataclass
class Result:
    """檢查結果。lines 是要印給使用者看的內容，逐行。"""

    ok: bool
    lines: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def _fmt(version: Tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


def evaluate(
    python_version: Tuple[int, ...],
    pywinauto_version: Optional[str],
    py_launcher_found: bool,
) -> Result:
    """
    依環境事實判斷是否可以往下走。

    python_version      實際的 Python 版本，例如 (3, 12, 10)
    pywinauto_version   已安裝的版本字串；None 表示匯入失敗
    py_launcher_found   是否找得到 py launcher（只影響提示，不影響結果）

    問題會一次全部列出，避免使用者修好一個、再跑一次才發現下一個。
    """
    lines: List[str] = ["AccuMark 批次匯出 — 環境檢查", "=" * 44]
    problems: List[str] = []

    # Python 版本
    if python_version[:2] >= MIN_PYTHON:
        lines.append("[OK]  Python 版本   %s" % _fmt(python_version))
    else:
        lines.append("[!!]  Python 版本   %s" % _fmt(python_version))
        problems.append(
            "Python 版本過低：目前 %s，需要 %s 以上。"
            % (_fmt(python_version), _fmt(MIN_PYTHON))
        )

    # pywinauto
    if pywinauto_version:
        lines.append("[OK]  pywinauto     %s" % pywinauto_version)
    else:
        lines.append("[!!]  pywinauto     未安裝")
        problems.append(
            "缺少 pywinauto。兩種安裝方式，擇一即可：\n"
            "\n"
            "  (1) 這台機器連得上網路：\n"
            "        %s\n"
            "\n"
            "  (2) 這台機器沒有外網——把交付資料夾裡的 wheels\\ 帶過來，\n"
            "      在 scripts\\ 資料夾底下執行：\n"
            "        %s\n" % (ONLINE_INSTALL, OFFLINE_INSTALL)
        )

    # py launcher：找不到也無妨，.bat 會退回 python，僅提示
    if py_launcher_found:
        lines.append("[OK]  py launcher   可用")
    else:
        lines.append("[--]  py launcher   找不到（會改用 python，不影響執行）")

    lines.append("")
    if problems:
        lines.append("檢查未通過，需要先處理以下問題：")
        lines.append("")
        for i, p in enumerate(problems, 1):
            lines.append("%d. %s" % (i, p))
        lines.append("處理完之後，再雙擊一次這個 .bat 確認。")
    else:
        lines.append("檢查通過，可以進行下一步（1_執行探測.bat）。")

    return Result(ok=not problems, lines=lines)


def probe_environment() -> Result:
    """收集真實環境事實，交給 evaluate() 判斷。"""
    try:
        import pywinauto

        pywinauto_version = getattr(pywinauto, "__version__", "unknown")
    except Exception:
        pywinauto_version = None

    import shutil

    return evaluate(
        python_version=sys.version_info[:3],
        pywinauto_version=pywinauto_version,
        py_launcher_found=shutil.which("py") is not None,
    )


def main() -> int:
    configure_stdout()
    result = probe_environment()
    for line in result.lines:
        print(line)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
