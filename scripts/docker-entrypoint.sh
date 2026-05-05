#!/bin/sh
# AVA container entrypoint — runs as user 999. /data is a Docker named volume
# in v2.0.0+ so ownership is stable across Windows/WSL restarts.
# Ensures /data subdirectories exist and have correct permissions before
# gunicorn starts.  This prevents ChromaDB Rust HNSW EACCES decay without
# requiring manual chown on the Windows/WSL host.
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

# Fix any mode drift within the named volume: directories get rwxrwxr-x,
# regular files get rw-rw-r--. User 999 owns the initialized volume.
chmod -R ug+rwX /data

exec "$@"
