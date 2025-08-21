# Use official Python base image
FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the health check script
COPY scripts/health_check.py .

# Copy configuration example
COPY config/alert_config.example ./config/

# Make script executable
RUN chmod +x health_check.py

# Run the health check script
CMD ["python", "health_check.py"]