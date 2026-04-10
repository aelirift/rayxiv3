/* RayXI Chat — two-panel: conversation (left) + activity feed (right) */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let currentProject = '';
let streaming      = false;

// ── DOM refs ───────────────────────────────────────────────────────────────
const projectInput    = document.getElementById('project-input');
const projectStatus   = document.getElementById('project-status');
const projectList     = document.getElementById('project-list');
const messages        = document.getElementById('messages');
const msgInput        = document.getElementById('msg-input');
const sendBtn         = document.getElementById('send-btn');
const agentBadge      = document.getElementById('agent-badge');
const activityFeed    = document.getElementById('activity-feed');
const activityTitle   = document.getElementById('activity-title');
const activityMeta    = document.getElementById('activity-meta');
const activityEmpty   = document.getElementById('activity-empty');
const codeContent     = document.getElementById('code-content');
const codeFilename    = document.getElementById('code-filename');
const copyBtn         = document.getElementById('copy-btn');
const playBtn         = document.getElementById('play-btn');

// ── Init ───────────────────────────────────────────────────────────────────
(async function init() {
  await loadProjects();
  projectInput.addEventListener('change', onProjectChange);
  projectInput.addEventListener('input',  onProjectChange);
  sendBtn.addEventListener('click', onSend);
  copyBtn.addEventListener('click', onCopy);
  playBtn.addEventListener('click', onPlay);
  msgInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend(); }
  });
  msgInput.addEventListener('input', autoResize);
})();

// ── Project loading ────────────────────────────────────────────────────────
async function loadProjects() {
  try {
    const res  = await fetch('/api/projects');
    const data = await res.json();
    projectList.innerHTML = '';
    for (const name of data.projects) {
      const opt = document.createElement('option');
      opt.value = name;
      projectList.appendChild(opt);
    }
  } catch (e) {
    console.error('Failed to load projects', e);
  }
}

async function onProjectChange() {
  const name = projectInput.value.trim();
  if (!name) { projectStatus.textContent = ''; return; }

  currentProject = name;

  const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/source`);
  const data = await res.json();

  if (data.exists) {
    projectStatus.textContent = '● existing';
    projectStatus.style.color = 'var(--l1)';
    setCode(data.source, name);
    await loadHistory(name);
  } else {
    projectStatus.textContent = '○ new';
    projectStatus.style.color = 'var(--muted)';
    setCode('', name);
    messages.innerHTML = '';
  }
}

async function loadHistory(name) {
  messages.innerHTML = '';
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/history`);
    const data = await res.json();
    for (const entry of data.history) {
      if (entry.role === 'user') {
        appendUserMsg(entry.content);
      } else {
        appendAgentSummary(entry.content, 'partial');
      }
    }
    scrollMessages();
  } catch (e) {
    console.error('Failed to load history', e);
  }
}

// ── Send ───────────────────────────────────────────────────────────────────
async function onSend() {
  const msg = msgInput.value.trim();
  if (!msg || streaming) return;

  const project = projectInput.value.trim() || 'untitled';
  currentProject = project;

  msgInput.value = '';
  autoResize();
  appendUserMsg(msg);

  await streamChat(project, msg);
}

