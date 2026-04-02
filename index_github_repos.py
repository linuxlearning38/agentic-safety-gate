import os
import subprocess
import chromadb
import ollama
import hashlib

# Config
REPOS = [
    "https://github.com/bregman-arie/devops-exercises",
    "https://github.com/iam-veeramalla/aws-devops-zero-to-hero",
    "https://github.com/iam-veeramalla/Jenkins-Zero-To-Hero",
    "https://github.com/iam-veeramalla/Docker-Zero-to-Hero",
    "https://github.com/iam-veeramalla/terraform-zero-to-hero",
]

CLONE_DIR = "/mnt/i/ai-lab/repos"
CHUNK_SIZE = 200
CHUNK_OVERLAP = 50

# ChromaDB
chroma = chromadb.PersistentClient(path="/mnt/i/ai-lab/chromadb")
col = chroma.get_or_create_collection("devops_policies_v2")

print(f"Starting chunks: {col.count()}")

def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i+size])
        if len(chunk.strip()) > 50:
            chunks.append(chunk)
        i += size - overlap
    return chunks

def index_repo(repo_url):
    repo_name = repo_url.split("/")[-1]
    owner = repo_url.split("/")[-2]
    clone_path = os.path.join(CLONE_DIR, repo_name)

    print(f"\n{'='*50}")
    print(f"Processing: {owner}/{repo_name}")

    # Clone or pull
    os.makedirs(CLONE_DIR, exist_ok=True)
    if os.path.exists(clone_path):
        print("Already cloned, pulling latest...")
        subprocess.run(["git", "-C", clone_path, "pull"], capture_output=True)
    else:
        print("Cloning...")
        result = subprocess.run(["git", "clone", "--depth=1", repo_url, clone_path], capture_output=True)
        if result.returncode != 0:
            print(f"Clone failed: {result.stderr.decode()}")
            return

    # Find markdown files
    md_files = []
    for root, dirs, files in os.walk(clone_path):
        # Skip hidden and git folders
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            if f.endswith('.md') or f.endswith('.txt'):
                md_files.append(os.path.join(root, f))

    print(f"Found {len(md_files)} markdown/text files")

    added = 0
    skipped = 0

    for filepath in md_files:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()

            if len(content) < 100:
                continue

            # Create chunks
            chunks = chunk_text(content)
            rel_path = filepath.replace(clone_path, "").lstrip("/")

            for i, chunk in enumerate(chunks):
                # Create unique ID
                chunk_id = f"{owner}_{repo_name}_{hashlib.md5(chunk.encode()).hexdigest()[:12]}"

                # Skip if already exists
                try:
                    existing = col.get(ids=[chunk_id])
                    if existing["documents"]:
                        skipped += 1
                        continue
                except:
                    pass

                # Embed and add
                try:
                    embed = ollama.embeddings(model="nomic-embed-text", prompt=chunk)
                    col.add(
                        ids=[chunk_id],
                        documents=[chunk],
                        embeddings=[embed["embedding"]],
                        metadatas=[{"source": f"{owner}/{repo_name}", "file": rel_path}]
                    )
                    added += 1
                    if added % 50 == 0:
                        print(f"  Added {added} chunks so far...")
                except Exception as e:
                    print(f"  Embed error: {e}")
                    continue

        except Exception as e:
            print(f"  File error {filepath}: {e}")

    print(f"Done: {added} added, {skipped} skipped")

# Run indexing
for repo in REPOS:
    index_repo(repo)

print(f"\nFinal chunks in ChromaDB: {col.count()}")
print("Indexing complete!")
