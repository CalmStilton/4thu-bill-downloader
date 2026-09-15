#!/usr/bin/env python3
"""
4thU Bill Downloader
---------------------
Logs into my.4thutility.co.uk, walks the /bills table, downloads any invoice
PDF that hasn't already been saved locally, pushes new bills to Google Drive
via rclone, and (optionally) fires a Home Assistant notification when a new
bill is found. Always logs a line so a run's outcome is visible later, even
when nothing new was found.

Designed to run as a one-shot container triggered by Ofelia (job-run), not
as a long-lived service.
"""

import json
import os
import re
import sys
import time
import subprocess
from datetime import datetime

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

LOGIN_URL = "https://my.4thutility.co.uk/login"
BILLS_URL = "https://my.4thutility.co.uk/bills"

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")
LOG_FILE = os.path.join(LOG_DIR, "download_bills.log")

ISP_USERNAME = os.environ.get("ISP_USERNAME")
ISP_PASSWORD = os.environ.get("ISP_PASSWORD")

RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "").strip()       # e.g. "gdrive"
RCLONE_PATH = os.environ.get("RCLONE_PATH", "4th Utility Bills")  # folder on the remote

HA_URL = os.environ.get("HA_URL", "").strip()                     # e.g. "http://<your-home-assistant-host>:8123"
HA_TOKEN = os.environ.get("HA_TOKEN", "").strip()                 # long-lived access token
HA_NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "persistent_notification/create")


def _bool_env(name, default=True):
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


HA_NOTIFY_ON_SUCCESS = _bool_env("HA_NOTIFY_ON_SUCCESS", True)
HA_NOTIFY_ON_ERROR = _bool_env("HA_NOTIFY_ON_ERROR", True)

BOOKSTACK_URL = os.environ.get("BOOKSTACK_URL", "").strip()
BOOKSTACK_TOKEN_ID = os.environ.get("BOOKSTACK_TOKEN_ID", "").strip()
BOOKSTACK_TOKEN_SECRET = os.environ.get("BOOKSTACK_TOKEN_SECRET", "").strip()
BOOKSTACK_PAGE_ID = os.environ.get("BOOKSTACK_PAGE_ID", "").strip()

ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)", re.IGNORECASE)


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def build_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1400,1200")
    # No --user-agent override here on purpose: a hardcoded UA drifts out of
    # sync with whatever Chromium version the base image actually bundles
    # (it floats on :latest), and a UA that claims an older version than the
    # real one is a textbook bot-detection signal on sites with WAF/bot
    # management - let Chromium report its own real UA instead.
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
    return webdriver.Chrome(options=options)


def login(driver):
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, 20)

    # Explicit waits on every element we interact with, not just the first -
    # the page's HTML can arrive before its JS has finished hydrating and
    # attaching click handlers, and a click that lands in that window
    # succeeds at the WebDriver level but silently does nothing.
    email_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'], input[type='text']")))
    password_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='password']")))

    email_input.clear()
    email_input.send_keys(ISP_USERNAME)
    password_input.clear()
    password_input.send_keys(ISP_PASSWORD)

    submit_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'Submit')]")))
    submit_btn.click()

    try:
        wait.until(lambda d: "/login" not in d.current_url)
    except Exception as exc:
        raise RuntimeError(
            "Login did not redirect away from /login - credentials may be wrong "
            "or the login page structure has changed."
        ) from exc


def save_failure_artifacts(driver):
    """Screenshot, page source, browser console log, and network trace at
    the moment of failure, so the next unexpected error is diagnosable from
    the logs instead of a guess. Returns the list of files written."""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = []
    try:
        screenshot_path = os.path.join(LOG_DIR, f"failure_{timestamp}.png")
        driver.save_screenshot(screenshot_path)
        paths.append(screenshot_path)

        html_path = os.path.join(LOG_DIR, f"failure_{timestamp}.html")
        with open(html_path, "w") as f:
            f.write(driver.page_source)
        paths.append(html_path)

        console_path = os.path.join(LOG_DIR, f"failure_{timestamp}_console.log")
        with open(console_path, "w") as f:
            for entry in driver.get_log("browser"):
                f.write(f"[{entry['level']}] {entry['message']}\n")
        paths.append(console_path)

        network_path = os.path.join(LOG_DIR, f"failure_{timestamp}_network.json")
        with open(network_path, "w") as f:
            json.dump(driver.get_log("performance"), f, indent=2)
        paths.append(network_path)

        log(f"Saved failure artifacts: {', '.join(os.path.basename(p) for p in paths)}")
    except Exception as exc:
        log(f"WARNING: could not save failure artifacts: {exc}")
    return paths


