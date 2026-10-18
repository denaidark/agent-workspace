"""Self-contained public uptime dashboard for broker.agentsweb.space."""


def dashboard_html():
    return r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#071019">
  <title>Agentsweb status</title>
  <style>
    :root { color-scheme:dark; --bg:#071019; --panel:#0c1823; --panel2:#101f2d; --line:#203244; --text:#edf7f5; --muted:#8fa5ae; --green:#46e6ad; --green2:#1da879; --red:#ff6b79; --amber:#f5bf60; --blue:#68a9ff; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif }
    * { box-sizing:border-box }
    body { margin:0; min-height:100vh; background:radial-gradient(circle at 75% -10%,#123244 0,transparent 34%),var(--bg); color:var(--text) }
    a { color:inherit }
    main { width:min(1120px,calc(100% - 32px)); margin:0 auto; padding:34px 0 64px }
    header { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:24px }
    .brand { display:flex; align-items:center; gap:13px }
    .mark { width:42px; height:42px; display:grid; place-items:center; border:1px solid #2b6659; border-radius:13px; background:linear-gradient(145deg,#123f37,#0b201e); color:var(--green); box-shadow:0 0 30px #36d9a21c; font-weight:900 }
    h1 { margin:0; font-size:25px; letter-spacing:-.03em }
    .subtitle,.quiet { color:var(--muted) }
    .subtitle { margin-top:3px; font-size:13px }
    .overall { display:flex; align-items:center; gap:9px; border:1px solid var(--line); border-radius:999px; background:#0a1721cc; padding:9px 14px; font-size:13px; font-weight:750 }
    .dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 12px var(--green) }
    .degraded .dot,.down .dot { background:var(--red); box-shadow:0 0 12px var(--red) }
    .hero { padding:26px; border:1px solid #274357; border-radius:19px; background:linear-gradient(145deg,#0e202cdd,#0a151fdd); box-shadow:0 24px 90px #0004 }
    .hero-top { display:flex; align-items:flex-start; justify-content:space-between; gap:20px }
    .eyebrow { color:var(--green); font-size:11px; font-weight:800; letter-spacing:.13em; text-transform:uppercase }
    h2 { margin:7px 0 4px; font-size:30px; letter-spacing:-.04em }
    .refresh { border:1px solid var(--line); border-radius:10px; background:#112230; color:var(--text); padding:9px 12px; cursor:pointer; font:inherit; font-size:12px; font-weight:700 }
    .refresh:hover,.refresh:focus-visible,.tab:hover,.tab:focus-visible { border-color:#4c778d; outline:none }
    .timeline { display:grid; grid-template-columns:repeat(90,minmax(2px,1fr)); gap:3px; height:54px; align-items:end; margin-top:22px }
    .bar { height:74%; border-radius:3px; background:#1a2a37 }
    .bar.ok { height:74%; background:var(--green2) }
    .bar.error { height:100%; background:var(--red) }
    .bar.empty { height:34%; opacity:.7 }
    .timeline-labels { display:flex; justify-content:space-between; color:var(--muted); font-size:11px; margin-top:8px }
    .metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0 24px }
    .metric { min-height:100px; padding:17px; border:1px solid var(--line); border-radius:14px; background:var(--panel) }
    .metric-label { color:var(--muted); font-size:12px }
    .metric-value { margin-top:8px; font-size:25px; font-weight:850; letter-spacing:-.035em }
    .metric-note { color:var(--muted); font-size:11px; margin-top:4px }
    .tabs { display:flex; gap:7px; padding:5px; width:max-content; max-width:100%; overflow:auto; border:1px solid var(--line); border-radius:12px; background:#09141d }
    .tab { border:1px solid transparent; border-radius:8px; background:transparent; color:var(--muted); padding:8px 12px; cursor:pointer; font:inherit; font-size:13px; font-weight:750; white-space:nowrap }
    .tab.active { border-color:#27564e; background:#12332e; color:var(--green) }
    .panel { display:none; margin-top:14px }
    .panel.active { display:block }
    .grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px }
    .card { min-width:0; padding:18px; border:1px solid var(--line); border-radius:14px; background:linear-gradient(150deg,var(--panel2),var(--panel)) }
    .card-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px }
    .name { font-size:16px; font-weight:800 }
    .host { margin-top:3px; color:var(--muted); font-size:11px; overflow-wrap:anywhere }
    .badge { flex:0 0 auto; border-radius:999px; padding:4px 8px; color:var(--green); background:#12362e; font-size:10px; font-weight:850; text-transform:uppercase }
    .badge.bad { color:var(--red); background:#3b1922 }
    .stats { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; margin-top:20px }
    .stat span { display:block; color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.06em }
    .stat strong { display:block; margin-top:4px; font-size:14px }
    .open { display:inline-flex; margin-top:18px; color:var(--blue); font-size:12px; font-weight:700; text-decoration:none }
    .open:hover { text-decoration:underline }
    .empty-state { padding:35px 20px; border:1px dashed var(--line); border-radius:14px; color:var(--muted); text-align:center }
    .incident { display:grid; grid-template-columns:12px 1fr auto; align-items:start; gap:11px; padding:15px 3px; border-bottom:1px solid var(--line) }
    .incident:last-child { border-bottom:0 }
    .incident-icon { width:9px; height:9px; margin-top:5px; border-radius:50%; background:var(--green) }
    .incident.down .incident-icon { background:var(--red) }
    .incident-title { font-size:13px; font-weight:750 }
    .incident-detail,.incident-time { color:var(--muted); font-size:11px }
    .incident-detail { margin-top:3px }
    footer { display:flex; justify-content:space-between; gap:20px; margin-top:25px; color:var(--muted); font-size:11px }
    .error-banner { display:none; margin-bottom:14px; padding:12px 14px; border:1px solid #71323b; border-radius:10px; background:#381820; color:#ffc2c8; font-size:12px }
    .error-banner.visible { display:block }
    .skeleton { opacity:.55; animation:pulse 1.4s infinite alternate }
    @keyframes pulse { to { opacity:.85 } }
    @media (max-width:800px) { .metrics,.grid { grid-template-columns:repeat(2,minmax(0,1fr)) } }
    @media (max-width:560px) { main { width:min(100% - 22px,1120px); padding-top:20px } header,.hero-top,footer { flex-direction:column } .overall { align-self:flex-start } .hero { padding:19px } h2 { font-size:25px } .metrics,.grid { grid-template-columns:1fr 1fr } .metric { min-height:90px; padding:14px } .metric-value { font-size:21px } .timeline { gap:1px } .incident { grid-template-columns:12px 1fr } .incident-time { grid-column:2 } }
    @media (max-width:390px) { .grid { grid-template-columns:1fr } }
    @media (prefers-reduced-motion:reduce) { .skeleton { animation:none } }
  </style>
</head>
<body>
  <main>
    <header>
      <div class="brand"><div class="mark" aria-hidden="true">A2</div><div><h1>Agentsweb status</h1><div class="subtitle">Core infrastructure and active workers</div></div></div>
      <div id="overall" class="overall skeleton" aria-live="polite"><span class="dot"></span><span id="overallText">Checking services…</span></div>
    </header>
    <div id="errorBanner" class="error-banner" role="alert"></div>
    <section class="hero" aria-labelledby="heroTitle">
      <div class="hero-top"><div><div class="eyebrow">Core availability · last 90 minutes</div><h2 id="heroTitle">Loading uptime…</h2><div id="heroNote" class="quiet">Waiting for the latest broker probe cycle.</div></div><button id="refresh" class="refresh" type="button">Refresh now</button></div>
      <div id="timeline" class="timeline" aria-label="90-minute core availability timeline"></div>
      <div class="timeline-labels"><span>90 minutes ago</span><span>Now</span></div>
    </section>
    <section class="metrics" aria-label="Status summary">
      <div class="metric"><div class="metric-label">Core uptime</div><div id="coreUptime" class="metric-value">—</div><div id="coreChecks" class="metric-note">No checks yet</div></div>
      <div class="metric"><div class="metric-label">Core services</div><div id="coreServices" class="metric-value">—</div><div class="metric-note">A2, Development, Guest</div></div>
      <div class="metric"><div class="metric-label">Active workers</div><div id="workers" class="metric-value">—</div><div class="metric-note">Discovered through FRP</div></div>
      <div class="metric"><div class="metric-label">Probe freshness</div><div id="freshness" class="metric-value">—</div><div id="freshnessNote" class="metric-note">Expected every 30 seconds</div></div>
    </section>
    <nav class="tabs" aria-label="Status views">
      <button class="tab active" data-panel="core" type="button">Core services</button>
      <button class="tab" data-panel="worker" type="button">Active workers <span id="workerTabCount"></span></button>
      <button class="tab" data-panel="incidents" type="button">Incidents</button>
    </nav>
    <section id="core" class="panel active"><div id="coreGrid" class="grid"></div></section>
    <section id="worker" class="panel"><div id="workerGrid" class="grid"></div></section>
    <section id="incidents" class="panel"><div id="incidentList" class="card"></div></section>
    <footer><span>Public status contains no registration secrets or bearer-protected event data.</span><span id="updated">Not updated yet</span></footer>
  </main>
  <script>
  (() => {
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const duration = seconds => { seconds=Math.max(0,Number(seconds)||0); if(seconds<60)return `${seconds}s`; if(seconds<3600)return `${Math.floor(seconds/60)}m`; return `${Math.floor(seconds/3600)}h`; };
    const rootUrl = url => String(url||'').replace(/\/api\/status\/?$/,'');
    function bars(items) { $('timeline').innerHTML=(items||[]).map(b => `<span class="bar ${esc(b.level||'empty')}" title="${esc(b.start)} · ${Number(b.checks)||0} checks · ${Number(b.failedChecks)||0} failed"></span>`).join(''); }
    function cards(items, target) {
      const list=items||[];
      $(target).innerHTML=list.length ? list.map(t => `<article class="card"><div class="card-head"><div><div class="name">${esc(t.name)}</div><div class="host">${esc(new URL(t.url).host)}</div></div><span class="badge ${t.ok?'':'bad'}">${t.ok?'Healthy':'Unavailable'}</span></div><div class="stats"><div class="stat"><span>90m uptime</span><strong>${Number(t.uptimePercent||0).toFixed(2)}%</strong></div><div class="stat"><span>Latency</span><strong>${t.lastLatencyMs == null ? '—' : `${Number(t.lastLatencyMs)} ms`}</strong></div><div class="stat"><span>Last check</span><strong>${duration(t.lastCheckAgeSeconds)} ago</strong></div><div class="stat"><span>HTTP</span><strong>${Number(t.lastStatus)||'—'}</strong></div></div><a class="open" href="${esc(rootUrl(t.url))}" target="_blank" rel="noreferrer">Open service ↗</a></article>`).join('') : '<div class="empty-state">No active services in this group.</div>';
    }
    function incidents(items) {
      $('incidentList').innerHTML=(items||[]).length ? items.map(i => `<div class="incident ${i.type==='target_down'?'down':''}"><span class="incident-icon"></span><div><div class="incident-title">${esc(i.name)} ${i.type==='target_down'?'became unavailable':'recovered'}</div><div class="incident-detail">${esc(i.detail||'Health state changed')}</div></div><time class="incident-time" datetime="${esc(i.time)}">${esc(i.time)}</time></div>`).join('') : '<div class="empty-state">No state changes in this window.</div>';
    }
    function render(data) {
      const s=data.summary||{}, core=s.coreTargets||[], workers=s.workerTargets||[];
      $('overall').className=`overall ${data.ok?'':'degraded'}`; $('overall').classList.remove('skeleton');
      $('overallText').textContent=data.ok?'All core services operational':'Core services degraded';
      $('heroTitle').textContent=data.ok?'All core systems operational':'Some core systems need attention';
      $('heroNote').textContent=data.ok?'Broker process, probe freshness, and all required targets are healthy.':(data.failureReasons||[]).join(' · ');
      $('coreUptime').textContent=`${Number(s.uptimePercent||0).toFixed(2)}%`;
      $('coreChecks').textContent=`${Number(s.probeCount)||0} checks · ${Number(s.failedProbeCount)||0} failed`;
      $('coreServices').textContent=`${core.filter(t=>t.ok).length} / ${core.length}`;
      $('workers').textContent=String(workers.length); $('workerTabCount').textContent=workers.length?`(${workers.length})`:'';
      $('freshness').textContent=data.probesFresh ? 'Fresh' : 'Stale'; $('freshness').style.color=data.probesFresh?'var(--green)':'var(--red)';
      $('freshnessNote').textContent=data.lastProbeAt?`Latest check ${duration(data.lastProbeAgeSeconds)} ago`:'No completed probe cycle';
      $('updated').textContent=`Updated ${new Date(data.updatedAt).toLocaleString()}`;
      bars(s.buckets); cards(core,'coreGrid'); cards(workers,'workerGrid'); incidents(s.incidents);
      $('errorBanner').classList.remove('visible');
    }
    async function refresh() {
      $('refresh').disabled=true;
      try { const response=await fetch('/api/status',{cache:'no-store'}); const data=await response.json(); render(data); }
      catch(error) { $('overall').className='overall down'; $('overallText').textContent='Status feed unavailable'; $('errorBanner').textContent=`Could not refresh status: ${error.message}`; $('errorBanner').classList.add('visible'); }
      finally { $('refresh').disabled=false; }
    }
    document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => { document.querySelectorAll('.tab,.panel').forEach(el=>el.classList.remove('active')); tab.classList.add('active'); $(tab.dataset.panel).classList.add('active'); }));
    $('refresh').addEventListener('click',refresh); refresh(); setInterval(refresh,10000);
  })();
  </script>
</body>
</html>'''
