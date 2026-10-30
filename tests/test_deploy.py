"""systemd unit files must parse cleanly (a quoting mistake once broke dresearch-analyze)."""
import shutil
import subprocess
from pathlib import Path

import pytest

UNITS = sorted((Path(__file__).parents[1] / "deploy").glob("dresearch-*.*"))


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not available")
@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_parses_without_warnings(unit):
    out = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    problems = [l for l in (out.stdout + out.stderr).splitlines()
                if l.strip() and "is not executable" not in l and "No such file" not in l]
    assert not problems, "\n".join(problems)


def test_analyze_unit_returns_immediately_and_quotes_args():
    text = (Path(__file__).parents[1] / "deploy" / "dresearch-analyze.service").read_text()
    assert 'Environment="ANALYZE_ARGS=--days 90 --sims 100"' in text
    assert "Type=simple" in text, "systemctl start must not block the terminal for an hour"
