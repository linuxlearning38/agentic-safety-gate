"""
AVA Phase 5A — New Collections Ingestor
phase5a_ingestor.py

Creates two new ChromaDB collections:
  - devops_patterns_v1   infrastructure patterns, runbooks, best practices
  - devops_fixes_v1      error fixes, troubleshooting solutions, incident resolutions

ChromaDB path: /home/manoj/ava-data/chroma_db  (env: CHROMA_PATH)
Embedding:     nomic-embed-text via Ollama      (env: OLLAMA_HOST)
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timezone

import chromadb
import requests
from chromadb.config import Settings

try:
    from knowledge_updater.architecture_reference_corpus import ARCHITECTURE_REFERENCE_DOCS
except Exception:
    from architecture_reference_corpus import ARCHITECTURE_REFERENCE_DOCS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("ava.phase5a_ingestor")

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_PATH   = os.getenv("CHROMA_PATH",  "/home/manoj/ava-data/chromadb")
OLLAMA_HOST   = os.getenv("OLLAMA_HOST",  "http://localhost:11434")
EMBED_MODEL   = "nomic-embed-text"

COLLECTION_PATTERNS = "devops_patterns_v1"
COLLECTION_FIXES    = "devops_fixes_v1"
COLLECTION_POLICIES = "devops_policies_v2"
COLLECTION_BLOGS    = "devops_blogs_v1"

# ── Embedding Client (matches ingestor.py exactly) ────────────────────────────
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


# ── ChromaDB Client ───────────────────────────────────────────────────────────
def _get_client() -> chromadb.PersistentClient:
    return chromadb.PersistentClient(
        path=CHROMA_PATH,
        settings=Settings(anonymized_telemetry=False)
    )


# ── Public API ────────────────────────────────────────────────────────────────
def ingest_text_to_collection(collection_name: str, text: str, metadata: dict) -> bool:
    """
    Embed a text chunk and upsert it into the named collection.

    Args:
        collection_name: target collection (e.g. "devops_fixes_v1")
        text:            the text to embed and store
        metadata:        dict of string-valued metadata fields

    Returns:
        True if ingested, False on error.
    """
    embedder = EmbeddingClient(OLLAMA_HOST, EMBED_MODEL)
    client   = _get_client()

    collection = client.get_or_create_collection(
        name=collection_name,
        metadata={"description": f"AVA Phase 5A — {collection_name}"}
    )

    embedding = embedder.embed(text)
    if not embedding:
        logger.error(f"Skipping chunk — embedding failed for collection '{collection_name}'")
        return False

    chunk_hash = hashlib.sha256(text.encode()).hexdigest()
    chunk_id   = f"{collection_name[:8]}_{chunk_hash[:16]}"

    # Ensure all metadata values are strings (ChromaDB requirement)
    safe_meta = {k: str(v) for k, v in metadata.items()}
    safe_meta["ingested_at"] = datetime.now(timezone.utc).isoformat()
    safe_meta["chunk_hash"]  = chunk_hash

    collection.upsert(
        ids=[chunk_id],
        embeddings=[embedding],
        documents=[text],
        metadatas=[safe_meta]
    )
    logger.info(f"Ingested chunk into '{collection_name}' (id: {chunk_id[:24]}...)")
    return True


def get_collection_stats() -> dict:
    """
    Print and return chunk counts for all 4 AVA collections.

    Returns:
        dict with collection names as keys and chunk counts as values.
    """
    client = _get_client()
    stats  = {}

    for name in [COLLECTION_POLICIES, COLLECTION_BLOGS, COLLECTION_PATTERNS, COLLECTION_FIXES]:
        try:
            col          = client.get_or_create_collection(name=name)
            count        = col.count()
            stats[name]  = count
            logger.info(f"  {name:<30} {count:>6} chunks")
        except Exception as e:
            stats[name] = 0
            logger.warning(f"  {name:<30} ERROR — {e}")

    return stats


# ── Seed Data: 20 Real DevOps Fixes ──────────────────────────────────────────
SEED_FIXES = [
    # ── Kubernetes ────────────────────────────────────────────────────────────
    {
        "error": "Pod stuck in CrashLoopBackOff",
        "root_cause": "Container exits immediately due to missing config, bad entrypoint, or OOM. Kubernetes keeps restarting it with exponential back-off.",
        "fix": "kubectl logs <pod> --previous\nkubectl describe pod <pod>\n# Fix the root cause (env var, config map, image tag), then:\nkubectl rollout restart deployment/<name>",
        "prevention": "Add liveness and readiness probes. Use resource limits. Test image locally with docker run before deploying.",
        "tags": ["kubernetes", "pod", "crashloop", "restart"]
    },
    {
        "error": "Pod OOMKilled — exit code 137",
        "root_cause": "Container exceeded its memory limit. Linux kernel OOM killer terminated the process.",
        "fix": "kubectl describe pod <pod>  # confirms OOMKilled\n# Increase memory limit in deployment spec:\nresources:\n  limits:\n    memory: \"512Mi\"   # was 256Mi\n  requests:\n    memory: \"256Mi\"\nkubectl apply -f deployment.yaml",
        "prevention": "Profile memory usage under realistic load before setting limits. Set requests slightly below actual usage, limits 2x requests.",
        "tags": ["kubernetes", "pod", "oom", "memory", "exit137"]
    },
    {
        "error": "ImagePullBackOff or ErrImagePull",
        "root_cause": "Kubelet cannot pull the container image. Causes: wrong image tag, private registry missing imagePullSecret, registry unreachable.",
        "fix": "kubectl describe pod <pod>  # shows exact error\n# Wrong tag:\nkubectl set image deployment/<name> <container>=<image>:<correct-tag>\n# Private registry:\nkubectl create secret docker-registry regcred \\\n  --docker-server=<registry> \\\n  --docker-username=<user> \\\n  --docker-password=<token>\n# Add to pod spec: imagePullSecrets: [{name: regcred}]",
        "prevention": "Tag images with git SHA not 'latest'. Test registry connectivity from node before deploy.",
        "tags": ["kubernetes", "pod", "imagepull", "registry", "secret"]
    },
    {
        "error": "Pod stuck in Pending state — 0/N nodes available",
        "root_cause": "Insufficient CPU/memory on nodes, PVC not bound, node selector/affinity mismatch, or taint without matching toleration.",
        "fix": "kubectl describe pod <pod>  # read Events section\n# Insufficient resources:\nkubectl top nodes\nkubectl scale deployment <name> --replicas=<lower>\n# PVC not bound:\nkubectl get pvc\nkubectl describe pvc <name>\n# Taint mismatch:\nkubectl describe nodes | grep Taint",
        "prevention": "Set resource requests accurately. Use pod disruption budgets. Monitor node capacity.",
        "tags": ["kubernetes", "pod", "pending", "scheduling", "resources", "pvc"]
    },
    {
        "error": "Pod in Evicted state",
        "root_cause": "Node under disk or memory pressure. Kubelet evicts pods to recover resources. Evicted pods remain in kubectl get pods output.",
        "fix": "# Remove all evicted pods in a namespace:\nkubectl get pods -n <namespace> | grep Evicted | awk '{print $1}' | xargs kubectl delete pod -n <namespace>\n# Investigate node pressure:\nkubectl describe nodes | grep -A5 'Conditions'",
        "prevention": "Set imagefs.availableThreshold and memory.availableThreshold in kubelet config. Add node monitoring alerts.",
        "tags": ["kubernetes", "pod", "evicted", "disk-pressure", "memory-pressure"]
    },

    # ── Docker ────────────────────────────────────────────────────────────────
    {
        "error": "Docker container exits immediately with code 1",
        "root_cause": "Entrypoint or CMD fails. Common causes: missing env var, config file not found, wrong working directory, or script syntax error.",
        "fix": "# Run interactively to debug:\ndocker run -it --entrypoint /bin/sh <image>\n# Check logs:\ndocker logs <container-id>\n# Override entrypoint:\ndocker run --entrypoint /bin/bash <image> -c 'env && ls /app'",
        "prevention": "Add HEALTHCHECK to Dockerfile. Test entrypoint locally. Validate required env vars at container startup.",
        "tags": ["docker", "container", "exit", "entrypoint", "cmd"]
    },
    {
        "error": "Docker volume mount: permission denied",
        "root_cause": "Container process runs as non-root UID but host directory is owned by root or different UID. SELinux/AppArmor may also block access.",
        "fix": "# Fix host directory ownership:\nsudo chown -R 1000:1000 /host/path\n# Or use :z flag for SELinux:\ndocker run -v /host/path:/container/path:z <image>\n# Or set user in docker run:\ndocker run --user $(id -u):$(id -g) <image>",
        "prevention": "Explicitly set USER in Dockerfile. Document the expected UID. Use named volumes instead of bind mounts where possible.",
        "tags": ["docker", "volume", "permission", "selinux", "chown"]
    },
    {
        "error": "Docker network: container cannot reach other container",
        "root_cause": "Containers on different networks. Default bridge network isolates containers unless linked. Custom networks are needed for DNS resolution.",
        "fix": "# Create shared network:\ndocker network create app-net\ndocker run --network app-net --name service1 <image1>\ndocker run --network app-net --name service2 <image2>\n# service2 can now reach service1 by hostname 'service1'\n# For compose:\nservices:\n  app:\n    networks: [app-net]\n  db:\n    networks: [app-net]\nnetworks:\n  app-net:",
        "prevention": "Always use custom named networks in production. Never rely on --link (deprecated).",
        "tags": ["docker", "network", "dns", "bridge", "compose"]
    },

    # ── Terraform ─────────────────────────────────────────────────────────────
    {
        "error": "Error acquiring the state lock — ConditionalCheckFailedException",
        "root_cause": "A previous terraform apply/plan was interrupted and left a lock record in DynamoDB. The lock was not released.",
        "fix": "# Show lock info:\nterraform force-unlock -force <LOCK_ID>\n# Get lock ID from the error message or DynamoDB:\naws dynamodb scan --table-name <tf-lock-table> --region <region>\n# Delete the lock item directly if force-unlock fails:\naws dynamodb delete-item --table-name <tf-lock-table> \\\n  --key '{\"LockID\": {\"S\": \"<state-path>\"}}' --region <region>",
        "prevention": "Never kill terraform mid-run. Use CI/CD pipelines with proper locking. Set DynamoDB TTL on lock items as a safety net.",
        "tags": ["terraform", "state-lock", "dynamodb", "force-unlock"]
    },
    {
        "error": "Error: No valid credential sources found for AWS Provider",
        "root_cause": "Terraform AWS provider cannot find credentials. AWS_ACCESS_KEY_ID not set, no ~/.aws/credentials, or assumed role expired.",
        "fix": "# Option 1: Environment variables:\nexport AWS_ACCESS_KEY_ID=<key>\nexport AWS_SECRET_ACCESS_KEY=<secret>\nexport AWS_DEFAULT_REGION=ap-south-1\n# Option 2: AWS profile:\nexport AWS_PROFILE=my-profile\n# Option 3: EC2 instance role (no creds needed if running on EC2)\n# Verify:\naws sts get-caller-identity",
        "prevention": "Use IAM roles for EC2/ECS/Lambda. Store credentials in AWS Secrets Manager or Vault. Rotate access keys every 90 days.",
        "tags": ["terraform", "aws", "credentials", "iam", "provider"]
    },
    {
        "error": "Terraform detects drift — resources changed outside Terraform",
        "root_cause": "Someone modified infrastructure manually (console, CLI, another tool). Terraform state no longer matches real infrastructure.",
        "fix": "# See what drifted:\nterraform plan  # shows what terraform wants to change\n# Option A: Accept drift — import current state:\nterraform import <resource_type>.<name> <resource_id>\n# Option B: Reconcile — let terraform revert to desired state:\nterraform apply\n# Option C: Remove from state if resource was deleted manually:\nterraform state rm <resource_type>.<name>",
        "prevention": "Use Terraform Cloud or Atlantis for all infra changes. Enable AWS Config rules to detect out-of-band changes.",
        "tags": ["terraform", "drift", "state", "import", "apply"]
    },

    # ── Linux ─────────────────────────────────────────────────────────────────
    {
        "error": "No space left on device — disk full",
        "root_cause": "Filesystem at 100%. Common culprits: application logs not rotated, Docker layer accumulation, large core dumps, or full /tmp.",
        "fix": "# Find what's using space:\ndf -h\ndu -sh /* 2>/dev/null | sort -rh | head -20\n# Docker cleanup (if applicable):\ndocker system prune -af --volumes\n# Log cleanup:\njournalctl --vacuum-size=500M\nfind /var/log -name '*.gz' -mtime +7 -delete\n# Find large files:\nfind / -size +500M -type f 2>/dev/null",
        "prevention": "Set up logrotate for all application logs. Monitor disk usage with alerting at 80%/90%. Configure Docker log max-size.",
        "tags": ["linux", "disk", "space", "logs", "docker", "cleanup"]
    },
    {
        "error": "High CPU — system load average above 10, server unresponsive",
        "root_cause": "Runaway process, CPU-bound workload spike, or zombie process accumulation. Load average counts both running and waiting processes.",
        "fix": "# Identify culprit:\ntop -b -n1 | head -20\nps aux --sort=-%cpu | head -10\n# Kill runaway process (get PID from top):\nkill -15 <PID>  # graceful\nkill -9 <PID>   # force if needed\n# Check for I/O wait specifically:\niostat -x 1 5\n# Renice non-critical process:\nrenice +10 -p <PID>",
        "prevention": "Set CPU limits in systemd unit files (CPUQuota=). Use cgroups for containerised workloads. Alert on load > 8 sustained 5 minutes.",
        "tags": ["linux", "cpu", "load", "performance", "kill", "top"]
    },
    {
        "error": "Zombie processes accumulating — defunct in ps output",
        "root_cause": "Child processes that have exited but whose parent has not called wait() to collect exit status. They hold PID slots but no resources.",
        "fix": "# Identify zombies:\nps aux | grep 'Z'\n# Find parent of zombie:\nps -o ppid= -p <zombie_PID>\n# Kill the parent (forces kernel to clean up zombies):\nkill -15 <parent_PID>\n# If parent is critical, send SIGCHLD to force wait():\nkill -SIGCHLD <parent_PID>",
        "prevention": "Ensure applications handle SIGCHLD and call waitpid(). Use a proper init system (tini, dumb-init) in containers.",
        "tags": ["linux", "zombie", "process", "defunct", "sigchld"]
    },
    {
        "error": "Cron job not running — no output, no errors",
        "root_cause": "Common causes: wrong PATH in cron environment, script not executable, syntax error in crontab, or mail delivery failure hiding output.",
        "fix": "# Test crontab syntax:\ncrontab -l\n# Check cron daemon:\nsystemctl status cron  # or crond on RHEL\njournalctl -u cron -n 50\n# Debug: redirect output in crontab:\n* * * * * /path/script.sh >> /tmp/cron.log 2>&1\n# Fix PATH — add to top of crontab:\nPATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n# Fix permissions:\nchmod +x /path/script.sh",
        "prevention": "Always redirect cron output to a log file. Test scripts manually with same env. Use 'cron.d' drop-in files over user crontabs for services.",
        "tags": ["linux", "cron", "crontab", "scheduler", "path"]
    },

    # ── AWS ───────────────────────────────────────────────────────────────────
    {
        "error": "AccessDenied: User is not authorized to perform action on resource",
        "root_cause": "IAM policy does not grant the required permission. Could be missing Allow statement, explicit Deny overriding Allow, or resource ARN mismatch.",
        "fix": "# Check what policy is attached:\naws iam get-user --user-name <user>\naws iam list-attached-user-policies --user-name <user>\naws iam get-policy-version --policy-arn <arn> --version-id v1\n# Use IAM policy simulator to test:\naws iam simulate-principal-policy \\\n  --policy-source-arn <user-arn> \\\n  --action-names <action> \\\n  --resource-arns <resource-arn>\n# Add missing permission to policy, then re-test.",
        "prevention": "Use IAM Access Analyzer. Follow least-privilege — grant only required actions on specific resources. Never use Action: '*'.",
        "tags": ["aws", "iam", "accessdenied", "permissions", "policy"]
    },
    {
        "error": "S3 403 Forbidden when accessing bucket",
        "root_cause": "Multiple possible causes: bucket policy denies access, ACL mismatch, S3 Block Public Access settings, missing KMS key permission, or cross-account role issue.",
        "fix": "# Check bucket policy:\naws s3api get-bucket-policy --bucket <bucket-name>\n# Check block public access settings:\naws s3api get-public-access-block --bucket <bucket-name>\n# Check ACL:\naws s3api get-bucket-acl --bucket <bucket-name>\n# Test with verbose output:\naws s3 ls s3://<bucket>/ --debug 2>&1 | grep 'x-amz-request-id'\n# For cross-account: ensure trust policy allows s3:GetObject on the specific ARN",
        "prevention": "Use bucket policies over ACLs. Enable S3 access logging. Use S3 Access Analyzer to find public or cross-account exposures.",
        "tags": ["aws", "s3", "403", "bucket-policy", "acl", "kms"]
    },
    {
        "error": "EKS node not ready — NotReady status in kubectl get nodes",
        "root_cause": "Node kubelet stopped reporting. Causes: node ran out of disk/memory, kubelet service crashed, CNI plugin failure, or EC2 instance health issue.",
        "fix": "# Check node conditions:\nkubectl describe node <node-name>  # read Conditions section\n# SSH to node (EC2):\naws ec2 describe-instances --filters 'Name=private-dns-name,Values=<node>' \\\n  --query 'Reservations[].Instances[].InstanceId'\n# On the node:\nsystemctl status kubelet\njournalctl -u kubelet -n 100\n# Common fix — restart kubelet:\nsystemctl restart kubelet\n# If CNI issue:\nsystemctl restart aws-node  # or calico-node",
        "prevention": "Enable EC2 auto-recovery. Use managed node groups for automatic replacement. Set up Karpenter for node lifecycle management.",
        "tags": ["aws", "eks", "node", "notready", "kubelet", "cni"]
    },
    {
        "error": "Kubernetes: kubectl command hangs or API server timeout",
        "root_cause": "API server overloaded, etcd latency high, kubeconfig pointing to wrong cluster, or network connectivity issue between kubectl client and API server endpoint.",
        "fix": "# Verify kubeconfig:\nkubectl config current-context\nkubectl config get-contexts\n# Test connectivity:\ncurl -k https://<api-server>:443/healthz\n# Check API server pods (if you have node access):\nkubectl get pods -n kube-system | grep kube-apiserver\n# Check etcd health:\nkubectl exec -n kube-system etcd-<node> -- etcdctl endpoint health\n# Restart API server (kubeadm only):\nkubectl delete pod kube-apiserver-<node> -n kube-system",
        "prevention": "Monitor etcd latency and disk I/O. Use dedicated etcd SSDs. Set resource limits on API server. Enable API priority and fairness.",
        "tags": ["kubernetes", "kubectl", "apiserver", "etcd", "timeout", "kubeconfig"]
    },
    {
        "error": "Terraform: Error creating EC2 instance — InvalidParameterValue subnet not in VPC",
        "root_cause": "Subnet ID hardcoded or read from wrong tfvars. Subnet belongs to a different VPC or region than the security group or instance profile.",
        "fix": "# Verify subnet is in the expected VPC:\naws ec2 describe-subnets --subnet-ids <subnet-id> \\\n  --query 'Subnets[].VpcId'\n# Verify security group VPC:\naws ec2 describe-security-groups --group-ids <sg-id> \\\n  --query 'SecurityGroups[].VpcId'\n# Fix: ensure subnet_id and vpc_security_group_ids reference same VPC\n# Use data sources in Terraform instead of hardcoded IDs:\ndata \"aws_subnet\" \"selected\" { filter { name = \"tag:Name\" values = [\"app-subnet\"] } }",
        "prevention": "Never hardcode AWS resource IDs — use data sources or SSM Parameter Store references. Use terraform validate and plan before apply.",
        "tags": ["terraform", "aws", "ec2", "subnet", "vpc", "security-group"]
    },
]


def _fix_to_text(fix: dict) -> str:
    """Convert a fix dict to a formatted text chunk for embedding."""
    tags_str = ", ".join(fix.get("tags", []))
    return (
        f"ERROR: {fix['error']}\n\n"
        f"ROOT CAUSE: {fix['root_cause']}\n\n"
        f"FIX:\n{fix['fix']}\n\n"
        f"PREVENTION: {fix['prevention']}\n\n"
        f"TAGS: {tags_str}"
    )


# ── Seed Data: 50 Real Infrastructure Patterns ───────────────────────────────
SEED_PATTERNS = [

    # ── Kubernetes (15) ───────────────────────────────────────────────────────
    {
        "name": "Sidecar Container Pattern",
        "category": "kubernetes",
        "description": "Run a helper container alongside the main container in the same Pod, sharing network and storage. Use for logging agents, proxies, config reloaders, or TLS terminators without modifying the main app image.",
        "implementation": "spec:\n  containers:\n  - name: app\n    image: myapp:1.0\n  - name: log-shipper\n    image: fluent-bit:2.0\n    volumeMounts:\n    - name: log-vol\n      mountPath: /var/log\n  volumes:\n  - name: log-vol\n    emptyDir: {}",
        "benefits": "Separation of concerns — app team owns app container, platform team owns sidecar. No app code changes needed to add cross-cutting concerns.",
        "tradeoffs": "Increases pod resource consumption. Sidecar lifecycle is tied to pod — if sidecar crashes, it restarts but app continues.",
        "example": "Istio service mesh injects an Envoy sidecar into every pod automatically for mTLS, observability, and traffic management.",
        "tags": "kubernetes, sidecar, pod-design, logging, proxy"
    },
    {
        "name": "Init Container Pattern",
        "category": "kubernetes",
        "description": "Run one or more containers to completion before the main container starts. Use for database migrations, secret fetching, dependency checks, or config generation.",
        "implementation": "spec:\n  initContainers:\n  - name: wait-for-db\n    image: busybox\n    command: ['sh', '-c', 'until nc -z postgres 5432; do sleep 2; done']\n  - name: run-migrations\n    image: myapp:1.0\n    command: ['python', 'manage.py', 'migrate']\n  containers:\n  - name: app\n    image: myapp:1.0",
        "benefits": "Guarantees prerequisites are met before app starts. Init containers run sequentially — each must succeed before the next runs.",
        "tradeoffs": "Increases pod startup time. Init containers cannot be updated without restarting the pod.",
        "example": "WordPress pod uses an init container to wait for MySQL to be ready before starting Apache.",
        "tags": "kubernetes, init-container, pod-design, migrations, dependency"
    },
    {
        "name": "Resource Requests and Limits Best Practice",
        "category": "kubernetes",
        "description": "Always set CPU requests, memory requests, and memory limits on every container. CPU limits are optional but memory limits are critical — OOMKill is safer than node degradation.",
        "implementation": "resources:\n  requests:\n    cpu: '100m'\n    memory: '128Mi'\n  limits:\n    memory: '256Mi'\n    # CPU limit intentionally omitted — causes throttling\n    # Set only if you need strict isolation\n# Rule: limits.memory = 2x requests.memory\n# Rule: requests.cpu = measured P99 usage under load",
        "benefits": "Scheduler can make intelligent placement decisions. Prevents noisy-neighbour memory issues. Enables VPA recommendations.",
        "tradeoffs": "Under-provisioning requests causes evictions. Over-provisioning wastes cluster capacity. CPU limits cause throttling even when node has spare CPU.",
        "example": "Set requests based on actual profiling with kubectl top pods. Never use 0 requests — it makes the pod Burstable class and first to be evicted.",
        "tags": "kubernetes, resources, limits, requests, oom, scheduling, best-practice"
    },
    {
        "name": "Pod Disruption Budget (PDB)",
        "category": "kubernetes",
        "description": "Define the minimum number of pods that must remain available during voluntary disruptions (node drains, upgrades). Essential for HA — prevents all replicas being evicted simultaneously.",
        "implementation": "apiVersion: policy/v1\nkind: PodDisruptionBudget\nmetadata:\n  name: api-pdb\nspec:\n  minAvailable: 2    # OR maxUnavailable: 1\n  selector:\n    matchLabels:\n      app: api\n# Apply before any cluster upgrade or node maintenance",
        "benefits": "Cluster upgrades and node drains respect PDB. Prevents service outage during maintenance windows.",
        "tradeoffs": "Too strict PDB (minAvailable = all replicas) blocks node drains indefinitely. Must be paired with enough replicas to satisfy the budget.",
        "example": "A 3-replica API with minAvailable: 2 — node drain will only proceed when 2 pods are healthy elsewhere.",
        "tags": "kubernetes, pdb, availability, disruption, maintenance, ha"
    },
    {
        "name": "Horizontal Pod Autoscaler (HPA) Setup",
        "category": "kubernetes",
        "description": "Automatically scale Deployment replicas based on CPU, memory, or custom metrics. Configure with realistic min/max bounds and a proper stabilization window to prevent flapping.",
        "implementation": "apiVersion: autoscaling/v2\nkind: HorizontalPodAutoscaler\nmetadata:\n  name: api-hpa\nspec:\n  scaleTargetRef:\n    apiVersion: apps/v1\n    kind: Deployment\n    name: api\n  minReplicas: 2\n  maxReplicas: 20\n  metrics:\n  - type: Resource\n    resource:\n      name: cpu\n      target:\n        type: Utilization\n        averageUtilization: 70\n  behavior:\n    scaleDown:\n      stabilizationWindowSeconds: 300",
        "benefits": "Handles traffic spikes automatically. Reduces cost during low-traffic periods by scaling down.",
        "tradeoffs": "HPA requires metrics-server. Custom metrics need Prometheus adapter. Scale-up latency (pod startup time) means HPA is reactive not proactive.",
        "example": "Set minReplicas: 2 always — never 1 — so one pod restart doesn't cause downtime.",
        "tags": "kubernetes, hpa, autoscaling, scaling, cpu, metrics"
    },
    {
        "name": "ConfigMap vs Secret Decision Pattern",
        "category": "kubernetes",
        "description": "Use ConfigMap for non-sensitive configuration (feature flags, URLs, log levels). Use Secret for credentials, tokens, keys. Never store secrets in ConfigMaps or environment variables in plain-text Dockerfiles.",
        "implementation": "# ConfigMap — non-sensitive:\nkubectl create configmap app-config \\\n  --from-literal=LOG_LEVEL=info \\\n  --from-literal=DB_HOST=postgres\n\n# Secret — sensitive:\nkubectl create secret generic db-creds \\\n  --from-literal=DB_PASSWORD=mypassword\n\n# Mount as env vars:\nenvFrom:\n- configMapRef:\n    name: app-config\n- secretRef:\n    name: db-creds\n\n# Better: mount as files (avoids env var exposure):\nvolumeMounts:\n- name: secrets\n  mountPath: /secrets\n  readOnly: true",
        "benefits": "Clear separation of config vs secrets. Secrets can be managed with external tools (Vault, AWS Secrets Manager, ESO).",
        "tradeoffs": "Kubernetes Secrets are only base64-encoded by default — not encrypted at rest unless etcd encryption is enabled or you use ESO.",
        "example": "Use External Secrets Operator to sync AWS Secrets Manager secrets into Kubernetes Secrets automatically.",
        "tags": "kubernetes, configmap, secret, configuration, security, eso"
    },
    {
        "name": "Probe Design: Liveness vs Readiness vs Startup",
        "category": "kubernetes",
        "description": "Liveness: restart the container if unhealthy. Readiness: remove from Service endpoints if not ready. Startup: delay liveness checks for slow-starting containers. All three serve different purposes — misconfiguring causes cascading failures.",
        "implementation": "livenessProbe:\n  httpGet:\n    path: /healthz\n    port: 8080\n  initialDelaySeconds: 30\n  periodSeconds: 10\n  failureThreshold: 3\nreadinessProbe:\n  httpGet:\n    path: /ready\n    port: 8080\n  periodSeconds: 5\n  failureThreshold: 2\nstartupProbe:\n  httpGet:\n    path: /healthz\n    port: 8080\n  failureThreshold: 30   # 30 * 10s = 5 min max startup\n  periodSeconds: 10",
        "benefits": "Automatic self-healing. Traffic only reaches healthy pods. Slow apps aren't killed during startup.",
        "tradeoffs": "Liveness probe that checks external dependencies (DB) causes cascading restarts. Liveness should check only internal health. Readiness checks dependencies.",
        "example": "Never check DB connectivity in liveness — if DB is down, all pods restart simultaneously, making recovery impossible.",
        "tags": "kubernetes, probe, liveness, readiness, startup, health-check"
    },
    {
        "name": "Namespace Isolation Strategy",
        "category": "kubernetes",
        "description": "Organise workloads into namespaces by team, environment, or risk tier. Combine with ResourceQuotas, LimitRanges, and NetworkPolicies for true isolation.",
        "implementation": "# Create namespace with labels:\nkubectl create namespace payments\nkubectl label namespace payments team=fintech env=prod tier=critical\n\n# ResourceQuota per namespace:\napiVersion: v1\nkind: ResourceQuota\nmetadata:\n  name: payments-quota\n  namespace: payments\nspec:\n  hard:\n    requests.cpu: '10'\n    requests.memory: 20Gi\n    pods: '50'\n\n# LimitRange for defaults:\napiVersion: v1\nkind: LimitRange\nmetadata:\n  name: payments-limits\n  namespace: payments\nspec:\n  limits:\n  - default:\n      memory: 256Mi\n      cpu: 200m\n    defaultRequest:\n      memory: 128Mi\n      cpu: 100m\n    type: Container",
        "benefits": "Cost allocation per team. Blast radius containment. RBAC can be scoped to namespace.",
        "tradeoffs": "Namespaces do not provide kernel-level isolation — a compromised container can still escape. For hard multi-tenancy, use separate clusters.",
        "example": "Use namespaces: default (dev), staging, production. Each has separate RBAC, quotas, and network policies.",
        "tags": "kubernetes, namespace, isolation, quota, limitrange, multi-tenancy"
    },
    {
        "name": "RBAC Least Privilege Pattern",
        "category": "kubernetes",
        "description": "Grant only the permissions actually needed — specific verbs on specific resources in specific namespaces. Use ServiceAccounts for pods, not the default SA. Audit regularly.",
        "implementation": "# ServiceAccount for app:\napiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: api-sa\n  namespace: production\n---\napiVersion: rbac.authorization.k8s.io/v1\nkind: Role\nmetadata:\n  name: api-role\n  namespace: production\nrules:\n- apiGroups: ['']\n  resources: [configmaps]\n  verbs: [get, list]\n---\napiVersion: rbac.authorization.k8s.io/v1\nkind: RoleBinding\nmetadata:\n  name: api-rolebinding\n  namespace: production\nsubjects:\n- kind: ServiceAccount\n  name: api-sa\nroleRef:\n  kind: Role\n  name: api-role\n  apiGroup: rbac.authorization.k8s.io",
        "benefits": "Limits blast radius of compromised pods. Satisfies compliance audits (SOC2, PCI-DSS).",
        "tradeoffs": "Fine-grained RBAC requires maintenance. Use kubectl auth can-i to verify permissions.",
        "example": "A pod that only reads ConfigMaps should have get+list on configmaps only — not cluster-admin.",
        "tags": "kubernetes, rbac, security, serviceaccount, least-privilege, compliance"
    },
    {
        "name": "Pod Anti-Affinity for High Availability",
        "category": "kubernetes",
        "description": "Use pod anti-affinity to spread replicas across nodes or availability zones. Prevents all replicas landing on one node and being lost in a single node failure.",
        "implementation": "affinity:\n  podAntiAffinity:\n    requiredDuringSchedulingIgnoredDuringExecution:\n    - labelSelector:\n        matchExpressions:\n        - key: app\n          operator: In\n          values: [api]\n      topologyKey: kubernetes.io/hostname\n# For zone-level spread:\n  podAntiAffinity:\n    preferredDuringSchedulingIgnoredDuringExecution:\n    - weight: 100\n      podAffinityTerm:\n        labelSelector:\n          matchLabels:\n            app: api\n        topologyKey: topology.kubernetes.io/zone",
        "benefits": "Survives single node or AZ failures without downtime. Essential for any production service.",
        "tradeoffs": "required anti-affinity can block scheduling if not enough nodes. Use preferred for flexibility with best-effort spreading.",
        "example": "3-replica deployment with requiredDuringScheduling anti-affinity — each replica guaranteed on different node.",
        "tags": "kubernetes, affinity, anti-affinity, ha, availability, zone, topology"
    },
    {
        "name": "DaemonSet vs Deployment Decision",
        "category": "kubernetes",
        "description": "Use DaemonSet when you need exactly one pod per node (log collectors, monitoring agents, CNI plugins, node-level storage). Use Deployment when you need N replicas distributed across nodes.",
        "implementation": "# DaemonSet — runs on every node:\napiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: node-exporter\nspec:\n  selector:\n    matchLabels:\n      app: node-exporter\n  template:\n    spec:\n      hostPID: true\n      hostNetwork: true\n      containers:\n      - name: node-exporter\n        image: prom/node-exporter:v1.7\n        ports:\n        - containerPort: 9100\n          hostPort: 9100",
        "benefits": "DaemonSet automatically runs on new nodes as they join. No need to scale manually.",
        "tradeoffs": "DaemonSet pods consume resources on every node — use tolerations carefully to avoid running on control plane nodes.",
        "example": "Fluentd log shipper as DaemonSet ensures logs from every node are collected. Prometheus node-exporter as DaemonSet collects node metrics.",
        "tags": "kubernetes, daemonset, deployment, logging, monitoring, node-agent"
    },
    {
        "name": "StatefulSet for Stateful Workloads",
        "category": "kubernetes",
        "description": "Use StatefulSet for databases, message queues, and any workload needing stable network identity and persistent storage. Each pod gets a stable hostname (pod-0, pod-1) and its own PVC.",
        "implementation": "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: postgres\nspec:\n  serviceName: postgres-headless\n  replicas: 3\n  selector:\n    matchLabels:\n      app: postgres\n  template:\n    spec:\n      containers:\n      - name: postgres\n        image: postgres:16\n        volumeMounts:\n        - name: data\n          mountPath: /var/lib/postgresql/data\n  volumeClaimTemplates:\n  - metadata:\n      name: data\n    spec:\n      accessModes: [ReadWriteOnce]\n      resources:\n        requests:\n          storage: 100Gi",
        "benefits": "Ordered startup/shutdown. Stable DNS names (pod-0.service, pod-1.service). Individual PVC per pod survives pod restarts.",
        "tradeoffs": "Scaling down does NOT delete PVCs — must be done manually. Ordered rolling updates are slower than Deployments.",
        "example": "PostgreSQL primary (pod-0) and replicas (pod-1, pod-2) with stable hostnames for replication config.",
        "tags": "kubernetes, statefulset, database, pvc, persistence, postgres"
    },
    {
        "name": "NetworkPolicy Zero-Trust Pattern",
        "category": "kubernetes",
        "description": "Deny all ingress and egress by default, then explicitly allow only required traffic. Implements zero-trust networking within the cluster.",
        "implementation": "# Step 1: Default deny all:\napiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: default-deny-all\n  namespace: production\nspec:\n  podSelector: {}\n  policyTypes: [Ingress, Egress]\n\n# Step 2: Allow specific traffic:\napiVersion: networking.k8s.io/v1\nkind: NetworkPolicy\nmetadata:\n  name: allow-api-to-db\n  namespace: production\nspec:\n  podSelector:\n    matchLabels:\n      app: postgres\n  ingress:\n  - from:\n    - podSelector:\n        matchLabels:\n          app: api\n    ports:\n    - port: 5432",
        "benefits": "Prevents lateral movement if one pod is compromised. Required for PCI-DSS and SOC2 compliance.",
        "tradeoffs": "Requires CNI plugin that supports NetworkPolicy (Calico, Cilium, Weave). Flannel does NOT support it.",
        "example": "Database pods accept connections only from API pods. API pods accept only from ingress controller. Nothing else can connect.",
        "tags": "kubernetes, networkpolicy, zero-trust, security, isolation, calico, cilium"
    },
    {
        "name": "Ingress Controller Patterns",
        "category": "kubernetes",
        "description": "Use a single Ingress controller (nginx-ingress or Traefik) as the cluster entry point. Configure TLS termination, path routing, rate limiting, and auth at the Ingress layer.",
        "implementation": "apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: api-ingress\n  annotations:\n    nginx.ingress.kubernetes.io/ssl-redirect: 'true'\n    nginx.ingress.kubernetes.io/rate-limit: '100'\n    cert-manager.io/cluster-issuer: letsencrypt-prod\nspec:\n  ingressClassName: nginx\n  tls:\n  - hosts: [api.example.com]\n    secretName: api-tls\n  rules:\n  - host: api.example.com\n    http:\n      paths:\n      - path: /v1\n        pathType: Prefix\n        backend:\n          service:\n            name: api-service\n            port:\n              number: 80",
        "benefits": "Single point for TLS, auth, rate limiting. Reduces complexity in individual services.",
        "tradeoffs": "Ingress controller is a single point of failure — run with 2+ replicas and PDB. Different controllers have different annotation formats.",
        "example": "nginx-ingress with cert-manager handles automatic TLS renewal from Let's Encrypt for all services.",
        "tags": "kubernetes, ingress, nginx, tls, routing, cert-manager, load-balancer"
    },
    {
        "name": "Multi-Container Pod Design Principles",
        "category": "kubernetes",
        "description": "Pods should contain tightly-coupled containers that must share lifecycle, network, and storage. Avoid putting loosely-coupled services in the same pod — use separate Deployments instead.",
        "implementation": "# CORRECT: App + log shipper (share log volume)\n# CORRECT: App + config reloader (share config volume)\n# CORRECT: App + Envoy proxy (share network namespace)\n\n# WRONG: Frontend + Backend in same pod\n# WRONG: App + Database in same pod\n# WRONG: Microservice A + Microservice B in same pod\n\n# Decision rule:\n# Same pod IF: must scale together, must share localhost, must share files\n# Separate pods IF: different scaling needs, different teams, different failure modes",
        "benefits": "Correct pod boundaries reduce coupling, simplify debugging, and enable independent scaling.",
        "tradeoffs": "Over-splitting into too many pods increases network hops and latency for tightly-coupled components.",
        "example": "Istio sidecar (Envoy) must be in the same pod as the app to intercept localhost traffic — valid use of multi-container pod.",
        "tags": "kubernetes, pod-design, multi-container, sidecar, coupling, architecture"
    },

    # ── CI/CD Patterns (10) ───────────────────────────────────────────────────
    {
        "name": "GitOps with ArgoCD Pattern",
        "category": "cicd",
        "description": "Store all Kubernetes manifests in Git. ArgoCD watches the Git repo and automatically syncs the cluster state to match. Git is the single source of truth — no kubectl apply in CI pipelines.",
        "implementation": "# 1. Install ArgoCD:\nkubectl create namespace argocd\nkubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml\n\n# 2. Create Application:\napiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: my-app\n  namespace: argocd\nspec:\n  project: default\n  source:\n    repoURL: https://github.com/org/k8s-configs\n    targetRevision: main\n    path: apps/my-app\n  destination:\n    server: https://kubernetes.default.svc\n    namespace: production\n  syncPolicy:\n    automated:\n      prune: true\n      selfHeal: true",
        "benefits": "Full audit trail in Git. Rollback = git revert. Drift detection built-in. No cluster credentials in CI.",
        "tradeoffs": "Learning curve. Secrets management needs separate solution (Sealed Secrets or ESO). ArgoCD itself needs HA setup.",
        "example": "PR to k8s-configs repo triggers ArgoCD sync within 3 minutes. Rollback any deployment by reverting the PR.",
        "tags": "cicd, gitops, argocd, kubernetes, deployment, git"
    },
    {
        "name": "Blue-Green Deployment Pattern",
        "category": "cicd",
        "description": "Maintain two identical production environments (blue=current, green=new). Deploy to green, test it, then switch traffic. Instant rollback by switching back to blue.",
        "implementation": "# Two Deployments, one Service:\n# Blue (current production):\nkubectl create deployment app-blue --image=myapp:v1\n# Green (new version):\nkubectl create deployment app-green --image=myapp:v2\n\n# Service points to blue:\napiVersion: v1\nkind: Service\nmetadata:\n  name: app-service\nspec:\n  selector:\n    deployment: app-blue   # Switch to app-green to cut over\n  ports:\n  - port: 80\n\n# After testing green: patch the service selector:\nkubectl patch service app-service -p '{\"spec\":{\"selector\":{\"deployment\":\"app-green\"}}}'",
        "benefits": "Zero-downtime deployments. Instant rollback. Green is tested with production traffic before blue is decommissioned.",
        "tradeoffs": "Requires 2x compute during cutover. Database schema changes must be backward-compatible with both versions simultaneously.",
        "example": "AWS ALB weighted target groups: 100% to blue, shift to 100% green after smoke tests pass.",
        "tags": "cicd, blue-green, deployment, zero-downtime, rollback"
    },
    {
        "name": "Canary Deployment Pattern",
        "category": "cicd",
        "description": "Route a small percentage of traffic to the new version while the majority continues on the old version. Gradually increase traffic as confidence grows. Catches regressions before full rollout.",
        "implementation": "# Using nginx-ingress canary annotations:\napiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: app-canary\n  annotations:\n    nginx.ingress.kubernetes.io/canary: 'true'\n    nginx.ingress.kubernetes.io/canary-weight: '10'  # 10% traffic\nspec:\n  rules:\n  - host: app.example.com\n    http:\n      paths:\n      - path: /\n        pathType: Prefix\n        backend:\n          service:\n            name: app-v2-service\n            port:\n              number: 80\n# Increment canary-weight: 10 -> 25 -> 50 -> 100",
        "benefits": "Real production traffic validates new version. Easy rollback — set canary-weight to 0. Flagger can automate the progression based on metrics.",
        "tradeoffs": "Both versions must handle the same data schema simultaneously. Error rates from 10% traffic may have wide confidence intervals.",
        "example": "Flagger with Prometheus: auto-promote canary if error rate < 1% and p99 latency < 500ms for 5 minutes.",
        "tags": "cicd, canary, deployment, traffic-splitting, flagger, progressive-delivery"
    },
    {
        "name": "Feature Flag Pattern",
        "category": "cicd",
        "description": "Decouple feature release from code deployment. Deploy code with features disabled, enable them per user/percentage/environment without redeployment. Enables dark launches and A/B testing.",
        "implementation": "# Using LaunchDarkly or Unleash (self-hosted):\n# Code:\nif feature_flags.is_enabled('new-checkout-flow', user_id):\n    return new_checkout()\nreturn old_checkout()\n\n# Unleash Docker setup:\ndocker run -d --name unleash \\\n  -e DATABASE_URL=postgres://... \\\n  -p 4242:4242 \\\n  unleashorg/unleash-server\n\n# Toggle via API:\ncurl -X POST http://unleash:4242/api/admin/features/new-checkout-flow/toggleOn \\\n  -H 'Authorization: Bearer <token>'",
        "benefits": "Deploy any time without releasing. Instant kill switch for bad features. Test with real users before full rollout.",
        "tradeoffs": "Technical debt — old code paths accumulate. Flags must be cleaned up after full rollout. Testing matrix grows with each flag.",
        "example": "Netflix uses feature flags to enable new algorithms for 1% of users, measure engagement, then roll out globally.",
        "tags": "cicd, feature-flag, dark-launch, ab-testing, unleash, launchdarkly"
    },
    {
        "name": "Pipeline as Code Best Practices",
        "category": "cicd",
        "description": "Define CI/CD pipelines in version-controlled files (Jenkinsfile, .github/workflows, .gitlab-ci.yml). Never configure pipelines through UI — it creates undocumented configuration drift.",
        "implementation": "# GitHub Actions example:\nname: Deploy\non:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n    - uses: actions/checkout@v4\n    - run: make test\n  build:\n    needs: test\n    steps:\n    - name: Build and push\n      run: |\n        docker build -t $IMAGE:${{ github.sha }} .\n        docker push $IMAGE:${{ github.sha }}\n  deploy:\n    needs: build\n    environment: production  # requires approval\n    steps:\n    - run: kubectl set image deployment/app app=$IMAGE:${{ github.sha }}",
        "benefits": "Pipeline changes are reviewed via PR. History in git. Reproducible — any team member can understand the pipeline.",
        "tradeoffs": "YAML pipelines can become complex. Secrets management in CI requires careful handling. Cross-repo dependencies are hard.",
        "example": "Tag every Docker image with the Git SHA — never with 'latest'. This makes deployments fully traceable to a commit.",
        "tags": "cicd, pipeline, github-actions, jenkins, gitlab-ci, automation"
    },
    {
        "name": "Trunk-Based Development",
        "category": "cicd",
        "description": "All developers commit to a single main branch (trunk) at least daily. Short-lived feature branches (< 2 days). Feature flags gate incomplete work. Enables continuous integration.",
        "implementation": "# Branch strategy:\n# main (trunk) — always deployable\n# feature/xxx — max 1-2 days, small PRs\n# No long-lived feature branches\n# No environment branches (dev/staging/prod)\n\n# Git workflow:\ngit checkout -b feature/add-auth\n# ... make small, focused changes ...\ngit commit -m 'add JWT validation'\ngit push && gh pr create\n# PR merged same day\n\n# Gating in-progress work:\nif (featureFlags.enabled('new-auth')):\n    use_new_auth()\nelse:\n    use_old_auth()",
        "benefits": "Eliminates merge hell from long-lived branches. Continuous integration is actually continuous. Smaller PRs are easier to review.",
        "tradeoffs": "Requires feature flags for incomplete work. Team discipline — no sneaking big changes in small commits.",
        "example": "Google has thousands of engineers committing to a single monorepo trunk daily, enabled by feature flags and automated testing.",
        "tags": "cicd, trunk-based-development, branching, git, continuous-integration"
    },
    {
        "name": "Semantic Versioning for Releases",
        "category": "cicd",
        "description": "Version software as MAJOR.MINOR.PATCH. MAJOR = breaking change, MINOR = backward-compatible feature, PATCH = backward-compatible bug fix. Apply to Docker images, Helm charts, Terraform modules, and APIs.",
        "implementation": "# Conventional commits drive semver:\n# feat: new feature -> MINOR bump (1.2.0 -> 1.3.0)\n# fix: bug fix -> PATCH bump (1.3.0 -> 1.3.1)\n# feat!: or BREAKING CHANGE: -> MAJOR bump (1.3.1 -> 2.0.0)\n\n# GitHub Actions with semantic-release:\n- uses: cycjimmy/semantic-release-action@v4\n  env:\n    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n# Automatically creates git tags, GitHub releases, CHANGELOG\n\n# Docker image tagging:\ndocker tag myapp:latest myapp:2.3.1\ndocker tag myapp:latest myapp:2.3\ndocker tag myapp:latest myapp:2",
        "benefits": "Clear contract with consumers. Automated changelogs. Dependency managers (Helm, pip, npm) can use version ranges.",
        "tradeoffs": "Requires commit message discipline. Teams must agree on what constitutes a breaking change.",
        "example": "Helm chart versioning: appVersion tracks app version, chart version tracks chart changes independently.",
        "tags": "cicd, semver, versioning, release, conventional-commits, changelog"
    },
    {
        "name": "Docker Multi-Stage Build Pattern",
        "category": "cicd",
        "description": "Use multiple FROM stages in Dockerfile to separate build dependencies from runtime. Final image contains only the binary and runtime — no build tools, compilers, or source code.",
        "implementation": "# Multi-stage Dockerfile:\nFROM golang:1.22 AS builder\nWORKDIR /src\nCOPY go.mod go.sum ./\nRUN go mod download\nCOPY . .\nRUN CGO_ENABLED=0 go build -o /app ./cmd/server\n\n# Final minimal image:\nFROM gcr.io/distroless/static:nonroot\nCOPY --from=builder /app /app\nUSER nonroot\nEXPOSE 8080\nENTRYPOINT [\"/app\"]\n\n# Result: Go app from ~1GB builder -> 10MB final image",
        "benefits": "Dramatically smaller images (10x-100x). Smaller attack surface — no compiler or source in production. Faster pulls.",
        "tradeoffs": "Build cache invalidation requires care — copy dependency files before source code. Debugging distroless images is harder.",
        "example": "Java Spring Boot: builder uses maven:3.9-jdk21, final uses eclipse-temurin:21-jre-alpine. Image: 800MB -> 180MB.",
        "tags": "docker, multi-stage-build, image-optimization, distroless, security"
    },
    {
        "name": "Immutable Infrastructure Pattern",
        "category": "cicd",
        "description": "Never modify running infrastructure. Instead, build a new image/AMI/container with changes and replace the old one. Treat servers like cattle, not pets.",
        "implementation": "# Wrong (mutable):\nssh prod-server\napt install new-package\n# Server is now different from what's in code\n\n# Right (immutable):\n# 1. Change Dockerfile or Packer template\n# 2. Build new image: docker build -t app:v2 .\n# 3. Push: docker push app:v2\n# 4. Deploy: kubectl set image deployment/app app=app:v2\n# 5. Old pods terminated, new pods created\n# The fleet is uniform — every instance identical\n\n# With Packer for AMIs:\npacker build -var 'ami_version=v1.2.3' template.pkr.hcl\n# New AMI ID used in Terraform: ami = data.packer.latest.id",
        "benefits": "No configuration drift. Rollback is trivial — deploy previous image. All instances identical — no snowflake servers.",
        "tradeoffs": "Requires fast image build pipeline. Stateful data must be in external storage (S3, RDS) not on instance.",
        "example": "Netflix Bake/Deploy: every deployment creates a new AMI from base image + app code. No SSH in production.",
        "tags": "cicd, immutable-infrastructure, packer, docker, cattle-not-pets, ami"
    },
    {
        "name": "Rolling Deployment Zero-Downtime",
        "category": "cicd",
        "description": "Replace pods one at a time during updates, keeping the service available throughout. Configure maxUnavailable and maxSurge to control the rollout speed and risk.",
        "implementation": "spec:\n  strategy:\n    type: RollingUpdate\n    rollingUpdate:\n      maxUnavailable: 0    # Never reduce capacity below desired\n      maxSurge: 1          # Add 1 extra pod during update\n  # With 3 replicas:\n  # Step 1: Start new pod (4 total, 3 old + 1 new)\n  # Step 2: New pod healthy -> kill 1 old (3 total, 2 old + 1 new)\n  # Step 3: Repeat until all new\n\n# Also needed for zero-downtime:\n# 1. Readiness probe — new pod only gets traffic when ready\n# 2. preStop hook + sleep — drain connections before termination:\nlifecycle:\n  preStop:\n    exec:\n      command: ['/bin/sh', '-c', 'sleep 5']",
        "benefits": "Zero-downtime by default in Kubernetes. No extra infrastructure needed unlike blue-green.",
        "tradeoffs": "Old and new versions run simultaneously — APIs must be backward-compatible. Slower than blue-green for large deployments.",
        "example": "maxUnavailable: 0 + maxSurge: 25% for a 100-pod deployment rolls 25 pods at a time, ~4 waves.",
        "tags": "kubernetes, rolling-update, zero-downtime, deployment, strategy"
    },

    # ── AWS Patterns (10) ─────────────────────────────────────────────────────
    {
        "name": "VPC 3-Tier Architecture",
        "category": "aws",
        "description": "Design VPC with public, private-app, and private-data subnets across multiple AZs. Internet-facing load balancers in public subnets. App servers in private-app. Databases in private-data. No direct internet access for apps or data.",
        "implementation": "# Terraform structure:\nmodule vpc {\n  # Public subnets (per AZ): ALB, NAT Gateway, Bastion\n  public_subnets  = [\"10.0.1.0/24\", \"10.0.2.0/24\", \"10.0.3.0/24\"]\n  # Private app subnets: EC2, EKS nodes, Lambda\n  private_subnets = [\"10.0.11.0/24\", \"10.0.12.0/24\", \"10.0.13.0/24\"]\n  # Private data subnets: RDS, ElastiCache, Redshift\n  data_subnets    = [\"10.0.21.0/24\", \"10.0.22.0/24\", \"10.0.23.0/24\"]\n  # NAT Gateway per AZ for HA (not single NAT Gateway)\n  enable_nat_gateway     = true\n  single_nat_gateway     = false  # one per AZ\n  one_nat_gateway_per_az = true\n}",
        "benefits": "Defense in depth. Database never exposed to internet. App servers not directly reachable. Meets compliance requirements.",
        "tradeoffs": "NAT Gateway per AZ costs ~$32/month each. Total VPC design adds complexity but is essential for production.",
        "example": "EKS cluster with worker nodes in private-app subnets, ALB in public subnet, RDS in private-data subnet with no route to internet.",
        "tags": "aws, vpc, networking, 3-tier, subnets, nat-gateway, architecture"
    },
    {
        "name": "IAM Least Privilege Role Pattern",
        "category": "aws",
        "description": "Create specific IAM roles per service with only required permissions. Use IAM Roles for Service Accounts (IRSA) for EKS pods. Never use access keys on EC2 — use instance profiles.",
        "implementation": "# EC2 instance profile (not access keys):\nresource aws_iam_role ec2_role {\n  assume_role_policy = jsonencode({\n    Statement = [{\n      Action    = \"sts:AssumeRole\"\n      Effect    = \"Allow\"\n      Principal = { Service = \"ec2.amazonaws.com\" }\n    }]\n  })\n}\nresource aws_iam_policy app_policy {\n  policy = jsonencode({\n    Statement = [{\n      Effect   = \"Allow\"\n      Action   = [\"s3:GetObject\", \"s3:PutObject\"]\n      Resource = \"arn:aws:s3:::my-bucket/*\"\n    }]\n  })\n}\n# EKS IRSA:\nannotations:\n  eks.amazonaws.com/role-arn: arn:aws:iam::123:role/app-role",
        "benefits": "Credentials rotate automatically. No long-lived access keys to leak. CloudTrail audits show exactly which role did what.",
        "tradeoffs": "IRSA setup requires cluster OIDC provider configuration. Cross-account access needs trust policies.",
        "example": "App pod on EKS reads from S3 using IRSA — no credentials in environment variables or Kubernetes Secrets.",
        "tags": "aws, iam, least-privilege, irsa, eks, instance-profile, security"
    },
    {
        "name": "S3 Lifecycle Policy Pattern",
        "category": "aws",
        "description": "Automatically transition objects to cheaper storage tiers and expire old objects. Move logs to Glacier after 30 days, delete after 365 days. Dramatically reduces S3 costs.",
        "implementation": "resource aws_s3_bucket_lifecycle_configuration lifecycle {\n  bucket = aws_s3_bucket.logs.id\n  rule {\n    id     = \"log-lifecycle\"\n    status = \"Enabled\"\n    transition {\n      days          = 30\n      storage_class = \"STANDARD_IA\"   # 30-90 days\n    }\n    transition {\n      days          = 90\n      storage_class = \"GLACIER\"        # 90-365 days\n    }\n    expiration {\n      days = 365                       # Delete after 1 year\n    }\n    noncurrent_version_expiration {\n      noncurrent_days = 30             # Clean old versions\n    }\n  }\n}",
        "benefits": "S3 Standard: $0.023/GB. Glacier: $0.004/GB. 80%+ cost reduction for old logs. Automatic — zero maintenance.",
        "tradeoffs": "Glacier retrieval takes minutes to hours. Not suitable for data that needs immediate access. Check compliance requirements before deleting.",
        "example": "Application logs: Standard (0-30 days), Standard-IA (30-90 days), Glacier (90-365 days), deleted after 1 year.",
        "tags": "aws, s3, lifecycle, cost-optimization, glacier, storage-class"
    },
    {
        "name": "EKS Node Group Strategy",
        "category": "aws",
        "description": "Use multiple node groups: a small on-demand group for system pods (CoreDNS, metrics-server) and a larger Spot instance group for application workloads. Spot saves 60-90% on compute.",
        "implementation": "# System node group (on-demand, small, always on):\nresource aws_eks_node_group system {\n  node_group_name = \"system\"\n  instance_types  = [\"t3.medium\"]\n  scaling_config {\n    desired_size = 2\n    min_size     = 2\n    max_size     = 4\n  }\n  # Taint to reserve for system pods only:\n  taint {\n    key    = \"CriticalAddonsOnly\"\n    effect = \"NO_SCHEDULE\"\n  }\n}\n# Application node group (Spot):\nresource aws_eks_node_group app_spot {\n  node_group_name = \"app-spot\"\n  capacity_type   = \"SPOT\"\n  instance_types  = [\"m5.xlarge\", \"m5a.xlarge\", \"m4.xlarge\"]\n  scaling_config {\n    desired_size = 3\n    min_size     = 1\n    max_size     = 50\n  }\n}",
        "benefits": "60-90% compute cost savings on Spot. Multiple instance types reduce Spot interruption risk. System pods on on-demand for reliability.",
        "tradeoffs": "Spot instances can be interrupted with 2-minute warning. Apps must be stateless and handle graceful shutdown.",
        "example": "Karpenter can provision Spot instances across 5+ instance families, minimizing interruption risk while maximizing savings.",
        "tags": "aws, eks, spot-instances, node-groups, cost-optimization, karpenter"
    },
    {
        "name": "ALB vs NLB Decision Pattern",
        "category": "aws",
        "description": "Application Load Balancer (ALB) for HTTP/HTTPS with path routing, host routing, auth, WAF. Network Load Balancer (NLB) for TCP/UDP, ultra-low latency, static IPs, or non-HTTP protocols.",
        "implementation": "# Use ALB when:\n# - HTTP/HTTPS traffic\n# - Path-based routing (/api -> service-a, /web -> service-b)\n# - Host-based routing (api.domain.com vs web.domain.com)\n# - WebSocket support\n# - Cognito/OIDC authentication\n# - AWS WAF integration\n# - Content-based routing\n\n# Use NLB when:\n# - TCP/UDP protocols (databases, gaming, IoT)\n# - Ultra-low latency requirement (< 1ms)\n# - Static IP addresses needed (firewall whitelisting)\n# - Preserve source IP without X-Forwarded-For\n# - Millions of requests per second\n# - TLS passthrough (encryption end-to-end)",
        "benefits": "Choosing correctly avoids unnecessary complexity and cost. NLB is cheaper per LCU. ALB has more features for HTTP.",
        "tradeoffs": "ALB adds ~1-2ms latency vs NLB. NLB does not support path-based routing. ALB does not support static IPs natively.",
        "example": "EKS cluster: ALB Ingress Controller for web/API traffic (path routing), NLB for database proxy (TCP, static IP for firewall rules).",
        "tags": "aws, alb, nlb, load-balancer, networking, decision"
    },
    {
        "name": "Auto Scaling Group Patterns",
        "category": "aws",
        "description": "Configure ASG with target tracking scaling on CPU or custom metrics. Use launch templates (not launch configurations). Set multiple instance types for availability.",
        "implementation": "resource aws_autoscaling_policy cpu_tracking {\n  autoscaling_group_name = aws_autoscaling_group.app.name\n  name                   = \"cpu-target-tracking\"\n  policy_type            = \"TargetTrackingScaling\"\n  target_tracking_configuration {\n    predefined_metric_specification {\n      predefined_metric_type = \"ASGAverageCPUUtilization\"\n    }\n    target_value = 70.0\n  }\n}\n# Best practices:\n# - health_check_type = ELB (not EC2) for ALB-backed ASG\n# - instance_refresh for zero-downtime AMI updates\n# - Mixed instances policy for Spot diversity",
        "benefits": "Automatic capacity management. Handles traffic spikes and scale-in for cost savings. Instance refresh enables fleet updates without downtime.",
        "tradeoffs": "Scale-out has a 3-5 minute lag (instance boot + app startup). Pre-warm for known traffic spikes (Black Friday).",
        "example": "Target 70% CPU utilization with 5-minute cooldown. At 90% CPU, ASG adds instances within 3-5 minutes automatically.",
        "tags": "aws, asg, autoscaling, scaling, ec2, target-tracking"
    },
    {
        "name": "RDS Multi-AZ Setup Pattern",
        "category": "aws",
        "description": "Enable Multi-AZ for production RDS to get automatic failover to standby in case of AZ failure. Standby is synchronous replica — not a read replica. Failover takes 1-2 minutes.",
        "implementation": "resource aws_db_instance postgres {\n  identifier        = \"prod-postgres\"\n  engine            = \"postgres\"\n  engine_version    = \"16.2\"\n  instance_class    = \"db.r7g.xlarge\"\n  allocated_storage = 100\n  storage_type      = \"gp3\"\n  storage_encrypted = true\n\n  multi_az               = true   # Synchronous standby\n  backup_retention_period = 7     # Days\n  backup_window          = \"03:00-04:00\"\n  maintenance_window     = \"Mon:04:00-Mon:05:00\"\n  deletion_protection    = true\n\n  # Read replicas for read scaling (separate from Multi-AZ):\n  # aws_db_instance with replicate_source_db\n}",
        "benefits": "Automatic failover without manual intervention. Standby in different AZ — survives AZ outage. Maintenance done on standby first.",
        "tradeoffs": "Multi-AZ doubles RDS cost. Failover takes 1-2 minutes — applications must handle brief connection errors with retry logic.",
        "example": "Production Postgres: Multi-AZ primary + 2 read replicas. App writes to primary endpoint, reads from reader endpoint.",
        "tags": "aws, rds, multi-az, database, ha, failover, postgres"
    },
    {
        "name": "CloudWatch Alerting Pattern",
        "category": "aws",
        "description": "Define alerts on key metrics: p99 latency, error rate, CPU, memory, queue depth. Use composite alarms to reduce noise. Route alerts to SNS -> PagerDuty/Slack.",
        "implementation": "resource aws_cloudwatch_metric_alarm api_errors {\n  alarm_name          = \"api-error-rate-high\"\n  comparison_operator = \"GreaterThanThreshold\"\n  evaluation_periods  = 2\n  metric_name         = \"5XXError\"\n  namespace           = \"AWS/ApplicationELB\"\n  period              = 60\n  statistic           = \"Sum\"\n  threshold           = 10\n  alarm_actions       = [aws_sns_topic.alerts.arn]\n  dimensions = {\n    LoadBalancer = aws_alb.main.arn_suffix\n  }\n}\n# Composite alarm — only alert if BOTH high error rate AND high latency:\nresource aws_cloudwatch_composite_alarm api_degraded {\n  alarm_rule = \"ALARM(api-error-rate-high) AND ALARM(api-latency-high)\"\n}",
        "benefits": "Composite alarms reduce false positives. SNS fan-out to multiple channels. Anomaly detection for variable-baseline metrics.",
        "tradeoffs": "CloudWatch metrics have 1-minute minimum granularity (high-res: 10s). Complex queries need CloudWatch Insights.",
        "example": "Alert only when BOTH error rate > 1% AND p99 latency > 2s for 2 consecutive minutes — prevents alerts during brief spikes.",
        "tags": "aws, cloudwatch, alerting, monitoring, sns, composite-alarm"
    },
    {
        "name": "Cost Optimization Tagging Strategy",
        "category": "aws",
        "description": "Tag all AWS resources with environment, team, service, and cost-center. Enable AWS Cost Explorer by tag. Set tag policies in AWS Organizations to enforce tagging.",
        "implementation": "# Required tags on every resource:\nlocals {\n  mandatory_tags = {\n    Environment = var.environment      # prod, staging, dev\n    Team        = var.team_name        # payments, platform, data\n    Service     = var.service_name     # checkout-api, data-pipeline\n    CostCenter  = var.cost_center      # CC-1234\n    ManagedBy   = \"terraform\"\n  }\n}\nresource aws_instance app {\n  tags = merge(local.mandatory_tags, {\n    Name = \"${var.service_name}-${var.environment}\"\n  })\n}\n# Enforce with AWS Config rule:\n# required-tags rule checks all resources have mandatory tags",
        "benefits": "Chargeback by team/service. Identify cost anomalies per service. Meet FinOps requirements for cloud cost allocation.",
        "tradeoffs": "Tag enforcement takes time to implement across existing resources. Tags don't appear on Cost Explorer for 24 hours.",
        "example": "Engineering finds that 40% of AWS costs are from one team's dev environment running 24/7 — fixed with auto-shutdown schedules.",
        "tags": "aws, cost-optimization, tagging, finops, cost-explorer, organizations"
    },
    {
        "name": "Security Group Layering Pattern",
        "category": "aws",
        "description": "Layer security groups for defense-in-depth: ALB security group accepts only 443 from internet, app security group accepts only from ALB SG, DB security group accepts only from app SG. Reference SGs by ID not CIDR.",
        "implementation": "# ALB SG: internet -> ALB\nresource aws_security_group alb {\n  ingress {\n    from_port   = 443\n    to_port     = 443\n    protocol    = \"tcp\"\n    cidr_blocks = [\"0.0.0.0/0\"]\n  }\n}\n# App SG: only from ALB SG (not CIDR)\nresource aws_security_group app {\n  ingress {\n    from_port       = 8080\n    to_port         = 8080\n    protocol        = \"tcp\"\n    security_groups = [aws_security_group.alb.id]  # Reference SG, not CIDR\n  }\n}\n# DB SG: only from App SG\nresource aws_security_group db {\n  ingress {\n    from_port       = 5432\n    to_port         = 5432\n    protocol        = \"tcp\"\n    security_groups = [aws_security_group.app.id]\n  }\n}",
        "benefits": "Database is unreachable from internet or even other app tiers. Changes to app CIDR don't require DB SG updates.",
        "tradeoffs": "Cross-VPC security group references require VPC peering. Circular SG references are not allowed.",
        "example": "Three-tier layering: internet -> ALB SG -> App SG -> DB SG. No IP addresses — all SG-to-SG references.",
        "tags": "aws, security-group, networking, vpc, defense-in-depth, layering"
    },

    # ── Terraform Patterns (8) ────────────────────────────────────────────────
    {
        "name": "Terraform Module Structure Best Practice",
        "category": "terraform",
        "description": "Organise Terraform code into reusable modules with clear inputs, outputs, and versions. Each module does one thing. Root module composes infrastructure from child modules.",
        "implementation": "# Directory structure:\ninfra/\n  modules/\n    vpc/\n      main.tf        # Resources\n      variables.tf   # Input variables with validation\n      outputs.tf     # Output values\n      versions.tf    # Required providers + version constraints\n      README.md\n    eks/\n      ...\n  environments/\n    prod/\n      main.tf        # Calls modules\n      terraform.tfvars\n    staging/\n      ...\n\n# Call module:\nmodule vpc {\n  source  = \"../../modules/vpc\"\n  version = \"~> 2.0\"  # If published to registry\n  cidr    = \"10.0.0.0/16\"\n  azs     = [\"ap-south-1a\", \"ap-south-1b\", \"ap-south-1c\"]\n}",
        "benefits": "DRY — same module for dev/staging/prod with different inputs. Easy to test modules in isolation. Clear contracts via variables and outputs.",
        "tradeoffs": "Module versioning requires registry or git tags. Too many abstraction layers makes debugging harder.",
        "example": "VPC module used by 5 environments with different CIDR ranges — same tested module, different inputs.",
        "tags": "terraform, modules, structure, dry, best-practice"
    },
    {
        "name": "Remote State with S3 and DynamoDB Lock",
        "category": "terraform",
        "description": "Store Terraform state in S3 with server-side encryption and versioning. Use DynamoDB for state locking to prevent concurrent runs. Never use local state in production.",
        "implementation": "terraform {\n  backend s3 {\n    bucket         = \"myorg-terraform-state\"\n    key            = \"prod/vpc/terraform.tfstate\"\n    region         = \"ap-south-1\"\n    encrypt        = true\n    dynamodb_table = \"terraform-state-lock\"\n  }\n}\n\n# Create the S3 bucket and DynamoDB table (bootstrap once):\nresource aws_s3_bucket tf_state {\n  bucket = \"myorg-terraform-state\"\n  versioning { enabled = true }\n  server_side_encryption_configuration {\n    rule { apply_server_side_encryption_by_default { sse_algorithm = \"AES256\" } }\n  }\n}\nresource aws_dynamodb_table tf_lock {\n  name         = \"terraform-state-lock\"\n  billing_mode = \"PAY_PER_REQUEST\"\n  hash_key     = \"LockID\"\n  attribute { name = \"LockID\"; type = \"S\" }\n}",
        "benefits": "State accessible to entire team. Versioning enables state recovery. DynamoDB prevents simultaneous applies that corrupt state.",
        "tradeoffs": "Requires bootstrapping — S3 bucket and DynamoDB table created before Terraform can use them.",
        "example": "CI/CD pipeline runs terraform apply — DynamoDB lock ensures only one pipeline runs at a time even on parallel PRs.",
        "tags": "terraform, remote-state, s3, dynamodb, state-lock, backend"
    },
    {
        "name": "Terraform Workspace Strategy",
        "category": "terraform",
        "description": "Use workspaces to manage multiple environments from the same Terraform config. Each workspace has separate state. Combine with tfvars files for environment-specific values.",
        "implementation": "# Create workspaces:\nterraform workspace new dev\nterraform workspace new staging\nterraform workspace new prod\nterraform workspace select prod\n\n# Use workspace in config:\nlocals {\n  env_config = {\n    dev     = { instance_type = \"t3.micro\", min_size = 1 }\n    staging = { instance_type = \"t3.medium\", min_size = 2 }\n    prod    = { instance_type = \"m5.xlarge\", min_size = 3 }\n  }\n  config = local.env_config[terraform.workspace]\n}\n\nresource aws_instance app {\n  instance_type = local.config.instance_type\n}",
        "benefits": "Single codebase for all environments. Workspace-specific state isolation. terraform.workspace enables environment-conditional logic.",
        "tradeoffs": "Workspaces share the same backend config. Complex environments may need separate repos/modules rather than workspaces.",
        "example": "terraform workspace select prod && terraform plan -- promotes exact same code that ran in staging.",
        "tags": "terraform, workspace, environments, dev-staging-prod, state"
    },
    {
        "name": "Terraform Variable Validation",
        "category": "terraform",
        "description": "Add validation blocks to input variables to catch invalid values before apply. Prevents deploying with wrong instance types, invalid CIDR ranges, or missing required values.",
        "implementation": "variable environment {\n  type        = string\n  description = \"Deployment environment\"\n  validation {\n    condition     = contains([\"dev\", \"staging\", \"prod\"], var.environment)\n    error_message = \"Environment must be dev, staging, or prod.\"\n  }\n}\n\nvariable instance_type {\n  type    = string\n  default = \"t3.medium\"\n  validation {\n    condition     = can(regex(\"^(t3|m5|c5|r5)\\\\.\", var.instance_type))\n    error_message = \"Only t3, m5, c5, or r5 instance families are approved.\"\n  }\n}\n\nvariable vpc_cidr {\n  type = string\n  validation {\n    condition     = can(cidrhost(var.vpc_cidr, 0))\n    error_message = \"Must be a valid CIDR block.\"\n  }\n}",
        "benefits": "Fail fast at plan time — not after apply. Self-documenting constraints. Enforces organization standards in shared modules.",
        "tradeoffs": "Complex validation logic can be hard to read. Cannot validate against remote state or data sources.",
        "example": "Module validation prevents deploying prod with t3.micro or a CIDR that overlaps with on-prem network.",
        "tags": "terraform, validation, variables, best-practice, fail-fast"
    },
    {
        "name": "Terraform Output Chaining Between Modules",
        "category": "terraform",
        "description": "Pass outputs from one module as inputs to another instead of hardcoding resource IDs. Creates explicit dependency graph and makes modules composable.",
        "implementation": "# vpc module outputs:\noutput vpc_id {\n  value       = aws_vpc.main.id\n  description = \"VPC ID for use by other modules\"\n}\noutput private_subnet_ids {\n  value = aws_subnet.private[*].id\n}\n\n# eks module receives vpc outputs:\nmodule vpc {\n  source = \"./modules/vpc\"\n  cidr   = \"10.0.0.0/16\"\n}\n\nmodule eks {\n  source             = \"./modules/eks\"\n  vpc_id             = module.vpc.vpc_id          # Chained\n  subnet_ids         = module.vpc.private_subnet_ids  # Chained\n  # Terraform automatically creates dependency: eks runs after vpc\n}",
        "benefits": "No hardcoded IDs — change VPC CIDR and everything updates. Explicit dependencies prevent ordering issues.",
        "tradeoffs": "Circular dependencies between modules are impossible — must design acyclic dependency graph.",
        "example": "VPC -> EKS -> ALB module chain: each module receives IDs from previous, Terraform resolves correct apply order.",
        "tags": "terraform, outputs, modules, dependencies, chaining, composition"
    },
    {
        "name": "Provider Version Pinning",
        "category": "terraform",
        "description": "Pin Terraform and provider versions in versions.tf using pessimistic constraint (~>) to allow patch updates but not breaking changes. Check in .terraform.lock.hcl to ensure team consistency.",
        "implementation": "# versions.tf:\nterraform {\n  required_version = \"~> 1.7\"   # Allow 1.7.x, not 2.x\n  required_providers {\n    aws = {\n      source  = \"hashicorp/aws\"\n      version = \"~> 5.40\"   # Allow 5.40.x and above, not 6.x\n    }\n    kubernetes = {\n      source  = \"hashicorp/kubernetes\"\n      version = \"~> 2.27\"\n    }\n  }\n}\n\n# Commit lock file (critical):\ngit add .terraform.lock.hcl\ngit commit -m 'pin provider versions'\n\n# Update providers intentionally:\nterraform init -upgrade\n# Review changes in lock file, test, then commit",
        "benefits": "Reproducible runs across team members and CI. Prevents surprise breaking changes from provider updates.",
        "tradeoffs": "Must regularly update pinned versions to get security fixes. Lock file conflicts when team members run init on different platforms.",
        "example": "AWS provider 5.x -> 6.x had breaking changes in resource naming. Pinning to ~> 5.40 protected production from accidental upgrade.",
        "tags": "terraform, versioning, provider, lock-file, reproducibility, pinning"
    },
    {
        "name": "Resource Tagging Strategy in Terraform",
        "category": "terraform",
        "description": "Define default tags at the provider level so all resources inherit them. Override or add resource-specific tags as needed. Ensures consistent tagging without repetition.",
        "implementation": "# Provider-level default tags (apply to ALL resources):\nprovider aws {\n  region = var.aws_region\n  default_tags {\n    tags = {\n      Environment = var.environment\n      ManagedBy   = \"terraform\"\n      Repository  = \"github.com/org/infra\"\n      Team        = var.team\n    }\n  }\n}\n\n# Resource-specific tags merged with defaults:\nresource aws_instance app {\n  ami           = data.aws_ami.ubuntu.id\n  instance_type = \"m5.xlarge\"\n  tags = {\n    Name    = \"app-${var.environment}\"\n    Service = \"checkout-api\"   # Additional tag, not overriding defaults\n  }\n  # Final tags: Environment + ManagedBy + Repository + Team + Name + Service\n}",
        "benefits": "Zero effort per-resource tagging. Tags enforced consistently. Change default tags in one place to update all resources.",
        "tradeoffs": "Default tags cannot be overridden at resource level (they merge). Some resources have tag limits.",
        "example": "100-resource module with provider default_tags — all resources tagged consistently without adding tags to each resource block.",
        "tags": "terraform, tagging, cost-allocation, aws, default-tags, finops"
    },
    {
        "name": "Terragrunt DRY Pattern",
        "category": "terraform",
        "description": "Use Terragrunt to eliminate duplication in Terraform root modules. Define backend config, provider version, and remote state once. Each environment has minimal terragrunt.hcl with only environment-specific values.",
        "implementation": "# Root terragrunt.hcl (shared config):\nremote_state {\n  backend = \"s3\"\n  config = {\n    bucket         = \"myorg-tf-state\"\n    key            = \"${path_relative_to_include()}/terraform.tfstate\"\n    region         = \"ap-south-1\"\n    dynamodb_table = \"tf-lock\"\n  }\n}\ngenerate \"provider\" {\n  path    = \"provider.tf\"\n  content = <<-EOF\n    provider \"aws\" { region = \"ap-south-1\" }\n  EOF\n}\n\n# Per-environment terragrunt.hcl:\ninclude \"root\" {\n  path = find_in_parent_folders()\n}\nterraform {\n  source = \"../../modules//eks\"\n}\ninputs = {\n  cluster_name = \"prod-eks\"\n  node_count   = 5\n}",
        "benefits": "Backend config defined once — not in every module. Environment configs are tiny — just the differences.",
        "tradeoffs": "Adds Terragrunt as a dependency. Learning curve. Complex dependency chains between modules.",
        "example": "50 Terraform modules, 3 environments = 150 root modules. With Terragrunt: 50 module configs + tiny per-env overrides.",
        "tags": "terraform, terragrunt, dry, modules, environments, backend"
    },

    # ── Observability Patterns (7) ────────────────────────────────────────────
    {
        "name": "RED Method for Service Metrics",
        "category": "observability",
        "description": "Monitor every service with three metrics: Rate (requests per second), Errors (failed requests per second), Duration (response time distribution). These three metrics cover 95% of service health questions.",
        "implementation": "# Prometheus metrics for RED:\n# Rate:\nsum(rate(http_requests_total{service='api'}[5m]))\n\n# Error Rate:\nsum(rate(http_requests_total{service='api', status=~'5..'}[5m]))\n/\nsum(rate(http_requests_total{service='api'}[5m]))\n\n# Duration (p99 latency):\nhistogram_quantile(0.99,\n  sum(rate(http_request_duration_seconds_bucket{service='api'}[5m]))\n  by (le)\n)\n\n# Alert thresholds:\n# Error rate > 1% for 5 minutes\n# p99 latency > 500ms for 5 minutes",
        "benefits": "Minimal metric set that covers all critical failure modes. Easy to explain to product and leadership. Grafana dashboard per service takes 30 minutes to build.",
        "tradeoffs": "RED focuses on the service boundary — doesn't capture internal issues (DB slow queries, cache miss rate). Combine with USE for infra.",
        "example": "Grafana dashboard: Rate (req/s graph), Error rate (% with alert threshold line), Duration (p50/p95/p99 latency).",
        "tags": "observability, monitoring, red-method, prometheus, metrics, grafana, sre"
    },
    {
        "name": "USE Method for Infrastructure Metrics",
        "category": "observability",
        "description": "Monitor every infrastructure resource with: Utilization (% time resource is busy), Saturation (queue depth or waiting requests), Errors (error events). Apply to CPU, memory, disk, network, database connections.",
        "implementation": "# CPU USE:\n# Utilization:\n100 - avg(rate(node_cpu_seconds_total{mode='idle'}[5m])) by (instance) * 100\n# Saturation (run queue):\nnode_load1 / count(node_cpu_seconds_total{mode='idle'}) by (instance)\n# Errors: hardware errors (rare, use node_hwmon)\n\n# Disk USE:\n# Utilization:\nrate(node_disk_io_time_seconds_total[5m]) * 100\n# Saturation:\nrate(node_disk_io_time_weighted_seconds_total[5m])\n# Errors:\nrate(node_disk_read_errors_total[5m]) + rate(node_disk_write_errors_total[5m])\n\n# DB Connection USE:\n# Utilization: connections_used / max_connections\n# Saturation: waiting connections in queue",
        "benefits": "Systematic — covers every resource type consistently. Identifies bottlenecks before they cause user impact.",
        "tradeoffs": "Saturation metrics are resource-specific and harder to standardise. Some saturation metrics not available from node_exporter alone.",
        "example": "Disk saturation (io_time_weighted) spikes before disk utilization does — early warning of I/O bottleneck.",
        "tags": "observability, monitoring, use-method, prometheus, infrastructure, cpu, disk"
    },
    {
        "name": "Four Golden Signals (Google SRE)",
        "category": "observability",
        "description": "Google SRE's four signals for monitoring any service: Latency, Traffic, Errors, Saturation. If you can only monitor four things, monitor these. Foundation for SLI/SLO definition.",
        "implementation": "# 1. Latency (not just average — use percentiles):\nhistogram_quantile(0.99, rate(http_duration_bucket[5m]))\n\n# 2. Traffic:\nsum(rate(http_requests_total[5m])) by (service)\n\n# 3. Errors:\nsum(rate(http_requests_total{code=~'5..'}[5m]))\n/ sum(rate(http_requests_total[5m]))\n\n# 4. Saturation:\n# CPU: node_load1 / num_cpus\n# Memory: 1 - (node_memory_MemAvailable / node_memory_MemTotal)\n# Queue depth: kafka_consumer_lag, redis_blocked_clients\n\n# Key insight: distinguish ERROR latency from SUCCESS latency\n# Fast errors don't indicate good service",
        "benefits": "Covered by Google SRE book — industry standard. Four signals cover symptoms (latency, errors) and capacity (traffic, saturation).",
        "tradeoffs": "Must distinguish success vs error latency. Saturation definition varies by resource type.",
        "example": "SLO: 99.9% of requests complete in < 200ms with < 0.1% errors, measured over 30-day rolling window.",
        "tags": "observability, sre, four-golden-signals, latency, errors, traffic, saturation"
    },
    {
        "name": "Structured Logging JSON Format",
        "category": "observability",
        "description": "Emit logs as JSON with consistent fields: timestamp, level, service, trace_id, request_id, user_id, message, and error. Enables log aggregation, searching, and correlation with traces.",
        "implementation": "# Python (structlog):\nimport structlog\nlog = structlog.get_logger()\nlog.info('request_processed',\n    service='checkout-api',\n    method='POST',\n    path='/orders',\n    status=201,\n    duration_ms=45,\n    trace_id=ctx.trace_id,\n    user_id=ctx.user_id\n)\n# Output:\n# {\"timestamp\":\"2026-04-05T10:00:00Z\",\"level\":\"info\",\n#  \"service\":\"checkout-api\",\"method\":\"POST\",\n#  \"status\":201,\"duration_ms\":45,\n#  \"trace_id\":\"abc123\",\"user_id\":\"u-456\"}\n\n# Avoid: print('User 456 placed order 789')\n# Prefer: log.info('order_placed', user_id='456', order_id='789')",
        "benefits": "grep becomes jq. Log aggregation (ELK, Loki) can index and search fields. trace_id links logs to distributed traces.",
        "tradeoffs": "JSON logs are ~3x larger than plain text. Developers must learn structured logging discipline.",
        "example": "All services emit trace_id — Grafana Loki query: {trace_id='abc123'} shows every log line across 5 services for one request.",
        "tags": "observability, logging, structured-logs, json, tracing, correlation"
    },
    {
        "name": "Distributed Tracing Setup",
        "category": "observability",
        "description": "Instrument services with OpenTelemetry to generate spans. Export to Jaeger or Tempo. Every request gets a trace_id that follows it across all services and databases.",
        "implementation": "# Python OpenTelemetry setup:\nfrom opentelemetry import trace\nfrom opentelemetry.sdk.trace import TracerProvider\nfrom opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter\nfrom opentelemetry.sdk.trace.export import BatchSpanProcessor\n\nprovider = TracerProvider()\nprocessor = BatchSpanProcessor(OTLPSpanExporter(endpoint='http://otel-collector:4317'))\nprovider.add_span_processor(processor)\ntrace.set_tracer_provider(provider)\n\ntracer = trace.get_tracer(__name__)\n\n# In route handler:\nwith tracer.start_as_current_span('process_order') as span:\n    span.set_attribute('order.id', order_id)\n    span.set_attribute('user.id', user_id)\n    result = process_order(order_id)\n    span.set_attribute('order.total', result.total)",
        "benefits": "See entire request flow across microservices. Identify which service is slow. Correlate with logs via trace_id.",
        "tradeoffs": "Adds latency overhead (< 1ms typically). Requires instrumentation in every service. Sampling strategy needed for high-traffic services.",
        "example": "User reports slow checkout. Trace shows: API (10ms) -> Order Service (50ms) -> Payment Service (800ms) -> bottleneck found.",
        "tags": "observability, tracing, opentelemetry, jaeger, tempo, distributed-systems"
    },
    {
        "name": "Alert Fatigue Prevention Pattern",
        "category": "observability",
        "description": "Design alerts to be actionable, rare, and correctly routed. Every alert should require human action. Use symptom-based alerts not cause-based. Set proper thresholds and evaluation windows.",
        "implementation": "# BAD alerts (cause alert fatigue):\n# - CPU > 80% for 1 minute (too sensitive, not actionable)\n# - Any 5xx error (single errors are normal)\n# - Disk > 70% (too early, too many false positives)\n\n# GOOD alerts (actionable, rare):\n# Error RATE > 1% sustained 5 minutes:\nexpr: sum(rate(errors[5m])) / sum(rate(requests[5m])) > 0.01\nfor: 5m\n\n# p99 latency > SLO threshold:\nexpr: histogram_quantile(0.99, rate(duration_bucket[5m])) > 0.5\nfor: 5m\n\n# SLO burn rate (most important alert):\n# Consuming error budget 14x faster than normal:\nexpr: job:slo_errors:rate1h > 14 * (1 - 0.999)\nfor: 2m\n\n# Routing:\n# p0 (paging): SLO burn rate\n# p1 (slack): error rate > threshold\n# p2 (ticket): disk, capacity warnings",
        "benefits": "On-call engineers respond to alerts instead of ignoring them. Fewer false positives = faster response to real incidents.",
        "tradeoffs": "Requires investment in SLO definition before SLO-based alerting. Multi-window burn rate alerts are complex to set up.",
        "example": "Team reduced alert volume from 200/week to 20/week by switching from cause-based to symptom-based SLO burn rate alerts.",
        "tags": "observability, alerting, slo, burn-rate, on-call, pagerduty, alert-fatigue"
    },
    {
        "name": "SLI / SLO / SLA Definition Pattern",
        "category": "observability",
        "description": "SLI (Service Level Indicator): measured metric (e.g., error rate). SLO (Service Level Objective): internal target (e.g., 99.9% success). SLA (Service Level Agreement): external contract with penalty. SLO must be stricter than SLA.",
        "implementation": "# SLI: what you measure\nsli_availability = good_requests / total_requests\n# where good = status < 500 and latency < 200ms\n\n# SLO: internal target (set 10% stricter than SLA)\n# SLA: 99.5% availability -> SLO: 99.9%\n\n# Error budget: 1 - SLO = 0.1% errors allowed per month\n# 0.1% of 30 days = 43.2 minutes of allowed downtime per month\n\n# Prometheus recording rule:\nrecord: job:slo_errors:rate5m\nexpr: |\n  sum(rate(http_requests_total{status=~'5..'}[5m]))\n  /\n  sum(rate(http_requests_total[5m]))\n\n# Track error budget remaining:\nerror_budget_remaining = 1 - (sum(slo_errors) / error_budget_total)",
        "benefits": "Data-driven reliability decisions. Error budget enables feature velocity vs reliability trade-off conversations.",
        "tradeoffs": "Defining good SLIs requires understanding what users care about. Wrong SLI leads to gaming the metric.",
        "example": "Payment service SLO: 99.95% of payment requests complete in < 500ms, measured per 28-day rolling window.",
        "tags": "observability, slo, sli, sla, reliability, error-budget, sre"
    },
]


# ── Seed Data: AVA Self-Architecture ─────────────────────────────────────────
AVA_ARCHITECTURE = [
    {
        "name": "AVA System Overview",
        "category": "ava_system",
        "description": "AVA is an Autonomous DevOps AI Assistant running locally on WSL2 Ubuntu. Built by Manoj, Delhi. Production URL: https://172.24.212.81:5443",
        "implementation": """
