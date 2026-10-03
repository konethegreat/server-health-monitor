"""Tests for scripts/health_check.py.

Everything runs on simulated metrics and fake notifiers, so the results do not
depend on the machine's real load and nothing touches the network.
"""

import datetime
import logging
import smtplib

import pytest
import requests

import health_check as hc

DEFAULTS = hc.DEFAULT_THRESHOLDS  # cpu 80, memory 85, disk 90 (percent)
NOW = datetime.datetime(2026, 10, 3, 12, 0, 0)

# Every environment variable the monitor reads; cleared so a developer's own settings cannot leak into tests
CONFIG_VARIABLES = (
    "CPU_THRESHOLD",
    "MEMORY_THRESHOLD",
    "DISK_THRESHOLD",
    "SLACK_WEBHOOK_URL",
    "SMTP_SERVER",
    "SMTP_PORT",
    "SENDER_EMAIL",
    "RECEIVER_EMAIL",
    "EMAIL_PASSWORD",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if a test reaches a real sender, and ignore any local configuration."""

    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted in a test")

    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(smtplib, "SMTP", blocked)
    for name in CONFIG_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def metrics(cpu=10, memory=20, disk=30):
    return {"cpu": cpu, "memory": memory, "disk": disk}


class FakeChannel:
    """Stands in for a notifier and records every alert it is asked to send."""

    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.sent = []

    def send(self, subject, body):
        if self.fail:
            raise RuntimeError("delivery failed")
        self.sent.append((subject, body))

    @property
    def notifier(self):
        return hc.Notifier(self.name, self.send)


def run(channels, **readings):
    return hc.run_health_check(
        metrics_source=lambda: metrics(**readings),
        notifiers=[channel.notifier for channel in channels],
        now=lambda: NOW,
    )


# ---------------------------------------------------------------- thresholds


@pytest.mark.parametrize(
    "readings",
    [
        metrics(),
        metrics(cpu=0, memory=0, disk=0),
        metrics(cpu=80, memory=85, disk=90),  # exactly at each threshold is not a breach
    ],
)
def test_usage_at_or_below_thresholds_is_healthy(readings):
    assert hc.evaluate_thresholds(readings, DEFAULTS) == []


@pytest.mark.parametrize(
    "metric, just_above",
    [("cpu", 80.1), ("memory", 85.1), ("disk", 90.1)],
)
def test_each_metric_breaches_just_above_its_own_threshold(metric, just_above):
    readings = metrics(**{metric: just_above})
    assert hc.evaluate_thresholds(readings, DEFAULTS) == [hc.Breach(metric, just_above, DEFAULTS[metric])]


def test_thresholds_are_independent_per_metric():
    # 82% would be fine for memory (85) and disk (90) but is above the CPU threshold (80)
    breaches = hc.evaluate_thresholds(metrics(cpu=82, memory=82, disk=82), DEFAULTS)
    assert [b.metric for b in breaches] == ["cpu"]


def test_all_metrics_can_breach_at_once_in_a_stable_order():
    breaches = hc.evaluate_thresholds(metrics(disk=99, cpu=95, memory=90), DEFAULTS)
    assert [b.metric for b in breaches] == ["cpu", "memory", "disk"]


def test_custom_thresholds_replace_the_defaults():
    strict = {"cpu": 5, "memory": 5, "disk": 5}
    assert [b.metric for b in hc.evaluate_thresholds(metrics(), strict)] == ["cpu", "memory", "disk"]
    lax = {"cpu": 100, "memory": 100, "disk": 100}
    assert hc.evaluate_thresholds(metrics(cpu=99.9, memory=99.9, disk=99.9), lax) == []


def test_breach_description_names_the_metric_value_and_threshold():
    assert hc.Breach("cpu", 91.5, 80).describe() == "High CPU usage: 91.5% (threshold: 80%)"
    assert hc.Breach("memory", 88.0, 85).describe() == "High Memory usage: 88.0% (threshold: 85%)"
    assert hc.Breach("disk", 90.5, 90).describe() == "High Disk usage: 90.5% (threshold: 90%)"


# ------------------------------------------------------- alerting behaviour


def test_healthy_run_sends_no_alert():
    slack, email = FakeChannel("Slack"), FakeChannel("Email")
    result = run([slack, email])
    assert result.healthy
    assert result.breaches == []
    assert result.deliveries == {}
    assert slack.sent == [] and email.sent == []


def test_breach_sends_one_alert_per_channel_listing_every_issue():
    slack, email = FakeChannel("Slack"), FakeChannel("Email")
    result = run([slack, email], cpu=91.5, memory=50, disk=95)

    assert not result.healthy
    assert result.deliveries == {"Slack": True, "Email": True}
    for channel in (slack, email):
        assert len(channel.sent) == 1  # one message for the whole run, not one per breach
        subject, body = channel.sent[0]
        assert subject == "SERVER HEALTH ALERT"
        assert "Time: 2026-10-03 12:00:00" in body
        assert "High CPU usage: 91.5% (threshold: 80%)" in body
        assert "High Disk usage: 95% (threshold: 90%)" in body
        assert "Memory" not in body


def test_failed_channel_is_reported_and_does_not_block_the_others(caplog):
    slack, email = FakeChannel("Slack", fail=True), FakeChannel("Email")
    with caplog.at_level(logging.ERROR):
        result = run([slack, email], cpu=99)

    assert result.deliveries == {"Slack": False, "Email": True}
    assert len(email.sent) == 1
    assert "Failed to send Slack alert" in caplog.text


def test_all_channels_failing_does_not_raise():
    result = run([FakeChannel("Slack", fail=True), FakeChannel("Email", fail=True)], disk=99)
    assert result.deliveries == {"Slack": False, "Email": False}
    assert not result.healthy


def test_persistent_breach_alerts_again_on_every_run():
    """By default there is no de-duplication: each run that is above a threshold alerts."""
    channel = FakeChannel("Slack")
    for _ in range(3):
        run([channel], cpu=95)
    assert len(channel.sent) == 3


def test_only_runs_above_a_threshold_alert():
    channel = FakeChannel("Slack")
    run([channel], cpu=95)  # breach
    run([channel], cpu=20)  # recovered, no alert
    run([channel], memory=90)  # a different metric breaches
    assert len(channel.sent) == 2
    assert "High CPU usage" in channel.sent[0][1]
    assert "High Memory usage" in channel.sent[1][1]


# ------------------------------------------------------- metric collection


def test_collect_metrics_maps_psutil_readings(monkeypatch):
    class Memory:
        percent = 61.5

    class Disk:
        percent = 72.5

    monkeypatch.setattr(hc.psutil, "cpu_percent", lambda interval=None: 33.0)
    monkeypatch.setattr(hc.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(hc.psutil, "disk_usage", lambda path: Disk())

    assert hc.collect_metrics() == {"cpu": 33.0, "memory": 61.5, "disk": 72.5}


# ------------------------------------------------------------ configuration


def test_thresholds_default_when_nothing_is_configured():
    assert hc.load_thresholds({}) == {"cpu": 80, "memory": 85, "disk": 90}


def test_thresholds_can_be_overridden_per_metric():
    env = {"CPU_THRESHOLD": "50", "DISK_THRESHOLD": " 95.5 "}
    assert hc.load_thresholds(env) == {"cpu": 50, "memory": 85, "disk": 95.5}


def test_empty_threshold_setting_falls_back_to_the_default():
    assert hc.load_thresholds({"MEMORY_THRESHOLD": ""})["memory"] == 85


@pytest.mark.parametrize("bad", ["abc", "-1", "100.5", "nan", "inf", "80%"])
def test_invalid_threshold_is_rejected_and_names_the_setting(bad):
    with pytest.raises(hc.ConfigError, match="CPU_THRESHOLD"):
        hc.load_thresholds({"CPU_THRESHOLD": bad})


@pytest.mark.parametrize("edge", ["0", "100"])
def test_threshold_range_includes_both_ends(edge):
    assert hc.load_thresholds({"DISK_THRESHOLD": edge})["disk"] == float(edge)


def test_run_health_check_uses_thresholds_from_the_environment(monkeypatch):
    channel = FakeChannel("Slack")
    monkeypatch.setenv("CPU_THRESHOLD", "50")

    result = hc.run_health_check(
        metrics_source=lambda: metrics(cpu=60), notifiers=[channel.notifier], now=lambda: NOW
    )

    assert [b.metric for b in result.breaches] == ["cpu"]
    assert "High CPU usage: 60% (threshold: 50%)" in channel.sent[0][1]


def test_fractional_thresholds_are_described_without_padding():
    assert hc.Breach("cpu", 76.0, 75.5).describe() == "High CPU usage: 76.0% (threshold: 75.5%)"
    assert hc.Breach("cpu", 76.0, 75.0).describe() == "High CPU usage: 76.0% (threshold: 75%)"


def test_main_reports_invalid_configuration_and_exits_with_status_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(hc, "CONFIG_FILE", tmp_path / "no-such-config")
    monkeypatch.setattr(hc, "LOG_FILE", tmp_path / "logs" / "health_monitor.log")
    monkeypatch.setenv("CPU_THRESHOLD", "abc")

    with pytest.raises(SystemExit) as excinfo:
        hc.main()

    assert excinfo.value.code == 2
    assert "CPU_THRESHOLD" in capsys.readouterr().err


# ------------------------------------------------------------------ senders


class FakeResponse:
    def __init__(self, error=None):
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error


def test_slack_alert_posts_the_message_as_json_to_the_webhook(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    hc.send_slack_alert("https://example.invalid/hook", "disk is full")

    ((url, kwargs),) = calls
    assert url == "https://example.invalid/hook"
    assert "disk is full" in kwargs["json"]["text"]


def test_slack_alert_raises_when_slack_rejects_the_request(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(requests.HTTPError("404")))
    with pytest.raises(requests.HTTPError):
        hc.send_slack_alert("https://example.invalid/hook", "message")


class FakeSMTP:
    instances = []

    def __init__(self, server, port, **kwargs):
        self.server, self.port = server, port
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def sendmail(self, sender, receiver, message):
        self.calls.append(("sendmail", sender, receiver, message))


def test_email_alert_connects_secures_logs_in_and_sends(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    config = {
        "smtp_server": "smtp.example.invalid",
        "smtp_port": 587,
        "sender_email": "monitor@example.invalid",
        "receiver_email": "admin@example.invalid",
        "password": "not-a-real-password",
    }

    hc.send_email_alert(config, "SUBJECT", "BODY")

    (smtp,) = FakeSMTP.instances
    assert (smtp.server, smtp.port) == ("smtp.example.invalid", 587)
    assert smtp.calls[0] == "starttls"  # TLS before credentials are sent
    assert smtp.calls[1] == ("login", "monitor@example.invalid")
    kind, sender, receiver, message = smtp.calls[2]
    assert (kind, sender, receiver) == ("sendmail", "monitor@example.invalid", "admin@example.invalid")
    assert message.startswith("Subject: SUBJECT") and message.endswith("BODY")
