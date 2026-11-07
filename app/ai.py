"""AI integration: health summaries, chat assistant, query optimization
advice. All calls are grounded in real metrics pulled from the target
Postgres instance -- the model never sees credentials or raw connection
strings, only the metric values themselves.

Two provider shapes are supported (see app.ai_settings for how one gets
selected/configured):
  anthropic          The Anthropic Messages API -- api.anthropic.com by
                      default, or any Anthropic-compatible endpoint via a
                      configured base_url (some enterprise LLM gateways
                      speak this wire format).
  openai_compatible   The OpenAI chat-completions wire format that most
                      self-hosted/local model servers standardize on
                      (vLLM, Ollama, LocalAI, Azure OpenAI, text-
                      generation-webui, an internal enterprise proxy, ...).
                      Plain HTTP via httpx -- deliberately not the `openai`
                      SDK, since this app only ever needs one simple,
                      non-streaming call shape and every function here
                      already passes the same flat role/content message
                      list either provider accepts as-is.
Both are dispatched through the one _complete() below; every feature
function in this module just builds a prompt and calls that, unaware of
which provider is actually configured.
"""
from __future__ import annotations

import json

import anthropic
import httpx

from app import ai_settings

SYSTEM_PROMPT = """You are an expert PostgreSQL SRE / DBA assistant embedded in a
live database monitoring dashboard called "pgmon". You are given real,
current metrics pulled moments ago from pg_stat_activity, pg_stat_database,
pg_stat_user_tables, pg_stat_user_indexes and related catalog views.

Be concrete and specific to the numbers you're given -- cite actual values
(connection counts, cache hit ratio, dead tuple percentages, query
durations) rather than generic advice. If something looks healthy, say so
plainly instead of inventing problems. Keep recommendations actionable
(specific SQL, config parameters, or index suggestions) rather than vague
platitudes. If data needed to answer is missing (e.g. pg_stat_statements
not enabled), say what's missing and how to enable it."""


