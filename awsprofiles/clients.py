"""The Clients sheet: every client's login page and sign-in email in one view.

A list on the left, the selected client's fields on the right. Editing a name,
URL or email and pressing Save applies it at once; nothing is staged, so the
sheet can be dismissed at any moment without losing or half-applying a change.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSBezelBorder,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSPanel,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSWindowStyleMaskTitled,
    NSBackingStoreBuffered,
)
from Foundation import NSIndexSet, NSMakeRect, NSObject

from .store import StoreError

WIDTH, HEIGHT = 640.0, 360.0
LIST_WIDTH = 190.0


class ClientsSheet(NSObject):
    def initWithController_(self, controller):
        self = objc.super(ClientsSheet, self).init()
        if self is None:
            return None
        self.controller = controller
        self.store = controller.store
        self.clients = []
        self.editing_new = False
        self._build()
        return self

    # ----------------------------------------------------------------- layout

    @objc.python_method
    def _build(self):
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), NSWindowStyleMaskTitled, NSBackingStoreBuffered, False
        )
        panel.setTitle_("Clients")
        self.panel = panel
        content = panel.contentView()

        # Left: the list of clients with + and − beneath it.
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 60, LIST_WIDTH, HEIGHT - 80))
        scroll.setBorderType_(NSBezelBorder)
        scroll.setHasVerticalScroller_(True)
        table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        column = NSTableColumn.alloc().initWithIdentifier_("name")
        column.setWidth_(LIST_WIDTH - 4)
        column.setEditable_(False)
        table.addTableColumn_(column)
        table.setHeaderView_(None)
        table.setDataSource_(self)
        table.setDelegate_(self)
        table.setRowHeight_(24.0)
        scroll.setDocumentView_(table)
        content.addSubview_(scroll)
        self.table = table

        self.add_button = self._button(content, "+", "addClicked:", 20, 24, 32)
        self.remove_button = self._button(content, "−", "removeClicked:", 56, 24, 32)

        # Right: the form.
        x = 20 + LIST_WIDTH + 24
        field_x = x + 84
        field_w = WIDTH - field_x - 20
        top = HEIGHT - 44
        self.name_field = self._row(content, "Name", x, field_x, field_w, top)
        self.url_field = self._row(content, "Login URL", x, field_x, field_w, top - 34)
        self.email_field = self._row(content, "Email", x, field_x, field_w, top - 68)
        self.url_field.setPlaceholderString_("https://d-1234567890.awsapps.com/start")
        self.email_field.setPlaceholderString_("you@example.com")

        note = NSTextField.wrappingLabelWithString_(
            "Profiles grouped under a client get a Login page action that opens this URL "
            "and puts the email on the clipboard for the Username box."
        )
        note.setFrame_(NSMakeRect(x, top - 136, WIDTH - x - 20, 46))
        note.setSelectable_(False)
        note.setFont_(NSFont.systemFontOfSize_(11))
        note.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(note)

        self.members = NSTextField.labelWithString_("")
        self.members.setFrame_(NSMakeRect(x, top - 160, WIDTH - x - 20, 18))
        self.members.setFont_(NSFont.systemFontOfSize_(11))
        self.members.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self.members)

        self.error = NSTextField.labelWithString_("")
        self.error.setFrame_(NSMakeRect(x, 64, WIDTH - x - 20, 36))
        self.error.setFont_(NSFont.systemFontOfSize_(11))
        self.error.setTextColor_(NSColor.systemRedColor())
        content.addSubview_(self.error)

        self.open_button = self._button(content, "Open login page", "openClicked:", x, 24, 130)
        self.save_button = self._button(content, "Save", "saveClicked:", WIDTH - 20 - 80 - 8 - 80, 24, 80)
        done = self._button(content, "Done", "doneClicked:", WIDTH - 20 - 80, 24, 80)
        done.setKeyEquivalent_("\x1b")  # Escape closes; Return stays with Save
        self.save_button.setKeyEquivalent_("\r")

    @objc.python_method
    def _button(self, content, title, action, x, y, width):
        button = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, width, 28))
        button.setTitle_(title)
        button.setBezelStyle_(NSBezelStyleRounded)
        button.setTarget_(self)
        button.setAction_(action)
        content.addSubview_(button)
        return button

    @objc.python_method
    def _row(self, content, label, x, field_x, field_w, y):
        caption = NSTextField.labelWithString_(label)
        caption.setFrame_(NSMakeRect(x, y + 1, 76, 20))
        caption.setAlignment_(2)  # right
        content.addSubview_(caption)
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(field_x, y, field_w, 22))
        content.addSubview_(field)
        return field

    # ------------------------------------------------------------------- data

    @objc.python_method
    def reload(self, select: str | None = None):
        self.clients = self.store.list_clients()
        self.table.reloadData()
        names = [c.name for c in self.clients]
        if select in names:
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(names.index(select)), False)
        elif self.clients and not self.editing_new:
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
        self._show_selected()

    @objc.python_method
    def _selected(self):
        row = self.table.selectedRow()
        return self.clients[row] if 0 <= row < len(self.clients) else None

    @objc.python_method
    def _show_selected(self):
        client = self._selected()
        self.error.setStringValue_("")
        if client is None:
            self.name_field.setStringValue_("")
            self.url_field.setStringValue_("")
            self.email_field.setStringValue_("")
            self.members.setStringValue_("New client" if self.editing_new else "")
            self.remove_button.setEnabled_(False)
            self.open_button.setEnabled_(False)
            self.save_button.setEnabled_(self.editing_new)
            if self.editing_new:
                self.panel.makeFirstResponder_(self.name_field)
            return
        self.editing_new = False
        self.name_field.setStringValue_(client.name)
        self.url_field.setStringValue_(client.login_url)
        self.email_field.setStringValue_(client.email)
        count = sum(1 for p in self.controller._profiles if p.client == client.name)
        self.members.setStringValue_(f"{count} profile(s) grouped under {client.name}")
        self.remove_button.setEnabled_(True)
        self.open_button.setEnabled_(True)
        self.save_button.setEnabled_(True)

    def numberOfRowsInTableView_(self, table):
        return len(self.clients)

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        return self.clients[row].name if 0 <= row < len(self.clients) else ""

    def tableViewSelectionDidChange_(self, notification):
        if self.table.selectedRow() >= 0:
            self.editing_new = False
        self._show_selected()

    # ---------------------------------------------------------------- actions

    @objc.python_method
    def show(self, parent, select: str | None = None):
        self.editing_new = not self.store.list_clients()
        self.reload(select)
        parent.beginSheet_completionHandler_(self.panel, None)

    def addClicked_(self, sender):
        self.editing_new = True
        self.table.deselectAll_(None)
        self._show_selected()

    def removeClicked_(self, sender):
        client = self._selected()
        if client is None:
            return
        self.controller.remove_client(client.name, on_done=lambda: self.reload())

    def openClicked_(self, sender):
        client = self._selected()
        if client:
            self.controller.open_login_page(client.name)

    def saveClicked_(self, sender):
        name = str(self.name_field.stringValue()).strip()
        url = str(self.url_field.stringValue()).strip()
        email = str(self.email_field.stringValue()).strip()
        current = self._selected()
        try:
            if current is not None and name != current.name:
                self.store.rename_client(current.name, name)
            saved = self.store.save_client(name, url, email)
        except (StoreError, OSError) as exc:
            self.error.setStringValue_(str(exc))
            return
        self.editing_new = False
        self.reload(select=saved.name)
        self.controller.rebuild_menu()

    def doneClicked_(self, sender):
        parent = self.panel.sheetParent()
        if parent is not None:
            parent.endSheet_(self.panel)
        else:
            self.panel.orderOut_(None)
        self.controller.rebuild_menu()
