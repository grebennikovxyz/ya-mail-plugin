"""Owner-only encrypted storage explicitly selected for a dedicated Linux host."""

import os
import stat
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet, InvalidToken

from . import accounts
from .accounts import Problem


def cipher(create=False):
    if sys.platform != "linux":
        raise Problem("Локальное серверное хранилище доступно только на Linux.")
    directory = accounts.DATA
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise Problem(
            "Для локального хранилища нужен личный каталог владельца с правами 0700."
        )
    path = directory / "mail-vault.key"
    if create and not path.exists():
        with NamedTemporaryFile(dir=directory, delete=False) as f:
            tmp = Path(f.name)
            f.write(Fernet.generate_key())
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as f:
            info = os.fstat(f.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise Problem("Проверьте владельца ключа хранилища и права 0600.")
            return Fernet(f.read(64))
    except (ValueError, OSError):
        raise Problem("Ключ локального хранилища недоступен.") from None


def seal(value):
    return "local:" + cipher(create=True).encrypt(value.encode()).decode("ascii")


def unseal(record):
    try:
        return cipher().decrypt(record.removeprefix("local:").encode("ascii")).decode()
    except InvalidToken:
        raise Problem("Не удалось открыть запись локального хранилища.") from None
