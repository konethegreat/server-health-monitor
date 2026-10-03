"""Tests for scripts/health_check.py.

Everything runs on simulated metrics and fake notifiers, so the results do not
depend on the machine's real load and nothing touches the network.
"""

import datetime
import email.policy
import email.utils
import logging
import pathlib
import smtplib
import socket
import threading
import time

import pytest
import requests
from dotenv import dotenv_values

import health_check as hc

DEFAULTS = hc.DEFAULT_THRESHOLDS  # cpu 80, memory 85, disk 90 (percent)
NOW = datetime.datetime(2026, 10, 3, 12, 0, 0)

# The real senders, kept before the autouse fixture below replaces them. Only the
# loopback timeout tests use them.
REAL_POST = requests.post
REAL_SMTP = smtplib.SMTP
EXAMPLE_CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config" / "alert_config.example"

# Every environment variable the monitor reads; cleared so a developer's own settings cannot leak into tests
CONFIG_VARIABLES = (
    "CPU_THRESHOLD",
    "MEMORY_THRESHOLD",
    "DISK_THRESHOLD",
    "DISK_PATH",
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


def test_breach_without_any_channel_warns_that_no_alert_was_sent(caplog):
    with caplog.at_level(logging.WARNING):
        result = run([], cpu=99)

    assert not result.healthy
    assert result.deliveries == {}
    assert "No alert channel is configured" in caplog.text


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


def test_evaluation_skips_a_metric_that_was_not_read():
    assert hc.evaluate_thresholds({"cpu": 95, "memory": 20}, DEFAULTS) == [hc.Breach("cpu", 95, 80)]


def test_a_breach_is_still_alerted_when_another_reading_is_missing(caplog):
    channel = FakeChannel("Slack")
    with caplog.at_level(logging.INFO):
        result = hc.run_health_check(
            metrics_source=lambda: {"cpu": 95, "memory": 20},  # the disk reading failed
            notifiers=[channel.notifier],
            now=lambda: NOW,
        )

    assert [breach.metric for breach in result.breaches] == ["cpu"]
    assert result.unreadable == ["disk"]
    assert not result.healthy
    assert len(channel.sent) == 1 and "High CPU usage" in channel.sent[0][1]
    assert "Not checked because the reading failed: Disk" in caplog.text
    assert "All systems nominal" not in caplog.text


def test_a_missing_reading_without_a_breach_is_not_reported_as_nominal(caplog):
    channel = FakeChannel("Slack")
    with caplog.at_level(logging.INFO):
        result = hc.run_health_check(
            metrics_source=lambda: {"cpu": 10, "disk": 30},  # the memory reading failed
            notifiers=[channel.notifier],
            now=lambda: NOW,
        )

    assert result.breaches == []
    assert result.unreadable == ["memory"]
    assert not result.healthy
    assert channel.sent == []  # nothing is above a threshold, so no alert; the failure is logged
    assert "Not checked because the reading failed: Memory" in caplog.text
    assert "All systems nominal" not in caplog.text


# ------------------------------------------------------- metric collection


def stub_psutil(monkeypatch, cpu=33.0, memory=61.5, disk=72.5):
    """Replace the psutil readings; returns the list of paths disk_usage is asked about."""
    paths = []

    class Reading:
        def __init__(self, percent):
            self.percent = percent

    def disk_usage(path):
        paths.append(path)
        return Reading(disk)

    monkeypatch.setattr(hc.psutil, "cpu_percent", lambda interval=None: cpu)
    monkeypatch.setattr(hc.psutil, "virtual_memory", lambda: Reading(memory))
    monkeypatch.setattr(hc.psutil, "disk_usage", disk_usage)
    return paths


def test_collect_metrics_maps_psutil_readings(monkeypatch):
    stub_psutil(monkeypatch, cpu=33.0, memory=61.5, disk=72.5)
    assert hc.collect_metrics() == {"cpu": 33.0, "memory": 61.5, "disk": 72.5}


def test_disk_check_defaults_to_the_root_partition(monkeypatch):
    paths = stub_psutil(monkeypatch)
    hc.collect_metrics()
    assert paths == ["/"]


def test_disk_path_can_be_set_in_the_environment(monkeypatch):
    paths = stub_psutil(monkeypatch)
    monkeypatch.setenv("DISK_PATH", "/data")
    hc.collect_metrics()
    assert paths == ["/data"]


def test_empty_disk_path_setting_falls_back_to_the_root_partition(monkeypatch):
    paths = stub_psutil(monkeypatch)
    monkeypatch.setenv("DISK_PATH", "")
    hc.collect_metrics()
    assert paths == ["/"]


def test_explicit_disk_path_wins_over_the_environment(monkeypatch):
    paths = stub_psutil(monkeypatch)
    monkeypatch.setenv("DISK_PATH", "/data")
    hc.collect_metrics(disk_path="/srv")
    assert paths == ["/srv"]


def test_a_reading_that_fails_is_left_out_and_logged_while_the_others_are_still_read(monkeypatch, caplog):
    stub_psutil(monkeypatch, cpu=33.0, memory=61.5)

    def unreadable(path):
        raise OSError("no such path")

    monkeypatch.setattr(hc.psutil, "disk_usage", unreadable)
    with caplog.at_level(logging.ERROR):
        readings = hc.collect_metrics()

    assert readings == {"cpu": 33.0, "memory": 61.5}
    assert "Could not read Disk usage: OSError: no such path" in caplog.text


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


# ------------------------------------------- notification configuration

SLACK_ENV = {"SLACK_WEBHOOK_URL": "https://example.invalid/hook"}
EMAIL_ENV = {
    "SENDER_EMAIL": "monitor@example.invalid",
    "RECEIVER_EMAIL": "admin@example.invalid",
    "EMAIL_PASSWORD": "not-a-real-password",
}


def channel_names(env):
    return [notifier.name for notifier in hc.build_notifiers(env)]


def test_no_alert_channel_is_enabled_without_configuration():
    assert channel_names({}) == []
    assert channel_names({"SLACK_WEBHOOK_URL": "  ", "SMTP_SERVER": "smtp.example.invalid"}) == []


def test_slack_is_enabled_by_a_webhook_url():
    assert channel_names(SLACK_ENV) == ["Slack"]


def test_email_is_enabled_by_sender_receiver_and_password():
    assert channel_names(EMAIL_ENV) == ["Email"]


def test_both_channels_are_used_when_both_are_configured():
    assert channel_names({**SLACK_ENV, **EMAIL_ENV}) == ["Slack", "Email"]


@pytest.mark.parametrize("missing", sorted(EMAIL_ENV))
def test_partial_email_settings_disable_email_and_warn_without_leaking_values(missing, caplog):
    env = {name: value for name, value in EMAIL_ENV.items() if name != missing}

    with caplog.at_level(logging.WARNING):
        assert channel_names(env) == []

    assert missing in caplog.text
    assert "not-a-real-password" not in caplog.text


def test_invalid_smtp_port_is_a_configuration_error():
    with pytest.raises(hc.ConfigError, match="SMTP_PORT"):
        hc.build_notifiers({**EMAIL_ENV, "SMTP_PORT": "five-eight-seven"})


def test_smtp_port_is_not_checked_while_email_is_disabled():
    assert channel_names({"SMTP_PORT": "five-eight-seven"}) == []


def test_example_config_matches_the_defaults_and_enables_nothing():
    """config/alert_config.example must stay loadable, show the real defaults and hold no credentials."""
    values = dotenv_values(EXAMPLE_CONFIG)

    assert hc.load_thresholds(values) == hc.load_thresholds({})
    assert hc.build_notifiers(values) == []
    assert values["SLACK_WEBHOOK_URL"] == ""
    assert values["EMAIL_PASSWORD"] == ""


def test_slack_notifier_posts_the_alert_body_to_the_configured_webhook(monkeypatch):
    posted = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: posted.append((url, kw)) or FakeResponse())

    (notifier,) = hc.build_notifiers(SLACK_ENV)
    notifier.send("SUBJECT", "the alert body")

    ((url, kwargs),) = posted
    assert url == "https://example.invalid/hook"
    assert "the alert body" in kwargs["json"]["text"]


