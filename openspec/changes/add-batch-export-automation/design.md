# Design: AccuMark V18 批次匯出自動化

- **change-id**: `add-batch-export-automation`
- **日期**: 2026-08-30
- **狀態**: ✅ 已核准（2026-08-30）

---

## 1. Overview

### 1.1 Purpose

本設計解決一個受外部限制夾擊的自動化問題：目標軟體（Gerber AccuMark V18）沒有可用的程式化介面，唯一入口是 GUI；使用者明確要求不得佔用實體滑鼠；而執行環境是一台**開發者接觸不到、且能否安裝軟體都未知**的電腦。

三個限制交叉之後，可行解的空間非常窄。本設計的核心主張是：**把「不確定的部分」與「確定的部分」在架構上徹底切開**——UI 互動是不確定的（要到目標機才知道長什麼樣），檔案處理與流程控制是確定的（現在就能寫完並完整測試）。切開之後，不確定的部分收斂成一份設定檔，確定的部分可以走完整的 TDD。

### 1.2 Scope

**包含**
- 期一：零安裝的 UI 控制項探測腳本
- 期二：批次匯出主腳本（4 model × 3 格式）
- 檔案歸檔、日誌、續跑、對話框防護
- 目標機操作說明文件

**排除**
- 同一 model 切換多種匯出設定（本案四份為四個獨立原檔，匯出動作相同）
- 修改 AccuMark 產出的檔案內容
- AAMA ↔ ASTM 格式互轉
- PDS 版型繪製畫布的自動化
- 圖形介面
- 代為推送 GitHub（全域規則禁止，由使用者自行執行）

### 1.3 Related Documents

- [`proposal.md`](proposal.md) — 需求背景、已排除的替代方案
- [`specs/ui-probe.md`](specs/ui-probe.md) — 探測能力規格
- [`specs/batch-export.md`](specs/batch-export.md) — 批次匯出規格
- [`specs/file-archival.md`](specs/file-archival.md) — 歸檔規格
- [`specs/operability.md`](specs/operability.md) — 可運維性與安全規格

---

## 2. Architecture

### 2.1 System Context

```
┌────────────────────────────────────────────────────────────────┐
│ 開發機（本機，Python 3.12）                                       │
│                                                                 │
│   pytest ──────▶ lib/ 純函式層                                   │
│   (RED-GREEN-REFACTOR 全覆蓋)   檔案穩定判定 / 歸檔路徑 /         │
│                                 續跑判定 / 設定驗證 / 白名單比對   │
└────────────────────────────────────────────────────────────────┘
                    │
                    │  ① 複製整個 scripts\ 資料夾（USB／網路磁碟）
                    ▼
┌────────────────────────────────────────────────────────────────┐
│ 目標機（跑 AccuMark V18，已有 Python）                             │
│                                                                 │
│  ┌── 期零 ────────────────────────────────────────────────┐    │
│  │  0_檢查環境.bat → python 版本 / pywinauto 可用？          │    │
│  │  失敗 → 依手冊離線安裝 wheel，或啟動 TD-1 退路            │    │
│  └────────────────────┬───────────────────────────────────┘    │
│                       ▼                                        │
│  ┌── 期一 ────────────────────────────────────────────────┐    │
│  │  probe_ui.py                                            │    │
│  │    走訪 UIA 樹 → probe-output\probe_<時間戳>.json        │    │
│  └────────────────────┬───────────────────────────────────┘    │
│                       │ ② 報告帶回開發機                        │
│                       │ ③ 開發機依報告填寫 config.json 的        │
│                       │    controls 區段，再送回目標機           │
│                       ▼                                        │
│  ┌── 期二 ────────────────────────────────────────────────┐    │
│  │  batch_export.py                                        │    │
│  │    ├─ 環境檢查（AccuMark 在跑？未鎖屏？目錄可寫？）        │    │
│  │    ├─ 讀 config.json + 續跑狀態                          │    │
│  │    ├─ 解析 model 清單（Explorer 選取項 或 明確清單）      │    │
│  │    └─ 任務迴圈：N model × 3 格式                         │    │
│  │         ├─ UI 層  ：選取 model → 觸發匯出               │    │
│  │         ├─ 偵測層 ：輪詢暫存夾直到檔案大小穩定            │    │
│  │         ├─ 守衛層 ：每輪檢查有無非白名單對話框            │    │
│  │         └─ 歸檔層 ：立即搬離 → model 資料夾（保留原檔名） │    │
│  └────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 Components

分層原則：**能寫成純函式的一律寫成純函式**。這不是潔癖，而是因為 UI 互動層在開發機上完全無法測試，若讓流程邏輯混在裡面，整個專案就會退化成「只能在目標機上試錯」——那是最昂貴的除錯方式。

| 檔案 | 職責 | 純度 | 測試方式 |
|---|---|---|---|
| `lib/config.py` | 讀取、驗證設定檔；缺漏欄位給明確錯誤 | 純 | pytest 全覆蓋 |
| `lib/stability.py` | 依取樣序列判定「檔案已寫完」 | 純（注入取樣函式） | pytest 全覆蓋 |
| `lib/archival.py` | 計算目的路徑、偵測衝突並改名 | 純 | pytest 全覆蓋 |
| `lib/runstate.py` | 續跑狀態讀寫、「該不該跳過」判定 | 純 | pytest 全覆蓋 |
| `lib/dialog_guard.py` | 白名單比對（純）＋ 前景視窗偵測（不純，隔離成單一函式） | 混合 | 純的部分 pytest 覆蓋 |
| `lib/reporting.py` | 結構化日誌與摘要輸出 | 純 | pytest 全覆蓋 |
| `lib/uia.py` | pywinauto 封裝：尋找控制項、Invoke、SetValue、Select、讀取選取項 | **不純** | 目標機驗收 |
| `probe_ui.py` | 期一探測進入點 | 不純 | 目標機驗收 |
| `batch_export.py` | 期二主流程編排 | 不純 | 以替身做整合測試 |
| `config.json` | 全部可變參數 | 資料 | schema 驗證 |

**不純的程式碼被壓縮到三個檔案，且其中兩個是薄薄的進入點。** 這是本設計最重要的結構性決定，也是 TD-1 換語言後代價可控的原因——分層沒有動，只有語法換了。

### 2.3 Component Interactions

單次任務（`model` × `format`）的完整流程：

```
batch_export.py
  │
  ├─ runstate.should_skip(model, format)? ──是──▶ 記錄 SKIPPED_ALREADY_DONE，下一個
  │                                       否
  ├─ 斷言暫存夾為空（不為空 → 中止整批）
  │
  ├─ uia.select_model(model)              ← 依 config.controls 定位
  ├─ uia.trigger_export(format)           ← 依 config.controls 定位
  │
  ├─ 輪詢迴圈（每 poll_interval_ms 一次，至多 timeout_sec）
  │    ├─ dialog_guard.check_foreground()
  │    │     └─ 非白名單視窗 → HALTED_UNKNOWN_DIALOG，中止整批
  │    ├─ 取樣暫存夾檔案清單與大小
  │    └─ stability.is_stable(取樣序列)? ──是──▶ 跳出
  │
  ├─ 逾時 → FAILED_TIMEOUT，清理暫存夾，下一個任務
  │
  ├─ archival.plan(產出檔案清單, model, 目的夾) → 目的路徑清單（純計算）
  │     └─ 保留原檔名；僅在目的地已存在同名檔時才附加區別後綴
  ├─ 實際搬移（失敗 → FAILED_MOVE，保留原檔，中止整批）
  │
  ├─ runstate.mark_success(model, format, 產出清單)
  └─ reporting.write(任務結果)
