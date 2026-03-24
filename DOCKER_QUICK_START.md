# 🐳 Docker Setup Quick Reference

Your Flat File Scrubber project is now ready for containerization and Snowflake deployment!

## What Was Created

```
FFIS/
├── Dockerfile                 # Container definition with multi-stage build
├── docker-compose.yml         # Local development and deployment orchestration
├── .dockerignore              # Excludes unnecessary files from Docker build
├── .dockerenv.example         # Environment variables template
├── snowflake_connector.py     # Optional Snowflake integration helper
├── build.sh                   # Build script for macOS/Linux
├── build.bat                  # Build script for Windows
├── DEPLOYMENT.md              # Comprehensive deployment guide (4 cloud options)
└── .gitignore                 # Updated with Docker entries
```

## Quick Start (Local Testing)

### 1️⃣ Build the Image
```bash
# On Windows
build.bat

# On macOS/Linux
chmod +x build.sh
./build.sh
```

### 2️⃣ Configure Environment
```bash
cp .dockerenv.example .env
# Edit .env with your settings (nano, VS Code, etc.)
```

### 3️⃣ Run Locally
```bash
docker-compose up -d
```

### 4️⃣ Access Application
- 🌐 **URL:** http://localhost:8501
- 📋 **View Logs:** `docker-compose logs -f`
- 🛑 **Stop:** `docker-compose down`

---

## Snowflake Integration

### Option A: Environment Variables
```bash
export SNOWFLAKE_ACCOUNT=xy12345.us-east-1
export SNOWFLAKE_USER=your_user
export SNOWFLAKE_PASSWORD=your_password
export SNOWFLAKE_DATABASE=your_db
export SNOWFLAKE_SCHEMA=your_schema
export SNOWFLAKE_WAREHOUSE=your_warehouse

docker-compose up -d
```

### Option B: Secrets File
Place `secrets.json` in project root:
```json
{
  "snowflake": {
    "account": "xy12345.us-east-1",
    "user": "your_user",
    "password": "your_password",
    "database": "your_db",
    "schema": "your_schema",
    "warehouse": "your_warehouse"
  }
}
```

Then use the optional snowflake_connector helper in your code:
```python
from snowflake_connector import connect_snowflake, export_to_snowflake

# Test connection
conn = connect_snowflake()

# Export a DataFrame to Snowflake
export_to_snowflake(df, "my_table_name")
```

**⚠️ IMPORTANT:** Never commit `secrets.json` or `.env` to git!

---

## Cloud Deployment

### Google Cloud Run
```bash
gcloud builds submit --tag gcr.io/PROJECT_ID/flat-file-scrubber:latest
gcloud run deploy flat-file-scrubber \
  --image gcr.io/PROJECT_ID/flat-file-scrubber:latest \
  --port 8501 \
  --memory 2Gi \
  --set-env-vars SNOWFLAKE_ACCOUNT=...,SNOWFLAKE_USER=...
```

### AWS ECS
```bash
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com

docker build -t flat-file-scrubber:latest .
docker tag flat-file-scrubber:latest 123456789.dkr.ecr.us-east-1.amazonaws.com/flat-file-scrubber:latest
docker push 123456789.dkr.ecr.us-east-1.amazonaws.com/flat-file-scrubber:latest
```

### Kubernetes
```bash
kubectl apply -f deployment.yaml
# (see DEPLOYMENT.md for full deployment.yaml)
```

---

## Common Commands

```bash
# View logs
docker-compose logs -f flat-file-scrubber

# Rebuild image (after code changes)
docker-compose up -d --build

# Stop and remove containers
docker-compose down

# Clean everything (containers, volumes, images)
docker-compose down -v
docker rmi flat-file-scrubber:latest

# Test image manually
docker run -p 8501:8501 flat-file-scrubber:latest

# Check image size
docker images flat-file-scrubber:latest
```

---

## Security Best Practices

✅ **Do This:**
- Use environment variables or Docker secrets for credentials
- Scan images: `docker scan flat-file-scrubber:latest`
- Run behind reverse proxy (nginx) with TLS/SSL
- Keep dependencies updated: `pip install --upgrade -r requirements.txt`

❌ **Don't Do This:**
- Don't commit `.env` or `secrets.json` to git
- Don't run with `--privileged` flag
- Don't hardcode credentials in Dockerfile
- Don't use generic passwords

---

## Troubleshooting

**Container won't start?**
```bash
docker-compose logs flat-file-scrubber
```

**Port 8501 already in use?**
```bash
# Change in docker-compose.yml or kill other container:
docker rm $(docker ps -q)
```

**Out of memory?**
Edit `docker-compose.yml` and increase `memory: 4G`

**Permission errors on mounted files?**
```bash
chmod 644 secrets.json .env
```

---

## Next Steps

1. ✅ Run locally with `docker-compose up -d`
2. ✅ Test Snowflake connection (if configured)
3. ✅ Push image to your registry (GCR, ECR, Docker Hub, etc.)
4. ✅ Deploy to production cloud platform
5. ✅ Monitor logs and set up alerts

**For full deployment guide:** See [DEPLOYMENT.md](DEPLOYMENT.md)

