# n8n + Twilio WhatsApp (very simple setup)

Workflow file:
- [n8n/twilio_whatsapp_router.workflow.json](n8n/twilio_whatsapp_router.workflow.json)

## Fastest way (no env variables needed)

### Step 1: Import workflow
1. Open n8n.
2. Click **Import from file**.
3. Select [n8n/twilio_whatsapp_router.workflow.json](n8n/twilio_whatsapp_router.workflow.json).

### Step 2: Paste Twilio credentials in n8n UI
1. Open node **Normalize Input**.
2. In assignments, find these 3 fields:
	- `twilio_account_sid`
	- `twilio_auth_token`
	- `twilio_whatsapp_number`
3. Replace their values with your real Twilio values.

Use this format:
- `twilio_account_sid`: `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
- `twilio_auth_token`: `your_auth_token`
- `twilio_whatsapp_number`: `whatsapp:+14155238886`

### Step 3: Set Twilio webhook URL
In Twilio WhatsApp Sandbox (or sender) settings:
- **When a message comes in**: `POST`
- URL: `https://<your-n8n-domain>/webhook/twilio-whatsapp-in`

### Step 4: Activate workflow
Click **Activate** in n8n.

### Step 5: Test
Send `HELP` on WhatsApp.
If setup is correct, you get a reply.

---

## Where to edit commands
Open node **Command Router (edit here)** and edit `jsCode`.

Current supported commands:
- `REGISTER <user_id> <password>`
- `LOGIN <user_id> <password>`
- `CHECK` / `REFRESH`
- `SETYEAR <YYYY-YY>`
- `SETSEM <N>`
- `WHOAMI`
- `LOGOUT`
- `HELP`

---

## Optional secure method (env variables)
If you prefer, keep credentials in server env instead of node values:
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_WHATSAPP_NUMBER`

---

## If user is not getting reply
Check in this order:
1. Workflow is **Active**
2. Twilio webhook URL is exactly correct
3. User joined Twilio WhatsApp Sandbox
4. Twilio credentials are correct in **Normalize Input**
5. n8n execution logs show no error in **Send WhatsApp via Twilio API**
