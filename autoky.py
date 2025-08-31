#!/usr/bin/env python3
import argparse
import base64
import csv
import hashlib
import json
import sys
import traceback
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
import tkinter as tk
from tkinter import ttk, messagebox
import platform
import shutil
import re

# ----- Optional Pillow imports (lazy-safe) -----
try:
    from PIL import Image, ImageTk  # type: ignore
    PIL_AVAILABLE = True
except Exception:
    Image = None  # type: ignore
    ImageTk = None  # type: ignore
    PIL_AVAILABLE = False

# ----- Config -----
MODEL_NAME = "gemma3:12b"
PROMPT_TEMPLATE = (
    "Provide just a comma-separated list of keywords that someone searching for this image,"
    " or one with a similar visual or emotional tone, might use to find it, including dominant colors and themes, with no comments."
    " As a single keyword, one time, display the word RANK and how good the image is, on a scale from 1 to 10, such as 'RANK 4'."
    " As the final keyword, specify the high-level type of the image, such as photo, drawing, receipt or whatever it is."
)
OLLAMA_BASE = "http://localhost:11434"
CHAT_ENDPOINT = "/api/chat"
DEFAULT_TIMEOUT = 15
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

# ----- Model -----
class ImageData:
    def __init__(self, filename, keywords, rank, hash_code):
        self.filename = filename
        self.keywords = keywords
        self.rank = rank
        self.hash_code = hash_code
        self.full_path = None

    def matches_keywords(self, filter_keywords, partial=False):
        if not filter_keywords:
            return True
        if partial:
            # any filter keyword appears anywhere in any image keyword
            return any(any(k.lower() in kw.lower() for kw in self.keywords) for k in filter_keywords)
        # exact match against normalized set
        lower_set = {kw.lower() for kw in self.keywords}
        return any(k.lower() in lower_set for k in filter_keywords)

    def matches_rank(self, min_rank):
        return self.rank >= min_rank


