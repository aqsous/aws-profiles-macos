"""A real window for the app, because a menu bar icon can be unreachable.

On a crowded menu bar macOS silently collapses the newest status item into an
overflow chevron: the item reports itself visible, has a correct title and a
sane frame, and still draws nothing. An app whose only surface is that icon
becomes unusable. This window is the primary interface; the menu bar item is a
shortcut for when there is room for it.

The table is grouped by client, coloured by how much life the credentials have
left, filterable from the toolbar search field, and every row action is also
in its right-click menu.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelBorder,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSEventModifierFlagCommand,
    NSEventModifierFlagShift,
    NSFont,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSScrollView,
    NSSearchToolbarItem,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSTextFieldCell,
    NSToolbar,
    NSToolbarFlexibleSpaceItemIdentifier,
    NSToolbarItem,
    NSViewHeightSizable,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
    NSViewWidthSizable,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSIndexSet, NSMakeRect, NSObject

from . import sts
from .parsers import mask

WINDOW_WIDTH = 960.0
WINDOW_HEIGHT = 540.0
FOOTER_HEIGHT = 34.0

# identifier, heading, width
COLUMNS = (
    ("glyph", "", 26.0),
    ("name", "Profile", 330.0),
    ("kind", "Type", 96.0),
    ("state", "Status", 230.0),
    ("key", "Access key", 140.0),
    ("region", "Region", 100.0),
)

STATUS_TEXT = {
    sts.Status.VALID: "Works",
    sts.Status.EXPIRED: "Expired",
    sts.Status.INVALID: "Rejected by AWS",
    sts.Status.OFFLINE: "Offline",
    sts.Status.UNKNOWN: "Not checked",
}

STATUS_GLYPH = {
    sts.Status.VALID: "🟢",
    sts.Status.EXPIRED: "🔴",
    sts.Status.INVALID: "🟠",
    sts.Status.OFFLINE: "⚪️",
    sts.Status.UNKNOWN: "⚪️",
}

# Toolbar items: identifier, label, SF Symbol, action, tooltip, needs a selection.
TOOLBAR = (
    ("import", "Paste", "square.and.arrow.down", "importClicked:",
     "Import credentials from the clipboard (⌘I)", False),
    ("login", "Login page", "globe", "loginClicked:",
     "Open this profile's client portal with the sign-in name copied (⌘L)", True),
    ("default", "Use as default", "star", "defaultClicked:",
     "Copy this profile's credentials into [default] (⌘D)", True),
    ("check", "Check", "checkmark.shield", "checkClicked:",
     "Ask AWS whether these credentials still work (⇧⌘R)", True),
    ("edit", "Edit", "pencil", "editClicked:", "Hand-edit this profile's credentials (⌘E)", True),
    ("delete", "Delete", "trash", "deleteClicked:", "Delete this profile, after a backup (⌘⌫)", True),
    (NSToolbarFlexibleSpaceItemIdentifier, None, None, None, None, False),
    ("clients", "Clients", "person.2", "clientsClicked:", "Manage clients and their login pages (⌘K)", False),
    ("backups", "Backups", "clock.arrow.circlepath", "restoreClicked:", "Restore an earlier credentials file", False),
    ("checkall", "Check all", "arrow.clockwise", "checkAllClicked:", "Re-check every profile (⌘R)", False),
    ("search", None, None, None, None, False),
)
SELECTION_ITEMS = {ident for ident, *_rest, needs in TOOLBAR if needs}
SELECTION_ACTIONS = {"loginClicked:", "defaultClicked:", "checkClicked:", "editClicked:",
                     "deleteClicked:", "renameClicked:"}

URGENCY_COLOR = {
    "expired": NSColor.systemRedColor,
    "soon": NSColor.systemOrangeColor,
}


class ProfilesTable(NSTableView):
    """Return edits, Delete deletes — the keys a Mac user tries first."""

    def keyDown_(self, event):
        code = event.keyCode()
        owner = self.delegate()
        if code in (36, 76) and owner is not None:  # Return, Enter
            owner.editClicked_(self)
        elif code in (51, 117) and owner is not None:  # Delete, Forward delete
            owner.deleteClicked_(self)
        else:
            objc.super(ProfilesTable, self).keyDown_(event)


class ProfilesWindow(NSObject):
    """Owns the window, its toolbar, table and footer.

    Every action defers to the controller (the menu bar app object), so the
    window and the menu drive exactly the same confirmed, backed-up operations.
    """

    def initWithController_(self, controller):
        self = objc.super(ProfilesWindow, self).init()
        if self is None:
            return None
        self.controller = controller
        self.rows = []
        self.query = ""
        self.search_field = None
        self.group_cell = NSTextFieldCell.alloc().initTextCell_("")
        self.group_cell.setFont_(NSFont.boldSystemFontOfSize_(12))
        self.group_cell.setTextColor_(NSColor.secondaryLabelColor())
        self._build()
        return self

    # ----------------------------------------------------------------- layout

    @objc.python_method
    def _build(self):
        style = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT), style, NSBackingStoreBuffered, False
        )
        window.setTitle_("AWS Profiles")
        window.setMinSize_(NSMakeRect(0, 0, 720, 360).size)
        # Closing the window must not destroy it; the Dock icon reopens it.
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self)
        window.center()
        self.window = window
        content = window.contentView()

        self._build_toolbar(window)
        self._build_table(content)
        self._build_empty_state(content)
        self._build_footer(content)

    @objc.python_method
    def _build_toolbar(self, window):
        toolbar = NSToolbar.alloc().initWithIdentifier_("AWSProfilesToolbar")
        toolbar.setDelegate_(self)
        toolbar.setAllowsUserCustomization_(True)
        toolbar.setAutosavesConfiguration_(True)
        toolbar.setDisplayMode_(1)  # NSToolbarDisplayModeIconAndLabel
        window.setToolbar_(toolbar)
        try:
            window.setToolbarStyle_(3)  # NSWindowToolbarStyleUnified
        except AttributeError:
            pass
        self.toolbar = toolbar

    def toolbarAllowedItemIdentifiers_(self, toolbar):
        return [ident for ident, *_ in TOOLBAR]

    def toolbarDefaultItemIdentifiers_(self, toolbar):
        return [ident for ident, *_ in TOOLBAR]

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, toolbar, ident, inserted):
        spec = next((t for t in TOOLBAR if t[0] == ident), None)
        if spec is None:
            return None
        if ident == "search":
            item = NSSearchToolbarItem.alloc().initWithItemIdentifier_(ident)
            item.setLabel_("Filter")
            item.setPreferredWidthForSearchField_(200)
            field = item.searchField()
            field.setPlaceholderString_("Filter profiles")
            field.setTarget_(self)
            field.setAction_("searchChanged:")
            field.setSendsSearchStringImmediately_(True)
            field.setSendsWholeSearchString_(False)
            self.search_field = field
            return item
        _ident, label, symbol, action, tooltip, _needs = spec
        item = NSToolbarItem.alloc().initWithItemIdentifier_(ident)
        item.setLabel_(label)
        item.setPaletteLabel_(label)
        item.setToolTip_(tooltip)
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, label)
        if image is not None:
            item.setImage_(image)
        item.setTarget_(self)
        item.setAction_(action)
        item.setBordered_(True)
        return item

    def validateToolbarItem_(self, item):
        if item.itemIdentifier() in SELECTION_ITEMS:
            return self.selected_name() is not None
        return True

    def validateMenuItem_(self, item):
        if str(item.action()) in SELECTION_ACTIONS:
            return self.selected_name() is not None
        return True

    @objc.python_method
    def _build_table(self, content):
        bounds = content.bounds()
        frame = NSMakeRect(0, FOOTER_HEIGHT, bounds.size.width, bounds.size.height - FOOTER_HEIGHT)
        scroll = NSScrollView.alloc().initWithFrame_(frame)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)  # NSNoBorder — the toolbar already frames it
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

        table = ProfilesTable.alloc().initWithFrame_(scroll.bounds())
        for identifier, heading, width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.setWidth_(width)
            column.headerCell().setStringValue_(heading)
            column.setEditable_(False)
            table.addTableColumn_(column)
        table.setDataSource_(self)
        table.setDelegate_(self)
        table.setUsesAlternatingRowBackgroundColors_(True)
        table.setAllowsMultipleSelection_(False)
        table.setRowHeight_(22.0)
        table.setFloatsGroupRows_(True)
        table.setTarget_(self)
        table.setDoubleAction_("rowDoubleClicked:")

        menu = NSMenu.alloc().initWithTitle_("Row")
        menu.setDelegate_(self)
        table.setMenu_(menu)

        scroll.setDocumentView_(table)
        content.addSubview_(scroll)
        self.table = table

    @objc.python_method
    def _build_empty_state(self, content):
        width, height = 380.0, 120.0
        x = (WINDOW_WIDTH - width) / 2
        y = (WINDOW_HEIGHT - height) / 2
        title = NSTextField.labelWithString_("No profiles yet")
        title.setFrame_(NSMakeRect(x, y + 84, width, 24))
        title.setAlignment_(1)  # centre
        title.setFont_(NSFont.boldSystemFontOfSize_(16))
        body = NSTextField.labelWithString_(
            "Copy a credential block from an AWS access portal, then paste it here.\n"
            "Add a client first to keep the portal's login page one click away."
        )
        body.setFrame_(NSMakeRect(x - 60, y + 36, width + 120, 40))
        body.setAlignment_(1)
        body.setFont_(NSFont.systemFontOfSize_(12))
        body.setTextColor_(NSColor.secondaryLabelColor())
        paste = NSButton.alloc().initWithFrame_(NSMakeRect(x + 40, y, 150, 30))
        paste.setTitle_("Paste credentials")
        paste.setBezelStyle_(NSBezelStyleRounded)
        paste.setTarget_(self)
        paste.setAction_("importClicked:")
        add = NSButton.alloc().initWithFrame_(NSMakeRect(x + 200, y, 140, 30))
        add.setTitle_("Add client…")
        add.setBezelStyle_(NSBezelStyleRounded)
        add.setTarget_(self)
        add.setAction_("clientsClicked:")
        self.empty_views = [title, body, paste, add]
        for view in self.empty_views:
            view.setAutoresizingMask_(NSViewMinXMargin | 1 << 2 | NSViewMinYMargin | NSViewMaxYMargin)
            view.setHidden_(True)
            content.addSubview_(view)

    @objc.python_method
    def _build_footer(self, content):
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(16, 9, WINDOW_WIDTH - 32 - 110, 17))
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(True)
        label.setFont_(NSFont.systemFontOfSize_(11))
        label.setTextColor_(NSColor.secondaryLabelColor())
        label.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(label)
        self.footer = label

        stop = NSButton.alloc().initWithFrame_(NSMakeRect(WINDOW_WIDTH - 16 - 100, 4, 100, 26))
        stop.setTitle_("Stop waiting")
        stop.setBezelStyle_(NSBezelStyleRounded)
        stop.setFont_(NSFont.systemFontOfSize_(11))
        stop.setTarget_(self)
        stop.setAction_("stopWaitingClicked:")
        stop.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        stop.setHidden_(True)
        content.addSubview_(stop)
        self.stop_button = stop

    # ------------------------------------------------------------------- data

    def reload_(self, payload=None):
        """Rebuild the table from the controller's current view of ~/.aws."""
        profiles = getattr(self.controller, "_profiles", []) or []
        checking = getattr(self.controller, "checking", set())
        clients = {c.name: c for c in self.controller.store.list_clients()}

        selected = self.selected_name()
        rows = []

        def profile_row(profile):
            identity = profile.identity
            status = profile.status
            state = STATUS_TEXT.get(status, "Not checked")
            if identity and identity.status is sts.Status.VALID:
                bits = [b for b in (identity.account, identity.role_name) if b]
                if bits:
                    state = " · ".join(bits)
            elif profile.name in checking:
                state = "Checking…"
            name = profile.name
            if profile.mirrors:
                name += f"  (mirrors {profile.mirrors})"
            remaining = profile.time_left()
            if remaining:
                name += f"   ·   {remaining}"
            return {
                "_kind": "profile",
                "_name": profile.name,
                "_client": profile.client,
                "_urgency": profile.urgency,
                "_search": " ".join(filter(None, (
                    profile.name, profile.client, identity.account if identity else None,
                    identity.role_name if identity else None, profile.access_key_id,
                ))).lower(),
                "glyph": "🔄" if profile.name in checking else STATUS_GLYPH.get(status, "⚪️"),
                "name": name,
                "kind": "Temporary" if profile.is_temporary else "Long-lived",
                "state": state,
                "key": mask(profile.access_key_id),
                "region": profile.region or "—",
            }

        def matches(row):
            return not self.query or self.query in row["_search"]

        if clients:
            for client in clients.values():
                members = [profile_row(p) for p in profiles if p.client == client.name]
                members = [m for m in members if matches(m)]
                if not members and self.query:
                    continue
                header = f"{client.name}   ·   {len(members)} profile{'s' if len(members) != 1 else ''}"
                if client.email:
                    header += f"   ·   {client.email}"
                rows.append({"_kind": "group", "_client": client.name, "name": header})
                rows.extend(members)
            loose = [m for m in (profile_row(p) for p in profiles if p.client is None) if matches(m)]
            if loose:
                rows.append({"_kind": "group", "_client": None, "name": "Other profiles"})
                rows.extend(loose)
        else:
            rows = [m for m in (profile_row(p) for p in profiles) if matches(m)]

        self.rows = rows
        self.table.reloadData()
        if selected:
            self.select_name(selected)
        self._update_empty_state(bool(profiles))
        self._update_footer(len(profiles))
        self.toolbar.validateVisibleItems()

    @objc.python_method
    def _update_empty_state(self, has_profiles: bool):
        for view in self.empty_views:
            view.setHidden_(has_profiles)
        self.table.enclosingScrollView().setHidden_(not has_profiles)

    @objc.python_method
    def _update_footer(self, total: int):
        waiting = getattr(self.controller, "waiting_for", None)
        self.stop_button.setHidden_(waiting is None)
        if waiting is not None:
            self.footer.setStringValue_(
                f"Waiting for credentials from {waiting} — copy the block in the portal and it imports itself."
            )
            return
        warnings = []
        try:
            warnings = self.controller.store.check_permissions() + self.controller.store.config_warnings()
        except OSError:
            pass
        shown = [r for r in self.rows if r["_kind"] == "profile"]
        counts = {}
        for row in shown:
            counts[row["glyph"]] = counts.get(row["glyph"], 0) + 1
        summary = f"{len(shown)} of {total} profiles" if self.query else f"{total} profiles"
        if counts.get("🟢"):
            summary += f" · {counts['🟢']} working"
        if counts.get("🔴"):
            summary += f" · {counts['🔴']} expired"
        if counts.get("🟠"):
            summary += f" · {counts['🟠']} rejected"
        if warnings:
            summary += f"   ⚠️ {len(warnings)} issue(s) — see the menu bar item"
        self.footer.setStringValue_(summary)

    @objc.python_method
    def selected_name(self):
        row = self.table.selectedRow()
        if row is None or row < 0 or row >= len(self.rows):
            return None
        return self.rows[row].get("_name")

    @objc.python_method
    def select_name(self, name):
        for index, row in enumerate(self.rows):
            if row.get("_name") == name:
                self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
                return

    # -------------------------------------------------------------- table glue

    def numberOfRowsInTableView_(self, table):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if row < 0 or row >= len(self.rows):
            return ""
        data = self.rows[row]
        if data["_kind"] == "group":
            # With a nil column the table is drawing the full-width group cell.
            return data["name"] if column is None or str(column.identifier()) == "name" else ""
        return data.get(str(column.identifier()), "")

    def tableView_dataCellForTableColumn_row_(self, table, column, row):
        if column is None and self.tableView_isGroupRow_(table, row):
            return self.group_cell
        return column.dataCell() if column is not None else None

    def tableView_isGroupRow_(self, table, row):
        return 0 <= row < len(self.rows) and self.rows[row]["_kind"] == "group"

    def tableView_shouldSelectRow_(self, table, row):
        return not self.tableView_isGroupRow_(table, row)

    def tableView_heightOfRow_(self, table, row):
        return 26.0 if self.tableView_isGroupRow_(table, row) else 22.0

    def tableView_willDisplayCell_forTableColumn_row_(self, table, cell, column, row):
        if row < 0 or row >= len(self.rows):
            return
        data = self.rows[row]
        if data["_kind"] == "group":
            cell.setFont_(NSFont.boldSystemFontOfSize_(12))
            cell.setTextColor_(NSColor.secondaryLabelColor())
            return
        cell.setFont_(NSFont.systemFontOfSize_(13))
        color = URGENCY_COLOR.get(data["_urgency"])
        cell.setTextColor_(color() if color else NSColor.controlTextColor())

    def tableViewSelectionDidChange_(self, notification):
        self.toolbar.validateVisibleItems()

    # ----------------------------------------------------------- context menu

    def menuNeedsUpdate_(self, menu):
        menu.removeAllItems()
        row = self.table.clickedRow()
        if row < 0 or row >= len(self.rows):
            return
        data = self.rows[row]
        if data["_kind"] == "group":
            if data["_client"] is None:
                return
            client = self.controller.store.client(data["_client"])
            self._item(menu, "Open login page", "ctxOpenLogin:", data["_client"])
            if client and client.email:
                self._item(menu, f"Copy sign-in name ({client.email})", "ctxCopyEmail:", data["_client"])
            menu.addItem_(NSMenuItem.separatorItem())
            self._item(menu, "Edit client…", "ctxEditClient:", data["_client"])
            return

        self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        name = data["_name"]
        if name != "default":
            self._item(menu, "Use as default", "ctxDefault:", name)
        if data["_client"]:
            self._item(menu, f"Open {data['_client']} login page", "ctxLogin:", name)
        self._item(menu, "Update from clipboard…", "ctxUpdate:", name)
        self._item(menu, "Edit credentials…", "ctxEdit:", name)
        self._item(menu, "Check now", "ctxCheck:", name)
        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, "Copy export AWS_PROFILE", "ctxCopyExport:", name)
        self._item(menu, "Copy credentials as env vars…", "ctxCopyEnv:", name)
        menu.addItem_(NSMenuItem.separatorItem())

        picker = NSMenu.alloc().initWithTitle_("Client")
        none_item = self._item(picker, "None", "ctxAssign:", (name, None))
        none_item.setState_(1 if data["_client"] is None else 0)
        for client in self.controller.store.list_clients():
            entry = self._item(picker, client.name, "ctxAssign:", (name, client.name))
            entry.setState_(1 if data["_client"] == client.name else 0)
        picker.addItem_(NSMenuItem.separatorItem())
        self._item(picker, "New client…", "ctxNewClient:", name)
        holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Client" if data["_client"] is None else f"Client: {data['_client']}", None, ""
        )
        holder.setSubmenu_(picker)
        menu.addItem_(holder)
        menu.addItem_(NSMenuItem.separatorItem())
        self._item(menu, "Rename…", "ctxRename:", name)
        self._item(menu, "Delete…", "ctxDelete:", name)

    @objc.python_method
    def _item(self, menu, title, action, payload):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        item.setTarget_(self)
        item.setRepresentedObject_(payload)
        menu.addItem_(item)
        return item

    def ctxDefault_(self, sender):
        self.controller.set_default(sender.representedObject())

    def ctxLogin_(self, sender):
        self.controller.open_login_for_profile(sender.representedObject())

    def ctxUpdate_(self, sender):
        self.controller.import_credentials(target=sender.representedObject())

    def ctxEdit_(self, sender):
        self.controller.edit_profile(sender.representedObject())

    def ctxCheck_(self, sender):
        self.controller.check_one(sender.representedObject())

    def ctxCopyExport_(self, sender):
        self.controller.copy_profile_export(sender.representedObject())

    def ctxCopyEnv_(self, sender):
        self.controller.copy_env(sender.representedObject())

    def ctxAssign_(self, sender):
        name, client = sender.representedObject()
        self.controller.assign_client(name, client, quiet=True)

    def ctxNewClient_(self, sender):
        self.controller.add_client(assign_to=sender.representedObject())

    def ctxRename_(self, sender):
        self.controller.rename_profile(sender.representedObject())

    def ctxDelete_(self, sender):
        self.controller.delete_profile(sender.representedObject())

    def ctxOpenLogin_(self, sender):
        self.controller.open_login_page(sender.representedObject())

    def ctxCopyEmail_(self, sender):
        self.controller.copy_client_email(sender.representedObject())

    def ctxEditClient_(self, sender):
        self.controller.manage_clients(select=sender.representedObject())

    # ---------------------------------------------------------------- actions

    def windowShouldClose_(self, sender):
        """Hide rather than close.

        The window is the only reliable way into this app, so closing it must
        not put it out of reach. It is hidden and reopened from the Window menu
        (⌘0), the Dock icon or the menu bar item.
        """
        self.window.orderOut_(None)
        return False

    def openWindowFromMenu_(self, sender):
        self.show()

    def quitFromMenu_(self, sender):
        self.controller.quit_app()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_windows):
        self.show()
        return True

    @objc.python_method
    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

    @objc.python_method
    def _with_selection(self, handler):
        name = self.selected_name()
        if name:
            handler(name)

    def searchChanged_(self, sender):
        self.query = str(sender.stringValue()).strip().lower()
        self.reload_(None)

    def focusSearch_(self, sender):
        if self.search_field is not None:
            self.show()
            self.window.makeFirstResponder_(self.search_field)

    def importClicked_(self, sender):
        self.controller.import_credentials()

    def loginClicked_(self, sender):
        self._with_selection(self.controller.open_login_for_profile)

    def defaultClicked_(self, sender):
        self._with_selection(self.controller.set_default)

    def checkClicked_(self, sender):
        self._with_selection(self.controller.check_one)

    def editClicked_(self, sender):
        self._with_selection(self.controller.edit_profile)

    def renameClicked_(self, sender):
        self._with_selection(self.controller.rename_profile)

    def deleteClicked_(self, sender):
        self._with_selection(self.controller.delete_profile)

    def checkAllClicked_(self, sender):
        self.controller.refresh_all()

    def restoreClicked_(self, sender):
        self.controller.choose_backup()

    def clientsClicked_(self, sender):
        self.controller.manage_clients()

    def stopWaitingClicked_(self, sender):
        self.controller.stop_waiting()

    def rowDoubleClicked_(self, sender):
        row = self.table.clickedRow()
        if 0 <= row < len(self.rows) and self.rows[row]["_kind"] == "group":
            if self.rows[row]["_client"]:
                self.controller.open_login_page(self.rows[row]["_client"])
            return
        self._with_selection(self.controller.set_default)


