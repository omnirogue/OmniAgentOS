import os
import stat
from pathlib import Path


def test_job_sh_is_executable():
    repo_root = Path(__file__).resolve().parent.parent.parent
    target_path = repo_root / "scripts" / "golden-suite" / "golden-suite.sh"

    st_mode = target_path.stat().st_mode
    observed_mode = stat.S_IMODE(st_mode)

    assert observed_mode == 0o755, f"Expected mode 0o755, but got {oct(observed_mode)}"
    assert os.access(target_path, os.X_OK), "File is not executable (os.X_OK failed)"
