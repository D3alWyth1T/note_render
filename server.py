"""
Note Display Server - A lightweight markdown note viewer.

Usage:
    .venv/bin/python server.py [--port PORT] [--notes-dir PATH]

Configuration via environment variables or .env file:
    NOTES_DIR       - Path to notes directory (default: ~/notes)
    DEFAULT_NOTE    - Default note to show on homepage (default: homepage)
    PORT            - Port to run on (default: 5000)
"""

__version__ = "1.0.2"

import argparse
import fnmatch
import os
import queue
import re
import threading
import time
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
def index():
    """Serve the homepage."""
    return serve_note(DEFAULT_NOTE)


@app.route("/search")
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
def graph():
    """Serve the graph visualization page."""
    return render_template("graph.html")


@app.route("/api/graph-data")
def graph_data():
    """Return graph data as JSON for visualization."""
    return jsonify(build_graph_data())


@app.route("/api/local-graph")
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


def main():
    """Main entry point."""
    global NOTES_DIR, DEFAULT_NOTE, ALLOW_ALL_PATHS  # pylint: disable=global-statement

    default_port = int(os.environ.get("PORT", 5000))

    parser = argparse.ArgumentParser(
        description="Note Display Server - A lightweight markdown note viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (can also be set in .env file):
  NOTES_DIR        Path to notes directory
  DEFAULT_NOTE     Default note for homepage
  PORT             Server port
  ALLOW_ALL_PATHS  Allow accessing files outside notes directory (DANGEROUS)
        """,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"Port to run on (default: {default_port})",
    )
    parser.add_argument(
        "--notes-dir",
        type=Path,
        default=NOTES_DIR,
        help=f"Notes directory (default: {NOTES_DIR})",
    )
    parser.add_argument(
        "--default-note",
        type=str,
        default=DEFAULT_NOTE,
        help=f"Default note to show on homepage (default: {DEFAULT_NOTE})",
    )
    parser.add_argument(
        "--allow-all-paths",
        action="store_true",
        default=ALLOW_ALL_PATHS,
        help="Allow accessing files outside notes directory (DANGEROUS)",
    )
    args = parser.parse_args()

    NOTES_DIR = args.notes_dir.resolve()
    DEFAULT_NOTE = args.default_note
    ALLOW_ALL_PATHS = args.allow_all_paths

    print(f"Serving notes from: {NOTES_DIR}")
    print(f"Homepage: {DEFAULT_NOTE}")
    if ALLOW_ALL_PATHS:
        print("WARNING: Path traversal protection DISABLED - all files accessible")
    print(f"Running on http://localhost:{args.port}")

    # Start file watcher for live reload
    start_file_watcher()
    print("Live reload enabled (watching for file changes)")

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
