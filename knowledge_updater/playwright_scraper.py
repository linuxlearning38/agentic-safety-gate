"""
AVA Knowledge Updater - Phase 5A Day 3
playwright_scraper.py — Async Playwright scraper for sources that block RSS.

Targets: HashiCorp Blog, Datadog Engineering, Jenkins Blog, Spacelift Blog
Stores into devops_blogs_v1 ChromaDB collection.
Matches chunking/embedding/dedup pattern from ingestor.py exactly.
"""

import asyncio
import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import chromadb
import requests
from chromadb.config import Settings
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("ava.playwright_scraper")

# ── Config ─────────────────────────────────────────────────────────────────────
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/home/manoj/ava-data/chromadb")
COLLECTION_NAME = "devops_blogs_v1"
OLLAMA_HOST    = os.getenv("OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL    = "nomic-embed-text"

CHUNK_SIZE     = 400
CHUNK_OVERLAP  = 50
MIN_CHUNK_SIZE = 150
MIN_CONTENT_LEN = 300
REQUEST_DELAY  = 3      # seconds between article fetches
PAGE_TIMEOUT   = 30_000 # ms

PLAYWRIGHT_SOURCES = [
    {
        "name": "HashiCorp Blog",
        "url": "https://www.hashicorp.com/blog",
        "article_selector": "a[href*='/blog/']",
        "content_selector": "article, .blog-content, main",
        "max_articles": 10,
        "enabled": True
    },
    {
        "name": "Datadog Engineering",
        "url": "https://www.datadoghq.com/blog/engineering/",
        "article_selector": "a[href*='/blog/']",
        "content_selector": "article, .post-content, main",
        "max_articles": 10,
        "enabled": True
    },
    {
        "name": "Jenkins Blog",
        "url": "https://www.jenkins.io/blog/",
        "article_selector": "a[href*='/blog/']",
        "content_selector": "article, .content, main",
        "max_articles": 10,
        "enabled": True
    },
    {
        "name": "Spacelift Blog",
        "url": "https://spacelift.io/blog",
        "article_selector": "a[href*='/blog/']",
        "content_selector": "article, .post-body, main",
        "max_articles": 10,
        "enabled": True
    },
]

# ── Noise selectors to remove from pages ──────────────────────────────────────
NOISE_SELECTORS = [
    "nav", "footer", "header", ".sidebar", ".cookie-banner",
    ".newsletter-signup", ".related-posts", ".tags", ".comments",
    "[class*='ad-']", "[class*='promo']", "[id*='cookie']",
    "script", "style", "noscript", ".social-share", ".author-bio",
    ".subscription", ".cta", ".banner", "[class*='popup']",
    "[class*='overlay']", "[class*='modal']",
]

TECHNICAL_KEYWORDS = [
    "kubernetes", "docker", "terraform", "ci/cd", "pipeline", "deployment",
    "infrastructure", "microservice", "container", "helm", "ansible",
    "github actions", "aws", "azure", "gcp", "monitoring", "observability",
    "prometheus", "grafana", "kafka", "redis", "postgres", "nginx",
    "service mesh", "api gateway", "load balancer", "autoscaling", "devops",
    "sre", "platform engineering", "opentelemetry", "tracing", "alerting",
    "incident", "reliability", "security", "zero trust", "rbac", "iam",
    "vault", "consul", "nomad", "hcl", "packer", "boundary", "waypoint",
    "datadog", "jenkins", "spacelift", "hashicorp",
]


# ── Embedding Client (matches ingestor.py exactly) ─────────────────────────────
class EmbeddingClient:
    def __init__(self, ollama_url: str, model: str):
        self.url   = f"{ollama_url}/api/embeddings"
        self.model = model

    def embed(self, text: str):
        try:
            resp = requests.post(
                self.url,
                json={"model": self.model, "prompt": text},
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except Exception as e:
            logger.error(f"Embedding failed: {e}")
            return None


# ── Text chunker (matches ingestor.py exactly) ─────────────────────────────────
def chunk_text(text: str) -> list:
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if len(text) <= CHUNK_SIZE:
        return [text] if len(text) >= MIN_CHUNK_SIZE else []

    chunks = []
    start  = 0
    while start < len(text):
        end = start + CHUNK_SIZE
        if end >= len(text):
            chunk = text[start:].strip()
            if len(chunk) >= MIN_CHUNK_SIZE:
                chunks.append(chunk)
            break

        break_pos = text.rfind('\n\n', start, end)
        if break_pos == -1 or break_pos <= start:
            break_pos = text.rfind('. ', start, end)
        if break_pos == -1 or break_pos <= start:
            break_pos = text.rfind(' ', start, end)
        if break_pos == -1 or break_pos <= start:
            break_pos = end

        chunk = text[start:break_pos].strip()
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append(chunk)
        start = max(break_pos - CHUNK_OVERLAP, start + 1)

    return chunks


# ── ChromaDB helper ────────────────────────────────────────────────────────────
def get_collection():
    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False)
    )
    col = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"description": "AVA dynamic knowledge — engineering blog articles"}
    )
    logger.info(f"Collection '{COLLECTION_NAME}' ready — {col.count()} existing chunks")
    return col


