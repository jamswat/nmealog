FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NMEALOG_DATA_DIR=/data \
    NMEALOG_WEB_BIND=0.0.0.0:8000 \
    TZ=UTC

RUN useradd --system --uid 10001 --no-create-home nmealog \
 && mkdir /data \
 && chown nmealog:nmealog /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nmealog ./nmealog

USER nmealog
EXPOSE 8000

CMD gunicorn --bind "$NMEALOG_WEB_BIND" --workers 1 --threads 8 \
    --timeout 300 --access-logfile - "nmealog.web:create_app()"
