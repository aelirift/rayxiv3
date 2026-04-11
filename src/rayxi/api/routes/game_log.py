"""Game Log API — view pipeline trace logs for a game.

Endpoints:
  GET /log/{game_name}                          — trace log viewer page
  GET /api/log/{game_name}                      — JSON trace data
  GET /api/log/{game_name}/artifact/{filename}  — phase product (HLR, MLR, etc.)

Reads trace + phase artifacts from: output/{game_name}/
  - trace.json    — written by the runner via TraceLog.save()
  - hlr.json, mlr_*.json, ... — phase products, referenced by trace events'
                                 `artifacts` field
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
log = logging.getLogger("rayxi.api.game_log")

_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "output"


@router.get("/log/{game_name}", response_class=HTMLResponse)
async def log_page(game_name: str):
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Log: {game_name}</title>
    <style>
        body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
        h1 {{ color: #00ff88; margin-bottom: 8px; }}
        .topnav {{ margin-bottom: 16px; }}
        .topnav a {{ color: #00ff88; margin-right: 16px; }}

        .meta-card {{
            background: #0d0d1a; border: 1px solid #2a2a40; border-radius: 8px;
            padding: 16px 20px; margin-bottom: 16px; font-size: 13px;
        }}
        .meta-card .row {{ margin: 4px 0; }}
        .meta-card .key {{ color: #888; display: inline-block; min-width: 110px; }}
        .meta-card .val {{ color: #e0e0e0; }}
        .meta-card .prompt {{
            background: #16162a; padding: 8px 12px; border-radius: 4px;
            color: #ccffaa; margin-top: 4px; white-space: pre-wrap; word-break: break-word;
        }}

        .controls {{ margin: 10px 0; display: flex; gap: 10px; align-items: center; }}
        input {{ background: #0d0d1a; color: #e0e0e0; border: 1px solid #333; padding: 6px; border-radius: 4px; flex: 1; }}

        .log-container {{
            background: #0d0d1a; padding: 15px; border-radius: 8px; overflow-y: auto;
            max-height: 65vh; font-size: 13px; line-height: 1.6;
        }}
        .event {{ padding: 2px 0; border-bottom: 1px solid #1a1a30; }}
        .event:hover {{ background: #16213e; }}
        .t {{ color: #666; min-width: 70px; display: inline-block; }}
        .phase {{ color: #00ff88; min-width: 100px; display: inline-block; }}
        .ev-pipeline_start, .ev-pipeline_end {{ color: #fff; font-weight: bold; font-size: 14px; }}
        .ev-phase_start {{ color: #00ccff; font-weight: bold; }}
        .ev-phase_end {{ color: #00ff88; font-weight: bold; }}
        .ev-llm_start {{ color: #ffcc00; }}
        .ev-llm_end {{ color: #88cc00; }}
        .ev-validation {{ color: #cc88ff; }}
        .ev-build_start, .ev-build_end {{ color: #88aaff; }}
        .ev-verify {{ color: #aaccff; }}
        .ev-error {{ color: #ff4444; font-weight: bold; }}
        .detail {{ color: #888; font-size: 11px; }}

        .artifact-row {{ margin-top: 4px; margin-left: 80px; }}
        .artifact-btn {{
            display: inline-block; background: #16213e; color: #66ddff; border: 1px solid #00ccff;
            padding: 3px 10px; border-radius: 4px; margin: 2px 6px 2px 0; font-size: 11px;
            cursor: pointer; text-decoration: none;
        }}
        .artifact-btn:hover {{ background: #00ccff; color: #0d0d1a; }}

        .no-logs {{ color: #666; padding: 40px; text-align: center; }}

        .artifact-viewer {{
            display: none; position: fixed; top: 40px; right: 20px; width: 55%; max-height: 85vh;
            background: #0d0d1a; border: 1px solid #00ccff; border-radius: 8px; padding: 16px;
            overflow: auto; z-index: 100; font-size: 12px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.6);
        }}
        .artifact-viewer pre {{ white-space: pre-wrap; color: #e0e0e0; word-break: break-word; }}
        .artifact-viewer .close {{
            position: absolute; top: 6px; right: 12px; color: #ff4444; cursor: pointer;
            font-size: 22px; font-weight: bold;
        }}
        .artifact-viewer h3 {{ color: #00ccff; margin: 0 0 12px 0; padding-right: 30px; word-break: break-all; }}
    </style>
</head>
<body>
    <h1>📋 Pipeline Log: {game_name}</h1>
    <div class="topnav">
        <a href="/godot/{game_name}/" target="_blank">▶ Play</a>
        <a href="/test/{game_name}" target="_blank">🧪 Test</a>
        <a href="/gallery">← Gallery</a>
    </div>

    <div class="meta-card" id="metaCard">
        <div class="row"><span class="key">Loading…</span></div>
    </div>

    <div class="controls">
        <label>Filter:</label>
        <input id="filterInput" type="text" placeholder="phase, event, or text…" oninput="applyFilter()">
    </div>
    <div class="log-container" id="logContainer">
        <div class="no-logs">Loading…</div>
    </div>

    <div class="artifact-viewer" id="artifactViewer">
        <span class="close" onclick="closeArtifact()">&times;</span>
        <h3 id="artifactTitle">Artifact</h3>
        <pre id="artifactContent"></pre>
    </div>

    <script>
        const gameName = "{game_name}";
        let allEvents = [];

        async function init() {{
            const res = await fetch('/api/log/' + gameName);
            if (!res.ok) {{
                document.getElementById('logContainer').innerHTML =
                    '<div class="no-logs">No trace found for ' + gameName + '</div>';
                document.getElementById('metaCard').innerHTML = '';
                return;
            }}
            const trace = await res.json();
            renderMeta(trace);
            allEvents = trace.events || [];
            render(allEvents);
        }}

        function renderMeta(trace) {{
            const m = document.getElementById('metaCard');
            const dur = trace.total_duration_s || 0;
            const projectName = trace.project_name || gameName;
            m.innerHTML =
                '<div class="row"><span class="key">project_name:</span> <span class="val">' + escapeHtml(projectName) + '</span></div>' +
                '<div class="row"><span class="key">start_time:</span> <span class="val">' + escapeHtml(trace.start_time || '?') + '</span></div>' +
                '<div class="row"><span class="key">end_time:</span> <span class="val">' + escapeHtml(trace.end_time || '(in progress)') + '</span></div>' +
                '<div class="row"><span class="key">total_duration:</span> <span class="val">' + dur + 's</span></div>' +
                '<div class="row"><span class="key">events:</span> <span class="val">' + (trace.event_count || allEvents.length) + '</span></div>' +
                '<div class="row"><span class="key">user_prompt:</span></div>' +
                '<div class="prompt">' + escapeHtml(trace.user_prompt || '(none)') + '</div>';
        }}

        function applyFilter() {{
            const q = document.getElementById('filterInput').value.toLowerCase();
            if (!q) {{ render(allEvents); return; }}
            const filtered = allEvents.filter(e => JSON.stringify(e).toLowerCase().includes(q));
            render(filtered);
        }}

        function render(events) {{
            const c = document.getElementById('logContainer');
            if (!events.length) {{
                c.innerHTML = '<div class="no-logs">No matching events</div>';
                return;
            }}
            const skipKeys = new Set(['event','t','ts','phase','label','artifacts']);
            let html = '';
            for (const e of events) {{
                const ev = e.event || '?';
                const t = e.t != null ? e.t : '0';
                const phase = e.phase || '';
                const label = e.label || '';
                const details = Object.entries(e)
                    .filter(([k]) => !skipKeys.has(k))
                    .map(([k,v]) => k + '=' + (typeof v === 'object' ? JSON.stringify(v) : v))
                    .join(' ');
                html += '<div class="event ev-' + ev + '">';
                html += '<span class="t">[' + t + 's]</span> ';
                html += '<span class="phase">' + escapeHtml(phase) + '</span> ';
                html += '<strong>' + escapeHtml(ev) + '</strong>';
                if (label) html += ' <em>' + escapeHtml(label) + '</em>';
                if (details) html += ' <span class="detail">' + escapeHtml(details) + '</span>';
                if (Array.isArray(e.artifacts) && e.artifacts.length) {{
                    html += '<div class="artifact-row">';
                    for (const fname of e.artifacts) {{
                        const safe = String(fname).replace(/'/g, "\\\\'");
                        html += '<a class="artifact-btn" onclick="showArtifact(\\''+safe+'\\')">📄 ' + escapeHtml(fname) + '</a>';
                    }}
                    html += '</div>';
                }}
                html += '</div>';
            }}
            c.innerHTML = html;
        }}

        async function showArtifact(filename) {{
            const viewer = document.getElementById('artifactViewer');
            const title = document.getElementById('artifactTitle');
            const content = document.getElementById('artifactContent');
            title.textContent = filename;
            content.textContent = 'Loading…';
            viewer.style.display = 'block';
            try {{
                const res = await fetch('/api/log/' + gameName + '/artifact/' + encodeURIComponent(filename));
                if (!res.ok) {{
                    const err = await res.json().catch(() => ({{}}));
                    content.textContent = 'Error: ' + (err.error || res.status);
                    return;
                }}
                const data = await res.json();
                content.textContent = JSON.stringify(data, null, 2);
            }} catch (err) {{
                content.textContent = 'Error: ' + err.message;
            }}
        }}

        function closeArtifact() {{
            document.getElementById('artifactViewer').style.display = 'none';
        }}

        function escapeHtml(s) {{
            return String(s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }}

        init();
    </script>
</body>
</html>"""


@router.get("/api/log/{game_name}")
async def get_log(game_name: str) -> JSONResponse:
    """Return the most recent trace for a game.

    Reads `output/{game_name}/trace.json` (single file, not multi-run).
    """
    if not _is_safe_name(game_name):
        return JSONResponse({"error": "invalid game name"}, status_code=400)
    trace_path = _OUTPUT_DIR / game_name / "trace.json"
    if not trace_path.exists():
        return JSONResponse({"error": f"no trace for {game_name}"}, status_code=404)
    try:
        return JSONResponse(json.loads(trace_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/log/{game_name}/artifact/{filename}")
async def get_artifact(game_name: str, filename: str) -> JSONResponse:
    """Return a phase product file from output/{game_name}/{filename}."""
    if not _is_safe_name(game_name):
        return JSONResponse({"error": "invalid game name"}, status_code=400)
    if not re.match(r"^[\w\-]+\.json$", filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)

    artifact_path = _OUTPUT_DIR / game_name / filename
    if not artifact_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return JSONResponse(json.loads(artifact_path.read_text(encoding="utf-8")))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


def _is_safe_name(name: str) -> bool:
    return bool(re.match(r"^[\w\-]+$", name))