def upload_failure_artifacts_to_drive(paths):
    if not paths:
        return
    if not RCLONE_REMOTE:
        log("RCLONE_REMOTE not set, skipping failure-artifact upload")
        return
    dest = f"{RCLONE_REMOTE}:{RCLONE_PATH}/failure-logs"
    failures = 0
    for path in paths:
        result = subprocess.run(
            ["rclone", "copyto", path, f"{dest}/{os.path.basename(path)}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            failures += 1
            log(f"WARNING: failed to upload {os.path.basename(path)} to Drive: {result.stderr.strip()}")
    uploaded = len(paths) - failures
    if uploaded:
        log(f"Uploaded {uploaded}/{len(paths)} failure artifact(s) to Drive ({dest})")


def parse_bill_date(text):
    """'11th August 2026' -> datetime(2026, 8, 11)"""
    cleaned = ORDINAL_RE.sub(r"\1", text).strip()
    return datetime.strptime(cleaned, "%d %B %Y")


def hook_window_open(driver):
    driver.execute_script(
        "window.__opens = []; "
        "window.open = function(...args) { window.__opens.push(args); return null; };"
    )


def last_opened_url(driver):
    opens = driver.execute_script("return window.__opens")
    driver.execute_script("window.__opens = [];")
    if not opens:
        return None
    return opens[-1][0]


def collect_invoice_rows(driver):
    """Return list of (reference, bill_date, row_element) for Invoice rows."""
    wait = WebDriverWait(driver, 20)
    wait.until(EC.presence_of_element_located((By.XPATH, "//td[contains(text(), 'Invoice #')]")))

    rows = driver.find_elements(By.XPATH, "//tr[td[contains(text(), 'Invoice #')]]")
    invoices = []
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 4:
            continue
        date_text = cells[0].text.strip()
        ref_text = cells[3].text.strip()  # Reference column, e.g. INV1402505
        if not ref_text.startswith("INV"):
            continue
        try:
            bill_date = parse_bill_date(date_text)
        except ValueError:
            log(f"WARNING: could not parse date '{date_text}' for {ref_text}, skipping")
            continue
        invoices.append((ref_text, bill_date, row))
    return invoices


def session_from_driver(driver):
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain", "my.4thutility.co.uk"))
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session


def download_invoice(driver, session, row, dest_path):
    hook_window_open(driver)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
    driver.execute_script("arguments[0].click();", row)

    url = None
    for _ in range(10):
        url = last_opened_url(driver)
        if url:
            break
        time.sleep(0.3)

    if not url:
        raise RuntimeError("Clicking the invoice row did not produce a download URL")

    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "pdf" not in content_type.lower():
        raise RuntimeError(f"Expected a PDF but got content-type '{content_type}'")

    with open(dest_path, "wb") as f:
        f.write(resp.content)


def sync_to_drive():
    if not RCLONE_REMOTE:
        log("RCLONE_REMOTE not set, skipping Google Drive sync")
        return
    result = subprocess.run(
        ["rclone", "copy", DATA_DIR, f"{RCLONE_REMOTE}:{RCLONE_PATH}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log(f"WARNING: rclone sync to Drive failed: {result.stderr.strip()}")
    else:
        log("Synced data folder to Google Drive")


def notify_home_assistant(new_bills):
    if not (HA_URL and HA_TOKEN):
        return
    filenames = ", ".join(b["filename"] for b in new_bills)
    payload = {
        "title": "New 4th Utility bill",
        "message": f"Downloaded: {filenames}",
    }
    _post_to_ha(payload)


def notify_home_assistant_error(error):
    if not (HA_URL and HA_TOKEN):
        return
    payload = {
        "title": "4th Utility bill check failed",
        "message": str(error)[:200],
    }
    _post_to_ha(payload)


def _post_to_ha(payload):
    try:
        resp = requests.post(
            f"{HA_URL.rstrip('/')}/api/services/{HA_NOTIFY_SERVICE}",
            headers={"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        log(f"WARNING: Home Assistant notification failed: {exc}")


def drive_folder_link():
    """Link to the pinned Drive destination folder, if we're syncing to one."""
    folder_id = os.environ.get("RCLONE_CONFIG_GDRIVE_ROOT_FOLDER_ID", "").strip()
    if not folder_id:
        return None
    return f"https://drive.google.com/drive/folders/{folder_id}"


def build_history_line(new_bills, error):
    date_str = datetime.now().strftime("%Y-%m-%d")
    if error:
        status = "Error"
        message = str(error).replace("|", "/")[:150]
        # Links to the pinned Drive root, not the failure-logs/ subfolder
        # itself - rclone creates that subfolder on demand and we never
        # learn its own Drive folder ID, so this is one click short of a
        # direct deep link.
        link = drive_folder_link()
        detail = f"{message} — [failure-logs]({link})" if link else message
    elif new_bills:
        status = "New bill downloaded"
        names = ", ".join(f"{b['filename']}`" for b in new_bills)
        link = drive_folder_link()
        detail = f"[Drive folder]({link}) — {names}" if link else names
    else:
        status = "Checked, nothing new"
        detail = "-"
    return f"| {date_str} | {status} | {detail} |"


def update_bookstack_log(status_line):
    """Prepend a row to the Run history table on the wiki page. Reads the
    current page, inserts the new row under the table header, and writes the
    whole page back - BookStack's API has no partial-content update, so this
    keeps everything else on the page untouched by round-tripping the full
    markdown."""
    if not (BOOKSTACK_URL and BOOKSTACK_TOKEN_ID and BOOKSTACK_TOKEN_SECRET and BOOKSTACK_PAGE_ID):
        return

    base = BOOKSTACK_URL.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    headers = {
        "Authorization": f"Token {BOOKSTACK_TOKEN_ID}:{BOOKSTACK_TOKEN_SECRET}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(f"{base}/api/pages/{BOOKSTACK_PAGE_ID}", headers=headers, timeout=15)
        resp.raise_for_status()
        content = resp.json().get("markdown", "")

        lines = content.splitlines()
        header_idx = next(
            (i for i, line in enumerate(lines)
             if line.strip().startswith("|") and "Date" in line and "Status" in line),
            None,
        )
        if header_idx is None:
            log("WARNING: BookStack update skipped - run history table header not found on page")
            return

        insert_idx = header_idx + 2  # past the header row and its "|---|---|---|" separator
        new_lines = lines[:insert_idx] + [status_line]
        rest = [l for l in lines[insert_idx:] if "no runs logged yet" not in l]
        new_content = "\n".join(new_lines + rest)

        put_resp = requests.put(
            f"{base}/api/pages/{BOOKSTACK_PAGE_ID}",
            headers=headers,
            json={"markdown": new_content},
            timeout=15,
        )
        put_resp.raise_for_status()
    except Exception as exc:
        log(f"WARNING: BookStack page update failed: {exc}")


def main():
    if not (ISP_USERNAME and ISP_PASSWORD):
        log("ERROR: ISP_USERNAME / ISP_PASSWORD not set, aborting")
        sys.exit(1)

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    driver = None
    new_bills = []
    error = None
    try:
        driver = build_driver()
        login(driver)
        driver.get(BILLS_URL)
        invoices = collect_invoice_rows(driver)
        session = session_from_driver(driver)

        for reference, bill_date, row in invoices:
            filename = f"{bill_date.year:04d}-{bill_date.month:02d}-4thUBill.pdf"
            dest_path = os.path.join(DATA_DIR, filename)
            if os.path.exists(dest_path):
                continue
            try:
                download_invoice(driver, session, row, dest_path)
                log(f"Downloaded new bill: {filename} ({reference})")
                new_bills.append({"filename": filename, "reference": reference})
            except Exception as exc:
                log(f"ERROR: failed to download {reference} ({filename}): {exc}")

        if new_bills:
            sync_to_drive()
            if HA_NOTIFY_ON_SUCCESS:
                notify_home_assistant(new_bills)
        else:
            log("Checked, nothing new.")

    except Exception as exc:
        error = exc
        log(f"ERROR: run failed: {exc}")
        if driver is not None:
            artifact_paths = save_failure_artifacts(driver)
            upload_failure_artifacts_to_drive(artifact_paths)
    finally:
        if driver is not None:
            driver.quit()

    update_bookstack_log(build_history_line(new_bills, error))

    if error:
        if HA_NOTIFY_ON_ERROR:
            notify_home_assistant_error(error)
        sys.exit(1)


if __name__ == "__main__":
    main()
