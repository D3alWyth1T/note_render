# Note Render
<!-- md_toc GFM -->

* [Note Render](#note-render)
    * [Features](#features)
    * [Quick Start](#quick-start)
    * [Authentication](#authentication)
        * [User Management](#user-management)
        * [API Keys](#api-keys)
    * [Configuration](#configuration)
        * [Environment Variables](#environment-variables)
        * [Command-Line Arguments](#command-line-arguments)
    * [Network Access](#network-access)
    * [API Endpoints](#api-endpoints)
    * [Notes Directory Structure](#notes-directory-structure)
    * [Supported Features](#supported-features)
        * [Wiki Links](#wiki-links)
        * [Task Lists](#task-lists)
        * [Tables](#tables)
        * [Images and Static Files](#images-and-static-files)
        * [Table of Contents](#table-of-contents)
    * [Graph Visualization](#graph-visualization)
    * [Live Reload](#live-reload)
    * [Integration with Telekasten (Neovim)](#integration-with-telekasten-neovim)
        * [Clipboard Requirements (Linux)](#clipboard-requirements-linux)
    * [Running on Startup](#running-on-startup)
        * [Systemd (Linux)](#systemd-linux)
    * [Development](#development)
    * [License](#license)

<!-- md_toc -->

**v1.2.0**

A lightweight Python web server for viewing and editing markdown notes in a browser. Features authentication, live reload, graph visualization, and API access for integrations. Designed to work with note-taking setups like [Telekasten](https://github.com/nvim-telekasten/telekasten.nvim) or [Obsidian](https://obsidian.md/).

## Features

- **Authentication** - HTTP Basic Auth + API keys for programmatic access
- **Dark mode interface** - Easy on the eyes
- **Live reload** - Notes update in browser when files change
- **In-browser editing** - Edit notes directly with save/cancel
- **Interactive task lists** - Click checkboxes to toggle them
- **Graph visualization** - D3.js force-directed graph of note connections
- **`[[wiki-link]]` support** - Automatic resolution across directories
- **Full-text search** - Search across all notes with snippets
- **Recently modified sidebar** - Quick access to recent notes
- **Local graph panel** - See connected notes for current page
- **API endpoints** - For bot/automation integrations

## Quick Start

```bash
# Clone the repository
git clone https://github.com/D3alWyth1T/note_render.git
cd note_render

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure (copy and edit .env)
cp .env.example .env
# Edit .env to set your NOTES_DIR

# Create your first user
python server.py adduser admin

# Run the server
python server.py serve
```

Open http://localhost:5000 in your browser.

## Authentication

All routes require authentication via HTTP Basic Auth (browser login prompt) or API key header.

### User Management

```bash
# Add a user (prompts for password)
python server.py adduser <username>

# Delete a user
python server.py deluser <username>

# List all users
python server.py listusers
```

### API Keys

API keys provide reliable authentication for bots and scripts.

```bash
# Create an API key
python server.py apikey-create <username> --name "My Bot"
# Save the key - it cannot be retrieved again!

# List API keys
python server.py apikey-list
python server.py apikey-list <username>

# Revoke a key (use first 8 characters)
python server.py apikey-revoke <prefix>
```

Use API keys with the `X-API-Key` header:

```bash
curl -H 'X-API-Key: <your-key>' http://localhost:5000/search?q=topic
```

## Configuration

Configuration can be done via environment variables, a `.env` file, or command-line arguments.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NOTES_DIR` | Path to your notes directory | `~/notes` |
| `DEFAULT_NOTE` | Note to show on homepage (without `.md`) | `homepage` |
| `PORT` | Server port | `5000` |
| `HOST` | Host to bind to | `127.0.0.1` |
| `DATABASE_PATH` | Path to user/API key database | `~/.note_render/users.db` |
| `GRAPH_EXCLUDE` | Comma-separated patterns to exclude from graph | |
| `ALLOW_ALL_PATHS` | Allow access outside NOTES_DIR (DANGEROUS) | `false` |

### Command-Line Arguments

```bash
python server.py serve --help

# Examples
python server.py serve --port 8080
python server.py serve --host 0.0.0.0
python server.py serve --notes-dir /path/to/notes
python server.py serve --default-note index
```

Command-line arguments override environment variables.

## Network Access

By default, the server binds to `127.0.0.1` (localhost only). To expose on your network:

```bash
# Via environment variable
HOST=0.0.0.0 python server.py serve

# Via command line
python server.py serve --host 0.0.0.0
```

**Important:** When binding to `0.0.0.0`, use a reverse proxy (nginx, caddy) with HTTPS in production. Basic Auth credentials are sent with every request and should be encrypted in transit.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Homepage (default note) |
| `/<path>` | GET | Rendered note or static file |
| `/search?q=<query>` | GET | Search notes |
| `/graph` | GET | Graph visualization page |
| `/api/graph-data` | GET | JSON graph data (nodes/edges) |
| `/api/local-graph?path=<path>` | GET | Connected notes for a note |
| `/api/get-note-raw?path=<path>` | GET | Raw markdown content |
| `/api/save-note` | POST | Save note content |
| `/api/toggle-checkbox` | POST | Toggle task list checkbox |
| `/api/events` | GET | Server-Sent Events for live reload |

Run `python server.py --help` for CLI documentation.

## Notes Directory Structure

The server expects markdown files (`.md`) in your notes directory. Example structure:

```
~/notes/
├── homepage.md          # Default landing page
├── daily-note.md
├── project-ideas.md
├── day/
│   └── 2026-01-21.md    # Daily notes
├── img/
│   └── screenshot.png
└── templates/
    └── daily.md
```

## Supported Features

### Wiki Links

Links in `[[double brackets]]` are automatically resolved:

```markdown
Check out my [[project-ideas]] note.
```

Resolution order:
1. Current directory
2. Root notes directory
3. Subdirectories

### Task Lists

Interactive checkboxes that persist to the markdown file:

```markdown
- [x] Completed task
- [ ] Pending task
```

Click a checkbox in the browser to toggle it.

### Tables

GitHub-flavored markdown tables are supported:

```markdown
| Column 1 | Column 2 |
|----------|----------|
| Cell 1   | Cell 2   |
```

### Images and Static Files

Images and other files are served directly. Supported extensions:
- Images: `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.webp`
- Documents: `.pdf`, `.doc`, `.docx`, `.xls`, `.xlsx`
- Media: `.mp3`, `.mp4`, `.wav`, `.webm`
- Data: `.json`, `.csv`, `.txt`
- Archives: `.zip`, `.tar`, `.gz`

Example markdown:
```markdown
![Screenshot](img/screenshot.png)
```

### Table of Contents

Headings automatically get anchor IDs matching GitHub-style slugs:

```markdown
## My Section

[Jump to section](#my-section)
```

## Graph Visualization

Access the graph at `/graph` to see a force-directed visualization of all notes and their wiki-link connections.

- Node size reflects connection count
- Drag nodes to rearrange
- Click nodes to navigate
- Zoom and pan supported
- Exclude notes with `GRAPH_EXCLUDE` env var

The local graph panel (toggle with "Links" button) shows connections for the current note.

## Live Reload

The server watches your notes directory for changes. When you save a file in your editor, the browser automatically refreshes to show the updated content.

- Uses watchdog for file system monitoring
- Server-Sent Events for instant updates
- Won't interrupt if you're editing in the browser

## Integration with Telekasten (Neovim)

This server pairs well with [Telekasten](https://github.com/nvim-telekasten/telekasten.nvim). Recommended config:

```lua
require('telekasten').setup({
  home = vim.fn.expand("~/notes"),
  image_subdir = vim.fn.expand('~/notes/img'),
  image_link_style = "markdown",
})

vim.keymap.set("n", "<leader>ni", "<cmd>Telekasten paste_img_and_link<CR>")
```

**Note:** Use `image_link_style = "markdown"` to include the `img/` path in image links.

### Clipboard Requirements (Linux)

For image pasting on Linux:
- **Wayland:** `wl-clipboard` (`wl-paste`, `wl-copy`)
- **X11:** `xclip` or `xsel`

## Running on Startup

### Systemd (Linux)

Create `~/.config/systemd/user/note-render.service`:

```ini
[Unit]
Description=Note Render Server

[Service]
Type=simple
WorkingDirectory=/path/to/note_render
ExecStart=/path/to/note_render/.venv/bin/python server.py serve
Restart=on-failure
Environment="NOTES_DIR=/home/user/notes"

[Install]
WantedBy=default.target
```

Then:
```bash
systemctl --user daemon-reload
systemctl --user enable note-render
systemctl --user start note-render
```

## Development

```bash
# Install dev dependencies
pip install black isort pylint

# Format code
isort server.py && black server.py

# Lint
pylint server.py
```

## License

MIT
