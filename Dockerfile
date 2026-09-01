FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV SANDBOX_ENABLED=false
ENV STORAGE_DIR=/app/storage
ENV DISABLE_HWACCEL=1
ENV PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ca-certificates curl ffmpeg fonts-noto-cjk libgl1 libglib2.0-0 \
    && curl -fsSLO https://issuecdn.baidupcs.com/issue/netdisk/ai-bdpan/installer/3.8.4/bdpan-installer-linux-amd64 \
    && echo '02050e9a5ed5c5ddc314bf920c103238a669366a130e3bd43a125d83fdd00548  bdpan-installer-linux-amd64' | sha256sum -c - \
    && chmod +x bdpan-installer-linux-amd64 \
    && ./bdpan-installer-linux-amd64 --yes \
    && install -m 0755 /root/.local/bin/bdpan /usr/local/bin/bdpan \
    && rm -f bdpan-installer-linux-amd64 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p storage/raw storage/segments storage/final storage/frames

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT}"]
