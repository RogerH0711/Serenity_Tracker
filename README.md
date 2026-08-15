# Serenity Tracker

Serenity Tracker 會定期抓取指定 X 帳號近期貼文，從明示的 cashtag（例如 `$NVDA`）提取半導體與 AI 供應鏈觀點，並生成不需要後端伺服器的靜態儀表板。
<img width="2880" height="1624" alt="image" src="https://github.com/user-attachments/assets/c2b044ab-bb1a-42af-89bb-48c15b96f90c" />


## 設計重點

- 以 X status ID 作為穩定的 `post_id`，不會因相同貼文再次出現在頁面上而重複處理。
- Gemini 只解析新貼文或先前失敗的貼文，避免重複消耗 API 配額。
- SQLite 強制 `UNIQUE(post_id, ticker)`，重跑整條管線也不會產生重複歷史紀錄。
- Gemini 使用相容的結構化輸出與本地 Pydantic 驗證；ticker 只能來自原文明示的 `$CASHTAG`，常見 `$100B`、`$3.4M` 等金額會被排除。
- 主貼文與引用貼文脈絡分開保存；情緒代表作者對股票的投資立場，不會只因 `bottleneck`、`shortage` 等負面字眼就判為 Bearish。
- 每個立場必須附上輸入原文中的逐字證據，本地驗證找不到該證據時拒絕寫入；方向不明時使用 Neutral。
- 每筆分析會顯示可信度：人工覆核、證據已核對、舊資料未驗證或待覆核；有逐字證據不等於解讀一定正確，仍應閱讀原文。
- 人工修正獨立存放在 `mention_overrides`，建站時優先於模型結果，Gemini 重新解析也不會覆蓋人工決定。
- 重新解析失敗時保留最後一版成功分析；程式設定造成的 4xx 會立即停止管線，不會誤報成功。
- 每階段採原子檔案替換；爬蟲失敗時不會讓下游誤用舊 JSON。
- 靜態頁面使用 DOM `textContent` 渲染外部資料，不使用 `innerHTML`。
- 儀表板以資料集最新日期為截止日，提供日、7 天、28 天、90 天四種滾動視圖；`Daily` 不會再混入舊股票。
- 搜尋、情緒、風險與排序狀態會寫入網址；收藏與最近查看只保存在目前裝置的瀏覽器中。
- `ticker_aliases.json` 是可人工審核、可進版控的股票代碼合併規則，避免同一家公司因市場後綴或 OTC 代碼被拆成多張卡。

## 資料流

```text
X profile
  └─ scraper.py ──> raw_tweets.json (atomic)
                         │
                         └─ parser.py ──> posts + mentions (SQLite)
                                                │
                                                └─ build_site.py ──> index.html (atomic)
```

SQLite 使用三張核心資料表：

```text
posts(post_id PK, timestamp, text, context, url, parse_status, analysis_version, ...)
  └─ mentions(post_id FK, ticker, sentiment, sentiment_evidence, thesis, risks, ...)
       UNIQUE(post_id, ticker)
  └─ mention_overrides(post_id, ticker, sentiment, evidence, thesis, risks, review_note, ...)
       PRIMARY KEY(post_id, ticker)
```

`parse_status` 會記錄 `pending`、`completed` 或 `failed`。暫時失敗的項目會在下一次排程重試；遇到 429/503 時會停止本次剩餘 API 呼叫，避免連續撞擊服務。歷史 `legacy-v1` 分析不會只因程式版本改變而自動重跑，避免舊資料突然消失或大量消耗配額。

## 安裝

