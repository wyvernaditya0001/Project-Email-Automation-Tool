"""
Email Automation Tool(Sign in with Google)
=====================================
A simple app that sends an email at a date and time you choose.
You sign in with your Google account (one click) - no passwords to type.

Run it with:   python email_scheduler.py
Your browser opens automatically. Keep this program running so it can send
your emails at the right time.

Install the libraries it needs (once):
    pip install flask google-api-python-client google-auth-oauthlib
"""

import base64
import os
import socket
import threading
import time
import webbrowser
from datetime import datetime
from email.message import EmailMessage

from flask import Flask, render_template_string, request, redirect

# Allow the local (http://localhost) sign-in redirect and relax scope order.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

app = Flask(__name__)

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

# ---- App state (kept in memory - no database) ------------------------------
google_creds = None      # the signed-in Google credentials
google_email = None      # the signed-in email address
emails = []              # the list of scheduled emails
lock = threading.Lock()


PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Email Automation Tool</title>
  <style>
    body { font-family: Arial, sans-serif; background: #f4f5f7; color: #222;
           max-width: 640px; margin: 30px auto; padding: 0 16px; }
    h1 { font-size: 22px; }  h2 { font-size: 17px; margin-top: 26px; }
    .card { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 18px; }
    label { display: block; font-size: 13px; margin: 10px 0 4px; font-weight: bold; }
    input, textarea { width: 100%; padding: 8px; border: 1px solid #ccc;
                      border-radius: 6px; font-size: 14px; box-sizing: border-box; }
    textarea { height: 90px; }
    .row { display: flex; gap: 12px; } .row > div { flex: 1; }
    button, .gbtn { margin-top: 14px; background: #2563eb; color: #fff; border: none;
             padding: 10px 18px; border-radius: 6px; font-size: 15px; cursor: pointer;
             text-decoration: none; display: inline-block; }
    .signed { display: flex; align-items: center; justify-content: space-between;
              background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px 16px; }
    .signed .who { font-size: 14px; } .signed .who b { color: #137333; }
    .link { color: #2563eb; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; background: #fff; }
    th, td { border: 1px solid #ddd; padding: 8px; font-size: 13px; text-align: left; }
    th { background: #f0f0f0; }
    .msg { color: #137333; font-size: 14px; }
    .err { background: #fce8e6; border: 1px solid #f5b5ae; color: #b00020;
           padding: 10px 12px; border-radius: 6px; font-size: 14px; }
    .hint { font-size: 12px; color: #666; }
    a { color: #2563eb; }
  </style>
</head>
<body>
  <h1>Email Automation Tool</h1>
  {% if message %}<p class="msg">{{ message }}</p>{% endif %}
  {% if error %}<p class="err">{{ error }}</p>{% endif %}

  {% if not email %}
    <div class="card">
      <p>To send emails, sign in with the Google account you want to send from.
         You will pick your account and click <b>Allow</b> - no password is typed here.</p>
      <a class="gbtn" href="/connect">Sign in with Google</a>
      <p class="hint" style="margin-top:14px">A Google tab will open. After you allow access,
         come back to this page.</p>
    </div>
  {% else %}
    <div class="signed">
      <span class="who">Signed in as <b>{{ email }}</b></span>
      <form method="post" action="/disconnect" style="margin:0">
        <button style="margin:0;background:#eee;color:#333">Sign out</button>
      </form>
    </div>

    <div class="card" style="margin-top:16px">
      <form method="post" action="/schedule">
        <label>Send to</label>
        <input name="to" type="email" placeholder="friend@example.com" required>
        <label>Subject</label>
        <input name="subject" required>
        <label>Message</label>
        <textarea name="body" required></textarea>
        <div class="row">
          <div><label>Date</label><input name="date" type="date" required></div>
          <div><label>Time</label><input name="time" type="time" required></div>
        </div>
        <button type="submit">Schedule Email</button>
      </form>
    </div>

    <h2>Scheduled emails</h2>
    <table>
      <tr><th>To</th><th>Subject</th><th>Send at</th><th>Status</th></tr>
      {% for e in emails %}
        <tr><td>{{ e.to }}</td><td>{{ e.subject }}</td>
            <td>{{ e.send_at.strftime('%d %b %Y, %I:%M %p') }}</td>
            <td>{{ e.status }}</td></tr>
      {% else %}
        <tr><td colspan="4">Nothing scheduled yet.</td></tr>
      {% endfor %}
    </table>
    <p class="hint">Refresh this page to see statuses change from Pending to Sent.</p>
  {% endif %}
</body>
</html>
"""


@app.route("/")
def home():
    with lock:
        rows = list(emails)
    return render_template_string(PAGE, emails=rows, email=google_email,
                                  message=request.args.get("msg"), error=None)


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
        info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
        google_email = info.get("email")
        return redirect("/?msg=Signed in! You can schedule emails now.")
    except Exception as err:
        return render_template_string(
            PAGE, emails=[], email=None, message=None,
            error="Google sign-in failed or was cancelled. "
                  "Make sure your email was added as a test user. Details: " + str(err)), 400


@app.route("/disconnect", methods=["POST"])
def disconnect():
    global google_creds, google_email
    google_creds = None
    google_email = None
    return redirect("/?msg=Signed out.")


@app.route("/schedule", methods=["POST"])
def schedule():
    if not google_email:
        return redirect("/")
    f = request.form
    send_at = datetime.strptime(f["date"] + " " + f["time"], "%Y-%m-%d %H:%M")
    with lock:
        emails.append({"to": f["to"], "subject": f["subject"], "body": f["body"],
                       "send_at": send_at, "status": "Pending"})
    return redirect("/?msg=Email scheduled! Keep this program running.")


def send_email(e):
    """Send one email through the Gmail API using the signed-in account."""
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if google_creds and not google_creds.valid and google_creds.expired and google_creds.refresh_token:
        google_creds.refresh(Request())

    message = EmailMessage()
    message["To"] = e["to"]
    message["Subject"] = e["subject"]
    message.set_content(e["body"])
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=google_creds, cache_discovery=False)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def process_due():
    """Send every email whose chosen time has arrived."""
    now = datetime.now()
    with lock:
        due = [e for e in emails if e["status"] == "Pending" and e["send_at"] <= now]
    for e in due:
        try:
            if not google_creds:
                raise RuntimeError("Not signed in to Google.")
            send_email(e)
            e["status"] = "Sent"
        except Exception as err:
            e["status"] = "Failed: " + str(err)


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
    port = find_free_port(5000)
    url = "http://127.0.0.1:" + str(port)
    threading.Thread(target=background_worker, daemon=True).start()
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print("Email Scheduler is running. Open " + url)
    # threaded=True so the sign-in step doesn't freeze the rest of the app.
    app.run(host="127.0.0.1", port=port, threaded=True)
