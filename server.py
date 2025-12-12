"""
Note Display Server - A lightweight markdown note viewer.

Usage:
    .venv/bin/python server.py [--port PORT] [--notes-dir PATH]

Configuration via environment variables or .env file:
    NOTES_DIR       - Path to notes directory (default: ~/notes)
    DEFAULT_NOTE    - Default note to show on homepage (default: homepage)
    PORT            - Port to run on (default: 5000)
"""

__version__ = "1.0.1"

import argparse
import os
import re
from pathlib import Path

import mistune
from flask import Flask, abort, render_template, request, send_from_directory

# Load .env file if present
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional

app = Flask(__name__)

# Configuration from environment, can be overridden via command line
NOTES_DIR = Path(os.environ.get("NOTES_DIR", Path.home() / "notes"))
DEFAULT_NOTE = os.environ.get("DEFAULT_NOTE", "homepage")

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


class HeadingRenderer(mistune.HTMLRenderer):
    """Custom renderer that adds IDs to headings for anchor links."""

    def heading(self, text: str, level: int, **attrs) -> str:
        """Render heading with slugified ID for anchor linking."""
        slug = slugify(text)
        return f'<h{level} id="{slug}">{text}</h{level}>\n'


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


def render_markdown(content: str, current_dir: Path) -> str:
    """Render markdown content to HTML with wiki-link support."""
    # Pre-process wiki-links to standard markdown links
    content = preprocess_wiki_links(content, current_dir)
    renderer = HeadingRenderer()
    markdown = mistune.create_markdown(
        renderer=renderer,
        plugins=["table", "strikethrough"],
    )
    return markdown(content)


def get_note_path(note_name: str) -> Path | None:
    """
    Get the full path to a note file.

    Returns None if the note doesn't exist.
    """
    # Add .md extension if not present
    if not note_name.endswith(".md"):
        note_name = note_name + ".md"

    note_path = NOTES_DIR / note_name

    # Security: ensure we're not escaping the notes directory
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
    recent_notes = get_recent_notes()

    if not query:
        return render_template(
            "base.html",
            title="Search",
            content="<p>Enter a search term.</p>",
            recent_notes=recent_notes,
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
        recent_notes=recent_notes,
    )


@app.route("/<path:note_path>")
def serve_note(note_path: str):
    """Serve a markdown note or static file."""
    # Check if this is a static file request
    file_path = NOTES_DIR / note_path
    if file_path.suffix.lower() in STATIC_EXTENSIONS:
        # Security check: ensure path is within notes directory
        try:
            resolved = file_path.resolve()
            resolved.relative_to(NOTES_DIR.resolve())
        except ValueError:
            abort(404)

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
    recent_notes = get_recent_notes()

    return render_template(
        "base.html", title=title, content=html_content, recent_notes=recent_notes
    )


@app.errorhandler(404)
def not_found(_):
    """Handle 404 errors."""
    recent_notes = get_recent_notes()
    return (
        render_template(
            "base.html",
            title="Not Found",
            content="<p>The requested note does not exist.</p>",
            recent_notes=recent_notes,
        ),
        404,
    )


def main():
    """Main entry point."""
    global NOTES_DIR, DEFAULT_NOTE  # pylint: disable=global-statement

    default_port = int(os.environ.get("PORT", 5000))

    parser = argparse.ArgumentParser(
        description="Note Display Server - A lightweight markdown note viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (can also be set in .env file):
  NOTES_DIR       Path to notes directory
  DEFAULT_NOTE    Default note for homepage
  PORT            Server port
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
    args = parser.parse_args()

    NOTES_DIR = args.notes_dir.resolve()
    DEFAULT_NOTE = args.default_note

    print(f"Serving notes from: {NOTES_DIR}")
    print(f"Homepage: {DEFAULT_NOTE}")
    print(f"Running on http://localhost:{args.port}")

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