```bash
git clone https://github.com/your-username/serenity-tracker.git
cd serenity-tracker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

建立 `.env`：

```env
GEMINI_API_KEY=your_gemini_api_key
X_AUTH_TOKEN=your_x_auth_token
```

選用設定：

```env
X_TARGET_ACCOUNT=aleabitoreddit
GEMINI_MODEL=gemini-2.5-flash
SCRAPE_SCROLL_ROUNDS=3
SCRAPE_MAX_POSTS=40
PARSER_MAX_POSTS=20
```

請把 `.env` 視為密碼檔案，不要提交或貼到日誌。透過 Session Cookie 自動存取 X 也可能受平台規則或登入驗證變化影響，部署前請自行確認適用條款。

## 執行

```bash
./run_pipeline.sh
```

管線依序執行：

1. 初始化或遷移資料庫。
2. 成功抓取並原子更新 `raw_tweets.json`。
3. 只解析資料庫中尚未完成的貼文。
4. 從資料庫重新生成 `index.html`。

### 儀表板查詢

搜尋框除了 ticker 與全文搜尋，也支援常用的規則式問句，不會呼叫 Gemini：

```text
幫我看 SIVE
AXTI 有哪些風險？
目前有哪些偏多股票？
本週偏空股票
顯示我的收藏
```

期間、情緒、風險、排序、搜尋、個股與分頁會同步到網址，因此可以重新整理或分享，例如：

```text
index.html?period=week&sentiment=Bullish
index.html?ticker=AXTI&tab=risks
```

收藏與最近查看屬於裝置本機偏好，使用瀏覽器 `localStorage`，不會進入 SQLite 或上傳。

### Ticker alias 管理

在 `ticker_aliases.json` 以 canonical ticker 為鍵，列出需要合併的別名：

```json
{
  "SIVE": {
    "aliases": ["SIVEF"],
    "company_name": "Sivers Semiconductors",
    "exchange": ""
  }
}
```

原始 `mentions.ticker` 不會被改寫；alias 只在建站時合併，個股完整紀錄仍會標示原始代碼，方便人工追查。若同一 alias 被指向兩個 canonical ticker，建站會直接失敗而不是靜默合併。

腳本會透過 `.pipeline.lock` 的作業系統 advisory lock 防止兩次 cron 排程同時執行；即使程序異常結束，鎖也會由作業系統釋放。任何未處理的階段錯誤都會立即停止後續步驟。

若人工查核後需要重新分析某一篇貼文，可在該貼文仍存在於 `raw_tweets.json` 時執行：

```bash
venv/bin/python parser.py --reparse 2088226398708338889
venv/bin/python build_site.py
```

重新解析成功前，資料庫會保留上一版分析，不會先刪除既有歷史。

### 可信度與人工覆核

儀表板的可信度標籤定義如下：

- `人工覆核`：有人工 override，或該筆本來就是人工分析版本；建站時優先採用。
- `證據已核對`：新版模型結果包含可在輸入來源文字中找到的逐字立場依據。
- `舊資料未驗證`：`legacy-v1` 歷史資料沒有逐字立場依據，多空只能作為線索。
- `待覆核`：非 legacy 資料但目前沒有立場證據。

先查看模型結果與原文：

```bash
venv/bin/python review.py show 2088226398708338889 --ticker SHKY
```

確認後建立人工覆核，再重建網站：

```bash
venv/bin/python review.py set 2088226398708338889 SHKY \
  --sentiment Bullish \
  --evidence 'The $SHKY, Samsung, $SNDK, $MU memory bottleneck never changed anon' \
  --thesis '作者延續對記憶體瓶頸受惠標的的偏多立場。' \
  --note '結合貼文引用的既有看多脈絡人工確認' \
  --reviewer roger
venv/bin/python build_site.py
```

`--risks` 可省略。用 `review.py list` 查看所有覆核，用 `review.py delete POST_ID TICKER` 刪除後即可恢復模型結果。覆核備註、覆核者與時間會進入產生的公開 `index.html`，請勿填入敏感資訊。

### 舊資料庫遷移

第一次執行新版 `db_setup.py` 時，若偵測到舊版扁平 `mentions` 表，系統會：

1. 在 `backups/` 建立一致性的 SQLite 備份。
2. 從原始網址擷取 `post_id`。
3. 每個 `(post_id, ticker)` 保留最早一筆分析。
4. 建立外鍵與唯一約束後才完成交易。

備份資料夾已被 Git 忽略。確認新站點與資料無誤後，可自行決定備份保留時間。

## Cron

使用完整路徑將輸出追加至日誌，例如每小時執行：

```cron
0 * * * * /absolute/path/serenity-tracker/run_pipeline.sh >> /absolute/path/serenity-tracker/pipeline.log 2>&1
```

`run_pipeline.sh` 會自行判斷專案位置，不需要把使用者名稱寫死在腳本裡。

### 安全自動發布 GitHub Pages

如果希望管線成功後自動提交網站產物，改用：

```bash
./publish_site.sh
```

發布腳本會先確認目前位於 `main`、工作目錄乾淨且沒有未推送 commit，再執行 `git pull --rebase` 與完整管線。它只允許 `index.html` 發生變化，也只會 stage、commit、push 這一個檔案。解析失敗、出現其他檔案異動或 Git 分支分歧時都會停止，不會推送半成品。

每小時更新並發布可使用：

```cron
0 * * * * /absolute/path/serenity-tracker/publish_site.sh >> /absolute/path/serenity-tracker/pipeline.log 2>&1
```

執行環境仍須具備 GitHub push 權限。若不需要自動推送，繼續使用 `run_pipeline.sh` 即可。

## 測試

測試不會連線 X 或 Gemini：

```bash
venv/bin/python -m unittest discover -s tests -v
```

涵蓋舊資料遷移、重複寫入、人工覆核持久性、增量與指定重析、引用脈絡、立場證據驗證、cashtag 擷取，以及靜態 HTML 的 script/XSS escaping。

## GitHub Pages 與雲端排程

`index.html` 可以直接發布至 GitHub Pages。若再加入 GitHub Actions：

- 將 `GEMINI_API_KEY` 和 `X_AUTH_TOKEN` 放入 GitHub Actions Secrets。
- 不建議每小時 commit SQLite；二進位差異會讓 Git 歷史快速膨脹。
- 需要跨 Runner 保存歷史時，使用持久化資料庫或受控的資料庫 artifact。
- 先監控 429、503、登入導向和 `failed` 數量，再設定憑證失效通知。

## 免責聲明

本專案僅供技術研究與資訊整理。情緒、論點與風險由模型根據貼文自動摘要，不保證完整或正確，也不構成投資建議。投資決策前請閱讀原文並獨立查證。
