/**
 * campaign.js — Campaign launcher: talks to api_server.py to run the
 * scrape → enrich → analyze → generate pipeline directly from the
 * dashboard, then streams the resulting leads straight into the existing
 * triage table (AppState from app.js) — no manual CSV export/import step.
 */

const API_BASE = window.LEADGEN_API_BASE || 'https://leadgen-engine-ngxx.onrender.com';

// Must match API_SECRET on Render. For a static dashboard this is visible
// in page source — still blocks random scrapers; upgrade to login later.
const API_KEY = window.LEADGEN_API_KEY || 'CHANGE_ME_TO_A_LONG_RANDOM_SECRET';

function apiHeaders(extra) {
  const h = Object.assign({ 'Content-Type': 'application/json' }, extra || {});
  if (API_KEY && API_KEY !== 'CHANGE_ME_TO_A_LONG_RANDOM_SECRET') {
    h['X-API-Key'] = API_KEY;
  }
  return h;
}

const CampaignState = {
  selectedCategories: new Set(),
  pollTimer: null,
  stateWideConfirmed: false,
};

document.addEventListener('DOMContentLoaded', () => {
  loadStates();
  loadCategories();
  ensureAiControls();   // self-heal: build AI Fill + cache controls if the HTML predates them
  refreshCacheBadge();
});

// ═══════════════════════════════════════════════════
// Self-sufficient UI: if the served index.html is an older version that
// lacks the ✨ AI Fill button or the cache toggle/badge, CREATE them from
// JS. This decouples features from HTML deployment — as long as this
// campaign.js loads, the controls exist.
// ═══════════════════════════════════════════════════
function ensureAiControls() {
  // --- ✨ AI Fill button next to the query input ---
  const queryEl = document.getElementById('campaign-query');
  if (queryEl && !document.getElementById('btn-ai-parse')) {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display:flex;gap:10px;align-items:center;';
    queryEl.parentNode.insertBefore(wrap, queryEl);
    queryEl.style.flex = '1';
    queryEl.placeholder = "✨ Try: 'fetch real estate businesses in Miami' — AI fills the fields below";
    wrap.appendChild(queryEl);
    const btn = document.createElement('button');
    btn.id = 'btn-ai-parse';
    btn.className = 'btn btn-secondary';
    btn.title = 'Let AI extract state, city, and categories from your text';
    btn.textContent = '✨ AI Fill';
    btn.onclick = aiParseQuery;
    wrap.appendChild(btn);
  }
  // Enter in the query box triggers AI Fill (bind once).
  if (queryEl && !queryEl.dataset.aiBound) {
    queryEl.dataset.aiBound = '1';
    queryEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); aiParseQuery(); }
    });
  }

  // --- Force-fresh toggle + cache badge next to Run Campaign ---
  const runBtn = document.getElementById('btn-run-campaign');
  if (runBtn && !document.getElementById('force-refresh-toggle')) {
    const actions = document.createElement('div');
    actions.className = 'campaign-actions';
    actions.style.cssText = 'display:flex;align-items:center;gap:14px;flex:0 0 auto;';
    runBtn.parentNode.insertBefore(actions, runBtn);
    const label = document.createElement('label');
    label.title = 'Ignore cached results and scrape this area fresh';
    label.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:0.85rem;color:var(--text-secondary,#9aa);cursor:pointer;';
    label.innerHTML = '<input type="checkbox" id="force-refresh-toggle" style="cursor:pointer;"><span>🔄 Force fresh scrape</span>';
    const badge = document.createElement('span');
    badge.id = 'cache-badge';
    badge.style.cssText = 'font-size:0.8rem;color:var(--text-secondary,#9aa);';
    actions.appendChild(label);
    actions.appendChild(badge);
    actions.appendChild(runBtn);
  }
}

// Fetch cache stats and show a small badge (e.g. "⚡ 240 leads cached").
// Non-fatal: if the endpoint is unavailable the badge simply stays empty.
async function refreshCacheBadge() {
  const el = document.getElementById('cache-badge');
  if (!el) return;
  try {
    const res = await fetch(`${API_BASE}/api/cache/stats`, { headers: apiHeaders() });
    if (!res.ok) { el.textContent = ''; return; }
    const s = await res.json();
    const fresh = (s && typeof s.rows_fresh === 'number') ? s.rows_fresh : 0;
    el.textContent = fresh > 0 ? `⚡ ${fresh} leads cached` : '';
    el.title = fresh > 0
      ? `${fresh} fresh cached leads across ${s.distinct_businesses} businesses (TTL ${s.ttl_days} days)`
      : '';
  } catch {
    el.textContent = '';
  }
}

