FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/bot.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY persona.md replies.toml ./
COPY lore ./lore
COPY memories ./memories

RUN useradd --create-home --uid 1000 botuser && mkdir -p /app/data && chown botuser /app/data
USER botuser
VOLUME ["/app/data"]

CMD ["python", "-m", "bot.main"]
