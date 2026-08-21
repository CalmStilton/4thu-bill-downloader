# Same base pattern as another-similar-project: a real Selenium/Chromium image
# so browser + matching chromedriver are already correct, rather than
# hand-rolling a chromium install on a generic slim base.
FROM selenium/standalone-chromium:latest

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
        rclone \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY download_bills.py .

RUN mkdir -p /app/data /app/logs

# rclone config (rclone.conf, containing the Google Drive OAuth token) is
# bind-mounted in at runtime from the host - see docker-compose.yml. It is
# never baked into the image.
ENV RCLONE_CONFIG=/root/.config/rclone/rclone.conf

ENTRYPOINT ["python3", "/app/download_bills.py"]
