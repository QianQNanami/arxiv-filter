import argparse
import html
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


OUTPUT_DIR = Path(os.environ.get("ARXIV_REPORT_OUTPUT_DIR", "output"))


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
    {empty}
    {f'<iframe src="{iframe_src}" title="Selected report"></iframe>' if iframe_src else ''}
  </main>
</body>
</html>
"""


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
