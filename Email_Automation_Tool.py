"""
Email Automation Tool (Sign in with Google)
=====================================
A simple app that sends an email at a date and time you choose - once, on a
repeat schedule (daily / weekly / monthly), or to a whole list of people at
once from a CSV file.
You sign in with your Google account (one click) - no passwords to type.

Run it with:   python email_scheduler.py
Your browser opens automatically. Keep this program running so it can send
your emails at the right time.

Install the libraries it needs (once):
    pip install flask google-api-python-client google-auth-oauthlib

Features:
  - Schedules, templates, attachments and your Google sign-in are all saved
    to disk (scheduler.db, token.json, attachments/) so nothing is lost
    when you restart the program.
  - Recurring emails: daily / weekly / monthly.
  - Edit or cancel a pending email, or send it immediately.
  - CC / BCC fields, and file attachments.
  - Reusable templates (save a subject + body once, reuse later).
  - Bulk send / mail-merge: upload a CSV of recipients and send a
    personalized email to everyone on the list, spaced out over time.
  - The schedule list updates live in the browser - no manual refresh.
"""

import base64
import calendar
import csv
import io
import mimetypes
import os
import re
import socket
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from email.message import EmailMessage

from flask import Flask, render_template_string, request, redirect, jsonify

# Allow the local (http://localhost) sign-in redirect and relax scope order.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # Gmail's own attachment ceiling

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "scheduler.db")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")
UPLOAD_DIR = os.path.join(BASE_DIR, "attachments")

# ---- Google sign-in settings ----------------------------------------------
# This is a Google "Desktop" OAuth client (project zomato-486209). For desktop
# apps Google does not treat the client secret as confidential, so it can live
# in the code. Each person who signs in must be added as a "test user" in the
# Google Cloud console until the app is verified by Google.
CLIENT_CONFIG = {
    "installed": {
        "client_id": "106582235137-lmqliq1t5qu0e1q45q1953i60ufg8dd0.apps.googleusercontent.com",
        "project_id": "zomato-486209",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "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",
        "redirect_uris": ["http://localhost"],
    }
}
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

# ---- App state --------------------------------------------------------
google_creds = None      # the signed-in Google credentials
google_email = None      # the signed-in email address
lock = threading.Lock()

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


# =========================================================================
# Storage (SQLite - a single local file, no server needed)
# =========================================================================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                to_addr TEXT NOT NULL,
                cc TEXT DEFAULT '',
                bcc TEXT DEFAULT '',
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                send_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                recurrence TEXT NOT NULL DEFAULT 'none'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL
            )
        """)


def next_occurrence(dt, recurrence):
    """Return the next send time for a repeating email."""
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(days=7)
    if recurrence == "monthly":
        month = dt.month + 1
        year = dt.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        return dt.replace(year=year, month=month, day=min(dt.day, last_day))
    return None


def safe_filename(name):
    return name.replace("/", "_").replace("\\", "_").strip() or "file"


def save_uploaded_files(files, email_id):
    """Save uploaded Werkzeug FileStorage objects to disk and record them."""
    folder = os.path.join(UPLOAD_DIR, str(email_id))
    os.makedirs(folder, exist_ok=True)
    with get_db() as conn:
        for f in files:
            if not f or not f.filename:
                continue
            fname = safe_filename(f.filename)
            path = os.path.join(folder, fname)
            f.save(path)
            conn.execute("INSERT INTO attachments (email_id, filename, filepath) VALUES (?, ?, ?)",
                         (email_id, fname, path))


def save_shared_attachment_bytes(attachments_bytes, email_id):
    """Write the same in-memory attachment bytes (used for bulk sends) to a new email's folder."""
    if not attachments_bytes:
        return
    folder = os.path.join(UPLOAD_DIR, str(email_id))
    os.makedirs(folder, exist_ok=True)
    with get_db() as conn:
        for fname, data in attachments_bytes:
            path = os.path.join(folder, fname)
            with open(path, "wb") as out:
                out.write(data)
            conn.execute("INSERT INTO attachments (email_id, filename, filepath) VALUES (?, ?, ?)",
                         (email_id, fname, path))


