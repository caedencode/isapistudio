import argparse
import datetime
import getpass
import ipaddress
import json
import re
import socket
import sys
import threading
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests
import urllib3
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

APP_NAME = "ISAPI Studio"
VERSION = "1.0.0"
COLS, ROWS = 22, 18
CFG = {}
SERVER = None

SECTIONS = {
    "Device info": "/ISAPI/System/deviceInfo",
    "Time": "/ISAPI/System/time",
    "NTP servers": "/ISAPI/System/time/ntpServers",
    "Network (eth0)": "/ISAPI/System/Network/interfaces/1",
    "Image / color": "/ISAPI/Image/channels/{ch}/color",
    "Day/night (IR cut)": "/ISAPI/Image/channels/{ch}/ircutFilter",
    "Video input": "/ISAPI/System/Video/inputs/channels/{ch}",
    "OSD / overlays": "/ISAPI/System/Video/inputs/channels/{ch}/overlays",
    "Main stream": "/ISAPI/Streaming/channels/{ch}01",
    "Sub stream": "/ISAPI/Streaming/channels/{ch}02",
    "Users (read)": "/ISAPI/Security/users",
}


def cam_request(method, path, **kw):
    if not CFG:
        raise RuntimeError("Not connected to a camera yet.")
    url = f"{CFG['scheme']}://{CFG['ip']}:{CFG['cport']}{path}"
    for auth in (HTTPDigestAuth(CFG["user"], CFG["password"]),
                 HTTPBasicAuth(CFG["user"], CFG["password"])):
        r = requests.request(method, url, auth=auth, timeout=15, verify=False, **kw)
        if r.status_code != 401:
            return r
    return r


def fmt(path):
    if not CFG:
        raise RuntimeError("Not connected to a camera yet.")
    return path.format(ch=CFG["channel"])


def motion_path():
    return fmt("/ISAPI/System/Video/inputs/channels/{ch}/motionDetection")


def backup(name, xml):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    fn = f"backup_{CFG['ip']}_{label}_{stamp}.xml"
    with open(fn, "w", encoding="utf-8") as f:
        f.write(xml)
    return fn


def decode_grid(hexstr):
    grid = [[0] * COLS for _ in range(ROWS)]
    hexstr = hexstr.strip()
    if len(hexstr) < ROWS * 6:
        return grid
    for r in range(ROWS):
        v = int(hexstr[r * 6:(r + 1) * 6], 16)
        for c in range(COLS):
            grid[r][c] = (v >> (23 - c)) & 1
    return grid


def encode_grid(grid):
    out = []
    for r in range(ROWS):
        v = 0
        for c in range(COLS):
            if grid[r][c]:
                v |= 1 << (23 - c)
        out.append(f"{v:06x}")
    return "".join(out)


def get_current():
    global COLS, ROWS
    r = cam_request("GET", motion_path())
    if r.status_code != 200:
        raise RuntimeError(f"Camera returned {r.status_code}: {r.text[:300]}")
    xml = r.text
    cols = re.search(r"<columnGranularity>(\d+)</columnGranularity>", xml)
    rows = re.search(r"<rowGranularity>(\d+)</rowGranularity>", xml)
    if cols and rows and int(cols.group(1)) <= 24:
        COLS, ROWS = int(cols.group(1)), int(rows.group(1))
    grid = re.search(r"<gridMap>(.*?)</gridMap>", xml, re.S)
    sens = re.search(r"<sensitivityLevel>(\d+)</sensitivityLevel>", xml)
    return xml, (decode_grid(grid.group(1)) if grid else None), int(sens.group(1)) if sens else 50


