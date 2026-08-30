"""
A0 的環境檢查邏輯測試（對應 spec: ui-probe「相依前置驗證」）。

check_env 的判斷邏輯抽成純函式 evaluate()，因此可以完整測試各種
環境組合，而不需要真的去弄壞開發機的 Python 安裝。
"""

import check_env


def lines_of(result):
    return "\n".join(result.lines)


# ── 相依齊備 ──────────────────────────────────────────────────────────


def test_all_dependencies_present():
    r = check_env.evaluate(
        python_version=(3, 12, 10),
        pywinauto_version="0.6.9",
        py_launcher_found=True,
    )
    assert r.ok is True
    assert r.exit_code == 0
    text = lines_of(r)
    assert "3.12.10" in text
    assert "0.6.9" in text


def test_minimum_supported_python_passes():
    """3.8 是下限，剛好符合就該通過（邊界值）。"""
    r = check_env.evaluate((3, 8, 0), "0.6.9", True)
    assert r.ok is True


# ── pywinauto 未安裝 ─────────────────────────────────────────────────


def test_missing_pywinauto_fails():
    r = check_env.evaluate((3, 12, 10), None, True)
    assert r.ok is False
    assert r.exit_code != 0


def test_missing_pywinauto_names_the_package():
    r = check_env.evaluate((3, 12, 10), None, True)
    assert "pywinauto" in lines_of(r)


def test_missing_pywinauto_offers_online_install():
    """規格要求給出具體可執行的補救指示，不是丟 traceback。"""
    r = check_env.evaluate((3, 12, 10), None, True)
    assert "pip install pywinauto" in lines_of(r)


def test_missing_pywinauto_offers_offline_install():
    """CAD 工作站常無外網，離線指令必須同時給出。"""
    text = lines_of(check_env.evaluate((3, 12, 10), None, True))
    assert "--no-index" in text
    assert "--find-links" in text


def test_offline_command_uses_absolute_path_when_known():
    """
    這條是為了一個真實的錯誤加的迴歸測試。

    交付包裡 wheels\\ 與 scripts\\ 是同層，不是 scripts\\ 的子目錄。
    原本的訊息叫使用者「在 scripts\\ 底下執行 --find-links=wheels」，
    照著做會找不到套件——而且錯誤訊息只會說找不到套件，看不出是
    路徑問題。

    相對路徑的正確性取決於使用者在哪個資料夾開 PowerShell，那是我們
    控制不了的，所以指令裡要填絕對路徑。
    """
    cmd = check_env.offline_install_command(r"C:\交付包\wheels")
    assert r"C:\交付包\wheels" in cmd
    assert "--find-links" in cmd


def test_offline_command_quotes_the_path():
    """路徑含空白或中文時，沒有引號會被 pip 當成多個參數。"""
    cmd = check_env.offline_install_command(r"C:\我的 交付包\wheels")
    assert '"C:\\我的 交付包\\wheels"' in cmd


def test_offline_command_falls_back_without_path():
    """找不到 wheels 資料夾時仍要給出一個看得懂的指令，而不是空字串。"""
    cmd = check_env.offline_install_command(None)
    assert "pip install" in cmd and "--no-index" in cmd


def test_absolute_path_reaches_the_user_message():
    text = lines_of(
        check_env.evaluate((3, 12, 10), None, True, wheels_dir=r"D:\交付包\wheels")
    )
    assert r"D:\交付包\wheels" in text


def test_message_does_not_give_directory_dependent_instructions():
    """
    知道絕對路徑時，訊息不該再叫使用者切到某個特定資料夾——
    那正是原本出錯的地方。
    """
    text = lines_of(
        check_env.evaluate((3, 12, 10), None, True, wheels_dir=r"D:\交付包\wheels")
    )
    assert "scripts\\ 資料夾底下執行" not in text


# ── Python 版本過低 ──────────────────────────────────────────────────


def test_old_python_fails():
    r = check_env.evaluate((3, 6, 8), "0.6.9", True)
    assert r.ok is False
    assert r.exit_code != 0


def test_old_python_reports_both_actual_and_required():
    text = lines_of(check_env.evaluate((3, 6, 8), "0.6.9", True))
    assert "3.6.8" in text, "未印出實際版本"
    assert "3.8" in text, "未印出所需版本"


# ── 多重問題 ─────────────────────────────────────────────────────────


def test_both_problems_are_reported_together():
    """一次講完所有問題，避免使用者修好一個又要再跑一次才發現下一個。"""
    text = lines_of(check_env.evaluate((3, 6, 8), None, True))
    assert "3.6.8" in text
    assert "pywinauto" in text


# ── py launcher ──────────────────────────────────────────────────────


def test_missing_py_launcher_is_a_warning_not_a_failure():
    """py 不在也沒關係，.bat 會退回 python；只需提示，不該擋住。"""
    r = check_env.evaluate((3, 12, 10), "0.6.9", py_launcher_found=False)
    assert r.ok is True
    assert "py" in lines_of(r)


# ── 輸出編碼（換用 Python 後新增的雷）────────────────────────────────


def test_module_configures_stdout_encoding():
    """
    cp950 主控台下印中文會拋 UnicodeEncodeError。
    腳本必須主動設定輸出編碼，否則使用者只會看到崩潰。
    """
    src = check_env.__file__
    with open(src, "r", encoding="utf-8") as f:
        text = f.read()
    assert "reconfigure" in text or "PYTHONIOENCODING" in text, (
        "check_env.py 未處理主控台輸出編碼"
    )
