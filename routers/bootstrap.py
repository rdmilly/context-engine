"""Bootstrap router — rebuild ContextEngine state from available data.

Enables recovery from:
- Fresh install (no master context, empty ChromaDB)
- Data loss (ChromaDB wiped, master context missing)
- Migration (moving to new host)

Bootstrap sources (in priority order):
1. Session files in cold storage — reprocess through worker
2. Existing ChromaDB data — rebuild master context from archive
3. Empty state — create minimal master context scaffold
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter

from config import SESSIONS_DIR
from services import kb_gateway, chromadb_client
from services.openrouter import get_client
from worker.processor import get_processor
from utils.degradation import get_manager as get_degradation_manager
from utils.logging_ import logger

router = APIRouter()


@router.get("/api/bootstrap/status")
async def bootstrap_status():
    """Check what data is available for bootstrap."""
    dm = get_degradation_manager()

    # Count session files
    all_sessions = list(SESSIONS_DIR.glob("*.json"))
    processed = 0
    unprocessed = 0
    for sf in all_sessions:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if data.get("_processed"):
                processed += 1
            else:
                unprocessed += 1
        except Exception:
            pass

    # Check ChromaDB collections
    collection_counts = {}
    try:
        client = chromadb_client.get_chromadb()
        for name in ["sessions", "project_archive", "decisions", "failures", "entities", "patterns"]:
            try:
                col = client.get_collection(name)
                collection_counts[name] = col.count()
            except Exception:
                collection_counts[name] = 0
    except Exception:
        collection_counts = {"error": "ChromaDB not available"}

    # Check master context
    mc = kb_gateway.read_master_context()

    return {
        "master_context_exists": mc is not None,
        "master_context_size": len(mc) if mc else 0,
        "cache_available": dm.get_cached_context() is not None,
        "session_files": {
            "total": len(all_sessions),
            "processed": processed,
            "unprocessed": unprocessed,
        },
        "chromadb_collections": collection_counts,
        "degradation_level": dm.level.value,
        "recommendation": _get_recommendation(mc, len(all_sessions), unprocessed, collection_counts),
    }


def _get_recommendation(mc, total_sessions, unprocessed, collections):
    """Suggest the best bootstrap action."""
    if mc and total_sessions > 0:
        if unprocessed > 0:
            return f"System healthy. {unprocessed} unprocessed sessions can be queued."
        return "System fully operational. No bootstrap needed."
    if not mc and total_sessions > 0:
        return "Master context missing. Run /api/bootstrap/rebuild-master to regenerate from ChromaDB archive."
    if not mc and total_sessions == 0:
        return "Fresh install detected. Run /api/bootstrap/scaffold to create initial structure."
    return "Run /api/bootstrap/reprocess to rebuild from session files."


@router.post("/api/bootstrap/reprocess")
async def reprocess_sessions(limit: int = 50):
    """Queue unprocessed session files for worker reprocessing.

    This rebuilds ChromaDB archive from raw session data.
    """
    all_sessions = sorted(SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    queued = 0
    processor = get_processor()

    for sf in all_sessions[:limit]:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if data.get("_processed"):
                continue
            session_id = data.get("session_id", sf.stem)
            processor.enqueue(session_id, str(sf))
            queued += 1
        except Exception as e:
            logger.warning(f"Bootstrap: failed to queue {sf.name}: {e}")

    return {
        "queued": queued,
        "total_available": len(all_sessions),
        "message": f"Queued {queued} sessions for reprocessing. Worker will process at rate limit.",
    }


@router.post("/api/bootstrap/rebuild-master")
async def rebuild_master():
    """Rebuild master context from ChromaDB archive data.

    Uses LLM to synthesize a new master context from:
    - Recent sessions
    - Archived project data
    - Decisions
    - Entities
    """
    dm = get_degradation_manager()

    # Gather data from ChromaDB
    sources = {}
    try:
        sources["sessions"] = chromadb_client.search_collection("sessions", "recent work projects", n_results=20)
        sources["archive"] = chromadb_client.search_collection("project_archive", "active projects infrastructure", n_results=15)
        sources["decisions"] = chromadb_client.search_collection("decisions", "architecture deployment", n_results=15)
        sources["entities"] = chromadb_client.search_collection("entities", "people services projects", n_results=20)
    except Exception as e:
        return {"error": f"ChromaDB not available: {e}"}

    total_items = sum(len(v) for v in sources.values())
    if total_items == 0:
        return {"error": "No data in ChromaDB. Run /api/bootstrap/reprocess first."}

    # Build prompt for master context generation
    parts = []
    parts.append("You are rebuilding a master context document from archived data.")
    parts.append("Generate a comprehensive markdown document organized by: Active Projects, Infrastructure State, Recent Decisions, Known Issues.")
    parts.append("Be specific and technical. Include version numbers, ports, container names, etc.")
    parts.append("")

    for source_name, items in sources.items():
        if items:
            parts.append(f"## Data from {source_name}:")
            for item in items[:15]:
                content = item.get("content", "")[:500]
                parts.append(f"- {content}")
            parts.append("")

    parts.append("Generate the master context markdown now. Start with '# ContextEngine — Master Context'.")

    try:
        llm = get_client()
        result = await asyncio.to_thread(
            llm._call,
            llm._get_model("master_compression"),
            [{"role": "user", "content": "\n".join(parts)}]
        )

        if result and result.get("choices"):
            new_master = result["choices"][0]["message"]["content"]
            # Add header
            timestamp = datetime.now(timezone.utc).strftime("%B %d, %Y")
            if not new_master.startswith("# ContextEngine"):
                new_master = f"# ContextEngine — Master Context\n**Last Updated:** {timestamp} (Bootstrap rebuild)\n\n{new_master}"

            # Write to KB
            written = kb_gateway.write_master_context(new_master, "ContextEngine: bootstrap rebuild master context")
            dm.update_cache(new_master, source="bootstrap")

            return {
                "success": True,
                "written_to_kb": written,
                "size_bytes": len(new_master),
                "sources_used": {k: len(v) for k, v in sources.items()},
                "message": "Master context rebuilt from ChromaDB archive.",
            }
        else:
            return {"error": "LLM call returned no content"}
    except Exception as e:
        return {"error": f"Bootstrap rebuild failed: {e}"}


@router.post("/api/bootstrap/scaffold")
async def scaffold():
    """Create a minimal master context for fresh installs.

    This creates a bare-bones structure that will be filled as sessions are processed.
    """
    timestamp = datetime.now(timezone.utc).strftime("%B %d, %Y")
    scaffold_content = f"""# ContextEngine — Master Context
**Last Updated:** {timestamp} (Fresh install scaffold)
**System Status:** Bootstrapping — awaiting first session data

## Active Projects
*No projects tracked yet. Context will build automatically as sessions are processed.*

## Infrastructure State
*Infrastructure details will be populated from session data.*

## Recent Decisions
*No decisions recorded yet.*

## Known Issues
*No known issues.*

## Nudges
*Nudge generation will activate after sufficient session data is available.*
"""

    written = kb_gateway.write_master_context(scaffold_content, "ContextEngine: fresh install scaffold")
    dm = get_degradation_manager()
    dm.update_cache(scaffold_content, source="bootstrap")

    return {
        "success": True,
        "written_to_kb": written,
        "message": "Scaffold created. Process sessions to build context.",
    }