def apply_motion(grid, sensitivity):
    xml, _, _ = get_current()
    fn = backup("motion", xml)
    if "<gridMap>" not in xml:
        raise RuntimeError("This firmware's motion config has no gridMap; backup saved to " + fn)

    new = re.sub(r"<gridMap>.*?</gridMap>", f"<gridMap>{encode_grid(grid)}</gridMap>", xml, flags=re.S)
    new = re.sub(r"<sensitivityLevel>\d+</sensitivityLevel>",
                 f"<sensitivityLevel>{int(sensitivity)}</sensitivityLevel>", new)
    new = re.sub(r"(<MotionDetection[^>]*>\s*<enabled>)\w+(</enabled>)", r"\1true\2", new, count=1)
    new = re.sub(r"<regionType>\w+</regionType>", "<regionType>grid</regionType>", new)

    r = cam_request("PUT", motion_path(), data=new.encode("utf-8"),
                    headers={"Content-Type": "application/xml"})
    ok = r.status_code == 200 and "<statusCode>1</statusCode>" in r.text
    return {"ok": ok, "status": r.status_code, "body": r.text[:400], "backup": fn}


def leaves(root):
    return [e for e in root.iter() if len(e) == 0]


def tag(e):
    return e.tag.split("}")[-1]


def parse_xml(text):
    m = re.match(r"\s*(?:<\?xml[^>]*\?>)?\s*<[^>\s]+[^>]*\sxmlns=\"([^\"]+)\"", text)
    if m:
        ET.register_namespace("", m.group(1))
    return ET.fromstring(text.encode("utf-8"))


def section_get(path):
    r = cam_request("GET", fmt(path))
    if r.status_code != 200:
        raise RuntimeError(f"Camera returned {r.status_code}: {r.text[:300]}")
    root = parse_xml(r.text)
    fields = [{"i": i, "tag": tag(e), "value": (e.text or "").strip()}
              for i, e in enumerate(leaves(root))]
    return {"xml": r.text, "fields": fields}


def section_put(path, edits):
    r = cam_request("GET", fmt(path))
    if r.status_code != 200:
        raise RuntimeError(f"Camera returned {r.status_code}: {r.text[:300]}")
    fn = backup(path, r.text)
    root = parse_xml(r.text)
    found = leaves(root)
    for e in edits:
        el = found[e["i"]]
        if tag(el) != e["tag"]:
            raise RuntimeError("Settings changed on the camera since you loaded them; reload.")
        el.text = str(e["value"])
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    r = cam_request("PUT", fmt(path), data=body, headers={"Content-Type": "application/xml"})
    ok = r.status_code == 200 and "<statusCode>1</statusCode>" in r.text
    return {"ok": ok, "status": r.status_code, "body": r.text[:400], "backup": fn}


def raw(method, path, body):
    plain = re.fullmatch(r"/ISAPI/[A-Za-z0-9_./{}?&=,:-]*", path) and ".." not in path
    if method not in ("GET", "PUT", "POST", "DELETE") or not plain:
        raise RuntimeError("Method must be GET/PUT/POST/DELETE and the path a plain /ISAPI/... path")
    r = cam_request(method, fmt(path), data=body.encode("utf-8") if body else None,
                    headers={"Content-Type": "application/xml"} if body else None)
    return {"status": r.status_code, "body": r.text[:20000]}


def connect(data):
    CFG.clear()
    CFG.update(ip=data["ip"].strip(), user=data["user"], password=data["password"],
               cport=int(data.get("port") or 80), scheme="https" if data.get("https") else "http",
               channel=int(data.get("channel") or 1))
    try:
        get_current()
    except Exception:
        CFG.clear()
        raise
    return {"cols": COLS, "rows": ROWS, "cam": f"{CFG['user']}@{CFG['ip']}  channel {CFG['channel']}"}


def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return str(ipaddress.ip_network(s.getsockname()[0] + "/24", strict=False))
    finally:
        s.close()


def probe_host(ip, port):
    try:
        r = requests.get(f"http://{ip}:{port}/ISAPI/System/deviceInfo", timeout=1.5, verify=False)
    except requests.RequestException:
        return None
    challenge = r.headers.get("WWW-Authenticate", "")
    if r.status_code == 401 and challenge:
        realm = re.search(r'realm="([^"]*)"', challenge)
        return {"ip": ip, "realm": realm.group(1) if realm else "", "server": r.headers.get("Server", ""),
                "open": False}
    if r.status_code == 200 and "<DeviceInfo" in r.text:
        model = re.search(r"<model>(.*?)</model>", r.text)
        return {"ip": ip, "realm": model.group(1) if model else "", "server": r.headers.get("Server", ""),
                "open": True}
    return None


