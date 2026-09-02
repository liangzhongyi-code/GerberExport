"""
C2 歸檔測試（對應 spec: file-archival 全部 / TD-8）。

plan() 是純函式：吃「有哪些產出、目的地已經有什麼」，吐出搬去哪裡，
完全不碰檔案系統。因此每一種撞名情境都能直接構造出來測。

TD-8 的核心：**預設保留 AccuMark 的原始檔名**，只有在目的地真的已有
同名檔時才加區別字尾。使用者每天在看這些檔名，比預防性設計更清楚實情；
但採信他的判斷不等於拿掉安全網——判斷落空時保留兩個檔案並記 WARN，
而不是靜默覆蓋。

D1 補的三塊：
- 撞名比對不分大小寫。Windows 檔案系統把 `A.dxf` 與 `a.dxf` 當同一個檔，
  比對若分大小寫，shutil.move 會靜默覆蓋——正是 TD-8 要擋的那種無聲失敗。
- TD-9 防線 check_ownership()：任務逐 model，暫存夾裡的東西理應全屬它；
  主檔名對不上的要被看見，不能靜默歸進錯的資料夾。
- 殘留物落點 residue_dir()：`_未歸類\<任務>\` 與 `_逾時殘留\<任務>\`，
  搬移仍走 plan() + execute()，所以「絕不覆蓋、保留原檔名」一體適用。
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


# ── 大小寫：Windows 檔案系統不分，撞名比對也不能分 ───────────────────
#
# 這一段全走純函式 plan()，不靠檔案系統——在 Windows 上用真檔案測
# 「A.DXF 撞 a.dxf」，檔案系統自己就會把兩者當同一個，測不出比對邏輯
# 到底有沒有分大小寫。


def test_conflict_detected_when_existing_differs_only_in_case():
    """
    目的地有 a.dxf、新來的是 A.DXF：Windows 會把它們當同一個檔，
    比對若分大小寫就會判「沒撞」，接著 shutil.move 靜默覆蓋。
    """
    moves = plan(["A.DXF"], fmt="ASTM", existing=["a.dxf"])
    assert moves[0].renamed is True
    assert moves[0].reason


def test_conflict_detected_case_insensitively_within_same_batch():
    """同一批的兩個產出僅大小寫不同，也算撞名。"""
    moves = plan(["A.DXF", "a.dxf"], fmt="AAMA")
    assert len({n.casefold() for n in names(moves)}) == 2


def test_fallback_names_are_also_checked_case_insensitively():
    """
    加了字尾之後的候選名一樣要不分大小寫地比：目的地已有 a_astm.dxf，
    A_ASTM.DXF 就不能用，得繼續往序號退。
    """
    moves = plan(["A.DXF"], fmt="ASTM", existing=["a.dxf", "a_astm.dxf"])
    assert names(moves) == ["A_ASTM_2.DXF"]


def test_original_case_is_preserved_when_no_conflict():
    """比對不分大小寫，但輸出的檔名要逐字保留 AccuMark 給的大小寫。"""
    moves = plan(["A-1234.DXF"])
    assert names(moves) == ["A-1234.DXF"]


def test_original_case_is_preserved_when_renamed():
    """改名時也只加字尾，不能順手把大小寫「正規化」掉。"""
    moves = plan(["A-1234.DXF"], fmt="ASTM", existing=["a-1234.dxf"])
    assert names(moves) == ["A-1234_ASTM.DXF"]


@pytest.mark.parametrize("incoming", ["a.dxf", "A.DXF", "A.dxf", "a.DXF"])
def test_plan_never_targets_an_existing_file_regardless_of_case(incoming):
    existing = {"A.dxf", "a_ASTM.DXF", "A_astm_2.dxf"}
    moves = plan([incoming], fmt="ASTM", existing=existing)
    assert moves[0].dest_path.name.casefold() not in {e.casefold() for e in existing}


def test_execute_refuses_case_variant_of_existing_file(tmp_path):
    """
    最後一道防線也要不分大小寫：plan 沒被告知 existing 時，
    execute 不能讓 A.DXF 蓋掉目的地的 a.dxf。
    """
    src = make_temp_export(tmp_path, **{"A.DXF": b"new"})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dxf").write_bytes(b"original")
    with pytest.raises(arc.ArchivalError, match="a.dxf"):
        arc.execute(plan(["A.DXF"], dest=dest), source_dir=src)
    assert (dest / "a.dxf").read_bytes() == b"original", "既有檔案被覆蓋了"
    assert (src / "A.DXF").is_file()


def test_execute_case_check_does_not_rely_on_exists(tmp_path, monkeypatch):
    """
    在區分大小寫的檔案系統上（Linux CI、開了大小寫敏感旗標的 NTFS 目錄），
    dest_path.exists() 看不見「僅大小寫不同」的既有檔。這裡把 exists()
    換成嚴格比對名稱的版本來模擬那種檔案系統，證明預檢不是靠它擋的——
    否則在 Windows 上跑測試永遠是綠的，換台機器就會靜默覆蓋。
    """
    import os

    def strict_exists(self, *args, **kwargs):
        parent = os.path.dirname(str(self)) or "."
        return os.path.isdir(parent) and os.path.basename(str(self)) in os.listdir(parent)

    monkeypatch.setattr(Path, "exists", strict_exists)

    src = make_temp_export(tmp_path, **{"A.DXF": b"new"})
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "a.dxf").write_bytes(b"original")
    with pytest.raises(arc.ArchivalError):
        arc.execute(plan(["A.DXF"], dest=dest), source_dir=src)
    assert (dest / "a.dxf").read_bytes() == b"original", "既有檔案被覆蓋了"
    assert (src / "A.DXF").is_file()


def test_execute_refuses_batch_whose_targets_differ_only_in_case(tmp_path):
    """
    同一批兩個目的名僅大小寫不同：預檢時兩者都不存在，若不互相比對，
    第一個搬進去之後第二個就會蓋掉它。這種批次應該整批拒絕、一個都不搬。
    """
    src = make_temp_export(tmp_path, **{"x1.dxf": b"1", "x2.dxf": b"2"})
    dest = tmp_path / "out"
    moves = (
        arc.PlannedMove(source_name="x1.dxf", dest_path=dest / "A.DXF", renamed=False),
        arc.PlannedMove(source_name="x2.dxf", dest_path=dest / "a.dxf", renamed=False),
    )
    with pytest.raises(arc.ArchivalError):
        arc.execute(moves, source_dir=src)
    assert (src / "x1.dxf").is_file() and (src / "x2.dxf").is_file()
    assert not dest.exists() or list(dest.iterdir()) == []


# ── TD-9 防線：產出歸屬 ──────────────────────────────────────────────
#
# 任務逐 model，暫存夾裡的東西理應全屬當前 model。這個檢查不是拿來推
# 歸屬的（結構已保證），而是防線：DCU 若額外輸出、或選取殘留讓別的
# model 混進來，要被看見，而不是靜默歸進錯的資料夾。


def test_ownership_splits_files_by_stem():
    owned, foreign = arc.check_ownership(
        ["A-1234.dxf", "A-1234.rul", "B-9.dxf"], "A-1234"
    )
    assert owned == ("A-1234.dxf", "A-1234.rul")
    assert foreign == ("B-9.dxf",)


def test_ownership_ignores_case():
    """AccuMark 吐出來的大小寫未必跟清單上的一致，不能因此判成外來檔。"""
    owned, foreign = arc.check_ownership(["a-1234.DXF", "A-1234.Rul"], "A-1234")
    assert owned == ("a-1234.DXF", "A-1234.Rul")
    assert foreign == ()


def test_ownership_strips_only_the_last_extension():
    """主檔名的定義與改名邏輯一致：只去掉最後一個副檔名。"""
    owned, foreign = arc.check_ownership(["A-1234.tar.gz"], "A-1234.tar")
    assert owned == ("A-1234.tar.gz",)
    owned, foreign = arc.check_ownership(["A-1234.tar.gz"], "A-1234")
    assert foreign == ("A-1234.tar.gz",)


def test_ownership_requires_exact_stem_not_prefix():
    """
    前綴相同不算：A-12345 是另一個 model，A-1234_ASTM 不是這次匯出該有的名字。
    用 startswith 比對會把這些全放進去，防線就形同虛設。
    """
    owned, foreign = arc.check_ownership(
        ["A-12345.dxf", "A-1234_ASTM.dxf", "A-1234-AAMA.dxf", "xA-1234.dxf"], "A-1234"
    )
    assert owned == ()
    assert len(foreign) == 4


def test_ownership_file_without_extension():
    owned, foreign = arc.check_ownership(["A-1234", "A-1234.dxf"], "A-1234")
    assert owned == ("A-1234", "A-1234.dxf")
    assert foreign == ()


def test_ownership_preserves_input_order_and_returns_tuples():
    """回傳要能直接餵給 plan()，且日誌列出的順序要跟暫存夾看到的一致。"""
    owned, foreign = arc.check_ownership(
        ["z.dxf", "A-1234.rul", "y.dxf", "A-1234.dxf"], "A-1234"
    )
    assert isinstance(owned, tuple) and isinstance(foreign, tuple)
    assert owned == ("A-1234.rul", "A-1234.dxf")
    assert foreign == ("z.dxf", "y.dxf")


def test_ownership_of_empty_temp_dir():
    assert arc.check_ownership([], "A-1234") == ((), ())


def test_ownership_rejects_empty_model():
    """空的 model 名稱是呼叫端的 bug，要炸出來，而不是把所有檔案都判成外來。"""
    for bad in ("", "   "):
        with pytest.raises(arc.ArchivalError):
            arc.check_ownership(["A-1234.dxf"], bad)


# ── 殘留物落點 ───────────────────────────────────────────────────────
#
# 兩種殘留物：主檔名不符的（_未歸類）、逾時的（_逾時殘留）。都不刪、
# 都不留在暫存夾、都不進 model 資料夾。搬移沿用 plan() + execute()。


def test_residue_dirnames_are_fixed_strings():
    """資料夾名寫進規格與使用手冊，改了使用者就找不到東西。"""
    assert arc.UNCLASSIFIED_DIRNAME == "_未歸類"
    assert arc.TIMEOUT_RESIDUE_DIRNAME == "_逾時殘留"


def test_task_label_format():
    assert arc.task_label("AAMA", "A-1234") == "AAMA_A-1234"
    assert arc.task_label("ZIP", "A-1234") == "ZIP_A-1234"


def test_task_label_rejects_illegal_characters():
    """任務標籤直接當資料夾名，非法字元與路徑穿越要用 model_dir 同一套規則擋。"""
    for bad in ("../evil", "a/b", "a:b", "a*b", "a.", " a"):
        with pytest.raises(arc.ArchivalError):
            arc.task_label("AAMA", bad)
    with pytest.raises(arc.ArchivalError):
        arc.task_label("A/B", "A-1234")


def test_task_label_rejects_empty_parts():
    for fmt, model in (("", "A-1234"), ("AAMA", ""), ("AAMA", "  ")):
        with pytest.raises(arc.ArchivalError):
            arc.task_label(fmt, model)


def test_residue_dir_layout():
    out = Path(r"C:\out\260902_1430")
    assert arc.residue_dir(out, arc.UNCLASSIFIED_DIRNAME, "AAMA_A-1234") == (
        out / "_未歸類" / "AAMA_A-1234"
    )
    assert arc.residue_dir(out, arc.TIMEOUT_RESIDUE_DIRNAME, "ZIP_A-1234") == (
        out / "_逾時殘留" / "ZIP_A-1234"
    )


@pytest.mark.parametrize("kind", ["", "_其他", "AAMA", "未歸類", "_未歸類/", "A-1234"])
def test_residue_dir_rejects_unknown_kind(kind):
    """
    kind 只有兩個合法值。放任意字串進來，殘留物會散落在自訂資料夾裡，
    使用者依手冊找 `_未歸類\\` 會找不到。
    """
    with pytest.raises(arc.ArchivalError):
        arc.residue_dir(Path(r"C:\out"), kind, "AAMA_A-1234")


def test_residue_dir_rejects_illegal_label():
    for bad in ("", "../x", "a/b"):
        with pytest.raises(arc.ArchivalError):
            arc.residue_dir(Path(r"C:\out"), arc.UNCLASSIFIED_DIRNAME, bad)


def test_residue_dirs_of_different_tasks_and_kinds_do_not_overlap():
    """同一 model 的 AAMA 與 ASTM 殘留、以及未歸類與逾時殘留，都要分開放。"""
    out = Path(r"C:\out")
    dirs = {
        arc.residue_dir(out, arc.UNCLASSIFIED_DIRNAME, arc.task_label("AAMA", "A-1234")),
        arc.residue_dir(out, arc.UNCLASSIFIED_DIRNAME, arc.task_label("ASTM", "A-1234")),
        arc.residue_dir(out, arc.TIMEOUT_RESIDUE_DIRNAME, arc.task_label("AAMA", "A-1234")),
        arc.residue_dir(out, arc.TIMEOUT_RESIDUE_DIRNAME, arc.task_label("ZIP", "A-1234")),
    }
    assert len(dirs) == 4
    assert all(d.parent.parent == out for d in dirs)


def test_foreign_files_do_not_land_in_model_dir_and_keep_names(tmp_path):
    """
    端到端：check_ownership 分流 → 各自 plan → 各自 execute。
    外來檔要到 `_未歸類\\<任務>\\`，原檔名不變，不進 model 資料夾，
    也不留在暫存夾；owned 檔照常歸進 model 資料夾。
    """
    src = make_temp_export(
        tmp_path, **{"A-1234.dxf": b"a", "A-1234.rul": b"r", "B-9.dxf": b"b"}
    )
    out = tmp_path / "out"
    listing = sorted(p.name for p in src.iterdir())

    owned, foreign = arc.check_ownership(listing, "A-1234")
    assert foreign == ("B-9.dxf",)

    owned_dir = arc.model_dir(out, "A-1234")
    foreign_dir = arc.residue_dir(
        out, arc.UNCLASSIFIED_DIRNAME, arc.task_label("AAMA", "A-1234")
    )
    arc.execute(plan(owned, dest=owned_dir), source_dir=src)
    arc.execute(plan(foreign, dest=foreign_dir), source_dir=src)

    assert sorted(p.name for p in owned_dir.iterdir()) == ["A-1234.dxf", "A-1234.rul"]
    assert [p.name for p in foreign_dir.iterdir()] == ["B-9.dxf"]
    assert (foreign_dir / "B-9.dxf").read_bytes() == b"b"
    assert not (owned_dir / "B-9.dxf").exists()
    assert foreign_dir != owned_dir and owned_dir not in foreign_dir.parents
    assert list(src.iterdir()) == []


def test_timeout_residue_keeps_every_file_and_name(tmp_path):
    """
    逾時時暫存夾裡有什麼就搬什麼——包括寫到一半的檔——到 `_逾時殘留\\<任務>\\`，
    MUST NOT 刪除。搬過去的是證據，使用者要靠它判斷發生了什麼。
    """
    src = make_temp_export(tmp_path, **{"A-1234.dxf": b"partial", "A-1234.rul": b""})
    out = tmp_path / "out"
    residue = arc.residue_dir(
        out, arc.TIMEOUT_RESIDUE_DIRNAME, arc.task_label("AAMA", "A-1234")
    )
    listing = sorted(p.name for p in src.iterdir())
    arc.execute(plan(listing, dest=residue), source_dir=src)

    assert sorted(p.name for p in residue.iterdir()) == ["A-1234.dxf", "A-1234.rul"]
    assert (residue / "A-1234.dxf").read_bytes() == b"partial"
    assert not (arc.model_dir(out, "A-1234")).exists()
    assert list(src.iterdir()) == []
