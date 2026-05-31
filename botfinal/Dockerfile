FROM python:3.12-slim

WORKDIR /app

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create persistent directories
RUN mkdir -p /app/data /app/logs

# Create non-root user
RUN useradd -m -r trader && chown -R trader:trader /app
USER trader

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('/app/data/zentry.db').close()"

ENTRYPOINT ["python", "-u", "main.py"]
CMD ["--mode", "live"]