def scan(cidr, port):
    net = ipaddress.ip_network(cidr or local_subnet(), strict=False)
    if net.num_addresses > 1024:
        raise RuntimeError("That range is too large; use /22 or smaller.")
    with ThreadPoolExecutor(max_workers=100) as pool:
        hits = pool.map(lambda h: probe_host(str(h), port), net.hosts())
    found = sorted((h for h in hits if h), key=lambda h: ipaddress.ip_address(h["ip"]))
    return {"range": str(net), "found": found}


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>ISAPI Studio</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/twitter-bootstrap/3.4.1/css/bootstrap.min.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/twitter-bootstrap/3.4.1/css/bootstrap-theme.min.css">
<style>
body{padding:0 20px 40px}
#wrap{position:relative;display:inline-block;border:2px solid #555;max-width:100%}
#img{display:block;max-width:100%;min-width:320px;min-height:180px;background:#222}
#cv{position:absolute;left:0;top:0;cursor:crosshair}
pre{white-space:pre-wrap;max-height:380px;overflow:auto}
.panel-heading{text-align:center}
.tab-content{padding-top:15px}
#fields .form-group{margin-bottom:8px}
#found tr{cursor:pointer}
</style></head><body>
<div class="text-center" style="margin:14px 0 12px">
 <h3 style="margin:0">ISAPI Studio</h3>
 <small class="text-muted" id="cam">Not connected</small></div>

<div class="panel panel-default"><div class="panel-heading"><h3 class="panel-title">Connect to a camera</h3></div>
<div class="panel-body">
 <div class="form-inline text-center">
  <input id="c_ip" class="form-control" placeholder="Camera IP" size="16">
  <input id="c_port" class="form-control" placeholder="Port" value="80" size="5">
  <input id="c_user" class="form-control" placeholder="User" value="admin" size="10">
  <input id="c_pass" type="password" class="form-control" placeholder="Password" size="14">
  <input id="c_ch" class="form-control" placeholder="Ch" value="1" size="3">
  <label class="checkbox-inline"><input type="checkbox" id="c_https"> HTTPS</label>
  <button class="btn btn-primary" id="c_go">Connect</button>
  <button class="btn btn-default" id="c_quit">Quit</button>
 </div>
 <hr>
 <div class="form-inline text-center">
  <input id="sc_range" class="form-control" placeholder="Range, e.g. 192.168.1.0/24 (blank = my network)" size="42">
  <button class="btn btn-info" id="sc_go">Scan for cameras</button>
 </div>
 <p class="text-center text-muted" id="sc_msg" style="margin-top:8px"></p>
 <table class="table table-hover table-condensed hidden" id="found"><thead><tr><th>IP</th><th>Realm / model</th><th>Server</th><th>Access</th></tr></thead><tbody></tbody></table>
 <pre id="c_msg" class="hidden"></pre>
</div></div>

<ul class="nav nav-tabs" id="tabs">
 <li class="active"><a href="#t_motion" data-toggle="tab">Motion zones</a></li>
 <li><a href="#t_set" data-toggle="tab">Settings</a></li>
 <li><a href="#t_raw" data-toggle="tab">ISAPI console</a></li>
</ul>
<div class="tab-content">

<div class="tab-pane active" id="t_motion">
 <div class="panel panel-default"><div class="panel-heading"><h3 class="panel-title">Draw where motion should be detected</h3></div>
 <div class="panel-body">
  <div class="btn-group">
   <button class="btn btn-danger" id="t_brush">Brush</button>
   <button class="btn btn-default" id="t_erase">Erase</button>
   <button class="btn btn-default" id="t_poly">Polygon</button>
  </div>
  <button class="btn btn-default" id="b_fill">Fill all</button>
  <button class="btn btn-default" id="b_clear">Clear all</button>
  <button class="btn btn-info" id="b_load">Load current from camera</button>
  <button class="btn btn-default" id="b_snap">New snapshot</button>
  <div style="margin:10px 0"><div id="wrap"><img id="img"><canvas id="cv"></canvas></div></div>
  <div class="form-inline">
   <label>Sensitivity</label> <input id="sens" type="range" min="0" max="100" value="60" class="form-control" style="width:240px">
   <b id="sv">60</b> <button class="btn btn-success" id="b_apply">Apply to camera</button>
  </div>
  <p class="help-block">Brush/Erase: click and drag. Polygon: click corners, double-click (or Enter) to close and fill. Esc cancels. Right-click erases.</p>
  <pre id="msg" class="hidden"></pre>
 </div></div>
