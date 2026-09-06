from pathlib import Path

from zzz_ddtool.retain import retain


def test_retain_drops_oldest_over_file_cap(tmp_path: Path) -> None:
    for index in range(5):
        path = tmp_path / f"{index}.png"
        path.write_bytes(b"x" * 10)
        path.stat()
    removed = retain(tmp_path, pattern="*.png", max_files=3, max_bytes=10_000)
    left = sorted(p.name for p in tmp_path.glob("*.png"))
    assert left == ["2.png", "3.png", "4.png"]
    assert [p.name for p in removed] == ["0.png", "1.png"]


def test_retain_drops_oldest_over_byte_cap(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.png").write_bytes(b"y" * 40)
    retain(tmp_path, pattern="*.png", max_files=10, max_bytes=90)
    left = list(tmp_path.glob("*.png"))
    assert len(left) == 2
    assert "0.png" not in {p.name for p in left}