async function streamChat(project, message) {
  streaming = true;
  sendBtn.disabled = true;
  setBadge('thinking', 'Thinking…');

  // Detect mode from message
  const mode = detectMode(message);

  // Start new session in right-panel activity feed
  startActivitySession(project, mode);
  const workingLine = appendActivityLine('working', '↻', 'Contacting agent…', true);

  const summaryParts = [];

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project, message, max_iterations: 3 }),
    });

    if (!res.ok) {
      workingLine && workingLine.remove();
      appendActivityLine('error', '✗', `HTTP ${res.status}`);
      setBadge('done-error', 'Error');
      setActivityMeta('done-error', 'Error');
      return;
    }

    let firstEvent = true;
    const reader   = res.body.getReader();
    const decoder  = new TextDecoder();
    let   buf      = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const event = JSON.parse(line.slice(6));
            if (firstEvent) { workingLine && workingLine.remove(); firstEvent = false; }
            handleEvent(event, summaryParts);
          } catch (e) { console.error('SSE parse error', e, line); }
        }
      }
    }
  } catch (e) {
    appendActivityLine('error', '✗', `Connection error: ${esc(e.message)}`);
    setBadge('done-error', 'Error');
    setActivityMeta('done-error', 'Error');
  } finally {
    streaming = false;
    sendBtn.disabled = false;

    // Add compact summary to left conversation panel
    if (summaryParts.length) {
      const outcome = summaryParts.some(p => p.startsWith('Error')) ? 'error'
                    : summaryParts.some(p => p.includes('fixes')) ? 'partial'
                    : 'clean';
      appendAgentSummary(summaryParts.join(' '), outcome);
    }

    await loadProjects();
    scrollMessages();
  }
}

// Guess mode from the message text (mirrors backend logic)
function detectMode(msg) {
  const lower = msg.toLowerCase();
  const fixWords = ['fix','bug','error','broken','issue','crash','fail','wrong','security','vulnerability'];
  if (fixWords.some(w => lower.includes(w))) return 'fix';
  return currentProject ? 'improve' : 'create';
}

