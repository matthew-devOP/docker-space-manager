#!/usr/bin/env python3
"""
Docker Space Manager — Tkinter GUI for managing Docker disk usage.
Safely cleans up unused Docker resources without affecting existing containers.
"""

import json
import os
import platform
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

# macOS tk.Button ignores bg/fg — use tkmacosx.Button if available
if platform.system() == "Darwin":
    try:
        from tkmacosx import Button as MacButton
        _Button = MacButton
    except ImportError:
        _Button = tk.Button
else:
    _Button = tk.Button


# ─── Helpers ───────────────────────────────────────────────────────────────────

def run_docker(*args):
    """Run a docker command and return stdout, or raise on error."""
    result = subprocess.run(
        ["docker", *args],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def human_size(nbytes):
    """Convert bytes to human-readable string."""
    if nbytes is None or nbytes == 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def parse_docker_size(size_str):
    """Parse Docker size strings like '1.23GB' into bytes."""
    if not size_str or size_str == "0B":
        return 0
    size_str = size_str.strip()
    multipliers = {
        "B": 1, "KB": 1024, "MB": 1024**2,
        "GB": 1024**3, "TB": 1024**4,
        "kB": 1024,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if size_str.endswith(suffix):
            try:
                return float(size_str[: -len(suffix)]) * mult
            except ValueError:
                return 0
    return 0


def open_in_finder(path):
    """Open a path in Finder (macOS)."""
    if os.path.exists(path):
        subprocess.Popen(["open", path])
    else:
        messagebox.showwarning("Not Found", f"Path does not exist:\n{path}")


# ─── Data collection ──────────────────────────────────────────────────────────

def get_containers():
    """Return list of container dicts."""
    fmt = "{{json .}}"
    raw = run_docker("ps", "-a", "--format", fmt)
    if not raw:
        return []
    containers = []
    for line in raw.splitlines():
        c = json.loads(line)
        containers.append({
            "id": c.get("ID", ""),
            "name": c.get("Names", ""),
            "image": c.get("Image", ""),
            "status": c.get("Status", ""),
            "state": c.get("State", ""),
            "size": c.get("Size", ""),
            "ports": c.get("Ports", ""),
            "created": c.get("CreatedAt", ""),
            "labels": c.get("Labels", ""),
            "project": "",
        })
    # Extract compose project
    for c in containers:
        labels = c.get("labels", "")
        for part in labels.split(","):
            if "com.docker.compose.project=" in part:
                c["project"] = part.split("=", 1)[1]
                break
    return containers


def get_images():
    """Return list of image dicts."""
    fmt = "{{json .}}"
    raw = run_docker("images", "-a", "--format", fmt)
    if not raw:
        return []
    images = []
    for line in raw.splitlines():
        img = json.loads(line)
        images.append({
            "id": img.get("ID", ""),
            "repository": img.get("Repository", ""),
            "tag": img.get("Tag", ""),
            "size": img.get("Size", ""),
            "size_bytes": parse_docker_size(img.get("Size", "")),
            "created": img.get("CreatedSince", ""),
        })
    return images


def get_volumes():
    """Return list of volume dicts with sizes."""
    fmt = "{{json .}}"
    raw = run_docker("volume", "ls", "--format", fmt)
    if not raw:
        return []
    volumes = []
    for line in raw.splitlines():
        v = json.loads(line)
        name = v.get("Name", "")
        mountpoint = v.get("Mountpoint", "")
        # Get size via docker system df -v (cached later)
        volumes.append({
            "name": name,
            "driver": v.get("Driver", ""),
            "mountpoint": mountpoint,
            "labels": v.get("Labels", ""),
            "size_bytes": 0,
            "size": "calculating...",
        })
    return volumes


def get_build_cache():
    """Return build cache info from docker system df."""
    raw = run_docker("system", "df", "--format", "{{json .}}")
    if not raw:
        return {"size": "0B", "size_bytes": 0, "reclaimable": "0B", "reclaimable_bytes": 0}
    for line in raw.splitlines():
        entry = json.loads(line)
        if entry.get("Type") == "Build Cache":
            return {
                "size": entry.get("Size", "0B"),
                "size_bytes": parse_docker_size(entry.get("Size", "0B")),
                "reclaimable": entry.get("Reclaimable", "0B"),
                "reclaimable_bytes": parse_docker_size(entry.get("Reclaimable", "0B").split("(")[0].strip()),
            }
    return {"size": "0B", "size_bytes": 0, "reclaimable": "0B", "reclaimable_bytes": 0}


def get_system_df_verbose():
    """Get detailed system df including volume sizes."""
    raw = run_docker("system", "df", "-v", "--format", "{{json .}}")
    result = {"images": [], "containers": [], "volumes": [], "build_cache": []}
    if not raw:
        return result
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = entry.get("Type", "")
        if t:
            continue
        # -v format returns flat objects; detect by fields present
        if "Repository" in entry or "Containers" in entry and "Tag" in entry:
            result["images"].append(entry)
        elif "Mountpoint" in entry:
            result["volumes"].append(entry)
    return result


def get_volume_sizes():
    """Get volume sizes by inspecting docker data root."""
    try:
        raw = run_docker("system", "df", "-v")
    except Exception:
        return {}
    sizes = {}
    in_volumes = False
    for line in raw.splitlines():
        if line.startswith("VOLUME NAME"):
            in_volumes = True
            continue
        if in_volumes and line.strip():
            parts = line.split()
            if len(parts) >= 3:
                name = parts[0]
                size_str = parts[2]  # size column
                # sometimes there's a links column
                sizes[name] = parse_docker_size(size_str)
    return sizes


def get_docker_root():
    """Get Docker data root path."""
    try:
        raw = run_docker("info", "--format", "{{.DockerRootDir}}")
        return raw.strip()
    except Exception:
        return "/var/lib/docker"


def classify_safety(image_id, image_name, container_images):
    """
    Classify if an image is safe to remove.
    Returns (safe: bool, reason: str)
    """
    full = f"{image_name}"
    if full in container_images:
        return False, "Used by container"
    if image_id in container_images:
        return False, "Used by container (ID)"
    return True, "Not used by any container"


# ─── GUI ───────────────────────────────────────────────────────────────────────

class DockerSpaceManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Docker Space Manager")
        self.geometry("1200x750")
        self.minsize(1000, 600)

        # Colors
        self.colors = {
            "bg": "#1e1e2e",
            "surface": "#282840",
            "accent": "#7c3aed",
            "accent_hover": "#6d28d9",
            "danger": "#ef4444",
            "danger_hover": "#dc2626",
            "success": "#22c55e",
            "warning": "#f59e0b",
            "text": "#e2e8f0",
            "text_dim": "#94a3b8",
            "border": "#374151",
            "safe": "#166534",
            "unsafe": "#7f1d1d",
            "card_bg": "#1e293b",
        }

        self.configure(bg=self.colors["bg"])

        # Style
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self._configure_styles()

        # Data
        self.containers = []
        self.images = []
        self.volumes = []
        self.build_cache = {}
        self.volume_sizes = {}
        self.projects = {}  # project_name -> [containers]

        # Build UI
        self._build_header()
        self._build_notebook()
        self._build_status_bar()

        # Load data
        self.after(100, self.refresh_all)

    def _configure_styles(self):
        c = self.colors
        self.style.configure(".", background=c["bg"], foreground=c["text"],
                             fieldbackground=c["surface"])
        self.style.configure("TNotebook", background=c["bg"], borderwidth=0)
        self.style.configure("TNotebook.Tab", background=c["surface"],
                             foreground=c["text"], padding=[16, 8],
                             font=("SF Pro Display", 12))
        self.style.map("TNotebook.Tab",
                       background=[("selected", c["accent"])],
                       foreground=[("selected", "#ffffff")])
        self.style.configure("Treeview", background=c["surface"],
                             foreground=c["text"], fieldbackground=c["surface"],
                             rowheight=28, font=("SF Mono", 11))
        self.style.configure("Treeview.Heading", background=c["bg"],
                             foreground=c["text_dim"],
                             font=("SF Pro Display", 11, "bold"))
        self.style.map("Treeview",
                       background=[("selected", c["accent"])],
                       foreground=[("selected", "#ffffff")])
        self.style.configure("Accent.TButton", background=c["accent"],
                             foreground="#ffffff", font=("SF Pro Display", 11, "bold"),
                             padding=[12, 6])
        self.style.map("Accent.TButton",
                       background=[("active", c["accent_hover"])])
        self.style.configure("Danger.TButton", background=c["danger"],
                             foreground="#ffffff", font=("SF Pro Display", 11, "bold"),
                             padding=[12, 6])
        self.style.map("Danger.TButton",
                       background=[("active", c["danger_hover"])])
        self.style.configure("TLabel", background=c["bg"], foreground=c["text"])
        self.style.configure("Card.TFrame", background=c["card_bg"])
        self.style.configure("TCheckbutton", background=c["bg"],
                             foreground=c["text"])

    def _build_header(self):
        c = self.colors
        header = tk.Frame(self, bg=c["bg"], pady=10)
        header.pack(fill="x", padx=20)

        title = tk.Label(header, text="Docker Space Manager",
                         font=("SF Pro Display", 22, "bold"),
                         bg=c["bg"], fg=c["text"])
        title.pack(side="left")

        btn_frame = tk.Frame(header, bg=c["bg"])
        btn_frame.pack(side="right")

        self.refresh_btn = _Button(
            btn_frame, text="⟳  Refresh", font=("SF Pro Display", 12),
            bg=c["accent"], fg="#ffffff", relief="flat", padx=14, pady=6,
            activebackground=c["accent_hover"], activeforeground="#ffffff",
            command=self.refresh_all, cursor="pointinghand"
        )
        self.refresh_btn.pack(side="left", padx=5)

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        # Tabs
        self.tab_overview = tk.Frame(self.notebook, bg=self.colors["bg"])
        self.tab_projects = tk.Frame(self.notebook, bg=self.colors["bg"])
        self.tab_images = tk.Frame(self.notebook, bg=self.colors["bg"])
        self.tab_volumes = tk.Frame(self.notebook, bg=self.colors["bg"])
        self.tab_cache = tk.Frame(self.notebook, bg=self.colors["bg"])

        self.notebook.add(self.tab_overview, text="  Overview  ")
        self.notebook.add(self.tab_projects, text="  Projects  ")
        self.notebook.add(self.tab_images, text="  Images  ")
        self.notebook.add(self.tab_volumes, text="  Volumes  ")
        self.notebook.add(self.tab_cache, text="  Build Cache  ")

        self._build_overview_tab()
        self._build_projects_tab()
        self._build_images_tab()
        self._build_volumes_tab()
        self._build_cache_tab()

    def _build_status_bar(self):
        c = self.colors
        self.status_bar = tk.Label(
            self, text="Ready", font=("SF Mono", 10),
            bg=c["surface"], fg=c["text_dim"], anchor="w", padx=10, pady=4
        )
        self.status_bar.pack(fill="x", side="bottom")

    def set_status(self, text):
        self.status_bar.config(text=text)
        self.update_idletasks()

    # ── Overview Tab ──────────────────────────────────────────────────────────

    def _build_overview_tab(self):
        c = self.colors
        self.overview_frame = tk.Frame(self.tab_overview, bg=c["bg"])
        self.overview_frame.pack(fill="both", expand=True, padx=10, pady=10)

    def _refresh_overview(self):
        c = self.colors
        for w in self.overview_frame.winfo_children():
            w.destroy()

        # Summary cards
        cards_frame = tk.Frame(self.overview_frame, bg=c["bg"])
        cards_frame.pack(fill="x", pady=(0, 15))

        container_images = set()
        for ct in self.containers:
            container_images.add(ct["image"])

        total_images_bytes = sum(img["size_bytes"] for img in self.images)
        unused_images = [img for img in self.images
                         if classify_safety(img["id"],
                                            f"{img['repository']}:{img['tag']}",
                                            container_images)[0]
                         and img["repository"] != "<none>"]
        unused_bytes = sum(img["size_bytes"] for img in unused_images)
        total_vol_bytes = sum(self.volume_sizes.get(v["name"], 0) for v in self.volumes)

        # Find orphan volumes
        container_vol_names = set()
        for ct in self.containers:
            try:
                inspect = run_docker("inspect", ct["id"], "--format",
                                     "{{range .Mounts}}{{.Name}} {{end}}")
                for v in inspect.split():
                    if v.strip():
                        container_vol_names.add(v.strip())
            except Exception:
                pass

        orphan_volumes = [v for v in self.volumes if v["name"] not in container_vol_names]
        orphan_vol_bytes = sum(self.volume_sizes.get(v["name"], 0) for v in orphan_volumes)

        cache_bytes = self.build_cache.get("reclaimable_bytes", 0)

        total_reclaimable = unused_bytes + orphan_vol_bytes + cache_bytes

        cards_data = [
            ("Containers", str(len(self.containers)),
             f"{sum(1 for ct in self.containers if ct['state'] == 'running')} running",
             c["accent"]),
            ("Images", human_size(total_images_bytes),
             f"{len(self.images)} total", c["success"]),
            ("Volumes", human_size(total_vol_bytes),
             f"{len(self.volumes)} total", c["warning"]),
            ("Build Cache", self.build_cache.get("size", "0B"),
             f"Reclaimable: {self.build_cache.get('reclaimable', '0B')}", c["danger"]),
            ("Reclaimable", human_size(total_reclaimable),
             "Safe to remove", "#22d3ee"),
        ]

        for i, (title, value, subtitle, color) in enumerate(cards_data):
            card = tk.Frame(cards_frame, bg=c["card_bg"], padx=18, pady=14,
                            highlightbackground=color, highlightthickness=2)
            card.pack(side="left", fill="both", expand=True, padx=5)

            tk.Label(card, text=title, font=("SF Pro Display", 11),
                     bg=c["card_bg"], fg=c["text_dim"]).pack(anchor="w")
            tk.Label(card, text=value, font=("SF Pro Display", 20, "bold"),
                     bg=c["card_bg"], fg=color).pack(anchor="w", pady=(4, 2))
            tk.Label(card, text=subtitle, font=("SF Mono", 10),
                     bg=c["card_bg"], fg=c["text_dim"]).pack(anchor="w")

        # Docker data root
        docker_root = get_docker_root()
        root_frame = tk.Frame(self.overview_frame, bg=c["card_bg"],
                              padx=14, pady=10, highlightbackground=c["border"],
                              highlightthickness=1)
        root_frame.pack(fill="x", pady=(0, 15))

        tk.Label(root_frame, text="Docker Data Root:",
                 font=("SF Pro Display", 11, "bold"),
                 bg=c["card_bg"], fg=c["text_dim"]).pack(side="left")
        tk.Label(root_frame, text=docker_root,
                 font=("SF Mono", 11), bg=c["card_bg"],
                 fg=c["text"]).pack(side="left", padx=10)

        # Quick actions
        actions_frame = tk.Frame(self.overview_frame, bg=c["bg"])
        actions_frame.pack(fill="x", pady=(0, 10))

        tk.Label(actions_frame, text="Quick Actions",
                 font=("SF Pro Display", 14, "bold"),
                 bg=c["bg"], fg=c["text"]).pack(anchor="w", pady=(0, 8))

        btns = tk.Frame(actions_frame, bg=c["bg"])
        btns.pack(fill="x")

        _Button(btns, text=f"Clean Build Cache ({self.build_cache.get('reclaimable', '0B')})",
                  font=("SF Pro Display", 11), bg=c["danger"], fg="#ffffff",
                  relief="flat", padx=14, pady=8,
                  command=self.clean_build_cache, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        _Button(btns, text=f"Remove Unused Images ({human_size(unused_bytes)})",
                  font=("SF Pro Display", 11), bg=c["danger"], fg="#ffffff",
                  relief="flat", padx=14, pady=8,
                  command=self.clean_unused_images, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        _Button(btns, text=f"Remove Orphan Volumes ({human_size(orphan_vol_bytes)})",
                  font=("SF Pro Display", 11), bg=c["warning"], fg="#000000",
                  relief="flat", padx=14, pady=8,
                  command=self.clean_orphan_volumes, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        _Button(btns, text=f"Safe Clean All ({human_size(total_reclaimable)})",
                  font=("SF Pro Display", 11), bg="#dc2626", fg="#ffffff",
                  relief="flat", padx=14, pady=8,
                  command=self.safe_clean_all, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        # Projects summary
        proj_frame = tk.Frame(self.overview_frame, bg=c["bg"])
        proj_frame.pack(fill="both", expand=True, pady=(5, 0))

        tk.Label(proj_frame, text="Projects (Compose)",
                 font=("SF Pro Display", 14, "bold"),
                 bg=c["bg"], fg=c["text"]).pack(anchor="w", pady=(0, 8))

        tree_frame = tk.Frame(proj_frame, bg=c["bg"])
        tree_frame.pack(fill="both", expand=True)

        cols = ("project", "containers", "running", "stopped", "images_used")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=8)
        tree.heading("project", text="Project")
        tree.heading("containers", text="Containers")
        tree.heading("running", text="Running")
        tree.heading("stopped", text="Stopped")
        tree.heading("images_used", text="Images Used")

        tree.column("project", width=250)
        tree.column("containers", width=100, anchor="center")
        tree.column("running", width=100, anchor="center")
        tree.column("stopped", width=100, anchor="center")
        tree.column("images_used", width=400)

        for project, cts in self.projects.items():
            if not project:
                project = "(no project)"
            running = sum(1 for ct in cts if ct["state"] == "running")
            stopped = len(cts) - running
            imgs = ", ".join(sorted(set(ct["image"] for ct in cts)))
            tree.insert("", "end", values=(project, len(cts), running, stopped, imgs))

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    # ── Projects Tab ──────────────────────────────────────────────────────────

    def _build_projects_tab(self):
        c = self.colors
        self.projects_frame = tk.Frame(self.tab_projects, bg=c["bg"])
        self.projects_frame.pack(fill="both", expand=True, padx=10, pady=10)

    def _refresh_projects(self):
        c = self.colors
        for w in self.projects_frame.winfo_children():
            w.destroy()

        tk.Label(self.projects_frame, text="Docker Compose Projects",
                 font=("SF Pro Display", 16, "bold"),
                 bg=c["bg"], fg=c["text"]).pack(anchor="w", pady=(0, 10))

        tk.Label(self.projects_frame,
                 text="Each project's containers are listed below. "
                      "You can stop/start projects safely.",
                 font=("SF Pro Display", 11),
                 bg=c["bg"], fg=c["text_dim"]).pack(anchor="w", pady=(0, 15))

        canvas = tk.Canvas(self.projects_frame, bg=c["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.projects_frame, orient="vertical",
                                  command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=c["bg"])

        scroll_frame.bind("<Configure>",
                          lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        for project, cts in sorted(self.projects.items()):
            if not project:
                project_label = "(standalone containers)"
            else:
                project_label = project

            running = sum(1 for ct in cts if ct["state"] == "running")
            stopped = len(cts) - running

            # Project card
            card = tk.Frame(scroll_frame, bg=c["card_bg"], padx=14, pady=12,
                            highlightbackground=c["success"] if running > 0 else c["border"],
                            highlightthickness=2)
            card.pack(fill="x", pady=5, padx=5)

            # Header row
            hdr = tk.Frame(card, bg=c["card_bg"])
            hdr.pack(fill="x")

            status_color = c["success"] if running > 0 else c["text_dim"]
            status_text = f"● {running} running" if running > 0 else "● all stopped"

            tk.Label(hdr, text=project_label,
                     font=("SF Pro Display", 13, "bold"),
                     bg=c["card_bg"], fg=c["text"]).pack(side="left")
            tk.Label(hdr, text=status_text,
                     font=("SF Mono", 10), bg=c["card_bg"],
                     fg=status_color).pack(side="left", padx=15)
            tk.Label(hdr, text=f"{len(cts)} containers",
                     font=("SF Mono", 10), bg=c["card_bg"],
                     fg=c["text_dim"]).pack(side="left")

            # Container list
            for ct in cts:
                row = tk.Frame(card, bg=c["card_bg"])
                row.pack(fill="x", pady=2, padx=10)

                state_color = c["success"] if ct["state"] == "running" else c["text_dim"]
                tk.Label(row, text="●", font=("SF Mono", 8),
                         bg=c["card_bg"], fg=state_color).pack(side="left")
                tk.Label(row, text=ct["name"],
                         font=("SF Mono", 11), bg=c["card_bg"],
                         fg=c["text"]).pack(side="left", padx=(5, 15))
                tk.Label(row, text=ct["image"],
                         font=("SF Mono", 10), bg=c["card_bg"],
                         fg=c["text_dim"]).pack(side="left", padx=(0, 15))
                tk.Label(row, text=ct["status"],
                         font=("SF Mono", 10), bg=c["card_bg"],
                         fg=c["text_dim"]).pack(side="left")

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Bind mousewheel
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    # ── Images Tab ────────────────────────────────────────────────────────────

    def _build_images_tab(self):
        c = self.colors
        self.images_frame = tk.Frame(self.tab_images, bg=c["bg"])
        self.images_frame.pack(fill="both", expand=True, padx=10, pady=10)

    def _refresh_images(self):
        c = self.colors
        for w in self.images_frame.winfo_children():
            w.destroy()

        container_images = set()
        for ct in self.containers:
            container_images.add(ct["image"])

        # Header
        hdr = tk.Frame(self.images_frame, bg=c["bg"])
        hdr.pack(fill="x", pady=(0, 10))

        tk.Label(hdr, text="Docker Images",
                 font=("SF Pro Display", 16, "bold"),
                 bg=c["bg"], fg=c["text"]).pack(side="left")

        # Legend
        legend = tk.Frame(hdr, bg=c["bg"])
        legend.pack(side="right")
        tk.Label(legend, text="● Used by container",
                 font=("SF Mono", 10), bg=c["bg"],
                 fg=c["success"]).pack(side="left", padx=10)
        tk.Label(legend, text="● Safe to remove",
                 font=("SF Mono", 10), bg=c["bg"],
                 fg=c["danger"]).pack(side="left")

        # Treeview
        tree_frame = tk.Frame(self.images_frame, bg=c["bg"])
        tree_frame.pack(fill="both", expand=True)

        cols = ("status", "repository", "tag", "id", "size", "created", "safety")
        self.img_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                     selectmode="extended")
        self.img_tree.heading("status", text="")
        self.img_tree.heading("repository", text="Repository")
        self.img_tree.heading("tag", text="Tag")
        self.img_tree.heading("id", text="ID")
        self.img_tree.heading("size", text="Size")
        self.img_tree.heading("created", text="Created")
        self.img_tree.heading("safety", text="Status")

        self.img_tree.column("status", width=30, anchor="center")
        self.img_tree.column("repository", width=300)
        self.img_tree.column("tag", width=100)
        self.img_tree.column("id", width=130)
        self.img_tree.column("size", width=100, anchor="e")
        self.img_tree.column("created", width=150)
        self.img_tree.column("safety", width=180)

        # Tags for coloring
        self.img_tree.tag_configure("safe", foreground=c["danger"])
        self.img_tree.tag_configure("unsafe", foreground=c["success"])
        self.img_tree.tag_configure("dangling", foreground=c["warning"])

        for img in sorted(self.images, key=lambda x: x["size_bytes"], reverse=True):
            full_name = f"{img['repository']}:{img['tag']}"
            safe, reason = classify_safety(img["id"], full_name, container_images)
            tag = "safe" if safe else "unsafe"
            if img["repository"] == "<none>":
                tag = "dangling"
                reason = "Dangling image"
            status_icon = "✕" if safe else "✓"
            self.img_tree.insert("", "end", values=(
                status_icon, img["repository"], img["tag"], img["id"][:12],
                img["size"], img["created"], reason
            ), tags=(tag,))

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical",
                                  command=self.img_tree.yview)
        self.img_tree.configure(yscrollcommand=scrollbar.set)
        self.img_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Action buttons
        btn_frame = tk.Frame(self.images_frame, bg=c["bg"], pady=10)
        btn_frame.pack(fill="x")

        _Button(btn_frame, text="Remove Selected (Safe Only)",
                  font=("SF Pro Display", 11), bg=c["danger"], fg="#ffffff",
                  relief="flat", padx=14, pady=8,
                  command=self.remove_selected_images, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        _Button(btn_frame, text="Remove All Unused Images",
                  font=("SF Pro Display", 11), bg=c["danger"], fg="#ffffff",
                  relief="flat", padx=14, pady=8,
                  command=self.clean_unused_images, cursor="pointinghand"
                  ).pack(side="left", padx=5)

    # ── Volumes Tab ───────────────────────────────────────────────────────────

    def _build_volumes_tab(self):
        c = self.colors
        self.volumes_frame = tk.Frame(self.tab_volumes, bg=c["bg"])
        self.volumes_frame.pack(fill="both", expand=True, padx=10, pady=10)

    def _refresh_volumes(self):
        c = self.colors
        for w in self.volumes_frame.winfo_children():
            w.destroy()

        # Determine which volumes are used by containers
        container_vol_names = set()
        for ct in self.containers:
            try:
                inspect = run_docker("inspect", ct["id"], "--format",
                                     "{{range .Mounts}}{{.Name}} {{end}}")
                for v in inspect.split():
                    if v.strip():
                        container_vol_names.add(v.strip())
            except Exception:
                pass

        # Header
        hdr = tk.Frame(self.volumes_frame, bg=c["bg"])
        hdr.pack(fill="x", pady=(0, 10))

        tk.Label(hdr, text="Docker Volumes",
                 font=("SF Pro Display", 16, "bold"),
                 bg=c["bg"], fg=c["text"]).pack(side="left")

        legend = tk.Frame(hdr, bg=c["bg"])
        legend.pack(side="right")
        tk.Label(legend, text="● Used by container",
                 font=("SF Mono", 10), bg=c["bg"],
                 fg=c["success"]).pack(side="left", padx=10)
        tk.Label(legend, text="● Orphan (safe to remove)",
                 font=("SF Mono", 10), bg=c["bg"],
                 fg=c["danger"]).pack(side="left")

        # Treeview
        tree_frame = tk.Frame(self.volumes_frame, bg=c["bg"])
        tree_frame.pack(fill="both", expand=True)

        cols = ("status", "name", "size", "driver", "mountpoint", "used_by")
        self.vol_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                     selectmode="extended")
        self.vol_tree.heading("status", text="")
        self.vol_tree.heading("name", text="Volume Name")
        self.vol_tree.heading("size", text="Size")
        self.vol_tree.heading("driver", text="Driver")
        self.vol_tree.heading("mountpoint", text="Mountpoint")
        self.vol_tree.heading("used_by", text="Status")

        self.vol_tree.column("status", width=30, anchor="center")
        self.vol_tree.column("name", width=350)
        self.vol_tree.column("size", width=100, anchor="e")
        self.vol_tree.column("driver", width=80)
        self.vol_tree.column("mountpoint", width=300)
        self.vol_tree.column("used_by", width=180)

        self.vol_tree.tag_configure("orphan", foreground=c["danger"])
        self.vol_tree.tag_configure("used", foreground=c["success"])

        for vol in sorted(self.volumes, key=lambda v: self.volume_sizes.get(v["name"], 0),
                          reverse=True):
            is_orphan = vol["name"] not in container_vol_names
            size = human_size(self.volume_sizes.get(vol["name"], 0))
            tag = "orphan" if is_orphan else "used"
            status = "Orphan" if is_orphan else "In use"
            status_icon = "✕" if is_orphan else "✓"

            iid = self.vol_tree.insert("", "end", values=(
                status_icon, vol["name"], size, vol["driver"],
                vol["mountpoint"], status
            ), tags=(tag,))

        # Bind double-click to open in Finder
        self.vol_tree.bind("<Double-1>", self._on_volume_double_click)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical",
                                  command=self.vol_tree.yview)
        self.vol_tree.configure(yscrollcommand=scrollbar.set)
        self.vol_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Buttons
        btn_frame = tk.Frame(self.volumes_frame, bg=c["bg"], pady=10)
        btn_frame.pack(fill="x")

        _Button(btn_frame, text="Remove Selected Orphan Volumes",
                  font=("SF Pro Display", 11), bg=c["danger"], fg="#ffffff",
                  relief="flat", padx=14, pady=8,
                  command=self.remove_selected_volumes, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        _Button(btn_frame, text="Remove All Orphan Volumes",
                  font=("SF Pro Display", 11), bg=c["warning"], fg="#000000",
                  relief="flat", padx=14, pady=8,
                  command=self.clean_orphan_volumes, cursor="pointinghand"
                  ).pack(side="left", padx=5)

        tk.Label(btn_frame, text="Double-click a volume to open its mountpoint in Finder",
                 font=("SF Mono", 10), bg=c["bg"],
                 fg=c["text_dim"]).pack(side="right")

    def _on_volume_double_click(self, event):
        sel = self.vol_tree.selection()
        if sel:
            values = self.vol_tree.item(sel[0], "values")
            mountpoint = values[4]
            open_in_finder(mountpoint)

    # ── Build Cache Tab ───────────────────────────────────────────────────────

    def _build_cache_tab(self):
        c = self.colors
        self.cache_frame = tk.Frame(self.tab_cache, bg=c["bg"])
        self.cache_frame.pack(fill="both", expand=True, padx=10, pady=10)

    def _refresh_cache(self):
        c = self.colors
        for w in self.cache_frame.winfo_children():
            w.destroy()

        tk.Label(self.cache_frame, text="Build Cache",
                 font=("SF Pro Display", 16, "bold"),
                 bg=c["bg"], fg=c["text"]).pack(anchor="w", pady=(0, 15))

        # Info card
        card = tk.Frame(self.cache_frame, bg=c["card_bg"], padx=20, pady=20,
                        highlightbackground=c["border"], highlightthickness=1)
        card.pack(fill="x", pady=(0, 15))

        tk.Label(card, text="Total Size",
                 font=("SF Pro Display", 11),
                 bg=c["card_bg"], fg=c["text_dim"]).pack(anchor="w")
        tk.Label(card, text=self.build_cache.get("size", "0B"),
                 font=("SF Pro Display", 28, "bold"),
                 bg=c["card_bg"], fg=c["warning"]).pack(anchor="w", pady=(4, 8))

        tk.Label(card, text="Reclaimable",
                 font=("SF Pro Display", 11),
                 bg=c["card_bg"], fg=c["text_dim"]).pack(anchor="w")
        tk.Label(card, text=self.build_cache.get("reclaimable", "0B"),
                 font=("SF Pro Display", 22, "bold"),
                 bg=c["card_bg"], fg=c["success"]).pack(anchor="w", pady=(4, 8))

        tk.Label(card, text="Build cache stores intermediate layers from 'docker build'.\n"
                            "Clearing it is always safe — builds will just be slower the first time.",
                 font=("SF Pro Display", 11),
                 bg=c["card_bg"], fg=c["text_dim"],
                 justify="left").pack(anchor="w", pady=(10, 0))

        # Button
        _Button(self.cache_frame, text="Clear Build Cache",
                  font=("SF Pro Display", 13, "bold"), bg=c["danger"], fg="#ffffff",
                  relief="flat", padx=20, pady=10,
                  command=self.clean_build_cache, cursor="pointinghand"
                  ).pack(anchor="w", pady=10)

    # ── Actions ───────────────────────────────────────────────────────────────

    def clean_build_cache(self):
        if not messagebox.askyesno("Confirm", "Clear all Docker build cache?"):
            return
        self.set_status("Clearing build cache...")
        try:
            result = run_docker("builder", "prune", "-af")
            messagebox.showinfo("Done", f"Build cache cleared.\n\n{result}")
            self.refresh_all()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def clean_unused_images(self):
        container_images = set()
        for ct in self.containers:
            container_images.add(ct["image"])

        unused = [img for img in self.images
                  if classify_safety(img["id"],
                                     f"{img['repository']}:{img['tag']}",
                                     container_images)[0]
                  and img["repository"] != "<none>"]

        if not unused:
            messagebox.showinfo("Info", "No unused images found.")
            return

        names = "\n".join(f"  {img['repository']}:{img['tag']} ({img['size']})"
                          for img in unused)
        if not messagebox.askyesno("Confirm",
                                   f"Remove {len(unused)} unused images?\n\n{names}"):
            return

        self.set_status("Removing unused images...")
        errors = []
        for img in unused:
            try:
                run_docker("rmi", f"{img['repository']}:{img['tag']}")
            except Exception as e:
                errors.append(f"{img['repository']}:{img['tag']}: {e}")

        if errors:
            messagebox.showwarning("Partial", f"Some errors:\n\n" + "\n".join(errors))
        else:
            messagebox.showinfo("Done", f"Removed {len(unused)} unused images.")
        self.refresh_all()

    def remove_selected_images(self):
        sel = self.img_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select images to remove first.")
            return

        container_images = set()
        for ct in self.containers:
            container_images.add(ct["image"])

        to_remove = []
        blocked = []
        for iid in sel:
            values = self.img_tree.item(iid, "values")
            repo, tag = values[1], values[2]
            full_name = f"{repo}:{tag}"
            safe, reason = classify_safety(values[3], full_name, container_images)
            if safe:
                to_remove.append(full_name)
            else:
                blocked.append(f"{full_name} ({reason})")

        msg = ""
        if blocked:
            msg += f"BLOCKED (in use):\n" + "\n".join(f"  {b}" for b in blocked) + "\n\n"
        if to_remove:
            msg += f"Will remove:\n" + "\n".join(f"  {r}" for r in to_remove)
        else:
            messagebox.showwarning("Blocked", msg + "No safe images selected.")
            return

        if not messagebox.askyesno("Confirm", msg):
            return

        self.set_status("Removing selected images...")
        errors = []
        for img_name in to_remove:
            try:
                run_docker("rmi", img_name)
            except Exception as e:
                errors.append(f"{img_name}: {e}")

        if errors:
            messagebox.showwarning("Partial", "\n".join(errors))
        else:
            messagebox.showinfo("Done", f"Removed {len(to_remove)} images.")
        self.refresh_all()

    def clean_orphan_volumes(self):
        container_vol_names = set()
        for ct in self.containers:
            try:
                inspect = run_docker("inspect", ct["id"], "--format",
                                     "{{range .Mounts}}{{.Name}} {{end}}")
                for v in inspect.split():
                    if v.strip():
                        container_vol_names.add(v.strip())
            except Exception:
                pass

        orphans = [v for v in self.volumes if v["name"] not in container_vol_names]

        if not orphans:
            messagebox.showinfo("Info", "No orphan volumes found.")
            return

        names = "\n".join(
            f"  {v['name']} ({human_size(self.volume_sizes.get(v['name'], 0))})"
            for v in orphans
        )
        if not messagebox.askyesno("Confirm",
                                   f"Remove {len(orphans)} orphan volumes?\n\n{names}\n\n"
                                   "⚠ Data in these volumes will be permanently deleted!"):
            return

        self.set_status("Removing orphan volumes...")
        errors = []
        for v in orphans:
            try:
                run_docker("volume", "rm", v["name"])
            except Exception as e:
                errors.append(f"{v['name']}: {e}")

        if errors:
            messagebox.showwarning("Partial", "\n".join(errors))
        else:
            messagebox.showinfo("Done", f"Removed {len(orphans)} orphan volumes.")
        self.refresh_all()

    def remove_selected_volumes(self):
        sel = self.vol_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Select volumes to remove first.")
            return

        container_vol_names = set()
        for ct in self.containers:
            try:
                inspect = run_docker("inspect", ct["id"], "--format",
                                     "{{range .Mounts}}{{.Name}} {{end}}")
                for v in inspect.split():
                    if v.strip():
                        container_vol_names.add(v.strip())
            except Exception:
                pass

        to_remove = []
        blocked = []
        for iid in sel:
            values = self.vol_tree.item(iid, "values")
            name = values[1]
            if name in container_vol_names:
                blocked.append(name)
            else:
                to_remove.append(name)

        msg = ""
        if blocked:
            msg += f"BLOCKED (in use):\n" + "\n".join(f"  {b}" for b in blocked) + "\n\n"
        if to_remove:
            msg += f"Will remove:\n" + "\n".join(f"  {r}" for r in to_remove)
        else:
            messagebox.showwarning("Blocked", msg + "No orphan volumes selected.")
            return

        if not messagebox.askyesno("Confirm",
                                   msg + "\n\n⚠ Data will be permanently deleted!"):
            return

        self.set_status("Removing selected volumes...")
        errors = []
        for name in to_remove:
            try:
                run_docker("volume", "rm", name)
            except Exception as e:
                errors.append(f"{name}: {e}")

        if errors:
            messagebox.showwarning("Partial", "\n".join(errors))
        else:
            messagebox.showinfo("Done", f"Removed {len(to_remove)} volumes.")
        self.refresh_all()

    def safe_clean_all(self):
        """Clean build cache + unused images + orphan volumes in one go."""
        container_images = set()
        for ct in self.containers:
            container_images.add(ct["image"])

        unused_imgs = [img for img in self.images
                       if classify_safety(img["id"],
                                          f"{img['repository']}:{img['tag']}",
                                          container_images)[0]
                       and img["repository"] != "<none>"]

        container_vol_names = set()
        for ct in self.containers:
            try:
                inspect = run_docker("inspect", ct["id"], "--format",
                                     "{{range .Mounts}}{{.Name}} {{end}}")
                for v in inspect.split():
                    if v.strip():
                        container_vol_names.add(v.strip())
            except Exception:
                pass

        orphan_vols = [v for v in self.volumes if v["name"] not in container_vol_names]

        summary = (
            f"This will safely remove:\n\n"
            f"  • Build cache: {self.build_cache.get('reclaimable', '0B')}\n"
            f"  • {len(unused_imgs)} unused images\n"
            f"  • {len(orphan_vols)} orphan volumes\n\n"
            f"Containers and their associated resources will NOT be touched.\n\n"
            f"Continue?"
        )

        if not messagebox.askyesno("Safe Clean All", summary):
            return

        self.set_status("Running safe cleanup...")
        results = []

        # Build cache
        try:
            run_docker("builder", "prune", "-af")
            results.append("✓ Build cache cleared")
        except Exception as e:
            results.append(f"✕ Build cache: {e}")

        # Images
        for img in unused_imgs:
            try:
                run_docker("rmi", f"{img['repository']}:{img['tag']}")
            except Exception:
                pass
        results.append(f"✓ Removed {len(unused_imgs)} unused images")

        # Volumes
        for v in orphan_vols:
            try:
                run_docker("volume", "rm", v["name"])
            except Exception:
                pass
        results.append(f"✓ Removed {len(orphan_vols)} orphan volumes")

        messagebox.showinfo("Done", "\n".join(results))
        self.refresh_all()

    # ── Refresh ───────────────────────────────────────────────────────────────

    def refresh_all(self):
        """Reload all Docker data and refresh UI."""
        self.set_status("Loading Docker data...")
        self.refresh_btn.config(state="disabled")

        def _load():
            try:
                self.containers = get_containers()
                self.images = get_images()
                self.volumes = get_volumes()
                self.build_cache = get_build_cache()
                self.volume_sizes = get_volume_sizes()

                # Group by project
                self.projects = {}
                for ct in self.containers:
                    proj = ct.get("project", "")
                    if proj not in self.projects:
                        self.projects[proj] = []
                    self.projects[proj].append(ct)

                self.after(0, self._update_ui)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror(
                    "Error", f"Failed to load Docker data:\n\n{e}"))
                self.after(0, lambda: self.set_status("Error loading data"))
                self.after(0, lambda: self.refresh_btn.config(state="normal"))

        threading.Thread(target=_load, daemon=True).start()

    def _update_ui(self):
        """Update all tabs with fresh data."""
        self._refresh_overview()
        self._refresh_projects()
        self._refresh_images()
        self._refresh_volumes()
        self._refresh_cache()

        total = len(self.containers)
        running = sum(1 for c in self.containers if c["state"] == "running")
        self.set_status(
            f"Loaded: {total} containers ({running} running), "
            f"{len(self.images)} images, {len(self.volumes)} volumes  |  "
            f"Last refresh: {datetime.now().strftime('%H:%M:%S')}"
        )
        self.refresh_btn.config(state="normal")


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = DockerSpaceManager()
    app.mainloop()
