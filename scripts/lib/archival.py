"""
把匯出產物從暫存夾搬到它該去的地方。

TD-8：**預設保留 AccuMark 的原始檔名。** 使用者每天在看這些檔名，比任何
預防性設計更清楚實情；工廠端與 Illustrator 端拿到的檔名也應該跟手動匯出
完全一致。

但採信他的判斷不等於拿掉安全網。萬一真的撞名，做法是「保留兩個檔案 +
記一筆 WARN」，而不是靜默覆蓋——後者的失敗是無聲的：使用者會拿到少一個
檔案卻毫無錯誤訊息。

plan() 純算路徑，execute() 才動檔案。分開之後，所有撞名情境都能在沒有
檔案系統的情況下測完。

撞名比對一律**不分大小寫**：Windows 檔案系統把 `A.dxf` 與 `a.dxf` 當同一個
檔，比對若分大小寫，會判「沒撞」然後讓 shutil.move 靜默覆蓋。輸出的檔名
仍逐字保留原始大小寫——比對怎麼比是一回事，改人家的檔名是另一回事。

TD-9 的防線也在這裡：任務逐 model，暫存夾裡的東西理應全屬當前 model。
check_ownership() 把主檔名對不上的挑出來，主流程把它們搬到 `_未歸類\\<任務>\\`；
逾時的殘留物則搬到 `_逾時殘留\\<任務>\\`。兩者都只是「換個目的資料夾」，
搬移仍走 plan() + execute()，所以絕不覆蓋、保留原檔名的規則一體適用。
"""

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence, Set, Tuple

# Windows 檔名不能有這些字元；順便擋掉路徑穿越。
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PLACEHOLDER = re.compile(r"\{([a-zA-Z]+)\}")

MAX_RENAME_ATTEMPTS = 999

# 殘留物的落點。名稱寫進規格與使用手冊，使用者靠它找東西，不要改。
UNCLASSIFIED_DIRNAME = "_未歸類"
TIMEOUT_RESIDUE_DIRNAME = "_逾時殘留"
_RESIDUE_KINDS = (UNCLASSIFIED_DIRNAME, TIMEOUT_RESIDUE_DIRNAME)


class ArchivalError(RuntimeError):
    """歸檔過程出錯。發生時原始檔案一律留在暫存夾。"""


@dataclass(frozen=True)
class PlannedMove:
    source_name: str
    dest_path: Path
    renamed: bool
    reason: str = ""


# ── 路徑計算 ─────────────────────────────────────────────────────────


def resolve_output_dir(pattern: str, root: Path, when: datetime) -> Path:
    """
    展開輸出根目錄的命名樣板。

    拼錯的佔位符會變成字面文字，讓資料夾出現在奇怪的名字底下，
    所以未知佔位符直接拒絕。
    """
    values = {
        "root": str(root),
        "yymmdd": when.strftime("%y%m%d"),
        "HHMM": when.strftime("%H%M"),
    }
    unknown = [m.group(1) for m in _PLACEHOLDER.finditer(pattern) if m.group(1) not in values]
    if unknown:
        raise ArchivalError(
            f"output_dir_pattern 含無法辨識的佔位符：{unknown}，"
            f"可用的是 {sorted(values)}"
        )
    return Path(pattern.format(**values))


def _validate_dir_component(value: str, what: str) -> str:
    """
    要拿來當資料夾名的字串，一律過這一關：不能空、不能含非法字元、
    不能以點或空白開頭結尾（Windows 會自己吃掉，路徑就對不上）。
    """
    if not value or not value.strip():
        raise ArchivalError(f"{what}是空的，無法建立資料夾")
    if _ILLEGAL.search(value) or value.strip(". ") != value:
        raise ArchivalError(f"{what}含不能用於資料夾的字元：{value!r}")
    return value


def model_dir(output_dir: Path, model: str) -> Path:
    """model 名稱直接當資料夾名，因此非法字元與路徑穿越必須擋下。"""
    return output_dir / _validate_dir_component(model, "model 名稱")


