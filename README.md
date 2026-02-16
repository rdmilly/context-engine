# ContextEngine

**The memory system that actually thinks about what it remembers.**

ContextEngine is a self-contained, recursive context management system for AI assistants. Unlike passive RAG systems that just embed and retrieve, ContextEngine uses an LLM-in-the-loop pipeline to evaluate, triage, compress, and curate information — giving your AI assistant a coherent, always-current understanding of your work.

## How It Works

```
You ←→ AI Assistant ←→ ContextEngine
                          ├── Master Context (hot, always loaded)
                          ├── ChromaDB Archive (warm, searched on demand)
                          ├── File Watcher (auto-detects infrastructure changes)
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
- An LLM provider (any OpenAI-compatible API — see below)

### Install

```bash
git clone https://github.com/rdmilly/context-engine.git
cd context-engine

# Option A: Configure via .env file
cp .env.example .env
# Edit .env with your LLM provider + API key

# Option B: Skip .env — configure via the web dashboard after launch

# Launch
docker compose -f docker-compose.product.yml up -d
```

ContextEngine will be running at `http://localhost:9040`.

Open `http://localhost:9040/dashboard` → click **⚙ Settings** to configure your LLM provider, file watcher, and notifications from the UI.

### Supported LLM Providers

ContextEngine works with **any OpenAI-compatible API**. Pick one:

| Provider | Base URL | Cost | Notes |
|----------|----------|------|-------|
| [OpenRouter](https://openrouter.ai/keys) | `https://openrouter.ai/api/v1` | ~$1-3/mo | 200+ models, recommended |
| [OpenAI](https://platform.openai.com/api-keys) | `https://api.openai.com/v1` | ~$2-5/mo | Direct access |
| [Groq](https://console.groq.com/keys) | `https://api.groq.com/openai/v1` | Free tier | Ultra-fast inference |
| [Together AI](https://api.together.xyz/settings/api-keys) | `https://api.together.xyz/v1` | ~$1-3/mo | Fast open models |
| [Ollama](https://ollama.com/download) | `http://host.docker.internal:11434/v1` | Free | Local, no API key needed |
| [LM Studio](https://lmstudio.ai/) | `http://host.docker.internal:1234/v1` | Free | Local GUI |

### Verify

```bash
curl http://localhost:9040/api/health
```

### Connect to Claude Desktop

Add to your Claude Desktop MCP config:

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

## Features

### Web Dashboard

Eight-tab dashboard at `/dashboard`:

- **Overview** — system health, session counts, degradation status
- **Master Context** — view/edit the hot context document
- **Sessions** — browse processed sessions
- **Archive Search** — semantic search across ChromaDB
- **Entities** — tracked people, projects, services, tools
- **Nudges & Anomalies** — proactive alerts and integrity flags
- **Backups** — create/restore backups (local + S3)
- **⚙ Settings** — configure LLM provider, file watcher, notifications (hot-reloads, no restart needed)

### File Watcher

Built-in infrastructure change detection. Mount a directory and ContextEngine will:

- **Auto-commit** every file change to git (10s debounce)
- **Parse compose files** — extract services, ports, images, networks → write directly to KB
- **Detect credentials** — alert on passwords/API keys, never send them to the LLM
- **Register new services** — auto-detect new stack/project directories
- **Transcript drop zone** — drop conversation transcripts for automatic Haiku processing

Enable by setting `WATCH_DIRS` or via the Settings tab.

### Three-Tier Memory

| Tier | Storage | Size | Access |
|------|---------|------|--------|
| **Hot** | Master context document | ~2K tokens | Always loaded, every conversation |
| **Warm** | ChromaDB vector archive | Unlimited | Semantic search on demand |
| **Cold** | Raw session files + transcripts | Unlimited | Reprocessing, forensics, backup |

### Worker Pipeline

The background worker processes sessions at a rate-limited pace (1/minute):

1. Session summary extraction
2. Entity extraction (people, projects, services, tools)
3. Decision extraction
4. Failure extraction
5. Triage classification (keep/archive/merge/discard)
6. ChromaDB archival
7. Pattern detection (every 5th session)
8. Nudge generation (every 3rd session)
9. Anomaly detection (every 4th session)
10. Master context compression (when over token budget)

All steps run on Haiku-class models by default (~$0.01-0.03 per session).

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

### REST Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/health` | Health check with degradation level |
| `GET /api/stats` | Collection sizes, session counts, LLM stats |
| `GET /api/nudges` | Active nudges |
| `GET /api/anomalies` | Active anomaly flags |
| `GET /api/settings` | Current configuration |
| `POST /api/settings` | Update configuration (hot-reload) |
| `POST /api/settings/test-llm` | Test LLM connection |
| `GET /api/settings/presets` | LLM provider presets |
| `POST /api/backup/create` | Create backup (local + S3) |
| `GET /api/backup/list` | List available backups |
| `POST /api/backup/restore` | Restore from backup |
| `GET /dashboard` | Web dashboard |

## Configuration

Configure via `.env` file, environment variables, or the dashboard Settings tab.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Any OpenAI-compatible endpoint |
| `LLM_API_KEY` | — | API key (leave empty for local models) |
| `LLM_MODEL_FAST` | `anthropic/claude-haiku-4.5` | Model for extraction, summaries |
| `LLM_MODEL_SMART` | `anthropic/claude-haiku-4.5` | Model for triage, compression |
| `WATCH_DIRS` | — | Comma-separated dirs to watch |
| `WATCH_TRANSCRIPT_DIR` | — | Transcript drop zone directory |
| `TELEGRAM_BOT_TOKEN` | — | Telegram alerts bot token |
| `TELEGRAM_CHAT_ID` | — | Telegram chat ID |
| `STANDALONE_MODE` | `false` | Skip external KB mount |
| `LEARNING_MODE` | `true` | Log-only mode (no context updates) |
| `CE_PORT` | `9040` | External port mapping |

## Cost

- **Haiku on OpenRouter:** ~$0.01-0.03 per session, ~$1-3/month with daily use
- **Groq free tier:** $0
- **Ollama / LM Studio:** $0 (runs on your hardware)

## What It's NOT

ContextEngine is not a general-purpose RAG system for querying document collections. It's specifically a **conversational memory and context management system** for ongoing AI assistant collaboration.

## License

MIT
