"""
Note Display Server - A lightweight markdown note viewer with authentication.

Usage:
    python server.py serve [--port PORT] [--host HOST] [--notes-dir PATH]
    python server.py adduser <username>
    python server.py deluser <username>
    python server.py listusers
    python server.py apikey-create <username> [--name NAME]
    python server.py apikey-revoke <prefix>
    python server.py apikey-list [username]

Authentication:
    - HTTP Basic Auth (browser login prompt)
    - API Key via X-API-Key header (for bots/scripts)

Configuration via environment variables or .env file:
    NOTES_DIR       - Path to notes directory (default: ~/notes)
    DEFAULT_NOTE    - Default note to show on homepage (default: homepage)
    PORT            - Port to run on (default: 5000)
    HOST            - Host to bind to (default: 127.0.0.1)
    DATABASE_PATH   - Path to user database (default: ~/.note_render/users.db)
"""

__version__ = "1.2.0"

import argparse
import fnmatch
import getpass
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
from functools import wraps
from pathlib import Path

import mistune
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from werkzeug.security import check_password_hash, generate_password_hash

# Load .env file if present
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional

app = Flask(__name__)

# Configuration from environment, can be overridden via command line
NOTES_DIR = Path(os.environ.get("NOTES_DIR", Path.home() / "notes")).expanduser()
DEFAULT_NOTE = os.environ.get("DEFAULT_NOTE", "homepage")
ALLOW_ALL_PATHS = os.environ.get("ALLOW_ALL_PATHS", "").lower() in ("true", "1", "yes")
GRAPH_EXCLUDE_PATTERNS = [
    pattern.strip().lower()
    for pattern in os.environ.get("GRAPH_EXCLUDE", "").split(",")
    if pattern.strip()
]
DATABASE_PATH = Path(
    os.environ.get("DATABASE_PATH", "~/.note_render/users.db")
).expanduser()


