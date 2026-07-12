# Project--Email-Automation-Tool
It schedules the Email Automatically according to your predefined date and time


# Email Scheduler (Sign in with Google)

Write an email now, pick a date and time, and it sends automatically at that
time from your Gmail. You **sign in with Google** (one click) — no passwords to
type, no App Password, no 2‑Step Verification needed.

## 1. Install Python
Install Python 3.10 or newer from https://python.org
(On Windows, tick **"Add Python to PATH"** during installation.)

## 2. Install the libraries (once)
Open a terminal **in this folder** and run:

    pip install flask google-api-python-client google-auth-oauthlib

(On Mac/Linux use `pip3`.)

## 3. Run it

    python email_scheduler.py

(On Mac/Linux: `python3 email_scheduler.py`)

Your browser opens automatically.

## 4. Sign in and schedule
1. Click **Sign in with Google**.
2. A Google tab opens — pick your account and click **Allow**.
3. Back in the app, fill in **To, Subject, Message**, pick a **date & time**,
   and click **Schedule Email**. Keep the program running.

> **"Google hasn't verified this app" screen?** That's normal while the app is
> in testing. Click **Advanced → Go to Email Scheduler (unsafe)** to continue.
> (It's your own app — it's safe.)

> **"Access blocked / you're not a test user"?** Your Gmail must be added to the
> app's test‑user list first — ask whoever shared this app to add you (see below).

---

## For the person sharing this app (one‑time setup)
The Google sign‑in uses a Google Cloud project (`zomato-486209`). Until Google
formally verifies it, **only accounts you add as "test users" can sign in.**
To let a friend sign in:

1. Go to https://console.cloud.google.com → project **zomato-486209**.
2. **APIs & Services → OAuth consent screen.**
3. Make sure **User type = External**.
4. Under **Test users → Add users**, add your own Gmail and each friend's Gmail
   (up to 100).

That's it — added testers can now sign in and send. (For unlimited public use,
the app would need Google's full verification, which is a separate, longer
process.)

## Good to know
- Your computer must be **on** and this program **running** at the send time.
- The scheduled list is kept in memory, so closing the program clears it.
- A personal **@gmail.com** works best. A school/work (Workspace) account may be
  blocked by its admin from signing in to outside apps.
- Files: `email_scheduler.py` and this `README.md`. That's it.
