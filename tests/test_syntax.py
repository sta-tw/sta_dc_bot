from __future__ import annotations

from pathlib import Path

def _iter_python_files(root: Path) -> list[Path]:
    exclude_dirs = {".git", ".venv", "venv", "ENV", "__pycache__", "data"}
    files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        files.append(path)
    return files

def test_python_files_compile() -> None:
    root = Path(__file__).resolve().parents[1]
    files = _iter_python_files(root)
    assert files, "No Python files found to compile."
    for path in files:
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")