# ----- Viewer -----
class ImageViewer:
    def __init__(self, master, image_data_list):
        self.master = master
        self.all_images = image_data_list
        self.filtered_images = []
        self.current_index = 0
        self.duplicates_count = 0

        # state
        self.zoom_percent = tk.DoubleVar(value=100.0)
        self.fit_mode = tk.StringVar(value="fit")  # "fit" or "actual"
        self.partial_match = tk.BooleanVar(value=False)
        self.fullscreen = False
        self.current_pil_image = None
        self.current_tk_image = None
        self._drag_start = None
        self.thumb_cache = {}     # (path,max_h) -> PhotoImage
        self.thumb_widgets = []   # per filtered image

        self._build_ui()
        self._bind_keys()
        self._apply_filters()     # show thumbnails and first image immediately

    # UI
    def _build_ui(self):
        self.master.title("Image Analyzer and Viewer")
        self.master.geometry("1400x900")
        self.master.minsize(900, 600)

        style = ttk.Style()
        try:
            style.theme_use("clam" if "clam" in style.theme_names() else style.theme_use())
        except Exception:
            pass
        base_font = ("Segoe UI", 10) if platform.system() == "Windows" else ("Helvetica", 11)
        style.configure("TLabel", font=base_font)
        style.configure("TLabelframe.Label", font=(base_font[0], 12, "bold"))

        paned = ttk.PanedWindow(self.master, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        # left panel
        left = ttk.Frame(paned, padding=12)
        paned.add(left, weight=1)

        # right panel
        right = ttk.Frame(paned, padding=0)
        paned.add(right, weight=6)

        # Filters
        f = ttk.LabelFrame(left, text="Filters", padding=12)
        f.pack(fill=tk.X)
        ttk.Label(f, text="Keywords, separated by commas").pack(anchor="w")
        kw_row = ttk.Frame(f)
        kw_row.pack(fill=tk.X, pady=(4, 8))
        self.keyword_entry = ttk.Entry(kw_row)
        self.keyword_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(kw_row, text="Clear", command=self._clear_filters).pack(side=tk.LEFT, padx=(8, 0))
        self.keyword_entry.bind("<KeyRelease>", lambda _e: self._on_filter_change())

        mm_row = ttk.Frame(f)
        mm_row.pack(fill=tk.X, pady=(0, 8))
        self.partial_chk = ttk.Checkbutton(
            mm_row, text="Enable partial contains matching", variable=self.partial_match,
            command=self._on_filter_change
        )
        self.partial_chk.pack(anchor="w")

        ttk.Label(f, text="Minimum rank").pack(anchor="w")
        rank_row = ttk.Frame(f); rank_row.pack(fill=tk.X, pady=(4, 8))
        self.rank_var = tk.IntVar(value=1)
        self.rank_scale = ttk.Scale(rank_row, from_=1, to=10, orient=tk.HORIZONTAL,
                                    variable=self.rank_var, command=lambda _e=None: self._on_filter_change())
        self.rank_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.rank_value_label = ttk.Label(rank_row, text="1"); self.rank_value_label.pack(side=tk.LEFT, padx=(8, 0))

        # Stats
        s = ttk.LabelFrame(left, text="Statistics", padding=12)
        s.pack(fill=tk.X, pady=(10, 0))
        self.total_label = ttk.Label(s, text="Total images: 0"); self.total_label.pack(anchor="w")
        self.filtered_label = ttk.Label(s, text="Matching filters: 0"); self.filtered_label.pack(anchor="w")
        self.duplicates_label = ttk.Label(s, text="Duplicates removed: 0"); self.duplicates_label.pack(anchor="w")

        # Nav
        nav = ttk.LabelFrame(left, text="Navigation", padding=12)
        nav.pack(fill=tk.X, pady=(10, 0))
        row = ttk.Frame(nav); row.pack(fill=tk.X)
        self.prev_btn = ttk.Button(row, text="← Previous", command=self._prev_image); self.prev_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self.next_btn = ttk.Button(row, text="Next →", command=self._next_image); self.next_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(8, 0))
        self.position_label = ttk.Label(nav, text="0 / 0"); self.position_label.pack(pady=(8, 0))

        # Actions
        act = ttk.LabelFrame(left, text="Actions", padding=12)
        act.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(act, text="Copy keywords", command=self._copy_keywords).pack(fill=tk.X)
        ttk.Button(act, text="Open image externally", command=self._open_externally).pack(fill=tk.X, pady=(8, 0))

        ttk.Frame(left).pack(fill=tk.BOTH, expand=True)  # spacer

        # Right: controls bar
        controls = ttk.Frame(right, padding=10)
        controls.pack(fill=tk.X, side=tk.TOP)
        ttk.Label(controls, text="View").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(controls, text="Fit", variable=self.fit_mode, value="fit",
                        command=self._on_fit_mode_change).pack(side=tk.LEFT)
        ttk.Radiobutton(controls, text="Actual", variable=self.fit_mode, value="actual",
                        command=self._on_fit_mode_change).pack(side=tk.LEFT, padx=(4, 12))

        # Zoom area (hidden in Fit mode)
        self.zoom_wrap = ttk.Frame(controls)
        ttk.Label(self.zoom_wrap, text="Zoom").pack(side=tk.LEFT)
        self.zoom_scale = ttk.Scale(self.zoom_wrap, from_=10, to=300, orient=tk.HORIZONTAL,
                                    variable=self.zoom_percent, command=lambda _=None: self._on_zoom_change())
        self.zoom_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.zoom_label = ttk.Label(self.zoom_wrap, text="100%")
        self.zoom_label.pack(side=tk.LEFT)
        self.zoom_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(controls, text="Fullscreen", command=self._toggle_fullscreen).pack(side=tk.RIGHT)

        # Thumbnail strip (pack BEFORE canvas so it shows immediately)
        thumbs_outer = ttk.Frame(right)
        thumbs_outer.pack(fill=tk.X, side=tk.TOP, pady=(2, 8))

        self.thumbs_canvas = tk.Canvas(thumbs_outer, height=100, bg="#101010", highlightthickness=0, cursor="hand2")
        self.thumbs_canvas.grid(row=0, column=0, sticky="ew")
        self.thumbs_scroll = ttk.Scrollbar(thumbs_outer, orient=tk.HORIZONTAL, command=self.thumbs_canvas.xview)
        self.thumbs_scroll.grid(row=1, column=0, sticky="ew")
        thumbs_outer.columnconfigure(0, weight=1)
        self.thumbs_canvas.configure(xscrollcommand=self.thumbs_scroll.set)

        self.thumbs_inner = ttk.Frame(self.thumbs_canvas)
        self.thumbs_window = self.thumbs_canvas.create_window(0, 0, anchor="nw", window=self.thumbs_inner)
        # keep scrollregion accurate as content or size changes
        self.thumbs_inner.bind("<Configure>", self._thumbs_update_scroll)
        self.thumbs_canvas.bind("<Configure>", self._thumbs_update_scroll)

        # Canvas for main image (pack AFTER thumbnails)
        canvas_frame = ttk.Frame(right); canvas_frame.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(canvas_frame, bg="#0b0b0b", highlightthickness=0, cursor="hand2")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        vbar.grid(row=0, column=1, sticky="ns")
        hbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL, command=self.canvas.xview)
        hbar.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.canvas.bind("<Configure>", lambda _e: self._redraw_image())
        self.canvas.bind("<Button-1>", self._drag_start_ev)
        self.canvas.bind("<B1-Motion>", self._drag_move_ev)
        self.canvas.bind("<Double-Button-1>", lambda _e: self._toggle_fullscreen())
        # wheel zoom with modifiers
        if platform.system() == "Darwin":
            self.canvas.bind("<Command-MouseWheel>", self._wheel_zoom)
        else:
            self.canvas.bind("<Control-MouseWheel>", self._wheel_zoom)

        # Info panel
        info = ttk.LabelFrame(right, text="Image information", padding=12)
        info.pack(fill=tk.X, side=tk.BOTTOM)
        grid = ttk.Frame(info); grid.pack(fill=tk.X)
        ttk.Label(grid, text="File").grid(row=0, column=0, sticky="w")
        self.file_label = ttk.Label(grid, text="", foreground="#2a61c1")
        self.file_label.grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(grid, text="Keywords").grid(row=1, column=0, sticky="nw", pady=(6, 0))
        kw_wrap = ttk.Frame(grid); kw_wrap.grid(row=1, column=1, sticky="ew", pady=(4, 0), padx=(10, 0))
        self.keywords_text = tk.Text(kw_wrap, height=4, wrap=tk.WORD)
        kw_scroll = ttk.Scrollbar(kw_wrap, orient=tk.VERTICAL, command=self.keywords_text.yview)
        self.keywords_text.configure(yscrollcommand=kw_scroll.set)
        self.keywords_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        kw_scroll.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0))
        ttk.Label(grid, text="Rank").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.rank_info_label = ttk.Label(grid, text="")
        self.rank_info_label.grid(row=2, column=1, sticky="w", padx=(10, 0))
        ttk.Label(grid, text="Hash").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.hash_label = ttk.Label(grid, text="", font=("Courier New", 9))
        self.hash_label.grid(row=3, column=1, sticky="w", padx=(10, 0))
        grid.columnconfigure(1, weight=1)

        self._update_zoom_visibility()

    def _bind_keys(self):
        for key in ("<Left>", "<Up>"):
            self.master.bind(key, lambda _e: self._prev_image())
        for key in ("<Right>", "<Down>", "<space>"):
            self.master.bind(key, lambda _e: self._next_image())
        for key in ("f", "F"):
            self.master.bind(key, lambda _e: self._toggle_fullscreen())
        self.master.bind("<Escape>", lambda _e: self._exit_fullscreen())

    # Filtering
    def _on_filter_change(self):
        self.rank_value_label.config(text=str(int(self.rank_var.get())))
        self._apply_filters()

    def _clear_filters(self):
        self.keyword_entry.delete(0, tk.END)
        self.rank_var.set(1)
        self.partial_match.set(False)
        self._on_filter_change()

    def _apply_filters(self):
        kw_text = self.keyword_entry.get().strip()
        filter_kws = [k.strip() for k in kw_text.split(",") if k.strip()] if kw_text else []
        min_rank = int(self.rank_var.get())
        partial = self.partial_match.get()

        filtered, seen, duplicates = [], set(), 0
        for img in self.all_images:
            if img.matches_keywords(filter_kws, partial) and img.matches_rank(min_rank):
                if img.hash_code in seen:
                    duplicates += 1
                else:
                    seen.add(img.hash_code)
                    filtered.append(img)

        self.filtered_images = filtered
        self.duplicates_count = duplicates
        self.current_index = 0

        self._update_stats()
        self._render_thumbnails()
        self._display_current_image()

    def _update_stats(self):
        total = len(self.all_images)
        filtered = len(self.filtered_images)
        self.total_label.config(text=f"Total images: {total}")
        self.filtered_label.config(text=f"Matching filters: {filtered}")
        self.duplicates_label.config(text=f"Duplicates removed: {self.duplicates_count}")
        self.position_label.config(text=f"{self.current_index + 1} / {filtered}" if filtered else "0 / 0")

    # Thumbnails
    def _thumbs_update_scroll(self, *_):
        bbox = self.thumbs_canvas.bbox("all")
        if bbox:
            self.thumbs_canvas.configure(scrollregion=bbox)

    def _render_thumbnails(self):
        for w in self.thumb_widgets:
            w.destroy()
        self.thumb_widgets.clear()

        if not self.filtered_images:
            self.thumbs_canvas.configure(height=60)
            return

        self.thumbs_canvas.configure(height=100)
        pad = 6

        for idx, img_data in enumerate(self.filtered_images):
            p = self._resolve_image_path(img_data)
            tile = tk.Frame(self.thumbs_inner, bg="#101010", highlightthickness=2, highlightbackground="#303030")
            tile.pack(side=tk.LEFT, padx=(pad, 0), pady=pad)

            if PIL_AVAILABLE and p:
                thumb = self._get_thumbnail(p, max_h=84)
                lbl = tk.Label(tile, image=thumb, bg="#101010")
                lbl.image = thumb  # keep ref
            else:
                lbl = tk.Label(tile, text="No preview", fg="#bbb", bg="#101010", width=14, height=6)

            lbl.pack()
            lbl.bind("<Button-1>", lambda _e, i=idx: self._goto_index(i))

            # small caption (rank)
            cap = tk.Label(tile, text=f"R {img_data.rank}", fg="#ddd", bg="#101010")
            cap.pack()

            self.thumb_widgets.append(tile)

        self._highlight_thumb(self.current_index)
        self._thumbs_update_scroll()

    def _get_thumbnail(self, path: Path, max_h=84):
        key = (str(path), max_h)
        if key in self.thumb_cache:
            return self.thumb_cache[key]
        try:
            im = Image.open(path)
            ratio = max_h / max(1, im.height)
            w = max(1, int(im.width * ratio))
            Resampling = getattr(Image, "Resampling", Image)
            filt = getattr(Resampling, "LANCZOS", getattr(Image, "LANCZOS", Image.BICUBIC))
            im = im.resize((w, max_h), filt)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            tkimg = ImageTk.PhotoImage(im)
            self.thumb_cache[key] = tkimg
            return tkimg
        except Exception:
            ph = tk.PhotoImage(width=100, height=max_h)
            self.thumb_cache[key] = ph
            return ph

    def _highlight_thumb(self, index):
        for i, tile in enumerate(self.thumb_widgets):
            tile.configure(highlightbackground=("#5aa0ff" if i == index else "#303030"))

    def _goto_index(self, i):
        if 0 <= i < len(self.filtered_images):
            self.current_index = i
            self._update_stats()
            self._display_current_image()
            self._highlight_thumb(i)

    # Image display
    def _display_current_image(self):
        if not self.filtered_images:
            self.current_pil_image = None  # prevent redraw attempts
            self.canvas.delete("all")
            self._draw_message("No images match current filters")
            self._set_info_panel("", [], "", "")
            self.prev_btn.config(state=tk.DISABLED)
            self.next_btn.config(state=tk.DISABLED)
            return

        self.prev_btn.config(state=tk.NORMAL if self.current_index > 0 else tk.DISABLED)
        self.next_btn.config(state=tk.NORMAL if self.current_index < len(self.filtered_images) - 1 else tk.DISABLED)

        img_data = self.filtered_images[self.current_index]
        path = self._resolve_image_path(img_data)
        self._set_info_panel(img_data.filename, img_data.keywords, img_data.rank, img_data.hash_code)

        if not PIL_AVAILABLE:
            self._draw_message("Pillow is required to display images.\nInstall with: pip install pillow")
            return
        if not path:
            self._draw_message(f"Image not found: {img_data.filename}")
            return

        try:
            self.current_pil_image = Image.open(path)
        except Exception as e:
            self._draw_message(f"Error loading image:\n{e}")
            return

        self._redraw_image()

    def _set_info_panel(self, filename, keywords, rank, hash_code):
        self.file_label.config(text=filename or "")
        self.keywords_text.delete("1.0", tk.END)
        if keywords:
            self.keywords_text.insert(tk.END, ", ".join(keywords))
        self.rank_info_label.config(text=str(rank) if rank != "" else "")
        self.hash_label.config(text=str(hash_code) if hash_code != "" else "")

    def _resolve_image_path(self, img_data) -> Path | None:
        if img_data.full_path and Path(img_data.full_path).exists():
            return Path(img_data.full_path)
        for candidate in (Path(img_data.filename), Path.cwd() / img_data.filename):
            if candidate.exists():
                return candidate
        return None

    def _draw_message(self, text):
        self.canvas.delete("all")
        w = self.canvas.winfo_width() or 800
        h = self.canvas.winfo_height() or 600
        self.canvas.create_text(w // 2, h // 2, text=text, fill="#e06666", font=("Helvetica", 12), anchor="c")

    def _redraw_image(self):
        # Guards against empty list or stale index
        if not self.filtered_images or self.current_index >= len(self.filtered_images):
            self.canvas.delete("all")
            return
        if not self.current_pil_image:
            self.canvas.delete("all")
            return

        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())

        img = self.current_pil_image
        scale = min(cw / img.width, ch / img.height) if self.fit_mode.get() == "fit" else self.zoom_percent.get() / 100.0
        tw, th = max(1, int(img.width * scale)), max(1, int(img.height * scale))

        # High quality resize across Pillow versions
        Resampling = getattr(Image, "Resampling", Image)
        filt = getattr(Resampling, "LANCZOS", getattr(Image, "LANCZOS", Image.BICUBIC))
        try:
            img_draw = img if (tw == img.width and th == img.height) else img.resize((tw, th), filt)
        except Exception:
            img_draw = img if (tw == img.width and th == img.height) else img.resize((tw, th))

        if img_draw.mode not in ("RGB", "RGBA"):
            img_draw = img_draw.convert("RGB")

        self.current_tk_image = ImageTk.PhotoImage(img_draw)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self.current_tk_image, anchor="c")
        self.canvas.image = self.current_tk_image
        self.canvas.configure(scrollregion=(0, 0, tw, th))

    # Zoom and fit
    def _on_fit_mode_change(self):
        self._update_zoom_visibility()
        self._redraw_image()

    def _on_zoom_change(self):
        # Only redraw in Actual mode
        if self.fit_mode.get() == "actual":
            self._redraw_image()

    def _update_zoom_visibility(self):
        if self.fit_mode.get() == "fit":
            self.zoom_wrap.pack_forget()
        else:
            self.zoom_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _wheel_zoom(self, event):
        step = 5 if event.delta > 0 else -5
        new_val = max(10, min(300, self.zoom_percent.get() + step))
        self.zoom_percent.set(new_val)
        self._on_zoom_change()

    # Panning
    def _drag_start_ev(self, e):
        self._drag_start = (e.x, e.y)

    def _drag_move_ev(self, e):
        if not self._drag_start:
            return
        dx = self._drag_start[0] - e.x
        dy = self._drag_start[1] - e.y
        self._drag_start = (e.x, e.y)
        self.canvas.xview_scroll(int(dx / 2), "units")
        self.canvas.yview_scroll(int(dy / 2), "units")

    # Fullscreen
    def _toggle_fullscreen(self):
        """Toggle fullscreen with a safe Windows/macOS/Linux fallback."""
        self.fullscreen = not self.fullscreen
        try:
            self.master.attributes("-fullscreen", self.fullscreen)
        except tk.TclError:
            if self.fullscreen:
                self._prev_geometry = self.master.geometry()
                try:
                    self.master.state("zoomed")  # Windows
                except tk.TclError:
                    self.master.attributes("-zoomed", True)  # some X11 WMs
            else:
                try:
                    self.master.state("normal")
                except tk.TclError:
                    self.master.attributes("-zoomed", False)
                if hasattr(self, "_prev_geometry"):
                    self.master.geometry(self._prev_geometry)

    def _exit_fullscreen(self):
        if getattr(self, "fullscreen", False):
            self.fullscreen = False
            try:
                self.master.attributes("-fullscreen", False)
            except tk.TclError:
                try:
                    self.master.state("normal")
                except tk.TclError:
                    self.master.attributes("-zoomed", False)

    # Navigation
    def _prev_image(self):
        if self.current_index > 0:
            self.current_index -= 1
            self._update_stats()
            self._display_current_image()
            self._highlight_thumb(self.current_index)

    def _next_image(self):
        if self.current_index < len(self.filtered_images) - 1:
            self.current_index += 1
            self._update_stats()
            self._display_current_image()
            self._highlight_thumb(self.current_index)

    # Actions
    def _copy_keywords(self):
        if not self.filtered_images:
            return
        text = self.keywords_text.get("1.0", tk.END).strip()
        if not text:
            return
        self.master.clipboard_clear()
        self.master.clipboard_append(text)
        messagebox.showinfo("Copied", "Keywords copied to clipboard")

    def _open_externally(self):
        if not self.filtered_images:
            return
        p = self._resolve_image_path(self.filtered_images[self.current_index])
        if not p:
            messagebox.showerror("Not found", "Could not locate the image file on disk.")
            return
        try:
            if platform.system() == "Windows":
                import os
                os.startfile(str(p))
            elif platform.system() == "Darwin":
                shutil.which("open") and __import__("subprocess").run(["open", str(p)])
            else:
                shutil.which("xdg-open") and __import__("subprocess").run(["xdg-open", str(p)])
        except Exception as e:
            messagebox.showerror("Open failed", f"Could not open externally:\n{e}")


