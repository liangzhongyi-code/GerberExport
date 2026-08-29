"""
A0 的靜態守衛測試（對應 spec: operability「純雙擊啟動」「啟動檔內容限用 ASCII」）。

這些測試不執行任何 .bat，只檢查內容是否符合 TD-7 的骨架要求。
每一條斷言都對應一個實際會炸的失敗模式，理由見 design.md TD-7。
"""

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

# .bat 檔名 → 它應該呼叫的 Python 腳本
BAT_TARGETS = {
    "0_檢查環境.bat": "check_env.py",
    "1_執行探測.bat": "probe_ui.py",
    "2_執行探測_對話框.bat": "probe_ui.py",
    "3_執行批次匯出.bat": "batch_export.py",
    "4_強制全部重跑.bat": "batch_export.py",
}
ALL_BATS = sorted(BAT_TARGETS)


def read_bytes(name: str) -> bytes:
    return (SCRIPTS / name).read_bytes()


def read_text(name: str) -> str:
    return read_bytes(name).decode("ascii")


# ── 存在性 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ALL_BATS)
def test_bat_exists(name):
    assert (SCRIPTS / name).is_file(), f"缺少啟動檔 {name}"


@pytest.mark.parametrize("name", ALL_BATS)
def test_target_script_exists(name):
    target = SCRIPTS / BAT_TARGETS[name]
    assert target.is_file(), f"{name} 指向不存在的 {BAT_TARGETS[name]}"


def test_no_unexpected_bat_files():
    """交付資料夾不應出現未列管的 .bat，避免使用者點到不該點的東西。"""
    found = {p.name for p in SCRIPTS.glob("*.bat")}
    assert found == set(ALL_BATS), f"未列管的 .bat：{found - set(ALL_BATS)}"


# ── 純 ASCII（cp950 主控台的致命雷）───────────────────────────────────


@pytest.mark.parametrize("name", ALL_BATS)
def test_bat_is_pure_ascii(name):
    """.bat 由 cmd.exe 以主控台代碼頁解讀；含非 ASCII 位元組會讓指令本身變亂碼。"""
    raw = read_bytes(name)
    bad = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not bad, f"{name} 含非 ASCII 位元組（前 3 個）：{bad[:3]}"


@pytest.mark.parametrize("name", ALL_BATS)
def test_bat_has_no_bom(name):
    """UTF-8 BOM 會被 cmd.exe 當成指令的一部分。"""
    assert not read_bytes(name).startswith(b"\xef\xbb\xbf"), f"{name} 帶有 UTF-8 BOM"


# ── TD-7 骨架必要元素 ────────────────────────────────────────────────


@pytest.mark.parametrize("name", ALL_BATS)
def test_uses_script_relative_path(name):
    """雙擊時工作目錄未必等於 .bat 所在目錄，必須用 %~dp0 並加引號。"""
    text = read_text(name)
    assert "%~dp0" in text, f"{name} 未使用 %~dp0，換個目錄雙擊就會找不到腳本"
    assert f'"%~dp0{BAT_TARGETS[name]}"' in text, f"{name} 的 %~dp0 路徑未以雙引號包住"


@pytest.mark.parametrize("name", ALL_BATS)
def test_pauses_before_closing(name):
    """沒有 pause，成功或失敗都是一閃即逝，使用者連錯誤訊息都看不到。"""
    lines = [ln.strip() for ln in read_text(name).splitlines()]
    assert "pause" in lines, f"{name} 缺少獨立一行的 pause"


@pytest.mark.parametrize("name", ALL_BATS)
def test_bat_uses_crlf_line_endings(name):
    """cmd.exe 對純 LF 的批次檔在多行結構上有已知問題，一律用 CRLF。"""
    raw = read_bytes(name)
    assert b"\r\n" in raw, f"{name} 未使用 CRLF"
    assert b"\n" not in raw.replace(b"\r\n", b""), f"{name} 含裸 LF 換行"


@pytest.mark.parametrize("name", ALL_BATS)
def test_python_launcher_fallback(name):
    """目標機的 python 未必在 PATH 上；py launcher 可用性較高，需雙軌。"""
    text = read_text(name)
    assert "py -3" in text, f"{name} 未優先使用 py -3"
    assert "where /q py" in text, f"{name} 缺少 py 存在性檢查"
    assert "set \"PY=python\"" in text, f"{name} 缺少退回 python 的分支"


@pytest.mark.parametrize("name", ALL_BATS)
def test_no_bytecode_cache(name):
    """-B 避免在交付資料夾（可能位於 USB／唯讀磁碟）產生 __pycache__。"""
    assert " -B " in read_text(name), f"{name} 缺少 -B，會產生 __pycache__"


@pytest.mark.parametrize("name", ALL_BATS)
def test_propagates_exit_code(name):
    text = read_text(name)
    assert "set RC=%ERRORLEVEL%" in text, f"{name} 未擷取結束碼"
    assert "exit /b %RC%" in text, f"{name} 未傳遞結束碼"


@pytest.mark.parametrize("name", ALL_BATS)
def test_forwards_arguments(name):
    """%* 讓腳本仍可手動帶參數（--only / --format），不影響雙擊。"""
    assert "%*" in read_text(name), f"{name} 未轉發參數"


@pytest.mark.parametrize("name", ALL_BATS)
def test_echo_off_and_setlocal(name):
    text = read_text(name)
    assert text.lstrip().startswith("@echo off"), f"{name} 未以 @echo off 開頭"
    assert "setlocal" in text, f"{name} 缺少 setlocal，會汙染呼叫端環境"


# ── 各 .bat 專屬參數 ─────────────────────────────────────────────────


def test_dialog_probe_passes_mode_flag():
    assert "--mode dialog" in read_text("2_執行探測_對話框.bat")


def test_force_rerun_passes_force_flag():
    assert "--force" in read_text("4_強制全部重跑.bat")


def test_normal_run_does_not_force():
    """一般批次絕不能帶 --force，否則每次都重跑已完成的任務。"""
    assert "--force" not in read_text("3_執行批次匯出.bat")


def test_plain_probe_has_no_mode_flag():
    assert "--mode" not in read_text("1_執行探測.bat")
