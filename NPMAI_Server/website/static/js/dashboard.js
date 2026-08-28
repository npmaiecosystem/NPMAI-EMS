const severityColor = {
  healthy: '#2DD4BF', minor: '#F5A623', intermediate: '#F5A623',
  serious: '#F2712B', emergency: '#E5484D',
};
const severityBg = {
  healthy: 'rgba(45,212,191,0.12)', minor: 'rgba(245,166,35,0.12)', intermediate: 'rgba(245,166,35,0.12)',
  serious: 'rgba(242,113,43,0.12)', emergency: 'rgba(229,72,77,0.12)',
};

async function loadDashboard() {
  try {
    const resp = await fetch('/api/dashboard-data');
    if (resp.status === 401 || resp.redirected) {
      window.location.href = '/login';
      return;
    }
    const data = await resp.json();
    if (!data.ok) {
      document.getElementById('dash-loading').textContent = 'Could not load your building data: ' + data.error;
      return;
    }
    if (!data.building.has_data) {
      renderEmptyState(data);
    } else {
      renderDashboard(data);
    }
  } catch (err) {
    document.getElementById('dash-loading').textContent = 'Could not reach the server. Please refresh.';
  }
}

function renderEmptyState(data) {
  document.getElementById('dash-loading').hidden = true;
  const content = document.getElementById('dash-content');
  content.hidden = false;
  content.innerHTML = `
    <div class="empty-state">
      <div class="eyebrow">NO SENSORS REPORTING YET</div>
      <h1>${data.building.name} has no installed hardware yet.</h1>
      <p class="dash-sub" style="margin-top:14px; max-width:520px;">
        Once your site visit is complete and sensors are installed, real resistance-drift
        data will appear here automatically — this dashboard reads directly from the same
        database your building's ESP32 nodes report into.
      </p>
      <div style="margin-top:28px; display:flex; gap:14px;">
        <a href="https://wa.me/917878455129" target="_blank" class="btn btn-thermal">Book installation on WhatsApp</a>
        <button class="btn btn-ghost" id="seed-demo-btn">Load demo data to preview the dashboard</button>
      </div>
    </div>
  `;
  document.getElementById('seed-demo-btn').addEventListener('click', async (e) => {
    e.target.textContent = 'Loading demo data...';
    await fetch('/api/seed-demo', { method: 'POST' });
    loadDashboard();
  });
}


function renderDashboard(data) {
  document.getElementById('dash-loading').hidden = true;
  document.getElementById('dash-content').hidden = false;

  document.getElementById('building-name').textContent = data.building.name;
  document.getElementById('building-meta').textContent = data.building.building_id;
  document.getElementById('stat-total').textContent = data.summary.total_joints;
  document.getElementById('stat-flagged').textContent = data.summary.flagged_count;


  const grid = document.getElementById('floor-grid');
  grid.innerHTML = '';

  data.floors.forEach(floor => {
    const card = document.createElement('div');
    card.className = 'floor-card';

    const color = severityColor[floor.worst_severity] || '#5C6873';
    const bg = severityBg[floor.worst_severity] || 'rgba(92,104,115,0.12)';

    let jointsHtml = '';
    floor.joints.forEach(j => {
      jointsHtml += `
        <div class="joint-chip" data-joint='${JSON.stringify(j).replace(/'/g, "&apos;")}'>
          <div class="joint-chip-id">${j.panel_id} · ${j.breaker_id}</div>
          <div class="joint-chip-sev" style="color:${severityColor[j.severity]}">
            <span class="sev-dot" style="background:${severityColor[j.severity]}"></span>
            ${j.severity}
          </div>
        </div>
      `;
    });

    card.innerHTML = `
      <div class="floor-card-head">
        <div class="floor-badge" style="background:${bg}; color:${color}">F${floor.floor}</div>
        <div class="floor-title">Floor ${floor.floor}</div>
        <div class="floor-status-pill" style="background:${bg}; color:${color}">${floor.worst_severity}</div>
      </div>
      <div class="joint-row-list">${jointsHtml}</div>
    `;
    grid.appendChild(card);
  });

  document.querySelectorAll('.joint-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const joint = JSON.parse(chip.dataset.joint.replace(/&apos;/g, "'"));
      showJointDetail(joint);
    });
  });
}

function showJointDetail(joint) {
  const panel = document.getElementById('joint-detail');
  const body = document.getElementById('detail-body');
  const color = severityColor[joint.severity] || '#5C6873';

  body.innerHTML = `
    <div class="eyebrow" style="color:${color}">${joint.severity.toUpperCase()}</div>
    <h2 style="margin-bottom:24px;">${joint.panel_id}<br>${joint.breaker_id}</h2>
    <div class="detail-row"><span class="k">Current (I_rms)</span><span class="v">${joint.i_rms ?? '—'} A</span></div>
    <div class="detail-row"><span class="k">CUSUM (drift score)</span><span class="v">${joint.cusum ?? '—'}</span></div>
    <div class="detail-row"><span class="k">Resistance drift</span><span class="v">${joint.dr_dt_ohms_per_day ? (joint.dr_dt_ohms_per_day * 1e6).toFixed(2) + ' µΩ/day' : '—'}</span></div>
    <div class="detail-row"><span class="k">Days to critical</span><span class="v">${joint.projected_days_to_critical ?? 'Not degrading'}</span></div>
    <div class="detail-row"><span class="k">Last updated</span><span class="v">${new Date(joint.ts).toLocaleString()}</span></div>
  `;
  panel.hidden = false;
}

document.getElementById('detail-close').addEventListener('click', () => {
  document.getElementById('joint-detail').hidden = true;
});

document.getElementById('logout-btn').addEventListener('click', async () => {
  const resp = await fetch('/api/logout', { method: 'POST' });
  const data = await resp.json();
  window.location.href = data.redirect;
});

loadDashboard();
// live-ish refresh, matching the real system's polling cadence philosophy (Layer 4/5 poll every 30-60s)
setInterval(loadDashboard, 30000);
