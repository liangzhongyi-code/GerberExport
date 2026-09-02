# Tasks: AccuMark V18 批次匯出自動化

- **change-id**: `add-batch-export-automation`
- **分支**: `feature/add-batch-export-automation`
- **技術棧**: Python 3.8+ / pywinauto / pytest（TD-1，2026-08-30 修訂）
- **狀態**: ✅ 已核准（2026-08-30）· Phase 3 進行中

## 進度

| 任務 | 狀態 | 測試 | 突變檢查 |
|---|---|---|---|
| `A0` 環境檢查與 `.bat` 包裝層 | ✅ | 76 | 8/8 全紅 |
| `A1` 設定模組 | ✅ | 130 | 9/9 全紅（1 項第一輪存活，已補測試） |
| `A2` 結果分類與日誌 | ✅ | 160 | 9/9 全紅 |
| `C1` 完成偵測（TD-4） | ✅ | 186 | 10/10 全紅（2 項第一輪存活，已補測試） |
| `C2` 歸檔（TD-8） | ✅ | 215 | 12/12 全紅（1 項第一輪存活，發現設計缺陷並修正） |
| `C3` 續跑判定 | ✅ | 246 | 11/11 全紅（1 項第一輪存活，已補測試） |
| `C4` 對話框守衛（TD-5） | ✅ | 268 | 12/12 全紅（2 項第一輪存活，發現跨平台問題並修正） |
| `C5` 靜態掃描 | ✅ | 335 | 4 條掃描＋偵測器自檢 |
| `B1`+`B2` UIA 走訪與定位策略 | ✅ | 401 | 4/4 全紅 |
| `B3` `probe_ui.py` 進入點 | ✅ | 433 | 9/9 全紅（1 項第一輪存活） |
| `B4` 使用手冊（期零＋期一） | ✅ | — | 逐字核對實作 |
| `D1`–`D5` 期二整合 | ⏸ 等 ⛸ 交付點 1 | | |

**階段 A、B、C 全數完成。433 項測試通過。⛸ 交付點 1 已打包。**

## 過程中抓到的真實缺陷

突變檢查累計抓到 **9 個測試盲點**；實測另外抓到 **3 個真正的缺陷**：

| 來源 | 缺陷 | 影響 |
|---|---|---|
| 突變檢查 | 歸檔逐個「檢查→搬移」，會搬到一半才發現缺檔 | 暫存夾與目的地同時處在半完成狀態 |
| 突變檢查 | `fnmatch` 的大小寫行為隨平台改變 | 開發與執行都在 Windows，測試根本驗不出來 |
| 記事本實測（B1/B2） | 控制項 Name 帶前後空白 | 抄進 config.json 會定位不到，錯誤訊息只說「找不到控制項」 |
| 記事本實測（B1/B2） | `NoPatternInterfaceError` 訊息為空 | 印出「讀取選取狀態時出錯：NoPatternInterfaceError:。」 |
| 實測（B3） | **報告記的是搜尋條件而非實際抓到的視窗** | `--title "AccuMark.*"` 匹配到瀏覽器開著的同名文件仍回報成功，期二會建在錯誤結構上 |
| 手冊逐字核對（B4） | 離線安裝指令的路徑指示錯誤 | 照做會找不到套件，且錯誤訊息看不出是路徑問題 |
| 手冊逐字核對（B4） | `pywinauto` 授權誤植為 MIT（實為 BSD 3-Clause） | 該段供 IT 查核，寫錯會當場被抓到 |

全部已修正，並補上盯住該不變式的測試。

---

## 任務地圖

```
階段 A  基礎建設（含 A0 環境驗證）
   └─▶ ⛸ 交付點 0：你跑 0_檢查環境.bat，確認 pywinauto 可用   ← 最優先
階段 B  期一探測腳本        ─┐
階段 C  純函式層（可並行）   ─┴─▶ ⛸ 交付點 1：你跑探測，帶回報告
                                        │
階段 D  期二整合 ◀──────────────────────┘
                                        │
                              ⛸ 交付點 2：你在目標機驗收
```

