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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s"
)
logger = logging.getLogger("ava.phase5a_ingestor")

# ── Config ────────────────────────────────────────────────────────────────────
CHROMA_PATH   = os.getenv("CHROMA_PATH",  "/home/manoj/ava-data/chroma_db")
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
