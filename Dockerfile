FROM python:3.10-slim

# 模型缓存目录指向挂载卷，避免每次重建容器重新下载 Kokoro 模型
# 启动日志不缓冲，方便 docker logs 实时查看
ENV HF_HOME=/app/models \
    PYTHONUNBUFFERED=1 \
    MAX_TEXT_LENGTH=100000

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    espeak-ng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# requirements.txt pins torch CPU wheels; this image does not enable GPU acceleration.
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY index.html .
COPY api.html .
COPY style.css .
COPY favicon.ico .

EXPOSE 8880

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8880"]
