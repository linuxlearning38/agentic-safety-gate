import requests
from bs4 import BeautifulSoup
import ollama
import chromadb
import time

BASE = "https://mrcloudbook.com"
CHROMA_PATH = "/mnt/i/ai-lab/chromadb"
SITEMAP_URL = "https://mrcloudbook.com/sitemap.xml"

# Pages that are NOT blog posts
NOT_POSTS = [
    "/about/", "/contact/", "/privacy/", "/terms/",
    "/cheat-sheets/", "/certifications/", "/projects/",
    "/tools/", "/challenges/", "/write/", "/explore/",
    "/blog/", "/chat/"
]

chroma = chromadb.PersistentClient(path=CHROMA_PATH)
collection = chroma.get_or_create_collection("devops_policies_v2")

def chunk_text(text, chunk_size=800, overlap=100):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

def get_all_post_links():
    print(f"📡 Fetching sitemap: {SITEMAP_URL}")
    res = requests.get(SITEMAP_URL, timeout=10)
    soup = BeautifulSoup(res.text, "xml")
    
    all_urls = [loc.get_text() for loc in soup.find_all("loc")]
    print(f"   Total URLs in sitemap: {len(all_urls)}")
    
    # Filter to only blog posts
    posts = []
    for url in all_urls:
        path = url.replace(BASE, "")
        # Skip non-post pages
        if any(path.startswith(x) for x in NOT_POSTS):
            continue
        # Skip homepage
        if path == "/":
            continue
        # Must look like a blog post slug
        if path.count("/") == 2 and len(path) > 5:
            posts.append(url)
    
    return posts

def scrape_post(url):
    try:
        res = requests.get(url, timeout=10)
        soup = BeautifulSoup(res.text, "html.parser")
        title = soup.find("h1")
        title_text = title.get_text(strip=True) if title else "No title"
        content = (
            soup.find("article") or
            soup.find("div", class_="entry-content") or
            soup.find("main")
        )
        if not content:
            return None
        text = content.get_text(separator="\n", strip=True)
        return f"Title: {title_text}\nSource: {url}\n\n{text}"
    except Exception as e:
        print(f"  ❌ Scrape failed: {e}")
        return None

# ── Already indexed ──
existing = set(collection.get()["ids"])
print(f"📦 Already indexed: {len(existing)} chunks\n")

links = get_all_post_links()
print(f"✅ Blog posts found in sitemap: {len(links)}\n")

# Print all discovered URLs so we can verify
for i, l in enumerate(links):
    print(f"  {i+1}. {l}")

print(f"\n▶️  Starting indexing...\n")

total_chunks = 0
skipped = 0
failed = 0

for i, url in enumerate(links):
    slug = url.replace(BASE + "/", "").strip("/")
    check_id = f"blog_{slug}_chunk_0"

    if check_id in existing:
        print(f"[{i+1}/{len(links)}] ⏭️  Skip: {slug}")
        skipped += 1
        continue

    print(f"[{i+1}/{len(links)}] 📄 {slug}")
    text = scrape_post(url)
    if not text:
        failed += 1
        continue

    chunks = chunk_text(text)
    for j, chunk in enumerate(chunks):
        chunk_id = f"blog_{slug}_chunk_{j}"
        try:
            response = ollama.embeddings(model="nomic-embed-text", prompt=chunk)
            collection.add(
                ids=[chunk_id],
                embeddings=[response["embedding"]],
                documents=[chunk],
                metadatas=[{"source": url, "chunk": j, "type": "blog", "title": slug}]
            )
            total_chunks += 1
        except Exception as e:
            print(f"  ❌ {e}")

    time.sleep(0.3)

print(f"\n🎉 Done!")
print(f"   Posts found       : {len(links)}")
print(f"   New chunks added  : {total_chunks}")
print(f"   Posts skipped     : {skipped} (already indexed)")
print(f"   Posts failed      : {failed}")
print(f"   Total collection  : {collection.count()} chunks")