# ----- Helpers -----
def encode_image_base64(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")

def file_sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def pretty_json_try(data_bytes):
    try:
        parsed = json.loads(data_bytes.decode("utf-8", "replace"))
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except Exception:
        try:
            return data_bytes.decode("utf-8", "replace")
        except Exception:
            return repr(data_bytes)

def extract_text_from_response(resp_json):
    if not isinstance(resp_json, dict):
        return None
    if isinstance(resp_json.get("response"), str):
        return resp_json["response"]
    m = resp_json.get("message")
    if isinstance(m, dict) and isinstance(m.get("content"), str):
        return m["content"]
    ch = resp_json.get("choices")
    if isinstance(ch, list) and ch:
        first = ch[0]
        msg = isinstance(first, dict) and first.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
        if isinstance(first.get("content"), str):
            return first["content"]
    for k in ("text", "output", "content"):
        if isinstance(resp_json.get(k), str):
            return resp_json[k]
    return None

def build_chat_payload(model, prompt, image_b64):
    return {"model": model, "messages": [{"role": "user", "content": prompt, "images": [image_b64]}], "stream": False}

def do_request(url: str, payload: dict, timeout=DEFAULT_TIMEOUT):
    data_bytes = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data_bytes, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        resp_bytes = resp.read()
        status = getattr(resp, "status", None) or getattr(resp, "getcode", lambda: None)()
        headers = resp.getheaders() if hasattr(resp, "getheaders") else dict(resp.headers)
        return status, headers, resp_bytes

def find_images_in_path(path: Path):
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_EXTS else []
    if path.is_dir():
        return [p for p in path.rglob("*") if p.suffix.lower() in SUPPORTED_EXTS]
    return []

def expand_wildcards_and_folders(args):
    files = []
    for a in args:
        p = Path(a)
        if p.exists():
            files.extend(find_images_in_path(p))
        else:
            for m in Path().glob(a):
                files.extend(find_images_in_path(m))
    return [Path(s) for s in sorted({str(p.resolve()) for p in files})]

def process_keywords(raw_text: str, sha256_hex: str):
    parts = [kw.strip() for kw in raw_text.split(",") if kw.strip()]
    seen, cleaned = set(), []
    for kw in parts:
        low = kw.lower()
        if low not in seen:
            seen.add(low)
            cleaned.append(kw)
    cleaned.sort(key=str.lower)
    cleaned.append(sha256_hex)
    return cleaned

def parse_csv_file(csv_path: Path):
    out = []
    try:
        with open(csv_path, "r", newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 2:
                    continue
                filename, kws, rank, h = row[0], [], 1, ""
                for item in (c.strip() for c in row[1:] if c.strip()):
                    m = re.search(r"RANK\s+(\d+)", item, re.IGNORECASE)
                    if m:
                        rank = int(m.group(1)); continue
                    if re.fullmatch(r"[a-fA-F0-9]{64}", item):
                        h = item.lower(); continue
                    kws.append(item)
                if not h:
                    h = hashlib.sha256(filename.encode()).hexdigest()
                d = ImageData(filename, kws, rank, h)
                for p in (Path(filename), Path(csv_path.parent) / filename, Path.cwd() / filename):
                    if p.exists():
                        d.full_path = str(p.resolve()); break
                out.append(d)
    except Exception as e:
        print(f"Error parsing CSV file {csv_path}: {e}", file=sys.stderr)
    return out

def run_for_file(path: Path, args, csv_writer):
    full_path = str(path.resolve())
    try:
        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        print(f"{full_path}: Failed to read or encode image: {e}", file=sys.stderr)
        return

    url = urljoin(OLLAMA_BASE, CHAT_ENDPOINT.lstrip("/"))
    payload = build_chat_payload(MODEL_NAME, PROMPT_TEMPLATE, image_b64)

    if args.debug:
        print(f"\n--- Processing {full_path} ---", file=sys.stderr)
        print("Request URL:", url, file=sys.stderr)

    try:
        status, headers, resp_bytes = do_request(url, payload, timeout=args.timeout)
        if args.debug:
            print("\nHTTP Status:", status, file=sys.stderr)
            for k, v in headers:
                print(f"{k}: {v}", file=sys.stderr)
            print("Response body snippet:\n", pretty_json_try(resp_bytes)[:2000], file=sys.stderr)

        resp_json = json.loads(resp_bytes.decode("utf-8", "replace"))
        extracted = extract_text_from_response(resp_json)
        if not extracted:
            csv_writer.writerow([full_path, "[No text extracted]"]); return
        sha = file_sha256_hex(path)
        csv_writer.writerow([full_path] + process_keywords(extracted, sha))

    except HTTPError as he:
        if args.debug:
            err_body = he.read()
            print(f"HTTP Error {he.code} {he.reason}", file=sys.stderr)
            print("Error body:\n", pretty_json_try(err_body), file=sys.stderr)
            traceback.print_exc()
        csv_writer.writerow([full_path, f"HTTP Error {he.code}"])
    except URLError as ue:
        if args.debug:
            traceback.print_exc()
        csv_writer.writerow([full_path, f"Connection Error: {ue.reason}"])
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        csv_writer.writerow([full_path, f"Error: {e}"])


# ----- Main -----
def main():
    p = argparse.ArgumentParser(
        description="Send images to a local Ollama vision model and get sorted unique keywords plus SHA-256, "
                    "or view existing CSV data from .txt files in a modern image viewer."
    )
    p.add_argument("paths", nargs="+", help="Image files, wildcards, folders, or *.txt CSV files to process")
    p.add_argument("--debug", action="store_true", help="Enable detailed debug output")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Network timeout in seconds")
    args = p.parse_args()

    txt_files, other = [], []
    for s in args.paths:
        pth = Path(s)
        if s.endswith("*.txt"):
            txt_files.extend(Path().glob(s))
        elif pth.suffix.lower() == ".txt" and pth.exists():
            txt_files.append(pth)
        else:
            other.append(s)

    if txt_files and not other:
        all_data = []
        for tf in txt_files:
            print(f"Processing CSV file: {tf.resolve()}", file=sys.stderr)
            all_data.extend(parse_csv_file(tf))
        if not all_data:
            print("No image data found in the provided .txt files.", file=sys.stderr); sys.exit(1)

        if not PIL_AVAILABLE:
            print("Pillow is required for the viewer. Install it with:", file=sys.stderr)
            print("    pip install pillow", file=sys.stderr)
            try:
                root = tk.Tk(); root.withdraw()
                messagebox.showerror("Pillow required", "Pillow is required for the viewer.\nInstall it with:\n\npip install pillow")
                root.destroy()
            except Exception:
                pass
            sys.exit(1)

        print(f"Loaded {len(all_data)} images from CSV files. Launching GUI...", file=sys.stderr)
        root = tk.Tk()
        ImageViewer(root, all_data)
        root.mainloop()
    else:
        imgs = expand_wildcards_and_folders(args.paths)
        if not imgs:
            print("No matching images found.", file=sys.stderr); sys.exit(1)
        writer = csv.writer(sys.stdout)
        for img in imgs:
            run_for_file(img, args, writer)

if __name__ == "__main__":
    main()
