FROM python:3.11-slim

# Install Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
RUN mkdir -p /opt/render/.cache/ms-playwright && playwright install chromium

COPY . .

EXPOSE 10000

CMD ["gunicorn", "webhook_server:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--timeout", "120"]