async function loadStates() {
  const select = document.getElementById('campaign-state');
  try {
    const res = await fetch(`${API_BASE}/api/states`);
    const states = await res.json();
    select.innerHTML = `<option value="">Select a state…</option>` +
      states.map(s => `<option value="${s}">${s}</option>`).join('');
    select.addEventListener('change', () => loadCitiesForState(select.value));
  } catch (err) {
    select.innerHTML = `<option value="">API offline — run api_server.py</option>`;
  }
}

// Populate a <datalist> of cities for the chosen state so the operator can
// pick from a real, comprehensive list — while the field STAYS a free-text
// input (so unlisted towns can still be typed). Leaving it blank is always
// valid: it just means a state-wide search.
async function loadCitiesForState(state) {
  const cityInput = document.getElementById('campaign-city');
  let datalist = document.getElementById('city-datalist');
  if (!datalist) {
    datalist = document.createElement('datalist');
    datalist.id = 'city-datalist';
    cityInput.setAttribute('list', 'city-datalist');
    cityInput.parentNode.appendChild(datalist);
  }
  cityInput.placeholder = 'Start typing, pick from the list, or leave blank for the whole state';
  if (!state) { datalist.innerHTML = ''; return; }
  try {
    const res = await fetch(`${API_BASE}/api/cities?state=${encodeURIComponent(state)}`, { headers: apiHeaders() });
    const cities = await res.json();
    datalist.innerHTML = Array.isArray(cities) ? cities.map(c => `<option value="${c}">`).join('') : '';
  } catch {
    datalist.innerHTML = '';
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

// ═══════════════════════════════════════════════════
// ✨ AI Query Parse — "Copilot-style" natural language input.
// Types "fetch real estate businesses in Miami" -> fills State=Florida,
// City=Miami, selects the Real Estate chip. The user SEES what was
// understood and can correct anything before running.
// ═══════════════════════════════════════════════════
async function aiParseQuery() {
  const queryEl = document.getElementById('campaign-query');
  const q = queryEl.value.trim();
  if (!q) {
    showToast('Type what you want first — e.g. "fetch real estate businesses in Miami".', 'error');
    return;
  }

  const btn = document.getElementById('btn-ai-parse');
  const orig = btn.textContent;
  btn.textContent = '✨ Parsing…'; btn.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/api/parse_query`, {
      method: 'POST', headers: apiHeaders(), body: JSON.stringify({ query: q }),
    });
    const p = await res.json();
    if (!res.ok) throw new Error(p.error || `HTTP ${res.status}`);

    let filled = [];
    // State (select)
    if (p.state) {
      const sel = document.getElementById('campaign-state');
      if ([...sel.options].some(o => o.value === p.state)) {
        sel.value = p.state; filled.push(`State: ${p.state}`);
        loadCitiesForState(p.state); // populate the city datalist for the parsed state
      }
    }
    // City (text input)
    if (p.city) {
      document.getElementById('campaign-city').value = p.city;
      filled.push(`City: ${p.city}`);
    }
    // Categories (chips) — clear current selection, select parsed ones.
    if (p.category_ids && p.category_ids.length) {
      CampaignState.selectedCategories.clear();
      document.querySelectorAll('.category-chip.selected').forEach(c => c.classList.remove('selected'));
      p.category_ids.forEach(id => {
        const chip = document.querySelector(`.category-chip[data-id="${id}"]`);
        if (chip) { chip.classList.add('selected'); CampaignState.selectedCategories.add(id); }
      });
      filled.push(`Categories: ${p.category_ids.join(', ')}`);
    }

    if (filled.length) {
      // The free-text has been converted into structured fields, so clear
      // it — otherwise it would ALSO be sent as a business-name filter and
      // (a) wrongly narrow results, (b) bypass the leads cache.
      queryEl.value = '';
      const missing = (p.needs || []).length
        ? ` Still needed: ${p.needs.join(' & ').replace('category_ids','category')}.`
        : ' Review below, then Run Campaign.';
      showToast(`✨ Understood — ${filled.join(' · ')}.${missing}`, 'success');
    } else {
      showToast('Couldn\'t extract campaign details from that. Try naming a business type and a city, e.g. "salons in Denver".', 'error');
    }
  } catch (err) {
    showToast(`AI parse failed: ${err.message}`, 'error');
  } finally {
    btn.textContent = orig; btn.disabled = false;
  }
}

// (Enter-to-parse is bound once inside ensureAiControls with a guard.)

async function runCampaign() {
  const state = document.getElementById('campaign-state').value;
  const city = document.getElementById('campaign-city').value.trim();
  const query = document.getElementById('campaign-query').value.trim();

    if (!state) {
    showToast('Pick a state to scrape first.', 'error');
    return;
  }
  // City is OPTIONAL: no city = state-wide search. We no longer hard-block
  // this — instead we ask for a quick confirmation (state-wide is slower
  // and the city dropdown makes it easy to narrow down first).
  if (!city && !CampaignState.stateWideConfirmed) {
    showToast('No city selected — this will search the WHOLE state (slower). Click Run Campaign again to confirm, or pick a city above.', 'info');
    CampaignState.stateWideConfirmed = true;
    setTimeout(() => { CampaignState.stateWideConfirmed = false; }, 8000); // confirmation window
    return;
  }
  CampaignState.stateWideConfirmed = false;

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
      headers: apiHeaders(),
      body: JSON.stringify({
        state,
        city,
        query,
        category_ids: Array.from(CampaignState.selectedCategories),
        dry_run: true,          // never auto-send; approval happens in Triage
        generate_emails: false, // triage-first: leads load fast, emails generated on demand per lead
        force_refresh: !!(document.getElementById('force-refresh-toggle') || {}).checked, // bypass cache when ticked
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
  CampaignState.pollFinished = false; // guards against overlapping async ticks
  CampaignState.pollTimer = setInterval(async () => {
    // A tick may still be in flight (awaiting fetch/loadCampaignLeads) when
    // the next fires. Without this guard, several ticks can each enter the
    // 'done' branch and stack duplicate "Campaign complete" toasts.
    if (CampaignState.pollFinished) return;
    try {
      // apiHeaders() is required here: this endpoint is auth-protected,
      // so polling without the X-API-Key gets a 401 the moment
      // API_SECRET_KEY is set in production — the campaign would start
      // fine (runCampaign sends headers) and then every poll would fail
      // as a fake "Lost connection".
      const res = await fetch(`${API_BASE}/api/campaigns/${jobId}`, { headers: apiHeaders() });
      const job = await res.json();

      if (CampaignState.pollFinished) return; // finished while this fetch was in flight

      progressMsg.textContent = job.message || job.phase;
      progressFill.style.width = `${PHASE_PROGRESS[job.phase] || 10}%`;

      if (job.status === 'done') {
        CampaignState.pollFinished = true;
        clearInterval(CampaignState.pollTimer);
        progressFill.style.width = '100%';
        await loadCampaignLeads(jobId);
        resetCampaignButton();
        if (job.summary && job.summary.leads_scraped === 0) {
          showToast(job.summary.warning || 'No leads found for this search — try a different city or category.', 'error');
        } else {
          const fromCache = job.summary && job.summary.source === 'cache';
          showToast(
            fromCache
              ? '⚡ Loaded instantly from cache — leads in Triage Queue.'
              : 'Campaign complete — leads loaded into Triage Queue.',
            'success'
          );
        }
        refreshCacheBadge();
      } else if (job.status === 'error') {
        CampaignState.pollFinished = true;
        clearInterval(CampaignState.pollTimer);
        showToast(`Campaign failed: ${job.error}`, 'error');
        resetCampaignButton();
      }
    } catch (err) {
      CampaignState.pollFinished = true;
      clearInterval(CampaignState.pollTimer);
      showToast('Lost connection to the API server.', 'error');
      resetCampaignButton();
    }
  }, 1500);
}

async function loadCampaignLeads(jobId) {
  const res = await fetch(`${API_BASE}/api/campaigns/${jobId}/leads`, { headers: apiHeaders() });
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
