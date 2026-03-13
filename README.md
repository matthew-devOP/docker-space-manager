# Docker Space Manager

A desktop GUI application for visualizing and managing Docker disk usage. Built with Python and Tkinter, it provides a safe and intuitive way to reclaim disk space by cleaning up unused Docker resources — without risking your running containers.

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

## Features

### Overview Dashboard
- Summary cards showing containers, images, volumes, build cache, and total reclaimable space
- Docker data root path display
- Quick action buttons for one-click cleanup
- Docker Compose project summary table

### Projects Tab
- Groups containers by Docker Compose project
- Visual status indicators (running/stopped) per project
- Container details including image, status, and ports

### Images Tab
- Full list of Docker images sorted by size
- Color-coded safety classification:
  - **Green** — used by a running/existing container (protected)
  - **Red** — not used by any container (safe to remove)
  - **Yellow** — dangling images (`<none>`)
- Selective or bulk removal of unused images

### Volumes Tab
- Lists all Docker volumes with sizes
- Identifies orphan volumes not attached to any container
- Double-click to open volume mountpoint in Finder (macOS)
- Selective or bulk removal of orphan volumes

### Build Cache Tab
- Displays total build cache size and reclaimable space
- One-click cache clearing

### Safe Clean All
- Combines all cleanup actions into a single operation
- Removes build cache, unused images, and orphan volumes
- Never touches resources used by existing containers

## Installation

### Prerequisites

- **Python 3.8+**
- **Docker** installed and running
- **Tkinter** (included with most Python installations)

### Setup

```bash
git clone https://github.com/matthew-devOP/docker-space-manager.git
cd docker-space-manager
```

#### macOS (recommended)

Install `tkmacosx` for proper button color rendering on macOS:

```bash
pip install tkmacosx
```

> Without this package, buttons will still work but may not display custom colors correctly due to a macOS Tkinter limitation.

### Run

```bash
python app.py
```

## How It Works

The application queries Docker using CLI commands (`docker ps`, `docker images`, `docker volume ls`, `docker system df`) and presents the data in a tabbed interface. All data collection runs in background threads to keep the UI responsive.

### Safety Model

Before removing any resource, the app checks whether it is actively used:

- **Images** — checks if any existing container references the image
- **Volumes** — inspects container mounts to determine if a volume is attached
- **Build cache** — always safe to clear (rebuilds are just slower)

Resources in use are visually marked and protected from deletion. Confirmation dialogs are shown before every destructive action.

## Tech Stack

- **Python 3** with **Tkinter** for the GUI
- **ttk** themed widgets with a custom dark color scheme
- **tkmacosx** (optional) for native macOS button rendering
- **subprocess** for Docker CLI integration
- **threading** for non-blocking data loading

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License.