</div>

<div class="tab-pane" id="t_set">
 <div class="panel panel-default"><div class="panel-heading"><h3 class="panel-title">Camera settings</h3></div>
 <div class="panel-body">
  <div class="form-inline text-center" style="margin-bottom:15px">
   <select id="sec" class="form-control" style="min-width:260px"></select>
   <button class="btn btn-info" id="s_load">Load</button>
   <button class="btn btn-success" id="s_save">Save</button>
   <button class="btn btn-warning" id="s_reboot">Reboot camera</button>
  </div>
  <div class="row">
   <div class="col-md-7"><form class="form-horizontal" id="fields"></form></div>
   <div class="col-md-5" id="pv" style="display:none">
    <div class="panel panel-info"><div class="panel-heading"><h3 class="panel-title">Live preview</h3></div>
     <div class="panel-body text-center"><img id="pvimg" style="max-width:100%">
      <p style="margin-top:8px"><button type="button" class="btn btn-default btn-sm" id="pv_re">Refresh preview</button></p>
      <p class="help-block">Snapshot from the camera, so overlays and image changes show after Save.</p></div></div>
   </div>
  </div>
  <pre id="s_msg" class="hidden"></pre>
 </div></div>
</div>

<div class="tab-pane" id="t_raw">
 <div class="panel panel-default"><div class="panel-heading"><h3 class="panel-title">ISAPI console</h3></div>
 <div class="panel-body">
  <div class="form-inline" style="margin-bottom:10px">
   <select id="r_m" class="form-control"><option>GET</option><option>PUT</option><option>POST</option><option>DELETE</option></select>
   <input id="r_p" class="form-control" style="width:60%" value="/ISAPI/System/deviceInfo">
   <button class="btn btn-primary" id="r_go">Send</button>
  </div>
  <textarea id="r_b" class="form-control" rows="6" placeholder="XML body (PUT/POST)"></textarea>
  <pre id="r_out" style="margin-top:10px"></pre>
  <p class="help-block">{ch} in a path is replaced with the channel number.</p>
 </div></div>
</div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/twitter-bootstrap/3.4.1/js/bootstrap.min.js"></script>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const show = (id, t) => { const e = $(id); e.textContent = t; e.classList.remove('hidden'); };

async function api(url, body) {
  const r = await fetch(url, {
    method: body ? 'POST' : 'GET',
    headers: {'X-Tool': '1', 'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined
  });
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j;
}

let COLS = 22, ROWS = 18;
let grid = [], tool = 'brush', painting = false, val = 1, poly = [];
const img = $('img'), cv = $('cv'), ctx = cv.getContext('2d');
const msg = t => show('msg', t);

function setSize(c, r) {
  COLS = c; ROWS = r;
  grid = Array.from({length: ROWS}, () => Array(COLS).fill(0));
  draw();
}

function resize() {
  cv.width = img.clientWidth;
  cv.height = img.clientHeight;
  draw();
}
img.onload = resize;
window.onresize = resize;
$('tabs').addEventListener('shown.bs.tab', resize);

function inside(pt, pg) {
  let x = pt[0], y = pt[1], ins = false;
  for (let i = 0, j = pg.length - 1; i < pg.length; j = i++) {
    const xi = pg[i][0], yi = pg[i][1], xj = pg[j][0], yj = pg[j][1];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) ins = !ins;
  }
  return ins;
}

function hit(r, c, cw, ch, pg) {
  for (const dy of [.1, .5, .9])
    for (const dx of [.1, .5, .9])
      if (inside([(c + dx) * cw, (r + dy) * ch], pg)) return true;
  return false;
}

