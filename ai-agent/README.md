# DevOps AI Agent

A fully local AI-powered DevOps assistant built on top of the agentic-safety-gate policy engine.

## Features

- Local LLM inference using Ollama (llama3.1:8b) on RTX 5060 Ti
- RAG pipeline with ChromaDB and nomic-embed-text embeddings
- 17 documents indexed (OPA policies, Terraform, Docker, test scenarios)
- Shell access with safety whitelist
- Persistent chat history within session
- Live token counter
- Domain restricted to DevOps/infrastructure questions only
- Claude-style web UI at localhost:5000

## Architecture
```
User Question
     ↓
nomic-embed-text (retrieval)
     ↓
ChromaDB vector search (31 chunks)
     ↓
Always anchors on infrastructure.rego
     ↓
llama3.1:8b reasoning (GPU accelerated)
     ↓
Shell execution if needed (whitelisted commands)
     ↓
Answer
```

## Stack

- Ollama — local LLM inference
- ChromaDB — vector database
- Flask — web server
- nomic-embed-text — document embeddings
- llama3.1:8b — reasoning model

## Setup
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b
ollama pull nomic-embed-text

# Install dependencies
python3 -m venv venv
source venv/bin/activate
pip install ollama chromadb flask langchain langchain-community langchain-ollama

# Index knowledge base
python3 index_knowledge_v2.py

# Run the agent
python3 web_agent.py
```

Open http://localhost:5000 in your browser.

## Hardware

Tested on:
- GPU: RTX 5060 Ti 16GB
- CPU: AMD Ryzen 5 1600
- RAM: 32GB
- OS: WSL2 Ubuntu on Windows
