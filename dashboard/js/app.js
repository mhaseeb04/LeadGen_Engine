/**
 * app.js — LeadGen Command Center Logic
 * Manages the data grid, CSV import from the Python backend, and review modal.
 */

const AppState = {
  leads: [],
  currentReviewIdx: null
};

// ═══════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', () => {
  renderLeadsTable();
  updateStats();
});

// ═══════════════════════════════════════════════════
// CSV IMPORT LOGIC
// ═══════════════════════════════════════════════════
function importCSV() {
  document.getElementById('csv-file-input').click();
}

function handleCSVImport(event) {
  const file = event.target.files[0];
  if (!file) return;

  const reader = new FileReader();
  reader.onload = (e) => {
    const text = e.target.result;
    const lines = parseCSVString(text);
    if (lines.length < 2) {
      showToast('CSV file is empty or invalid', 'error');
      return;
    }

    const headers = lines[0].map(h => h.trim().toLowerCase());
    const records = [];

    for (let i = 1; i < lines.length; i++) {
      const values = lines[i];
      if (values.length !== headers.length) continue; // Skip malformed
      
      const record = {};
      headers.forEach((h, idx) => {
        record[h] = (values[idx] || '').trim();
      });

      // Map to internal format
      records.push({
        name: record.name || record.business_name || '',
        phone: record.phone || '',
        city: record.city || '',
        strategy: record.strategy || 'no_website',
        primaryFlaw: record.primary_flaw || '',
        flawCount: parseInt(record.flaw_count || '0', 10),
        demoUrl: record.demo_url || '',
        emailSubject: record.email_subject || 'Quick question',
        emailBody: record.email_body || '',
        status: record.status || 'pending', // pending, approved
        ...record // Keep all raw data
      });
    }

    AppState.leads = records;
    updateStats();
    renderLeadsTable();
    showToast(`Successfully loaded ${records.length} leads.`, 'success');
  };
  reader.readAsText(file);
  event.target.value = ''; // Reset input
}

// A robust CSV parser for embedded commas and quotes
function parseCSVString(strData) {
  const objPattern = new RegExp(
    ("(\\,|\\r?\\n|\\r|^)" + "(?:\"([^\"]*(?:\"\"[^\"]*)*)\"|" + "([^\\,\\r\\n]*))"), "gi"
  );
  const arrData = [[]];
  let arrMatches = null;
  while (arrMatches = objPattern.exec(strData)) {
    const strMatchedDelimiter = arrMatches[1];
    if (strMatchedDelimiter.length && strMatchedDelimiter !== ",") {
      arrData.push([]);
    }
    let strMatchedValue;
    if (arrMatches[2]) {
      strMatchedValue = arrMatches[2].replace(new RegExp("\"\"", "g"), "\"");
    } else {
      strMatchedValue = arrMatches[3];
    }
    arrData[arrData.length - 1].push(strMatchedValue);
  }
  // Remove empty trailing row
  if (arrData[arrData.length - 1].length === 1 && arrData[arrData.length - 1][0] === '') {
    arrData.pop();
  }
  return arrData;
}

// ═══════════════════════════════════════════════════
// DATA GRID RENDERING
// ═══════════════════════════════════════════════════
function renderLeadsTable() {
  const tbody = document.getElementById('leads-tbody');
  
  if (AppState.leads.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-tertiary);padding:2rem;">Load pipeline data to begin triage.</td></tr>`;
    return;
  }

  tbody.innerHTML = AppState.leads.map((lead, idx) => `
    <tr style="opacity: ${lead.status === 'approved' ? '0.5' : '1'}">
      <td style="font-weight: 500">${escapeHtml(lead.name || '—')}</td>
      <td>
        <div style="font-size:0.85rem">${escapeHtml(lead.phone || '—')}</div>
        <div style="font-size:0.75rem;color:var(--text-tertiary)">${escapeHtml(lead.city || '—')}</div>
      </td>
      <td>${getStrategyBadge(lead.strategy)}</td>
      <td>
        <div class="audit-text">${escapeHtml(lead.primaryFlaw || 'None')}</div>
        <div style="display:flex;gap:8px;margin-top:2px">
          ${lead.flawCount > 0 ? `<span style="font-size:0.7rem;color:var(--neon-cyan)">${lead.flawCount} flaws</span>` : ''}
          ${lead.grade ? `<span style="font-size:0.7rem;color:var(--text-tertiary)">Grade ${escapeHtml(String(lead.grade))} (${escapeHtml(String(lead.score))})</span>` : ''}
        </div>
      </td>
      <td>
        <button class="btn btn-sm btn-secondary" onclick="openReviewPanel(${idx})">
          ${lead.status === 'approved' ? '✅ Reviewed' : '🔍 Review'}
        </button>
      </td>
    </tr>
  `).join('');
}

