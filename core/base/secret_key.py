"""Resolution of the Django SECRET_KEY.

Historically a fixed secret key was shipped in ``default.yaml`` (and another one in ``sample.env``).
Any installation still using one of those publicly known values allows session forgery and makes
long-lived API token hashes predictable. This module refuses to use them and falls back to a
randomly generated key that is persisted next to the configuration files.
"""

import logging
import os
import secrets

logger = logging.getLogger(__name__)

# Publicly known keys that previously shipped with the project. Never accept these.
INSECURE_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "89!yi9gvd2r*m5rq9y-elj86u*zg@-j3ce90@^7+0(-!fkgpzd",  # old default.yaml / config_models default
        "cd=u*s>z8ipv3*qr)ap4r@b8gkrwx!5x52trrf3icdmp_)2tQy",  # old sample.env
    }
)

SECRET_KEY_FILENAME = ".django_secret_key"
ALLOW_INSECURE_ENV = "PANDA_ALLOW_INSECURE_SECRET_KEY"


def is_insecure_secret_key(key: str | None) -> bool:
    return not key or key in INSECURE_SECRET_KEYS


def _load_or_create_key_file(path: str) -> str:
    """Read the persisted key, creating it atomically (0600) if missing.

    Uses O_EXCL so concurrent processes (web server + workers) starting at the same time
    end up sharing the same key instead of overwriting each other's.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, encoding="utf-8") as f:
            existing_key = f.read().strip()
        if existing_key:
            return existing_key
        # Empty file (e.g. another process is mid-write or a previous write failed): regenerate.
        new_key = secrets.token_urlsafe(50)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_key)
        os.chmod(path, 0o600)
        return new_key

    new_key = secrets.token_urlsafe(50)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_key)
    logger.warning("Generated a new random Django secret key at %s", path)
    return new_key


def resolve_secret_key(configured_key: str | None, config_dir: str | None) -> str:
    """Return a safe secret key.

    - A configured key that is not a known/weak value is used as-is.
    - Otherwise a random key is loaded from (or generated into) ``<config_dir>/.django_secret_key``.
    - Setting ``PANDA_ALLOW_INSECURE_SECRET_KEY=1`` keeps using the configured weak key (not recommended),
      e.g. to temporarily avoid invalidating existing sessions and API tokens while migrating.
    """
    if configured_key and not is_insecure_secret_key(configured_key):
        return configured_key

    if configured_key and os.environ.get(ALLOW_INSECURE_ENV, "").lower() in ("1", "true", "yes"):
        logger.warning(
            "Using an insecure/publicly known Django secret key because %s is set. "
            "Set 'general.django_secret_key' or DJANGO_SECRET_KEY to a long random value.",
            ALLOW_INSECURE_ENV,
        )
        return configured_key

    key_dir = config_dir or os.getcwd()
    key_path = os.path.join(key_dir, SECRET_KEY_FILENAME)

    if configured_key:
        logger.warning(
            "The configured Django secret key is a publicly known default; "
            "ignoring it and using the generated key in %s instead. Existing sessions and long-lived API "
            "tokens created with the old key are no longer valid. Set 'general.django_secret_key' or "
            "DJANGO_SECRET_KEY to a long random value to silence this warning.",
            key_path,
        )

    try:
        return _load_or_create_key_file(key_path)
    except OSError as e:
        raise RuntimeError(
            f"No secure Django secret key is configured and one could not be persisted to {key_path}: {e}. "
            "Set 'general.django_secret_key' in settings.yaml or the DJANGO_SECRET_KEY environment variable."
        ) from e
