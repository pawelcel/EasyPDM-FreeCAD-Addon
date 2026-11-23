# EasyPDM add-on for FreeCAD: an EasyPDM workbench with Upload, Download, Log in and Log out,
# instead of running macros by hand. The same menu can also be shown in every workbench,
# off by default (FreeCAD's Addon Index asks add-ons to keep to their own workbench unless the
# user asks otherwise): "EasyPDM menu in every workbench" in the workbench's menu.
#
# Upload and Download live in upload.py and download.py (the former EasyPDMUpload/
# EasyPDMDownload macros), with what both use in common.py and the drawing sheet in sheet.py,
# which tools/build_templates.py generates. Each menu click reloads the module it runs, so every
# run starts from fresh per-run state, as a macro did when it was executed. Settings are in
# User parameter:BaseApp/EasyPDM (ApiBaseUrl, SavedDisplayName); the login token is kept in the
# system's secret storage (secure_store.py).
#
# Log in approves the sign-in in the browser (POST /api/auth/device/start, DeviceLoginEndpoints
# on the server): FreeCAD never sees the password, and gets its own macro token, listed and
# revocable under Settings > Users. "Log in with password" falls back to the user name and
# password dialog (common.LoginDialog), for servers older than 0.9.

import importlib
import json
import os
import sys
import urllib.error
import urllib.request
import webbrowser

import FreeCAD as App
import FreeCADGui as Gui

from . import net
from . import secure_store
from .i18n import N_, translate

# FreeCAD's own Qt wrapper (Ext/PySide), as the Addon Index asks: it maps to PySide6 or PySide2.
from PySide import QtCore, QtGui, QtWidgets

PARAM_GROUP = "User parameter:BaseApp/EasyPDM"
HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_NAME = "EasyPDM for FreeCAD"


# Item statuses as the server names them, in English; translated like any other text.
_STATUS_EN = {"w_pracy": N_("in progress"), "sprawdzany": N_("in review"), "wydany": N_("released"),
              "anulowana": N_("cancelled")}


def _tr(text, **values):
    """`text` in FreeCAD's language (i18n.py), with {name} placeholders filled from `values`."""
    out = translate(_STATUS_EN.get(text, text))
    return out.format(**values) if values else out


def _params():
    return App.ParamGet(PARAM_GROUP)


def _base_url():
    return _params().GetString("ApiBaseUrl", "http://localhost:5000/api").rstrip("/")


def _server_url():
    base = _base_url()
    return base[:-4] if base.endswith("/api") else base


def _token():
    return secure_store.get(_base_url())


def _signed_in_as():
    return _params().GetString("SavedDisplayName", "") or None


def _post(url, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Cookie"] = "pdm_session=" + token
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with net.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, {"detail": raw.decode("utf-8", "replace")}


def api(method, path, body=None):
    """A call to the EasyPDM API with the stored login: (status, parsed JSON or None)."""
    headers = {"Content-Type": "application/json"}
    token = _token()
    if token:
        headers["Cookie"] = "pdm_session=" + token
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_base_url() + path, data=data, method=method, headers=headers)
    try:
        with net.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"detail": raw}


class CommandError(Exception):
    """A message for the person, shown as it is."""


def api_error(status, body):
    """The server's reason in words: it answers refusals with a JSON string, a ProblemDetails
    object or plain text, depending on the endpoint."""
    if isinstance(body, str) and body:
        return body
    if isinstance(body, dict):
        return body.get("detail") or body.get("title") or json.dumps(body)
    return "HTTP %s" % status


def module(name, fresh=False):
    """upload, download, common or sheet; with fresh=True reloaded first, so a run starts from
    clean per-run state (upload's _RUN, download's _CANCEL) as an executed macro did."""
    full = "%s.%s" % (__package__, name)
    if full not in sys.modules:
        return importlib.import_module(full)
    return importlib.reload(sys.modules[full]) if fresh else sys.modules[full]


