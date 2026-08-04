# arXiv Paper Filter

Fetch papers from arXiv, ask an LLM to judge whether they match configured research topics, export related papers to CSV, and optionally send an email report.

## Features

- Fetch arXiv papers by date range and category.
- Resume interrupted arXiv fetching with a JSONL checkpoint.
- Keep API keys and SMTP secrets out of `config.json`.
- Keep the LLM prompt in `prompt.json` for easy editing.
- Export related papers to the `output` directory with a date suffix by default.
- Optionally email sorted paper summaries by topic and arXiv ID.
- Send different topic subsets to different recipient groups.

## Install

Use Python 3.10+.

```powershell
pip install requests feedparser tqdm openai
```

## Files

- `filter.py`: main script.
- `config.json`: non-secret runtime configuration.
- `secrets.json`: private API keys and SMTP credentials. This file is ignored by git.
- `prompt.json`: LLM prompt template.
- `email_template.txt`: plain-text email template.

## Configure

### `config.json`

Main fields:

- `arxiv.start_date` / `arxiv.end_date`: date range used when `--use-yesterday` is not set.
- `arxiv.categories`: arXiv categories to search.
- `topics`: research topics used by the LLM relevance judge.
- `prompt.file`: prompt template file, default `prompt.json`.
- `output.csv`: base CSV output filename.
- `output.dir`: output directory for CSV files, default `output`.
- `output.append_date`: append a date suffix to the exported filename.
- `email.recipient_groups`: email recipient groups. Each group can set a topic subset.
- `email.subject`: email subject template. `${date}` is the covered arXiv period.
- `email.template`: email body template file.
- `email.smtp_host`, `email.smtp_port`, `email.use_tls`, `email.use_ssl`: SMTP connection settings.

Email is not enabled in `config.json`. It is controlled by the command line with `--send-email`.

Example recipient groups:

```json
"email": {
  "recipient_groups": [
    {
      "to": ["person-a@example.com", "person-b@example.com"],
      "topics": ["Efficient VLM/VLA Models"]
    },
    {
      "to": ["person-c@example.com"],
      "topics": []
    }
  ]
}
```

`topics` should refer to entries from the top-level `topics` list. If a group does not set `topics`, or sets it to an empty list, that group receives all related papers. Email recipients must be configured through `recipient_groups`.

### `secrets.json`

Example:

```json
{
  "api-key": "YOUR_DEEPSEEK_API_KEY",
  "mail-key": "YOUR_SMTP_PASSWORD",
  "smtp": {
    "host": "smtp.example.com",
    "port": 587,
    "username": "your-email@example.com",
    "password": "YOUR_SMTP_PASSWORD",
    "from": "your-email@example.com"
  }
}
```

Supported LLM key names are `deepseek_api_key`, `api-key`, or `api_key`.

For SMTP, prefer the nested `smtp` block. The legacy `mail-key` field is also supported as the SMTP password.

### `prompt.json`

The prompt uses Python `string.Template` placeholders:

- `${topic_text}`
- `${arxiv_id}`
- `${title}`
- `${abstract}`

The model should return JSON with:

```json
{
  "related_topics": ["topic"],
  "reason": "short reason",
  "tldr": "one sentence TL;DR"
}
```

The LLM generates `tldr` for each judged paper. Only related papers are exported or emailed.

### `email_template.txt`

The email template supports:

- `${date}`: the covered arXiv period, not the email send date.
- `${start_date}`
- `${end_date}`
- `${paper_count}`
- `${output_csv}`
- `${papers}`

If the period is a single day, `${date}` is that day, for example `2026-08-03`.
If the period is a range, `${date}` is formatted like `2026-07-01 to 2026-07-05`.

## Run

Use dates from `config.json`:

```powershell
python filter.py
```

Use yesterday as both `start_date` and `end_date`:

```powershell
python filter.py --use-yesterday
```

Send an email report after filtering:

```powershell
python filter.py --send-email
```

Daily run for yesterday and email the report:

```powershell
python filter.py --use-yesterday --send-email
```

## Output

By default, CSV files are saved under `output/`, and the filename appends a date:

- Normal run: uses today's date, for example `output/related_arxiv_papers_20260804.csv`.
- `--use-yesterday`: uses yesterday's date, for example `output/related_arxiv_papers_20260803.csv`.

CSV columns:

- `topic`
- `arxiv_id`
- `title`
- `abstract`
- `tldr`
- `published`
- `updated`
- `categories`
- `link`

Email papers are sorted by topic and then arXiv ID, and formatted as:

```text
[Arxiv id] Title
Abstract: ...
TL;DR: ...
Link: ...
```

## Notes

- `secrets.json` is ignored by git and should not be committed.
- arXiv fetching can resume from `arxiv.checkpoint`.
- If you rerun the same date range with checkpoint resume enabled, already fetched papers are loaded from the checkpoint.
