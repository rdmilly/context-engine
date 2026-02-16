"""Infrastructure change detector.

Tier 1 processing — no LLM involved. Detects infrastructure-significant
changes from file diffs and writes structured data directly to the KB.

Handles:
- Compose file changes → parse YAML → extract services/ports/images → KB
- Credential detection → alert, never send to LLM
- New stack/project directories → register in KB
"""

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logging_ import logger

# Credential patterns (never send these to LLM)
CREDENTIAL_PATTERNS = [
    (r'(?:password|passwd|pwd)\s*[=:]\s*\S+', 'password'),
    (r'(?:api[_-]?key|apikey)\s*[=:]\s*\S+', 'api_key'),
    (r'(?:secret[_-]?key|secret)\s*[=:]\s*\S+', 'secret'),
    (r'(?:access[_-]?key|token)\s*[=:]\s*\S+', 'token'),
    (r'(?:database[_-]?url|db[_-]?url|postgres://|mysql://|mongodb://)\S+', 'database_url'),
    (r'sk-[a-zA-Z0-9\-_]{20,}', 'api_key_pattern'),
    (r'ghp_[a-zA-Z0-9]{36}', 'github_token'),
    (r'xoxb-[a-zA-Z0-9\-]+', 'slack_token'),
]

# Files that likely contain credentials
CREDENTIAL_FILES = {'.env', '.env.local', '.env.production', 'secrets.yml', 'credentials.json'}


def analyze_changes(changed_files: list[str], git_root: str) -> dict:
    """Analyze a batch of changed files and return structured findings.
    
    Returns:
        {
            "compose_changes": [...],  # Parsed service info from compose files
            "credential_alerts": [...],  # Detected credentials (masked)
            "new_directories": [...],  # New stack/project dirs
            "kb_updates": [...],  # Ready-to-write KB entries
        }
    """
    result = {
        "compose_changes": [],
        "credential_alerts": [],
        "new_directories": [],
        "kb_updates": [],
    }

    for filepath in changed_files:
        full_path = Path(git_root) / filepath
        
        # Compose file changes
        if _is_compose_file(filepath):
            services = _parse_compose(full_path)
            if services:
                stack_name = _get_stack_name(filepath)
                result["compose_changes"].append({
                    "stack": stack_name,
                    "file": filepath,
                    "services": services,
                })
                result["kb_updates"].append({
                    "type": "compose",
                    "stack": stack_name,
                    "services": services,
                })

        # Credential detection
        if _is_credential_file(filepath):
            creds = _scan_credentials(full_path)
            if creds:
                result["credential_alerts"].extend(creds)

        # Also scan non-credential files for leaked secrets in diffs
        elif full_path.exists() and full_path.suffix in ('.yml', '.yaml', '.json', '.js', '.py', '.sh', '.conf'):
            diff_creds = _scan_diff_for_credentials(filepath, git_root)
            if diff_creds:
                result["credential_alerts"].extend(diff_creds)

        # New directories
        if _is_new_directory(filepath, git_root):
            dir_info = _classify_directory(filepath)
            if dir_info:
                result["new_directories"].append(dir_info)
                result["kb_updates"].append({
                    "type": "new_directory",
                    **dir_info,
                })

    return result


def write_to_kb(kb_root: Path, updates: list[dict]) -> list[str]:
    """Write infrastructure updates directly to KB files.
    
    Returns list of files written.
    """
    written = []
    
    for update in updates:
        if update["type"] == "compose":
            path = _write_compose_update(kb_root, update)
            if path:
                written.append(path)
        elif update["type"] == "new_directory":
            path = _write_directory_update(kb_root, update)
            if path:
                written.append(path)

    # Git commit KB changes
    if written:
        try:
            subprocess.run(["git", "add", "-A"], cwd=kb_root, capture_output=True, timeout=10)
            subprocess.run(
                ["git", "commit", "-m", f"auto: infra detector — {len(written)} updates"],
                cwd=kb_root, capture_output=True, timeout=10,
            )
            logger.info(f"InfraDetector: KB committed {len(written)} updates")
        except Exception as e:
            logger.warning(f"InfraDetector: KB git commit failed: {e}")

    return written


# ─── Compose parsing ────────────────────────────────────────────

