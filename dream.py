#!/usr/bin/env python3
"""
Hermes Dreaming — Nightly Self-Improvement Loop

Reads recent session history, extracts patterns/corrections/preferences,
and stores them as OpenViking skills and Hermes memory updates.

Phases (inspired by OpenClaw's dreaming model):
  1. LIGHT SLEEP — Scan recent sessions for corrections, preferences, and lessons
  2. REM — Connect patterns across sessions, identify recurring themes
  3. DEEP SLEEP — Score and promote high-value insights to permanent memory

Usage:
  python3 dream.py [--days N] [--dry-run] [--verbose]

Environment:
  OPENVIKING_URL  — OpenViking server URL (default: http://127.0.0.1:1933)
  HERMES_HOME     — Hermes home directory (default: ~/.hermes)
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────

OPENVIKING_URL = os.environ.get("OPENVIKING_URL", "http://127.0.0.1:1933")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE_DB = HERMES_HOME / "state.db"

# Score weights for Deep Sleep phase
WEIGHTS = {
    "correction": 10,      # User corrections are highest value
    "preference": 8,       # User preferences are high value
    "lesson": 7,           # Lessons learned from failures
    "pattern": 5,          # Recurring patterns
    "tool_discovery": 4,   # Tool/command discoveries
    "context": 2,          # General context (low value, usually noise)
}

# ─── Phase 1: Light Sleep — Extract raw signals ──────────────────────────────

def load_recent_sessions(db_path: Path, days: int = 1) -> list[dict]:
    """Load sessions and messages from the last N days."""
    if not db_path.exists():
        print(f"[dream] ERROR: Session DB not found at {db_path}", file=sys.stderr)
        return []

    cutoff = time.time() - (days * 86400)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    sessions = conn.execute(
        "SELECT id, title, started_at, message_count, tool_call_count, model, source "
        "FROM sessions WHERE started_at > ? AND archived = 0 AND source != 'subagent' "
        "ORDER BY started_at DESC",
        (cutoff,)
    ).fetchall()

    result = []
    for s in sessions:
        messages = conn.execute(
            "SELECT id, role, content, tool_name, timestamp "
            "FROM messages WHERE session_id = ? AND active = 1 "
            "ORDER BY timestamp ASC",
            (s["id"],)
        ).fetchall()

        result.append({
            "id": s["id"],
            "title": s["title"] or "(untitled)",
            "started_at": s["started_at"],
            "message_count": s["message_count"],
            "tool_call_count": s["tool_call_count"],
            "model": s["model"],
            "source": s["source"],
            "messages": [
                {
                    "id": m["id"],
                    "role": m["role"],
                    "content": m["content"] or "",
                    "tool_name": m["tool_name"],
                    "timestamp": m["timestamp"],
                }
                for m in messages
            ],
        })

    conn.close()
    return result


def is_framework_generated_user_message(content: str) -> bool:
    """Reject Hermes-injected wrappers that are stored with the user role."""
    normalized = content.lstrip()
    markers = (
        "[CONTEXT COMPACTION — REFERENCE ONLY]",
        "[IMPORTANT: You are running as a scheduled cron job.",
        "[IMPORTANT: The user has invoked the ",
        "[Your active task list was preserved across context compression]",
        "[ASYNC DELEGATION ",
    )
    # Compression can prepend a short acknowledgement (for example, ``Ok``)
    # before injecting a task-list wrapper, so checking only startswith() is
    # insufficient. These bracketed markers are Hermes control payloads, not
    # direct user preferences, corrections, or lessons.
    return any(marker in normalized for marker in markers)


def extract_signals(sessions: list[dict]) -> list[dict]:
    """
    Phase 1: Light Sleep — Scan messages for correction, preference,
    and lesson signals. Returns a list of extracted signals.
    """
    signals = []

    # Patterns that indicate user corrections — require strong correction signals
    CORRECTION_PATTERNS = [
        r"(?:don't|dont|do not|never|stop)\s+(?:use|say|do|put|include|add|write|call)\b",
        r"\b(?:that's wrong|that is wrong|you're wrong|you are wrong|incorrect|that's not right|not quite|nope|nah)\b",
        r"\b(?:I (?:said|meant|wanted)\s+(?:to )?(?:use|say|do|call|name))\b",
        r"\b(?:use .+ instead of|replace .+ with|change .+ to)\b",
        r"\b(?:should be|needs? to be|must be|ought to be|supposed to be)\b.{5,}",  # Require trailing context
    ]

    # Patterns that indicate preferences — require explicit preference language
    PREFERENCE_PATTERNS = [
        r"\b(?:I (?:prefer|like|want|love|hate|dislike))\b",
        r"\b(?:always|never|usually|typically)\s+(?:use|do|make|set|run|generate)\b",
        r"\b(?:my (?:style|preference|convention|rule|default))\b",
        r"\b(?:make it|keep it|ensure it)\s+(?:short|concise|brief|detailed|verbose|simple|fast)\b",
        r"\b(?:low.token|low token)\b",
    ]

    # Patterns that indicate lessons — failures, errors, gotchas
    LESSON_PATTERNS = [
        r"\b(?:error|failed|failure|bug|issue|problem|broke|broken)\b",
        r"\b(?:fix|fixed|patch|workaround|gotcha|pitfall)\b",
        r"\b(?:doesn't work|didn't work|not working|won't work|still broken)\b",
        r"\b(?:be careful|watch out|note that|important:)\b",
    ]

    # Patterns that indicate tool/command discoveries — require specific tool names
    TOOL_PATTERNS = [
        r"\b(?:pip|brew|npm)\s+install\b",
        r"\b(?:hermes)\s+(?:config|setup|cron|tools|skills|auth|model)\b",
        r"\b(?:openviking|curl).*(?:1933|api/v1)\b",
        r"\b(?:launchctl|systemctl)\s+\w+\b",
    ]

    for session in sessions:
        user_msgs = [m for m in session["messages"] if m["role"] == "user" and m["content"]]
        assistant_msgs = [m for m in session["messages"] if m["role"] == "assistant" and m["content"]]

        for msg in user_msgs:
            content = msg["content"]
            # Skip empty, very short, or framework-generated messages.
            if len(content.strip()) < 5 or is_framework_generated_user_message(content):
                continue
            # Skip [/learn] directives (those are already handled)
            if content.strip().startswith("[/learn]"):
                continue

            # Check for corrections
            for pattern in CORRECTION_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    signals.append({
                        "type": "correction",
                        "content": content[:500],
                        "session_id": session["id"],
                        "timestamp": msg["timestamp"],
                        "weight": WEIGHTS["correction"],
                    })
                    break  # One signal per message

            # Check for preferences
            for pattern in PREFERENCE_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    signals.append({
                        "type": "preference",
                        "content": content[:500],
                        "session_id": session["id"],
                        "timestamp": msg["timestamp"],
                        "weight": WEIGHTS["preference"],
                    })
                    break

            # Check for lessons
            for pattern in LESSON_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    signals.append({
                        "type": "lesson",
                        "content": content[:500],
                        "session_id": session["id"],
                        "timestamp": msg["timestamp"],
                        "weight": WEIGHTS["lesson"],
                    })
                    break

        # Extract tool discoveries from assistant messages
        for msg in assistant_msgs:
            content = msg["content"]
            for pattern in TOOL_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    # Only capture if it's a discovery (first occurrence in session)
                    signals.append({
                        "type": "tool_discovery",
                        "content": content[:300],
                        "session_id": session["id"],
                        "timestamp": msg["timestamp"],
                        "weight": WEIGHTS["tool_discovery"],
                    })
                    break

    return signals


# ─── Phase 2: REM — Connect patterns across sessions ─────────────────────────

def find_patterns(signals: list[dict]) -> list[dict]:
    """
    Phase 2: REM — Connect related signals across sessions.
    Group similar signals and identify recurring themes.
    """
    patterns = []

    # Group by type
    by_type = {}
    for s in signals:
        by_type.setdefault(s["type"], []).append(s)

    # Find recurring corrections (same topic mentioned multiple times)
    correction_contents = [s["content"].lower() for s in by_type.get("correction", [])]
    if len(correction_contents) >= 2:
        # Simple keyword overlap grouping
        seen_keywords = {}
        for i, c in enumerate(correction_contents):
            words = set(re.findall(r'\b\w{4,}\b', c))
            for j, c2 in enumerate(correction_contents):
                if i >= j:
                    continue
                words2 = set(re.findall(r'\b\w{4,}\b', c2))
                overlap = words & words2
                if len(overlap) >= 3:
                    key = tuple(sorted(overlap)[:5])
                    if key not in seen_keywords:
                        seen_keywords[key] = []
                    for signal in (by_type["correction"][i], by_type["correction"][j]):
                        if signal not in seen_keywords[key]:
                            seen_keywords[key].append(signal)

        for key, group in seen_keywords.items():
            theme = " ".join(key)
            patterns.append({
                "type": "pattern",
                "theme": theme,
                "count": len(group),
                "signals": group,
                "weight": WEIGHTS["pattern"] * min(len(group), 3),  # Cap multiplier
                "description": f"Recurring correction theme: '{theme}' (mentioned {len(group)} times)",
            })

    # Find recurring preferences
    pref_contents = by_type.get("preference", [])
    if len(pref_contents) >= 2:
        # Check for overlapping preference themes
        pref_words = {}
        for p in pref_contents:
            words = set(re.findall(r'\b\w{4,}\b', p["content"].lower()))
            for w in words:
                pref_words[w] = pref_words.get(w, 0) + 1

        # Words appearing in multiple preferences indicate a strong pattern
        strong_prefs = {w: c for w, c in pref_words.items() if c >= 2 and len(w) >= 4}
        if strong_prefs:
            patterns.append({
                "type": "pattern",
                "theme": " ".join(sorted(strong_prefs.keys())[:5]),
                "count": sum(strong_prefs.values()),
                "signals": pref_contents,
                "weight": WEIGHTS["pattern"] * 2,  # Strong pattern bonus
                "description": f"Strong preference pattern: {', '.join(strong_prefs.keys())}",
            })

    # Detect session topics (most common tool calls = what we worked on)
    tool_counts = {}
    for s in signals:
        if s["type"] == "tool_discovery":
            tool_counts[s["content"][:50]] = tool_counts.get(s["content"][:50], 0) + 1

    return patterns


# ─── Phase 3: Deep Sleep — Score and promote to memory ────────────────────────

def score_and_rank(signals: list[dict], patterns: list[dict]) -> list[dict]:
    """
    Phase 3: Deep Sleep — Score all signals and patterns, then rank them.
    Only promote insights above a threshold to permanent memory.
    """
    all_insights = []

    # Score individual signals
    for s in signals:
        # Recency bonus: more recent = higher score
        age_hours = (time.time() - s.get("timestamp", time.time())) / 3600
        recency_bonus = max(0, 3 - (age_hours / 24))  # Decay over 3 days

        # Deduplicate by content similarity
        score = s["weight"] + recency_bonus
        all_insights.append({
            "type": s["type"],
            "content": s["content"],
            "score": round(score, 2),
            "source": s.get("session_id", "unknown"),
        })

    # Score patterns (higher weight because they're recurring)
    for p in patterns:
        score = p["weight"] + (p["count"] * 2)  # Frequency bonus
        all_insights.append({
            "type": p["type"],
            "content": p.get("description", p.get("theme", "")),
            "score": round(score, 2),
            "source": "pattern",
        })

    # Sort by score descending
    all_insights.sort(key=lambda x: x["score"], reverse=True)

    # Deduplicate by content similarity (simple: skip if >60% word overlap with a higher-scored item)
    deduped = []
    for item in all_insights:
        words = set(re.findall(r'\b\w{3,}\b', item["content"].lower()))
        is_dup = False
        for existing in deduped:
            existing_words = set(re.findall(r'\b\w{3,}\b', existing["content"].lower()))
            overlap = len(words & existing_words)
            union = len(words | existing_words)
            if union > 0 and overlap / union > 0.6:
                is_dup = True
                break
        if not is_dup:
            deduped.append(item)

    return deduped


# ─── Storage: Write to OpenViking and Hermes memory ──────────────────────────

def store_to_openviking(insights: list[dict], dry_run: bool = False) -> list[str]:
    """Store high-value insights as OpenViking skills."""
    import urllib.request
    import urllib.error

    stored = []

    # Only store insights with score >= 5
    high_value = [i for i in insights if i["score"] >= 5]

    if not high_value:
        return ["No high-value insights to store (threshold: score >= 5)"]

    # Group insights by type for organized storage
    by_type = {}
    for i in high_value:
        by_type.setdefault(i["type"], []).append(i)

    for insight_type, items in by_type.items():
        # Create a consolidated dream note per type
        skill_name = f"dream-{insight_type}-{datetime.now().strftime('%Y%m%d')}"
        lines = [f"# Dream Insight: {insight_type.title()} ({datetime.now().strftime('%Y-%m-%d')})", ""]

        for item in items[:10]:  # Max 10 per type per dream
            lines.append(f"- [{item['score']:.1f}] {item['content'][:200]}")

        skill_content = "\n".join(lines)

        if dry_run:
            stored.append(f"[DRY] Would store '{skill_name}' with {len(items)} insights")
            continue

        # Write via WebDAV (create collection first, then overwrite)
        content_bytes = skill_content.encode("utf-8")
        skill_name_safe = skill_name.replace(" ", "-")

        # Ensure the dreams collection exists
        coll_url = f"{OPENVIKING_URL}/webdav/resources/dreams/"
        try:
            urllib.request.urlopen(
                urllib.request.Request(coll_url, method="MKCOL",
                                       headers={"X-OpenViking-Actor-Peer": "hermes-dream"}),
                timeout=10,
            )
        except (urllib.error.HTTPError, urllib.error.URLError):
            pass  # Collection may already exist

        url = f"{OPENVIKING_URL}/webdav/resources/dreams/{skill_name_safe}.md"
        req = urllib.request.Request(
            url,
            data=content_bytes,
            method="PUT",
            headers={
                "Content-Type": "text/markdown",
                "X-OpenViking-Actor-Peer": "hermes-dream",
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=30)
            if resp.status in (200, 201, 204):
                stored.append(f"Stored '{skill_name}' ({len(items)} insights)")
            else:
                stored.append(f"Failed to store '{skill_name}': HTTP {resp.status}")
        except urllib.error.URLError as e:
            stored.append(f"Error storing '{skill_name}': {e}")

    return stored


def count_successful_stores(results: list[str]) -> int:
    """Count only confirmed WebDAV writes, never errors or dry-run plans."""
    return sum(result.startswith("Stored '") for result in results)


def generate_memory_updates(insights: list[dict], dry_run: bool = False) -> list[str]:
    """
    Generate Hermes memory operation suggestions for the highest-value insights.
    Returns a list of memory operations that should be applied.
    """
    # Only the top 3 insights become memory entries (memory is limited)
    top = [i for i in insights if i["score"] >= 7][:3]

    operations = []
    for item in top:
        # Summarize into a compact memory entry (< 200 chars for Hermes memory)
        content = item["content"][:180]
        # Remove newlines and extra spaces
        content = re.sub(r'\s+', ' ', content).strip()
        operations.append({
            "action": "add",
            "content": f"Dream:{content}",
        })

    return operations


# ─── Main Dream Pipeline ──────────────────────────────────────────────────────

def dream(days: int = 1, dry_run: bool = False, verbose: bool = False) -> dict:
    """
    Run the full dreaming pipeline.
    Returns a summary dict for the cron job to deliver.
    """
    start = time.time()
    report_lines = []
    report_lines.append(f"🧠 Hermes Dream Report — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    report_lines.append(f"   Scanning last {days} day(s) of sessions")
    report_lines.append("")

    # Phase 1: Light Sleep — Load and extract
    sessions = load_recent_sessions(STATE_DB, days)
    report_lines.append(f"📋 Phase 1 (Light Sleep): Found {len(sessions)} sessions")

    if not sessions:
        report_lines.append("   No sessions found. Nothing to dream about. 🌙")
        return {
            "phases": "skipped",
            "insights": 0,
            "stored": 0,
            "report": "\n".join(report_lines),
        }

    total_msgs = sum(s["message_count"] for s in sessions)
    report_lines.append(f"   {total_msgs} total messages across {len(sessions)} sessions")

    signals = extract_signals(sessions)
    report_lines.append(f"   Extracted {len(signals)} signals")

    # Count by type
    type_counts = {}
    for s in signals:
        type_counts[s["type"]] = type_counts.get(s["type"], 0) + 1
    for t, c in sorted(type_counts.items()):
        report_lines.append(f"   - {t}: {c}")

    report_lines.append("")

    # Phase 2: REM — Pattern detection
    patterns = find_patterns(signals)
    report_lines.append(f"🔄 Phase 2 (REM): Found {len(patterns)} recurring patterns")
    for p in patterns[:5]:
        report_lines.append(f"   - {p.get('description', p.get('theme', 'pattern'))[:80]}")

    report_lines.append("")

    # Phase 3: Deep Sleep — Score and rank
    insights = score_and_rank(signals, patterns)
    report_lines.append(f"⭐ Phase 3 (Deep Sleep): {len(insights)} ranked insights")

    # Show top insights
    for i in insights[:5]:
        report_lines.append(f"   [{i['score']:.1f}] ({i['type']}) {i['content'][:100]}")

    report_lines.append("")

    # Store to OpenViking
    stored_results = store_to_openviking(insights, dry_run=dry_run)
    successful_stores = count_successful_stores(stored_results)
    report_lines.append(f"💾 Storage: {successful_stores} insights stored to OpenViking")
    for r in stored_results:
        report_lines.append(f"   {r}")

    report_lines.append("")

    # Generate memory update suggestions
    memory_ops = generate_memory_updates(insights, dry_run=dry_run)
    report_lines.append(f"🧠 Memory: {len(memory_ops)} high-value insights for Hermes memory")
    for op in memory_ops:
        report_lines.append(f"   → {op['action']}: {op['content'][:80]}")

    # Apply memory updates via OpenViking (not Hermes memory — too small)
    if memory_ops and not dry_run:
        for op in memory_ops:
            skill_name = f"memory-{datetime.now().strftime('%Y%m%d')}"
            content_bytes = op["content"].encode("utf-8")
            url = f"{OPENVIKING_URL}/webdav/resources/dreams/{skill_name}-updates.md"
            import urllib.request
            req = urllib.request.Request(
                url,
                data=content_bytes,
                method="PUT",
                headers={
                    "Content-Type": "text/markdown",
                    "X-OpenViking-Actor-Peer": "hermes-dream",
                },
            )
            try:
                urllib.request.urlopen(req, timeout=30)
            except Exception:
                pass  # Best effort

    elapsed = time.time() - start
    report_lines.append("")
    report_lines.append(f"⏱ Completed in {elapsed:.1f}s")
    report_lines.append("🌙 Dream complete. Good night.")

    return {
        "phases": "complete",
        "sessions_scanned": len(sessions),
        "signals_extracted": len(signals),
        "patterns_found": len(patterns),
        "insights_ranked": len(insights),
        "insights_stored": successful_stores,
        "memory_updates": len(memory_ops),
        "report": "\n".join(report_lines),
    }


def main():
    parser = argparse.ArgumentParser(description="Hermes Dreaming — Nightly Self-Improvement Loop")
    parser.add_argument("--days", type=int, default=1, help="Number of days to scan")
    parser.add_argument("--dry-run", action="store_true", help="Don't write anything, just report")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    args = parser.parse_args()

    result = dream(days=args.days, dry_run=args.dry_run, verbose=args.verbose)

    print(result["report"])

    if args.dry_run:
        print("\n⚠ DRY RUN — no changes were made")

    # Exit with error if no sessions found
    if result["sessions_scanned"] == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