def fill_placeholders(template, row):
    """Replace {{column}} in a template with values from a CSV row (case-insensitive)."""
    lower_row = {(k or "").strip().lower(): v for k, v in row.items()}

    def repl(m):
        return lower_row.get(m.group(1).strip().lower(), "") or ""

    return PLACEHOLDER_RE.sub(repl, template)


# =========================================================================
# Google sign-in
# =========================================================================
def save_token(creds):
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())


def load_saved_signin():
    """Try to restore a previous Google sign-in when the app starts."""
    global google_creds, google_email
    if not os.path.exists(TOKEN_FILE):
        return
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if creds and creds.valid:
            google_creds = creds
            info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
            google_email = info.get("email")
    except Exception:
        pass  # if this fails, the user just signs in again from the page


# =========================================================================
# Page template
# =========================================================================
PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email Automation Tool</title>
<style>
  :root {
    --accent: #4f46e5; --accent-dark: #4338ca; --bg: #f6f7fb; --card: #ffffff;
    --border: #e5e7eb; --text: #1f2330; --muted: #6b7280;
    --amber-bg: #fff7e6; --amber-fg: #b45309;
    --green-bg: #e9f9ee; --green-fg: #0f7a3d;
    --red-bg: #fdecec; --red-fg: #c0271f;
    --gray-bg: #eef0f4; --gray-fg: #556;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; background: var(--bg);
         color: var(--text); max-width: 900px; margin: 0 auto; padding: 24px 16px 60px; }
  h1 { font-size: 22px; margin: 4px 0 2px; display:flex; align-items:center; gap:8px; }
  .sub { color: var(--muted); font-size: 13px; margin-bottom: 22px; }
  h2 { font-size: 15px; margin: 28px 0 10px; color: #333; display:flex; align-items:center; gap:6px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px;
          padding: 20px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  label { display: block; font-size: 12.5px; margin: 12px 0 4px; font-weight: 600; color: #374151; }
  input, textarea, select { width: 100%; padding: 9px 10px; border: 1px solid var(--border);
                    border-radius: 8px; font-size: 14px; background: #fbfbfe; }
  input:focus, textarea:focus, select:focus { outline: 2px solid var(--accent); background: #fff; }
  textarea { height: 90px; resize: vertical; }
  input[type=file] { background: #fff; padding: 7px; }
  code { background: #f0f0f7; padding: 1px 5px; border-radius: 4px; font-size: 12.5px; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row > div { flex: 1; min-width: 120px; }
  button, .gbtn { margin-top: 16px; background: var(--accent); color: #fff; border: none;
           padding: 10px 18px; border-radius: 8px; font-size: 14px; cursor: pointer;
           text-decoration: none; display: inline-block; font-weight: 600; }
  button:hover, .gbtn:hover { background: var(--accent-dark); }
  button.secondary { background: #fff; color: var(--text); border: 1px solid var(--border); }
  button.secondary:hover { background: #f3f4f6; }
  button.danger { background: #fff; color: var(--red-fg); border: 1px solid #f3c9c6; }
  button.danger:hover { background: var(--red-bg); }
  button.small { padding: 5px 10px; font-size: 12.5px; margin: 0; }
  .presets { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
  .presets button { margin: 0; }
  .signed { display: flex; align-items: center; justify-content: space-between;
            background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 18px; }
  .signed .who { font-size: 14px; } .signed .who b { color: var(--green-fg); }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; background: var(--card);
          border-radius: 10px; overflow: hidden; border: 1px solid var(--border); }
  th, td { padding: 10px; font-size: 13px; text-align: left; border-bottom: 1px solid var(--border); }
  th { background: #f9fafb; color: #444; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
  tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .badge.Pending { background: var(--amber-bg); color: var(--amber-fg); }
  .badge.Sent { background: var(--green-bg); color: var(--green-fg); }
  .badge.Cancelled { background: var(--gray-bg); color: var(--gray-fg); }
  .badge.Failed { background: var(--red-bg); color: var(--red-fg); }
  .countdown { font-size: 11.5px; color: var(--muted); margin-top: 2px; }
  .clip { font-size: 11.5px; color: var(--muted); }
  .msg { color: var(--green-fg); font-size: 14px; background: var(--green-bg); padding: 8px 12px; border-radius: 8px; }
  .err { background: var(--red-bg); border: 1px solid #f5b5ae; color: var(--red-fg);
         padding: 10px 12px; border-radius: 8px; font-size: 14px; }
  .hint { font-size: 12px; color: var(--muted); }
  a { color: var(--accent); }
  .tpl-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .tpl-chip { background: #f3f4f6; border: 1px solid var(--border); border-radius: 999px;
              padding: 5px 12px; font-size: 12.5px; cursor: pointer; display:flex; align-items:center; gap:6px; }
  .tpl-chip:hover { background: #eceeff; }
  .tpl-chip form { margin: 0; }
  .tpl-chip .x { color: var(--red-fg); font-weight: 700; cursor: pointer; background:none; border:none; padding:0; margin:0; font-size:13px; }
  .att-chip { background: #f3f4f6; border: 1px solid var(--border); border-radius: 999px;
              padding: 5px 12px; font-size: 12px; display:flex; align-items:center; gap:6px; cursor: default; }
  .att-chip label { margin: 0; font-weight: 400; display:flex; align-items:center; gap:4px; }
  .search { width: 260px; }
  .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; }
  .actions { display: flex; gap: 6px; flex-wrap: wrap; }
  #toast { position: fixed; bottom: 20px; right: 20px; background: #1f2330; color: #fff;
           padding: 10px 16px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .3s; pointer-events:none; }
  #toast.show { opacity: 1; }
</style>
</head>
<body>
  <h1>✉️ Email Automation Tool</h1>
  <div class="sub">Sign in, write an email (or a whole mail-merge campaign), pick a time - it sends itself.</div>

  {% if message %}<p class="msg">{{ message }}</p>{% endif %}
  {% if error %}<p class="err">{{ error }}</p>{% endif %}

  {% if not email %}
    <div class="card">
      <p>To send emails, sign in with the Google account you want to send from.
         You will pick your account and click <b>Allow</b> - no password is typed here.</p>
      <a class="gbtn" href="/connect">Sign in with Google</a>
      <p class="hint" style="margin-top:14px">A Google tab will open. After you allow access,
         come back to this page. You'll stay signed in the next time you run this program.</p>
    </div>
  {% else %}
    <div class="signed">
      <span class="who">Signed in as <b>{{ email }}</b></span>
      <form method="post" action="/disconnect" style="margin:0">
        <button style="margin:0;background:#eee;color:#333">Sign out</button>
      </form>
    </div>

    <h2>📝 Templates</h2>
    <div class="card">
      {% if templates %}
      <div class="tpl-list">
        {% for t in templates %}
          <span class="tpl-chip" onclick="useTemplate('{{ t.subject|e }}', {{ t.body|tojson }})">
            {{ t.name }}
            <form method="post" action="/template/delete/{{ t.id }}" onsubmit="event.stopPropagation()">
              <button class="x" title="Delete template" onclick="event.stopPropagation()">✕</button>
            </form>
          </span>
        {% endfor %}
      </div>
      {% else %}
        <p class="hint">No templates yet - fill the form below and click "Save as template".</p>
      {% endif %}
    </div>

    <h2>{{ '✏️ Edit scheduled email' if editing else '📅 Schedule an email' }}</h2>
    <div class="card">
      <form method="post" action="/schedule" id="mainForm" enctype="multipart/form-data">
        <input type="hidden" name="id" value="{{ editing.id if editing else '' }}">
        <label>Send to</label>
        <input name="to" type="email" placeholder="friend@example.com" value="{{ editing.to_addr if editing else '' }}" required>
        <div class="grid2">
          <div>
            <label>CC (optional)</label>
            <input name="cc" type="text" placeholder="cc@example.com" value="{{ editing.cc if editing else '' }}">
          </div>
          <div>
            <label>BCC (optional)</label>
            <input name="bcc" type="text" placeholder="bcc@example.com" value="{{ editing.bcc if editing else '' }}">
          </div>
        </div>
        <label>Subject</label>
        <input name="subject" id="subjectInput" value="{{ editing.subject if editing else '' }}" required>
        <label>Message</label>
        <textarea name="body" id="bodyInput" required>{{ editing.body if editing else '' }}</textarea>

        <div class="row">
          <div><label>Date</label><input name="date" id="dateInput" type="date" value="{{ editing.date if editing else '' }}" required></div>
          <div><label>Time</label><input name="time" id="timeInput" type="time" value="{{ editing.time if editing else '' }}" required></div>
          <div><label>Repeat</label>
            <select name="recurrence">
              <option value="none" {{ 'selected' if editing and editing.recurrence=='none' else '' }}>Doesn't repeat</option>
              <option value="daily" {{ 'selected' if editing and editing.recurrence=='daily' else '' }}>Daily</option>
              <option value="weekly" {{ 'selected' if editing and editing.recurrence=='weekly' else '' }}>Weekly</option>
              <option value="monthly" {{ 'selected' if editing and editing.recurrence=='monthly' else '' }}>Monthly</option>
            </select>
          </div>
        </div>

        <div class="presets">
          <button type="button" class="secondary small" onclick="preset(1)">In 1 hour</button>
          <button type="button" class="secondary small" onclick="presetTomorrow(9,0)">Tomorrow 9:00 AM</button>
          <button type="button" class="secondary small" onclick="presetNextMonday()">Next Monday 9:00 AM</button>
        </div>

        <label>Attach files (optional)</label>
        <input type="file" name="attachments" multiple>
        {% if editing and editing.attachments %}
          <div class="tpl-list">
            {% for a in editing.attachments %}
              <span class="att-chip">📎 {{ a.filename }}
                <label><input type="checkbox" name="remove_attachment" value="{{ a.id }}"> remove</label>
              </span>
            {% endfor %}
          </div>
        {% endif %}

        <div class="actions">
          <button type="submit">{{ 'Update Email' if editing else 'Schedule Email' }}</button>
          {% if editing %}<a class="gbtn secondary" style="background:#fff;color:#333;border:1px solid #ddd" href="/">Cancel edit</a>{% endif %}
          <button type="button" class="secondary" onclick="saveTemplate()">Save as template</button>
        </div>
      </form>
    </div>

    <h2>📤 Bulk send from a CSV list (mail-merge)</h2>
    <div class="card">
      <form method="post" action="/bulk_schedule" enctype="multipart/form-data">
        <label>CSV file</label>
        <input type="file" name="csv_file" accept=".csv" required>
        <p class="hint">The file needs a column named <code>email</code> (or <code>to</code>). Any other
           column, e.g. <code>name</code>, becomes a placeholder you can use below as <code>{{ '{{name}}' }}</code>.</p>

        <div class="grid2">
          <div><label>CC (optional, all emails)</label><input name="cc" type="text"></div>
          <div><label>BCC (optional, all emails)</label><input name="bcc" type="text"></div>
        </div>

        <label>Subject template</label>
        <input name="subject_template" placeholder="Hi {{ '{{name}}' }}, quick update" required>
        <label>Message template</label>
        <textarea name="body_template" placeholder="Hello {{ '{{name}}' }}, ..." required></textarea>

        <div class="row">
          <div><label>Start date</label><input name="date" type="date" required></div>
          <div><label>Start time</label><input name="time" type="time" required></div>
          <div><label>Minutes between each send</label><input name="stagger" type="number" min="0" value="1"></div>
        </div>
        <p class="hint">Spacing sends out a bit helps avoid Gmail flagging a burst of identical-looking mail.</p>

        <label>Attach files to every email (optional)</label>
        <input type="file" name="attachments" multiple>

        <button type="submit">Schedule bulk send</button>
      </form>
    </div>

    <div class="toolbar">
      <h2 style="margin-bottom:0">📬 Scheduled emails</h2>
      <input class="search" id="searchBox" placeholder="Search by recipient or subject..." oninput="filterRows()">
    </div>
    <table id="emailTable">
      <thead>
        <tr><th>To</th><th>Subject</th><th>Send at</th><th>Repeat</th><th>Status</th><th></th></tr>
      </thead>
      <tbody id="emailBody">
        {% for e in emails %}
          <tr data-search="{{ (e.to_addr ~ ' ' ~ e.subject)|lower }}" data-id="{{ e.id }}">
            <td>{{ e.to_addr }}</td>
            <td>{{ e.subject }}{% if e.attachments %}<div class="clip">📎 {{ e.attachments|length }} file{{ 's' if e.attachments|length != 1 else '' }}</div>{% endif %}</td>
            <td>{{ e.send_at_display }}<div class="countdown" data-send="{{ e.send_at }}" data-status="{{ e.status }}"></div></td>
            <td>{{ e.recurrence if e.recurrence != 'none' else '-' }}</td>
            <td><span class="badge {{ e.status.split(':')[0] }}">{{ e.status }}</span></td>
            <td class="actions">
              {% if e.status == 'Pending' %}
                <form method="post" action="/send_now/{{ e.id }}" style="display:inline"><button class="secondary small">Send now</button></form>
                <a class="gbtn secondary small" style="background:#fff;color:#333;border:1px solid #ddd" href="/?edit={{ e.id }}">Edit</a>
                <form method="post" action="/cancel/{{ e.id }}" style="display:inline"><button class="danger small">Cancel</button></form>
              {% endif %}
            </td>
          </tr>
        {% else %}
          <tr><td colspan="6">Nothing scheduled yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
    <p class="hint">This list updates on its own every few seconds while this page is open.</p>
  {% endif %}

<div id="toast"></div>
<script>
function pad(n) { return n.toString().padStart(2, "0"); }

function setDateTime(d) {
  document.getElementById("dateInput").value = d.getFullYear() + "-" + pad(d.getMonth()+1) + "-" + pad(d.getDate());
  document.getElementById("timeInput").value = pad(d.getHours()) + ":" + pad(d.getMinutes());
}
function preset(hoursFromNow) {
  var d = new Date(Date.now() + hoursFromNow * 3600 * 1000);
  setDateTime(d);
}
function presetTomorrow(h, m) {
  var d = new Date(); d.setDate(d.getDate() + 1); d.setHours(h, m, 0, 0);
  setDateTime(d);
}
function presetNextMonday() {
  var d = new Date();
  var add = (8 - d.getDay()) % 7 || 7;
  d.setDate(d.getDate() + add); d.setHours(9, 0, 0, 0);
  setDateTime(d);
}
function useTemplate(subject, body) {
  document.getElementById("subjectInput").value = subject;
  document.getElementById("bodyInput").value = body;
  showToast("Template loaded into the form");
}
function saveTemplate() {
  var subject = document.getElementById("subjectInput").value;
  var body = document.getElementById("bodyInput").value;
  if (!subject || !body) { showToast("Write a subject and message first"); return; }
  var name = prompt("Name this template:", subject.slice(0, 30));
  if (!name) return;
  var f = document.createElement("form");
  f.method = "post"; f.action = "/template/save";
  ["name","subject","body"].forEach(function(k, i){
    var v = [name, subject, body][i];
    var inp = document.createElement("input"); inp.type="hidden"; inp.name=k; inp.value=v;
    f.appendChild(inp);
  });
  document.body.appendChild(f); f.submit();
}
function showToast(text) {
  var t = document.getElementById("toast");
  t.textContent = text; t.classList.add("show");
  setTimeout(function(){ t.classList.remove("show"); }, 2500);
}
function filterRows() {
  var q = document.getElementById("searchBox").value.toLowerCase();
  document.querySelectorAll("#emailBody tr[data-search]").forEach(function(row){
    row.style.display = row.getAttribute("data-search").indexOf(q) === -1 ? "none" : "";
  });
}
function tick() {
  document.querySelectorAll(".countdown").forEach(function(el){
    if (el.getAttribute("data-status") !== "Pending") { el.textContent = ""; return; }
    var target = new Date(el.getAttribute("data-send"));
    var diff = target - new Date();
    if (diff <= 0) { el.textContent = "due now"; return; }
    var mins = Math.floor(diff / 60000);
    var days = Math.floor(mins / 1440); mins -= days * 1440;
    var hrs = Math.floor(mins / 60); mins -= hrs * 60;
    var parts = [];
    if (days) parts.push(days + "d");
    if (hrs) parts.push(hrs + "h");
    parts.push(mins + "m");
    el.textContent = "in " + parts.join(" ");
  });
}
setInterval(tick, 1000); tick();

// Poll the server every 5s and refresh statuses without a full page reload.
function poll() {
  fetch("/api/emails").then(r => r.json()).then(data => {
    document.querySelectorAll("#emailBody tr").forEach(function(row){
      var id = row.getAttribute("data-id");
      if (!id) return;
      var match = data.find(e => String(e.id) === id);
      if (match) {
        var badge = row.querySelector(".badge");
        if (badge) { badge.textContent = match.status; badge.className = "badge " + match.status.split(":")[0]; }
        var cd = row.querySelector(".countdown");
        if (cd) cd.setAttribute("data-status", match.status);
      }
    });
  }).catch(function(){});
}
setInterval(poll, 5000);
{% if message %}showToast({{ message|tojson }});{% endif %}
</script>
</body>
</html>
"""


# =========================================================================
# Routes
# =========================================================================
@app.route("/")
def home():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM emails ORDER BY send_at ASC").fetchall()
        templates = conn.execute("SELECT * FROM templates ORDER BY id DESC").fetchall()

        emails_view = []
        for r in rows:
            d = dict(r)
            dt = datetime.fromisoformat(d["send_at"])
            d["send_at_display"] = dt.strftime("%d %b %Y, %I:%M %p")
            d["attachments"] = [dict(a) for a in conn.execute(
                "SELECT * FROM attachments WHERE email_id=?", (d["id"],)).fetchall()]
            emails_view.append(d)

        editing = None
        edit_id = request.args.get("edit")
        if edit_id:
            row = conn.execute("SELECT * FROM emails WHERE id=? AND status='Pending'", (edit_id,)).fetchone()
            if row:
                editing = dict(row)
                dt = datetime.fromisoformat(editing["send_at"])
                editing["date"] = dt.strftime("%Y-%m-%d")
                editing["time"] = dt.strftime("%H:%M")
                editing["attachments"] = [dict(a) for a in conn.execute(
                    "SELECT * FROM attachments WHERE email_id=?", (edit_id,)).fetchall()]

    return render_template_string(
        PAGE, emails=emails_view, templates=templates, editing=editing,
        email=google_email, message=request.args.get("msg"), error=None,
    )


@app.route("/connect")
def connect():
    """Open Google sign-in, then remember the signed-in account."""
    global google_creds, google_email
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
        creds = flow.run_local_server(
            port=0, access_type="offline", prompt="consent",
            success_message="Signed in! You can close this tab and go back to the app.",
        )
        google_creds = creds
        save_token(creds)
        info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
        google_email = info.get("email")
        return redirect("/?msg=Signed in! You can schedule emails now.")
    except Exception as err:
        return render_template_string(
            PAGE, emails=[], templates=[], editing=None, email=None, message=None,
            error="Google sign-in failed or was cancelled. "
                  "Make sure your email was added as a test user. Details: " + str(err)), 400


@app.route("/disconnect", methods=["POST"])
def disconnect():
    global google_creds, google_email
    google_creds = None
    google_email = None
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    return redirect("/?msg=Signed out.")


@app.route("/schedule", methods=["POST"])
def schedule():
    if not google_email:
        return redirect("/")
    f = request.form
    send_at = datetime.strptime(f["date"] + " " + f["time"], "%Y-%m-%d %H:%M").isoformat()
    record = (f["to"], f.get("cc", ""), f.get("bcc", ""), f["subject"], f["body"],
              send_at, f.get("recurrence", "none"))

    edit_id = f.get("id")
    with get_db() as conn:
        if edit_id:
            conn.execute(
                "UPDATE emails SET to_addr=?, cc=?, bcc=?, subject=?, body=?, send_at=?, recurrence=?, status='Pending' WHERE id=?",
                record + (edit_id,),
            )
            eid = int(edit_id)
            for att_id in request.form.getlist("remove_attachment"):
                row = conn.execute("SELECT * FROM attachments WHERE id=? AND email_id=?", (att_id, eid)).fetchone()
                if row:
                    try:
                        os.remove(row["filepath"])
                    except OSError:
                        pass
                    conn.execute("DELETE FROM attachments WHERE id=?", (att_id,))
            msg = "Email updated."
        else:
            cur = conn.execute(
                "INSERT INTO emails (to_addr, cc, bcc, subject, body, send_at, recurrence, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')",
                record,
            )
            eid = cur.lastrowid
            msg = "Email scheduled! Keep this program running."

    files = [uf for uf in request.files.getlist("attachments") if uf and uf.filename]
    if files:
        save_uploaded_files(files, eid)

    return redirect("/?msg=" + msg)


@app.route("/bulk_schedule", methods=["POST"])
def bulk_schedule():
    if not google_email:
        return redirect("/")
    f = request.form
    csv_file = request.files.get("csv_file")
    if not csv_file or not csv_file.filename:
        return redirect("/?msg=Please choose a CSV file.")

    text = csv_file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return redirect("/?msg=That CSV file had no rows.")

    email_col = None
    for col in reader.fieldnames or []:
        if col and col.strip().lower() in ("email", "to", "e-mail"):
            email_col = col
            break
    if not email_col:
        return redirect("/?msg=Your CSV needs a column named 'email' (or 'to').")

    base_dt = datetime.strptime(f["date"] + " " + f["time"], "%Y-%m-%d %H:%M")
    stagger = int(f.get("stagger") or 0)
    subject_template = f["subject_template"]
    body_template = f["body_template"]
    cc, bcc = f.get("cc", ""), f.get("bcc", "")

    shared_attachments = [(safe_filename(uf.filename), uf.read())
                           for uf in request.files.getlist("attachments") if uf and uf.filename]

    MAX_ROWS = 500
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    scheduled, skipped, i = 0, 0, 0
    with get_db() as conn:
        for row in rows:
            to_addr = (row.get(email_col) or "").strip()
            if not to_addr or "@" not in to_addr:
                skipped += 1
                continue
            subject = fill_placeholders(subject_template, row)
            body = fill_placeholders(body_template, row)
            send_at = (base_dt + timedelta(minutes=stagger * i)).isoformat()
            cur = conn.execute(
                "INSERT INTO emails (to_addr, cc, bcc, subject, body, send_at, recurrence, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'none', 'Pending')",
                (to_addr, cc, bcc, subject, body, send_at),
            )
            save_shared_attachment_bytes(shared_attachments, cur.lastrowid)
            scheduled += 1
            i += 1

    msg = f"Bulk send ready: {scheduled} emails scheduled"
    if skipped:
        msg += f", {skipped} rows skipped (missing/invalid email)"
    if truncated:
        msg += f", only the first {MAX_ROWS} rows were used"
    return redirect("/?msg=" + msg + ".")


@app.route("/cancel/<int:email_id>", methods=["POST"])
def cancel(email_id):
    with get_db() as conn:
        conn.execute("UPDATE emails SET status='Cancelled' WHERE id=? AND status='Pending'", (email_id,))
    return redirect("/?msg=Email cancelled.")


@app.route("/send_now/<int:email_id>", methods=["POST"])
def send_now(email_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM emails WHERE id=? AND status='Pending'", (email_id,)).fetchone()
    if row:
        _send_and_update(dict(row))
    return redirect("/?msg=Send attempted - check the status column.")


@app.route("/template/save", methods=["POST"])
def template_save():
    f = request.form
    with get_db() as conn:
        conn.execute("INSERT INTO templates (name, subject, body) VALUES (?, ?, ?)",
                     (f["name"], f["subject"], f["body"]))
    return redirect("/?msg=Template saved.")


@app.route("/template/delete/<int:template_id>", methods=["POST"])
def template_delete(template_id):
    with get_db() as conn:
        conn.execute("DELETE FROM templates WHERE id=?", (template_id,))
    return redirect("/?msg=Template deleted.")


@app.route("/api/emails")
def api_emails():
    with get_db() as conn:
        rows = conn.execute("SELECT id, status FROM emails").fetchall()
    return jsonify([dict(r) for r in rows])


# =========================================================================
# Sending
# =========================================================================
def _attach_files(message, email_id):
    with get_db() as conn:
        atts = conn.execute("SELECT * FROM attachments WHERE email_id=?", (email_id,)).fetchall()
    for a in atts:
        path = a["filepath"]
        if not os.path.exists(path):
            continue
        ctype, encoding = mimetypes.guess_type(path)
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with open(path, "rb") as fh:
            data = fh.read()
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=a["filename"])


def _send_and_update(e):
    """Send one email and update its row in the database."""
    try:
        if not google_creds:
            raise RuntimeError("Not signed in to Google.")
        _send_email(e)
        recurrence = e.get("recurrence", "none")
        if recurrence and recurrence != "none":
            nxt = next_occurrence(datetime.fromisoformat(e["send_at"]), recurrence)
            with get_db() as conn:
                conn.execute("UPDATE emails SET send_at=?, status='Pending' WHERE id=?",
                             (nxt.isoformat(), e["id"]))
        else:
            with get_db() as conn:
                conn.execute("UPDATE emails SET status='Sent' WHERE id=?", (e["id"],))
    except Exception as err:
        with get_db() as conn:
            conn.execute("UPDATE emails SET status=? WHERE id=?", ("Failed: " + str(err), e["id"]))


def _send_email(e):
    """Send one email through the Gmail API using the signed-in account."""
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if google_creds and not google_creds.valid and google_creds.expired and google_creds.refresh_token:
        google_creds.refresh(Request())
        save_token(google_creds)

    message = EmailMessage()
    message["To"] = e["to_addr"]
    if e.get("cc"):
        message["Cc"] = e["cc"]
    if e.get("bcc"):
        message["Bcc"] = e["bcc"]
    message["Subject"] = e["subject"]
    message.set_content(e["body"])
    _attach_files(message, e["id"])
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=google_creds, cache_discovery=False)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def process_due():
    """Send every email whose chosen time has arrived."""
    now = datetime.now().isoformat()
    with lock:
        with get_db() as conn:
            due = conn.execute(
                "SELECT * FROM emails WHERE status='Pending' AND send_at<=?", (now,)
            ).fetchall()
        for row in due:
            _send_and_update(dict(row))


def background_worker():
    while True:
        process_due()
        time.sleep(30)


def find_free_port(start=5000):
    """Return the first free port at or after `start`, in case 5000 is busy."""
    for port in range(start, start + 25):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test:
            if test.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


if __name__ == "__main__":
    init_db()
    load_saved_signin()
    port = find_free_port(5000)
    url = "http://127.0.0.1:" + str(port)
    threading.Thread(target=background_worker, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print("Email Scheduler is running. Open " + url)
    # threaded=True so the sign-in step doesn't freeze the rest of the app.
    app.run(host="127.0.0.1", port=port, threaded=True)
