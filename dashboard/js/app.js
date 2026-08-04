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
  document.getElementById('review-body').value = lead.emailBody || '';
  
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

function closeReviewPanel() {
  document.getElementById('review-panel-overlay').classList.remove('active');
  document.getElementById('review-panel').classList.remove('active');
  AppState.currentReviewIdx = null;
}

function approveCurrentLead() {
  if (AppState.currentReviewIdx !== null) {
    // Optionally save edits from the textarea back to the state
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

async function sendApprovedEmails() {
  const verified = AppState.leads.filter(l => l.status === 'approved');

  if (verified.length === 0) {
    showToast('No leads have been approved yet to send emails.', 'error');
    return;
  }

  const jobId = AppState.currentJobId;
  if (!jobId) {
    showToast('No active campaign found. Run a campaign from the New Campaign panel first.', 'error');
    return;
  }

  try {
    const response = await fetch(`/api/campaigns/${jobId}/send_emails`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      // The backend filters the full generated-email CSV down to just
      // these approved leads (matched by email address) before sending —
      // it does NOT send to every eligible lead in the campaign.
      body: JSON.stringify({ leads: verified.map(l => ({ email: l.email, name: l.name })) }),
    });

    const result = await response.json();

    if (response.ok) {
      showToast(result.status, 'success');
      // Optionally update UI to show sending status
    } else {
      showToast(`Error sending emails: ${result.error}`, 'error');
    }
  } catch (error) {
    console.error('Error sending emails:', error);
    showToast('Failed to connect to the server to send emails.', 'error');
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
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span style="font-size:1.2rem">${type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️'}</span>
    <span>${message}</span>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}
