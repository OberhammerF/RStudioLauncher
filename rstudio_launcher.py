#!/usr/bin/env python3
"""
RStudio HPC Launcher — Local Web UI
A Flask-based graphical interface to launch RStudio Server on HPC clusters.

Run:
    python rstudio_launcher.py

Then open http://localhost:5050 in your browser.

Requirements:
    pip install flask paramiko
"""

import json
import os
import re
import subprocess
import threading
import time
import webbrowser
import sys

from flask import Flask, render_template_string, request, jsonify

try:
    import paramiko
except ImportError:
    print("ERROR: paramiko is required.  pip install paramiko")
    sys.exit(1)

# For interactive authentication
class InteractiveAuth(paramiko.ServerInterface):
    """Handle interactive SSH authentication (password/passphrase prompts)"""
    def __init__(self, password=None):
        self.password = password
        self.auth_complete = False
        self.auth_failure = False

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == self.password else paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        return paramiko.AUTH_SUCCESSFUL

    def check_auth_interactive_response(self, responses):
        if self.password and len(responses) == 1:
            if responses[0] == self.password:
                self.auth_complete = True
                return paramiko.AUTH_SUCCESSFUL
        self.auth_failure = True
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,keyboard-interactive"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

# ─── Interactive authentication state ────────────────────────────────────────
auth_state = {
    "password_requested": False,
    "password_provided": None,
    "passphrase_requested": False,
    "passphrase_provided": None,
}

# ─── Settings persistence ────────────────────────────────────────────────────
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".rstudio_hpc_launcher.json")

SETTINGS_DEFAULTS = {
    "hpc_user": "",
    "gateway_host": "hpcgw.op.umcutrecht.nl",
    "login_node": "hpcs06.op.umcutrecht.nl",
    "ssh_alias": "gw2hpcs06",
    "script_path": "",
    "wall_time": "08:00:00",
    "cpus": "1",
    "mem": "32",
    "tmpspace": "20",
    "r_version": "4.3.1",
    "xdg_data_home": "",
    "local_port": "8787",
    "discover_user": "",
}


def load_settings() -> dict:
    """Load saved settings from disk, falling back to defaults."""
    settings = dict(SETTINGS_DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r") as f:
            saved = json.load(f)
        settings.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return settings


def save_settings(data: dict):
    """Persist the given settings dict to disk."""
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save settings: {e}")


# ─── App state (simple in-memory, single-user) ──────────────────────────────
state = {
    "logs": [],
    "tunnel_logs": [],
    "tunnel_error": None,
    "active_jobs": [],
    "active_tunnels": [],
    "job_id": None,
    "remote_hostname": None,
    "remote_port": None,
    "rstudio_user": None,
    "rstudio_password": None,
    "tunnel_active": False,
    "status": "idle",          # idle | submitting | waiting | running | error
    "status_msg": "Ready",
    "_current_user": "",       # Track HPC username for cleanup
    "_script_dir": "",         # Track script directory for wrapper placement
}

tunnel_processes: dict = {}   # Key: local_port, Value: (process, tunnel_info)
ssh_clients: dict = {}        # gw_client, login_client


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    state["logs"].append(f"[{ts}] {msg}")
    # keep last 200 lines
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]
    print(f"[{ts}] {msg}")


def log_tunnel(msg: str):
    """Log messages specifically for the tunnel log."""
    ts = time.strftime("%H:%M:%S")
    state["tunnel_logs"].append(f"[{ts}] {msg}")
    # keep last 200 lines
    if len(state["tunnel_logs"]) > 200:
        state["tunnel_logs"] = state["tunnel_logs"][-200:]
    print(f"[TUNNEL {ts}] {msg}")