def task_label(fmt: str, model: str) -> str:
    """
    殘留物資料夾用的任務標籤，例如 `AAMA_A-1234`。

    兩段各自驗證：標籤會直接變成資料夾名，任何一段夾帶斜線或 `..`
    都能把殘留物寫到輸出資料夾外面去。
    """
    return (
        f"{_validate_dir_component(fmt, '格式名稱')}"
        f"_{_validate_dir_component(model, 'model 名稱')}"
    )


def residue_dir(output_dir: Path, kind: str, label: str) -> Path:
    """
    殘留物該放哪：`<輸出資料夾>\\<kind>\\<任務標籤>\\`。

    kind 只收 UNCLASSIFIED_DIRNAME 或 TIMEOUT_RESIDUE_DIRNAME。放任意字串
    進來，殘留物會散落在自訂資料夾裡，使用者照手冊找 `_未歸類\\` 會找不到。
    """
    if kind not in _RESIDUE_KINDS:
        raise ArchivalError(
            f"殘留物資料夾種類 {kind!r} 不認得，只能是 {list(_RESIDUE_KINDS)}"
        )
    return output_dir / kind / _validate_dir_component(label, "任務標籤")


def _split_name(name: str) -> Tuple[str, str]:
    """
    切成 (主檔名, 副檔名)。".tar.gz" 這種只取最後一段，
    改名後副檔名仍在末尾，程式與人都還認得出來。
    """
    p = Path(name)
    return (p.stem, p.suffix) if p.suffix else (name, "")


def _with_suffix_token(name: str, token: str) -> str:
    stem, ext = _split_name(name)
    return f"{stem}_{token}{ext}"


def _fold(name: str) -> str:
    """
    撞名比對用的鍵。Windows 檔案系統不分大小寫，比對就不能分。

    casefold() 比 NTFS 的大小寫表更寬（例如把 ß 折成 ss），偶爾會把
    其實不撞的判成撞——後果只是多加一個字尾並記 WARN，是安全的方向；
    反過來漏判才會覆蓋檔案。
    """
    return name.casefold()


# ── TD-9 防線：產出歸屬 ──────────────────────────────────────────────


