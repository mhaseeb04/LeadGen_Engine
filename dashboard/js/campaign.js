/**
 * campaign.js — Campaign launcher: talks to api_server.py to run the
 * scrape → enrich → analyze → generate pipeline directly from the
 * dashboard, then streams the resulting leads straight into the existing
 * triage table (AppState from app.js) — no manual CSV export/import step.
 */

const API_BASE = window.LEADGEN_API_BASE || 'http://127.0.0.1:5055';

const CampaignState = {
  selectedCategories: new Set(),
  pollTimer: null,
};

document.addEventListener('DOMContentLoaded', () => {
  loadStates();
  loadCategories();
});

async function loadStates() {
  const select = document.getElementById('campaign-state');
  try {
    const res = await fetch(`${API_BASE}/api/states`);
    const states = await res.json();
    select.innerHTML = states.map(s => `<option value="${s}">${s}</option>`).join('');
  } catch (err) {
    select.innerHTML = `<option value="">API offline — run api_server.py</option>`;
  }
}

async function loadCategories() {
  const picker = document.getElementById('category-picker');
  try {
    const res = await fetch(`${API_BASE}/api/categories`);
    const cats = await res.json();
    picker.innerHTML = cats.map(c => `
      <div class="category-chip" data-id="${c.id}" onclick="toggleCategory('${c.id}')">
        <span>${c.icon}</span><span>${c.label}</span>
      </div>
    `).join('');
  } catch (err) {
    picker.innerHTML = `<div class="progress-message">Couldn't reach the API server at ${API_BASE}. Start it with: <code>python scripts/api_server.py</code></div>`;
  }
}

function toggleCategory(id) {
  const chip = document.querySelector(`.category-chip[data-id="${id}"]`);
  if (CampaignState.selectedCategories.has(id)) {
    CampaignState.selectedCategories.delete(id);
    chip.classList.remove('selected');
  } else {
    CampaignState.selectedCategories.add(id);
    chip.classList.add('selected');
  }
}

async function runCampaign() {
  const state = document.getElementById('campaign-state').value;
  const city = document.getElementById('campaign-city').value.trim();
  const query = document.getElementById('campaign-query').value.trim();

  if (!state) {
    showToast('Pick a state to scrape first.', 'error');
    return;
  }

  const btn = document.getElementById('btn-run-campaign');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';

  const progressEl = document.getElementById('campaign-progress');
  const progressMsg = document.getElementById('campaign-progress-message');
  const progressFill = document.getElementById('campaign-progress-fill');
  progressEl.style.display = 'block';
  progressMsg.textContent = 'Submitting campaign…';
  progressFill.style.width = '5%';

  try {
    const res = await fetch(`${API_BASE}/api/campaigns`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        state,
        city,
        query,
        category_ids: Array.from(CampaignState.selectedCategories),
        dry_run: true, // dashboard-launched runs never auto-send; approval happens in Triage
      }),
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }

    const { job_id } = await res.json();
    pollCampaign(job_id);
  } catch (err) {
    showToast(`Couldn't start campaign: ${err.message}`, 'error');
    resetCampaignButton();
  }
}

const PHASE_PROGRESS = { queued: 5, scrape: 25, enrich: 50, generate: 75, send: 90, done: 100 };

function pollCampaign(jobId) {
  const progressMsg = document.getElementById('campaign-progress-message');
  const progressFill = document.getElementById('campaign-progress-fill');

  clearInterval(CampaignState.pollTimer);
  CampaignState.pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/campaigns/${jobId}`);
      const job = await res.json();

      progressMsg.textContent = job.message || job.phase;
      progressFill.style.width = `${PHASE_PROGRESS[job.phase] || 10}%`;

      if (job.status === 'done') {
        clearInterval(CampaignState.pollTimer);
        progressFill.style.width = '100%';
        await loadCampaignLeads(jobId);
        resetCampaignButton();
        if (job.summary && job.summary.leads_scraped === 0) {
          showToast(job.summary.warning || 'No leads found for this search — try a different city or category.', 'error');
        } else {
          showToast('Campaign complete — leads loaded into Triage Queue.', 'success');
        }
      } else if (job.status === 'error') {
        clearInterval(CampaignState.pollTimer);
        showToast(`Campaign failed: ${job.error}`, 'error');
        resetCampaignButton();
      }
    } catch (err) {
      clearInterval(CampaignState.pollTimer);
      showToast('Lost connection to the API server.', 'error');
      resetCampaignButton();
    }
  }, 1500);
}

async function loadCampaignLeads(jobId) {
  const res = await fetch(`${API_BASE}/api/campaigns/${jobId}/leads`);
  if (!res.ok) return;
  const rows = await res.json();

  // Remembered so app.js's sendApprovedEmails() can find it later — it
  // was previously read from a URL query param that nothing ever set,
  // which meant "Send Approved Emails" failed with "Job ID not found"
  // on every single campaign run.
  AppState.currentJobId = jobId;

  // Map API/CSV field names -> the internal shape app.js's table expects.
  AppState.leads = rows.map(r => ({
    name: r.name || r.business_name || '',
    phone: r.phone || '',
    city: r.city || '',
    strategy: r.strategy || 'no_website',
    primaryFlaw: r.primary_flaw || '',
    flawCount: parseInt(r.flaw_count || 0, 10) || 0,
    score: r.score || '',
    grade: r.grade || '',
    demoUrl: r.demo_url || '',
    emailSubject: r.email_subject || 'Quick question',
    emailBody: r.email_body || '',
    status: 'pending',
    ...r,
  }));

  updateStats();
  renderLeadsTable();
}

function resetCampaignButton() {
  const btn = document.getElementById('btn-run-campaign');
  btn.disabled = false;
  btn.textContent = '⚡ Run Campaign';
}
