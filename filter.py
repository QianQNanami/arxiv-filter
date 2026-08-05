import csv
import smtplib
import argparse
import hashlib
import html
import json
import time
import re
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from string import Template
from typing import Dict, Any, List


ARXIV_API_URL = "http://export.arxiv.org/api/query"


def load_json(json_path: str) -> Dict[str, Any]:
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(config_path: str) -> Dict[str, Any]:
    return load_json(config_path)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_arxiv_id(entry_id: str) -> str:
    return entry_id.rstrip("/").split("/")[-1]


def build_arxiv_query(config: Dict[str, Any]) -> str:
    arxiv_cfg = config["arxiv"]

    start = arxiv_cfg["start_date"].replace("-", "") + "0000"
    end = arxiv_cfg["end_date"].replace("-", "") + "2359"
    date_query = f"submittedDate:[{start} TO {end}]"

    categories = arxiv_cfg.get("categories", [])
    if categories:
        cat_query = " OR ".join([f"cat:{cat}" for cat in categories])
        return f"({cat_query}) AND {date_query}"

    return date_query


def request_arxiv_with_retry(
    params: Dict[str, Any],
    retry: int = 8,
    base_sleep: int = 15,
    max_sleep: int = 180
) -> Any:
    import requests

    last_error = None

    for attempt in range(1, retry + 1):
        try:
            response = requests.get(
                ARXIV_API_URL,
                params=params,
                timeout=60
            )

            if response.status_code in [429, 500, 502, 503, 504]:
                wait_time = min(base_sleep * (2 ** (attempt - 1)), max_sleep)
                print(
                    f"[arXiv] HTTP {response.status_code}, "
                    f"retry {attempt}/{retry}, sleep {wait_time}s"
                )
                time.sleep(wait_time)
                continue

            response.raise_for_status()
            return response

        except requests.exceptions.RequestException as e:
            last_error = e
            wait_time = min(base_sleep * (2 ** (attempt - 1)), max_sleep)
            print(
                f"[arXiv] Request failed: {e}, "
                f"retry {attempt}/{retry}, sleep {wait_time}s"
            )
            time.sleep(wait_time)

    raise RuntimeError(
        f"arXiv request failed after {retry} retries: {last_error}"
    )


def load_checkpoint(checkpoint_path: str) -> List[Dict[str, str]]:
    papers = []

    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                papers.append(json.loads(line))
    except FileNotFoundError:
        pass

    return papers


def append_checkpoint(checkpoint_path: str, paper: Dict[str, str]) -> None:
    Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(paper, ensure_ascii=False) + "\n")
        f.flush()


def build_checkpoint_path(config: Dict[str, Any], query: str) -> str:
    arxiv_cfg = config["arxiv"]
    checkpoint_template = arxiv_cfg.get(
        "checkpoint",
        "output/checkpoints/arxiv_${date}_${query_hash}.jsonl"
    )
    period_date = format_period_date(config)
    query_hash = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    checkpoint_path = Template(checkpoint_template).safe_substitute(
        date=date_text_to_slug(period_date),
        start_date=arxiv_cfg.get("start_date", ""),
        end_date=arxiv_cfg.get("end_date", ""),
        query_hash=query_hash
    )
    return checkpoint_path


