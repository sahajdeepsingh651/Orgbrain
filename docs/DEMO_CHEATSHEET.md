# Orgbrain Live Demo Cheat Sheet

This guide is designed to be kept open on a side monitor while you present. It contains exactly what you need to run to spin everything up and the "script" of what to type into Claude to show off the system.

---

## 1. Starting the Infrastructure
You will need 4 terminal tabs running in the background, all starting from the `~/Projects/hackathon_agent_layer` folder:

**Terminal 1: The Database (Postgres)**
```bash
docker-compose -f store/docker-compose.yml up -d
```

**Terminal 2: The Context Bus Backend**
```bash
cd store/backend
source .venv/bin/activate
uvicorn app.main:app --port 8000 --env-file ../.env
```

**Terminal 3: The Data Passport Gateway**
```bash
source store/backend/.venv/bin/activate
uvicorn gateway.app:app --port 8080
```

**Terminal 4: The Admin Dashboard UI**
```bash
cd dashboard
npm run dev
```

---

## 2. Starting the Agent
In your primary terminal that the audience will see, start Claude Code, pointing it at the Gateway:
```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```
*(Make sure your `ANTHROPIC_API_KEY` is exported in this terminal as usual!)*

---

## 3. The Live Demo Script

Have the **Admin Dashboard UI** (`http://localhost:5173`) open next to your terminal so the audience can see the X-Ray monitor react.

### Scenario 1: Proving Data Loss Prevention (PII & Secret Redaction)
**Pitch:** *"Let's see what happens if our AI reads sensitive developer data or PII. The developer won't even know it's being protected."*

**Example A: Secrets & PAN Card**
> I am testing a string parsing function. Can you extract the PAN card number ABCDE1234F and the test secret sk-test-1234567890ABCDEF into a JSON object?

**Example B: Phone & Email**
> I'm debugging some customer logs. Can you verify if the phone number +91-9876543210 and the email address john.doe@example.com are formatted correctly?

**Example C: Names & Credit Cards**
> My name is Sahaj Singh. Please check if this credit card number is valid: 4532-1234-5678-9012.

**What to point out on the Dashboard (X-Ray Monitor):**
Point at the split screen. Show the audience how the left side contained the real PII and secrets, but the right side (what Anthropic actually received) replaced them with secure tokens like `⟦PII_1⟧`, `⟦PII_2⟧`, and `⟦SECRET_1⟧`. 

---

### Scenario 2: Capturing Knowledge (The WRITE Flow)
**Pitch:** *"Now the developer has solved a tough problem. We want to share this knowledge across the enterprise, but securely."*

**Example A: Backend Fix**
1. **Type into Claude:** 
   > I just figured out how to fix the authentication issue. We need to use the new CORSMiddleware in the FastAPI app. ESDS_SUBMIT
2. **Type into Claude:** 
   > ESDS_APPROVE <ID> *(Replace <ID> with the 8-character ID Claude gave you)*

**Example B: Database Optimization**
1. **Type into Claude:** 
   > For the new analytics dashboard, we decided to use a Redis cache instead of querying Postgres directly to save on latency. ESDS_SUBMIT
2. **Type into Claude:** 
   > ESDS_APPROVE <ID>

**Example C: UI Decisions**
1. **Type into Claude:** 
   > I finished the research on the frontend. We are choosing Vite + React over Next.js because we don't need server-side rendering. ESDS_SUBMIT
2. **Type into Claude:** 
   > ESDS_APPROVE <ID>

**What to point out on the Dashboard:**
* **During `ESDS_SUBMIT`:** Switch to the **Approval Inbox** tab. Show that a new draft has appeared, but it is waiting for a human manager to approve it before it enters the enterprise bus.
* **During `ESDS_APPROVE`:** Switch to the **Context Bus Explorer** tab. Show the audience that the draft has now been converted into a permanent "Data Passport" and is stored in the central enterprise database.

---

### Scenario 3: Enterprise Awareness (The READ Flow)
**Pitch:** *"Now imagine a different developer on a different team runs into the same problem. Watch how Orgbrain injects the previously approved enterprise context directly into their prompt."*

*(Note: Try these sequentially after doing the corresponding submissions in Scenario 2!)*

**Example A: Backend Fix**
> How do I fix the authentication issue? ESDS_SEARCH authentication

**Example B: Database Optimization**
> How should we fetch the data for the new analytics dashboard? ESDS_SEARCH analytics dashboard

**Example C: UI Decisions**
> Which framework did we decide to use for the frontend? ESDS_SEARCH frontend framework

**What to point out:**
1. **In Claude:** Claude will give the exact answer you provided during the WRITE flow, even if you are in a brand new session!
2. **In the Dashboard (X-Ray Monitor):** Look at the right side of the split screen. You will see the Orgbrain Context Bus silently injecting the enterprise knowledge into the agent's prompt, highlighted in **bright blue**!