def init_db() -> None:
    """Initialize the SQLite database for user authentication."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key_hash TEXT UNIQUE NOT NULL,
            key_prefix TEXT NOT NULL,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)
    conn.commit()
    conn.close()
    # Set restrictive permissions on database file (owner read/write only)
    DATABASE_PATH.chmod(0o600)


def get_db() -> sqlite3.Connection:
    """Get a database connection."""
    return sqlite3.connect(DATABASE_PATH)


def check_auth(username: str, password: str) -> bool:
    """Verify username and password against the database."""
    if not DATABASE_PATH.exists():
        return False
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password_hash FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return False
    return check_password_hash(row[0], password)


def create_api_key(username: str, name: str | None = None) -> str | None:
    """Create a new API key for a user. Returns the key (only shown once)."""
    if not DATABASE_PATH.exists():
        return None
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return None

    user_id = row[0]
    # Generate a secure random key (32 bytes = 64 hex chars)
    raw_key = secrets.token_hex(32)
    key_prefix = raw_key[:8]  # Store prefix for identification
    key_hash = generate_password_hash(raw_key)

    cursor.execute(
        "INSERT INTO api_keys (user_id, key_hash, key_prefix, name) VALUES (?, ?, ?, ?)",
        (user_id, key_hash, key_prefix, name),
    )
    conn.commit()
    conn.close()
    return raw_key


def check_api_key(api_key: str) -> bool:
    """Verify an API key against the database."""
    if not DATABASE_PATH.exists() or not api_key:
        return False
    conn = get_db()
    cursor = conn.cursor()
    # Use prefix to narrow down candidates (optimization)
    key_prefix = api_key[:8]
    cursor.execute("SELECT key_hash FROM api_keys WHERE key_prefix = ?", (key_prefix,))
    rows = cursor.fetchall()
    conn.close()
    for (key_hash,) in rows:
        if check_password_hash(key_hash, api_key):
            return True
    return False


def revoke_api_key(key_prefix: str) -> bool:
    """Revoke an API key by its prefix. Returns True if key was found and revoked."""
    if not DATABASE_PATH.exists():
        return False
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM api_keys WHERE key_prefix = ?", (key_prefix,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def list_api_keys(username: str | None = None) -> list[dict]:
    """List API keys, optionally filtered by username."""
    if not DATABASE_PATH.exists():
        return []
    conn = get_db()
    cursor = conn.cursor()
    if username:
        cursor.execute(
            """
            SELECT ak.key_prefix, ak.name, ak.created_at, u.username
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            WHERE u.username = ?
            ORDER BY ak.created_at DESC
            """,
            (username,),
        )
    else:
        cursor.execute("""
            SELECT ak.key_prefix, ak.name, ak.created_at, u.username
            FROM api_keys ak
            JOIN users u ON ak.user_id = u.id
            ORDER BY u.username, ak.created_at DESC
            """)
    rows = cursor.fetchall()
    conn.close()
    return [
        {"prefix": r[0], "name": r[1], "created_at": r[2], "username": r[3]}
        for r in rows
    ]


def requires_auth(f):
    """Decorator to require HTTP Basic Auth or API key on a route."""

    @wraps(f)
    def decorated(*args, **kwargs):
        # Check for API key first (preferred for bots)
        api_key = request.headers.get("X-API-Key")
        if api_key and check_api_key(api_key):
            return f(*args, **kwargs)

        # Fall back to Basic Auth
        auth = request.authorization
        if auth and check_auth(auth.username, auth.password):
            return f(*args, **kwargs)

        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="Note Render"'},
        )

    return decorated


# SSE clients - thread-safe list of queues for connected clients
sse_clients: list[queue.Queue] = []
sse_clients_lock = threading.Lock()


class NoteChangeHandler(FileSystemEventHandler):
    """Handle file system events and notify SSE clients."""

    def __init__(self, notes_dir: Path):
        self.notes_dir = notes_dir
        self._last_event_time: dict[str, float] = {}
        self._debounce_seconds = 0.5  # Debounce rapid events

    def _should_process(self, path: str) -> bool:
        """Check if we should process this event (debounce + filter)."""
        # Only process markdown files
        if not path.endswith(".md"):
            return False

        # Skip hidden files/directories
        if "/." in path or path.startswith("."):
            return False

        # Debounce: ignore if we just processed this file
        now = time.time()
        last_time = self._last_event_time.get(path, 0)
        if now - last_time < self._debounce_seconds:
            return False

        self._last_event_time[path] = now
        return True

    def _notify_clients(self, event_type: str, path: str):
        """Send event to all connected SSE clients."""
        try:
            rel_path = str(Path(path).relative_to(self.notes_dir))
        except ValueError:
            rel_path = path

        # Remove .md extension for the note path
        if rel_path.endswith(".md"):
            rel_path = rel_path[:-3]

        event_data = f"event: {event_type}\ndata: /{rel_path}\n\n"

        with sse_clients_lock:
            dead_clients = []
            for client_queue in sse_clients:
                try:
                    client_queue.put_nowait(event_data)
                except queue.Full:
                    dead_clients.append(client_queue)

            # Clean up dead clients
            for dead in dead_clients:
                sse_clients.remove(dead)

    def on_modified(self, event):
        if not event.is_directory and self._should_process(event.src_path):
            self._notify_clients("modified", event.src_path)

    def on_created(self, event):
        if not event.is_directory and self._should_process(event.src_path):
            self._notify_clients("created", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and event.src_path.endswith(".md"):
            self._notify_clients("deleted", event.src_path)


# Global observer instance (mutable singleton)
_file_observer: Observer | None = None  # pylint: disable=invalid-name


def start_file_watcher():
    """Start the file system watcher for NOTES_DIR."""
    global _file_observer  # pylint: disable=global-statement

    if _file_observer is not None:
        return  # Already running

    handler = NoteChangeHandler(NOTES_DIR)
    _file_observer = Observer()
    _file_observer.schedule(handler, str(NOTES_DIR), recursive=True)
    _file_observer.daemon = True
    _file_observer.start()


# Static file extensions to serve directly (not as markdown)
STATIC_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",  # Images
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",  # Documents
    ".mp3",
    ".mp4",
    ".wav",
    ".webm",  # Media
    ".zip",
    ".tar",
    ".gz",  # Archives
    ".json",
    ".csv",
    ".txt",  # Data
}


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug matching GitHub/TOC style."""
    # Lowercase
    slug = text.lower()
    # Replace spaces with hyphens
    slug = slug.replace(" ", "-")
    # Remove characters that aren't alphanumeric, hyphens, or underscores
    slug = re.sub(r"[^a-z0-9\-_]", "", slug)
    # Collapse multiple hyphens
    slug = re.sub(r"-+", "-", slug)
    return slug


