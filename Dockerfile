FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies including PostgreSQL client libs
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Runtime stage ---
FROM python:3.12-slim

WORKDIR /app

# Install runtime PostgreSQL client lib only
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Create persistent directories
RUN mkdir -p /app/logs

# Create non-root user
RUN useradd -m -r trader && chown -R trader:trader /app
USER trader

# Health check against PostgreSQL via Python
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "from sqlalchemy import create_engine; import os; e=create_engine(os.environ['DATABASE_URL']); c=e.connect(); c.close(); print('OK')"

ENTRYPOINT ["python", "-u", "main.py"]
CMD ["--mode", "live"]
