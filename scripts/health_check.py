#!/usr/bin/env python3
"""
Server Health Monitoring Script
Checks CPU, memory, disk usage and sends alerts via Slack/email when thresholds are exceeded.

Author: Kone Tshivhinda
Date: 2025/08/21
"""

import datetime
import logging
import os
import smtplib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil
import requests
from dotenv import load_dotenv

# ======================
# CONFIGURATION SECTION
# ======================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config" / "alert_config"
LOG_FILE = PROJECT_ROOT / "logs" / "health_monitor.log"

# Default thresholds in percent used. An alert is raised when a reading is
# greater than its threshold. Override them with CPU_THRESHOLD,
# MEMORY_THRESHOLD and DISK_THRESHOLD (see load_thresholds).
DEFAULT_THRESHOLDS = {"cpu": 80, "memory": 85, "disk": 90}

METRICS = ("cpu", "memory", "disk")
LABELS = {"cpu": "CPU", "memory": "Memory", "disk": "Disk"}

ALERT_SUBJECT = "SERVER HEALTH ALERT"

class ConfigError(ValueError):
    """An invalid setting in the environment or in config/alert_config"""

def _percentage(env, name, default):
    """Read a 0-100 setting; an unset or empty value gives the default"""
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number between 0 and 100, got {raw!r}") from None
    if not 0 <= value <= 100:
        raise ConfigError(f"{name} must be between 0 and 100, got {raw!r}")
    return value

def load_thresholds(environ=None):
    """Thresholds in percent from CPU_THRESHOLD, MEMORY_THRESHOLD and DISK_THRESHOLD.

    Settings that are not set keep their value from DEFAULT_THRESHOLDS.
    """
    env = os.environ if environ is None else environ
    return {
        metric: _percentage(env, f"{metric.upper()}_THRESHOLD", DEFAULT_THRESHOLDS[metric])
        for metric in METRICS
    }

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

def check_disk(path="/"):
    """Check disk usage of the filesystem at path (default: root partition) and return percentage"""
    disk = psutil.disk_usage(path)
    disk_percent = disk.percent
    logging.info(f"Disk usage ({path}): {disk_percent}%")
    return disk_percent

def collect_metrics(disk_path=None):
    """Read the current usage (percent) of every monitored resource.

    The disk reading covers disk_path, else DISK_PATH from the environment, else "/".
    """
    disk_path = disk_path or os.environ.get("DISK_PATH") or "/"
    return {"cpu": check_cpu(), "memory": check_memory(), "disk": check_disk(disk_path)}

# ======================
# THRESHOLD EVALUATION
# ======================

@dataclass(frozen=True)
class Breach:
    """A metric whose usage is above its threshold"""
    metric: str
    value: float
    threshold: float

    def describe(self):
        return f"High {LABELS[self.metric]} usage: {self.value}% (threshold: {self.threshold:g}%)"

def evaluate_thresholds(metrics, thresholds):
    """Return a Breach for every metric whose value is greater than its threshold.

    Pure function (no I/O), so it can be tested with simulated readings.
    A value exactly equal to its threshold is not a breach.
    """
    return [
        Breach(name, metrics[name], thresholds[name])
        for name in METRICS
        if metrics[name] > thresholds[name]
    ]

# ======================
# ALERTING FUNCTIONS
# ======================

@dataclass(frozen=True)
class Notifier:
    """One alert channel. send(subject, body) raises if delivery fails."""
    name: str
    send: Callable[[str, str], None]

def send_slack_alert(webhook_url, message):
    """Send alert to Slack using webhook"""
    payload = {
        "text": f"⚠️ SERVER ALERT ⚠️\n{message}",
        "username": "Health Monitor",
        "icon_emoji": ":warning:"
    }
    response = requests.post(webhook_url, json=payload)
    response.raise_for_status()

def send_email_alert(email_config, subject, body):
    """Send email alert using SMTP"""
    with smtplib.SMTP(email_config["smtp_server"], email_config["smtp_port"]) as server:
        server.starttls()
        server.login(email_config["sender_email"], email_config["password"])
        message = f"Subject: {subject}\n\n{body}"
        server.sendmail(
            email_config["sender_email"],
            email_config["receiver_email"],
            message
        )

EMAIL_REQUIRED = ("SENDER_EMAIL", "RECEIVER_EMAIL", "EMAIL_PASSWORD")

