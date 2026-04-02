import ollama
import chromadb
import os

chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")

# Fresh collection with nomic embeddings
try:
    chroma.delete_collection("devops_policies_v2")
except:
    pass
collection = chroma.get_or_create_collection("devops_policies_v2")

DOCS = {
    "infrastructure.rego":      "/home/manoj85/agentic-api/policies/infrastructure.rego",
    "gatekeeper.py":            "/home/manoj85/agentic-api/gatekeeper.py",
    "secure_apply.sh":          "/home/manoj85/agentic-api/secure_apply.sh",
    "docker-compose.yml":       "/home/manoj85/agentic-api/docker-compose.yml",
    "main.tf":                  "/home/manoj85/agentic-api/terraform-test/main.tf",
    "tests_runner.py":          "/home/manoj85/agentic-api/tests/runner.py",
    "deny_cost_exceeded":       "/home/manoj85/agentic-api/tests/scenarios/deny_cost_exceeded.json",
    "deny_iam_wildcard":        "/home/manoj85/agentic-api/tests/scenarios/deny_iam_wildcard.json",
    "deny_public_ssh":          "/home/manoj85/agentic-api/tests/scenarios/deny_public_ssh.json",
    "deny_unencrypted":         "/home/manoj85/agentic-api/tests/scenarios/deny_unencrypted.json",
    "deny_public_s3":           "/home/manoj85/agentic-api/tests/scenarios/deny_public_s3.json",
    "deny_public_rds":          "/home/manoj85/agentic-api/tests/scenarios/deny_public_rds.json",
    "allow_valid_instance":     "/home/manoj85/agentic-api/tests/scenarios/allow_valid_instance.json",
    "deny_multiple_violations": "/home/manoj85/agentic-api/tests/scenarios/deny_multiple_violations.json",
    "deny_t2_micro":            "/home/manoj85/agentic-api/tests/scenarios/deny_t2_micro.json",
    "deny_missing_tag":         "/home/manoj85/agentic-api/tests/scenarios/deny_missing_tag.json",
    "deny_bad_region":          "/home/manoj85/agentic-api/tests/scenarios/deny_bad_region.json",
}

def chunk_text(text, chunk_size=800, overlap=100):
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

total_chunks = 0

for name, path in DOCS.items():
    try:
        content = open(path).read()
        chunks = chunk_text(content)
        for i, chunk in enumerate(chunks):
            chunk_id = f"{name}_chunk_{i}"
            response = ollama.embeddings(model="nomic-embed-text", prompt=chunk)
            collection.add(
                ids=[chunk_id],
                embeddings=[response["embedding"]],
                documents=[chunk],
                metadatas=[{"source": name, "chunk": i}]
            )
            total_chunks += 1
        print(f"✅ {name} → {len(chunks)} chunks")
    except Exception as e:
        print(f"❌ {name}: {e}")

print(f"\nDone. Total chunks indexed: {total_chunks}")

# ─────────────────────────────────────────
# BLOG SCRAPER — mrcloudbook.com
# ─────────────────────────────────────────
import requests
from bs4 import BeautifulSoup
import time

BLOG_URL = "https://mrcloudbook.com/blog/"

def get_post_links(base_url):
    links = []
    page = 1
    while True:
        url = base_url if page == 1 else f"{base_url}page/{page}/"
        print(f"🔍 Scanning blog page {page}...")
        try:
            res = requests.get(url, timeout=10)
            if res.status_code != 200:
                break
            soup = BeautifulSoup(res.text, "html.parser")
            found = [
                a["href"] for a in soup.find_all("a", href=True)
                if "mrcloudbook.com/" in a["href"]
                and "/blog/" not in a["href"].replace(base_url, "")
                and a["href"] not in links
                and "?" not in a["href"]
            ]
            if not found:
                break
            links.extend(found)
            page += 1
            time.sleep(1)
        except Exception as e:
            print(f"❌ Error on page {page}: {e}")
            break
    return list(set(links))

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
        print(f"❌ Failed {url}: {e}")
        return None

print("\n📡 Starting blog indexing from mrcloudbook.com...")
post_links = get_post_links(BLOG_URL)
print(f"✅ Found {len(post_links)} posts")

blog_chunks = 0
for i, link in enumerate(post_links):
    print(f"[{i+1}/{len(post_links)}] {link}")
    text = scrape_post(link)
    if not text:
        continue
    chunks = chunk_text(text)  # reuse existing chunk_text function
    for j, chunk in enumerate(chunks):
        chunk_id = f"blog_{i}_chunk_{j}"
        try:
            response = ollama.embeddings(model="nomic-embed-text", prompt=chunk)
            collection.add(
                ids=[chunk_id],
                embeddings=[response["embedding"]],
                documents=[chunk],
                metadatas={"source": link, "chunk": j, "type": "blog"}
            )
            blog_chunks += 1
        except Exception as e:
            print(f"  ❌ Embed error: {e}")
    time.sleep(0.5)

print(f"\n🎉 Blog indexing done! {blog_chunks} blog chunks added.")
print(f"📦 Total collection size: {collection.count()} chunks")
