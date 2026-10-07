# USSD CRM Reminder Platform

A Python CRM that answers USSD signup callbacks, tracks incomplete customer registrations, and automatically sends consent-based email reminders on days 7, 30, and 60.

## What it does

- Responds to a USSD signup flow and stores a customer's phone, full name, email, and reminder preference.
- Creates a CRM record on the first USSD contact, then keeps the original signup date as the reminder schedule anchor.
- Schedules personalized `Dear {full name},` emails for 7, 30, and 60 days after signup starts.
- Sends email only after the customer gives explicit consent and while the signup remains incomplete.
- Stops future reminders when staff mark a signup complete or the customer unsubscribes.
- Retries failed deliveries with backoff and prevents duplicate milestones with a unique customer/milestone key.
- Provides a password-protected CRM dashboard with customer status, consent, reminders, search, and completion actions.
- Offers a console preview mode for local development and SMTP delivery for real email.

The UI is server-rendered with Flask/Jinja templates; both the frontend routes and backend workflow are Python. The USSD endpoint accepts a cumulative-text callback payload (`sessionId`, `serviceCode`, `phoneNumber`, `text`) and returns `CON` or `END` text. Connect your USSD aggregator to `POST /webhooks/ussd`; map its request fields to this contract if needed.

## Run locally

Use Python 3.11 or newer.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python run.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). The local dashboard defaults to username `admin` and password `ChangeThisLocalPassword123!`; change these values in `.env` before using real customer data. SQLite is created in the ignored `instance/` directory. The scheduler checks for due reminders every 60 seconds.

On macOS or Linux, activate the environment with `source .venv/bin/activate`; the remaining commands are the same. For a single-process WSGI server, install Waitress with `pip install waitress` and run `waitress-serve --listen=127.0.0.1:5000 run:app`. Keep one application process so the in-process scheduler does not run more than once. If the web tier needs multiple processes, set `START_SCHEDULER=false` for every web process and run exactly one separate scheduler with `python -m reminder_platform.worker`.

## Configure real reminder email

The sample environment uses `EMAIL_BACKEND=console`. This safely records each reminder as a preview and does not send an email. To deliver email, configure `.env` with your SMTP provider:

```dotenv
EMAIL_BACKEND=smtp
MAIL_FROM=Your Sign-up Team <no-reply@yourdomain.example>
SMTP_HOST=smtp.your-provider.example
SMTP_PORT=587
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_USE_TLS=true
PUBLIC_BASE_URL=https://your-public-domain.example
SUPPORT_EMAIL=support@yourdomain.example
USSD_SHORT_CODE=*123#
```

Restart the app after changing settings. Never commit `.env` or use the local sample credentials in a public deployment. Delivery history identifies console previews separately from emails sent through SMTP. Console previews are not queued for later SMTP delivery; configure SMTP before processing real customer records.

## Connect the USSD provider

Expose the app behind HTTPS and configure the provider to POST its session callbacks to `/webhooks/ussd`. The expected JSON/form fields are:

```json
{
  "sessionId": "session-123",
  "serviceCode": "*123#",
  "phoneNumber": "+254700000000",
  "text": "1*Samira Njeri*samira@example.com*1"
}
```

The `text` field is cumulative. The customer flow asks whether to continue, collects a full name and email, then asks whether email reminders are wanted. The phone number must contain 7 to 15 digits; international numbers should include `+` or `00` so local country formats are preserved without guessing a country code. When configured, the app checks the `X-Webhook-Token` header against `USSD_WEBHOOK_TOKEN`. Leave this empty only for local demos; production startup refuses to run without it.

For a local callback smoke test, send a POST to `http://127.0.0.1:5000/webhooks/ussd` with the sample JSON. To advance the menu, call again with the same `sessionId`/`phoneNumber` and cumulative `text` values: `1`, `1*Samira Njeri`, `1*Samira Njeri*samira@example.com`, and finally `1*Samira Njeri*samira@example.com*1`. Each callback returns the next `CON` prompt or an `END` confirmation.

## Run tests

```powershell
py -3 -m unittest discover -v
```

Tests exercise the USSD conversation, email consent, reminder timing and deduplication, completion suppression, and unsubscribe behavior without sending real email.

## Before production

- Set a unique high-entropy `FLASK_SECRET_KEY`, a strong admin password, and `USSD_WEBHOOK_TOKEN`.
- Use HTTPS, restrict the webhook at the USSD provider or ingress, and configure SMTP sender authentication.
- Replace SQLite with a managed database and run the scheduler as a single dedicated worker before scaling the web tier beyond one process.
- Add provider-specific signature validation and monitoring for your aggregator and mail provider.
- Configure retention, access policy, backups, and customer privacy notices for your operating jurisdiction.
