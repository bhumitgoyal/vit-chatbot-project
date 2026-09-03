FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    GCP_PROJECT_ID=nuveroai \
    GCP_LOCATION=us-central1 \
    GEMINI_MODEL=gemini-2.5-flash

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["sh", "-c", "exec python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
