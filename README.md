"# Void-Labs" 
"# Void-Labs" 

## Monitor (admin)

`/admin/ops` checks Donut, Pixel and the main site every minute and shows response time, 24 h uptime, a 60-check strip and an incident log. A site counts as down after two failed checks in a row.

- **Auto status:** set a product's public status to *Auto* (in Settings or with the buttons on the Monitor page) and the site follows the monitor. Maintenance stays manual.
- **Discord alerts:** set `DISCORD_WEBHOOK_URL` to get a message when a site goes down or recovers.
- **Other settings:** `MONITOR_INTERVAL` (seconds, default 60, `0` = off), `MONITOR_URLS` (override targets, e.g. `donut=https://...,pixel=https://...`).
- History is kept in `monitor.json` next to `main.py`.