async def _complete(system: str, messages: list[dict], max_tokens: int, override: dict | None = None) -> str:
    """Dispatches to whichever provider app.ai_settings currently resolves
    to (or, for the settings screen's "Test connection" button, a
    not-yet-saved candidate config passed as `override`). `messages` is a
    flat [{role, content}, ...] list with plain-string content -- the same
    shape both the Anthropic Messages API and OpenAI's chat-completions
    API accept directly, so no per-provider translation is needed here."""
    eff = override or await ai_settings.get_effective()
    if not eff["api_key"]:
        raise RuntimeError(
            "No AI provider is configured -- set one up from the AI Assistant tab, "
            "or set ANTHROPIC_API_KEY."
        )

    if eff["provider"] == "openai_compatible":
        base_url = (eff["base_url"] or "").rstrip("/")
        if not base_url:
            raise RuntimeError("An OpenAI-compatible provider needs a base URL -- set one from the AI Assistant tab.")
        if not eff["model"]:
            raise RuntimeError("An OpenAI-compatible provider needs a model name -- set one from the AI Assistant tab.")
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {eff['api_key']}"},
                json={
                    "model": eff["model"],
                    "messages": [{"role": "system", "content": system}, *messages],
                    "max_tokens": max_tokens,
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"AI provider returned HTTP {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"Unexpected response shape from AI provider: {json.dumps(data)[:500]}")

    client = anthropic.AsyncAnthropic(api_key=eff["api_key"], base_url=eff["base_url"] or None)
    resp = await client.messages.create(
        model=eff["model"] or ai_settings.DEFAULT_ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return "".join(block.text for block in resp.content if block.type == "text")


ASSESSMENT_SCHEMA_HINT = """Respond with ONLY a single JSON object (no markdown
fences, no prose outside the JSON) matching exactly this shape:
{
  "score": <integer 0-100, higher is better/safer>,
  "status": <"healthy" | "watch" | "degraded" | "critical">,
  "headline": <one sentence summary>,
  "issues": [ { "title": <short>, "detail": <specific, cites real values from the data given>, "severity": "low"|"medium"|"high" } ],
  "recommendations": [ <specific actionable string, e.g. exact SQL or config change> ]
}
If there are no issues, return an empty issues array and status "healthy"."""


async def _structured_assessment(focus: str, data: dict, extra_context: str = "") -> dict:
    """Shared plumbing for the three "score + issues + recommendations" AI
    features (health, security, backup readiness) -- same prompt shape, same
    JSON-parse-with-fallback handling."""
    prompt = (
        f"Focus area for this assessment: {focus}\n\n"
        f"Current data (JSON):\n{json.dumps(data, default=str, indent=2)}\n\n"
        + (f"{extra_context}\n\n" if extra_context else "")
        + ASSESSMENT_SCHEMA_HINT
    )
    text = await _complete(SYSTEM_PROMPT, [{"role": "user", "content": prompt}], max_tokens=1500)
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[cleaned.find("{"):]
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return {
            "score": None,
            "status": "unknown",
            "headline": "AI response could not be parsed as structured data; showing raw output.",
            "issues": [],
            "recommendations": [],
            "raw": text,
        }


async def health_summary(snapshot: dict, history_summary: str) -> dict:
    return await _structured_assessment(
        "Overall database health -- connections, cache hit ratio, long-running "
        "queries, locks, dead tuples, vacuum status",
        snapshot,
        extra_context=f"Recent trend context:\n{history_summary}",
    )


async def security_review(security_info: dict) -> dict:
    return await _structured_assessment(
        "Security posture -- role privileges (especially superuser/createrole "
        "counts), SSL enforcement, password encryption method, pg_hba.conf "
        "authentication rules (flag any 'trust' or 'password' i.e. "
        "plaintext methods), and installed extensions",
        security_info,
    )


async def backup_review(backup_info: dict) -> dict:
    return await _structured_assessment(
        "Backup & recovery readiness -- WAL level and archive_mode "
        "configuration, archiver success/failure counts, time since last "
        "checkpoint, and replication/standby status. Note this instance may "
        "have no backup automation configured at all, which is itself the "
        "main finding to surface if true.",
        backup_info,
    )


async def performance_review(perf_info: dict) -> dict:
    return await _structured_assessment(
        "Database performance -- current IO throughput (reads/writes/hits "
        "per second, from pg_stat_io), the current wait-event breakdown of "
        "active sessions (what they're blocked on right now: locks, IO, "
        "client, CPU), and memory configuration (shared_buffers, "
        "effective_cache_size, work_mem, cache hit ratio, temp file spill "
        "as a sign work_mem is too small for some queries).",
        perf_info,
    )


async def administration_review(admin_info: dict) -> dict:
    return await _structured_assessment(
        "Database administration/maintenance health -- autovacuum "
        "configuration and whether it's keeping up (dead tuple ratios, "
        "any tables overdue for a vacuum), estimated table bloat, disk "
        "usage and growth rate trend, WAL generation rate, and checkpoint "
        "frequency vs. checkpoint_timeout/max_wal_size (frequent "
        "'requested' checkpoints relative to 'timed' ones suggests "
        "max_wal_size is too small for the write volume). The table bloat "
        "figures are an estimate from the dead-tuple ratio, not an exact "
        "pgstattuple measurement -- say so if citing them.",
        admin_info,
    )


async def chat(messages: list[dict], snapshot: dict) -> str:
    """`messages` is the running conversation as [{role, content}, ...] pairs
    (role is 'user' or 'assistant'); the latest live snapshot is re-injected
    as fresh context on every turn."""
    context_msg = {
        "role": "user",
        "content": (
            "(Context -- current live database snapshot, for your reference "
            f"in answering the conversation below)\n{json.dumps(snapshot, default=str)}"
        ),
    }
    return await _complete(SYSTEM_PROMPT, [context_msg, *messages], max_tokens=1200)


async def analyze_query(query: str, plan: object) -> str:
    prompt = (
        "Analyze this PostgreSQL query and its real EXPLAIN (ANALYZE, BUFFERS) "
        "output (JSON format) from the live database. Identify the slowest "
        "part of the plan, explain why in plain terms, and give specific, "
        "concrete suggestions (exact CREATE INDEX statements, rewrite "
        "suggestions, or config changes) to speed it up. If the plan is "
        "already efficient for the data volume shown, say so.\n\n"
        f"Query:\n{query}\n\n"
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) output:\n{json.dumps(plan, default=str, indent=2)}"
    )
    return await _complete(SYSTEM_PROMPT, [{"role": "user", "content": prompt}], max_tokens=1500)


async def test_connection(provider: str, api_key: str, base_url: str | None, model: str | None) -> str:
    """Used by the AI Assistant tab's settings screen to validate a
    candidate provider config *before* saving it -- a real, minimal call
    against exactly what the user just typed, not the currently-saved
    settings. Returns the model's reply on success; raises on failure
    (bad key, unreachable base_url, unknown model, ...)."""
    override = {"provider": provider, "api_key": api_key, "base_url": base_url or None, "model": model or None}
    return await _complete(
        "Reply with only the word OK.", [{"role": "user", "content": "Say OK."}], max_tokens=10, override=override,
    )