**⛸ 交付點 0 為什麼要獨立且最優先**：整個技術棧建立在「目標機能安裝 pywinauto」這個前提上。這件事花五分鐘就能驗證，卻決定後續 17 個任務是否白做。先驗證再往下走，是 TD-2「用最小成本先打掉最大未知數」的同一個原則。

規範：每個任務走 RED-GREEN-REFACTOR，`pytest` 必須先看到真實失敗。UI 相關任務因無法在開發機執行，改以靜態掃描或目標機驗收覆蓋（見 design.md §9）。**任何任務都不執行 `git commit`**，commit 訊息於階段結束時以文字交付。

---

## 階段 A — 基礎建設

### A0. 環境檢查腳本與 `.bat` 包裝層

- **產出**：`scripts/check_env.py`、五支 `.bat`、`tests/test_bat_wrapper.py`
- **相依**：無
- **對應 Scenario**：相依齊備、`pywinauto` 未安裝、Python 版本過低、`python` 不在 PATH、Python 與 py 皆不可用、雙擊執行批次匯出、從其他工作目錄雙擊、執行結束後視窗保留、腳本啟動即失敗、結束碼正確傳遞、不同系統語系下執行
- **驗證**：`pytest tests/test_bat_wrapper.py`
- **成功條件**：
  - 五支 `.bat` 皆含 `%~dp0`、`py -3` 退回 `python`、`-B`、`pause`、`exit /b %RC%`
  - **逐位元組掃描：所有 `.bat` 無任何 `> 0x7F` 位元組**
  - `check_env.py` 在缺套件時同時給出線上與離線兩種安裝指令，結束碼非零
  - `sys.stdout` 編碼已處理，cp950 主控台輸出中文不拋 `UnicodeEncodeError`
  - 突變：移除任一必要元素 → 測試變紅
- **估時**：35 分

> ### ⛸ 交付點 0
> 交付 `scripts/` 的 `.bat` + `check_env.py`。你在目標機雙擊 `0_檢查環境.bat`，把畫面結果回報。
> **若 `pywinauto` 不可用**：先試離線 wheel（我會一併附上），仍不行則啟動 TD-1 退路（改回 PowerShell），此時 A1 之後的任務需重估。

### A1. `config` 模組

- **產出**：`scripts/lib/config.py`、`scripts/config.json`、`tests/test_config.py`
- **相依**：A0
- **對應 Scenario**：更換 model 清單、目標機控制項與探測結果不符、路徑含中文或空白、選取模式不可用時退回明確清單
- **驗證**：`pytest tests/test_config.py`
- **成功條件**：缺必填欄位 → 拋出指名該欄位的錯誤；`%USERPROFILE%` 正確展開；含中文與空白的路徑正確處理；`models` 同時接受 `"SELECTED"` 與清單兩種型別；突變（移除必填檢查）→ 測試變紅
- **估時**：25 分

### A2. `reporting` 模組

- **產出**：`scripts/lib/reporting.py`、`tests/test_reporting.py`
- **相依**：A1
- **對應 Scenario**：全部成功、部分失敗、日誌檔以 UTF-8 寫入
- **驗證**：`pytest tests/test_reporting.py`
- **成功條件**：12 筆全成功 → 摘要「成功 12 / 失敗 0」且結束碼 0；含 2 筆失敗 → 摘要正確、逐條列原因、結束碼非零；日誌檔為 UTF-8
- **估時**：20 分

---

## 階段 B — 期一探測腳本

### B1. `uia` 樹走訪

- **產出**：`scripts/lib/uia.py`（走訪部分）
- **相依**：A1
- **對應 Scenario**：探測 AccuMark Explorer 主視窗、探測模態匯出對話框、自繪畫布無法曝光
- **驗證**：**開發機無 AccuMark → 以本機記事本驗證走訪邏輯**；AccuMark 專屬行為列入目標機驗收
- **成功條件**：能對記事本產出含 `name`/`automation_id`/`control_type`/`class_name`/`is_enabled`/深度/同層索引的完整樹；深度上限可設；無子節點的控制項不導致例外
- **估時**：35 分

