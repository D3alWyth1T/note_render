# Note Display

**v1.0.0**

A lightweight Python web server for viewing markdown notes in a browser. Designed to work with note-taking setups like [Telekasten](https://github.com/nvim-telekasten/telekasten.nvim) or [Obsidian](https://obsidian.md/).

## Features

- Dark mode interface
- `[[wiki-link]]` support with automatic resolution
- Standard markdown link support
- Full-text search across all notes
- Sidebar with 25 most recently modified notes
- Table of contents anchor links
- Serves images and other static files from your notes directory
- Path-based URLs that mirror your folder structure

## Quick Start

```bash
# Clone the repository
git clone https://github.com/D3alWyth1T/note_render.git
cd note_display

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure (copy and edit .env)
cp .env.example .env
# Edit .env to set your NOTES_DIR

# Run the server
python server.py
```

Open http://localhost:5000 in your browser.

## Configuration

Configuration can be done via environment variables, a `.env` file, or command-line arguments.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NOTES_DIR` | Path to your notes directory | `~/notes` |
| `DEFAULT_NOTE` | Note to show on homepage (without `.md`) | `homepage` |
| `PORT` | Server port | `5000` |

### Command-Line Arguments

```bash
python server.py --help

# Examples
python server.py --port 8080
python server.py --notes-dir /path/to/notes
python server.py --default-note index
```

Command-line arguments override environment variables.

## Notes Directory Structure

The server expects markdown files (`.md`) in your notes directory. Example structure:

```
~/notes/
├── homepage.md          # Default landing page
├── daily-note.md
├── project-ideas.md
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

Headings automatically get anchor IDs matching GitHub-style slugs, so TOC links work:

```markdown
## My Section

[Jump to section](#my-section)
```

## Integration with Telekasten (Neovim)

This server pairs well with [Telekasten](https://github.com/nvim-telekasten/telekasten.nvim). Recommended Telekasten config:

```lua
require('telekasten').setup({
  home = vim.fn.expand("~/notes"),
  image_subdir = vim.fn.expand('~/notes/img'),
  image_link_style = "markdown",  -- Use markdown style for proper paths
})

-- Keybinding to paste images from clipboard
vim.keymap.set("n", "<leader>ni", "<cmd>Telekasten paste_img_and_link<CR>")
```

**Note:** Use `image_link_style = "markdown"` (not `"wiki"`) to include the `img/` path in image links.

### Clipboard Requirements (Linux)

For image pasting on Linux, install clipboard tools:
- **Wayland:** `wl-clipboard` (`wl-paste`, `wl-copy`)
- **X11:** `xclip` or `xsel`

## Running on Startup

### Systemd (Linux)

Create `~/.config/systemd/user/note-display.service`:

```ini
[Unit]
Description=Note Display Server

[Service]
Type=simple
WorkingDirectory=/path/to/note_display
ExecStart=/path/to/note_display/.venv/bin/python server.py
Restart=on-failure

[Install]
WantedBy=default.target
```

Then:
```bash
systemctl --user enable note-display
systemctl --user start note-display
```

### Simple Background Process

```bash
/path/to/note_display/.venv/bin/python /path/to/note_display/server.py &
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