RUNTIME STACK:
- OS: WSL2 Ubuntu on Windows 11
- GPU: RTX 5060 Ti 16GB GDDR7
- RAM: 32GB DDR4 (24GB allocated to WSL2)
- CPU: Ryzen 1600 (6 cores)

DOCKER CONTAINERS (docker compose):
- ava-agent      → Flask/Gunicorn app on :5443 (HTTPS)
- agent_postgres → PostgreSQL 15 on :5432
- agent_redis    → Redis 7 on :6379
- opa            → Open Policy Agent on :8181

PROCESS INSIDE ava-agent:
- Gunicorn (2 workers) serving Flask app
- Main file: web_agent_v2.1_guardrail.py
- WSGI entry: wsgi.py
- Gunicorn config: gunicorn.conf.py
""",
        "benefits": "AVA runs fully offline. No data leaves the machine. All inference is local.",
        "tradeoffs": "Response time 4-12s due to local LLM inference.",
        "example": "curl -sk https://localhost:5443/health",
        "tags": "ava, system, architecture, overview, runtime"
    },
    {
        "name": "AVA AI Models",
        "category": "ava_system",
        "description": "LLM and embedding models used by AVA for inference and RAG",
        "implementation": """
LLM MODEL:
- Model: qwen2.5:14b (Q4_K_M quantization)
- Provider: Ollama (running as systemd service)
- Ollama host: http://host.docker.internal:11434
- Models path: /mnt/i/ai-lab/models
- Keep-alive: 30 minutes
- Context window: 8192 tokens
- Temperature: 0.2 (responses), 0.0 (warmup)
- Vision model: llava:13b (for image analysis)

