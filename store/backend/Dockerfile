FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Run the migrations and then start the uvicorn server
# Note: In production, migrations should ideally be run as a separate step or init container,
# but for this setup, wrapping it in a simple startup script is easiest.
CMD ["sh", "-c", "python db/migrate.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
