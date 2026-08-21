FROM golang:1.26 AS gobuilder

WORKDIR /build
COPY fast_scraper_go/ .
RUN go build -o fast_scraper_go .

FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV SELENIUM_HEADLESS=1
ENV FAST_MODE=1
ENV ENABLE_HTTP_FAST_PATH=1
ENV PAGE_LOAD_STRATEGY=eager
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY --from=gobuilder /build/fast_scraper_go /app/fast_scraper_go
RUN chmod +x /app/fast_scraper_go/fast_scraper_go && echo '{}' > /app/fast_scraper_go/session_cookies.json

EXPOSE 8080

CMD ["python", "telegram_bot.py"]
# rebuilt to invalidate render cache