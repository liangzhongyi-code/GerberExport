"""
UI Automation 樹走訪與定位策略評估（B1、B2）。

這是全專案唯一允許碰 pywinauto 的檔案（TD-3）。UI 互動在開發機上根本跑不
起來——這裡沒有 AccuMark——所以整個專案的策略是把不確定的東西壓縮進這一個
檔案，其餘模組維持純函式並完整 TDD。

但「壓縮進一個檔案」不等於「這個檔案就不用測」。檔案**內部**同樣切成兩段：

  純的一段   ─ 定位策略評估、唯一性判定、UNSTABLE 標記、扁平化、摘要統計、
               選取狀態判讀。吃 UiaNode／dict，吐 dict，不 import pywinauto，
               由 tests/test_locator.py 完整覆蓋。
  不純的一段 ─ 只負責把 pywinauto 的控制項物件翻譯成 UiaNode。薄到幾乎沒有
               判斷邏輯，因此目標機驗收時要看的東西很少。

pywinauto 一律**延遲匯入**（在函式內部才 import）。理由有二：純函式層必須
在沒有安裝 pywinauto 的機器上也能 import，否則 test_locator.py 會整份變成
collection error；而在目標機上缺套件時，使用者該看到的是「請先跑
0_檢查環境.bat」而不是一段 ImportError traceback。

走訪與操作全程只讀屬性、只用 UIA pattern，不動實體游標與鍵盤（由
tests/test_static_scan.py 逐字串守護）。
"""

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# ── 定位策略詞彙 ─────────────────────────────────────────────────────
#
# 前三個必須與 config.VALID_STRATEGIES 逐字相同：使用者是照著探測報告把
# strategy 抄進 config.json 的，兩邊用詞一漂移，他照抄的設定會被設定檔
# 驗證擋下來，而錯誤訊息會指向設定檔——完全指不到真正的原因。
STRATEGY_AUTO_ID = "auto_id"
STRATEGY_NAME = "name"
STRATEGY_INDEX = "index"

# UNSTABLE 刻意不是合法的 config 策略：它代表「這個控制項沒有可靠的定位
# 方式」，是一個要人來處理的結論，不是可以填進設定檔的選項。
STRATEGY_UNSTABLE = "UNSTABLE"

ALL_STRATEGIES: Tuple[str, ...] = (
    STRATEGY_AUTO_ID,
    STRATEGY_NAME,
    STRATEGY_INDEX,
    STRATEGY_UNSTABLE,
)

# 摘要區的 UNSTABLE 清單上限。決策依據是總數，逐條路徑只是樣本——
# 一棵樹可能有上千個無名的 ListItem，全列出來的摘要沒有人會讀。
MAX_LISTED_UNSTABLE = 50

PATH_SEPARATOR = " > "
UNKNOWN_TYPE = "?"


# ── 純資料結構 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class UiaNode:
    """
    一個控制項節點。欄位對應 spec「控制項樹匯出」列的最小集合。

    全部欄位都有預設值，因為「什麼都讀不到」正是最需要被測到的情況——
    自繪畫布與動態產生的清單項目常常只剩一個 control_type。
    """

    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    class_name: str = ""
    is_enabled: bool = False
    depth: int = 0
    sibling_index: int = 0
    children: Tuple["UiaNode", ...] = ()
    # 因深度上限而沒有再往下走。與「真的沒有子節點」必須分得開，
    # 否則使用者會以為樹就是這麼淺，然後在報告裡找不到他要的控制項。
    truncated: bool = False


@dataclass(frozen=True)
class Locator:
    """對單一控制項的定位建議。value 可直接抄進 config.json 的 controls。"""

    strategy: str
    value: str
    stable: bool
    reason: str


@dataclass(frozen=True)
class NodeReport:
    node: UiaNode
    path: str
    locator: Locator

    @property
    def child_count(self) -> int:
        return len(self.node.children)


