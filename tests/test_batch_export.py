"""
期二進入點的純函式（D4）。

主流程本身要有 AccuMark 才跑得動，但「什麼情況該在動任何東西之前停下來」
全是純判斷——而那正是最要緊的部分：這些檢查存在的理由，就是讓失敗發生在
桌面上還沒多出任何東西的時候。
"""

from pathlib import Path

import pytest

import batch_export as be
from lib import archival


# ── 命令列 ───────────────────────────────────────────────────────────


def test_defaults_are_a_normal_full_run():
    args = be.parse_args([])
    assert args.only is None and args.only_format is None
    assert args.force is False and args.dry_run is False


def test_force_and_dry_run_flags():
    assert be.parse_args(["--force"]).force is True
    assert be.parse_args(["--dry-run"]).dry_run is True


def test_only_and_format_filters():
    args = be.parse_args(["--only", "A-1234", "--format", "AAMA"])
    assert args.only == "A-1234"
    assert args.only_format == "AAMA"


# ── model 名稱 ───────────────────────────────────────────────────────


def test_ordinary_model_names_pass():
    assert be.check_model_names(("A-1234", "外套-左前片")) is None


@pytest.mark.parametrize("name", [archival.UNCLASSIFIED_DIRNAME, archival.TIMEOUT_RESIDUE_DIRNAME])
def test_reserved_folder_names_are_rejected(name):
    """
    model 若剛好叫 `_未歸類` 或 `_逾時殘留`，它的產出會跟殘留物落在同一個
    資料夾裡。使用者之後分不出哪些是正常產出、哪些是出過問題的。
    """
    problem = be.check_model_names((name,))
    assert problem is not None
    assert name in problem


def test_model_name_with_illegal_characters_is_rejected():
    """model 名稱直接當資料夾名，斜線會把產出寫到別的地方去。"""
    assert be.check_model_names(("A/B",)) is not None


def test_the_offending_name_is_named():
    problem = be.check_model_names(("好的", "A/B"))
    assert "A/B" in problem


# ── 暫存夾 ───────────────────────────────────────────────────────────


def test_empty_temp_dir_is_fine():
    assert be.check_temp_dir(Path("X"), lambda d: ()) is None


def test_leftovers_stop_the_run():
    """
    殘留會被下一個任務當成自己的產出，歸到錯的 model 底下。
    這是「暫存夾裡的東西必屬當前任務」這條不變式的入口檢查。
    """
    problem = be.check_temp_dir(Path("X"), lambda d: ("上次的.dxf",))
    assert problem is not None
    assert "上次的.dxf" in problem


def test_the_message_never_offers_to_delete():
    """
    殘留可能是上次中斷時唯一的一份。腳本自動刪掉的話，使用者連東西
    曾經存在都不會知道——所以訊息要明講「不會替你刪」。
    """
    problem = be.check_temp_dir(Path("X"), lambda d: ("半成品.dxf",))
    assert "不會替你刪" in problem


def test_many_leftovers_are_truncated_but_counted():
    """一次列二十個檔名會把畫面洗掉，但總數要說清楚。"""
    problem = be.check_temp_dir(Path("X"), lambda d: tuple(f"f{i}.dxf" for i in range(20)))
    assert "20" in problem
    assert "…" in problem


def test_list_files_tolerates_a_missing_directory(tmp_path):
    """第一次執行時暫存夾還不存在，那是正常的，不是錯誤。"""
    assert be.list_files(tmp_path / "還沒建") == ()


# ── model 清單來源 ───────────────────────────────────────────────────


class FakeConfig:
    def __init__(self, is_selection_mode, models=()):
        self.is_selection_mode = is_selection_mode
        self.models = tuple(models)


class FakeOps:
    def __init__(self, selected=(), error=None):
        self._selected = tuple(selected)
        self._error = error

    def selected_models(self):
        if self._error:
            raise self._error
        return self._selected


def test_explicit_list_is_used_as_is():
    models, problem = resolve = be.resolve_models(FakeConfig(False, ("A", "B")), FakeOps())
    assert models == ("A", "B") and problem is None


def test_selection_mode_reads_the_explorer():
    models, problem = be.resolve_models(FakeConfig(True), FakeOps(selected=("A", "B")))
    assert models == ("A", "B") and problem is None


def test_nothing_selected_stops_instead_of_doing_everything():
    """
    忘了框選就把整個儲存區匯出一遍，是這個工具能造成的最大浪費——
    而且使用者要等它跑完才發現。
    """
    models, problem = be.resolve_models(FakeConfig(True), FakeOps(selected=()))
    assert models == ()
    assert problem is not None and "框選" in problem


def test_unreadable_selection_suggests_the_explicit_list():
    """
    清單不支援讀取選取狀態時，使用者需要知道「還有另一條路」，
    否則他會以為這個工具在他的機器上不能用。
    """
    from lib.uia import UiaError

    models, problem = be.resolve_models(FakeConfig(True), FakeOps(error=UiaError("沒有 pattern")))
    assert "明確清單" in problem
