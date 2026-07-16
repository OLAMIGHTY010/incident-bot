// DOM Elements
const statusAckText = document.getElementById('status-ack-text');
const statusFillText = document.getElementById('status-fill-text');
const btnStartAck = document.getElementById('btn-start-ack');
const btnStopAck = document.getElementById('btn-stop-ack');
const btnStartFill = document.getElementById('btn-start-fill');
const btnStopFill = document.getElementById('btn-stop-fill');

const terminalBodyAck = document.getElementById('terminal-body-ack');
const terminalBodyFill = document.getElementById('terminal-body-fill');
const btnClearAck = document.getElementById('btn-clear-ack');
const btnClearFill = document.getElementById('btn-clear-fill');

const metricAcked = document.getElementById('metric-acked');
const metricFilled = document.getElementById('metric-filled');
const metricManual = document.getElementById('metric-manual');
const metricAi = document.getElementById('metric-ai');
const metricIgnored = document.getElementById('metric-ignored');
const metricMemory = document.getElementById('metric-memory');
const metricUptime = document.getElementById('metric-uptime');

const memoryTbody = document.getElementById('memory-tbody');
const btnAddMemory = document.getElementById('btn-add-memory');
const memoryModal = document.getElementById('memory-modal');
const memoryForm = document.getElementById('memory-form');
const btnCancelModal = document.getElementById('btn-cancel-modal');

const btnApiKey = document.getElementById('btn-api-key');
const apiModal = document.getElementById('api-modal');
const apiForm = document.getElementById('api-form');
const btnCancelApi = document.getElementById('btn-cancel-api');

// State
let ws = null;
let uptimeInterval = null;
let startTime = null;
let unknownQueue = []; // Queue for automated manual filling

let stats = {
    acked: 0,
    filled: 0,
    manual: 0,
    ai_filled: 0,
    ignored: 0
};

// Initialize
async function init() {
    await fetchStatus();
    await fetchMemory();
    setupWebSocket();
    setupEventListeners();
}

// WebSocket Setup
function setupWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        fetchStatus();
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'log_ack') {
            appendLog(data.message, 'ack');
        } else if (data.type === 'log_fill') {
            appendLog(data.message, 'fill');
        } else if (data.type === 'log') {
            appendLog(data.message, 'ack');
            appendLog(data.message, 'fill');
        } else if (data.type === 'status_ack') {
            updateStatusAck(data.status);
        } else if (data.type === 'status_fill') {
            updateStatusFill(data.status);
        } else if (data.type === 'metric') {
            if (stats[data.key] !== undefined) {
                stats[data.key] += data.value;
                if (data.key === 'acked') metricAcked.innerText = stats.acked;
                if (data.key === 'filled') metricFilled.innerText = stats.filled;
                if (data.key === 'manual') metricManual.innerText = stats.manual;
                if (data.key === 'ai_filled') metricAi.innerText = stats.ai_filled;
                if (data.key === 'ignored') metricIgnored.innerText = stats.ignored;
            }
        } else if (data.type === 'unknown_incident') {
            handleUnknownIncident(data.key);
        }
    };
    
    ws.onclose = () => {
        updateStatusAck('disconnected');
        updateStatusFill('disconnected');
        setTimeout(setupWebSocket, 3000); // Reconnect
    };
}

// API Calls
async function fetchStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        updateStatusAck(data.status_ack);
        updateStatusFill(data.status_fill);
    } catch (e) {
        updateStatusAck('disconnected');
        updateStatusFill('disconnected');
    }
}

async function fetchMemory() {
    try {
        const res = await fetch('/api/memory');
        const data = await res.json();
        renderMemory(data);
    } catch (e) {
        console.error("Failed to load memory", e);
    }
}

async function startBotAck() {
    await fetch('/api/start_ack', { method: 'POST' });
}

async function stopBotAck() {
    await fetch('/api/stop_ack', { method: 'POST' });
}

async function startBotFill() {
    await fetch('/api/start_fill', { method: 'POST' });
}

async function stopBotFill() {
    await fetch('/api/stop_fill', { method: 'POST' });
}

async function deleteMemory(key) {
    if (confirm(`Are you sure you want to delete ${key}?`)) {
        await fetch('/api/memory/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key })
        });
        fetchMemory();
    }
}