@dataclass(frozen=True)
class SelectionProbe:
    """
    不純層對清單控制項讀取選取狀態的原始結果，未經判讀。

    supported 是「控制項曝光了選取狀態」，items 是「真的讀到的項目」。
    兩者刻意分開記錄——它們並不等價，而這正是判讀時的關鍵（見
    evaluate_selection）。
    """

    supported: bool = False
    items: Tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class SelectionReport:
    readable: bool
    items: Tuple[str, ...]
    hint: str


@dataclass(frozen=True)
class ProbeSummary:
    total_nodes: int
    max_depth: int
    truncated_count: int
    unstable_count: int
    strategy_counts: Mapping[str, int]
    selection_readable: bool
    selection_items: Tuple[str, ...]
    selection_hint: str
    unstable_paths: Tuple[str, ...]


@dataclass(frozen=True)
class ProbeReport:
    target: str
    nodes: Tuple[NodeReport, ...]
    selection: SelectionReport
    summary: ProbeSummary


# ── 純函式：序列化 ───────────────────────────────────────────────────


def node_to_mapping(node: UiaNode) -> Dict[str, Any]:
    """把節點連同子樹轉成可 json.dump 的巢狀 dict。"""
    return {
        "name": node.name,
        "automation_id": node.automation_id,
        "control_type": node.control_type,
        "class_name": node.class_name,
        "is_enabled": node.is_enabled,
        "depth": node.depth,
        "sibling_index": node.sibling_index,
        "truncated": node.truncated,
        "children": [node_to_mapping(c) for c in node.children],
    }


def node_from_mapping(data: Mapping[str, Any]) -> UiaNode:
    """
    從報告 JSON 讀回節點樹（D1 在開發機重新評估定位策略時用）。

    這裡刻意寬鬆——與 config.py 的「未知欄位一律拒絕」相反。設定檔拒絕未知
    欄位是為了攔住拼錯的參數；探測報告則是一份會隨版本長出新欄位的紀錄，
    讀舊報告時因為多了一個欄位就整份掛掉，只會讓人放棄用工具去讀它。
    """
    return UiaNode(
        name=str(data.get("name") or ""),
        automation_id=str(data.get("automation_id") or ""),
        control_type=str(data.get("control_type") or ""),
        class_name=str(data.get("class_name") or ""),
        is_enabled=bool(data.get("is_enabled", False)),
        depth=int(data.get("depth", 0)),
        sibling_index=int(data.get("sibling_index", 0)),
        children=tuple(node_from_mapping(c) for c in data.get("children") or ()),
        truncated=bool(data.get("truncated", False)),
    )


# ── 純函式：定位策略評估 ─────────────────────────────────────────────


def _count_matching(nodes: Sequence[UiaNode], getter: Callable[[UiaNode], str], value: str) -> int:
    return sum(1 for n in nodes if getter(n).strip() == value)


def _padding_note(raw: str) -> str:
    """
    值前後帶空白時的警語。

    記事本的狀態列就是這樣：Name 是 "  第 1 列，第 1 行"。空白在報告裡看不
    出來，使用者抄進 config.json 時會很自然地把它修掉，然後定位就找不到東西
    ——而錯誤訊息只會說「找不到控制項」，完全指不到真正的原因。
    """
    return "（注意：這個值前後帶有空白，抄進設定檔時必須一字不差）" if raw != raw.strip() else ""


def _sibling_pool(node: UiaNode, siblings: Sequence[UiaNode]) -> Tuple[UiaNode, ...]:
    """
    確保待評估的節點本身在同層集合裡。

    身分用 sibling_index 判斷而不是物件相等：同層兩個一模一樣的空白按鈕在
    值上完全相等，用 `node in siblings` 會把「同層有兩個」誤判成「同層只有
    我一個」——而那正好是唯一性判定最需要抓到的情況。
    """
    pool = tuple(siblings)
    if any(s.sibling_index == node.sibling_index for s in pool):
        return pool
    return pool + (node,)