def load_email_config(environ=None):
    """SMTP settings from the environment, or None when email alerts are not configured.

    Email is enabled only when SENDER_EMAIL, RECEIVER_EMAIL and EMAIL_PASSWORD
    are all set. SMTP_SERVER defaults to smtp.gmail.com and SMTP_PORT to 587.
    """
    env = os.environ if environ is None else environ
    missing = [name for name in EMAIL_REQUIRED if not env.get(name, "").strip()]
    if len(missing) == len(EMAIL_REQUIRED):
        logging.info("Email alerts are disabled: SENDER_EMAIL, RECEIVER_EMAIL and EMAIL_PASSWORD are not set")
        return None
    if missing:
        logging.warning(f"Email alerts are disabled: {', '.join(missing)} not set")
        return None

    port = env.get("SMTP_PORT", "").strip() or "587"
    try:
        smtp_port = int(port)
    except ValueError:
        raise ConfigError(f"SMTP_PORT must be a whole number, got {port!r}") from None
    return {
        "smtp_server": env.get("SMTP_SERVER", "").strip() or "smtp.gmail.com",
        "smtp_port": smtp_port,
        "sender_email": env["SENDER_EMAIL"].strip(),
        "receiver_email": env["RECEIVER_EMAIL"].strip(),
        "password": env["EMAIL_PASSWORD"],
    }

def build_notifiers(environ=None):
    """Create the alert channels that are configured in the environment.

    Both channels are off until configured. The Slack webhook URL is a secret
    with no default: Slack alerts need SLACK_WEBHOOK_URL.
    """
    env = os.environ if environ is None else environ
    notifiers = []

    webhook_url = env.get("SLACK_WEBHOOK_URL", "").strip()
    if webhook_url:
        notifiers.append(Notifier("Slack", lambda subject, body: send_slack_alert(webhook_url, body)))
    else:
        logging.info("SLACK_WEBHOOK_URL is not set; Slack alerts are disabled")

    email_config = load_email_config(env)
    if email_config:
        notifiers.append(Notifier("Email", lambda subject, body: send_email_alert(email_config, subject, body)))
    return notifiers

def deliver(notifiers, subject, body):
    """Send an alert through every notifier; one failing channel never blocks the others.

    Returns {notifier name: True if it was sent, False if it raised}.
    """
    results = {}
    for notifier in notifiers:
        try:
            notifier.send(subject, body)
        except Exception as e:
            logging.error(f"Failed to send {notifier.name} alert: {str(e)}")
            results[notifier.name] = False
        else:
            logging.info(f"{notifier.name} alert sent successfully")
            results[notifier.name] = True
    return results

# ======================
# MAIN MONITORING FUNCTION
# ======================

@dataclass
class HealthResult:
    """What one health check found and which alert channels were used"""
    metrics: dict
    breaches: list
    deliveries: dict = field(default_factory=dict)

    @property
    def healthy(self):
        return not self.breaches

def run_health_check(metrics_source=collect_metrics, notifiers=None, thresholds=None, now=datetime.datetime.now):
    """Run all health checks and trigger alerts if needed.

    metrics_source: callable returning {"cpu": ..., "memory": ..., "disk": ...} (percent)
    notifiers: list of Notifier; defaults to the channels configured in the environment
    thresholds: mapping of metric name to threshold; defaults to load_thresholds()
    now: callable returning the current datetime
    """
    timestamp = now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"Starting health check at {timestamp}")

    thresholds = load_thresholds() if thresholds is None else thresholds
    notifiers = build_notifiers() if notifiers is None else notifiers

    metrics = metrics_source()
    breaches = evaluate_thresholds(metrics, thresholds)
    result = HealthResult(metrics=metrics, breaches=breaches)

    # Send alerts if issues found
    if breaches:
        alert_message = "\n".join(breach.describe() for breach in breaches)
        full_message = f"Server Health Alert!\nTime: {timestamp}\n\n{alert_message}"

        result.deliveries = deliver(notifiers, ALERT_SUBJECT, full_message)
        if not notifiers:
            logging.warning(
                "No alert channel is configured, so no alert was sent "
                "(set SLACK_WEBHOOK_URL and/or the email settings)"
            )

        logging.warning(f"Health issues detected: {alert_message}")
    else:
        logging.info("All systems nominal")

    return result

# ======================
# EXECUTION
# ======================

def main():
    """Load the configuration, set up logging and run one health check"""
    load_dotenv(CONFIG_FILE)

    # Create log directory if it doesn't exist (works on Windows and Linux)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logging.info("Logging system initialized successfully")

    try:
        run_health_check()
    except ConfigError as e:
        logging.critical(f"Invalid configuration: {e}")
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        logging.critical(f"Health check script failed: {str(e)}")

if __name__ == "__main__":
    main()
