"""
C2 歸檔測試（對應 spec: file-archival 全部 / TD-8）。

plan() 是純函式：吃「有哪些產出、目的地已經有什麼」，吐出搬去哪裡，
完全不碰檔案系統。因此每一種撞名情境都能直接構造出來測。

TD-8 的核心：**預設保留 AccuMark 的原始檔名**，只有在目的地真的已有
同名檔時才加區別字尾。使用者每天在看這些檔名，比預防性設計更清楚實情；
但採信他的判斷不等於拿掉安全網——判斷落空時保留兩個檔案並記 WARN，
而不是靜默覆蓋。
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from lib import archival as arc

DEST = Path(r"C:\out\260830_1430\A-1234")


def plan(files, fmt="AAMA", existing=(), add_suffix=False, dest=DEST):
    return arc.plan(
        files=tuple(files),
        fmt=fmt,
        dest_dir=dest,
        existing=set(existing),
        add_format_suffix=add_suffix,
    )


def names(moves):
    return [m.dest_path.name for m in moves]


# ── 輸出資料夾命名 ───────────────────────────────────────────────────


def test_output_dir_expands_pattern():
    got = arc.resolve_output_dir(
        pattern="{root}_{yymmdd}_{HHMM}",
        root=Path(r"C:\Users\me\Desktop\AccuMark匯出"),
        when=datetime(2026, 8, 30, 14, 30),
    )
    assert got == Path(r"C:\Users\me\Desktop\AccuMark匯出_260830_1430")


def test_two_runs_same_day_get_different_dirs():
    """同一天跑兩次，第一次的結果不能被碰到。"""
    a = arc.resolve_output_dir(
        "{root}_{yymmdd}_{HHMM}", Path(r"C:\out"), datetime(2026, 8, 30, 9, 5)
    )
    b = arc.resolve_output_dir(
        "{root}_{yymmdd}_{HHMM}", Path(r"C:\out"), datetime(2026, 8, 30, 16, 42)
    )
    assert a != b


def test_unknown_placeholder_is_rejected():
    """拼錯的佔位符會變成字面文字，讓資料夾出現在奇怪的名字底下。"""
    with pytest.raises(arc.ArchivalError, match="yyyymmdd"):
        arc.resolve_output_dir(
            "{root}_{yyyymmdd}", Path(r"C:\out"), datetime(2026, 8, 30, 14, 30)
        )


def test_model_subdir():
    assert arc.model_dir(Path(r"C:\out\run"), "A-1234") == Path(r"C:\out\run\A-1234")


def test_model_name_with_illegal_characters_is_rejected():
    """model 名稱直接拿來當資料夾名，路徑穿越或非法字元必須擋下。"""
    for bad in ("../evil", "a/b", "a:b", "a*b"):
        with pytest.raises(arc.ArchivalError):
            arc.model_dir(Path(r"C:\out\run"), bad)


# ── TD-8：預設保留原始檔名 ───────────────────────────────────────────


def test_original_name_kept_when_no_conflict():
    """正常情況下檔名與 AccuMark 原始產出逐字相同。"""
    moves = plan(["A-1234.dxf"])
    assert names(moves) == ["A-1234.dxf"]
    assert moves[0].renamed is False


def test_all_three_formats_keep_original_names():
    zip_ = plan(["A-1234.zip"], fmt="ZIP")
    aama = plan(["A-1234-AAMA.dxf"], fmt="AAMA")
    astm = plan(["A-1234-ASTM.dxf"], fmt="ASTM")
    assert names(zip_) + names(aama) + names(astm) == [
        "A-1234.zip",
        "A-1234-AAMA.dxf",
        "A-1234-ASTM.dxf",
    ]


def test_multiple_outputs_all_planned():
    """AAMA 匯出可能同時吐出 .dxf 與規則檔，兩個都要歸檔。"""
    moves = plan(["A-1234.dxf", "A-1234.rul"])
    assert names(moves) == ["A-1234.dxf", "A-1234.rul"]


def test_dest_path_is_under_dest_dir():
    moves = plan(["A-1234.dxf"])
    assert moves[0].dest_path.parent == DEST


# ── TD-8：衝突時才改名 ──────────────────────────────────────────────


def test_conflict_appends_format_suffix():
    """
    AAMA 先歸檔保留原名，ASTM 後到發現撞名 → 加 _ASTM，兩者皆保留。
    """
    moves = plan(["A-1234.dxf"], fmt="ASTM", existing=["A-1234.dxf"])
    assert names(moves) == ["A-1234_ASTM.dxf"]
    assert moves[0].renamed is True


def test_conflict_is_flagged_for_warning():
    """衝突要能在日誌留下 WARN，否則使用者會以為腳本亂改名。"""
    moves = plan(["A-1234.dxf"], fmt="ASTM", existing=["A-1234.dxf"])
    assert moves[0].reason
    assert "A-1234.dxf" in moves[0].reason


def test_suffix_collision_falls_back_to_number():
    moves = plan(
        ["A-1234.dxf"],
        fmt="ASTM",
        existing=["A-1234.dxf", "A-1234_ASTM.dxf"],
    )
    assert names(moves) == ["A-1234_ASTM_2.dxf"]


def test_numbering_keeps_climbing():
    existing = ["A-1234.dxf", "A-1234_ASTM.dxf", "A-1234_ASTM_2.dxf"]
    moves = plan(["A-1234.dxf"], fmt="ASTM", existing=existing)
    assert names(moves) == ["A-1234_ASTM_3.dxf"]


def test_extension_is_preserved_when_renaming():
    moves = plan(["A-1234.tar.gz"], fmt="ZIP", existing=["A-1234.tar.gz"])
    assert names(moves)[0].endswith(".gz")
    assert "_ZIP" in names(moves)[0]


def test_file_without_extension_still_renames():
    moves = plan(["README"], fmt="ZIP", existing=["README"])
    assert names(moves) == ["README_ZIP"]


def test_two_outputs_of_same_task_do_not_collide_with_each_other():
    """同一次匯出的兩個產出彼此也不能撞名。"""
    moves = plan(["same.dxf", "same.dxf"], fmt="AAMA")
    assert len(set(names(moves))) == 2


# ── 強制加後綴 ───────────────────────────────────────────────────────


def test_forced_suffix_applies_without_conflict():
    moves = plan(["A-1234.dxf"], fmt="AAMA", add_suffix=True)
    assert names(moves) == ["A-1234_AAMA.dxf"]


def test_forced_suffix_still_avoids_collision():
    moves = plan(
        ["A-1234.dxf"], fmt="AAMA", add_suffix=True, existing=["A-1234_AAMA.dxf"]
    )
    assert names(moves) == ["A-1234_AAMA_2.dxf"]


# ── 絕不覆蓋 ─────────────────────────────────────────────────────────


def test_plan_never_targets_an_existing_file():
    existing = {"A-1234.dxf", "A-1234_AAMA.dxf", "A-1234_AAMA_2.dxf"}
    moves = plan(["A-1234.dxf"], fmt="AAMA", existing=existing)
    assert moves[0].dest_path.name not in existing


def test_plan_is_pure(tmp_path):
    """plan() 不該碰檔案系統——它只是算路徑。"""
    before = list(tmp_path.iterdir())
    plan(["a.dxf"], dest=tmp_path)
    assert list(tmp_path.iterdir()) == before


# ── 實際搬移 ─────────────────────────────────────────────────────────


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_temp_export(tmp_path, **files):
    src = tmp_path / "_temp"
    src.mkdir()
    for name, content in files.items():
        (src / name).write_bytes(content)
    return src


def test_execute_moves_files(tmp_path):
    src = make_temp_export(tmp_path, **{"A-1234.dxf": b"vector data"})
    dest = tmp_path / "out" / "A-1234"
    moves = plan(["A-1234.dxf"], dest=dest)
    written = arc.execute(moves, source_dir=src)
    assert (dest / "A-1234.dxf").is_file()
    assert written == (str(dest / "A-1234.dxf"),)


def test_execute_returns_strings_not_paths(tmp_path):
    """
    下游 TaskRecord.outputs 與 runstate 都以字串為準，而且 outputs 會被
    json.dumps 寫進 state.json——Path 物件在那裡會直接拋 TypeError。

    這個接縫沒有生產呼叫者，五個模組各自的單元測試都只餵字串，
    所以型別不符不會有任何測試亮紅燈。
    """
    import json

    src = make_temp_export(tmp_path, **{"a.dxf": b"x"})
    written = arc.execute(plan(["a.dxf"], dest=tmp_path / "out"), source_dir=src)
    assert all(isinstance(w, str) for w in written)
    json.dumps({"outputs": list(written)})  # 不應拋出


def test_execute_empties_the_temp_dir(tmp_path):
    """
    核心不變式：下一次匯出開始前暫存夾必為空，
    因此「暫存夾裡出現的任何東西」必然屬於當前任務。
    """
    src = make_temp_export(tmp_path, **{"a.dxf": b"x", "a.rul": b"y"})
    moves = plan(["a.dxf", "a.rul"], dest=tmp_path / "out")
    arc.execute(moves, source_dir=src)
    assert list(src.iterdir()) == []


def test_execute_preserves_bytes_exactly(tmp_path):
    content = bytes(range(256)) * 40
    src = make_temp_export(tmp_path, **{"A-1234.dxf": content})
    before = sha(src / "A-1234.dxf")
    dest = tmp_path / "out"
    moves = plan(["A-1234.dxf"], dest=dest)
    arc.execute(moves, source_dir=src)
    assert sha(dest / "A-1234.dxf") == before


def test_execute_creates_destination_directory(tmp_path):
    src = make_temp_export(tmp_path, **{"a.dxf": b"x"})
    dest = tmp_path / "deep" / "nested" / "A-1234"
    arc.execute(plan(["a.dxf"], dest=dest), source_dir=src)
    assert (dest / "a.dxf").is_file()


def test_execute_refuses_to_overwrite(tmp_path):
    """
    最後一道防線：即使 plan 算錯了，execute 也不能覆蓋既有檔案。
    """
    src = make_temp_export(tmp_path, **{"a.dxf": b"new"})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dxf").write_bytes(b"original")
    moves = plan(["a.dxf"], dest=dest)  # existing 沒告知，故意讓它撞
    with pytest.raises(arc.ArchivalError):
        arc.execute(moves, source_dir=src)
    assert (dest / "a.dxf").read_bytes() == b"original", "既有檔案被覆蓋了"


def test_source_files_survive_a_failed_move(tmp_path, monkeypatch):
    """
    搬移失敗時原始檔案必須留在暫存夾——那可能是唯一一份。
    """
    src = make_temp_export(tmp_path, **{"a.dxf": b"x", "b.dxf": b"y"})
    dest = tmp_path / "out"
    moves = plan(["a.dxf", "b.dxf"], dest=dest)

    real_move = arc.shutil.move
    calls = {"n": 0}

    def flaky_move(s, d):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("磁碟空間不足")
        return real_move(s, d)

    monkeypatch.setattr(arc.shutil, "move", flaky_move)

    with pytest.raises(arc.ArchivalError, match="磁碟空間不足"):
        arc.execute(moves, source_dir=src)
    assert (src / "b.dxf").is_file(), "失敗的那個檔案不見了"


def test_missing_source_file_is_an_error(tmp_path):
    """
    比對「暫存夾裡找不到」而不只是檔名：少了存在性檢查的話，
    shutil.move 拋的 OSError 訊息剛好也含檔名，測試會碰巧通過。
    """
    src = make_temp_export(tmp_path, **{"a.dxf": b"x"})
    moves = plan(["a.dxf", "ghost.dxf"], dest=tmp_path / "out")
    with pytest.raises(arc.ArchivalError, match="暫存夾裡找不到") as exc:
        arc.execute(moves, source_dir=src)
    assert "ghost.dxf" in str(exc.value)


def test_nothing_is_moved_when_one_source_is_missing(tmp_path):
    """
    先把整批檢查完再動手。逐個邊檢查邊搬的話，第一個檔案已經搬走、
    第二個才失敗，暫存夾與目的地會同時處在半完成狀態。
    """
    src = make_temp_export(tmp_path, **{"a.dxf": b"x"})
    dest = tmp_path / "out"
    moves = plan(["a.dxf", "ghost.dxf"], dest=dest)
    with pytest.raises(arc.ArchivalError):
        arc.execute(moves, source_dir=src)
    assert (src / "a.dxf").is_file(), "整批失敗了，卻已經有檔案被搬走"
    assert not dest.exists() or list(dest.iterdir()) == []


def test_nothing_is_moved_when_one_destination_exists(tmp_path):
    """同理：撞名的檢查也要在動手之前全部做完。"""
    src = make_temp_export(tmp_path, **{"a.dxf": b"x", "b.dxf": b"y"})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "b.dxf").write_bytes(b"original")
    moves = plan(["a.dxf", "b.dxf"], dest=dest)
    with pytest.raises(arc.ArchivalError):
        arc.execute(moves, source_dir=src)
    assert (src / "a.dxf").is_file()
    assert (dest / "b.dxf").read_bytes() == b"original"