def evaluate_locator(node: UiaNode, siblings: Sequence[UiaNode]) -> Locator:
    """
    純函式：這個控制項該怎麼定位。

    優先序 automation_id > name > control_type + 同層索引，理由是穩定性由高
    到低：AutomationId 由開發者指定，改版才會動；Name 通常是顯示文字，換語
    系或改文案就會變；位置則會隨版本與資料筆數改變。

    唯一性只在**同一個父容器**下判斷。拿整棵樹判斷的話，AccuMark 那種每個
    ListItem 都叫 "item" 的樹會讓幾乎所有節點被判成不可用；而實務上定位本來
    就是先找到容器再往下找。

    三者皆不可靠時回傳 UNSTABLE 而不是硬給一個位置。硬給位置的定位「今天能
    跑、明天沉默失效」，而失效的樣子是點到隔壁的控制項——那比直接標紅危險
    太多。

    value 一律輸出**原始字串**，只有「空不空」「唯不唯一」這兩個判斷才看去掉
    前後空白的版本。UIA 的屬性比對是逐字的，報告裡若擅自把空白修掉，抄進設定
    檔的值就定位不到真正的控制項。
    """
    pool = _sibling_pool(node, siblings)

    auto_id = node.automation_id.strip()
    if auto_id and _count_matching(pool, lambda n: n.automation_id, auto_id) == 1:
        return Locator(
            strategy=STRATEGY_AUTO_ID,
            value=node.automation_id,
            stable=True,
            reason=(
                "AutomationId 在同層唯一，是最不受改版與語系影響的定位方式"
                + _padding_note(node.automation_id)
            ),
        )

    name = node.name.strip()
    if name and _count_matching(pool, lambda n: n.name, name) == 1:
        return Locator(
            strategy=STRATEGY_NAME,
            value=node.name,
            stable=True,
            reason=(
                "沒有可用的 AutomationId；Name 在同層唯一。"
                "注意 Name 多半是顯示文字，改文案或換語系就會失效"
                + _padding_note(node.name)
            ),
        )

    control_type = node.control_type.strip()
    same_type = [n for n in pool if n.control_type.strip() == control_type]
    if control_type and len(same_type) == 1:
        position = sorted(s.sibling_index for s in same_type).index(node.sibling_index)
        return Locator(
            strategy=STRATEGY_INDEX,
            value=f"{control_type}#{position}",
            stable=True,
            reason=f"沒有可用的 AutomationId 與 Name，但同層只有這一個 {control_type}",
        )

    if not control_type:
        reason = "連 ControlType 都讀不到，沒有任何可用的定位依據"
    else:
        reason = (
            f"AutomationId 與 Name 皆不可用，同層還有 {len(same_type)} 個 "
            f"{control_type}，只能靠位置——位置會隨版本與資料筆數改變"
        )
    return Locator(strategy=STRATEGY_UNSTABLE, value="", stable=False, reason=reason)


# ── 純函式：樹的扁平化與標註 ─────────────────────────────────────────


def flatten(root: UiaNode) -> Tuple[UiaNode, ...]:
    """深度優先前序展開。葉節點（例如自繪畫布）自然回傳只有它自己的序列。"""
    collected: List[UiaNode] = [root]
    for child in root.children:
        collected.extend(flatten(child))
    return tuple(collected)


def _segment(node: UiaNode) -> str:
    return f"{node.control_type.strip() or UNKNOWN_TYPE}[{node.sibling_index}]"


def annotate(root: UiaNode) -> Tuple[NodeReport, ...]:
    """
    純函式：為整棵樹的每個節點算出路徑與定位建議。

    路徑存在的理由是「指名」：摘要區說有 37 個 UNSTABLE 節點，使用者得有辦法
    在報告裡把它們一個個找出來，否則那個數字只能拿來焦慮。
    """
    reports: List[NodeReport] = []

    def visit(node: UiaNode, siblings: Sequence[UiaNode], parent_path: str) -> None:
        segment = _segment(node)
        path = f"{parent_path}{PATH_SEPARATOR}{segment}" if parent_path else segment
        reports.append(
            NodeReport(node=node, path=path, locator=evaluate_locator(node, siblings))
        )
        for child in node.children:
            visit(child, node.children, path)

    visit(root, (root,), "")
    return tuple(reports)


