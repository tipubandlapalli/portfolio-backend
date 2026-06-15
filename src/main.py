# from fastapi import FastAPI

# app = FastAPI()

# @app.get("/")
# def root():
#     return "hello dear: happy coding dear"


############!/usr/bin/env python3
"""Local search app for the voter-list PDFs in the ./pdfs directory."""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sqlite3
import subprocess
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "pdfs"
DB_PATH = BASE_DIR / "pdf_search_index.sqlite3"

index_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "current": "",
    "error": "",
    "warnings": [],
}
state_lock = threading.Lock()


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>2002 Voter List Search</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #f4f6f8; color: #17202a; }
    main { max-width: 850px; margin: 55px auto; padding: 0 18px; }
    .card { background: white; padding: 28px; border-radius: 14px; box-shadow: 0 5px 24px #0001; }
    h1 { margin: 0 0 8px; font-size: 1.7rem; }
    p { color: #566573; }
    form { display: flex; gap: 10px; margin: 24px 0 14px; }
    input { flex: 1; min-width: 0; padding: 13px 15px; font-size: 1rem; border: 1px solid #b9c3cc; border-radius: 8px; }
    button { padding: 0 22px; border: 0; border-radius: 8px; background: #1769aa; color: white; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: wait; }
    #status { min-height: 24px; color: #566573; }
    ul { padding: 0; list-style: none; }
    li { margin-top: 9px; border: 1px solid #e1e6ea; border-radius: 8px; }
    li a { display: block; padding: 13px 15px; color: #135f99; text-decoration: none; font-weight: 650; }
    li a:hover { background: #f5faff; }
  </style>
</head>
<body><main><div class="card">
  <h1>2002 Voter List Search</h1>
  <p>Search a door number, serial number, voter ID, or any searchable text.</p>
  <form id="form">
    <input id="query" autocomplete="off" placeholder="Example: 3-20" required autofocus>
    <button id="searchButton">Search</button>
  </form>
  <div id="status">Checking PDF index...</div>
  <ul id="results"></ul>
</div></main>
<script>
const form = document.querySelector('#form');
const query = document.querySelector('#query');
const button = document.querySelector('#searchButton');
const statusBox = document.querySelector('#status');
const results = document.querySelector('#results');
let indexing = true;

async function checkStatus() {
  try {
    const s = await (await fetch('/api/status')).json();
    indexing = s.running;
    button.disabled = indexing;
    if (s.error) statusBox.textContent = 'Index error: ' + s.error;
    else if (s.running) statusBox.textContent = `Indexing PDFs: ${s.done} / ${s.total} ${s.current || ''}`;
    else statusBox.textContent = `Ready. ${s.total} PDFs checked.${s.warnings.length ? ` ${s.warnings.length} could not be indexed.` : ''}`;
    if (s.running) setTimeout(checkStatus, 700);
  } catch (_) {
    statusBox.textContent = 'Unable to read index status.';
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (indexing) return;
  const q = query.value.trim();
  if (!q) return;
  button.disabled = true;
  statusBox.textContent = 'Searching...';
  results.replaceChildren();
  try {
    const data = await (await fetch('/api/search?q=' + encodeURIComponent(q))).json();
    statusBox.textContent = data.matches.length
      ? `${data.matches.length} PDF${data.matches.length === 1 ? '' : 's'} found with 2 or more matches.`
      : 'No PDF contains this text 2 or more times.';
    for (const match of data.matches) {
      const li = document.createElement('li');
      const a = document.createElement('a');
      a.href = '/pdf/' + encodeURIComponent(match.filename);
      a.target = '_blank';
      a.textContent = `${match.filename} (${match.count} matches)`;
      li.appendChild(a);
      results.appendChild(li);
    }
  } catch (_) {
    statusBox.textContent = 'Search failed. Please try again.';
  } finally {
    button.disabled = false;
  }
});
checkStatus();
</script></body></html>"""


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS documents (
               filename TEXT PRIMARY KEY,
               size INTEGER NOT NULL,
               modified_ns INTEGER NOT NULL,
               content TEXT NOT NULL
           )"""
    )
    return connection


def build_index() -> None:
    extractor = Path("/usr/bin/pdftotext")
    if not extractor.is_file():
        found_extractor = shutil.which("pdftotext")
        extractor = Path(found_extractor) if found_extractor else extractor
    if not extractor.is_file():
        with state_lock:
            index_state["error"] = "The pdftotext command is not installed."
        return

    pdfs = sorted(PDF_DIR.glob("*.pdf"), key=lambda path: path.name.lower())
    with state_lock:
        index_state.update(
            running=True, done=0, total=len(pdfs), current="", error="", warnings=[]
        )

    try:
        with connect_db() as database:
            existing = {
                row[0]: (row[1], row[2])
                for row in database.execute("SELECT filename, size, modified_ns FROM documents")
            }
            current_names = {pdf.name for pdf in pdfs}
            database.executemany(
                "DELETE FROM documents WHERE filename = ?",
                ((name,) for name in existing.keys() - current_names),
            )

            for number, pdf in enumerate(pdfs, 1):
                stat = pdf.stat()
                with state_lock:
                    index_state["current"] = pdf.name

                if existing.get(pdf.name) != (stat.st_size, stat.st_mtime_ns):
                    result = subprocess.run(
                        [str(extractor), "-layout", "-enc", "UTF-8", str(pdf), "-"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    if result.returncode != 0:
                        message = result.stderr.decode("utf-8", errors="replace").strip()
                        with state_lock:
                            index_state["warnings"].append(
                                f"{pdf.name}: {message or 'text extraction failed'}"
                            )
                    else:
                        content = result.stdout.decode("utf-8", errors="replace").lower()
                        database.execute(
                            """INSERT INTO documents(filename, size, modified_ns, content)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(filename) DO UPDATE SET
                                 size=excluded.size,
                                 modified_ns=excluded.modified_ns,
                                 content=excluded.content""",
                            (pdf.name, stat.st_size, stat.st_mtime_ns, content),
                        )
                        database.commit()

                with state_lock:
                    index_state["done"] = number
    except Exception as exc:
        with state_lock:
            index_state["error"] = str(exc)
    finally:
        with state_lock:
            index_state["running"] = False
            index_state["current"] = ""


def search_pdfs(query: str, minimum_count: int = 2) -> list[dict[str, object]]:
    normalized_query = query.lower()
    # ESCAPE makes %, _, and backslash literal search characters in the SQL pre-filter.
    escaped = normalized_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with connect_db() as database:
        rows = database.execute(
            "SELECT filename, content FROM documents WHERE content LIKE ? ESCAPE '\\'",
            (f"%{escaped}%",),
        ).fetchall()
    matches = [
        {"filename": filename, "count": content.count(normalized_query)}
        for filename, content in rows
        if content.count(normalized_query) >= minimum_count
    ]
    return sorted(matches, key=lambda match: (-int(match["count"]), str(match["filename"]).lower()))


class RequestHandler(BaseHTTPRequestHandler):
    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/status":
            with state_lock:
                state = dict(index_state)
            self.send_bytes(json.dumps(state).encode(), "application/json")
            return

        if parsed.path == "/api/search":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if not query:
                self.send_bytes(b'{"matches": []}', "application/json")
                return
            with state_lock:
                running = index_state["running"]
            matches = [] if running else search_pdfs(query)
            self.send_bytes(json.dumps({"matches": matches}).encode(), "application/json")
            return

        if parsed.path.startswith("/pdf/"):
            filename = Path(urllib.parse.unquote(parsed.path[5:])).name
            pdf = PDF_DIR / filename
            if pdf.is_file() and pdf.suffix.lower() == ".pdf":
                self.send_bytes(pdf.read_bytes(), "application/pdf")
            else:
                self.send_error(404, "PDF not found")
            return

        self.send_error(404)

    def log_message(self, format_string: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Search all PDFs in the pdfs folder.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not PDF_DIR.is_dir():
        raise SystemExit(f"PDF folder not found: {PDF_DIR}")

    # Create the schema before accepting requests, then update it in the background.
    connect_db().close()
    threading.Thread(target=build_index, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Voter-list search is running at {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
