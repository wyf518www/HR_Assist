FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HR_STATE_DIR=/state HR_DATA_DIR=/data HR_SECURE_COOKIE=true \
    TZ=Asia/Shanghai OMP_THREAD_LIMIT=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 hrassist && useradd --uid 10001 --gid 10001 --no-create-home hrassist \
    && mkdir /state /data && chown 10001:10001 /state /data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY hr_assist ./hr_assist
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"
CMD ["python","-m","uvicorn","hr_assist.main:create_app","--factory","--host","0.0.0.0","--port","8000","--workers","1","--no-proxy-headers","--no-access-log","--timeout-graceful-shutdown","30"]
