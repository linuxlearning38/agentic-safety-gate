# Agentic Safety Gate

A complete AI-powered infrastructure policy enforcement platform built locally.

## What this project does

Combines an OPA policy engine with a local LLM agent to enforce infrastructure compliance and answer DevOps questions — all running 100% offline on a local GPU.

## Components

### Policy Engine (`/policies`, `/tests`, `gatekeeper.py`)
- OPA-based policy enforcement with 10 violation rules
- Terraform plan evaluation before deployment
- PostgreSQL audit logging
- Docker containerized with CI/CD via GitHub Actions

### AI Agent (`/ai-agent`)
- Local LLM inference using Ollama (llama3.1:8b)
- RAG pipeline with ChromaDB and nomic-embed-text embeddings
- Shell access with safety whitelist
- Claude-style web UI at localhost:5000
- Domain restricted to DevOps/infrastructure questions

## Stack
`Python` `OPA/Rego` `Docker` `Terraform` `Ollama` `ChromaDB` `Flask` `LangChain`