def is_graph_excluded(note_path: str) -> bool:
    """Check if a note path matches any exclusion pattern."""
    note_path_lower = note_path.lower()
    for pattern in GRAPH_EXCLUDE_PATTERNS:
        # Check exact match (for simple names like "todo")
        if pattern == note_path_lower:
            return True
        # Check glob pattern match (for patterns like "dailies/*")
        if fnmatch.fnmatch(note_path_lower, pattern):
            return True
        # Also check just the note name against the pattern
        note_name = Path(note_path_lower).stem
        if pattern == note_name or fnmatch.fnmatch(note_name, pattern):
            return True
    return False


def extract_tags(content: str) -> set[str]:
    """Extract all @tags from content."""
    # Match @tag (alphanumeric and underscores/hyphens)
    pattern = r"(?<!\w)@([a-zA-Z][a-zA-Z0-9_-]*)"
    return set(re.findall(pattern, content))


def get_all_tags() -> list[dict]:
    """Get all unique tags from notes with their counts."""
    tag_counts: dict[str, int] = {}

    for md_file in NOTES_DIR.rglob("*.md"):
        # Skip hidden directories
        if any(part.startswith(".") for part in md_file.parts):
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
            tags = extract_tags(content)
            for tag in tags:
                tag_lower = tag.lower()
                tag_counts[tag_lower] = tag_counts.get(tag_lower, 0) + 1
        except (OSError, UnicodeDecodeError):
            continue

    # Sort by count (descending), then alphabetically
    sorted_tags = sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))
    return [{"name": tag, "count": count} for tag, count in sorted_tags]


class HeadingRenderer(mistune.HTMLRenderer):
    """Custom renderer that adds IDs to headings for anchor links."""

    def __init__(self) -> None:
        super().__init__()
        self.checkbox_index = 0

    def heading(self, text: str, level: int, **attrs) -> str:
        """Render heading with slugified ID for anchor linking."""
        slug = slugify(text)
        return f'<h{level} id="{slug}">{text}</h{level}>\n'

    def softbreak(self) -> str:
        """Render soft line breaks as HTML breaks for better readability."""
        return "<br>\n"

    def task_list_item(self, text: str, checked: bool = False) -> str:
        """Render task list item with interactive checkbox."""
        checkbox = (
            f'<input class="task-list-item-checkbox" type="checkbox" '
            f'data-checkbox-index="{self.checkbox_index}"'
        )
        if checked:
            checkbox += " checked"
        checkbox += "/>"

        self.checkbox_index += 1

        if text.startswith("<p>"):
            text = text.replace("<p>", "<p>" + checkbox, 1)
        else:
            text = checkbox + text

        return '<li class="task-list-item">' + text + "</li>\n"


def get_recent_notes(limit: int = 25) -> list[dict]:
    """Get the most recently modified notes."""
    notes = []

    for md_file in NOTES_DIR.rglob("*.md"):
        # Skip hidden directories
        if any(part.startswith(".") for part in md_file.parts):
            continue

        try:
            mtime = md_file.stat().st_mtime
            rel_path = md_file.relative_to(NOTES_DIR)
            notes.append(
                {
                    "title": md_file.stem,
                    "path": "/" + str(rel_path.with_suffix("")),
                    "mtime": mtime,
                }
            )
        except OSError:
            continue

    # Sort by modification time, most recent first
    notes.sort(key=lambda x: x["mtime"], reverse=True)
    return notes[:limit]


def is_within_notes_dir(path: Path) -> bool:
    """Check if a path is within the notes directory."""
    try:
        path.resolve().relative_to(NOTES_DIR.resolve())
        return True
    except ValueError:
        return False


def resolve_wiki_link(link_name: str, current_dir: Path) -> str:
    """
    Resolve a wiki-link to a URL path.

    Checks current directory first, then root notes directory.
    """
    # Clean the link name
    link_name = link_name.strip()

    # Check current directory first (only if result is within notes dir)
    current_path = (current_dir / f"{link_name}.md").resolve()
    if current_path.exists() and is_within_notes_dir(current_path):
        rel_path = current_path.relative_to(NOTES_DIR.resolve())
        return "/" + str(rel_path.with_suffix(""))

    # Check root directory
    root_path = (NOTES_DIR / f"{link_name}.md").resolve()
    if root_path.exists() and is_within_notes_dir(root_path):
        rel_path = root_path.relative_to(NOTES_DIR.resolve())
        return "/" + str(rel_path.with_suffix(""))

    # Check subdirectories
    for subdir in NOTES_DIR.iterdir():
        if subdir.is_dir() and not subdir.name.startswith("."):
            subdir_path = (subdir / f"{link_name}.md").resolve()
            if subdir_path.exists() and is_within_notes_dir(subdir_path):
                rel_path = subdir_path.relative_to(NOTES_DIR.resolve())
                return "/" + str(rel_path.with_suffix(""))

    # Link doesn't exist or is outside notes dir - create a dead link
    return "/" + link_name.replace("..", "").lstrip("/")