# --------------------------------------------------------------- main menu


def build_main_menu(target):
    """Give the app a menu bar of its own.

    Without this there is no ⌘Q, and — more importantly — no Edit menu, so ⌘V
    would not work inside the app's own paste and rename dialogs. The Profile
    menu is where the window's keyboard shortcuts live.
    """
    main = NSMenu.alloc().init()

    app_item = NSMenuItem.alloc().init()
    main.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    hide = app_menu.addItemWithTitle_action_keyEquivalent_("Hide AWS Profiles", "hide:", "h")
    hide.setKeyEquivalentModifierMask_(NSEventModifierFlagCommand)
    app_menu.addItem_(NSMenuItem.separatorItem())
    quit_item = app_menu.addItemWithTitle_action_keyEquivalent_(
        "Quit AWS Profiles", "quitFromMenu:", "q"
    )
    quit_item.setTarget_(target)
    app_item.setSubmenu_(app_menu)

    edit_item = NSMenuItem.alloc().init()
    main.addItem_(edit_item)
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "Z"),
        (None, None, None),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
        (None, None, None),
        ("Find Profile…", "focusSearch:", "f"),
    ):
        if title is None:
            edit_menu.addItem_(NSMenuItem.separatorItem())
            continue
        item = edit_menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        if action == "focusSearch:":
            item.setTarget_(target)
    edit_item.setSubmenu_(edit_menu)

    profile_item = NSMenuItem.alloc().init()
    main.addItem_(profile_item)
    profile_menu = NSMenu.alloc().initWithTitle_("Profile")
    for title, action, key, mask_ in (
        ("Paste Credentials…", "importClicked:", "i", NSEventModifierFlagCommand),
        (None, None, None, 0),
        ("Open Login Page", "loginClicked:", "l", NSEventModifierFlagCommand),
        ("Use as Default", "defaultClicked:", "d", NSEventModifierFlagCommand),
        ("Check", "checkClicked:", "r", NSEventModifierFlagCommand | NSEventModifierFlagShift),
        ("Edit Credentials…", "editClicked:", "e", NSEventModifierFlagCommand),
        ("Rename…", "renameClicked:", "", 0),
        ("Delete…", "deleteClicked:", "\x08", NSEventModifierFlagCommand),
        (None, None, None, 0),
        ("Check All", "checkAllClicked:", "r", NSEventModifierFlagCommand),
        ("Clients…", "clientsClicked:", "k", NSEventModifierFlagCommand),
        ("Backups…", "restoreClicked:", "", 0),
    ):
        if title is None:
            profile_menu.addItem_(NSMenuItem.separatorItem())
            continue
        item = profile_menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        item.setKeyEquivalentModifierMask_(mask_)
        item.setTarget_(target)
    profile_item.setSubmenu_(profile_menu)

    window_item = NSMenuItem.alloc().init()
    main.addItem_(window_item)
    window_menu = NSMenu.alloc().initWithTitle_("Window")
    window_menu.addItemWithTitle_action_keyEquivalent_("Minimise", "performMiniaturize:", "m")
    window_menu.addItemWithTitle_action_keyEquivalent_("Close", "performClose:", "w")
    window_menu.addItem_(NSMenuItem.separatorItem())
    # The way back in after the window has been closed.
    reopen = window_menu.addItemWithTitle_action_keyEquivalent_(
        "AWS Profiles", "openWindowFromMenu:", "0"
    )
    reopen.setTarget_(target)
    window_item.setSubmenu_(window_menu)

    NSApplication.sharedApplication().setMainMenu_(main)
    NSApplication.sharedApplication().setWindowsMenu_(window_menu)
    return main
