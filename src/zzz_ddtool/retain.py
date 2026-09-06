from __future__ import annotations

from pathlib import Path

MAX_CAPTURE_FILES = 40
MAX_CAPTURE_BYTES = 200 * 1024 * 1024
MAX_LOG_FILES = 15
MAX_LOG_BYTES = 16 * 1024 * 1024


def retain(
    directory: Path,
    *,
    pattern: str,
    max_files: int,
    max_bytes: int,
) -> list[Path]:
    """Delete oldest matching files until both caps hold. Newest files stay."""
    directory.mkdir(parents=True, exist_ok=True)
    files = [path for path in directory.glob(pattern) if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime)
    removed: list[Path] = []

    def over() -> bool:
        alive = [path for path in files if path.exists()]
        size = sum(path.stat().st_size for path in alive)
        return len(alive) > max_files or size > max_bytes

    while files and over():
        oldest = files.pop(0)
        try:
            oldest.unlink()
            removed.append(oldest)
        except OSError:
            break
    return removed


def retain_captures(directory: Path | None = None) -> list[Path]:
    from zzz_ddtool.paths import captures_dir

    return retain(
        directory or captures_dir(),
        pattern="zzz-*.png",
        max_files=MAX_CAPTURE_FILES,
        max_bytes=MAX_CAPTURE_BYTES,
    )


def retain_logs(directory: Path | None = None) -> list[Path]:
    from zzz_ddtool.paths import logs_dir

    return retain(
        directory or logs_dir(),
        pattern="*.log*",
        max_files=MAX_LOG_FILES,
        max_bytes=MAX_LOG_BYTES,
    )