def preprocess_wiki_links(content: str, current_dir: Path) -> str:
    """Convert [[wiki-links]] to standard markdown links."""
    pattern = r"\[\[([^\]]+)\]\]"

    def replace_wiki_link(match: re.Match) -> str:
        link_text = match.group(1)
        url = resolve_wiki_link(link_text, current_dir)
        return f"[{link_text}]({url})"

    return re.sub(pattern, replace_wiki_link, content)


def preprocess_checkboxes(content: str) -> str:
    """Convert standalone checkboxes to GFM task list format.

    Converts lines like:
        [x] Task
        [ ] Task
    To:
        - [x] Task
        - [ ] Task
    """
    # Match lines starting with optional whitespace, then [ ] or [x]/[X], not already a list
    pattern = r"^(\s*)\[([xX ])\]"
    return re.sub(pattern, r"\1- [\2]", content, flags=re.MULTILINE)


def render_markdown(content: str, current_dir: Path) -> str:
    """Render markdown content to HTML with wiki-link support."""
    # Pre-process wiki-links to standard markdown links
    content = preprocess_wiki_links(content, current_dir)
    # Pre-process standalone checkboxes to GFM task list format
    content = preprocess_checkboxes(content)
    renderer = HeadingRenderer()
    markdown = mistune.create_markdown(
        renderer=renderer,
        plugins=["table", "strikethrough", "task_lists"],
    )
    return markdown(content)


def extract_wiki_links(content: str) -> list[str]:
    """Extract all wiki-link targets from markdown content."""
    pattern = r"\[\[([^\]]+)\]\]"
    return re.findall(pattern, content)


def resolve_linked_note_path(link_name: str) -> Path | None:
    """Try to resolve a wiki-link to an existing file path."""
    # Check in NOTES_DIR first
    note_path = NOTES_DIR / f"{link_name}.md"
    if note_path.exists():
        return note_path

    # Check subdirectories of NOTES_DIR
    for subdir in NOTES_DIR.iterdir():
        if subdir.is_dir() and not subdir.name.startswith("."):
            subdir_path = subdir / f"{link_name}.md"
            if subdir_path.exists():
                return subdir_path

    # If ALLOW_ALL_PATHS, try to find the file elsewhere
    if ALLOW_ALL_PATHS:
        # Try home directory paths
        home_path = Path.home() / f"{link_name}.md"
        if home_path.exists():
            return home_path

        # Try as absolute path with .md extension
        abs_path = Path(f"/{link_name}.md").expanduser()
        if abs_path.exists():
            return abs_path

    return None


