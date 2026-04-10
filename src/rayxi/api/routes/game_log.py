"""Game Log API — view pipeline trace logs for a game.

Endpoints:
  GET /log/{game_name} — trace log viewer page
  GET /api/log/{game_name} — JSON trace data
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()
log = logging.getLogger("rayxi.api.game_log")

_TRACE_DIR = Path(__file__).resolve().parents[4] / ".debug" / "traces"


@router.get("/log/{game_name}", response_class=HTMLResponse)
async def log_page(game_name: str):
    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Log: {game_name}</title>
    <style>
        body {{ font-family: monospace; background: #1a1a2e; color: #e0e0e0; padding: 20px; }}
        h1 {{ color: #00ff88; }}
        .controls {{ margin: 10px 0; display: flex; gap: 10px; align-items: center; }}
        select, input {{ background: #0d0d1a; color: #e0e0e0; border: 1px solid #333; padding: 6px; border-radius: 4px; }}
        .log-container {{ background: #0d0d1a; padding: 15px; border-radius: 8px; overflow-y: auto;
                          max-height: 75vh; font-size: 13px; line-height: 1.6; }}
        .event {{ padding: 2px 0; border-bottom: 1px solid #1a1a30; }}
        .event:hover {{ background: #16213e; }}
        .t {{ color: #666; min-width: 70px; display: inline-block; }}
        .phase {{ color: #00ff88; min-width: 100px; display: inline-block; }}
        .ev-PHASE_START {{ color: #00ccff; font-weight: bold; }}
        .ev-PHASE_DONE {{ color: #00ff88; }}
        .ev-PHASE_ERROR {{ color: #ff4444; font-weight: bold; }}
        .ev-LLM_CALL {{ color: #ffcc00; }}
        .ev-LLM_DONE {{ color: #88cc00; }}
        .ev-LLM_ERROR {{ color: #ff6644; }}
        .ev-VALIDATE {{ color: #cc88ff; }}
        .ev-DATA {{ color: #88aaff; }}
        .ev-WARN {{ color: #ffaa00; }}
        .ev-INFO {{ color: #aaa; }}
        .ev-PIPELINE_START, .ev-PIPELINE_END {{ color: #fff; font-weight: bold; font-size: 14px; }}
        .detail {{ color: #888; font-size: 11px; }}
        .filter-active {{ background: #26a !important; }}
        a {{ color: #00ff88; }}
        .artifact-link {{ color: #00ccff; text-decoration: underline; cursor: pointer; margin-left: 8px; font-size: 12px; }}
        .artifact-link:hover {{ color: #66ddff; }}
        .no-logs {{ color: #666; padding: 40px; text-align: center; }}
        .artifact-viewer {{ display: none; position: fixed; top: 50px; right: 20px; width: 50%; max-height: 80vh;
                            background: #0d0d1a; border: 1px solid #00ccff; border-radius: 8px; padding: 15px;
                            overflow: auto; z-index: 100; font-size: 12px; }}
        .artifact-viewer pre {{ white-space: pre-wrap; color: #e0e0e0; }}
        .artifact-viewer .close {{ position: absolute; top: 5px; right: 10px; color: #ff4444; cursor: pointer;
                                   font-size: 18px; font-weight: bold; }}
        .artifact-viewer h3 {{ color: #00ccff; margin: 0 0 10px 0; }}
    </style>
</head>
<body>
    <h1>📋 Pipeline Log: {game_name}</h1>
    <p><a href="/godot/{game_name}/" target="_blank">▶ Play</a> | <a href="/test/{game_name}" target="_blank">🧪 Test</a> | <a href="/gallery">← Gallery</a></p>

    <div class="controls">
        <label>Run:</label>
        <select id="runSelect" onchange="loadRun()"></select>
        <label>Filter:</label>
        <input id="filterInput" type="text" placeholder="phase, event, or text..." oninput="applyFilter()">
    </div>
    <div class="log-container" id="logContainer">
        <div class="no-logs">Loading...</div>
    </div>
    <div class="artifact-viewer" id="artifactViewer">
        <span class="close" onclick="closeArtifact()">&times;</span>
        <h3 id="artifactTitle">Phase Result</h3>
        <pre id="artifactContent"></pre>
    </div>

    <script>
        const gameName = "{game_name}";
        let allEvents = [];

        async function init() {{
            const res = await fetch('/api/log/' + gameName);
            const data = await res.json();
            const sel = document.getElementById('runSelect');
            sel.innerHTML = '';
            if (!data.runs || data.runs.length === 0) {{
                document.getElementById('logContainer').innerHTML = '<div class="no-logs">No trace logs found for ' + gameName + '</div>';
                return;
            }}
            data.runs.forEach((run, i) => {{
                const opt = document.createElement('option');
                opt.value = i;
                opt.textContent = run.file + ' (' + run.events + ' events)';
                sel.appendChild(opt);
            }});
            allEvents = data.runs[0].data;
            render(allEvents);
        }}

        async function loadRun() {{
            const idx = document.getElementById('runSelect').value;
            const res = await fetch('/api/log/' + gameName);
            const data = await res.json();
            allEvents = data.runs[idx].data;
            applyFilter();
        }}

        function applyFilter() {{
            const q = document.getElementById('filterInput').value.toLowerCase();
            if (!q) {{ render(allEvents); return; }}
            const filtered = allEvents.filter(e => JSON.stringify(e).toLowerCase().includes(q));
            render(filtered);
        }}

        function render(events) {{
            const c = document.getElementById('logContainer');
            if (!events.length) {{ c.innerHTML = '<div class="no-logs">No matching events</div>'; return; }}
            let html = '';
            for (const e of events) {{
                const ev = e.event || '?';
                const t = e.t || '0';
                const phase = e.phase || '';
                const artifact = e.artifact || '';
                const details = Object.entries(e)
                    .filter(([k]) => !['event','t','phase','artifact'].includes(k))
                    .map(([k,v]) => k + '=' + (typeof v === 'object' ? JSON.stringify(v) : v))
                    .join(' ');
                html += '<div class="event ev-' + ev + '">';
                html += '<span class="t">[' + t + 's]</span> ';
                html += '<span class="phase">' + phase + '</span> ';
                html += '<strong>' + ev + '</strong> ';
                if (details) html += '<span class="detail">' + details + '</span>';
                if (artifact) {{
                    html += '<a class="artifact-link" onclick="showArtifact(\'' + artifact + '\', \'' + phase + '\')">[view result]</a>';
                }}
                html += '</div>';
            }}
            c.innerHTML = html;
        }}

        async function showArtifact(filename, phase) {{
            const viewer = document.getElementById('artifactViewer');
            const title = document.getElementById('artifactTitle');
            const content = document.getElementById('artifactContent');
            title.textContent = 'Phase Result: ' + phase;
            content.textContent = 'Loading...';
            viewer.style.display = 'block';
            try {{
                const res = await fetch('/api/log/artifact/' + filename);
                const data = await res.json();
                content.textContent = JSON.stringify(data, null, 2);
            }} catch (err) {{
                content.textContent = 'Error: ' + err.message;
            }}
        }}

        function closeArtifact() {{
            document.getElementById('artifactViewer').style.display = 'none';
        }}

        init();
    </script>
</body>
</html>"""


@router.get("/api/log/{game_name}")
async def get_log(game_name: str) -> JSONResponse:
    """Return all trace runs for a game."""
    runs = []

    if not _TRACE_DIR.exists():
        return JSONResponse({"runs": []})

    # Find all trace files for this game (JSONL format)
    for f in sorted(_TRACE_DIR.glob(f"{game_name}_*.jsonl"), reverse=True):
        events = []
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        except Exception:
            continue

        runs.append({
            "file": f.name,
            "events": len(events),
            "data": events,
        })

    return JSONResponse({"runs": runs})


@router.get("/api/log/artifact/{filename}")
async def get_artifact(filename: str) -> JSONResponse:
    """Return a phase artifact JSON file."""
    # Sanitize — only allow alphanumerics, underscores, dashes, and dots
    import re
    if not re.match(r"^[\w\-]+\.json$", filename):
        return JSONResponse({"error": "invalid filename"}, status_code=400)

    artifact_path = _TRACE_DIR / filename
    if not artifact_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        return JSONResponse(data)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
