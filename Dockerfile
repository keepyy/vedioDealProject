FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV SANDBOX_ENABLED=false
ENV STORAGE_DIR=/app/storage
ENV DISABLE_HWACCEL=1
ENV PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p storage/raw storage/segments storage/final storage/frames

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT}"]
