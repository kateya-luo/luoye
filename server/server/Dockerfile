FROM python:3.12-slim
WORKDIR /app
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir \
    -i https://mirrors.cloud.tencent.com/pypi/simple \
    --trusted-host mirrors.cloud.tencent.com \
    -r requirements.txt
COPY server/app ./app
RUN mkdir -p /app/data/transcripts /app/data/summaries /app/data/audio_cache
ENV DATA_DIR=/app/data
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