def fetch_arxiv_papers(config: Dict[str, Any]) -> List[Dict[str, str]]:
    import feedparser

    arxiv_cfg = config["arxiv"]

    query = build_arxiv_query(config)
    batch_size = arxiv_cfg.get("batch_size", 80)
    sleep_time = arxiv_cfg.get("sleep", 5)
    verbose = arxiv_cfg.get("verbose", True)

    retry = arxiv_cfg.get("retry", 8)
    retry_sleep = arxiv_cfg.get("retry_sleep", 15)
    max_retry_sleep = arxiv_cfg.get("max_retry_sleep", 180)

    checkpoint_path = build_checkpoint_path(config, query)
    resume = arxiv_cfg.get("resume", True)
    print(f"[arXiv] Checkpoint: {checkpoint_path}")

    papers = []
    seen_ids = set()

    if resume:
        papers = load_checkpoint(checkpoint_path)
        seen_ids = {p["arxiv_id"] for p in papers}
        print(f"[arXiv] Loaded checkpoint papers: {len(papers)}")

    start = len(papers)

    print(f"[arXiv] Query: {query}")

    while True:
        params = {
            "search_query": query,
            "start": start,
            "max_results": batch_size,
            "sortBy": "submittedDate",
            "sortOrder": "ascending"
        }

        print(f"\n[arXiv] Fetch batch: start={start}, max_results={batch_size}")

        try:
            response = request_arxiv_with_retry(
                params=params,
                retry=retry,
                base_sleep=retry_sleep,
                max_sleep=max_retry_sleep
            )
        except Exception as e:
            print(f"[arXiv] Fatal error at start={start}: {e}")
            print(f"[arXiv] Already pulled {len(papers)} papers.")
            print(f"[arXiv] Checkpoint saved in: {checkpoint_path}")
            break

        feed = feedparser.parse(response.text)

        if getattr(feed, "bozo", False):
            print(f"[arXiv] Feed parse warning: {feed.bozo_exception}")

        entries = feed.entries

        if not entries:
            print("[arXiv] No more entries.")
            break

        batch_new_count = 0

        for entry in entries:
            paper = {
                "arxiv_id": normalize_arxiv_id(entry.id),
                "title": clean_text(entry.get("title", "")),
                "abstract": clean_text(entry.get("summary", "")),
                "published": entry.get("published", ""),
                "updated": entry.get("updated", ""),
                "link": entry.get("id", ""),
                "categories": ",".join(
                    tag["term"] for tag in entry.get("tags", [])
                )
            }

            if paper["arxiv_id"] in seen_ids:
                continue

            seen_ids.add(paper["arxiv_id"])
            papers.append(paper)
            batch_new_count += 1

            append_checkpoint(checkpoint_path, paper)

            if verbose:
                print(f"[arXiv] Pulled: {paper['arxiv_id']} | {paper['title']}")

        print(
            f"[arXiv] Batch entries: {len(entries)}, "
            f"new: {batch_new_count}, total: {len(papers)}"
        )

        if len(entries) < batch_size:
            print("[arXiv] Last batch reached.")
            break

        start += batch_size
        time.sleep(sleep_time)

    return papers


def get_secret(secrets: Dict[str, Any], *keys: str, required: bool = True) -> str:
    for key in keys:
        value = secrets.get(key)
        if value:
            return value

    if required:
        joined_keys = ", ".join(keys)
        raise KeyError(f"Missing required secret: {joined_keys}")

    return ""


def append_date_to_filename(filename: str, date_text: str | None = None) -> str:
    path = Path(filename)
    suffix = "".join(path.suffixes)
    stem = path.name[:-len(suffix)] if suffix else path.name
    date_suffix = date_text or datetime.now().strftime("%Y%m%d")
    dated_name = f"{stem}_{date_suffix}{suffix}"
    return str(path.with_name(dated_name))