def ssh_connect_with_auth(hostname, username, password=None):
    """
    Connect via SSH with interactive password/passphrase support.
    If password is None, will prompt the frontend for it.
    Returns (client, password_was_provided_interactively)
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        # Try with key-based auth first (no password needed)
        client.connect(hostname, username=username, allow_agent=True, look_for_keys=True, timeout=10)
        log(f"Connected to {hostname} using SSH key")
        return client, False
    except paramiko.AuthenticationException as e:
        log(f"Key auth failed, trying password auth for {hostname}...")
        
        if password:
            # Try with provided password
            try:
                client.connect(hostname, username=username, password=password, timeout=10)
                log(f"Connected to {hostname} using provided password")
                return client, False
            except paramiko.AuthenticationException:
                log(f"Password authentication failed for {hostname}")
                raise
        else:
            # Request password from frontend
            auth_state["password_requested"] = True
            auth_state["password_provided"] = None
            log(f"Awaiting password input for {username}@{hostname}...")
            
            # Wait for password (max 5 minutes)
            max_wait = 300
            wait_interval = 0.5
            elapsed = 0
            while elapsed < max_wait:
                if auth_state["password_provided"] is not None:
                    pwd = auth_state["password_provided"]
                    auth_state["password_provided"] = None
                    auth_state["password_requested"] = False
                    try:
                        client.connect(hostname, username=username, password=pwd, timeout=10)
                        log(f"Connected to {hostname} with interactive password")
                        return client, True
                    except paramiko.AuthenticationException:
                        log(f"Authentication failed - password incorrect")
                        auth_state["password_requested"] = True
                        auth_state["password_provided"] = None
                time.sleep(wait_interval)
                elapsed += wait_interval
            
            state["status"] = "error"
            state["status_msg"] = "Password input timeout"
            log("ERROR: Timed out waiting for password input")
            raise Exception("Password input timeout")


# ─── Flask app ───────────────────────────────────────────────────────────────
app = Flask(__name__)

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RStudio HPC Launcher</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --surface2: #334155;
    --accent: #38bdf8; --accent-hover: #7dd3fc; --danger: #f87171;
    --success: #4ade80; --warn: #fbbf24; --text: #e2e8f0; --muted: #94a3b8;
    --radius: 10px; --font: 'Segoe UI', system-ui, -apple-system, sans-serif;
    --mono: 'SF Mono', 'Menlo', 'Consolas', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-user-select: auto; user-select: auto; }
  body { font-family: var(--font); background: var(--bg); color: var(--text); min-height: 100vh; -webkit-user-select: text; user-select: text; }

  .container { max-width: 900px; margin: 0 auto; padding: 24px 16px; }
  
  .app-header { position: sticky; top: 0; z-index: 101; background: var(--bg); padding-bottom: 0; }
  h1 { font-size: 1.8rem; margin-bottom: 4px; }
  h1 span { color: var(--accent); }
  .subtitle { color: var(--muted); margin-bottom: 16px; font-size: 0.95rem; }

  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 20px; background: var(--bg); padding: 8px 0 0 0; }
  .tab { padding: 10px 24px; border-radius: var(--radius) var(--radius) 0 0; background: var(--surface2);
         color: var(--muted); cursor: pointer; font-weight: 600; border: none; font-size: 0.95rem;
         transition: all .15s; white-space: nowrap; }
  .tab.active { background: var(--surface); color: var(--accent); }
  .tab:hover { color: var(--text); }
  .panel { display: none; background: var(--surface); border-radius: 0 var(--radius) var(--radius) var(--radius);
           padding: 24px; }
  .panel.active { display: block; }

  /* Cards */
  .card { background: var(--surface2); border-radius: var(--radius); padding: 20px; margin-bottom: 16px; }
  .card h3 { font-size: 1rem; color: var(--accent); margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .card h3 .icon { font-size: 1.2rem; }

  /* Form grid */
  .form-grid { display: grid; grid-template-columns: 180px 1fr; gap: 10px 16px; align-items: center; }
  label { font-size: 0.88rem; color: var(--muted); font-weight: 500; }
  input, select { background: var(--bg); border: 1px solid var(--surface2); color: var(--text);
                  padding: 8px 12px; border-radius: 6px; font-size: 0.9rem; width: 100%; }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  .hint { font-size: 0.78rem; color: var(--muted); grid-column: 2; margin-top: -6px; }

  /* Buttons */
  .btn-row { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
  button.action { font-family: var(--font); font-size: 0.9rem; padding: 10px 22px; border-radius: 8px;
           cursor: pointer; font-weight: 600; border: none; transition: all .15s; }
  .btn-primary { background: var(--accent); color: var(--bg); }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-primary:disabled { opacity: .4; cursor: not-allowed; }
  .btn-danger { background: var(--danger); color: #fff; }
  .btn-danger:hover { background: #fca5a5; }
  .btn-danger:disabled { opacity: .4; cursor: not-allowed; }
  .btn-secondary { background: var(--surface2); color: var(--text); border: 1px solid var(--muted); }
  .btn-secondary:hover { background: var(--surface); }
  .btn-secondary:disabled { opacity: .4; cursor: not-allowed; }
  .btn-success { background: var(--success); color: var(--bg); }
  .btn-success:hover { opacity: .85; }
  .btn-success:disabled { opacity: .4; cursor: not-allowed; }

  /* Status badge */
  .status-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; padding: 10px 16px;
                border-radius: var(--radius); background: var(--surface2); }
  .status-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .status-dot.idle { background: var(--muted); }
  .status-dot.submitting, .status-dot.waiting { background: var(--warn); animation: pulse 1.5s infinite; }
  .status-dot.running { background: var(--success); }
  .status-dot.error { background: var(--danger); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }
  .status-text { font-size: 0.9rem; }

  /* Connection info */
  .info-grid { display: grid; grid-template-columns: 160px 1fr; gap: 6px 12px; }
  .info-grid .lbl { color: var(--muted); font-size: 0.88rem; }
  .info-grid .val { font-family: var(--mono); font-size: 0.88rem; color: var(--success); -webkit-user-select: text; user-select: text; }
  .info-grid .val.empty { color: var(--muted); }

  /* Log */
  .log-box { background: var(--bg); border-radius: 8px; padding: 12px; font-family: var(--mono);
             font-size: 0.8rem; color: var(--muted); height: 200px; overflow-y: auto; overflow-x: hidden;
             white-space: pre-wrap; word-break: break-all; line-height: 1.6; -webkit-user-select: text; user-select: text; border: 1px solid var(--surface2); }

  /* Setup guide */
  .guide { line-height: 1.7; font-size: 0.92rem; -webkit-user-select: text; user-select: text; }
  .guide h2 { color: var(--accent); font-size: 1.15rem; margin: 24px 0 8px 0; padding-bottom: 4px;
              border-bottom: 1px solid var(--surface2); -webkit-user-select: text; user-select: text; }
  .guide h2:first-child { margin-top: 0; }
  .guide h3 { color: var(--text); font-size: 1rem; margin: 16px 0 6px 0; -webkit-user-select: text; user-select: text; }
  .guide ol, .guide ul { padding-left: 22px; margin: 6px 0; -webkit-user-select: text; user-select: text; }
  .guide li { margin-bottom: 6px; -webkit-user-select: text; user-select: text; }
  .guide code { background: var(--bg); color: var(--accent); padding: 2px 7px; border-radius: 4px;
                font-family: var(--mono); font-size: 0.84rem; -webkit-user-select: text; user-select: text; }
  .guide pre { background: var(--bg); border-radius: 8px; padding: 12px 16px; margin: 8px 0;
               font-family: var(--mono); font-size: 0.82rem; color: var(--accent); overflow-x: auto;
               border: 1px solid var(--surface2); -webkit-user-select: text; user-select: text; }
  .guide .tip { background: #1e3a5f; border-left: 3px solid var(--accent); padding: 10px 14px;
                border-radius: 0 8px 8px 0; margin: 10px 0; font-size: 0.88rem; -webkit-user-select: text; user-select: text; }
  .guide .warn { background: #3d2e0a; border-left: 3px solid var(--warn); padding: 10px 14px;
                 border-radius: 0 8px 8px 0; margin: 10px 0; font-size: 0.88rem; -webkit-user-select: text; user-select: text; }
  .guide .step-group { background: var(--surface2); border-radius: var(--radius); padding: 18px 20px;
                       margin-bottom: 14px; -webkit-user-select: text; user-select: text; }

  /* Guide scenario cards */
  .guide-overview { display: flex; gap: 12px; margin: 16px 0 20px 0; }
  .guide-card { flex: 1; background: var(--surface2); border: 2px solid transparent; border-radius: var(--radius);
                padding: 16px; cursor: pointer; transition: all 0.2s ease; text-align: center; }
  .guide-card:hover { border-color: var(--accent); background: var(--surface); }
  .guide-card.active { border-color: var(--accent); background: var(--surface); box-shadow: 0 0 12px rgba(96,165,250,0.15); }
  .guide-card .card-icon { font-size: 1.8rem; margin-bottom: 8px; }
  .guide-card .card-title { font-weight: 600; color: var(--text); font-size: 0.92rem; margin-bottom: 4px; }
  .guide-card .card-desc { font-size: 0.78rem; color: var(--muted); line-height: 1.4; }

  /* Collapsible scenario sections */
  .guide-scenario { display: none; animation: fadeIn 0.25s ease; }
  .guide-scenario.visible { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

  /* Breadcrumb / back link */
  .guide-back { color: var(--accent); cursor: pointer; font-size: 0.85rem; margin-bottom: 12px; display: inline-block; }
  .guide-back:hover { text-decoration: underline; }

  /* Number badges for steps */
  .step-num { display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px;
              background: var(--accent); color: var(--bg); border-radius: 50%; font-weight: 700; font-size: 0.82rem;
              margin-right: 8px; flex-shrink: 0; vertical-align: middle; }

  /* Always-visible sections */
  .guide-extra { margin-top: 20px; }

  /* Auth Modal */
  .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
                   background: rgba(0,0,0,0.7); z-index: 9999; }
  .modal-overlay.active { display: flex; align-items: center; justify-content: center; }
  .modal { background: var(--surface); border-radius: var(--radius); padding: 32px; max-width: 400px;
           width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.8); }
  .modal h2 { font-size: 1.2rem; color: var(--accent); margin-bottom: 12px; }
  .modal p { font-size: 0.9rem; color: var(--text); margin-bottom: 16px; line-height: 1.5; }
  .modal-input { background: var(--bg); border: 1px solid var(--surface2); color: var(--text);
                 padding: 10px 14px; border-radius: 6px; font-size: 0.95rem; width: 100%;
                 margin-bottom: 16px; font-family: var(--font); }
  .modal-input:focus { outline: none; border-color: var(--accent); }
  .modal-buttons { display: flex; gap: 10px; }
  .modal-btn { flex: 1; padding: 10px 16px; border-radius: 6px; border: none; font-weight: 600;
               font-size: 0.9rem; cursor: pointer; transition: all 0.15s; }
  .modal-btn-submit { background: var(--accent); color: var(--bg); }
  .modal-btn-submit:hover { background: var(--accent-hover); }
  .modal-btn-cancel { background: var(--surface2); color: var(--text); }
  .modal-btn-cancel:hover { background: var(--surface); }
</style>
</head>
<body>
<div class="container">
  <div class="app-header">
    <h1>&#x1F5A5; <span>RStudio</span> HPC Launcher</h1>
    <p class="subtitle">Launch and connect to RStudio Server on any HPC cluster</p>

    <!-- Tabs -->
    <div class="tabs">
      <button class="tab active" onclick="switchTab(this,'launch')">&#x1F680; Launch Job</button>
      <button class="tab" onclick="switchTab(this,'connect')">&#x1F517; Connect</button>
      <button class="tab" onclick="switchTab(this,'setup')">&#x1F4D6; Setup Guide</button>
    </div>
  </div>

  <!-- Status bar -->
  <div class="status-bar">
    <div class="status-dot" id="statusDot"></div>
    <span class="status-text" id="statusText">Ready</span>
  </div>

  <!-- LAUNCH TAB -->
  <div class="panel active" id="panel-launch">

    <div class="card">
      <h3><span class="icon">&#x1F510;</span> SSH Connection</h3>
      <div class="form-grid">
        <label>HPC Username</label>
        <input id="hpc_user" value="">
        <label>Gateway Host</label>
        <input id="gateway_host" value="hpcgw.op.umcutrecht.nl">
        <label>Login Node</label>
        <input id="login_node" value="hpcs06.op.umcutrecht.nl">
        <label>SSH Config Alias</label>
        <input id="ssh_alias" value="gw2hpcs06">
        <span class="hint">Used for the tunnel command (e.g. gw2hpcs06). Leave blank to use full -J jump.</span>
      </div>
    </div>

    <div class="card">
      <h3><span class="icon">&#x1F4C4;</span> Script Location</h3>
      <div class="form-grid">
        <label>Remote script path</label>
        <input id="script_path" value="" placeholder="/home/pmc_research/youruser/rstudio_server.sh">
        <span class="hint">Full path to rstudio_server.sh on the HPC</span>
      </div>
    </div>

    <div class="card">
      <h3><span class="icon">&#x2699;&#xFE0F;</span> SLURM Parameters</h3>
      <div class="form-grid">
        <label>Wall-time</label>
        <input id="wall_time" value="08:00:00" placeholder="HH:MM:SS">
        <label>CPUs per task</label>
        <input id="cpus" type="number" value="1" min="1" max="64">
        <label>Memory (GB)</label>
        <input id="mem" type="number" value="32" min="1" max="512">
        <label>Tmp space (GB)</label>
        <input id="tmpspace" type="number" value="20" min="1" max="200">
      </div>
    </div>

    <div class="card">
      <h3><span class="icon">&#x1F4CA;</span> R / RStudio Settings</h3>
      <div class="form-grid">
        <label>R version</label>
        <select id="r_version">
          <option value="4.3.1" selected>4.3.1</option>
          <option value="4.3.2">4.3.2</option>
          <option value="4.4.0">4.4.0</option>
          <option value="4.4.1">4.4.1</option>
        </select>
        <span class="hint">Must match the version defined in your script's singularity_dir</span>
        <label>XDG_DATA_HOME</label>
        <input id="xdg_data_home" value="" placeholder="/hpc/pmc_kuiper/youruser/RStudioSessions">
      </div>
    </div>

    <div class="btn-row">
      <button class="action btn-primary" id="btnSubmit" onclick="submitJob()">&#x25B6; Submit Job</button>
      <button class="action btn-danger" id="btnCancel" onclick="cancelJob()" disabled>&#x23F9; Cancel Job</button>
    </div>

    <div class="card" style="margin-top:16px">
      <h3><span class="icon">&#x1F4DD;</span> Log</h3>
      <div class="log-box" id="logBox"></div>
    </div>

    <div class="card" style="margin-top:16px; border-left: 4px solid var(--accent);">
      <h3 style="margin-bottom:8px;">Status</h3>
      <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
        <div class="status-dot" id="statusDot2" style="width: 14px; height: 14px;"></div>
        <span id="statusText2" style="font-weight: 500; font-size: 0.95rem;">Ready to submit</span>
      </div>
      <p id="statusHint" style="font-size: 0.88rem; color: var(--muted); display: none;">
        ✓ Job submitted! Go to the <strong>🔗 Connect</strong> tab to open the tunnel and access RStudio.
      </p>
    </div>
  </div>

  <!-- CONNECT TAB -->
  <div class="panel" id="panel-connect">

    <!-- STEP 1: CHOOSE A JOB TO CONNECT TO -->
    <div class="card" style="background:var(--surface); border-left:4px solid var(--accent);">
      <h3><span class="icon">📍</span> Step 1: Load Job Info</h3>
      <p style="font-size:0.88rem; color:var(--muted); margin-bottom:12px;">
        <strong style="color:var(--text);">Choose which RStudio job to connect to.</strong> The job's connection details (hostname, port, credentials) will be loaded below.
      </p>
      
      <!-- Option A: Last submitted job (compact) -->
      <div style="margin-bottom:12px; padding:12px; background:var(--surface2); border-radius:6px; border-left:4px solid var(--success);">
        <div style="font-weight:600; margin-bottom:8px; color:var(--success);">✓ Last Submitted or Selected Job</div>
        <div class="info-grid" style="font-size:0.85rem; grid-template-columns: 130px 1fr; gap: 4px 10px;">
          <span class="lbl">Job ID</span>          <span class="val empty" id="infoJobId" style="font-size:0.85rem;">&ndash;</span>
          <span class="lbl">Host:Port</span>     <span class="val empty" id="infoHostPort" style="font-size:0.85rem;">&ndash;</span>
          <span class="lbl">RStudio User</span>  <span class="val empty" id="infoUser" style="font-size:0.85rem;">&ndash;</span>
          <span class="lbl">Password</span>       <span class="val empty" id="infoPass" style="font-size:0.85rem;">&ndash;</span>
        </div>
        <p id="jobReadyHint" style="font-size:0.8rem; color:var(--muted); margin:8px 0 0 0; font-style:italic; display:none;">This job is loaded and ready to use. Proceed to Step 2 to open the tunnel.</p>
      </div>

      <!-- Option B: Discover other jobs (compact) -->
      <div style="padding:12px; background:var(--surface2); border-radius:6px; border-left:3px solid var(--accent);">
        <div style="font-weight:600; margin-bottom:8px;">🔍 Find Other Jobs on HPC</div>
        <p style="font-size:0.8rem; color:var(--muted); margin:0 0 8px 0;">Don't see your job above? Search for other active RStudio jobs:</p>
        <div style="display:flex; gap:8px; margin-bottom:10px;">
          <input id="discover_user" value="" placeholder="Your HPC username" onkeypress="if(event.key==='Enter'){discoverJobs();}" style="flex:1; font-size:0.85rem; padding:6px 10px;">
          <button class="action btn-secondary" onclick="discoverJobs()" style="font-size:0.85rem; padding:6px 12px; white-space:nowrap;">🔎 Search</button>
        </div>
        <div id="activeJobsPanel" style="display:block;">
          <div id="activeJobsList" style="display:flex; flex-direction:column; gap:8px;"><div style="background:var(--surface2);padding:12px;border-radius:6px;border-left:3px solid var(--muted);"><p style="font-size:0.85rem; color:var(--text); margin:0;">No active RStudio jobs found yet.</p><p style="font-size:0.8rem; color:var(--muted); margin:8px 0 0 0;">Go to the <strong>🚀 Launch</strong> tab to submit a new job.</p></div></div>
        </div>
      </div>
    </div>

    <!-- Error display -->
    <div id="tunnelErrorBox" style="display:none; margin-top:16px; padding:12px; background:#3d2e0a; border-left:3px solid var(--warn); border-radius:4px;">
      <p style="margin:0; font-size:0.88rem; color:var(--warn); font-weight:600;">⚠️ Tunnel Error</p>
      <p id="tunnelErrorMsg" style="margin:8px 0 0 0; font-size:0.85rem; color:var(--text);"></p>
    </div>

    <!-- STEP 2: OPEN TUNNEL & MANAGE ACTIVE TUNNELS -->
    <div class="card" style="margin-top:16px; background:var(--surface); border-left:4px solid var(--accent);">
      <h3><span class="icon">🔗</span> Step 2: Open SSH Tunnel</h3>
      
      <!-- Open tunnel section -->
      <div style="margin-bottom:16px; padding-bottom:16px; border-bottom:1px solid var(--surface2);">
        <p style="font-size:0.88rem; color:var(--muted); margin-bottom:12px;">
          <strong style="color:var(--text);">After loading a job above:</strong> Open an SSH tunnel to forward the remote RStudio port to your local machine.
        </p>
        <div style="display:flex; gap:12px; align-items:flex-end;">
          <div style="flex-shrink:0;">
            <label style="font-size:0.85rem; color:var(--muted); display:block; margin-bottom:4px;">Local port</label>
            <input id="local_port" type="number" value="8787" min="1024" max="65535" onkeypress="if(event.key==='Enter'){openTunnel();}" style="width:100px; font-size:0.85rem; padding:6px 10px;">
          </div>
          <button class="action btn-primary" id="btnTunnel" onclick="openTunnel()" disabled style="flex:1; font-size:0.85rem; padding:8px 12px;">🔗 Open Tunnel</button>
        </div>
        <p id="tunnelStatus" style="margin:8px 0 0 0; color:var(--muted); font-size:0.85rem;">Tunnel: not active</p>
      </div>

      <!-- Active tunnels section -->
      <div>
        <p style="font-size:0.88rem; color:var(--muted); margin-bottom:12px; font-weight:600;">
          <span style="color:var(--accent);">✓ Tunnels Active:</span> Click the 🌐 Open button to launch RStudio in your browser
        </p>
        <div id="activeTunnelsList" style="display:flex; flex-direction:column; gap:8px;">
          <p style="font-size:0.85rem; color:var(--muted);">No active tunnels</p>
        </div>
      </div>
    </div>

    <!-- HELP -->
    <div class="card" style="margin-top:16px; background: var(--surface2); border-left: 3px solid var(--warn); padding: 14px 16px;">
      <p style="font-size:0.85rem; color:var(--text); line-height:1.6; margin:0;">
        <strong>Lost connection?</strong> Close the tunnel and reopen it, or select a different job and open a new tunnel on a different local port.
      </p>
    </div>

    <!-- CLEANUP -->
    <div class="card" style="margin-top:16px; background: var(--surface2); border-left: 3px solid var(--warn); padding: 14px 16px; border-radius:6px;">
      <h3 style="font-size:0.95rem; margin:0 0 10px 0;">Cleanup</h3>
      <p style="font-size:0.85rem; color:var(--muted); line-height:1.6; margin:0 0 12px 0;">
        Cancel your RStudio jobs when done. Close tunnels using the × button in Step 2 above.
      </p>
      <button class="action btn-danger" id="btnCancelAll" onclick="cancelAllRStudioJobs()" disabled style="width:100%;">⏹ Cancel All RStudio Jobs</button>
    </div>

    <!-- TUNNEL LOG -->
    <div class="card" style="margin-top:16px">
      <h3><span class="icon">📋</span> Tunnel Log</h3>
      <div class="log-box" id="logBox2" style="height:180px;"></div>
    </div>
  </div>

  <!-- SETUP GUIDE TAB -->
  <div class="panel" id="panel-setup">
    <div class="guide">

      <h2 id="guideTitle">&#x1F4D6; HPC Setup Guide</h2>
      <p id="guideIntro">Welcome! Start with the <strong>SSH prerequisites</strong> below, then choose the scenario that matches your situation.</p>

      <!-- ═══════════════════════════════════════════ -->
      <!-- ALWAYS VISIBLE: SSH PREREQUISITES          -->
      <!-- ═══════════════════════════════════════════ -->
      <div class="guide-extra" id="guidePrereqs">

        <h2>&#x1F511; Step 0 &mdash; SSH Prerequisites (everyone)</h2>
        <p>Before doing anything else, make sure SSH access to the HPC is working. You connect through a <strong>gateway</strong> (<code>hpcgw.op.umcutrecht.nl</code>) to reach the actual login node (<code>hpcs06.op.umcutrecht.nl</code>). Direct access to the login node is not possible.</p>

        <div class="step-group">
          <h3><span class="step-num">1</span> Test that you can connect</h3>
          <p>Open a terminal on your Mac and run:</p>
          <pre>ssh YOUR_USERNAME@hpcgw.op.umcutrecht.nl</pre>
          <p>If this works (you get a shell prompt), you can reach the gateway. Type <code>exit</code> to disconnect.</p>
          <div class="tip">&#x1F4A1; If this fails, contact your HPC admin &mdash; your account may not be set up yet, or you may need to be on the hospital VPN / network.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">2</span> Set up SSH config (recommended)</h3>
          <p>Add this block to <code>~/.ssh/config</code> on your <strong>local Mac</strong>. It creates a shortcut so you (and this launcher) can reach the login node in a single hop:</p>
          <pre>Host gw2hpcs06
    HostName hpcs06.op.umcutrecht.nl
    User YOUR_USERNAME
    ProxyJump YOUR_USERNAME@hpcgw.op.umcutrecht.nl</pre>
          <p>After saving, test it:</p>
          <pre>ssh gw2hpcs06</pre>
          <p>You should land directly on the login node <code>hpcs06</code>. This config tells SSH to automatically jump through the gateway to reach the login node.</p>
          <div class="tip">&#x1F4A1; Once this works, enter <code>gw2hpcs06</code> in the <strong>SSH Alias</strong> field on the Launch tab. The launcher will then use this alias for SSH tunnels instead of building the multi-hop command itself.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">3</span> SSH keys (optional but handy)</h3>
          <p>To avoid typing your password every time the tunnel opens, you can copy your SSH key to the gateway:</p>
          <pre>ssh-copy-id YOUR_USERNAME@hpcgw.op.umcutrecht.nl</pre>
          <p>After that, SSH (and the launcher's tunnel) will connect without a password prompt.</p>
        </div>

      </div>

      <!-- ── OVERVIEW CARDS ── -->
      <h2 style="margin-top:28px;">Choose your scenario</h2>
      <div class="guide-overview" id="guideCards">
        <div class="guide-card" onclick="showScenario('fresh')">
          <div class="card-icon">&#x1F195;</div>
          <div class="card-title">Fresh Setup</div>
          <div class="card-desc">Never used RStudio on the HPC? Start here to set up everything from scratch.</div>
        </div>
        <div class="guide-card" onclick="showScenario('newversion')">
          <div class="card-icon">&#x1F4E6;</div>
          <div class="card-title">New R Version</div>
          <div class="card-desc">Add a new R / Bioconductor version to an existing group setup.</div>
        </div>
        <div class="guide-card" onclick="showScenario('existing')">
          <div class="card-icon">&#x2705;</div>
          <div class="card-title">Existing Setup</div>
          <div class="card-desc">Someone in your group already set things up? Just configure &amp; launch.</div>
        </div>
      </div>

      <!-- ═══════════════════════════════════════════ -->
      <!-- SCENARIO: FRESH SETUP                      -->
      <!-- ═══════════════════════════════════════════ -->
      <div class="guide-scenario" id="scenario-fresh">
        <span class="guide-back" onclick="showOverview()">&#x2190; Back to overview</span>
        <h2>&#x1F195; Complete Fresh Setup</h2>
        <p style="color:var(--muted); margin-bottom:14px;">You have <strong>never used RStudio on the HPC</strong> and need to set up everything from scratch.</p>

        <div class="step-group">
          <h3><span class="step-num">1</span> Get the template script</h3>
          <p>The file <code>rstudio_server_template.sh</code> is included in this project. Copy it to a convenient location and rename it:</p>
          <pre>cp rstudio_server_template.sh rstudio_server.sh</pre>
          <p>This template has a <code>USER CONFIGURATION</code> block at the top with clearly marked <code>CHANGE_ME</code> placeholders. You will fill these in during Step 5 below.</p>
        </div>

        <div class="step-group">
          <h3><span class="step-num">2</span> Download Apptainer images</h3>
          <p>Start an interactive job on the HPC and pull the container images you need:</p>
          <pre>srun --pty bash

# Create a directory for your images
mkdir -p /hpc/pmc_YOUR_GROUP/singularity
cd /hpc/pmc_YOUR_GROUP/singularity

# Pull the RStudio / Bioconductor image
apptainer pull docker://bioconductor/bioconductor_docker:RELEASE_3_20-R-4.4.2
mv bioconductor_docker_RELEASE_3_20-R-4.4.2.sif rstudio_4.4.2_bioconductor.sif

# Pull the Python helper image (used internally for port detection)
apptainer pull docker://python:3.11.3
mv python_3.11.3.sif python_3.11.3.sif</pre>
          <div class="tip">&#x1F4A1; Replace version numbers with the ones you need. Check available tags at <code>hub.docker.com/r/bioconductor/bioconductor_docker</code>.</div>
          <p style="margin-top:8px;">&#x1F4DD; <strong>Remember the path</strong> to this directory &mdash; you will enter it as <code>singularity_dir</code> in the script.</p>
        </div>

        <div class="step-group">
          <h3><span class="step-num">3</span> Create directories</h3>
          <p>You need three directories. Create them on the HPC:</p>
          <pre># 1. R package library
#    This is where R packages (install.packages / BiocManager) get installed to.
#    You can share this with your group, or keep it personal (see note below).
mkdir -p /hpc/pmc_YOUR_GROUP/Rstudio_Server_Libs/Rstudio_4.4.2_libs

# 2. Personal session data (RStudio bookmarks, history, etc.)
mkdir -p /hpc/pmc_YOUR_GROUP/YOUR_USERNAME/rStudioSessions

# 3. Personal temp directory (avoids filling up /tmp on the compute node)
mkdir -p /hpc/pmc_YOUR_GROUP/YOUR_USERNAME/temp_dir</pre>

          <div class="tip">&#x1F4A1; <strong>Shared vs. personal R library:</strong> If you use a shared path like
          <code>/hpc/pmc_YOUR_GROUP/Rstudio_Server_Libs/Rstudio_4.4.2_libs</code>, everyone in
          your group shares the same installed packages &mdash; handy, but someone might update a
          package and break another person's code. If you prefer isolation, create your own library
          folder instead, e.g. <code>/hpc/pmc_YOUR_GROUP/YOUR_USERNAME/Rstudio_4.4.2_libs</code>,
          and use that as <code>R_LIBS_USER_PATH</code> in the script.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">4</span> Set up TMPDIR in your R projects</h3>
          <p>During computation, R creates temporary files (for downloads, package compilation, etc.). On an HPC compute node, these temporary files are written to <code>/tmp</code>, which has limited space. If R fills it up, you'll see "disk full" errors and your session may become unstable.</p>
          <p>To avoid this, you can tell R to use your personal temp directory instead. <strong>In every R project</strong> you work on, create or edit the file <code>.Renviron</code> (in the project root) and add:</p>
          <pre>TMPDIR=/hpc/pmc_YOUR_GROUP/YOUR_USERNAME/temp_dir</pre>
          <p>Restart the R session after adding this. Now R will write temporary files to your allocated space instead of the crowded <code>/tmp</code>.</p>
          <div class="tip">&#x1F4A1; This is optional but highly recommended, especially for large analyses or package compilation.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">5</span> Configure the script</h3>
          <p>Open <code>rstudio_server.sh</code> and fill in the <code>USER CONFIGURATION</code> section at the top. Each variable maps directly to a directory you just created:</p>

          <table style="width:100%; font-size:0.88rem; margin:10px 0; border-collapse:collapse;">
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:8px 10px;"><code>version</code></td>
              <td style="padding:8px 10px;">The R version you pulled, e.g. <code>"4.4.2"</code></td>
            </tr>
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:8px 10px;"><code>singularity_dir</code></td>
              <td style="padding:8px 10px;">Path to the directory from <strong>Step 2</strong> that contains your <code>.sif</code> images</td>
            </tr>
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:8px 10px;"><code>XDG_DATA_HOME_PATH</code></td>
              <td style="padding:8px 10px;">The session data directory from <strong>Step 3</strong> (e.g. <code>.../YOUR_USERNAME/rStudioSessions</code>)</td>
            </tr>
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:8px 10px;"><code>R_LIBS_USER_PATH</code></td>
              <td style="padding:8px 10px;">The R package library from <strong>Step 3</strong> (e.g. <code>.../Rstudio_4.4.2_libs</code>)</td>
            </tr>
            <tr>
              <td style="padding:8px 10px;"><code>PYTHON_SIF</code></td>
              <td style="padding:8px 10px;">Full path to the Python <code>.sif</code> from <strong>Step 2</strong></td>
            </tr>
          </table>

          <p>Also check the <code>APPTAINER_BIND</code> lines further in the script &mdash; they need to include the directories you created so they are visible inside the container.</p>

          <div class="tip">&#x1F4A1; <strong>What the launcher does automatically:</strong> When you click <em>Submit Job</em> on the Launch tab, this app reads the script from the HPC, replaces the <code>version</code> value and the <code>XDG_DATA_HOME</code> value with whatever you entered in the <strong>R Version</strong> and <strong>XDG_DATA_HOME</strong> fields on the Launch tab, then submits the patched script. This means you can switch R versions and session directories without manually editing the script every time. All other variables (<code>singularity_dir</code>, <code>R_LIBS_USER_PATH</code>, <code>PYTHON_SIF</code>, <code>APPTAINER_BIND</code>) must be set correctly in the script file itself &mdash; the launcher does not change those.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">6</span> Upload and launch</h3>
          <p>Upload the configured script to the HPC (e.g. to your home directory <code>~/rstudio_server.sh</code>), then:</p>
          <ol>
            <li>Go to the <strong>&#x1F680; Launch Job</strong> tab</li>
            <li>Fill in your <strong>Username</strong>, the <strong>Script path</strong> on the HPC (e.g. <code>~/rstudio_server.sh</code>), desired <strong>R Version</strong>, and your <strong>XDG_DATA_HOME</strong> session directory path</li>
            <li>Click <strong>Submit Job</strong></li>
          </ol>
          <p>The launcher will connect to the HPC, patch the script, submit it via <code>sbatch</code>, and poll the log file for connection details.</p>
        </div>
      </div>

      <!-- ═══════════════════════════════════════════ -->
      <!-- SCENARIO: NEW R VERSION                    -->
      <!-- ═══════════════════════════════════════════ -->
      <div class="guide-scenario" id="scenario-newversion">
        <span class="guide-back" onclick="showOverview()">&#x2190; Back to overview</span>
        <h2>&#x1F4E6; Adding a New R Version</h2>
        <p style="color:var(--muted); margin-bottom:14px;">Your group already has a working setup, but you need a <strong>different R version</strong>.</p>

        <div class="step-group">
          <h3><span class="step-num">1</span> Download the new Apptainer image</h3>
          <p>Start an interactive job and pull the image into your group's shared image directory:</p>
          <pre>srun --pty bash
cd /hpc/pmc_YOUR_GROUP/singularity

apptainer pull docker://bioconductor/bioconductor_docker:RELEASE_3_20-R-4.4.2
mv bioconductor_docker_RELEASE_3_20-R-4.4.2.sif rstudio_4.4.2_bioconductor.sif</pre>
          <div class="tip">&#x1F4A1; Use <code>devel</code> instead of a release tag for the latest development version.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">2</span> Create a new R packages directory</h3>
          <p>Each R version needs its <strong>own</strong> library directory (mixing versions breaks packages):</p>
          <pre>mkdir -p /hpc/pmc_YOUR_GROUP/Rstudio_Server_Libs/Rstudio_4.4.2_libs</pre>
          <div class="tip">&#x1F4A1; If you want your own private library instead of a shared one, use a personal path like <code>/hpc/pmc_YOUR_GROUP/YOUR_USERNAME/Rstudio_4.4.2_libs</code> and update <code>R_LIBS_USER_PATH</code> in the script to match.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">3</span> Update and launch</h3>
          <p>On the <strong>&#x1F680; Launch Job</strong> tab, change the <strong>R Version</strong> field to the new version (e.g. <code>4.4.2</code>) and click Submit.</p>
          <p>The launcher automatically patches <code>version</code> and <code>XDG_DATA_HOME</code> in the script before submitting. If the <code>R_LIBS_USER_PATH</code> in your script uses the <code>version</code> variable (like the template does &mdash; <code>Rstudio_&#36;{version}_libs</code>), then it will also resolve to the correct library directory automatically. No manual script editing needed.</p>
          <p>If you changed <code>R_LIBS_USER_PATH</code> to a custom personal path, make sure that path is also updated in the script for the new version.</p>
        </div>

        <div class="step-group">
          <h3><span class="step-num">4</span> Don't forget TMPDIR</h3>
          <p>In every R project, make sure <code>.Renviron</code> contains:</p>
          <pre>TMPDIR=/hpc/pmc_YOUR_GROUP/YOUR_USERNAME/temp_dir</pre>
          <p>Create the directory if it doesn't exist yet. This prevents <code>/tmp</code> from filling up.</p>
        </div>
      </div>

      <!-- ═══════════════════════════════════════════ -->
      <!-- SCENARIO: EXISTING SETUP                   -->
      <!-- ═══════════════════════════════════════════ -->
      <div class="guide-scenario" id="scenario-existing">
        <span class="guide-back" onclick="showOverview()">&#x2190; Back to overview</span>
        <h2>&#x2705; Using an Existing Setup</h2>
        <p style="color:var(--muted); margin-bottom:14px;">Someone in your group has already set everything up. You just need to configure a few personal things.</p>

        <div class="step-group">
          <h3><span class="step-num">1</span> Copy the script</h3>
          <p>Ask your group member for the <code>rstudio_server.sh</code> script and copy it to your home directory on the cluster:</p>
          <pre>cp /path/to/groups/rstudio_server.sh ~/rstudio_server.sh</pre>
          <p>The script already has the correct <code>singularity_dir</code>, <code>R_LIBS_USER_PATH</code>, and <code>PYTHON_SIF</code> for your group. You only need to set up your personal directories.</p>
        </div>

        <div class="step-group">
          <h3><span class="step-num">2</span> Create your personal directories</h3>
          <p>You need two directories of your own:</p>
          <pre># Session data (RStudio bookmarks, history, workspace)
mkdir -p /hpc/pmc_YOUR_GROUP/YOUR_USERNAME/rStudioSessions

# Temp directory (prevents /tmp from filling up)
mkdir -p /hpc/pmc_YOUR_GROUP/YOUR_USERNAME/temp_dir</pre>
          <div class="tip">&#x1F4A1; <strong>Optional &mdash; personal R library:</strong> By default the script likely uses a shared R package library. If you want your own isolated library (so others can't overwrite your packages), create a personal one:<br>
          <code>mkdir -p /hpc/pmc_YOUR_GROUP/YOUR_USERNAME/Rstudio_4.4.2_libs</code><br>
          Then update <code>R_LIBS_USER_PATH</code> in your copy of the script to point to it.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">3</span> Set up TMPDIR</h3>
          <p><strong>In every R project</strong> you work on, create or edit <code>.Renviron</code> in the project root and add:</p>
          <pre>TMPDIR=/hpc/pmc_YOUR_GROUP/YOUR_USERNAME/temp_dir</pre>
          <p>This tells R to use your personal temp directory instead of <code>/tmp</code>. Restart R after adding this.</p>
          <div class="tip">&#x1F4A1; This prevents "disk full" errors when R creates temporary files during computation or package compilation.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">4</span> Launch!</h3>
          <p>Go to the <strong>&#x1F680; Launch Job</strong> tab and fill in:</p>
          <ul>
            <li><strong>Username</strong> &mdash; your HPC username</li>
            <li><strong>Script path</strong> &mdash; where you copied the script (e.g. <code>~/rstudio_server.sh</code>)</li>
            <li><strong>R Version</strong> &mdash; a version available in your group's <code>singularity</code> directory</li>
            <li><strong>XDG_DATA_HOME</strong> &mdash; the session directory you created in Step 2 (e.g. <code>/hpc/pmc_YOUR_GROUP/YOUR_USERNAME/rStudioSessions</code>)</li>
          </ul>
          <p>Click <strong>Submit Job</strong>. The launcher will read the script from the HPC, automatically replace the <code>version</code> and <code>XDG_DATA_HOME</code> values with what you entered above, submit it via <code>sbatch</code>, and monitor the log for connection details. You don't need to manually edit the script file for these two values.</p>
        </div>
      </div>

      <!-- ═══════════════════════════════════════════ -->
      <!-- ALWAYS VISIBLE: CONNECT TO RSTUDIO         -->
      <!-- ═══════════════════════════════════════════ -->
      <div class="guide-extra">

        <h2>&#x1F517; Step 5 &mdash; Connect to RStudio</h2>
        <p>Once your job is running (you'll see the connection details in the Launch Job log), go to the <strong>🔗 Connect</strong> tab to reach your RStudio instance.</p>

        <div class="step-group">
          <h3><span class="step-num">1</span> Load your job</h3>
          <p>In the <strong>Connect</strong> tab, you'll see two ways to find your job:</p>
          <ul>
            <li><strong>Last Submitted</strong> &mdash; Your most recent job (if it's still running)</li>
            <li><strong>Find Other Jobs</strong> &mdash; Search for any running RStudio job by username</li>
          </ul>
          <p>Select your job. The app will extract the hostname, port, username, and password from your SLURM log.</p>
        </div>

        <div class="step-group">
          <h3><span class="step-num">2</span> Open SSH Tunnel</h3>
          <p>Click the <strong>&#x1F517; Open SSH Tunnel</strong> button. This creates a secure port-forwarded connection from your Mac to the RStudio port on the compute node. You'll see:</p>
          <ul>
            <li>The tunnel appear in the <strong>Active Tunnels</strong> list</li>
            <li>The 🌐 <strong>Open</strong> button becomes available</li>
            <li>Messages in the Tunnel Log showing the connection is established</li>
          </ul>
          <p><strong>Tip:</strong> You can press <strong>Enter</strong> to open the tunnel instead of clicking the button.</p>
        </div>

        <div class="step-group">
          <h3><span class="step-num">3</span> Open in Browser</h3>
          <p>Click the 🌐 <strong>Open</strong> button next to your active tunnel. Your default browser will open with RStudio Server ready to use.</p>
          <p>Log in with the username and password shown in the job info.</p>
          <div class="tip">&#x1F4A1; <strong>Can't connect?</strong> See the <strong>"Can't connect to the server?"</strong> troubleshooting section below for manual SSH tunnel steps.</div>
        </div>

        <div class="step-group">
          <h3><span class="step-num">4</span> When done</h3>
          <p>To cleanly shut down:</p>
          <ol>
            <li>Exit RStudio (power button in top-right corner)</li>
            <li>Close the tunnel (× button next to it in Active Tunnels)</li>
            <li>Cancel the job (⏹ <strong>Cancel All RStudio Jobs</strong> button, which closes all tunnels automatically)</li>
          </ol>
          <p>You can also manage multiple tunnels at once if you have several jobs running.</p>
        </div>

        <div class="step-group">
          <h3>&#x26A0;&#xFE0F; Can't connect to the server?</h3>
          <p>If you get an error when clicking the 🌐 <strong>Open</strong> button, the SSH tunnel may not have connected properly. Try manually opening the tunnel:</p>
          <ol>
            <li>Go to the HPC and navigate to your selected <strong>XDG_DATA_HOME</strong> location where your RStudio job file was created</li>
            <li>Open the job file (it will be named something like <code>rstudio_job_*.sh</code>) and find the line that sets up the SSH tunnel, typically something like:
              <pre>ssh -L 8787:${HOSTNAME}:${PORT} gw2hpcs06</pre>
            </li>
            <li>Copy that entire SSH command and paste it into a new terminal window on your laptop</li>
            <li>When you run it, SSH will ask you to validate the fingerprint (the host's cryptographic signature). Answer <strong>yes</strong> to accept and verify the connection</li>
            <li>Once the tunnel is established, try opening RStudio in your browser again</li>
          </ol>
          <div class="tip">&#x1F4A1; If you're having trouble locating the SSH command in your job file, look for the line containing both <code>-L 8787</code> and <code>gw2hpcs06</code>.</div>
        </div>

      </div>

      <!-- ═══════════════════════════════════════════ -->
      <!-- ALWAYS VISIBLE: TIPS & TROUBLESHOOTING     -->
      <!-- ═══════════════════════════════════════════ -->
      <div class="guide-extra">

        <h2>&#x1F4CC; Tips &amp; Troubleshooting</h2>

        <div class="step-group">
          <h3>&#x1F4C2; Navigate to your files in RStudio</h3>
          <p>When you first open RStudio, the <strong>Files</strong> pane (bottom right) shows your home directory. To access your actual data files, you need to navigate to where they are stored on the HPC.</p>
          <p>In the <strong>Files</strong> pane, look for the <strong>three dots</strong> menu button (&#x22EE;) in the top right corner. Click it and select <strong>"Go to folder..."</strong> or <strong>"More..."</strong>. Then type the full path to your data, for example:</p>
          <pre>/hpc/pmc_research/shared_project/data</pre>
          <p>Press Enter and RStudio will navigate to that folder. You can also use this menu to set a bookmark for folders you visit often.</p>
        </div>

        <div class="step-group">
          <h3>&#x1F50C; Lost connection?</h3>
          <p>If RStudio disconnects, close the tunnel by clicking the × button next to it in Step 2, re-open it (&#x1F517; <strong>Open SSH Tunnel</strong> on the Connect tab), and refresh the browser. Your SLURM job (and R session) is still running &mdash; you just need a new tunnel.</p>
        </div>

        <div class="step-group">
          <h3>&#x1F6D1; Shutting down properly</h3>
          <ol>
            <li>Exit the RStudio session (power button in RStudio's top-right corner)</li>
            <li>Close the SSH tunnel (× button next to it in the active tunnels list on the Connect tab)</li>
            <li>Cancel the SLURM job (or use the ⏹ <strong>Cancel All RStudio Jobs</strong> button, which will close tunnels automatically)</li>
          </ol>
        </div>

        <div class="step-group">
          <h3>&#x1F4C2; Where are my files?</h3>
          <p>Summary of all the directories and what they're for:</p>
          <table style="width:100%; font-size:0.85rem; border-collapse:collapse;">
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:6px 10px;"><code>singularity_dir</code></td>
              <td style="padding:6px 10px;">Container images (<code>.sif</code> files)</td>
            </tr>
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:6px 10px;"><code>R_LIBS_USER_PATH</code></td>
              <td style="padding:6px 10px;">Installed R packages (one per R version)</td>
            </tr>
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:6px 10px;"><code>XDG_DATA_HOME</code></td>
              <td style="padding:6px 10px;">RStudio session data (personal, per user)</td>
            </tr>
            <tr style="border-bottom:1px solid var(--surface2);">
              <td style="padding:6px 10px;"><code>TMPDIR</code></td>
              <td style="padding:6px 10px;">R temp files (set in <code>.Renviron</code>, personal)</td>
            </tr>
            <tr>
              <td style="padding:6px 10px;"><code>PYTHON_SIF</code></td>
              <td style="padding:6px 10px;">Python container for port detection (shared)</td>
            </tr>
          </table>
        </div>

        <div class="warn">&#x26A0;&#xFE0F; Always cancel your job when done to free up cluster resources for others!</div>

      </div>

    </div>
  </div>
</div>

<script>
// ── Guide interactivity ──
function showScenario(name) {
  // Hide all scenarios
  document.querySelectorAll('.guide-scenario').forEach(function(s){ s.classList.remove('visible'); });
  // Deactivate all cards
  document.querySelectorAll('.guide-card').forEach(function(c){ c.classList.remove('active'); });
  // Show the chosen scenario
  var el = document.getElementById('scenario-' + name);
  if (el) el.classList.add('visible');
  // Highlight the clicked card
  var cards = document.querySelectorAll('.guide-card');
  var idx = {fresh:0, newversion:1, existing:2}[name];
  if (idx !== undefined && cards[idx]) cards[idx].classList.add('active');
  // Scroll the scenario into view
  if (el) el.scrollIntoView({behavior:'smooth', block:'start'});
}
function showOverview() {
  document.querySelectorAll('.guide-scenario').forEach(function(s){ s.classList.remove('visible'); });
  document.querySelectorAll('.guide-card').forEach(function(c){ c.classList.remove('active'); });
  document.getElementById('guideCards').scrollIntoView({behavior:'smooth', block:'center'});
}

// Tab switching
function switchTab(el, name) {
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.remove('active'); });
  document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
  document.getElementById('panel-' + name).classList.add('active');
  el.classList.add('active');
}

// Poll state every 1.5s
var lastLogLen = 0;
function poll() {
  fetch('/api/state').then(function(r){ return r.json(); }).then(function(s){
    // Status dots (top and bottom of launch tab)
    var dot = document.getElementById('statusDot');
    dot.className = 'status-dot ' + s.status;
    document.getElementById('statusText').textContent = s.status_msg;
    
    var dot2 = document.getElementById('statusDot2');
    dot2.className = 'status-dot ' + s.status;
    document.getElementById('statusText2').textContent = s.status_msg;
    
    // Show hint when job is running
    var hint = document.getElementById('statusHint');
    if (s.status === 'running' || (s.job_id && s.remote_hostname && s.remote_port)) {
      hint.style.display = 'block';
    } else {
      hint.style.display = 'none';
    }

    // Logs
    var box = document.getElementById('logBox');
    if (s.logs.length !== lastLogLen) {
      box.textContent = s.logs.join('\n');
      box.scrollTop = box.scrollHeight;
      lastLogLen = s.logs.length;
    }

    // Tunnel logs
    var box2 = document.getElementById('logBox2');
    if (s.tunnel_logs && s.tunnel_logs.length > 0) {
      box2.textContent = s.tunnel_logs.join('\n');
      box2.scrollTop = box2.scrollHeight;
    }

    // Connection info
    function setInfo(id, val) {
      var el = document.getElementById(id);
      el.textContent = val || '\u2013';
      el.className = val ? 'val' : 'val empty';
    }
    setInfo('infoJobId', s.job_id);
    // Combine host and port into one field
    var hostPort = (s.remote_hostname && s.remote_port) ? s.remote_hostname + ':' + s.remote_port : null;
    setInfo('infoHostPort', hostPort);
    setInfo('infoUser', s.rstudio_user);
    setInfo('infoPass', s.rstudio_password);
    
    // Show/hide job ready hint
    var jobReadyHint = document.getElementById('jobReadyHint');
    if (s.job_id && s.remote_hostname && s.remote_port) {
      jobReadyHint.style.display = 'block';
    } else {
      jobReadyHint.style.display = 'none';
    }

    // Tunnel error display
    var errorBox = document.getElementById('tunnelErrorBox');
    if (s.tunnel_error) {
      errorBox.style.display = 'block';
      document.getElementById('tunnelErrorMsg').textContent = s.tunnel_error;
    } else {
      errorBox.style.display = 'none';
    }

    // Active jobs display
    if (s.active_jobs && s.active_jobs.length > 0) {
      var jobsList = document.getElementById('activeJobsList');
      var html = '';
      s.active_jobs.forEach(function(job, idx) {
        html += '<div style="background:var(--surface2);padding:10px;border-radius:6px;border-left:3px solid var(--success); display:flex; justify-content:space-between; align-items:center; gap:8px;">';
        html += '<div style="font-size:0.85rem; flex:1;">';
        html += '<strong>Job ' + job.job_id + '</strong> | ' + job.hostname + ':' + job.port + ' | ' + job.rstudio_user;
        html += '</div>';
        html += '<button class="action btn-primary" style="font-size:0.75rem;padding:4px 8px; white-space:nowrap;" onclick="loadJob(' + idx + ')">Load</button>';
        html += '<button class="action btn-danger" style="font-size:0.75rem;padding:4px 8px; white-space:nowrap;" onclick="cancelActiveJob(' + idx + ')">Cancel</button>';
        html += '</div>';
      });
      jobsList.innerHTML = html;
      document.getElementById('activeJobsPanel').style.display = 'block';
    } else if (document.getElementById('discover_user').value.trim()) {
      // Show message only if user has searched
      var timeStr = window.lastJobsCheckTime ? window.lastJobsCheckTime.toLocaleTimeString() : 'unknown';
      var jobsList = document.getElementById('activeJobsList');
      jobsList.innerHTML = '<div style="background:var(--surface2);padding:12px;border-radius:6px;border-left:3px solid var(--muted);"><p style="font-size:0.85rem; color:var(--text); margin:0;">No active RStudio jobs found for this user.</p><p style="font-size:0.8rem; color:var(--muted); margin:8px 0 0 0;">Go to the <strong>🚀 Launch</strong> tab to submit a new job.</p><p style="font-size:0.75rem; color:var(--muted); margin:8px 0 0 0;">Checked at ' + timeStr + '</p></div>';
      document.getElementById('activeJobsPanel').style.display = 'block';
    } else {
      document.getElementById('activeJobsPanel').style.display = 'none';
    }

    // Active tunnels display
    if (s.active_tunnels && s.active_tunnels.length > 0) {
      var tunnelsList = document.getElementById('activeTunnelsList');
      var html = '';
      s.active_tunnels.forEach(function(tunnel, idx) {
        html += '<div style="background:var(--surface2);padding:10px;border-radius:6px;border-left:3px solid var(--accent); display:flex; justify-content:space-between; align-items:center; gap:8px;">';
        html += '<div style="font-size:0.85rem; flex:1;">';
        html += '<strong>localhost:' + tunnel.local_port + '</strong> → ' + tunnel.remote_host + ':' + tunnel.remote_port;
        if (tunnel.job_id) {
          html += '<br/><span style="color:var(--muted);">Job ' + tunnel.job_id + '</span>';
        }
        html += '</div>';
        html += '<button class="action btn-success" style="font-size:0.75rem; padding:6px 10px; white-space:nowrap;" onclick="openBrowserForPort(' + tunnel.local_port + ')">🌐 Open</button>';
        html += '<button class="action btn-danger" style="font-size:0.75rem; padding:6px 10px; white-space:nowrap;" onclick="closeTunnelByPort(' + tunnel.local_port + ')">× Close</button>';
        html += '</div>';
      });
      tunnelsList.innerHTML = html;
    } else {
      document.getElementById('activeTunnelsList').innerHTML = '<p style="font-size:0.85rem; color:var(--muted);">No active tunnels</p>';
    }

    // Buttons
    document.getElementById('btnSubmit').disabled = (s.status === 'submitting' || s.status === 'waiting');
    document.getElementById('btnCancel').disabled = !s.job_id;
    document.getElementById('btnTunnel').disabled = !(s.remote_hostname && s.remote_port) || s.tunnel_active;
    // Enable cancel all button if there are tunnels, jobs, or active jobs
    var hasTunnels = s.active_tunnels && s.active_tunnels.length > 0;
    var hasJobs = s.job_id || (s.active_jobs && s.active_jobs.length > 0);
    document.getElementById('btnCancelAll').disabled = !(hasTunnels || hasJobs);

    // Tunnel status text
    var ts = document.getElementById('tunnelStatus');
    ts.textContent = s.tunnel_active ? 'Tunnel: active ✅' : 'Tunnel: not active';
    ts.style.color = s.tunnel_active ? 'var(--success)' : 'var(--muted)';
  });
}
setInterval(poll, 1500);
poll();

// ── Settings persistence ──
var FIELD_IDS = ['hpc_user','gateway_host','login_node','ssh_alias','script_path',
                 'wall_time','cpus','mem','tmpspace','r_version','xdg_data_home','local_port','discover_user'];

function loadSettings() {
  fetch('/api/settings').then(function(r){ return r.json(); }).then(function(s){
    FIELD_IDS.forEach(function(id){
      var el = document.getElementById(id);
      if (el && s[id] !== undefined && s[id] !== '') {
        el.value = s[id];
      }
    });
    // Auto-search for jobs if username is saved
    if (s.discover_user && s.discover_user.trim()) {
      console.log('Auto-searching for jobs for user:', s.discover_user);
      setTimeout(function(){
        discoverJobs();
      }, 500);
    }
  });
}
loadSettings();

var _saveTimer = null;
function saveSettings() {
  var body = {};
  FIELD_IDS.forEach(function(id){
    var el = document.getElementById(id);
    if (el) body[id] = el.value;
  });
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
}
function debouncedSave() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(saveSettings, 500);
}

// Auto-save whenever any field changes (debounced)
FIELD_IDS.forEach(function(id){
  var el = document.getElementById(id);
  if (el) {
    el.addEventListener('input', debouncedSave);
    el.addEventListener('change', saveSettings);
  }
});

// Actions
function submitJob() {
  saveSettings();
  var body = {
    hpc_user:      document.getElementById('hpc_user').value,
    gateway_host:  document.getElementById('gateway_host').value,
    login_node:    document.getElementById('login_node').value,
    script_path:   document.getElementById('script_path').value,
    wall_time:     document.getElementById('wall_time').value,
    cpus:          document.getElementById('cpus').value,
    mem:           document.getElementById('mem').value,
    tmpspace:      document.getElementById('tmpspace').value,
    r_version:     document.getElementById('r_version').value,
    xdg_data_home: document.getElementById('xdg_data_home').value,
  };
  fetch('/api/submit', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
}
function cancelJob() {
  fetch('/api/cancel', {method:'POST'});
}
function cancelAllRStudioJobs() {
  if (!confirm('Cancel ALL RStudio jobs? Associated tunnels will also be closed.')) {
    return;
  }
  console.log('Canceling all RStudio jobs...');
  
  // First, get state to find all tunnels associated with jobs
  fetch('/api/state')
    .then(function(r){ return r.json(); })
    .then(function(s){
      // Close tunnels associated with jobs
      if (s.active_tunnels && s.active_tunnels.length > 0) {
        console.log('Closing', s.active_tunnels.length, 'associated tunnels...');
        s.active_tunnels.forEach(function(tunnel){
          fetch('/api/tunnel/close', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({port: tunnel.local_port})
          }).then(function(r){
            console.log('Closed tunnel on port', tunnel.local_port);
          });
        });
      }
      return s;
    })
    .then(function(s){
      // Cancel the currently selected job
      return fetch('/api/cancel', {method:'POST'})
        .then(function(r){
          console.log('Current job cancel response:', r.status);
          return s;
        });
    })
    .then(function(s){
      // Also cancel any discovered jobs
      if (s.active_jobs && s.active_jobs.length > 0) {
        var user = document.getElementById('discover_user').value.trim();
        console.log('Canceling', s.active_jobs.length, 'discovered jobs for user:', user);
        var cancelPromises = s.active_jobs.map(function(job){
          return fetch('/api/cancel-active-job', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              job_id: job.job_id,
              hpc_user: user
            })
          }).then(function(r){
            console.log('Canceled job', job.job_id, '- response:', r.status);
          });
        });
        return Promise.all(cancelPromises);
      }
    })
    .then(function(){
      console.log('✓ All RStudio jobs canceled and associated tunnels closed');
    })
    .catch(function(err){
      console.error('Error during job cancellation:', err);
    });
}
function cancelAllJobsAndTunnels() {
  if (!confirm('Cancel ALL RStudio jobs and close ALL tunnels? This will free up all resources.')) {
    return;
  }
  console.log('Starting cleanup: closing tunnels and canceling jobs...');
  
  // Close all tunnels first
  fetch('/api/tunnel/close', {method:'POST'})
    .then(function(r){
      console.log('Tunnels close response:', r.status);
      return r.ok ? r.json().catch(function(){ return {}; }) : {};
    })
    .catch(function(e){ console.error('Tunnel close error:', e); })
    .then(function(){
      console.log('Now canceling currently selected job...');
      return fetch('/api/cancel', {method:'POST'});
    })
    .then(function(r){
      console.log('Cancel response:', r.status);
      return r.ok ? r.json().catch(function(){ return {}; }) : {};
    })
    .catch(function(e){ console.error('Cancel error:', e); })
    .then(function(){
      console.log('Fetching state to check for discovered jobs...');
      return fetch('/api/state');
    })
    .then(function(r){ return r.json(); })
    .then(function(s){
      console.log('State check complete. Active jobs:', s.active_jobs ? s.active_jobs.length : 0);
      if (s.active_jobs && s.active_jobs.length > 0) {
        var user = document.getElementById('discover_user').value.trim();
        console.log('Canceling', s.active_jobs.length, 'discovered jobs for user:', user);
        var cancelPromises = s.active_jobs.map(function(job){
          return fetch('/api/cancel-active-job', {
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({
              job_id: job.job_id,
              hpc_user: user
            })
          }).then(function(r){
            console.log('Canceled job', job.job_id, '- response:', r.status);
            return r.ok ? r.json().catch(function(){ return {}; }) : {};
          });
        });
        return Promise.all(cancelPromises);
      }
    })
    .then(function(){
      console.log('✓ All jobs and tunnels cleaned up successfully');
    })
    .catch(function(err){
      console.error('Error during cleanup:', err);
    });
}
function useLastJob() {
  // Confirm last job is ready and guide user to next step
  var jobId = document.getElementById('infoJobId').textContent;
  if (jobId === '–' || !jobId) {
    alert('No job submitted yet. Submit a job from the Launch tab first.');
    return;
  }
  fetch('/api/state').then(function(r){ return r.json(); }).then(function(s){
    if (s.remote_hostname && s.remote_port) {
      console.log('✓ Job ready. Opening SSH tunnel...');
      // Focus tunnel port input to guide user to next step
      setTimeout(function() {
        document.getElementById('tunnelPort').focus();
        document.getElementById('tunnelPort').select();
      }, 100);
    }
  });
}
function openTunnel() {
  var btn = document.getElementById('btnTunnel');
  var originalText = btn.textContent;
  
  // Show loading state
  btn.disabled = true;
  btn.textContent = '⏳ Opening tunnel...';
  
  var body = {
    local_port:   document.getElementById('local_port').value,
    ssh_alias:    document.getElementById('ssh_alias').value,
    hpc_user:     document.getElementById('hpc_user').value,
    gateway_host: document.getElementById('gateway_host').value,
    login_node:   document.getElementById('login_node').value,
  };
  
  fetch('/api/tunnel/open', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})
    .then(function(){
      // Reset button after a short delay to allow state update
      setTimeout(function(){
        btn.textContent = originalText;
      }, 1000);
    })
    .catch(function(err){
      btn.textContent = originalText;
      btn.disabled = false;
      console.error('Error opening tunnel:', err);
    });
}
function closeTunnelByPort(port) {
  // Close a specific tunnel by port (for closing from Active Tunnels list)
  fetch('/api/tunnel/close', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({port: port})});
}
function openBrowser() {
  var port = document.getElementById('local_port').value;
  fetch('/api/open-browser', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({port: port})});
}
function openBrowserForPort(port) {
  // Open browser for a specific tunnel port
  fetch('/api/open-browser', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({port: port})});
}

function discoverJobs() {
  var user = document.getElementById('discover_user').value.trim();
  if (!user) {
    alert('Please enter your HPC username');
    return;
  }
  document.getElementById('activeJobsPanel').style.display = 'block';
  document.getElementById('activeJobsList').innerHTML = '<p style="color:var(--muted); font-size:0.88rem;">Searching for your jobs...</p>';
  // Store the current time when search is performed
  window.lastJobsCheckTime = new Date();
  fetch('/api/discover-jobs', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({hpc_user: user})
  });
}

function loadJob(idx) {
  fetch('/api/state').then(function(r){ return r.json(); }).then(function(s){
    if (s.active_jobs && s.active_jobs[idx]) {
      var job = s.active_jobs[idx];
      console.log('Loading job:', job.job_id);
      fetch('/api/load-job', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          job_id: job.job_id,
          hostname: job.hostname,
          port: job.port,
          rstudio_user: job.rstudio_user,
          rstudio_password: job.rstudio_password
        })
      }).then(function(r){ return r.json(); }).then(function(data){
        console.log('Job loaded:', data);
        // Force UI update
        poll();
      });
    }
  });
}

function cancelActiveJob(idx) {
  fetch('/api/state').then(function(r){ return r.json(); }).then(function(s){
    if (s.active_jobs && s.active_jobs[idx]) {
      var job = s.active_jobs[idx];
      if (!confirm('Cancel job ' + job.job_id + '? Associated tunnels will also be closed.')) {
        return;
      }
      
      // First, close any tunnels associated with this job
      if (s.active_tunnels && s.active_tunnels.length > 0) {
        s.active_tunnels.forEach(function(tunnel){
          if (tunnel.job_id === job.job_id) {
            console.log('Closing tunnel on port', tunnel.local_port, 'for job', job.job_id);
            fetch('/api/tunnel/close', {
              method:'POST',
              headers:{'Content-Type':'application/json'},
              body:JSON.stringify({port: tunnel.local_port})
            }).then(function(r){
              console.log('Closed tunnel on port', tunnel.local_port);
            });
          }
        });
      }
      
      // Then cancel the job
      var user = document.getElementById('discover_user').value.trim();
      fetch('/api/cancel-active-job', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          job_id: job.job_id,
          hpc_user: user
        })
      }).then(function(r){
        console.log('Canceled job', job.job_id);
      });
    }
  });
}

// ─── SSH Authentication Modal ───────────────────────────────────────────
let authCheckInterval = null;

function startAuthCheck() {
  // Poll for auth requests every 500ms
  if (authCheckInterval) clearInterval(authCheckInterval);
  authCheckInterval = setInterval(checkAuthStatus, 500);
}

function stopAuthCheck() {
  if (authCheckInterval) {
    clearInterval(authCheckInterval);
    authCheckInterval = null;
  }
}

function checkAuthStatus() {
  fetch('/api/auth/status')
    .then(r => r.json())
    .then(data => {
      if (data.password_requested) {
        showAuthModal();
      } else {
        hideAuthModal();
      }
    })
    .catch(err => console.error('Auth status check failed:', err));
}

function showAuthModal() {
  document.getElementById('authModal').classList.add('active');
  document.getElementById('authPassword').focus();
}

function hideAuthModal() {
  document.getElementById('authModal').classList.remove('active');
  document.getElementById('authPassword').value = '';
}

function submitPassword() {
  var pwd = document.getElementById('authPassword').value;
  if (!pwd) {
    alert('Please enter a password');
    return;
  }
  
  fetch('/api/auth/provide-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: pwd })
  }).then(r => r.json())
    .then(data => {
      // Clear the input after sending (sensitive data)
      document.getElementById('authPassword').value = '';
      // Modal will auto-hide on next check
    })
    .catch(err => {
      alert('Failed to submit password');
      console.error(err);
    });
}

function cancelAuth() {
  hideAuthModal();
  stopAuthCheck();
  // User should cancel the job too
  alert('Authentication cancelled. Please cancel the job.');
}

// Start auth checking when page loads
window.addEventListener('load', startAuthCheck);
// Stop on page unload
window.addEventListener('unload', stopAuthCheck);

// Also check auth status on every state update
(function() {
  var originalFetch = fetch;
  window.fetchState = function() {
    return originalFetch('/api/state')
      .then(r => r.json())
      .then(data => {
        checkAuthStatus(); // Check auth status after state update
        return data;
      });
  };
})();
</script>

<!-- SSH Authentication Modal -->
<div class="modal-overlay" id="authModal">
  <div class="modal">
    <h2>SSH Authentication Required</h2>
    <p>Enter your HPC password or SSH key passphrase:</p>
    <input type="password" id="authPassword" class="modal-input" placeholder="Password / Passphrase" />
    <div class="modal-buttons">
      <button class="modal-btn modal-btn-submit" onclick="submitPassword()">Submit</button>
      <button class="modal-btn modal-btn-cancel" onclick="cancelAuth()">Cancel</button>
    </div>
  </div>
</div>

</body>
</html>
"""


