import csv
import json
import time
import re
from typing import Dict, Any, List

import requests
import feedparser
from tqdm import tqdm
from openai import OpenAI


ARXIV_API_URL = "http://export.arxiv.org/api/query"


def load_config(config_path: str) -> Dict[str, Any]:
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


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
) -> requests.Response:
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
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(paper, ensure_ascii=False) + "\n")
        f.flush()


def fetch_arxiv_papers(config: Dict[str, Any]) -> List[Dict[str, str]]:
    arxiv_cfg = config["arxiv"]

    query = build_arxiv_query(config)
    batch_size = arxiv_cfg.get("batch_size", 80)
    sleep_time = arxiv_cfg.get("sleep", 5)
    verbose = arxiv_cfg.get("verbose", True)

    retry = arxiv_cfg.get("retry", 8)
    retry_sleep = arxiv_cfg.get("retry_sleep", 15)
    max_retry_sleep = arxiv_cfg.get("max_retry_sleep", 180)

    checkpoint_path = arxiv_cfg.get("checkpoint", "arxiv_checkpoint.jsonl")
    resume = arxiv_cfg.get("resume", True)

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


def get_deepseek_client(config: Dict[str, Any]) -> OpenAI:
    deepseek_cfg = config["deepseek"]

    return OpenAI(
        api_key=deepseek_cfg["api_key"],
        base_url=deepseek_cfg.get("base_url", "https://api.deepseek.com")
    )


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
    client: OpenAI,
    config: Dict[str, Any],
    paper: Dict[str, str]
) -> List[str]:
    deepseek_cfg = config["deepseek"]
    topics = config["topics"]

    model = deepseek_cfg.get("model", "deepseek-chat")
    temperature = deepseek_cfg.get("temperature", 0)
    retry = deepseek_cfg.get("retry", 3)
    sleep_time = deepseek_cfg.get("sleep", 2)
    verbose = deepseek_cfg.get("verbose", True)

    topic_text = "\n".join(
        [f"{i + 1}. {topic}" for i, topic in enumerate(topics)]
    )

    prompt = f"""
你是一名严谨的学术论文筛选助手。

请根据论文标题和摘要，判断该论文是否与下面任意研究主题相关。

研究主题：
{topic_text}

论文编号：
{paper["arxiv_id"]}

论文标题：
{paper["title"]}

论文摘要：
{paper["abstract"]}

判断标准：
1. 只根据标题和摘要判断，不要臆测全文内容。
2. 只要论文与某个研究主题在问题、方法、系统、应用场景上有明确关系，就认为相关。
3. 如果只是泛泛出现相似词汇，但研究问题明显不同，不要认为相关。
4. 一篇论文可以关联多个主题。
5. 如果不相关，返回空列表。

请只输出 JSON，不要输出解释文字。

输出格式：
{{
  "related_topics": ["主题词1", "主题词2"],
  "reason": "一句话说明为什么相关或不相关"
}}
""".strip()

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

            if verbose:
                print(f"[DeepSeek] Raw response: {content}")
                print(f"[DeepSeek] Parsed topics: {related_topics}")
                print(f"[DeepSeek] Reason: {reason}")
                print(
                    "[DeepSeek] Decision: "
                    + ("RELATED" if related_topics else "NOT RELATED")
                )

            return related_topics

        except Exception as e:
            wait_time = sleep_time * attempt
            print(
                f"[DeepSeek] Attempt {attempt}/{retry} failed "
                f"for {paper['arxiv_id']}: {e}. Sleep {wait_time}s"
            )
            time.sleep(wait_time)

    print(f"[DeepSeek] Failed, skip: {paper['arxiv_id']} | {paper['title']}")
    return []


def save_results(results: List[Dict[str, str]], output_csv: str) -> None:
    fieldnames = [
        "topic",
        "arxiv_id",
        "title",
        "abstract",
        "published",
        "updated",
        "categories",
        "link"
    ]

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def main() -> None:
    config = load_config("config.json")

    output_csv = config.get("output", {}).get(
        "csv",
        "related_arxiv_papers.csv"
    )

    print("开始从 arXiv 拉取论文...")
    papers = fetch_arxiv_papers(config)
    print(f"\n共拉取论文数量：{len(papers)}")

    client = get_deepseek_client(config)
    results = []

    print("\n开始调用 DeepSeek 判断论文主题相关性...")

    for paper in tqdm(papers, desc="DeepSeek filtering"):
        related_topics = judge_relevance(
            client=client,
            config=config,
            paper=paper
        )

        for topic in related_topics:
            results.append({
                "topic": topic,
                "arxiv_id": paper["arxiv_id"],
                "title": paper["title"],
                "abstract": paper["abstract"],
                "published": paper["published"],
                "updated": paper["updated"],
                "categories": paper["categories"],
                "link": paper["link"]
            })

    save_results(results, output_csv)

    print("\n筛选完成")
    print(f"相关记录数量：{len(results)}")
    print(f"结果已保存到：{output_csv}")


if __name__ == "__main__":
    main()