EMBEDDING MODEL:
- Model: nomic-embed-text
- Provider: Ollama
- Used for: ChromaDB vector embeddings
- Dimensions: 768

WARMUP:
- LLM warmed on startup in background thread
- Warmup time: ~23 seconds
- Prevents cold-start on first query
""",
        "benefits": "Fully offline inference. No API costs. RTX 5060 Ti handles 14B model at 4-8s response.",
        "tradeoffs": "Model size limits to 14B for fast responses. 32B used only for complex reasoning.",
        "example": "OLLAMA_HOST=0.0.0.0 OLLAMA_MODELS=/mnt/i/ai-lab/models ollama serve",
        "tags": "ava, models, ollama, qwen, llm, embedding, nomic"
    },
    {
        "name": "AVA ChromaDB Knowledge Base",
        "category": "ava_system",
        "description": "AVA's vector database with 4 collections storing DevOps knowledge",
        "implementation": """
CHROMADB PATH: /home/manoj/ava-data/chromadb
DOCKER MOUNT: /home/manoj/ava-data/chromadb -> /data/chromadb
CHROMA_PATH env: /data/chromadb (inside container)

COLLECTIONS:
1. devops_policies_v2   — 3,885 chunks
   Source: mrcloudbook.com DevOps policies
   Content: Kubernetes, Docker, AWS, Linux policies

