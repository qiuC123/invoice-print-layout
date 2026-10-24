"""File identities for bounded, process-local caches; missing files never hit."""
from pathlib import Path

FileSignature = tuple[str, int, int, int]


def file_signature(path: Path) -> FileSignature:
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ino
