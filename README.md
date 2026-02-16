# ContextEngine

**The memory system that actually thinks about what it remembers.**

ContextEngine is a self-contained, recursive context management system for AI assistants. Unlike passive RAG systems that just embed and retrieve, ContextEngine uses an LLM-in-the-loop pipeline to evaluate, triage, compress, and curate information — giving your AI assistant a coherent, always-current understanding of your work.

## How It Works

```
You ←→ AI Assistant ←→ ContextEngine
                          ├── Master Context (hot, always loaded)
                          ├── ChromaDB Archive (warm, searched on demand)
                          └── Raw Sessions (cold, reprocessing/forensics)
```

Every conversation is saved as a session. A background worker triages each session through an LLM pipeline that:

1. **Summarizes** the session into structured data
2. **Extracts** entities (people, projects, services), decisions, and failures
3. **Archives** compressed data into ChromaDB collections
4. **Detects patterns** across sessions (recurring topics, technology preferences, risk areas)
5. **Generates nudges** (follow-up reminders, contradiction warnings, stale context alerts)
6. **Flags anomalies** (regressions, scope drift, escalating issues)
7. **Compresses** the master context document when it grows too large

The result: your AI assistant starts every conversation with a curated briefing of who you are, what you're working on, recent decisions, known issues, and proactive nudges — not a bag of embedding fragments.

## Quick Start

### Prerequisites
- Docker and Docker Compose
- One of:
  - **OpenRouter API key** (~$3-5/month) — [get one here](https://openrouter.ai/keys)
  - **Ollama installed locally** (free) — [install here](https://ollama.ai)

### Install

```bash
git clone https://github.com/your-org/context-engine.git
cd context-engine

# Configure
cp .env.example .env
# Edit .env with your preferred LLM backend + credentials

# Launch
docker compose -f docker-compose.product.yml up -d
```

ContextEngine will be running at `http://localhost:9040`.

### Verify

```bash
curl http://localhost:9040/api/health
```

Open `http://localhost:9040/dashboard` for the web UI.

### Connect to Claude Desktop

Add to your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "context-engine": {
      "url": "http://localhost:9040/mcp"
    }
  }
}
```

Or use the SSE transport:

```json
{
  "mcpServers": {
    "context-engine": {
      "transport": "sse",
      "url": "http://localhost:9040/sse"
    }
  }
}
```

### Using with Ollama (Free, Local)

```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Pull required models
ollama pull llama3.2:3b    # Light tasks (summaries, extraction)
ollama pull llama3.1:8b    # Heavy tasks (compression, patterns)

# Configure .env
LLM_BACKEND=ollama
OLLAMA_URL=http://host.docker.internal:11434
```

## Architecture

### Three-Tier Memory

| Tier | Storage | Size | Access |
|------|---------|------|--------|
| **Hot** | Master context document | ~2K tokens | Always loaded, every conversation |
| **Warm** | ChromaDB vector archive | Unlimited | Semantic search on demand |
| **Cold** | Raw session files + transcripts | Unlimited | Reprocessing, forensics, backup |

### Worker Pipeline

The background worker processes sessions at a rate-limited pace (1/minute) through these steps:

1. Session summary (Haiku)
2. Entity extraction (Haiku)
3. Decision extraction (Sonnet)
4. Failure extraction (Haiku)
5. Triage classification (Haiku)
6. ChromaDB archival
7. Pattern detection (every 5th session, Sonnet)
8. Nudge generation (every 3rd session, Haiku)
9. Anomaly detection (every 4th session, Haiku)
10. Master context compression (when over budget, Sonnet)

### Graceful Degradation

ContextEngine continues operating when dependencies fail:

- **KB mount gone?** Falls back to local file, then in-memory cache
- **ChromaDB down?** Reads from cache, queues writes for recovery
- **LLM provider down?** Circuit breaker pauses worker, re-queues sessions
- **Everything down?** Serves last known good context from memory

Four degradation levels: `full` → `partial` → `minimal` → `offline`

### Self-Bootstrap

Fresh install or data loss? ContextEngine can rebuild:

- `POST /api/bootstrap/scaffold` — Create minimal starter context
- `POST /api/bootstrap/reprocess` — Re-run worker on raw session files
- `POST /api/bootstrap/rebuild-master` — Synthesize master context from ChromaDB archive

## API Reference

### MCP Tools

| Tool | Description |
|------|-------------|
| `context_load` | Load full context (master + archive search + nudges) |
| `context_save` | Save session with structured data or brief note |
| `context_checkpoint` | Lightweight mid-session save |
| `context_search` | Search ChromaDB archive |
| `context_correct` | Fix incorrect information |

### Internal Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check with degradation level |
| `GET /api/summary` | Master context for auto-injection |
| `GET /api/stats` | Collection sizes, session counts, LLM stats |
| `GET /api/nudges` | Active nudges |
| `GET /api/anomalies` | Active anomaly flags |
| `GET /api/degradation` | Dependency health + circuit breaker status |
| `GET /api/bootstrap/status` | Bootstrap readiness assessment |
| `POST /api/backup/create` | Create backup (local + MinIO) |
| `GET /api/backup/list` | List available backups |
| `POST /api/backup/restore` | Restore from backup |
| `GET /dashboard` | Web dashboard UI |

## Configuration

All configuration via environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `openrouter` | `openrouter` or `ollama` |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama server URL |
| `STANDALONE_MODE` | `false` | Skip external KB mount |
| `LEARNING_MODE` | `true` | Log-only mode (no context updates) |
| `CE_PORT` | `9040` | External port mapping |

## Cost

With OpenRouter (Haiku + Sonnet):
- **Per session:** ~$0.03-0.05
- **Monthly (daily use):** ~$3-5
- **With Ollama:** $0 (runs on your hardware)

## What It's NOT

ContextEngine is not a general-purpose RAG system for querying document collections. It's specifically a **conversational memory and context management system** for ongoing AI assistant collaboration.

## License

MIT