# ── 純函式：選取狀態判讀 ─────────────────────────────────────────────


_FALLBACK_HINT = "請把 config.json 的 models 改成明確的 model 清單"


def evaluate_selection(probe: Optional[SelectionProbe]) -> SelectionReport:
    """
    純函式：這份清單到底能不能拿來當 `models: "SELECTED"` 的來源。

    關鍵判斷是「曝光 pattern 不等於讀得到」。控制項可能宣稱支援 Selection
    卻永遠回傳空集合（自繪清單、只在內部維護選取狀態的實作都會這樣）。把
    「宣稱支援」當成證據，使用者會設成 SELECTED 模式、框選了四個 model、
    然後跑出零個任務——而摘要只會說「沒有任何任務被執行」，他無從得知是這
    裡出的問題。

    因此 readable 只在**真的讀到項目**時才為真。代價是使用者若忘了先框選就
    探測，會拿到 false；所以那個情況給的是「請先框選再重跑」而不是「不可用」，
    兩者的差別在 hint 講清楚。
    """
    if probe is None:
        return SelectionReport(
            readable=False,
            items=(),
            hint=(
                "沒有指定 model 清單控制項，因此沒有探測選取狀態。"
                "請先在報告中找出清單控制項（ControlType 為 List／DataGrid／Table），"
                "再重跑一次探測"
            ),
        )

    if probe.error:
        return SelectionReport(
            readable=False,
            items=(),
            hint=f"讀取選取狀態時出錯：{probe.error}。SELECTED 模式不可用，{_FALLBACK_HINT}",
        )

    if not probe.supported:
        return SelectionReport(
            readable=False,
            items=(),
            hint=f"這個控制項未曝光選取狀態。SELECTED 模式不可用，{_FALLBACK_HINT}",
        )

    if not probe.items:
        return SelectionReport(
            readable=False,
            items=(),
            hint=(
                "控制項曝光了選取狀態，但目前讀到 0 個項目。"
                "請先在 AccuMark Explorer 框選幾個 model 再重跑探測；"
                f"若框選後仍是 0，代表 SELECTED 模式不可用，{_FALLBACK_HINT}"
            ),
        )

    return SelectionReport(
        readable=True,
        items=tuple(probe.items),
        hint=(
            f"讀到 {len(probe.items)} 個選取項目，"
            "請比對是否與你在 Explorer 框選的完全一致；一致才代表 SELECTED 模式可用"
        ),
    )


# ── 純函式：摘要與報告 ───────────────────────────────────────────────


def summarize(
    nodes: Sequence[NodeReport], selection: SelectionReport
) -> ProbeSummary:
    """純函式：把逐節點的評估壓成一份看得完的摘要。"""
    # 四個策略一律給值，缺鍵會逼報告閱讀端自己處理 KeyError。
    counts = {strategy: 0 for strategy in ALL_STRATEGIES}
    unstable_paths: List[str] = []

    for report in nodes:
        counts[report.locator.strategy] = counts.get(report.locator.strategy, 0) + 1
        if not report.locator.stable:
            unstable_paths.append(report.path)

    return ProbeSummary(
        total_nodes=len(nodes),
        max_depth=max((r.node.depth for r in nodes), default=0),
        truncated_count=sum(1 for r in nodes if r.node.truncated),
        unstable_count=len(unstable_paths),
        strategy_counts=MappingProxyType(counts),
        selection_readable=selection.readable,
        selection_items=selection.items,
        selection_hint=selection.hint,
        unstable_paths=tuple(unstable_paths[:MAX_LISTED_UNSTABLE]),
    )


def build_report(
    root: UiaNode,
    *,
    target: str = "",
    selection: Optional[SelectionProbe] = None,
) -> ProbeReport:
    """純函式：走訪結果 ＋ 選取狀態探測 → 完整報告。"""
    nodes = annotate(root)
    selection_report = evaluate_selection(selection)
    return ProbeReport(
        target=target,
        nodes=nodes,
        selection=selection_report,
        summary=summarize(nodes, selection_report),
    )


