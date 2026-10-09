"""
MetaScrub - metadata viewer and scrubber.

Dark-mode Tkinter GUI. Uses scrub_core (bundled ExifTool) to read and wipe
metadata from images, audio, video and documents, preserving ICC color
profiles across the scrub.
"""

import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import scrub_core

# ---------------------------------------------------------------------------
# Dark palette
# ---------------------------------------------------------------------------

BG        = "#1b1d21"   # window background
PANEL     = "#24262c"   # panels / frames
FIELD     = "#2e3138"   # entry / treeview fields
HOVER     = "#383c44"
FG        = "#e8e8e8"   # primary text
DIM       = "#9aa0a8"   # secondary text
ACCENT    = "#4f9cf9"   # buttons / highlights
ACCENT_DK = "#3570c4"
GOOD      = "#3ddc84"
WARN      = "#f9a44f"
BAD       = "#f26d6d"
BORDER    = "#3a3e46"


def apply_dark_titlebar(win):
    """Force the Windows title bar (and other DWM chrome) into dark mode.

    Tk's winfo_id() isn't always the decorated top-level HWND, so the
    attribute is set on both the widget HWND and its parent; setting it
    on a non-top-level window is harmless.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        wid = win.winfo_id()
        user32 = ctypes.windll.user32
        parent = user32.GetParent(wid)
        hwnds = {wid}
        if parent:
            hwnds.add(parent)
        val = ctypes.c_int(1)
        for hwnd in hwnds:
            # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Win10 20H1+ / Win11)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
            # SWP_NOMOVE|SWP_NOSIZE|SWP_FRAMECHANGED nudges DWM to repaint.
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0020)
    except Exception:
        pass


def configure_dark_style(root):
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                    bordercolor=BORDER, darkcolor=BG, lightcolor=BG)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Dim.TLabel", background=BG, foreground=DIM)
    style.configure("Panel.TLabel", background=PANEL, foreground=FG)
    style.configure("Title.TLabel", background=BG, foreground=FG,
                    font=("Segoe UI", 11, "bold"))

    style.configure("TButton", background=FIELD, foreground=FG,
                    borderwidth=1, relief="flat", padding=(10, 6),
                    font=("Segoe UI", 9))
    style.map("TButton",
              background=[("active", HOVER), ("pressed", HOVER)],
              foreground=[("disabled", DIM)])
    style.configure("Accent.TButton", background=ACCENT_DK, foreground="white")
    style.map("Accent.TButton",
              background=[("active", ACCENT), ("pressed", ACCENT_DK)],
              foreground=[("disabled", DIM)])
    style.configure("Danger.TButton", background="#7a2e2e", foreground="white")
    style.map("Danger.TButton",
              background=[("active", "#933838"), ("pressed", "#7a2e2e")])

    style.configure("TEntry", fieldbackground=FIELD, foreground=FG,
                    insertcolor=FG, borderwidth=1, relief="flat", padding=6)
    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.map("TCheckbutton", background=[("active", BG)])

    style.configure("Treeview", background=FIELD, fieldbackground=FIELD,
                    foreground=FG, borderwidth=0, relief="flat",
                    rowheight=22, font=("Segoe UI", 9))
    style.configure("Treeview.Heading", background=PANEL, foreground=DIM,
                    borderwidth=0, relief="flat",
                    font=("Segoe UI", 9, "bold"))
    style.map("Treeview",
              background=[("selected", ACCENT_DK)],
              foreground=[("selected", "white")])
    style.map("Treeview.Heading",
              background=[("active", HOVER)])

    style.configure("Vertical.TScrollbar", background=PANEL, borderwidth=0,
                    arrowcolor=DIM, troughcolor=BG)
    style.configure("Horizontal.TScrollbar", background=PANEL, borderwidth=0,
                    arrowcolor=DIM, troughcolor=BG)

    style.configure("TProgressbar", background=ACCENT, troughcolor=FIELD,
                    borderwidth=0, thickness=8)
    style.configure("TLabelframe", background=BG, foreground=DIM,
                    bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=DIM)

    style.configure("TPanedwindow", background=BG)
    return style


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class MetaScrubApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MetaScrub - Metadata Viewer & Scrubber")
        self.root.geometry("1180x760")
        self.root.minsize(980, 620)
        self.root.configure(bg=BG)
        configure_dark_style(root)

        self.q = queue.Queue()
        self.files = []            # list of dicts: path, category, size, tags, gps, thumb, status
        self.meta_cache = {}       # path -> raw tagdict
        self._iid_to_path = {}     # treeview item id -> file path
        self._meta_after_id = None # pending debounced metadata load
        self.scanning = False
        self.working = False
        self.folder = ""

        self._build_ui()
        # Dark title bar once the window exists on screen.
        self.root.after(150, lambda: apply_dark_titlebar(self.root))
        self.root.after(120, self._pump_queue)

        if not scrub_core.exiftool_available():
            messagebox.showwarning(
                "ExifTool missing",
                "Could not find exiftool.\n\n"
                "Place exiftool.exe next to MetaScrub.exe (or on PATH) and restart.\n"
                "The compile.bat build bundles it automatically.")
        self.log("Ready. Pick a folder and hit Scan.")

    # -- UI ---------------------------------------------------------------
    def _build_ui(self):
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Browse...", command=self.browse).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Scan Folder", style="Accent.TButton",
                   command=self.scan).pack(side="left", padx=2)
        ttk.Button(bar, text="Rescan",
                   command=self.recheck).pack(side="left", padx=2)
        ttk.Label(bar, text="Folder:").pack(side="left", padx=(12, 0))
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(bar, textvariable=self.folder_var, width=50)
        self.folder_entry.pack(side="left", padx=(6, 6), fill="x", expand=True)
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Include subfolders",
                        variable=self.recursive_var).pack(side="left", padx=(8, 0))

        paned = ttk.Panedwindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        # Left: file list
        left = ttk.Frame(paned, style="Panel.TFrame", padding=6)
        paned.add(left, weight=4)
        ttk.Label(left, text="Files", style="Panel.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        cols = ("file", "type", "size", "tags", "gps", "thumb", "status")
        self.file_tree = ttk.Treeview(left, columns=cols, show="headings",
                                      selectmode="extended")
        headings = [("file", "File", 220), ("type", "Type", 70),
                    ("size", "Size", 70), ("tags", "Tags", 55),
                    ("gps", "GPS", 45), ("thumb", "Thumbnail", 80),
                    ("status", "Status", 110)]
        for cid, text, w in headings:
            self.file_tree.heading(cid, text=text)
            self.file_tree.column(cid, width=w, anchor="w",
                                  stretch=(cid == "file"))
        fscroll = ttk.Scrollbar(left, orient="vertical",
                                command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=fscroll.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        fscroll.pack(side="right", fill="y")
        self.file_tree.bind("<<TreeviewSelect>>", self._on_file_select)

        # Right: metadata viewer
        right = ttk.Frame(paned, style="Panel.TFrame", padding=6)
        paned.add(right, weight=3)
        ttk.Label(right, text="Metadata", style="Panel.TLabel",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        srow = ttk.Frame(right, style="Panel.TFrame")
        srow.pack(fill="x", pady=(0, 4))
        ttk.Label(srow, text="Filter:", style="Panel.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        fentry = ttk.Entry(srow, textvariable=self.filter_var, width=30)
        fentry.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self.filter_var.trace_add("write", lambda *_: self._render_metadata())
        self.meta_title = ttk.Label(right, text="Select a file to inspect its metadata.",
                                    style="Panel.TLabel", foreground=DIM,
                                    wraplength=480)
        self.meta_title.pack(anchor="w", pady=(0, 4))
        self.meta_tree = ttk.Treeview(right, columns=("tag", "value"),
                                      show="tree headings")
        self.meta_tree.heading("#0", text="Group")
        self.meta_tree.heading("tag", text="Tag")
        self.meta_tree.heading("value", text="Value")
        self.meta_tree.column("#0", width=110, stretch=False)
        self.meta_tree.column("tag", width=170, stretch=False)
        self.meta_tree.column("value", width=200, stretch=True)
        mscroll = ttk.Scrollbar(right, orient="vertical",
                                command=self.meta_tree.yview)
        self.meta_tree.configure(yscrollcommand=mscroll.set)
        self.meta_tree.pack(side="left", fill="both", expand=True)
        mscroll.pack(side="right", fill="y")

        # Bottom: actions + progress + log
        bottom = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        bottom.pack(fill="x")
        # Left: scrub buttons, then checkbox, then Export Report (stacked)
        leftcol = ttk.Frame(bottom)
        leftcol.pack(side="left")
        scrubrow = ttk.Frame(leftcol)
        scrubrow.pack(anchor="w")
        ttk.Button(scrubrow, text="Scrub Selected", style="Danger.TButton",
                   command=self.scrub_selected).pack(side="left", padx=(0, 6))
        ttk.Button(scrubrow, text="Scrub ALL in Folder", style="Danger.TButton",
                   command=self.scrub_all).pack(side="left", padx=2)
        self.keep_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(scrubrow,
                        text="Copy to 'Scrubbed Clean' folder (keep originals)",
                        variable=self.keep_var).pack(side="left", padx=(12, 0))
        # Right: progress + status
        rightcol = ttk.Frame(bottom)
        rightcol.pack(side="right")
        self.progress = ttk.Progressbar(rightcol, mode="determinate", length=220)
        self.progress.pack(anchor="w", pady=(0, 2))
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(rightcol, textvariable=self.status_var,
                  style="Dim.TLabel").pack(anchor="w")

        logframe = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        logframe.pack(fill="x")
        self.logbox = tk.Text(logframe, height=6, bg=FIELD, fg=FG,
                              insertbackground=FG, relief="flat",
                              font=("Consolas", 9), wrap="word",
                              highlightthickness=1,
                              highlightbackground=BORDER)
        lscroll = ttk.Scrollbar(logframe, orient="vertical",
                                command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lscroll.set, state="disabled")
        self.logbox.pack(side="left", fill="x", expand=True)
        lscroll.pack(side="right", fill="y")
        logbtns = ttk.Frame(logframe)
        logbtns.pack(side="right", padx=(6, 0))
        ttk.Button(logbtns, text="Clear",
                   command=self._clear_log).pack(anchor="w", pady=(0, 4))
        ttk.Button(logbtns, text="Export Report",
                   command=self.export_report).pack(anchor="w")

    # -- log --------------------------------------------------------------
    def log(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    def set_status(self, msg):
        self.status_var.set(msg)

    # -- queue pump -------------------------------------------------------
    def _pump_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                try:
                    self._dispatch(kind, payload)
                except Exception as exc:
                    # One bad UI update must never kill the pump.
                    self.log("UI update error (%s): %s" % (kind, exc))
        except queue.Empty:
            pass
        self.root.after(120, self._pump_queue)

    def _dispatch(self, kind, payload):
        if kind == "log":
            self.log(payload)
        elif kind == "status":
            self.set_status(payload)
        elif kind == "progress":
            done, total = payload
            self.progress["maximum"] = total
            self.progress["value"] = done
        elif kind == "scan_row":
            self._add_file_row(payload)
        elif kind == "scan_done":
            self.scanning = False
            self.set_status("Idle - %d files" % len(self.files))
            self.log("Scan complete: %d supported files." % len(self.files))
        elif kind == "file_update":
            self._update_file_row(payload)
        elif kind == "refresh_meta":
            self._render_metadata()
        elif kind == "work_done":
            self.working = False
            self.set_status("Idle")
            self.progress["value"] = 0
            self.log(payload)

    # -- folder scan ------------------------------------------------------
    def browse(self):
        folder = filedialog.askdirectory(title="Choose folder to scan")
        if folder:
            self.folder_var.set(folder)

    def scan(self):
        if self.scanning or self.working:
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("No folder", "Pick a valid folder first.")
            return
        if not scrub_core.exiftool_available():
            messagebox.showerror("ExifTool missing", "Cannot scan without exiftool.")
            return
        self.folder = folder
        self.files.clear()
        self.meta_cache.clear()
        self._iid_to_path.clear()
        for iid in self.file_tree.get_children():
            self.file_tree.delete(iid)
        self.meta_tree.delete(*self.meta_tree.get_children())
        self.meta_title.configure(text="Select a file to inspect its metadata.")
        self.scanning = True
        self.set_status("Scanning...")
        self.log("Scanning %s ..." % folder)
        threading.Thread(target=self._scan_worker,
                         args=(folder, self.recursive_var.get()),
                         daemon=True).start()

    def _scan_worker(self, folder, recursive):
        try:
            paths = scrub_core.list_supported_files(folder, recursive)
            total = len(paths)
            self.q.put(("status", "Reading metadata (0/%d)..." % total))
            # Read metadata in one exiftool pass for speed.
            try:
                allmeta = scrub_core.scan_folder_metadata(folder, recursive)
            except Exception as exc:
                self.q.put(("log", "Bulk scan failed (%s); reading file-by-file." % exc))
                allmeta = {}
                for i, p in enumerate(paths):
                    try:
                        allmeta[os.path.abspath(p)] = scrub_core.get_metadata(p)
                    except Exception:
                        allmeta[os.path.abspath(p)] = {}
                    if i % 10 == 0:
                        self.q.put(("status", "Reading metadata (%d/%d)..." % (i, total)))
            for i, p in enumerate(paths):
                md = allmeta.get(os.path.abspath(p), {})
                self.meta_cache[os.path.abspath(p)] = md
                row = {
                    "path": os.path.abspath(p),
                    "name": os.path.basename(p),
                    "category": scrub_core.category_of(p) or "?",
                    "size": os.path.getsize(p) if os.path.isfile(p) else 0,
                    "tags": scrub_core.count_metadata_tags(md),
                    "gps": scrub_core.has_gps(md),
                    "thumb": scrub_core.has_thumbnail(md),
                    "status": "Has metadata" if scrub_core.count_metadata_tags(md) else "Clean",
                }
                self.files.append(row)
                self.q.put(("scan_row", row))
                self.q.put(("progress", (i + 1, total)))
        except Exception as exc:
            self.q.put(("log", "Scan error: %s" % exc))
        finally:
            self.q.put(("scan_done", None))

    def _add_file_row(self, row):
        gps = "YES" if row["gps"] else "-"
        thumb = "yes" if row["thumb"] else "-"
        iid = self.file_tree.insert("", "end", values=(
            row["name"], row["category"], fmt_size(row["size"]),
            row["tags"], gps, thumb, row["status"]))
        # stash path on the item via a parallel dict keyed by iid
        self._iid_to_path[iid] = row["path"]
        self.file_tree.tag_configure("gps_yes", foreground=WARN)
        self.file_tree.tag_configure("dirty", foreground=WARN)
        self.file_tree.tag_configure("clean", foreground=GOOD)
        tags = []
        if row["gps"]:
            tags.append("gps_yes")
        tags.append("clean" if row["tags"] == 0 else "dirty")
        self.file_tree.item(iid, tags=tuple(tags))

    def _row_for_path(self, path):
        for r in self.files:
            if r["path"] == path:
                return r
        return None

    def _update_file_row(self, info):
        # info: (path, tags, status_text)
        path, tags, status_text = info
        row = self._row_for_path(path)
        if row:
            row["tags"] = tags
            row["status"] = status_text
        for iid, p in list(self._iid_to_path.items()):
            if p == path:
                if not self.file_tree.exists(iid):
                    # stale entry (e.g. list was rebuilt) - drop it
                    del self._iid_to_path[iid]
                    continue
                vals = list(self.file_tree.item(iid, "values"))
                vals[3] = tags
                vals[6] = status_text
                self.file_tree.item(iid, values=vals,
                                    tags=("clean",) if tags == 0 else ("dirty",))
                break

    # -- metadata viewer --------------------------------------------------
    def _selected_paths(self):
        sel = self.file_tree.selection()
        paths = []
        for iid in sel:
            p = self._iid_to_path.get(iid)
            if p:
                paths.append(p)
        return paths

    def _on_file_select(self, _event=None):
        # Debounced: drag-selecting fires this rapidly, and each metadata
        # read costs an exiftool run. Wait until the selection settles.
        if self._meta_after_id:
            self.root.after_cancel(self._meta_after_id)
        self._meta_after_id = self.root.after(300, self._show_selected_metadata)

    def _show_selected_metadata(self):
        self._meta_after_id = None
        paths = self._selected_paths()
        if not paths:
            return
        self._show_metadata(paths[0])

    def _show_metadata(self, path):
        md = self.meta_cache.get(path)
        if md is None:
            try:
                md = scrub_core.get_metadata(path)
                self.meta_cache[path] = md
            except Exception as exc:
                self.meta_title.configure(text="Could not read metadata: %s" % exc)
                return
        self._current_meta_path = path
        self._current_meta = md
        self._render_metadata()

    def _render_metadata(self):
        if not hasattr(self, "_current_meta"):
            return
        path = self._current_meta_path
        md = self._current_meta
        filt = self.filter_var.get().strip().lower()
        self.meta_tree.delete(*self.meta_tree.get_children())
        grouped = scrub_core.parse_grouped(md)
        ntags = scrub_core.count_metadata_tags(md)
        bits = ["%d metadata tags" % ntags]
        if scrub_core.has_gps(md):
            bits.append("GPS PRESENT")
        if scrub_core.has_thumbnail(md):
            bits.append("embedded thumbnail")
        desc = scrub_core.icc_description(md)
        if desc:
            bits.append("ICC: %s" % desc)
        self.meta_title.configure(
            text="%s  -  %s" % (os.path.basename(path), " | ".join(bits)))
        shown = 0
        for group, tags in grouped.items():
            if group in ("File", "ExifTool"):
                continue
            items = [(t, v) for t, v in tags
                     if not filt or filt in t.lower() or filt in v.lower()
                     or filt in group.lower()]
            if not items:
                continue
            gid = self.meta_tree.insert("", "end", text="%s (%d)"
                                        % (group, len(items)), open=True)
            for tag, val in items:
                if len(val) > 400:
                    val = val[:400] + "..."
                self.meta_tree.insert(gid, "end", text="",
                                      values=(tag, val))
                shown += 1
        if shown == 0 and filt:
            self.meta_tree.insert("", "end", text="No tags match filter.")

    # -- scrubbing --------------------------------------------------------
    def scrub_selected(self):
        paths = self._selected_paths()
        if not paths:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        self._confirm_and_scrub(paths, "Scrub metadata from %d selected file(s)?" % len(paths))

    def scrub_all(self):
        if not self.files:
            messagebox.showinfo("No files", "Scan a folder first.")
            return
        paths = [r["path"] for r in self.files]
        self._confirm_and_scrub(paths, "Scrub metadata from ALL %d files in this folder?"
                                       % len(paths))

    def _confirm_and_scrub(self, paths, prompt):
        if self.working or self.scanning:
            return
        keep = self.keep_var.get()
        if keep:
            prompt += ("\n\nOriginals will be kept untouched. "
                       "Scrubbed copies go to 'Scrubbed Clean' in the scan folder.")
        else:
            prompt += "\n\nThis modifies the files in place."
        if not messagebox.askyesno("Confirm scrub", prompt):
            return
        self.working = True
        self.set_status("Scrubbing...")
        self.progress["value"] = 0
        threading.Thread(target=self._scrub_worker, args=(paths, keep), daemon=True).start()

    @staticmethod
    def _unique_dest(dest_dir, name):
        base, ext = os.path.splitext(name)
        cand = os.path.join(dest_dir, name)
        i = 2
        while os.path.exists(cand):
            cand = os.path.join(dest_dir, "%s (%d)%s" % (base, i, ext))
            i += 1
        return cand

    def _prepare_copies(self, paths):
        """Copy files to '<folder>/Scrubbed Clean/' and return the copy paths.

        Returns None on fatal error (already logged)."""
        if not self.folder or not os.path.isdir(self.folder):
            self.q.put(("log", "Cannot copy: no scan folder selected."))
            return None
        dest_dir = os.path.join(self.folder, "Scrubbed Clean")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as exc:
            self.q.put(("log", "Cannot create 'Scrubbed Clean' folder: %s" % exc))
            return None
        dest_abs = os.path.abspath(dest_dir)
        self.q.put(("log", "Copying %d file(s) to '%s'..." % (len(paths), dest_dir)))
        work = []
        for p in paths:
            try:
                if os.path.abspath(os.path.dirname(p)) == dest_abs:
                    work.append(p)  # already a copy; scrub in place
                    continue
                dest = self._unique_dest(dest_dir, os.path.basename(p))
                shutil.copy2(p, dest)
                self.q.put(("log", "COPIED %s -> Scrubbed Clean/" % os.path.basename(p)))
                work.append(dest)
            except Exception as exc:
                self.q.put(("log", "COPY FAILED %s: %s" % (os.path.basename(p), exc)))
        if not work:
            self.q.put(("log", "Nothing to scrub: all copies failed."))
            return None
        return work, dest_dir

    def _scrub_worker(self, paths, keep_originals=False):
        ok = 0
        failed = 0
        dest_dir = None
        aborted = False
        try:
            if keep_originals:
                prepared = self._prepare_copies(paths)
                if prepared is None:
                    aborted = True
                    return
                paths, dest_dir = prepared
            total = len(paths)
            for i, p in enumerate(paths):
                name = os.path.basename(p)
                self.q.put(("status", "Scrubbing %d/%d: %s" % (i + 1, total, name)))
                try:
                    res = scrub_core.scrub_file(p)
                except Exception as exc:
                    res = {"ok": False, "error": str(exc), "tags_before": 0,
                           "tags_after": 0, "icc_preserved": False, "notes": []}
                if res["ok"]:
                    ok += 1
                    removed = res["tags_before"] - res["tags_after"]
                    msg = "SCRUBBED %s: removed %d tag(s)" % (name, removed)
                    if res["icc_preserved"]:
                        msg += " [ICC profile '%s' preserved]" % (
                            res.get("icc_description") or "restored")
                    for note in res.get("notes", []):
                        if "WARNING" in note:
                            msg += " !! %s" % note
                    self.q.put(("log", msg))
                    # refresh cached metadata
                    try:
                        self.meta_cache[p] = scrub_core.get_metadata(p)
                    except Exception:
                        pass
                    if keep_originals:
                        # originals untouched; show the clean copy as a new row
                        self._queue_copy_row(p, res)
                    else:
                        self.q.put(("file_update", (p, res["tags_after"], "Scrubbed")))
                    if getattr(self, "_current_meta_path", None) == p:
                        self._current_meta = self.meta_cache.get(p, {})
                        self.q.put(("refresh_meta", None))
                else:
                    failed += 1
                    self.q.put(("log", "FAILED %s: %s" % (name, res.get("error"))))
                    if not keep_originals:
                        self.q.put(("file_update", (p, self._row_for_path(p)["tags"]
                                                    if self._row_for_path(p) else 0,
                                                    "Error")))
                self.q.put(("progress", (i + 1, total)))
        finally:
            # Always release the UI, even if something unexpected blew up.
            if aborted:
                self.q.put(("work_done", "Scrub aborted."))
            else:
                msg = "Scrub finished: %d ok, %d failed." % (ok, failed)
                if keep_originals and dest_dir:
                    msg += " Clean copies are in '%s'." % dest_dir
                self.q.put(("work_done", msg))

    def _queue_copy_row(self, path, res):
        """Add a scrubbed copy as a new row in the file list (via the queue)."""
        try:
            md = self.meta_cache.get(path, {})
            # avoid duplicates if the copy was already listed
            if self._row_for_path(path):
                self.q.put(("file_update", (path, res["tags_after"], "Scrubbed")))
                return
            row = {
                "path": path,
                "category": scrub_core.category_of(path),
                "size": os.path.getsize(path),
                "tags": res["tags_after"],
                "gps": scrub_core.has_gps(md),
                "thumb": scrub_core.has_thumbnail(md),
                "status": "Scrubbed",
            }
            self.q.put(("scan_row", row))
        except Exception:
            pass

    def recheck(self):
        """Re-read metadata for the selected files."""
        paths = self._selected_paths()
        if not paths:
            self.log("Select one or more files first.")
            return
        for p in paths:
            try:
                md = scrub_core.get_metadata(p)
                self.meta_cache[p] = md
                n = scrub_core.count_metadata_tags(md)
                self._update_file_row((p, n, "Clean" if n == 0 else "Has metadata"))
                if getattr(self, "_current_meta_path", None) == p:
                    self._current_meta = md
                    self._render_metadata()
            except Exception as exc:
                self.log("Rescan failed for %s: %s" % (os.path.basename(p), exc))
        self.log("Rescan done.")

    def export_report(self):
        if not self.files:
            messagebox.showinfo("No files", "Scan a folder first.")
            return
        out = filedialog.asksaveasfilename(
            title="Save metadata report",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not out:
            return
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("MetaScrub metadata report\n")
                fh.write("Folder: %s\n\n" % self.folder)
                for r in self.files:
                    md = self.meta_cache.get(r["path"], {})
                    fh.write("=" * 70 + "\n")
                    fh.write("%s  [%s, %s]\n" % (r["name"], r["category"],
                                                 fmt_size(r["size"])))
                    fh.write("GPS: %s | Thumbnail: %s | Tags: %d | Status: %s\n\n"
                             % ("YES" if r["gps"] else "no",
                                "yes" if r["thumb"] else "no",
                                r["tags"], r["status"]))
                    grouped = scrub_core.parse_grouped(md)
                    for group, tags in grouped.items():
                        if group in ("File", "ExifTool"):
                            continue
                        fh.write("[%s]\n" % group)
                        for tag, val in tags:
                            fh.write("  %s = %s\n" % (tag, val))
                        fh.write("\n")
            self.log("Report written to %s" % out)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))


def main():
    root = tk.Tk()
    app = MetaScrubApp(root)
    # Re-assert dark chrome after the window is mapped (some WM repaint it).
    root.bind("<Map>", lambda _e: apply_dark_titlebar(root))
    root.mainloop()


if __name__ == "__main__":
    main()