function getStrategyBadge(strategy) {
  if (strategy === 'no_website') {
    return `<span class="badge badge-noweb">No Website</span>`;
  } else {
    return `<span class="badge badge-upgrade">Website Upgrade</span>`;
  }
}

function updateStats() {
  const total = AppState.leads.length;
  const noWeb = AppState.leads.filter(l => l.strategy === 'no_website').length;
  const upgrade = AppState.leads.filter(l => l.strategy === 'website_upgrade').length;
  const verified = AppState.leads.filter(l => l.status === 'approved').length;

  document.getElementById('stat-total').textContent = total;
  document.getElementById('stat-noweb').textContent = noWeb;
  document.getElementById('stat-upgrade').textContent = upgrade;
  document.getElementById('stat-verified').textContent = verified;
}

// ═══════════════════════════════════════════════════
// SLIDE-OUT REVIEW PANEL
// ═══════════════════════════════════════════════════
function openReviewPanel(idx) {
  AppState.currentReviewIdx = idx;
  const lead = AppState.leads[idx];

  document.getElementById('review-biz-name').textContent = lead.name || 'Unnamed Business';
  document.getElementById('review-strategy-badge').innerHTML = getStrategyBadge(lead.strategy);
  
  const auditBox = document.getElementById('review-audit-box');
  if (lead.primaryFlaw) {
    auditBox.innerHTML = `Primary Flaw: <strong>${escapeHtml(lead.primaryFlaw)}</strong> <span class="flaw-count">(${lead.flawCount} total flaws)</span>`;
  } else {
    auditBox.innerHTML = `No major flaws detected or No Website strategy.`;
  }

  document.getElementById('review-subject').value = lead.emailSubject || '';
  const bodyEl = document.getElementById('review-body');
  bodyEl.value = lead.emailBody || '';

  // Triage-first: emails aren't generated up front anymore. If this lead
  // doesn't have one yet, generate it on demand now (one Gemini call for
  // the lead the operator is actually looking at).
  if (!lead.emailBody || !lead.emailBody.trim()) {
    generateEmailForLead(idx);
  }

  document.getElementById('review-demo-url').value = lead.demoUrl || '';
  const demoLink = document.getElementById('review-demo-link');
  if (lead.demoUrl) {
    demoLink.href = lead.demoUrl;
    demoLink.style.display = 'inline-flex';
  } else {
    demoLink.style.display = 'none';
  }

  document.getElementById('review-panel-overlay').classList.add('active');
  document.getElementById('review-panel').classList.add('active');
}

function regenerateEmail() {
  // Force a fresh generation for the currently-open lead, even if it
  // already has a body (used to retry after a 429, or to get a new draft).
  if (AppState.currentReviewIdx === null) return;
  AppState.leads[AppState.currentReviewIdx].emailBody = '';
  generateEmailForLead(AppState.currentReviewIdx);
}

