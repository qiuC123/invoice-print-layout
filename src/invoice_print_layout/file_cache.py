"""File identities for bounded, process-local caches; missing files never hit."""
from pathlib import Path
from functools import lru_cache
import hashlib

FileSignature = tuple[str, int, int, int]


def file_signature(path: Path) -> FileSignature:
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ino


@lru_cache(maxsize=2048)
def _digest(signature: FileSignature) -> str:
    path = Path(signature[0])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if file_signature(path) != signature:
        raise OSError('Source changed while reading')
    return digest


def file_digest(path: Path) -> str:
    signature = file_signature(path)
    digest = _digest(signature)
    if file_signature(path) != signature:
        raise OSError('Source changed while reading')
    return digest
