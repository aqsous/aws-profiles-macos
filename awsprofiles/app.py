"""AWS Profiles — a macOS menu bar app for the ~/.aws/credentials file.

The workflow this exists to remove: open the AWS access portal, copy the
credential block, open ~/.aws/credentials in an editor, find the right profile,
select exactly the old three lines, paste, save, hope nothing else moved.

Here that is: copy the block, then one menu click.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import objc
import rumps
from Foundation import NSObject
from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBezelBorder,
    NSComboBox,
    NSFont,
    NSImage,
    NSMakeRect,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSView,
    NSColor,
)

from . import sts
from .parsers import ParseError, parse_all, plan_import
from .clients import ClientsSheet
from .window import ProfilesWindow, build_main_menu
from .store import Client, ProfileStore, StoreError

REFRESH_SECONDS = 600  # re-check credential validity every 10 minutes
WAIT_SECONDS = 300  # how long "Login page" watches the clipboard for credentials
CLIPBOARD_CLEAR_SECONDS = 60  # secrets copied on purpose are wiped after this
DRAIN_SECONDS = 0.4  # how often the main thread applies background results

STATUS_GLYPH = {
    sts.Status.VALID: "🟢",
    sts.Status.EXPIRED: "🔴",
    sts.Status.INVALID: "🟠",
    sts.Status.OFFLINE: "⚪️",
    sts.Status.UNKNOWN: "⚪️",
}

LAUNCH_AGENT = os.path.expanduser("~/Library/LaunchAgents/io.github.aqsous.awsprofiles.plist")

# Running against a throwaway copy of ~/.aws is badged in the menu bar, so a
# sandbox instance can never be mistaken for the one editing the real file.
SANDBOX_BADGE = "🧪 " if os.environ.get("AWSPROFILES_DIR") else ""


# --------------------------------------------------------------------- dialogs


def clipboard_text() -> str:
    pasteboard = NSPasteboard.generalPasteboard()
    return pasteboard.stringForType_(NSPasteboardTypeString) or ""


def copy_to_clipboard(text: str) -> None:
    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    pasteboard.setString_forType_(text, NSPasteboardTypeString)


def multiline_prompt(title: str, message: str, default_text: str = "", ok: str = "Save") -> str | None:
    """A text box that accepts a pasted multi-line credential block.

    ``rumps.Window`` wraps a single-line ``NSTextField``, which mangles a
    pasted block, so this builds the accessory view directly.
    """
    alert = new_alert(title, message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_("Cancel")

    frame = NSMakeRect(0, 0, 460, 170)
    scroll = NSScrollView.alloc().initWithFrame_(frame)
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(NSBezelBorder)

    field = NSTextView.alloc().initWithFrame_(frame)
    field.setFont_(NSFont.userFixedPitchFontOfSize_(11))
    field.setRichText_(False)
    # macOS would otherwise turn quotes into curly quotes and -- into an em
    # dash, quietly corrupting any secret pasted through this box.
    field.setAutomaticQuoteSubstitutionEnabled_(False)
    field.setAutomaticDashSubstitutionEnabled_(False)
    field.setAutomaticTextReplacementEnabled_(False)
    field.setAutomaticSpellingCorrectionEnabled_(False)
    field.setString_(default_text)
    scroll.setDocumentView_(field)

    alert.setAccessoryView_(scroll)
    alert.window().setInitialFirstResponder_(field)
    if alert.runModal() == NSAlertFirstButtonReturn:
        return str(field.string())
    return None


class ImportForm(NSObject):
    """The one import dialog: a credential box that parses as you type, and a
    target profile you can pick from the existing ones or name afresh."""

    def initWithProfiles_text_target_(self, profiles, text, target):
        self = objc.super(ImportForm, self).init()
        if self is None:
            return None
        self.found = []
        frame = NSMakeRect(0, 0, 500, 240)
        view = NSView.alloc().initWithFrame_(frame)

        caption = NSTextField.labelWithString_("Profile")
        caption.setFrame_(NSMakeRect(0, 212, 60, 22))
        view.addSubview_(caption)
        combo = NSComboBox.alloc().initWithFrame_(NSMakeRect(64, 210, 436, 26))
        combo.addItemsWithObjectValues_(list(profiles))
        combo.setPlaceholderString_("existing profile, or a new name")
        combo.setCompletes_(True)
        if target:
            combo.setStringValue_(target)
        combo.setEnabled_(target is None)
        view.addSubview_(combo)
        self.combo = combo
        self.locked = target is not None

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 30, 500, 170))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        field = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 500, 170))
        field.setFont_(NSFont.userFixedPitchFontOfSize_(11))
        field.setRichText_(False)
        field.setAutomaticQuoteSubstitutionEnabled_(False)
        field.setAutomaticDashSubstitutionEnabled_(False)
        field.setAutomaticTextReplacementEnabled_(False)
        field.setAutomaticSpellingCorrectionEnabled_(False)
        field.setString_(text)
        field.setDelegate_(self)
        scroll.setDocumentView_(field)
        view.addSubview_(scroll)
        self.field = field

        status = NSTextField.labelWithString_("")
        status.setFrame_(NSMakeRect(0, 4, 500, 20))
        status.setFont_(NSFont.systemFontOfSize_(11))
        view.addSubview_(status)
        self.status = status
        self.view = view
        self._parse()
        return self

    def textDidChange_(self, notification):
        self._parse()

    @objc.python_method
    def _parse(self):
        text = str(self.field.string())
        try:
            self.found = parse_all(text) if text.strip() else []
        except ParseError as exc:
            self.found = []
            self.status.setTextColor_(NSColor.systemRedColor())
            self.status.setStringValue_(_truncate(str(exc), 90))
            return
        if not self.found:
            self.status.setTextColor_(NSColor.secondaryLabelColor())
            self.status.setStringValue_("Paste a credential block from the AWS access portal, or any export/JSON form.")
            return
        self.status.setTextColor_(NSColor.systemGreenColor())
        if len(self.found) > 1:
            names = ", ".join(c.profile_name or "unnamed" for c in self.found)
            self.status.setStringValue_(_truncate(f"{len(self.found)} blocks: {names} — all will be imported", 90))
            return
        creds = self.found[0]
        kind = "temporary" if creds.is_temporary else "long-lived"
        line = f"Found {kind} credentials · {creds.masked_key}"
        if creds.expiration:
            line += f" · expires {creds.expiration.astimezone().strftime('%H:%M')}"
        self.status.setStringValue_(line)
        if not self.locked and creds.profile_name and not str(self.combo.stringValue()).strip():
            self.combo.setStringValue_(creds.profile_name)

    @objc.python_method
    def target(self) -> str:
        return str(self.combo.stringValue()).strip()


def import_prompt(profiles: list[str], text: str, target: str | None) -> tuple[list, str] | None:
    """Show the import dialog. Returns (found credentials, target name) or None."""
    alert = new_alert(
        f"Update [{target}]" if target else "Paste credentials",
        "The clipboard is already pasted below. Check what was found, pick the profile it belongs to, and import.",
    )
    alert.addButtonWithTitle_("Import")
    alert.addButtonWithTitle_("Cancel")
    form = ImportForm.alloc().initWithProfiles_text_target_(profiles, text, target)
    alert.setAccessoryView_(form.view)
    alert.window().setInitialFirstResponder_(form.field if target else form.combo)
    if alert.runModal() == NSAlertFirstButtonReturn:
        return form.found, form.target()
    return None


def form_prompt(title: str, message: str, fields: list[tuple[str, str]], ok: str = "Save") -> list[str] | None:
    """A small labelled form: one single-line text field per ``(label, default)``."""
    alert = new_alert(title, message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_("Cancel")

    row, label_width, field_width = 30, 90, 330
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, label_width + field_width, row * len(fields)))
    inputs = []
    for index, (label, default) in enumerate(fields):
        y = row * (len(fields) - index - 1) + 4
        caption = NSTextField.labelWithString_(label)
        caption.setFrame_(NSMakeRect(0, y, label_width - 8, 22))
        caption.setAlignment_(2)  # NSTextAlignmentRight
        view.addSubview_(caption)
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(label_width, y, field_width, 22))
        field.setStringValue_(default)
        view.addSubview_(field)
        inputs.append(field)
    for first, second in zip(inputs, inputs[1:]):
        first.setNextKeyView_(second)

    alert.setAccessoryView_(view)
    if inputs:
        alert.window().setInitialFirstResponder_(inputs[0])
    if alert.runModal() == NSAlertFirstButtonReturn:
        return [str(field.stringValue()).strip() for field in inputs]
    return None


def choose_option(title: str, message: str, options: list[str], current: str | None = None,
                  ok: str = "Choose") -> str | None:
    """A pop-up list. Returns the chosen option, or None when cancelled."""
    alert = new_alert(title, message)
    alert.addButtonWithTitle_(ok)
    alert.addButtonWithTitle_("Cancel")

    popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, 0, 300, 26), False)
    popup.addItemsWithTitles_(options)
    if current in options:
        popup.selectItemWithTitle_(current)
    alert.setAccessoryView_(popup)
    if alert.runModal() == NSAlertFirstButtonReturn:
        return str(popup.titleOfSelectedItem())
    return None


def notice(title: str, message: str, ok: str = "OK", suppress_label: str = "Don't show this again") -> bool:
    """An informational alert with a suppression checkbox. Returns True if ticked."""
    alert = new_alert(title, message)
    alert.addButtonWithTitle_(ok)
    alert.setShowsSuppressionButton_(True)
    alert.suppressionButton().setTitle_(suppress_label)
    alert.runModal()
    return bool(alert.suppressionButton().state())


def new_alert(title: str, message: str) -> NSAlert:
    """An NSAlert wearing the app's own icon rather than the Python rocket."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    icon = NSApplication.sharedApplication().applicationIconImage()
    if icon is not None:
        alert.setIcon_(icon)
    return alert


