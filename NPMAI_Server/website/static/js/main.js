// ============ NAV SCROLL + MOBILE MENU ============
const nav = document.getElementById('nav');
window.addEventListener('scroll', () => {
  if (window.scrollY > 12) nav.classList.add('scrolled');
  else nav.classList.remove('scrolled');
});
const navToggle = document.getElementById('nav-toggle');
const mobileMenu = document.getElementById('mobile-menu');
if (navToggle) {
  navToggle.addEventListener('click', () => {
    mobileMenu.hidden = !mobileMenu.hidden;
  });
}

// ============ HERO CANVAS — animated building cross-section ============
(function heroBuilding() {
  const canvas = document.getElementById('hero-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const FLOORS = 4, BREAKERS = 7;
  let grid = Array.from({length: FLOORS}, () => Array.from({length: BREAKERS}, () => 0));

  function resize() {
    const rect = canvas.parentElement.getBoundingClientRect();
    canvas.width = rect.width;
    canvas.height = rect.height;
  }
  window.addEventListener('resize', resize);
  resize();

  const colors = ['#2DD4BF', '#F5A623', '#F2712B', '#E5484D'];

  function draw() {
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    const padX = w * 0.07, padY = h * 0.04;
    const boxW = w - padX * 2, boxH = h - padY * 2;
    const floorH = boxH / FLOORS;

    ctx.strokeStyle = '#232B33';
    ctx.lineWidth = 1.5;
    ctx.strokeRect(padX, padY, boxW, boxH);

    grid.forEach((row, f) => {
      const y = padY + f * floorH;
      if (f > 0) {
        ctx.beginPath();
        ctx.moveTo(padX, y);
        ctx.lineTo(padX + boxW, y);
        ctx.strokeStyle = '#232B33';
        ctx.stroke();
      }
      ctx.fillStyle = '#5C6873';
      ctx.font = '10px IBM Plex Mono';
      ctx.fillText(`F${FLOORS - f}`, padX + 10, y + 22);

      const cellSize = Math.min(26, boxW / (BREAKERS + 2));
      const gap = cellSize * 0.35;
      const rowWidth = BREAKERS * (cellSize + gap) - gap;
      const startX = padX + (boxW - rowWidth) * 0.62;
      row.forEach((level, b) => {
        const x = startX + b * (cellSize + gap);
        const cy = y + floorH * 0.62;
        ctx.globalAlpha = level === 0 ? 0.35 : 0.95;
        ctx.fillStyle = colors[level];
        roundRect(ctx, x, cy, cellSize, cellSize, 4);
        ctx.fill();
        ctx.globalAlpha = 1;
      });
    });
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  draw();

  setInterval(() => {
    const f = Math.floor(Math.random() * FLOORS);
    const b = Math.floor(Math.random() * BREAKERS);
    if (Math.random() < 0.12) grid[f][b] = Math.min(3, grid[f][b] + 1);
    else if (Math.random() < 0.3) grid[f][b] = Math.max(0, grid[f][b] - 1);
    draw();
  }, 900);

  // floating readout card, simulated live values
  const readoutEl = document.getElementById('readout-card');
  const panels = ['panel-2F-main', 'panel-3F-main', 'panel-4F-east', 'panel-GF-incomer'];
  const breakers = ['brk-01','brk-02','brk-03','brk-04','brk-05','brk-06','brk-07'];
  const statusColor = { healthy: '#2DD4BF', intermediate: '#F5A623', serious: '#F2712B' };

  function updateReadout() {
    const isDrift = Math.random() < 0.35;
    const status = isDrift ? (Math.random() < 0.4 ? 'serious' : 'intermediate') : 'healthy';
    const drift = isDrift ? (Math.random()*1.8+0.3).toFixed(2) : (Math.random()*0.05).toFixed(2);
    const panel = panels[Math.floor(Math.random()*panels.length)];
    const breaker = breakers[Math.floor(Math.random()*breakers.length)];
    readoutEl.innerHTML = `
      <div class="readout-title">LIVE READOUT (SIMULATED)</div>
      <div class="readout-row"><span class="k">panel</span><span class="v">${panel}</span></div>
      <div class="readout-row"><span class="k">breaker</span><span class="v">${breaker}</span></div>
      <div class="readout-row"><span class="k">drift</span><span class="v">+${drift}%/day</span></div>
      <div class="readout-row"><span class="k">status</span><span class="v" style="color:${statusColor[status]}">${status}</span></div>
    `;
  }
  updateReadout();
  setInterval(updateReadout, 2600);

  // position readout card responsively
  function positionReadout() {
    readoutEl.style.position = 'absolute';
    readoutEl.style.bottom = '8%';
    readoutEl.style.right = '-2%';
  }
  positionReadout();
  window.addEventListener('resize', positionReadout);
})();

// ============ FEEDBACK LOOP DIAGRAM (canvas) ============
(function loopDiagram() {
  const canvas = document.getElementById('loop-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const stages = ['Loose joint', 'Resistance ↑', 'I²R heating', 'Thermal cycling'];
  const positions = [
    { x: W/2, y: 46 }, { x: W-50, y: H/2 }, { x: W/2, y: H-46 }, { x: 50, y: H/2 },
  ];
  let active = 0;

  function draw() {
    ctx.clearRect(0, 0, W, H);

    // dashed outer circle
    ctx.strokeStyle = '#232B33';
    ctx.setLineDash([3, 5]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(W/2, H/2, 150, 0, Math.PI*2);
    ctx.stroke();
    ctx.setLineDash([]);

    // connecting lines with arrowheads
    positions.forEach((p, i) => {
      const next = positions[(i+1) % 4];
      const isActive = active === i;
      ctx.strokeStyle = isActive ? '#F2712B' : '#232B33';
      ctx.lineWidth = isActive ? 2.5 : 1.5;
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(next.x, next.y);
      ctx.stroke();

      // arrowhead
      const angle = Math.atan2(next.y - p.y, next.x - p.x);
      const ax = p.x + (next.x - p.x) * 0.55;
      const ay = p.y + (next.y - p.y) * 0.55;
      ctx.fillStyle = isActive ? '#F2712B' : '#5C6873';
      ctx.beginPath();
      ctx.moveTo(ax + Math.cos(angle)*6, ay + Math.sin(angle)*6);
      ctx.lineTo(ax + Math.cos(angle+2.5)*5, ay + Math.sin(angle+2.5)*5);
      ctx.lineTo(ax + Math.cos(angle-2.5)*5, ay + Math.sin(angle-2.5)*5);
      ctx.closePath();
      ctx.fill();
    });

    // nodes
    positions.forEach((p, i) => {
      const isActive = active === i;
      const r = isActive ? 46 : 40;
      ctx.fillStyle = isActive ? '#171D24' : '#12171D';
      ctx.strokeStyle = isActive ? '#F2712B' : '#313B45';
      ctx.lineWidth = isActive ? 2 : 1.2;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI*2);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = isActive ? '#E9EEF2' : '#94A3B0';
      ctx.font = (isActive ? '600 ' : '400 ') + '12.5px IBM Plex Sans';
      ctx.textAlign = 'center';
      const words = stages[i].split(' ');
      words.forEach((word, wi) => {
        ctx.fillText(word, p.x, p.y + (wi === 0 ? -3 : 13));
      });
    });
  }

  draw();
  setInterval(() => { active = (active + 1) % 4; draw(); }, 1100);
})();

// ============ SCALE VISUALIZER — genuinely interactive, plain JS ============
(function scaleVisualizer() {
  const stage = document.getElementById('visualizer-stage');
  if (!stage) return;

  const sliders = {
    buildings: document.getElementById('s-buildings'),
    floors: document.getElementById('s-floors'),
    panels: document.getElementById('s-panels'),
    breakers: document.getElementById('s-breakers'),
  };
  const labels = {
    buildings: document.getElementById('v-buildings'),
    floors: document.getElementById('v-floors'),
    panels: document.getElementById('v-panels'),
    breakers: document.getElementById('v-breakers'),
  };
  const totalEl = document.getElementById('v-total');

  function render() {
    const b = +sliders.buildings.value, f = +sliders.floors.value, p = +sliders.panels.value, k = +sliders.breakers.value;
    labels.buildings.textContent = b;
    labels.floors.textContent = f;
    labels.panels.textContent = p;
    labels.breakers.textContent = k;
    totalEl.textContent = (b * f * p * k).toLocaleString();

    let html = '';
    for (let bi = 0; bi < b; bi++) {
      html += `<div class="building-block"><div class="building-block-label">BUILDING ${bi+1} / ${b}</div>`;
      for (let fi = 0; fi < f; fi++) {
        html += `<div class="floor-row"><div class="floor-label">F${f - fi}</div>`;
        for (let pi = 0; pi < p; pi++) {
          html += `<div class="node-row">`;
          for (let ni = 0; ni < k; ni++) {
            html += `<div class="node-dot" data-key="${bi}-${fi}-${ni}"></div>`;
          }
          html += `</div>`;
        }
        html += `</div>`;
      }
      html += `</div>`;
    }
    stage.innerHTML = html;
  }

  Object.values(sliders).forEach(s => s.addEventListener('input', render));
  render();

  // ambient pulse animation - picks a random visible dot and flashes it
  setInterval(() => {
    const dots = stage.querySelectorAll('.node-dot');
    if (!dots.length) return;
    const dot = dots[Math.floor(Math.random() * dots.length)];
    dot.classList.add('pulse');
    setTimeout(() => dot.classList.remove('pulse'), 260);
  }, 180);
})();
