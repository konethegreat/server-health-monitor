# Server Health Monitor

A Python tool that samples CPU, memory, and disk usage, compares them with
configured thresholds, and sends a Slack or email alert when a threshold is
exceeded. Each invocation runs one check; use a scheduler for repeated checks.

[![Checks](https://github.com/konethegreat/server-health-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/konethegreat/server-health-monitor/actions/workflows/ci.yml)

## Install and run

Use Python 3.10 or later on Windows or Linux:

```bash
git clone https://github.com/konethegreat/server-health-monitor.git
cd server-health-monitor
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/health_check.py
```

No alert channel is enabled until you configure it. Results are written to
`logs/health_monitor.log`; the directory is created automatically. Read that
file to inspect readings, threshold breaches, and delivery outcomes.

## Configuration

Copy `config/alert_config.example` to `config/alert_config` and edit the copy.
PowerShell uses `Copy-Item config/alert_config.example config/alert_config`;
Linux/macOS uses `cp config/alert_config.example config/alert_config`.
Existing environment variables take precedence over the configuration file.

| Variable | Default / behavior |
| --- | --- |
| `CPU_THRESHOLD` | 80 percent used |
| `MEMORY_THRESHOLD` | 85 percent used |
| `DISK_THRESHOLD` | 90 percent used |
| `DISK_PATH` | `/`; on Windows, set a path such as `C:/` explicitly |
| `SLACK_WEBHOOK_URL` | Unset disables Slack |
| `SENDER_EMAIL`, `RECEIVER_EMAIL`, `EMAIL_PASSWORD` | All three are required to enable email |
| `SMTP_SERVER`, `SMTP_PORT` | `smtp.gmail.com`, 587; SMTP uses STARTTLS |

A reading must be greater than its threshold to trigger an alert. Thresholds
must be between 0 and 100. Notification requests time out after 10 seconds;
a failed channel does not prevent trying the other channel. Delivery failures
are logged without the webhook URL, password, or server response text.

Keep the real configuration private. It is gitignored. Follow your mail
provider's SMTP authentication instructions when choosing a password or app
password.

## Scheduling

On Linux, a cron entry can run the script every five minutes. Use absolute
paths to the project and virtual environment:

```cron
*/5 * * * * /path/to/server-health-monitor/.venv/bin/python /path/to/server-health-monitor/scripts/health_check.py
```

On Windows, create a Task Scheduler task whose program is the virtual
environment's `python.exe` and whose argument is the absolute path to
`scripts/health_check.py`. Select the desired repeat interval.

The tool sends an alert on every run that detects a breach. It does not yet
deduplicate alerts, track incidents, or provide a dashboard. Check the log for
delivery success; the process exit code is not a delivery receipt.

## Docker

```bash
docker build -f docker/Dockerfile -t server-health-monitor .
docker run --rm --env-file config/alert_config server-health-monitor
```

The container runs one sample and exits. To retain logs, mount a directory
at `/app/logs`. Container metrics depend on the host and runtime's visibility;
use a host installation when you need readings for a particular host disk.

## Development and verification

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Tests cover threshold boundaries, configuration, independent delivery
failures, timeout settings, and redacted error logging with mocked services.
A separate smoke test checks that psutil returns percentages on the current
machine. GitHub Actions runs the checks on Windows and Linux. Mocked delivery
tests do not prove that your Slack or SMTP credentials work.

Report reproducible bugs and propose focused changes through
[issues](https://github.com/konethegreat/server-health-monitor/issues) and pull
requests. Never include webhook URLs, passwords, or private logs in an issue.
