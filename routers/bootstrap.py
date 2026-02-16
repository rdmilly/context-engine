"""Bootstrap router - rebuild ContextEngine state from available data."""

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
    dm = get_degradation_manager()
    all_sessions = list(SESSIONS_DIR.glob("*.json"))
    processed = unprocessed = 0
    for sf in all_sessions:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if data.get("_processed"): processed += 1
            else: unprocessed += 1
        except: pass
    collection_counts = {}
    try:
        client = chromadb_client.get_chromadb()
        for name in ["sessions", "project_archive", "decisions", "failures", "entities", "patterns"]:
            try: collection_counts[name] = client.get_collection(name).count()
            except: collection_counts[name] = 0
    except: collection_counts = {"error": "ChromaDB not available"}
    mc = kb_gateway.read_master_context()
    return {"master_context_exists": mc is not None, "master_context_size": len(mc) if mc else 0, "cache_available": dm.get_cached_context() is not None, "session_files": {"total": len(all_sessions), "processed": processed, "unprocessed": unprocessed}, "chromadb_collections": collection_counts, "degradation_level": dm.level.value}


@router.post("/api/bootstrap/reprocess")
async def reprocess_sessions(limit: int = 50):
    all_sessions = sorted(SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    queued = 0
    processor = get_processor()
    for sf in all_sessions[:limit]:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            if data.get("_processed"): continue
            processor.enqueue(data.get("session_id", sf.stem), str(sf))
            queued += 1
        except Exception as e:
            logger.warning(f"Bootstrap: failed to queue {sf.name}: {e}")
    return {"queued": queued, "total_available": len(all_sessions), "message": f"Queued {queued} sessions for reprocessing."}


@router.post("/api/bootstrap/rebuild-master")
async def rebuild_master():
    dm = get_degradation_manager()
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
    parts = ["You are rebuilding a master context document from archived data.", "Generate comprehensive markdown organized by: Active Projects, Infrastructure State, Recent Decisions, Known Issues.", ""]
    for source_name, items in sources.items():
        if items:
            parts.append(f"## Data from {source_name}:")
            for item in items[:15]:
                parts.append(f"- {item.get('content', '')[:500]}")
            parts.append("")
    parts.append("Generate the master context markdown now.")
    try:
        llm = get_client()
        result = await asyncio.to_thread(llm._call, llm._get_model("master_compression"), [{"role": "user", "content": "\n".join(parts)}])
        if result and result.get("choices"):
            new_master = result["choices"][0]["message"]["content"]
            timestamp = datetime.now(timezone.utc).strftime("%B %d, %Y")
            if not new_master.startswith("# ContextEngine"):
                new_master = f"# ContextEngine \u2014 Master Context\n**Last Updated:** {timestamp} (Bootstrap rebuild)\n\n{new_master}"
            written = kb_gateway.write_master_context(new_master, "ContextEngine: bootstrap rebuild")
            dm.update_cache(new_master, source="bootstrap")
            return {"success": True, "written_to_kb": written, "size_bytes": len(new_master), "sources_used": {k: len(v) for k, v in sources.items()}}
        return {"error": "LLM call returned no content"}
    except Exception as e:
        return {"error": f"Bootstrap rebuild failed: {e}"}


@router.post("/api/bootstrap/scaffold")
async def scaffold():
    timestamp = datetime.now(timezone.utc).strftime("%B %d, %Y")
    scaffold_content = f"""# ContextEngine \u2014 Master Context\n**Last Updated:** {timestamp} (Fresh install scaffold)\n**System Status:** Bootstrapping\n\n## Active Projects\n*No projects tracked yet.*\n\n## Infrastructure State\n*Will be populated from session data.*\n\n## Recent Decisions\n*No decisions recorded yet.*\n\n## Known Issues\n*No known issues.*\n"""
    written = kb_gateway.write_master_context(scaffold_content, "ContextEngine: fresh install scaffold")
    get_degradation_manager().update_cache(scaffold_content, source="bootstrap")
    return {"success": True, "written_to_kb": written, "message": "Scaffold created."}
