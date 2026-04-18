# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim

# Install runtime system deps:
#   curl      → healthcheck + debugging
#   docker.io → docker CLI for mounted /var/run/docker.sock
#   procps    → free/ps monitoring tools
#   kubectl   → kubernetes CLI for pod/node operations
#   trivy     → /scan/trivy endpoint
#   lynis     → /scan/lynis endpoint
#   ca-certs  → TLS verification for external calls
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        wget \
        ca-certificates \
        gnupg \
        apt-transport-https \
        docker.io \
        iproute2 \
        net-tools \
        procps \
        lynis \
    && \
    # Trivy via official apt repo (more reliable than binary download)
    wget -qO - https://aquasecurity.github.io/trivy-repo/deb/public.key \
        | gpg --dearmor > /usr/share/keyrings/trivy.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/trivy.gpg] https://aquasecurity.github.io/trivy-repo/deb generic main" \
        > /etc/apt/sources.list.d/trivy.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends trivy && \
    KUBECTL_VERSION="$(curl -L -s https://dl.k8s.io/release/stable.txt)" && \
    curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    chmod +x /usr/local/bin/kubectl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Non-root user: ava
RUN groupadd -r ava && \
    useradd -r -g ava -d /app -s /sbin/nologin -c "AVA agent user" ava

WORKDIR /app

# Copy installed Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages \
                    /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/gunicorn /usr/local/bin/gunicorn

# Copy application source
# NOTE: certs/, chromadb/, logs/, query_history.json, ava_memory.json
#       are intentionally NOT copied — they are mounted as volumes at runtime.
COPY web_agent_v2.1_guardrail.py .
COPY wsgi.py .
COPY gunicorn.conf.py .
COPY users.json .
COPY control/ ./control/
COPY knowledge_updater/ ./knowledge_updater/

# Create mount-point directories so volume mounts don't land as root-owned
RUN mkdir -p \
        /app/certs \
        /data/chromadb \
        /data/logs \
        /data/history \
    && chown -R ava:ava /app /data

# Switch to non-root
USER ava

EXPOSE 5443

# Health check: Python-based (no reliance on curl being in PATH for ava user)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python3 -c "\
import urllib.request, ssl; \
ctx = ssl.create_default_context(); \
ctx.check_hostname = False; \
ctx.verify_mode = ssl.CERT_NONE; \
urllib.request.urlopen('https://localhost:5443/health', context=ctx, timeout=5)" \
    || exit 1

# Run a single HTTPS Gunicorn process.
# HTTP :5002 remains available via start_ava.sh on bare-metal only.
CMD ["gunicorn", \
     "--config", "/app/gunicorn.conf.py", \
     "--bind",    "0.0.0.0:5443", \
     "--certfile", "/app/certs/ava.crt", \
     "--keyfile",  "/app/certs/ava.key", \
     "wsgi:application"]