def date_text_to_slug(date_text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", date_text).strip("_")


def ensure_output_path(filename: str, output_dir: str = "output") -> str:
    path = Path(filename)
    output_path = path if path.parent != Path(".") else Path(output_dir) / path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return str(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch arXiv papers, filter them with an LLM, and export results."
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="Send an email notification after filtering."
    )
    parser.add_argument(
        "--use-yesterday",
        action="store_true",
        help="Use yesterday as both arXiv start_date and end_date."
    )
    return parser.parse_args()


def apply_runtime_options(
    config: Dict[str, Any],
    args: argparse.Namespace
) -> str | None:
    if not args.use_yesterday:
        return None

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    config["arxiv"]["start_date"] = yesterday
    config["arxiv"]["end_date"] = yesterday
    print(f"[Config] Using yesterday for arXiv date range: {yesterday}")
    return yesterday


def format_period_date(config: Dict[str, Any]) -> str:
    arxiv_cfg = config["arxiv"]
    start_date = arxiv_cfg.get("start_date", "")
    end_date = arxiv_cfg.get("end_date", "")

    if start_date and end_date and start_date != end_date:
        return f"{start_date} to {end_date}"

    return start_date or end_date or datetime.now().strftime("%Y-%m-%d")


def get_deepseek_client(
    config: Dict[str, Any],
    secrets: Dict[str, Any]
) -> Any:
    from openai import OpenAI

    deepseek_cfg = config["deepseek"]

    return OpenAI(
        api_key=get_secret(secrets, "deepseek_api_key", "api-key", "api_key"),
        base_url=deepseek_cfg.get("base_url", "https://api.deepseek.com")
    )


def build_prompt(
    prompt_config: Dict[str, Any],
    topics: List[str],
    paper: Dict[str, str]
) -> str:
    topic_text = "\n".join(
        [f"{i + 1}. {topic}" for i, topic in enumerate(topics)]
    )
    template = Template(prompt_config["relevance_prompt"])
    return template.safe_substitute(
        topic_text=topic_text,
        arxiv_id=paper["arxiv_id"],
        title=paper["title"],
        abstract=paper["abstract"]
    ).strip()


def extract_json_from_response(text: str) -> Dict[str, Any]:
    text = clean_text(text)

    if text.startswith("```"):
        text = re.sub(r"^```json", "", text)
        text = re.sub(r"^```", "", text)
        text = re.sub(r"```$", "", text)
        text = text.strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)

    return json.loads(text)


def judge_relevance(
    client: Any,
    config: Dict[str, Any],
    prompt_config: Dict[str, Any],
    paper: Dict[str, str]
) -> Dict[str, Any]:
    deepseek_cfg = config["deepseek"]
    topics = config["topics"]

    model = deepseek_cfg.get("model", "deepseek-chat")
    temperature = deepseek_cfg.get("temperature", 0)
    retry = deepseek_cfg.get("retry", 3)
    sleep_time = deepseek_cfg.get("sleep", 2)
    verbose = deepseek_cfg.get("verbose", True)

    prompt = build_prompt(prompt_config, topics, paper)

    if verbose:
        print("\n" + "=" * 100)
        print(f"[DeepSeek] Judging paper: {paper['arxiv_id']}")
        print(f"[DeepSeek] Title: {paper['title']}")
        print(f"[DeepSeek] Categories: {paper['categories']}")

    for attempt in range(1, retry + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature
            )

            content = response.choices[0].message.content.strip()
            data = extract_json_from_response(content)

            raw_related_topics = data.get("related_topics", [])
            reason = data.get("reason", "")

            related_topics = [
                topic for topic in raw_related_topics
                if topic in topics
            ]
            tldr = clean_text(data.get("tldr", ""))

            if verbose:
                print(f"[DeepSeek] Raw response: {content}")
                print(f"[DeepSeek] Parsed topics: {related_topics}")
                print(f"[DeepSeek] Reason: {reason}")
                print(f"[DeepSeek] TL;DR: {tldr}")
                print(
                    "[DeepSeek] Decision: "
                    + ("RELATED" if related_topics else "NOT RELATED")
                )

            return {
                "related_topics": related_topics,
                "reason": reason,
                "tldr": tldr
            }

        except Exception as e:
            wait_time = sleep_time * attempt
            print(
                f"[DeepSeek] Attempt {attempt}/{retry} failed "
                f"for {paper['arxiv_id']}: {e}. Sleep {wait_time}s"
            )
            time.sleep(wait_time)

    print(f"[DeepSeek] Failed, skip: {paper['arxiv_id']} | {paper['title']}")
    return {"related_topics": [], "reason": "", "tldr": ""}


