FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /app/data/sessions /app/data/logs /app/data/prompts /app/data/transcripts
EXPOSE 9040
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9040", "--log-level", "info"]
