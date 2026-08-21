# 4thu-bill-downloader

Scheduled downloader for 4th Utility bills, run via Selenium on Portainer/Ofelia.

Logs into `my.4thutility.co.uk`, walks the bills table, downloads any invoice
PDF not already saved locally (as `YYYY-MM-4thUBill.pdf`), syncs new bills to
Google Drive via `rclone`, and optionally pings Home Assistant when a new
bill shows up. Every run logs a line, including "checked, nothing new" weeks,
so there's a visible history in `logs/download_bills.log`.

Designed as a **one-shot container**, not a long-running service - it's meant
to be triggered on a schedule by [Ofelia](https://github.com/mcuadros/ofelia)
rather than kept resident.

## Requirements on the host

- **Ofelia** must already be running as its own standalone Portainer stack
  (split out from the media-stack's `media-automation` stack) with `docker.sock`
  mounted, so it can trigger this container's `job-run` on a schedule. This
  repo does not deploy Ofelia itself.
- **rclone**, configured for Google Drive once, with the resulting
  `rclone.conf` placed next to `docker-compose.yml` on the host (bind-mounted
  read-only into the container - never committed to this repo). Generating
  that file requires a one-time interactive OAuth consent (`rclone config`,
  or `rclone authorize "drive"` if doing it headless and pasting the token
  back in) - this can't be automated from inside the container.

## Environment variables

Set as Portainer stack environment variables (never committed here):

| Variable | Required | Notes |
|---|---|---|
| `ISP_USERNAME` | yes | 4th Utility account email |
| `ISP_PASSWORD` | yes | 4th Utility account password |
| `RCLONE_REMOTE` | no (default `gdrive`) | name of the remote in `rclone.conf` |
| `RCLONE_PATH` | no (default `4th Utility Bills`) | destination folder on the remote |
| `HA_URL` | no | e.g. `http://<your-home-assistant-host>:8123`, enables the notification |
| `HA_TOKEN` | no | Home Assistant long-lived access token |
| `HA_NOTIFY_SERVICE` | no (default `persistent_notification/create`) | any HA service path, e.g. `notify/mobile_app_<yourname>` |

## Ofelia job

Once this stack is deployed, add a job to Ofelia's config pointing at this
container, e.g. in `config.ini`:

```ini
[job-run "4thu-bill-check"]
schedule = @weekly
image = 4thu-bill-downloader:latest
network = 4thu-bill-downloader_default
volume = /root/4thu-bill-downloader/data:/app/data
volume = /root/4thu-bill-downloader/logs:/app/logs
volume = /root/4thu-bill-downloader/rclone.conf:/root/.config/rclone/rclone.conf:ro
```

(Exact volume paths depend on where the stack ends up on the host - adjust
to match.) Billing is monthly (around the 11th), checking weekly just means
most weeks are a no-op.

## Local layout

```
data/    downloaded bill PDFs (gitignored, lives on the host)
logs/    download_bills.log (gitignored, lives on the host)
```
