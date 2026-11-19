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

## Status page, updates log, media (admin)

- **`/status`** (public): live state of Donut and Pixel from the monitor, 24 h uptime, recent checks and incidents. A manual Maintenance or Offline setting wins over the monitor.
- **Updates:** `/admin/updates` publishes short dated notes. The latest five show on the home page (section and nav link appear only when there is at least one). Stored in `updates.json` (or `UPDATES_FILE`).
- **Media:** `/admin/media` uploads or removes the Donut and Pixel screenshots (PNG, JPG or WebP, up to 5 MB, checked by file signature). Files are written to `shots/`. If you redeploy from git, keep that folder or re-upload.

## Game prices (admin)

`/admin/prices` edits the Minecraft server's sell worth and shop prices (Oneblock). The game plugin downloads `/prices.json` every few minutes, so edits go live in game without touching the server files.

- **Protect the download:** set `PRICES_KEY` (any long random string). `/prices.json` then needs `Authorization: Bearer <key>` or `?key=<key>`. Put the same key in the plugin config as `economy.prices-key`.
- **Starting values:** `prices.default.json` is used until the first save. Saved prices live in `prices.json` (or `PRICES_FILE`) and are included in Settings → Download backup.
- **Rules the game plugin enforces:** a shop price is raised to at least 3x the item's sell worth; items with no worth sell for 25% of their shop price. The editor warns you when a price is too low.
- Up to 7 shop categories (the in-game menu fits 7).