def ask(title: str, message: str, ok: str = "OK", cancel: str = "Cancel", other: str | None = None) -> int:
    """Show an alert. Returns 1 for ok, 0 for cancel, -1 for the third button."""
    alert = new_alert(title, message)
    alert.addButtonWithTitle_(ok)
    if cancel:
        alert.addButtonWithTitle_(cancel)
    if other:
        alert.addButtonWithTitle_(other)
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    result = alert.runModal()
    if result == NSAlertFirstButtonReturn:
        return 1
    if result == NSAlertFirstButtonReturn + 1 and cancel:
        return 0
    return -1 if other else 0


def inform(title: str, message: str) -> None:
    ask(title, message, ok="OK", cancel=None)


# ------------------------------------------------------------------------ app


class AWSProfilesApp(rumps.App):
    def __init__(self):
        super().__init__("AWS Profiles", title=f"{SANDBOX_BADGE}☁️", quit_button=None)
        self.store = ProfileStore()
        self.identities: dict[str, sts.Identity] = {}
        self.checking: set[str] = set()
        self._results: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="sts")
        self._last_refresh: datetime | None = None

        # Background threads never touch AppKit. They push results onto a queue
        # that this timer drains on the main thread, which is the only place a
        # menu may safely be rebuilt.
        self._drain_timer = rumps.Timer(self._drain, DRAIN_SECONDS)
        self._drain_timer.start()
        self._refresh_timer = rumps.Timer(lambda _: self.refresh_all(), REFRESH_SECONDS)
        self._refresh_timer.start()

        self._profiles = []
        self.waiting_for: str | None = None  # client whose credentials we watch the clipboard for
        self._wait_timer: rumps.Timer | None = None
        self._wait_started: datetime | None = None
        self._wait_change_count = 0
        self._clear_timer: rumps.Timer | None = None
        self.window = ProfilesWindow.alloc().initWithController_(self)
        self.clients_sheet = ClientsSheet.alloc().initWithController_(self)
        build_main_menu(self.window)

        self.rebuild_menu()
        self.refresh_all()

        # Show the window once the run loop is going. This is the app's real
        # interface: the menu bar item is unreachable on a crowded menu bar.
        self._show_timer = rumps.Timer(self._show_window_once, 0.3)
        self._show_timer.start()

    # -------------------------------------------------------------- menu build

    def rebuild_menu(self) -> None:
        try:
            profiles = self.store.list_profiles()
            for profile in profiles:
                profile.identity = self.identities.get(profile.name)
        except OSError as exc:
            self._profiles = []
            self.menu.clear()
            self.menu.update([rumps.MenuItem(f"Cannot read ~/.aws: {exc}"), rumps.separator,
                              rumps.MenuItem("Quit", callback=self.quit_app)])
            return

        items: list = [
            rumps.MenuItem("Open AWS Profiles window", callback=self.open_window, key="o"),
            rumps.MenuItem("Paste credentials…", callback=self.import_credentials, key="v"),
            rumps.separator,
        ]

        clients = self.store.list_clients()
        if not profiles:
            items.append(rumps.MenuItem("No profiles yet"))
        elif not clients:
            for profile in profiles:
                items.append(self._profile_item(profile))
        else:
            # Grouped by client, each group headed by its sign-in shortcuts.
            for client in clients:
                members = [p for p in profiles if p.client == client.name]
                items.append(self._client_heading(client, len(members)))
                for profile in members:
                    items.append(self._profile_item(profile, indent=1))
            loose = [p for p in profiles if p.client is None]
            if loose:
                items.append(rumps.MenuItem("Other profiles"))
                for profile in loose:
                    items.append(self._profile_item(profile, indent=1))

        items += [
            rumps.separator,
            self._clients_menu(clients, profiles),
            rumps.MenuItem(self._refresh_title(), callback=lambda _: self.refresh_all(), key="r"),
            rumps.separator,
            self._file_menu(),
            self._make_toggle("Start at login", self.toggle_login_item, login_item_enabled()),
            rumps.separator,
            rumps.MenuItem("Quit AWS Profiles", callback=self.quit_app, key="q"),
        ]

        self._profiles = profiles
        self.menu.clear()
        self.menu.update(items)
        self._update_title(profiles)
        if getattr(self, "window", None) is not None:
            self.window.reload_(None)

    def _refresh_title(self) -> str:
        if self.checking:
            return f"Checking {len(self.checking)} profile(s)…"
        if self._last_refresh is None:
            return "Check all profiles"
        return f"Check all profiles (last: {self._last_refresh.strftime('%H:%M')})"

    def _profile_item(self, profile, indent: int = 0) -> rumps.MenuItem:
        identity = self.identities.get(profile.name)
        status = identity.status if identity else profile.status
        glyph = "🔄" if profile.name in self.checking else STATUS_GLYPH.get(status, "⚪️")

        label = f"{glyph}  {profile.label}"
        if profile.mirrors:
            label += f"  (mirrors {profile.mirrors})"
        item = rumps.MenuItem(label)
        if profile.nickname:
            item.add(rumps.MenuItem(f"[{profile.name}]"))
        item._menuitem.setIndentationLevel_(indent)

        # Read-only facts about the profile, shown greyed out at the top.
        if identity and identity.status is sts.Status.VALID:
            if identity.account:
                item.add(rumps.MenuItem(f"Account {identity.account}"))
            if identity.role_name:
                item.add(rumps.MenuItem(identity.role_name))
        elif identity and identity.error:
            item.add(rumps.MenuItem(_truncate(identity.error, 60)))

        kind = "Temporary" if profile.is_temporary else "Long-lived"
        detail = f"{kind} · {profile.masked_key}"
        remaining = profile.time_left()
        if remaining:
            detail += f" · {remaining}"
        item.add(rumps.MenuItem(detail))
        if profile.region:
            item.add(rumps.MenuItem(f"Region {profile.region}"))
        if not profile.is_complete:
            item.add(rumps.MenuItem("⚠️ Incomplete — missing a key"))
        item.add(rumps.separator)

        name = profile.name
        if name != "default":
            item.add(rumps.MenuItem("Use as default", callback=self._wrap(self.set_default, name)))
        item.add(rumps.MenuItem("Update from clipboard…", callback=self._wrap(self.import_credentials, None, name)))
        item.add(rumps.MenuItem("Edit credentials…", callback=self._wrap(self.edit_profile, name)))
        item.add(rumps.MenuItem("Check now", callback=self._wrap(self.check_one, name)))
        item.add(rumps.separator)
        if profile.client:
            item.add(rumps.MenuItem(self._login_title(self.store.client(profile.client)),
                                    callback=self._wrap(self.open_login_page, profile.client)))
        item.add(self._client_picker(profile))
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Copy export AWS_PROFILE", callback=self._wrap(self.copy_profile_export, name)))
        item.add(rumps.MenuItem("Copy credentials as env vars…", callback=self._wrap(self.copy_env, name)))
        item.add(rumps.separator)
        item.add(rumps.MenuItem("Nickname…", callback=self._wrap(self.set_nickname, name)))
        item.add(rumps.MenuItem("Rename…", callback=self._wrap(self.rename_profile, name)))
        item.add(rumps.MenuItem("Delete…", callback=self._wrap(self.delete_profile, name)))
        return item

    def _client_heading(self, client: Client, count: int) -> rumps.MenuItem:
        """The group header: a submenu holding the client's sign-in shortcuts."""
        heading = rumps.MenuItem(f"{client.name}  ·  {count} profile{'s' if count != 1 else ''}")
        heading.add(rumps.MenuItem(self._login_title(client), callback=self._wrap(self.open_login_page, client.name)))
        if client.email:
            heading.add(rumps.MenuItem(f"Copy sign-in name  ({client.email})",
                                       callback=self._wrap(self.copy_client_email, client.name)))
        heading.add(rumps.MenuItem(_truncate(client.login_url, 60)))
        heading.add(rumps.separator)
        heading.add(rumps.MenuItem("Edit client…", callback=self._wrap(self.manage_clients, None, client.name)))
        heading.add(rumps.MenuItem("Remove client…", callback=self._wrap(self.remove_client, client.name)))
        return heading

    @staticmethod
    def _login_title(client: Client | None) -> str:
        if client and client.email:
            return f"Open login page  (copies {client.email})"
        return "Open login page"

    def _clients_menu(self, clients: list[Client], profiles) -> rumps.MenuItem:
        menu = rumps.MenuItem("Clients")
        for client in clients:
            menu.add(self._client_heading(client, sum(1 for p in profiles if p.client == client.name)))
        menu.add(rumps.separator)
        menu.add(rumps.MenuItem("Manage clients…", callback=lambda _: self.manage_clients()))
        return menu

    def _client_picker(self, profile) -> rumps.MenuItem:
        """Submenu that groups a profile under a client, ticking the current one."""
        picker = rumps.MenuItem("Client" if profile.client is None else f"Client: {profile.client}")
        none_item = rumps.MenuItem("None", callback=self._wrap(self.assign_client, profile.name, None))
        none_item.state = 1 if profile.client is None else 0
        picker.add(none_item)
        for client in self.store.list_clients():
            entry = rumps.MenuItem(client.name, callback=self._wrap(self.assign_client, profile.name, client.name))
            entry.state = 1 if profile.client == client.name else 0
            picker.add(entry)
        picker.add(rumps.separator)
        picker.add(rumps.MenuItem("New client…", callback=self._wrap(self.add_client, None, profile.name)))
        return picker

    def _file_menu(self) -> rumps.MenuItem:
        menu = rumps.MenuItem("Credentials file")
        menu.add(rumps.MenuItem("Open in editor", callback=lambda _: self.store.open_in_editor()))
        menu.add(rumps.MenuItem("Reveal in Finder", callback=lambda _: self.store.reveal()))
        menu.add(rumps.separator)

        backups = rumps.MenuItem("Restore a backup")
        snapshots = self.store.list_backups()[:15]
        if not snapshots:
            backups.add(rumps.MenuItem("No backups yet"))
        for path, when in snapshots:
            title = f"{when.strftime('%d %b %H:%M:%S')} — {os.path.basename(path).split('.')[0]}"
            backups.add(rumps.MenuItem(title, callback=self._wrap(self.restore_backup, path)))
        menu.add(backups)

        warnings = self.store.check_permissions() + self.store.config_warnings()
        if warnings:
            menu.add(rumps.separator)
            menu.add(rumps.MenuItem(f"⚠️ {len(warnings)} issue(s) found", callback=self.show_warnings))
        return menu

    def _make_toggle(self, title: str, callback, state: bool) -> rumps.MenuItem:
        item = rumps.MenuItem(title, callback=callback)
        item.state = 1 if state else 0
        return item

    def _update_title(self, profiles) -> None:
        current = next((p for p in profiles if p.name == "default"), None)
        waiting = "⏳ " if self.waiting_for else ""
        if current is None:
            self.title = f"{SANDBOX_BADGE}{waiting}☁️"
            return
        identity = self.identities.get("default")
        glyph = STATUS_GLYPH.get(identity.status if identity else current.status, "⚪️")
        label = current.mirrors or "default"
        source = next((p for p in profiles if p.name == label), None)
        if source is not None:
            label = source.label
            if source.client:
                label = f"{source.client} / {label}"
        self.title = f"{SANDBOX_BADGE}{waiting}{glyph} {_truncate(label, 22)}"

    @staticmethod
    def _wrap(handler, *args):
        """Adapt a handler to the ``callback(sender)`` shape rumps expects."""
        return lambda sender: handler(*args)

    # ------------------------------------------------------------- background

    def _drain(self, _timer) -> None:
        """Apply queued background results. Runs on the main thread."""
        changed = False
        while True:
            try:
                name, identity = self._results.get_nowait()
            except queue.Empty:
                break
            self.identities[name] = identity
            self.checking.discard(name)
            changed = True
        if changed:
            if not self.checking:
                self._last_refresh = datetime.now()
            self.rebuild_menu()

    def _check(self, name: str) -> None:
        try:
            identity = self.store.identity_for(name)
        except Exception as exc:
            identity = sts.Identity(sts.Status.UNKNOWN, error=str(exc))
        self._results.put((name, identity))

    def refresh_all(self, names: list[str] | None = None) -> None:
        try:
            names = names if names is not None else [p.name for p in self.store.list_profiles()]
        except OSError:
            return
        for name in names:
            if name not in self.checking:
                self.checking.add(name)
                self._pool.submit(self._check, name)
        self.rebuild_menu()

    def check_one(self, name: str) -> None:
        self.refresh_all([name])

    # ---------------------------------------------------------------- actions

    def import_credentials(self, _sender=None, target: str | None = None, text: str | None = None) -> None:
        """The one way credentials come in: from the clipboard, checked, into a named profile."""
        text = clipboard_text() if text is None else text
        try:
            preview = parse_all(text) if text.strip() else []
        except ParseError:
            preview = []
        if preview:
            action, payload = plan_import(preview, target)
            if action == "write":
                self._write(*payload)
                return
            if action == "many":
                self._import_many(payload)
                return

        names = [p.name for p in self._profiles]
        answer = import_prompt(names, text.strip()[:6000], target)
        if answer is None:
            return
        found, name = answer
        if not found:
            inform("Nothing to import", "No credentials were recognised in the text.")
            return
        action, payload = plan_import(found, target or name or None)
        if action == "write":
            self._write(*payload)
        elif action == "many":
            self._import_many(payload)
        else:
            inform("Nothing was changed", "Give the profile a name.")

    # Older entry points, kept so the window and menus have one target.
    def paste_from_clipboard(self, _sender=None, target: str | None = None) -> None:
        self.import_credentials(target=target)

    def new_profile(self, _sender=None) -> None:
        self.import_credentials()

    def update_profile(self, name: str) -> None:
        self.import_credentials(target=name)

    def _import_many(self, found) -> None:
        names = ", ".join(c.profile_name or "unnamed" for c in found)
        if ask("Import several profiles?", f"The clipboard holds {len(found)} credential blocks:\n\n{names}",
               ok=f"Import all {len(found)}") != 1:
            return
        written, failed = [], []
        for creds in found:
            name = creds.profile_name
            if not name:
                failed.append("one block had no profile name")
                continue
            try:
                self.store.upsert(name, creds)
                written.append(name)
            except (StoreError, OSError) as exc:
                failed.append(f"{name}: {exc}")
        report = ""
        if written:
            report += "Updated:\n" + "\n".join(f"  • {n}" for n in written)
        if failed:
            report += "\n\nSkipped:\n" + "\n".join(f"  • {f}" for f in failed)
        inform("Import finished", report.strip())
        self.refresh_all(written)

    def _write(self, name: str, creds) -> None:
        """Confirm, then write one credential set into ``name``."""
        existing = next((p for p in self.store.list_profiles() if p.name == name), None)
        kind = "temporary" if creds.is_temporary else "long-lived"
        summary = f"{kind} credentials · {creds.masked_key}"
        if creds.expiration:
            summary += f"\nExpires {creds.expiration.astimezone().strftime('%a %d %b, %H:%M')}"

        if existing:
            was = f"currently {existing.masked_key}"
            if existing.status is sts.Status.EXPIRED:
                was += " (expired)"
            question = f"Replace the credentials in [{name}]?\n\n{was}\nnew: {summary}"
            confirm = ask("Update profile", question, ok="Replace")
        else:
            confirm = ask("Create profile", f"Create a new profile [{name}]?\n\n{summary}", ok="Create")
        if confirm != 1:
            return

        try:
            result = self.store.upsert(name, creds)
        except (StoreError, OSError) as exc:
            inform("Nothing was changed", str(exc))
            return

        message = result.message
        if result.warnings:
            message += "\n\n" + "\n".join(result.warnings)
        if result.backup_path:
            message += f"\n\nPrevious file backed up as {os.path.basename(result.backup_path)}."

        choice = ask("Saved", message, ok="Done",
                     other=None if name == "default" else f"Also use as default")
        if choice == -1:
            self.set_default(name, confirm=False)
        if not existing and name != "default":
            if self.waiting_for and self.store.client(self.waiting_for):
                self.assign_client(name, self.waiting_for, quiet=True)
            else:
                self._offer_client(name)
        if self.waiting_for:
            self.stop_waiting()
        self.refresh_all([name, "default"] if name != "default" else [name])

    def _offer_client(self, name: str) -> None:
        """Group a brand-new profile under one of the known clients, if any exist."""
        clients = self.store.list_clients()
        if not clients:
            return
        last = self.store.last_client()
        options = ["None"] + [c.name for c in clients]
        choice = choose_option(
            f"Which client is [{name}] for?",
            "Grouping it under a client puts its sign-in page one click away.",
            options, current=last.name if last else "None", ok="Done",
        )
        if choice and choice != "None":
            self.assign_client(name, choice, quiet=True)

    def edit_profile(self, name: str) -> None:
        """Hand-edit one profile's credentials without opening the whole file."""
        profile = next((p for p in self.store.list_profiles() if p.name == name), None)
        if profile is None:
            return
        template = (
            f"[{name}]\n"
            f"aws_access_key_id={profile.access_key_id or ''}\n"
            "aws_secret_access_key=\n"
            + ("aws_session_token=\n" if profile.is_temporary else "")
        )
        text = multiline_prompt(
            f"Edit [{name}]",
            "Paste or type the full credential block. The secret is blank for safety — "
            "fill it in, or cancel to leave the profile untouched.",
            template,
        )
        if text is None:
            return
        try:
            creds = parse_all(text)[0]
        except ParseError as exc:
            inform("Nothing was changed", str(exc))
            return
        self._write(name, creds)

    def _prompt_name(self, title: str, message: str, default: str = "") -> str | None:
        window = rumps.Window(message=message, title=title, default_text=default,
                              ok="Continue", cancel="Cancel", dimensions=(320, 22))
        response = window.run()
        return response.text.strip() if response.clicked else None

    def set_default(self, name: str, confirm: bool = True) -> None:
        if confirm:
            question = (
                f"Copy the credentials from [{name}] into [default]?\n\n"
                "Every AWS tool that does not name a profile will then use them — "
                "including terminals that are already open."
            )
            if ask("Set default profile", question, ok="Set as default") != 1:
                return
        try:
            result = self.store.set_default(name)
        except (StoreError, OSError) as exc:
            inform("Nothing was changed", str(exc))
            return
        self.refresh_all(["default"])
        inform("Default updated", result.message)

    def set_nickname(self, name: str) -> None:
        """A friendly label for an unwieldy profile name; the ini section is untouched."""
        profile = next((p for p in self._profiles if p.name == name), None)
        current = profile.nickname if profile else ""
        nickname = self._prompt_name(
            "Nickname", f"A short label for [{name}]. Leave it blank to remove the nickname.", current or ""
        )
        if nickname is None:
            return
        try:
            self.store.set_nickname(name, nickname)
        except StoreError as exc:
            inform("Nothing was changed", str(exc))
            return
        self.rebuild_menu()

    def rename_profile(self, name: str) -> None:
        new = self._prompt_name("Rename profile", f"New name for [{name}]:", name)
        if not new or new == name:
            return
        try:
            result = self.store.rename(name, new)
        except (StoreError, OSError) as exc:
            inform("Nothing was changed", str(exc))
            return
        self.identities[new] = self.identities.pop(name, None) or sts.Identity(sts.Status.UNKNOWN)
        self.rebuild_menu()
        inform("Renamed", result.message)

    def delete_profile(self, name: str) -> None:
        profile = next((p for p in self.store.list_profiles() if p.name == name), None)
        detail = f"\n\nIt holds {profile.masked_key}." if profile else ""
        question = (
            f"Delete the profile [{name}] from ~/.aws/credentials?{detail}\n\n"
            "A timestamped backup is written first, so this can be undone from "
            "“Credentials file ▸ Restore a backup”."
        )
        if ask("Delete profile", question, ok="Delete") != 1:
            return
        try:
            result = self.store.delete(name)
        except (StoreError, OSError) as exc:
            inform("Nothing was changed", str(exc))
            return
        self.identities.pop(name, None)
        self.rebuild_menu()
        message = result.message
        if result.backup_path:
            message += f"\n\nBacked up as {os.path.basename(result.backup_path)}."
        inform("Deleted", message)

    def copy_profile_export(self, name: str) -> None:
        copy_to_clipboard(f"export AWS_PROFILE={name}")
        inform("Copied", f"export AWS_PROFILE={name}\n\nPaste it into a terminal to use this profile there.")

    def copy_env(self, name: str) -> None:
        question = (
            f"Copy the actual secrets from [{name}] to the clipboard?\n\n"
            "Anything that reads your clipboard will be able to see them. "
            "Prefer “Copy export AWS_PROFILE” unless you need to paste into a remote shell."
        )
        if ask("Copy secrets?", question, ok="Copy secrets") != 1:
            return
        from .credfile import IniFile

        values = IniFile.load(self.store.credentials_path).values(name)
        lines = [
            f"export AWS_ACCESS_KEY_ID={values.get('aws_access_key_id', '')}",
            f"export AWS_SECRET_ACCESS_KEY={values.get('aws_secret_access_key', '')}",
        ]
        token = values.get("aws_session_token") or values.get("aws_security_token")
        if token:
            lines.append(f"export AWS_SESSION_TOKEN={token}")
        copy_to_clipboard("\n".join(lines) + "\n")
        self._clear_clipboard_later()
        inform("Copied", f"The credentials for [{name}] are on the clipboard.\n\n"
                         f"They are wiped automatically after {CLIPBOARD_CLEAR_SECONDS} seconds.")

    def _clear_clipboard_later(self) -> None:
        """Wipe the secrets we just copied, unless the user has copied something else since."""
        pasteboard = NSPasteboard.generalPasteboard()
        marker = pasteboard.changeCount()
        calls = {"n": 0}

        def clear(timer):
            # rumps timers fire once on start and then every interval; act on the second call.
            calls["n"] += 1
            if calls["n"] < 2:
                return
            timer.stop()
            if pasteboard.changeCount() == marker:
                pasteboard.clearContents()
            self._clear_timer = None

        self._clear_timer = rumps.Timer(clear, CLIPBOARD_CLEAR_SECONDS)
        self._clear_timer.start()

    # ---------------------------------------------------------------- clients

    def open_login_page(self, client_name: str) -> None:
        """Open the client's access portal, with the sign-in email ready to paste."""
        client = self.store.client(client_name)
        if client is None:
            inform("Nothing to open", f"There is no client named {client_name} any more.")
            return
        if client.email:
            copy_to_clipboard(client.email)
            # The portal ignores every prefill parameter, so the email travels via
            # the clipboard. Say so once, or nobody would know to press ⌘V.
            if not self.store.preference("login_hint_seen"):
                if notice(
                    "Sign-in name copied — paste it into the sign-in form",
                    f"{client.email} is on the clipboard.\n\n"
                    "AWS does not let a link prefill the Username box, so when the login "
                    "page opens, click Username and press ⌘V.",
                    ok="Open login page",
                ):
                    self.store.set_preference("login_hint_seen", True)
        try:
            self.store.open_url(client.login_url)
        except StoreError as exc:
            inform("Could not open the login page", str(exc))
            return
        self.start_waiting(client.name)

    def copy_client_email(self, client_name: str) -> None:
        client = self.store.client(client_name)
        if client and client.email:
            copy_to_clipboard(client.email)
            inform("Copied", f"{client.email} is on the clipboard.")

    def _client_form(self, title: str, message: str, client: Client | None) -> Client | None:
        """Show the client form until it validates or is cancelled."""
        name, url, email = (client.name, client.login_url, client.email) if client else ("", "", "")
        while True:
            answer = form_prompt(title, message,
                                 [("Client", name), ("Login URL", url), ("Email/username", email)])
            if answer is None:
                return None
            name, url, email = answer
            try:
                if client and name != client.name:
                    self.store.rename_client(client.name, name)
                return self.store.save_client(name, url, email)
            except (StoreError, OSError) as exc:
                inform("Check the form", str(exc))

    def add_client(self, _sender=None, assign_to: str | None = None) -> None:
        client = self._client_form(
            "New client",
            "The access portal you sign in to for this client, and the email or username you use there.\n"
            "Example: https://d-1234567890.awsapps.com/start",
            None,
        )
        if client is None:
            return
        if assign_to:
            self.assign_client(assign_to, client.name, quiet=True)
        self.rebuild_menu()

    def edit_client(self, client_name: str) -> None:
        self.manage_clients(select=client_name)

    def remove_client(self, client_name: str, on_done=None) -> None:
        members = [p.name for p in self._profiles if p.client == client_name]
        detail = f"\n\nIts {len(members)} profile(s) stay in ~/.aws/credentials; they just lose the grouping." \
            if members else "\n\nNo credentials are touched."
        if ask("Remove client", f"Forget the client {client_name} and its login page?{detail}", ok="Remove") != 1:
            return
        try:
            result = self.store.delete_client(client_name)
        except StoreError as exc:
            inform("Nothing was changed", str(exc))
            return
        self.rebuild_menu()
        if on_done:
            on_done()
        inform("Removed", result.message)

    def assign_client(self, name: str, client_name: str | None, quiet: bool = False) -> None:
        try:
            self.store.assign_client(name, client_name)
        except StoreError as exc:
            inform("Nothing was changed", str(exc))
            return
        self.rebuild_menu()
        if not quiet and client_name:
            inform("Grouped", f"[{name}] is now listed under {client_name}.")

    def choose_client_for(self, name: str) -> None:
        """Pick a client for a profile from a list — the window's Client… button."""
        clients = self.store.list_clients()
        if not clients:
            if ask("No clients yet", "Add a client first: its login page and sign-in name.", ok="Add client…") == 1:
                self.add_client(assign_to=name)
            return
        current = next((p.client for p in self._profiles if p.name == name), None)
        options = ["None"] + [c.name for c in clients] + ["New client…"]
        choice = choose_option(f"Client for [{name}]", "Profiles are grouped under the client whose portal issues them.",
                               options, current=current or "None")
        if choice is None:
            return
        if choice == "New client…":
            self.add_client(assign_to=name)
        else:
            self.assign_client(name, None if choice == "None" else choice, quiet=True)

    def manage_clients(self, _sender=None, select: str | None = None) -> None:
        """Open the Clients sheet on the main window."""
        self.window.show()
        self.clients_sheet.show(self.window.window, select)

    # ------------------------------------------------ login page follow-through

    def start_waiting(self, client_name: str) -> None:
        """Watch the clipboard for a credential block after the portal opened."""
        self.stop_waiting(quiet=True)
        self.waiting_for = client_name
        self._wait_started = datetime.now()
        self._wait_change_count = NSPasteboard.generalPasteboard().changeCount()
        self._wait_timer = rumps.Timer(self._poll_clipboard, 1.0)
        self._wait_timer.start()
        self.rebuild_menu()

    def stop_waiting(self, _sender=None, quiet: bool = False) -> None:
        if self._wait_timer is not None:
            self._wait_timer.stop()
            self._wait_timer = None
        was = self.waiting_for
        self.waiting_for = None
        if was and not quiet:
            self.rebuild_menu()

    def _poll_clipboard(self, _timer) -> None:
        if self.waiting_for is None:
            self.stop_waiting(quiet=True)
            return
        if (datetime.now() - self._wait_started).total_seconds() > WAIT_SECONDS:
            self.stop_waiting()
            return
        count = NSPasteboard.generalPasteboard().changeCount()
        if count == self._wait_change_count:
            return
        self._wait_change_count = count
        text = clipboard_text()
        try:
            found = parse_all(text) if text.strip() else []
        except ParseError:
            return
        if not found:
            return
        # Credentials arrived: bring the app forward and run the normal import.
        client = self.waiting_for
        self._wait_timer.stop()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.import_credentials(text=text)
        if self.waiting_for == client and self._wait_timer is not None:
            self._wait_timer.start()  # import was cancelled; keep watching

    def open_login_for_profile(self, name: str) -> None:
        """The window's Login page button: open the portal for the selected profile."""
        client = self.store.client_for(name)
        if client is None:
            self.choose_client_for(name)
            return
        self.open_login_page(client.name)

    def restore_backup(self, path: str) -> None:
        question = (
            f"Restore {os.path.basename(path)}?\n\n"
            "Your current file is backed up first, so this is reversible."
        )
        if ask("Restore backup", question, ok="Restore") != 1:
            return
        try:
            result = self.store.restore(path)
        except (StoreError, OSError) as exc:
            inform("Nothing was changed", str(exc))
            return
        self.identities.clear()
        self.refresh_all()
        inform("Restored", result.message)

    def show_warnings(self, _sender=None) -> None:
        permissions = self.store.check_permissions()
        warnings = permissions + self.store.config_warnings()
        if not warnings:
            inform("All good", "No problems found in ~/.aws.")
            return
        body = "\n\n".join(f"• {w}" for w in warnings)
        if permissions:
            if ask("Issues in ~/.aws", body, ok="Fix permissions") == 1:
                fixed = self.store.fix_permissions()
                inform("Permissions fixed", "Set to 0600: " + ", ".join(fixed) if fixed else "Nothing to fix.")
                self.rebuild_menu()
            return
        inform("Issues in ~/.aws", body)

    # ------------------------------------------------------------ login item

    def toggle_login_item(self, sender) -> None:
        try:
            if login_item_enabled():
                disable_login_item()
                sender.state = 0
            else:
                enable_login_item()
                sender.state = 1
        except OSError as exc:
            inform("Could not change login item", str(exc))

    def _show_window_once(self, timer) -> None:
        timer.stop()
        self.window.show()

    def open_window(self, _sender=None) -> None:
        self.window.show()

    def choose_backup(self, _sender=None) -> None:
        """Pick a backup to restore, for the window's Backups… button."""
        snapshots = self.store.list_backups()[:15]
        if not snapshots:
            inform("No backups yet", "A backup is written automatically before every change.")
            return
        listing = "\n".join(
            f"{i + 1}.  {when.strftime('%d %b %H:%M:%S')} — {os.path.basename(path).split('.')[0]}"
            for i, (path, when) in enumerate(snapshots)
        )
        choice = self._prompt_name(
            "Restore a backup",
            f"Type the number of the backup to restore:\n\n{listing}",
            "1",
        )
        if not choice:
            return
        try:
            index = int(choice.strip()) - 1
            path = snapshots[index][0]
        except (ValueError, IndexError):
            inform("Nothing was changed", f"'{choice}' is not one of the listed numbers.")
            return
        self.restore_backup(path)

    def quitFromMenu_(self, sender) -> None:
        self.quit_app()

    def quit_app(self, _sender=None) -> None:
        self._pool.shutdown(wait=False)
        rumps.quit_application()


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------ login item

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.github.aqsous.awsprofiles</string>
  <key>ProgramArguments</key>
  <array>{arguments}</array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


