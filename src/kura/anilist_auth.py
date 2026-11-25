"""AniList OAuth token handling: client ID, PIN-flow URL, secret token file.

The access token is a bearer credential. It lives only in ``anilist-auth.json`` inside Kura's
private state directory (dir 0700, file 0600, symlinks refused) and is attached to requests only
by :func:`kura.anilist.graphql_fetch`. It is never accepted as an argument, never read from the
environment, and never placed in output, errors or logs: callers show :func:`fingerprint` only.
Library code here never prints or prompts.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import urllib.parse

from .anilist import valid_token
from .models import KuraError
from .safety import ensure_dir, no_symlinks

DEFAULT_CLIENT_ID = None        # public identifier, set in a 1-line PR once the owner registers the app
CLIENT_ID_ENV = "KURA_ANILIST_CLIENT_ID"
AUTHORIZE = "https://anilist.co/api/v2/oauth/authorize"
TOKEN_FILE = "anilist-auth.json"
TOKEN_MAX_BYTES = 8192
FILE_LIMIT = 16 * 1024
YEAR = 365 * 86400
WARN_DAYS = 30
VERSION = 1


def resolve_client_id(option=None, env=None) -> int:
    """`--client-id` > environment > DEFAULT_CLIENT_ID. Public, not secret."""
    value = option
    if value is None:
        value = (env if env is not None else os.environ).get(CLIENT_ID_ENV) or None
    if value is None:
        value = DEFAULT_CLIENT_ID
    if value is None:
        raise KuraError("anilist_client_missing",
                        "No AniList client ID: pass --client-id N or set " + CLIENT_ID_ENV)
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise KuraError("anilist_client_missing", "The AniList client ID must be a positive integer") from None
    if not 0 < number < 10**9 or isinstance(value, bool):
        raise KuraError("anilist_client_missing", "The AniList client ID must be a positive integer")
    return number


def authorize_url(client_id: int) -> str:
    return AUTHORIZE + "?" + urllib.parse.urlencode({"client_id": client_id, "response_type": "token"})


def fingerprint(token: str) -> str:
    """Short one-way tag for telling tokens apart. Not reversible, not the token."""
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:8]


def clean_token(text) -> str:
    """One pasted token, or an error that never repeats what was pasted."""
    if not isinstance(text, str):
        raise KuraError("anilist_token_invalid", "No token was provided")
    text = text.strip()
    if not valid_token(text):
        raise KuraError("anilist_token_invalid",
                        "That does not look like an AniList access token (one line, no spaces)")
    return text


def read_stdin_token(stream) -> str:
    """Read one line of at most 8 KiB from `stream`."""
    raw = stream.read(TOKEN_MAX_BYTES + 2)
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    if len(raw) > TOKEN_MAX_BYTES + 1:
        raise KuraError("anilist_token_invalid", "The token input is too long")
    return clean_token(raw)


def jwt_expiry(token: str):
    """The `exp` claim of an (unverified) JWT payload as epoch seconds, or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload.encode())).get("exp")
    except (ValueError, UnicodeError, AttributeError):
        return None
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not 0 < exp < 4_102_444_800:
        return None
    return float(exp)


def expiry(token: str, issued_at: float) -> tuple[float, str]:
    exp = jwt_expiry(token)
    return (exp, "jwt_exp") if exp is not None else (issued_at + YEAR, "issued_plus_365d")


def expiry_state(expires_at: float, now: float) -> tuple[float, list[str]]:
    """(days left, warning codes)."""
    days = (expires_at - now) / 86400
    if days <= 0:
        return days, ["anilist_token_expired"]
    return days, ["anilist_token_expires_soon"] if days < WARN_DAYS else []


# -- token file ---------------------------------------------------------------------

def path(root: Path) -> Path:
    return Path(root) / TOKEN_FILE


def _secure_dir(root: Path):
    ensure_dir(Path(root))      # refuses symlinked components
    os.chmod(root, 0o700)


def save(root: Path, record: dict):
    """Atomically write the token record: temp file O_EXCL|O_NOFOLLOW 0600, then os.replace."""
    _secure_dir(root)
    target = path(root)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and not stat.S_ISREG(mode):
        raise KuraError("unsafe_path", "The token path is not a regular file")
    temp = Path(root) / f".{TOKEN_FILE}.{secrets.token_hex(8)}.tmp"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(record, sort_keys=True).encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def load(root: Path) -> dict:
    """The stored record. Refuses symlinks and any file or directory readable by others."""
    root = Path(root)
    target = path(root)
    no_symlinks(root)       # every component, as on save
    try:
        if root.is_symlink() or stat.S_IMODE(root.lstat().st_mode) & 0o077:
            raise KuraError("anilist_token_insecure", "The Kura state directory must be private (0700)")
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise KuraError("anilist_not_logged_in", "Not logged in: run kura anilist login") from None
    except OSError:
        raise KuraError("unsafe_path", "The token file cannot be opened safely") from None
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise KuraError("unsafe_path", "The token path is not a regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise KuraError("anilist_token_insecure", "The token file must be private (0600)")
        raw = f.read(FILE_LIMIT + 1)
    if len(raw) > FILE_LIMIT:
        raise KuraError("anilist_token_invalid", "The token file is malformed")
    try:
        record = json.loads(raw.decode("utf-8"))
        viewer = record["viewer"]
        ok = (record["version"] == VERSION and valid_token(record["token"]) and
              isinstance(record["client_id"], int) and isinstance(viewer["id"], int) and viewer["id"] > 0 and
              isinstance(viewer["name"], str) and
              all(isinstance(record[k], (int, float)) for k in ("issued_at", "expires_at")) and
              record["expires_source"] in ("jwt_exp", "issued_plus_365d"))
    except (ValueError, KeyError, TypeError, UnicodeError):
        ok = False
    if not ok:
        raise KuraError("anilist_token_invalid", "The token file is malformed; log in again")
    return record


def remove(root: Path) -> bool:
    """Delete the token file. Returns whether one existed. Refuses a symlink."""
    no_symlinks(Path(root))
    target = path(root)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        raise KuraError("unsafe_path", "The token path is not a regular file")
    os.unlink(target)
    return True


def record_for(token: str, client_id: int, viewer: dict, now: float) -> dict:
    expires_at, source = expiry(token, now)
    return {"version": VERSION, "token": token, "client_id": client_id, "issued_at": now,
            "expires_at": expires_at, "expires_source": source,
            "viewer": {"id": viewer["id"], "name": viewer["name"]}, "verified_at": now}


def public(record: dict, now: float) -> dict:
    """Everything about a record that is safe to show: never the token itself."""
    days, warnings = expiry_state(record["expires_at"], now)
    return {"viewer": record["viewer"], "client_id": record["client_id"],
            "issued_at": record["issued_at"], "expires_at": record["expires_at"],
            "expires_source": record["expires_source"], "expires_in_days": round(days, 1),
            "token_fingerprint": fingerprint(record["token"]), "verified_at": record.get("verified_at"),
            "warnings": warnings}