def test_email_notifier_defaults_to_gmail_on_port_587(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    (notifier,) = hc.build_notifiers(EMAIL_ENV)
    notifier.send("SUBJECT", "the alert body")

    (smtp,) = FakeSMTP.instances
    assert (smtp.server, smtp.port) == ("smtp.gmail.com", 587)
    assert ("login", "monitor@example.invalid") in smtp.calls


def test_email_notifier_uses_the_configured_server_and_port(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    env = {**EMAIL_ENV, "SMTP_SERVER": "smtp.example.invalid", "SMTP_PORT": "2525"}

    (notifier,) = hc.build_notifiers(env)
    notifier.send("SUBJECT", "the alert body")

    (smtp,) = FakeSMTP.instances
    assert (smtp.server, smtp.port) == ("smtp.example.invalid", 2525)


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


EMAIL_CONFIG = {
    "smtp_server": "smtp.example.invalid",
    "smtp_port": 587,
    "sender_email": "monitor@example.invalid",
    "receiver_email": "admin@example.invalid",
    "password": "not-a-real-password",
}


class FakeSMTP(REAL_SMTP):
    """An SMTP connection that never reaches a network.

    It subclasses the real class so that send_message() runs the standard library's
    own message flattening and recipient handling. Only the calls that would talk
    to a server are replaced; sendmail() records the bytes that would go on the wire.
    """

    instances = []

    def __init__(self, server, port, **kwargs):  # no super().__init__(): that would connect
        self.server, self.port, self.kwargs = server, port, kwargs
        self.esmtp_features = {}
        self.calls = []
        FakeSMTP.instances.append(self)

    def __exit__(self, *exc):  # the real one sends QUIT
        return False

    def ehlo_or_helo_if_needed(self):
        pass

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        if isinstance(msg, str):  # like the real sendmail(), which encodes text as ASCII
            msg = msg.encode("ascii")
        self.calls.append(("sendmail", from_addr, to_addrs, msg))


def send_through_fake_smtp(monkeypatch, config, subject="SUBJECT", body="BODY"):
    """Send one email through send_email_alert() and return the FakeSMTP connection it used."""
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    hc.send_email_alert(config, subject, body)
    (smtp,) = FakeSMTP.instances
    return smtp


def delivered_message(smtp):
    """The message the server would receive, parsed back from the bytes handed to sendmail()."""
    *_, raw = smtp.calls[-1]
    return email.message_from_bytes(raw, policy=email.policy.default)


def test_slack_request_has_a_timeout(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append(kw) or FakeResponse())

    hc.send_slack_alert("https://example.invalid/hook", "message")

    assert calls[0]["timeout"] == hc.NOTIFY_TIMEOUT_SECONDS
    assert 0 < hc.NOTIFY_TIMEOUT_SECONDS <= 30


def test_smtp_connection_has_a_timeout(monkeypatch):
    smtp = send_through_fake_smtp(monkeypatch, EMAIL_CONFIG)

    assert smtp.kwargs["timeout"] == hc.NOTIFY_TIMEOUT_SECONDS


@pytest.fixture
def silent_server(monkeypatch):
    """A loopback server that completes the TCP handshake but never answers.

    Returns its port. Real sockets, but only on 127.0.0.1.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")  # never route the loopback test through a proxy
    monkeypatch.setattr(requests, "post", REAL_POST)
    monkeypatch.setattr(smtplib, "SMTP", REAL_SMTP)
    monkeypatch.setattr(hc, "NOTIFY_TIMEOUT_SECONDS", 0.5)
    yield server.getsockname()[1]
    server.close()  # also releases a sender that was (wrongly) still waiting


def call_with_deadline(func, seconds=4):
    """Return the exception func raised (or None), failing the test if func is still blocked after `seconds`.

    Runs func in a daemon thread so that a sender without a working timeout fails
    the test instead of hanging the whole test run.
    """
    outcome = {}

    def target():
        try:
            func()
            outcome["error"] = None
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            outcome["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"still blocked after {seconds}s: the sender has no effective timeout")
    return outcome["error"]


def test_slack_alert_gives_up_when_the_server_never_answers(silent_server):
    started = time.monotonic()
    error = call_with_deadline(lambda: hc.send_slack_alert(f"http://127.0.0.1:{silent_server}/hook", "message"))
    assert isinstance(error, requests.exceptions.Timeout)
    assert time.monotonic() - started < 2


def test_email_alert_gives_up_when_the_server_never_answers(silent_server):
    config = {**EMAIL_CONFIG, "smtp_server": "127.0.0.1", "smtp_port": silent_server}
    started = time.monotonic()
    error = call_with_deadline(lambda: hc.send_email_alert(config, "SUBJECT", "BODY"))
    assert isinstance(error, OSError)  # smtplib.SMTPServerDisconnected is an OSError
    assert time.monotonic() - started < 2


def test_email_alert_connects_secures_logs_in_and_sends(monkeypatch):
    smtp = send_through_fake_smtp(monkeypatch, EMAIL_CONFIG)

    assert (smtp.server, smtp.port) == ("smtp.example.invalid", 587)
    assert smtp.calls[0] == "starttls"  # TLS before credentials are sent
    assert smtp.calls[1] == ("login", "monitor@example.invalid")
    kind, sender, recipients, _ = smtp.calls[2]
    assert (kind, sender, recipients) == ("sendmail", "monitor@example.invalid", ["admin@example.invalid"])
    assert len(smtp.calls) == 3


def test_email_has_the_subject_sender_recipient_date_and_body(monkeypatch):
    message = delivered_message(send_through_fake_smtp(monkeypatch, EMAIL_CONFIG))

    assert message["Subject"] == "SUBJECT"
    assert message["From"] == "monitor@example.invalid"
    assert message["To"] == "admin@example.invalid"
    assert email.utils.parsedate_to_datetime(message["Date"])  # a valid Date header
    assert message.get_content().strip() == "BODY"


def test_email_with_non_ascii_text_is_sent_and_reads_back_unchanged(monkeypatch):
    subject = "ALERT: Zo\u00eb's server"
    body = "Disk at 95% on k\u00f6ln-01 \u2013 check it"

    message = delivered_message(send_through_fake_smtp(monkeypatch, EMAIL_CONFIG, subject, body))

    assert message["Subject"] == subject
    assert message.get_content().strip() == body


def test_every_comma_separated_receiver_is_sent_the_alert(monkeypatch):
    config = {**EMAIL_CONFIG, "receiver_email": "admin@example.invalid, oncall@example.invalid"}

    smtp = send_through_fake_smtp(monkeypatch, config)

    _, _, recipients, _ = smtp.calls[-1]
    assert recipients == ["admin@example.invalid", "oncall@example.invalid"]


# ------------------------------------------- failures are logged without secrets

SECRET = "SYNTHETICSECRET0123"
SECRET_URL = f"https://example.invalid/services/T000/B000/{SECRET}"


def deliver_slack(env, caplog):
    """Deliver through the real Slack notifier and return the log text it produced."""
    (notifier,) = hc.build_notifiers(env)
    with caplog.at_level(logging.INFO):
        assert hc.deliver([notifier], "SUBJECT", "BODY") == {"Slack": False}
    return caplog.text


def test_http_error_is_logged_with_its_status_but_without_the_webhook_url(monkeypatch, caplog):
    response = requests.Response()
    response.status_code = 404
    response.reason = "Not Found"
    response.url = SECRET_URL  # raise_for_status() puts this URL into the exception message
    monkeypatch.setattr(requests, "post", lambda url, **kw: response)

    log = deliver_slack({"SLACK_WEBHOOK_URL": SECRET_URL}, caplog)

    assert "Failed to send Slack alert: HTTPError (HTTP 404)" in log
    assert SECRET not in log


@pytest.mark.parametrize("error_type", [requests.ConnectionError, requests.Timeout])
def test_connection_failures_are_logged_without_the_webhook_url(error_type, monkeypatch, caplog):
    def fail(url, **kwargs):
        raise error_type(f"Max retries exceeded with url: /services/T000/B000/{SECRET}")

    monkeypatch.setattr(requests, "post", fail)

    log = deliver_slack({"SLACK_WEBHOOK_URL": SECRET_URL}, caplog)

    assert f"Failed to send Slack alert: {error_type.__name__}" in log
    assert SECRET not in log


def test_smtp_failure_is_logged_without_server_text_or_credentials(monkeypatch, caplog):
    class RefusingSMTP(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(
                535, f"Username monitor@example.invalid and password {EMAIL_ENV['EMAIL_PASSWORD']} not accepted".encode()
            )

    monkeypatch.setattr(smtplib, "SMTP", RefusingSMTP)
    (notifier,) = hc.build_notifiers(EMAIL_ENV)

    with caplog.at_level(logging.INFO):
        assert hc.deliver([notifier], "SUBJECT", "BODY") == {"Email": False}

    assert "Failed to send Email alert: SMTPAuthenticationError (SMTP 535)" in caplog.text
    assert EMAIL_ENV["EMAIL_PASSWORD"] not in caplog.text
    assert "monitor@example.invalid" not in caplog.text
