import json
from pathlib import Path

import pytest

from scripts.bk2021 import __main__ as cli
from scripts.bk2021.models import TenantConfig
from scripts.bk2021.runner import FeedRun


def test_cli_forces_only_selected_feed_and_reports_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "tenant.yaml"
    path.write_text(
        "tenant: alpha\nfeeds:\n  - key: daily\n    queries: [BESS]\n    interval_hours: 24\n",
        encoding="utf-8",
    )

    def failed(config: TenantConfig, *, feed_key: str | None, force: bool) -> tuple[FeedRun, ...]:
        assert (config.tenant, feed_key, force) == ("alpha", "daily", True)
        return (FeedRun(key="daily", state="failed", error="source unavailable"),)

    monkeypatch.setattr(cli, "tick", failed)
    assert cli.main(["--config", str(path), "tick", "--feed", "daily", "--force"]) == 1
    assert json.loads(capsys.readouterr().out)[0]["state"] == "failed"