### B2. 定位策略評估與選取狀態偵測

- **產出**：`scripts/lib/uia.py`（純函式部分）、`tests/test_locator.py`
- **相依**：B1
- **對應 Scenario**：控制項具備 AutomationId、控制項無任何穩定識別、清單支援讀取選取項、清單不支援讀取選取項
- **驗證**：`pytest tests/test_locator.py`
- **成功條件**：優先序 `automation_id` > `name` > `control_type+索引` 正確套用；三者皆不可靠 → 標記 `UNSTABLE` 並統計；報告輸出 `selection_readable` 布林值；突變（反轉優先序）→ 測試變紅
- **估時**：30 分

### B3. `probe_ui.py` 進入點與報告輸出

- **產出**：`scripts/probe_ui.py`、`tests/test_probe_output.py`
- **相依**：B1、B2、A2
- **對應 Scenario**：目標程序未啟動、報告產出位置、相依清單稽核
- **驗證**：`pytest tests/test_probe_output.py`；實跑 `1_執行探測.bat`（以記事本為目標）
- **成功條件**：目標程序不存在 → 非零結束碼 + 明確訊息 + **不產生報告檔**；報告落於 `probe-output/probe_<時間戳>.json` 並印出完整路徑；報告可被 `json.load` 解析
- **估時**：35 分

### B4. 使用手冊 · 期零與期一章節

- **產出**：`docs/使用手冊.md`（前半）
- **相依**：B3
- **對應 Scenario**：複製即用、目標機無外網
- **成功條件**：涵蓋複製資料夾、環境檢查、離線 wheel 安裝、探測兩步驟操作、報告帶回方式
- **估時**：25 分

> ### ⛸ 交付點 1
> 交付 `scripts/` + 手冊前半。你在目標機執行 `1_` 與 `2a`–`2c`，把 `probe-output/` 與回報表帶回。
> **交付前輸出 commit 訊息文字供你自行提交。**

---

## 階段 C — 純函式層（與階段 B 並行，不依賴探測）

### C1. `stability` 模組

- **產出**：`scripts/lib/stability.py`、`tests/test_stability.py`
- **相依**：A1
- **對應 Scenario**：大型 model 匯出耗時較久、檔案仍在寫入、逾時未產生檔案、一次匯出產生多個檔案
- **驗證**：`pytest tests/test_stability.py`
- **成功條件**：
  - 取樣函式以參數注入，測試餵入人工序列，不依賴真實計時
  - 大小仍在增長 → 不判定完成；連續 N 次相同 → 判定完成
  - **大小為 0 一律視為未穩定**（零位元組佔位檔防護）
  - 多檔案時須**全部**穩定才算完成
  - 突變：①「連續 N 次」改「任一次」②移除大小為 0 判斷 → 皆須變紅
- **估時**：35 分

### C2. `archival` 模組（TD-8）

- **產出**：`scripts/lib/archival.py`、`tests/test_archival.py`
- **相依**：A1
- **對應 Scenario**：連續兩次匯出、搬移失敗、四個 model 的歸檔結果、輸出根目錄含批次時間戳、正常無衝突、兩種 DXF 原始檔名相同、附加字尾後仍衝突、強制一律加後綴、目的地已有同名檔案、檔案內容不被修改
- **驗證**：`pytest tests/test_archival.py`（用 `tmp_path` fixture）
- **成功條件**：
  - `plan()` 為純函式，回傳目的路徑清單，**不觸碰檔案系統**
  - **無衝突 → 檔名與原始產出逐字相同**（TD-8 核心）
  - 有衝突 → 後者附加 `_AAMA`／`_ASTM`，仍衝突則加序號，兩者皆保留 + 記 WARN
  - `add_format_suffix=true` → 一律加後綴
  - 搬移層以雜湊比對驗證位元組不變；搬移失敗 → 原檔保留於暫存目錄
  - 突變：①移除衝突偵測 ②衝突改成覆蓋 → 皆須變紅
