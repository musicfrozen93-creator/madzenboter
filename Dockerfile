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
    apt-get install -y --no-install-recommends libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Create persistent directories
RUN mkdir -p /app/logs

# Create non-root user
RUN useradd -m -r analyst && chown -R analyst:analyst /app
USER analyst

EXPOSE 8000

# Health check hits the service's own liveness endpoint.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["python", "-u", "main.py"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