// ── Event rendering → right panel ─────────────────────────────────────────
function handleEvent(ev, summaryParts) {
  const t = ev.type;

  if (t === 'status') {
    appendActivityLine('status', '↻', esc(ev.message));

  } else if (t === 'created') {
    appendActivityLine('created', '✦',
      `Game <strong>${esc(ev.game_name)}</strong> created`);
    if (ev.source) setCode(ev.source, ev.game_name);
    projectInput.value = ev.game_name;
    currentProject = ev.game_name;
    projectStatus.textContent = '● existing';
    projectStatus.style.color = 'var(--l1)';
    summaryParts.push(`Game '${ev.game_name}' created.`);

  } else if (t === 'scan') {
    const h = ev.high;
    if (h === 0) {
      appendActivityLine('scan', '✓', `Scan ${ev.iteration}: no HIGH findings`);
    } else {
      const chips = (ev.findings || [])
        .map(f => `<span class="finding-chip">${esc(f.finding_type)}</span>`).join(' ');
      appendActivityLine('scan', '⚑',
        `Scan ${ev.iteration}: <strong>${h} HIGH</strong>, ${ev.total} total &nbsp;${chips}`);
    }
    summaryParts.push(`Scan ${ev.iteration}: ${h} HIGH.`);

  } else if (t === 'fixing') {
    appendActivityLine('fixing', '↻',
      `Fixing <span class="fn-name">${esc(ev.function)}</span> &nbsp;` +
      `<span class="finding-chip">${esc(ev.finding_type)}</span>`);

  } else if (t === 'fixed') {
    if (ev.applied) {
      appendActivityLine('fixed', '✓',
        `Fixed <span class="fn-name">${esc(ev.function)}</span>`);
      if (ev.source) setCode(ev.source, currentProject);
      summaryParts.push(`Fixed ${ev.function}.`);
    } else {
      appendActivityLine('fix_failed', '✗',
        `Could not fix <span class="fn-name">${esc(ev.function)}</span>`);
    }

  } else if (t === 'fix_failed') {
    appendActivityLine('fix_failed', '✗',
      `No fix returned for <span class="fn-name">${esc(ev.function)}</span>`);

  } else if (t === 'iteration_done') {
    appendActivityLine('iter', '→',
      `Iteration ${ev.iteration}: ${ev.fixed} fixed, ${ev.findings_after} remaining`);

  } else if (t === 'done') {
    const reason = ev.stopped_reason;
    const cls    = (reason === 'CLEAN' || reason === 'NO_FINDINGS') ? 'clean' : 'partial';
    const icon   = cls === 'clean' ? '✦' : '⚠';
    const label  = reason === 'CLEAN'
      ? `All HIGH findings fixed (${ev.total_fixes} total)`
      : reason === 'NO_FINDINGS'
      ? 'No issues detected — code is clean'
      : `Stopped after max iterations (${ev.total_fixes} fixes applied)`;
    appendActivityLine(`done ${cls}`, icon, `<strong>${label}</strong>`);
    if (ev.source) setCode(ev.source, ev.game_name || currentProject);
    setBadge(cls === 'clean' ? 'done-clean' : 'done-partial',
             cls === 'clean' ? 'Done ✓' : 'Partial');
    setActivityMeta(cls === 'clean' ? 'done-clean' : 'done-partial',
                    cls === 'clean' ? 'Done ✓'    : 'Partial');
    summaryParts.push(`Done (${reason}, ${ev.total_fixes} fixes).`);

  } else if (t === 'oow_defined') {
    const objs = (ev.objects || [])
      .map(o => {
        const states = o.states && o.states.length
          ? ` <span class="oow-states">[${o.states.map(esc).join(' → ')}]</span>` : '';
        return `<span class="oow-obj oow-role-${esc(o.role)}">${esc(o.name)}</span>${states}`;
      }).join('  ');
    const ixs = (ev.interactions || [])
      .map(i => `<span class="oow-ix">${esc(i.subject)}:${esc(i.action)}:${esc(i.object)}</span>`)
      .join('  ');
    appendActivityLine('oow', '◈',
      `<strong>World: ${esc(ev.game_name)}</strong> — ` +
      `${ev.object_count} objects · ${ev.interaction_count} interactions · ` +
      `${ev.level_count} levels · ${ev.mode_count} modes`);
    if (objs)  appendActivityLine('oow-detail', ' ', `<span class="oow-section">objects</span>  ${objs}`);
    if (ixs)   appendActivityLine('oow-detail', ' ', `<span class="oow-section">interactions</span>  ${ixs}`);

  } else if (t === 'knowledge_retrieved') {
    appendActivityLine('knowledge', '⊕', esc(ev.message));

  } else if (t === 'instance_mapped') {
    const chars = (ev.characters || []).map(c => `<span class="oow-obj">${esc(c)}</span>`).join('  ');
    appendActivityLine('instance-map', '◉',
      `<strong>${ev.instance_count} instances</strong> — ` +
      `${ev.character_count} characters · ${ev.move_count} special moves`);
    if (chars) appendActivityLine('oow-detail', ' ', `<span class="oow-section">characters</span>  ${chars}`);

  } else if (t === 'web_research') {
    appendActivityLine('web-research', '⚲', esc(ev.message));

  } else if (t === 'planned') {
    appendActivityLine('planned', '✦',
      `Architecture: <strong>${esc(ev.name)}</strong> — ` +
      `${ev.state_count} states, ${ev.interaction_count || 0} interactions, ${ev.function_count} functions`);

  } else if (t === 'simulating') {
    appendActivityLine('simulating', '↻', esc(ev.message));

  } else if (t === 'sim_pass') {
    appendActivityLine('sim-pass', '✓', esc(ev.message));

  } else if (t === 'sim_fail') {
    const chips = (ev.issues || [])
      .map(i => `<span class="finding-chip">${esc(i.issue_type)}</span>`).join(' ');
    appendActivityLine('sim-fail', '⚑', `${esc(ev.message)} &nbsp;${chips}`);

  } else if (t === 'redesigning') {
    appendActivityLine('redesigning', '↻', esc(ev.message));

  } else if (t === 'redesigned') {
    appendActivityLine('redesigned', '✦',
      `Redesigned: <strong>${esc(ev.name)}</strong> — ${ev.state_count} states`);

  } else if (t === 'sprites_generated') {
    appendActivityLine('sprites', '⬡',
      `Sprites: <strong>${esc(String(ev.count))}</strong> generated via Minimax`);

  } else if (t === 'ast_mapped') {
    appendActivityLine('ast-map', '◈',
      `Skeleton: <strong>${esc(String(ev.class_count))} classes</strong> · ${esc(String(ev.line_count))} lines`);

  } else if (t === 'verified') {
    appendActivityLine('verified', '✓', esc(ev.message || 'Verified OK'));

  } else if (t === 'verify_failed') {
    appendActivityLine('verify-failed', '⚑',
      `Verification failed (${esc(ev.error_type)}): ${esc(ev.message)}`);

  } else if (t === 'error') {
    appendActivityLine('error', '✗', `<strong>Error:</strong> ${esc(ev.message)}`);
    setBadge('done-error', 'Error');
    setActivityMeta('done-error', 'Error');
    summaryParts.push(`Error: ${ev.message}`);
  }
}

