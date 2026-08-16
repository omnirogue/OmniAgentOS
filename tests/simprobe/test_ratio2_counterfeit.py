import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_behavioral_suite_rejects_zero_whole_counterfeit(tmp_path):
    package = tmp_path / "omniagentos"
    simprobe = package / "simprobe"
    simprobe.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (simprobe / "__init__.py").write_text("")
    (simprobe / "ratio2.py").write_text(
        "def safe_share(part, whole):\n"
        "    return 1.0 if whole == 0 else part / whole\n"
    )

    behavioral_test = Path(__file__).resolve().with_name("test_ratio2.py")
    shutil.copyfile(behavioral_test, tmp_path / "test_ratio2.py")

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "test_ratio2.py", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    output = proc.stdout + proc.stderr

    assert proc.returncode != 0, output
    assert "test_zero_whole_returns_none" in output
    assert "2 passed" in output
