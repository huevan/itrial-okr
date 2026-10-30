FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY . .
RUN useradd --create-home --uid 10001 okr && mkdir -p data && chown -R okr:okr /app
USER okr
EXPOSE 8888
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/healthz', timeout=3)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8888"]