def _is_compose_file(filepath: str) -> bool:
    name = Path(filepath).name
    return name in ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')


def _get_stack_name(filepath: str) -> str:
    """Extract stack name from filepath like stacks/loki/docker-compose.yml."""
    parts = Path(filepath).parts
    if len(parts) >= 2:
        # stacks/loki/docker-compose.yml → loki
        # projects/context-engine/docker-compose.yml → context-engine
        return parts[-2]
    return "unknown"


def _parse_compose(path: Path) -> list[dict]:
    """Parse a docker-compose file and extract service details."""
    if not path.exists():
        return []
    
    try:
        # Use simple YAML parsing (avoid heavy deps)
        import yaml
    except ImportError:
        # Fallback: regex-based extraction
        return _parse_compose_regex(path)
    
    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not data or not isinstance(data, dict):
            return []
        
        services = []
        for name, svc in data.get("services", {}).items():
            if not isinstance(svc, dict):
                continue
            
            service_info = {
                "name": svc.get("container_name", name),
                "image": svc.get("image", "custom (build)"),
                "ports": [],
                "networks": [],
                "volumes": [],
                "environment_keys": [],
            }
            
            # Parse ports
            for p in svc.get("ports", []):
                service_info["ports"].append(str(p))
            
            # Parse networks
            nets = svc.get("networks", [])
            if isinstance(nets, list):
                service_info["networks"] = nets
            elif isinstance(nets, dict):
                service_info["networks"] = list(nets.keys())
            
            # Parse volume mounts (paths only, no data)
            for v in svc.get("volumes", []):
                if isinstance(v, str):
                    service_info["volumes"].append(v.split(":")[0] if ":" in v else v)
            
            # Parse environment variable NAMES only (not values — those may be secrets)
            env = svc.get("environment", [])
            if isinstance(env, list):
                for e in env:
                    if "=" in str(e):
                        key = str(e).split("=")[0].strip().lstrip("- ")
                        service_info["environment_keys"].append(key)
            elif isinstance(env, dict):
                service_info["environment_keys"] = list(env.keys())
            
            services.append(service_info)
        
        return services
    except Exception as e:
        logger.warning(f"InfraDetector: Failed to parse {path}: {e}")
        return []


def _parse_compose_regex(path: Path) -> list[dict]:
    """Fallback compose parser using regex when PyYAML isn't available."""
    try:
        content = path.read_text(encoding="utf-8")
        services = []
        
        # Find container_name entries
        for match in re.finditer(r'container_name:\s*(\S+)', content):
            services.append({
                "name": match.group(1),
                "image": "unknown",
                "ports": [],
            })
        
        # Find image entries
        for match in re.finditer(r'image:\s*(\S+)', content):
            if services:
                for s in services:
                    if s["image"] == "unknown":
                        s["image"] = match.group(1)
                        break
        
        # Find port mappings
        for match in re.finditer(r'"?(\d+):(\d+)"?', content):
            if services:
                services[-1]["ports"].append(f"{match.group(1)}:{match.group(2)}")
        
        return services
    except Exception:
        return []


# ─── Credential detection ───────────────────────────────────────

def _is_credential_file(filepath: str) -> bool:
    return Path(filepath).name in CREDENTIAL_FILES


def _scan_credentials(path: Path) -> list[dict]:
    """Scan a file for credential patterns. Returns masked alerts."""
    if not path.exists():
        return []
    
    alerts = []
    try:
        content = path.read_text(encoding="utf-8")
        for pattern, cred_type in CREDENTIAL_PATTERNS:
            matches = re.findall(pattern, content, re.IGNORECASE)
            for match in matches:
                # Mask the value
                masked = _mask_value(match)
                alerts.append({
                    "file": str(path),
                    "type": cred_type,
                    "masked_value": masked,
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                })
    except Exception:
        pass
    return alerts


