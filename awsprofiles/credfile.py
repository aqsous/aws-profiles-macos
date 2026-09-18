"""Line-preserving reader/writer for AWS shared credential/config files.

The AWS files are hand-edited by humans: they carry comments, blank-line
grouping and a mix of ``key=value`` and ``key = value`` styles. A naive
``configparser`` round-trip silently throws all of that away, so this module
keeps the file as a list of raw lines and rewrites only the lines it must.

Anything this module is not explicitly asked to change stays byte-identical.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
import time
from datetime import datetime

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
ENTRY_RE = re.compile(r"^(?P<indent>\s*)(?P<key>[^\s=#;\[][^=]*?)(?P<sep>\s*=\s*)(?P<value>.*?)\s*$")
COMMENT_RE = re.compile(r"^\s*[#;]")

# Only these keys are ever rewritten or removed when credentials are updated.
# Everything else in a profile (region, output, role_arn, custom keys) is left
# untouched, so this tool can never clobber settings it does not understand.
CREDENTIAL_KEYS = (
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "aws_security_token",  # legacy alias some tools still write
)


@dataclass
class Entry:
    key: str
    index: int
    value: str
    indent: str
    sep: str
    raw_key: str


@dataclass
class Section:
    name: str
    start: int  # index of the "[name]" header line
    end: int  # exclusive; first line that no longer belongs to this section
    entries: dict[str, Entry] = field(default_factory=dict)

    @property
    def last_entry_index(self) -> int:
        """Index of the final key line, or the header when the section is empty."""
        return max((e.index for e in self.entries.values()), default=self.start)


class IniFile:
    """A parsed view over an AWS-style ini file that rewrites lines in place."""

    def __init__(self, lines: list[str], path: str | None = None, trailing_newline: bool = True):
        self.lines = lines
        self.path = path
        self.trailing_newline = trailing_newline
        self._reparse()

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str) -> "IniFile":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            return cls([], path=path)
        lines = text.split("\n")
        # A trailing newline produces a final empty element; drop it and
        # remember so render() can put it back exactly as it was.
        trailing_newline = bool(lines) and lines[-1] == ""
        if trailing_newline:
            lines.pop()
        return cls(lines, path=path, trailing_newline=trailing_newline)

    def _reparse(self) -> None:
        self.sections: list[Section] = []
        current: Section | None = None
        prev_entry_was_empty = False
        for i, line in enumerate(self.lines):
            m = SECTION_RE.match(line)
            if m:
                if current is not None:
                    current.end = i
                current = Section(name=m.group(1).strip(), start=i, end=len(self.lines))
                self.sections.append(current)
                prev_entry_was_empty = False
                continue
            if current is None or COMMENT_RE.match(line) or not line.strip():
                continue
            em = ENTRY_RE.match(line)
            if not em:
                continue
            # AWS nested config (`s3 =` followed by indented sub-keys): an
            # indented line after an empty-valued key belongs to that key.
            if em.group("indent") and prev_entry_was_empty:
                continue
            raw_key = em.group("key").strip()
            current.entries[raw_key.lower()] = Entry(
                key=raw_key.lower(),
                index=i,
                value=em.group("value"),
                indent=em.group("indent"),
                sep=em.group("sep"),
                raw_key=raw_key,
            )
            prev_entry_was_empty = not em.group("value").strip()

    # ---------------------------------------------------------------- reading

    def section(self, name: str) -> Section | None:
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def section_names(self) -> list[str]:
        return [s.name for s in self.sections]

    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        s = self.section(section)
        if s is None:
            return default
        entry = s.entries.get(key.lower())
        return entry.value if entry else default

    def values(self, section: str) -> dict[str, str]:
        s = self.section(section)
        return {k: e.value for k, e in s.entries.items()} if s else {}

    # ---------------------------------------------------------------- writing

    def _preferred_sep(self, section: Section) -> str:
        """Match the section's existing ``=`` spacing when adding a new key."""
        seps = [e.sep for e in section.entries.values()]
        return seps[0] if seps else " = "

    def set_values(self, name: str, values: dict[str, str], remove: tuple[str, ...] = ()) -> None:
        """Create or update ``[name]``, setting ``values`` and dropping ``remove``.

        Existing lines keep their indentation, key capitalisation and spacing.
        Keys absent from both arguments are never touched.
        """
        section = self.section(name)
        if section is None:
            self._append_section(name, values)
            return

        sep = self._preferred_sep(section)
        # Collect edits first, then apply by descending line index so that the
        # deletions do not shift the indices of edits still pending.
        replacements: dict[int, str] = {}
        deletions: set[int] = set()
        additions: list[str] = []

        for key, value in values.items():
            entry = section.entries.get(key.lower())
            if entry is not None:
                replacements[entry.index] = f"{entry.indent}{entry.raw_key}{entry.sep}{value}"
            else:
                additions.append(f"{key}{sep}{value}")

        for key in remove:
            entry = section.entries.get(key.lower())
            if entry is not None and entry.index not in replacements:
                deletions.add(entry.index)

        for index, text in replacements.items():
            self.lines[index] = text
        if additions:
            self.lines[section.last_entry_index + 1 : section.last_entry_index + 1] = additions
        for index in sorted(deletions, reverse=True):
            del self.lines[index]
        self._reparse()

    def _append_section(self, name: str, values: dict[str, str]) -> None:
        block: list[str] = []
        if self.lines and self.lines[-1].strip():
            block.append("")
        block.append(f"[{name}]")
        block.extend(f"{k} = {v}" for k, v in values.items())
        self.lines.extend(block)
        self._reparse()

    def remove_section(self, name: str) -> bool:
        section = self.section(name)
        if section is None:
            return False
        # A comment block sitting directly above the next header introduces
        # *that* section, so it must survive this deletion. Blank separators
        # and comments written inside this profile go with it.
        boundary = section.end
        while boundary > section.start + 1 and COMMENT_RE.match(self.lines[boundary - 1]):
            boundary -= 1
        del self.lines[section.start : boundary]
        self._reparse()
        return True

    def rename_section(self, old: str, new: str) -> bool:
        section = self.section(old)
        if section is None:
            return False
        self.lines[section.start] = f"[{new}]"
        self._reparse()
        return True

    def render(self) -> str:
        text = "\n".join(self.lines)
        if self.trailing_newline and text and not text.endswith("\n"):
            text += "\n"
        return text