def build_graph_data() -> dict:
    """Build graph data structure with nodes and edges from wiki-links."""
    note_map = {}  # Map lowercase name to node info
    pending_links = []  # Links to process after first pass

    # First pass: collect all notes from NOTES_DIR as nodes
    for md_file in NOTES_DIR.rglob("*.md"):
        # Skip hidden directories
        if any(part.startswith(".") for part in md_file.parts):
            continue

        name = md_file.stem
        name_lower = name.lower()
        rel_path_str = str(md_file.relative_to(NOTES_DIR).with_suffix(""))

        # Skip excluded notes
        if is_graph_excluded(rel_path_str):
            continue

        rel_path = "/" + rel_path_str

        note_map[name_lower] = {
            "id": name_lower,
            "label": name,
            "path": rel_path,
            "connections": 0,
            "file_path": md_file,
        }

    # Second pass: extract links and collect external links if ALLOW_ALL_PATHS
    for name_lower, node_info in list(note_map.items()):
        md_file = node_info["file_path"]

        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        links = extract_wiki_links(content)

        for link in links:
            target = link.lower()

            # Skip excluded targets
            if is_graph_excluded(target):
                continue

            # Skip self-links
            if target == name_lower:
                continue

            pending_links.append((name_lower, target))

            # If ALLOW_ALL_PATHS and target not in map, try to find it
            if ALLOW_ALL_PATHS and target not in note_map:
                resolved = resolve_linked_note_path(link)
                if resolved and resolved.exists():
                    # Add external note to the graph
                    try:
                        ext_rel_path = "/" + str(
                            resolved.relative_to(NOTES_DIR).with_suffix("")
                        )
                    except ValueError:
                        # File is outside NOTES_DIR, use path with ~
                        try:
                            ext_rel_path = "/~/" + str(
                                resolved.relative_to(Path.home()).with_suffix("")
                            )
                        except ValueError:
                            ext_rel_path = "/" + str(resolved.with_suffix(""))

                    note_map[target] = {
                        "id": target,
                        "label": resolved.stem,
                        "path": ext_rel_path,
                        "connections": 0,
                        "file_path": resolved,
                    }

    # Third pass: build edges from collected links
    edges = []
    for source, target in pending_links:
        if source in note_map and target in note_map:
            edges.append({"source": source, "target": target})
            note_map[source]["connections"] += 1
            note_map[target]["connections"] += 1

    # Remove file_path from output (not needed in JSON)
    nodes = []
    for node in note_map.values():
        nodes.append(
            {
                "id": node["id"],
                "label": node["label"],
                "path": node["path"],
                "connections": node["connections"],
            }
        )

    return {"nodes": nodes, "edges": edges}


def get_note_path(note_name: str) -> Path | None:
    """
    Get the full path to a note file.

    Returns None if the note doesn't exist.
    """
    # Add .md extension if not present
    if not note_name.endswith(".md"):
        note_name = note_name + ".md"

    # When ALLOW_ALL_PATHS is enabled, handle absolute and home paths
    if ALLOW_ALL_PATHS and (note_name.startswith("~") or note_name.startswith("/")):
        note_path = Path(note_name).expanduser()
    else:
        note_path = NOTES_DIR / note_name

    # Security: ensure we're not escaping the notes directory (unless allowed)
    if not ALLOW_ALL_PATHS:
        try:
            note_path.resolve().relative_to(NOTES_DIR.resolve())
        except ValueError:
            return None

    if note_path.exists() and note_path.is_file():
        return note_path

    return None


def search_notes(query: str) -> list[dict]:
    """
    Search all notes for the given query.

    Returns a list of dicts with 'title', 'path', and 'snippet'.
    """
    results = []
    query_lower = query.lower()

    for md_file in NOTES_DIR.rglob("*.md"):
        # Skip hidden directories
        if any(part.startswith(".") for part in md_file.parts):
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
            content_lower = content.lower()

            if query_lower in content_lower or query_lower in md_file.stem.lower():
                # Get a snippet around the match
                snippet = ""
                idx = content_lower.find(query_lower)
                if idx != -1:
                    start = max(0, idx - 50)
                    end = min(len(content), idx + len(query) + 50)
                    snippet = content[start:end]
                    if start > 0:
                        snippet = "..." + snippet
                    if end < len(content):
                        snippet = snippet + "..."

                rel_path = md_file.relative_to(NOTES_DIR)
                results.append(
                    {
                        "title": md_file.stem,
                        "path": "/" + str(rel_path.with_suffix("")),
                        "snippet": snippet,
                    }
                )
        except (OSError, UnicodeDecodeError):
            continue

    return results


@app.route("/")
@requires_auth
def index():
    """Serve the homepage."""
    return serve_note(DEFAULT_NOTE)


@app.route("/search")
@requires_auth
def search():
    """Handle search requests."""
    query = request.args.get("q", "").strip()
    tags = get_all_tags()

    if not query:
        return render_template(
            "base.html",
            title="Search",
            content="<p>Enter a search term.</p>",
            tags=tags,
        )

    results = search_notes(query)

    if not results:
        content = f"<p>No results found for: <strong>{query}</strong></p>"
    else:
        content = f"<h2>Search results for: {query}</h2><ul>"
        for result in results:
            snippet_html = (
                f"<br><small>{result['snippet']}</small>" if result["snippet"] else ""
            )
            content += f'<li><a href="{result["path"]}">{result["title"]}</a>{snippet_html}</li>'
        content += "</ul>"

    return render_template(
        "base.html",
        title=f"Search: {query}",
        content=content,
        tags=tags,
    )


@app.route("/graph")
@requires_auth
def graph():
    """Serve the graph visualization page."""
    return render_template("graph.html")


