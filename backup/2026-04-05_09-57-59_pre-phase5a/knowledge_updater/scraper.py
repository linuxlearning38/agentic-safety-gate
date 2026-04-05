"""
AVA Knowledge Updater - Phase 2
scraper.py — Blog article fetcher with RSS parsing, rate limiting, and quality scoring
"""

import hashlib
import logging
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("ava.scraper")


@dataclass
class Article:
    url: str
    title: str
    source_name: str
    content: str
    published_date: Optional[str]
    quality_score: float = 0.0
    url_hash: str = ""
    content_hash: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.url_hash = hashlib.sha256(self.url.encode()).hexdigest()
        self.content_hash = hashlib.sha256(self.content.encode()).hexdigest()


class QualityScorer:
    TECHNICAL_KEYWORDS = [
        "kubernetes", "docker", "terraform", "ci/cd", "pipeline", "deployment",
        "infrastructure", "microservice", "container", "helm", "ansible", "jenkins",
        "github actions", "aws", "azure", "gcp", "monitoring", "observability",
        "prometheus", "grafana", "kafka", "redis", "postgres", "nginx", "istio",
        "service mesh", "api gateway", "load balancer", "autoscaling", "devops",
        "sre", "platform engineering", "opentelemetry", "tracing", "alerting",
        "incident", "reliability", "security", "zero trust", "rbac", "iam",
        "llm", "mlops", "rag", "vector", "embedding", "agent", "automation"
    ]

    PAYWALL_SIGNALS = [
        "subscribe to continue", "subscribe to read", "member-only",
        "sign in to read", "create a free account", "unlock this story",
        "this story is for paying subscribers"
    ]

    def __init__(self, quality_config: dict):
        self.config = quality_config

    def score(self, title: str, content: str) -> float:
        score = 0.0
        content_lower = content.lower()
        word_count = len(content.split())

        if any(signal in content_lower for signal in self.PAYWALL_SIGNALS):
            logger.debug(f"Paywall detected in: {title[:60]}")
            return 0.0

        if word_count < self.config["signals"]["word_count_min"]:
            score += 0.0
        elif word_count >= self.config["signals"]["word_count_ideal"]:
            score += 0.25
        else:
            ratio = (word_count - self.config["signals"]["word_count_min"]) / \
                    (self.config["signals"]["word_count_ideal"] - self.config["signals"]["word_count_min"])
            score += 0.25 * ratio

        has_code = bool(re.search(r'```|\bcode\b|<code>|\$\s+\w', content))
        if has_code:
            score += self.config["signals"]["has_code_blocks"]

        has_headers = bool(re.search(r'^#{1,3}\s|\n#{1,3}\s', content))
        if has_headers:
            score += self.config["signals"]["has_headers"]

        keyword_hits = sum(1 for kw in self.TECHNICAL_KEYWORDS if kw in content_lower)
        tech_score = min(keyword_hits / 10.0, 1.0) * self.config["signals"]["technical_keywords"]
        score += tech_score

        score += self.config["signals"]["no_paywall_signals"]

        final = min(round(score, 3), 1.0)
        logger.debug(f"Quality score {final:.2f} | words={word_count} | code={has_code} | kw={keyword_hits} | {title[:50]}")
        return final


