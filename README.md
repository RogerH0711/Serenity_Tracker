# Serenity Tracker

Serenity Tracker 會定期抓取指定 X 帳號近期貼文，從明示的 cashtag（例如 `$NVDA`）提取半導體與 AI 供應鏈觀點，並生成不需要後端伺服器的靜態儀表板。

![Serenity Tracker 月度股票儀表板](docs/images/dashboard.png)

## 功能畫面

### 個股研究摘要與人工覆核

![MU 個股研究摘要與人工覆核](docs/images/ticker-research.png)

### 股價、提及標記與績效

![NVDA 股價、提及標記與績效](docs/images/market-performance.png)

### 有原文來源的本機 AI 問答

![MU 與 SHKY 投資論點比較](docs/images/ai-question.png)

## 設計重點

- 以 X status ID 作為穩定的 `post_id`，不會因相同貼文再次出現在頁面上而重複處理。
- Gemini 只解析新貼文或先前失敗的貼文，避免重複消耗 API 配額。
- SQLite 強制 `UNIQUE(post_id, ticker)`，重跑整條管線也不會產生重複歷史紀錄。
- Gemini 使用相容的結構化輸出與本地 Pydantic 驗證；ticker 只能來自原文明示的 `$CASHTAG`，常見 `$100B`、`$3.4M` 等金額會被排除。
- 主貼文與引用貼文脈絡分開保存；情緒代表作者對股票的投資立場，不會只因 `bottleneck`、`shortage` 等負面字眼就判為 Bearish。
- 每個立場必須附上輸入原文中的逐字證據，本地驗證找不到該證據時拒絕寫入；方向不明時使用 Neutral。
- 每筆分析會顯示可信度：人工覆核、證據已核對、舊資料未驗證或待覆核；有逐字證據不等於解讀一定正確，仍應閱讀原文。
- 人工修正獨立存放在 `mention_overrides`，建站時優先於模型結果，Gemini 重新解析也不會覆蓋人工決定。
- 每次執行會保存抓取、長文補抓、解析、語意摘要與建站健康狀態，網站可辨識正常、部分失敗及資料過期。
- 個股語意摘要使用內容指紋增量更新；沒有新貼文時不呼叫 Gemini，失敗時保留上一版或回退規則式整理。
- 新 ticker alias 只會進入待審核清單，必須人工核准後才會更新 `ticker_aliases.json`。
- 重新解析失敗時保留最後一版成功分析；程式設定造成的 4xx 會立即停止管線，不會誤報成功。
- 每階段採原子檔案替換；爬蟲失敗時不會讓下游誤用舊 JSON。
- 靜態頁面使用 DOM `textContent` 渲染外部資料，不使用 `innerHTML`。
- 儀表板以資料集最新日期為截止日，提供日、7 天、28 天、90 天四種滾動視圖；`Daily` 不會再混入舊股票。
- 搜尋、情緒、風險與排序狀態會寫入網址；收藏與最近查看只保存在目前裝置的瀏覽器中。
- `ticker_aliases.json` 是可人工審核、可進版控的股票代碼合併規則，避免同一家公司因市場後綴或 OTC 代碼被拆成多張卡。
- 調整後日線、提及後 1／7／30／90 日績效與多空標記只快取在本機 SQLite，不會把行情資料發布到 GitHub Pages。
- 本機 AI 問答只讀取已收錄的研究內容，模型引用未知 `post_id` 時會拒絕回答；相同問題與相同資料會使用快取，避免重複消耗額度。

## 資料流

```text
X profile
  └─ scraper.py ──> raw_tweets.json (atomic)
                         │
                         └─ parser.py ──> posts + mentions (SQLite)
                                                │
                                                ├─ summarize.py ──> ticker_snapshots
                                                ├─ alias_review.py scan ──> alias candidates
                                                ├─ prices.py ──> adjusted prices (local SQLite)
                                                └─ build_site.py ──> index.html (atomic)
```

SQLite 的主要資料表：