function draw() {
  ctx.clearRect(0, 0, cv.width, cv.height);
  const cw = cv.width / COLS, ch = cv.height / ROWS;
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++) {
    if (grid[r][c]) { ctx.fillStyle = 'rgba(255,0,0,0.35)'; ctx.fillRect(c * cw, r * ch, cw, ch); }
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.strokeRect(c * cw, r * ch, cw, ch);
  }
  if (poly.length >= 3) for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++)
    if (hit(r, c, cw, ch, poly)) { ctx.fillStyle = 'rgba(255,235,59,0.3)'; ctx.fillRect(c * cw, r * ch, cw, ch); }
  if (poly.length) {
    ctx.strokeStyle = '#ffeb3b'; ctx.lineWidth = 2; ctx.beginPath();
    poly.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.stroke();
    poly.forEach(p => { ctx.fillStyle = '#ffeb3b'; ctx.fillRect(p[0] - 3, p[1] - 3, 6, 6); });
  }
}

function cell(e) {
  return [Math.min(ROWS - 1, Math.max(0, Math.floor(e.offsetY / cv.height * ROWS))),
          Math.min(COLS - 1, Math.max(0, Math.floor(e.offsetX / cv.width * COLS)))];
}

function setTool(t) {
  tool = t; poly = [];
  for (const n of ['brush', 'erase', 'poly']) $('t_' + n).className = 'btn ' + (n === t ? 'btn-danger' : 'btn-default');
  draw();
}
['brush', 'erase', 'poly'].forEach(n => $('t_' + n).onclick = () => setTool(n));

function closePoly() {
  if (poly.length < 3) { poly = []; draw(); return; }
  const cw = cv.width / COLS, ch = cv.height / ROWS;
  for (let r = 0; r < ROWS; r++) for (let c = 0; c < COLS; c++)
    if (hit(r, c, cw, ch, poly)) grid[r][c] = 1;
  poly = [];
  draw();
}

cv.oncontextmenu = e => e.preventDefault();
cv.onmousedown = e => {
  if (tool === 'poly') {
    if (e.button === 0) { poly.push([e.offsetX, e.offsetY]); draw(); }
    return;
  }
  painting = true;
  val = (e.button === 2 || tool === 'erase') ? 0 : 1;
  const [r, c] = cell(e); grid[r][c] = val; draw();
};
cv.onmousemove = e => { if (painting) { const [r, c] = cell(e); grid[r][c] = val; draw(); } };
cv.ondblclick = () => { if (tool === 'poly') { poly.pop(); closePoly(); } };
window.onmouseup = () => painting = false;
window.onkeydown = e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 'Enter' && tool === 'poly') closePoly();
  if (e.key === 'Escape') { poly = []; draw(); }
};

$('b_fill').onclick = () => { grid = grid.map(r => r.map(() => 1)); draw(); };
$('b_clear').onclick = () => { grid = grid.map(r => r.map(() => 0)); draw(); };
$('b_snap').onclick = () => { img.src = '/snapshot?' + Date.now(); };
$('sens').oninput = e => $('sv').textContent = e.target.value;

$('b_load').onclick = async () => {
  msg('Loading...');
  try {
    const j = await api('/api/current');
    if (j.cols !== COLS || j.rows !== ROWS) setSize(j.cols, j.rows);
    if (j.grid) grid = j.grid;
    $('sens').value = j.sensitivity;
    $('sv').textContent = j.sensitivity;
    draw();
    msg('Loaded current camera settings.');
  } catch (e) { msg('Error: ' + e.message); }
};

$('b_apply').onclick = async () => {
  if (tool === 'poly' && poly.length >= 3) closePoly();
  if (!grid.some(r => r.some(v => v)) && !confirm('Grid is empty: this will disable all motion detection. Continue?')) return;
  msg('Sending to camera...');
  try {
    const j = await api('/api/apply', {grid: grid, sensitivity: +$('sens').value});
    msg((j.ok ? 'Applied successfully.' : 'Camera did not accept it.') +
        '\nHTTP ' + j.status + '\n' + j.body + '\nBackup of old settings: ' + j.backup);
  } catch (e) { msg('Error: ' + e.message); }
};

