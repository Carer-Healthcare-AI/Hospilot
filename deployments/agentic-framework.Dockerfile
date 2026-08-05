FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source (overridden at runtime by the bind mount in dev)
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Flat layout: config, api.*, workflows.*, agents.* all resolve from /app
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
