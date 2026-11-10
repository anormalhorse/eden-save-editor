"""
Eden-Based Save Editor - easy version (modern dark/light look).

Run:  python eden_save_editor.py
Before editing: CLOSE the game/emulator.
Every edit creates a backup in the "save_backups" folder inside the data folder
(Windows: C:/Users/<you>/EdenSaveEditor; the "Data folder" button in the header opens it).

Also browses/searches game archives in .nup format without extracting them, and game
folders (the extracted contents of a NUP) the same way (button "Game archive / folder...").
Compressed data needs:  pip install zstandard

Finding EVERYTHING in a save (inventory, quest/story flags...):
  - "All values..." lists every value of the file (key, whole, decimal, raw hex), lets you
    name keys, load a list of names from the game (the key is a hash of the name; the program
    detects which hash type is used), edit one or many values at once and export to CSV.
  - "Compare two saves..." shows what changed after doing ONE thing in the game.
  - "Data blocks..." shows the variable-size area after the hash table (lists of IDs, etc.).
    Adding/removing entries from a size-prefixed list automatically updates the size field
    and every later pointer so the save stays valid. Special labels for Ocarina of Time Remake
    (title 0400486024DEC000) when that game is open.
  - "Restore a backup..." puts back any earlier version of the file.

Updates: at every startup (and with the "Updates" button) the program looks for a newer release on
GitHub. A newer version is mandatory: it is downloaded and installed by itself, then the program restarts.
It also checks the project's saved_fields.json: if GitHub has entries you miss, a bar
suggests adding them (all at once or reviewing them first). Nothing of yours is ever removed. The previous version is kept in the
"program_backups" folder inside the data folder.
"""
import csv
import glob
import hashlib
import io
import json
import mmap
import os
import queue
import re
import shutil
import struct
import sys
import threading
import time
import webbrowser
import zlib
from collections import Counter

APP_VERSION = "1.2.2"
BASE = os.path.dirname(os.path.abspath(__file__))  # where the program is (icon, assets)


def _data_dir():
    """ONE folder for the user's own files (names, fields, settings, backups), kept apart from the
    program so they survive updates and a moved/deleted program folder.

    It is always the same folder, however the program is started (script, Store Python, .exe):
      Windows : C:\\Users\\<you>\\EdenSaveEditor   (visible in Explorer, never redirected by Python
                from the Microsoft Store, unlike AppData)
      macOS   : ~/Library/Application Support/EdenSaveEditor
      Linux   : ~/.config/EdenSaveEditor
    To use another folder (e.g. Documents), set the environment variable EDEN_SAVE_EDITOR_DATA.
    Falls back to the program folder if the folder cannot be created."""
    custom = os.environ.get("EDEN_SAVE_EDITOR_DATA")
    if custom:
        d = os.path.abspath(os.path.expanduser(custom))
    else:
        if os.name == "nt":
            root = os.path.expanduser("~")
        elif sys.platform == "darwin":
            root = os.path.expanduser("~/Library/Application Support")
        else:
            root = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        d = os.path.join(root, "EdenSaveEditor")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return BASE


DATA_DIR = _data_dir()
BACKUP_DIR = os.path.join(DATA_DIR, "save_backups")
FIELDS_FILE = os.path.join(DATA_DIR, "saved_fields.json")
START_DIR = os.path.expandvars(r"%APPDATA%\eden\nand\user\save")
NAMES_FILE = os.path.join(DATA_DIR, "hash_names.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "ui_settings.json")

# ---- one-time consolidation of the data folders that older versions left around -------------
# Older versions stored the same files in different places depending on how the program was
# started: next to the program (e.g. in Documents), in %APPDATA%\EdenSaveEditor, or in the hidden
# private folder of Python from the Microsoft Store (...\Packages\PythonSoftwareFoundation.Python.*\
# LocalCache\Roaming\EdenSaveEditor). Everything is merged into DATA_DIR (nothing of yours is
# overwritten or deleted) and the old copies are moved to DATA_DIR\old_data_migrated\.
_DATA_FILES = ("saved_fields.json", "hash_names.json", "ui_settings.json")
OLD_DATA_SUBDIR = "old_data_migrated"


def _deep_add_missing(dst, src):
    """Add to dict dst whatever dict src has and dst lacks (recursive). Values already in dst win."""
    changed = False
    for k, v in src.items():
        if k not in dst:
            dst[k] = v
            changed = True
        elif isinstance(dst[k], dict) and isinstance(v, dict) and _deep_add_missing(dst[k], v):
            changed = True
    return changed


def _merge_json_file(src, dst):
    """Copy src to dst, or, if dst exists, add to it the entries it is missing (atomic write).
    Raises if a file cannot be read, so the caller keeps the original untouched."""
    if not os.path.exists(dst):
        shutil.copy2(src, dst)
        return
    with open(src, encoding="utf-8-sig") as f:
        s = json.load(f)
    with open(dst, encoding="utf-8-sig") as f:
        d = json.load(f)
    if isinstance(s, dict) and isinstance(d, dict) and _deep_add_missing(d, s):
        tmp = dst + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)


def _merge_backups(src_dir, dst_dir, label):
    """Copy every backup file of src_dir that dst_dir does not have (a different file with the same
    name is kept under another name)."""
    import filecmp
    os.makedirs(dst_dir, exist_ok=True)
    for n in os.listdir(src_dir):
        s, d = os.path.join(src_dir, n), os.path.join(dst_dir, n)
        if not os.path.isfile(s):
            continue
        if os.path.exists(d):
            if filecmp.cmp(s, d, shallow=False):
                continue
            d = os.path.join(dst_dir, f"{n}.from_{label}")
            if os.path.exists(d):
                continue
        shutil.copy2(s, d)


def _archive_dir(label):
    base = os.path.join(DATA_DIR, OLD_DATA_SUBDIR)
    os.makedirs(base, exist_ok=True)
    d, i = os.path.join(base, label), 1
    while os.path.exists(d):
        i += 1
        d = os.path.join(base, f"{label}_{i}")
    os.makedirs(d)
    return d


def _consolidate_one(label, src_dir, is_program_dir, data_dir=None):
    """Merge one old folder into the data folder, then move its data to old_data_migrated/<label>.
    Returns True if something was merged. On any problem the original files stay where they are."""
    data_dir = data_dir or DATA_DIR
    names = [n for n in _DATA_FILES if os.path.isfile(os.path.join(src_dir, n))]
    has_bk = os.path.isdir(os.path.join(src_dir, "save_backups"))
    if not names and not has_bk and is_program_dir:
        return False
    if not is_program_dir and not names and not has_bk and not os.listdir(src_dir):
        try:
            os.rmdir(src_dir)  # an empty leftover folder
        except OSError:
            pass
        return False
    ok, merged = [], []
    for n in names:
        try:
            _merge_json_file(os.path.join(src_dir, n), os.path.join(data_dir, n))
            ok.append(n)
        except (OSError, ValueError):
            pass
    bk_ok = False
    if has_bk:
        try:
            _merge_backups(os.path.join(src_dir, "save_backups"), os.path.join(data_dir, "save_backups"), label)
            bk_ok = True
        except OSError:
            pass
    all_ok = len(ok) == len(names) and (bk_ok or not has_bk)
    if not all_ok:
        return bool(ok or bk_ok)
    try:
        arch = _archive_dir(label)
        if is_program_dir:  # only the data files: the program itself stays where it is
            for n in ok:
                shutil.move(os.path.join(src_dir, n), os.path.join(arch, n))
            if has_bk:
                shutil.move(os.path.join(src_dir, "save_backups"), os.path.join(arch, "save_backups"))
        else:  # a pure data folder: move all of it, so that it disappears from its old place
            shutil.rmtree(arch, ignore_errors=True)
            shutil.move(src_dir, arch)
    except (OSError, shutil.Error):
        pass
    return True


def _legacy_data_folders():
    """(label, folder, is_program_folder) of every place an older version may have used."""
    out = [("program_folder", BASE, True)]
    if os.name == "nt":
        home = os.path.expanduser("~")
        ap = os.environ.get("APPDATA")
        if ap:
            out.append(("appdata_roaming", os.path.join(ap, "EdenSaveEditor"), False))
        la = os.environ.get("LOCALAPPDATA")
        if la:
            pat = os.path.join(la, "Packages", "PythonSoftwareFoundation.Python.*", "LocalCache", "Roaming",
                               "EdenSaveEditor")
            for i, d in enumerate(sorted(glob.glob(pat))):
                out.append((f"python_store_cache{i or ''}", d, False))
        homes = [home, os.path.join(home, "OneDrive"), os.environ.get("OneDrive") or ""]
        for h in homes:
            for docs in ("Documents", "Documenti"):
                if h:
                    out.append(("documents", os.path.join(h, docs, "EdenSaveEditor"), False))
    return out


def _consolidate_data_folders():
    if os.path.abspath(DATA_DIR) == os.path.abspath(BASE):
        return
    seen = {os.path.normcase(os.path.abspath(DATA_DIR))}
    for label, d, is_prog in _legacy_data_folders():
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or not os.path.isdir(d):
            continue
        seen.add(key)
        # never touch the archive itself or a folder that contains the data folder
        if os.path.normcase(os.path.abspath(DATA_DIR)).startswith(key + os.sep):
            continue
        try:
            _consolidate_one(label, d, is_prog)
        except Exception:
            pass  # never block the start of the program because of this


_consolidate_data_folders()
PALETTES = {
    "dark": dict(bg="#12141a", surface="#1b1e27", surface2="#252a38", border="#2f3446",
                 text="#e8eaf0", muted="#8d95aa", accent="#7c5cff", accent_hi="#9680ff",
                 accent_fg="#ffffff", danger="#ff5c6c", ok="#3ddc97", sel="#33305f",
                 zebra="#1f2330"),
    "light": dict(bg="#f3f4f8", surface="#ffffff", surface2="#eceef5", border="#d6d9e5",
                  text="#1d2030", muted="#6a7189", accent="#6a47f5", accent_hi="#7f60ff",
                  accent_fg="#ffffff", danger="#d93a4c", ok="#17a36a", sel="#ddd6ff",
                  zebra="#f7f8fc"),
}
_THEME = {"mode": "dark"}


def load_ui_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ui_settings(d):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def pal():
    return PALETTES[_THEME["mode"]]


def _recolor_tk(w, p):
    """Recolor classic tk widgets (Tk, Toplevel, Listbox) that ttk styles don't reach."""
    try:
        cls = w.winfo_class()
        if cls in ("Tk", "Toplevel", "Frame"):
            w.configure(bg=p["bg"])
        elif cls == "Listbox":
            w.configure(bg=p["surface"], fg=p["text"], selectbackground=p["sel"],
                        selectforeground=p["text"], highlightthickness=1,
                        highlightbackground=p["border"], highlightcolor=p["accent"],
                        relief="flat", borderwidth=0, activestyle="none")
        elif cls == "Text":
            w.configure(bg=p["surface"], fg=p["text"], insertbackground=p["text"],
                        selectbackground=p["sel"], relief="flat")
    except Exception:
        pass
    for c in w.winfo_children():
        _recolor_tk(c, p)


def apply_theme(root, mode=None):
    """Modern flat theme for every ttk widget (also in the pop-up windows)."""
    from tkinter import ttk
    import tkinter.font as tkfont
    if mode:
        _THEME["mode"] = mode
    p = pal()
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except Exception:
        pass
    families = set(tkfont.families())
    ui = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Inter",
                           "Helvetica Neue", "DejaVu Sans") if f in families), "TkDefaultFont")
    mono = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono")
                 if f in families), "TkFixedFont")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=ui, size=10)
        except Exception:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=mono, size=10)
    except Exception:
        pass
    root.configure(bg=p["bg"])
    root.option_add("*Toplevel.background", p["bg"])
    root.option_add("*Listbox.background", p["surface"])
    root.option_add("*Listbox.foreground", p["text"])
    root.option_add("*Listbox.selectBackground", p["sel"])
    root.option_add("*Listbox.selectForeground", p["text"])
    root.option_add("*Listbox.highlightThickness", 1)
    root.option_add("*Listbox.relief", "flat")
    root.option_add("*Listbox.activestyle", "none")
    root.option_add("*TCombobox*Listbox.background", p["surface"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])

    st.configure(".", background=p["bg"], foreground=p["text"], fieldbackground=p["surface2"],
                 bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                 troughcolor=p["surface"], focuscolor=p["accent"], font=(ui, 10))
    st.configure("TFrame", background=p["bg"])
    st.configure("Card.TFrame", background=p["surface"], relief="flat")
    st.configure("TLabel", background=p["bg"], foreground=p["text"])
    st.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
    st.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
    st.configure("CardMuted.TLabel", background=p["surface"], foreground=p["muted"])
    st.configure("Title.TLabel", background=p["bg"], foreground=p["text"], font=(ui, 20, "bold"))
    st.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"], font=(ui, 10))
    st.configure("Link.TLabel", background=p["bg"], foreground=p["accent_hi"], font=(ui, 10, "underline"))
    st.configure("Section.TLabel", background=p["surface"], foreground=p["text"], font=(ui, 11, "bold"))
    st.configure("Badge.TLabel", background=p["surface2"], foreground=p["accent_hi"],
                 font=(ui, 9, "bold"), padding=(8, 2))
    st.configure("Status.TLabel", background=p["surface"], foreground=p["muted"], padding=(12, 6))
    st.configure("TLabelframe", background=p["surface"], bordercolor=p["border"], relief="solid",
                 borderwidth=1, padding=8)
    st.configure("TLabelframe.Label", background=p["surface"], foreground=p["accent_hi"],
                 font=(ui, 10, "bold"))
    st.configure("TSeparator", background=p["border"])

    st.configure("TButton", background=p["surface2"], foreground=p["text"], bordercolor=p["border"],
                 focusthickness=0, padding=(12, 6), relief="flat", borderwidth=1)
    st.map("TButton", background=[("disabled", p["surface"]), ("pressed", p["border"]),
                                  ("active", p["border"])],
           foreground=[("disabled", p["muted"])], bordercolor=[("focus", p["accent"])])
    st.configure("Accent.TButton", background=p["accent"], foreground=p["accent_fg"],
                 bordercolor=p["accent"], padding=(16, 7), font=(ui, 10, "bold"))
    st.map("Accent.TButton", background=[("disabled", p["surface2"]), ("pressed", p["accent"]),
                                         ("active", p["accent_hi"])],
           foreground=[("disabled", p["muted"])])
    st.configure("Danger.TButton", background=p["surface2"], foreground=p["danger"],
                 bordercolor=p["border"], padding=(12, 6))
    st.map("Danger.TButton", background=[("active", p["danger"])],
           foreground=[("active", "#ffffff")])
    st.configure("Ghost.TButton", background=p["bg"], foreground=p["muted"], bordercolor=p["bg"],
                 padding=(8, 4))
    st.map("Ghost.TButton", background=[("active", p["surface2"])],
           foreground=[("active", p["text"])])

    st.configure("TEntry", fieldbackground=p["surface2"], foreground=p["text"],
                 insertcolor=p["text"], bordercolor=p["border"], padding=6)
    st.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])],
           darkcolor=[("focus", p["accent"])])
    st.configure("TCombobox", fieldbackground=p["surface2"], background=p["surface2"],
                 foreground=p["text"], arrowcolor=p["text"], bordercolor=p["border"], padding=5)
    st.map("TCombobox", fieldbackground=[("readonly", p["surface2"])],
           bordercolor=[("focus", p["accent"])])
    st.configure("TSpinbox", fieldbackground=p["surface2"], foreground=p["text"],
                 arrowcolor=p["text"], bordercolor=p["border"])
    for w in ("TCheckbutton", "TRadiobutton"):
        st.configure(w, background=p["bg"], foreground=p["text"],
                     indicatorbackground=p["surface2"], indicatorforeground=p["accent_fg"],
                     upperbordercolor=p["muted"], lowerbordercolor=p["muted"],
                     indicatormargin=(0, 0, 6, 0), padding=(2, 3))
        st.map(w, background=[("active", p["bg"])],
               indicatorbackground=[("selected", p["accent"]), ("pressed", p["accent_hi"]),
                                    ("active", p["border"])],
               indicatorforeground=[("selected", p["accent_fg"]), ("!selected", p["surface2"])],
               upperbordercolor=[("selected", p["accent"]), ("active", p["accent_hi"])],
               lowerbordercolor=[("selected", p["accent"]), ("active", p["accent_hi"])])

    st.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                 foreground=p["text"], rowheight=28, borderwidth=0, bordercolor=p["border"])
    st.map("Treeview", background=[("selected", p["sel"])],
           foreground=[("selected", p["text"])])
    st.configure("Treeview.Heading", background=p["surface2"], foreground=p["muted"],
                 relief="flat", font=(ui, 9, "bold"), padding=(8, 6), borderwidth=0)
    st.map("Treeview.Heading", background=[("active", p["border"])],
           foreground=[("active", p["text"])])
    st.configure("Vertical.TScrollbar", background=p["surface2"], troughcolor=p["bg"],
                 bordercolor=p["bg"], arrowcolor=p["muted"], relief="flat", gripcount=0)
    st.configure("Horizontal.TScrollbar", background=p["surface2"], troughcolor=p["bg"],
                 bordercolor=p["bg"], arrowcolor=p["muted"], relief="flat", gripcount=0)
    st.map("Vertical.TScrollbar", background=[("active", p["border"])])
    st.map("Horizontal.TScrollbar", background=[("active", p["border"])])
    st.configure("TNotebook", background=p["bg"], bordercolor=p["border"], tabmargins=(2, 6, 2, 0))
    # tabs that read as buttons: always a visible box, a hover colour, no jumping or dotted focus
    st.configure("TNotebook.Tab", background=p["surface2"], foreground=p["muted"], padding=(22, 9),
                 bordercolor=p["border"], lightcolor=p["surface2"], darkcolor=p["surface2"],
                 focuscolor=p["surface2"], font=(ui, 10))
    st.map("TNotebook.Tab",
           background=[("selected", p["surface"]), ("active", p["border"])],
           foreground=[("selected", p["accent_hi"]), ("active", p["text"])],
           lightcolor=[("selected", p["accent"])], darkcolor=[("selected", p["surface"])],
           bordercolor=[("selected", p["accent"])], focuscolor=[("selected", p["surface"])],
           padding=[("selected", (22, 9)), ("active", (22, 9))],  # the theme shrinks the selected tab otherwise
           expand=[("selected", (0, 0, 0, 0))])
    st.configure("Horizontal.TProgressbar", background=p["accent"], troughcolor=p["surface2"],
                 bordercolor=p["surface2"], lightcolor=p["accent"], darkcolor=p["accent"])
    st.configure("TPanedwindow", background=p["bg"])
    st.configure("Sash", background=p["border"])
    _recolor_tk(root, p)


def autofit(w, width, height):
    """Open a window at least 'width' x 'height', but never smaller than what its widgets need
    (so no button is cut off), never bigger than the screen, and centred."""
    def go():
        try:
            w.update_idletasks()
            sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
            rw, rh = w.winfo_reqwidth(), w.winfo_reqheight()
            nw = min(max(width, rw + 8), sw - 60)
            nh = min(max(height, rh + 8), sh - 110)
            x, y = max(0, (sw - nw) // 2), max(0, (sh - nh) // 3)
            w.geometry(f"{nw}x{nh}+{x}+{y}")
            w.minsize(min(rw, nw), min(rh, nh, 520))
        except Exception:
            pass
    w.after(0, go)


# ------------------------------------------------------------ themed question pop-ups
def _dialog_window(parent, title):
    """A modal pop-up that follows the program theme. Returns (window, content frame)."""
    import tkinter as tk
    from tkinter import ttk
    parent = parent or tk._default_root
    d = tk.Toplevel(parent)
    d.withdraw()
    d.title(title)
    d.configure(bg=pal()["bg"])
    d.resizable(False, False)
    try:
        d.transient(parent.winfo_toplevel())
    except Exception:
        pass
    body = ttk.Frame(d, padding=(22, 18, 22, 10))
    body.pack(fill="both", expand=True)
    return d, body


def _dialog_buttons(d, buttons):
    """Right-aligned buttons under a thin line. buttons = [(text, command, is_accent)], the LAST is the main one."""
    from tkinter import ttk
    ttk.Separator(d, orient="horizontal").pack(fill="x")
    bar = ttk.Frame(d, padding=(22, 12, 22, 14))
    bar.pack(fill="x")
    made = []
    for text, cmd, accent in reversed(buttons):
        b = ttk.Button(bar, text=text, command=cmd, style="Accent.TButton" if accent else "TButton", width=14)
        b.pack(side="right", padx=(8, 0))
        made.append(b)
    return made


def _dialog_show(d, parent, width):
    """Centre the pop-up over its parent, make it modal and wait until it is closed."""
    import tkinter as tk
    parent = parent or tk._default_root
    d.update_idletasks()
    rw, rh = max(width, d.winfo_reqwidth()), d.winfo_reqheight()
    try:
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - rw) // 2
        y = top.winfo_rooty() + (top.winfo_height() - rh) // 3
    except Exception:
        x, y = 200, 150
    sw, sh = d.winfo_screenwidth(), d.winfo_screenheight()
    d.geometry(f"{rw}x{rh}+{max(0, min(x, sw - rw))}+{max(0, min(y, sh - rh - 40))}")
    d.deiconify()
    try:
        d.wait_visibility()
        d.grab_set()
    except tk.TclError:
        pass
    d.wait_window()


def _bold_font(size=12):
    import tkinter.font as tkfont
    f = tkfont.nametofont("TkDefaultFont").copy()
    f.configure(size=size, weight="bold")
    return f


NOTIFY_AFTER = 5.0  # seconds: a task that ends sooner than this does not notify


