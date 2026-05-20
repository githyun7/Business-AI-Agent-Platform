
const API = 'http://localhost:8000';
let currentSessionId = null;
let ws = null;

// Session management

async function createSession() {
  const btn = document.getElementById('new-session-btn');
  btn.textContent = 'Starting...';
  btn.disabled = true;

  try {
    const res = await fetch(`${API}/sessions`, { method: 'POST' });
    const data = await res.json();
    await loadSessions();
    selectSession(data.session_id);
  } catch (e) {
    addMessage('system', `Failed to create session: ${e.message}`);
  } finally {
    btn.textContent = '+ New Session';
    btn.disabled = false;
  }
}

async function loadSessions() {
  const res = await fetch(`${API}/sessions`);
  const sessions = await res.json();
  const list = document.getElementById('session-list');
  list.innerHTML = '';

  for (const s of sessions) {
    const div = document.createElement('div');
    div.className = `session-item ${s.session_id === currentSessionId ? 'active' : ''}`;
    div.onclick = () => selectSession(s.session_id);
    div.innerHTML = `
      <div class="s-id">${s.session_id.slice(0, 8)}…</div>
      <div class="s-status status-${s.status}">${s.status}</div>
    `;
    list.appendChild(div);
  }
}

async function selectSession(sessionId) {
  currentSessionId = sessionId;

  document.getElementById('no-session').style.display = 'none';
  const view = document.getElementById('session-view');
  view.classList.add('visible');

  document.getElementById('messages').innerHTML = '';
  setInputEnabled(false);

  connectWebSocket(sessionId);
  await loadSessions();

  const res = await fetch(`${API}/sessions/${sessionId}`);
  const session = await res.json();

  if (session.status === 'ready') {
    showVnc(session.vnc_url);
    setInputEnabled(true);
  } else if (session.status === 'starting') {
    addMessage('system', 'Container starting — waiting for desktop...');
  }
}

function connectWebSocket(sessionId) {
  if (ws) ws.close();
  ws = new WebSocket(`ws://localhost:8000/ws/${sessionId}`);

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    handleEvent(data);
  };

  ws.onclose = () => {
    setTimeout(() => {
      if (currentSessionId === sessionId) connectWebSocket(sessionId);
    }, 2000);
  };
}

function handleEvent(data) {
  switch (data.type) {
    case 'session_ready':
    case 'session_status':
      if (data.status === 'ready') {
        showVnc(data.vnc_url);
        setInputEnabled(true);
        addMessage('system', '✅ Agent ready');
        loadSessions();
      } else if (data.status === 'error') {
        addMessage('system', '❌ Session failed to start');
        loadSessions();
      }
      break;

    case 'output':
      const block = data.block;
      if (block?.type === 'text' && block.text) {
        addMessage('assistant', block.text);
      } else if (block?.type === 'tool_use') {
        addMessage('tool', `🔧 ${block.name}\n${JSON.stringify(block.input, null, 2)}`);
      }
      break;

    case 'tool_result':
      if (data.output) addMessage('tool', `📤 ${data.output}`);
      if (data.error)  addMessage('tool', `❌ ${data.error}`);
      break;

    case 'error':
      addMessage('system', `Error: ${data.error}`);
      break;
  }
}

// ── Message sending ───────────────────────────────────────────────────────────

async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim();
  if (!text || !currentSessionId) return;

  input.value = '';
  addMessage('user', text);
  setInputEnabled(false);

  try {
    await fetch(`${API}/sessions/${currentSessionId}/message`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text }),
    });
    setTimeout(() => setInputEnabled(true), 2000);
  } catch (e) {
    addMessage('system', `Failed to send: ${e.message}`);
    setInputEnabled(true);
  }
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function showVnc(url) {
  const frame = document.getElementById('vnc-frame');
  const placeholder = document.getElementById('vnc-placeholder');
  if (url) {
    frame.src = url;
    frame.style.display = 'block';
    placeholder.style.display = 'none';
  }
}

function addMessage(type, text) {
  const container = document.getElementById('messages');
  const div = document.createElement('div');
  div.className = `msg msg-${type}`;
  div.textContent = text;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function setInputEnabled(enabled) {
  document.getElementById('msg-input').disabled = !enabled;
  document.getElementById('send-btn').disabled = !enabled;
}

// Load sessions on page load
loadSessions();