def item_for(doc):
    """The EasyPDM item of `doc` (as GET /items/{id} returns it), or None when it has none.

    The same match the upload makes before sending: the document's label is the record
    name and revision it was saved under ("C0012(Bracket).B"), and number and name must both
    agree with the item -- so a file merely named like one is not taken for it."""
    if doc is None:
        return None
    return module("upload").match_existing_item_single(doc)


def require_item(doc):
    if doc is None:
        raise CommandError(_tr("No document open."))
    if not _token():
        raise CommandError(_tr("Not signed in"))
    item = item_for(doc)
    if item is None:
        raise CommandError(_tr("This document is not in EasyPDM yet (upload it first)."))
    return item


def item_label(item):
    return item.get("recordName") or "%s(%s)" % (item.get("itemNumber"), item.get("name") or item.get("fileName"))


class DeviceLoginDialog(QtWidgets.QDialog):
    """Server address, then the browser: shows the code, polls until the person allows it."""

    def __init__(self, parent=None):
        super().__init__(parent or Gui.getMainWindow())
        self.setWindowTitle(_tr("EasyPDM – Log in"))
        self.setMinimumWidth(460)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(_tr("EasyPDM server address:")))
        self.server = QtWidgets.QLineEdit(_server_url())
        layout.addWidget(self.server)
        self.info = QtWidgets.QLabel(
            _tr("EasyPDM opens in the browser. Check that it shows the same code as here and click Allow."))
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.code = QtWidgets.QLabel("")
        font = self.code.font()
        font.setPointSize(font.pointSize() + 6)
        font.setBold(True)
        self.code.setFont(font)
        self.code.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.code)
        buttons = QtWidgets.QHBoxLayout()
        self.password_button = QtWidgets.QPushButton(_tr("Log in with password…"))
        self.start_button = QtWidgets.QPushButton(_tr("Log in in the browser"))
        self.start_button.setDefault(True)
        cancel = QtWidgets.QPushButton(_tr("Cancel"))
        buttons.addWidget(self.password_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self.start_button.clicked.connect(self.start)
        self.password_button.clicked.connect(self.with_password)
        cancel.clicked.connect(self.reject)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.device_code = None
        self.ticks = 0

    def _server(self):
        url = self.server.text().strip().rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        return url[:-4] if url.endswith("/api") else url

    def start(self):
        server = self._server()
        if not server:
            return
        try:
            status, body = _post(server + "/api/auth/device/start", {"client": CLIENT_NAME})
        except Exception as e:
            self.info.setText(_tr("No connection to {server}: {error}", server=server, error=e))
            return
        if status == 404:
            self.info.setText(_tr("This EasyPDM server is older than 0.9. Use “Log in with password…”."))
            return
        if status != 200 or not body:
            self.info.setText(_tr("The server refused the sign-in ({status}).", status=status))
            return
        self.device_code = body["deviceCode"]
        self.code.setText(body["userCode"])
        self.info.setText(_tr("Waiting for you to allow the sign-in in the browser…"))
        self.start_button.setEnabled(False)
        self.server.setEnabled(False)
        self.ticks = 0
        webbrowser.open(server + body["verificationPath"])
        self.timer.start(2000)

    def poll(self):
        self.ticks += 1
        if self.ticks > 300:
            self.timer.stop()
            self.info.setText(_tr("The sign-in was not allowed in time. Try again."))
            self.start_button.setEnabled(True)
            self.server.setEnabled(True)
            return
        server = self._server()
        try:
            status, body = _post(server + "/api/auth/device/token", {"deviceCode": self.device_code})
        except Exception:
            return  # a passing network hiccup; the next tick tries again
        if status == 202:
            return
        self.timer.stop()
        if status == 200 and body and body.get("sessionToken"):
            p = _params()
            p.SetString("ApiBaseUrl", server + "/api")
            secure_store.set(server + "/api", body["sessionToken"])
            p.SetString("SavedDisplayName", body.get("displayName") or body.get("username") or "")
            self.accept()
            return
        self.info.setText((body or {}).get("detail") or _tr("The sign-in did not go through ({status}).", status=status))
        self.start_button.setEnabled(True)
        self.server.setEnabled(True)

    def with_password(self):
        server = self._server()
        if server:
            _params().SetString("ApiBaseUrl", server + "/api")
        self.timer.stop()
        if module("common").LoginDialog().exec() == QtWidgets.QDialog.Accepted:
            self.accept()


def log_in():
    dialog = DeviceLoginDialog()
    if dialog.exec() == QtWidgets.QDialog.Accepted:
        App.Console.PrintMessage("EasyPDM: logged in as %s\n" % (_signed_in_as() or "?"))
        QtWidgets.QMessageBox.information(Gui.getMainWindow(), "EasyPDM",
                                          _tr("Logged in to {server} as {name}.", server=_server_url(), name=_signed_in_as() or "?"))
        _refresh_panel()
    show_account()


def log_out():
    token = _token()
    if token:
        try:
            _post(_base_url() + "/auth/logout", {}, token)
        except Exception:
            pass  # signed out here anyway; the token can be revoked under Settings > Users
    secure_store.set(_base_url(), None)
    _params().SetString("SavedDisplayName", "")
    QtWidgets.QMessageBox.information(Gui.getMainWindow(), "EasyPDM", _tr("Logged out of EasyPDM."))
    _refresh_panel()
    show_account()


def _notify(message):
    App.Console.PrintMessage("EasyPDM: %s\n" % message)
    QtWidgets.QMessageBox.information(Gui.getMainWindow(), "EasyPDM", message)
    _refresh_panel()


def _set_lock(lock):
    doc = App.ActiveDocument
    item = require_item(doc)
    status, body = api("POST", "/items/%s/%s" % (item["id"], "lock" if lock else "release"))
    if status != 200:
        raise CommandError(api_error(status, body))
    _notify(_tr("Locked for you: {item}" if lock else "Lock released: {item}", item=item_label(item)))


# --- One command each for the lock and for the account -------------------------------------
# Lock for editing / Release lock and Log in / Log out are each one button, in the toolbar, the
# menus and the panel, showing what a click would do now. FreeCAD asks a command for its text
# and icon once, so the actions are updated in place (_show_command) whenever the answer changes.

def _actions(name):
    """The QActions FreeCAD made for command `name`, and those of the menu in every workbench."""
    mw = Gui.getMainWindow()
    action_class = getattr(QtGui, "QAction", None) or QtWidgets.QAction
    found = [a for a in mw.findChildren(action_class) if a.objectName() == name]
    try:
        found += [a for a in Gui.Command.get(name).getAction() if a not in found]
    except Exception:
        pass
    return found


def _show_command(name, text, icon, tip):
    for action in _actions(name):
        action.setText(text)
        action.setIcon(QtGui.QIcon(_icon(icon)))
        action.setToolTip(tip)
        action.setStatusTip(tip)


_me = None


def my_id():
    """The signed-in user's id, asked once per server and login."""
    global _me
    key = (_server_url(), _token())
    if _me is None or _me[0] != key:
        status, me = api("GET", "/auth/me")
        _me = (key, (me or {}).get("id") if status == 200 else None)
    return _me[1]


def lock_choice(item):
    """(what a click does: "lock", "release" or None, text, icon, why not) for `item`: release
    when the signed-in user holds the lock, lock when nobody does; nothing for a released or
    cancelled item (it cannot be changed) or while someone else holds the lock."""
    if item is None:
        return None, N_("Lock for editing"), "Lock", ""
    if item.get("status") in ("wydany", "anulowana"):
        return None, N_("Lock for editing"), "Lock", _tr("Released or cancelled items cannot be changed.")
    if not item.get("ownerLocked"):
        return "lock", N_("Lock for editing"), "Lock", ""
    try:
        mine = bool(item.get("ownerId")) and item.get("ownerId") == my_id()
    except Exception:
        mine = False
    if mine:
        return "release", N_("Release lock"), "Release", ""
    return None, N_("Release lock"), "Release", _tr("Locked by {name}", name=item.get("ownerDisplayName") or "?")


def _doc_key(doc):
    return (doc.Name, doc.Label) if doc is not None else None


# What the lock button can do for the document it was last worked out for; until then (another
# document) it stays clickable and the click asks the server.
_lock_known = {"doc": None, "action": None}


def show_lock(item):
    """Puts the lock button in every place into the state for `item`, the active document's."""
    choice = lock_choice(item)
    action, text, icon, why = choice
    _lock_known.update(doc=_doc_key(App.ActiveDocument), action=action)
    tips = {"lock": N_("Lock the active document's item so only you can change it"),
            "release": N_("Release your lock on the active document's item")}
    _show_command("EasyPDM_Lock", _tr(text), icon, why or _tr(tips.get(action, tips["lock"])))
    return choice


def toggle_lock():
    doc = App.ActiveDocument
    found = require_item(doc)
    status, item = api("GET", "/items/%s" % found["id"])
    item = item if status == 200 else found
    action, _text, _icon_name, why = lock_choice(item)
    if action is None:
        show_lock(item)
        raise CommandError(why)
    _set_lock(action == "lock")
    status, item = api("GET", "/items/%s" % found["id"])
    show_lock(item if status == 200 else None)
    _refresh_panel()


class _LockCommand:
    def GetResources(self):
        return {"MenuText": _tr("Lock for editing"),
                "ToolTip": _tr("Lock the active document's item so only you can change it"),
                "Pixmap": _icon("Lock")}

    def IsActive(self):
        doc = App.ActiveDocument
        if doc is None:
            return False
        return _lock_known["doc"] != _doc_key(doc) or _lock_known["action"] is not None

    def Activated(self):
        _run_safely(toggle_lock)


def _looks_signed_in():
    """Whether the account button should offer Log out, without touching the system's secret
    storage: what this session already read or wrote there, else the display name saved at log
    in and cleared at log out. Reading the token at start-up would run secret-tool or `security`
    (and could make KWallet ask for its password) before anyone clicked anything, which the
    Addon Index rules out; the click itself still checks the real token."""
    known, token = secure_store.peek(_base_url())
    return bool(token) if known else bool(_signed_in_as())


def _account_state():
    if _looks_signed_in():
        return N_("Log out"), N_("Sign out of EasyPDM"), "LogOut"
    return N_("Log in…"), N_("Sign in to EasyPDM by approving it in the browser"), "LogIn"


def show_account():
    text, tip, icon = _account_state()
    _show_command("EasyPDM_Account", _tr(text), icon, _tr(tip))


class _AccountCommand:
    def GetResources(self):
        text, tip, icon = _account_state()
        return {"MenuText": _tr(text), "ToolTip": _tr(tip), "Pixmap": _icon(icon)}

    def IsActive(self):
        return True

    def Activated(self):
        _run_safely(log_out if _token() else log_in)


def _run_safely(action):
    try:
        action()
    except CommandError as e:
        QtWidgets.QMessageBox.warning(Gui.getMainWindow(), "EasyPDM", str(e))
    except Exception as e:  # never let a click take FreeCAD down
        App.Console.PrintError("EasyPDM: %s\n" % e)
        QtWidgets.QMessageBox.critical(Gui.getMainWindow(), "EasyPDM", str(e))


def refresh_sheet():
    """Fills the EasyPDM drawing sheet of the active document with the item's current data,
    without uploading anything -- the same refresh Upload and Download do (sheet.refresh_sheets); pages without the EasyPDM sheet are left alone and nothing is saved."""
    doc = App.ActiveDocument
    item = require_item(doc)
    sheet = module("sheet")
    changed = sheet.refresh_sheets(doc, item["id"], None, True)
    if changed:
        doc.recompute()
        _notify(_tr("Drawing sheet refreshed on {pages}.", pages=", ".join(p.Label for p in changed)))
    # Nothing changed is not the same as no sheet: a sheet already up to date used to be
    # reported as missing (seen right after a refresh that only renumbered balloons).
    elif any(sheet.is_pdm_template(getattr(o, "Template", None))
             for o in doc.Objects if o.TypeId == "TechDraw::DrawPage"):
        _notify(_tr("The drawing sheet is already up to date."))
    else:
        _notify(_tr("No page uses the EasyPDM sheet."))


def toggle_panel():
    from . import panel
    panel.toggle()


def _refresh_panel():
    try:
        from . import panel
        panel.refresh_if_open()
    except Exception:
        pass


def _icon(name):
    return os.path.join(HERE, "icons", name + ".svg")


class _Command:
    def __init__(self, text, tip, action, icon, needs_document=False):
        self.text, self.tip, self.action, self.icon, self.needs_document = text, tip, action, icon, needs_document

    def GetResources(self):
        return {"MenuText": _tr(self.text), "ToolTip": _tr(self.tip), "Pixmap": _icon(self.icon)}

    def IsActive(self):
        return App.ActiveDocument is not None if self.needs_document else True

    def Activated(self):
        _run_safely(self.action)


COMMANDS = [
    ("EasyPDM_Upload", _Command(N_("Upload"), N_("Send the active document to EasyPDM"),
                                lambda: module("upload", fresh=True).run(), "Upload", needs_document=True)),
    ("EasyPDM_Download", _Command(N_("Download"), N_("Download an item from EasyPDM with its components"),
                                  lambda: module("download", fresh=True).main(), "Download")),
    ("EasyPDM_Panel", _Command(N_("Document panel"), N_("Show the active document's item in EasyPDM"), toggle_panel, "Panel")),
    ("EasyPDM_Lock", _LockCommand()),
    ("EasyPDM_RefreshSheet", _Command(N_("Refresh drawing sheet"), N_("Fill the EasyPDM drawing sheet with the item's current data, without uploading"),
                                      refresh_sheet, "RefreshSheet", needs_document=True)),
    ("EasyPDM_Account", _AccountCommand()),
]
# Where the menus put separators: before these commands.
_GROUP_STARTS = {"EasyPDM_Panel", "EasyPDM_Account"}
_TOOLBAR = ["EasyPDM_Upload", "EasyPDM_Download", "EasyPDM_Panel", "EasyPDM_Lock", "EasyPDM_Account"]


ICON = _icon("EasyPDM")
MENU_EVERYWHERE = "MenuInEveryWorkbench"


def menu_everywhere():
    return _params().GetBool(MENU_EVERYWHERE, False)


class _MenuEverywhereCommand:
    """The opt-in for the EasyPDM menu in every workbench, a tick in the workbench's own menu."""

    def GetResources(self):
        return {"MenuText": _tr("EasyPDM menu in every workbench"),
                "ToolTip": _tr("Show the EasyPDM menu in every workbench, not only in EasyPDM"),
                "Checkable": menu_everywhere()}

    def IsActive(self):
        return True

    def Activated(self, checked=None):
        on = (not menu_everywhere()) if checked is None else bool(checked)
        _params().SetBool(MENU_EVERYWHERE, on)
        apply_menu_everywhere()


def register_commands():
    for name, command in COMMANDS:
        Gui.addCommand(name, command)
    Gui.addCommand("EasyPDM_MenuEverywhere", _MenuEverywhereCommand())


def _global_menu_action(bar):
    return next((a for a in bar.actions() if a.objectName() == "EasyPDM_Menu"), None)


# The menu in every workbench, when the user turned it on. FreeCAD rebuilds the menu bar whenever
# the workbench changes, so it is put back after every switch, at the end of the bar, before Help
# when there is one. In the EasyPDM workbench its own menu is there already.
def status_text():
    if not _token():
        return _tr("Not signed in")
    return _tr("Signed in: {name} ({server})", name=_signed_in_as() or "?", server=_server_url())


def _show_status(menu):
    """Puts who is signed in, and to which server, at the top of an EasyPDM menu, freshly each
    time it opens (a disabled entry: information, not a command)."""
    status = next((a for a in menu.actions() if a.objectName() == "EasyPDM_Status"), None)
    if status is None:
        first = menu.actions()[0] if menu.actions() else None
        status = QtGui.QAction(menu) if hasattr(QtGui, "QAction") else QtWidgets.QAction(menu)
        status.setObjectName("EasyPDM_Status")
        status.setEnabled(False)
        menu.insertAction(first, status)
        menu.insertSeparator(first)
    status.setText(status_text())


def _decorate_menus(bar):
    for action in bar.actions():
        menu = action.menu()
        if menu is None or action.text().replace("&", "") != "EasyPDM" or menu.property("easypdm_status"):
            continue
        menu.setProperty("easypdm_status", True)
        menu.aboutToShow.connect(lambda m=menu: _show_status(m))


def _decorate_own_menu():
    try:
        _decorate_menus(Gui.getMainWindow().menuBar())
        show_account()
    except Exception as e:
        App.Console.PrintError("EasyPDM: %s\n" % e)


def _sync_menu(*_):
    mw = Gui.getMainWindow()
    bar = mw.menuBar()
    ours = _global_menu_action(bar)
    if not menu_everywhere():
        if ours is not None:
            bar.removeAction(ours)
        _decorate_menus(bar)
        return
    # FreeCAD hides the menus the current workbench does not know rather than removing them
    # (opening a document does that to ours), so a hidden one does not count as being there.
    for action in bar.actions():
        if action.isVisible() and action.text().replace("&", "") == "EasyPDM":
            _decorate_menus(bar)
            return
    if ours is not None:
        ours.setVisible(True)
        _title_menu(ours.menu())
        _decorate_menus(bar)
        return
    # Always a new menu: FreeCAD deletes the menus it takes off the bar on a rebuild, so the
    # previous one may be gone on the C++ side and cannot be inserted again.
    menu = QtWidgets.QMenu(mw)
    menu.menuAction().setObjectName("EasyPDM_Menu")
    _title_menu(menu)
    for name, command in COMMANDS:
        if name in _GROUP_STARTS:
            menu.addSeparator()
        res = command.GetResources()
        action = menu.addAction(QtGui.QIcon(res["Pixmap"]), res["MenuText"])
        action.setToolTip(res["ToolTip"])
        action.setObjectName(name)  # found again by _show_command
        action.triggered.connect(lambda checked=False, n=name: Gui.runCommand(n))
    _insert_menu(bar, menu)
    _decorate_menus(bar)


def _title_menu(menu):
    # FreeCAD retranslates every menu on the bar (on the first document opened, among others)
    # from the untranslated title it keeps in the menu action's data. A menu without it was
    # given an empty title: still there and visible, but blank. "EasyPDM" needs no translation.
    menu.menuAction().setData("&EasyPDM")
    menu.setTitle("&EasyPDM")


def _insert_menu(bar, menu):
    help_action = next((a for a in bar.actions() if a.text().replace("&", "").lower() in ("help", "pomoc", "hilfe")), None)
    if help_action is not None:
        bar.insertMenu(help_action, menu)
    else:
        bar.addMenu(menu)


class _MenuBarWatch(QtCore.QObject):
    """Puts the menu back whenever FreeCAD rebuilds the menu bar.

    workbenchActivated alone is not enough: opening a document activates the document's
    workbench and then sets the bar up once more, after that signal, hiding the menus it does
    not know -- ours among them -- until the next switch of workbench. Any action removed from
    the bar, or changed (hidden), schedules one sync, run once FreeCAD is done; the sync only
    acts when the menu is missing or hidden, so its own change does not start another round."""

    _WATCHED = tuple(getattr(QtCore.QEvent, name, None) or getattr(QtCore.QEvent.Type, name)
                     for name in ("ActionRemoved", "ActionChanged"))

    def __init__(self, bar):
        super().__init__(bar)
        self._pending = False

    def eventFilter(self, watched, event):
        if event.type() in self._WATCHED and not self._pending:
            self._pending = True
            QtCore.QTimer.singleShot(0, self._sync)
        return False

    def _sync(self):
        self._pending = False
        # Only the menu: this runs on every change of a menu bar action, and the account button
        # (show_account walks all of FreeCAD's actions) is brought up to date on workbench changes.
        _safe_sync(menu_only=True)


def _safe_sync(*_, menu_only=False):
    try:
        _sync_menu() if menu_only else _sync_menu_and_buttons()
    except Exception as e:  # a menu that fails must still leave FreeCAD working, and say why
        App.Console.PrintError("EasyPDM: could not put the menu back: %s\n" % e)


def _sync_menu_and_buttons(*_):
    _sync_menu()
    # FreeCAD makes a command's actions when a workbench first shows it, with the text it had at
    # registration: bring the account button to the current login.
    show_account()


_menu_watch = None


def _on_workbench_activated(*_):
    QtCore.QTimer.singleShot(0, _safe_sync)


def install_menu():
    """At start-up: hooks into FreeCAD's menu bar only when the user turned the menu in every
    workbench on. Off (the default), the add-on stays inside its own workbench and touches
    nothing of FreeCAD's interface, as the Addon Index asks."""
    if menu_everywhere():
        _hook_menu_bar()


def _hook_menu_bar():
    global _menu_watch
    if _menu_watch is not None:
        return
    mw = Gui.getMainWindow()
    mw.workbenchActivated.connect(_on_workbench_activated)
    _menu_watch = _MenuBarWatch(mw.menuBar())
    mw.menuBar().installEventFilter(_menu_watch)
    QtCore.QTimer.singleShot(0, _safe_sync)


def _unhook_menu_bar():
    global _menu_watch
    if _menu_watch is None:
        return
    mw = Gui.getMainWindow()
    try:
        mw.workbenchActivated.disconnect(_on_workbench_activated)
    except (RuntimeError, TypeError):
        pass
    mw.menuBar().removeEventFilter(_menu_watch)
    _menu_watch.deleteLater()
    _menu_watch = None


def apply_menu_everywhere():
    """After the option changed: hooks in and shows the menu, or takes both away again."""
    if menu_everywhere():
        _hook_menu_bar()
    else:
        _unhook_menu_bar()
    _safe_sync()


class EasyPDMWorkbench(Gui.Workbench):
    """The EasyPDM workbench: its menu and toolbar hold the add-on's commands."""

    MenuText = "EasyPDM"
    ToolTip = "Upload to and download from an EasyPDM server"
    Icon = ICON

    def Initialize(self):
        menu = []
        for name, _ in COMMANDS:
            if name in _GROUP_STARTS:
                menu.append("Separator")
            menu.append(name)
        self.appendToolbar("EasyPDM", _TOOLBAR)
        self.appendMenu("EasyPDM", menu + ["Separator", "EasyPDM_MenuEverywhere"])

    def Activated(self):
        # Once FreeCAD has built this workbench's menu: who is signed in at its top, and the
        # account button brought to the current login (FreeCAD made it with the registration text).
        QtCore.QTimer.singleShot(0, _decorate_own_menu)

    def GetClassName(self):
        return "Gui::PythonWorkbench"
