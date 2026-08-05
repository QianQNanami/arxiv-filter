import argparse
import base64
import hmac
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


OUTPUT_DIR = Path(os.environ.get("ARXIV_REPORT_OUTPUT_DIR", "output"))
CONFIG_PATH = Path(os.environ.get("ARXIV_REPORT_CONFIG", "config.json"))
FILTER_SCRIPT = Path(os.environ.get("ARXIV_FILTER_SCRIPT", "filter.py"))
TASK_LOG = OUTPUT_DIR / "web_tasks.log"
ADMIN_USER = os.environ.get("ARXIV_REPORT_ADMIN_USER", "")
ADMIN_PASSWORD = os.environ.get("ARXIV_REPORT_ADMIN_PASSWORD", "")


def list_reports(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("report_*.html"), reverse=True)


def report_label(path: Path) -> str:
    label = path.stem.removeprefix("report_").replace("_", "-")
    return label or path.stem


def render_index(output_dir: Path, selected: str = "") -> str:
    reports = list_reports(output_dir)
    if not selected and reports:
        selected = reports[0].name

    options = []
    for report in reports:
        selected_attr = " selected" if report.name == selected else ""
        options.append(
            f'<option value="{html.escape(report.name)}"{selected_attr}>'
            f'{html.escape(report_label(report))}</option>'
        )

    iframe_src = f"/reports/{quote(selected)}" if selected else ""
    empty = ""
    if not reports:
        empty = """
        <section class="empty">
          <h2>No reports yet</h2>
          <p>Run <code>python filter.py --use-yesterday</code> to generate the first daily report.</p>
        </section>
        """

    latest_log = read_task_log()

    admin_enabled = bool(ADMIN_USER and ADMIN_PASSWORD)
    admin_notice = (
        "Admin actions require HTTP Basic authentication."
        if admin_enabled
        else "Admin actions are disabled until ARXIV_REPORT_ADMIN_USER and ARXIV_REPORT_ADMIN_PASSWORD are set."
    )
    disabled_attr = "" if admin_enabled else " disabled"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>arXiv Reports</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --panel: #ffffff;
      --ink: #1f2937;
      --muted: #6b7280;
      --line: #d9e0ea;
      --accent: #2563eb;
      --accent-soft: #e8f0ff;
      --shadow: 0 14px 36px rgba(31, 41, 55, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    }}
    header {{
      background: rgba(255, 255, 255, 0.92);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    nav {{
      max-width: 1200px;
      min-height: 60px;
      margin: 0 auto;
      padding: 0 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .brand {{ font-weight: 750; }}
    .shell {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 28px 24px 42px;
    }}
    .toolbar {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 18px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 18px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;
      margin-bottom: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
    }}
    p {{ margin: 6px 0 0; color: var(--muted); }}
    label {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 6px;
    }}
    select {{
      min-width: 260px;
      height: 40px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      padding: 0 12px;
      font: inherit;
    }}
    input, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      padding: 10px 12px;
      font: inherit;
    }}
    textarea {{
      min-height: 160px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
      font-size: 13px;
    }}
    button {{
      height: 40px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: white;
      padding: 0 14px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }}
    .row {{
      display: flex;
      gap: 10px;
      align-items: end;
    }}
    .row > div {{ flex: 1; }}
    pre {{
      margin: 10px 0 0;
      max-height: 180px;
      overflow: auto;
      background: #111827;
      color: #e5e7eb;
      border-radius: 8px;
      padding: 12px;
      font-size: 12px;
      line-height: 1.5;
    }}
    iframe {{
      width: 100%;
      height: calc(100vh - 170px);
      min-height: 620px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      box-shadow: var(--shadow);
    }}
    .empty {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 32px;
    }}
    @media (max-width: 760px) {{
      nav {{ padding: 0 16px; }}
      .shell {{ padding: 20px 16px 34px; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      select {{ width: 100%; min-width: 0; }}
      iframe {{ height: 72vh; min-height: 520px; }}
    }}
  </style>
</head>
<body>
  <header>
    <nav>
      <div class="brand">arXiv Paper Filter</div>
      <div>{len(reports)} reports</div>
    </nav>
  </header>
  <main class="shell">
    <section class="toolbar">
      <div>
        <h1>Daily Reports</h1>
        <p>Select a date to inspect the generated HTML report.</p>
      </div>
      <form method="get" action="/">
        <label for="date">Report date</label>
        <select id="date" name="date" onchange="this.form.submit()">
          {''.join(options)}
        </select>
      </form>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Regenerate Report</h2>
        <p>{html.escape(admin_notice)}</p>
        <form method="post" action="/rerun">
          <div class="row">
            <div>
              <label for="rerun-date">Report date</label>
              <input id="rerun-date" name="date" type="date" required>
            </div>
            <button type="submit"{disabled_attr}>Regenerate</button>
          </div>
        </form>
        <pre>{html.escape(latest_log)}</pre>
      </div>
      <div class="panel">
        <h2>Topics</h2>
        <p>{html.escape(admin_notice)}</p>
        <form method="post" action="/topics">
          <label for="topics">Configured topics</label>
          <textarea id="topics" name="topics"{disabled_attr}>{html.escape(format_topics_text())}</textarea>
          <p><button type="submit"{disabled_attr}>Save Topics</button></p>
        </form>
      </div>
    </section>
    {empty}
    {f'<iframe src="{iframe_src}" title="Selected report"></iframe>' if iframe_src else ''}
  </main>
</body>
</html>
"""


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")


def format_topics_text() -> str:
    try:
        config = load_config()
    except FileNotFoundError:
        return ""
    return "\n".join(config.get("topics", []))


def read_task_log() -> str:
    if not TASK_LOG.exists():
        return "No web tasks have been started yet."
    content = TASK_LOG.read_text(encoding="utf-8", errors="replace")
    return "\n".join(content.splitlines()[-30:])


def append_task_log(message: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(TASK_LOG, "a", encoding="utf-8") as f:
        f.write(message + "\n")


def is_valid_date(date_text: str) -> bool:
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def start_regenerate(date_text: str) -> None:
    if not is_valid_date(date_text):
        raise ValueError("Invalid date format.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(TASK_LOG, "a", encoding="utf-8")
    log_file.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] regenerate {date_text}\n")
    log_file.flush()

    subprocess.Popen(
        [
            sys.executable,
            str(FILTER_SCRIPT),
            "--date",
            date_text,
            "--no-resume"
        ],
        cwd=Path.cwd(),
        stdout=log_file,
        stderr=subprocess.STDOUT
    )
    log_file.close()


def admin_auth_configured() -> bool:
    return bool(ADMIN_USER and ADMIN_PASSWORD)


def parse_basic_auth(header: str) -> tuple[str, str] | None:
    if not header.startswith("Basic "):
        return None

    try:
        decoded = base64.b64decode(header.removeprefix("Basic ")).decode("utf-8")
    except Exception:
        return None

    if ":" not in decoded:
        return None

    username, password = decoded.split(":", 1)
    return username, password


def credentials_valid(header: str) -> bool:
    parsed = parse_basic_auth(header or "")
    if not parsed:
        return False

    username, password = parsed
    return (
        hmac.compare_digest(username, ADMIN_USER)
        and hmac.compare_digest(password, ADMIN_PASSWORD)
    )


class ReportHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/":
            selected = parse_qs(parsed.query).get("date", [""])[0]
            self.send_html(render_index(OUTPUT_DIR, selected))
            return

        if parsed.path.startswith("/reports/"):
            filename = unquote(parsed.path.removeprefix("/reports/"))
            if not re.fullmatch(r"report_[0-9A-Za-z_]+\.html", filename):
                self.send_error(404)
                return

            report_path = OUTPUT_DIR / filename
            if not report_path.exists():
                self.send_error(404)
                return

            data = report_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404)

    def do_POST(self) -> None:
        if not admin_auth_configured():
            self.send_error(
                403,
                "Admin credentials are not configured on the server."
            )
            return

        if not credentials_valid(self.headers.get("Authorization", "")):
            self.request_auth()
            return

        parsed = urlparse(self.path)
        form = self.read_form()

        try:
            if parsed.path == "/rerun":
                date_text = form.get("date", [""])[0]
                start_regenerate(date_text)
                self.redirect("/")
                return

            if parsed.path == "/topics":
                topics_text = form.get("topics", [""])[0]
                topics = [
                    line.strip()
                    for line in topics_text.splitlines()
                    if line.strip()
                ]
                config = load_config()
                config["topics"] = topics
                save_config(config)
                append_task_log(
                    f"[{datetime.now().isoformat(timespec='seconds')}] saved {len(topics)} topics"
                )
                self.redirect("/")
                return
        except Exception as exc:
            self.send_error(400, str(exc))
            return

        self.send_error(404)

    def read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        return parse_qs(data)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def request_auth(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="arXiv Report Admin"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Authentication required.")

    def send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve generated arXiv reports.")
    parser.add_argument(
        "--host",
        default=os.environ.get("ARXIV_REPORT_HOST", "0.0.0.0")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ARXIV_REPORT_PORT", "8000"))
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    print(f"Serving reports at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
