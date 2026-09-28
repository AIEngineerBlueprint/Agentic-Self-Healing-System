FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ /app/src/
ENV PYTHONPATH=/app/src

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD curl -fsS http://localhost:8080/health || exit 1

# --reload with the source volume-mounted is what lets a repaired service pick
# up an agent-applied patch without a container restart.
CMD ["uvicorn", "calc_api.main:app", "--host", "0.0.0.0", "--port", "8080", "--reload", "--reload-dir", "/app/src"]
