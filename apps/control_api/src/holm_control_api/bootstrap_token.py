"""Generation and delivery of the one-time initialization Token.

Bootstrap Token delivery is restricted: the plaintext goes to the install
terminal and a root-readable file, and nowhere else.
When installation has not provisioned a Token, the Control API generates it on
a first start of an uninitialized platform. When the installer takes over it
replaces *this* module only; the verification path in ``bootstrap_service`` and
the ``core.bootstrap_tokens`` table do not move.

Three properties this module is responsible for:

*   the plaintext is written to exactly one file and logged exactly once, and
    the database only ever receives the SHA-256 digest;
*   the file's mode is ``0400`` even under a permissive ``umask`` -- ``open``'s
    mode argument is masked, so the mode is set explicitly with ``fchmod``
    afterwards (the same trap found on the Agent socket);
*   issuance is skipped entirely once the platform is initialized, so a
    restart of a running platform never mints a fresh way in.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from holm_persistence import (
    AsyncSessionFactory,
    BootstrapTokenRepository,
    SystemInitializationRepository,
    database_utc_now,
)
from holm_security import generate_token, hash_token

DEFAULT_BOOTSTRAP_TOKEN_PATH: Final[Path] = Path("/etc/holm/secrets/bootstrap-token")
#: Default bootstrap Token path; the installer owns path configuration.
BOOTSTRAP_TOKEN_LIFETIME: Final[timedelta] = timedelta(minutes=30)
BOOTSTRAP_TOKEN_ENTROPY_BITS: Final[int] = 256
BOOTSTRAP_TOKEN_FILE_MODE: Final[int] = 0o400
BOOTSTRAP_TOKEN_DIRECTORY_MODE: Final[int] = 0o700
BOOTSTRAP_TOKEN_ISSUED_EVENT: Final[str] = "bootstrap_token_issued"

_LOGGER: Final[logging.Logger] = logging.getLogger("holm.control_api")


class BootstrapTokenDeliveryError(RuntimeError):
    """The plaintext could not be delivered, so no Token was left usable."""


@dataclass(frozen=True, slots=True)
class IssuedBootstrapToken:
    """What the caller learns after a Token was minted; no plaintext is returned.

    The plaintext deliberately never crosses this boundary: it exists inside
    :meth:`BootstrapTokenIssuer.ensure_token` and reaches only the file and the
    single log record. Returning it would create a third copy whose lifetime
    this module could no longer reason about.
    """

    path: Path
    expires_in_seconds: int


def write_token_file(path: Path, plaintext: str) -> None:
    """Write the plaintext to ``path`` with mode 0400, replacing any old copy.

    The write goes to a temporary sibling and is then renamed, so a reader never
    observes a half-written Token and a failed write never destroys a usable
    one. ``fchmod`` runs before any content is written: a permissive ``umask``
    would otherwise leave the file world-readable for the length of the write.
    """
    directory = path.parent
    directory.mkdir(mode=BOOTSTRAP_TOKEN_DIRECTORY_MODE, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask too, and an existing directory keeps
    # whatever mode it already had.
    directory.chmod(BOOTSTRAP_TOKEN_DIRECTORY_MODE)
    temporary = directory / f".{path.name}.new"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(temporary, flags, BOOTSTRAP_TOKEN_FILE_MODE)
    try:
        os.fchmod(descriptor, BOOTSTRAP_TOKEN_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(f"{plaintext}\n")
            handle.flush()
            os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    os.replace(temporary, path)


class BootstrapTokenIssuer:
    """Mint at most one usable initialization Token for an uninitialized platform."""

    __slots__ = ("_lifetime", "_logger", "_session_factory", "_token_path")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        token_path: Path = DEFAULT_BOOTSTRAP_TOKEN_PATH,
        lifetime: timedelta = BOOTSTRAP_TOKEN_LIFETIME,
        logger: logging.Logger = _LOGGER,
    ) -> None:
        if lifetime <= timedelta(0):
            raise ValueError("bootstrap token lifetime must be positive")
        self._session_factory = session_factory
        self._token_path = token_path
        self._lifetime = lifetime
        self._logger = logger

    @property
    def token_path(self) -> Path:
        return self._token_path

    async def ensure_token(self) -> IssuedBootstrapToken | None:
        """Issue a Token if and only if the platform is uninitialized and has none.

        The file is written before the transaction commits: if delivery fails
        there must be no Token in the database that nobody can present, and if
        the commit fails the leftover file is merely unusable.
        """
        async with self._session_factory() as session:
            if await SystemInitializationRepository(session).read() is not None:
                return None
            tokens = BootstrapTokenRepository(session)
            if await tokens.count_usable() > 0:
                return None
            plaintext = generate_token(entropy_bits=BOOTSTRAP_TOKEN_ENTROPY_BITS)
            now = await database_utc_now(session)
            await tokens.create(
                token_hash=hash_token(plaintext),
                expires_at=now + self._lifetime,
                now=now,
            )
            try:
                write_token_file(self._token_path, plaintext)
            except OSError as error:
                await session.rollback()
                raise BootstrapTokenDeliveryError(
                    "the one-time initialization token could not be written"
                ) from error
            await session.commit()
            # The one place the plaintext is logged. Bootstrap delivery requires an
            # operator-visible copy; nothing else in the platform may reproduce
            # it, and no request-scoped logger ever sees it.
            self._logger.warning(
                "one-time initialization token issued",
                extra={
                    "event_name": BOOTSTRAP_TOKEN_ISSUED_EVENT,
                    "component": "control-api",
                    "bootstrap_token": plaintext,
                    "bootstrap_token_path": str(self._token_path),
                    "expires_in_seconds": int(self._lifetime.total_seconds()),
                },
            )
            return IssuedBootstrapToken(
                path=self._token_path,
                expires_in_seconds=int(self._lifetime.total_seconds()),
            )


__all__ = [
    "BOOTSTRAP_TOKEN_DIRECTORY_MODE",
    "BOOTSTRAP_TOKEN_ENTROPY_BITS",
    "BOOTSTRAP_TOKEN_FILE_MODE",
    "BOOTSTRAP_TOKEN_ISSUED_EVENT",
    "BOOTSTRAP_TOKEN_LIFETIME",
    "DEFAULT_BOOTSTRAP_TOKEN_PATH",
    "BootstrapTokenDeliveryError",
    "BootstrapTokenIssuer",
    "IssuedBootstrapToken",
    "write_token_file",
]