- **估時**：45 分

### C3. `runstate` 模組

- **產出**：`scripts/lib/runstate.py`、`tests/test_runstate.py`
- **相依**：A1
- **對應 Scenario**：第 7 個任務中斷後重跑、狀態檔記載成功但檔案已被刪除、強制全部重跑
- **驗證**：`pytest tests/test_runstate.py`
- **成功條件**：`should_skip` 需 `status=SUCCESS` **且** 所有 `outputs` 路徑存在；檔案被刪 → 不跳過；`--force` → 全部重跑且狀態檔重置；突變（移除 outputs 存在檢查）→ 變紅
- **估時**：30 分

### C4. `dialog_guard` 白名單比對

- **產出**：`scripts/lib/dialog_guard.py`、`tests/test_dialog_guard.py`
- **相依**：A1
- **對應 Scenario**：出現未知對話框、出現已知的覆蓋確認對話框、白名單可擴充而不改程式碼
- **驗證**：`pytest tests/test_dialog_guard.py`（參數化）
- **成功條件**：`match_whitelist()` 為純函式，輸入標題／內文／按鈕清單；**預設回傳「不在白名單」**（安全預設）；白名單自設定檔載入；突變（預設改成「在白名單」）→ 變紅
- **估時**：30 分

### C5. 靜態掃描測試

- **產出**：`tests/test_static_scan.py`
- **相依**：C1–C4
- **對應 Scenario**：執行期間游標不動、使用者同時操作其他程式
- **驗證**：`pytest tests/test_static_scan.py`
- **成功條件**：
  - 掃描所有 `.py`，斷言不含 `SetCursorPos`、`mouse_event`、`SendInput`、**`click_input`**、**`type_keys`**、`send_keys`、`pyautogui`
  - 掃描所有 `.bat`，斷言純 ASCII
  - 突變：插入 `click_input(` → 變紅（驗畢須還原並確認 `git diff` 乾淨）
- **備註**：`click_input` 的防護特別重要——pywinauto 同時提供 `click_input()`（實體滑鼠）與 `click()`（UIA Invoke），**名稱只差三個字，誤用不會報錯只會默默搶走滑鼠**
- **估時**：25 分

---

## 階段 D — 期二整合

> 2026-09-02 重排。TD-9／TD-10 之後，D 階段不再以探測報告為前提；探測報告與回報表變成 D6 的修正輸入。

### D0. 修審查 blocker 與設定 schema 擴充

- **產出**：`lib/archival.py`、`lib/dialog_guard.py`、`lib/config.py`、`config.json`、對應測試
- **相依**：無
- **成功條件**：歸檔撞名比對不分大小寫；守衛在標題為 None 時安全停機；config 接受 `expected_outputs`／`zip`／`dxf`／巢狀 `controls`（explorer／dcu）、策略新增 `title_re`／`control_type`、`quiet_period_sec`，移除 `verify_exclusive_lock`；`temp_dir` 巢狀於 `output_root` 拒絕；`result_status` 對照 Status 白名單；`zip.complete_dialog.title_like` 拒絕純萬用字元
- **估時**：40 分

### D1. `archival.check_ownership` 與 `_未歸類`／`_逾時殘留`

- **產出**：`lib/archival.py`、`tests/test_archival.py`
- **相依**：D0
- **對應 Scenario**：產出主檔名須符合當前 model、產出檔名不符當前 model、逾時訊號未到
- **成功條件**：純函式；不分大小寫；不符的不丟、不歸錯；突變檢查通過
- **估時**：35 分

### D2. `completion` 模組——訊號 + 穩定

- **產出**：`lib/completion.py`、`tests/test_completion.py`
- **相依**：C1
- **對應 Scenario**：ZIP 完成對話框出現、完成對話框出現但暫存夾仍在寫入、DXF 以預期檔案數判定、逾時訊號未到、一次匯出產生多個檔案、大型 model 匯出耗時較久
- **成功條件**：純函式（訊號偵測函式注入）；訊號未到 MUST NOT 開始算穩定；`files` 模式預期數量正確（單一 model）；突變檢查通過
- **估時**：45 分