```text
posts(post_id PK, timestamp, text, context, url, parse_status, analysis_version, ...)
  └─ mentions(post_id FK, ticker, sentiment, sentiment_evidence, thesis, risks, ...)
       UNIQUE(post_id, ticker)
  └─ mention_overrides(post_id, ticker, sentiment, evidence, thesis, risks, review_note, ...)
       PRIMARY KEY(post_id, ticker)
pipeline_runs(run metrics, status, failed_stage, timestamps, ...)
ticker_snapshots(ticker PK, source_fingerprint, semantic summary, source ids, ...)
ticker_alias_candidates(canonical_ticker, alias, confidence, review status, ...)
ticker_price_profiles(ticker PK, provider symbol, currency, refresh status, ...)
market_prices(ticker, price_date, adjusted_close, fetched_at)
qa_cache(question hash PK, context fingerprint, cited answer, hit count, ...)
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
SUMMARY_MAX_TICKERS=2
PRICE_MAX_TICKERS=5
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
4. 只更新內容有變化的個股語意摘要。
5. 掃描新的 ticker alias 候選。
6. 增量更新最多五組本機調整後日線行情。
7. 從資料庫重新生成 `index.html`。

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

### 本機行情與提及後績效

執行完整管線後，個股頁會多出「股價／績效」分頁。為避免把第三方行情重新發布到公開站點，價格只存在 gitignore 的 `serenity.db`，需從本機研究站查看：

```bash
./review_site.sh
```

分頁提供調整後日線、Bullish／Bearish／Neutral 提及標記，以及從首次提及基準計算的 1、7、30、90 日與至今報酬。可在頁面按「更新行情」，或用 CLI 指定股票：

```bash
venv/bin/python prices.py --ticker SIVE
```

不同市場的 symbol、幣別與 alias 一起放在 `ticker_aliases.json`：

```json
{
  "SIVE": {
    "aliases": ["SIVEF"],
    "exchange": "STO",
    "price_symbol": "SIVE.ST",
    "currency": "SEK"
  }
}
```

若某 ticker 不應抓行情，可把 `price_symbol` 設為 `null`。行情由 `yfinance` 取得；該專案明確標示 Yahoo 資料供個人使用，請勿將本機快取當成可再散布的正式行情源。

### 有來源的本機 AI 問答

規則式查詢仍不呼叫 Gemini。啟動 `./review_site.sh` 且 `.env` 已設定 `GEMINI_API_KEY` 後，搜尋框也能處理需要綜合推理的問題，例如：

```text
為什麼作者目前看多 SIVE？
比較 MU 與 SNDK 的論點差異
哪些風險在不同股票重複出現？
```

問答後端只會提供目前 SQLite 中的論點、風險、立場與來源 ID，不允許模型補外部資訊；每個來源都會顯示成可開啟的 X 原文連結。模型若引用不存在的 ticker／`post_id`，回答不會顯示也不會寫入快取。快取會在相關研究內容改變時自動失效，本機服務另限制每分鐘最多 6 次問答。公開 GitHub Pages 維持無金鑰、無後端的唯讀規則式查詢。

### Ticker alias 管理

在 `ticker_aliases.json` 以 canonical ticker 為鍵，列出需要合併的別名：

```json
{
  "SIVE": {
    "aliases": ["SIVEF"],
    "company_name": "Sivers Semiconductors",
    "exchange": "STO",
    "price_symbol": "SIVE.ST",
    "currency": "SEK"
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

日常使用建議直接啟動本機研究網站（人工覆核、行情與 AI 問答共用同一入口）：

```bash
./review_site.sh
```

瀏覽器開啟 `http://127.0.0.1:8765`，進入個股的「最新摘要」或「完整紀錄」，點選該筆旁邊的「人工覆核」。表單會自動帶入貼文代碼、ticker、立場、論點與風險，不需要手動尋找或輸入 `post_id`。儲存或移除覆核後會直接更新 SQLite 並重新生成 `index.html`。

覆核寫入 API 只監聽本機 `127.0.0.1`，並要求同源及本次啟動的隨機 token。部署在 GitHub Pages 的公開網站會維持唯讀，不會顯示覆核按鈕。使用完畢可在終端按 `Ctrl+C` 關閉。

以下 CLI 保留作為批次處理及除錯備援。先查看模型結果與原文：

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

### Pipeline 健康狀態

`pipeline_runs` 會保存每次執行的抓取數、長文補抓結果、解析成功／失敗、語意摘要更新／失敗、行情更新／失敗、alias 候選數、失敗階段與錯誤分類。X 登入失效及 API 429 限流會分開標示，網站側欄會顯示：

- `資料正常`：最近一次管線完整成功。
- `部分失敗`：長文補抓、解析或語意摘要有部分失敗。
- `管線失敗`：最近一次執行未完成。
- `資料已過期`：最後完成時間距今超過三小時。

純靜態網站無法在失敗發生的當下自行更新；失敗紀錄會在下一次成功建站後出現在公開頁面，本機資料庫則會立即保存。

### 增量個股語意摘要

`summarize.py` 只選擇內容指紋改變或尚未建立摘要的 ticker。每次管線預設最多處理兩組，避免一次消耗大量 Gemini 額度：

```bash
venv/bin/python summarize.py
venv/bin/python summarize.py --ticker SIVE
```

語意摘要會整理目前論點、`new / reinforced / reversed / new_risk / corrected` 演變、三個代表觀點與相近風險。每個結論都必須引用輸入內的 `post_id`；模型回傳未知來源時會拒絕儲存。沒有語意摘要或更新失敗時，網站繼續使用原本的規則式整理。

### Alias 待審核流程

管線只會用保守規則偵測「相同基本代碼＋交易所後綴」及可能的 `F-share`，不會自動合併：

```bash
venv/bin/python alias_review.py scan
venv/bin/python alias_review.py list
venv/bin/python alias_review.py approve 3 --company-name "Company Name" --exchange NASDAQ
venv/bin/python alias_review.py reject 4 --note "不同公司"
```

核准時才會原子更新 `ticker_aliases.json`；拒絕結果留在 SQLite，後續掃描不會再次變成待審核。

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

測試不會連線 X、Yahoo 或 Gemini：

```bash
venv/bin/python -m unittest discover -s tests -v
```

涵蓋舊資料遷移、重複寫入、人工覆核持久性、管線健康紀錄、增量語意摘要、alias 審核、調整後行情快取與提及後績效、AI 引用驗證與問答快取、增量與指定重析、引用脈絡、立場證據驗證、cashtag 擷取，以及靜態 HTML 的 script/XSS escaping。測試使用假行情與假模型，不會連線 X、Yahoo 或 Gemini。

## GitHub Pages 與雲端排程

`index.html` 可以直接發布至 GitHub Pages。若再加入 GitHub Actions：

- 將 `GEMINI_API_KEY` 和 `X_AUTH_TOKEN` 放入 GitHub Actions Secrets。
- 不建議每小時 commit SQLite；二進位差異會讓 Git 歷史快速膨脹。
- 需要跨 Runner 保存歷史時，使用持久化資料庫或受控的資料庫 artifact。
- 先監控 429、503、登入導向和 `failed` 數量，再設定憑證失效通知。

## 免責聲明

本專案僅供技術研究與資訊整理。情緒、論點與風險由模型根據貼文自動摘要，不保證完整或正確，也不構成投資建議。投資決策前請閱讀原文並獨立查證。
