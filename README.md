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
  with `docker.sock` mounted, so it can trigger this container's `job-run`
  on a schedule. This repo does not deploy Ofelia itself.
- **rclone**, configured for Google Drive once, with the resulting
  `rclone.conf` placed next to `docker-compose.yml` on the host (bind-mounted
  read-only into the container - never committed to this repo). Generating
  that file requires a one-time interactive OAuth consent (`rclone config`,
  or `rclone authorize "drive"` if doing it headless and pasting the token
  back in) - this can't be automated from inside the container.

  Use the **full `drive` scope** (not `drive.file`) when setting up the
  remote, so it can see and write into folders that already exist in your
  Drive rather than only ones it created itself.

  The destination folder is pinned by Google Drive folder ID rather than by
  path, via the `RCLONE_GDRIVE_ROOT_FOLDER_ID` stack env var below (see
  [Environment variables](#environment-variables)) - so the folder can be
  freely renamed or moved in Drive afterwards without breaking anything, as
  long as the ID itself stays the same. To get a folder's ID: open it in the
  Drive web UI and copy the string after `/folders/` in the URL, e.g.
  `https://drive.google.com/drive/folders/<this-part>`.

  This env var is Google Drive-specific by design - see
  [Adding another rclone destination](#adding-another-rclone-destination)
  below for how to point this at somewhere other than Drive.

## Environment variables

Set as Portainer stack environment variables (never committed here):

| Variable | Required | Notes |
|---|---|---|
| `ISP_USERNAME` | yes | 4th Utility account email |
| `ISP_PASSWORD` | yes | 4th Utility account password |
| `RCLONE_REMOTE` | no (default `gdrive`) | name of the remote in `rclone.conf` |
| `RCLONE_GDRIVE_ROOT_FOLDER_ID` | no (default blank - remote falls back to `root_folder_id` in `rclone.conf`, or the real Drive root if neither is set) | Google Drive folder ID to sync into, only meaningful when `RCLONE_REMOTE` is a `drive`-type remote - see [Requirements on the host](#requirements-on-the-host) above |
| `RCLONE_PATH` | no (default blank - syncs directly into the folder above) | subfolder path relative to `RCLONE_GDRIVE_ROOT_FOLDER_ID` (or to whatever remote root you've configured), only needed for further nesting |
| `HA_URL` | no | e.g. `http://<your-home-assistant-host>:8123`, enables the notification |
| `HA_TOKEN` | no | Home Assistant long-lived access token |
| `HA_NOTIFY_SERVICE` | no (default `persistent_notification/create`) | any HA service path, e.g. `notify/mobile_app_<yourname>` |
| `HA_NOTIFY_ON_SUCCESS` | no (default `true`) | send an HA notification when a new bill is found |
| `HA_NOTIFY_ON_ERROR` | no (default `true`) | send an HA notification when a run fails |
| `BOOKSTACK_URL` | no | BookStack base URL, e.g. `http://<your-bookstack-host>:6875` - enables the wiki run-history update |
| `BOOKSTACK_TOKEN_ID` | no | API Token ID for the BookStack service account (see [Wiki run history](#wiki-run-history)) |
| `BOOKSTACK_TOKEN_SECRET` | no | API Token Secret for the same account |
| `BOOKSTACK_PAGE_ID` | no | ID of the wiki page to append run history to (see [Wiki run history](#wiki-run-history)) |

## Ofelia job

Once this stack is deployed, add a job to Ofelia's config pointing at this
container, e.g. in `config.ini`:

```ini
[job-run "4thu-bill-check"]
schedule = @weekly
image = 4thu-bill-downloader:latest
volume = /data/compose/<stack-id>/data:/app/data
volume = /data/compose/<stack-id>/logs:/app/logs
volume = /data/compose/<stack-id>/rclone.conf:/root/.config/rclone/rclone.conf:ro
```

(`<stack-id>` is whatever numeric ID Portainer assigned this stack - check the stack's URL, or its files under `/data/compose/` on the host. No `network =` line is needed here: a `job-run` container runs standalone and doesn't need to share a network namespace with anything.)

Billing is monthly (around the 11th), checking weekly just means
most weeks are a no-op.

## Adding another rclone destination

`RCLONE_GDRIVE_ROOT_FOLDER_ID` is the only backend-specific variable this
project exposes as a Portainer stack env var, because Google Drive is the
only remote it's actually been used with. If you want to sync somewhere else
too (a NAS, SFTP, S3, Dropbox, whatever `rclone` supports), there's no need
to add more env vars or touch this repo's code - configure the new remote
directly on the host, the same way you'd set up `rclone` for yourself
outside of any of this:

1. Run `rclone --config /path/to/this/stack/rclone.conf config` on the host
   and add a new remote (`n`), give it a name (e.g. `nas`), and follow that
   backend's own prompts - each backend asks for whatever it needs (host,
   user, key, bucket, etc.), which is why this doesn't get its own generic
   env var here.
2. Set the stack's `RCLONE_REMOTE` env var to that new remote's name to
   switch the running container over to it, or duplicate this stack
   (different container name, same image) if you want both destinations
   syncing independently rather than replacing one with the other.
3. If that backend needs something equivalent to Drive's root folder ID,
   just bake it into `rclone.conf` for that remote directly (most backends
   take a root path or equivalent as a plain config field during setup) -
   only Drive's version of this got promoted to a stack env var, because it
   was already changing size, but for a personal setup shared with one
   other person, one more line in a config file is simpler than another
   layer of env var plumbing.

## Wiki run history

Every run - success, no-op, or failure - appends a row to the "Run history"
table on the configured `BOOKSTACK_PAGE_ID` page in your wiki, so there's an
all-time record in one place without relying on container logs alone. The
script talks to BookStack's REST API directly using its own credentials
(`BOOKSTACK_TOKEN_ID` / `BOOKSTACK_TOKEN_SECRET`).

This needs a dedicated BookStack service-account user, set up once by hand
(there's no API for creating users):

1. In BookStack, go to **Settings → Users → Invite/Add a user**. Give it a
   name like `4thu-bill-downloader`, a throwaway email/password (it'll only
   ever authenticate via API token), and the **Editor** role.
2. Restrict it to just the "4th Utility" book: on that book's page, use
   **Permissions** to set custom permissions for this user (view/create/edit
   on that book only) rather than leaving it with wiki-wide Editor access.
3. Log in as that user (or use an admin's "Manage Users" impersonate option),
   go to **Edit Profile → API Tokens → Create Token**, and copy the **Token
   ID** and **Token Secret** it shows you - the secret is only ever shown
   once.
4. Create a page (e.g. "Bill Download Log") under that book. Get its
   numeric ID from the page's **Permalink** option (shown as
   `.../link/<id>`) or via `GET /api/pages` on the BookStack API - the
   normal page URL is slug-based and won't show it. Set `BOOKSTACK_PAGE_ID`
   to that value along with `BOOKSTACK_URL` and the token pair as this
   stack's env vars.

If any of the three `BOOKSTACK_*` variables are left blank, the script skips
the wiki update entirely (same pattern as the optional HA notification) -
nothing else about the run is affected.

## Local layout

```
data/    downloaded bill PDFs (gitignored, lives on the host)
logs/    download_bills.log (gitignored, lives on the host)
```

On any run failure (login or otherwise), a screenshot, page source, browser
console log, and network trace are saved to `logs/failure_<timestamp>.*` and
also pushed to `<RCLONE_PATH>/failure-logs/` on the configured rclone remote
(when `RCLONE_REMOTE` is set), so a scheduled-run failure is diagnosable
without needing host access to pull the files yourself.

## Future enhancements

- **Reduce the automated-browser fingerprint further.** A scheduled run
  failed with "Login did not redirect away from /login" while a manual
  login on the same account worked fine moments later - the leading
  suspect is bot detection on the ISP's login flow scoring the automation
  fingerprint (Selenium's default `navigator.webdriver = true` flag,
  plus headless Chrome's other detectable quirks like WebGL renderer
  strings and missing plugins). A stale spoofed User-Agent contributing
  to that fingerprint has already been fixed (see `build_driver()`).
  Remaining options, roughly in order of impact:
  - Patch out `navigator.webdriver` (e.g. via a CDP
    `Page.addScriptToEvaluateOnNewDocument` call before navigation).
  - Run headed inside a virtual display (Xvfb/VNC, already supported by
    the `selenium/standalone-chromium` base image) instead of
    `--headless=new`, since headless mode is itself a detectable signal
    on some sites.
