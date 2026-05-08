# JKK Monitor 🏠

Monitors JKK Tokyo (jkk.go.jp) for new apartment vacancies every 5 minutes
and sends Slack notifications for listings matching your criteria.

---

## Quick Start

### 1. Install dependencies

```bash
cd jkk_monitor
pip install -r requirements.txt
```

### 2. Set your Slack Webhook URL

Get one at: https://api.slack.com/messaging/webhooks

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
```

To make it permanent, add the line above to your `~/.zshrc` or `~/.bashrc`.

### 3. Start the config API server (Terminal 1)

```bash
python server.py
```

This runs on http://localhost:5050

### 4. Open the dashboard

Open `dashboard.html` in your browser. Set your filters, click **設定を保存**.

### 5. Start the scraper (Terminal 2)

```bash
python scraper.py
```

It will check JKK every 5 minutes and ping Slack when a match is found.

---

## Filters (set in dashboard)

| Filter          | Description                              |
|-----------------|------------------------------------------|
| Slack Webhook   | Your Slack incoming webhook URL          |
| エリア（区）    | Tokyo wards to watch (blank = all)       |
| 最低家賃        | Minimum rent in yen (0 = no limit)       |
| 上限家賃        | Maximum rent in yen (0 = no limit)       |
| 間取り          | Room layouts e.g. 1LDK, 2LDK            |
| 最低面積        | Minimum floor area in m²                 |

Config is saved to `config.json` and reloaded each scrape cycle —
no restart needed when you change filters.

---

## Auto-start on Mac (launchd)

Create `~/Library/LaunchAgents/com.jkk.monitor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>             <string>com.jkk.monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/PATH/TO/jkk_monitor/scraper.py</string>
  </array>
  <key>WorkingDirectory</key>  <string>/PATH/TO/jkk_monitor</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SLACK_WEBHOOK_URL</key>
    <string>https://hooks.slack.com/services/XXX/YYY/ZZZ</string>
  </dict>
  <key>RunAtLoad</key>         <true/>
  <key>KeepAlive</key>         <true/>
  <key>StandardOutPath</key>   <string>/tmp/jkk_monitor.log</string>
  <key>StandardErrorPath</key> <string>/tmp/jkk_monitor.err</string>
</dict>
</plist>
```

Then: `launchctl load ~/Library/LaunchAgents/com.jkk.monitor.plist`

---

## Notes on JKK selectors

The scraper uses CSS selectors that match JKK Tokyo's listing page structure
as of early 2026. If the site redesigns, update the selectors in `scraper.py`
inside `fetch_listings()` and `parse_card()`.

The main listings URL being monitored:
  https://www.to-kousya.or.jp/chintai/list/index.html

---

## File structure

```
jkk_monitor/
├── scraper.py          ← main monitor loop
├── server.py           ← config API (Flask)
├── dashboard.html      ← browser dashboard
├── config.json         ← your filter settings
├── seen_listings.json  ← tracks already-seen listings
├── jkk_monitor.log     ← runtime log
└── requirements.txt
```
