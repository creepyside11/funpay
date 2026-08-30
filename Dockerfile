FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py plugin_system.py playerok_plugin_system.py telethon_plugin.py cardinal.py \
    PLUGIN_DEVELOPMENT.md PLAYEROK_PLUGIN_DEVELOPMENT.md ./
COPY FunPayAPI ./FunPayAPI
COPY playerokapi ./playerokapi
COPY telebot ./telebot
COPY tg_bot ./tg_bot

RUN useradd --create-home --uid 10001 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