# Tunnel persistence file
TUNNELS_INFO_FILE = os.path.join(os.path.expanduser("~"), ".rstudio_hpc_tunnels.json")


def _save_tunnel_info():
    """Persist active tunnels to disk for recovery on restart."""
    try:
        with open(TUNNELS_INFO_FILE, "w") as f:
            json.dump(state["active_tunnels"], f, indent=2)
    except Exception as e:
        log(f"Warning: could not save tunnel info: {e}")


def _load_tunnel_info():
    """Load previously saved tunnel information from disk."""
    try:
        if os.path.exists(TUNNELS_INFO_FILE):
            with open(TUNNELS_INFO_FILE, "r") as f:
                saved_tunnels = json.load(f)
            # Load the tunnels into state
            state["active_tunnels"] = saved_tunnels
            for tunnel in saved_tunnels:
                log(f"ℹ️  Restored tunnel on port {tunnel.get('local_port')} from previous session")
            return True
    except Exception as e:
        log(f"Note: Could not load saved tunnel info: {e}")
    return False


def _detect_existing_tunnels():
    """Scan for SSH tunnel processes from previous sessions."""
    import subprocess
    try:
        # Use ps to find existing SSH tunnel processes
        # Look for ssh processes with -L (local port forward) flag
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
        lines = result.stdout.split('\n')
        
        active_ports = set()
        for line in lines:
            if 'ssh' in line and '-L' in line and '-N' in line:
                # This is likely a tunnel process we created
                # Extract the local port from the -L flag
                parts = line.split()
                try:
                    # Find -L flag and get the port from the next part
                    for i, part in enumerate(parts):
                        if part == '-L' and i + 1 < len(parts):
                            port_forward = parts[i + 1]
                            # Format is typically: 8787:hostname:port
                            local_port = port_forward.split(':')[0]
                            active_ports.add(local_port)
                            break
                except Exception as e:
                    pass
        
        # Check if any saved tunnels are no longer running
        if active_ports:
            # Update tunnel_processes for the ports we found running
            for tunnel in state.get("active_tunnels", []):
                port_str = str(tunnel.get("local_port"))
                if port_str in active_ports:
                    log(f"✓ Tunnel on port {port_str} is still running")
                    # We can't directly reference the process, but we know it's running
    except Exception as e:
        log(f"Note: Could not scan for existing tunnels: {e}")


