from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import math
import os
import re
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from PIL import Image, ImageDraw, ImageFont


CN_TZ = timezone(timedelta(hours=8))
USER_AGENT = (
    "Mozilla/5.0 (compatible; AI-News-Bot/1.0; "
    "+https://github.com/SuperCup/AI-News)"
)


@dataclass
class Candidate:
    title: str
    url: str
    source: str
    published_at: str
    snippet: str
    query: str
    topic_hints: list[str]
    query_weight: float
    country_hint: str | None = None
    company_hint: str | None = None
    article_excerpt: str = ""
    companies: list[str] | None = None
    country_focus: str = "OTHER"
    topics: list[str] | None = None
    importance: float = 0.0

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "snippet": self.snippet[:700],
            "article_excerpt": self.article_excerpt[:1400],
            "companies": self.companies or [],
            "country_focus": self.country_focus,
            "topics": self.topics or [],
            "importance": round(self.importance, 2),
        }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def load_rules(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    with (root / "config" / "news_rules.json").open("r", encoding="utf-8") as fh:
        rules = json.load(fh)
    rules["site_base_url"] = (os.getenv("SITE_BASE_URL") or rules["site_base_url"]).rstrip("/")
    return rules


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value)
    if "<" in raw and ">" in raw:
        soup = BeautifulSoup(raw, "html.parser")
        text = soup.get_text(" ", strip=True)
    else:
        text = raw.strip()
    text = re.sub(r"\s+", " ", text)
    return html.unescape(text).strip()


def has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(CN_TZ)


def iso_or_raw_date(value: str | None) -> str:
    dt = parse_dt(value)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M %z")
    return clean_text(value) or "未标明"