def notify_done(win, title, text, seconds=9):
    """Tell the user a long task has ended, even if the window is behind others or minimised: a short
    sound, the taskbar button flashes (Windows) and a small themed card appears in a corner of the screen.
    Clicking the card brings the window back."""
    import tkinter as tk
    from tkinter import ttk
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        else:
            win.bell()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3 | 0xC, 0, 0)  # taskbar + caption, until focused
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass
    try:
        p = pal()
        t = tk.Toplevel(win)
        t.withdraw()
        t.overrideredirect(True)
        try:
            t.attributes("-topmost", True)
        except tk.TclError:
            pass
        t.configure(bg=p["accent"])  # 2 px accent border around the card
        card = ttk.Frame(t, style="Card.TFrame", padding=(16, 12))
        card.pack(padx=2, pady=2)
        ttk.Label(card, text="✔  " + title, style="Card.TLabel", font=_bold_font(11)).pack(anchor="w")
        ttk.Label(card, text=text, style="CardMuted.TLabel", wraplength=300, justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Label(card, text="Click to open the window", style="CardMuted.TLabel").pack(anchor="w", pady=(6, 0))

        def bring_back(_e=None):
            try:
                if win.state() == "iconic":
                    win.deiconify()
                win.lift()
                win.focus_force()
            except tk.TclError:
                pass
            t.destroy()

        for wd in (t, card, *card.winfo_children()):
            wd.bind("<Button-1>", bring_back)
        t.update_idletasks()
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        tw, th = t.winfo_reqwidth(), t.winfo_reqheight()
        t.geometry(f"+{sw - tw - 24}+{sh - th - 80}")
        t.deiconify()
        t.after(int(seconds * 1000), lambda: t.winfo_exists() and t.destroy())
    except Exception:
        pass



def ask_text(parent, title, heading, detail="", prompt="", examples=None, initial="", ok_text="OK", width=520):
    """Themed replacement for simpledialog.askstring. Returns the text (maybe empty) or None on Cancel."""
    import tkinter as tk
    from tkinter import ttk
    res = {"v": None}
    d, body = _dialog_window(parent, title)
    d._font = _bold_font()  # keep a reference
    ttk.Label(body, text=heading, font=d._font).pack(anchor="w")
    if detail:
        ttk.Label(body, text=detail, style="Muted.TLabel", wraplength=width - 50, justify="left"
                  ).pack(anchor="w", pady=(6, 0))
    if examples:
        box = ttk.Frame(body, style="Card.TFrame", padding=(12, 8))
        box.pack(fill="x", pady=(10, 0))
        ttk.Label(box, text="Examples", style="CardMuted.TLabel").pack(anchor="w")
        for ex in examples:
            ttk.Label(box, text=ex, style="Card.TLabel", font="TkFixedFont").pack(anchor="w", pady=(2, 0))
    if prompt:
        ttk.Label(body, text=prompt).pack(anchor="w", pady=(14, 4))
    var = tk.StringVar(value=initial)
    ent = ttk.Entry(body, textvariable=var)
    ent.pack(fill="x", pady=(0 if prompt else 14, 4), ipady=3)

    def ok(_e=None):
        res["v"] = var.get()
        d.destroy()

    def cancel(_e=None):
        d.destroy()

    _dialog_buttons(d, [("Cancel", cancel, False), (ok_text, ok, True)])
    d.bind("<Return>", ok)
    d.bind("<Escape>", cancel)
    d.protocol("WM_DELETE_WINDOW", cancel)
    d.after(50, lambda: (ent.focus_force(), ent.select_range(0, "end"), ent.icursor("end")))
    _dialog_show(d, parent, width)
    return res["v"]


def ask_yesno(title, message, parent=None, icon=None, yes_text="Yes", no_text="No", width=480):
    """Themed replacement for messagebox.askyesno (same arguments). With icon='warning' the default is No."""
    from tkinter import ttk
    res = {"v": False}
    d, body = _dialog_window(parent, title)
    row = ttk.Frame(body)
    row.pack(fill="x")
    warn = icon == "warning"
    d._font = _bold_font(22)
    ttk.Label(row, text="⚠" if warn else "?", font=d._font,
              foreground=pal()["accent_hi"] if not warn else "#f0a030").pack(side="left", anchor="n", padx=(0, 14))
    ttk.Label(row, text=message, wraplength=width - 110, justify="left").pack(side="left", fill="x", expand=True)

    def yes(_e=None):
        res["v"] = True
        d.destroy()

    def no(_e=None):
        d.destroy()

    ybtn, nbtn = _dialog_buttons(d, [(no_text, no, warn), (yes_text, yes, not warn)])
    d.bind("<Return>", no if warn else yes)
    d.bind("<Escape>", no)
    d.protocol("WM_DELETE_WINDOW", no)
    d.after(50, lambda: (nbtn if warn else ybtn).focus_force())
    _dialog_show(d, parent, width)
    return res["v"]


def pack_scroll(tv, **kw):
    """Pack a Treeview with a vertical scrollbar next to it (mouse wheel works too)."""
    from tkinter import ttk
    sb = ttk.Scrollbar(tv.master, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y", pady=kw.get("pady", 0))
    tv.pack(side="left", fill="both", expand=True, **kw)


def zebra(tv):
    """Alternate row colours (call after filling a Treeview)."""
    p = pal()
    tv.tag_configure("odd", background=p["zebra"])
    for i, iid in enumerate(tv.get_children("")):
        tv.item(iid, tags=("odd",) if i % 2 else ())


# ------------------------------------------------------------ logic
def read(path):
    with open(path, "rb") as f:
        return bytearray(f.read())


def title_id(path):
    """The game ID is the last 16-character hex folder in the path."""
    parti = [x for x in path.replace("\\", "/").split("/") if re.fullmatch(r"[0-9A-Fa-f]{16}", x)]
    return parti[-1].upper() if parti else "generic"


# 32-bit value types: struct format, allowed range (integers only) and the names shown in the wizard
_FMT = {"int32": "<i", "uint32": "<I", "float32": "<f"}
_RANGE = {"int32": (-(2 ** 31), 2 ** 31 - 1), "uint32": (0, 2 ** 32 - 1)}
KINDS = {"Whole number": "int32", "Decimal number": "float32", "Whole number (unsigned)": "uint32"}


def get_value(data, offset, kind):
    if offset + 4 > len(data):
        return None
    return struct.unpack_from(_FMT[kind], data, offset)[0]


def fmt_value(v, kind):
    if v is None:
        return "?"
    return str(v) if kind in _RANGE else f"{v:.2f}"


def parse_value(text, kind):
    text = text.strip().replace(",", ".")
    if kind in _RANGE:
        n = int(float(text))
        if not _RANGE[kind][0] <= n <= _RANGE[kind][1]:
            raise ValueError("out of range")
        return n
    return float(text)


def find_value(data, kind, value, tol=0.01):
    out = []
    if kind in _RANGE:
        raw = struct.pack(_FMT[kind], int(value))
        for off in range(0, len(data) - 3, 4):
            if data[off:off + 4] == raw:
                out.append(off)
    else:
        for off in range(0, len(data) - 3, 4):
            x = struct.unpack_from("<f", data, off)[0]
            if x == x and abs(x - float(value)) <= tol:
                out.append(off)
    return out


def index_hash(data):
    """Entries are (key, value) pairs of 4+4 bytes, starting at 0x28 and ending where the
    table ends (number at offset 8 of the header). Returns key -> key position,
    only for keys that appear exactly once in the table."""
    if len(data) < 0x30:
        return {}
    table_end = min(struct.unpack_from("<I", data, 8)[0], len(data))
    pos = range(0x28, table_end - 7, 8)
    keys = [struct.unpack_from("<I", data, o)[0] for o in pos]
    cnt = Counter(keys)
    return {k: o for k, o in zip(keys, pos) if cnt[k] == 1}


def entry_value(data, idx, h, kind):
    o = idx.get(h)
    return None if o is None else get_value(data, o + 4, kind)


def match(v, kind, target, tol=0.01):
    if v is None:
        return False
    return v == target if kind in _RANGE else (v == v and abs(v - target) <= tol)


def cand_value(data, idx, k, kind):
    """Current value of a candidate: ('h', key) or ('o', position)."""
    return entry_value(data, idx, k[1], kind) if k[0] == "h" else get_value(data, k[1], kind)


def scan_candidates(data, idx, kind, val):
    """All places in the file that contain 'val': keyed entries first, then other positions."""
    cand = [("h", h) for h in idx if match(entry_value(data, idx, h, kind), kind, val)]
    entry_positions = {o + 4 for o in idx.values()}
    cand += [("o", o) for o in find_value(data, kind, val) if o not in entry_positions]
    return cand


def filter_candidates(cand, data, idx, kind, val):
    return [k for k in cand if match(cand_value(data, idx, k, kind), kind, val)]


def backup(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base, stamp, n = os.path.basename(path), time.strftime('%Y%m%d_%H%M%S'), 1
    backup_path = os.path.join(BACKUP_DIR, f"{base}.{stamp}.bak")
    while os.path.exists(backup_path):
        n += 1
        backup_path = os.path.join(BACKUP_DIR, f"{base}.{stamp}_{n}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def write_value(path, data, offset, kind, value):
    raw = struct.pack(_FMT[kind], value)
    backup_path = backup(path)
    data[offset:offset + 4] = raw
    with open(path, "wb") as f:
        f.write(data)
    return backup_path


def find_string_slots(data, min_len=4, max_slot=64):
    """Find printable ASCII/UTF-8-ish strings and the fixed slot they sit in.
    Returns list of (offset, text, slot_size). Slot size is the room available for
    the string including the terminating NUL (typical game saves use 32 or 64)."""
    raw = bytes(data)
    out = []
    for m in re.finditer(rb"[A-Za-z_][ -~]{3,}", raw):
        o = m.start()
        text = m.group().decode("latin-1")
        # Slot: from start, include the string, optional NUL, then trailing zeros.
        # Cap at max_slot; also stop if we hit non-zero after the first NUL.
        end = o + len(text)
        if end < len(raw) and raw[end] == 0:
            end += 1
            while end < len(raw) and end - o < max_slot and raw[end] == 0:
                end += 1
        slot = min(max(end - o, len(text) + 1), max_slot)
        # Prefer aligned 32/64 slots when the string is short and the rest is zero-padded.
        for prefer in (64, 32):
            if o % 4 == 0 and prefer >= len(text) + 1 and o + prefer <= len(raw):
                pad = raw[o + len(text):o + prefer]
                if all(b == 0 for b in pad):
                    slot = prefer
                    break
        out.append((o, text, slot))
    return out


def write_string(path, data, offset, slot_size, new_text):
    """Write a new string into a fixed slot (NUL-terminated, zero-padded)."""
    encoded = new_text.encode("ascii", "strict")  # game names are almost always ASCII
    if len(encoded) + 1 > slot_size:
        raise ValueError(f"Text is too long for this slot (max {slot_size - 1} characters).")
    backup_path = backup(path)
    data[offset:offset + slot_size] = encoded + b"\x00" + b"\x00" * (slot_size - len(encoded) - 1)
    with open(path, "wb") as f:
        f.write(data)
    return backup_path


def load_fields():
    try:
        with open(FIELDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_fields(fields):
    with open(FIELDS_FILE, "w", encoding="utf-8") as f:
        json.dump(fields, f, indent=2, ensure_ascii=False)



# ------------------------------------------------------------ all values / hash names
def table_entries(data):
    """Every (value position, key, raw 32-bit value) of the hash table, in file order
    (duplicate keys included, unlike index_hash)."""
    if len(data) < 0x30:
        return []
    end = min(struct.unpack_from("<I", data, 8)[0], len(data))
    return [(o + 4,) + struct.unpack_from("<II", data, o) for o in range(0x28, end - 7, 8)]


def raw_views(raw):
    """The same 32 bits seen as a signed whole number and as a decimal number."""
    return (struct.unpack("<i", struct.pack("<I", raw))[0],
            struct.unpack("<f", struct.pack("<I", raw))[0])


def fmt_float(f):
    """Decimal view, empty when the bits are clearly not a sensible decimal number."""
    if f != f or f in (float("inf"), float("-inf")):
        return ""
    if f != 0 and not (1e-4 <= abs(f) < 1e9):
        return ""
    return f"{f:.4g}"


def encode_value(text, kind):
    """Text -> 4 raw bytes. kind: int32, uint32, float32 or hex (raw 32-bit pattern, e.g. 0x3F800000)."""
    t = text.strip().replace(",", ".")
    if not t:
        raise ValueError("empty")
    try:
        if kind == "hex":
            return struct.pack("<I", int(t, 16))
        if kind == "float32":
            return struct.pack("<f", float(t))
        n = int(t, 0) if t.lower().lstrip("-").startswith("0x") else int(float(t))
        return struct.pack(_FMT[kind], n)
    except (struct.error, OverflowError):
        raise ValueError("Number out of range for this type.")


def write_raws(path, data, items):
    """items = [(position, 4 raw bytes)]. Writes them all with ONE backup."""
    backup_path = backup(path)
    for o, raw in items:
        data[o:o + 4] = raw
    with open(path, "wb") as f:
        f.write(data)
    return backup_path


def list_backups(path):
    """Backups of this file, newest first."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    rx = re.compile(r"^" + re.escape(os.path.basename(path)) + r"\.\d{8}_\d{6}(_\d+)?\.bak$")
    names = [n for n in os.listdir(BACKUP_DIR) if rx.match(n)]
    return [os.path.join(BACKUP_DIR, n) for n in sorted(names, reverse=True)]


def restore_backup(path, bak):
    """Put a backup back in place (the current file is backed up first)."""
    backup(path)
    shutil.copy2(bak, path)


# --- hash functions the games commonly use to turn a name into a 32-bit key
_M32 = 0xFFFFFFFF


def _h_crc32(b):
    return zlib.crc32(b) & _M32


_CRC32C_TABLE = []


def _h_crc32c(b):
    if not _CRC32C_TABLE:
        for i in range(256):
            c = i
            for _ in range(8):
                c = ((c >> 1) ^ 0x82F63B78) if c & 1 else (c >> 1)
            _CRC32C_TABLE.append(c)
    c = _M32
    for x in b:
        c = _CRC32C_TABLE[(c ^ x) & 255] ^ (c >> 8)
    return c ^ _M32


def _h_fnv1a(b):
    h = 0x811C9DC5
    for x in b:
        h = ((h ^ x) * 0x01000193) & _M32
    return h


def _h_fnv1(b):
    h = 0x811C9DC5
    for x in b:
        h = ((h * 0x01000193) & _M32) ^ x
    return h


def _h_djb2(b):
    h = 5381
    for x in b:
        h = (h * 33 + x) & _M32
    return h


def _h_sdbm(b):
    h = 0
    for x in b:
        h = (x + (h << 6) + (h << 16) - h) & _M32
    return h


def _h_jenkins(b):
    h = 0
    for x in b:
        h = (h + x) & _M32
        h = (h + (h << 10)) & _M32
        h ^= h >> 6
    h = (h + (h << 3)) & _M32
    h ^= h >> 11
    return (h + (h << 15)) & _M32


def _h_murmur3(b, seed=0):
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h, n = seed, len(b)
    for i in range(0, n - n % 4, 4):
        k = struct.unpack_from("<I", b, i)[0]
        k = (k * c1) & _M32
        k = ((k << 15) | (k >> 17)) & _M32
        k = (k * c2) & _M32
        h ^= k
        h = ((h << 13) | (h >> 19)) & _M32
        h = (h * 5 + 0xE6546B64) & _M32
    tail = b[n - n % 4:]
    if tail:
        k = 0
        for j in range(len(tail) - 1, -1, -1):
            k = (k << 8) | tail[j]
        k = (k * c1) & _M32
        k = ((k << 15) | (k >> 17)) & _M32
        h ^= (k * c2) & _M32
    h ^= n
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & _M32
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & _M32
    return h ^ (h >> 16)


HASH_ALGOS = {}  # label -> function(name: str) -> 32-bit key
for _n, _f in (("CRC32", _h_crc32), ("CRC32C", _h_crc32c), ("FNV-1a", _h_fnv1a), ("FNV-1", _h_fnv1),
               ("Murmur3", _h_murmur3), ("DJB2", _h_djb2), ("SDBM", _h_sdbm), ("Jenkins", _h_jenkins)):
    HASH_ALGOS[_n] = lambda s, f=_f: f(s.encode("utf-8"))
    HASH_ALGOS[_n + " (lowercase)"] = lambda s, f=_f: f(s.lower().encode("utf-8"))


def expand_names(lines):
    """Clean a list of names. Accepts the 'Save list' file of the NUP search (name<TAB>count<TAB>path),
    skips '[file name]' rows, comments and blanks, and expands ranges: 'PouchItem_{0-19}'."""
    out = []
    for ln in lines:
        ln = ln.split("\t")[0].strip()
        if not ln or ln.startswith("#") or ln.startswith("[file name]"):
            continue
        m = re.search(r"\{(\d+)-(\d+)\}", ln)
        if m:
            a, b = int(m.group(1)), min(int(m.group(2)), int(m.group(1)) + 100000)
            out += [ln[:m.start()] + str(i) + ln[m.end():] for i in range(a, b + 1)]
        else:
            out.append(ln)
    return list(dict.fromkeys(out))


def read_names_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return expand_names(f.read().splitlines())


def detect_hash(names, keys):
    """For every known hash type: how many of the names produce a key that really exists in the file."""
    keys = set(keys)
    return {a: sum(1 for n in names if f(n) in keys) for a, f in HASH_ALGOS.items()}


def resolve_names(names, algo, keys):
    """{ 'A77921D7': 'Rupees', ... } for the names whose hash is present among keys."""
    f, keys, out = HASH_ALGOS[algo], set(keys), {}
    for n in names:
        h = f(n)
        if h in keys:
            out.setdefault(f"{h:08X}", n)
    return out


def load_names_db():
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_names_db(db):
    # Atomic write: first a temporary file, then a rename. If the program crashes or the PC
    # turns off in the middle, hash_names.json stays intact (never half written).
    tmp = NAMES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, NAMES_FILE)


# ------------------------------------------------------------ share / merge the JSON data files
# names  = hash_names.json   : {title: {"algo": "...", "map": {KEYHEX: name}}}
# fields = saved_fields.json : {title: {save file name: {label: {"kind": ..., "hash": ...}}}}
JSON_KINDS = {
    "names": {"label": "Hash names", "file": NAMES_FILE, "depth": 3,
              "help": "The names found for the keys of each game (the ones 'Find names' and 'Add by name' save). "
                      "Built on your own computer from your own game dumps: it is not downloaded from GitHub."},
    "fields": {"label": "Saved fields", "file": FIELDS_FILE, "depth": 3, "remote": "saved_fields.json",
               "help": "The entries of the main list (label, type, key) that you created for each save file."},
}


GITHUB_RAW = "https://raw.githubusercontent.com/anormalhorse/eden-save-editor/main/"
GUIDE_URL = "https://github.com/anormalhorse/eden-save-editor#quick-start"  # README, Quick start section


def fetch_remote_json(name, timeout=15, max_bytes=10 * 1024 * 1024):
    """Download one of the project's JSON files from GitHub (main branch) and parse it.
    Raises RuntimeError with a readable message when anything goes wrong. Nothing is written to disk."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(GITHUB_RAW + name, headers={"User-Agent": f"EdenSaveEditor/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub answered {e.code} for {name}"
                           + (" (file not found in the repository)." if e.code == 404 else "."))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Could not reach GitHub ({getattr(e, 'reason', e)}). Check your internet connection.")
    if len(raw) > max_bytes:
        raise RuntimeError(f"{name} is unexpectedly large: download cancelled.")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise RuntimeError(f"The downloaded {name} is not a valid JSON file.")


# ---------------------------------------------------------------------------------------------
#  Program updates from GitHub releases: check, download, replace the program, restart
# ---------------------------------------------------------------------------------------------
GITHUB_OWNER = "anormalhorse"
GITHUB_REPO = "eden-save-editor"
RELEASE_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
SCRIPT_NAME = "eden_save_editor.py"
UPDATE_DIR = os.path.join(DATA_DIR, "update")                 # temporary download folder
UPDATE_BACKUP_DIR = os.path.join(DATA_DIR, "program_backups")  # the previous program, kept after an update
MAX_UPDATE_BYTES = 200 * 1024 * 1024


class UpdateUnavailable(Exception):
    """The program cannot replace itself here: the window falls back to the release page."""


class UpdateCancelled(Exception):
    pass


def version_tuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v or ""))


def _http_get(url, accept=None, timeout=30):
    import urllib.request
    headers = {"User-Agent": f"EdenSaveEditor/{APP_VERSION}"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def fetch_latest_release(timeout=15):
    """Latest published release of the project: {'tag', 'notes', 'page', 'zipball', 'assets': [...]}.
    Raises RuntimeError with a readable message when anything goes wrong."""
    import urllib.error
    try:
        with _http_get(RELEASE_API, "application/vnd.github+json", timeout) as r:
            data = json.loads(r.read(5 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("No release has been published on GitHub yet.")
        if e.code in (403, 429):
            raise RuntimeError("GitHub is limiting the requests from this connection. Try again later.")
        raise RuntimeError(f"GitHub answered {e.code}.")
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Could not reach GitHub ({getattr(e, 'reason', e)}). Check your internet connection.")
    except ValueError:
        raise RuntimeError("GitHub sent an answer that could not be read.")
    assets = []
    for a in data.get("assets") or []:
        digest = a.get("digest") or ""
        assets.append({"name": a.get("name") or "", "url": a.get("browser_download_url") or "",
                       "size": int(a.get("size") or 0),
                       "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""})
    return {"tag": data.get("tag_name") or "", "notes": data.get("body") or "",
            "page": data.get("html_url") or RELEASES_URL, "zipball": data.get("zipball_url") or "",
            "assets": [a for a in assets if a["name"] and a["url"]]}


def find_update():
    """The release info if a newer version than APP_VERSION exists on GitHub, else None."""
    info = fetch_latest_release()
    return info if version_tuple(info["tag"]) > version_tuple(APP_VERSION) else None


def _mac_bundle():
    """The .app that contains the running program (macOS), or None."""
    p = os.path.realpath(sys.executable)
    while p and p != os.path.dirname(p):
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return None


def update_target():
    """('source', path of this .py) or ('frozen', path of the program to replace: the .exe, the Linux
    binary / AppImage, or the macOS .app). Raises UpdateUnavailable when it cannot be replaced from here."""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            dest = _mac_bundle()
            if not dest:
                raise UpdateUnavailable("the program is not inside a .app")
        elif os.name == "nt":
            dest = os.path.abspath(sys.executable)
        else:
            dest = os.path.abspath(os.environ.get("APPIMAGE") or sys.executable)
        if not os.access(os.path.dirname(dest), os.W_OK):
            raise UpdateUnavailable(f"no write permission in {os.path.dirname(dest)}")
        return "frozen", dest
    script = os.path.abspath(__file__)
    folder = os.path.dirname(script)
    if os.path.isdir(os.path.join(folder, ".git")):
        raise UpdateUnavailable("this copy of the program is a git repository: update it with git pull")
    if not (os.access(folder, os.W_OK) and os.access(script, os.W_OK)):
        raise UpdateUnavailable(f"no write permission in {folder}")
    return "source", script


def _host_os():
    return "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"


def pick_update_asset(info, kind):
    """The release file to download for this kind of installation (and, when packaged, this system)."""
    assets = info.get("assets", [])
    if kind == "frozen":
        import platform
        host = _host_os()
        os_tokens = {"windows": {"win", "windows", "win32", "win64"},
                     "macos": {"mac", "macos", "osx", "darwin", "macosx"}, "linux": {"linux"}}
        ok_ext = {"windows": (".exe", ".zip"), "macos": (".zip",),
                  "linux": (".tar.gz", "_tar.gz", ".tgz", ".appimage", "")}
        mach = platform.machine().lower()
        is_arm = mach in ("arm64", "aarch64") or mach.startswith("arm")
        best, score = None, -1
        for a in assets:
            n = a["name"].lower()
            if re.search(r"setup|install|source", n) or n.endswith((".sha256", ".sig", ".txt", ".md", ".json",
                                                                    ".dmg", ".msi", ".deb", ".rpm", ".py")):
                continue
            tokens = set(re.split(r"[^a-z0-9]+", n))
            oses = {k for k, v in os_tokens.items() if tokens & v}
            if oses and host not in oses:
                continue
            ext = next((e for e in ok_ext[host] if e and n.endswith(e)), None)
            if ext is None and not ("" in ok_ext[host] and "." not in n.rsplit("-", 1)[-1]):
                continue
            a_arm, a_x64 = bool(tokens & {"arm64", "aarch64"}), bool(tokens & {"x64", "x86_64", "amd64"})
            if (a_arm and not is_arm) or (a_x64 and is_arm and host != "windows"):
                continue  # built for another CPU
            s = (10 if oses else 0) + (3 if (a_arm or a_x64) else 0) + (2 if ext in (".exe", ".tar.gz", "_tar.gz") else 1)
            if s > score:
                best, score = a, s
        if best:
            return best
        raise UpdateUnavailable("the release has no file for this system")
    exact = [a for a in assets if a["name"].lower() == SCRIPT_NAME]
    loose = sorted((a for a in assets if re.fullmatch(r"eden_save_editor.*\.py", a["name"].lower())),
                   key=lambda a: a["name"])
    if exact:
        return exact[0]
    if loose:
        return loose[-1]
    if info.get("zipball"):  # GitHub makes a zip of the sources for every release
        return {"name": "source code (zip)", "url": info["zipball"], "size": 0, "sha256": ""}
    raise UpdateUnavailable("the release has no program file")


def download_file(url, dest, size=0, sha256="", progress=None, cancel=None):
    """Download to dest (through a .part file), checking size and sha256 when known.
    progress(done, total) is called a few times per second; cancel is a threading.Event."""
    tmp = dest + ".part"
    try:
        h = hashlib.sha256()
        done, last = 0, 0.0
        with _http_get(url, timeout=60) as r, open(tmp, "wb") as out:
            total = size or int(r.headers.get("Content-Length") or 0)
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateCancelled()
                chunk = r.read(128 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > MAX_UPDATE_BYTES:
                    raise OSError("the file is unexpectedly large")
                now = time.time()
                if progress and now - last >= 0.2:
                    last = now
                    progress(done, total)
            out.flush()
            try:
                os.fsync(out.fileno())
            except OSError:
                pass
        if size and done != size:
            raise OSError(f"incomplete download ({done} of {size} bytes)")
        if sha256 and h.hexdigest() != sha256:
            raise OSError("the checksum does not match the one published by GitHub")
        if progress:
            progress(done, done)
        os.replace(tmp, dest)
    except UpdateCancelled:
        raise
    except Exception as e:
        raise RuntimeError(f"Download failed: {e}")
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _read_zip_member(zpath, choose):
    """Bytes of the member of the zip picked by choose(list of ZipInfo) (None if there is none).
    Nothing is extracted to disk, so paths inside the zip can never write outside our folder."""
    import zipfile
    with zipfile.ZipFile(zpath) as z:
        m = choose([i for i in z.infolist() if not i.is_dir()])
        if m is None:
            return None
        if m.file_size > MAX_UPDATE_BYTES:
            raise RuntimeError("a file inside the download is unexpectedly large")
        return z.read(m)


def _choose_script(members):
    found = [m for m in members if m.filename.replace("\\", "/").rsplit("/", 1)[-1] == SCRIPT_NAME]
    return min(found, key=lambda m: m.filename.count("/")) if found else None


def _choose_exe(members):
    exes = [m for m in members if m.filename.lower().endswith(".exe")]
    mine = os.path.basename(sys.executable).lower()
    same = [m for m in exes if m.filename.replace("\\", "/").rsplit("/", 1)[-1].lower() == mine]
    pool = same or exes
    return min(pool, key=lambda m: m.filename.count("/")) if pool else None


def check_new_script(data, tag):
    """Refuse a download that is not a sane, newer Eden-Based Save Editor (nothing is installed yet)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise RuntimeError("The downloaded program is not a text file: update cancelled.")
    try:
        compile(text, SCRIPT_NAME, "exec")
    except SyntaxError as e:
        raise RuntimeError(f"The downloaded program is damaged (line {e.lineno}): update cancelled.")
    if "def gui(" not in text:
        raise RuntimeError("The downloaded file is not Eden-Based Save Editor: update cancelled.")
    m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if m and version_tuple(m.group(1)) < version_tuple(tag):
        raise RuntimeError(f"The release {tag} contains a program that says it is v{m.group(1)}, so it was not "
                           f"installed (the program would offer the same update again and again).\n\n"
                           f"Whoever publishes the release must set APP_VERSION to {tag.lstrip('vV')}.")


def install_script_update(data, script):
    """Replace the running .py with data, keeping the previous version in program_backups."""
    os.makedirs(UPDATE_BACKUP_DIR, exist_ok=True)
    bak = os.path.join(UPDATE_BACKUP_DIR, f"eden_save_editor_v{APP_VERSION}.py")
    shutil.copy2(script, bak)
    tmp = script + ".new"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, script)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return bak


def _launch_windows_swap(new_exe, dest):
    """Packaged Windows program: a small script waits for this program to close, puts the new .exe in
    its place and starts it. The caller must quit right after this returns."""
    import subprocess
    os.makedirs(UPDATE_BACKUP_DIR, exist_ok=True)
    try:
        shutil.copy2(dest, os.path.join(UPDATE_BACKUP_DIR, f"{os.path.basename(dest)}.v{APP_VERSION}.bak"))
    except OSError:
        pass
    esc = lambda p: p.replace("%", "%%")
    bat = ("@echo off\r\nchcp 65001 >nul\r\n"
           f'set "NEW={esc(new_exe)}"\r\nset "DEST={esc(dest)}"\r\n'
           "set /a TRIES=0\r\n:retry\r\n"
           'move /y "%NEW%" "%DEST%" >nul 2>&1\r\n'
           "if not errorlevel 1 goto done\r\n"
           "set /a TRIES+=1\r\nif %TRIES% geq 60 exit /b 1\r\n"
           "ping -n 2 127.0.0.1 >nul\r\ngoto retry\r\n:done\r\n"
           'start "" "%DEST%"\r\n(goto) 2>nul & del "%~f0"\r\n')
    script = os.path.join(UPDATE_DIR, "apply_update.bat")
    with open(script, "w", encoding="utf-8", newline="") as f:
        f.write(bat)
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    subprocess.Popen(["cmd.exe", "/c", script], env=env, close_fds=True, creationflags=0x08000000)  # no window


def _extract_tar_member(tpath, out, preferred):
    """Write to out the file of the tar(.gz) named like `preferred` (or its only file). Streamed to disk,
    never extracted by its own path."""
    import tarfile
    with tarfile.open(tpath, "r:*") as t:
        files = [m for m in t.getmembers() if m.isfile()]
        pick = [m for m in files if os.path.basename(m.name) == preferred] or (files if len(files) == 1 else [])
        if not pick:
            raise RuntimeError("The downloaded archive does not contain the program.")
        if pick[0].size > MAX_UPDATE_BYTES:
            raise RuntimeError("a file inside the download is unexpectedly large")
        with t.extractfile(pick[0]) as src, open(out, "wb") as f:
            shutil.copyfileobj(src, f)


def _backup_name(path):
    return os.path.join(UPDATE_BACKUP_DIR, f"{os.path.basename(path)}.v{APP_VERSION}.bak")


def _install_linux(dl, asset_name, dest):
    """Linux: the running binary can be replaced by renaming a new file over it."""
    new = dest + ".new"
    try:
        if asset_name.lower().endswith((".tar.gz", "_tar.gz", ".tgz")):
            _extract_tar_member(dl, new, os.path.basename(dest))
        else:
            shutil.copyfile(dl, new)
        with open(new, "rb") as f:
            head = f.read(4)
        if head != b"\x7fELF" or os.path.getsize(new) < 1024 * 1024:
            raise RuntimeError("The downloaded file is not the Linux program: update cancelled.")
        os.chmod(new, 0o755)
        os.makedirs(UPDATE_BACKUP_DIR, exist_ok=True)
        bak = _backup_name(dest)
        shutil.copy2(dest, bak)
        os.replace(new, dest)
        return bak
    finally:
        try:
            if os.path.exists(new):
                os.remove(new)
        except OSError:
            pass


def _install_mac(dl, bundle):
    """macOS: unzip with ditto (keeps permissions and the signature), swap the whole .app."""
    import subprocess
    stage = os.path.join(UPDATE_DIR, "mac")
    os.makedirs(stage, exist_ok=True)
    r = subprocess.run(["ditto", "-x", "-k", dl, stage], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Could not unpack the download: " + (r.stderr or "").strip()[:200])
    apps = [e for e in os.listdir(stage) if e.endswith(".app")]
    if len(apps) != 1:
        raise RuntimeError("The release zip does not contain exactly one .app: update cancelled.")
    new = os.path.join(stage, apps[0])
    macos_dir = os.path.join(new, "Contents", "MacOS")
    if not (os.path.isdir(macos_dir) and os.listdir(macos_dir)):
        raise RuntimeError("The downloaded .app is incomplete: update cancelled.")
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", new], capture_output=True)
    os.makedirs(UPDATE_BACKUP_DIR, exist_ok=True)
    bak = os.path.join(UPDATE_BACKUP_DIR, f"{os.path.basename(bundle)}.v{APP_VERSION}")
    shutil.rmtree(bak, ignore_errors=True)
    shutil.move(bundle, bak)
    try:
        shutil.move(new, bundle)
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        shutil.move(bak, bundle)  # put the old program back
        raise
    return bak



def perform_update(info, progress, cancel, status=None):
    """Download the release, check it and install it. progress(done, total), status(text).
    Returns {'kind': 'source'|'frozen', 'backup': path}. Raises UpdateUnavailable (automatic update
    not possible here), UpdateCancelled or RuntimeError (readable message)."""
    kind, target = update_target()
    asset = pick_update_asset(info, kind)
    shutil.rmtree(UPDATE_DIR, ignore_errors=True)
    os.makedirs(UPDATE_DIR, exist_ok=True)
    try:
        dl = os.path.join(UPDATE_DIR, "download.bin")
        download_file(asset["url"], dl, asset.get("size", 0), asset.get("sha256", ""), progress, cancel)
        if status:
            status("Checking the downloaded file...")
        if kind == "source":
            if asset["name"].lower().endswith(".py"):
                with open(dl, "rb") as f:
                    data = f.read()
            else:
                data = _read_zip_member(dl, _choose_script)
            if not data:
                raise RuntimeError(f"The release does not contain {SCRIPT_NAME}.")
            check_new_script(data, info["tag"])
            if status:
                status("Installing...")
            return {"kind": "source", "backup": install_script_update(data, target)}
        if status:
            status("Installing...")
        if sys.platform == "darwin":
            return {"kind": "native", "backup": _install_mac(dl, target)}
        if os.name != "nt":
            return {"kind": "native", "backup": _install_linux(dl, asset["name"], target)}
        new_exe = os.path.join(UPDATE_DIR, "new_" + os.path.basename(target))
        if asset["name"].lower().endswith(".zip"):
            data = _read_zip_member(dl, _choose_exe)
            if not data:
                raise RuntimeError("The release zip does not contain an .exe file.")
            with open(new_exe, "wb") as f:
                f.write(data)
        else:
            os.replace(dl, new_exe)
        if os.path.getsize(new_exe) < 1024 * 1024:
            raise RuntimeError("The downloaded .exe is too small to be the program: update cancelled.")
        _launch_windows_swap(new_exe, target)
        return {"kind": "frozen", "backup": ""}
    except BaseException:
        shutil.rmtree(UPDATE_DIR, ignore_errors=True)
        raise
    finally:
        if not (kind == "frozen" and os.name == "nt"):
            shutil.rmtree(UPDATE_DIR, ignore_errors=True)  # the Windows swap script still needs the staged .exe


def restart_program():
    """Start a new copy of the program (the caller then closes the current window)."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-n", _mac_bundle()], env=env)
        else:
            subprocess.Popen([update_target()[1]], env=env, close_fds=True)
    else:
        subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:], close_fds=True)


def json_flatten(obj, leaf_depth, _path=()):
    """Yield (path, value) for the leaves of nested dicts (stops at leaf_depth or at a non-dict)."""
    if isinstance(obj, dict) and len(_path) < leaf_depth:
        for k, v in obj.items():
            yield from json_flatten(v, leaf_depth, _path + (str(k),))
    else:
        yield _path, obj


def json_set_path(root, path, value):
    d = root
    for k in path[:-1]:
        nxt = d.get(k)
        if not isinstance(nxt, dict):
            nxt = d[k] = {}
        d = nxt
    d[path[-1]] = json.loads(json.dumps(value))  # independent copy


def json_validate(kind, obj):
    """None if obj looks like a file of this kind, otherwise a short explanation."""
    if not isinstance(obj, dict):
        return "The file must contain a JSON object ({ ... })."
    if kind == "names":
        for t, v in obj.items():
            if not isinstance(v, dict):
                return f"Game '{t}' is not an object."
            m = v.get("map", {})
            if not isinstance(m, dict) or not all(isinstance(k, str) and isinstance(n, str) for k, n in m.items()):
                return f"The 'map' of game '{t}' must map key -> name (text)."
    else:
        for t, files in obj.items():
            if not isinstance(files, dict):
                return f"Game '{t}' is not an object."
            for fn, entries in files.items():
                if not isinstance(entries, dict) or not all(isinstance(c, dict) for c in entries.values()):
                    return f"'{t} / {fn}' must contain entries like {{\"kind\": ..., \"hash\": ...}}."
    return None


def json_diff(kind, mine, theirs):
    """Compare two files of the same kind. Returns (rows, identical_count); every row is
    (state, path, your_value, their_value) with state 'new' (missing in yours) or 'diff'
    (the same place holds another value). Nothing of yours is ever listed as removed."""
    depth = JSON_KINDS[kind]["depth"]
    a = dict(json_flatten(mine, depth))
    rows, same = [], 0
    for path, v in json_flatten(theirs, depth):
        if not path:
            continue
        if path not in a:
            rows.append(("new", path, None, v))
        elif a[path] != v:
            rows.append(("diff", path, a[path], v))
        else:
            same += 1
    return rows, same


def json_apply(mine, rows):
    """Write the chosen rows into 'mine' (in place). Entries not in rows are left untouched."""
    for _state, path, _old, new in rows:
        json_set_path(mine, path, new)


def json_where(kind, path):
    return " / ".join(x for x in path if not (kind == "names" and x == "map"))


def json_short(v, n=70):
    t = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return t if len(t) <= n else t[:n - 1] + "…"


def names_for(db, title):
    """Return key-hex -> name map. Built-in OoT Remake names are always included
    and can be overridden by anything the user saved in hash_names.json."""
    out = {}
    if title == OOT_REMAKE_ID:
        out.update(OOT_KNOWN_NAMES)
    out.update(db.get(title, {}).get("map", {}))
    return out


def guess_title(path, idx):
    """Game ID of a save. If the file was copied outside its title folder (no 16-hex folder in the
    path), recognise Ocarina of Time Remake by its well-known keys instead of giving up."""
    t = title_id(path)
    if t != "generic" or not idx:
        return t
    hits = sum(1 for k in OOT_KNOWN_NAMES if int(k, 16) in idx)
    return OOT_REMAKE_ID if hits >= 3 else t


# ------------------------------------------------------------ data blocks (variable-size area after the hash table)
# Many games store variable-length data after the key→value table. Pointers in the
# table point into this area. When a block grows/shrinks, every later pointer must
# be adjusted by the same delta or the save is corrupted.
#
# Specialised knowledge for Ocarina of Time Remake (title 0400486024DEC000) is
# applied only when that ID is detected.

OOT_REMAKE_ID = "0400486024DEC000"

# Known blocks for OoT Remake: key -> (label, kind)
# kind: "u64_list" = u32 size (bytes of payload) + payload of u64 values
OOT_KNOWN_BLOCKS = {
    0x70D7C5CB: ("Seen / unlocked IDs (list of u64 hashes)", "u64_list"),
}

# Names discovered for OoT Remake (hash = Murmur3 of the internal name).
# Source: community reverse-engineering (e.g. PlayerStatus.CurrentRupee, IsGet.*, …).
OOT_KNOWN_NAMES = {
    "A77921D7": "Rupees (PlayerStatus.CurrentRupee)",
    "31AB5580": "Current health (PlayerStatus.Life)",
    "FBE01DA1": "Max health (PlayerStatus.MaxLife)",
    "12D63A97": "Max magic (PlayerStatus.MPMax)",
    "04735978": "Gold Skulltula tokens (PouchData.Item_Token_Gold.StockNum)",
    "405D8A0F": "Kokiri Sword owned (IsGet.Weapon_Sword_Kokiri)",
    "7AE38E5B": "Master Sword owned (IsGet.Weapon_Sword_Master)",
    "33466812": "Biggoron Sword owned (IsGet.Weapon_Sword_Biggoron)",
    "5217DA1E": "Deku Shield owned (IsGet.Weapon_Shield_Deku)",
    "7D48DF76": "Bottle 1 contents (BottleA.BottleItem)",
    "1EDD164A": "Bottle 2 contents (BottleB.BottleItem)",
    "B80C70A8": "Bottle 3 contents (BottleC.BottleItem)",
    "E4C1B1FC": "Bottle 4 contents (BottleD.BottleItem)",
    "365E1953": "Bottle 5 contents (BottleE.BottleItem)",
    "3506A557": "Amiibo pouch owned (IsGet.Pouch_Amiibo)",
    "7FE2A5B2": "Lens of Truth owned (IsGet.Armor_GlassTruth)",
    "1106B082": "Bottle owned (IsGet.Item_Bottle)",
    "170B5DF2": "Bottle with letter owned (IsGet.Item_Bottle_Letter)",
    "0E6264C7": "Gerudo token owned (IsGet.Item_Token_Gerudo)",
    "A63F1E2D": "Small bomb bag owned (IsGet.Item_BombBag_Small)",
    "0EC22EE1": "Slingshot owned (IsGet.Weapon_Range_SlingShot)",
    "2B39DD4F": "Song of Storms owned (IsGet.Item_Melody_StormSong)",
    "FF98ED58": "Hookshot owned (IsGet.Item_Hookshot)",
    "38AEA3BB": "Longshot owned (IsGet.Item_Longshot)",
    "FFABF058": "Farore's Wind owned (IsGet.Item_FaroreWind)",
    "165DD1AD": "Sun's Song owned (IsGet.Item_Melody_SunSong)",
}

# Useful defaults to put in the main editable list for OoT Remake
OOT_DEFAULT_FIELDS = {
    "Rupees": {"kind": "int32", "hash": 0xA77921D7},
    "Current health": {"kind": "int32", "hash": 0x31AB5580},
    "Max health": {"kind": "int32", "hash": 0xFBE01DA1},
    "Max magic": {"kind": "int32", "hash": 0x12D63A97},
    "Gold Skulltula tokens": {"kind": "int32", "hash": 0x04735978},
    "Kokiri Sword owned": {"kind": "int32", "hash": 0x405D8A0F},
    "Master Sword owned": {"kind": "int32", "hash": 0x7AE38E5B},
    "Biggoron Sword owned": {"kind": "int32", "hash": 0x33466812},
    "Deku Shield owned": {"kind": "int32", "hash": 0x5217DA1E},
}


def table_end_of(data):
    if len(data) < 0x30:
        return 0
    return min(struct.unpack_from("<I", data, 8)[0], len(data))


def data_pointers(data):
    """Return list of (key, table_offset, pointer) for every table entry whose
    value looks like an offset into the data area ( >= table_end )."""
    te = table_end_of(data)
    out = []
    if te < 0x30:
        return out
    for o in range(0x28, te - 7, 8):
        k, v = struct.unpack_from("<II", data, o)
        if te <= v < len(data):
            out.append((k, o, v))
    return out


def infer_blocks(data):
    """Heuristic list of blocks in the data area.
    Returns list of dicts: start, end, size, key (or None), kind, label.
    'kind' is 'u64_list', 'raw', or 'unknown'."""
    te = table_end_of(data)
    if te >= len(data):
        return []
    ptrs = data_pointers(data)
    starts = sorted({p[2] for p in ptrs})
    # also include table_end itself as a possible start
    if te not in starts:
        starts = [te] + starts
    starts = sorted(set(starts))
    blocks = []
    for i, start in enumerate(starts):
        # end = next start or end of file
        end = starts[i + 1] if i + 1 < len(starts) else len(data)
        # which keys point here?
        keys_here = [k for k, _, v in ptrs if v == start]
        key = keys_here[0] if keys_here else None
        size = end - start
        kind, label = "raw", ""
        # try size-prefixed u64 list
        if size >= 4:
            payload_len = struct.unpack_from("<I", data, start)[0]
            if (payload_len % 8 == 0 and 0 < payload_len <= size - 4
                    and start + 4 + payload_len <= len(data)):
                # good candidate; prefer the declared size as block end if it fits
                # (some games put padding after)
                if payload_len + 4 <= size:
                    kind = "u64_list"
                    label = f"u64 list ({payload_len // 8} entries)"
        if key is not None and key in OOT_KNOWN_BLOCKS:
            label, kind = OOT_KNOWN_BLOCKS[key]
        blocks.append({
            "start": start,
            "end": end,
            "size": size,
            "key": key,
            "keys": keys_here,
            "kind": kind,
            "label": label or (f"key {key:08X}" if key is not None else "unkeyed"),
        })
    return blocks


def read_u64_list(data, start):
    """Return (payload_len_bytes, list_of_u64) or (None, []) if not valid."""
    if start + 4 > len(data):
        return None, []
    payload_len = struct.unpack_from("<I", data, start)[0]
    if payload_len % 8 != 0 or start + 4 + payload_len > len(data):
        return None, []
    n = payload_len // 8
    vals = list(struct.unpack_from(f"<{n}Q", data, start + 4))
    return payload_len, vals


def write_u64_list_payload(entries):
    """Build the bytes that go after the size field (just the u64s)."""
    return b"".join(struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF) for v in entries)


def apply_block_resize(data, block_start, old_total_size, new_block_bytes):
    """Replace the block [block_start : block_start+old_total_size] with
    new_block_bytes. Adjust every table value that is a pointer >= old end
    by the delta. Returns the new data (bytearray) and the delta."""
    old_end = block_start + old_total_size
    new_len = len(new_block_bytes)
    delta = new_len - old_total_size
    # build new file content
    new_data = bytearray(data[:block_start] + new_block_bytes + data[old_end:])
    if delta == 0:
        return new_data, 0
    # update all pointer-like values in the hash table
    te = table_end_of(new_data)  # table itself did not move
    for o in range(0x28, te - 7, 8):
        v = struct.unpack_from("<I", new_data, o + 4)[0]
        if v >= old_end:
            struct.pack_into("<I", new_data, o + 4, (v + delta) & 0xFFFFFFFF)
    return new_data, delta


def set_u64_list(path, data, block_start, new_entries):
    """Rewrite a size-prefixed u64 list. Creates a backup. Returns (new_data, backup_path)."""
    old_payload_len, _ = read_u64_list(data, block_start)
    if old_payload_len is None:
        raise ValueError("Not a valid u64 list at this offset")
    old_total = 4 + old_payload_len
    payload = write_u64_list_payload(new_entries)
    new_block = struct.pack("<I", len(payload)) + payload
    new_data, delta = apply_block_resize(data, block_start, old_total, new_block)
    backup_path = backup(path)
    with open(path, "wb") as f:
        f.write(new_data)
    return new_data, backup_path


# ------------------------------------------------------------ NUP archives
# Reader for the NUP container, a clean-room format for unencrypted Switch 1/2
# game dumps (spec and reference tool: https://github.com/a55uka/nup, MIT licence).
# Files are read straight from the archive: nothing is extracted unless you ask.
_NUP_SUPER = struct.Struct("<4sHHQQ32sQQII16s32s")
_NUP_TOC = struct.Struct("<4sIQQQQQ")
_NUP_PKG = struct.Struct("<QIIIIBBHIQI20x")
_NUP_DIR = struct.Struct("<IIIIQ")
_NUP_FILE = struct.Struct("<IIQQQ32sBBH12x")
NUP_KINDS = {0: "base", 1: "update", 2: "addon", 3: "addon update", 4: "system"}
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
NAME_CHARS = rb"[A-Za-z0-9_\-]*"


def zstd_decompress(data, limit):
    """Decompress a zstd frame; returns at most limit+1 bytes (so callers can detect 'too big')."""
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data))
        out = bytearray()
        while len(out) <= limit:
            chunk = reader.read(min(1 << 20, limit + 1 - len(out)))
            if not chunk:
                break
            out += chunk
        return bytes(out)
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        raise RuntimeError("Reading compressed data needs the 'zstandard' package.\n"
                           "Install it with:   pip install zstandard")
    return zstd.ZstdDecompressor().decompress(data, limit + 1)


class _MMReader:
    """File-like view of one part of a memory-mapped file (reads copy only what is asked)."""

    def __init__(self, mm, off, size):
        self.mm, self.off, self.size, self.pos = mm, off, size, 0

    def read(self, n=-1):
        if n is None or n < 0 or n > self.size - self.pos:
            n = self.size - self.pos
        b = bytes(self.mm[self.off + self.pos:self.off + self.pos + n])
        self.pos += n
        return b


def open_zstd_stream(stream):
    """Decompress a zstd stream while it is read (RAM stays small, whatever the size)."""
    try:
        import zstandard
    except ImportError:
        zstandard = None
    if zstandard is not None:
        try:
            return zstandard.ZstdDecompressor().stream_reader(stream, read_across_frames=True)
        except TypeError:  # older zstandard versions
            return zstandard.ZstdDecompressor().stream_reader(stream)
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        raise RuntimeError("Reading compressed data needs the 'zstandard' package.\n"
                           "Install it with:   pip install zstandard")
    return zstd.ZstdFile(stream)


PIECE_OVERLAP = 1024  # bytes repeated at the start of the next piece, so no word is cut in half


def iter_pieces(stream, piece, overlap=PIECE_OVERLAP):
    """Read a stream in pieces of about 'piece' bytes. Yields (data, ov): data is a bytearray whose first
    'ov' bytes are the end of the previous piece (matches that lie fully inside them were already seen)."""
    tail = b""
    while True:
        buf = bytearray(tail)
        want = piece
        while want > 0:
            c = stream.read(min(1 << 20, want))
            if not c:
                break
            buf += c
            want -= len(c)
        if len(buf) <= len(tail):
            break
        yield buf, len(tail)
        tail = bytes(buf[-overlap:])
        if want > 0:  # end of the stream reached
            break


class NupArchive:
    def __init__(self, path):
        self.path = path
        self._fh = open(path, "rb")
        self._mm = None
        try:
            try:
                self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
                (magic, _hs, _hv, toc_off, toc_size, toc_sha, _blob, _fs, npk, _fl, _uuid,
                 _r) = _NUP_SUPER.unpack_from(self._mm, 0)
            except (ValueError, struct.error):
                raise ValueError("This file is not a NUP archive.")
            if magic[:3] != b"NUP":
                raise ValueError("This file is not a NUP archive.")
            if magic[3] != 1:
                raise ValueError(f"Unsupported NUP version {magic[3]}.")
            if toc_off + toc_size > len(self._mm):
                raise ValueError("The archive is truncated.")
            toc = bytes(self._mm[toc_off:toc_off + toc_size])
            if hashlib.sha256(toc).digest() != toc_sha:
                raise ValueError("The file table is damaged (checksum mismatch).")
            tmagic, npkg, ndir, nfile, pool_off, pool_size, _x = _NUP_TOC.unpack_from(toc, 0)
            if tmagic[:3] != b"TOC":
                raise ValueError("Bad file table.")
            pool = toc[pool_off:pool_off + pool_size]
            name = lambda o, n: pool[o:o + n].decode("utf-8", "replace")
            pos = _NUP_TOC.size
            self.packages = []
            for i in range(npkg):
                (tid, root, _ex, _ro, ver, kind, _pf, _a, _b, _mo, _ms) = _NUP_PKG.unpack_from(toc, pos)
                pos += _NUP_PKG.size
                k = NUP_KINDS.get(kind, f"kind {kind}")
                self.packages.append({"index": i, "root": root, "label":
                                      f"{k} v{ver >> 16}.{(ver >> 8) & 255}.{ver & 255}  (title {tid:016X})"})
            self.dirs = []
            for _ in range(ndir):
                o, n, first, count, _z = _NUP_DIR.unpack_from(toc, pos)
                pos += _NUP_DIR.size
                self.dirs.append((name(o, n), first, count))
            self.files = []  # (name, size, extent_offset, extent_size, codec)
            for _ in range(nfile):
                o, n, size, eo, es, _sha, codec, _cf, _z = _NUP_FILE.unpack_from(toc, pos)
                pos += _NUP_FILE.size
                self.files.append((name(o, n), size, eo, es, codec))
            self.children_raw = struct.unpack_from(f"<{(pool_off - pos) // 4}I", toc, pos)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        self._fh.close()

    def children(self, dir_index):
        """List of (is_dir, index, name) inside a directory."""
        _, first, count = self.dirs[dir_index]
        out = []
        for slot in range(first, first + count):
            cid = self.children_raw[slot]
            if cid >= len(self.dirs):
                out.append((False, cid - len(self.dirs), self.files[cid - len(self.dirs)][0]))
            else:
                out.append((True, cid, self.dirs[cid][0]))
        return out

    def open_stream(self, fi):
        """The content of a file as a readable stream (nothing is loaded whole)."""
        _n, size, eo, es, codec = self.files[fi]
        if size == 0:
            return io.BytesIO(b"")
        rd = _MMReader(self._mm, eo, es)
        if codec == 0:
            return rd
        if codec == 1:
            return open_zstd_stream(rd)
        raise ValueError(f"Unknown compression type {codec}.")

    def read_file(self, fi):
        _n, size, eo, es, codec = self.files[fi]
        if size == 0:
            return b""
        raw = bytes(self._mm[eo:eo + es])
        if codec == 0:
            data = raw
        elif codec == 1:
            data = zstd_decompress(raw, size)
        else:
            raise ValueError(f"Unknown compression type {codec}.")
        if len(data) != size:
            raise ValueError("File size does not match the archive table.")
        return data


class FolderArchive:
    """A game folder on disk (e.g. the already extracted contents of a NUP), exposed with the
    same interface as NupArchive so browsing, searching and extracting work the same way."""
    is_folder = True

    def __init__(self, path, progress=None, stop=lambda: False):
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            raise ValueError("This is not a folder.")
        self.path = path
        root_name = os.path.basename(path.rstrip("\\/")) or path
        self.packages = [{"index": 0, "root": 0, "label": root_name}]
        dirs, kids, files, self._paths = [(root_name, path)], [[]], [], []
        work = [0]
        while work:
            if stop():
                raise InterruptedError("stopped")
            di = work.pop()
            try:
                with os.scandir(dirs[di][1]) as it:
                    entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append((e.name, e.path))
                        kids.append([])
                        kids[di].append((True, len(dirs) - 1))
                        work.append(len(dirs) - 1)
                    elif e.is_file(follow_symlinks=False):
                        size = e.stat(follow_symlinks=False).st_size
                        files.append((e.name, size, 0, size, 2))  # codec 2 = plain file on disk
                        self._paths.append(e.path)
                        kids[di].append((False, len(files) - 1))
                except OSError:
                    continue
            if progress and len(files) % 200 < len(entries):
                progress(len(files))
        nd = len(dirs)
        self.children_raw, self.dirs = [], []
        for (name, _p), k in zip(dirs, kids):
            self.dirs.append((name, len(self.children_raw), len(k)))
            self.children_raw.extend(i if is_dir else nd + i for is_dir, i in k)
        self.files = files

    def close(self):
        pass

    def children(self, dir_index):
        _, first, count = self.dirs[dir_index]
        out = []
        for slot in range(first, first + count):
            cid = self.children_raw[slot]
            if cid >= len(self.dirs):
                out.append((False, cid - len(self.dirs), self.files[cid - len(self.dirs)][0]))
            else:
                out.append((True, cid, self.dirs[cid][0]))
        return out

    def open_stream(self, fi):
        if self.files[fi][1] == 0:
            return io.BytesIO(b"")
        return open(self._paths[fi], "rb")

    def read_file(self, fi):
        if self.files[fi][1] == 0:
            return b""
        with open(self._paths[fi], "rb") as f:
            return f.read()

    def copy_file(self, fi, target):
        shutil.copyfile(self._paths[fi], target)


def export_file(arch, fi, target):
    """Write one archive/folder file to 'target' (plain copy when it already is a file on disk)."""
    if hasattr(arch, "copy_file"):
        arch.copy_file(fi, target)
    else:
        with open(target, "wb") as f:
            f.write(arch.read_file(fi))


DEFAULT_TEMPLATES = ["{}"]
# Example shapes for Ocarina of Time Remake (button "Example (Ocarina of Time)" in the Find names tab)
OOT_EXAMPLE_SHAPES = "{};PlayerStatus.{};PouchData.{}.StockNum;IsGet.{};BottleA.{};BottleB.{};BottleC.{};BottleD.{};BottleE.{};HavePlayedEvent.{};IsVisitLocation.{};HaveTalked.{};HavePlayedNaviBalloonMessage.{};DungeonInfo.{}.IsGetCompass;DungeonInfo.{}.IsGetBossKey;HaveTalked_Special.{};DungeonInfo.{}"


def learn_templates(names):
    """Name shapes seen in names you already know (any game): 'IsGet.Item_Hookshot' -> 'IsGet.{}',
    'PouchData.Item_Token_Gold.StockNum' -> 'PouchData.{}.StockNum'."""
    out = ["{}"]
    for n in names:
        parts = n.split(".")
        if len(parts) == 2:
            out.append(parts[0] + ".{}")
        elif len(parts) == 3:
            out.append(parts[0] + ".{}." + parts[2])
    return list(dict.fromkeys(out))
_TOKEN_RX = re.compile(rb"[A-Za-z_][A-Za-z0-9_.]{2,100}")


_TOKCH = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.")
_CHUNK = 2 * 1024 * 1024  # a regex pass over a huge file is cut in pieces so it never blocks the window


class _Yielder:
    """Called often by a worker thread doing heavy pure-Python work: every ~30 ms it sleeps a few
    ms. That hands the interpreter lock to the window thread, so the window keeps answering
    (no 'not responding') while the job runs."""

    def __init__(self, every=0.03, nap=0.003):
        self.every, self.nap, self.t = every, nap, time.monotonic()

    def __call__(self):
        if time.monotonic() - self.t >= self.every:
            time.sleep(self.nap)
            self.t = time.monotonic()


# Resource use of the searches (RAM / CPU): size in MB of the pieces a file is read in, words remembered by the name search,
# and how often (every, seconds) the worker sleeps (nap, seconds) so the computer stays responsive.
RESOURCE_PRESETS = {
    "Gentle (low RAM and CPU)": {"mb": 16, "words": 500_000, "every": 0.02, "nap": 0.02},
    "Balanced (default)": {"mb": 64, "words": 4_000_000, "every": 0.03, "nap": 0.003},
    "Fast (more RAM and CPU)": {"mb": 256, "words": 16_000_000, "every": 0.2, "nap": 0.0},
}
DEFAULT_RESOURCES = RESOURCE_PRESETS["Balanced (default)"]


def fmt_duration(sec):
    """12 s / 3 min 05 s / 1 h 02 min."""
    sec = int(max(0, round(sec)))
    if sec < 60:
        return f"{sec} s"
    if sec < 3600:
        return f"{sec // 60} min {sec % 60:02d} s"
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min"


class ProgressEta:
    """Percentage + elapsed + estimated remaining time from a 0..1 fraction (smoothed)."""

    def __init__(self):
        self.t0 = time.monotonic()
        self.eta = None

    def text(self, frac):
        frac = min(max(frac, 0.0), 1.0)
        el = time.monotonic() - self.t0
        out = f"{frac * 100:.1f}%  -  elapsed {fmt_duration(el)}"
        if frac >= 0.01 and el >= 3:
            raw = el * (1 - frac) / frac
            self.eta = raw if self.eta is None else 0.8 * self.eta + 0.2 * raw
            out += f"  -  about {fmt_duration(self.eta)} left"
        else:
            out += "  -  estimating time..."
        return out


def nup_find_names(arch, keys, algo, templates, max_bytes=64 * 1024 * 1024, progress=None,
                   stop=lambda: False, on_found=None, save_every=3.0, on_match=None, on_file=None,
                   res=None, skip_big=False, include=(), exclude=()):
    """Reverse lookup of save keys. A hash cannot be undone, so: collect every identifier-like
    word stored in the game files (also inside compressed files), build candidate names with the
    templates ('IsGet.{}' ...), hash each one with the game's hash type and keep those whose hash
    is a key of the save. Returns (found {KEYHEX: name}, files scanned, candidates tried, stopped).
    progress(n, total_files, names_matched, candidates_tried, fraction) is called a few times per
    second; fraction (0..1) is weighted by file size, so it reflects the real work left.
    on_match(key_hex, name, path) is called at once for every new name matched (path = the game
    file where the word was first seen) and on_file(path) a few times per second with the file
    being scanned: they feed the live log of the 'Find names' tab.
    on_found(found_copy) is called at most every save_every seconds while new names are being
    found (and one last time at the end, also after Stop), so the caller can save them little by
    little and nothing is lost if the program crashes."""
    f = HASH_ALGOS[algo]
    keys = set(keys)
    found, seen = {}, set()
    scanned = tried = 0
    stopped = False
    res = res or DEFAULT_RESOURCES
    max_words = res["words"]  # words remembered at once (this is what uses most RAM)
    y = _Yielder(res["every"], res["nap"])
    files = []
    for item in nup_walk(arch):
        files.append(item)
        if not len(files) & 0x1FFF:
            y()
    weights = []
    include = [x.lower() for x in include if x]
    exclude = [x.lower() for x in exclude if x]

    def path_ok(path):
        # include/exclude are lowercase pieces matched anywhere in the file path (e.g. '/tex/', '.bntx')
        pl = "/" + str(path).replace("\\", "/").lower().lstrip("/")
        if exclude and any(x in pl for x in exclude):
            return False
        return not include or any(x in pl for x in include)

    for _label, _path, fi in files:
        size = arch.files[fi][1]
        weights.append(size if 0 < size and not (skip_big and size > max_bytes) and path_ok(_path) else 0)
        if not len(weights) & 0x1FFF:
            y()
    total = sum(weights) or 1
    done = 0
    last = [0.0]
    flush = [time.monotonic(), 0]  # [time of last save, names already handed to on_found]

    def autosave(force=False):
        if on_found is None or len(found) == flush[1]:
            return
        now = time.monotonic()
        if force or now - flush[0] >= save_every:
            flush[0], flush[1] = now, len(found)
            on_found(dict(found))

    def report(n, frac):
        if not progress and not on_file:
            return
        now = time.monotonic()
        if now - last[0] >= 0.1:
            last[0] = now
            if progress:
                progress(n, len(files), len(found), tried, frac)
            if on_file and n < len(files):
                on_file(files[n][1])

    for n, (_label, path, fi) in enumerate(files):
        if stop():
            stopped = True
            break
        report(n, done / total)
        wt = weights[n]
        if wt == 0:
            continue
        try:
            stream = arch.open_stream(fi)
            head = stream.read(4)
            if hasattr(stream, "close"):
                stream.close()
            stream = arch.open_stream(fi)
            if head == ZSTD_MAGIC:  # compressed inside: unpack while reading
                stream = open_zstd_stream(stream)
        except Exception:
            done += wt
            continue
        scanned += 1
        k = 0
        try:
            for data, _ov in iter_pieces(stream, max_bytes):
                toks = set()
                n_data, pos = len(data), 0
                while pos < n_data:  # piece by piece: one huge regex pass would freeze the window
                    if stop():
                        stopped = True
                        break
                    end = min(pos + _CHUNK, n_data)
                    lim = min(end + 256, n_data)
                    while end < lim and data[end] in _TOKCH:  # never cut a word in half
                        end += 1
                    for m in _TOKEN_RX.findall(data, pos, end):
                        toks.add(m)
                        if b"." in m:
                            toks.update(m.split(b"."))
                        k += 1
                        if not k & 0x3FF:
                            y()
                    pos = end
                    y()
                if stopped:
                    break
                nt = len(toks) or 1
                for i, t in enumerate(toks):
                    if i % 256 == 0:
                        if stop():
                            stopped = True
                            break
                        report(n, (done + wt * i / nt) / total)
                    if t in seen or len(seen) > max_words:
                        continue
                    seen.add(t)
                    y()
                    word = t.decode("ascii", "ignore")
                    for tpl in templates:
                        name = tpl.replace("{}", word)
                        tried += 1
                        h = f(name)
                        if h in keys:
                            kh = f"{h:08X}"
                            if kh not in found:
                                found[kh] = name
                                if on_match:
                                    on_match(kh, name, path)
                            autosave()
                if stopped:
                    break
        except Exception:
            pass  # unreadable or damaged file: keep what was found and go on
        finally:
            if hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass
        if stopped:
            break
        done += wt
        autosave()
    autosave(force=True)
    return found, scanned, tried, stopped


def nup_walk(arch):
    """Yield (package label, path, file index) for every file in the archive."""
    for p in arch.packages:
        stack = [(p["root"], "")]
        while stack:
            di, prefix = stack.pop()
            for is_dir, idx, name in arch.children(di):
                if is_dir:
                    stack.append((idx, prefix + name + "/"))
                else:
                    yield p["label"], prefix + name, idx


def nup_search(arch, o, progress=None, stop=lambda: False, on_find=None, res=None):
    """Search names and/or contents. o = dict(pattern, inside, nested, utf16, icase, whole,
    pathfilter, max_bytes = size of the pieces a file is read in, skip_big = skip files bigger than that). Returns (found, scanned, skipped, stopped); found = name -> [count, where].
    If on_find is set, it is called as on_find(name, count, where) every time a name is added or
    its count increases (so the UI can update live while the search continues)."""
    flags = re.I if o["icase"] else 0
    core = re.escape(o["pattern"].encode("utf-8"))
    rx = re.compile(NAME_CHARS + core + NAME_CHARS if o["whole"] else core, flags)
    rx16 = None
    if o["utf16"]:
        c16 = re.escape(o["pattern"].encode("utf-16-le"))
        ch16 = rb"(?:[A-Za-z0-9_\-]\x00)*"
        rx16 = re.compile(ch16 + c16 + ch16 if o["whole"] else c16, flags)
    low = o["pattern"].lower() if o["icase"] else o["pattern"]
    pf = o["pathfilter"].lower()
    skip_big = bool(o.get("skip_big"))
    found, scanned, skipped = {}, 0, 0

    def add(name, where):
        e = found.get(name)
        if e is None:
            found[name] = [1, where]
            if on_find:
                on_find(name, 1, where)
        else:
            e[0] += 1
            if on_find:
                on_find(name, e[0], e[1])

    def scan(data, where, ov=0):
        for m in rx.finditer(data):
            if ov and m.end() <= ov:
                continue  # fully inside the end of the previous piece: already counted
            add(m.group().decode("latin-1"), where)
        if rx16 is not None:
            for m in rx16.finditer(data):
                if ov and m.end() <= ov:
                    continue
                add(m.group().decode("utf-16-le", "replace"), where)

    res = res or DEFAULT_RESOURCES
    y = _Yielder(res["every"], res["nap"])
    files = []
    for item in nup_walk(arch):
        files.append(item)
        if not len(files) & 0x1FFF:
            y()
    stopped = False
    weights = []
    for _label, _path, fi in files:
        size = arch.files[fi][1]
        weights.append(size if (o["inside"] and 0 < size and not (skip_big and size > o["max_bytes"])) else 1)
        if not len(weights) & 0x1FFF:
            y()
    total = sum(weights) or 1
    done = 0
    last = 0.0
    for n, (_label, path, fi) in enumerate(files):
        if stop():
            stopped = True
            break
        y()
        frac = done / total
        done += weights[n]
        now = time.monotonic()
        if progress and now - last >= 0.1:
            last = now
            progress(n, len(files), len(found), frac)
        fname = path.rsplit("/", 1)[-1]
        if low in (fname.lower() if o["icase"] else fname):
            add("[file name] " + fname, path)
        if not o["inside"] or (pf and pf not in path.lower()):
            continue
        size = arch.files[fi][1]
        if size == 0:
            continue
        if skip_big and size > o["max_bytes"]:
            skipped += 1
            continue
        try:
            stream = arch.open_stream(fi)
            head = stream.read(4)
            if hasattr(stream, "close"):
                stream.close()
            where = path
            stream = arch.open_stream(fi)
            if o["nested"] and head == ZSTD_MAGIC:
                try:
                    stream = open_zstd_stream(stream)
                    where = path + "  (inside the compressed file)"
                except Exception:
                    stream = arch.open_stream(fi)  # cannot unpack: look at the stored bytes
        except Exception:
            skipped += 1
            continue
        scanned += 1
        try:
            first = True
            try:
                for data, ov in iter_pieces(stream, o["max_bytes"]):
                    first = False
                    scan(data, where, ov)
                    y()
                    if stop():
                        stopped = True
                        break
            except Exception:
                if first and where != path:  # unpacking failed at once: scan the stored bytes instead
                    for data, ov in iter_pieces(arch.open_stream(fi), o["max_bytes"]):
                        scan(data, path, ov)
                        y()
                        if stop():
                            stopped = True
                            break
        except Exception:
            skipped += 1
        finally:
            if hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    pass
        if stopped:
            break
    return found, scanned, skipped, stopped


def _safe_name(n):
    n = re.sub(r'[<>:"|?*\\/]', "_", n)
    return n if n not in ("", ".", "..") else "_"


def nup_extract(arch, di, out_dir, progress=None):
    """Extract a directory (and everything below it) into out_dir. Returns files written."""
    n = 0
    stack = [(di, out_dir)]
    while stack:
        d, path = stack.pop()
        os.makedirs(path, exist_ok=True)
        for is_dir, idx, name in arch.children(d):
            target = os.path.join(path, _safe_name(name))
            if is_dir:
                stack.append((idx, target))
            else:
                export_file(arch, idx, target)
                n += 1
                if progress:
                    progress(n, name)
    return n


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def open_nup_browser(root, tk, ttk, filedialog, messagebox, initial_dir, resolver=None):
    w = tk.Toplevel(root)
    w.title("Game archive (NUP) or game folder")
    w.geometry("940x680")
    autofit(w, 940, 680)
    S = {"arch": None, "stop": False, "busy": False, "closing": False, "found": {}}
    msgs = queue.Queue()

    head = ttk.Frame(w)
    head.pack(fill="x", padx=10, pady=8)
    info = ttk.Label(head, wraplength=720, justify="left",
                     text="Open a .nup file or a game folder (the extracted contents of a NUP). "
                          "Files are read in place: nothing is extracted or copied unless you press Extract.")
    info.pack(side="left")
    status = ttk.Label(w, text="")
    pbar = ttk.Progressbar(w, mode="determinate", maximum=1000)  # shown only while a job reports progress

    nb = ttk.Notebook(w)
    nb.pack(fill="both", expand=True, padx=10, pady=4)
    t1, t2, t3 = ttk.Frame(nb), ttk.Frame(nb), ttk.Frame(nb)
    nb.add(t1, text="Browse")
    nb.add(t2, text="Search names / text")
    nb.add(t3, text="Find names for the open save")

    # ---- browse tab
    tv = ttk.Treeview(t1, columns=("size", "codec"), selectmode="extended", height=18)
    tv.heading("#0", text="Name")
    tv.heading("size", text="Size")
    tv.heading("codec", text="Stored as")
    tv.column("#0", width=520)
    tv.column("size", width=110)
    tv.column("codec", width=90)
    sb = ttk.Scrollbar(t1, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tv.pack(fill="both", expand=True, padx=(8, 0), pady=8)
    tv.configure(height=12)

    def populate(iid):
        arch = S["arch"]
        kids = tv.get_children(iid)
        if arch is None or len(kids) != 1 or not kids[0].startswith("x"):
            return
        tv.delete(kids[0])
        di = arch.packages[int(iid[1:])]["root"] if iid[0] == "p" else int(iid[1:])
        for is_dir, idx, name in arch.children(di):
            if is_dir:
                tv.insert(iid, "end", iid=f"d{idx}", text=name, values=("", ""))
                tv.insert(f"d{idx}", "end", iid=f"xd{idx}", text="...")
            else:
                _n, size, _eo, _es, codec = arch.files[idx]
                tv.insert(iid, "end", iid=f"f{idx}", text=name,
                          values=(human_size(size), {0: "raw", 1: "zstd"}.get(codec, "on disk")))

    tv.bind("<<TreeviewOpen>>", lambda e: populate(tv.focus()))

    def open_nup():
        if S["busy"]:
            messagebox.showinfo("Editor", "Wait for the current job to finish (or press Stop).", parent=w)
            return
        p = filedialog.askopenfilename(parent=w, initialdir=initial_dir, title="Choose the .nup file",
                                       filetypes=[("NUP archive", "*.nup"), ("All files", "*.*")])
        if not p:
            return
        try:
            arch = NupArchive(p)
        except Exception as e:
            messagebox.showerror("Editor", str(e), parent=w)
            return
        show_arch(arch, p)

    def show_arch(arch, p):
        if S["arch"]:
            S["arch"].close()
        S["arch"] = arch
        if getattr(arch, "is_folder", False):
            info.config(text=f"{p}\n{len(arch.files)} files in {len(arch.dirs)} folders")
        else:
            info.config(text=f"{p}\n{len(arch.files)} files in {len(arch.packages)} package(s)")
        tv.delete(*tv.get_children())
        for pk in arch.packages:
            tv.insert("", "end", iid=f"p{pk['index']}", text=pk["label"], values=("", ""))
            tv.insert(f"p{pk['index']}", "end", iid=f"xp{pk['index']}", text="...")
        if getattr(arch, "is_folder", False):
            tv.item("p0", open=True)
            populate("p0")

    def open_folder():
        if S["busy"]:
            messagebox.showinfo("Editor", "Wait for the current job to finish (or press Stop).", parent=w)
            return
        p = filedialog.askdirectory(parent=w, initialdir=initial_dir, title="Choose the game folder")
        if not p:
            return

        def work(prog, stop):
            try:
                arch = FolderArchive(p, progress=lambda n: prog(f"Scanning folder... {n} files"), stop=stop)
            except InterruptedError:
                return {"kind": "open", "arch": None, "path": p}
            return {"kind": "open", "arch": arch, "path": p}

        start_job(work, "Scanning folder...")

    def start_job(fn, title):
        if S["busy"]:
            messagebox.showinfo("Editor", "Another job is running. Wait or press Stop.", parent=w)
            return
        S["busy"], S["stop"] = True, False
        S["t0"] = time.monotonic()
        status.config(text=title)

        def run():
            try:
                msgs.put(("done", fn(lambda text, frac=None: msgs.put(("prog", text, frac)),
                                     lambda: S["stop"])))
            except Exception as e:
                msgs.put(("error", str(e)))

        threading.Thread(target=run, daemon=True).start()

    def extract_selected():
        arch = S["arch"]
        sel = tv.selection()
        if arch is None or not sel:
            messagebox.showinfo("Editor", "Open a .nup file or a folder and select what to extract.", parent=w)
            return
        dest = filedialog.askdirectory(parent=w, title="Extract into which folder?")
        if not dest:
            return
        jobs = []
        for iid in sel:
            if iid[0] == "p":
                pk = arch.packages[int(iid[1:])]
                jobs.append(("dir", pk["root"], os.path.join(dest, _safe_name(pk["label"]))))
            elif iid[0] == "d":
                jobs.append(("dir", int(iid[1:]), os.path.join(dest, _safe_name(tv.item(iid, "text")))))
            elif iid[0] == "f":
                jobs.append(("file", int(iid[1:]), dest))

        def work(prog, stop):
            total = 0
            for kind, idx, out in jobs:
                if stop():
                    break
                if kind == "dir":
                    total += nup_extract(arch, idx, out, lambda n, name: prog(f"Extracted {total + n} files..."))
                else:
                    os.makedirs(out, exist_ok=True)
                    export_file(arch, idx, os.path.join(out, _safe_name(arch.files[idx][0])))
                    total += 1
            return {"kind": "extract", "n": total, "dest": dest}

        start_job(work, "Extracting...")

    bar1 = ttk.Frame(t1)
    bar1.pack(fill="x", side="bottom", padx=8, pady=(0, 8), before=sb)
    ttk.Button(bar1, text="Extract selected...", command=extract_selected).pack(side="left")
    ttk.Label(bar1, text="  (select files or folders; Ctrl/Shift for several)").pack(side="left")

    # ---- search tab
    pat = tk.StringVar(value="Weapon_")
    v_inside, v_nested = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
    v_utf16, v_icase, v_whole = tk.BooleanVar(value=False), tk.BooleanVar(value=True), tk.BooleanVar(value=True)
    pfilter, maxmb = tk.StringVar(), tk.StringVar(value="64")
    preset = tk.StringVar(value="Balanced (default)")
    v_skip = tk.BooleanVar(value=False)
    v_notify = tk.BooleanVar(value=True)

    r1 = ttk.Frame(t2)
    r1.pack(fill="x", padx=8, pady=6)
    ttk.Label(r1, text="Text to look for:").pack(side="left")
    ttk.Entry(r1, textvariable=pat, width=28).pack(side="left", padx=6)
    ttk.Checkbutton(r1, text="Ignore upper/lower case", variable=v_icase).pack(side="left", padx=6)
    ttk.Checkbutton(r1, text="Show the whole name", variable=v_whole).pack(side="left", padx=6)
    r2 = ttk.Frame(t2)
    r2.pack(fill="x", padx=8)
    ttk.Checkbutton(r2, text="Look inside files", variable=v_inside).pack(side="left")
    ttk.Checkbutton(r2, text="Also inside compressed (.zs) files", variable=v_nested).pack(side="left", padx=8)
    ttk.Checkbutton(r2, text="Also UTF-16 text", variable=v_utf16).pack(side="left", padx=8)
    r3 = ttk.Frame(t2)
    r3.pack(fill="x", padx=8, pady=6)
    ttk.Label(r3, text="Only files whose path contains:").pack(side="left")
    ttk.Entry(r3, textvariable=pfilter, width=22).pack(side="left", padx=6)

    res = ttk.Treeview(t2, columns=("count", "where"), height=14)
    res.heading("#0", text="Name found")
    res.heading("count", text="Times")
    res.heading("where", text="First found in")
    res.column("#0", width=300)
    res.column("count", width=60)
    res.column("where", width=460)

    def get_limits():
        """(max MB per file, resource settings) from the 'Resource use' box, or None after an error message."""
        try:
            mb = float(maxmb.get().replace(",", "."))
            if mb <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Editor", "The piece size must be a number bigger than 0.", parent=w)
            return None
        rs = dict(RESOURCE_PRESETS.get(preset.get(), DEFAULT_RESOURCES))
        return mb, rs

    def search():
        arch = S["arch"]
        if arch is None:
            messagebox.showinfo("Editor", "Open a .nup file or a folder first.", parent=w)
            return
        if len(pat.get().strip()) < 2:
            messagebox.showinfo("Editor", "Type at least 2 characters to look for.", parent=w)
            return
        lim = get_limits()
        if lim is None:
            return
        mb, rs = lim
        o = {"pattern": pat.get().strip(), "inside": v_inside.get(), "nested": v_nested.get(),
             "utf16": v_utf16.get(), "icase": v_icase.get(), "whole": v_whole.get(),
             "pathfilter": pfilter.get().strip(), "max_bytes": int(mb * 1024 * 1024),
             "skip_big": v_skip.get()}
        S["found"] = {}
        res.delete(*res.get_children())

        def work(prog, stop):
            def on_find(name, count, where):
                msgs.put(("find", name, count, where))

            eta = ProgressEta()
            f, scanned, skipped, stopped = nup_search(
                arch, o,
                progress=lambda n, t, k, fr: prog(
                    f"Scanned {n}/{t} files, {k} names found...  {eta.text(fr)}", fr),
                stop=stop,
                on_find=on_find, res=rs)
            return {"kind": "search", "found": f, "scanned": scanned, "skipped": skipped, "stopped": stopped}

        start_job(work, "Searching...")

    def stop_job():
        S["stop"] = True

    def start_find():
        """Find the names of the keys of the save open in the main window, using this archive/folder."""
        if S["busy"]:
            messagebox.showinfo("Editor", "Another job is running. Wait or press Stop.", parent=w)
            return
        arch = S["arch"]
        if arch is None:
            messagebox.showinfo("Editor", "Open a .nup file or a folder first.", parent=w)
            return
        ctx = resolver() if resolver else None
        if not ctx:
            messagebox.showinfo("Editor", "Open a save file in the main window first: the names are "
                                          "matched against the keys of that file.", parent=w)
            return
        if not ctx["keys"]:
            messagebox.showinfo("Editor", f"All {ctx['all_keys']} keys of this save already have a name: "
                                          "nothing to search.", parent=w)
            return
        templates = [t.strip() for t in shapes.get().split(";") if t.strip()]
        if not any("{}" in t for t in templates):
            messagebox.showinfo("Editor", "Type at least one name shape containing {} "
                                          "(for example  {}  or  IsGet.{} ).", parent=w)
            return

        lim = get_limits()
        if lim is None:
            return
        mb, rs = lim
        S["added"] = 0
        flog(f"[{stamp()}] Resource use: {preset.get()}, files read in pieces of {mb:g} MB"
             f"{' (bigger files skipped)' if v_skip.get() else ' (no file skipped)'}, "
             f"up to {rs['words']:,} words remembered.")
        flog(f"[{stamp()}] Search started: {len(ctx['keys'])} keys without a name, hash type {ctx['algo']}.")
        flog("            Name shapes tried ({} = each word found in the game files):")
        for t in templates:
            flog(f"              {t}")
        inc = [x.strip() for x in inc_var.get().split(";") if x.strip()]
        exc = [x.strip() for x in exc_var.get().split(";") if x.strip()]
        flog(f"            Only files containing: {'; '.join(inc) if inc else '(all)'}")
        flog(f"            Skipping files containing: {'; '.join(exc) if exc else '(none)'}")

        def work(prog, stop):
            eta = ProgressEta()
            found, scanned, tried, stopped = nup_find_names(
                arch, ctx["keys"], ctx["algo"], templates, max_bytes=int(mb * 1024 * 1024), res=rs, skip_big=v_skip.get(),
                include=inc, exclude=exc,
                progress=lambda n, t, k, c, fr: prog(
                    f"Scanned {n}/{t} files, {k} names matched, {c} tried...  {eta.text(fr)}", fr),
                stop=stop,
                on_found=lambda f: msgs.put(("autosave", f, ctx)),
                on_match=lambda key, name, path: msgs.put(("match", key, name, path)),
                on_file=lambda path: msgs.put(("cur", path)))
            return {"kind": "resolve", "found": found, "scanned": scanned, "tried": tried,
                    "stopped": stopped, "ctx": ctx}

        start_job(work, "Looking for names...")

    def save_list():
        if not S["found"]:
            messagebox.showinfo("Editor", "Nothing to save yet.", parent=w)
            return
        p = filedialog.asksaveasfilename(parent=w, defaultextension=".txt", initialfile="names.txt",
                                         filetypes=[("Text file", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                for n in sorted(S["found"]):
                    c, where = S["found"][n]
                    f.write(f"{n}\t{c}\t{where}\n")

    def extract_result():
        sel = res.selection()
        arch = S["arch"]
        if not sel or arch is None:
            return
        where = S["found"].get(sel[0], [0, ""])[1].split("  (")[0]
        for _l, path, fi in nup_walk(arch):
            if path == where:
                dest = filedialog.askdirectory(parent=w, title="Extract this file into which folder?")
                if dest:
                    export_file(arch, fi, os.path.join(dest, _safe_name(arch.files[fi][0])))
                    messagebox.showinfo("Editor", f"Extracted to:\n{dest}", parent=w)
                return

    def copy_selected():
        sel = res.selection()
        if not sel:
            messagebox.showinfo("Editor", "Select one or more rows first (Ctrl/Shift for several).", parent=w)
            return
        lines = []
        for iid in sel:
            c, where = S["found"].get(iid, [0, ""])
            lines.append(f"{iid}\t{c}\t{where}")
        text = "\n".join(lines)
        w.clipboard_clear()
        w.clipboard_append(text)
        w.update()  # keep clipboard after window closes on some platforms
        messagebox.showinfo("Editor", f"Copied {len(sel)} row(s) to the clipboard.", parent=w)

    def copy_names_only():
        sel = res.selection()
        if not sel:
            # if nothing selected, copy all names currently in the list
            sel = res.get_children()
        if not sel:
            messagebox.showinfo("Editor", "Nothing to copy yet.", parent=w)
            return
        text = "\n".join(sel)
        w.clipboard_clear()
        w.clipboard_append(text)
        w.update()
        messagebox.showinfo("Editor", f"Copied {len(sel)} name(s) to the clipboard.", parent=w)

    def on_res_right_click(event):
        iid = res.identify_row(event.y)
        if iid and iid not in res.selection():
            res.selection_set(iid)
        menu = tk.Menu(w, tearoff=0)
        menu.add_command(label="Copy selected rows (name + count + path)", command=copy_selected)
        menu.add_command(label="Copy name(s) only", command=copy_names_only)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    res.bind("<Button-3>", on_res_right_click)  # right-click (Windows/Linux)
    res.bind("<Button-2>", on_res_right_click)  # middle / macOS alternative

    bar2 = ttk.Frame(t2)
    bar2.pack(fill="x", padx=8, pady=6)
    ttk.Button(bar2, text="Search", command=search).pack(side="left")
    ttk.Button(bar2, text="Stop", command=stop_job).pack(side="left", padx=6)
    ttk.Button(bar2, text="Save list...", command=save_list).pack(side="left", padx=6)
    ttk.Button(bar2, text="Copy selected", command=copy_selected).pack(side="left", padx=6)
    ttk.Button(bar2, text="Copy names", command=copy_names_only).pack(side="left", padx=6)
    ttk.Button(bar2, text="Extract the file of the selected row...", command=extract_result).pack(side="left", padx=6)
    res.configure(height=10)
    pack_scroll(res, padx=(8, 0), pady=(0, 8))

    # ---- "find names for the open save" tab
    shapes, f_info, f_now = tk.StringVar(), tk.StringVar(), tk.StringVar()

    def stamp():
        return time.strftime("%H:%M:%S")

    def names_help():
        """Small window that explains how the hash names are built, with the Ocarina of Time example."""
        d = tk.Toplevel(w)
        d.title("How to build the hash names")
        d.transient(w)
        body = (
            "HOW TO BUILD THE HASH NAMES\n\n"
            "A save file stores every value under a number (the key). That number is a hash of a name, and a hash "
            "cannot be reversed. So the program does the opposite: it takes the words found in the game files, builds "
            "possible names and checks which ones give a key of your save.\n\n"
            "Everything happens on your computer, with a game dump that you made yourself. The names found are saved in "
            "hash_names.json (in your data folder) and are never uploaded or downloaded.\n\n"
            "STEPS\n"
            "1. In the main window open your save (Open file...).\n"
            "2. Click 'Game archive / folder...' and open the .nup file or the folder of your own game dump.\n"
            "3. Go to the tab 'Find names for the open save'. It shows how many keys still have no name.\n"
            "4. Fill in the name shapes (box at the top), separated by ;  The symbol {} stands for each word found in "
            "the game files:\n"
            "     IsGet.{}  tries  IsGet.Item_Hookshot\n"
            "     PouchData.{}.StockNum  tries  PouchData.Item_DekuStick.StockNum\n"
            "   When some names of the game are already known the program suggests the shapes by itself. For a new game "
            "it starts with just {}: use 'Add by name' on a few entries, with their full names, to teach it the shapes, "
            "or type them yourself.\n"
            "5. Optional: limit the files read ('Only read files whose path contains', for example /event/;/message/) "
            "to get fewer false names. Textures, models, effects and audio are skipped by default.\n"
            "6. Press Start. Every name found appears in the log and is saved as soon as it is found.\n"
            "7. In the main window the names appear in 'All values' and 'Compare saves'.\n\n"
            "EXAMPLE: OCARINA OF TIME REMAKE\n"
            "Press 'Example (Ocarina of Time)' to fill the box with these shapes:\n\n"
            + OOT_EXAMPLE_SHAPES.replace(";", ";  ") + "\n\n"
            "TIPS\n"
            "- Nothing found? The game probably uses another shape: look at the names you already know and copy their "
            "pattern.\n"
            "- Random names (like BottleA.wwgD)? The search is reading binary files: keep the default skip filter or use "
            "'Only read files whose path contains'.\n"
            "- Keys that already have a name are skipped and never overwritten.\n"
            "- To use the names on another PC: 'JSON data...' -> Hash names -> Export... / Merge...\n")
        t = tk.Text(d, wrap="word", width=96, height=30, borderwidth=0, padx=12, pady=10)
        sb = ttk.Scrollbar(d, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        _recolor_tk(t, pal())
        t.insert("1.0", body)
        t.configure(state="disabled")
        bt = ttk.Frame(d)
        bt.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        ttk.Button(bt, text="Use the Ocarina of Time example", command=lambda: (shapes.set(OOT_EXAMPLE_SHAPES), d.destroy())
                   ).pack(side="left")
        ttk.Button(bt, text="Close", command=d.destroy).pack(side="right")
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        autofit(d, 760, 560)

    ttk.Label(t3, textvariable=f_info, wraplength=880, justify="left").pack(fill="x", padx=8, pady=(8, 2))
    g1 = ttk.Frame(t3)
    g1.pack(fill="x", padx=8, pady=(4, 0))
    ttk.Label(g1, text="Name shapes to try, separated by ;").pack(side="left")
    ttk.Label(g1, text="   {} = each word found in the game files, e.g.  IsGet.{}  ->  IsGet.Item_Hookshot").pack(
        side="left")
    ttk.Button(g1, text="How does it work?", command=names_help).pack(side="right")
    g2 = ttk.Frame(t3)
    g2.pack(fill="x", padx=8, pady=4)
    ttk.Entry(g2, textvariable=shapes).pack(side="left", fill="x", expand=True)
    ttk.Button(g2, text="Reset shapes", command=lambda: refresh_find(True)).pack(side="left", padx=(6, 0))
    ttk.Button(g2, text="Example (Ocarina of Time)", command=lambda: shapes.set(OOT_EXAMPLE_SHAPES)
               ).pack(side="left", padx=(6, 0))
    DEFAULT_EXCLUDE = "/tex/;/model/;/effect/;.bntx;.bfres;/terrainarc/;.extm;.hght;.bake;.bfsha;.bnsh;.bfstm;.bwav;.bars;.webm;/movie/"
    inc_var, exc_var = tk.StringVar(), tk.StringVar(value=DEFAULT_EXCLUDE)
    h1 = ttk.Frame(t3)
    h1.pack(fill="x", padx=8, pady=(4, 0))
    ttk.Label(h1, text="Only read files whose path contains (separated by ;, empty = all):").pack(side="left")
    h2 = ttk.Frame(t3)
    h2.pack(fill="x", padx=8, pady=2)
    ttk.Entry(h2, textvariable=inc_var).pack(side="left", fill="x", expand=True)
    ttk.Button(h2, text="Text/data only", command=lambda: (inc_var.set(""), exc_var.set(DEFAULT_EXCLUDE))
               ).pack(side="left", padx=(6, 0))
    h3 = ttk.Frame(t3)
    h3.pack(fill="x", padx=8, pady=(4, 0))
    ttk.Label(h3, text="Skip files whose path contains (separated by ;):").pack(side="left")
    h4 = ttk.Frame(t3)
    h4.pack(fill="x", padx=8, pady=2)
    ttk.Entry(h4, textvariable=exc_var).pack(side="left", fill="x", expand=True)
    ttk.Button(h4, text="Clear filters", command=lambda: (inc_var.set(""), exc_var.set(""))
               ).pack(side="left", padx=(6, 0))
    g3 = ttk.Frame(t3)
    g3.pack(fill="x", padx=8, pady=4)
    ttk.Button(g3, text="Start", style="Accent.TButton", command=lambda: start_find()).pack(side="left")
    ttk.Button(g3, text="Stop", command=stop_job).pack(side="left", padx=6)
    ttk.Button(g3, text="Copy log", command=lambda: copy_log()).pack(side="left", padx=6)
    ttk.Button(g3, text="Save log...", command=lambda: save_log()).pack(side="left", padx=6)
    ttk.Button(g3, text="Clear log", command=lambda: clear_log()).pack(side="left", padx=6)
    ttk.Label(t3, textvariable=f_now, wraplength=880, justify="left").pack(fill="x", padx=8)
    logbox = ttk.Frame(t3)
    logbox.pack(fill="both", expand=True, padx=8, pady=(4, 8))
    logt = tk.Text(logbox, height=12, wrap="none", borderwidth=0, padx=6, pady=4, state="disabled")
    lsb = ttk.Scrollbar(logbox, orient="vertical", command=logt.yview)
    lsb_x = ttk.Scrollbar(logbox, orient="horizontal", command=logt.xview)
    logt.configure(yscrollcommand=lsb.set, xscrollcommand=lsb_x.set)
    _recolor_tk(logt, pal())  # dark/light colours from the start, not only after a theme change
    lsb.pack(side="right", fill="y")
    lsb_x.pack(side="bottom", fill="x")
    logt.pack(side="left", fill="both", expand=True)

    def flog(line):
        at_end = logt.yview()[1] >= 0.98  # keep following the log unless the user scrolled up
        logt.configure(state="normal")
        logt.insert("end", line + "\n")
        logt.configure(state="disabled")
        if at_end:
            logt.see("end")

    def clear_log():
        logt.configure(state="normal")
        logt.delete("1.0", "end")
        logt.configure(state="disabled")

    def copy_log():
        text = logt.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("Editor", "The log is empty.", parent=w)
            return
        w.clipboard_clear()
        w.clipboard_append(text)
        w.update()

    def save_log():
        text = logt.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("Editor", "The log is empty.", parent=w)
            return
        p = filedialog.asksaveasfilename(parent=w, defaultextension=".txt", initialfile="find_names_log.txt",
                                         filetypes=[("Text file", "*.txt")])
        if p:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text + "\n")

    def refresh_find(reset=False):
        """Show what the open save needs and (re)fill the name shapes."""
        if S["busy"]:
            return
        ctx = resolver() if resolver else None
        if not ctx:
            f_info.set("No save is open in the main window: open a save there first, the names are "
                       "matched against the keys of that file.")
            return
        auto = ";".join(ctx.get("templates") or DEFAULT_TEMPLATES)
        if reset or not shapes.get().strip() or shapes.get() == S.get("auto_shapes"):
            shapes.set(auto)  # the box still holds the suggestion (not edited by hand): follow the open save
        S["auto_shapes"] = auto
        if ctx["keys"]:
            f_info.set(f"Open save: {ctx['all_keys']} keys, {ctx['already']} already have a name, "
                       f"{len(ctx['keys'])} to look for.  Hash type: {ctx['algo']}.")
        else:
            f_info.set(f"All {ctx['all_keys']} keys of the open save already have a name: nothing to search.")

    nb.bind("<<NotebookTabChanged>>", lambda e: refresh_find() if nb.select() == str(t3) else None)

    def maybe_notify(r):
        """Sound + taskbar flash + a small card when a task that took a while has ended (not when stopped)."""
        if not v_notify.get() or r.get("stopped") or time.monotonic() - S.get("t0", time.monotonic()) < NOTIFY_AFTER:
            return
        kind = r["kind"]
        if kind == "resolve":
            notify_done(w, "Name search finished", f"{len(r['found'])} keys of the save matched a name.")
        elif kind == "search":
            notify_done(w, "Search finished", f"{len(r['found'])} names found in {r['scanned']} files.")
        elif kind == "extract":
            notify_done(w, "Extraction finished", f"{r['n']} files extracted.")
        elif kind == "open" and r.get("arch") is not None:
            notify_done(w, "Scan finished", "The game folder is ready.")

    def finish(r):
        if r["kind"] == "resolve":
            added = S.pop("added", 0) + r["ctx"]["apply"](r["found"])
            extra = " (stopped)" if r["stopped"] else ""
            status.config(text=f"{len(r['found'])} keys matched, {added} new names saved{extra}.")
            f_now.set("")
            flog(f"[{stamp()}] {'Stopped' if r['stopped'] else 'Finished'}: {len(r['found'])} keys matched "
                 f"({added} new names saved), {r['tried']} candidate names tried in {r['scanned']} files.")
            if not r["found"]:
                flog("            No match. Try other name shapes (e.g. the part before/after the word).")
            refresh_find()
            messagebox.showinfo(
                "Editor",
                f"{len(r['found'])} keys of the save matched a name ({added} new, saved).\n"
                f"{r['tried']} candidate names tried in {r['scanned']} files{extra}.\n\n"
                + ("The names are listed in the log of the 'Find names' tab and now show up in "
                   "'All values' and 'Compare saves'." if r["found"] else
                   "No match. Try other name shapes (e.g. the part before/after the word), or search "
                   "the names by hand in the Search tab."), parent=w)
            return
        if r["kind"] == "open":
            if r["arch"] is None:
                status.config(text="Scan stopped.")
            else:
                status.config(text="Done.")
                show_arch(r["arch"], r["path"])
            return
        if r["kind"] == "extract":
            status.config(text="Done.")
            messagebox.showinfo("Editor", f"Extracted {r['n']} files to:\n{r['dest']}", parent=w)
            return
        # Live updates already filled the list; just sync S["found"] and status.
        S["found"] = r["found"]
        extra = " (stopped)" if r["stopped"] else ""
        sk = f", {r['skipped']} skipped (too big, as you asked, or unreadable)" if r["skipped"] else ""
        status.config(text=f"{len(r['found'])} names found in {r['scanned']} files{sk}{extra}."
                           + ("  Showing the first 5000." if len(r["found"]) > 5000 else ""))

    def poll():
        t_start, delay = time.monotonic(), 100
        try:
            while True:
                if time.monotonic() - t_start > 0.03:  # long backlog: let the window breathe, continue soon
                    delay = 20
                    break
                m = msgs.get_nowait()
                if m[0] == "prog":
                    status.config(text=m[1])
                    if len(m) > 2 and m[2] is not None:
                        if not pbar.winfo_ismapped():
                            pbar.pack(side="bottom", fill="x", padx=12, pady=(0, 4), before=nb)
                        pbar.configure(value=min(max(m[2], 0.0), 1.0) * 1000)
                elif m[0] == "find":
                    name, count, where = m[1], m[2], m[3]
                    S["found"][name] = [count, where]
                    if res.exists(name):
                        res.item(name, values=(count, where))
                    elif len(res.get_children()) < 5000:
                        res.insert("", "end", iid=name, text=name, values=(count, where))
                elif m[0] == "match":
                    flog(f"[{stamp()}] {m[1]}  {m[2]}    <- {m[3]}")
                elif m[0] == "cur":
                    f_now.set("Scanning: " + m[1])
                elif m[0] == "autosave":
                    # names found so far go to hash_names.json right away (main thread)
                    try:
                        S["added"] = S.get("added", 0) + m[2]["apply"](m[1])
                    except Exception:
                        pass  # a failed intermediate save must not stop the search
                elif m[0] == "done":
                    S["busy"] = False
                    pbar.pack_forget()
                    maybe_notify(m[1])
                    finish(m[1])
                else:
                    S["busy"] = False
                    pbar.pack_forget()
                    status.config(text="Error.")
                    f_now.set("")
                    messagebox.showerror("Editor", m[1], parent=w)
        except queue.Empty:
            pass
        if not S["closing"]:
            w.after(delay, poll)

    def on_close():
        S["stop"], S["closing"] = True, True
        if not S["busy"] and S["arch"]:
            S["arch"].close()
        w.destroy()

    ttk.Button(head, text="Open .nup...", style="Accent.TButton", command=open_nup).pack(side="right", padx=4)
    ttk.Button(head, text="Open folder...", style="Accent.TButton", command=open_folder).pack(side="right", padx=4)
    ttk.Button(head, text="Stop", command=stop_job).pack(side="right", padx=4)
    status.pack(side="bottom", anchor="w", padx=12, pady=(0, 8), before=nb)

    # ---- resource use (shared by both search tabs)
    resbox = ttk.LabelFrame(w, text="  Resource use of the searches  ")
    resbox.pack(side="bottom", fill="x", padx=10, pady=(2, 4), before=nb)
    ttk.Label(resbox, text="Mode:").pack(side="left", padx=(8, 0), pady=6)
    pcombo = ttk.Combobox(resbox, textvariable=preset, values=list(RESOURCE_PRESETS), state="readonly", width=26)
    pcombo.pack(side="left", padx=6)
    ttk.Label(resbox, text="Read files in pieces of (MB):").pack(side="left", padx=(12, 0))
    ttk.Entry(resbox, textvariable=maxmb, width=7).pack(side="left", padx=6)
    ttk.Checkbutton(resbox, text="Skip files bigger than that (faster, may miss data)",
                    variable=v_skip).pack(side="left", padx=8)
    ttk.Checkbutton(resbox, text="Notify me when a long task ends", variable=v_notify).pack(side="right", padx=8)
    pcombo.bind("<<ComboboxSelected>>", lambda e: maxmb.set(f"{RESOURCE_PRESETS[preset.get()]['mb']:g}"))
    w.protocol("WM_DELETE_WINDOW", on_close)
    poll()

# ------------------------------------------------------------ window
def set_app_icon(root):
    """Window / taskbar icon (assets/icon.png, also bundled by PyInstaller)."""
    import sys
    import tkinter as tk
    base = getattr(sys, "_MEIPASS", BASE)
    png = os.path.join(base, "assets", "icon.png")
    try:  # group the icon correctly in the Windows taskbar
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EdenSaveEditor.App")
    except Exception:
        pass
    try:
        img = tk.PhotoImage(file=png)
        root.iconphoto(True, img)
        root._icon_ref = img  # keep a reference so it is not garbage collected
    except Exception:
        pass


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    try:  # sharp text on Windows high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title(f"Eden-Based Save Editor v{APP_VERSION}")
    set_app_icon(root)
    root.geometry("1080x680")
    autofit(root, 1080, 680)
    root.minsize(860, 540)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        pass
    ui_cfg = load_ui_settings()
    _THEME["mode"] = ui_cfg.get("theme", "dark") if ui_cfg.get("theme") in PALETTES else "dark"
    apply_theme(root)
    st = {"path": None, "data": None, "idx": {}, "fields": load_fields(), "names": load_names_db()}

    def start_dir():
        return START_DIR if os.path.isdir(START_DIR) else os.path.expanduser("~")

    def file_key():
        return title_id(st["path"]), os.path.basename(st["path"])

    def file_fields():
        t, f = file_key()
        return st["fields"].setdefault(t, {}).setdefault(f, {})

    GAME_NAMES = {"0400486024DEC000": "Zelda: Ocarina of Time", "0400C3F00006E000": "Mario Kart World",
                  "04003F40232DE000": "Hyrule Warriors: Age of Imprisonment",
                  "040074A01BF12000": "Donkey Kong Bananza"}

    # ---- header
    header = ttk.Frame(root)
    header.pack(fill="x", padx=22, pady=(16, 6))
    tbox = ttk.Frame(header)
    tbox.pack(side="left")
    trow = ttk.Frame(tbox)
    trow.pack(anchor="w")
    ttk.Label(trow, text="Eden-Based Save Editor", style="Title.TLabel").pack(side="left")
    ttk.Label(trow, text=f"v{APP_VERSION}", style="Badge.TLabel").pack(side="left", padx=(10, 0), pady=(8, 0))
    ttk.Label(tbox, text="Edit your Switch saves safely: every change makes an automatic backup.",
              style="Sub.TLabel").pack(anchor="w")
    theme_btn = ttk.Button(header, style="Ghost.TButton")
    theme_btn.pack(side="right")

    def open_guide(_event=None):
        try:
            webbrowser.open(GUIDE_URL)
        except Exception:
            messagebox.showinfo("Guide", f"Open this link in your browser:\n{GUIDE_URL}", parent=root)

    def open_data_folder():
        path = os.path.normpath(DATA_DIR)
        try:
            root.clipboard_clear()
            root.clipboard_append(path)  # the path is also copied: paste it in the Explorer address bar
        except Exception:
            pass
        err = None
        try:
            os.makedirs(path, exist_ok=True)  # create it now if it is missing
            import subprocess
            if os.name == "nt":
                try:
                    os.startfile(path)  # noqa: S606
                except Exception:
                    subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except Exception as e:
            err = e
        if err is not None:
            messagebox.showerror("Data folder", f"Could not open the data folder:\n{path}\n\n{err}", parent=root)
        else:
            set_status(f"Data folder: {path}  (path copied)")

    ttk.Button(header, text="📖  Guide", style="Ghost.TButton", command=open_guide).pack(side="right", padx=(0, 4))
    ttk.Button(header, text="📁  Data folder", style="Ghost.TButton",
               command=open_data_folder).pack(side="right", padx=(0, 4))
    guide_link = ttk.Label(tbox, text="New here? Read the Quick start guide on GitHub  →",
                           style="Link.TLabel", cursor="hand2")
    guide_link.pack(anchor="w", pady=(2, 0))
    guide_link.bind("<Button-1>", open_guide)

    def theme_label():
        theme_btn.config(text="☀  Light" if _THEME["mode"] == "dark" else "🌙  Dark")

    def toggle_theme():
        apply_theme(root, "light" if _THEME["mode"] == "dark" else "dark")
        ui_cfg["theme"] = _THEME["mode"]
        save_ui_settings(ui_cfg)
        theme_label()
        zebra(tv)

    theme_btn.config(command=toggle_theme)
    theme_label()

    # ---- status bar (bottom)
    status_bar = ttk.Frame(root, style="Card.TFrame")
    status_bar.pack(side="bottom", fill="x")
    status_lbl = ttk.Label(status_bar, text="Ready.", style="Status.TLabel")
    status_lbl.pack(side="left")
    ttk.Label(status_bar, text=f"Eden-Based Save Editor v{APP_VERSION}", style="Status.TLabel").pack(side="right")

    def set_status(text, ok=True):
        status_lbl.config(text=text, foreground=pal()["ok"] if ok else pal()["danger"])
        root.after(6000, lambda: status_lbl.config(text="Ready.", foreground=pal()["muted"]))

    # ---- program updates from GitHub releases
    upd = {"win": None, "busy": False}

    def release_page():
        try:
            webbrowser.open(RELEASES_URL)
        except Exception:
            messagebox.showinfo("Updates", f"Open this link in your browser:\n{RELEASES_URL}", parent=root)

    def update_dialog(info):
        """Window with the release notes. 'Update now' downloads, installs and restarts the program."""
        try:
            if upd["win"] is not None and upd["win"].winfo_exists():
                upd["win"].lift()
                return
        except tk.TclError:
            pass
        tag = info["tag"]
        d = tk.Toplevel(root)
        upd["win"] = d
        d.title("Update required")
        d.transient(root)
        ttk.Label(d, text=f"Eden-Based Save Editor {tag} is available", style="Section.TLabel").pack(
            anchor="w", padx=16, pady=(14, 0))
        ttk.Label(d, text=f"You have v{APP_VERSION}. This update is required to keep using the program.\n"
                          f"What's new:", style="Muted.TLabel", justify="left").pack(
            anchor="w", padx=16, pady=(2, 6))
        box = ttk.Frame(d)
        box.pack(fill="both", expand=True, padx=16)
        txt = tk.Text(box, height=12, wrap="word", borderwidth=0, padx=8, pady=6)
        sb = ttk.Scrollbar(box, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        p = pal()
        txt.configure(bg=p["surface"], fg=p["text"], insertbackground=p["text"], relief="flat")
        txt.insert("1.0", (info.get("notes") or "").strip() or "No release notes.")
        txt.config(state="disabled")
        bar = ttk.Progressbar(d, mode="determinate", maximum=100)
        msg = ttk.Label(d, text="", style="Muted.TLabel")
        row = ttk.Frame(d)
        row.pack(fill="x", padx=16, pady=(10, 14), side="bottom")
        job = {"cancel": threading.Event(), "running": False, "q": queue.Queue()}

        def set_progress(pct, text):
            msg.config(text=text)
            if pct is None:
                if str(bar.cget("mode")) != "indeterminate":
                    bar.config(mode="indeterminate")
                    bar.start(12)
            else:
                bar.stop()
                bar.config(mode="determinate", value=pct)

        def reset():
            job["running"] = False
            bar.stop()
            bar.pack_forget()
            msg.pack_forget()
            btn_go.state(["!disabled"])
            btn_quit.config(text="Quit program")

        def finish_ok(res):
            if res["kind"] == "frozen":
                messagebox.showinfo("Update", f"Updating to {tag}. The program closes and starts again by "
                                              f"itself in a few seconds.", parent=d)
                root.destroy()
                return
            messagebox.showinfo("Updated", f"Updated to {tag}.\n\nThe previous version was saved in:\n"
                                           f"{res['backup']}\n\nThe program restarts now.", parent=d)
            try:
                restart_program()
            except Exception as e:
                reset()
                messagebox.showwarning("Restart", f"The update was installed, but the program could not restart "
                                                  f"by itself ({e}).\nClose it and open it again.", parent=d)
                return
            root.destroy()

        def poll():
            try:
                while True:
                    m = job["q"].get_nowait()
                    if m[0] == "p":
                        set_progress(m[1], m[2])
                    elif m[0] == "ok":
                        finish_ok(m[1])
                        return
                    elif m[0] == "cancel":
                        reset()
                        set_status("Update cancelled.")
                        return
                    elif m[0] == "na":
                        reset()
                        if ask_yesno("Update", f"The program cannot update itself here: {m[1]}.\n\n"
                                                         f"Open the release page to download it by hand?", parent=d):
                            release_page()
                        return
                    else:
                        reset()
                        messagebox.showerror("Update", m[1], parent=d)
                        return
            except queue.Empty:
                pass
            try:
                if d.winfo_exists():
                    d.after(100, poll)
            except tk.TclError:
                pass

        def go():
            if job["running"]:
                return
            job["running"] = True
            job["cancel"].clear()
            btn_go.state(["disabled"])
            btn_quit.config(text="Cancel")
            bar.pack(fill="x", padx=16, pady=(10, 0), before=row)
            msg.pack(anchor="w", padx=16, pady=(4, 0), before=row)
            set_progress(0, "Starting...")

            def work():
                def prog(done, total):
                    txt_ = "Downloading... " + human_size(done) + (f" / {human_size(total)}" if total else "")
                    job["q"].put(("p", done * 100.0 / total if total else None, txt_))
                try:
                    res = perform_update(info, prog, job["cancel"], lambda t: job["q"].put(("p", None, t)))
                    job["q"].put(("ok", res))
                except UpdateCancelled:
                    job["q"].put(("cancel",))
                except UpdateUnavailable as e:
                    job["q"].put(("na", str(e)))
                except Exception as e:
                    job["q"].put(("err", str(e)))

            threading.Thread(target=work, daemon=True).start()
            poll()

        def quit_or_cancel():
            if job["running"]:
                job["cancel"].set()  # stops the download; the window stays so the update can be retried
            else:
                root.destroy()  # the update is mandatory: without it the program closes

        btn_go = ttk.Button(row, text="Update now", style="Accent.TButton", command=go)
        btn_go.pack(side="left")
        ttk.Button(row, text="Release page", command=release_page).pack(side="left", padx=(8, 0))
        btn_quit = ttk.Button(row, text="Quit program", command=quit_or_cancel)
        btn_quit.pack(side="right")
        d.protocol("WM_DELETE_WINDOW", quit_or_cancel)
        autofit(d, 640, 520)
        try:  # modal: the main window cannot be used until the update is installed
            d.wait_visibility()
            d.grab_set()
            d.focus_set()
        except tk.TclError:
            pass

    def start_update_check(manual):
        """Ask GitHub in the background. At startup stay silent unless a new version exists."""
        if upd["busy"]:
            return
        upd["busy"] = True
        res = queue.Queue()
        if manual:
            set_status("Checking for updates...")

        def work():
            try:
                res.put(("ok", find_update()))
            except Exception as e:
                res.put(("err", str(e)))

        def poll():
            try:
                kind, val = res.get_nowait()
            except queue.Empty:
                root.after(150, poll)
                return
            upd["busy"] = False
            if kind == "err":
                if manual:
                    messagebox.showerror("Updates", val, parent=root)
                else:
                    start_json_check(False)
            elif val is None:
                start_json_check(manual, f"You are up to date: v{APP_VERSION} is the latest version.\n\n"
                                         if manual else "")
            else:
                update_dialog(val)  # mandatory: the data files are checked after the restart

        threading.Thread(target=work, daemon=True).start()
        root.after(150, poll)

    # ---- new items in the project's JSON files on GitHub: notification + add / review
    jchk = {"busy": False, "frame": None}

    def hide_json_banner():
        f = jchk["frame"]
        jchk["frame"] = None
        if f is not None:
            try:
                f.destroy()
            except tk.TclError:
                pass

    def json_news_sig(rows):
        paths = sorted("/".join(str(x) for x in r[1]) for r in rows if r[0] == "new")
        return hashlib.sha1("\n".join(paths).encode("utf-8")).hexdigest()

    def show_json_banner(items):
        hide_json_banner()
        f = ttk.Frame(root, style="Card.TFrame")
        f.pack(fill="x", padx=22, pady=(0, 6), after=header)
        jchk["frame"] = f
        parts = [f"{sum(1 for r in it['rows'] if r[0] == 'new')} in {JSON_KINDS[it['kind']]['label']}"
                 for it in items]
        ttk.Label(f, text="📥  GitHub has data that you are missing: " + ", ".join(parts) + ".",
                  style="Card.TLabel").pack(side="left", padx=12, pady=8)

        def mark_seen():  # not shown again until GitHub has something different
            seen = ui_cfg.setdefault("json_seen", {})
            for it in items:
                seen[it["kind"]] = it["sig"]
            save_ui_settings(ui_cfg)

        def review():
            mark_seen()
            hide_json_banner()
            for it in items:
                json_merge_window(it["kind"], it["rows"], it["same"], root)

        def add_all():
            mark_seen()
            hide_json_banner()
            lines = []
            for it in items:
                new = [r for r in it["rows"] if r[0] == "new"]
                json_backup(it["kind"])
                json_apply(json_data(it["kind"]), new)
                json_store(it["kind"])
                lines.append(f"{JSON_KINDS[it['kind']]['label']}: {len(new)} added")
            messagebox.showinfo("Editor", "\n".join(lines) + "\n\nNothing of yours was removed "
                                "(a backup was made first).", parent=root)

        def dismiss():
            mark_seen()
            hide_json_banner()

        ttk.Button(f, text="Dismiss", style="Ghost.TButton", command=dismiss).pack(side="right", padx=(0, 8))
        ttk.Button(f, text="Review and add...", command=review).pack(side="right", padx=4)
        ttk.Button(f, text="Add all new", style="Accent.TButton", command=add_all).pack(side="right", padx=4)

    def start_json_check(manual=False, prefix=""):
        """Download the project's JSON files in the background and compare them with yours. At startup
        stay silent unless GitHub has something you miss (and you have not already dismissed that)."""
        if jchk["busy"]:
            return
        jchk["busy"] = True
        res = queue.Queue()

        def work():
            got, errs = {}, []
            for k, spec in JSON_KINDS.items():
                if not spec.get("remote"):  # only the files published on GitHub (not hash_names.json)
                    continue
                try:
                    obj = fetch_remote_json(spec["remote"])
                    err = json_validate(k, obj)
                    if err:
                        errs.append(f"{spec['label']}: {err}")
                    else:
                        got[k] = obj
                except Exception as e:
                    errs.append(f"{spec['label']}: {e}")
            res.put((got, errs))

        def poll():
            try:
                got, errs = res.get_nowait()
            except queue.Empty:
                root.after(150, poll)
                return
            jchk["busy"] = False
            seen = ui_cfg.get("json_seen") or {}
            items = []
            for k, obj in got.items():
                rows, same = json_diff(k, json_data(k), obj)
                if any(r[0] == "new" for r in rows):
                    items.append({"kind": k, "rows": rows, "same": same, "sig": json_news_sig(rows)})
            fresh = [it for it in items if manual or seen.get(it["kind"]) != it["sig"]]
            if fresh:
                show_json_banner(fresh)
            if not manual:
                return
            if errs and not got:
                messagebox.showerror("Updates", prefix + "Could not check the data files on GitHub:\n" + "\n".join(errs),
                                     parent=root)
            elif not fresh:
                messagebox.showinfo("Updates", prefix + "Your data files already contain everything that is on "
                                    "GitHub." + ("\n\n" + "\n".join(errs) if errs else ""), parent=root)
            elif prefix:
                messagebox.showinfo("Updates", prefix.strip(), parent=root)

        threading.Thread(target=work, daemon=True).start()
        root.after(150, poll)

    ttk.Button(header, text="🔄  Updates", style="Ghost.TButton",
               command=lambda: start_update_check(True)).pack(side="right", padx=(0, 4))
    root.after(300, lambda: start_update_check(False))  # always: a new version must be installed

    # ---- toolbar
    extra = ttk.Frame(root)
    extra.pack(fill="x", padx=22, pady=(6, 4))

    # ---- 1) open the file
    f1 = ttk.LabelFrame(root, text="  File  ")
    f1.pack(fill="x", padx=22, pady=8)
    badge = ttk.Label(f1, text="no game", style="Badge.TLabel")
    badge.pack(side="left", padx=(6, 10), pady=6)
    lbl = ttk.Label(f1, text="No file open. For example, choose the progress.sav file of a slot.",
                    style="Card.TLabel", wraplength=700, justify="left")
    lbl.pack(side="left", padx=4, pady=6)

    # ---- 2) editable fields
    f2 = ttk.LabelFrame(root, text="  Things you can edit  ")
    f2.pack(fill="both", expand=True, padx=22, pady=(4, 10))
    info = ttk.Label(f2, wraplength=900, justify="left", style="CardMuted.TLabel")
    info.pack(anchor="w", padx=8, pady=(2, 6))
    NO_FILE_MSG = "Load a save file first: press 'Open file...' and choose your save."
    info.config(text=NO_FILE_MSG)
    search_row = ttk.Frame(f2, style="Card.TFrame")
    search_row.pack(fill="x", padx=8, pady=(0, 6))
    ttk.Label(search_row, text="Search:", style="Card.TLabel").pack(side="left")
    item_search = tk.StringVar()
    search_entry = ttk.Entry(search_row, textvariable=item_search, width=32)
    search_entry.pack(side="left", padx=8)
    ttk.Button(search_row, text="✕", width=3, command=lambda: (item_search.set(""), search_entry.focus_set())
               ).pack(side="left")
    search_count = ttk.Label(search_row, text="", style="CardMuted.TLabel")
    search_count.pack(side="left", padx=10)
    ttk.Label(search_row, text="by item name or by current value", style="CardMuted.TLabel").pack(side="right")
    tv_wrap = ttk.Frame(f2, style="Card.TFrame")
    tv_wrap.pack(fill="both", expand=True, padx=8)
    tv = ttk.Treeview(tv_wrap, columns=("name", "current"), show="headings", height=8)
    tv.heading("name", text="ITEM", anchor="w")
    tv.heading("current", text="CURRENT VALUE IN FILE", anchor="w")
    tv.column("name", width=420, anchor="w")
    tv.column("current", width=240, anchor="w")
    tv_sb = ttk.Scrollbar(tv_wrap, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=tv_sb.set)
    tv.pack(side="left", fill="both", expand=True)
    tv_sb.pack(side="right", fill="y")

    row = ttk.Frame(f2, style="Card.TFrame")
    row.pack(side="bottom", fill="x", padx=8, pady=10)  # packed first so the buttons are never cut off
    tv_wrap.pack_configure(after=row)
    ttk.Label(row, text="New value:", style="Card.TLabel").pack(side="left")
    new_value = tk.StringVar()
    new_entry = ttk.Entry(row, textvariable=new_value, width=16)
    new_entry.pack(side="left", padx=8)

    def value_offset(c):
        """Position of the value in the open file: looked up by key (survives shifts)."""
        if "hash" in c:
            o = st["idx"].get(c["hash"])
            return None if o is None else o + 4
        return c.get("offset")

    def refresh():
        keep = tv.selection()
        tv.delete(*tv.get_children())
        if st["data"] is None:
            search_count.config(text="")
            info.config(text=NO_FILE_MSG)
            return
        fields = file_fields()
        changed = False
        for c in fields.values():  # old entries (position only): work out the key
            o = c.get("offset")
            if "hash" not in c and o is not None and o >= 4 and o + 4 <= len(st["data"]):
                h = struct.unpack_from("<I", st["data"], o - 4)[0]
                if st["idx"].get(h) == o - 4:
                    c["hash"] = h
                    changed = True
        if changed:
            save_fields(st["fields"])
        needle = item_search.get().strip().lower()
        shown = 0
        for name, c in fields.items():
            o = value_offset(c)
            v = None if o is None else get_value(st["data"], o, c["kind"])
            cur = fmt_value(v, c["kind"])
            if needle and needle not in name.lower() and needle not in str(cur).lower():
                continue  # the search matches the item name or the value shown in the list
            tv.insert("", "end", iid=name, values=(name, cur))
            shown += 1
        zebra(tv)
        keep = [i for i in keep if tv.exists(i)]
        if keep:
            tv.selection_set(keep)
        search_count.config(text=(f"{shown} of {len(fields)}" if needle else (f"{len(fields)} items" if fields else "")))
        if fields and needle and not shown:
            info.config(text=f"No item matches '{item_search.get().strip()}'. Clear the search to see them all.")
        else:
            info.config(text="Select a row, type the new value and press Change." if fields else
                        "You have no entries yet. Press 'Add new entry...' and follow the steps: "
                        "the program finds where the value is by itself (rupees, hearts, etc.).")

    item_search.trace_add("write", lambda *a: refresh())

    def open_file():
        p = filedialog.askopenfilename(initialdir=start_dir(), title="Choose the save file")
        if p:
            st["path"], st["data"] = p, read(p)
            st["idx"] = index_hash(st["data"])
            tid = title_id(p)
            lbl.config(text=p)
            badge.config(text=GAME_NAMES.get(tid, f"game {tid}"))
            set_status(f"Opened {os.path.basename(p)}")
            # For OoT Remake, pre-fill useful fields if the user has none yet
            if tid == OOT_REMAKE_ID:
                ff = file_fields()
                if not ff:
                    ff.update(OOT_DEFAULT_FIELDS)
                    save_fields(st["fields"])
            refresh()

    def change_value():
        sel = tv.selection()
        if st["data"] is None or not sel:
            messagebox.showinfo("Editor", "Open a file and choose a row from the list.")
            return
        c = file_fields()[sel[0]]
        try:
            val = parse_value(new_value.get(), c["kind"])
        except ValueError:
            messagebox.showerror("Editor", "Type a valid number.")
            return
        o = value_offset(c)
        if o is None:
            messagebox.showerror("Editor", "I cannot find this entry in this save.\nCheck that you opened the right file.")
            return
        if not ask_yesno("Confirm", f"Write {val} into '{sel[0]}'?\nThe game must be closed."):
            return
        backup_path = write_value(st["path"], st["data"], o, c["kind"], val)
        st["idx"] = index_hash(st["data"])
        refresh()
        mod_time = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(st["path"])))
        set_status(f"Written {val} into '{sel[0]}' (backup created)")
        messagebox.showinfo("Done", f"Value written to:\n{st['path']}\n(modified at {mod_time})\n\nBackup of the previous version:\n{backup_path}")

    def remove_entry():
        sel = tv.selection()
        if sel and st["data"] is not None:
            file_fields().pop(sel[0], None)
            save_fields(st["fields"])
            refresh()

    # ---- duplicates: the same key must not appear twice in the main list under different names
    def entry_ref(c):
        """What an entry points to: ('hash', key) or ('offset', position)."""
        if "hash" in c:
            return ("hash", c["hash"])
        if c.get("offset") is not None:
            return ("offset", c["offset"])
        return None

    def ask_choice(parent, title, text, ok_text):
        """Question with two buttons: ok_text (True) and Cancel (False)."""
        return ask_yesno(title, text, parent=parent, yes_text=ok_text, no_text="Cancel", width=560)

    def rename_in_place(old, new):
        """Rename a main-list entry keeping its position, and keep its shown name in the names list in sync."""
        t, f = file_key()
        fields = file_fields()
        st["fields"][t][f] = {(new if k == old else k): v for k, v in fields.items()}
        c = st["fields"][t][f][new]
        if "hash" in c:
            db = st["names"].get(guess_title(st["path"], st["idx"]), {}).get("map", {})
            key = f"{c['hash']:08X}"
            cur = db.get(key, "")
            if cur.startswith(old + " ("):
                db[key] = new + cur[len(old):]
                save_names_db(st["names"])

    def place_entry(label, entry, parent):
        """Put an entry in the main list. If its key is already there under ANOTHER name the user chooses:
        rename the existing entry to the new name, or cancel. If the name is already used by a different
        key, it is refused. Returns the label used, or None when nothing was added."""
        fields = file_fields()
        ref = entry_ref(entry)
        if label in fields and entry_ref(fields[label]) != ref:
            messagebox.showerror("Editor", f"There is already an entry called '{label}' (it uses a different "
                                           "key).\nChoose another name.", parent=parent)
            return None
        other = next((lab for lab, c in fields.items() if lab != label and ref is not None
                      and entry_ref(c) == ref), None)
        if other is not None:
            what = f"key {ref[1]:08X}" if ref[0] == "hash" else f"position {ref[1]}"
            if not ask_choice(parent, "Already in the list",
                              f"The {what} is already in your list with the name:\n    {other}\n\n"
                              f"You are adding it as:\n    {label}\n\n"
                              "The same key cannot be listed twice. What do you want to do?",
                              f"Rename the existing entry to '{label}'"):
                return None
            rename_in_place(other, label)
        file_fields()[label] = entry  # an existing entry keeps its position in the list
        return label

    def rename_entry():
        sel = tv.selection()
        if st["data"] is None or not sel:
            messagebox.showinfo("Editor", "Open a file and choose a row from the list.")
            return
        old = sel[0]
        new = ask_text(root, "Rename", "Rename entry", detail=f"Current label: {old}",
                       prompt="New label:", initial=old, ok_text="Rename")
        if not new or not new.strip() or new.strip() == old:
            return
        new = new.strip()
        fields = file_fields()
        if new in fields:
            messagebox.showerror("Editor", f"There is already an entry called '{new}'.")
            return
        rename_in_place(old, new)  # keeps the order and the shown name in the names database in sync
        save_fields(st["fields"])
        refresh()
        if tv.exists(new):
            tv.selection_set(new)
            tv.see(new)
        set_status(f"Renamed '{old}' to '{new}'")

    # ---- guided wizard

    def add_by_name():
        """Add a parameter by typing the game's internal name (e.g. IsGet.Item_Hookshot).
        Hashes it (Murmur3 first, then other algos) and, if the key exists in this save,
        adds it to the main list and to hash_names.json."""
        if st["data"] is None or st["path"] is None:
            messagebox.showinfo("Editor", "Open a file first (step 1).")
            return
        nm = ask_text(root, "Add by name", "Add an entry by its internal name",
                      detail="Type the name the game uses, exactly as found in the NUP. "
                             "The program checks that it exists in this save.",
                      examples=["IsGet.Weapon_Sword_Kokiri", "PlayerStatus.CurrentRupee",
                                "PouchData.Item_Token_Gold.StockNum"],
                      prompt="Internal name:", ok_text="Add", width=560)
        if not nm or not nm.strip():
            return
        nm = nm.strip()
        keyset = set(st["idx"].keys())
        hits = [(a, f(nm)) for a, f in HASH_ALGOS.items() if f(nm) in keyset]
        if not hits:
            messagebox.showinfo(
                "Editor",
                f"'{nm}' does not match any key in this save\n"
                "(tried CRC32, Murmur3, FNV, …).\n\n"
                "Check the spelling, or the name may not be stored in this slot yet.",
                parent=root)
            return
        # Prefer Murmur3 if present
        hits_sorted = sorted(hits, key=lambda x: (0 if x[0].startswith("Murmur3") else 1, x[0]))
        algo, h = hits_sorted[0]
        lines = "\n".join(f"  {a}: {hh:08X}" for a, hh in hits_sorted)
        label = ask_text(root, "Name for the list", "Name for the main list",
                         detail=f"'{nm}' matches:\n{lines}\n\nUsing {algo} → {h:08X}",
                         prompt="Label to show in the main list:",
                         initial=nm.split(".")[-1] if "." in nm else nm, ok_text="Save", width=540)
        if not label or not label.strip():
            return
        label = label.strip()
        # Kind: default int32 (flags and counts)
        kind = "int32"
        tid = guess_title(st["path"], st["idx"])
        key_hex = f"{h:08X}"
        # the key may already have another name in the names list (and not be in the main list yet)
        old_name = names_for(st["names"], tid).get(key_hex)
        in_list = any(entry_ref(c) == ("hash", h) for c in file_fields().values())
        if old_name and not in_list and nm not in old_name and not old_name.startswith(label + " ("):
            if not ask_choice(root, "Key already named",
                              f"The key {key_hex} already has a name in your names list:\n    {old_name}\n\n"
                              f"You are adding it as:\n    {label} ({nm})",
                              "Replace the name with the new one"):
                return
        # Save to fields (asks what to do if the key is already there under another name)
        if place_entry(label, {"kind": kind, "hash": h}, root) is None:
            return
        save_fields(st["fields"])
        # Save to names db
        db = st["names"].setdefault(tid, {"algo": algo, "map": {}})
        db.setdefault("map", {})[key_hex] = f"{label} ({nm})"
        if not db.get("algo"):
            db["algo"] = algo
        save_names_db(st["names"])
        refresh()
        # Show current value
        cur = entry_value(st["data"], st["idx"], h, kind)
        messagebox.showinfo(
            "Added",
            f"Added '{label}'\n"
            f"key {h:08X} ({algo})\n"
            f"current value: {cur}",
            parent=root)

    def wizard():
        if st["data"] is None:
            messagebox.showinfo("Editor", "Open a file first (step 1).")
            return
        w = tk.Toplevel(root)
        w.title("Add an entry")
        w.geometry("660x600")
        autofit(w, 660, 600)
        s = {"cand": [], "last": None, "idx": None}

        ttk.Label(w, text="You need 2 saves of the same game where the value you are looking for is different.\n"
                          "Choose each file and type the number you had in it.",
                  justify="left").pack(anchor="w", padx=12, pady=8)

        a = ttk.Frame(w)
        a.pack(fill="x", padx=12)
        ttk.Label(a, text="Name of the entry:").pack(side="left")
        name = tk.StringVar()
        ttk.Entry(a, textvariable=name, width=24).pack(side="left", padx=6)
        kind = tk.StringVar(value="Whole number")
        ttk.Combobox(a, textvariable=kind, width=20, values=list(KINDS), state="readonly").pack(side="left", padx=6)

        file_rows = []

        def file_row(titolo):
            f = ttk.LabelFrame(w, text=titolo)
            f.pack(fill="x", padx=12, pady=6)
            path, val = tk.StringVar(), tk.StringVar()
            r = ttk.Frame(f)
            r.pack(fill="x", padx=8, pady=4)
            nf = ttk.Label(f, text="no file chosen", wraplength=600, justify="left")

            def pick_file():
                p = filedialog.askopenfilename(parent=w, initialdir=start_dir(), title=titolo)
                if p:
                    path.set(p)
                    nf.config(text=p)

            ttk.Button(r, text="Choose file...", command=pick_file).pack(side="left")
            ttk.Label(r, text="   Number you had in this file:").pack(side="left")
            ttk.Entry(r, textvariable=val, width=10).pack(side="left", padx=4)
            nf.pack(anchor="w", padx=8, pady=2)
            file_rows.append((path, val))

        file_row("File 1")
        file_row("File 2")
        file_row("File 3 (only if needed: the program will ask)")

        status = ttk.Label(w, text="", wraplength=620, justify="left")
        lb = tk.Listbox(w, height=5)

        def show_results():
            t = KINDS[kind.get()]
            lb.delete(0, "end")
            n = len(s["cand"])
            if n == 0:
                status.config(text="Nothing found. Check that the numbers are the ones you saw in the game "
                                  "when you saved each file, and that the two files are from different moments.")
                return
            if n == 1:
                status.config(text="Found! Press 'Save entry'.")
            elif n <= 40:
                status.config(text=f"I found {n}. Fill in File 3 too (with yet another number) and press Find.")
            else:
                status.config(text=f"Too many results ({n}). Fill in File 3 too (with yet another number) and press Find.")
                return
            for k in s["cand"]:
                now_val = fmt_value(cand_value(s["last"], s["idx"], k, t), t)
                lb.insert("end", f"entry {k[1]:08X}   (in the last file it is {now_val})" if k[0] == "h"
                          else f"position {k[1]}   (in the last file it is {now_val})")
            if n == 1:
                lb.selection_set(0)

        def find_entry():
            t = KINDS[kind.get()]
            used = [(p.get(), v.get()) for p, v in file_rows if p.get() and v.get().strip()]
            if len(used) < 2:
                messagebox.showinfo("Editor", "Choose at least File 1 and File 2 and type the number for each.", parent=w)
                return
            try:
                vals = [parse_value(v, t) for _, v in used]
            except ValueError:
                messagebox.showerror("Editor", "Type numbers only.", parent=w)
                return
            if len(set(vals)) == 1:
                messagebox.showinfo("Editor", "The numbers are all the same: you need different numbers.", parent=w)
                return
            cand = None
            for i, (p, _) in enumerate(used):
                data = read(p)
                idx = index_hash(data)
                cand = scan_candidates(data, idx, t, vals[i]) if i == 0 else filter_candidates(cand, data, idx, t, vals[i])
                s["last"], s["idx"] = data, idx
            s["cand"] = cand
            show_results()

        def save_entry():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("Editor", "Press Find first and choose a row from the list.", parent=w)
                return
            k = s["cand"][sel[0]]
            campo = {"kind": KINDS[kind.get()]}
            campo["hash" if k[0] == "h" else "offset"] = k[1]
            if place_entry(name.get().strip() or f"Entry {k[1]}", campo, w) is None:
                return
            save_fields(st["fields"])
            refresh()
            w.destroy()

        bt = ttk.Frame(w)
        bt.pack(pady=6)
        ttk.Button(bt, text="Find", command=find_entry).pack(side="left", padx=8)
        ttk.Button(bt, text="Save entry", command=save_entry).pack(side="left", padx=8)
        status.pack(anchor="w", padx=12)
        lb.pack(fill="x", padx=12, pady=4)

    # ---- extra tools: compare two saves / view strings
    def compare_window():
        w = tk.Toplevel(root)
        w.title("Compare two saves")
        w.geometry("820x580")
        autofit(w, 820, 580)
        s = {"a": st["path"], "b": None}
        ttk.Label(w, text="Pick two saves (for example before and after doing ONE thing in the game).\n"
                          "The list shows the entries that changed. Select one and press 'Use as entry...'.",
                  justify="left").pack(anchor="w", padx=12, pady=8)
        labels = {}

        def pick(which, title):
            def go():
                p = filedialog.askopenfilename(parent=w, initialdir=start_dir(), title=title)
                if p:
                    s[which] = p
                    labels[which].config(text=p)
            return go

        for which, title in (("a", "File A (before)"), ("b", "File B (after)")):
            f = ttk.LabelFrame(w, text=title)
            f.pack(fill="x", padx=12, pady=4)
            ttk.Button(f, text="Choose file...", command=pick(which, title)).pack(side="left", padx=8, pady=6)
            labels[which] = ttk.Label(f, text=s[which] or "no file chosen", wraplength=600, justify="left")
            labels[which].pack(side="left", padx=8)

        opts = ttk.Frame(w)
        opts.pack(fill="x", padx=12, pady=4)
        hide_flags = tk.BooleanVar(value=True)
        hide_ptr = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Hide on/off switches (0 and 1)", variable=hide_flags).pack(side="left")
        ttk.Checkbutton(opts, text="Hide internal positions (jump by 64)", variable=hide_ptr).pack(side="left", padx=12)

        cols = ("key", "name", "old", "new", "oldf", "newf")
        tv2 = ttk.Treeview(w, columns=cols, show="headings", height=12)
        for c, t, wd in (("key", "Key", 100), ("name", "Name", 200), ("old", "A (whole)", 110),
                         ("new", "B (whole)", 110), ("oldf", "A (decimal)", 100), ("newf", "B (decimal)", 100)):
            tv2.heading(c, text=t)
            tv2.column(c, width=wd)
        res = ttk.Label(w, text="")

        def run():
            if not (s["a"] and s["b"]):
                messagebox.showinfo("Editor", "Choose both files first.", parent=w)
                return
            A, B = read(s["a"]), read(s["b"])
            iA, iB = index_hash(A), index_hash(B)
            nmap = names_for(st["names"], guess_title(s["a"], iA))
            sg = lambda u: struct.unpack("<i", struct.pack("<I", u))[0]
            fl = lambda u: struct.unpack("<f", struct.pack("<I", u))[0]
            tv2.delete(*tv2.get_children())
            n = 0
            for k in list(iA) + [k for k in iB if k not in iA]:
                oa, ob = iA.get(k), iB.get(k)
                va = None if oa is None else struct.unpack_from("<I", A, oa + 4)[0]
                vb = None if ob is None else struct.unpack_from("<I", B, ob + 4)[0]
                if va == vb:
                    continue
                if va is not None and vb is not None:
                    if hide_flags.get() and va in (0, 1) and vb in (0, 1):
                        continue
                    if hide_ptr.get() and va >= 1000 and vb >= 1000 and abs(vb - va) % 64 == 0:
                        continue
                tv2.insert("", "end", iid=f"{k:08X}", values=(
                    f"{k:08X}", nmap.get(f"{k:08X}", ""),
                    "-" if va is None else sg(va), "-" if vb is None else sg(vb),
                    "-" if va is None else f"{fl(va):.4g}", "-" if vb is None else f"{fl(vb):.4g}"))
                n += 1
            res.config(text=f"{n} entries changed (a '-' means the entry exists in only one of the saves).")

        def use_selected():
            sel = tv2.selection()
            if not sel:
                messagebox.showinfo("Editor", "Select a row first.", parent=w)
                return
            if st["data"] is None:
                messagebox.showinfo("Editor", "Open a save file in the main window first (step 1).", parent=w)
                return
            nm = ask_text(w, "Entry name", "Name this entry",
                          detail="Choose a name you will recognise in the main list.",
                          prompt="Name:", ok_text="Save",
                          initial=names_for(st["names"], guess_title(st["path"], st["idx"])).get(sel[0], ""))
            if not nm:
                return
            dec = ask_yesno("Type", "Is it a decimal number (with a comma)?\nChoose No for a whole number.", parent=w)
            if place_entry(nm.strip(), {"kind": "float32" if dec else "int32", "hash": int(sel[0], 16)}, w) is None:
                return
            save_fields(st["fields"])
            refresh()
            messagebox.showinfo("Editor", f"Entry '{nm.strip()}' added to the main list.", parent=w)

        bt = ttk.Frame(w)
        bt.pack(pady=6)
        ttk.Button(bt, text="Compare", command=run).pack(side="left", padx=8)
        ttk.Button(bt, text="Use as entry...", command=use_selected).pack(side="left", padx=8)
        res.pack(anchor="w", padx=12)
        pack_scroll(tv2, padx=(12, 0), pady=4)

    def strings_window():
        if st["data"] is None:
            messagebox.showinfo("Editor", "Open a file first (step 1).")
            return
        w = tk.Toplevel(root)
        w.title("Edit strings")
        w.geometry("780x560")
        autofit(w, 780, 560)
        ttk.Label(w, text="Names stored in the file (items, weapons, places...). They live in fixed slots "
                          "(usually 32 or 64 bytes).\n"
                          "Pick a row, type the new name (e.g. a Weapon_… string found in the NUP archive) "
                          "and press Write. A backup is created every time.\n"
                          "The new text must fit in the slot (max length shown). Use only ASCII characters.",
                  justify="left").pack(anchor="w", padx=12, pady=8)
        q = tk.StringVar()
        r = ttk.Frame(w)
        r.pack(fill="x", padx=12)
        ttk.Label(r, text="Filter:").pack(side="left")
        ttk.Entry(r, textvariable=q, width=30).pack(side="left", padx=6)
        cnt = ttk.Label(r, text="")
        cnt.pack(side="left", padx=8)
        tv3 = ttk.Treeview(w, columns=("off", "slot", "txt"), show="headings", height=16)
        tv3.heading("off", text="Position")
        tv3.heading("slot", text="Max chars")
        tv3.heading("txt", text="Current text")
        tv3.column("off", width=100)
        tv3.column("slot", width=80)
        tv3.column("txt", width=520)
        pack_scroll(tv3, padx=(12, 0), pady=4)

        edit_row = ttk.Frame(w)
        edit_row.pack(fill="x", padx=12, pady=8)
        ttk.Label(edit_row, text="New text:").pack(side="left")
        new_str = tk.StringVar()
        ent = ttk.Entry(edit_row, textvariable=new_str, width=48)
        ent.pack(side="left", padx=6)
        slot_info = ttk.Label(edit_row, text="")
        slot_info.pack(side="left", padx=8)

        # (offset, text, slot_size) — refreshed after each write
        slots = {"list": find_string_slots(st["data"])}

        def fill(*_):
            tv3.delete(*tv3.get_children())
            f = q.get().strip().lower()
            n = 0
            for o, t, slot in slots["list"]:
                if f in t.lower():
                    tv3.insert("", "end", iid=str(o), values=(f"0x{o:X}", slot - 1, t))
                    n += 1
            cnt.config(text=f"{n} found")

        def on_select(_=None):
            sel = tv3.selection()
            if not sel:
                return
            o = int(sel[0])
            for off, t, slot in slots["list"]:
                if off == o:
                    new_str.set(t)
                    slot_info.config(text=f"(max {slot - 1} characters)")
                    ent.icursor("end")
                    ent.focus_set()
                    return

        def write_str():
            sel = tv3.selection()
            if not sel:
                messagebox.showinfo("Editor", "Select a string from the list first.", parent=w)
                return
            o = int(sel[0])
            entry = next(((off, t, slot) for off, t, slot in slots["list"] if off == o), None)
            if entry is None:
                messagebox.showerror("Editor", "Could not find that string in the file.", parent=w)
                return
            _off, old, slot = entry
            text = new_str.get()
            if not text:
                messagebox.showinfo("Editor", "Type the new text.", parent=w)
                return
            if len(text.encode("ascii", "replace")) + 1 > slot:
                messagebox.showerror("Editor",
                                     f"Text is too long. This slot holds at most {slot - 1} characters.",
                                     parent=w)
                return
            try:
                text.encode("ascii")
            except UnicodeEncodeError:
                messagebox.showerror("Editor", "Use only ASCII characters (letters, digits, _ - etc.).", parent=w)
                return
            if not ask_yesno("Confirm",
                                       f"Replace:\n  {old}\nwith:\n  {text}\n\n"
                                       f"at 0x{o:X} (slot {slot} bytes)?\nThe game must be closed.",
                                       parent=w):
                return
            try:
                backup_path = write_string(st["path"], st["data"], o, slot, text)
            except ValueError as e:
                messagebox.showerror("Editor", str(e), parent=w)
                return
            st["idx"] = index_hash(st["data"])
            slots["list"] = find_string_slots(st["data"])
            fill()
            # re-select the same offset if still present
            if tv3.exists(str(o)):
                tv3.selection_set(str(o))
                tv3.see(str(o))
            mod_time = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(st["path"])))
            messagebox.showinfo("Done",
                                f"String written to:\n{st['path']}\n(modified at {mod_time})\n\n"
                                f"Backup of the previous version:\n{backup_path}",
                                parent=w)

        tv3.bind("<<TreeviewSelect>>", on_select)
        tv3.bind("<Double-1>", lambda e: (on_select(), ent.focus_set()))
        ttk.Button(edit_row, text="Write", command=write_str).pack(side="left", padx=6)
        q.trace_add("write", fill)
        fill()

    # ---- every value of the file: browse, name, edit (also many at once), export
    def names_map():
        return names_for(st["names"], guess_title(st["path"], st["idx"])) if st["path"] else {}

    def name_db():
        return st["names"].setdefault(guess_title(st["path"], st["idx"]), {"algo": None, "map": {}}).setdefault("map", {})

    def values_window():
        if st["data"] is None:
            messagebox.showinfo("Editor", "Open a file first (step 1).")
            return
        w = tk.Toplevel(root)
        w.title("All values in this file")
        w.geometry("1100x700")
        autofit(w, 1100, 700)
        ttk.Label(w, text="Every value stored in this save, with its key. To understand an unknown one: do ONE thing "
                          "in the game, save, and use 'Compare two saves'.\n"
                          "Names appear when you load a list of names taken from the game (NUP archive > Search > "
                          "Save list...) or when you name keys yourself. Select several rows to change them all "
                          "at once (e.g. every inventory quantity).",
                  justify="left", wraplength=1060).pack(anchor="w", padx=12, pady=6)

        E = {"list": table_entries(st["data"])}
        q = tk.StringVar()
        only_named, group = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        newv, tkind = tk.StringVar(), tk.StringVar(value="Whole number")
        TYPES = {"Whole number": "int32", "Decimal number": "float32",
                 "Whole number (unsigned)": "uint32", "Raw hex (0x...)": "hex"}

        top = ttk.Frame(w)
        top.pack(fill="x", padx=12)
        ttk.Label(top, text="Filter (name, key or number):").pack(side="left")
        ttk.Entry(top, textvariable=q, width=26).pack(side="left", padx=6)
        ttk.Checkbutton(top, text="Only named", variable=only_named, command=lambda: fill()).pack(side="left", padx=6)
        ttk.Checkbutton(top, text="Group by name", variable=group, command=lambda: fill()).pack(side="left", padx=6)
        cnt = ttk.Label(top, text="")
        cnt.pack(side="left", padx=8)

        # bottom bars first, so the list can take all the remaining space
        tools = ttk.Frame(w)
        tools.pack(side="bottom", fill="x", padx=12, pady=(0, 8))
        edit = ttk.Frame(w)
        edit.pack(side="bottom", fill="x", padx=12, pady=6)
        mid = ttk.Frame(w)
        mid.pack(fill="both", expand=True, padx=12, pady=4)

        cols = ("off", "key", "name", "int", "float", "hex")
        tvw = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="extended", height=18)
        tvw.heading("#0", text="Group")
        tvw.column("#0", width=150)
        for c, t, wd in (("off", "Position", 80), ("key", "Key", 90), ("name", "Name", 270),
                         ("int", "Whole", 110), ("float", "Decimal", 110), ("hex", "Raw (hex)", 100)):
            tvw.heading(c, text=t)
            tvw.column(c, width=wd)
        sbv = ttk.Scrollbar(mid, orient="vertical", command=tvw.yview)
        tvw.configure(yscrollcommand=sbv.set)
        sbv.pack(side="right", fill="y")
        tvw.pack(fill="both", expand=True)

        def match_row(t, nm, k, i, f):
            if not t:
                return True
            if t in nm.lower():
                return True
            kk = t[2:] if t.startswith("0x") else t
            if len(kk) >= 5 and kk in f"{k:08x}":
                return True
            try:
                x = float(t.replace(",", "."))
            except ValueError:
                return False
            return i == x or (f == f and abs(f - x) <= 0.01)

        def fill(*_):
            tvw.delete(*tvw.get_children())
            t = q.get().strip().lower()
            nmap = names_map()
            limit, matched, groups = 20000, 0, {}
            for vo, k, raw in E["list"]:
                nm = nmap.get(f"{k:08X}", "")
                if only_named.get() and not nm:
                    continue
                i, f = raw_views(raw)
                if not match_row(t, nm, k, i, f):
                    continue
                matched += 1
                if matched > limit:
                    continue
                parent = ""
                if group.get():
                    g = (re.sub(r"[_\-.\[]?\d+\]?$", "", nm) or nm) if nm else "(unnamed)"
                    if g not in groups:
                        groups[g] = f"g:{g}"
                        tvw.insert("", "end", iid=groups[g], text=g, values=("", "", "", "", "", ""))
                    parent = groups[g]
                tvw.insert(parent, "end", iid=str(vo),
                           values=(f"0x{vo:X}", f"{k:08X}", nm, i, fmt_float(f), f"0x{raw:08X}"))
            for g, iid in groups.items():
                tvw.item(iid, text=f"{g}  ({len(tvw.get_children(iid))})")
            cnt.config(text=f"{matched} shown" + (f" (first {limit} drawn)" if matched > limit else ""))

        def selected_positions():
            out = []
            for iid in tvw.selection():
                out += [int(c) for c in tvw.get_children(iid)] if iid.startswith("g:") else [int(iid)]
            return list(dict.fromkeys(out))

        def key_at(vo):
            return struct.unpack_from("<I", st["data"], vo - 4)[0]

        def reload_entries():
            st["idx"] = index_hash(st["data"])
            E["list"] = table_entries(st["data"])
            fill()
            refresh()

        def write_selected():
            pos = selected_positions()
            if not pos:
                messagebox.showinfo("Editor", "Select one or more rows first.", parent=w)
                return
            try:
                raw = encode_value(newv.get(), TYPES[tkind.get()])
            except ValueError as e:
                messagebox.showerror("Editor", str(e) if "range" in str(e) else "Type a valid value.", parent=w)
                return
            if not ask_yesno("Confirm", f"Write {newv.get().strip()} into {len(pos)} value(s)?\n"
                                                  "The game must be closed.", parent=w):
                return
            bp = write_raws(st["path"], st["data"], [(o, raw) for o in pos])
            reload_entries()
            messagebox.showinfo("Done", f"{len(pos)} value(s) written.\n\nBackup of the previous version:\n{bp}",
                                parent=w)

        def name_selected():
            pos = selected_positions()
            if len(pos) != 1:
                messagebox.showinfo("Editor", "Select exactly one row to name.", parent=w)
                return
            h = f"{key_at(pos[0]):08X}"
            nm = ask_text(w, "Name", "Name this key", detail=f"Key {h}", prompt="Name:",
                          initial=names_map().get(h, ""), ok_text="Save")
            if nm is None:
                return
            if nm.strip():
                name_db()[h] = nm.strip()
            else:
                name_db().pop(h, None)
            save_names_db(st["names"])
            fill()

        def add_to_main():
            pos = selected_positions()
            if not pos:
                messagebox.showinfo("Editor", "Select one or more rows first.", parent=w)
                return
            dec = ask_yesno("Type", "Are these decimal numbers (with a comma)?\n"
                                              "Choose No for whole numbers.", parent=w)
            nmap = names_map()
            fields, added, skipped = file_fields(), 0, 0
            for vo in pos:
                k = key_at(vo)
                entry = {"kind": "float32" if dec else "int32"}
                if st["idx"].get(k) == vo - 4:
                    entry["hash"] = k
                else:
                    entry["offset"] = vo
                if any(entry_ref(c) == entry_ref(entry) for c in fields.values()):
                    skipped += 1  # that key is already in the list (under some name)
                    continue
                label = nmap.get(f"{k:08X}") or f"Entry {k:08X}"
                if label in fields:  # same name already used by another key: keep both
                    label = f"{label} [{k:08X}]"
                fields[label] = entry
                added += 1
            save_fields(st["fields"])
            refresh()
            messagebox.showinfo("Editor", f"{added} entr{'y' if added == 1 else 'ies'} added to the main list."
                                + (f"\n{skipped} skipped: already in the list." if skipped else ""), parent=w)

        def load_names():
            p = filedialog.askopenfilename(parent=w, title="Choose a text file with names (one per line)",
                                           filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
            if not p:
                return
            try:
                names = read_names_file(p)
            except OSError as e:
                messagebox.showerror("Editor", str(e), parent=w)
                return
            if not names:
                messagebox.showinfo("Editor", "No names found in that file.", parent=w)
                return
            keys = [k for _, k, _ in E["list"]]
            score = detect_hash(names, keys)
            best = max(score, key=score.get)
            if score[best] == 0:
                messagebox.showinfo("Editor", f"{len(names)} names read, but none of them matches a key of this "
                                              "file with the hash types I know.\nThe list may not contain the "
                                              "names used by the save, or the game uses another hash.\n"
                                              "You can still name keys by hand.", parent=w)
                return
            top3 = "\n".join(f"  {a}: {n}" for a, n in sorted(score.items(), key=lambda x: -x[1])[:3] if n)
            if not ask_yesno("Hash type", f"Best match: {best} ({score[best]} of {len(names)} names "
                                                    f"found in this file).\n\n{top3}\n\nUse it?", parent=w):
                return
            db = st["names"].setdefault(guess_title(st["path"], st["idx"]), {"algo": None, "map": {}})
            db["algo"] = best
            nmap, added = db.setdefault("map", {}), 0
            for h, n in resolve_names(names, best, keys).items():
                if h not in nmap:
                    nmap[h] = n
                    added += 1
            save_names_db(st["names"])
            fill()
            messagebox.showinfo("Editor", f"{added} new name(s) matched ({best}).", parent=w)

        def try_name():
            nm = ask_text(w, "Try a name", "Try a name",
                          detail="Type a name the game might use. The program hashes it with every hash type "
                                 "and tells you which keys it matches.",
                          prompt="Name:", ok_text="Try")
            if not nm or not nm.strip():
                return
            nm = nm.strip()
            keyset = {k for _, k, _ in E["list"]}
            hits = [(a, f(nm)) for a, f in HASH_ALGOS.items() if f(nm) in keyset]
            if not hits:
                messagebox.showinfo("Editor", f"'{nm}' does not produce any key present in this file "
                                              "(with the hash types I know).", parent=w)
                return
            lines = "\n".join(f"  {a}: {h:08X}" for a, h in hits)
            if ask_yesno("Found", f"'{nm}' matches:\n{lines}\n\nSave this name for those keys?", parent=w):
                for _a, h in hits:
                    name_db()[f"{h:08X}"] = nm
                save_names_db(st["names"])
                fill()

        def export_csv():
            p = filedialog.asksaveasfilename(parent=w, defaultextension=".csv",
                                             initialfile=f"{title_id(st['path'])}_{os.path.basename(st['path'])}.csv",
                                             filetypes=[("CSV file", "*.csv")])
            if not p:
                return
            nmap = names_map()
            with open(p, "w", newline="", encoding="utf-8") as f:
                wr = csv.writer(f)
                wr.writerow(["position", "key", "name", "whole", "decimal", "raw_hex"])
                for vo, k, raw in E["list"]:
                    i, fl = raw_views(raw)
                    wr.writerow([f"0x{vo:X}", f"{k:08X}", nmap.get(f"{k:08X}", ""), i, fmt_float(fl), f"0x{raw:08X}"])
            messagebox.showinfo("Editor", f"Saved {len(E['list'])} rows to:\n{p}", parent=w)

        def reload_file():
            st["data"] = read(st["path"])
            reload_entries()

        ttk.Label(edit, text="New value:").pack(side="left")
        ttk.Entry(edit, textvariable=newv, width=16).pack(side="left", padx=6)
        ttk.Combobox(edit, textvariable=tkind, width=24, values=list(TYPES), state="readonly").pack(side="left", padx=6)
        ttk.Button(edit, text="Write to selected rows", command=write_selected).pack(side="left", padx=8)
        for text, cmd in (("Name selected...", name_selected), ("Add to main list", add_to_main),
                          ("Try a name...", try_name), ("Load names list...", load_names),
                          ("Export CSV...", export_csv), ("Reload file", reload_file)):
            ttk.Button(tools, text=text, command=cmd).pack(side="left", padx=4)
        q.trace_add("write", fill)
        fill()

    def blocks_window():
        if st["data"] is None:
            messagebox.showinfo("Editor", "Open a file first (step 1).")
            return
        tid = title_id(st["path"]) if st["path"] else "generic"
        is_oot = (tid == OOT_REMAKE_ID)

        w = tk.Toplevel(root)
        w.title("Data blocks" + ("  [Ocarina of Time Remake]" if is_oot else ""))
        w.geometry("1020x700")
        autofit(w, 1020, 700)

        help_txt = (
            "HOW TO UNDERSTAND WHAT A VALUE IS\n"
            "1. Do ONE thing in the game (pick up 1 item, finish 1 quest, visit 1 place), save, then use "
            "\"Compare two saves...\". The keys that changed are almost always that thing — name them and "
            "add them to the main list.\n"
            "2. Inventory quantities (bombs, nuts, hearts, rupees…) live in the normal 32-bit table entries, "
            "not in these variable blocks. Find them with Compare.\n"
            "3. The big \"Seen / unlocked IDs\" list (OoT) is a set of event/location flags already triggered. "
            "It is NOT item counts. New entries appear when you do new story things.\n"
            "4. Load names from the game archive (NUP → Search → Save list) then \"All values → Load names list\" "
            "so keys get readable names."
        )
        if is_oot:
            help_txt += "\n\nThis file is Ocarina of Time Remake — specialised labels are active."

        ttk.Label(w, text=help_txt, justify="left", wraplength=980,
                  font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=6)

        blocks_state = {"list": infer_blocks(st["data"])}
        nmap = names_for(st["names"], tid)

        # left: list of blocks
        left = ttk.Frame(w)
        left.pack(side="left", fill="both", expand=True, padx=(12, 4), pady=4)
        ttk.Label(left, text="Blocks (variable-size data after the hash table)").pack(anchor="w")
        cols = ("start", "size", "kind", "label")
        tv = ttk.Treeview(left, columns=cols, show="headings", height=20, selectmode="browse")
        for c, t, wd in (("start", "Start", 90), ("size", "Size", 70),
                         ("kind", "Type", 90), ("label", "What it is", 340)):
            tv.heading(c, text=t)
            tv.column(c, width=wd)
        sb = ttk.Scrollbar(left, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(fill="both", expand=True)

        # right: detail / list editor
        right = ttk.Frame(w)
        right.pack(side="right", fill="both", expand=True, padx=(4, 12), pady=4)
        detail = ttk.Label(right, text="Select a block on the left", justify="left", wraplength=420)
        detail.pack(anchor="w", pady=(0, 4))

        ttk.Label(right, text="Entries of the selected list (high 32 : low 32). "
                              "If a half matches a named key it is shown.").pack(anchor="w")
        # tree for entries so we can show name column
        ecols = ("idx", "full", "hi", "lo", "name")
        etv = ttk.Treeview(right, columns=ecols, show="headings", height=16, selectmode="extended")
        for c, t, wd in (("idx", "#", 40), ("full", "Full u64", 150), ("hi", "High", 90),
                         ("lo", "Low", 90), ("name", "Known name?", 160)):
            etv.heading(c, text=t)
            etv.column(c, width=wd)
        esb = ttk.Scrollbar(right, orient="vertical", command=etv.yview)
        etv.configure(yscrollcommand=esb.set)
        esb.pack(side="right", fill="y")
        etv.pack(fill="both", expand=True)

        entry_row = ttk.Frame(right)
        entry_row.pack(fill="x", pady=4)
        new_hex = tk.StringVar()
        ttk.Label(entry_row, text="New entry (hex):").pack(side="left")
        ttk.Entry(entry_row, textvariable=new_hex, width=20).pack(side="left", padx=4)
        ttk.Label(entry_row, text="e.g. 11867CB902B55CED").pack(side="left")

        def resolve_half(v32):
            h = f"{v32:08X}"
            return nmap.get(h, "")

        def refresh_blocks():
            tv.delete(*tv.get_children())
            blocks_state["list"] = infer_blocks(st["data"])
            for i, b in enumerate(blocks_state["list"]):
                label = b["label"]
                if not label and b["key"] is not None:
                    label = f"key {b['key']:08X}"
                    nm = nmap.get(f"{b['key']:08X}", "")
                    if nm:
                        label = nm
                tv.insert("", "end", iid=str(i), values=(
                    f"0x{b['start']:X}", b["size"], b["kind"], label))

        def on_select(_=None):
            sel = tv.selection()
            etv.delete(*etv.get_children())
            if not sel:
                detail.config(text="Select a block on the left")
                return
            b = blocks_state["list"][int(sel[0])]
            keys = ", ".join(f"{k:08X}" for k in b["keys"]) or "—"
            extra = ""
            if b["kind"] == "u64_list" and b.get("key") == 0x70D7C5CB:
                extra = ("\nThis is the set of story/location/event flags already seen or unlocked. "
                         "Not inventory counts. New IDs appear when you do new things in the story.")
            detail.config(text=f"Start 0x{b['start']:X}   size {b['size']} bytes   keys: {keys}\n"
                               f"{b['label']}{extra}")
            if b["kind"] == "u64_list":
                _, vals = read_u64_list(st["data"], b["start"])
                for i, v in enumerate(vals):
                    hi = (v >> 32) & 0xFFFFFFFF
                    lo = v & 0xFFFFFFFF
                    nm = resolve_half(lo) or resolve_half(hi) or ""
                    etv.insert("", "end", iid=str(i), values=(
                        i, f"{v:016X}", f"{hi:08X}", f"{lo:08X}", nm))
            else:
                etv.insert("", "end", values=("", "(not a u64 list — use All values / Compare to inspect)", "", "", ""))

        def selected_entry_indices():
            return [int(i) for i in etv.selection()]

        def do_remove():
            sel = tv.selection()
            if not sel:
                return
            b = blocks_state["list"][int(sel[0])]
            if b["kind"] != "u64_list":
                messagebox.showinfo("Editor", "Only u64 lists can be edited here for now.", parent=w)
                return
            idxs = selected_entry_indices()
            if not idxs:
                messagebox.showinfo("Editor", "Select one or more rows in the list to remove.", parent=w)
                return
            _, vals = read_u64_list(st["data"], b["start"])
            for i in sorted(idxs, reverse=True):
                if 0 <= i < len(vals):
                    del vals[i]
            if not ask_yesno("Confirm",
                                       f"Remove {len(idxs)} entr{'y' if len(idxs)==1 else 'ies'}?\n"
                                       "Size and later pointers will be updated automatically.\n"
                                       "The game must be closed.", parent=w):
                return
            try:
                new_data, bp = set_u64_list(st["path"], st["data"], b["start"], vals)
            except Exception as e:
                messagebox.showerror("Editor", str(e), parent=w)
                return
            st["data"] = new_data
            st["idx"] = index_hash(st["data"])
            refresh_blocks()
            if tv.exists(sel[0]):
                tv.selection_set(sel[0])
                on_select()
            messagebox.showinfo("Done", f"List updated.\nBackup:\n{bp}", parent=w)

        def do_add():
            sel = tv.selection()
            if not sel:
                return
            b = blocks_state["list"][int(sel[0])]
            if b["kind"] != "u64_list":
                messagebox.showinfo("Editor", "Only u64 lists can be edited here for now.", parent=w)
                return
            t = new_hex.get().strip().replace(" ", "").replace("0x", "")
            try:
                v = int(t, 16) & 0xFFFFFFFFFFFFFFFF
            except ValueError:
                messagebox.showerror("Editor", "Type a valid 64-bit hex value.", parent=w)
                return
            _, vals = read_u64_list(st["data"], b["start"])
            vals.append(v)
            if not ask_yesno("Confirm",
                                       f"Append 0x{v:016X} to the list?\n"
                                       "Size and later pointers will be updated automatically.\n"
                                       "The game must be closed.", parent=w):
                return
            try:
                new_data, bp = set_u64_list(st["path"], st["data"], b["start"], vals)
            except Exception as e:
                messagebox.showerror("Editor", str(e), parent=w)
                return
            st["data"] = new_data
            st["idx"] = index_hash(st["data"])
            refresh_blocks()
            if tv.exists(sel[0]):
                tv.selection_set(sel[0])
                on_select()
            new_hex.set("")
            messagebox.showinfo("Done", f"Entry added.\nBackup:\n{bp}", parent=w)

        def copy_selected_entries():
            idxs = selected_entry_indices()
            if not idxs:
                messagebox.showinfo("Editor", "Select rows first.", parent=w)
                return
            lines = []
            for i in idxs:
                vals = etv.item(str(i), "values")
                lines.append("\t".join(vals))
            text = "\n".join(lines)
            w.clipboard_clear()
            w.clipboard_append(text)
            w.update()
            messagebox.showinfo("Editor", f"Copied {len(idxs)} row(s).", parent=w)

        btn_row = ttk.Frame(right)
        btn_row.pack(fill="x", pady=6)
        ttk.Button(btn_row, text="Remove selected", command=do_remove).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Add entry", command=do_add).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Copy selected", command=copy_selected_entries).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Refresh", command=lambda: (refresh_blocks(), on_select())).pack(side="right", padx=3)

        tv.bind("<<TreeviewSelect>>", on_select)
        refresh_blocks()

    def names_resolver():
        """Context for the 'Find names' button of the archive browser (None if no save is open)."""
        if st["data"] is None or st["path"] is None or not st["idx"]:
            return None
        tid = guess_title(st["path"], st["idx"])
        keys = set(st["idx"].keys())
        algo = st["names"].get(tid, {}).get("algo")
        if algo not in HASH_ALGOS:
            known = []
            for v in names_for(st["names"], tid).values():
                m = re.search(r"\(([^()]+)\)$", v)
                known.append(m.group(1) if m else v)
            score = detect_hash(known, keys) if known else {}
            algo = max(score, key=score.get) if score and max(score.values()) > 0 else "Murmur3"

        def apply(found):
            db = st["names"].setdefault(tid, {"algo": None, "map": {}})
            if not db.get("algo"):
                db["algo"] = algo
            nmap, have, added = db.setdefault("map", {}), names_for(st["names"], tid), 0
            for h, n in found.items():
                if h not in have:
                    nmap[h] = n
                    added += 1
            save_names_db(st["names"])
            return added

        known_names = []
        for v in names_for(st["names"], tid).values():
            m = re.search(r"\(([^()]+)\)$", v)
            known_names.append(m.group(1) if m else v)
        # keys that already have a name (yours, built in, or found earlier) are not searched again
        named = set()
        for h in names_for(st["names"], tid):
            try:
                named.add(int(h, 16))
            except ValueError:
                pass
        todo = keys - named
        return {"keys": todo, "all_keys": len(keys), "already": len(keys & named), "algo": algo,
                "apply": apply, "templates": learn_templates(known_names)}

    def json_data(kind):
        return st["names"] if kind == "names" else st["fields"]

    def json_store(kind):
        (save_names_db if kind == "names" else save_fields)(json_data(kind))
        refresh()

    def json_backup(kind):
        f = JSON_KINDS[kind]["file"]
        if os.path.exists(f):
            backup(f)  # goes in the save_backups folder like every other backup

    def json_read(kind, parent, title):
        path = filedialog.askopenfilename(parent=parent, title=title, initialdir=start_dir(),
                                          filetypes=[("JSON file", "*.json"), ("All files", "*.*")])
        if not path:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                obj = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror("Editor", f"This is not a valid JSON file:\n{e}", parent=parent)
            return None
        err = json_validate(kind, obj)
        if err:
            messagebox.showerror("Editor", f"This file does not look like {os.path.basename(JSON_KINDS[kind]['file'])}.\n{err}",
                                 parent=parent)
            return None
        return obj

    def json_export(kind, parent):
        spec = JSON_KINDS[kind]
        data = json_data(kind)
        if not data:
            messagebox.showinfo("Editor", f"There is nothing to export yet ({spec['label'].lower()} is empty).", parent=parent)
            return
        path = filedialog.asksaveasfilename(parent=parent, title=f"Export {spec['label'].lower()}",
                                            defaultextension=".json", initialfile=os.path.basename(spec["file"]),
                                            filetypes=[("JSON file", "*.json")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1 if kind == "names" else 2, ensure_ascii=False, sort_keys=(kind == "names"))
        except OSError as e:
            messagebox.showerror("Editor", str(e), parent=parent)
            return
        n = sum(1 for _ in json_flatten(data, spec["depth"]))
        messagebox.showinfo("Editor", f"Exported {n} items to:\n{path}", parent=parent)

    def alive(widget):
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def json_replace_confirm(kind, obj, parent):
        spec = JSON_KINDS[kind]
        n_new = sum(1 for _ in json_flatten(obj, spec["depth"]))
        n_old = sum(1 for _ in json_flatten(json_data(kind), spec["depth"]))
        return ask_yesno(
            "Replace?",
            f"This REPLACES your current {spec['label'].lower()} ({n_old} items) with the new one ({n_new} items).\n"
            "Anything that is only in yours is lost (a backup is made first).\n\n"
            "To keep yours and only add what is missing, use 'Merge' instead.\n\nReplace?",
            icon="warning", parent=parent)

    def json_replace(kind, obj):
        json_backup(kind)
        data = json_data(kind)
        data.clear()
        data.update(obj)
        json_store(kind)

    def json_import(kind, parent):
        obj = json_read(kind, parent, f"Import {JSON_KINDS[kind]['label'].lower()} (replaces yours)")
        if obj is None or not json_replace_confirm(kind, obj, parent):
            return
        json_replace(kind, obj)
        messagebox.showinfo("Editor", "Imported. A backup of the previous file is in the save_backups folder.",
                            parent=parent)

    def json_merge_window(kind, rows, same, parent):
        """Preview of the differences; only the ticked rows are added/replaced. Modal."""
        spec = JSON_KINDS[kind]
        n_new = sum(1 for r in rows if r[0] == "new")
        n_diff = len(rows) - n_new
        d = tk.Toplevel(root)
        d.title(f"Merge {spec['label'].lower()}")
        d.transient(parent)
        ttk.Label(d, justify="left", wraplength=840,
                  text=f"{n_new} items are missing in yours (ticked: they will be added).  "
                       f"{n_diff} exist in both with a different value (not ticked: yours is kept unless you tick them).  "
                       f"{same} are identical.\nNothing of yours is ever removed. Click a row (or press Space) to tick/untick it."
                  ).pack(anchor="w", padx=12, pady=(10, 4))
        cols = ("take", "state", "where", "mine", "theirs")
        fr = ttk.Frame(d)
        fr.pack(fill="both", expand=True, padx=12, pady=4)
        tv = ttk.Treeview(fr, columns=cols, show="headings", selectmode="extended", height=16)
        for c, t, wd in (("take", "Add", 46), ("state", "Status", 80), ("where", "Where", 330),
                         ("mine", "Yours", 190), ("theirs", "In the file", 190)):
            tv.heading(c, text=t)
            tv.column(c, width=wd, anchor="center" if c in ("take", "state") else "w", stretch=(c == "where"))
        chosen = set()
        for i, (state, path, old, new) in enumerate(rows):
            if state == "new":
                chosen.add(i)
            tv.insert("", "end", iid=str(i), values=(
                "☑" if state == "new" else "☐", "New" if state == "new" else "Different",
                json_where(kind, path), "" if state == "new" else json_short(old), json_short(new)))
        pack_scroll(tv)

        def mark(i):
            tv.set(str(i), "take", "☑" if i in chosen else "☐")
            cnt.config(text=f"{len(chosen)} selected")

        def toggle(i):
            chosen.symmetric_difference_update({i})
            mark(i)

        def on_click(e):
            iid = tv.identify_row(e.y)
            if iid and tv.identify_region(e.x, e.y) == "cell":
                toggle(int(iid))
                return "break"

        def on_space(_e):
            for iid in tv.selection():
                toggle(int(iid))
            return "break"

        def pick(fn):
            chosen.clear()
            chosen.update(i for i, r in enumerate(rows) if fn(r))
            for i in range(len(rows)):
                mark(i)

        def do_merge():
            take = [rows[i] for i in sorted(chosen)]
            if not take:
                messagebox.showinfo("Editor", "Nothing is ticked.", parent=d)
                return
            json_backup(kind)
            json_apply(json_data(kind), take)
            json_store(kind)
            a = sum(1 for r in take if r[0] == "new")
            messagebox.showinfo("Editor", f"Merged: {a} added, {len(take) - a} replaced with the new value.\n"
                                          "Everything else of yours was left as it was (backup made first).", parent=parent)
            d.destroy()

        tv.bind("<Button-1>", on_click)
        tv.bind("<space>", on_space)
        bar = ttk.Frame(d)
        bar.pack(fill="x", padx=12, pady=(2, 10))
        cnt = ttk.Label(bar, text=f"{len(chosen)} selected")
        ttk.Button(bar, text="Merge ticked", style="Accent.TButton", command=do_merge).pack(side="right")
        ttk.Button(bar, text="Cancel", command=d.destroy).pack(side="right", padx=6)
        ttk.Button(bar, text="Only new", command=lambda: pick(lambda r: r[0] == "new")).pack(side="left")
        ttk.Button(bar, text="All", command=lambda: pick(lambda r: True)).pack(side="left", padx=6)
        ttk.Button(bar, text="None", command=lambda: pick(lambda r: False)).pack(side="left")
        cnt.pack(side="left", padx=12)
        autofit(d, 900, 560)
        try:
            d.wait_visibility()
            d.grab_set()
        except tk.TclError:
            pass
        d.wait_window()

    def json_merge(kind, parent):
        spec = JSON_KINDS[kind]
        obj = json_read(kind, parent, f"Choose the {spec['label'].lower()} file to merge into yours")
        if obj is None:
            return
        rows, same = json_diff(kind, json_data(kind), obj)
        if not rows:
            messagebox.showinfo("Editor", f"Nothing to add: yours already contains everything in that file "
                                          f"({same} identical items).", parent=parent)
            return
        json_merge_window(kind, rows, same, parent)

    def json_github_choice(kind, obj, parent):
        """The file was downloaded: let the user pick between comparing/merging and a full replace."""
        spec = JSON_KINDS[kind]
        rows, same = json_diff(kind, json_data(kind), obj)
        total = sum(1 for _ in json_flatten(obj, spec["depth"]))
        if not rows:
            messagebox.showinfo("Editor", f"{spec['label']}: you are up to date. Yours already contains all "
                                          f"{total} items of the GitHub version.", parent=parent)
            return
        n_new = sum(1 for r in rows if r[0] == "new")
        d = tk.Toplevel(root)
        d.title(f"{spec['label']} from GitHub")
        d.transient(parent)
        ttk.Label(d, justify="left", wraplength=470,
                  text=f"Downloaded {spec['remote']} from GitHub ({total} items).\n\n"
                       f"Compared with yours: {n_new} missing in yours, {len(rows) - n_new} with a different value, "
                       f"{same} identical.\n\nWhat do you want to do?").pack(anchor="w", padx=16, pady=(14, 8))

        def compare():
            d.destroy()
            json_merge_window(kind, rows, same, parent)

        def replace():
            d.destroy()
            if json_replace_confirm(kind, obj, parent):
                json_replace(kind, obj)
                messagebox.showinfo("Editor", "Replaced with the GitHub version. A backup of your previous file is in "
                                              "the save_backups folder.", parent=parent)

        ttk.Button(d, text="See the differences and add what is missing (recommended)", style="Accent.TButton",
                   command=compare).pack(fill="x", padx=16, pady=3)
        ttk.Button(d, text="Replace mine with the GitHub version", style="Danger.TButton",
                   command=replace).pack(fill="x", padx=16, pady=3)
        ttk.Button(d, text="Cancel", command=d.destroy).pack(fill="x", padx=16, pady=(3, 14))
        autofit(d, 500, 250)
        try:
            d.wait_visibility()
            d.grab_set()
        except tk.TclError:
            pass
        d.wait_window()

    def json_github(kinds, parent, lbl, btns, replace=False):
        """Download in the background (the window stays usable), then handle each file in turn."""
        out = queue.Queue()

        def work():
            for k in kinds:
                try:
                    out.put((k, fetch_remote_json(JSON_KINDS[k]["remote"]), None))
                except Exception as e:
                    out.put((k, None, str(e)))
            out.put(None)

        for b in btns:
            b.state(["disabled"])
        lbl.config(text="Downloading from GitHub...")
        threading.Thread(target=work, daemon=True).start()
        results = []

        def finish():
            if not alive(parent):
                return
            for b in btns:
                b.state(["!disabled"])
            lbl.config(text="")
            for k, obj, err in results:
                name = JSON_KINDS[k]["label"]
                if err is None:
                    err = json_validate(k, obj)
                    if err:
                        err = f"The downloaded file does not look right. {err}"
                if err:
                    messagebox.showerror("Editor", f"{name}: {err}", parent=parent)
                    continue
                if replace:
                    if json_replace_confirm(k, obj, parent):
                        json_replace(k, obj)
                        messagebox.showinfo("Editor", f"{name}: imported from GitHub (replaced). A backup of your "
                                                      "previous file is in the save_backups folder.", parent=parent)
                else:
                    json_github_choice(k, obj, parent)

        def poll():
            try:
                while True:
                    m = out.get_nowait()
                    if m is None:
                        finish()
                        return
                    results.append(m)
            except queue.Empty:
                pass
            if alive(parent):
                parent.after(100, poll)

        parent.after(100, poll)

    def json_window():
        w = tk.Toplevel(root)
        w.title("Import / export / merge data files")
        w.transient(root)
        ttk.Label(w, wraplength=560, justify="left",
                  text="Share your data files, move them to another PC or get the project's ready-made saved fields from GitHub.\n"
                       "Export = save a copy.  Merge = look at the differences with another file and add only what "
                       "you are missing (yours is never overwritten).  Import = replace yours with the file.  "
                       "Update from GitHub (Saved fields only) = download the project's file and choose between merging and replacing."
                  ).pack(anchor="w", padx=14, pady=(12, 4))
        gh_btns = []
        lbl = None  # created below, inside the bottom bar
        for kind in ("names", "fields"):
            spec = JSON_KINDS[kind]
            box = ttk.Frame(w, style="Card.TFrame")
            box.pack(fill="x", padx=14, pady=6)
            ttk.Label(box, text=f"{spec['label']}  ({os.path.basename(spec['file'])})", style="Card.TLabel",
                      font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
            ttk.Label(box, text=spec["help"], style="CardMuted.TLabel", wraplength=540,
                      justify="left").pack(anchor="w", padx=12, pady=(2, 6))
            b = ttk.Frame(box, style="Card.TFrame")
            b.pack(fill="x", padx=12, pady=(0, 10))
            ttk.Button(b, text="Export...", command=lambda k=kind: json_export(k, w)).pack(side="left")
            ttk.Button(b, text="Merge...", style="Accent.TButton",
                       command=lambda k=kind: json_merge(k, w)).pack(side="left", padx=6)
            ttk.Button(b, text="Import (replace)...", style="Danger.TButton",
                       command=lambda k=kind: json_import(k, w)).pack(side="left")
            if spec.get("remote"):  # GitHub buttons only for the files published there
                gb = ttk.Button(b, text="Update from GitHub...",
                                command=lambda k=kind: json_github([k], w, lbl, gh_btns))
                gb.pack(side="right")
                gh_btns.append(gb)
                ib = ttk.Button(b, text="Import from GitHub", style="Danger.TButton",
                                command=lambda k=kind: json_github([k], w, lbl, gh_btns, replace=True))
                ib.pack(side="right", padx=6)
                gh_btns.append(ib)
        bottom = ttk.Frame(w)
        bottom.pack(fill="x", padx=14, pady=(4, 12))
        lbl = ttk.Label(bottom, text="")
        lbl.pack(side="left", padx=10)
        ttk.Button(bottom, text="Close", command=w.destroy).pack(side="right")
        autofit(w, 620, 400)

    def restore_window():
        if st["path"] is None:
            messagebox.showinfo("Editor", "Open a file first (step 1).")
            return
        baks = list_backups(st["path"])
        if not baks:
            messagebox.showinfo("Editor", "There are no backups for this file yet.")
            return
        w = tk.Toplevel(root)
        w.title("Restore a backup")
        w.geometry("620x380")
        autofit(w, 620, 380)
        ttk.Label(w, text="Backups of this file, newest first. Restoring puts the chosen version back "
                          "(the current one is backed up first). The game must be closed.",
                  wraplength=580, justify="left").pack(anchor="w", padx=12, pady=8)
        lb = tk.Listbox(w, height=12)
        for b in baks:
            m = re.search(r"\.(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", os.path.basename(b))
            when = f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}:{m[6]}" if m else os.path.basename(b)
            lb.insert("end", f"{when}    ({human_size(os.path.getsize(b))})")
        lb.pack(fill="both", expand=True, padx=12, pady=4)

        def do():
            sel = lb.curselection()
            if not sel:
                messagebox.showinfo("Editor", "Choose a backup from the list.", parent=w)
                return
            if not ask_yesno("Confirm", "Restore this backup over the current file?", parent=w):
                return
            restore_backup(st["path"], baks[sel[0]])
            st["data"] = read(st["path"])
            st["idx"] = index_hash(st["data"])
            refresh()
            messagebox.showinfo("Done", "Backup restored.", parent=w)
            w.destroy()

        ttk.Button(w, text="Restore selected", command=do).pack(pady=8)

    ttk.Button(extra, text="📂  Open file...", style="Accent.TButton", command=open_file).pack(side="left", padx=(0, 10))
    ttk.Button(extra, text="All values...", command=values_window).pack(side="left", padx=3)
    ttk.Button(extra, text="Data blocks...", command=blocks_window).pack(side="left", padx=3)
    ttk.Button(extra, text="Compare saves...", command=compare_window).pack(side="left", padx=3)
    ttk.Button(extra, text="Edit strings...", command=strings_window).pack(side="left", padx=3)
    ttk.Button(extra, text="Restore backup...", command=restore_window).pack(side="left", padx=3)
    ttk.Button(extra, text="JSON data...", command=json_window).pack(side="left", padx=3)
    ttk.Button(extra, text="Game archive / folder...", command=lambda: open_nup_browser(
        root, tk, ttk, filedialog, messagebox, start_dir(), names_resolver)).pack(side="right", padx=3)
    ttk.Button(row, text="Change", style="Accent.TButton", command=change_value).pack(side="left", padx=4)
    ttk.Button(row, text="Add by name...", command=add_by_name).pack(side="right", padx=4)
    ttk.Button(row, text="Add new entry...", command=wizard).pack(side="right", padx=4)
    ttk.Button(row, text="Remove entry", style="Danger.TButton", command=remove_entry).pack(side="right", padx=4)
    ttk.Button(row, text="Rename...", command=rename_entry).pack(side="right", padx=4)

    # shortcuts and comfort
    def on_row_pick(_=None):
        sel = tv.selection()
        if sel:
            new_value.set(tv.item(sel[0], "values")[1])
            new_entry.focus_set()
            new_entry.select_range(0, "end")

    tv.bind("<Double-1>", on_row_pick)
    new_entry.bind("<Return>", lambda e: change_value())
    root.bind("<Control-o>", lambda e: open_file())
    root.bind("<Control-f>", lambda e: (search_entry.focus_set(), search_entry.select_range(0, "end")))
    root.bind("<F2>", lambda e: rename_entry())
    root.bind("<Delete>", lambda e: remove_entry() if root.focus_get() is tv else None)
    ttk.Label(row, text="double-click a row to edit · Enter to apply", style="CardMuted.TLabel").pack(side="left", padx=10)
    root.mainloop()


if __name__ == "__main__":
    gui()
