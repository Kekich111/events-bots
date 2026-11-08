FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY core ./core
COPY tg_bot.py vk_bot.py scheduler.py ./

# База хранится в /app/data — туда монтируется постоянный том
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Команда задаётся в docker-compose.yml для каждого сервиса
CMD ["python", "tg_bot.py"]