@app.route("/api/graph-data")
@requires_auth
def graph_data():
    """Return graph data as JSON for visualization."""
    return jsonify(build_graph_data())


@app.route("/api/local-graph")
@requires_auth
def local_graph_data():
    """Return graph data for a specific note and its connections."""
    note_path = request.args.get("path", "").lstrip("/")
    if not note_path:
        return jsonify({"error": "Missing path parameter"}), 400

    note_file = get_note_path(note_path)
    if note_file is None:
        return jsonify({"error": "Note not found"}), 404

    # Get the current note's name
    current_name = note_file.stem.lower()

    # Build full graph to find connections
    full_graph = build_graph_data()

    # Find all connected nodes (outgoing and incoming links)
    connected_ids = {current_name}
    for edge in full_graph["edges"]:
        if edge["source"] == current_name:
            connected_ids.add(edge["target"])
        elif edge["target"] == current_name:
            connected_ids.add(edge["source"])

    # Filter nodes and edges to only include connected ones
    nodes = [n for n in full_graph["nodes"] if n["id"] in connected_ids]
    edges = [
        e
        for e in full_graph["edges"]
        if e["source"] in connected_ids and e["target"] in connected_ids
    ]

    # Mark the current node
    for node in nodes:
        node["current"] = node["id"] == current_name

    return jsonify({"nodes": nodes, "edges": edges, "current": current_name})


@app.route("/api/events")
@requires_auth
def sse_events():
    """Server-Sent Events endpoint for live reload."""

    def event_stream():
        client_queue: queue.Queue = queue.Queue(maxsize=100)

        with sse_clients_lock:
            sse_clients.append(client_queue)

        try:
            # Send initial connection message
            yield "event: connected\ndata: ok\n\n"

            while True:
                try:
                    # Wait for events with timeout to allow checking connection
                    event_data = client_queue.get(timeout=30)
                    yield event_data
                except queue.Empty:
                    # Send keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_clients_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@app.route("/<path:note_path>")
@requires_auth
def serve_note(note_path: str):
    """Serve a markdown note or static file."""
    # Check if this is a static file request
    # When ALLOW_ALL_PATHS is enabled, handle absolute and home paths
    if ALLOW_ALL_PATHS and (note_path.startswith("~") or note_path.startswith("/")):
        file_path = Path(note_path).expanduser()
    else:
        file_path = NOTES_DIR / note_path

    if file_path.suffix.lower() in STATIC_EXTENSIONS:
        # Security check: ensure path is within notes directory (unless allowed)
        if not ALLOW_ALL_PATHS:
            try:
                resolved = file_path.resolve()
                resolved.relative_to(NOTES_DIR.resolve())
            except ValueError:
                abort(404)
        else:
            resolved = file_path.resolve()

        if resolved.exists() and resolved.is_file():
            return send_from_directory(
                resolved.parent, resolved.name, as_attachment=False
            )
        abort(404)

    # Handle as markdown note
    note_file = get_note_path(note_path)

    if note_file is None:
        abort(404)

    try:
        content = note_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        abort(500)

    current_dir = note_file.parent
    html_content = render_markdown(content, current_dir)
    title = note_file.stem
    tags = get_all_tags()

    return render_template("base.html", title=title, content=html_content, tags=tags)


@app.errorhandler(404)
def not_found(_):
    """Handle 404 errors."""
    tags = get_all_tags()
    return (
        render_template(
            "base.html",
            title="Not Found",
            content="<p>The requested note does not exist.</p>",
            tags=tags,
        ),
        404,
    )


def toggle_checkbox_in_content(content: str, checkbox_index: int, checked: bool) -> str:
    """Toggle the nth checkbox in markdown content."""
    pattern = r"\[[ xX]\]"
    matches = list(re.finditer(pattern, content))

    if checkbox_index < 0 or checkbox_index >= len(matches):
        raise ValueError(f"Checkbox index {checkbox_index} out of range")

    match = matches[checkbox_index]
    new_checkbox = "[x]" if checked else "[ ]"

    return content[: match.start()] + new_checkbox + content[match.end() :]