2. devops_blogs_v1      — 2,513 chunks
   Sources: AWS, Cloudflare, K8s, Pulumi, Azure,
            Red Hat, Ubuntu, GCP blogs
   Content: Technical blog articles

3. devops_fixes_v1      — 20+ chunks
   Content: Real DevOps troubleshooting fixes
   Covers: K8s errors, Docker, Terraform, Linux, AWS

4. devops_patterns_v1   — 50+ chunks
   Content: Infrastructure patterns, best practices
   Covers: K8s, CI/CD, AWS, Terraform, Observability

TOTAL: ~6,500+ chunks
TARGET: 50,000+ chunks (Phase 5A goal)
""",
        "benefits": "Local vector search. No external API needed. All 4 collections queried per request.",
        "tradeoffs": "NTFS mount causes ChromaDB errors — must use WSL-native ext4 path.",
        "example": "python3 knowledge_updater/phase5a_ingestor.py",
        "tags": "ava, chromadb, knowledge-base, collections, rag, vectors"
    },
    {
        "name": "AVA File Structure",
        "category": "ava_system",
        "description": "Key files and folders in AVA's codebase",
        "implementation": """
PROJECT ROOT: /mnt/i/ai-lab/projects/devops-agent/
GITHUB: https://github.com/linuxlearning38/agentic-safety-gate (private)