def save_results(results: List[Dict[str, str]], output_csv: str) -> None:
    fieldnames = [
        "topic",
        "arxiv_id",
        "title",
        "abstract",
        "tldr",
        "published",
        "updated",
        "categories",
        "link"
    ]

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def format_email_papers(results: List[Dict[str, str]]) -> str:
    if not results:
        return "No related papers were found."

    sorted_results = sorted(
        results,
        key=lambda item: (item["topic"].lower(), item["arxiv_id"])
    )

    sections = []
    current_topic = None

    for paper in sorted_results:
        if paper["topic"] != current_topic:
            current_topic = paper["topic"]
            sections.append(f"\n## {current_topic}\n")

        sections.append(
            "\n".join([
                f"[{paper['arxiv_id']}] {paper['title']}",
                f"TL;DR: {paper.get('tldr') or 'N/A'}",
                f"Link: {paper['link']}"
            ])
        )

    return "\n\n".join(sections).strip()


def render_email_body(
    template_path: str,
    config: Dict[str, Any],
    results: List[Dict[str, str]],
    output_csv: str,
    report_html: str,
    period_date: str
) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        template = Template(f.read())

    arxiv_cfg = config["arxiv"]
    return template.safe_substitute(
        date=period_date,
        start_date=arxiv_cfg.get("start_date", ""),
        end_date=arxiv_cfg.get("end_date", ""),
        paper_count=str(len(results)),
        output_csv=output_csv,
        report_html=report_html,
        papers=format_email_papers(results)
    )


def normalize_recipients(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    return value or []


def get_email_recipient_groups(email_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    normalized_groups = []
    for group in email_cfg.get("recipient_groups", []):
        recipients = normalize_recipients(group.get("to", []))
        if not recipients:
            continue
        normalized_groups.append({
            "to": recipients,
            "topics": group.get("topics", [])
        })
    return normalized_groups


def filter_results_by_topics(
    results: List[Dict[str, str]],
    topics: List[str]
) -> List[Dict[str, str]]:
    if not topics:
        return results

    topic_set = set(topics)
    return [result for result in results if result["topic"] in topic_set]


def send_email_notification(
    config: Dict[str, Any],
    secrets: Dict[str, Any],
    results: List[Dict[str, str]],
    output_csv: str,
    report_html: str,
    period_date: str,
    enabled: bool
) -> None:
    email_cfg = config.get("email", {})
    if not enabled:
        print("[Email] Notification disabled.")
        return

    recipient_groups = get_email_recipient_groups(email_cfg)
    if not recipient_groups:
        raise ValueError(
            "Email is enabled, but no recipients are configured."
        )

    smtp_cfg = secrets.get("smtp", {})
    smtp_host = smtp_cfg.get("host") or email_cfg.get("smtp_host")
    smtp_port = int(smtp_cfg.get("port") or email_cfg.get("smtp_port", 587))
    smtp_user = smtp_cfg.get("username") or get_secret(
        secrets,
        "mail-user",
        "mail_user",
        "smtp_username",
        required=False
    )
    smtp_password = smtp_cfg.get("password") or get_secret(
        secrets,
        "mail-key",
        "mail_key",
        "smtp_password",
        required=False
    )
    sender = smtp_cfg.get("from") or email_cfg.get("from") or smtp_user

    if not smtp_host or not sender or not smtp_password:
        raise ValueError(
            "Email is enabled, but SMTP host, sender, or password is missing."
        )

    template_path = email_cfg.get("template", "email_template.txt")
    use_ssl = email_cfg.get("use_ssl", False)
    use_tls = email_cfg.get("use_tls", not use_ssl)

    if use_ssl:
        server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)

    with server:
        if use_tls:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_password)

        for group in recipient_groups:
            recipients = group["to"]
            group_results = filter_results_by_topics(
                results,
                group.get("topics", [])
            )
            body = render_email_body(
                template_path,
                config,
                group_results,
                output_csv,
                report_html,
                period_date
            )

            message = EmailMessage()
            message["From"] = sender
            message["To"] = ", ".join(recipients)
            message["Subject"] = Template(
                email_cfg.get("subject", "arXiv paper filter - ${date}")
            ).safe_substitute(date=period_date)
            message.set_content(body)
            server.send_message(message)

            print(
                "[Email] Notification sent to: "
                f"{', '.join(recipients)} ({len(group_results)} records)"
            )


