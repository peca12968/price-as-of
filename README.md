# Rowgroup Scout｜Parquet 查詢可以跳過多少資料

同一批 12,288 筆包裹掃描紀錄，按序號寫入或交錯寫入各會形成 6 個 row group。查詢序號 4,096 到 6,143 時，兩份檔案都得到 2,048 筆，序號總和都是 10,484,736。排序檔可從欄位統計排除其中 5 組，交錯檔則需保留全部 6 組。

## 查指定 Iceberg 快照，遇到欄位改名怎麼算？

庫存表原有的 `qty` 改名為 `available`，後來又新增一欄叫 `qty`。在同一個快照查詢 10 到 19 時，兩個欄位分別有 3 筆和 2 筆符合。若只看實體檔案裡叫 `qty` 的欄位，兩種意思會混在一起，算成 5 筆。工具用快照 schema 的 field ID 對應實體 Parquet 欄，並以 PyIceberg 實際讀回的列驗證。

安裝選用依賴後，參數依序是 metadata JSON 路徑、快照 ID、最上層整數欄位 ID、含括下界與排除上界。以下為命令格式與本機整合示範。

```sh
python -m pip install -r requirements-iceberg.txt
python snapshot_scout.py /path/to/metadata.json 12345 1 10 20
git clone https://github.com/peca12968/iceberg-footprint.git ../iceberg-footprint
git -C ../iceberg-footprint checkout a4324515476369c77c3b19587ea02cae93290257
python iceberg_demo.py ../iceberg-footprint/footprint.py
```

報告逐檔列出實體欄名、判定依據、候選 row group 與符合筆數上下界。示範匯入一個缺少 field ID 的外部檔後，界限放寬至 2 到 4 筆，完整解碼查得 3 筆。若欄位在舊檔可確定不存在，則依其初始預設值或空值判定。對含位置刪除的資料檔，下界按關聯刪除檔的紀錄數保守扣除，上界維持原估算。支援 PyIceberg 能建立掃描計畫的 v1/v2 快照和 Parquet 資料檔。

## 重現兩種排列

在 Python 3.12 的虛擬環境中執行以下命令。示範程式會建立新目錄、用 DuckDB 寫出兩份 Parquet，再用 SQL 查核篩選結果。

```sh
python -m pip install -r requirements.txt
python layout_demo.py build-layout
python scout.py build-layout/sorted.parquet seq 4096 6144 --project label
python -m unittest -v
```

在這份示範中，序號與標籤兩欄的候選壓縮資料量，排序檔為 18,557 B，交錯檔為 113,061 B。此值加總保留群組中指定欄位的壓縮區塊大小；快取、頁面索引、讀取合併與查詢引擎都會影響實際 I/O。

## 查兩個條件同時成立的筆數範圍

以 `python scout.py orders.parquet amount 100 500 --and quantity 2 6 --project sku` 查詢金額及數量。整數範圍含下界、排除上界；可重複提供 `--and` 與 `--project`。工具接受最上層的單一整數篩選欄位與平面投影欄位，每欄只計一次大小。同欄條件先取交集，交集為空時即使沒有統計也能排除該組。

報告列出使用的欄位、各組的最小與最大值、空值數、符合筆數上下界。若整組都落在範圍外或皆為空值，可跳過；統計不足則保留候選。對多欄 AND 條件，上界取各條件上界的最小值，下界用各條件可能不符合的筆數扣回。每組 4 筆中若兩欄各有 1 個空值，且所有非空值均符合條件，結果界限是 2 至 3 筆。測試把兩個空值放在不同列與同一列，兩種檔尾統計相同，實際查詢分別得到 2、3 筆。

測試涵蓋缺失統計、全空群組、半開端點、超過有號 64 位元上限的整數；另用 240 組多條件篩選逐列核對每組的估算範圍。執行環境與輸入範例皆可重建，專案以 AI 輔助開發。[Parquet 檔案結構](https://parquet.apache.org/docs/concepts/)