def stable_id(*parts: str) -> str:
    raw = "||".join(parts).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def norm_title(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^\w\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def company_index(rules: dict[str, Any]) -> list[dict[str, Any]]:
    companies: list[dict[str, Any]] = []
    for country, rows in rules["priority_companies"].items():
        for rank, row in enumerate(rows, start=1):
            companies.append(
                {
                    "country": country,
                    "rank": rank,
                    "name": row["name"],
                    "aliases": row["aliases"],
                    "aliases_lower": [alias.lower() for alias in row["aliases"]],
                }
            )
    return companies


def topic_keywords() -> dict[str, list[str]]:
    return {
        "large_model": ["large language model", "llm", "大模型", "foundation model", "模型发布"],
        "ai_agent": ["agent", "智能体", "autonomous", "computer use", "tool use", "mcp"],
        "robotics": ["robot", "robotics", "机器人", "humanoid", "具身智能"],
        "chips": ["chip", "gpu", "accelerator", "inference", "training", "芯片", "算力", "昇腾"],
        "open_source_model": ["open source", "开源", "weights", "apache 2.0", "mit license"],
        "research_paper": ["paper", "arxiv", "benchmark", "research", "论文", "评测"],
        "ai_product": ["launch", "product", "app", "assistant", "copilot", "发布", "产品"],
        "enterprise_ai": ["enterprise", "customer", "production", "workflow", "企业", "落地"],
        "regulation": ["regulation", "policy", "law", "act", "监管", "政策", "法规"],
        "safety_security": ["safety", "security", "risk", "vulnerability", "安全", "风险", "漏洞"],
        "commercial": ["funding", "acquisition", "merger", "ipo", "partnership", "融资", "并购", "合作"],
    }


def product_application_keywords() -> list[str]:
    return [
        "product",
        "launch",
        "release",
        "released",
        "rollout",
        "available",
        "app",
        "assistant",
        "copilot",
        "agent",
        "workflow",
        "automation",
        "deployment",
        "customer",
        "production",
        "api",
        "sdk",
        "plugin",
        "产品",
        "应用",
        "发布",
        "上线",
        "推出",
        "接入",
        "部署",
        "落地",
        "客户",
        "场景",
        "工作流",
        "助手",
        "智能体",
        "工具",
    ]


def commercial_only_keywords() -> list[str]:
    return [
        "funding",
        "financing",
        "raised",
        "valuation",
        "acquisition",
        "merger",
        "ipo",
        "stock",
        "shares",
        "revenue",
        "earnings",
        "investment",
        "partnership",
        "融资",
        "估值",
        "收购",
        "并购",
        "IPO",
        "股价",
        "财报",
        "营收",
        "投资",
        "合作",
    ]


def has_product_application_signal(text: str, topics: list[str]) -> bool:
    low = text.lower()
    if {"ai_product", "enterprise_ai", "ai_agent"} & set(topics):
        return True
    return any(keyword.lower() in low for keyword in product_application_keywords())


def has_commercial_signal(text: str, topics: list[str]) -> bool:
    low = text.lower()
    if "commercial" in topics:
        return True
    return any(keyword.lower() in low for keyword in commercial_only_keywords())


def detect_companies(text: str, companies: list[dict[str, Any]]) -> tuple[list[str], str]:
    low = text.lower()
    found: list[tuple[int, str, str]] = []
    for company in companies:
        for alias in company["aliases_lower"]:
            if alias and alias in low:
                found.append((company["rank"], company["country"], company["name"]))
                break
    found.sort()
    names = []
    country = "OTHER"
    countries = {row[1] for row in found}
    for _, _, name in found:
        if name not in names:
            names.append(name)
    if "CN" in countries:
        country = "CN"
    elif "US" in countries:
        country = "US"
    return names, country


def detect_topics(text: str, hints: list[str]) -> list[str]:
    low = text.lower()
    topics = set(hints)
    for topic, words in topic_keywords().items():
        if any(word.lower() in low for word in words):
            topics.add(topic)
    return sorted(topics)


def normalize_region(value: str | None) -> str:
    value = (value or "OTHER").upper()
    if value in {"CN", "CHINA"}:
        return "CN"
    if value in {"US", "USA", "UNITED STATES"}:
        return "US"
    return "OTHER"


def region_label(value: str | None) -> str:
    region = normalize_region(value)
    return {"CN": "中国", "US": "美国", "OTHER": "其他"}.get(region, "其他")


def region_targets(rules: dict[str, Any], total: int | None = None) -> dict[str, int]:
    configured = rules.get("region_target_counts") or {}
    max_items = total or int(rules["max_daily_items"])
    targets = {
        "CN": int(configured.get("CN", math.ceil(max_items / 3))),
        "US": int(configured.get("US", math.ceil(max_items / 3))),
        "OTHER": int(configured.get("OTHER", max_items // 3)),
    }
    delta = max_items - sum(targets.values())
    order = ["OTHER", "US", "CN"]
    idx = 0
    while delta != 0:
        key = order[idx % len(order)]
        if delta > 0:
            targets[key] += 1
            delta -= 1
        elif targets[key] > 0:
            targets[key] -= 1
            delta += 1
        idx += 1
    return targets


def build_query_specs(rules: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for row in rules["topic_queries"]:
        specs.append(
            {
                "query": row["query"],
                "topics": row.get("topics", []),
                "weight": row.get("weight", 10),
                "country_hint": None,
                "company_hint": None,
                "kind": "topic",
            }
        )

    for country, rows in rules["priority_companies"].items():
        for row in rows:
            aliases = row["aliases"][:3]
            quoted_aliases = " OR ".join(f'"{alias}"' for alias in aliases)
            if country == "CN":
                query = (
                    f"({quoted_aliases}) "
                    "(AI OR 人工智能 OR 大模型 OR 智能体) "
                    "(产品 OR 应用 OR 发布 OR 上线 OR 开源 OR 模型 OR 机器人 OR 芯片 OR 部署 OR 合作)"
                )
            else:
                query = (
                    f"({quoted_aliases}) "
                    "(AI OR artificial intelligence OR agent OR model) "
                    "(product OR launch OR release OR deployment OR app OR customer OR open source OR chip OR robot)"
                )
            specs.append(
                {
                    "query": query,
                    "topics": [],
                    "weight": 22,
                    "country_hint": country,
                    "company_hint": row["name"],
                    "kind": "company",
                }
            )
    return specs


def google_news_rss_url(query: str, lookback_hours: int, locale: str = "en-US") -> str:
    days = max(1, math.ceil(lookback_hours / 24))
    q = quote_plus(f"{query} when:{days}d")
    if locale == "zh-CN" or has_cjk(query):
        return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def source_from_url(url: str, fallback: str = "News") -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or fallback


def has_ai_signal_text(value: str) -> bool:
    low = value.lower()
    signals = [
        "ai",
        "artificial intelligence",
        "llm",
        "large language model",
        "agent",
        "robot",
        "gpu",
        "chip",
        "model",
        "copilot",
        "人工智能",
        "大模型",
        "智能体",
        "机器人",
        "芯片",
        "模型",
        "算力",
        "开源",
    ]
    return any(signal in low for signal in signals)


def is_low_value_candidate(item: Candidate) -> bool:
    title = clean_text(item.title)
    low = title.lower()
    if len(norm_title(title)) < 8:
        return True
    host = source_from_url(item.url, "").lower()
    source_norm = norm_title(item.source)
    title_norm = norm_title(title)
    host_norm = norm_title(host)
    if title_norm and title_norm in {source_norm, host_norm}:
        return True
    if re.search(r"\s+-\s+[a-z0-9.-]+\.[a-z]{2,}\s*$", low) and not has_ai_signal_text(title):
        return True
    if re.search(
        r"\b(the\s+)?\d+\s+best\b|\bbest\s+.+\bworth using\b|\bbest\s+ai agents\b|\branked\b|\bcompared\b|\bultimate guide\b|\bhow to\b",
        low,
    ):
        return True
    if "what changes in 2026" in low and not any(word in low for word in ["launch", "announces", "released"]):
        return True
    return False


def fetch_google_news(spec: dict[str, Any], lookback_hours: int, limit: int = 8) -> list[Candidate]:
    url = google_news_rss_url(spec["query"], lookback_hours)
    feed = feedparser.parse(url)
    candidates: list[Candidate] = []
    for entry in feed.entries[:limit]:
        source = ""
        if hasattr(entry, "source") and getattr(entry.source, "title", ""):
            source = clean_text(entry.source.title)
        source = source or clean_text(getattr(entry, "author", "")) or "Google News"
        candidates.append(
            Candidate(
                title=clean_text(getattr(entry, "title", "")),
                url=clean_text(getattr(entry, "link", "")),
                source=source,
                published_at=iso_or_raw_date(getattr(entry, "published", "")),
                snippet=clean_text(getattr(entry, "summary", "")),
                query=spec["query"],
                topic_hints=spec["topics"],
                query_weight=float(spec["weight"]),
                country_hint=spec["country_hint"],
                company_hint=spec["company_hint"],
            )
        )
    return candidates


def fetch_serper_news(spec: dict[str, Any], lookback_hours: int, limit: int = 8) -> list[Candidate]:
    key = os.getenv("SERPER_API_KEY")
    if not key:
        return []
    payload = {"q": spec["query"], "num": limit, "tbs": "qdr:d"}
    resp = requests.post(
        "https://google.serper.dev/news",
        headers={"X-API-KEY": key, "Content-Type": "application/json", "User-Agent": USER_AGENT},
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates: list[Candidate] = []
    for row in data.get("news", [])[:limit]:
        candidates.append(
            Candidate(
                title=clean_text(row.get("title")),
                url=clean_text(row.get("link")),
                source=clean_text(row.get("source")) or "Serper",
                published_at=iso_or_raw_date(row.get("date")),
                snippet=clean_text(row.get("snippet")),
                query=spec["query"],
                topic_hints=spec["topics"],
                query_weight=float(spec["weight"]) + 2,
                country_hint=spec["country_hint"],
                company_hint=spec["company_hint"],
            )
        )
    return candidates


def fetch_tavily_news(spec: dict[str, Any], lookback_hours: int, limit: int = 8) -> list[Candidate]:
    key = os.getenv("SEARCH_TAVILY") or os.getenv("TAVILY_API_KEY")
    if not key:
        return []

    company_search_enabled = os.getenv("TAVILY_COMPANY_SEARCH", "").lower() in {"1", "true", "yes", "all"}
    if spec.get("kind") == "company" and not company_search_enabled:
        return []

    time_range = "day" if lookback_hours <= 48 else "week"
    start_date = (now_cn() - timedelta(hours=lookback_hours)).date().isoformat()
    end_date = now_cn().date().isoformat()
    payload = {
        "query": spec["query"],
        "topic": "news",
        "search_depth": os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
        "max_results": min(limit, 20),
        "time_range": time_range,
        "start_date": start_date,
        "end_date": end_date,
        "include_answer": False,
        "include_raw_content": False,
    }
    resp = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        json=payload,
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates: list[Candidate] = []
    for row in data.get("results", [])[:limit]:
        url = clean_text(row.get("url"))
        candidates.append(
            Candidate(
                title=clean_text(row.get("title")),
                url=url,
                source=clean_text(row.get("source")) or source_from_url(url, "Tavily"),
                published_at=iso_or_raw_date(row.get("published_date") or row.get("published_at") or row.get("date")),
                snippet=clean_text(row.get("content") or row.get("raw_content")),
                query=spec["query"],
                topic_hints=spec["topics"],
                query_weight=float(spec["weight"]) + 3,
                country_hint=spec["country_hint"],
                company_hint=spec["company_hint"],
            )
        )
    return candidates


def fetch_bing_news(spec: dict[str, Any], lookback_hours: int, limit: int = 8) -> list[Candidate]:
    key = os.getenv("BING_SEARCH_API_KEY")
    if not key:
        return []
    endpoint = os.getenv("BING_SEARCH_ENDPOINT") or "https://api.bing.microsoft.com/v7.0/news/search"
    resp = requests.get(
        endpoint,
        headers={"Ocp-Apim-Subscription-Key": key, "User-Agent": USER_AGENT},
        params={"q": spec["query"], "count": limit, "freshness": "Day", "mkt": "en-US", "safeSearch": "Off"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates: list[Candidate] = []
    for row in data.get("value", [])[:limit]:
        provider = row.get("provider") or []
        source = clean_text(provider[0].get("name")) if provider else "Bing News"
        candidates.append(
            Candidate(
                title=clean_text(row.get("name")),
                url=clean_text(row.get("url")),
                source=source,
                published_at=iso_or_raw_date(row.get("datePublished")),
                snippet=clean_text(row.get("description")),
                query=spec["query"],
                topic_hints=spec["topics"],
                query_weight=float(spec["weight"]) + 2,
                country_hint=spec["country_hint"],
                company_hint=spec["company_hint"],
            )
        )
    return candidates


def fetch_for_spec(spec: dict[str, Any], lookback_hours: int) -> list[Candidate]:
    rows: list[Candidate] = []
    for fetcher in (fetch_tavily_news, fetch_serper_news, fetch_bing_news, fetch_google_news):
        try:
            rows.extend(fetcher(spec, lookback_hours))
        except Exception as exc:
            print(f"[warn] fetch failed for {spec['query'][:80]} via {fetcher.__name__}: {exc}")
    return [row for row in rows if row.title and row.url and not is_low_value_candidate(row)]


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for item in candidates:
        url_key = urlparse(item.url).netloc.lower() + urlparse(item.url).path.lower()
        title_key = norm_title(item.title)
        key = stable_id(url_key, title_key[:90])
        if key not in seen:
            seen[key] = item
            continue
        existing = seen[key]
        if len(item.snippet) > len(existing.snippet):
            seen[key] = item
    return list(seen.values())


def fetch_article_excerpt(url: str) -> str:
    if "news.google.com" in urlparse(url).netloc:
        return ""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=12)
        if "text/html" not in resp.headers.get("Content-Type", ""):
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        meta = soup.find("meta", attrs={"name": "description"})
        pieces = []
        if meta and meta.get("content"):
            pieces.append(clean_text(meta.get("content")))
        for p in soup.find_all("p")[:18]:
            text = clean_text(p.get_text(" ", strip=True))
            if len(text) >= 45:
                pieces.append(text)
        excerpt = " ".join(pieces)
        return excerpt[:2800]
    except Exception:
        return ""


def score_candidates(candidates: list[Candidate], rules: dict[str, Any]) -> list[Candidate]:
    companies = company_index(rules)
    topic_weights = rules["topic_weights"]
    trusted_sources = [
        "reuters",
        "associated press",
        "ap news",
        "bloomberg",
        "financial times",
        "the information",
        "the verge",
        "techcrunch",
        "venturebeat",
        "mit technology review",
        "nature",
        "science",
        "arxiv",
        "中国新闻网",
        "财新",
        "晚点",
        "量子位",
        "机器之心",
    ]

    for item in candidates:
        text = " ".join([item.title, item.snippet, item.source, item.query, item.company_hint or ""])
        found_companies, detected_country = detect_companies(text, companies)
        if item.company_hint and item.company_hint not in found_companies:
            found_companies.insert(0, item.company_hint)
        item.companies = found_companies[:4]
        item.country_focus = item.country_hint or detected_country
        if normalize_region(item.country_focus) == "OTHER" and has_cjk(text):
            item.country_focus = "CN"
        item.country_focus = normalize_region(item.country_focus)
        item.topics = detect_topics(text, item.topic_hints)
        application_signal = has_product_application_signal(text, item.topics)
        commercial_signal = has_commercial_signal(text, item.topics)

        score = item.query_weight
        score += sum(topic_weights.get(topic, 0) for topic in item.topics) / 2
        if "ai_product" in item.topics:
            score += 8
        if "enterprise_ai" in item.topics:
            score += 7
        if application_signal:
            score += 8
        if commercial_signal and not application_signal:
            score -= 18
            if set(item.topics).issubset({"commercial"}):
                score -= 6
        elif commercial_signal:
            score -= 4
        if item.companies:
            score += 24
            if item.country_focus == "CN":
                score += 4
            elif item.country_focus == "US":
                score += 4
        elif item.country_focus == "OTHER":
            score += 3
        if any(name in item.source.lower() for name in trusted_sources):
            score += 7

        dt = parse_dt(item.published_at)
        if dt:
            age_hours = max(0, (now_cn() - dt).total_seconds() / 3600)
            if age_hours <= 24:
                score += 12
            elif age_hours <= 48:
                score += 7
            elif age_hours <= 96:
                score += 2
        item.importance = score
    return sorted(candidates, key=lambda row: row.importance, reverse=True)


def enrich_top_candidates(candidates: list[Candidate], limit: int = 70) -> None:
    top = candidates[:limit]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_article_excerpt, item.url): item for item in top}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                item.article_excerpt = future.result()
            except Exception:
                item.article_excerpt = ""


def discover_candidates(rules: dict[str, Any]) -> list[Candidate]:
    lookback = int(os.getenv("NEWS_LOOKBACK_HOURS", rules["default_lookback_hours"]))
    specs = build_query_specs(rules)
    all_rows: list[Candidate] = []
    max_workers = int(os.getenv("NEWS_FETCH_WORKERS", "8"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fetch_for_spec, spec, lookback) for spec in specs]
        for future in concurrent.futures.as_completed(futures):
            all_rows.extend(future.result())
    rows = score_candidates(dedupe_candidates(all_rows), rules)
    enrich_top_candidates(rows)
    return score_candidates(rows, rules)


def balanced_candidate_pool(candidates: list[Candidate], rules: dict[str, Any], limit: int = 90) -> list[Candidate]:
    selected: list[Candidate] = []
    selected_ids: set[int] = set()
    per_region = max(12, math.ceil(limit / 3))

    for region in ("CN", "US", "OTHER"):
        region_rows = [row for row in candidates if normalize_region(row.country_focus) == region]
        for row in region_rows[:per_region]:
            selected.append(row)
            selected_ids.add(id(row))

    for row in candidates:
        if len(selected) >= limit:
            break
        if id(row) not in selected_ids:
            selected.append(row)
            selected_ids.add(id(row))

    return selected[:limit]


def extract_json_block(value: str) -> Any:
    cleaned = value.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    start = min([idx for idx in [cleaned.find("["), cleaned.find("{")] if idx >= 0] or [0])
    cleaned = cleaned[start:]
    return json.loads(cleaned)


def llm_client():
    from openai import OpenAI

    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        return OpenAI(base_url=base_url.rstrip("/"))
    return OpenAI()


def llm_text(system_prompt: str, user_prompt: str) -> str:
    client = llm_client()
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    if os.getenv("OPENAI_BASE_URL"):
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.output_text
    except Exception:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""


def llm_daily_items(candidates: list[Candidate], rules: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        max_items = int(rules["max_daily_items"])
        payload = [item.as_prompt_dict() for item in balanced_candidate_pool(candidates, rules)]
        prompt = {
            "task": "从候选新闻中选出今日 AI 日报，生成中文内容。",
            "rules": [
                f"必须尽量选满 {max_items} 条；候选不足时才少于该数量。",
                "区域配比按中国、美国、其他地区各约三分之一执行；15 条时目标为中国 5 条、美国 5 条、其他地区 5 条。候选不足时用剩余高重要性新闻补齐。",
                "优先中美头部 AI 公司相关消息，同时保留欧洲、日本、韩国、英国、加拿大、新加坡等其他地区的重要 AI 动态。",
                "优先选择已经发布、上线、开放测试、开始客户部署或明显影响产品/应用形态的消息。",
                "覆盖大模型、AI Agent、机器人、芯片、开源模型、论文、AI 产品、企业应用、监管政策、安全事件等。",
                "融资、并购、股价、财报、战略合作等纯商业消息只在确有行业影响或直接关联产品、算力、模型、落地应用时入选。",
                "country_focus 按新闻事件主体或相关公司所在地判断，不按媒体所在地判断；例如中国公司被美国媒体报道仍应归为 CN。",
                "只使用候选中给出的事实、来源、日期和链接；不要编造链接、时间、公司或细节。",
                "每条包含 title、summary、details、url、source、published_at、companies、country_focus、topics、importance；country_focus 只能是 CN、US 或 OTHER。",
                "summary 用 3-5 句中文概览；details 说明事件细节或注明只能从摘要判断。",
                "如果候选标题、摘要或正文是英文或其他非中文语言，summary 和 details 中的事实性描述必须翻译成中文；公司名、模型名、产品名、论文名可保留原文。",
                "保留英文模型名、公司名、产品名，不强行翻译。",
            ],
            "candidates": payload,
        }
        output_text = llm_text(
            "你是严谨的 AI 产业新闻编辑。输出必须是 JSON 数组，不要输出解释。",
            json.dumps(prompt, ensure_ascii=False),
        )
        data = extract_json_block(output_text)
        if not isinstance(data, list):
            return None
        return normalize_items(data, candidates, rules)
    except Exception as exc:
        print(f"[warn] OpenAI daily curation failed, falling back to heuristic: {exc}")
        return None


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？.!?])\s+", clean_text(text))
    return [part for part in parts if part]


def topic_label(topic: str) -> str:
    labels = {
        "large_model": "大模型",
        "ai_agent": "AI Agent",
        "robotics": "机器人",
        "chips": "AI 芯片",
        "open_source_model": "开源模型",
        "research_paper": "论文/评测",
        "ai_product": "AI 产品",
        "enterprise_ai": "企业应用",
        "regulation": "监管政策",
        "safety_security": "安全事件",
        "commercial": "商业活动",
    }
    return labels.get(topic, topic)


def fallback_summary_parts(item: Candidate, source_text: str) -> list[str]:
    if has_cjk(source_text):
        parts = split_sentences(source_text)[:3]
        if parts:
            return parts

    topic_text = "、".join(topic_label(topic) for topic in (item.topics or [])[:4]) or "AI"
    company_text = "、".join(item.companies or []) or "相关公司/机构"
    return [
        f"{item.source} 报道了「{item.title}」相关进展。",
        f"该消息涉及{topic_text}方向，关联主体包括{company_text}。",
        "相关事实已整理为中文概览，完整背景、数据和引用以原文为准。",
    ]


def daily_item_from_candidate(item: Candidate) -> dict[str, Any]:
    source_text = item.article_excerpt or item.snippet
    summary_parts = fallback_summary_parts(item, source_text)
    if not summary_parts:
        summary_parts = [f"该消息围绕 {item.title}。"]
    if len(summary_parts) < 3:
        summary_parts.append("该事件与今日 AI 技术、产品或产业动态相关。")
    if len(summary_parts) < 3:
        summary_parts.append("更多背景、数据和引用可查看原文。")
    details = item.article_excerpt[:900] if item.article_excerpt else item.snippet[:900]
    if not details:
        details = "未抓取到足够正文，详情以原文链接为准。"
    return {
        "id": stable_id(item.title, item.url),
        "title": item.title,
        "summary": " ".join(summary_parts[:5]),
        "details": details,
        "url": item.url,
        "source": item.source,
        "published_at": item.published_at,
        "companies": item.companies or [],
        "country_focus": item.country_focus,
        "topics": item.topics or [],
        "importance": round(item.importance, 2),
    }


def fallback_daily_items(candidates: list[Candidate], rules: dict[str, Any]) -> list[dict[str, Any]]:
    selected = balanced_select(candidates, rules)
    return [daily_item_from_candidate(item) for item in selected]


def preferred_country_focus(raw_value: str | None, candidate: Candidate | None) -> str:
    if candidate and candidate.companies and normalize_region(candidate.country_focus) != "OTHER":
        return normalize_region(candidate.country_focus)
    if raw_value:
        return normalize_region(raw_value)
    return normalize_region(candidate.country_focus if candidate else "OTHER")


def ensure_daily_balance(
    items: list[dict[str, Any]], candidates: list[Candidate], rules: dict[str, Any]
) -> list[dict[str, Any]]:
    max_items = int(rules["max_daily_items"])
    targets = region_targets(rules, max_items)
    selected = balanced_items(items, rules)
    seen_urls = {row.get("url") for row in selected if row.get("url")}

    def region_count(region: str) -> int:
        return sum(1 for row in selected if normalize_region(row.get("country_focus")) == region)

    for region in ("CN", "US", "OTHER"):
        for candidate in candidates:
            if region_count(region) >= targets[region]:
                break
            if candidate.url in seen_urls or normalize_region(candidate.country_focus) != region:
                continue
            selected.append(daily_item_from_candidate(candidate))
            seen_urls.add(candidate.url)

    selected = balanced_items(selected, rules)
    seen_urls = {row.get("url") for row in selected if row.get("url")}
    for candidate in candidates:
        if len(selected) >= max_items:
            break
        if candidate.url in seen_urls:
            continue
        selected.append(daily_item_from_candidate(candidate))
        seen_urls.add(candidate.url)

    return balanced_items(selected, rules)[:max_items]


def normalize_items(data: list[dict[str, Any]], candidates: list[Candidate], rules: dict[str, Any]) -> list[dict[str, Any]]:
    candidate_by_url = {row.url: row for row in candidates}
    companies = company_index(rules)
    rows: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        url = clean_text(raw.get("url"))
        title = clean_text(raw.get("title"))
        if not title or not url:
            continue
        candidate = candidate_by_url.get(url)
        summary = clean_text(raw.get("summary"))
        details = clean_text(raw.get("details"))
        raw_companies = [clean_text(value) for value in (raw.get("companies") or []) if clean_text(value)]
        detected_companies, detected_country = detect_companies(
            " ".join([title, summary, details, " ".join(raw_companies)]), companies
        )
        merged_companies: list[str] = []
        for name in [*(candidate.companies if candidate else []), *detected_companies, *raw_companies]:
            if name and name not in merged_companies:
                merged_companies.append(name)
        country_focus = preferred_country_focus(raw.get("country_focus"), candidate)
        if detected_country != "OTHER":
            country_focus = detected_country
        rows.append(
            {
                "id": stable_id(title, url),
                "title": title,
                "summary": summary,
                "details": details,
                "url": url,
                "source": clean_text(raw.get("source")) or (candidate.source if candidate else "未标明"),
                "published_at": clean_text(raw.get("published_at")) or (candidate.published_at if candidate else "未标明"),
                "companies": merged_companies[:4],
                "country_focus": country_focus,
                "topics": raw.get("topics") or (candidate.topics if candidate else []),
                "importance": float(raw.get("importance") or (candidate.importance if candidate else 0)),
            }
        )
    return ensure_daily_balance(rows, candidates, rules)


def balanced_select(candidates: list[Candidate], rules: dict[str, Any]) -> list[Candidate]:
    max_items = int(rules["max_daily_items"])
    selected: list[Candidate] = []
    selected_ids: set[int] = set()
    targets = region_targets(rules, max_items)

    for region in ("CN", "US", "OTHER"):
        pool = [row for row in candidates if normalize_region(row.country_focus) == region]
        for row in pool:
            if len([item for item in selected if normalize_region(item.country_focus) == region]) >= targets[region]:
                break
            if id(row) not in selected_ids:
                selected.append(row)
                selected_ids.add(id(row))

    for row in candidates:
        if len(selected) >= max_items:
            break
        if id(row) not in selected_ids:
            selected.append(row)
            selected_ids.add(id(row))

    return sorted(selected[:max_items], key=lambda row: row.importance, reverse=True)


def balanced_items(items: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    items = sorted(items, key=lambda row: float(row.get("importance") or 0), reverse=True)
    max_items = int(rules["max_daily_items"])
    selected: list[dict[str, Any]] = []
    selected_ids: set[int] = set()
    targets = region_targets(rules, max_items)

    for region in ("CN", "US", "OTHER"):
        pool = [row for row in items if normalize_region(row.get("country_focus")) == region]
        for row in pool:
            if len([item for item in selected if normalize_region(item.get("country_focus")) == region]) >= targets[region]:
                break
            if id(row) not in selected_ids:
                selected.append(row)
                selected_ids.add(id(row))

    for row in items:
        if len(selected) >= max_items:
            break
        if id(row) not in selected_ids:
            selected.append(row)
            selected_ids.add(id(row))
    return sorted(selected, key=lambda row: float(row.get("importance") or 0), reverse=True)


def collect_daily(date_value: str | None = None, root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    rules = load_rules(root)
    date_value = date_value or now_cn().strftime("%Y-%m-%d")
    candidates = discover_candidates(rules)
    items = llm_daily_items(candidates, rules) or fallback_daily_items(candidates, rules)
    daily = {
        "date": date_value,
        "generated_at": now_cn().isoformat(),
        "site_url": f"{rules['site_base_url']}/daily/{date_value}/",
        "image_path": f"assets/daily/{date_value}.jpg",
        "stats": {
            "total": len(items),
            "china_count": sum(1 for row in items if normalize_region(row.get("country_focus")) == "CN"),
            "us_count": sum(1 for row in items if normalize_region(row.get("country_focus")) == "US"),
            "other_count": sum(1 for row in items if normalize_region(row.get("country_focus")) == "OTHER"),
            "priority_company_count": sum(1 for row in items if row.get("companies")),
        },
        "items": items,
    }
    out_dir = root / "data" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{date_value}.json").open("w", encoding="utf-8") as fh:
        json.dump(daily, fh, ensure_ascii=False, indent=2)
    return daily


def load_daily_files(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((root / "data" / "daily").glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            rows.append(json.load(fh))
    return rows


def load_weekly_files(root: Path) -> list[dict[str, Any]]:
    rows = []
    weekly_dir = root / "data" / "weekly"
    if not weekly_dir.exists():
        return rows
    for path in sorted(weekly_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            rows.append(json.load(fh))
    return rows


def week_id_for(date_value: datetime) -> str:
    year, week, _ = date_value.isocalendar()
    return f"{year}-W{week:02d}"


def llm_weekly_items(candidates: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]] | None:
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        limit = int(rules["weekly_impact_items"])
        compact = [
            {
                "title": row["title"],
                "summary": row.get("summary", "")[:500],
                "details": row.get("details", "")[:700],
                "url": row["url"],
                "source": row.get("source"),
                "published_at": row.get("published_at"),
                "companies": row.get("companies", []),
                "country_focus": row.get("country_focus"),
                "topics": row.get("topics", []),
                "importance": row.get("importance", 0),
            }
            for row in candidates[:90]
        ]
        prompt = {
            "task": "从过去一周 AI 日报中抽取对世界或行业影响最大的新闻。",
            "rules": [
                f"选出 {limit} 条。",
                "优先影响范围大、技术或产业方向明确、可能改变竞争格局或监管环境的事件。",
                "输出中文。每条保留原链接、来源和发布时间。",
                "新增 impact_reason 字段，用 2-3 句说明为什么重要。",
                "不要编造事实；只能基于候选内容。",
            ],
            "candidates": compact,
        }
        output_text = llm_text(
            "你是 AI 产业周报主编。只输出 JSON 数组。",
            json.dumps(prompt, ensure_ascii=False),
        )
        data = extract_json_block(output_text)
        if isinstance(data, list):
            return [normalize_weekly_item(row) for row in data[:limit] if isinstance(row, dict)]
    except Exception as exc:
        print(f"[warn] OpenAI weekly curation failed, falling back to heuristic: {exc}")
    return None


def normalize_weekly_item(row: dict[str, Any]) -> dict[str, Any]:
    title = clean_text(row.get("title"))
    url = clean_text(row.get("url"))
    return {
        "id": stable_id(title, url),
        "title": title,
        "summary": clean_text(row.get("summary")),
        "details": clean_text(row.get("details")),
        "impact_reason": clean_text(row.get("impact_reason")) or clean_text(row.get("summary")),
        "url": url,
        "source": clean_text(row.get("source")),
        "published_at": clean_text(row.get("published_at")),
        "companies": row.get("companies") or [],
        "country_focus": normalize_region(row.get("country_focus")),
        "topics": row.get("topics") or [],
        "importance": float(row.get("importance") or 0),
    }


def build_weekly(end_date: str | None = None, root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    rules = load_rules(root)
    end_dt = parse_dt(end_date) if end_date else now_cn()
    if end_dt is None:
        end_dt = now_cn()
    start_dt = end_dt - timedelta(days=6)
    wanted_dates = {(start_dt + timedelta(days=idx)).strftime("%Y-%m-%d") for idx in range(7)}
    dailies = [row for row in load_daily_files(root) if row.get("date") in wanted_dates]
    all_items = []
    seen = set()
    for daily in dailies:
        for item in daily.get("items", []):
            key = stable_id(item.get("title", ""), item.get("url", ""))
            if key not in seen:
                seen.add(key)
                all_items.append(item)
    all_items.sort(key=lambda row: float(row.get("importance") or 0), reverse=True)

    weekly_items = llm_weekly_items(all_items, rules)
    if weekly_items is None:
        weekly_items = []
        for row in all_items[: int(rules["weekly_impact_items"])]:
            item = normalize_weekly_item(row)
            item["impact_reason"] = (
                item.get("impact_reason")
                or "该消息关联技术演进、产业竞争或监管环境变化。"
            )
            weekly_items.append(item)

    week_id = week_id_for(end_dt)
    weekly = {
        "week": week_id,
        "date_range": f"{start_dt.strftime('%Y-%m-%d')} 至 {end_dt.strftime('%Y-%m-%d')}",
        "generated_at": now_cn().isoformat(),
        "site_url": f"{rules['site_base_url']}/weekly/{week_id}/",
        "image_path": f"assets/weekly/{week_id}.jpg",
        "stats": {"total": len(weekly_items), "source_daily_count": len(dailies)},
        "items": weekly_items,
    }
    out_dir = root / "data" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{week_id}.json").open("w", encoding="utf-8") as fh:
        json.dump(weekly, fh, ensure_ascii=False, indent=2)
    return weekly


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for name in names:
        if name and Path(name).exists():
            return ImageFont.truetype(name, size=size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    tokens = list(text) if has_cjk(text) else re.split(r"(\s+)", text)
    lines: list[str] = []
    current = ""
    for token in tokens:
        trial = current + token
        if text_width(draw, trial, font) <= max_width or not current:
            current = trial
            continue
        lines.append(current.strip())
        current = token.strip()
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current.strip())
    if len(lines) == max_lines and len("".join(lines)) < len(text):
        lines[-1] = lines[-1].rstrip("，。,. ") + "..."
    return lines


def draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    xy: tuple[int, int],
    font: ImageFont.ImageFont,
    fill: str,
    line_gap: int = 8,
) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


def display_text(value: str | None) -> str:
    text = clean_text(value)
    replacements = [
        "自动化已根据标题、来源摘要、公司和主题标签判断其与今日 AI 动态相关。",
        "建议打开原文查看完整背景、数据和引用。",
        "原始信息来自非中文或机器可读摘要；",
        "该消息在本周候选集中综合重要性较高，",
    ]
    for old in replacements:
        text = text.replace(old, "")
    text = text.replace("概览已翻译/转写为中文，", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def render_overview_image(
    title: str,
    items: list[dict[str, Any]],
    out_path: Path,
    kind: str = "daily",
    subtitle: str = "",
    note: str = "",
) -> Path:
    width = 1240
    margin = 58
    card_gap = 16
    card_pad = 24
    content_w = width - margin * 2
    title_font = find_font(44, bold=True)
    sub_font = find_font(22)
    item_title_font = find_font(24, bold=True)
    body_font = find_font(19)
    meta_font = find_font(17)
    num_font = find_font(20, bold=True)

    probe = Image.new("RGB", (width, 200), "#F7F7F3")
    draw = ImageDraw.Draw(probe)
    cards: list[dict[str, Any]] = []
    header_h = 188 if subtitle or note else 156
    card_start_y = header_h + 32
    total_h = card_start_y
    for idx, item in enumerate(items, start=1):
        title_lines = wrap_text(draw, item.get("title", ""), item_title_font, content_w - card_pad * 2 - 58, 2)
        body = item.get("impact_reason") if kind == "weekly" else item.get("summary")
        body = display_text(body)
        body_lines = wrap_text(draw, body or "", body_font, content_w - card_pad * 2, 3)
        meta = f"{item.get('source', '未标明')}｜{item.get('published_at', '未标明')}"
        if item.get("companies"):
            meta = f"{'、'.join(item.get('companies', [])[:3])}｜{meta}"
        meta_lines = wrap_text(draw, meta, meta_font, content_w - card_pad * 2, 2)
        card_h = 28 + len(title_lines) * 32 + 8 + len(body_lines) * 27 + 10 + len(meta_lines) * 24 + 26
        cards.append({"title": title_lines, "body": body_lines, "meta": meta_lines, "height": card_h})
        total_h += card_h + card_gap
    height = total_h + 58
    image = Image.new("RGB", (width, height), "#F7F7F3")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, header_h), fill="#1E5B4F")
    draw.text((margin, 40), title, font=title_font, fill="#FFFFFF")
    if subtitle:
        draw.text((margin, 104), subtitle, font=sub_font, fill="#FFFFFF")
    if note:
        draw.text((margin, 138), note, font=sub_font, fill="#FFFFFF")

    palette = ["#C4554D", "#2B6CB0", "#1E7A5F", "#B56B24"]
    y = card_start_y
    for idx, (item, card) in enumerate(zip(items, cards), start=1):
        card_box = (margin, y, margin + content_w, y + card["height"])
        draw.rounded_rectangle(card_box, radius=14, fill="#FFFFFF")
        accent = palette[(idx - 1) % len(palette)]
        circle = (margin + 22, y + 25, margin + 56, y + 59)
        draw.ellipse(circle, fill=accent)
        num = f"{idx:02d}"
        num_box = draw.textbbox((0, 0), num, font=num_font)
        draw.text(
            (margin + 39 - (num_box[2] - num_box[0]) / 2, y + 30),
            num,
            font=num_font,
            fill="#FFFFFF",
        )
        tx = margin + card_pad + 52
        line_y = y + 24
        line_y = draw_lines(draw, card["title"], (tx, line_y), item_title_font, "#17231F", 8)
        line_y += 5
        line_y = draw_lines(draw, card["body"], (margin + card_pad, line_y), body_font, "#24332E", 8)
        line_y += 5
        draw_lines(draw, card["meta"], (margin + card_pad, line_y), meta_font, "#5F6863", 7)
        y += card["height"] + card_gap

    footer = "图片用于快速阅读；完整详情与可点击原文链接请打开网页版。"
    draw.text((margin, height - 42), footer, font=meta_font, fill="#5F6863")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    quality = 88
    while quality >= 52:
        image.save(out_path, quality=quality, optimize=True)
        if out_path.stat().st_size <= 1_900_000:
            break
        quality -= 8
    return out_path


def rel_from_page(target: str, depth: int) -> str:
    return "../" * depth + target


def item_card(item: dict[str, Any]) -> str:
    companies = "、".join(item.get("companies", [])[:4])
    meta_bits = [item.get("source", "未标明"), item.get("published_at", "未标明")]
    if companies:
        meta_bits.insert(0, companies)
    return f"""
    <article class="news-card">
      <div class="meta">{html.escape("｜".join(meta_bits))}</div>
      <h2>{html.escape(item.get("title", ""))}</h2>
      <p class="summary">{html.escape(display_text(item.get("summary", "")))}</p>
      <p>{html.escape(display_text(item.get("details", "")))}</p>
      <a class="source-link" href="{html.escape(item.get("url", ""))}" target="_blank" rel="noopener">打开原文</a>
    </article>
    """


def html_page(title: str, body: str, depth: int = 0) -> str:
    css = rel_from_page("styles.css", depth)
    index = rel_from_page("index.html", depth)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="{css}">
</head>
<body>
  <header class="topbar">
    <a href="{index}">AI News</a>
  </header>
  {body}
</body>
</html>
"""


def render_daily_page(daily: dict[str, Any], docs: Path) -> None:
    date_value = daily["date"]
    stats = daily.setdefault("stats", {})
    stats["total"] = stats.get("total", len(daily.get("items", [])))
    stats["china_count"] = stats.get(
        "china_count",
        sum(1 for row in daily.get("items", []) if normalize_region(row.get("country_focus")) == "CN"),
    )
    stats["us_count"] = stats.get(
        "us_count",
        sum(1 for row in daily.get("items", []) if normalize_region(row.get("country_focus")) == "US"),
    )
    stats["other_count"] = stats.get(
        "other_count",
        sum(1 for row in daily.get("items", []) if normalize_region(row.get("country_focus")) == "OTHER"),
    )
    stats["priority_company_count"] = stats.get(
        "priority_company_count",
        sum(1 for row in daily.get("items", []) if row.get("companies")),
    )
    image_path = docs / daily["image_path"]
    render_overview_image(
        title=f"AI 新闻日报 · {date_value}",
        items=daily["items"],
        out_path=image_path,
        kind="daily",
    )
    cards = "\n".join(item_card(item) for item in daily["items"])
    body = f"""
  <main class="page">
    <section class="hero">
      <p class="eyebrow">Daily Collection</p>
      <h1>AI 新闻日报 · {html.escape(date_value)}</h1>
      <a class="primary-link" href="{html.escape(daily['site_url'])}">当前页面链接</a>
    </section>
    <section class="list">{cards}</section>
  </main>
"""
    page_dir = docs / "daily" / date_value
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "index.html").write_text(html_page(f"AI 新闻日报 {date_value}", body, depth=2), encoding="utf-8")


def render_weekly_page(weekly: dict[str, Any], docs: Path) -> None:
    week = weekly["week"]
    image_path = docs / weekly["image_path"]
    render_overview_image(
        title=f"AI 影响力周报 · {week}",
        items=weekly["items"],
        out_path=image_path,
        kind="weekly",
    )
    cards = "\n".join(item_card(item) for item in weekly["items"])
    body = f"""
  <main class="page">
    <section class="hero">
      <p class="eyebrow">Weekly Impact Digest</p>
      <h1>AI 影响力周报 · {html.escape(week)}</h1>
      <a class="primary-link" href="{html.escape(weekly['site_url'])}">当前页面链接</a>
    </section>
    <section class="list">{cards}</section>
  </main>
"""
    page_dir = docs / "weekly" / week
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / "index.html").write_text(html_page(f"AI 影响力周报 {week}", body, depth=2), encoding="utf-8")


def render_index(dailies: list[dict[str, Any]], weeklies: list[dict[str, Any]], docs: Path) -> None:
    daily_links = "\n".join(
        f'<a class="archive-link" href="daily/{html.escape(row["date"])}/"><strong>{html.escape(row["date"])}</strong></a>'
        for row in reversed(dailies[-30:])
    )
    weekly_links = "\n".join(
        f'<a class="archive-link" href="weekly/{html.escape(row["week"])}/"><strong>{html.escape(row["week"])}</strong><span>{html.escape(row["date_range"])}</span></a>'
        for row in reversed(weeklies[-12:])
    )
    body = f"""
  <main class="page">
    <section class="hero">
      <p class="eyebrow">AI News Archive</p>
      <h1>AI 新闻集合</h1>
    </section>
    <section class="archive-grid">
      <div>
        <h2>日报</h2>
        <div class="archive-list">{daily_links or '<p>暂无日报。</p>'}</div>
      </div>
      <div>
        <h2>周报</h2>
        <div class="archive-list">{weekly_links or '<p>暂无周报。</p>'}</div>
      </div>
    </section>
  </main>
"""
    (docs / "index.html").write_text(html_page("AI 新闻集合", body), encoding="utf-8")


def render_css(docs: Path) -> None:
    css = """
:root {
  color-scheme: light;
  --ink: #17231f;
  --muted: #5f6863;
  --paper: #f7f7f3;
  --panel: #ffffff;
  --accent: #1e5b4f;
  --line: #d8e2dc;
  --link: #1d63a7;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Inter", "Noto Sans CJK SC", "Microsoft YaHei UI", system-ui, sans-serif;
  background: var(--paper);
  color: var(--ink);
}
.topbar {
  height: 56px;
  display: flex;
  align-items: center;
  padding: 0 28px;
  background: var(--accent);
}
.topbar a { color: #fff; text-decoration: none; font-weight: 800; }
.page {
  width: min(1120px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 34px 0 64px;
}
.hero {
  padding: 16px 0 28px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 28px;
}
.eyebrow {
  margin: 0 0 8px;
  color: #b34f48;
  font-size: 13px;
  letter-spacing: 0;
  font-weight: 800;
  text-transform: uppercase;
}
h1 {
  margin: 0;
  font-size: clamp(32px, 6vw, 58px);
  letter-spacing: 0;
  line-height: 1.05;
}
.hero p {
  color: var(--muted);
  font-size: 18px;
  line-height: 1.7;
  max-width: 760px;
}
.primary-link, .source-link {
  color: var(--link);
  font-weight: 800;
  text-decoration: none;
}
.list {
  display: grid;
  gap: 16px;
}
.news-card {
  background: var(--panel);
  border: 1px solid var(--line);
  padding: 24px;
  border-radius: 8px;
}
.news-card h2 {
  margin: 7px 0 12px;
  font-size: 24px;
  line-height: 1.35;
}
.news-card p {
  color: #283a34;
  line-height: 1.75;
  margin: 0 0 12px;
}
.news-card .summary {
  font-weight: 650;
}
.meta {
  color: var(--muted);
  font-size: 14px;
  line-height: 1.6;
}
.archive-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 22px;
}
.archive-grid h2 { margin: 0 0 14px; }
.archive-list {
  display: grid;
  gap: 10px;
}
.archive-link {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #fff;
  color: var(--ink);
  text-decoration: none;
}
.archive-link span { color: var(--muted); }
@media (max-width: 760px) {
  .archive-grid { grid-template-columns: 1fr; }
  .archive-link { display: block; }
  .news-card { padding: 18px; }
}
"""
    (docs / "styles.css").write_text(css.strip() + "\n", encoding="utf-8")


def render_site(root: Path | None = None) -> None:
    root = root or repo_root()
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "assets" / "daily").mkdir(parents=True, exist_ok=True)
    (docs / "assets" / "weekly").mkdir(parents=True, exist_ok=True)
    render_css(docs)
    dailies = load_daily_files(root)
    weeklies = load_weekly_files(root)
    for daily in dailies:
        render_daily_page(daily, docs)
    for weekly in weeklies:
        render_weekly_page(weekly, docs)
    render_index(dailies, weeklies, docs)


def send_wecom_markdown(webhook: str, content: str) -> dict[str, Any]:
    resp = requests.post(
        webhook,
        json={"msgtype": "markdown", "markdown": {"content": content}},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"WeCom markdown send failed: {data}")
    return data


def send_wecom_image(webhook: str, image_path: Path) -> dict[str, Any]:
    raw = image_path.read_bytes()
    b64 = __import__("base64").b64encode(raw).decode("ascii")
    md5 = hashlib.md5(raw).hexdigest()
    resp = requests.post(
        webhook,
        json={"msgtype": "image", "image": {"base64": b64, "md5": md5}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"WeCom image send failed: {data}")
    return data


def send_daily(daily: dict[str, Any], root: Path | None = None) -> None:
    root = root or repo_root()
    webhook = os.getenv("WECHAT_WEBHOOK_URL")
    if not webhook:
        print("[info] WECHAT_WEBHOOK_URL is not set; skip WeCom push.")
        return
    image_path = root / "docs" / daily["image_path"]
    send_wecom_image(webhook, image_path)
    content = textwrap.dedent(
        f"""
        **AI 新闻日报｜{daily['date']}**
        [查看网页版]({daily['site_url']})
        """
    ).strip()
    send_wecom_markdown(webhook, content)


def send_weekly(weekly: dict[str, Any], root: Path | None = None) -> None:
    root = root or repo_root()
    webhook = os.getenv("WECHAT_WEBHOOK_URL")
    if not webhook:
        print("[info] WECHAT_WEBHOOK_URL is not set; skip WeCom push.")
        return
    image_path = root / "docs" / weekly["image_path"]
    send_wecom_image(webhook, image_path)
    content = textwrap.dedent(
        f"""
        **AI 影响力周报｜{weekly['week']}**
        [查看网页版]({weekly['site_url']})
        """
    ).strip()
    send_wecom_markdown(webhook, content)


def daily_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Date in YYYY-MM-DD. Defaults to current Asia/Shanghai date.")
    parser.add_argument("--send", action="store_true", help="Send image and page URL to WeCom.")
    args = parser.parse_args(argv)
    daily = collect_daily(args.date)
    render_site()
    if args.send:
        send_daily(daily)
    print(json.dumps({"date": daily["date"], "items": len(daily["items"]), "url": daily["site_url"]}, ensure_ascii=False))


def weekly_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", help="End date for the 7-day digest. Defaults to current Asia/Shanghai date.")
    parser.add_argument("--send", action="store_true", help="Send image and page URL to WeCom.")
    args = parser.parse_args(argv)
    weekly = build_weekly(args.end_date)
    render_site()
    if args.send:
        send_weekly(weekly)
    print(json.dumps({"week": weekly["week"], "items": len(weekly["items"]), "url": weekly["site_url"]}, ensure_ascii=False))


def render_cli(argv: list[str] | None = None) -> None:
    render_site()
    print(json.dumps({"rendered": True, "docs": str(repo_root() / "docs")}, ensure_ascii=False))


def send_latest_cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["daily", "weekly"])
    parser.add_argument("--id", help="Daily date YYYY-MM-DD or weekly id YYYY-Www. Defaults to latest.")
    args = parser.parse_args(argv)
    root = repo_root()
    if args.kind == "daily":
        rows = load_daily_files(root)
        if not rows:
            raise SystemExit("No daily data found.")
        row = next((item for item in rows if item.get("date") == args.id), rows[-1])
        send_daily(row, root)
        print(json.dumps({"sent": "daily", "date": row["date"], "url": row["site_url"]}, ensure_ascii=False))
    else:
        rows = load_weekly_files(root)
        if not rows:
            raise SystemExit("No weekly data found.")
        row = next((item for item in rows if item.get("week") == args.id), rows[-1])
        send_weekly(row, root)
        print(json.dumps({"sent": "weekly", "week": row["week"], "url": row["site_url"]}, ensure_ascii=False))