let loaded = [];
const PREVIEW = /image|day|video input|osd|stream/i;
const refreshPreview = () => { $('pvimg').src = '/snapshot?' + Date.now(); };
$('pv_re').onclick = refreshPreview;

$('s_load').onclick = async () => {
  show('s_msg', 'Loading...');
  try {
    const j = await api('/api/section?name=' + encodeURIComponent($('sec').value));
    loaded = j.fields;
    const wantPreview = PREVIEW.test($('sec').value);
    $('pv').style.display = wantPreview ? '' : 'none';
    if (wantPreview) refreshPreview();
    $('fields').innerHTML = j.fields.map((f, k) => {
      const isBool = /^(true|false)$/.test(f.value);
      const input = isBool
        ? '<select class="form-control" data-k="' + k + '"><option' + (f.value === 'true' ? ' selected' : '') + '>true</option><option' + (f.value === 'false' ? ' selected' : '') + '>false</option></select>'
        : '<input class="form-control" data-k="' + k + '" value="' + esc(f.value) + '">';
      return '<div class="form-group"><label class="col-sm-4 control-label">' + esc(f.tag) + '</label><div class="col-sm-6">' + input + '</div></div>';
    }).join('') || '<p class="text-center text-muted">No editable fields on this page.</p>';
    show('s_msg', j.fields.length + ' fields loaded.');
  } catch (e) { show('s_msg', 'Error: ' + e.message); }
};

$('s_save').onclick = async () => {
  const edits = [];
  document.querySelectorAll('#fields [data-k]').forEach(el => {
    const f = loaded[+el.dataset.k];
    if (el.value !== f.value) edits.push({i: f.i, tag: f.tag, value: el.value});
  });
  if (!edits.length) { show('s_msg', 'Nothing changed.'); return; }
  try {
    const j = await api('/api/section', {name: $('sec').value, edits: edits});
    show('s_msg', (j.ok ? 'Saved.' : 'Camera did not accept it.') +
         '\nHTTP ' + j.status + '\n' + j.body + '\nBackup of old settings: ' + j.backup);
    if (PREVIEW.test($('sec').value)) setTimeout(refreshPreview, 1500);
  } catch (e) { show('s_msg', 'Error: ' + e.message); }
};

$('s_reboot').onclick = async () => {
  if (!confirm('Reboot the camera now?')) return;
  try {
    const j = await api('/api/raw', {method: 'PUT', path: '/ISAPI/System/reboot', body: ''});
    show('s_msg', 'HTTP ' + j.status + '\n' + j.body);
  } catch (e) { show('s_msg', 'Error: ' + e.message); }
};

$('r_go').onclick = async () => {
  try {
    const j = await api('/api/raw', {method: $('r_m').value, path: $('r_p').value, body: $('r_b').value});
    $('r_out').textContent = 'HTTP ' + j.status + '\n' + j.body;
  } catch (e) { $('r_out').textContent = 'Error: ' + e.message; }
};

$('c_go').onclick = async () => {
  show('c_msg', 'Connecting...');
  try {
    const j = await api('/api/connect', {
      ip: $('c_ip').value, port: $('c_port').value, user: $('c_user').value,
      password: $('c_pass').value, channel: $('c_ch').value, https: $('c_https').checked
    });
    $('cam').textContent = 'Connected: ' + j.cam;
    $('c_msg').classList.add('hidden');
    setSize(j.cols, j.rows);
    img.src = '/snapshot?' + Date.now();
    $('b_load').click();
  } catch (e) { show('c_msg', 'Could not connect: ' + e.message); }
};

$('sc_go').onclick = async () => {
  $('sc_msg').textContent = 'Scanning...';
  $('found').classList.add('hidden');
  try {
    const j = await api('/api/scan', {range: $('sc_range').value, port: $('c_port').value});
    $('sc_msg').textContent = j.found.length
      ? 'Found ' + j.found.length + ' device(s) in ' + j.range + '. Click one to use it.'
      : 'Nothing found in ' + j.range + '.';
    $('found').querySelector('tbody').innerHTML = j.found.map(h =>
      '<tr data-ip="' + esc(h.ip) + '"><td>' + esc(h.ip) + '</td><td>' + esc(h.realm) + '</td><td>' + esc(h.server) +
      '</td><td>' + (h.open ? 'No login needed' : 'Login required') + '</td></tr>').join('');
    if (j.found.length) $('found').classList.remove('hidden');
  } catch (e) { $('sc_msg').textContent = 'Scan failed: ' + e.message; }
};