def login_item_enabled() -> bool:
    return os.path.exists(LAUNCH_AGENT)


def enable_login_item() -> None:
    launcher = os.environ.get("AWSPROFILES_LAUNCHER") or sys.executable
    arguments = [launcher] if launcher.endswith("AWSProfiles") else [launcher, "-m", "awsprofiles"]
    body = PLIST.format(arguments="".join(f"<string>{a}</string>" for a in arguments))
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    with open(LAUNCH_AGENT, "w", encoding="utf-8") as fh:
        fh.write(body)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", LAUNCH_AGENT],
                   check=False, capture_output=True)


def disable_login_item() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/io.github.aqsous.awsprofiles"],
                   check=False, capture_output=True)
    if os.path.exists(LAUNCH_AGENT):
        os.unlink(LAUNCH_AGENT)


def _claim_app_name(name: str = "AWS Profiles") -> None:
    """Make macOS call this app by its own name.

    The launcher execs the framework Python, so the main bundle macOS sees is
    Python.app — and the application menu is titled from that bundle's
    CFBundleName, giving a menu reading "Python". Overriding the loaded info
    dictionary before the menu is built fixes the name everywhere it shows.
    """
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
    except Exception:
        pass  # a cosmetic nicety; never worth failing the launch over


def _set_app_icon() -> None:
    """Show our own icon in dialogs and the Dock instead of Python's rocket."""
    launcher = os.environ.get("AWSPROFILES_LAUNCHER")
    candidates = []
    if launcher:
        candidates.append(os.path.join(os.path.dirname(launcher), "..", "Resources", "AppIcon.icns"))
    image = None
    for path in candidates:
        if os.path.exists(path):
            image = NSImage.alloc().initWithContentsOfFile_(path)
            break
    if image is None:
        try:
            import tempfile

            from .icon import _render

            path = os.path.join(tempfile.gettempdir(), "awsprofiles-icon.png")
            _render(256, path)
            image = NSImage.alloc().initWithContentsOfFile_(path)
        except Exception:
            return
    if image is not None:
        NSApplication.sharedApplication().setApplicationIconImage_(image)


def main() -> None:
    _claim_app_name()
    _set_app_icon()
    app = AWSProfilesApp()
    # Menu bar only: no Dock icon, no application menu in the top-left.
    # Our bundle's Info.plist sets LSUIElement, but the launcher execs the
    # framework Python, so macOS reads *its* bundle instead of ours and the
    # app would otherwise appear as a normal foreground app called "Python".
    # Regular, not Accessory: the app needs a Dock icon and a ⌘Tab entry,
    # because macOS hides the menu bar item when the bar is full.
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyRegular
    )
    app.run()


if __name__ == "__main__":
    main()
