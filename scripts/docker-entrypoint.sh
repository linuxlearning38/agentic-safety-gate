#!/bin/sh
# AVA container entrypoint — runs as user 999 (owner of /data bind mount).
# Ensures /data subdirectories exist and have correct permissions before
# gunicorn starts.  This prevents ChromaDB Rust HNSW EACCES decay that
# occurs when the bind-mount inherits wrong mode bits across container
# restarts on WSL2.
set -e

# All new files/dirs inherit group-write so subsequent processes can share.
umask 002

# Create all directories AVA expects under /data.
mkdir -p \
    /data/chromadb \
    /data/tmp \
    /data/tmp/trivy-cache \
    /data/logs \
    /data/ava_reports \
    /data/reports \
    /data/runtime

# Fix any mode drift: directories get rwxrwxr-x, regular files get rw-rw-r--.
# User 999 is the owner, so chmod works without root.
chmod -R ug+rwX /data

exec "$@"