def report_to_dict(report: ProbeReport) -> Dict[str, Any]:
    """
    純函式：報告 → 可 json.dump 的 dict（B3 直接拿去寫檔）。

    節點輸出成扁平清單而不是巢狀樹：報告的用途是被人用記事本翻、被 grep 找，
    而每個節點都帶著完整路徑，扁平化不會丟失結構資訊。
    """
    return {
        "target": report.target,
        "summary": {
            "total_nodes": report.summary.total_nodes,
            "max_depth": report.summary.max_depth,
            "truncated_count": report.summary.truncated_count,
            "unstable_count": report.summary.unstable_count,
            "strategy_counts": dict(report.summary.strategy_counts),
            "selection_readable": report.summary.selection_readable,
            "selection_items": list(report.summary.selection_items),
            "selection_hint": report.summary.selection_hint,
            "unstable_paths": list(report.summary.unstable_paths),
        },
        "nodes": [
            {
                "path": r.path,
                "depth": r.node.depth,
                "sibling_index": r.node.sibling_index,
                "name": r.node.name,
                "automation_id": r.node.automation_id,
                "control_type": r.node.control_type,
                "class_name": r.node.class_name,
                "is_enabled": bool(r.node.is_enabled),
                "child_count": r.child_count,
                "truncated": r.node.truncated,
                "locator": {
                    "strategy": r.locator.strategy,
                    "value": r.locator.value,
                    "stable": r.locator.stable,
                    "reason": r.locator.reason,
                },
            }
            for r in report.nodes
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# 以下為不純的部分：唯一會呼叫 pywinauto 的地方
# ══════════════════════════════════════════════════════════════════════

BACKEND = "uia"

# 深度上限的用途不是省時間，是防失控：AccuMark 的樹裡可能有上萬個節點的清單，
# 而探測的目的是拿到定位資訊，不是完整鏡射整個 UI。走不完的部分會被標成
# truncated，使用者看得到，需要時再調高。
DEFAULT_MAX_DEPTH = 12

# 等待視窗出現的秒數。這不是「等匯出寫完」的等待（那由 stability.py 依設定檔
# 輪詢判定），只是找視窗時給 UIA 的一點餘裕。
DEFAULT_WAIT_SEC = 10.0


class UiaError(RuntimeError):
    """UI 自動化層的錯誤。訊息一律寫成使用者看得懂的下一步。"""


class PywinautoMissingError(UiaError):
    """pywinauto 匯入失敗。"""


class WindowNotFoundError(UiaError):
    """找不到目標視窗。"""


def _load_pywinauto():
    """
    延遲匯入。缺套件時給的是「下一步該做什麼」，不是 traceback——
    目標機上看報錯的人是使用者，不是開發者。
    """
    try:
        import pywinauto  # noqa: PLC0415 — 刻意延遲匯入，理由見模組 docstring
    except ImportError as exc:
        raise PywinautoMissingError(
            "找不到 pywinauto。請先雙擊 0_檢查環境.bat，"
            "它會告訴你線上與離線兩種安裝方式"
        ) from exc
    return pywinauto


def _read(getter: Callable[[], Any], default: Any) -> Any:
    """
    讀一個可能會爆的屬性。

    UIA 的屬性讀取是跨行程的 COM 呼叫：控制項在走訪途中被關掉、自繪畫布沒
    實作某個屬性、視窗正在重繪，都會直接丟例外。一個節點讀不到某個欄位，
    不該讓整棵樹的走訪失敗——探測報告拿回一半是沒有用的。
    """
    try:
        value = getter()
    except Exception:  # noqa: BLE001 — COM 的例外型別五花八門，一律當成讀不到
        return default
    return default if value is None else value


def snapshot(control, *, depth: int = 0, sibling_index: int = 0) -> UiaNode:
    """
    不純：把一個 pywinauto 控制項翻譯成 UiaNode（不含子節點）。

    is_enabled 讀不到時取 False。這個欄位在報告裡是給人看的判斷依據，
    「不確定」寫成「可用」會讓人把一個其實按不動的控制項填進設定檔。
    """
    info = _read(lambda: control.element_info, None)
    if info is None:
        info = control

    return UiaNode(
        name=str(_read(lambda: info.name, "")),
        automation_id=str(_read(lambda: info.automation_id, "")),
        control_type=str(_read(lambda: info.control_type, "")),
        class_name=str(_read(lambda: info.class_name, "")),
        is_enabled=bool(_read(lambda: info.enabled, False)),
        depth=depth,
        sibling_index=sibling_index,
    )


def _children(control) -> List[Any]:
    """
    取子控制項。取不到一律當成沒有子節點。

    對應 spec 的 Scenario「自繪畫布無法曝光」：PDS 的版型畫布在 UIA 下是一個
    沒有子節點的控制項，有些實作甚至會在列舉子節點時直接丟 COM 例外。報告
    要能照樣產出——那個「沒有內容」本身就是我們要帶回去的結論。
    """
    kids = _read(lambda: control.children(), None)
    if not kids:
        return []
    return list(kids)


def walk(
    control,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    depth: int = 0,
    sibling_index: int = 0,
) -> UiaNode:
    """
    不純：走訪整棵 UIA 樹，回傳純資料的 UiaNode 樹。

    這一層刻意不做任何判斷——策略評估、統計、選取狀態判讀全部交給上面的純
    函式。它唯一的職責是翻譯，所以目標機驗收時只要確認「樹的形狀對不對」。

    深度上限處理成「照樣去問有沒有子節點、但不往下走」，這樣 truncated 才是
    誠實的：把「到頂了」與「真的是葉節點」混為一談，會讓使用者以為 AccuMark
    的樹只有這麼深，然後找不到控制項也不知道要調參數。
    """
    node = snapshot(control, depth=depth, sibling_index=sibling_index)
    kids = _children(control)

    if depth >= max_depth:
        return replace(node, truncated=bool(kids))

    return replace(
        node,
        children=tuple(
            walk(child, max_depth=max_depth, depth=depth + 1, sibling_index=index)
            for index, child in enumerate(kids)
        ),
    )


def _no_pattern_error_types() -> tuple:
    """
    pywinauto 用來表示「這個控制項沒有實作該 pattern」的例外型別。

    抓不到型別時回傳空 tuple，效果是全部落到一般錯誤分支——比誤判成「沒有
    pattern」保守，也不會因為 pywinauto 改了模組路徑就整支探測掛掉。
    """
    try:
        from pywinauto.uia_defines import NoPatternInterfaceError  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ()
    return (NoPatternInterfaceError,)


def _describe_exception(exc: BaseException) -> str:
    """例外訊息常常是空的（NoPatternInterfaceError 就是），只留型別名比較好讀。"""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def read_selection(control) -> SelectionProbe:
    """
    不純：讀清單控制項目前選取了哪些項目。

    只讀不寫：走的是 UIA 的 Selection pattern，不碰實體滑鼠鍵盤，因此使用者
    在 Explorer 裡的選取狀態不會被這支探測改掉。

    「沒有 Selection pattern」與「有 pattern 但讀失敗」必須分開回報。前者是
    spec 的 Scenario「清單不支援讀取選取項」，結論明確——改用明確 model 清單；
    後者是偶發故障，值得重試一次再下結論。兩者都塞進 error 欄位的話，報告會
    對第一種情況印出一行沒有訊息的例外型別名，使用者看不出那其實是個確定的
    答案。

    任何例外都轉成欄位而不是往上拋：探測失敗本身就是要帶回去的資訊（它直接
    決定 `models: "SELECTED"` 能不能用），讓整支腳本掛掉反而什麼都不知道。
    """
    try:
        selected = control.get_selection()
    except _no_pattern_error_types():
        return SelectionProbe(supported=False)
    except Exception as exc:  # noqa: BLE001 — COM 的失敗型別五花八門
        return SelectionProbe(supported=False, error=_describe_exception(exc))

    return SelectionProbe(
        supported=True,
        items=tuple(_element_label(element) for element in selected or ()),
    )


def _element_label(element) -> str:
    """選取項的顯示名稱。Name 空的清單項目退回 rich_text，兩個都空就給空字串。"""
    name = str(_read(lambda: element.name, "")).strip()
    if name:
        return name
    return str(_read(lambda: element.rich_text, "")).strip()


def find_window(
    *,
    title_re: Optional[str] = None,
    class_name: Optional[str] = None,
    process: Optional[int] = None,
    path: Optional[str] = None,
    backend: str = BACKEND,
    timeout: float = DEFAULT_WAIT_SEC,
):
    """
    不純：找到要探測的頂層視窗，回傳 pywinauto 的控制項物件。

    找不到時丟 WindowNotFoundError 而不是回傳 None。對應 spec 的 Scenario
    「目標程序未啟動」：腳本必須以非零結束碼終止並說清楚原因，MUST NOT 產生
    一份空白或誤導性的報告檔——一份看起來正常但其實沒探到東西的報告，會讓
    整個期二依著錯誤的假設寫下去。
    """
    pywinauto = _load_pywinauto()

    if path is not None:
        try:
            app = pywinauto.Application(backend=backend).connect(
                path=path, timeout=timeout
            )
            return app.top_window().wrapper_object()
        except Exception as exc:  # noqa: BLE001 — pywinauto 的失敗型別不只一種
            raise WindowNotFoundError(
                f"連不到程式 {path}：{exc}。請先確認該程式正在執行且視窗沒有最小化"
            ) from exc

    criteria = {
        key: value
        for key, value in (
            ("title_re", title_re),
            ("class_name", class_name),
            ("process", process),
        )
        if value is not None
    }
    if not criteria:
        raise UiaError(
            "find_window 至少要給一個條件（title_re／class_name／process／path），"
            "否則會抓到不確定是哪一個的視窗"
        )

    try:
        spec = pywinauto.Desktop(backend=backend).window(**criteria)
        spec.wait("exists ready", timeout=timeout)
        return spec.wrapper_object()
    except Exception as exc:  # noqa: BLE001
        raise WindowNotFoundError(
            f"找不到符合 {criteria} 的視窗：{exc}。"
            "請確認目標程式正在執行、視窗沒有最小化，且條件沒有打錯"
        ) from exc


def window_label(control) -> str:
    """
    不純：實際抓到的視窗是誰。

    這個函式的存在是因為一個實測抓到的缺陷：探測報告原本記的是「搜尋條件」
    而不是「實際抓到的視窗」。開發機上沒有 AccuMark，`title_re="AccuMark.*"`
    卻探測成功——它匹配到了瀏覽器開著的一份標題以 AccuMark 開頭的文件，而
    報告上只顯示搜尋條件，使用者完全看不出探錯了對象。

    在目標機上，桌面只要有任何標題以 AccuMark 開頭的視窗，就會產出一份看
    起來正常、其實結構全錯的報告。
    """
    title = str(_read(lambda: control.window_text(), "")).strip()
    klass = str(_read(lambda: control.class_name(), "")).strip()
    if title and klass:
        return f"{title}　[{klass}]"
    return title or klass or "(無法取得視窗標題)"


def list_top_windows(backend: str = BACKEND) -> Tuple[str, ...]:
    """
    不純：列出目前畫面上有標題的頂層視窗，去重且保持原順序。

    只在「找不到目標視窗」時用得到。第一次探測時沒人知道 AccuMark 的視窗
    標題長什麼樣，列出候選讓使用者能直接指認，比雙方反覆猜快得多。

    任何失敗都回空 tuple 而不是拋例外：這是輔助資訊，它自己壞掉不該蓋掉
    「找不到視窗」這個真正的錯誤。
    """
    try:
        pywinauto = _load_pywinauto()
        titles = []
        for window in pywinauto.Desktop(backend=backend).windows():
            text = str(_read(lambda w=window: w.window_text(), "")).strip()
            if text:
                titles.append(text)
        return tuple(dict.fromkeys(titles))
    except Exception:  # noqa: BLE001
        return ()
