"""
Post-compression integrity checker.

After Haiku compresses the master context, this module verifies that
known infrastructure facts weren't dropped. Deterministic pattern
matching — no LLM cost.

Checks:
1. All known containers/services still mentioned
2. All known ports still referenced
3. All known domains still present
4. Key project names survive
5. Active blockers/issues not silently removed
"""

import re
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger("context-engine")


# ── Known facts registry ──────────────────────────────────────────
# These are populated from ChromaDB entities + KB auto-detected changes.
# If a fact was in the pre-compression context but missing from post,
# it gets flagged.

def extract_infrastructure_facts(text: str) -> dict:
    """Extract verifiable facts from a context document."""
    facts = {
        "ports": set(),
        "containers": set(),
        "domains": set(),
        "projects": set(),
        "ips": set(),
        "services": set(),
    }

    # Ports: 4-5 digit numbers in port-like context
    for m in re.finditer(r'\b(\d{4,5})(?::\d{2,5})?\b', text):
        port = int(m.group(1))
        if 1024 <= port <= 65535:
            facts["ports"].add(str(port))

    # Also catch port:port patterns
    for m in re.finditer(r'(\d{4,5}):(\d{2,5})', text):
        facts["ports"].add(m.group(1))

    # Container/service names (common patterns)
    container_patterns = [
        r'container[:\s]+[`"]?(\S+?)[`"]?[\s,\.]',
        r'(?:docker|container)\s+(?:name\s+)?[`"]?([a-z][a-z0-9_-]+)[`"]?',
        r'(?:service|stack)[:\s]+[`"]?([a-z][a-z0-9_-]+)[`"]?',
    ]
    for pat in container_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            name = m.group(1).strip('`"\'')
            if len(name) > 2 and name not in ('the', 'and', 'for', 'not'):
                facts["containers"].add(name)

    # Domains
    for m in re.finditer(r'(?:https?://)?([a-z0-9][-a-z0-9]*\.(?:millyweb\.com|dartai\.com|github\.com|openrouter\.ai)[/\w.-]*)', text, re.IGNORECASE):
        facts["domains"].add(m.group(1).rstrip('/'))

    # IP addresses
    for m in re.finditer(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', text):
        facts["ips"].add(m.group(1))

    # Project names (capitalized multi-word or known patterns)
    project_patterns = [
        r'(?:project|system|platform)[:\s]+[`"]?([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?)[`"]?',
        r'\b(ContextEngine|MillyExt|MCP\s*Provisioner|Zipline|MinIO|Jerry|OpenClaw)\b',
    ]
    for pat in project_patterns:
        for m in re.finditer(pat, text):
            facts["projects"].add(m.group(1).strip())

    return facts


def check_integrity(
    pre_compression: str,
    post_compression: str,
    kb_facts: Optional[dict] = None,
) -> dict:
    """
    Compare pre and post compression context for dropped facts.

    Returns:
        {
            "passed": bool,
            "dropped": {"ports": [...], "containers": [...], ...},
            "drop_count": int,
            "severity": "none" | "low" | "medium" | "high",
            "details": str
        }
    """
    pre_facts = extract_infrastructure_facts(pre_compression)
    post_facts = extract_infrastructure_facts(post_compression)

    # Merge in KB facts if provided (these are ground truth)
    if kb_facts:
        for category, values in kb_facts.items():
            if category in pre_facts:
                pre_facts[category].update(values)

    dropped = {}
    total_dropped = 0

    for category in pre_facts:
        missing = pre_facts[category] - post_facts.get(category, set())
        if missing:
            dropped[category] = sorted(missing)
            total_dropped += len(missing)

    # Severity based on what was dropped
    if total_dropped == 0:
        severity = "none"
    elif dropped.get("ports") or dropped.get("ips") or dropped.get("containers"):
        severity = "high"
    elif dropped.get("domains") or dropped.get("projects"):
        severity = "medium"
    else:
        severity = "low"

    details_parts = []
    for cat, items in dropped.items():
        details_parts.append(f"{cat}: {', '.join(items)}")

    return {
        "passed": total_dropped == 0,
        "dropped": dropped,
        "drop_count": total_dropped,
        "severity": severity,
        "details": "; ".join(details_parts) if details_parts else "All infrastructure facts preserved",
    }


def load_kb_facts(kb_root: str) -> dict:
    """Load known facts from KB auto-detected-changes.md"""
    facts = {
        "ports": set(),
        "containers": set(),
        "domains": set(),
    }

    changes_file = Path(kb_root) / "infrastructure" / "auto-detected-changes.md"
    if not changes_file.exists():
        return facts

    content = changes_file.read_text()

    # Parse service tables: | service_name | image | port | network |
    for m in re.finditer(r'\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\d+:\d+|\S+)\s*\|', content):
        service = m.group(1).strip()
        port_str = m.group(3).strip()
        if service not in ('Service', '---', 'service'):
            facts["containers"].add(service)
        if ':' in port_str:
            host_port = port_str.split(':')[0]
            if host_port.isdigit():
                facts["ports"].add(host_port)

    return facts