@app.route("/api/toggle-checkbox", methods=["POST"])
@requires_auth
def toggle_checkbox():
    """Toggle a checkbox in a markdown file."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "No JSON data provided"}), 400

    note_path = data.get("note_path")
    checkbox_index = data.get("checkbox_index")
    checked = data.get("checked")

    if note_path is None or checkbox_index is None or checked is None:
        return jsonify({"error": "Missing required fields"}), 400

    # Strip leading slash from path (window.location.pathname includes it)
    note_path = note_path.lstrip("/")
    note_file = get_note_path(note_path)
    if note_file is None:
        return jsonify({"error": "Note not found"}), 404

    try:
        content = note_file.read_text(encoding="utf-8")
        new_content = toggle_checkbox_in_content(content, checkbox_index, checked)
        note_file.write_text(new_content, encoding="utf-8")
        return jsonify({"success": True})
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/get-note-raw", methods=["GET"])
@requires_auth
def get_note_raw():
    """Get raw markdown content for editing."""
    note_path = request.args.get("path")

    if not note_path:
        return jsonify({"error": "Missing path parameter"}), 400

    # Strip leading slash from path (window.location.pathname includes it)
    note_path = note_path.lstrip("/")
    note_file = get_note_path(note_path)
    if note_file is None:
        return jsonify({"error": "Note not found"}), 404

    try:
        content = note_file.read_text(encoding="utf-8")
        return jsonify({"content": content})
    except (OSError, UnicodeDecodeError) as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/save-note", methods=["POST"])
@requires_auth
def save_note():
    """Save edited note content."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "No JSON data provided"}), 400

    note_path = data.get("note_path")
    content = data.get("content")

    if note_path is None or content is None:
        return jsonify({"error": "Missing required fields"}), 400

    # Strip leading slash from path (window.location.pathname includes it)
    note_path = note_path.lstrip("/")
    note_file = get_note_path(note_path)
    if note_file is None:
        return jsonify({"error": "Note not found"}), 404

    try:
        note_file.write_text(content, encoding="utf-8")
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


def cmd_adduser(args: argparse.Namespace) -> None:
    """Add a new user to the database."""
    init_db()
    password = getpass.getpass(f"Password for {args.username}: ")
    if not password:
        print("Error: Password cannot be empty")
        return
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: Passwords do not match")
        return

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (args.username, generate_password_hash(password)),
        )
        conn.commit()
        print(f"User '{args.username}' created successfully")
    except sqlite3.IntegrityError:
        print(f"Error: User '{args.username}' already exists")
    finally:
        conn.close()


def cmd_deluser(args: argparse.Namespace) -> None:
    """Delete a user from the database."""
    if not DATABASE_PATH.exists():
        print("Error: No users database found")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE username = ?", (args.username,))
    if cursor.rowcount == 0:
        print(f"Error: User '{args.username}' not found")
    else:
        print(f"User '{args.username}' deleted successfully")
    conn.commit()
    conn.close()


def cmd_listusers(_args: argparse.Namespace) -> None:
    """List all users in the database."""
    if not DATABASE_PATH.exists():
        print("No users database found")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username, created_at FROM users ORDER BY username")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("No users found")
        return

    print("Users:")
    for username, created_at in rows:
        print(f"  - {username} (created: {created_at})")


def cmd_apikey_create(args: argparse.Namespace) -> None:
    """Create a new API key for a user."""
    init_db()
    key = create_api_key(args.username, args.name)
    if key is None:
        print(f"Error: User '{args.username}' not found")
        return
    print(f"API key created for '{args.username}':")
    print(f"  {key}")
    print()
    print("Save this key - it cannot be retrieved again!")
    print(f"Use with: curl -H 'X-API-Key: {key}' <url>")


def cmd_apikey_revoke(args: argparse.Namespace) -> None:
    """Revoke an API key by its prefix."""
    if revoke_api_key(args.prefix):
        print(f"API key with prefix '{args.prefix}' revoked")
    else:
        print(f"Error: No API key found with prefix '{args.prefix}'")


def cmd_apikey_list(args: argparse.Namespace) -> None:
    """List API keys."""
    keys = list_api_keys(args.username if hasattr(args, "username") else None)
    if not keys:
        print("No API keys found")
        return
    print("API Keys:")
    for key in keys:
        name_str = f" ({key['name']})" if key["name"] else ""
        print(
            f"  - {key['prefix']}...{name_str} [{key['username']}] created: {key['created_at']}"
        )