class ContentExtractor:
    NOISE_SELECTORS = [
        "nav", "footer", "header", ".sidebar", ".cookie-banner",
        ".newsletter-signup", ".related-posts", ".tags", ".comments",
        "[class*='ad-']", "[class*='promo']", "[id*='cookie']",
        "script", "style", "noscript", ".social-share", ".author-bio"
    ]

    CONTENT_SELECTORS = [
        "article", "main", ".post-content", ".entry-content",
        ".article-body", ".blog-post", "[role='main']", ".content"
    ]

    def extract(self, html: str, url: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        for selector in self.NOISE_SELECTORS:
            for el in soup.select(selector):
                el.decompose()

        content_el = None
        for selector in self.CONTENT_SELECTORS:
            content_el = soup.select_one(selector)
            if content_el:
                break

        if not content_el:
            content_el = soup.find("body") or soup

        text_parts = []
        for el in content_el.find_all(["h1", "h2", "h3", "h4", "p", "pre", "code", "li", "blockquote"]):
            text = el.get_text(strip=True)
            if not text or len(text) < 10:
                continue
            tag = el.name
            if tag in ("h1", "h2", "h3"):
                text_parts.append(f"\n## {text}\n")
            elif tag in ("pre", "code"):
                text_parts.append(f"\n```\n{text}\n```\n")
            elif tag == "blockquote":
                text_parts.append(f"\n> {text}\n")
            else:
                text_parts.append(text)

        return "\n".join(text_parts).strip()


class BlogScraper:
    def __init__(self, config: dict):
        self.config = config
        self.scraper_config = config["scraper"]
        self.quality_config = config["quality"]
        self.sources = config["sources"]
        self.scorer = QualityScorer(self.quality_config)
        self.extractor = ContentExtractor()
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.scraper_config["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        return session

    def _fetch_url(self, url: str) -> Optional[str]:
        for attempt in range(self.scraper_config["max_retries"] + 1):
            try:
                resp = self.session.get(
                    url,
                    timeout=self.scraper_config["request_timeout"],
                    allow_redirects=True
                )
                resp.raise_for_status()
                return resp.text
            except requests.exceptions.HTTPError as e:
                logger.warning(f"HTTP {e.response.status_code} for {url}")
                return None
            except requests.exceptions.RequestException as e:
                if attempt < self.scraper_config["max_retries"]:
                    logger.warning(f"Attempt {attempt+1} failed for {url}: {e}. Retrying...")
                    time.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to fetch {url} after {attempt+1} attempts: {e}")
                    return None
        return None

    def _parse_rss(self, rss_url: str, max_articles: int) -> list:
        logger.info(f"Parsing RSS: {rss_url}")
        try:
            feed = feedparser.parse(rss_url)
            if feed.bozo and not feed.entries:
                logger.warning(f"RSS parse warning for {rss_url}: {feed.bozo_exception}")
                return []

            entries = []
            for entry in feed.entries[:max_articles]:
                url = entry.get("link", "")
                title = entry.get("title", "Untitled")
                published = entry.get("published", entry.get("updated", ""))
                if url:
                    entries.append({"url": url, "title": title, "published": published})
            logger.info(f"Found {len(entries)} entries in feed")
            return entries
        except Exception as e:
            logger.error(f"RSS parse error for {rss_url}: {e}")
            return []

    def _fetch_article(self, entry: dict, source_name: str, min_length: int) -> Optional[Article]:
        url = entry["url"]
        title = entry["title"]

        logger.info(f"Fetching: {title[:60]} — {url}")
        html = self._fetch_url(url)
        if not html:
            return None

        content = self.extractor.extract(html, url)

        if len(content) < min_length:
            logger.info(f"Skipped (too short: {len(content)} chars): {title[:60]}")
            return None

        quality_score = self.scorer.score(title, content)
        if quality_score < self.quality_config["min_score"]:
            logger.info(f"Skipped (quality {quality_score:.2f} < {self.quality_config['min_score']}): {title[:60]}")
            return None

        return Article(
            url=url,
            title=title,
            source_name=source_name,
            content=content,
            published_date=entry.get("published", ""),
            quality_score=quality_score,
            metadata={
                "source": source_name,
                "title": title,
                "url": url,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "published": entry.get("published", "unknown"),
            }
        )

    def fetch_source(self, source: dict) -> list:
        name = source["name"]
        articles = []

        if not source.get("enabled", True):
            logger.info(f"Source disabled: {name}")
            return []

        logger.info(f"{'='*50}")
        logger.info(f"Processing source: {name}")

        entries = self._parse_rss(source["rss"], source["max_articles"])
        if not entries:
            logger.warning(f"No entries found for {name}")
            return []

        for entry in entries:
            article = self._fetch_article(entry, name, source["min_content_length"])
            if article:
                articles.append(article)
                logger.info(f"✓ Accepted [{article.quality_score:.2f}]: {article.title[:60]}")
            time.sleep(self.scraper_config["delay_between_requests"])

        logger.info(f"Source {name}: {len(articles)}/{len(entries)} articles accepted")
        return articles

    def fetch_all(self) -> list:
        all_articles = []
        for source in self.sources:
            try:
                articles = self.fetch_source(source)
                all_articles.extend(articles)
            except Exception as e:
                logger.error(f"Unexpected error fetching {source['name']}: {e}", exc_info=True)

        logger.info(f"{'='*50}")
        logger.info(f"Scraping complete: {len(all_articles)} articles accepted")
        return all_articles
