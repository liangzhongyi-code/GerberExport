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

### Requirement: 每個任務恰含一個 model
The system SHALL 對清單中的每個 model，各執行一次 Explorer 的 Export Zip、一次 DCU 的 AAMA 匯出、一次 DCU 的 ASTM 匯出；每次匯出的來源 MUST 只包含該一個 model。DCU 任務觸發前 MUST 讀回 Source File Name 的選取狀態，恰好一項且為該 model 才可執行。

> **背景**（TD-9）：使用者確認 DCU 多選匯出會把全部 model 的裁片併進同一個 DXF。分檔必須由結構保證，不能靠選項或操作習慣。

#### Scenario: 四個 model 完整跑完
- **GIVEN** 清單含 4 個 model，Explorer 與 DCU 中皆存在
- **WHEN** 雙擊 `3_執行批次匯出.bat`
- **THEN** 共執行 12 個任務，日誌記錄 12 筆任務結果，每個 model 資料夾含 zip、AAMA 產出、ASTM 產出，且每個 DXF 只含該 model 的裁片

#### Scenario: 設定檔中的 model 不存在
- **GIVEN** 設定檔列出的某個 model 名稱在 Explorer 或 DCU 清單中找不到
- **WHEN** 腳本處理到該 model
- **THEN** 該 model 的 3 個任務標記為 `SKIPPED_NOT_FOUND`，腳本繼續處理下一個 model，最終結束碼為非零

#### Scenario: 匯出設定不被更動
- **GIVEN** Export Zip 畫面的元件選項，以及 DCU 的 Export Options、Source Storage Area、Notch Table
- **WHEN** 腳本完成全部任務
- **THEN** 上述選項狀態與執行前一致——腳本 MUST 只設定 File Type、來源選取、目的路徑三樣，MUST NOT 開啟 Export Options

#### Scenario: DCU 選取恰好一項
- **GIVEN** 腳本已在 Source File Name 清單選取該 model
- **WHEN** 觸發前讀回選取狀態
- **THEN** 讀到恰好一項且名稱等於該 model，腳本才按執行鈕

#### Scenario: 控制項定位不到
- **GIVEN** 介面語系與設定檔預填的名稱不同（例如選單顯示「檔案」而非 `File`）
- **WHEN** 腳本嘗試觸發該任務
- **THEN** 任務標記為 `FAILED_UI`，訊息指出是哪一個控制項、用什麼條件找的；腳本繼續下一個任務，MUST NOT 中止整批

#### Scenario: DCU 選取殘留多項
- **GIVEN** 讀回選取狀態顯示不只一項（例如上一次的選取仍亮著），或唯一一項不是該 model
- **WHEN** 腳本準備觸發
- **THEN** MUST NOT 執行；任務標記為 `FAILED_SELECTION`，日誌記錄讀到的全部項目，腳本繼續下一個任務

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

### Requirement: 完成偵測以 UI 訊號為主、檔案穩定為輔
The system SHALL 先等待該任務宣告的 UI 完成訊號（ZIP：標題符合設定的完成對話框；DXF：Results 窗格有結果，或暫存目錄檔案數達預期），訊號到達後再以「檔案大小連續 N 次取樣不變」確認，兩者皆滿足才判定完成。MUST NOT 以固定 sleep 秒數作為完成依據；MUST NOT 在訊號到達前開始計算穩定。

> **背景**（TD-4 修訂）：官方文件確認 Export Zip 結束會跳「Process Complete」對話框、DCU 於 Results 顯示結果。審查實測抓到的兩個提前判定缺陷，根源都是「太早開始算穩定」。

#### Scenario: ZIP 完成對話框出現
- **GIVEN** Export Zip 已觸發
- **WHEN** 標題符合 `zip.complete_dialog.title_like` 的視窗出現
- **THEN** 腳本開始檔案穩定確認；穩定後才按該對話框的 OK；在此之前 MUST NOT 對任何視窗送出 OK

#### Scenario: 完成對話框出現但暫存夾仍在寫入
- **GIVEN** 完成對話框已出現，但暫存夾內有檔案大小仍在變化
- **WHEN** 腳本取樣
- **THEN** 不判定完成、不按 OK，繼續等到穩定

#### Scenario: DXF 以預期檔案數判定
- **GIVEN** `dxf.completion` 為 `files`，`expected_outputs.AAMA` 為 `[".dxf", ".rul"]`
- **WHEN** 暫存目錄出現 2 個檔案且全部穩定
- **THEN** 任務判定完成；只出現 1 個時 MUST 繼續等待直到逾時

#### Scenario: 大型 model 匯出耗時較久
- **GIVEN** 某 model 裁片數量多，匯出耗時 45 秒
- **WHEN** 腳本等待
- **THEN** 持續等待直到訊號與穩定皆滿足，任務標記為成功（未因預設等待時間過短而誤判失敗）

#### Scenario: 逾時訊號未到
- **GIVEN** 匯出已觸發，經過 `timeout_sec` 後完成訊號仍未出現
- **WHEN** 逾時判定觸發
- **THEN** 任務標記為 `FAILED_TIMEOUT`，暫存夾殘留檔案搬到輸出資料夾的 `_逾時殘留\<任務>\`（MUST NOT 刪除），腳本繼續下一個任務

#### Scenario: 一次匯出產生多個檔案
- **GIVEN** AAMA 匯出同時產生 `.dxf` 與附帶的 `.rul`
- **WHEN** 腳本偵測完成
- **THEN** 該次任務的產出全部納入歸檔，MUST NOT 只取其中一個

---

### Requirement: `--dry-run` 只定位、不操作
The system SHALL 提供 `--dry-run` 模式（由 `2e_確認控制項.bat` 觸發）：依 `config.controls` 逐一定位 Explorer 與 DCU 的每個控制項並回報結果，MUST NOT 呼叫任何 Invoke／SetValue／Select／AddToSelection，MUST NOT 寫入暫存目錄或輸出目錄。

> **背景**（TD-10）：把「人讀整棵探測樹」變成「程式回報缺哪幾個」。也是驗證 pywinauto 看不看得見 AccuMark 的最快方法——一個都找不到就表示介面自繪，整個方案翻船，五分鐘驗得出來。

#### Scenario: 全部找到
- **GIVEN** 設定檔中所有控制項在目標機上皆可定位
- **WHEN** 雙擊 `2e_確認控制項.bat`
- **THEN** 逐項印出「找到」與實際的控制項型別，結束碼 0，且畫面記錄存入 `probe-output\`

#### Scenario: 部分找不到
- **GIVEN** 有 3 個控制項定位失敗
- **WHEN** 執行 dry-run
- **THEN** 逐項印出缺的 3 個與各自的搜尋條件，結束碼非零，其餘找到的照常列出，MUST NOT 因第一個失敗就停止

#### Scenario: 整個視窗找不到
- **GIVEN** DCU 未開啟
- **WHEN** 執行 dry-run
- **THEN** 回報 DCU 視窗找不到並列出目前所有視窗標題，Explorer 那一半照常檢查

#### Scenario: dry-run 不改變任何狀態
- **GIVEN** 執行前 Explorer 的選取狀態、DCU 的表單內容、暫存目錄內容
- **WHEN** dry-run 結束
- **THEN** 三者與執行前完全相同