def cmd_serve(args: argparse.Namespace) -> None:
    """Run the note server."""
    global NOTES_DIR, DEFAULT_NOTE, ALLOW_ALL_PATHS  # pylint: disable=global-statement

    # Initialize database if it doesn't exist
    init_db()

    # Check if any users exist
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    conn.close()

    if user_count == 0:
        print("WARNING: No users exist. Run 'server.py adduser <username>' to add one.")
        print("         Server will reject all requests until a user is created.")

    NOTES_DIR = args.notes_dir.resolve()
    DEFAULT_NOTE = args.default_note
    ALLOW_ALL_PATHS = args.allow_all_paths

    print(f"Serving notes from: {NOTES_DIR}")
    print(f"Homepage: {DEFAULT_NOTE}")
    if ALLOW_ALL_PATHS:
        print("WARNING: Path traversal protection DISABLED - all files accessible")

    host = args.host
    if host != "127.0.0.1":
        print(f"WARNING: Binding to {host} - ensure you are behind a reverse proxy!")

    print(f"Running on http://{host}:{args.port}")

    # Start file watcher for live reload
    start_file_watcher()
    print("Live reload enabled (watching for file changes)")

    app.run(host=host, port=args.port, debug=False)


def main():
    """Main entry point."""
    default_port = int(os.environ.get("PORT", 5000))
    default_host = os.environ.get("HOST", "127.0.0.1")

    parser = argparse.ArgumentParser(
        description="Note Display Server - A lightweight markdown note viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (can also be set in .env file):
  NOTES_DIR        Path to notes directory
  DEFAULT_NOTE     Default note for homepage
  PORT             Server port
  HOST             Host to bind to (default: 127.0.0.1)
  DATABASE_PATH    Path to user database (default: ~/.note_render/users.db)
  ALLOW_ALL_PATHS  Allow accessing files outside notes directory (DANGEROUS)
        """,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # serve command (default)
    serve_parser = subparsers.add_parser("serve", help="Run the note server")
    serve_parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"Port to run on (default: {default_port})",
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default=default_host,
        help=f"Host to bind to (default: {default_host})",
    )
    serve_parser.add_argument(
        "--notes-dir",
        type=Path,
        default=NOTES_DIR,
        help=f"Notes directory (default: {NOTES_DIR})",
    )
    serve_parser.add_argument(
        "--default-note",
        type=str,
        default=DEFAULT_NOTE,
        help=f"Default note to show on homepage (default: {DEFAULT_NOTE})",
    )
    serve_parser.add_argument(
        "--allow-all-paths",
        action="store_true",
        default=ALLOW_ALL_PATHS,
        help="Allow accessing files outside notes directory (DANGEROUS)",
    )
    serve_parser.set_defaults(func=cmd_serve)

    # adduser command
    adduser_parser = subparsers.add_parser("adduser", help="Add a new user")
    adduser_parser.add_argument("username", help="Username to add")
    adduser_parser.set_defaults(func=cmd_adduser)

    # deluser command
    deluser_parser = subparsers.add_parser("deluser", help="Delete a user")
    deluser_parser.add_argument("username", help="Username to delete")
    deluser_parser.set_defaults(func=cmd_deluser)

    # listusers command
    listusers_parser = subparsers.add_parser("listusers", help="List all users")
    listusers_parser.set_defaults(func=cmd_listusers)

    # apikey create command
    apikey_create_parser = subparsers.add_parser(
        "apikey-create", help="Create an API key for a user"
    )
    apikey_create_parser.add_argument("username", help="Username to create key for")
    apikey_create_parser.add_argument(
        "--name", "-n", help="Optional name/description for the key"
    )
    apikey_create_parser.set_defaults(func=cmd_apikey_create)

    # apikey revoke command
    apikey_revoke_parser = subparsers.add_parser(
        "apikey-revoke", help="Revoke an API key"
    )
    apikey_revoke_parser.add_argument(
        "prefix", help="First 8 characters of the key to revoke"
    )
    apikey_revoke_parser.set_defaults(func=cmd_apikey_revoke)

    # apikey list command
    apikey_list_parser = subparsers.add_parser("apikey-list", help="List API keys")
    apikey_list_parser.add_argument(
        "username", nargs="?", help="Optional: filter by username"
    )
    apikey_list_parser.set_defaults(func=cmd_apikey_list)

    args = parser.parse_args()

    # Default to serve if no command specified
    if args.command is None:
        # Re-parse with serve as default
        args = parser.parse_args(["serve"] + sys.argv[1:])

    args.func(args)


if __name__ == "__main__":
    main()
