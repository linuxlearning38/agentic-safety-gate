# AVA — Autonomous DevOps AI Agent

## Overview
AVA is a production-grade autonomous DevOps AI assistant built on local LLMs.
Fully offline, no data leaves the machine. Built by Manoj, Delhi.

## Stack
- LLM: qwen2.5:14b (Q4_K_M) via Ollama
- Embedding: nomic-embed-text
- Vision: llava:13b
- Vector DB: ChromaDB (8,780 chunks, 4 collections)
- Backend: Flask/Gunicorn (HTTPS :5443)
- Security: OPA + JWT + TLS + rate limiting
- Storage: SQLite + ChromaDB
- Runtime: Docker Compose (5 containers)

## Containers
| Container | Port | Purpose |
|-----------|------|---------|
| ava-agent | 5443 | Main Flask/Gunicorn app (HTTPS) |
| agent_postgres | 5432 | PostgreSQL 15 |
| agent_redis | 6379 | Redis 7 |
| agent_opa | 8181 | Open Policy Agent |
| agent_vault | 8200 | HashiCorp Vault |

## Quick Start
```bash
# 1. Start Ollama
OLLAMA_HOST=0.0.0.0 OLLAMA_MODELS=/mnt/i/ai-lab/models ollama serve

# 2. Start AVA
docker compose up -d

# 3. Get token
curl -sk https://localhost:5443/auth/login \
  -X POST -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<YOUR_ADMIN_PASSWORD>"}'

# 4. Ask AVA
curl -sk https://localhost:5443/ask \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is your architecture?"}'
```

## Endpoints
| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| /health | GET | None | Health + dependency check |
| /about | GET | None | System info + KB stats |
| /auth/login | POST | None | Get JWT token |
| /ask | POST | JWT | Main query endpoint |
| /webhook | POST | Secret | Alert ingestion |
| /heal | POST | Admin | Manual healing trigger |
| /healing/history | GET | JWT | Healing audit trail |

## Features
- RAG over 8,780 DevOps knowledge chunks
- Self-healing: auto-detect + fix K8s/infra issues
- Memory: store and recall user context
- Webhook: Alertmanager/Datadog/PagerDuty integration
- Multi-turn conversation
- Mermaid diagram generation
- Image analysis (llava:13b)
- Hallucination guard
- OPA policy enforcement
- JWT + TLS security

## Knowledge Base
| Collection | Chunks | Content |
|-----------|--------|---------|
| devops_policies_v2 | 3,881 | K8s, Docker, AWS policies |
| devops_blogs_v1 | 4,822 | Engineering blog articles |
| devops_patterns_v1 | 57 | Infrastructure patterns |
| devops_fixes_v1 | 20 | Real DevOps fixes |

## Project Structure
```text
devops-agent/
├── web_agent_v2.1_guardrail.py  # Main app (1800+ lines)
├── control/
│   ├── auth.py                  # JWT authentication
│   ├── database.py              # SQLite layer
│   ├── self_healer.py           # Self-healing engine
│   ├── monitor.py               # Background monitor
│   ├── react_loop.py            # ReAct reasoning
│   ├── security_layer.py        # OPA integration
│   └── registry.py              # Tool registry
├── knowledge_updater/
│   ├── hybrid_retrieval.py      # 4-collection RAG
│   ├── ingestor.py              # RSS + blog ingestion
│   └── phase5a_ingestor.py      # Phase 5A collections
├── tests/
│   └── intelligence_regression.py  # 18-test regression suite
└── docker-compose.yml
```

## Security Layers
1. TLS 1.3 (HTTPS only)
2. JWT authentication (24h tokens)
3. Rate limiting (30 req/min)
4. OPA policy gate
5. Command whitelist (shell=False)
6. Confidence-based execution
7. Hallucination guard
