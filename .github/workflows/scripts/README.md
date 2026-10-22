# GPU Cloud Rental Price Tracker / GPU 雲端租賃價格追蹤

## 功能與限制
- 每週一 UTC 02:15（台灣時間週一 10:15）自動檢查上游 GitHub 的**月度**歷史價格 CSV，變更時提交到自己的 repository。
- 僅保留 AWS / Azure / GCP / OCI、H100 / H200 / B200 / B300、on-demand、`in_index=True` 的資料；保留各地區、form factor 和原始來源欄位。
- **重要：這是每週檢查月度資料的自動更新版，不是真正的每週價格採樣。** 原始作者未更新時，資料不會有新月份。缺值不是零元，也不能用月度價格補造週價。
- 目前尚未整合獨立的即時 CSP API 每週報價。要取得真正每週序列，需再開發各 CSP SKU mapping、API 擷取、時點快照、失敗重試與一致性驗證。

## 使用步驟
1. 在 GitHub 建立自己的**私人或公開** repository，將此資料夾內容上傳到 repository 根目錄（包含隱藏的 `.github` 目錄）。
2. 到 Actions → Check GPU price dataset → Run workflow，手動執行第一次更新。若 GitHub Actions 無寫入權限，至 Settings → Actions → General → Workflow permissions 選 Read and write permissions。
3. 更新成功後 `data/historical_monthly.csv` 出現資料，`data/update_status.csv` 記錄上次檢查資訊。
4. 在 Excel：資料 → 取得資料 → 從 Web；輸入 `https://raw.githubusercontent.com/<YOUR_USER>/<YOUR_REPO>/main/data/historical_monthly.csv`（私人 repository 需改用驗證方式或本機檔案）。Power Query 設定 `month` 為文字、價格為小數，再按「關閉並載入」。
5. Excel：資料 → 查詢與連線 → 內容 → 開啟檔案時重新整理（重新整理需要能存取網路）。

## 資料定義
每列代表 month × provider × GPU × form_factor × segment × region × source；USD/GPU/hour。**不是每個月每家業者每種 GPU 都有價格。** 同月同業者可能有多個地區、來源，不要直接求和；先依 form factor / 地區 / source 取一致口徑或中位數。

## 本地執行
`python scripts/update.py`（Python 3.10+，標準函式庫即可）。

## 來源
https://github.com/rahilmittal1/gpu-price-history
上游資料的授權、使用條件請另查原始專案 `DATA_LICENSE.md`。