async function generateEmailForLead(idx) {
  const lead = AppState.leads[idx];
  const jobId = AppState.currentJobId;
  const bodyEl = document.getElementById('review-body');
  const subjectEl = document.getElementById('review-subject');

  if (!jobId) {
    bodyEl.value = 'Run a campaign from the New Campaign panel first.';
    return;
  }

  const base = (typeof API_BASE !== 'undefined' && API_BASE)
    ? API_BASE
    : (window.LEADGEN_API_BASE || 'https://leadgen-engine-ngxx.onrender.com');
  const headers = (typeof apiHeaders === 'function')
    ? apiHeaders() : { 'Content-Type': 'application/json' };

  bodyEl.value = '✍️ Generating a personalised email…';
  bodyEl.disabled = true;

  try {
    const res = await fetch(`${base}/api/campaigns/${jobId}/generate_email`, {
      method: 'POST',
      headers,
      body: JSON.stringify(lead), // send the whole lead (name, city, strategy, primary_flaw, report_json, demo_url…)
    });
    const data = await res.json().catch(() => ({}));

    // The operator may have moved on to another lead while this ran —
    // only apply the result if this lead is still the open one.
    if (AppState.currentReviewIdx !== idx) return;

    if (res.ok && data.body) {
      lead.emailBody = data.body;
      lead.emailSubject = data.subject || lead.emailSubject || 'Quick question';
      bodyEl.value = data.body;
      subjectEl.value = lead.emailSubject;
    } else {
      const reason = data.error || `HTTP ${res.status}`;
      // Surface the reason IN the body (not just a fleeting toast) so it's
      // obvious what happened and the operator can retry. A 429 here means
      // the Gemini key/model is out of quota for the moment.
      const hint = /429|quota|exhaust/i.test(reason)
        ? 'Gemini is rate-limited or out of daily quota right now. Wait a moment and click Regenerate, or check GEMINI_MODEL is gemini-2.0-flash.'
        : reason;
      bodyEl.value = `⚠️ Couldn't generate this email.\n\n${hint}`;
      showToast(`Couldn't generate email: ${reason}`, 'error');
    }
  } catch (err) {
    if (AppState.currentReviewIdx === idx) {
      bodyEl.value = '';
      showToast('Failed to reach the API to generate the email.', 'error');
    }
  } finally {
    bodyEl.disabled = false;
  }
}

function closeReviewPanel() {
  document.getElementById('review-panel-overlay').classList.remove('active');
  document.getElementById('review-panel').classList.remove('active');
  AppState.currentReviewIdx = null;
}

function approveCurrentLead() {
  if (AppState.currentReviewIdx !== null) {
    // Save operator edits to BOTH subject and body back to state so the
    // send step uses exactly what was approved.
    AppState.leads[AppState.currentReviewIdx].emailSubject = document.getElementById('review-subject').value;
    AppState.leads[AppState.currentReviewIdx].emailBody = document.getElementById('review-body').value;
    AppState.leads[AppState.currentReviewIdx].status = 'approved';
    
    closeReviewPanel();
    updateStats();
    renderLeadsTable();
    showToast('Lead marked as verified!', 'success');
  }
}