async function saveMemory(data) {
    await fetch('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    fetchMemory();
    closeModal();
}

// UI Updates
function updateStatusAck(status) {
    if (status === 'running') {
        statusAckText.innerText = 'Running';
        statusAckText.style.color = '#10b981';
        btnStartAck.disabled = true;
        btnStopAck.disabled = false;
        startUptime();
    } else if (status === 'stopped') {
        statusAckText.innerText = 'Stopped';
        statusAckText.style.color = '#9ca3af';
        btnStartAck.disabled = false;
        btnStopAck.disabled = true;
    } else {
        statusAckText.innerText = 'Disconnected';
        statusAckText.style.color = '#ef4444';
        btnStartAck.disabled = true;
        btnStopAck.disabled = true;
    }
}

function updateStatusFill(status) {
    if (status === 'running') {
        statusFillText.innerText = 'Running';
        statusFillText.style.color = '#10b981';
        btnStartFill.disabled = true;
        btnStopFill.disabled = false;
        startUptime();
    } else if (status === 'stopped') {
        statusFillText.innerText = 'Stopped';
        statusFillText.style.color = '#9ca3af';
        btnStartFill.disabled = false;
        btnStopFill.disabled = true;
    } else {
        statusFillText.innerText = 'Disconnected';
        statusFillText.style.color = '#ef4444';
        btnStartFill.disabled = true;
        btnStopFill.disabled = true;
    }
}

function appendLog(msg, type) {
    const terminal = type === 'ack' ? terminalBodyAck : terminalBodyFill;
    const div = document.createElement('div');
    div.className = 'log-line';
    
    // Simple coloring based on keywords
    if (msg.includes('[red]') || msg.toLowerCase().includes('error')) div.classList.add('error');
    else if (msg.includes('[yellow]') || msg.toLowerCase().includes('skipping') || msg.toLowerCase().includes('warning')) div.classList.add('warn');
    else if (msg.includes('[green]') || msg.toLowerCase().includes('success')) div.classList.add('info');
    
    // Strip remaining basic rich tags for safety
    div.innerText = msg.replace(/\[\/?(red|yellow|green|cyan)\]/g, '');
    
    terminal.appendChild(div);
    terminal.scrollTop = terminal.scrollHeight;
    
    // Keep max 500 lines
    if (terminal.children.length > 500) {
        terminal.removeChild(terminal.firstChild);
    }
}

function renderMemory(data) {
    memoryTbody.innerHTML = '';
    const keys = Object.keys(data);
    metricMemory.innerText = keys.length;
    
    keys.sort().forEach(key => {
        const row = data[key];
        const tr = document.createElement('tr');
        
        const prioLower = (row.priority || 'low').toLowerCase();
        
        tr.innerHTML = `
            <td><strong>${key}</strong></td>
            <td><span class="badge ${prioLower}">${row.priority || 'Low'}</span></td>
            <td>${row.rc_description || '-'}</td>
            <td>${row.rc_category || '-'}</td>
            <td>${row.rc_responsibility || '-'}</td>
            <td>
                <button class="icon-btn" onclick="deleteMemory('${key}')" title="Delete"><i class="fa-solid fa-trash-alt"></i></button>
            </td>
        `;
        memoryTbody.appendChild(tr);
    });
}

// Uptime Tracker
function startUptimeTracker() {
    if (uptimeInterval) clearInterval(uptimeInterval);
    uptimeInterval = setInterval(() => {
        if (!startTime) return;
        const diff = Math.floor((new Date() - startTime) / 1000);
        const m = Math.floor(diff / 60);
        const s = diff % 60;
        metricUptime.innerText = `${m}m ${s}s`;
    }, 1000);
}

function stopUptimeTracker() {
    if (uptimeInterval) clearInterval(uptimeInterval);
    startTime = null;
    metricUptime.innerText = '0m 0s';
}

// Modal Logic
function openModal() {
    document.getElementById('mem-key').value = '';
    document.getElementById('mem-desc').value = '';
    document.getElementById('mem-category').value = '';
    document.getElementById('mem-resp').value = '';
    memoryModal.classList.add('open');
}

function closeModal() {
    memoryModal.classList.remove('open');
}

// Event Listeners
function setupEventListeners() {
    btnStartAck.addEventListener('click', startBotAck);
    btnStopAck.addEventListener('click', stopBotAck);
    btnStartFill.addEventListener('click', startBotFill);
    btnStopFill.addEventListener('click', stopBotFill);
    
    btnClearAck.addEventListener('click', () => {
        terminalBodyAck.innerHTML = '';
    });
    
    btnClearFill.addEventListener('click', () => {
        terminalBodyFill.innerHTML = '';
    });
    
    btnAddMemory.addEventListener('click', () => {
        memoryModal.style.display = 'flex';
    });
    
    btnApiKey.addEventListener('click', async () => {
        // Fetch current key
        const res = await fetch('/api/key');
        const data = await res.json();
        document.getElementById('api-key-input').value = data.key || '';
        apiModal.style.display = 'flex';
    });
    
    btnCancelApi.addEventListener('click', () => {
        apiModal.style.display = 'none';
        apiForm.reset();
    });
    
    apiForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const key = document.getElementById('api-key-input').value.trim();
        await fetch('/api/key', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ key })
        });
        apiModal.style.display = 'none';
        
        // Let the user know
        appendLog(`[cyan]OpenRouter API Key updated![/cyan]`, 'fill');
    });
    
    btnCancelModal.addEventListener('click', () => {
        memoryModal.style.display = 'none';
        memoryForm.reset();
        
        // If they cancelled, we just skip it in the queue and try the next
        if (unknownQueue.length > 0) {
            const nextKey = unknownQueue.shift();
            showPopupForKey(nextKey);
        }
    });
    
    memoryForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const data = {
            key: document.getElementById('mem-key').value,
            priority: document.getElementById('mem-priority').value,
            rc_category: document.getElementById('mem-category').value,
            rc_responsibility: document.getElementById('mem-resp').value,
            rc_description: document.getElementById('mem-desc').value
        };
        await fetch('/api/memory', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        memoryModal.style.display = 'none';
        memoryForm.reset();
        fetchMemory();
        
        // Update Manual Metric (we don't wait for backend event, just increment UI)
        stats.manual += 1;
        metricManual.innerText = stats.manual;
        
        // Check Queue
        if (unknownQueue.length > 0) {
            const nextKey = unknownQueue.shift();
            showPopupForKey(nextKey);
        }
    });
}

function handleUnknownIncident(key) {
    if (memoryModal.style.display === 'flex') {
        // Modal is currently open! Just add it to the queue to not interrupt the user.
        if (!unknownQueue.includes(key)) {
            unknownQueue.push(key);
        }
    } else {
        showPopupForKey(key);
    }
}

function showPopupForKey(key) {
    document.getElementById('mem-key').value = key;
    document.getElementById('mem-desc').value = '';
    document.getElementById('mem-category').value = '';
    document.getElementById('mem-resp').value = '';
    
    // Auto popup
    memoryModal.style.display = 'flex';
}

// Boot
init();
