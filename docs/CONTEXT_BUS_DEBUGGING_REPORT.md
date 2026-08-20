# Context Bus Debugging & Post-Mortem Report

This document outlines the root causes and fixes for the chain of issues affecting the Context Bus, the Local Dashboard, and the VM Admin Dashboard. These issues primarily prevented users from successfully approving drafts and viewing them in the UI.

## 1. The `401 Unauthorized` Draft Approval Error
**Symptom:** Running `ESDS_APPROVE` in the agent CLI resulted in a `401 Unauthorized` rejection from the Context Bus backend on the VM, even after the `api_tokens.json` file was correctly SCP'd to the VM.
**Root Cause:** 
- The `docker-compose.yml` file mounted the local config folder to `/app/config/api_tokens.json` inside the container. 
- However, the Python backend (`store/backend/app/auth.py`) resolved its `BASE_DIR` as `/` due to how the Docker image was flattened during the `COPY . .` step in the Dockerfile.
- As a result, the backend was looking for the tokens at the absolute path `/config/api_tokens.json` (which didn't exist) and silently fell back to a stale built-in token mapping.
**Fix:** 
- Updated `store/.env` to explicitly include `API_TOKENS_FILE=/app/config/api_tokens.json`.
- Restarted the VM container using `docker-compose up -d` to inject the new environment variable, forcing it to read the correct mounted file.

## 2. Local Dashboard: Array Reversal & Missing Mock Data
**Symptom:** Once approvals succeeded, the Local Gateway Dashboard (`http://localhost:5173`) did not clearly display the new passports. Existing historical mock passports vanished entirely.
**Root Cause:** 
- `dashboard/src/App.jsx` was initializing its state using `useState([])` instead of `useState(PASSPORTS)`, instantly wiping out all historical mock data upon load.
- The 3-second polling interval in `fetchDrafts` was iterating over `prevPassports` and using `unique.unshift(p)`. This had the unintended side effect of completely reversing the order of the array every 3 seconds, and permanently burying newly approved drafts at the very bottom of the screen.
**Fix:**
- Initialized state with `useState(PASSPORTS)`.
- Swapped the `unshift()` method for `push()` and reordered the loops in `App.jsx` to ensure newly approved drafts appear stably at the top of the UI.

## 3. Admin Dashboard: Missing Records on Port 3001
**Symptom:** The Admin Dashboard served on the VM at port 3001 showed an empty table ("No context passports found"), despite drafts being successfully approved and ingested into the Postgres database.
**Root Cause:**
This was a multi-layered issue bridging frontend configuration and backend query design:
1. **Hardcoded Token:** The Admin Dashboard source code (`admin-dashboard/src/App.jsx`) was hardcoded to authenticate using `Authorization: Bearer admin-token-demo`. This token did not exist in `api_tokens.json`, causing the `/v1/agent-activity` endpoint to reject the polling requests with a `401`.
2. **Endpoint Mismatch:** The Admin Dashboard frontend was built to query the `/v1/agent-activity` endpoint. However, this endpoint was originally designed as a "Latest Activity Per Agent" view, and its SQL query explicitly filtered out any records where `agent_id IS NULL`.
3. **Gateway Payload Bug:** The Gateway proxy on the local laptop (`gateway/flows.py`) completely omitted the `agent_id` field when constructing the payload for `bus.ingest()`. Consequently, the Postgres database saved the user's drafts with `agent_id = NULL`, rendering them invisible to the Admin Dashboard's strict query.
4. **VM Hosting:** The Admin Dashboard is a statically built Vite app hosted directly from the VM (`/root/context_bus/admin-dashboard/dist`). Modifying the React code locally had no effect until the `dist` folder was compiled and SCP'd to the VM.

**Fix:**
- **Frontend Token:** Edited `admin-dashboard/src/App.jsx` to use the valid token `token-220834002a083aa0`.
- **Gateway Submission:** Modified `gateway/flows.py` to explicitly inject `agent_id="claude-code"` into `write_policy.build_ingest_payload()`, ensuring all future drafts meet the backend's strict visibility criteria.
- **VM Deployment:** Ran `npm run build` locally in `admin-dashboard/`, then SCP'd the compiled `dist/` directory directly to the VM, restarting the python `http.server`.
- **Database Backfill:** Ran a manual SQL patch on the VM's Postgres container (`UPDATE knowledge_entries SET agent_id = 'claude-code' WHERE agent_id IS NULL;`) to retroactively fix the missing agent IDs on the user's prior drafts, making them instantly visible in the UI.
