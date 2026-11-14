"# Void-Labs" 
"# Void-Labs" 

## Monitor (admin)

`/admin/ops` checks Donut, Pixel and the main site every minute and shows response time, 24 h uptime, a 60-check strip and an incident log. A site counts as down after two failed checks in a row.

- **Auto status:** set a product's public status to *Auto* (in Settings or with the buttons on the Monitor page) and the site follows the monitor. Maintenance stays manual.
- **Discord alerts:** set `DISCORD_WEBHOOK_URL` to get a message when a site goes down or recovers.
- **Other settings:** `MONITOR_INTERVAL` (seconds, default 60, `0` = off), `MONITOR_URLS` (override targets, e.g. `donut=https://...,pixel=https://...`).
- History is kept in `monitor.json` next to `main.py`.

## Announcement, notes, inbox, backup (admin)

- **Banner:** Settings → Announcement. A bar at the top of the site, hidden when empty.
- **Status notes:** a short line under Donut or Pixel, e.g. "Back around 18:00 UTC".
- **Contact form + Inbox:** the site has a contact form. Messages land in `/admin/inbox` (unread badge, mark read, delete) and ping Discord if `DISCORD_WEBHOOK_URL` is set. Spam guards: hidden honeypot field, signed form token, 3 messages per IP per hour. Stored in `messages.json` (or `MESSAGES_FILE`).
- **Backup:** Settings → Download backup gives a JSON of settings and messages.
