from __future__ import annotations

import json
import logging

from flask import Flask, Response, request

from . import __version__
from .controls import DELTA_PRO_COMMANDS, SHP_COMMANDS, DeviceController
from .mqtt_client import EcoFlowMqttClient

log = logging.getLogger(__name__)

# Global references set by run_web()
_mqtt: EcoFlowMqttClient | None = None
_device_types: dict[str, str] = {}
_device_names: dict[str, str] = {}
_controller: DeviceController | None = None

app = Flask(__name__)


@app.route("/")
def index() -> str:
    return HTML_PAGE


@app.route("/api/devices")
def api_devices() -> Response:
    if not _mqtt:
        return Response("{}", content_type="application/json")
    result = {}
    for sn, dtype in _device_types.items():
        data = _mqtt.get_device_data(sn)
        # Convert all values to JSON-serializable types
        clean = {}
        for k, v in data.items():
            if isinstance(v, (int, float, str, bool, type(None))):
                clean[k] = v
            else:
                clean[k] = str(v)
        result[sn] = {
            "type": dtype,
            "name": _device_names.get(sn, sn),
            "data": clean,
        }
    return Response(
        json.dumps({"connected": _mqtt.connected, "version": __version__, "devices": result}),
        content_type="application/json",
    )


@app.route("/api/command", methods=["POST"])
def api_command() -> Response:
    if not _mqtt or not _controller:
        return Response(json.dumps({"error": "not ready"}), status=503, content_type="application/json")
    body = request.get_json(silent=True) or {}
    sn = body.get("sn", "")
    key = body.get("key", "")
    if not sn or not key:
        return Response(json.dumps({"error": "sn and key required"}), status=400, content_type="application/json")
    result = _controller.handle_key(key, sn)
    return Response(json.dumps({"result": result or "no action"}), content_type="application/json")


