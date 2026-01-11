#!/usr/bin/env python3
"""
Smart File Sorter – Modern Edition
"""

import os
import sys
import json
import shutil
import threading
import time
import hashlib
import math
from pathlib import Path
from datetime import datetime
import mimetypes
import traceback

# GUI Imports
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

# Optional libs
PIL_AVAILABLE = True
IMAGEHASH_AVAILABLE = True
FACE_AVAILABLE = True

try:
    from PIL import Image, ImageTk, ExifTags
except ImportError:
    PIL_AVAILABLE = False

try:
    import imagehash
except ImportError:
    IMAGEHASH_AVAILABLE = False

try:
    import face_recognition
except ImportError:
    FACE_AVAILABLE = False

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
APP_NAME = "Smart File Sorter"
CACHE_DIRNAME = ".smartsort_cache"

EXTENSIONS = {
    "Photos": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heic"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm"},
    "Audio": {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"},
    "Documents": {".doc", ".docx", ".odt", ".rtf", ".txt"},
    "PDFs": {".pdf"},
    "Presentations": {".ppt", ".pptx", ".key"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "Code": {".py", ".java", ".c", ".cpp", ".js", ".ts", ".html", ".css", ".tsx", ".jsx"},
}

EXT_TO_GROUP = {}
for g, s in EXTENSIONS.items():
    for e in s:
        EXT_TO_GROUP[e] = g

MIME_MAP = [
    ("image", "Photos"),
    ("video", "Videos"),
    ("audio", "Audio"),
    ("application/pdf", "PDFs"),
    ("text", "Documents"),
]

DEFAULT_FOLDERS = [
    "Photos", "Videos", "Audio", "Documents", "PDFs", "Presentations",
    "Spreadsheets", "Archives", "Code", "Others"
]

# ---------------------------------------------------------
# Utility functions (Backend)
# ---------------------------------------------------------
def detect_group(path: Path):
    if path.is_dir():
        return None
    ext = path.suffix.lower()
    if ext in EXT_TO_GROUP:
        return EXT_TO_GROUP[ext]
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        for key, group in MIME_MAP:
            if mime.startswith(key):
                return group
        if mime == "application/pdf":
            return "PDFs"
    return "Others"

def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    parent = dest.parent
    stem = dest.stem
    suffix = dest.suffix
    i = 1
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1

def sha256(path: Path, chunk=1024*1024):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def ensure_dirs(base: Path, groups=None):
    groups = groups or DEFAULT_FOLDERS
    for g in groups:
        (base / g).mkdir(exist_ok=True)

def iter_files(base: Path, include_sub=False):
    if include_sub:
        for p in base.rglob("*"):
            if p.is_file():
                yield p
    else:
        for p in base.iterdir():
            if p.is_file():
                yield p

def safe_copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = unique_dest(dst)
    shutil.copy2(str(src), str(dst))
    return dst

def safe_move(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst = unique_dest(dst)
    shutil.move(str(src), str(dst))
    return dst

def get_exif_date(path: Path):
    if not PIL_AVAILABLE:
        return None
    try:
        im = Image.open(path)
        info = im._getexif()
        if not info:
            return None
        for tag, val in info.items():
            name = ExifTags.TAGS.get(tag, tag)
            if name in ("DateTimeOriginal", "DateTime"):
                try:
                    dt = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                    return dt
                except Exception:
                    pass
        return None
    except Exception:
        return None

def phash_image(path: Path):
    if not IMAGEHASH_AVAILABLE or not PIL_AVAILABLE:
        return None
    try:
        im = Image.open(path)
        return str(imagehash.phash(im))
    except Exception:
        return None

def face_encodings_for_image(path: Path):
    if not FACE_AVAILABLE:
        return []
    try:
        encs = face_recognition.face_encodings(face_recognition.load_image_file(str(path)))
        return encs
    except Exception:
        return []

# ---------------------------------------------------------
# Worker Logic
# ---------------------------------------------------------
class Worker(threading.Thread):
    def __init__(self, target, args=(), kwargs=None):
        super().__init__(daemon=True)
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self._stop_event = threading.Event()

    def run(self):
        try:
            self._target(self._stop_event, *self._args, **self._kwargs)
        except Exception as e:
            print(f"Worker Exception: {e}")
            traceback.print_exc()

    def stop(self):
        self._stop_event.set()

def sort_worker(stop_event, base: Path, move_mode: bool, include_sub: bool, groups_selected, exif_by_date, progress_cb, log_cb):
    print(f"DEBUG: sort_worker running on {base}")
    log_cb(f"Scanning {base}...")
    try:
        files = list(iter_files(base, include_sub))
        print(f"DEBUG: Found {len(files)} files")
    except Exception as e:
        print(f"DEBUG: Error listing files: {e}")
        log_cb(f"Scan failed: {e}")
        return

    total = len(files)
    if total == 0:
        print("DEBUG: No files found")
        log_cb("No files found to sort in this folder.")
        progress_cb(1.0)
        return

    print("DEBUG: Starting sort loop")
    log_cb(f"Found {total} files. Processing...")
    ensure_dirs(base, DEFAULT_FOLDERS)
    
    for idx, f in enumerate(files, start=1):
        if stop_event.is_set():
            log_cb("Sorting cancelled.")
            break
        try:
            group = detect_group(f)
            if groups_selected and group not in groups_selected:
                group = "Others"
            
            target_dir = base / group
            
            # Special case for Photos
            if group == "Photos" and exif_by_date and PIL_AVAILABLE:
                exif_dt = get_exif_date(f)
                if exif_dt:
                    target_dir = target_dir / f"{exif_dt.year}" / f"{exif_dt.month:02d}"
            
            dst = target_dir / f.name
            
            if move_mode:
                safe_move(f, dst)
                log_cb(f"Moved: {f.name}")
            else:
                safe_copy(f, dst)
                log_cb(f"Copied: {f.name}")
            
        except Exception as e:
            log_cb(f"Error {f.name}: {e}")
        
        progress_cb(idx / total)
    
    progress_cb(1.0)
    log_cb("Operation complete!")

def duplicate_scan_worker(stop_event, base: Path, include_sub: bool, images_only: bool, use_phash: bool, perceptual_threshold: float, progress_cb, log_cb, result_cb):
    log_cb("Listing files...")
    files = list(iter_files(base, include_sub))
    if images_only:
        files = [f for f in files if detect_group(f) == "Photos"]
    
    total = len(files)
    if total == 0:
        log_cb("No files to scan.")
        result_cb([])
        return

    log_cb(f"Scanning {total} files for duplicates...")
    size_map = {}
    for f in files:
        try:
            s = f.stat().st_size
            size_map.setdefault(s, []).append(f)
        except: pass
    
    duplicates = []
    processed = 0
    
    # Exact check
    total_size_groups = len(size_map)
    current_grp = 0
    
    for group in size_map.values():
        current_grp += 1
        if stop_event.is_set(): break
        if len(group) < 2:
            processed += len(group)
            progress_cb(current_grp / total_size_groups)
            continue
            
        hashes = {}
        for f in group:
            try:
                h = sha256(f)
                hashes.setdefault(h, []).append(f)
            except: pass
        
        for h_group in hashes.values():
            if len(h_group) > 1:
                keep = max(h_group, key=lambda p: p.stat().st_mtime)
                duplicates.append({"keep": keep, "delete": [p for p in h_group if p!=keep], "reason": "exact"})
        
        progress_cb(current_grp / total_size_groups)

    result_cb(duplicates)
    log_cb(f"Found {len(duplicates)} duplicate groups.")

def face_grouping_worker(stop_event, base: Path, include_sub: bool, progress_cb, log_cb, result_cb):
    if not FACE_AVAILABLE:
        log_cb("Face recognition not installed.")
        progress_cb(1.0)
        return

    photos = [p for p in iter_files(base, include_sub) if detect_group(p) == "Photos"]
    total = len(photos)
    
    if not photos:
        log_cb("No photos found.")
        progress_cb(1.0)
        return

    log_cb(f"Analyzing faces in {total} photos...")
    clusters = []
    
    for idx, p in enumerate(photos):
        if stop_event.is_set(): break
        encs = face_encodings_for_image(p)
        for enc in encs:
            found = False
            for c in clusters:
                dists = face_recognition.face_distance(c['encodings'], enc)
                if len(dists) > 0 and min(dists) < 0.6:
                    c['encodings'].append(enc)
                    c['images'].add(p)
                    found = True
                    break
            if not found:
                clusters.append({'encodings': [enc], 'images': {p}})
        
        progress_cb((idx+1)/total)

    faces_dir = base / "Faces"
    faces_dir.mkdir(exist_ok=True)
    
    final_groups = []
    for i, c in enumerate(clusters, 1):
        p_dir = faces_dir / f"Person_{i}"
        p_dir.mkdir(exist_ok=True)
        img_list = []
        for img in c['images']:
            dest = unique_dest(p_dir / img.name)
            shutil.copy2(str(img), str(dest))
            img_list.append(str(dest))
        final_groups.append(img_list)
        
    log_cb(f"Created {len(final_groups)} face albums.")
    result_cb(final_groups)

# ---------------------------------------------------------
# Modern UI
# ---------------------------------------------------------
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

class ModernApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.title(APP_NAME)
        self.geometry("1100x750")
        self.minsize(900, 600)
        
        # State
        self.current_worker = None
        self.worker_stop_event = None
        self.selected_dir = ctk.StringVar(value="")
        
        # Assets Path
        self.base_path = Path(__file__).parent / "assets"
        self.logo_img = None
        self.bg_img = None
        self._load_assets()

        # Layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.grid(row=0, column=0, sticky="nsew")
        self.main_container.grid_rowconfigure(0, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)
        
        if self.bg_img:
            self.bg_label = ctk.CTkLabel(self.main_container, text="", image=self.bg_img)
            self.bg_label.place(x=0, y=0, relwidth=1, relheight=1)

        self.pages = {}
        self._init_pages()
        self.show_page("dashboard")

    def _load_assets(self):
        try:
            p = self.base_path / "logo.png"
            if p.exists():
                pil_img = Image.open(p)
                self.logo_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(60, 60))
        except: pass

        try:
            p = self.base_path / "bg.png"
            if p.exists():
                pil_img = Image.open(p)
                self.bg_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(1600, 900))
        except: pass

    def _init_pages(self):
        for p in ["dashboard", "sort", "dup", "faces", "settings"]:
            frame = ctk.CTkFrame(self.main_container, corner_radius=20, fg_color=("#ffffff", "#0f172a")) 
            self.pages[p] = frame

        self._build_dashboard(self.pages["dashboard"])
        self._build_sort(self.pages["sort"])
        self._build_dup(self.pages["dup"])
        self._build_faces(self.pages["faces"])
        self._build_settings(self.pages["settings"])

    def show_page(self, name):
        for f in self.pages.values():
            f.grid_forget()
        self.pages[name].grid(row=0, column=0, sticky="nsew", padx=40, pady=40)
        
        if name == "dashboard":
            self.refresh_dashboard()

    def go_home(self):
        self.show_page("dashboard")

    def _build_dashboard(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)
        parent.grid_rowconfigure(2, weight=1)
        
        hdr_frame = ctk.CTkFrame(parent, fg_color="transparent")
        hdr_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=30, pady=(30, 20))
        
        if self.logo_img:
            ctk.CTkLabel(hdr_frame, text="", image=self.logo_img).pack(side="left", padx=10)
            
        title_box = ctk.CTkFrame(hdr_frame, fg_color="transparent")
        title_box.pack(side="left")
        ctk.CTkLabel(title_box, text=APP_NAME, font=ctk.CTkFont(size=28, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_box, text="Organize your digital chaos", text_color="gray70").pack(anchor="w")

        sel_frame = ctk.CTkFrame(parent, fg_color=("gray90", "#1e293b"), corner_radius=12)
        sel_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 30))
        
        ctk.CTkLabel(sel_frame, text="Working Directory:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=20, pady=15)
        self.lbl_path = ctk.CTkEntry(sel_frame, textvariable=self.selected_dir, border_width=0, fg_color="transparent", height=30)
        self.lbl_path.pack(side="left", fill="x", expand=True, padx=10)
        ctk.CTkButton(sel_frame, text="Change Folder", command=self.browse_folder, width=120).pack(side="right", padx=10, pady=10)

        grid_frame = ctk.CTkFrame(parent, fg_color="transparent")
        grid_frame.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=20, pady=(0, 30))
        grid_frame.grid_columnconfigure(0, weight=1)
        grid_frame.grid_columnconfigure(1, weight=1)
        grid_frame.grid_rowconfigure(0, weight=1)
        grid_frame.grid_rowconfigure(1, weight=1)

        self._make_action_card(grid_frame, 0, 0, "Sort Files", "Organize by type/date", "sort")
        self._make_action_card(grid_frame, 0, 1, "Duplicate Cleaner", "Recover space", "dup")
        self._make_action_card(grid_frame, 1, 0, "Face Albums", "Group people", "faces")
        self._make_action_card(grid_frame, 1, 1, "Settings", "App preferences", "settings")
        
        ftr = ctk.CTkFrame(parent, fg_color="#0f172a", height=40, corner_radius=0)
        ftr.grid(row=3, column=0, columnspan=2, sticky="ew")
        
        self.stat_lbl = ctk.CTkLabel(ftr, text="Ready", text_color="gray50")
        self.stat_lbl.pack(side="left", padx=20, pady=5)
        
        import webbrowser
        link_lbl = ctk.CTkLabel(ftr, text="@corevialabs", text_color="gray50", cursor="hand2", font=ctk.CTkFont(size=12, weight="bold"))
        link_lbl.place(relx=0.5, rely=0.5, anchor="center")
        link_lbl.bind("<Button-1>", lambda e: webbrowser.open("https://corevialabs.com"))
        link_lbl.bind("<Enter>", lambda e: link_lbl.configure(text_color="#3b82f6"))
        link_lbl.bind("<Leave>", lambda e: link_lbl.configure(text_color="gray50"))

    def _make_action_card(self, parent, r, c, title, sub, key):
        card = ctk.CTkButton(parent, text="", fg_color=("gray95", "#1e293b"), 
                             corner_radius=15, hover_color=("gray85", "#334155"),
                             command=lambda: self.show_page(key))
        card.grid(row=r, column=c, sticky="nsew", padx=10, pady=10)
        
        card.configure(text=f"\n{title}\n\n{sub}", font=ctk.CTkFont(size=18, weight="bold"))
        card.configure(border_width=2, border_color=("gray90", "#334155"))

    def _add_back_btn(self, parent):
        btn = ctk.CTkButton(parent, text="← Back to Dashboard", command=self.go_home, 
                            fg_color="transparent", text_color="gray70", anchor="w", width=150)
        btn.pack(anchor="w", padx=20, pady=(20, 0))

    def _build_sort(self, parent):
        self._add_back_btn(parent)
        ctk.CTkLabel(parent, text="Sort Files", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", padx=30, pady=(10,20))
        
        opts = ctk.CTkFrame(parent, fg_color="transparent")
        opts.pack(fill="x", padx=30)
        
        self.var_sub = ctk.BooleanVar(value=False)
        self.var_exif = ctk.BooleanVar(value=True)
        self.var_move = ctk.BooleanVar(value=False)
        
        # Mode Selection
        mode_frame = ctk.CTkFrame(opts, fg_color="transparent")
        mode_frame.pack(anchor="w", pady=10)
        ctk.CTkLabel(mode_frame, text="Mode:", font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0,10))
        ctk.CTkRadioButton(mode_frame, text="Copy (Safe)", variable=self.var_move, value=False).pack(side="left", padx=10)
        ctk.CTkRadioButton(mode_frame, text="Move (Clean)", variable=self.var_move, value=True).pack(side="left", padx=10)

        ctk.CTkSwitch(opts, text="Include Subfolders", variable=self.var_sub).pack(anchor="w", pady=5)
        ctk.CTkSwitch(opts, text="Organize Photos by Month/Year", variable=self.var_exif).pack(anchor="w", pady=5)
        
        # Group Checks
        grp_lbl = ctk.CTkLabel(parent, text="File Categories:", font=ctk.CTkFont(size=14, weight="bold"))
        grp_lbl.pack(anchor="w", padx=30, pady=(20, 10))
        
        gf = ctk.CTkFrame(parent, fg_color="transparent")
        gf.pack(fill="x", padx=30)
        
        self.groups_vars = {}
        for idx, g in enumerate(DEFAULT_FOLDERS):
            v = ctk.BooleanVar(value=True)
            self.groups_vars[g] = v
            ctk.CTkCheckBox(gf, text=g, variable=v).grid(row=idx//3, column=idx%3, sticky="w", pady=5, padx=5)

        # Action Bar at bottom
        self.sort_log = ctk.CTkLabel(parent, text="", text_color="gray")
        self.sort_log.pack(side="bottom", pady=5)
        self.prog_bar = ctk.CTkProgressBar(parent)
        self.prog_bar.set(0)
        self.prog_bar.pack(side="bottom", fill="x", padx=30, pady=10)
        
        ctk.CTkButton(parent, text="Start Sorting", height=50, 
                      font=ctk.CTkFont(size=16, weight="bold"),
                      fg_color="#3b82f6", hover_color="#2563eb",
                      command=self.start_sort).pack(side="bottom", fill="x", padx=30, pady=10)

    def _build_dup(self, parent):
        self._add_back_btn(parent)
        ctk.CTkLabel(parent, text="Duplicate Finder", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", padx=30, pady=(10,20))
        
        opts = ctk.CTkFrame(parent, fg_color="transparent")
        opts.pack(fill="x", padx=30)
        self.var_dup_images = ctk.BooleanVar(value=True)
        self.var_dup_phash = ctk.BooleanVar(value=True)
        
        ctk.CTkCheckBox(opts, text="Scan only Images", variable=self.var_dup_images).pack(anchor="w", pady=5)
        if IMAGEHASH_AVAILABLE:
            ctk.CTkCheckBox(opts, text="Use Perceptual Match (Find similar images)", variable=self.var_dup_phash).pack(anchor="w", pady=5)
        
        self.dup_res_box = ctk.CTkTextbox(parent)
        self.dup_res_box.pack(fill="both", expand=True, padx=30, pady=20)
        
        ctk.CTkButton(parent, text="Scan for Duplicates (No Delete)", height=50, 
                      fg_color="#ef4444", hover_color="#dc2626",
                      command=self.start_dup).pack(fill="x", padx=30, pady=30)

    def _build_faces(self, parent):
        self._add_back_btn(parent)
        ctk.CTkLabel(parent, text="Face Albums", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", padx=30, pady=(10,20))
        
        if not FACE_AVAILABLE:
            ctk.CTkLabel(parent, text="Feature unavailable. Install 'face_recognition'.", text_color="red").pack(padx=30)
            return

        ctk.CTkLabel(parent, text="Groups photos by unique faces detected. (Copies photos to albums)", text_color="gray").pack(anchor="w", padx=30)
        
        self.face_res_box = ctk.CTkTextbox(parent)
        self.face_res_box.pack(fill="both", expand=True, padx=30, pady=20)
        
        ctk.CTkButton(parent, text="Build Face Albums", height=50, 
                      fg_color="#22c55e", hover_color="#16a34a",
                      command=self.start_face).pack(fill="x", padx=30, pady=30)

    def _build_settings(self, parent):
        self._add_back_btn(parent)
        ctk.CTkLabel(parent, text="Settings", font=ctk.CTkFont(size=24, weight="bold")).pack(anchor="w", padx=30, pady=(10,20))
        ctk.CTkButton(parent, text="Clear Cache", command=lambda: messagebox.showinfo("Info", "Cache cleared")).pack(anchor="w", padx=30)
        
        ctk.CTkLabel(parent, text="\nAbout", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=30)
        ctk.CTkLabel(parent, text="Designed for simplicity and safety.\nVersion 2.3", justify="left", text_color="gray").pack(anchor="w", padx=30)

    # ------------------
    # Logic Connections
    # ------------------
    def browse_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.selected_dir.set(d)
            self.refresh_dashboard()

    def refresh_dashboard(self):
        p = self.selected_dir.get()
        if p and os.path.isdir(p):
            try:
                c = sum(1 for _ in Path(p).iterdir() if _.is_file())
                self.stat_lbl.configure(text=f"Ready to process {c} files in {Path(p).name}")
            except: pass
        else:
            self.stat_lbl.configure(text="Please select a folder")

    # THREAD-SAFE UPDATE WRAPPERS
    def _log_safe(self, msg):
        self.after(0, lambda: self._log(msg))

    def _progress_safe(self, val):
        self.after(0, lambda: self._progress(val))

    def _res_safe(self, widget, text):
        self.after(0, lambda: self._insert_text(widget, text))

    def _insert_text(self, widget, text):
        widget.insert("end", text)

    def _log(self, msg):
        if hasattr(self, 'sort_log'):
            self.sort_log.configure(text=msg)
            
    def _progress(self, val):
        if hasattr(self, 'prog_bar'):
            self.prog_bar.set(val)

    def _check_running(self):
        if self.current_worker and self.current_worker.is_alive():
            messagebox.showwarning("Busy", "Operation in progress.")
            return True
        return False

    def start_sort(self):
        if self._check_running(): return
        p = self.selected_dir.get()
        if not p:
            messagebox.showwarning("No Folder", "Please select a target folder.")
            return 
        
        print(f"DEBUG: Start Sort Requested. Path='{p}'")
        self.worker_stop_event = threading.Event()
        groups = [k for k,v in self.groups_vars.items() if v.get()]
        print(f"DEBUG: Groups selected: {groups}")
        print(f"DEBUG: Move={self.var_move.get()}, Sub={self.var_sub.get()}, Exif={self.var_exif.get()}")
        
        self.current_worker = Worker(
            target=sort_worker,
            args=(Path(p), self.var_move.get(), self.var_sub.get(), groups, self.var_exif.get(), self._progress_safe, self._log_safe)
        )
        self.current_worker.start()
        print("DEBUG: Worker started")

    def start_dup(self):
        if self._check_running(): return
        p = self.selected_dir.get()
        if not p:
            messagebox.showwarning("No Folder", "Please select a target folder.")
            return 
        
        self.dup_res_box.delete("0.0", "end")
        self.worker_stop_event = threading.Event()
        
        def on_done(res):
            self.after(0, lambda: self._insert_text(self.dup_res_box, f"Found {len(res)} duplicate groups.\n"))
            for g in res:
                self.after(0, lambda: self._insert_text(self.dup_res_box, f"\nKeep: {g['keep'].name}\n"))

        self.current_worker = Worker(
            target=duplicate_scan_worker,
            args=(self.worker_stop_event, Path(p), True, self.var_dup_images.get(), self.var_dup_phash.get(), 10, self._progress_safe, self._log_safe, on_done)
        )
        self.current_worker.start()
        
    def start_face(self):
        if self._check_running(): return
        if not FACE_AVAILABLE: return
        p = self.selected_dir.get()
        if not p:
            messagebox.showwarning("No Folder", "Please select a target folder.")
            return 
        
        self.face_res_box.delete("0.0", "end")
        self.worker_stop_event = threading.Event()
        
        def on_done(res):
            self.after(0, lambda: self._insert_text(self.face_res_box, f"Created {len(res)} person collections.\n"))
            
        self.current_worker = Worker(
            target=face_grouping_worker,
            args=(self.worker_stop_event, Path(p), True, self._progress_safe, self._log_safe, on_done)
        )
        self.current_worker.start()

if __name__ == "__main__":
    app = ModernApp()
    app.mainloop()
