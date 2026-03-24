---
name: FFIS Development
description: "Use when: Working on FFIS codebase including flat_file_scrubber.py, config.py, snowflake_connector.py, or FFIS infrastructure (Docker, deployment, security). Applies to development and deployment tasks."
applyTo: 
  - flat_file_scrubber.py
  - config.py
  - snowflake_connector.py
  - Dockerfile
  - docker-compose.yml
  - '**/.github/**'
---

# FFIS Development Instructions

## Codebase Overview

### Core Modules
- **`flat_file_scrubber.py`** (900+ lines) – Streamlit UI with 11 workflow tabs
- **`config.py`** – Configuration cascade loader (env vars → .env → secrets.json → defaults)
- **`snowflake_connector.py`** – Optional Snowflake integration helper
- **`requirements.txt`** – Python dependencies (streamlit, pandas, numpy, requests)

### Configuration Files
- **`.env.example`** – Template for application settings
- **`secrets.json.example`** – Template for sensitive credentials
- **`.dockerenv.example`** – Template for Docker environment

### Deployment
- **`Dockerfile`** – Multi-stage production build
- **`docker-compose.yml`** – Local dev orchestration
- **`build.sh`** / **`build.bat`** – Build scripts
- **`run.sh`** / **`run.bat`** – Launch scripts

### Documentation
- **`README.md`** – User guide and quick start
- **`SECURITY.md`** – Secrets management and protection
- **`DEPLOYMENT.md`** – Cloud deployment options (GCP, AWS, K8s)
- **`DOCKER_QUICK_START.md`** – Docker quick reference

## Development Workflow

### Local Development
```bash
# Activate virtual environment
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Run the app
run.bat                        # Windows
./run.sh                       # macOS/Linux
python -m streamlit run flat_file_scrubber.py  # Universal
```

### Testing Configuration
```python
# Test config loaded correctly
from config import get_streamlit_config, get_api_config
cfg = get_streamlit_config()
print(cfg)
```

### Docker Development
```bash
# Build image
docker build -t ffis:latest .

# Run locally with compose
docker-compose up -d

# Stop
docker-compose down
```

## Code Standards

### Python Style
- Follow PEP 8 (4-space indentation)
- Type hints recommended for functions
- Docstrings for all classes and functions
- Use f-strings for formatting

### Configuration Access
Always use the `config.py` helpers, never hardcode values:
```python
# ✅ Good
from config import get_streamlit_config, get_api_config
cfg = get_streamlit_config()

# ❌ Bad
import os
title = os.getenv("STREAMLIT_PAGE_TITLE", "default")
```

### Secrets Handling
- Never log secrets
- Always use environment variables or `secrets.json`
- Test with `setup_secrets.py`
- Document required secrets in `.env.example`

## Common Tasks

### Add a New Configuration Option
1. Add to `.env.example` with documentation
2. Add to `.dockerenv.example` if Docker-relevant
3. Create getter function in `config.py`:
   ```python
   def get_my_new_setting() -> str:
       return getenv_str("MY_NEW_SETTING", "default_value")
   ```
4. Use in code via: `from config import get_my_new_setting`

### Add a New Streamlit Tab
1. Import required modules at top of `flat_file_scrubber.py`
2. Create tab function with `with st.tabs()[n]:`
3. Document in README.md section "Step X — [Name]"

### Deploy to Cloud
- See [DEPLOYMENT.md](../../DEPLOYMENT.md) for GCP Cloud Run, AWS ECS, Kubernetes
- Use Docker image: reference [DOCKER_QUICK_START.md](../../DOCKER_QUICK_START.md)
- Configure secrets: reference [SECURITY.md](../../SECURITY.md)

### Fix a Security Issue
1. Review [SECURITY.md](../../SECURITY.md)
2. Check `.gitignore` entries for sensitive files
3. Use `setup_secrets.py` to test credential handling
4. Update documentation

## Important Files to Keep in Sync
- **`README.md`** – User-facing documentation
- **`.env.example`** – Shows all configuration options
- **`secrets.json.example`** – Shows credential schema
- **`requirements.txt`** – Python dependencies
- **`SECURITY.md`** – Secret management practices

## Testing

### Configuration Testing
```python
from pathlib import Path
from config import get_streamlit_config

# Verify config loads
cfg = get_streamlit_config()
assert cfg["page_title"] == "Flat File Scrubber"
assert cfg["layout"] == "wide"
```

### Docker Build Testing
```bash
docker build -t ffis:test .
docker run -p 8501:8501 ffis:test
# Visit http://localhost:8501
```

### Secret Testing (without committing)
```bash
python setup_secrets.py
# Interactive setup process
```

## Debugging

### App Won't Start
```bash
# Check Python version
python --version  # Should be 3.9+

# Check imports
python -c "import streamlit; import pandas; import numpy"

# Check config
python -c "from config import get_streamlit_config; print(get_streamlit_config())"
```

### Docker Issues
```bash
# Check image size
docker images ffis

# Check build logs
docker build --progress=plain -t ffis:latest .

# Debug container
docker run -it ffis:latest bash
```

### Configuration Not Loading
```bash
# Check .env file
cat .env

# Check environment variables
env | grep STREAMLIT

# Check secrets.json
cat secrets.json | python -m json.tool
```

## Git Workflow

### Before Committing
```bash
# Never commit secrets
git status | grep "secrets.json\|\.env"  # Should be empty

# Verify .gitignore is working
git check-ignore -v .env secrets.json

# Test build before pushing
docker build -t ffis:test .
```

### Commit Messages
Use conventional commits:
```
feat: Add new export format
fix: Resolve config loading issue
docs: Update deployment guide
chore: Update dependencies
```

## Resources

- [Python 3.9+ docs](https://docs.python.org/3/)
- [Streamlit docs](https://docs.streamlit.io/)
- [Pandas docs](https://pandas.pydata.org/)
- [Docker docs](https://docs.docker.com/)
- [Snowflake Python Connector](https://docs.snowflake.com/en/developer-guide/python-connector/)