### D3. `uia` 操作層

- **產出**：`lib/uia.py`（操作部分）
- **相依**：D0
- **成功條件**：`resolve(spec)`／`select_single`／`read_selection`／`set_value`／`invoke`／`set_combo`／`read_text`／`wait_window`；**不含任何實體輸入 API**（C5 守護）；`select_single` 後讀回確認恰好一項
- **驗證**：目標機驗收（`test_uia_live.py` 只覆蓋不需 AccuMark 的部分）
- **估時**：45 分

### D4. `batch_export.py` 主流程與 `--dry-run`

- **產出**：`scripts/batch_export.py`、`tests/test_orchestration.py`、`scripts/2e_確認控制項.bat`
- **相依**：D1–D3、C2–C4
- **對應 Scenario**：每個任務恰含一個 model 五條、固定輸出路徑四條、`--dry-run` 四條、暫存目錄啟動時非空、環境檢查（AccuMark 未啟動、工作階段已鎖定、環境檢查全數通過）、不佔用實體輸入裝置
- **驗證**：以假 uia 替身跑完整 12 任務流程；dry-run 以替身驗證「零操作呼叫」；替身記錄每次呼叫供斷言
- **成功條件**：12 任務依序、狀態轉移正確；DCU 讀回選取不是恰好該 model → `FAILED_SELECTION` 且替身記錄到零次執行鈕呼叫；`FAILED_MOVE`／`HALTED_UNKNOWN_DIALOG` 中止整批；觸發前斷言暫存夾為空；`--only`／`--format`／`--force`／`--dry-run`；state 落 `scripts\runs\`；OK 只按在宣告的完成對話框
- **估時**：60 分

### D5. 手冊期二章節

- **產出**：`docs/使用手冊.md`、`README.md`、`scripts/讀我_先跑這個.txt`
- **相依**：D4
- **對應 Scenario**：使用者依手冊完成首次設定、遇到未知對話框後自行排除、手冊與實作一致
- **成功條件**：dry-run 流程、12 任務結構、`_未歸類`／`_逾時殘留` 的意義、`FAILED_SELECTION` 的意義與處置、`expected_outputs` 怎麼填、**螢幕可關但不可鎖**、狀態代碼、日誌位置、白名單擴充；逐項比對 `.bat` 檔名／設定欄位／狀態代碼
- **估時**：40 分

> ### ⛸ 交付點 2
> 對方跑 `2e_確認控制項.bat`，帶回缺項清單（連同探測報告與回報表）。

### D6. 依缺項清單與探測報告修正 `controls`

- **產出**：`scripts/config.json`
- **相依**：⛸ 交付點 2
- **成功條件**：dry-run 全綠；`expected_outputs` 依回報表第 17–19 題修正；`models` 依選取狀態可讀性決定
- **估時**：30 分

> ### ⛸ 交付點 3
> 對方先跑 `3_執行批次匯出.bat --only <一個 model>` 試一個，再全跑。依 design.md §9.3 驗收清單回報。

---

## 統計

| 階段 | 任務數 | 估時 | 是否需等你 |
|---|---|---|---|
| A 基礎建設 | 3 | 80 分 | 僅 A0 後需你跑一次環境檢查 |
| B 期一探測 | 4 | 125 分 | 否 |
| C 純函式層 | 5 | 165 分 | 否 |
| D 期二整合 | 7 | 295 分 | D0–D5 否；D6 需交付點 2 的缺項清單 |
| **合計** | **19** | **約 11 小時** | |

無單一任務超過 60 分。Scenario 數依 2026-09-02 改版重算，見 design.md §13。

## 未決事項

全部已於 design.md §12 收斂。剩餘未知數由 `--dry-run`（交付點 2）解決，**不阻擋 D0–D5 開工**。
