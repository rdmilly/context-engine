"""Daily Telegram Digest — sends cockpit summary every morning.

Runs as a background task. Sends at configured hour (default 7:00 AM PST).
Also exposes send_digest() for manual/API triggers.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.cockpit import read_cockpit
from services.webhook import _send_telegram_direct
from utils.logging_ import logger

# Pacific time (UTC-8)
PST = timezone(timedelta(hours=-8))
DIGEST_HOUR = 7  # 7:00 AM PST
DIGEST_MINUTE = 0

_task: Optional[asyncio.Task] = None


def _build_digest_message(cockpit_md: str) -> str:
    """Parse cockpit markdown into a compact Telegram message."""
    lines = cockpit_md.split('\n')
    sections = {}
    current_section = None
    current_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('## '):
            if current_section:
                sections[current_section] = current_lines
            current_section = stripped[3:]
            current_lines = []
        elif current_section:
            current_lines.append(stripped)

    if current_section:
        sections[current_section] = current_lines

    # Build message
    now = datetime.now(PST)
    msg = f"\U0001f3af *Daily Project Cockpit* \u2014 {now.strftime('%A %b %d')}\n"

    # Active builds summary
    if 'ACTIVE BUILDS' in sections:
        msg += "\n*Active Builds:*\n"
        for line in sections['ACTIVE BUILDS']:
            if line.startswith('### '):
                title = line[4:]
                msg += f"  {title}\n"
            elif line.startswith('**Next:'):
                val = line.replace('**Next:**', '').strip()
                msg += f"    \u27a1 _{val[:100]}_\n"
            elif line.startswith('**Blockers:'):
                val = line.replace('**Blockers:**', '').strip()
                if val.lower() != 'none' and val.lower() != 'none (ready to proceed)':
                    msg += f"    \u26d4 {val[:80]}\n"

    # Deployed but needs work
    if 'DEPLOYED BUT NEEDS WORK' in sections:
        items = [l[4:] for l in sections['DEPLOYED BUT NEEDS WORK'] if l.startswith('### ')]
        if items:
            msg += f"\n*Needs Work:* {', '.join(items)}\n"

    # Infrastructure alerts (critical/high only)
    if 'INFRASTRUCTURE ALERTS' in sections:
        alerts = []
        for line in sections['INFRASTRUCTURE ALERTS']:
            if '|' in line and ('Critical' in line or 'High' in line):
                cells = [c.strip() for c in line.split('|') if c.strip()]
                if cells and not cells[0].startswith('-'):
                    alerts.append(cells[0])
        if alerts:
            msg += "\n\U0001f6a8 *Critical/High Alerts:*\n"
            for a in alerts:
                msg += f"  \u2022 {a}\n"

    # Waiting on Ryan
    if 'WAITING ON RYAN' in sections:
        items = []
        for line in sections['WAITING ON RYAN']:
            if line.startswith('- [ ]'):
                items.append(line.replace('- [ ] ', ''))
        if items:
            msg += f"\n\U0001f4cb *Waiting on you ({len(items)}):*\n"
            for item in items[:5]:
                msg += f"  \u2610 {item[:60]}\n"
            if len(items) > 5:
                msg += f"  _...and {len(items) - 5} more_\n"

    msg += f"\n\U0001f517 [Full cockpit](https://memory.millyweb.com/dashboard)"
    return msg


def send_digest() -> bool:
    """Send the daily digest now. Returns True on success."""
    cockpit = read_cockpit()
    if not cockpit:
        logger.warning("Daily digest: no cockpit data to send")
        return False

    message = _build_digest_message(cockpit)
    result = _send_telegram_direct(message)
    if result:
        logger.info(f"Daily digest sent ({len(message)} chars)")
    else:
        logger.error("Daily digest: Telegram send failed")
    return result


async def _digest_loop():
    """Background loop that sends digest at DIGEST_HOUR PST daily."""
    logger.info(f"Daily digest scheduler started (sends at {DIGEST_HOUR}:{DIGEST_MINUTE:02d} PST)")
    while True:
        try:
            now = datetime.now(PST)
            target = now.replace(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            logger.info(f"Daily digest: next send in {wait_seconds/3600:.1f} hours ({target.strftime('%Y-%m-%d %H:%M PST')})")
            await asyncio.sleep(wait_seconds)
            send_digest()
        except asyncio.CancelledError:
            logger.info("Daily digest scheduler stopped")
            break
        except Exception as e:
            logger.error(f"Daily digest error: {e}")
            await asyncio.sleep(3600)  # Retry in 1 hour on error


def start_scheduler():
    """Start the background digest scheduler."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_digest_loop())
    logger.info("Daily digest scheduler task created")


def stop_scheduler():
    """Stop the background digest scheduler."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        _task = None
