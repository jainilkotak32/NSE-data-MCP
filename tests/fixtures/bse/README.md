These payloads are **hand-built**, not captured from BSE: the environment in which
the BSE server was written had no network access to `bseindia.com`.

- `announcements.json`, `actions.json`, `securities.json`, `result_calendar.json`,
  `sensex.json`: field names and value formats follow the sample responses
  published with the open-source `bse` client (BennyThadikaran/BseIndiaApi,
  `src/samples`). Values are illustrative.
- `results_snapshot.json`: uses the `col1`–`col4`, `resultinCr`/`resultinM`,
  `title`/`v1`–`v3` keys that client reads from `TabResults_PAR`.
- `stock_history.csv`: column names of BSE's historical stock price download,
  including its trailing comma. The endpoint and columns are unconfirmed.
- `index_history.csv`, `index_names.json`: illustrative; the parsers keep
  whatever columns BSE returns.

Replace these with captured responses once the live tests
(`BSE_LIVE_TESTS=1`) have been run against BSE.
