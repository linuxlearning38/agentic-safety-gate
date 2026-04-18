import os
from pathlib import Path


def get_data_dir() -> Path:
    if os.name == "nt":
        default_dir = str(Path(__file__).resolve().parents[1] / ".ava-data")
    elif os.path.isdir("/data") and os.access("/data", os.W_OK):
        default_dir = "/data/runtime"
    else:
        default_dir = "/home/manoj/ava-data"
    raw = os.getenv("AVA_DATA_DIR", default_dir)
    if os.name == "nt" and raw.startswith("/"):
        raw = str(Path(__file__).resolve().parents[1] / ".ava-data")
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_runtime_path(env_var: str, default_filename: str) -> Path:
    raw = os.getenv(env_var)
    path = Path(raw).expanduser() if raw else (get_data_dir() / default_filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