def _scan_diff_for_credentials(filepath: str, git_root: str) -> list[dict]:
    """Check the git diff for newly added credential patterns."""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--", filepath],
            cwd=git_root, capture_output=True, text=True, timeout=10,
        )
        if not result.stdout:
            return []
        
        alerts = []
        # Only check added lines
        added_lines = [l[1:] for l in result.stdout.split('\n') if l.startswith('+') and not l.startswith('+++')]
        
        for line in added_lines:
            for pattern, cred_type in CREDENTIAL_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    alerts.append({
                        "file": filepath,
                        "type": cred_type,
                        "masked_value": _mask_value(line.strip()),
                        "source": "git_diff",
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    })
                    break  # One alert per line is enough
        
        return alerts
    except Exception:
        return []


def _mask_value(value: str) -> str:
    """Mask a credential value for safe logging."""
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


# ─── Directory detection ────────────────────────────────────────

def _is_new_directory(filepath: str, git_root: str) -> bool:
    """Check if this file represents a new top-level directory."""
    parts = Path(filepath).parts
    if len(parts) < 2:
        return False
    
    # Check if the parent directory is new (only 1 file in it)
    parent = Path(git_root) / parts[0] / parts[1]
    if parent.is_dir():
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "-2", "--", str(Path(parts[0]) / parts[1])],
                cwd=git_root, capture_output=True, text=True, timeout=5,
            )
            # If only 1 commit mentions this path, it's new
            lines = [l for l in result.stdout.strip().split('\n') if l]
            return len(lines) <= 1
        except Exception:
            pass
    return False


def _classify_directory(filepath: str) -> Optional[dict]:
    """Classify a new directory as a stack, project, or other."""
    parts = Path(filepath).parts
    if len(parts) < 2:
        return None
    
    category = parts[0]  # stacks, projects, mcp-servers, etc.
    name = parts[1]
    
    return {
        "category": category,
        "name": name,
        "path": f"{category}/{name}",
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── KB writing ─────────────────────────────────────────────────

def _write_compose_update(kb_root: Path, update: dict) -> Optional[str]:
    """Write compose service changes to the KB infra changelog."""
    stack = update["stack"]
    services = update["services"]
    
    changelog_path = kb_root / "infrastructure" / "auto-detected-changes.md"
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    entry = f"\n### [{now}] Stack: {stack}\n\n"
    entry += "| Service | Image | Ports | Networks |\n"
    entry += "|---------|-------|-------|----------|\n"
    for svc in services:
        ports = ", ".join(svc.get("ports", [])) or "-"
        networks = ", ".join(svc.get("networks", [])) or "-"
        image = svc.get("image", "-")
        entry += f"| {svc['name']} | {image} | {ports} | {networks} |\n"
    
    if svc.get("environment_keys"):
        entry += f"\nEnv vars: {', '.join(svc['environment_keys'][:15])}\n"
    
    try:
        # Append to changelog
        if changelog_path.exists():
            existing = changelog_path.read_text(encoding="utf-8")
        else:
            existing = "# Infrastructure Changes (Auto-Detected)\n\n> Generated by ContextEngine FileWatcher. Do not edit manually.\n"
        
        # Keep file from growing unbounded — trim to last 100 entries
        lines = existing.split("\n### [")
        if len(lines) > 100:
            existing = lines[0] + "\n### [".join(lines[-100:])
        
        changelog_path.write_text(existing + entry, encoding="utf-8")
        logger.info(f"InfraDetector: Updated KB for stack '{stack}' ({len(services)} services)")
        return str(changelog_path)
    except Exception as e:
        logger.warning(f"InfraDetector: Failed to write KB: {e}")
        return None


def _write_directory_update(kb_root: Path, update: dict) -> Optional[str]:
    """Register a new directory in the KB."""
    registry_path = kb_root / "infrastructure" / "auto-detected-changes.md"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    entry = f"\n### [{now}] New {update['category']}: {update['name']}\n\n"
    entry += f"- Path: `/{update['path']}/`\n"
    entry += f"- Category: {update['category']}\n"
    
    try:
        if registry_path.exists():
            existing = registry_path.read_text(encoding="utf-8")
        else:
            existing = "# Infrastructure Changes (Auto-Detected)\n\n> Generated by ContextEngine FileWatcher. Do not edit manually.\n"
        
        registry_path.write_text(existing + entry, encoding="utf-8")
        logger.info(f"InfraDetector: Registered new {update['category']}: {update['name']}")
        return str(registry_path)
    except Exception as e:
        logger.warning(f"InfraDetector: Failed to register directory: {e}")
        return None