// ═══════════════════════════════════════════════════
// EXPORT LOGIC
// ═══════════════════════════════════════════════════
function exportVerifiedCSV() {
  const verified = AppState.leads.filter(l => l.status === 'approved');
  
  if (verified.length === 0) {
    showToast('No leads have been approved yet.', 'error');
    return;
  }

  // Get all unique keys from the verified leads to use as headers
  const headersSet = new Set();
  verified.forEach(lead => {
    Object.keys(lead).forEach(k => {
      // Don't export internal camelCase properties, export raw keys
      if (k !== 'primaryFlaw' && k !== 'flawCount' && k !== 'demoUrl' && k !== 'emailSubject' && k !== 'emailBody' && k !== 'status') {
        headersSet.add(k);
      }
    });
  });
  const headers = Array.from(headersSet);
  
  // Format as CSV
  const rows = verified.map(l => headers.map(h => {
    let val = l[h] || '';
    if (typeof val === 'string') {
      val = val.replace(/"/g, '""');
      return `"${val}"`;
    }
    return `"${val}"`;
  }).join(','));
  
  const csv = [headers.join(','), ...rows].join('\n');
  downloadFile(csv, 'verified_leads.csv', 'text/csv');
  showToast(`Exported ${verified.length} verified leads for dispatch!`, 'success');
}

let _sendInFlight = false;
async function sendApprovedEmails() {
  // Prevent duplicate sends: if a send is already running, ignore extra
  // clicks (this is what was stacking 6 identical error toasts).
  if (_sendInFlight) return;

  const verified = AppState.leads.filter(l => l.status === 'approved');

  if (verified.length === 0) {
    showToast('No leads approved yet. Open a lead → Review → Approve & Mark Ready first.', 'error');
    return;
  }

  // Client-side pre-check so we give a precise, helpful message instead of
  // a generic server rejection: which approved leads are missing an email
  // address or a generated body?
  const sendable = verified.filter(l => (l.email || '').trim() && (l.emailBody || '').trim());
  if (sendable.length === 0) {
    const missingBody = verified.filter(l => !(l.emailBody || '').trim()).length;
    const missingEmail = verified.filter(l => !(l.email || '').trim()).length;
    let why = 'Your approved leads can’t be sent yet: ';
    if (missingBody) why += `${missingBody} have no generated email body (open Review to generate one). `;
    if (missingEmail) why += `${missingEmail} have no email address on file.`;
    showToast(why.trim(), 'error');
    return;
  }

  const jobId = AppState.currentJobId;
  if (!jobId) {
    showToast('No active campaign found. Run a campaign from the New Campaign panel first.', 'error');
    return;
  }

  const base = (typeof API_BASE !== 'undefined' && API_BASE)
    ? API_BASE
    : (window.LEADGEN_API_BASE || 'https://leadgen-engine-ngxx.onrender.com');
  const headers = (typeof apiHeaders === 'function')
    ? apiHeaders()
    : { 'Content-Type': 'application/json' };

  _sendInFlight = true;
  const sendBtn = document.querySelector('[onclick="sendApprovedEmails()"]');
  if (sendBtn) { sendBtn.disabled = true; sendBtn.style.opacity = '0.6'; }

  try {
    const response = await fetch(`${base}/api/campaigns/${jobId}/send_emails`, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        leads: sendable.map(l => ({
          email: l.email,
          name: l.name,
          city: l.city,
          email_body: l.emailBody,
          email_subject: l.emailSubject,
          demo_url: l.demoUrl,
        })),
      }),
    });

    const result = await response.json().catch(() => ({}));

    if (response.ok) {
      showToast(result.status || `Sending ${sendable.length} approved email(s)…`, 'success');
    } else {
      showToast(`Error sending emails: ${result.error || response.status}`, 'error');
    }
  } catch (error) {
    console.error('Error sending emails:', error);
    showToast('Failed to connect to the server to send emails.', 'error');
  } finally {
    _sendInFlight = false;
    if (sendBtn) { sendBtn.disabled = false; sendBtn.style.opacity = '1'; }
  }
}

// ═══════════════════════════════════════════════════
// UTILS
// ═══════════════════════════════════════════════════
function escapeHtml(unsafe) {
  if (!unsafe) return '';
  return unsafe.toString()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function downloadFile(content, fileName, contentType) {
  const a = document.createElement("a");
  const file = new Blob([content], { type: contentType });
  a.href = URL.createObjectURL(file);
  a.download = fileName;
  a.click();
  URL.revokeObjectURL(a.href);
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');

  // Dedupe: if an identical toast is already showing, don't stack another —
  // just restart its dismiss timer. Prevents floods of the same message.
  const existing = Array.from(container.querySelectorAll('.toast'))
    .find(t => t.dataset.message === message);
  if (existing) {
    clearTimeout(Number(existing.dataset.timer));
    const t = setTimeout(() => {
      existing.style.opacity = '0';
      existing.style.transform = 'translateX(100%)';
      setTimeout(() => existing.remove(), 300);
    }, 3000);
    existing.dataset.timer = String(t);
    return;
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.dataset.message = message;
  toast.innerHTML = `
    <span style="font-size:1.2rem">${type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️'}</span>
    <span>${message}</span>
  `;
  container.appendChild(toast);
  const timer = setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
  toast.dataset.timer = String(timer);
}