// ── Activity feed helpers ──────────────────────────────────────────────────
function startActivitySession(project, mode) {
  // Hide empty placeholder
  if (activityEmpty) activityEmpty.style.display = 'none';

  const hdr = document.createElement('div');
  hdr.className = 'activity-session-hdr';

  const ts = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  hdr.innerHTML =
    `<span class="session-project">${esc(project)}</span>` +
    `<span class="session-mode ${mode}">${mode}</span>` +
    `<span class="session-time">${ts}</span>`;

  activityFeed.appendChild(hdr);
  activityFeed.scrollTop = activityFeed.scrollHeight;

  // Update header meta badge
  setActivityMeta('running', mode.toUpperCase());
}

function appendActivityLine(evClass, icon, html, returnEl = false) {
  const line = document.createElement('div');
  const primary = evClass.split(' ')[0];
  const extra   = evClass.split(' ').slice(1).join(' ');
  line.className = `ev-line ev-${primary}${extra ? ' ' + extra : ''}`;
  line.innerHTML = `<span class="ev-icon">${icon}</span><span class="ev-text">${html}</span>`;
  activityFeed.appendChild(line);
  activityFeed.scrollTop = activityFeed.scrollHeight;
  if (returnEl) return line;
}

function setActivityMeta(cls, label) {
  activityMeta.className = cls;
  activityMeta.textContent = label;
}

// ── Left panel helpers ─────────────────────────────────────────────────────
function appendUserMsg(text) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `
    <div class="msg-role">You</div>
    <div class="msg-bubble">${esc(text)}</div>
  `;
  messages.appendChild(el);
  scrollMessages();
}

function appendAgentSummary(text, outcome = 'partial') {
  const icon = outcome === 'clean' ? '✦' : outcome === 'error' ? '✗' : '⚑';
  const el = document.createElement('div');
  el.className = 'msg agent';
  el.innerHTML = `
    <div class="msg-role">Agent</div>
    <div class="msg-bubble">
      <span class="msg-summary ${outcome}">
        <span class="sum-icon">${icon}</span>
        <span>${esc(text)}</span>
      </span>
    </div>
  `;
  messages.appendChild(el);
  scrollMessages();
}

// ── Code viewer ────────────────────────────────────────────────────────────
function setCode(source, projectName) {
  codeFilename.textContent = projectName ? `${projectName}/game.py` : 'game.py';
  if (!source || !source.trim()) {
    codeContent.innerHTML = '<span class="code-empty">No code yet.</span>';
    playBtn.hidden = true;
    return;
  }
  codeContent.textContent = source;
  playBtn.hidden = false;
}

function onCopy() {
  const text = codeContent.textContent;
  if (!text.trim() || codeContent.querySelector('.code-empty')) return;
  navigator.clipboard.writeText(text).then(() => {
    copyBtn.textContent = '✓';
    setTimeout(() => { copyBtn.textContent = '⧉'; }, 1500);
  });
}

function onPlay() {
  if (!currentProject) return;
  window.open(`/play/${encodeURIComponent(currentProject)}`, '_blank');
}

// ── Misc ───────────────────────────────────────────────────────────────────
function setBadge(cls, label) {
  agentBadge.className = cls;
  agentBadge.textContent = label;
}

function scrollMessages() {
  messages.scrollTop = messages.scrollHeight;
}

function autoResize() {
  msgInput.style.height = 'auto';
  msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + 'px';
}

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