def group_results_by_topic(
    results: List[Dict[str, str]]
) -> Dict[str, List[Dict[str, str]]]:
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for result in sorted(
        results,
        key=lambda item: (item["topic"].lower(), item["arxiv_id"])
    ):
        grouped.setdefault(result["topic"], []).append(result)
    return grouped


def render_html_report(
    config: Dict[str, Any],
    results: List[Dict[str, str]],
    output_csv: str,
    period_date: str
) -> str:
    grouped = group_results_by_topic(results)
    topics = config.get("topics", [])
    topic_blocks = []

    for topic in topics:
        papers = grouped.get(topic, [])
        if not papers:
            continue

        paper_cards = []
        for paper in papers:
            paper_cards.append(f"""
            <article class="paper-card">
              <div class="paper-meta">
                <a class="arxiv-id" href="{html.escape(paper['link'])}" target="_blank" rel="noreferrer">{html.escape(paper['arxiv_id'])}</a>
                <span>{html.escape(paper.get('categories', ''))}</span>
              </div>
              <h3>{html.escape(paper['title'])}</h3>
              <p class="tldr">{html.escape(paper.get('tldr') or 'N/A')}</p>
              <details>
                <summary>Abstract</summary>
                <p>{html.escape(paper['abstract'])}</p>
              </details>
            </article>
            """)

        topic_blocks.append(f"""
        <section class="topic-section">
          <div class="section-head">
            <h2>{html.escape(topic)}</h2>
            <span>{len(papers)} papers</span>
          </div>
          {''.join(paper_cards)}
        </section>
        """)

    if not topic_blocks:
        topic_blocks.append("""
        <section class="empty-state">
          <h2>No related papers</h2>
          <p>No papers matched the configured topics for this period.</p>
        </section>
        """)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>arXiv Filter Report - {html.escape(period_date)}</title>
  <style>
    :root {{
      color-scheme: light;
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
    .topbar {{
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.92);
      backdrop-filter: blur(12px);
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    .nav {{
      max-width: 1180px;
      margin: 0 auto;
      min-height: 58px;
      padding: 0 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .brand {{ font-weight: 700; letter-spacing: 0; }}
    .nav a {{
      color: var(--muted);
      text-decoration: none;
      margin-left: 20px;
    }}
    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 24px 56px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 24px;
      align-items: end;
      margin-bottom: 22px;
    }}
    h1 {{
      margin: 0;
      font-size: 32px;
      line-height: 1.2;
    }}
    .subtle {{ color: var(--muted); margin: 8px 0 0; }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(3, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .stat {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
      box-shadow: var(--shadow);
    }}
    .stat strong {{ display: block; font-size: 24px; }}
    .stat span {{ color: var(--muted); font-size: 13px; }}
    .topic-section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      margin-bottom: 18px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }}
    .section-head {{
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      background: #fbfcff;
    }}
    .section-head h2 {{ margin: 0; font-size: 18px; }}
    .section-head span {{
      color: var(--accent);
      background: var(--accent-soft);
      border-radius: 999px;
      padding: 3px 10px;
      white-space: nowrap;
      font-size: 13px;
    }}
    .paper-card {{
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }}
    .paper-card:last-child {{ border-bottom: 0; }}
    .paper-meta {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 7px;
    }}
    .arxiv-id {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 650;
    }}
    h3 {{
      margin: 0 0 9px;
      font-size: 17px;
      line-height: 1.35;
    }}
    .tldr {{
      margin: 0 0 12px;
      border-left: 3px solid var(--accent);
      padding-left: 12px;
      color: #374151;
    }}
    details {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: #fcfdff;
    }}
    summary {{
      cursor: pointer;
      color: var(--muted);
      font-weight: 650;
    }}
    details p {{ margin: 10px 0 0; color: #4b5563; }}
    .empty-state {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 32px;
      box-shadow: var(--shadow);
    }}
    @media (max-width: 760px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: 1fr; }}
      .nav {{ padding: 0 16px; }}
      .wrap {{ padding: 22px 16px 40px; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <nav class="nav">
      <div class="brand">arXiv Paper Filter</div>
      <div>
        <a href="./">Reports</a>
        <a href="{html.escape(output_csv)}">CSV</a>
      </div>
    </nav>
  </header>
  <main class="wrap">
    <section class="hero">
      <div>
        <h1>Report for {html.escape(period_date)}</h1>
        <p class="subtle">Related papers grouped by configured research topics.</p>
      </div>
    </section>
    <section class="stats">
      <div class="stat"><strong>{len(results)}</strong><span>Related records</span></div>
      <div class="stat"><strong>{len(grouped)}</strong><span>Matched topics</span></div>
      <div class="stat"><strong>{len(set(item['arxiv_id'] for item in results))}</strong><span>Unique papers</span></div>
    </section>
    {''.join(topic_blocks)}
  </main>
</body>
</html>
"""


def save_html_report(
    config: Dict[str, Any],
    results: List[Dict[str, str]],
    output_csv: str,
    period_date: str
) -> str:
    output_cfg = config.get("output", {})
    output_dir = output_cfg.get("dir", "output")
    report_name = output_cfg.get("html", "report_${date}.html")
    report_name = Template(report_name).safe_substitute(
        date=date_text_to_slug(period_date)
    )
    report_path = ensure_output_path(report_name, output_dir)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(render_html_report(config, results, output_csv, period_date))

    return report_path


def main() -> None:
    from tqdm import tqdm

    args = parse_args()
    config = load_config("config.json")
    runtime_date = apply_runtime_options(config, args)
    secrets = load_json("secrets.json")
    prompt_config = load_json(config.get("prompt", {}).get("file", "prompt.json"))

    output_csv = config.get("output", {}).get(
        "csv",
        "related_arxiv_papers.csv"
    )
    if config.get("output", {}).get("append_date", True):
        output_date = runtime_date.replace("-", "") if runtime_date else None
        output_csv = append_date_to_filename(output_csv, output_date)
    output_csv = ensure_output_path(
        output_csv,
        config.get("output", {}).get("dir", "output")
    )
    period_date = format_period_date(config)

    print("开始从 arXiv 拉取论文...")
    papers = fetch_arxiv_papers(config)
    print(f"\n共拉取论文数量：{len(papers)}")

    client = get_deepseek_client(config, secrets)
    results = []

    print("\n开始调用 DeepSeek 判断论文主题相关性...")

    for paper in tqdm(papers, desc="DeepSeek filtering"):
        judgment = judge_relevance(
            client=client,
            config=config,
            prompt_config=prompt_config,
            paper=paper
        )
        related_topics = judgment["related_topics"]

        for topic in related_topics:
            results.append({
                "topic": topic,
                "arxiv_id": paper["arxiv_id"],
                "title": paper["title"],
                "abstract": paper["abstract"],
                "tldr": judgment.get("tldr", ""),
                "published": paper["published"],
                "updated": paper["updated"],
                "categories": paper["categories"],
                "link": paper["link"]
            })

    save_results(results, output_csv)
    report_html = save_html_report(config, results, output_csv, period_date)
    send_email_notification(
        config,
        secrets,
        results,
        output_csv,
        report_html,
        period_date,
        args.send_email
    )

    print("\n筛选完成")
    print(f"相关记录数量：{len(results)}")
    print(f"结果已保存到：{output_csv}")
    print(f"HTML 报告已保存到：{report_html}")


if __name__ == "__main__":
    main()
