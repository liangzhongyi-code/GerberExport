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
"""

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence, Set, Tuple

# Windows 檔名不能有這些字元；順便擋掉路徑穿越。
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PLACEHOLDER = re.compile(r"\{([a-zA-Z]+)\}")

MAX_RENAME_ATTEMPTS = 999


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


def model_dir(output_dir: Path, model: str) -> Path:
    """model 名稱直接當資料夾名，因此非法字元與路徑穿越必須擋下。"""
    if not model or not model.strip():
        raise ArchivalError("model 名稱是空的，無法建立資料夾")
    if _ILLEGAL.search(model) or model.strip(". ") != model:
        raise ArchivalError(f"model 名稱含不能用於資料夾的字元：{model!r}")
    return output_dir / model


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
    """
    taken = set(existing)
    moves = []

    for name in files:
        candidate = _with_suffix_token(name, fmt) if add_format_suffix else name
        renamed = add_format_suffix
        reason = ""

        if candidate in taken:
            original = candidate
            # 先試格式字尾（比流水號有意義），再退回編號。
            with_fmt = _with_suffix_token(name, fmt)
            if with_fmt not in taken:
                candidate = with_fmt
            else:
                stem, ext = _split_name(name)
                for n in range(2, MAX_RENAME_ATTEMPTS + 1):
                    trial = f"{stem}_{fmt}_{n}{ext}"
                    if trial not in taken:
                        candidate = trial
                        break
                else:
                    raise ArchivalError(
                        f"{name} 在目的地已有太多同名檔案，無法產生不重複的名稱"
                    )
            renamed = True
            reason = (
                f"目的地已有 {original}，為避免覆蓋改存為 {candidate}"
            )

        taken.add(candidate)
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
    for m in moves:
        if not (src / m.source_name).is_file():
            raise ArchivalError(f"暫存夾裡找不到 {m.source_name}")
        # 最後一道防線：即使 plan 算錯了，也不能覆蓋既有檔案。
        if m.dest_path.exists():
            raise ArchivalError(
                f"目的地已存在 {m.dest_path.name}，拒絕覆蓋。"
                "請先確認該檔案是否還需要"
            )

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
