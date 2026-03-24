---
name: FFIS Skill
description: "Use when: Automating Flat File Scrubber operations, batch processing data, configuring integrations, or deploying to production. Includes workflows for common FFIS tasks."
slashCommand: ffis
---

# FFIS Skill: Workflows & Operations

This skill provides reusable workflows for common Flat File Scrubber operations.

## Available Workflows

### 1. Setup & Configuration
**Task:** Set up FFIS for the first time

**Steps:**
1. Verify Python 3.9+ is installed
2. Create virtual environment: `python -m venv .venv`
3. Activate: `.venv\Scripts\activate` (Windows) or `source .venv/bin/activate` (macOS/Linux)
4. Install dependencies: `pip install -r requirements.txt`
5. Configure secrets: `python setup_secrets.py` (interactive)
6. Test launch: `run.bat` or `./run.sh`

**Success Criteria:** Application opens at http://localhost:8501

---

### 2. Quick Data Cleaning
**Task:** Clean a single CSV file and export

**Steps:**
1. Launch: `python -m streamlit run flat_file_scrubber.py`
2. Upload CSV in "Ingest" tab
3. Review preview in "Inspect" tab
4. Use transform tabs to:
   - Drop unnecessary columns
   - Handle nulls/blanks
   - Clean special characters
   - Validate required fields
5. Download cleaned CSV from "Export" tab

**Configuration Needed:**
- None for local CSV → CSV workflow
- Salesforce API endpoint if exporting to SF
- Snowflake credentials if exporting to Snowflake

---

### 3. Batch Processing
**Task:** Automate cleaning of multiple CSV files

**Python Script:**
```python
import pandas as pd
from pathlib import Path

# Define data and output directories
data_dir = Path("data/raw")
output_dir = Path("data/cleaned")

# Process each CSV
for csv_file in data_dir.glob("*.csv"):
    df = pd.read_csv(csv_file)
    
    # Apply transformations (customize as needed)
    df = df.dropna(subset=["required_field"])
    df = df.drop_duplicates()
    df["field_name"] = df["field_name"].str.upper().str.strip()
    
    # Save cleaned version
    output_file = output_dir / csv_file.name
    df.to_csv(output_file, index=False)
    print(f"✓ Cleaned {csv_file.name}")
```

**Deploy:** Run on schedule with cron (macOS/Linux) or Task Scheduler (Windows)

---

### 4. Salesforce Integration
**Task:** Export cleaned data to Salesforce

**Configuration:**
```bash
# In .env or environment
SF_API_URL=https://your-instance.salesforce.com/services/data/v57.0
SF_API_KEY=your_oauth_token_here
```

**Use FFIS UI:**
1. Clean your data using the workflow
2. Go to "Export" tab
3. Select "REST API Post"
4. Configure batch size (typically 200 records)
5. Click "Export"

---

### 5. Snowflake Integration
**Task:** Load cleaned data to Snowflake

**Requirements:**
```bash
pip install snowflake-connector-python sqlalchemy
```

**Configuration:**
```bash
# Set environment variables
export SNOWFLAKE_ACCOUNT=xy12345.us-east-1
export SNOWFLAKE_USER=your_user
export SNOWFLAKE_PASSWORD=your_password
export SNOWFLAKE_DATABASE=your_db
export SNOWFLAKE_SCHEMA=your_schema
export SNOWFLAKE_WAREHOUSE=your_warehouse
```

**Python Code:**
```python
from snowflake_connector import export_to_snowflake
import pandas as pd

df = pd.read_csv("cleaned_data.csv")
success = export_to_snowflake(df, "target_table_name")
print("✓ Data loaded to Snowflake" if success else "✗ Load failed")
```

---

### 6. Docker Deployment
**Task:** Deploy FFIS for production use

**Steps:**
1. Build image: `docker build -t ffis:latest .` or `build.bat`
2. Configure environment: `cp .dockerenv.example .env`
3. Edit `.env` with your settings
4. Start: `docker-compose up -d`
5. Access: http://localhost:8501

**For Cloud Deployment:** See [DEPLOYMENT.md](../../DEPLOYMENT.md)

---

### 7. Secret Management
**Task:** Securely manage API keys and passwords

**Interactive Setup:**
```bash
python setup_secrets.py
```
Prompts you for:
- Salesforce API keys
- Snowflake credentials
- Email (IMAP/SMTP) passwords
- Any other authentication tokens

**Manual Setup:**
1. Copy templates: `cp .env.example .env` and `cp secrets.json.example secrets.json`
2. Edit with real values: `nano .env` or your editor
3. Never commit to git (both are git-ignored)

**Verification:**
```bash
git check-ignore -v .env secrets.json
# Should show both are ignored
```

---

### 8. Email Intake & Export
**Task:** Receive files via email and export results via email

**IMAP Configuration (Intake):**
```bash
# In .env or secrets.json
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USE_SSL=true
IMAP_EMAIL=your_email@gmail.com
IMAP_PASSWORD=your_app_password  # Not your regular password!
```

**SMTP Configuration (Export):**
```bash
# In .env or secrets.json
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_app_password  # Not your regular password!
```

**Use FFIS UI:**
- "Ingest" tab → Select "Email (IMAP)"
- "Export" tab → Select "Email (SMTP)"

---

### 9. Troubleshooting
**Common Issues:**

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: streamlit` | Run `pip install -r requirements.txt` |
| `Config not loading` | Run `python setup_secrets.py` or check `.env` exists |
| `Export fails to API` | Check API endpoint URL and token in `.env` |
| `Docker won't start` | Run `docker-compose logs` to see error |
| `Secrets exposed to git` | Run `git filter-branch` to remove, then rotate credentials |

**Debug Mode:**
```bash
# Verbose logging
STREAMLIT_LOGGER_LEVEL=debug python -m streamlit run flat_file_scrubber.py

# Check configuration
python -c "from config import *; print(get_streamlit_config())"
```

---

## Templates & Scripts

### CSV Cleaning Template
See: `templates/clean_csv.py` (batch processing example)

### Docker Compose Override
For development with custom ports:
```yaml
# docker-compose.override.yml
version: '3.8'
services:
  flat-file-scrubber:
    ports:
      - "9999:8501"  # Use port 9999 instead of 8501
    environment:
      - STREAMLIT_LOGGER_LEVEL=debug
```

### Kubernetes Deployment
See: [DEPLOYMENT.md](../../DEPLOYMENT.md) for full K8s manifest

---

## Getting Help

- **Using FFIS:** See [README.md](../../README.md)
- **Security:** See [SECURITY.md](../../SECURITY.md)
- **Deployment:** See [DEPLOYMENT.md](../../DEPLOYMENT.md)
- **Docker:** See [DOCKER_QUICK_START.md](../../DOCKER_QUICK_START.md)
- **Code:** Import modules and explore `config.py`, `snowflake_connector.py`

---

## Quick Commands

```bash
# Launch
run.bat                                    # Windows
./run.sh                                   # macOS/Linux

# Setup
python setup_secrets.py                    # Interactive credential setup
docker-compose up -d                       # Docker launch

# Debug
python -c "from config import *; print(get_streamlit_config())"
docker-compose logs -f ffis-app
git check-ignore -v .env secrets.json

# Stop
docker-compose down
```
