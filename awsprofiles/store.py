"""Profile operations on top of ~/.aws, with backups and permission repair."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import sts
from .credfile import CREDENTIAL_KEYS, IniFile, atomic_write, backup
from .parsers import ParsedCredentials, PROFILE_NAME_RE, mask


class StoreError(Exception):
    """A requested change was rejected before anything was written."""


@dataclass
class Profile:
    name: str
    access_key_id: str | None = None
    has_secret: bool = False
    is_temporary: bool = False
    region: str | None = None
    expires_at: datetime | None = None
    updated_at: datetime | None = None
    identity: sts.Identity | None = None
    mirrors: str | None = None  # for [default]: the profile it was copied from
    client: str | None = None  # name of the Client whose portal issues these
    nickname: str | None = None  # a friendly label; the ini section name stays as is

    @property
    def label(self) -> str:
        """What to call this profile in the UI: the nickname when there is one."""
        return self.nickname or self.name

    @property
    def display_name(self) -> str:
        """Nickname first, real section name after it so it is never hidden."""
        return f"{self.nickname}   ·   {self.name}" if self.nickname else self.name

    @property
    def status(self) -> sts.Status:
        if self.identity is not None:
            return self.identity.status
        if self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc):
            return sts.Status.EXPIRED
        return sts.Status.UNKNOWN

    @property
    def is_complete(self) -> bool:
        return bool(self.access_key_id and self.has_secret)

    @property
    def masked_key(self) -> str:
        return mask(self.access_key_id)

    def seconds_left(self) -> int | None:
        if self.expires_at is None:
            return None
        return int((self.expires_at - datetime.now(timezone.utc)).total_seconds())

    @property
    def urgency(self) -> str:
        """``expired``, ``soon`` (under an hour), ``ok`` or ``none`` for static keys.

        Temporary credentials are the reason the app exists, so their remaining
        life is surfaced as a colour rather than buried in a column.
        """
        if self.status is sts.Status.EXPIRED or self.status is sts.Status.INVALID:
            return "expired"
        seconds = self.seconds_left()
        if seconds is None:
            return "none"
        if seconds <= 0:
            return "expired"
        return "soon" if seconds < 3600 else "ok"

    def time_left(self) -> str | None:
        """Human phrasing of the remaining lifetime, when it is known."""
        seconds = self.seconds_left()
        if seconds is None:
            return None
        if seconds <= 0:
            return "expired"
        hours, minutes = divmod(seconds // 60, 60)
        if hours >= 24:
            return f"{hours // 24}d {hours % 24}h left"
        return f"{hours}h {minutes}m left" if hours else f"{minutes}m left"


@dataclass
class ChangeResult:
    message: str
    backup_path: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Client:
    """One organisation you sign in to: its access portal and the sign-in name used there.

    ``email`` holds whatever the portal's Username box wants: an email address for
    most Identity Center directories, a plain username for others.

    Profiles are grouped under the client whose portal issues their credentials,
    so the menu can take you straight to the right sign-in page.
    """

    name: str
    login_url: str
    email: str = ""


CLIENT_NAME_RE = re.compile(r"^[^\s\[\]]{1,40}$")


class ProfileStore:
    def __init__(self, aws_dir: str | None = None):
        # AWSPROFILES_DIR lets the test-suite (and a curious user) point the
        # app at a throwaway copy instead of the real ~/.aws.
        self.aws_dir = (
            aws_dir
            or os.environ.get("AWSPROFILES_DIR")
            or os.path.join(os.path.expanduser("~"), ".aws")
        )
        self.credentials_path = os.path.join(self.aws_dir, "credentials")
        self.config_path = os.path.join(self.aws_dir, "config")
        self.backup_dir = os.path.join(self.aws_dir, "awsprofiles-backups")
        self.state_path = os.path.join(self.aws_dir, ".awsprofiles-state.json")
        # Identity checks record their result from worker threads while the
        # main thread saves client assignments; without this, two overlapping
        # read-modify-write cycles would silently drop one of the changes.
        self._lock = threading.RLock()

    # ------------------------------------------------------------ persistence

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            atomic_write(self.state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
        except OSError:
            pass  # state is a convenience cache; never fail a write because of it

    def _profile_state(self, name: str) -> dict:
        return self._load_state().get("profiles", {}).get(name, {})

    def _record(self, name: str, **fields) -> None:
        with self._lock:
            state = self._load_state()
            profiles = state.setdefault("profiles", {})
            profiles.setdefault(name, {}).update(fields)
            self._save_state(state)

    # ----------------------------------------------------------------- safety

    def check_permissions(self) -> list[str]:
        """Report ~/.aws files that are readable by anyone but the owner."""
        warnings = []
        for path in (self.credentials_path, self.config_path):
            if not os.path.exists(path):
                continue
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                warnings.append(f"{os.path.basename(path)} is mode {mode:04o} — other users can read it.")
        return warnings

    def fix_permissions(self) -> list[str]:
        fixed = []
        for path in (self.credentials_path, self.config_path):
            if os.path.exists(path) and stat.S_IMODE(os.stat(path).st_mode) & 0o077:
                os.chmod(path, 0o600)
                fixed.append(os.path.basename(path))
        return fixed

    def _write(self, ini: IniFile, path: str) -> str | None:
        """Back up, then atomically replace ``path`` with ``ini``'s contents."""
        snapshot = backup(path, self.backup_dir)
        atomic_write(path, ini.render())
        return snapshot

    # ---------------------------------------------------------------- reading

    def _config_section_for(self, config: IniFile, name: str) -> str | None:
        """Find a profile's config section, tolerating a missing ``profile `` prefix."""
        if name == "default":
            return "default" if config.section("default") else None
        for candidate in (f"profile {name}", name):
            if config.section(candidate):
                return candidate
        return None

    def list_profiles(self) -> list[Profile]:
        credentials = IniFile.load(self.credentials_path)
        config = IniFile.load(self.config_path)
        state = self._load_state()
        profile_state = state.get("profiles", {})
        clients = state.get("clients", {})

        profiles = []
        for name in credentials.section_names():
            values = credentials.values(name)
            saved = profile_state.get(name, {})
            section = self._config_section_for(config, name)
            profiles.append(
                Profile(
                    name=name,
                    access_key_id=values.get("aws_access_key_id"),
                    has_secret=bool(values.get("aws_secret_access_key")),
                    is_temporary=bool(values.get("aws_session_token") or values.get("aws_security_token")),
                    region=config.get(section, "region") if section else None,
                    expires_at=_parse_iso(saved.get("expires_at")),
                    updated_at=_parse_iso(saved.get("updated_at")),
                    mirrors=state.get("default_mirrors") if name == "default" else None,
                    client=saved.get("client") if saved.get("client") in clients else None,
                    nickname=saved.get("nickname") or None,
                )
            )
        return profiles

    def config_warnings(self) -> list[str]:
        """Config sections AWS silently ignores because they lack ``profile ``."""
        config = IniFile.load(self.config_path)
        credentials = set(IniFile.load(self.credentials_path).section_names())
        warnings = []
        for name in config.section_names():
            if name == "default" or name.startswith("profile "):
                continue
            hint = " (it matches a credentials profile)" if name in credentials else ""
            warnings.append(
                f"~/.aws/config has [{name}], but non-default config sections must be "
                f"written as [profile {name}] — AWS ignores this one{hint}."
            )
        return warnings

    def identity_for(self, name: str, timeout: float = 8.0) -> sts.Identity:
        values = IniFile.load(self.credentials_path).values(name)
        identity = sts.check(
            values.get("aws_access_key_id", ""),
            values.get("aws_secret_access_key", ""),
            values.get("aws_session_token") or values.get("aws_security_token"),
            timeout=timeout,
        )
        if identity.status is sts.Status.VALID:
            self._record(name, account=identity.account, arn=identity.arn)
        return identity

    # ---------------------------------------------------------------- writing

    def _validate_name(self, name: str, must_be_new: bool = False) -> str:
        name = name.strip()
        if not name:
            raise StoreError("A profile name is required.")
        if not PROFILE_NAME_RE.match(name):
            raise StoreError(
                f"'{name}' is not a valid profile name.\n"
                "Use letters, digits and . _ - @ : + = , with no spaces."
            )
        if must_be_new and IniFile.load(self.credentials_path).section(name):
            raise StoreError(f"A profile named '{name}' already exists.")
        return name

    def upsert(self, name: str, creds: ParsedCredentials) -> ChangeResult:
        """Write ``creds`` into profile ``name``, creating it if necessary."""
        with self._lock:
            name = self._validate_name(name)
            credentials = IniFile.load(self.credentials_path)
            existed = credentials.section(name) is not None

            # Drop any stale token when replacing temporary creds with static ones,
            # so an expired token can never linger and shadow a working key.
            stale = tuple(k for k in CREDENTIAL_KEYS if k not in creds.to_ini_values())
            credentials.set_values(name, creds.to_ini_values(), remove=stale)
            snapshot = self._write(credentials, self.credentials_path)

            self._record(
                name,
                updated_at=datetime.now(timezone.utc).isoformat(),
                expires_at=creds.expiration.isoformat() if creds.expiration else None,
                temporary=creds.is_temporary,
            )

            warnings = []
            if creds.region:
                try:
                    self.set_region(name, creds.region)
                except Exception as exc:
                    warnings.append(f"Could not set region: {exc}")

            # Keep [default] in step when it is a mirror of the profile just updated.
            if self._load_state().get("default_mirrors") == name and name != "default":
                try:
                    self.set_default(name, _skip_backup=True)
                    warnings.append(f"[default] also updated (it mirrors {name}).")
                except Exception as exc:
                    warnings.append(f"Could not refresh [default]: {exc}")

            verb = "Updated" if existed else "Created"
            kind = "temporary" if creds.is_temporary else "long-lived"
            return ChangeResult(f"{verb} [{name}] with {kind} credentials.", snapshot, warnings)

    def set_region(self, name: str, region: str) -> None:
        config = IniFile.load(self.config_path)
        section = self._config_section_for(config, name)
        if section is None:
            section = "default" if name == "default" else f"profile {name}"
        config.set_values(section, {"region": region})
        self._write(config, self.config_path)

    def set_default(self, name: str, _skip_backup: bool = False) -> ChangeResult:
        """Copy ``name``'s credentials into ``[default]``.

        A menu bar app cannot change the environment of shells that are already
        running, so pointing ``[default]`` at a profile is the way to switch the
        profile every tool picks up — old terminals included.
        """
        with self._lock:
            if name == "default":
                raise StoreError("[default] is already the default profile.")
            credentials = IniFile.load(self.credentials_path)
            source = credentials.values(name)
            if not source.get("aws_access_key_id") or not source.get("aws_secret_access_key"):
                raise StoreError(f"[{name}] has no usable credentials to copy.")

            values = {k: source[k] for k in ("aws_access_key_id", "aws_secret_access_key") if k in source}
            token = source.get("aws_session_token") or source.get("aws_security_token")
            if token:
                values["aws_session_token"] = token
            stale = tuple(k for k in CREDENTIAL_KEYS if k not in values)

            credentials.set_values("default", values, remove=stale)
            snapshot = self._write(credentials, self.credentials_path)

            state = self._load_state()
            state["default_mirrors"] = name
            self._save_state(state)

            warnings = []
            region = self.region_for(name)
            if region:
                try:
                    self.set_region("default", region)
                except Exception as exc:
                    warnings.append(f"Could not copy region: {exc}")
            return ChangeResult(f"[default] now uses the credentials from [{name}].", snapshot, warnings)

    def region_for(self, name: str) -> str | None:
        config = IniFile.load(self.config_path)
        section = self._config_section_for(config, name)
        return config.get(section, "region") if section else None

    def rename(self, old: str, new: str) -> ChangeResult:
        with self._lock:
            new = self._validate_name(new, must_be_new=True)
            credentials = IniFile.load(self.credentials_path)
            if not credentials.section(old):
                raise StoreError(f"There is no profile named '{old}'.")
            credentials.rename_section(old, new)
            snapshot = self._write(credentials, self.credentials_path)

            config = IniFile.load(self.config_path)
            section = self._config_section_for(config, old)
            if section:
                config.rename_section(section, "default" if new == "default" else f"profile {new}")
                self._write(config, self.config_path)

            state = self._load_state()
            profiles = state.setdefault("profiles", {})
            if old in profiles:
                profiles[new] = profiles.pop(old)
            if state.get("default_mirrors") == old:
                state["default_mirrors"] = new
            self._save_state(state)
            return ChangeResult(f"Renamed [{old}] to [{new}].", snapshot)

    def delete(self, name: str) -> ChangeResult:
        with self._lock:
            credentials = IniFile.load(self.credentials_path)
            if not credentials.section(name):
                raise StoreError(f"There is no profile named '{name}'.")
            credentials.remove_section(name)
            snapshot = self._write(credentials, self.credentials_path)

            state = self._load_state()
            state.get("profiles", {}).pop(name, None)
            if state.get("default_mirrors") == name:
                state.pop("default_mirrors", None)
            self._save_state(state)

            warnings = []
            if name != "default":
                warnings.append(f"~/.aws/config still holds any [profile {name}] settings.")
            return ChangeResult(f"Deleted [{name}].", snapshot, warnings)

    # ---------------------------------------------------------------- clients

    def list_clients(self) -> list[Client]:
        raw = self._load_state().get("clients", {})
        clients = []
        for name, values in raw.items():
            if isinstance(values, dict) and values.get("login_url"):
                clients.append(Client(name, values["login_url"], values.get("email") or ""))
        return sorted(clients, key=lambda c: c.name.lower())

    def client(self, name: str | None) -> Client | None:
        if not name:
            return None
        return next((c for c in self.list_clients() if c.name == name), None)

    def client_for(self, profile_name: str) -> Client | None:
        return self.client(self._profile_state(profile_name).get("client"))

    def save_client(self, name: str, login_url: str, email: str = "") -> Client:
        """Create or update a client. The URL is normalised; a bare host gets https."""
        with self._lock:
            name = name.strip()
            if not CLIENT_NAME_RE.match(name):
                raise StoreError("A client name is required: up to 40 characters, no spaces or brackets.")
            login_url = login_url.strip()
            if not login_url:
                raise StoreError("A login URL is required.")
            if "://" not in login_url:
                login_url = "https://" + login_url
            if not login_url.lower().startswith("https://"):
                raise StoreError("The login URL must start with https:// — AWS portals are never plain http.")
            email = email.strip()
            if any(ch.isspace() for ch in email):
                raise StoreError("The email or username cannot contain spaces.")

            state = self._load_state()
            state.setdefault("clients", {})[name] = {"login_url": login_url, "email": email}
            self._save_state(state)
            return Client(name, login_url, email)

    def rename_client(self, old: str, new: str) -> Client:
        """Rename a client; every profile grouped under it follows."""
        with self._lock:
            new = new.strip()
            state = self._load_state()
            clients = state.get("clients", {})
            if old not in clients:
                raise StoreError(f"There is no client named '{old}'.")
            if not CLIENT_NAME_RE.match(new):
                raise StoreError("A client name is required: up to 40 characters, no spaces or brackets.")
            if new != old and new in clients:
                raise StoreError(f"A client named '{new}' already exists.")
            if new != old:
                clients[new] = clients.pop(old)
                for saved in state.get("profiles", {}).values():
                    if saved.get("client") == old:
                        saved["client"] = new
                if state.get("last_client") == old:
                    state["last_client"] = new
                self._save_state(state)
            values = clients[new]
            return Client(new, values["login_url"], values.get("email") or "")

    def delete_client(self, name: str) -> ChangeResult:
        """Forget a client and unassign its profiles. Credentials are untouched."""
        with self._lock:
            state = self._load_state()
            if name not in state.get("clients", {}):
                raise StoreError(f"There is no client named '{name}'.")
            del state["clients"][name]
            freed = [p for p, saved in state.get("profiles", {}).items() if saved.get("client") == name]
            for profile in freed:
                state["profiles"][profile].pop("client", None)
            self._save_state(state)
            detail = f" {len(freed)} profile(s) are no longer grouped under it." if freed else ""
            return ChangeResult(f"Removed client {name}.{detail}")

    def assign_client(self, profile_name: str, client_name: str | None) -> None:
        """Group ``profile_name`` under a client, or ungroup it with ``None``."""
        with self._lock:
            if client_name is not None and self.client(client_name) is None:
                raise StoreError(f"There is no client named '{client_name}'.")
            state = self._load_state()
            saved = state.setdefault("profiles", {}).setdefault(profile_name, {})
            if client_name is None:
                saved.pop("client", None)
            else:
                saved["client"] = client_name
                state["last_client"] = client_name
            self._save_state(state)

    def set_nickname(self, profile_name: str, nickname: str | None) -> None:
        """Give a profile a friendly label, or clear it with ``None`` or blank."""
        nickname = (nickname or "").strip()
        if len(nickname) > 40 or "\n" in nickname:
            raise StoreError("A nickname is a short label: up to 40 characters, one line.")
        with self._lock:
            state = self._load_state()
            saved = state.setdefault("profiles", {}).setdefault(profile_name, {})
            if nickname:
                saved["nickname"] = nickname
            else:
                saved.pop("nickname", None)
            self._save_state(state)

    def last_client(self) -> Client | None:
        """The client most recently assigned — the likely home for a new profile."""
        return self.client(self._load_state().get("last_client"))

    def preference(self, key: str, default=None):
        return self._load_state().get("preferences", {}).get(key, default)

    def set_preference(self, key: str, value) -> None:
        with self._lock:
            state = self._load_state()
            state.setdefault("preferences", {})[key] = value
            self._save_state(state)

    def open_url(self, url: str) -> None:
        if not url.lower().startswith("https://"):
            raise StoreError("Only https links can be opened.")
        subprocess.run(["open", url], check=False)

    # ---------------------------------------------------------------- backups

    def list_backups(self) -> list[tuple[str, datetime]]:
        try:
            names = os.listdir(self.backup_dir)
        except FileNotFoundError:
            return []
        snapshots = []
        for name in names:
            path = os.path.join(self.backup_dir, name)
            if os.path.isfile(path):
                snapshots.append((path, datetime.fromtimestamp(os.path.getmtime(path))))
        return sorted(snapshots, key=lambda item: item[1], reverse=True)

    def restore(self, snapshot_path: str) -> ChangeResult:
        """Put a backup back, after first backing up the current file."""
        # Only ever read from inside the backup directory: restore() copies the
        # file's contents straight over ~/.aws/credentials, so an arbitrary path
        # here would be a way to write arbitrary content into that file.
        backup_root = os.path.realpath(self.backup_dir)
        resolved = os.path.realpath(snapshot_path)
        if os.path.commonpath([resolved, backup_root]) != backup_root:
            raise StoreError("Only files in ~/.aws/awsprofiles-backups can be restored.")
        if not os.path.isfile(resolved):
            raise StoreError("That backup no longer exists.")
        snapshot_path = resolved
        target = self.credentials_path
        if os.path.basename(snapshot_path).startswith("config."):
            target = self.config_path
        snapshot = backup(target, self.backup_dir)
        with open(snapshot_path, "r", encoding="utf-8") as fh:
            atomic_write(target, fh.read())
        return ChangeResult(f"Restored {os.path.basename(target)} from {os.path.basename(snapshot_path)}.", snapshot)

    # ------------------------------------------------------------------ misc

    def reveal(self, path: str | None = None) -> None:
        subprocess.run(["open", "-R", path or self.credentials_path], check=False)

    def open_in_editor(self) -> None:
        subprocess.run(["open", "-t", self.credentials_path], check=False)

    def disk_free_ok(self) -> bool:
        try:
            return shutil.disk_usage(self.aws_dir).free > 1_000_000
        except OSError:
            return True


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
