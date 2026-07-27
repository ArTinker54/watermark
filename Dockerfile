FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# opencv-python-headless не тянет GUI, но всё ещё линкуется с glib и libgomp,
# которых в slim-образе нет.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# blind-watermark ставится отдельно с --no-deps: иначе он подтянет
# opencv-python с GUI и перекроет headless-сборку (см. requirements.txt).
COPY requirements.txt requirements-wm.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt \
 && pip install --no-deps -r requirements-wm.txt

COPY pyproject.toml ./
COPY app ./app

# Данные живут в томе, а не в образе.
RUN useradd --create-home --uid 1000 bot \
 && mkdir -p /data/storage \
 && chown -R bot:bot /data /app
USER bot

ENV DB_PATH=/data/db.sqlite3 \
    STORAGE_PATH=/data/storage

# Конкретный бот выбирается командой в docker-compose.
CMD ["python", "-m", "app.bots.student"]