CORE FILES:
- web_agent_v2.1_guardrail.py  <- MAIN APP (1700+ lines)
- wsgi.py                       <- Gunicorn WSGI entry
- gunicorn.conf.py              <- 2 workers, preload=False
- docker-compose.yml            <- Full stack definition
- Dockerfile                    <- Python 3.11, non-root ava user
- requirements.txt              <- All Python dependencies

CONTROL MODULE (control/):
- auth.py           <- JWT authentication (24h tokens)
- registry.py       <- Tool registry + whitelist (token-aware)
- react_loop.py     <- ReAct reasoning loop (5 iterations max)
- secure_executor.py <- shell=False command execution
- security_layer.py  <- OPA policy integration
- incident_reporter.py <- JSON incident reports
- vuln_scanner.py   <- Trivy + Lynis integration
- approval.py       <- Human-in-loop approval queue
- logger.py         <- Structured audit logging

KNOWLEDGE UPDATER (knowledge_updater/):
- ingestor.py         <- RSS scraper + ChromaDB ingestor
- hybrid_retrieval.py <- 4-collection RAG retrieval
- phase5a_ingestor.py <- New collections + seed data
- scraper.py          <- BeautifulSoup web scraper
- scheduler.py        <- Cron-based knowledge updates
- config.yaml         <- Blog sources + ChromaDB config

