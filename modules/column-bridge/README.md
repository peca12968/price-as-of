# Column Bridge｜揀貨工單的資料庫欄位遷移演練

倉庫把工單上的儲位欄位從 `bin_code` 改名為 `pick_slot`。發版期間，新工作站已經用新名稱讀寫，上一班的工作站還在使用舊名稱。這個 PostgreSQL 演練讓兩個版本共存，再用一筆被鎖住的歷史工單，驗證何時可以切換讀取來源。

## 跑一次四筆工單的交接

需要 Node.js 22 以上及可建立 schema 的 PostgreSQL 連線。若使用隨附的 Docker Compose，可在專案目錄執行以下命令。

```sh
docker compose up -d --wait
npm ci
```

設定連線字串。使用 PowerShell 時執行第一個區塊，使用 Bash 時執行第二個區塊。

```powershell
$env:DATABASE_URL = 'postgresql://postgres:local-drill@127.0.0.1:55432/rehearsal'
```

```sh
export DATABASE_URL=postgresql://postgres:local-drill@127.0.0.1:55432/rehearsal
```

```sh
npm run demo
npm test
```

每次演練都在資料庫內建立隨機命名的 schema，結束時清除該次資料。用完本機容器可執行 `docker compose stop`。[可執行演練](demo.mjs)

| 操作 | 資料庫實際結果 |
| --- | --- |
| 舊工作站鎖住工單 1，執行兩批補資料 | 第一批更新 2、3、4，第二批回傳空清單。新工作站仍讀得到工單 1 的 A1。 |
| 此時嘗試切換讀取來源 | 驗證約束發現工單 1 的新欄位仍為空，交易回滾。 |
| 釋放鎖，再次補資料並切換 | 補上工單 1。新工作站寫入 B7，舊工作站立即可從舊欄位讀到 B7。 |
| 舊工作站最後寫入 C9，退役舊欄位 | 新工作站透過同一個介面讀回 C9，仍可新增工單。 |

## 發布與回復邊界

1. **擴充。** 新增可空欄位，讓新版先透過可更新的 `tickets_v2` 檢視表操作舊欄位。資料表觸發器同步兩欄。以 `NOT VALID` 加入的約束會檢查之後的寫入，歷史工單留待分批處理。[擴充 SQL](sql/expand.sql)
2. **補資料。** 每批以主鍵排序，使用 `FOR UPDATE SKIP LOCKED` 取得目前能處理的工單。資料列的鎖與更新屬於同一交易。若交易回滾，下一批可以重新處理該筆。在有其他工作站持鎖時，空批次只表示這一次沒取得工單。[補資料 SQL](sql/backfill.sql) · [PostgreSQL 鎖定子句](https://www.postgresql.org/docs/17/sql-select.html#SQL-FOR-UPDATE-SHARE)
3. **切換。** 在同一交易驗證全部資料的新舊值一致，再把檢視表改為讀寫新欄。切換後觸發器仍維護舊欄，因此應用程式可退回舊版。[切換 SQL](sql/cutover.sql)
4. **收尾。** 部署端確認舊版已退出後，才移除舊欄與觸發器。退役條件由部署端判斷，演練透過結束舊版操作模擬。過早移除時，舊檢視表的欄位依賴會阻止刪除。取得鎖超過一秒時，整個階段會回滾，可在舊交易結束後重試。刪除舊欄後，舊版查詢會失效。[收尾 SQL](sql/contract.sql) · [交易處理](drill.mjs)

整合測試使用真正的 PostgreSQL 連線，讓兩個補資料程序在交易重疊時處理不同工單，並驗證其中一個回滾後另一個可以接手。其他案例涵蓋新舊版交錯新增與修改、互相矛盾的欄位值、過早收尾、鎖逾時與重試。CI 在 PostgreSQL 16 與 18 分別執行測試與示範。[整合測試](bridge.test.mjs) · [CI 設定](../../.github/workflows/system.yml)

本作品使用 AI 輔助開發，工單與儲位皆為合成資料。