def is_url_indexed(collection, url_hash: str) -> bool:
    try:
        results = collection.get(
            where={"url_hash": url_hash},
            limit=1,
            include=["metadatas"]
        )
        return len(results["ids"]) > 0
    except Exception:
        return False


def quality_score(title: str, content: str) -> float:
    score     = 0.0
    content_l = content.lower()
    words     = len(content.split())

    # Word count signal
    if words >= 1200:
        score += 0.25
    elif words >= 300:
        score += 0.25 * (words - 300) / 900

    # Code blocks
    if re.search(r'```|\bcode\b|<code>|\$\s+\w', content):
        score += 0.25

    # Headers
    if re.search(r'^#{1,3}\s|\n#{1,3}\s', content):
        score += 0.10

    # Technical keywords
    hits   = sum(1 for kw in TECHNICAL_KEYWORDS if kw in content_l)
    score += min(hits / 10.0, 1.0) * 0.20

    # No paywall signals
    paywall = ["subscribe to continue", "subscribe to read", "member-only",
               "sign in to read", "create a free account", "unlock this story"]
    if not any(p in content_l for p in paywall):
        score += 0.15

    return min(round(score, 3), 1.0)


# ── Content extraction from Playwright page ────────────────────────────────────
async def extract_content(page, content_selector: str, url: str) -> str:
    """Remove noise elements, then extract text from content area."""
    # Remove noise elements
    for sel in NOISE_SELECTORS:
        try:
            await page.evaluate(f"""
                document.querySelectorAll('{sel}').forEach(el => el.remove())
            """)
        except Exception:
            pass

    # Try content selectors in order
    content_text = ""
    for sel in [s.strip() for s in content_selector.split(",")]:
        try:
            el = await page.query_selector(sel)
            if el:
                content_text = await el.inner_text()
                if len(content_text.strip()) > MIN_CONTENT_LEN:
                    break
        except Exception:
            continue

    # Fallback: full body text
    if not content_text or len(content_text.strip()) < MIN_CONTENT_LEN:
        try:
            content_text = await page.inner_text("body")
        except Exception:
            content_text = ""

    # Clean up whitespace
    lines = [line.strip() for line in content_text.splitlines()]
    lines = [l for l in lines if l and len(l) > 10]
    return "\n".join(lines)