def check_ownership(
    files: Sequence[str], model: str
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """
    純函式：把暫存夾裡的檔名分成 (屬於該 model 的, 不屬於的)。

    主檔名（去掉最後一個副檔名）不分大小寫等於 model 才算它的。任務逐
    model，暫存夾裡照理全是它的；對不上的不是靜默歸錯資料夾，而是交給
    主流程搬到 `_未歸類\\<任務>\\` 並記 WARN。兩個回傳都保持輸入順序，
    可直接餵給 plan()，日誌列出來的順序也跟暫存夾看到的一致。
    """
    if not model or not model.strip():
        raise ArchivalError("model 名稱是空的，無法判斷產出歸屬")
    wanted = _fold(model)
    owned = []
    foreign = []
    for name in files:
        stem, _ = _split_name(name)
        (owned if _fold(stem) == wanted else foreign).append(name)
    return tuple(owned), tuple(foreign)


def plan(
    *,
    files: Sequence[str],
    fmt: str,
    dest_dir: Path,
    existing: Set[str],
    add_format_suffix: bool,
) -> Tuple[PlannedMove, ...]:
    """
    純函式：算出每個產出該叫什麼、放哪裡。不碰檔案系統。

    existing 是目的地已有的檔名集合。同一批次內已規劃的名稱也會一起
    佔位，避免同一次匯出的兩個產出彼此撞名。

    比對不分大小寫（taken 存的是折疊後的鍵），但輸出檔名逐字保留原始
    大小寫——只在末尾加字尾，不動 AccuMark 給的任何一個字。
    """
    taken = {_fold(n) for n in existing}
    moves = []

    for name in files:
        candidate = _with_suffix_token(name, fmt) if add_format_suffix else name
        renamed = add_format_suffix
        reason = ""

        if _fold(candidate) in taken:
            original = candidate
            # 先試格式字尾（比流水號有意義），再退回編號。
            with_fmt = _with_suffix_token(name, fmt)
            if _fold(with_fmt) not in taken:
                candidate = with_fmt
            else:
                stem, ext = _split_name(name)
                for n in range(2, MAX_RENAME_ATTEMPTS + 1):
                    trial = f"{stem}_{fmt}_{n}{ext}"
                    if _fold(trial) not in taken:
                        candidate = trial
                        break
                else:
                    raise ArchivalError(
                        f"{name} 在目的地已有太多同名檔案，無法產生不重複的名稱"
                    )
            renamed = True
            reason = (
                f"目的地已有 {original}（不分大小寫），為避免覆蓋改存為 {candidate}"
            )

        taken.add(_fold(candidate))
        moves.append(
            PlannedMove(
                source_name=name,
                dest_path=dest_dir / candidate,
                renamed=renamed,
                reason=reason,
            )
        )

    return tuple(moves)


# ── 實際搬移 ─────────────────────────────────────────────────────────


def _case_insensitive_clash(dest_path: Path) -> Optional[str]:
    """
    目的資料夾裡若已有與 dest_path 同名（不分大小寫）的項目，回傳它在
    磁碟上的實際名稱；沒有則 None。用列目錄而不用 exists()，是為了在
    區分大小寫的檔案系統上也能看見 a.dxf 與 A.DXF 撞在一起。
    """
    parent = dest_path.parent
    if not parent.is_dir():
        return None
    wanted = _fold(dest_path.name)
    for entry in parent.iterdir():
        if _fold(entry.name) == wanted:
            return entry.name
    return None


def execute(moves: Sequence[PlannedMove], source_dir: Path) -> Tuple[str, ...]:
    """
    把規劃好的搬移做掉，回傳實際寫入的路徑。

    失敗時原始檔案一律留在暫存夾——那可能是唯一一份。已經搬走的不回滾
    （它們在目的地是安全的），但會把錯誤往上拋，由主流程中止整批。

    回傳的是**字串**不是 Path：下游的 reporting.TaskRecord.outputs 與
    runstate 都以字串為準，而且 outputs 會被 json.dumps 寫進 state.json——
    Path 物件在那裡會直接拋 TypeError。這個接縫沒有生產呼叫者，五個模組
    各自的單元測試都只餵字串，所以型別不符不會有任何測試亮紅燈。
    """
    src = Path(source_dir)
    written = []

    # 先把整批檢查完再動手。逐個邊檢查邊搬的話，會搬到一半才發現缺檔，
    # 讓暫存夾與目的地同時處在半完成狀態——那比乾脆失敗更難收拾。
    claimed: Set[str] = set()
    for m in moves:
        if not (src / m.source_name).is_file():
            raise ArchivalError(f"暫存夾裡找不到 {m.source_name}")

        # 最後一道防線：即使 plan 算錯了，也不能覆蓋既有檔案。
        # 先列目的資料夾、不分大小寫地比，再退回 exists()——在區分大小寫
        # 的檔案系統上 exists() 看不見 a.dxf 與 A.DXF 是同一個檔；而
        # shutil.move 在 Windows 上會把它們當同一個檔直接蓋掉。
        clash = _case_insensitive_clash(m.dest_path)
        if clash is not None or m.dest_path.exists():
            shown = clash if clash is not None else m.dest_path.name
            hint = "" if shown == m.dest_path.name else f"（與 {m.dest_path.name} 僅大小寫不同）"
            raise ArchivalError(
                f"目的地已存在 {shown}{hint}，拒絕覆蓋。請先確認該檔案是否還需要"
            )

        # 同一批內兩個目的名僅大小寫不同：預檢時兩者都還不存在，
        # 不互相比對的話，第一個搬進去之後第二個就會蓋掉它。
        key = _fold(str(m.dest_path))
        if key in claimed:
            raise ArchivalError(
                f"同一批有兩個檔案要搬到 {m.dest_path}（不分大小寫），拒絕執行"
            )
        claimed.add(key)

    for m in moves:
        source = src / m.source_name
        try:
            m.dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(m.dest_path))
        except OSError as exc:
            raise ArchivalError(
                f"搬移 {m.source_name} 到 {m.dest_path} 失敗：{exc}"
            ) from exc

        written.append(str(m.dest_path))

    return tuple(written)