# ------------------------------------------------------------------- saving


def atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    """Replace ``path`` with ``text`` so readers never observe a partial file.

    Writes a sibling temp file, fsyncs it, then renames over the target. A
    crash mid-write leaves the original file intact.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".awsprofiles-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, mode)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


MAX_BACKUP_AGE_DAYS = 30


def backup(path: str, backup_dir: str, keep: int = 30, max_age_days: int = MAX_BACKUP_AGE_DAYS) -> str | None:
    """Snapshot ``path`` into ``backup_dir`` before it is modified.

    Old snapshots hold old secrets, so they are pruned by age as well as by
    count: a rotated long-lived key should not linger here for months.
    """
    if not os.path.exists(path):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(path)
    target = os.path.join(backup_dir, f"{base}.{stamp}")
    suffix = 1
    while os.path.exists(target):
        target = os.path.join(backup_dir, f"{base}.{stamp}-{suffix}")
        suffix += 1
    with open(path, "r", encoding="utf-8") as src:
        atomic_write(target, src.read())
    prune_backups(backup_dir, base, keep, max_age_days)
    return target


def prune_backups(backup_dir: str, base: str, keep: int, max_age_days: int = MAX_BACKUP_AGE_DAYS) -> None:
    try:
        snapshots = sorted(f for f in os.listdir(backup_dir) if f.startswith(base + "."))
    except FileNotFoundError:
        return
    stale = set(snapshots[:-keep] if keep > 0 else snapshots)
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        for name in snapshots:
            try:
                if os.path.getmtime(os.path.join(backup_dir, name)) < cutoff:
                    stale.add(name)
            except OSError:
                pass
    for name in stale:
        try:
            os.unlink(os.path.join(backup_dir, name))
        except OSError:
            pass