# ── Article link extraction ────────────────────────────────────────────────────
async def get_article_urls(page, source: dict) -> list:
    """Extract unique article URLs from the source listing page."""
    base_url = f"{urlparse(source['url']).scheme}://{urlparse(source['url']).netloc}"
    seen     = set()
    urls     = []

    try:
        links = await page.query_selector_all(source["article_selector"])
        for link in links:
            href = await link.get_attribute("href")
            if not href:
                continue
            # Make absolute
            if href.startswith("/"):
                href = urljoin(base_url, href)
            elif not href.startswith("http"):
                continue

            # Must be same domain
            if urlparse(href).netloc not in base_url and base_url not in urlparse(href).netloc:
                if urlparse(source["url"]).netloc not in urlparse(href).netloc:
                    continue

            # Skip listing page itself, anchors, query strings with no path
            path = urlparse(href).path.rstrip("/")
            if not path or path in ("/blog", "/blog/", "/engineering", "/engineering/"):
                continue

            if href not in seen:
                seen.add(href)
                urls.append(href)

            if len(urls) >= source["max_articles"] * 3:  # fetch extra, filter later
                break

    except Exception as e:
        logger.warning(f"Link extraction failed for {source['name']}: {e}")

    return urls[:source["max_articles"] * 2]


# ── Per-source scraper ─────────────────────────────────────────────────────────
async def scrape_source(source: dict, browser, collection) -> dict:
    stats = {
        "name":           source["name"],
        "articles_found": 0,
        "articles_added": 0,
        "chunks_added":   0,
        "skipped":        0,
        "errors":         0,
    }

    if not source.get("enabled", True):
        logger.info(f"Source disabled: {source['name']}")
        return stats

    logger.info(f"{'='*55}")
    logger.info(f"Processing: {source['name']} — {source['url']}")

    embedder = EmbeddingClient(OLLAMA_HOST, EMBED_MODEL)
    context  = await browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = await context.new_page()

    try:
        # Load listing page
        await page.goto(source["url"], timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        await asyncio.sleep(2)  # let JS render

        article_urls = await get_article_urls(page, source)
        stats["articles_found"] = len(article_urls)
        logger.info(f"Found {len(article_urls)} article URLs from {source['name']}")

        if not article_urls:
            logger.warning(f"No article URLs found for {source['name']} — selector may need update")
            return stats

        processed = 0
        for art_url in article_urls:
            if processed >= source["max_articles"]:
                break

            url_hash = hashlib.sha256(art_url.encode()).hexdigest()

            if is_url_indexed(collection, url_hash):
                logger.info(f"  Already indexed: {art_url[:80]}")
                stats["skipped"] += 1
                continue

            await asyncio.sleep(REQUEST_DELAY)

            try:
                art_page = await context.new_page()
                await art_page.goto(art_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                await asyncio.sleep(1)

                title = await art_page.title()
                title = re.sub(r'\s*[|\-–—].*$', '', title).strip() or art_url

                content = await extract_content(art_page, source["content_selector"], art_url)
                await art_page.close()

                if len(content) < MIN_CONTENT_LEN:
                    logger.info(f"  Skipped (too short: {len(content)} chars): {title[:60]}")
                    stats["skipped"] += 1
                    continue

                qs = quality_score(title, content)
                if qs < 0.35:
                    logger.info(f"  Skipped (quality {qs:.2f}): {title[:60]}")
                    stats["skipped"] += 1
                    continue

                # Build summary chunk (mirrors ingestor.py pattern)
                summary = (
                    f"Article: {title}\n"
                    f"Source: {source['name']}\n"
                    f"URL: {art_url}\n\n"
                    f"{content[:600]}"
                )
                chunks = [summary] + chunk_text(content)

                ids_to_add   = []
                embs_to_add  = []
                docs_to_add  = []
                metas_to_add = []

                for i, chunk_text_item in enumerate(chunks):
                    enriched   = f"Article: {title}\n\n{chunk_text_item}"
                    chunk_hash = hashlib.sha256(enriched.encode()).hexdigest()
                    chunk_id   = f"blog_{url_hash[:16]}_{i:04d}"

                    embedding = embedder.embed(enriched)
                    if not embedding:
                        logger.warning(f"  Embedding failed for chunk {i} of {title[:40]}")
                        continue

                    meta = {
                        "source":      source["name"],
                        "title":       title[:200],
                        "url":         art_url,
                        "url_hash":    url_hash,
                        "chunk_index": str(i),
                        "chunk_hash":  chunk_hash,
                        "scraped_at":  datetime.now(timezone.utc).isoformat(),
                        "scraper":     "playwright",
                        "quality_score": str(qs),
                    }

                    ids_to_add.append(chunk_id)
                    embs_to_add.append(embedding)
                    docs_to_add.append(enriched)
                    metas_to_add.append(meta)

                if ids_to_add:
                    collection.upsert(
                        ids=ids_to_add,
                        embeddings=embs_to_add,
                        documents=docs_to_add,
                        metadatas=metas_to_add
                    )
                    stats["chunks_added"]   += len(ids_to_add)
                    stats["articles_added"] += 1
                    processed += 1
                    logger.info(
                        f"  ✓ [{qs:.2f}] {len(ids_to_add)} chunks — {title[:60]}"
                    )

            except PlaywrightTimeout:
                logger.warning(f"  Timeout fetching: {art_url[:80]}")
                stats["errors"] += 1
            except Exception as e:
                logger.warning(f"  Error fetching {art_url[:80]}: {e}")
                stats["errors"] += 1

    except PlaywrightTimeout:
        logger.warning(f"Timeout loading listing page for {source['name']}")
        stats["errors"] += 1
    except Exception as e:
        logger.warning(f"Source {source['name']} failed: {e}")
        stats["errors"] += 1
    finally:
        try:
            await context.close()
        except Exception:
            pass

    logger.info(
        f"Source {source['name']}: "
        f"{stats['articles_added']} added, "
        f"{stats['skipped']} skipped, "
        f"{stats['errors']} errors, "
        f"{stats['chunks_added']} chunks"
    )
    return stats


# ── Stats helper ───────────────────────────────────────────────────────────────
def get_stats(label: str = "") -> int:
    client = chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False)
    )
    try:
        col   = client.get_collection(COLLECTION_NAME)
        count = col.count()
        logger.info(f"  {COLLECTION_NAME} {label}: {count} chunks")
        return count
    except Exception as e:
        logger.warning(f"  Could not get stats: {e}")
        return 0


# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    print("\n" + "="*60)
    print("AVA Phase 5A Day 3 — Playwright Scraper")
    print("="*60)
    print(f"  ChromaDB path : {CHROMA_PATH}")
    print(f"  Ollama host   : {OLLAMA_HOST}")
    print(f"  Embed model   : {EMBED_MODEL}")
    print(f"  Sources       : {len(PLAYWRIGHT_SOURCES)}")
    print("="*60)

    before = get_stats("BEFORE")

    collection = get_collection()

    all_stats = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        try:
            for source in PLAYWRIGHT_SOURCES:
                try:
                    s = await scrape_source(source, browser, collection)
                    all_stats.append(s)
                except Exception as e:
                    logger.error(f"Unexpected error for {source['name']}: {e}", exc_info=True)
                    all_stats.append({
                        "name": source["name"],
                        "articles_added": 0,
                        "chunks_added": 0,
                        "skipped": 0,
                        "errors": 1,
                    })
        finally:
            await browser.close()

    after = get_stats("AFTER")

    print("\n" + "="*60)
    print("Playwright Scrape Complete")
    print("="*60)
    print(f"  {'Source':<25} {'Added':>6} {'Skipped':>8} {'Errors':>7} {'Chunks':>7}")
    print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*7} {'-'*7}")
    total_chunks = 0
    for s in all_stats:
        print(
            f"  {s['name']:<25} "
            f"{s.get('articles_added', 0):>6} "
            f"{s.get('skipped', 0):>8} "
            f"{s.get('errors', 0):>7} "
            f"{s.get('chunks_added', 0):>7}"
        )
        total_chunks += s.get("chunks_added", 0)
    print(f"  {'-'*25} {'-'*6} {'-'*8} {'-'*7} {'-'*7}")
    print(f"  {'TOTAL':<25} {'':>6} {'':>8} {'':>7} {total_chunks:>7}")
    print(f"\n  devops_blogs_v1: {before} -> {after} chunks (+{after - before})")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
