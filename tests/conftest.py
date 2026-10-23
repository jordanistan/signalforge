"""Shared test isolation: no test may write into the live console's run/ directory."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


@pytest.fixture(autouse=True)
def _private_radio_snapshot(tmp_path, monkeypatch):
    """RadioWatch objects built without an explicit snapshot path write their survey summary here, not to
    run/radio-activity.json (a test with fake 'missing tools' used to overwrite the live console's file)."""
    import radiowatch
    monkeypatch.setattr(radiowatch, "SNAPSHOT", str(tmp_path / "radio-activity.json"))