$('found').onclick = e => {
  const row = e.target.closest('tr[data-ip]');
  if (row) { $('c_ip').value = row.dataset.ip; $('c_pass').focus(); }
};

$('c_quit').onclick = async () => {
  if (!confirm('Stop ISAPI Studio?')) return;
  try { await api('/api/quit', {}); } catch (e) {}
  document.body.innerHTML = '<h2 class="text-center" style="margin-top:80px">ISAPI Studio has stopped. You can close this tab.</h2>';
};

(async () => {
  const j = await api('/api/state');
  $('sec').innerHTML = j.sections.map(s => '<option>' + esc(s) + '</option>').join('');
  setSize(j.cols, j.rows);
  if (j.cam) {
    $('cam').textContent = 'Connected: ' + j.cam;
    img.src = '/snapshot?' + Date.now();
    $('b_load').click();
  }
})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def guard(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1"):
            self.send(403, "bad host", "text/plain")
            return False
        if self.path.startswith("/api/") and self.headers.get("X-Tool") != "1":
            self.send(403, "missing X-Tool header", "text/plain")
            return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        try:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                self.send(200, PAGE, "text/html; charset=utf-8")
            elif u.path == "/snapshot":
                r = cam_request("GET", fmt("/ISAPI/Streaming/channels/{ch}01/picture"))
                if r.status_code != 200:
                    self.send(502, f"Camera returned {r.status_code}", "text/plain")
                else:
                    self.send(200, r.content, "image/jpeg")
            elif u.path == "/api/state":
                cam = f"{CFG['user']}@{CFG['ip']}  channel {CFG['channel']}" if CFG else None
                self.send(200, json.dumps({"sections": list(SECTIONS), "cam": cam, "cols": COLS, "rows": ROWS}))
            elif u.path == "/api/current":
                _, grid, sens = get_current()
                self.send(200, json.dumps({"grid": grid, "sensitivity": sens, "cols": COLS, "rows": ROWS}))
            elif u.path == "/api/section":
                self.send(200, json.dumps(section_get(SECTIONS[q["name"][0]])))
            else:
                self.send(404, "not found", "text/plain")
        except Exception as e:
            self.send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        if not self.guard():
            return
        try:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/apply":
                out = apply_motion(data["grid"], data["sensitivity"])
            elif self.path == "/api/section":
                out = section_put(SECTIONS[data["name"]], data["edits"])
            elif self.path == "/api/raw":
                out = raw(data["method"], data["path"], data.get("body", ""))
            elif self.path == "/api/connect":
                out = connect(data)
            elif self.path == "/api/scan":
                out = scan(data.get("range", ""), int(data.get("port") or 80))
            elif self.path == "/api/quit":
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
                out = {"ok": True}
            else:
                return self.send(404, "not found", "text/plain")
            self.send(200, json.dumps(out))
        except Exception as e:
            self.send(500, json.dumps({"error": str(e)}))


def main():
    global SERVER
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    p.add_argument("--ip")
    p.add_argument("--user", default="admin")
    p.add_argument("--password")
    p.add_argument("--camport", type=int, default=80)
    p.add_argument("--https", action="store_true")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args()

    if a.ip:
        password = a.password or getpass.getpass("Camera password: ")
        try:
            connect({"ip": a.ip, "user": a.user, "password": password, "port": a.camport,
                     "https": a.https, "channel": a.channel})
            print("Connected to camera.")
        except Exception as e:
            print("Could not connect:", e)

    url = f"http://localhost:{a.port}"
    try:
        SERVER = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        webbrowser.open(url)
        return
    print(f"{APP_NAME} running at {url} (Ctrl+C to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
