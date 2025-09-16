#!/usr/bin/env python3
"""
Server Health Monitoring Script
Checks CPU, memory, disk usage and sends alerts via Slack/email when thresholds are exceeded.
Created for portfolio demonstration with proper documentation and error handling.

Author: Kone Tshivhinda
Date: 2025/08/21
"""

import psutil
import datetime
import smtplib
import requests
import logging
from pathlib import Path

# ======================
# CONFIGURATION SECTION
# ======================
from dotenv import load_dotenv
import os

# Load environment variables from config file
load_dotenv('config/alert_config')

# Get configuration values (with fallbacks for development)
SLACK_WEBHOOK_URL = os.getenv("https://hooks.slack.com/services/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", "TEST_WEBHOOK_URL")
EMAIL_CONFIG = {
    "smtp_server": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.getenv("SMTP_PORT", "587")),
    "sender_email": os.getenv("SENDER_EMAIL", "dev@example.com"),
    "receiver_email": os.getenv("RECEIVER_EMAIL", "dev@example.com"),
    "password": os.getenv("EMAIL_PASSWORD", "DEV_PASSWORD")
}

# Thresholds (customize as needed)
CPU_THRESHOLD = 80  # Percent
MEMORY_THRESHOLD = 85  # Percent
DISK_THRESHOLD = 90  # Percent


# ======================
# LOGGING CONFIGURATION (FIXED FOR WINDOWS)
# ======================
import os
from pathlib import Path

# Create logs directory in project root (works on Windows/Linux)
PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)  # Create if doesn't exist

LOG_FILE = LOGS_DIR / "health_monitor.log"

# Configure logging AFTER ensuring directory exists
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.info("✅ Logging system initialized successfully")
# This creates a log file to track all monitoring activities [[9]]

# ======================
# HEALTH CHECK FUNCTIONS
# ======================

def check_cpu():
    """Check CPU usage and return percentage"""
    cpu_percent = psutil.cpu_percent(interval=1)
    logging.info(f"CPU usage: {cpu_percent}%")
    return cpu_percent

def check_memory():
    """Check memory usage and return percentage"""
    memory = psutil.virtual_memory()
    memory_percent = memory.percent
    logging.info(f"Memory usage: {memory_percent}%")
    return memory_percent

def check_disk():
    """Check disk usage for root partition"""
    disk = psutil.disk_usage('/')
    disk_percent = disk.percent
    logging.info(f"Disk usage: {disk_percent}%")
    return disk_percent

# ======================
# ALERTING FUNCTIONS
# ======================

def send_slack_alert(message):
    """Send alert to Slack using webhook"""
    payload = {
        "text": f"⚠️ SERVER ALERT ⚠️\n{message}",
        "username": "Health Monitor",
        "icon_emoji": ":warning:"
    }
    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        logging.info("Slack alert sent successfully")
    except Exception as e:
        logging.error(f"Failed to send Slack alert: {str(e)}")

def send_email_alert(subject, body):
    """Send email alert using SMTP"""
    try:
        with smtplib.SMTP(EMAIL_CONFIG["smtp_server"], EMAIL_CONFIG["smtp_port"]) as server:
            server.starttls()
            server.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["password"])
            message = f"Subject: {subject}\n\n{body}"
            server.sendmail(
                EMAIL_CONFIG["sender_email"],
                EMAIL_CONFIG["receiver_email"],
                message
            )
        logging.info("Email alert sent successfully")
    except Exception as e:
        logging.error(f"Failed to send email alert: {str(e)}")

# ======================
# MAIN MONITORING FUNCTION
# ======================

def run_health_check():
    """Main function to run all health checks and trigger alerts if needed"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"Starting health check at {timestamp}")
    
    issues = []
    
    # Check CPU
    cpu = check_cpu()
    if cpu > CPU_THRESHOLD:
        issues.append(f"High CPU usage: {cpu}% (threshold: {CPU_THRESHOLD}%)")
    
    # Check Memory
    memory = check_memory()
    if memory > MEMORY_THRESHOLD:
        issues.append(f"High Memory usage: {memory}% (threshold: {MEMORY_THRESHOLD}%)")
    
    # Check Disk
    disk = check_disk()
    if disk > DISK_THRESHOLD:
        issues.append(f"High Disk usage: {disk}% (threshold: {DISK_THRESHOLD}%)")
    
    # Send alerts if issues found
    if issues:
        alert_message = "\n".join(issues)
        full_message = f"Server Health Alert!\nTime: {timestamp}\n\n{alert_message}"
        
        send_slack_alert(full_message)
        
        email_subject = "SERVER HEALTH ALERT"
        send_email_alert(email_subject, full_message)
        
        logging.warning(f"Health issues detected: {alert_message}")
    else:
        logging.info("All systems nominal")
    
    return len(issues) == 0

# ======================
# EXECUTION
# ======================

if __name__ == "__main__":
    # Create log directory if it doesn't exist
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
    
    try:
        run_health_check()
    except Exception as e:
        logging.critical(f"Health check script failed: {str(e)}")
        # Always handle exceptions in monitoring scripts [[3]]