def _initialize_tunnels():
    """Initialize tunnel tracking on app startup."""
    # First, load previously saved tunnel info
    _load_tunnel_info()
    # Then, verify which ones are still running
    _detect_existing_tunnels()


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.json
    # Only persist the known setting keys
    to_save = {k: data.get(k, v) for k, v in SETTINGS_DEFAULTS.items()}
    save_settings(to_save)
    return jsonify({"ok": True})


@app.route("/api/state")
def api_state():
    return jsonify(state)


@app.route("/api/auth/status")
def api_auth_status():
    """Check if password input is being requested."""
    return jsonify({
        "password_requested": auth_state["password_requested"],
        "passphrase_requested": auth_state["passphrase_requested"],
    })


@app.route("/api/auth/provide-password", methods=["POST"])
def api_provide_password():
    """Submit password/passphrase for SSH authentication."""
    data = request.json
    password = data.get("password", "")
    
    # Never log or store the password
    auth_state["password_provided"] = password
    auth_state["password_requested"] = False
    
    log("Password input received (not logged)")
    return jsonify({"ok": True})


@app.route("/api/submit", methods=["POST"])
def api_submit():
    data = request.json
    if state["status"] in ("submitting", "waiting"):
        return jsonify({"error": "Already submitting"}), 409
    threading.Thread(target=_submit_thread, args=(data,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    _cancel_job()
    return jsonify({"ok": True})


@app.route("/api/discover-jobs", methods=["POST"])
def api_discover_jobs():
    """Discover active RStudio jobs for the user."""
    data = request.json
    user = data.get("hpc_user", "").strip()
    if not user:
        return jsonify({"error": "Username required"}), 400
    threading.Thread(target=_discover_active_jobs, args=(user,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/load-job", methods=["POST"])
def api_load_job():
    """Load a specific job's connection details."""
    data = request.json
    job_id = data.get("job_id")
    hostname = data.get("hostname")
    port = data.get("port")
    user = data.get("rstudio_user")
    password = data.get("rstudio_password")
    
    state["job_id"] = job_id
    state["remote_hostname"] = hostname
    state["remote_port"] = port
    state["rstudio_user"] = user
    state["rstudio_password"] = password
    state["status"] = "running"
    state["tunnel_error"] = None
    state["status_msg"] = f"Loaded job {job_id} - ready to connect!"
    log(f"Loaded existing job {job_id}")
    return jsonify({"ok": True})


@app.route("/api/cancel-active-job", methods=["POST"])
def api_cancel_active_job():
    """Cancel a specific active job from the job list."""
    data = request.json
    job_id = data.get("job_id")
    hpc_user = data.get("hpc_user", "").strip()
    
    if not job_id:
        return jsonify({"error": "Job ID required"}), 400
    
    try:
        client = ssh_clients.get("login")
        if client:
            client.exec_command(f"scancel -f {job_id}")
            log(f"Cancelled job {job_id}")
            log_tunnel(f"Cancelled job {job_id}")
            
            # Remove the log file
            if hpc_user:
                log_file = f"/home/pmc_research/{hpc_user}/rstudio-server.job.{job_id}"
                client.exec_command(f"rm -f {log_file}")
                log(f"Removed log file {log_file}")
                log_tunnel(f"Removed log file {log_file}")
            
            # Refresh the job list
            state["active_jobs"] = [j for j in state.get("active_jobs", []) if j["job_id"] != job_id]
            return jsonify({"ok": True})
    except Exception as e:
        log(f"Failed to cancel job: {e}")
        return jsonify({"error": str(e)}), 500
    
    return jsonify({"error": "Could not connect to HPC"}), 500


@app.route("/api/tunnel/open", methods=["POST"])
def api_tunnel_open():
    data = request.json
    threading.Thread(target=_open_tunnel_thread, args=(data,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/tunnel/close", methods=["POST"])
def api_tunnel_close():
    data = request.json or {}
    port = data.get("port")
    if port is not None:
        port = str(port)  # Convert to string for consistency
    _close_tunnel(port)
    return jsonify({"ok": True})



@app.route("/api/open-browser", methods=["POST"])
def api_open_browser():
    data = request.json
    port = data.get("port", "8787")
    webbrowser.open(f"http://localhost:{port}")
    return jsonify({"ok": True})


# ─── Job submission logic ────────────────────────────────────────────────────
def _submit_thread(data: dict):
    global ssh_clients

    user = data["hpc_user"].strip()
    gw = data["gateway_host"].strip()
    login_node = data["login_node"].strip()
    script_path = data["script_path"].strip()
    r_version = data["r_version"].strip()
    xdg = data["xdg_data_home"].strip()
    wall = data["wall_time"].strip()
    cpus = data["cpus"].strip()
    mem = data["mem"].strip()
    tmp = data["tmpspace"].strip()

    if not user or not script_path:
        state["status"] = "error"
        state["status_msg"] = "Username and script path are required"
        log("ERROR: Username and script path are required")
        return

    # Store for cleanup operations
    state["_current_user"] = user
    # Extract script directory from script path
    if "/" in script_path:
        state["_script_dir"] = "/".join(script_path.split("/")[:-1])
    else:
        state["_script_dir"] = "~"

    state["status"] = "submitting"
    state["status_msg"] = "Connecting to HPC..."
    log(f"Connecting to {gw} as {user}...")

    try:
        # ── Connect through gateway ──────────────────────────────────────
        gw_client = ssh_connect_with_auth(gw, user)
        if isinstance(gw_client, tuple):
            gw_client = gw_client[0]
        log("Connected to gateway")

        transport = gw_client.get_transport()
        channel = transport.open_channel(
            "direct-tcpip", (login_node, 22), ("127.0.0.1", 0)
        )

        login_client = paramiko.SSHClient()
        login_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        login_client.connect(login_node, username=user, sock=channel)
        log(f"Connected to {login_node}")

        ssh_clients["gw"] = gw_client
        ssh_clients["login"] = login_client

        # ── Patch the script with user settings ──────────────────────────
        log("Patching script with your settings...")
        sftp = login_client.open_sftp()

        with sftp.open(script_path, "r") as f:
            script_content = f.read().decode("utf-8")

        # Patch version
        script_content = re.sub(
            r'^version="[^"]*"',
            f'version="{r_version}"',
            script_content,
            flags=re.MULTILINE,
        )
        # Patch XDG_DATA_HOME (matches "export XDG_DATA_HOME=..." on any line)
        script_content = re.sub(
            r"export XDG_DATA_HOME=.*",
            f"export XDG_DATA_HOME={xdg}",
            script_content,
        )
        # Also patch if using XDG_DATA_HOME_PATH variable (newer template format)
        script_content = re.sub(
            r"^XDG_DATA_HOME_PATH=\"[^\"]*\"",
            f'XDG_DATA_HOME_PATH="{xdg}"',
            script_content,
            flags=re.MULTILINE,
        )

        tmp_script = f"/home/pmc_research/{user}/.rstudio_launcher_tmp.sh"
        with sftp.open(tmp_script, "w") as f:
            f.write(script_content)
        sftp.close()

        login_client.exec_command(f"chmod +x {tmp_script}")
        time.sleep(0.5)

        # ── Submit via sbatch with overrides ─────────────────────────────
        cmd = (
            f"sbatch --time={wall} --cpus-per-task={cpus} "
            f"--mem={mem}G --gres=tmpspace:{tmp}G {tmp_script}"
        )
        log(f"$ {cmd}")

        _, stdout, stderr = login_client.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if err:
            log(f"stderr: {err}")
        log(f"sbatch: {out}")

        m = re.search(r"Submitted batch job (\d+)", out)
        if not m:
            state["status"] = "error"
            state["status_msg"] = "sbatch failed - see log"
            return

        state["job_id"] = m.group(1)
        log(f"Job {state['job_id']} submitted")
        state["status"] = "waiting"
        state["status_msg"] = f"Job {state['job_id']} queued - waiting for start..."

        # ── Poll job status: first check squeue, then wait for credentials ──
        log_path = f"/home/pmc_research/{user}/rstudio-server.job.{state['job_id']}"
        log(f"Waiting for job to start (checking squeue and {log_path})...")

        max_wait = 600  # 10 minutes
        poll_interval = 2  # Check every 2 seconds
        elapsed = 0
        content = ""
        job_running = False

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            # Check if SLURM job is actually running (fast)
            if not job_running:
                try:
                    _, so, _ = login_client.exec_command(
                        f"squeue -j {state['job_id']} -h -o '%T'"
                    )
                    status = so.read().decode().strip()
                    if status in ("RUNNING", "COMPLETING"):
                        job_running = True
                        log(f"Job allocated and running (SLURM status: {status})")
                        state["status"] = "running"
                        state["status_msg"] = f"Job {state['job_id']} running - waiting for credentials..."
                except Exception:
                    pass

            # Poll for credentials in log file
            if job_running or elapsed % 5 == 0:
                try:
                    _, so, _ = login_client.exec_command(f"cat {log_path} 2>/dev/null")
                    content = so.read().decode()
                except Exception:
                    content = ""

                if "password:" in content:
                    log("Found credentials in log file")
                    break

            # Status update every 5 seconds
            if elapsed % 5 == 0 and not job_running:
                state["status_msg"] = f"Job {state['job_id']} - queued ({elapsed}s)..."
            elif job_running and elapsed % 5 == 0:
                state["status_msg"] = f"Job {state['job_id']} - waiting for RStudio ({elapsed}s)..."
        else:
            state["status"] = "error"
            state["status_msg"] = "Timeout waiting for job to start"
            log("ERROR: Timed out waiting for job log file")
            return

        # ── Parse connection info from log ───────────────────────────────
        log("Parsing connection info from job log...")

        m_hp = re.search(r"ssh -N -L 8787:([^:]+):(\d+)", content)
        if not m_hp:
            m_hp = re.search(r"ssh -L 8787:([^:]+):(\d+)", content)
        m_user = re.search(r"user:\s*(\S+)", content)
        m_pass = re.search(r"password:\s*(\S+)", content)

        if m_hp:
            state["remote_hostname"] = m_hp.group(1)
            state["remote_port"] = m_hp.group(2)
        if m_user:
            state["rstudio_user"] = m_user.group(1)
        if m_pass:
            state["rstudio_password"] = m_pass.group(1)

        log(f"Host={state['remote_hostname']}  Port={state['remote_port']}")
        log(f"User={state['rstudio_user']}  Password={state['rstudio_password']}")
        state["status"] = "running"
        state["status_msg"] = f"Job {state['job_id']} running - ready to connect!"

    except Exception as e:
        log(f"ERROR: {e}")
        state["status"] = "error"
        state["status_msg"] = f"Error: {e}"


def _cancel_job():
    if not state["job_id"]:
        return
    try:
        client = ssh_clients.get("login")
        if client:
            job_id = state["job_id"]
            user = state.get("_current_user", "")
            
            # Cancel the job
            client.exec_command(f"scancel -f {job_id}")
            log(f"Sent scancel -f {job_id}")
            log_tunnel(f"Sent scancel -f {job_id}")
            
            # Remove the log file (contains sensitive credentials)
            if user:
                log_file = f"/home/pmc_research/{user}/rstudio-server.job.{job_id}"
                client.exec_command(f"rm -f {log_file}")
                log(f"Removed log file {log_file}")
                log_tunnel(f"Removed log file {log_file}")
            
            # Clear job info from state
            state["job_id"] = None
            state["remote_hostname"] = None
            state["remote_port"] = None
            state["rstudio_user"] = None
            state["rstudio_password"] = None
            state["status"] = "idle"
            state["status_msg"] = f"Job {job_id} cancelled"
    except Exception as e:
        log(f"Cancel failed: {e}")
        log_tunnel(f"Cancel failed: {e}")


def _discover_active_jobs(user: str):
    """Find all active RStudio jobs for the user and populate active_jobs list."""
    gw = state.get("gateway_host", "hpcgw.op.umcutrecht.nl")
    login_node = state.get("login_node", "hpcs06.op.umcutrecht.nl")
    
    try:
        log(f"Discovering active RStudio jobs for {user}...")
        
        # Connect if needed
        if "login" not in ssh_clients or not ssh_clients.get("login"):
            gw_client = ssh_connect_with_auth(gw, user)
            if isinstance(gw_client, tuple):
                gw_client = gw_client[0]
            
            transport = gw_client.get_transport()
            channel = transport.open_channel(
                "direct-tcpip", (login_node, 22), ("127.0.0.1", 0)
            )
            
            login_client = paramiko.SSHClient()
            login_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            login_client.connect(login_node, username=user, sock=channel, timeout=10)
            ssh_clients["gw"] = gw_client
            ssh_clients["login"] = login_client
        
        login_client = ssh_clients["login"]
        
        # Use a single command to find running jobs and extract info in one pass
        cmd = f"""
for f in /home/pmc_research/{user}/rstudio-server.job.*; do
  [ -f "$f" ] || continue
  job_id=${{f##*.}}
  if squeue -j "$job_id" -h -o '%T' 2>/dev/null | grep -q "RUNNING\\|COMPLETING"; then
    echo "---JOB_START:$job_id---"
    cat "$f" 2>/dev/null
    echo "---JOB_END---"
  fi
done
"""
        _, so, _ = login_client.exec_command(cmd)
        output = so.read().decode()
        
        jobs = []
        current_job_id = None
        current_content = []
        
        for line in output.split('\n'):
            if line.startswith('---JOB_START:'):
                current_job_id = line.split(':')[1].rstrip('-')
                current_content = []
            elif line.startswith('---JOB_END---'):
                if current_job_id and current_content:
                    content = '\n'.join(current_content)
                    # Extract connection details
                    m_hp = re.search(r"ssh -N -L 8787:([^:]+):(\d+)", content)
                    if not m_hp:
                        m_hp = re.search(r"ssh -L 8787:([^:]+):(\d+)", content)
                    m_user = re.search(r"user:\s*(\S+)", content)
                    m_pass = re.search(r"password:\s*(\S+)", content)
                    
                    if m_hp and m_user and m_pass:
                        job = {
                            "job_id": current_job_id,
                            "hostname": m_hp.group(1),
                            "port": m_hp.group(2),
                            "rstudio_user": m_user.group(1),
                            "rstudio_password": m_pass.group(1),
                        }
                        jobs.append(job)
                        log(f"Found active job {current_job_id}: {job['hostname']}:{job['port']}")
                current_job_id = None
            elif current_job_id:
                current_content.append(line)
        
        state["active_jobs"] = jobs
        log(f"Found {len(jobs)} active RStudio job(s)")
        
    except Exception as e:
        log(f"Error discovering jobs: {e}")
        state["active_jobs"] = []


# ─── SSH Tunnel ──────────────────────────────────────────────────────────────
def _open_tunnel_thread(data: dict):
    global tunnel_processes

    local_port = data.get("local_port", "8787").strip()
    alias = data.get("ssh_alias", "").strip()
    user = data.get("hpc_user", "").strip()
    gw = data.get("gateway_host", "").strip()
    login = data.get("login_node", "").strip()

    rhost = state.get("remote_hostname")
    rport = state.get("remote_port")
    if not rhost or not rport:
        log_tunnel("ERROR: No remote host/port. Submit a job first.")
        return

    if alias:
        cmd = ["ssh", "-N", "-L", f"{local_port}:{rhost}:{rport}", alias]
    else:
        cmd = [
            "ssh", "-N",
            "-L", f"{local_port}:{rhost}:{rport}",
            "-J", f"{user}@{gw}",
            "-l", user, login,
        ]

    log_tunnel(f"Opening tunnel on port {local_port}: {' '.join(cmd)}")

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(3)
        if process.poll() is not None:
            err = process.stderr.read().decode()
            log_tunnel(f"Tunnel failed: {err}")
            
            # Check for common port binding issues
            if "Address already in use" in err or "cannot listen" in err or "Permission denied" in err:
                state["tunnel_error"] = f"Cannot use port {local_port}. Please try a different port."
                log_tunnel(f"⚠️  Port {local_port} is already in use or permission denied.")
            else:
                state["tunnel_error"] = f"Tunnel failed: {err[:100]}"
            
            return
        state["tunnel_error"] = None
        
        # Track this tunnel process by port
        tunnel_processes[local_port] = process
        
        # Track active tunnel info
        tunnel_info = {
            "local_port": local_port,
            "remote_host": rhost,
            "remote_port": rport,
            "job_id": state.get("job_id")
        }
        if tunnel_info not in state["active_tunnels"]:
            state["active_tunnels"].append(tunnel_info)
            _save_tunnel_info()  # Persist tunnel info
        
        log_tunnel(f"✓ Tunnel active on port {local_port} -> {rhost}:{rport}")
        log(f"✓ Tunnel active on port {local_port} -> {rhost}:{rport}")
    except Exception as e:
        log_tunnel(f"Tunnel error: {e}")
        if "Address already in use" in str(e) or "cannot listen" in str(e):
            state["tunnel_error"] = f"Cannot use port {local_port}. Please try a different port."
            log_tunnel(f"⚠️  Port {local_port} is already in use.")
            log(f"⚠️  Port {local_port} is already in use.")
        else:
            state["tunnel_error"] = f"Tunnel error: {str(e)[:100]}"


def _close_tunnel(port=None):
    global tunnel_processes
    
    def kill_ssh_tunnel_on_port(port_str):
        """Kill SSH tunnel process listening on specific port."""
        try:
            result = subprocess.run(['lsof', '-i', f':{port_str}'], capture_output=True, text=True)
            for line in result.stdout.split('\n'):
                if 'ssh' in line.lower():
                    parts = line.split()
                    if len(parts) > 1:
                        pid = parts[1]
                        try:
                            os.kill(int(pid), 9)
                            log_tunnel(f"✓ Killed SSH tunnel process (PID {pid}) on port {port_str}")
                            log(f"✓ Killed SSH tunnel process (PID {pid}) on port {port_str}")
                            return True
                        except Exception as e:
                            log_tunnel(f"Warning: Could not kill process {pid}: {e}")
        except Exception as e:
            log_tunnel(f"Note: Could not find process on port {port_str}: {e}")
        return False
    
    if port:
        # Close specific tunnel by port
        port = str(port)  # Ensure port is string for dict key consistency
        
        # First try to close if we're tracking it
        if port in tunnel_processes:
            process = tunnel_processes[port]
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            del tunnel_processes[port]
            log_tunnel(f"✓ Closed tunnel on port {port}")
            log(f"✓ Closed tunnel on port {port}")
        
        # Try to kill any orphaned SSH tunnel on this port
        kill_ssh_tunnel_on_port(port)
        
        # Remove from active tunnels
        state["active_tunnels"] = [t for t in state.get("active_tunnels", []) if str(t.get("local_port")) != port]
        _save_tunnel_info()  # Persist updated tunnel info
    else:
        # Close all tunnels
        for port_num in list(tunnel_processes.keys()):
            process = tunnel_processes[port_num]
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            del tunnel_processes[port_num]
            log_tunnel(f"✓ Closed tunnel on port {port_num}")
            log(f"✓ Closed tunnel on port {port_num}")
        
        # Also try to kill any remaining SSH tunnels using pkill
        try:
            # Use pkill to find and kill all ssh processes with -L (port forwarding)
            result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
            ssh_processes = []
            for line in result.stdout.split('\n'):
                if 'ssh' in line and '-L' in line and '-N' in line and 'grep' not in line:
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            pid = int(parts[1])
                            ssh_processes.append((pid, line[:80]))
                        except:
                            pass
            
            # Kill the processes
            for pid, cmd_preview in ssh_processes:
                try:
                    os.kill(pid, 9)
                    log_tunnel(f"✓ Killed SSH tunnel process (PID {pid})")
                    log(f"✓ Killed SSH tunnel process (PID {pid})")
                except Exception as e:
                    log_tunnel(f"Note: Could not kill process {pid}: {e}")
        except Exception as e:
            log_tunnel(f"Note: Error scanning for SSH processes: {e}")
        
        state["active_tunnels"] = []
        _save_tunnel_info()  # Persist updated tunnel info
    
    log_tunnel("Tunnels closed")
    log("Tunnels closed")


# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = 5050
    
    # Initialize tunnel detection
    _initialize_tunnels()

    # Try native window via pywebview, fall back to browser
    try:
        import webview

        def _start_server():
            app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

        server_thread = threading.Thread(target=_start_server, daemon=True)
        server_thread.start()

        # Give Flask a moment to start
        time.sleep(0.5)

        print(f"\n  RStudio HPC Launcher (native window)\n")
        webview.create_window(
            "RStudio HPC Launcher",
            f"http://localhost:{port}",
            width=960,
            height=820,
            min_size=(700, 500),
        )
        webview.start()

    except ImportError:
        # pywebview not installed — fall back to browser mode
        print(f"\n  RStudio HPC Launcher running at: http://localhost:{port}\n")
        print("  (Install 'pywebview' for a native app window: pip install pywebview)\n")

        def _open_browser():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}")

        threading.Thread(target=_open_browser, daemon=True).start()
        app.run(host="127.0.0.1", port=port, debug=False)
