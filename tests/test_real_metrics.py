"""Smoke test against the real readings of the machine running the tests.

The other tests simulate metrics. This one only checks that psutil works on the
current operating system, Python and psutil version and returns usable
percentages. It would have caught psutil 5.9.5 raising SystemError from
disk_usage() on Windows with Python 3.12 and newer.
"""

import health_check as hc


def test_real_metrics_are_percentages():
    metrics = hc.collect_metrics()

    assert set(metrics) == {"cpu", "memory", "disk"}
    for name, value in metrics.items():
        assert 0 <= value <= 100, f"{name} reading {value} is not a percentage"