```

**關鍵不變式**：每次觸發匯出之前，暫存夾必為空。因此「暫存夾裡出現的任何東西」必然屬於當前任務——不需要比對檔名、不需要記錄前後差異、不會誤把上一個任務的殘留當成本次產出。這條不變式讓完成偵測與歸檔的邏輯都變得極簡且不易出錯。

---

## 3. Technical Decisions

### TD-1：技術棧採用 Python + pywinauto

> **本決策於 2026-08-30 修訂。** 初版選 PowerShell，前提是「目標機能否安裝 Python 未知」。使用者已確認目標機**已裝有 Python**，該前提消失，決策隨之翻轉。原方案降為退路（見 Consequences）。

**Context**
目標機已具備 Python。原本迫使我們選 PowerShell 的唯一理由——「不能有任何前置安裝需求」——已不成立。剩下的唯一未知數縮小為：`pywinauto` 這個 pip 套件能不能裝上去。

**Options Considered**

| 選項 | 優點 | 缺點 |
|---|---|---|
| A. PowerShell 5.1 + .NET UIAutomation | Windows 內建，完全零安裝 | 樣板碼冗長；UIA managed API 較慢；Pester 3.4 老舊；開發工時多 30–40% |
| **B. Python + pywinauto** | UIA 封裝成熟（`SelectionItem`／`Value`／`Invoke` 皆已包好）；檔案處理、JSON、日誌語法簡潔；pytest 遠優於 Pester 3.4；開發最快 | 需 `pip install pywinauto`（連帶 `comtypes`、`pywin32`） |
| C. Python + PyInstaller 打包 exe | 目標機零安裝 | 每次調整定位都要重新打包傳檔；使用者無法自行微調；除錯困難 |
| D. AutoHotkey v2 | 輕量 | 需安裝；UIA 支援需第三方函式庫；檔案處理與日誌能力弱 |

**Decision**
採用 **B**。並新增前置任務 **A0：目標機相依驗證**——交付一支 `0_檢查環境.bat`，在目標機執行後即可確認 Python 版本與 `pywinauto` 是否可用。

**Rationale**
前提改變後，B 在每一個維度都優於 A：`pywinauto` 把 UIA 的樣板碼（TreeWalker、Pattern 取得、逾時重試）全部包好，等價功能的程式碼量約為 PowerShell 的一半；pytest 支援 fixture 與參數化，讓 TD-3 的純函式層測試寫起來更精確；Python 的檔案與 JSON 處理不需要對付 `ConvertFrom-Json` 回傳 PSCustomObject 這類坑。

保留 A0 這個前置任務，是因為「有 Python」與「能裝 pywinauto」是兩件事——CAD 工作站可能沒有外網、或 pip 被 proxy 擋住。用一支五分鐘就能跑完的 `.bat` 消除這個不確定性，比事後發現再回頭改寫便宜太多。這與 TD-2「探測先行」是同一個思路：**用最小成本先把最大的未知數打掉。**

**離線安裝預案**（目標機無外網時）：在開發機執行

```
pip download pywinauto -d wheels/
```

把 `wheels/` 資料夾一併帶去目標機，改以 `pip install --no-index --find-links=wheels pywinauto` 安裝。此步驟寫入使用手冊。

**Consequences**
- ✅ 開發速度顯著提升，程式碼量約減半
- ✅ pytest 讓純函式層的測試表達力更強（fixture、參數化、`tmp_path`）
- ✅ `.bat` 包裝層（TD-7）不受影響，只是改為呼叫 `python` 而非 `powershell`
- ⚠️ 目標機需成功安裝 `pywinauto` — 由 **A0** 先行驗證，並備妥離線 wheel 方案
- ⚠️ **退路**：若 A0 顯示 `pywinauto` 無法安裝且離線方案也不可行，退回選項 A。此時 §2.2 的分層結構完全不變，只是把 `lib/*.py` 改寫為 `lib/*.ps1`——**TD-3 的架構決定讓這次退場的代價侷限在語法轉換，不涉及設計重做**
- ⚠️ 若 AccuMark 的控制項對 UIA 曝光不佳，`pywinauto` 可切換 `backend="win32"`（直送 Windows 訊息），這比 PowerShell 方案多一層退路

---

### TD-2：強制分兩期，探測先行

**Context**
在拿到 AccuMark 的 UI 結構之前，沒有任何人知道：model 清單是什麼控制項型別、匯出選單的階層、對話框有哪些欄位、控制項有沒有 `AutomationId`。這些全是主腳本每一行程式碼的輸入。

**Options Considered**
- **A. 先寫主腳本，到目標機再邊試邊改**：看似快，實際上是把全部風險集中到「開發者不在場的那台機器」上。每次修正都要一輪傳檔往返。
- **B. 先探測，帶回結構，再依結構寫主腳本**：多一次往返，但主腳本第一版就有正確的定位資訊。
- **C. 請使用者錄影／截圖描述 UI**：資訊量遠不足以支撐 UIA 定位（截圖看不到 `AutomationId`）。

**Decision**
採用 **B**，且「期一未完成不得開始期二」列為硬規則。

**Rationale**
GUI 自動化的失敗幾乎都源於「對 UI 結構的錯誤假設」。探測的成本是一支約 150 行的腳本加一次往返；不探測的成本是主腳本在目標機上反覆試錯，而每一輪試錯都需要使用者配合操作、傳檔、回報——那才是真正昂貴的部分。

**Consequences**
- ✅ 主腳本的控制項定位基於實測資料而非猜測
- ✅ 探測報告同時揭露「哪些控制項不可穩定定位」，可在寫程式碼前就調整策略
- ✅ 探測報告可長期保留，日後 AccuMark 升級時重跑一次即可比對差異
- ⚠️ 交付分兩批，總時程拉長一次往返
- ⚠️ 需使用者在目標機配合操作（開啟對話框後執行探測）

---

### TD-3：純函式層與 UI 層徹底分離

**Context**
本專案的 TDD 面臨一個現實問題：UI 自動化程式碼在開發機上**無法執行**（沒有 AccuMark），因此無法寫出會失敗、再變綠的測試。若不處理，Phase 3 的 RED-GREEN-REFACTOR 將淪為形式。

**Options Considered**
- **A. 不分層，全部視為不可測，跳過 TDD**：違反 spec-powers 硬規則，且流程控制的錯誤（續跑判定、衝突改名）將無從驗證。
- **B. 用 Mock 模擬整個 UIA 介面**：可讓測試跑起來，但 mock 的行為是開發者想像出來的，與真實 AccuMark 的落差正是風險所在——測試會綠得很有信心卻毫無意義。
- **C. 把所有不依賴 UI 的邏輯抽成純函式，對這一層做完整 TDD；UI 層薄化到只剩 API 呼叫，以目標機驗收清單覆蓋**。

**Decision**
採用 **C**。

**Rationale**
關鍵洞察是：**這個專案真正容易出錯的地方不在 UI 呼叫，而在流程判斷**。「檔案算不算寫完」「這個任務該不該跳過」「同名時該叫什麼」「這個對話框在不在白名單」——這些全都是純運算，全都可以測，而且全都是出錯會造成資料遺失的地方。UI 層反過來是最單純的：找到控制項、Invoke，成功或失敗一翻兩瞪眼。

B 選項被排除的理由值得記下：對一個你尚未觀察過的外部系統寫 mock，等於把假設寫死成測試，之後真實行為不符時，測試不但抓不到，還會給予錯誤的安全感。

**Consequences**
- ✅ 約 70% 的程式碼可在開發機完整 TDD，看得到真實的 RED
- ✅ UI 層薄，出錯時容易定位
- ✅ 目標機驗收清單明確（見 §9.3），不需開發者在場
- ⚠️ 需要額外的介面設計工夫，把時間、檔案系統等副作用注入純函式
- ⚠️ UI 層的正確性完全依賴目標機實測，開發機的綠燈**不代表**整體可用——此事實須在交付說明中明講，避免誤判完成度

---

### TD-4：以「檔案大小連續穩定」判定匯出完成

**Context**
腳本必須知道一次匯出何時真正結束，才能安全地搬檔並進入下一個任務。AccuMark 的匯出耗時隨裁片數量變動，且沒有可靠的程式化完成訊號。

**Options Considered**

| 選項 | 問題 |
|---|---|
| A. 固定 `Start-Sleep -Seconds N` | N 太小 → 搬到寫到一半的檔案（**靜默資料損毀**，最嚴重）；N 太大 → 12 次任務累積大量空等 |
| B. 偵測 UI 上的進度條／完成提示消失 | 依賴 UI 結構，是本專案最不穩定的部分；且不同格式的提示可能不同 |
| C. 輪詢暫存夾，檔案出現且大小連續 N 次取樣不變即判定完成 | 需選定取樣次數與間隔；理論上存在「寫入暫停恰好跨越取樣窗」的誤判 |
| D. 嘗試以獨佔模式開啟檔案，成功即表示無人寫入 | 判定最準確，但 PowerShell 中對某些檔案類型行為不一致，且可能干擾寫入端 |

**Decision**
採用 **C**，取樣參數外置於設定檔（預設：間隔 500ms、連續 3 次穩定、逾時 300 秒）。C 的判定結果再以 D 作為**選用的二次確認**（設定檔開關，預設關閉）。

**Rationale**
C 是唯一同時滿足「不依賴 UI 結構」與「不依賴猜測的等待時間」的方案。它直接觀測我們真正關心的東西——檔案本身——而不是任何代理指標。

C 的誤判風險（寫入暫停跨越取樣窗）以參數化緩解：預設 1.5 秒的穩定窗對本機磁碟寫入而言相當寬鬆，而若目標機實測發現大型 model 有長暫停，使用者可自行調高次數，無需改程式碼。保留 D 作為選用開關，是因為它在理論上更精確，但需目標機實測確認不會干擾 AccuMark 寫檔——不確定的東西不設為預設。

**Consequences**
- ✅ 大型 model 不會誤判失敗，小型 model 不會空等
- ✅ 完全不依賴 UI 結構，AccuMark 升級也不受影響
- ✅ 「逾時未出現檔案」自然成為匯出失敗的可靠訊號
- ⚠️ 每次任務固定多耗 1.5 秒確認時間（12 次共 18 秒，可接受）
- ⚠️ 若 AccuMark 先建立零位元組佔位檔再寫入，取樣邏輯須把「大小為 0」視為未穩定——已列入實作注意事項

---

### TD-5：對話框採白名單制，未知一律停機

**Context**
自動化過程中可能彈出各種對話框：檔案覆蓋確認、授權警告、model 損毀提示。若腳本盲目送出 Enter，有機率誤觸「是，覆蓋」而造成**不可回復的檔案遺失**。

**Options Considered**
- **A. 忽略所有對話框**：對話框是模態的，會直接卡死流程。
- **B. 黑名單：對已知危險的對話框停機，其餘按 Enter 通過**：無法列舉未知的危險項，安全模型是反的。
- **C. 白名單：只處理明確定義的對話框，其餘一律停機並記錄**。

**Decision**
採用 **C**。白名單置於設定檔，可由使用者擴充。

**Rationale**
在「誤判成本不對稱」的場合，安全預設必須偏向不作為。跑錯而停機的代價是使用者花五分鐘看日誌、把新對話框加進白名單；盲按 Enter 而覆蓋掉版型檔的代價可能是重做一整天的工作，而且**當下不會有人發現**。

停機時必須記錄對話框的標題、內文與所有按鈕文字——這正是使用者擴充白名單所需的全部資訊，讓「遇到未知情況」自然演化成「白名單長大一點」，而非需要開發者介入。

**Consequences**
- ✅ 不存在腳本誤覆蓋檔案的路徑
- ✅ 白名單隨使用而完備，不需改程式碼
- ⚠️ 首次在目標機執行時，可能因未收錄的對話框而中途停機數次
- ⚠️ 需在每輪輪詢中檢查前景視窗，略增開銷（可忽略）

---

### TD-6：匯出完成後立即搬離暫存區

**Context**
12 次匯出的產出需分類到 4 個 model 資料夾。搬移時機有兩種選擇。

**Options Considered**
- **A. 全部匯出完再一次分類**：暫存夾同時存在 12 個檔案，需靠檔名反推歸屬。若 AAMA 與 ASTM 輸出同名（極可能），第二次匯出會**直接覆蓋第一次**，且 AccuMark 未必會詢問。
- **B. 每次匯出完成後立即搬離**：暫存夾恆為空或恰含一個任務的產出。

**Decision**
採用 **B**。

**Rationale**
B 建立了 §2.3 所述的核心不變式——「觸發匯出前暫存夾必為空」。這一條同時解決三個問題：檔名衝突不可能發生；產出歸屬不需推論；完成偵測只需檢查「有沒有東西出現」而非「有沒有*新*東西出現」。

A 的致命處在於失敗是靜默的：使用者會拿到 11 個檔案卻以為有 12 個，且缺的那個沒有任何錯誤訊息。

**Consequences**
- ✅ 檔名衝突在架構上不可能發生
- ✅ 完成偵測與歸檔邏輯大幅簡化
- ✅ 中途失敗時，已完成的產出已在安全位置
- ⚠️ 啟動時若暫存夾非空，代表上次執行異常中斷 → 依規格停止並要求使用者確認，**不自動刪除**

---

### TD-7：`.bat` 薄包裝層，內容限用 ASCII

**Context**
使用者要求「全部包成 `.bat` 純雙擊」，不願開終端機或輸入命令。而雙擊 `.bat` 是一個比表面上麻煩的執行情境：工作目錄未必等於檔案所在目錄、執行完視窗會立刻關閉、`.bat` 的編碼受主控台代碼頁擺布。

**Options Considered**

| 選項 | 評估 |
|---|---|
| A. 單一 `.bat` 帶文字選單，使用者輸入數字選功能 | 仍需鍵盤輸入，不符「純雙擊」；且輸入驗證要自己寫 |
| B. 每個情境一個 `.bat`，檔名即說明，內容為薄包裝 | 看檔名就知道做什麼，零輸入 |
| C. 把腳本內嵌進 `.bat`（polyglot 檔） | 單檔交付，但可讀性極差、無法用編輯器正常編輯腳本部分、除錯困難 |
| D. 做成 `.lnk` 捷徑 | 捷徑的目標路徑是絕對路徑，複製到別台機器就失效 |

**Decision**
採用 **B**。五個 `.bat`，檔名用中文編號、內容純 ASCII：

```
0_檢查環境.bat              → check_env.py
1_執行探測.bat              → probe_ui.py
2_執行探測_對話框.bat        → probe_ui.py --mode dialog
3_執行批次匯出.bat           → batch_export.py
4_強制全部重跑.bat           → batch_export.py --force
```

每個 `.bat` 的骨架固定：

```bat
@echo off
setlocal
set "PY=py -3"
where /q py || set "PY=python"
%PY% -B "%~dp0probe_ui.py" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
```

**Rationale**
五個技術細節決定了這個骨架的每一行，全部是實際會炸的雷：

1. **`%~dp0`** — 雙擊時 cmd 的工作目錄未必是 `.bat` 所在位置（從捷徑或搜尋結果啟動時尤其如此）。`%~dp0` 展開為 `.bat` 自身所在目錄且**結尾帶反斜線**，加上引號即可容納含空白與中文的路徑。
2. **`pause`** — 沒有它，成功或失敗都是一閃即逝。使用者連錯誤訊息都來不及看，這會讓所有除錯溝通變成「畫面上好像有紅字」。
3. **`py -3` 優先、退回 `python`** — 目標機的 `python` 未必在 `PATH` 上（尤其當 Python 是隨其他軟體附帶安裝時），而 Windows 的 `py` launcher 位於 `System32`，可用性較高。先試 `py`，`where /q` 失敗才退回 `python`，兩者皆無時錯誤訊息會停留在畫面上（因為有 `pause`）。
4. **`-B`** — 不產生 `__pycache__`。交付資料夾可能位於 USB 或唯讀網路磁碟，且會被反覆複製；讓它保持乾淨可避免「複製回來的版本帶著別台機器的 bytecode」這類難以察覺的問題。
5. **內容純 ASCII** — `.bat` 由 cmd.exe 以主控台代碼頁（台灣通常 cp950）解讀。內含中文又存成 UTF-8 時，指令本身會變成亂碼而執行失敗。把所有中文推到 Python 層可徹底避開。**檔名可以是中文**——檔名由檔案系統以 UTF-16 處理，與 `.bat` 內容編碼無關。

C 選項的 polyglot 手法雖然常見，但它讓 `.py` 無法獨立編輯與測試，直接摧毀 TD-3 建立的「純函式層可完整 TDD」結構，代價遠大於少一個檔案的好處。

**Consequences**
- ✅ 使用者全程只需雙擊，零命令列
- ✅ `.py` 仍可獨立執行與測試，TD-3 的分層不受影響
- ✅ 結束碼經 `ERRORLEVEL` 正確傳遞，日後若要接排程器可直接使用
- ⚠️ 交付檔案數增加五個
- ⚠️ 「`.bat` 不得含非 ASCII 字元」成為一條需要被測試守護的規則——列入靜態掃描測試（見 §9.1）
- ⚠️ Python 主控台輸出中文時需明確處理編碼（`sys.stdout.reconfigure(encoding='utf-8')` 或設 `PYTHONIOENCODING`），否則在 cp950 主控台會拋 `UnicodeEncodeError`——**這是換用 Python 後新增的雷，列入 A1 的成功條件**
- ⚠️ `pause` 使腳本無法無人值守地被排程器呼叫；若日後有此需求，需另加 `--no-pause` 版本

---

### TD-8：歸檔保留 AccuMark 原始檔名，僅在衝突時改名

> **本決策於 2026-08-30 新增。** 初版規格要求「一律附加 `_ZIP`／`_AAMA`／`_ASTM` 後綴」。使用者指出檔名依版片編號產生、實務上不會衝突，且希望沿用原檔名，故改為條件式。

**Context**
AccuMark 匯出的檔名依版片編號產生。初版採取防禦姿態，對每個檔案一律附加格式後綴，以杜絕「AAMA 與 ASTM 同名互相覆蓋」的可能。使用者回報實際上不會衝突，並希望輸出沿用原檔名。

**Options Considered**
- **A. 一律加後綴**（初版）：絕對安全，但改變了工廠端與 Illustrator 端看到的檔名，且在不會衝突的情況下是多餘的雜訊。
- **B. 一律不加，同名就覆蓋**：完全信任「不會衝突」的判斷。一旦判斷有誤，失敗是**靜默的**——你會拿到少一個檔案卻毫無錯誤訊息。
- **C. 保留原檔名；僅在目的地已存在同名檔時才附加區別後綴，並記錄 WARN。**

**Decision**
採用 **C**。`archival.plan()` 預設輸出原檔名，偵測到目的地已有同名檔時才附加 `_AAMA`／`_ASTM`／`_2` 等區別字尾。

**Rationale**
使用者對自己資料的判斷應予採信——他每天在看這些檔名，比任何預防性設計更清楚實情。但「採信」與「拿掉安全網」是兩回事：C 讓正常情況完全照使用者期望（原檔名、零雜訊），只在判斷落空的那個瞬間才介入，而且是以「保留兩個檔案 + 記一筆 WARN」的方式介入，而非覆蓋。

這是本設計反覆出現的同一個原則：**在誤判成本不對稱的地方，讓安全網只在異常時現身，不要讓它干擾正常流程。**（同 TD-5 的白名單、TD-6 的暫存夾非空檢查。）

**Consequences**
- ✅ 正常情況下輸出檔名與手動匯出完全一致，工廠端與 Illustrator 端無感
- ✅ 判斷落空時不會靜默遺失檔案，且日誌明確指出發生了什麼
- ✅ `addFormatSuffix` 保留為設定檔開關，想強制加後綴仍可開啟
- ⚠️ 「不會衝突」若其實會衝突，使用者會在日誌看到 WARN 並拿到 `_AAMA` 字尾的檔案——需在使用手冊說明此行為，避免誤以為腳本亂改名

---

## 4. Data Design

### 4.1 `config.json`

```jsonc
{
  // "SELECTED" = 處理 AccuMark Explorer 中目前選取的項目（預設，免維護）
  // 也可改成明確清單：["A-1234", "A-1235"]
  "models": "SELECTED",
  "formats": ["ZIP", "AAMA", "ASTM"],

  "paths": {
    "temp_dir":    "%USERPROFILE%\\Desktop\\_accumark_temp",
    "output_root": "%USERPROFILE%\\Desktop\\AccuMark匯出"
  },

  "detection": {
    "poll_interval_ms":      500,
    "stable_samples":        3,
    "timeout_sec":           300,
    "verify_exclusive_lock": false     // TD-4 的選用二次確認
  },

  "archival": {
    // TD-8：預設保留 AccuMark 原始檔名，只在目的地已有同名檔時才附加區別字尾
    "add_format_suffix":  false,
    "output_dir_pattern": "{root}_{yymmdd}_{HHMM}"
  },

  "dialog_whitelist": [
    { "title_like": "*已存在*", "action": "Cancel",
      "result_status": "FAILED_TARGET_EXISTS" }
  ],

  "controls": {
    // ⚠️ 期一探測完成後填入。期一之前保持空白。
    "model_list":       { "strategy": "auto_id", "value": "" },
    "export_zip":       { "strategy": "name",    "value": "" },
    "export_aama":      { "strategy": "name",    "value": "" },
    "export_astm":      { "strategy": "name",    "value": "" },
    "dialog_path_box":  { "strategy": "auto_id", "value": "" },
    "dialog_ok_button": { "strategy": "name",    "value": "" }
  }
}
```

**`models: "SELECTED"` 的設計理由**：使用者希望不必維護 model 清單。若期一探測確認 Explorer 的清單控制項支援讀取選取狀態，使用者只要在 Explorer 框選要處理的 model 再雙擊 `.bat` 即可——設定檔完全不用碰，處理幾個都行。若探測顯示讀不到選取狀態，退回明確清單模式（設定格式已預留）。

### 4.2 `state.json`（續跑狀態）

```jsonc
{
  "runId": "260830_1430",
  "tasks": [
    {
      "model": "<model名1>", "format": "AAMA", "status": "SUCCESS",
      "startedAt": "2026-08-30T14:30:12", "finishedAt": "2026-08-30T14:30:47",
      "outputs": ["C:\\...\\<model名1>\\<model名1>_AAMA.dxf"]
    }
  ]
}
```

續跑判定：`status == "SUCCESS"` **且** `outputs` 中每個路徑皆存在 → 跳過。狀態檔說成功但檔案不見了，一律重跑（規格明訂）。

### 4.3 任務狀態列舉

| 狀態 | 意義 | 是否中止整批 |
|---|---|---|
| `SUCCESS` | 匯出並歸檔完成 | — |
| `SKIPPED_ALREADY_DONE` | 續跑時跳過 | 否 |
| `SKIPPED_NOT_FOUND` | model 在 Explorer 中不存在 | 否 |
| `FAILED_TIMEOUT` | 逾時未產生檔案 | 否 |
| `FAILED_TARGET_EXISTS` | 白名單對話框判定為覆蓋風險 | 否 |
| `FAILED_MOVE` | 歸檔搬移失敗（磁碟／權限） | **是** |
| `HALTED_UNKNOWN_DIALOG` | 遇到白名單外的視窗 | **是** |

---

## 6. Implementation Approach

### 6.1 Technology Stack

| 項目 | 選用 | 版本 | 備註 |
|---|---|---|---|
| 執行環境 | Python | 3.8+ | 目標機已具備（使用者確認）；開發機為 3.12.10 |
| UI 自動化 | `pywinauto` | 0.6.x | 需 `pip install`；由 **A0** 驗證，備離線 wheel 方案 |
| 測試框架 | `pytest` | 7.x+ | 僅開發機需要，目標機不跑測試 |
| 啟動層 | `.bat` | — | 內容純 ASCII（TD-7） |
| 設定格式 | JSON | — | 標準庫 `json` |
| 目標機額外相依 | 僅 `pywinauto`（連帶 `comtypes`、`pywin32`） | — | 不使用其他第三方套件 |

### 6.2 Code Organization

```
accumark-batch-export\
├── scripts\
│   ├── 0_檢查環境.bat             ← 使用者只碰這五個
│   ├── 1_執行探測.bat
│   ├── 2_執行探測_對話框.bat
│   ├── 3_執行批次匯出.bat
│   ├── 4_強制全部重跑.bat
│   ├── check_env.py              # 期零：Python / pywinauto 驗證
│   ├── probe_ui.py               # 期一進入點
│   ├── batch_export.py           # 期二進入點
│   ├── config.json               # 設定（探測後補齊 controls 區段）
│   └── lib\
│       ├── __init__.py
│       ├── config.py
│       ├── stability.py
│       ├── archival.py
│       ├── runstate.py
│       ├── dialog_guard.py
│       ├── reporting.py
│       └── uia.py
├── tests\
│   ├── test_config.py
│   ├── test_stability.py
│   ├── test_archival.py
│   ├── test_runstate.py
│   ├── test_dialog_guard.py
│   ├── test_reporting.py
│   ├── test_bat_wrapper.py       # .bat 靜態掃描
│   └── test_static_scan.py       # 禁用 API 掃描
├── wheels\                       # 離線安裝用（選用，A0 失敗時才需要）
├── docs\
│   └── 使用手冊.md                # 完整操作手冊（中文）
└── openspec\changes\add-batch-export-automation\
```

---

## 7. Security

本專案不處理帳密、不連網、不對外傳輸任何資料。安全考量集中於**資料完整性**：

| 風險 | 緩解 |
|---|---|
| 誤覆蓋既有檔案 | TD-5 白名單制；歸檔層「絕不覆蓋」規則；衝突時附加序號 |
| 搬移到一半的檔案 | TD-4 穩定判定；TD-6 立即搬離不變式 |
| 誤刪暫存夾殘留檔 | 啟動時暫存夾非空 → 停止並要求使用者確認，不自動清除 |
| 修改 AccuMark 產出內容 | 全程只做 move／rename，規格明訂位元組不變 |

`pywinauto` 為公開的開源套件（MIT 授權）；若目標機政策禁止對外 `pip install`，改用離線 wheel 方案（見 TD-1），此時無任何網路連線行為。腳本本身不建立網路連線、不讀取瀏覽器或憑證儲存區。此點需在使用手冊說明，供 IT 查核。

---

## 9. Testing Strategy

### 9.1 Scenario → 測試方法對照

| Scenario 出處 | 測試方法 | 可在開發機驗證 |
|---|---|---|
| `ui-probe` 全部（9 個） | 目標機驗收清單；樹走訪邏輯以本機記事本代測 | ❌ |
| `batch-export` 完成偵測（4 個） | pytest，注入取樣序列 | ✅ |
| `batch-export` 逐一匯出（3 個） | 目標機驗收清單 | ❌ |
| `batch-export` 不佔用滑鼠（2 個） | pytest 靜態掃描：斷言 `lib/` 與進入點中不含禁用 API 字串 | ✅ |
| `batch-export` 固定輸出路徑（4 個） | 路徑比對邏輯 pytest；實際設值目標機驗收 | 部分 |
| `file-archival` 全部（8 個） | pytest + `tmp_path` fixture 建立假檔案 | ✅ |
| `operability` 白名單比對（3 個） | pytest，參數化餵入標題／按鈕文字 | ✅ |
| `operability` 續跑判定（3 個） | pytest，構造 `state.json` | ✅ |
| `operability` 日誌（2 個） | pytest，檢查輸出格式與結束碼 | ✅ |
| `operability` 環境檢查（3 個） | 部分 pytest（目錄可寫）、部分目標機（鎖屏） | 部分 |
| `operability` 可攜（2 個） | 含中文與空白的路徑，pytest 覆蓋 | ✅ |
| `operability` 雙擊啟動（5 個） | pytest 靜態掃描 `.bat` 內容（`%~dp0`、`pause`、`py -3` 退回、`-B`、`exit /b`）；實際雙擊行為目標機驗收 | 部分 |
| `operability` `.bat` 限 ASCII（2 個） | pytest 逐位元組掃描，斷言所有 `.bat` 無 `> 0x7F` 位元組 | ✅ |

**覆蓋率**：32 個 Scenario 中 22 個可在開發機以 pytest 完整驗證（69%），10 個需目標機驗收。

「不佔用滑鼠」以**靜態掃描**驗證是刻意的設計：與其在執行期取樣游標座標（易受使用者實際移動滑鼠干擾而偽陽性），不如直接斷言原始碼中不存在 `SetCursorPos`、`mouse_event`、`SendInput`、`click_input`、`type_keys`、`send_keys`、`pyautogui` 這些字串。**這一點在換用 pywinauto 後更重要**——pywinauto 同時提供 `click_input()`（實體滑鼠）與 `click()`（UIA Invoke）兩組 API，名稱只差三個字，誤用不會報錯只會默默搶走滑鼠。靜態掃描是唯一能穩定攔住這件事的手段。

### 9.2 突變檢查對照（Phase 3 用）

每個純函式測試須通過對應突變：

| 測試對象 | 突變方式 | 預期 |
|---|---|---|
| `stability.is_stable` | 把「連續 N 次相同」改成「任一次相同」 | 測試必須失敗 |
| `stability.is_stable` | 移除「大小為 0 視為未穩定」判斷 | 測試必須失敗 |
| `archival.plan` | 移除「目的地已存在時附加區別字尾」 | 衝突測試必須失敗 |
| `archival.plan` | 把衝突改名改成直接覆蓋 | 測試必須失敗 |
| `runstate.should_skip` | 移除「outputs 檔案存在」檢查 | 測試必須失敗 |
| `dialog_guard.match_whitelist` | 把預設回傳由「不在白名單」改成「在白名單」 | 測試必須失敗 |
| `config.validate` | 移除必填欄位檢查 | 測試必須失敗 |
| 靜態掃描 | 在任一 `.py` 插入 `click_input(` | 測試必須失敗 |

### 9.3 目標機驗收清單（無法自動化的部分）

交付後由使用者在目標機依序執行並回報：

1. `0_檢查環境.bat` 通過：Python 版本足夠、`pywinauto` 可匯入
2. `1_執行探測.bat` 產出報告，且報告中能找到 model 清單控制項
3. 報告顯示清單控制項**可讀取選取狀態**（決定 `models: "SELECTED"` 能否使用）
4. 環境檢查全數通過（AccuMark 未開時正確報錯）
5. 單一 model 單一格式的最小驗證（`--only <model> --format AAMA`）
6. 完整批次跑完，輸出資料夾結構與檔案數正確
7. 執行期間手動操作其他程式，確認滑鼠與鍵盤未被干擾
8. 隨機中斷後重跑，確認已完成任務被跳過
9. 抽驗一個 DXF 以 Illustrator 開啟、一個 ZIP 以 AccuMark 匯入，確認與手動匯出結果一致

---

## 12. Open Questions

### 已解決

- [x] **model 的命名長什麼樣？** → 使用者確認命名依版片編號產生、不會衝突，且應**沿用原檔名**。因此設計不再要求命名範例：`models` 預設為 `"SELECTED"`（讀 Explorer 選取項），歸檔保留原檔名（TD-8）。
- [x] **AAMA 與 ASTM 檔名是否相同？** → 使用者判斷不會衝突。採信之，但保留條件式安全網（TD-8）：僅在目的地真的已有同名檔時才附加區別字尾並記 WARN。
- [x] **輸出根目錄放哪？** → 固定桌面。`output_root` 仍保留為設定項（成本為零），預設 `%USERPROFILE%\Desktop\AccuMark匯出`。
- [x] **目標機能否安裝 Python？** → 已具備。TD-1 因此翻轉為 Python + pywinauto。

### 期零（A0）可解決

- [ ] `pywinauto` 能否在目標機安裝？（否 → 走離線 wheel，仍否 → 啟用 TD-1 退路回 PowerShell）
- [ ] 目標機的 Python 版本與呼叫方式（`py` 或 `python`）

### 期一探測可解決

- [ ] AccuMark Explorer 的 model 清單是什麼控制項型別？
- [ ] **清單能否讀取「目前選取的項目」？**（決定 `models: "SELECTED"` 可否使用，否則退回明確清單）
- [ ] 三種匯出各自的選單／按鈕路徑為何？是主選單、右鍵選單、還是工具列？
- [ ] 匯出對話框的路徑欄位能否以 `ValuePattern` 設值？
- [ ] 匯出對話框會不會記住上次的輸出路徑？
- [ ] 關鍵控制項在 `backend="uia"` 下曝光是否足夠（若否 → 切 `backend="win32"`）

### 需目標機實測

- [ ] AccuMark 匯出時是否先建立零位元組佔位檔？（影響 TD-4 的取樣邏輯）
- [ ] 一次匯出是否產生 `.dxf` 以外的附帶檔案（如 `.rul`）？（歸檔須全數搬移，邏輯已涵蓋，此處僅確認實情）

---

## 13. Change Log

| 日期 | 變更 |
|---|---|
| 2026-08-30 | 初版。Phase 1 確認四份為四個獨立 model、匯出動作相同；Phase 0 確認無可用官方 API |
| 2026-08-30 | 設計核准。依使用者要求新增「全部包成 `.bat` 純雙擊」→ 新增 TD-7、`operability` 兩條 Requirement（7 個 Scenario）。Scenario 總數 25 → 32 |
| 2026-08-30 | **TD-1 翻轉**：使用者確認目標機已有 Python → 技術棧由 PowerShell 改為 Python + pywinauto，PowerShell 降為退路。新增期零任務 A0（相依驗證）與離線 wheel 預案。連帶更新 §2 元件表與流程、§4 設定 schema、§6 技術棧與目錄、§9 測試策略（Pester → pytest）、TD-7 的 `.bat` 骨架。<br>**新增 TD-8**：歸檔改為保留原檔名、僅衝突時改名。<br>**新增 `models: "SELECTED"`**：免維護 model 清單，改讀 Explorer 選取項。<br>交付物新增「完整使用手冊」。 |
