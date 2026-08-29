# Spec: 批次匯出（期二）

## ADDED Requirements

### Requirement: 不佔用實體輸入裝置
The system SHALL 全程透過 UI Automation 的 InvokePattern／ValuePattern／SelectionPattern 操作控制項，MUST NOT 呼叫任何會移動實體滑鼠游標或搶奪鍵盤焦點的 API（`SetCursorPos`、`mouse_event`、`SendInput`、`SendKeys`）。

#### Scenario: 執行期間游標不動
- **GIVEN** 批次任務正在執行且使用者未觸碰滑鼠
- **WHEN** 腳本在任務開始、每個任務結束、全部結束三個時點取樣 `Cursor.Position`
- **THEN** 三次取樣的座標完全相同，且此結果寫入日誌

#### Scenario: 使用者同時操作其他程式
- **GIVEN** 批次任務正在執行
- **WHEN** 使用者切換到瀏覽器並輸入文字
- **THEN** 使用者輸入的文字完整出現在瀏覽器中，未被腳本竄改或截走

---

### Requirement: model 清單來源
The system SHALL 支援兩種 model 清單來源：讀取 AccuMark Explorer 中目前選取的項目（`"SELECTED"`，預設），或設定檔中的明確清單。

> **背景**：使用者不希望維護清單。若探測確認清單控制項可讀取選取狀態，使用者只要框選再雙擊即可，設定檔完全不用碰。

#### Scenario: 選取模式
- **GIVEN** `models` 設為 `"SELECTED"`，使用者在 Explorer 中框選了 4 個 model
- **WHEN** 雙擊 `3_執行批次匯出.bat`
- **THEN** 腳本處理這 4 個 model，並在開始前印出讀到的清單供使用者確認

#### Scenario: 選取模式但未選取任何項目
- **GIVEN** `models` 設為 `"SELECTED"`，但 Explorer 中沒有任何項目被選取
- **WHEN** 腳本啟動
- **THEN** 中止並提示「請先在 Explorer 選取要處理的 model」，MUST NOT 誤把全部項目當成選取

#### Scenario: 選取模式不可用時退回明確清單
- **GIVEN** 探測顯示 `selection_readable: false`，設定檔改為明確清單
- **WHEN** 腳本啟動
- **THEN** 依明確清單處理，行為與選取模式一致

---

### Requirement: 逐一匯出
The system SHALL 對清單中的每個 model，依序執行 AccuMark ZIP、AAMA DXF、ASTM DXF 三種匯出，每次匯出的來源 MUST 只包含一個 model。

#### Scenario: 四個 model 完整跑完
- **GIVEN** 清單含 4 個 model，AccuMark Explorer 中皆存在
- **WHEN** 雙擊 `3_執行批次匯出.bat`
- **THEN** 共執行 12 次匯出，日誌記錄 12 筆任務結果，且成功筆數為 12

#### Scenario: 設定檔中的 model 不存在
- **GIVEN** 設定檔列出的某個 model 名稱在 AccuMark Explorer 中找不到
- **WHEN** 腳本處理到該 model
- **THEN** 該 model 的 3 個任務全部標記為 `SKIPPED_NOT_FOUND`，腳本繼續處理下一個 model，最終結束碼為非零

#### Scenario: 匯出設定不被更動
- **GIVEN** 匯出對話框中除輸出路徑外的所有選項
- **WHEN** 腳本完成全部 12 次匯出
- **THEN** 對話框的選項狀態與腳本執行前一致（腳本 MUST NOT 修改任何匯出選項）

---

### Requirement: 固定輸出路徑
The system SHALL 將所有匯出導向設定檔指定的單一暫存目錄，且該路徑在整批執行過程中 MUST NOT 改變。

#### Scenario: 對話框已記住上次路徑
- **GIVEN** 匯出對話框的路徑欄位已顯示正確的暫存目錄
- **WHEN** 腳本執行匯出
- **THEN** 腳本讀取該欄位、確認與設定檔一致後直接進行，未寫入任何值

#### Scenario: 對話框路徑不正確
- **GIVEN** 匯出對話框的路徑欄位顯示的不是設定檔指定的暫存目錄
- **WHEN** 腳本執行匯出
- **THEN** 腳本以 ValuePattern 直接設定該欄位為正確路徑，並在日誌記錄此次修正

#### Scenario: 暫存目錄不存在
- **GIVEN** 設定檔指定的暫存目錄尚未建立
- **WHEN** 腳本啟動
- **THEN** 腳本自動建立該目錄後才開始第一個任務

#### Scenario: 暫存目錄啟動時非空
- **GIVEN** 暫存目錄中殘留上次執行未清理的檔案
- **WHEN** 腳本啟動
- **THEN** 腳本停止並要求使用者確認，MUST NOT 自動刪除殘留檔案

---

### Requirement: 完成偵測不依賴固定等待
The system SHALL 以「暫存目錄出現新檔案，且該檔案大小連續 N 次取樣不變」判定單次匯出完成，MUST NOT 以固定 sleep 秒數作為完成依據。

#### Scenario: 大型 model 匯出耗時較久
- **GIVEN** 某 model 裁片數量多，匯出耗時 45 秒
- **WHEN** 腳本輪詢暫存目錄
- **THEN** 腳本持續等待直到檔案大小穩定，任務標記為成功（未因預設等待時間過短而誤判失敗）

#### Scenario: 檔案仍在寫入
- **GIVEN** 新檔案已出現但大小仍在增長
- **WHEN** 腳本進行第 k 次取樣
- **THEN** 腳本不判定完成，繼續輪詢

#### Scenario: 逾時未產生檔案
- **GIVEN** 匯出動作已觸發，但經過設定檔指定的逾時秒數後暫存目錄仍為空
- **WHEN** 逾時判定觸發
- **THEN** 該任務標記為 `FAILED_TIMEOUT`，記錄逾時秒數，腳本繼續下一個任務

#### Scenario: 一次匯出產生多個檔案
- **GIVEN** AAMA 匯出同時產生 `.dxf` 與附帶的規則檔
- **WHEN** 腳本偵測到檔案大小全部穩定
- **THEN** 該次任務的產出被記錄為多個檔案，全部納入後續歸檔，MUST NOT 只取其中一個
