# Iceberg Footprint｜兩個舊快照共用的檔案，該算幾次？

一張收貨明細表有四個 Iceberg 快照。逐一檢視最早的兩個快照，移除其中任一個都無法釋出資料檔。若一起移除，則有一個檔案失去最後的快照引用。財務稽核標籤仍指向第一版時，答案又變成零。工具會列出每個候選檔案的大小、原始引用者與模擬後剩餘的引用者。

## 重現四個時間點

使用 Python 3.12。演練在暫存目錄建立 SQLite catalog、Iceberg v2 表和實際 Parquet 資料檔。

```sh
python -m pip install --only-binary=:all: -r requirements.txt
python demo.py
python -m unittest -v
```

| 階段 | 以 Iceberg time travel 讀回的收據 ID |
| --- | --- |
| 第一次寫入 | 1、2 |
| 追加第二批 | 1、2、3、4 |
| 刪除前兩筆 | 3、4 |
| 再追加一筆 | 3、4、5 |

樣本共建立三份 Parquet。測試逐一讀取檔案內容、比對磁碟大小，再從各快照的查詢結果獨立核對引用關係。相同候選集合在有稽核標籤與解除標籤後得到不同答案。已經開啟的舊 metadata JSON 仍應重現原來的判定。[演練](demo.py) · [v1、v2 與分支測試](test_footprint.py)

## 盤點一個指定的 metadata 版本

```sh
python footprint.py /warehouse/shop/receipts/metadata/00004-example.metadata.json
python footprint.py /warehouse/shop/receipts/metadata/00004-example.metadata.json 12345 67890
```

第一行列出快照 ID、父快照與指向它的分支或標籤。第二行請換成真實 ID。工具依每份 manifest 的有效項目建立引用表，略過已刪除項目；同一個檔案路徑只加總一次。分支、標籤與目前快照的頂端一律保留。

- `candidate_file_bytes` 是候選快照引用的內容檔聯集大小；`unreferenced_file_bytes` 是在指定模擬下，失去此表所有快照引用的內容檔大小。
- 適用 Iceberg v1/v2。計算範圍是指定 metadata 所列的資料檔及刪除檔，沒有計入 manifest、metadata JSON、物件儲存版本或其他表的共享引用。
- 這是唯讀的指定集合模擬。執行真正的快照到期作業時，Iceberg 的分支歷史保留期、保留筆數與表設定也會影響結果。報告不應直接作為刪檔清單。[Iceberg 檔案規格](https://iceberg.apache.org/spec/) · [盤點程式](footprint.py)

本專案使用 AI 輔助開發，示範只使用本機合成資料。
