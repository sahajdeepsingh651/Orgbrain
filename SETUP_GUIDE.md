# Data Passport: Complete Setup Guide

This guide covers how to set up both the **Interceptor Gateway** (which runs on the developer's laptop) and the **Context Bus** (which represents the shared VM).

## Prerequisites
* Python 3.10 or 3.11+
* Docker and Docker Compose
* Claude Code installed globally (`npm install -g @anthropic-ai/claude-code`)

---

## 1. Install Dependencies

First, we need to set up isolated Python environments for both components.

### Setup the Gateway (Interceptor)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Setup the Context Bus (Shared VM)
The Context Bus is designed to run entirely via Docker, which will automatically spin up the database, run the migrations, and start the backend API server.

```bash
cd store
cp .env.example .env
docker compose up --build -d
cd ..
```
*(This starts both `data-passport-postgres` and `data-passport-backend` on port 8000).*

---

## 2. Configure Your Identity

We will use the automated setup script to capture your Anthropic account UUID and link it to a secure bus token.

1. In your first terminal (at the project root), run:
   ```bash
   source .venv/bin/activate
   python scripts/setup_identity.py
   ```
2. Open a **second terminal** and run this to send your UUID to the script:
   ```bash
   ANTHROPIC_BASE_URL=http://localhost:8081 claude
   ```
3. Go back to the **first terminal**. The script will capture your UUID and prompt you for your username, department, and team.
   *(This automatically creates and populates `store/config/account_map.json` and `store/config/api_tokens.json`)*.

---

## 3. Run the Interceptor Gateway

Since the Shared VM (Context Bus) is already running quietly in the background via Docker, you only need to start your local Gateway.

**Start the Gateway (Interceptor)**
```bash
# From the project root
source .venv/bin/activate
python -m uvicorn gateway.app:app --port 8080
```
*(Keep this running in your terminal)*

---

## 5. Start a Secure Claude Session

Now that both the Interceptor (`:8080`) and the Context Bus (`:8000`) are running, you can use Claude normally, but routed through your local Interceptor.

Open a **third terminal** and run:
```bash
ANTHROPIC_BASE_URL=http://localhost:8080 claude
```

**⚠️ Important:** Never run `export ANTHROPIC_BASE_URL=...` in your shell profile. Always prefix it to the specific command so only your AI agent traffic is intercepted.

You are now ready to interact with Claude! The Gateway will intercept your prompts, enforce Data Leak Prevention (DLP), and securely store your approved sessions in the Context Bus.
