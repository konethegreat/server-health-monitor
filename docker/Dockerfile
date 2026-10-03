# Build from the repository root:
#   docker build -f docker/Dockerfile -t server-health-monitor .
FROM python:3.12-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Same layout as the repository, so config/alert_config and logs/ mean the same
# thing inside the container as they do in a normal checkout
COPY scripts/ scripts/
COPY config/alert_config.example config/

# One health check per run; the container exits when it is done
CMD ["python", "scripts/health_check.py"]