def run_web(
    mqtt_client: EcoFlowMqttClient,
    device_types: dict[str, str],
    device_names: dict[str, str],
    port: int = 5000,
) -> None:
    global _mqtt, _device_types, _device_names, _controller
    _mqtt = mqtt_client
    _device_types = device_types
    _device_names = device_names
    _controller = DeviceController(mqtt_client, device_types)

    log.info("Starting web dashboard on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# Inline HTML — self-contained dashboard page
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>EcoFlow Dashboard</title>
<style>
  :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #e6edf3;
          --dim: #8b949e; --green: #3fb950; --red: #f85149; --yellow: #d29922;
          --cyan: #58a6ff; --blue: #1f6feb; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
         font-size: 13px; background: var(--bg); color: var(--text);
         padding: 8px; min-height: 100vh; }
  .header { display: flex; align-items: center; gap: 12px; padding: 8px 0;
            flex-wrap: wrap; }
  .header h1 { font-size: 16px; font-weight: 700; }
  .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .badge-green { background: #238636; color: #fff; }
  .badge-red { background: #da3633; color: #fff; }
  .badge-dim { background: var(--border); color: var(--dim); }
  .update-banner { background: #3d2e00; border: 1px solid var(--yellow);
                   padding: 6px 12px; border-radius: 6px; margin: 6px 0;
                   color: var(--yellow); font-size: 12px; }
  .grid { display: grid; gap: 8px; margin-top: 8px; }
  @media (min-width: 768px) { .grid-2 { grid-template-columns: 1fr 1fr; } }
  .card { background: var(--card); border: 1px solid var(--border);
          border-radius: 8px; padding: 12px; }
  .card-title { font-size: 14px; font-weight: 700; margin-bottom: 8px;
                display: flex; align-items: center; gap: 8px; }
  .soc { font-size: 28px; font-weight: 800; }
  .soc-green { color: var(--green); }
  .soc-yellow { color: var(--yellow); }
  .soc-red { color: var(--red); }
  .bar-bg { background: var(--border); border-radius: 4px; height: 8px;
            margin: 6px 0; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .bar-green { background: var(--green); }
  .bar-yellow { background: var(--yellow); }
  .bar-red { background: var(--red); }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 12px;
           font-size: 12px; margin-top: 8px; }
  .stat-label { color: var(--dim); }
  .stat-value { text-align: right; }
  .stat-green { color: var(--green); }
  .stat-red { color: var(--red); }
  .circuits { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
  .circuits th { text-align: left; color: var(--dim); font-weight: 400;
                 padding: 2px 6px; border-bottom: 1px solid var(--border); }
  .circuits td { padding: 2px 6px; }
  .circuits .power { text-align: right; font-weight: 600; }
  .circuits .dp-row { color: var(--cyan); }
  .controls { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .btn { padding: 6px 14px; border-radius: 6px; border: 1px solid var(--border);
         background: var(--card); color: var(--text); font-family: inherit;
         font-size: 12px; cursor: pointer; transition: all 0.15s; }
  .btn:hover { background: var(--border); }
  .btn:active { transform: scale(0.95); }
  .btn-on { border-color: var(--green); color: var(--green); }
  .btn-off { border-color: var(--dim); color: var(--dim); }
  .toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: var(--green); color: #000; padding: 8px 20px;
           border-radius: 8px; font-weight: 600; opacity: 0;
           transition: opacity 0.3s; z-index: 100; }
  .toast.show { opacity: 1; }
  .subtitle { color: var(--dim); font-size: 11px; margin-top: 4px; }
  .health { font-size: 11px; margin-top: 4px; }
  .health-green { color: var(--green); }
  .health-yellow { color: var(--yellow); }
  .health-red { color: var(--red); }
  .section-title { color: var(--dim); font-size: 11px; text-transform: uppercase;
                   letter-spacing: 1px; margin-top: 10px; margin-bottom: 4px; }
</style>
</head>
<body>
<div class="header">
  <h1>EcoFlow Dashboard</h1>
  <span class="badge badge-dim" id="version"></span>
  <span class="badge" id="mqtt-badge">--</span>
  <span style="color:var(--dim);font-size:12px" id="clock"></span>
</div>
<div id="update-banner"></div>
<div id="dashboard"></div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);

function socColor(v) { return v >= 60 ? 'green' : v >= 20 ? 'yellow' : 'red'; }
function fmtW(w) { return Math.abs(w) >= 1000 ? (w/1000).toFixed(1)+' kW' : Math.round(w)+' W'; }
function fmtWh(wh) { return wh >= 1000 ? (wh/1000).toFixed(2)+' kWh' : Math.round(wh)+' Wh'; }
function fmtTime(m) {
  m = Math.round(m);
  if (m <= 0) return '--';
  if (m >= 1440) return Math.floor(m/1440)+'d '+Math.floor((m%1440)/60)+'h';
  return Math.floor(m/60)+'h '+m%60+'m';
}
function fmtVer(n) {
  n = Math.round(n);
  if (n <= 0) return '--';
  return ((n>>24)&0xFF)+'.'+((n>>16)&0xFF)+'.'+((n>>8)&0xFF)+'.'+(n&0xFF);
}
function g(d, ...keys) {
  for (const k of keys) { const v = d[k]; if (v !== undefined && v !== null && v !== 0) return Number(v); }
  for (const k of keys) { const v = d[k]; if (v !== undefined && v !== null) return Number(v); }
  return 0;
}

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

async function sendCmd(sn, key) {
  try {
    const r = await fetch('/api/command', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({sn, key})
    });
    const j = await r.json();
    toast(j.result || 'sent');
  } catch(e) { toast('error: '+e.message); }
}

function buildDeltaPro(sn, name, d) {
  const soc = g(d,'ems.lcdShowSoc','ems.f32LcdShowSoc','bmsMaster.f32ShowSoc','bmsMaster.soc');
  const soh = g(d,'bmsMaster.soh');
  const c = socColor(soc);
  const totalIn = g(d,'pd.wattsInSum');
  const totalOut = g(d,'pd.wattsOutSum');
  const acOut = g(d,'inv.outputWatts');
  const solarIn = g(d,'mppt.inWatts');
  const acIn = g(d,'inv.inputWatts');
  const car = g(d,'pd.carWatts');
  const volts = g(d,'bmsMaster.vol')/1000;
  const amps = g(d,'bmsMaster.amp');
  const current = Math.abs(amps) > 100 ? amps/1000 : amps;
  const cycles = Math.round(g(d,'bmsMaster.cycles'));
  const chg = g(d,'ems.chgRemainTime');
  const dsg = g(d,'ems.dsgRemainTime');
  const isChg = totalIn > totalOut && totalIn > 0;
  const timeLabel = isChg ? 'Charge' : totalOut > 0 ? 'Discharge' : 'Idle';
  const timeVal = isChg ? fmtTime(chg) : totalOut > 0 ? fmtTime(dsg) : '--';
  const battTemp = Math.round(g(d,'bmsMaster.temp'));
  const invTemp = Math.round(g(d,'inv.outTemp'));
  const dcBus = g(d,'mppt.outWatts');
  const acEnabled = g(d,'inv.cfgAcEnabled');
  const dcEnabled = g(d,'mppt.carState');
  const minCell = g(d,'bmsMaster.minCellVol'); const maxCell = g(d,'bmsMaster.maxCellVol');
  const minV = minCell > 100 ? minCell/1000 : minCell;
  const maxV = maxCell > 100 ? maxCell/1000 : maxCell;
  const delta = ((maxV - minV)*1000).toFixed(0);

  const sohC = soh >= 80 ? 'green' : soh >= 60 ? 'yellow' : 'red';
  const sohLabel = soh >= 90 ? 'Excellent' : soh >= 80 ? 'Good' : soh >= 60 ? 'Fair' : 'Poor';

  return `<div class="card">
    <div class="card-title">${name} <span style="color:var(--dim);font-size:11px">${sn.slice(-6)}</span></div>
    <div class="soc soc-${c}">${Math.round(soc)}%</div>
    <div class="bar-bg"><div class="bar-fill bar-${c}" style="width:${Math.min(100,Math.max(0,soc))}%"></div></div>
    <div class="health health-${sohC}">Health: ${Math.round(soh)}% (${sohLabel})</div>
    <div class="stats">
      <span class="stat-label">Solar In</span><span class="stat-value stat-green">${fmtW(solarIn)}</span>
      <span class="stat-label">AC In</span><span class="stat-value stat-green">${fmtW(acIn)}</span>
      <span class="stat-label">AC Out</span><span class="stat-value stat-red">${fmtW(acOut)}</span>
      <span class="stat-label">12V/Car</span><span class="stat-value stat-red">${fmtW(car)}</span>
      <span class="stat-label">Total In</span><span class="stat-value stat-green">${fmtW(totalIn)}</span>
      <span class="stat-label">Total Out</span><span class="stat-value stat-red">${fmtW(totalOut)}</span>
      <span class="stat-label">${timeLabel}</span><span class="stat-value">${timeVal}</span>
      <span class="stat-label">DC Bus</span><span class="stat-value">${fmtW(dcBus)}</span>
      <span class="stat-label">Voltage</span><span class="stat-value">${volts.toFixed(1)} V</span>
      <span class="stat-label">Current</span><span class="stat-value" style="color:var(${current>0?'--green':'--red'})">${current.toFixed(1)} A</span>
      <span class="stat-label">Cell V</span><span class="stat-value">${minV.toFixed(2)}-${maxV.toFixed(2)} V <span style="color:var(${delta<=20?'--green':delta<=50?'--yellow':'--red'});">\u0394${delta}mV</span></span>
      <span class="stat-label">Batt / Inv</span><span class="stat-value">${battTemp}\u00b0 / ${invTemp}\u00b0C</span>
      <span class="stat-label">Cycles</span><span class="stat-value">${cycles}</span>
      <span class="stat-label">Limits</span><span class="stat-value">${Math.round(g(d,'ems.minDsgSoc'))}%-${Math.round(g(d,'ems.maxChargeSoc'))}%</span>
    </div>
    <div class="controls">
      <button class="btn ${acEnabled?'btn-on':'btn-off'}" onclick="sendCmd('${sn}','a')">AC ${acEnabled?'ON':'OFF'}</button>
      <button class="btn ${dcEnabled?'btn-on':'btn-off'}" onclick="sendCmd('${sn}','d')">DC ${dcEnabled?'ON':'OFF'}</button>
      <button class="btn" onclick="sendCmd('${sn}','x')">XBoost</button>
      <button class="btn" onclick="sendCmd('${sn}','c')">Chg ${g(d,'inv.cfgSlowChgWatts')>0?fmtW(g(d,'inv.cfgSlowChgWatts')):'PAUSED'}</button>
      <button class="btn" onclick="sendCmd('${sn}','=')">Chg+5%</button>
      <button class="btn" onclick="sendCmd('${sn}','-')">Chg-5%</button>
    </div>
  </div>`;
}

function buildSHP(sn, name, d, allDevices) {
  const gridSta = g(d,'gridSta','heartbeat.gridSta');
  const gridVol = g(d,'gridInfo.gridVol','gridVol');
  const gridFreq = g(d,'gridInfo.gridFreq','gridFreq');
  const gridDay = g(d,'gridDayWatth','heartbeat.gridDayWatth');
  const backupDay = g(d,'backupDayWatth','heartbeat.backupDayWatth');
  const combinedSoc = g(d,'backupBatPer','heartbeat.backupBatPer');
  const c = socColor(combinedSoc);
  const eps = d['eps'];

  let circuitHTML = '';
  let totalLoad = 0;
  for (let i = 0; i < 12; i++) {
    const w = g(d,`infoList.${i}.chWatt`,`loadCmdChCtrlInfos.${i}.ctrlWatt`);
    totalLoad += w;
    const isDp = i >= 10;
    const cls = isDp ? ' class="dp-row"' : '';
    let label = '';
    if (isDp) {
      const dpList = Object.entries(allDevices).filter(([s,v]) => v.type.includes('delta'));
      const dpIdx = i - 10;
      if (dpIdx < dpList.length) label = 'DP.'+dpList[dpIdx][0].slice(-4);
    }
    circuitHTML += `<tr${cls}><td>${i+1}</td><td>${label}</td><td class="power">${fmtW(w)}</td></tr>`;
  }

  return `<div class="card">
    <div class="card-title">${name} <span style="color:var(--dim);font-size:11px">${sn.slice(-6)}</span></div>
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span>Grid: <b style="color:var(${gridSta?'--green':'--red'})">${gridSta?'ON':'OFF'}</b></span>
      ${gridVol ? `<span style="color:var(--dim)">${Math.round(gridVol)}V ${Math.round(gridFreq)}Hz</span>` : ''}
      <span>EPS: <b style="color:var(${eps?'--yellow':'--dim'})">${eps?'ON':'OFF'}</b></span>
    </div>
    <div class="stats" style="margin-top:6px">
      <span class="stat-label">Combined</span><span class="stat-value soc-${c}">${Math.round(combinedSoc)}%</span>
      <span class="stat-label">Grid Today</span><span class="stat-value">${fmtWh(gridDay)}</span>
      <span class="stat-label">Backup Today</span><span class="stat-value">${fmtWh(backupDay)}</span>
      <span class="stat-label">Total Load</span><span class="stat-value" style="font-weight:700">${fmtW(totalLoad)}</span>
    </div>
    <div class="section-title">Circuits</div>
    <table class="circuits"><tr><th>#</th><th>Name</th><th style="text-align:right">Power</th></tr>${circuitHTML}</table>
    <div class="controls">
      <button class="btn ${eps?'btn-on':'btn-off'}" onclick="sendCmd('${sn}','e')">EPS ${eps?'ON':'OFF'}</button>
      <button class="btn" onclick="sendCmd('${sn}','g')">Grid Chg B1</button>
      <button class="btn" onclick="sendCmd('${sn}','h')">Grid Chg B2</button>
    </div>
  </div>`;
}

async function refresh() {
  try {
    const r = await fetch('/api/devices');
    const j = await r.json();

    $('#version').textContent = 'v' + j.version;
    const mb = $('#mqtt-badge');
    mb.textContent = j.connected ? 'MQTT Connected' : 'MQTT Disconnected';
    mb.className = 'badge ' + (j.connected ? 'badge-green' : 'badge-red');
    $('#clock').textContent = new Date().toLocaleTimeString();

    const devs = j.devices || {};
    const deltas = [], shps = [];
    for (const [sn, info] of Object.entries(devs)) {
      if (info.type.includes('delta')) deltas.push([sn, info]);
      else if (info.type.includes('panel')) shps.push([sn, info]);
    }

    let html = '<div class="grid grid-2">';
    for (const [sn, info] of deltas) {
      html += buildDeltaPro(sn, info.name, info.data);
    }
    html += '</div>';
    for (const [sn, info] of shps) {
      html += '<div class="grid">' + buildSHP(sn, info.name, info.data, devs) + '</div>';
    }
    $('#dashboard').innerHTML = html;
  } catch(e) {
    console.error('refresh error', e);
  }
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>"""