DATA PATHS (/home/manoj/ava-data/):
- chromadb/           <- ChromaDB vector store
- ava_memory.json     <- User memory (Manoj's infra stack)
- query_history.json  <- Past query history
- control_whitelist.json <- Approved commands
- logs/               <- Access + error logs
- reports/            <- Incident + security reports
""",
        "benefits": "Clean separation of concerns. Control module is independent of knowledge module.",
        "tradeoffs": "Main file is 1700+ lines — candidate for refactoring in Phase 6.",
        "example": "docker exec ava-agent ls /app/",
        "tags": "ava, files, structure, codebase, architecture"
    },
    {
        "name": "AVA Request Pipeline",
        "category": "ava_system",
        "description": "How a query flows through AVA from HTTP request to response",
        "implementation": """
REQUEST FLOW:
POST /ask
  |
JWT validation (control/auth.py)
  |
Rate limiting (Flask-Limiter, 30 req/min)
  |
detect_multiple_questions() -> split if 2-3 questions
  |
For each question:
  |
detect_query_intent() -> definition|comparison|diagram|troubleshooting|general
  |
hybrid_retrieval.query()
  |- devops_policies_v2  (n=6 for definition, n=4 for others)
  |- devops_blogs_v1     (n=2)
  |- devops_fixes_v1     (n=4 if troubleshooting)
  +- devops_patterns_v1  (n=2 if not troubleshooting)
  |
score_context_confidence() -> high|medium|low
  |
build_memory_context() -> inject Manoj's infra + past fixes
  |
generate_response() -> Qwen2.5:14b via Ollama
  |
ReAct loop (if command needed) -> OPA gate -> execute
  |
JSON response: {response, confidence, intent, sources_used}

RESPONSE TIMES:
- Definition: 4-7s
- Comparison: 5-8s
- Troubleshooting: 6-10s
- Diagram (Mermaid): 4-6s
- Multi-question (3x): 15-25s
""",
        "benefits": "Every query is grounded in RAG context. Confidence scoring prevents hallucination. Memory injection personalizes responses.",
        "tradeoffs": "Sequential multi-question adds latency. No streaming — complete JSON response only.",
        "example": "curl -sk https://localhost:5443/ask -H 'Authorization: Bearer TOKEN' -d '{\"query\":\"What is readiness probe?\"}'",
        "tags": "ava, pipeline, request-flow, rag, inference, architecture"
    },
    {
        "name": "AVA Security Architecture",
        "category": "ava_system",
        "description": "All security layers in AVA — from network to command execution",
        "implementation": """
LAYER 1 — NETWORK:
- TLS 1.3 (RSA 4096 self-signed cert)
- HTTPS only on :5443
- HTTP on :5002 redirects to HTTPS

LAYER 2 — AUTHENTICATION:
- JWT tokens (24h expiry)
- Roles: admin (full access), readonly (no execution)
- bcrypt password hashing
- Login: POST /auth/login

LAYER 3 — RATE LIMITING:
- Flask-Limiter: 30 req/min per user
- 429 JSON response on breach

LAYER 4 — COMMAND SAFETY (control/registry.py):
- Token-aware whitelist matching
- shell=False (no shell injection)
- Risk levels: LOW/MEDIUM/HIGH
- 'ls /etc/shadow' BLOCKED even if 'ls' is whitelisted

LAYER 5 — OPA POLICY GATE:
- Open Policy Agent on :8181
- Rego policies in policies/infrastructure.rego
- All commands validated before execution

LAYER 6 — CONFIDENCE SCORING:
- high (>0.85): auto-execute
- medium (0.6-0.85): suggest + wait
- low (<0.6): alert only, no execution

LAYER 7 — CONTAINER:
- Non-root user: ava (uid 1001)
- Read-only certs mount
- No privileged mode
""",
        "benefits": "7-layer defense. OPA is last gate before any execution. Confidence score prevents risky auto-execution.",
        "tradeoffs": "7 layers add ~50ms overhead per request. JWT secret must be rotated periodically.",
        "example": "docker exec ava-agent id  # shows: uid=1001(ava)",
        "tags": "ava, security, jwt, opa, tls, whitelist, confidence"
    },
    {
        "name": "AVA Phase Roadmap",
        "category": "ava_system",
        "description": "AVA's development phases from current state to autonomous control plane",
        "implementation": """
COMPLETED:
- Phase 1: Flask agent + ChromaDB RAG + OPA security
- Phase 2: RSS scraper + knowledge updater
- Phase 3: Mermaid diagrams + LLaVA image analysis + blog ingestion
- Phase 4: JWT + TLS + Docker + ReAct loop + Trivy/Lynis
- Phase 4.5: Confidence scoring + memory injection + multi-question
- Phase 5A Day 1: 4 collections + 20 fixes + 50 patterns seeded

IN PROGRESS:
- Phase 5A: Knowledge expansion to 50,000+ chunks

UPCOMING:
- Phase 5B: Webhook triggers + multi-turn conversation + SQLite
- Phase 5C: Self-healing module (auto-detect + fix known issues)
- Phase 5D: Jira + Slack + PagerDuty API integration
- Phase 5E: Terraform template provisioning (AWS/Azure/GCP)
- Phase 5F: Docker Compose installer + packaging
- Phase 6: Full autonomous control plane

COMMERCIAL TARGET:
- Indian enterprises (DPDP Act data localization)
- Banks, government, manufacturing (air-gapped)
- Replaces Watson/Accenture myWizard at 0/month recurring cost
""",
        "benefits": "Clear roadmap. Each phase builds on previous. Commercial value increases with each phase.",
        "tradeoffs": "AWS SAA-C03 exam (August 9, 2026) runs in parallel — time management critical.",
        "example": "AVA Phase 6 = Level 4 autonomy: Jira ticket -> provision infra -> deploy -> test -> close ticket",
        "tags": "ava, roadmap, phases, commercial, autonomous, devops"
    },
]


ARCHITECTURE_SEED_PATTERNS = [
    {
        "name": "API Gateway + Event Streaming Architecture",
        "category": "architecture",
        "description": "Use an API gateway as the synchronous entry point for client traffic, then publish domain events to a streaming backbone for asynchronous downstream processing. This separates request routing from event fan-out.",
        "implementation": "Request Flow:\n1. Client request enters through an API gateway such as Zuul, Kong, or Envoy.\n2. Gateway applies auth, routing, and service discovery.\n3. Request reaches the owning microservice.\n4. Service writes primary state and emits a domain event to Kafka.\n\nData Flow:\n1. Kafka topics carry immutable events such as playback-started, payment-authorized, or order-created.\n2. Stream processors and downstream services subscribe independently.\n3. Stateful stores or caches are updated from the event stream.\n\nWhen explaining this architecture, describe the synchronous request path separately from the asynchronous event path.",
        "benefits": "Clear separation of concerns. API path stays simple while downstream systems scale independently. Event fan-out avoids point-to-point integrations.",
        "tradeoffs": "Requires strong schema/version discipline. Debugging becomes harder because user-facing actions span both sync and async paths.",
        "example": "Netflix-style edge flow: Zuul routes playback/API requests, services publish events to Kafka, downstream consumers update stores, caches, and monitoring streams.",
        "tags": "architecture, api-gateway, kafka, event-driven, microservices, request-flow, data-flow"
    },
    {
        "name": "Cache-Aside Read Scaling Pattern",
        "category": "architecture",
        "description": "Serve hot reads from a cache while keeping the database as the source of truth. On cache miss, fetch from the database, populate the cache, and return the response.",
        "implementation": "Read Flow:\n1. Service receives request for an object or aggregate.\n2. Service checks EVCache or Redis first.\n3. If cache hit, return immediately.\n4. If cache miss, read from Cassandra or the primary database.\n5. Populate cache with TTL and return data.\n\nWrite Flow:\n1. Service writes to the primary store first.\n2. Cache entry is invalidated or refreshed.\n\nWhen describing this architecture, call out that cache reduces latency and shields the database from repeated hot reads.",
        "benefits": "Lower read latency. Reduced database load. Good fit for traffic hotspots and frequently reused aggregates.",
        "tradeoffs": "Cache invalidation and TTL selection are hard. Stale reads are possible if invalidation is delayed.",
        "example": "Netflix EVCache keeps hot catalog or playback metadata close to services while Cassandra remains the durable store.",
        "tags": "architecture, cache-aside, evcache, redis, cassandra, low-latency, read-scaling"
    },
    {
        "name": "Streaming Analytics Pipeline Pattern",
        "category": "architecture",
        "description": "Use a streaming log such as Kafka to collect events, then process them with a stream processor such as Samza or Flink to compute aggregates, enrich records, and feed downstream systems in near real time.",
        "implementation": "Pipeline Flow:\n1. Producers emit ordered events into Kafka topics.\n2. Stream processors consume partitions in parallel.\n3. Processors enrich, aggregate, or join event streams.\n4. Results are written to serving databases, caches, alerting systems, or downstream topics.\n\nWhen answering architecture questions, explain that Kafka is the transport backbone while the stream processor performs stateful computation over the event stream.",
        "benefits": "Near-real-time analytics. Decouples producers from consumers. Supports replay and reprocessing from the log.",
        "tradeoffs": "Operational complexity rises with consumer lag, partitioning, and schema evolution. Stateful stream jobs need checkpointing and recovery strategy.",
        "example": "Kafka carries playback or platform events, Samza computes aggregates, and downstream systems persist derived views for dashboards and APIs.",
        "tags": "architecture, kafka, samza, streaming, analytics, event-processing, pipeline"
    },
    {
        "name": "Operational Control Loop for Distributed Systems",
        "category": "architecture",
        "description": "Model large systems as a control loop: request path, state path, and observability path. The request path serves traffic, the state path stores or caches data, and the observability path monitors health and triggers action.",
        "implementation": "Three Paths:\n1. Request Path: gateway -> service -> downstream API/store.\n2. State Path: primary database + cache + derived views.\n3. Observability Path: metrics/events -> monitoring/alerting -> operator action.\n\nFor architecture answers, explicitly separate user traffic from telemetry flow so monitoring components like Mantis or alerting tools are not mistaken for primary request-serving components.",
        "benefits": "Makes architecture explanations clearer. Distinguishes business traffic from telemetry and control signals. Useful for troubleshooting and incident response.",
        "tradeoffs": "Adds conceptual complexity. Teams may over-model simple systems if this framing is used everywhere.",
        "example": "Netflix-style flow: Zuul and services handle requests, Cassandra/EVCache hold state, Kafka and monitoring tools feed control/observability loops such as Mantis.",
        "tags": "architecture, observability, mantis, request-path, telemetry, control-loop, distributed-systems"
    },
    {
        "name": "High-Scale Microservice Edge Pattern",
        "category": "architecture",
        "description": "At internet scale, edge routing, stateless services, event streaming, and distributed state stores work together. The edge gateway handles routing and protection, core services remain stateless, and state is externalized into databases, caches, and streams.",
        "implementation": "Design Rules:\n1. Keep stateless request-serving services behind the gateway.\n2. Put shared state in systems designed for scale: Cassandra, Kafka, distributed caches.\n3. Use asynchronous events for cross-domain side effects.\n4. Put monitoring and alerting beside the traffic path, not inside it.\n5. Explain request flow and data flow separately in documentation and AI answers.\n\nThis pattern is useful when answering questions about systems like Netflix, Uber, or large platform architectures.",
        "benefits": "Scales better than tightly coupled service meshes with embedded state. Makes independent scaling and failure isolation easier.",
        "tradeoffs": "Requires clear service boundaries, robust observability, and mature operational practices.",
        "example": "Gateway at the edge, stateless APIs in the middle tier, Cassandra + EVCache for state, Kafka + stream processing for asynchronous data movement.",
        "tags": "architecture, microservices, edge, cassandra, kafka, cache, scale, distributed"
    },
    {
        "name": "Architecture Explanation Pattern for AVA",
        "category": "architecture",
        "description": "When answering architecture questions, AVA should identify components first, then explain request flow, then data flow, then why each major technology exists. Avoid generic labels unless grounded by retrieved text.",
        "implementation": "Answer Template:\n1. Components: list named technologies exactly as grounded.\n2. Request Flow: explain the synchronous traffic path.\n3. Data Flow: explain event, storage, and cache movement.\n4. Key Technologies: map each named component to its role.\n5. Why They Are Used: summarize the system-level purpose.\n\nGrounding Rules:\n- Prefer named entities from the user query and retrieved context.\n- Prefer relationship lines with verbs like routes, carries, stores, caches, processes, monitors.\n- Avoid mixing unrelated SRE snippets into architecture explanations.",
        "benefits": "Makes architecture answers consistent, readable, and closer to expert system-design explanations.",
        "tradeoffs": "Needs clean architecture context to work well. If the retrieved evidence is weak, AVA should stay narrow instead of hallucinating.",
        "example": "For Zuul + Kafka + Cassandra + EVCache + Samza + Mantis: explain gateway routing, event streaming, durable state, cache reads, stream processing, and observability as separate roles.",
        "tags": "architecture, answer-quality, grounding, request-flow, data-flow, ava"
    },
]


def seed_ava_architecture():
    """Ingest AVA's own system architecture into devops_patterns_v1."""
    logger.info(f"Seeding {len(AVA_ARCHITECTURE)} AVA architecture entries into '{COLLECTION_PATTERNS}'...")
    ok = 0
    fail = 0
    for item in AVA_ARCHITECTURE:
        text = _pattern_to_text(item)
        meta = {
            "name":       item["name"][:200],
            "category":   item["category"],
            "tags":       item["tags"],
            "source":     "ava_self_architecture",
            "collection": COLLECTION_PATTERNS,
        }
        if ingest_text_to_collection(COLLECTION_PATTERNS, text, meta):
            ok += 1
        else:
            fail += 1
        time.sleep(0.1)
    logger.info(f"AVA architecture seed complete: {ok} ingested, {fail} failed")
    return ok, fail


def _pattern_to_text(p: dict) -> str:
    """Convert a pattern dict to a formatted text chunk for embedding."""
    return (
        f"PATTERN: {p['name']}\n\n"
        f"CATEGORY: {p['category']}\n\n"
        f"DESCRIPTION: {p['description']}\n\n"
        f"IMPLEMENTATION:\n{p['implementation']}\n\n"
        f"BENEFITS: {p['benefits']}\n\n"
        f"TRADEOFFS: {p['tradeoffs']}\n\n"
        f"EXAMPLE: {p['example']}\n\n"
        f"TAGS: {p['tags']}"
    )


def seed_patterns_collection():
    """Ingest all 50 seed patterns into devops_patterns_v1."""
    logger.info(f"Seeding {len(SEED_PATTERNS)} patterns into '{COLLECTION_PATTERNS}'...")
    ok = 0
    fail = 0
    for pattern in SEED_PATTERNS:
        text = _pattern_to_text(pattern)
        meta = {
            "name":       pattern["name"][:200],
            "category":   pattern["category"],
            "tags":       pattern["tags"],
            "source":     "ava_seed_patterns_v1",
            "collection": COLLECTION_PATTERNS,
        }
        if ingest_text_to_collection(COLLECTION_PATTERNS, text, meta):
            ok += 1
        else:
            fail += 1
        time.sleep(0.1)
    logger.info(f"Patterns seed complete: {ok} ingested, {fail} failed")
    return ok, fail


def seed_architecture_patterns_collection():
    """Ingest architecture-focused seed patterns into devops_patterns_v1."""
    logger.info(f"Seeding {len(ARCHITECTURE_SEED_PATTERNS)} architecture patterns into '{COLLECTION_PATTERNS}'...")
    ok = 0
    fail = 0
    for pattern in ARCHITECTURE_SEED_PATTERNS:
        text = _pattern_to_text(pattern)
        meta = {
            "name":       pattern["name"][:200],
            "category":   pattern["category"],
            "tags":       pattern["tags"],
            "source":     "ava_architecture_patterns_v1",
            "collection": COLLECTION_PATTERNS,
        }
        if ingest_text_to_collection(COLLECTION_PATTERNS, text, meta):
            ok += 1
        else:
            fail += 1
        time.sleep(0.1)
    logger.info(f"Architecture pattern seed complete: {ok} ingested, {fail} failed")
    return ok, fail


def _architecture_reference_to_text(doc: dict) -> str:
    return (
        f"ARCHITECTURE REFERENCE: {doc['title']}\n\n"
        f"CATEGORY: {doc['category']}\n\n"
        f"SOURCE: {doc['source']}\n"
        f"SOURCE URL: {doc['source_url']}\n\n"
        f"SUMMARY: {doc['summary']}\n\n"
        f"ARCHITECTURE NOTES: {doc['architecture_notes']}\n\n"
        f"REQUEST FLOW: {doc['request_flow']}\n\n"
        f"DATA FLOW: {doc['data_flow']}\n\n"
        f"TAGS: {doc['tags']}"
    )


def seed_architecture_reference_corpus():
    """Ingest curated architecture reference material into devops_patterns_v1."""
    logger.info(f"Seeding {len(ARCHITECTURE_REFERENCE_DOCS)} architecture references into '{COLLECTION_PATTERNS}'...")
    ok = 0
    fail = 0
    for doc in ARCHITECTURE_REFERENCE_DOCS:
        text = _architecture_reference_to_text(doc)
        meta = {
            "name":       doc["title"][:200],
            "category":   doc["category"],
            "tags":       doc["tags"],
            "source":     doc["source"],
            "source_url": doc["source_url"],
            "collection": COLLECTION_PATTERNS,
        }
        if ingest_text_to_collection(COLLECTION_PATTERNS, text, meta):
            ok += 1
        else:
            fail += 1
        time.sleep(0.1)
    logger.info(f"Architecture reference corpus seed complete: {ok} ingested, {fail} failed")
    return ok, fail


def seed_fixes_collection():
    """Ingest all 20 seed fixes into devops_fixes_v1."""
    logger.info(f"Seeding {len(SEED_FIXES)} fixes into '{COLLECTION_FIXES}'...")
    ok = 0
    fail = 0
    for fix in SEED_FIXES:
        text = _fix_to_text(fix)
        meta = {
            "error":      fix["error"][:200],
            "tags":       ", ".join(fix.get("tags", [])),
            "source":     "ava_seed_fixes_v1",
            "collection": COLLECTION_FIXES,
        }
        if ingest_text_to_collection(COLLECTION_FIXES, text, meta):
            ok += 1
        else:
            fail += 1
        time.sleep(0.1)  # brief pause between embeddings
    logger.info(f"Seed complete: {ok} ingested, {fail} failed")
    return ok, fail


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*60)
    print("AVA Phase 5A — Collection Setup")
    print("="*60)

    # Step 1: ensure both new collections exist
    client = _get_client()
    for name, desc in [
        (COLLECTION_PATTERNS, "infrastructure patterns, runbooks, best practices"),
        (COLLECTION_FIXES,    "error fixes, troubleshooting solutions, incident resolutions"),
    ]:
        col = client.get_or_create_collection(
            name=name,
            metadata={"description": desc, "created_by": "phase5a_ingestor.py", "phase": "5A"}
        )
        logger.info(f"Collection ready: '{name}' ({col.count()} existing chunks)")

    # Step 2: seed fixes
    print("\n--- Seeding devops_fixes_v1 ---")
    ok, fail = seed_fixes_collection()

    # Step 2b: seed patterns
    print("\n--- Seeding devops_patterns_v1 ---")
    ok_p, fail_p = seed_patterns_collection()

    # Step 2c: seed AVA self-architecture
    print("\n--- Seeding AVA architecture into devops_patterns_v1 ---")
    ok_a, fail_a = seed_ava_architecture()

    # Step 2d: seed architecture-focused expert patterns
    print("\n--- Seeding architecture expert patterns into devops_patterns_v1 ---")
    ok_arch, fail_arch = seed_architecture_patterns_collection()

    # Step 2e: seed curated architecture reference corpus
    print("\n--- Seeding architecture reference corpus into devops_patterns_v1 ---")
    ok_ref, fail_ref = seed_architecture_reference_corpus()

    # Step 3: stats
    print("\n--- Collection Stats ---")
    stats = get_collection_stats()
    print("\nSummary:")
    total = 0
    for name, count in stats.items():
        print(f"  {name:<32} {count:>6} chunks")
        total += count
    print(f"  {'TOTAL':<32} {total:>6} chunks")
    print("="*60 + "\n")
