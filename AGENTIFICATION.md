# 🤖 FFIS Agent: Using AI as Your Application Front-End

Your Flat File Scrubber project is now fully **agentified** – meaning you can use an intelligent AI agent as the primary interface to the application.

## What This Means

Instead of manually navigating the Streamlit UI, you can now:

1. **Chat with an expert agent** who understands the entire FFIS workflow
2. **Automate data operations** through Python code or REST API
3. **Integrate FFIS** into your development tools and scripts
4. **Deploy intelligent workflows** that adapt to your needs

---

## Three Ways to Use the Agent

### 1️⃣ Chat Interface (VS Code Copilot)

Type commands in VS Code chat to get help:

```
/ffis How do I set up FFIS?
/ffis I want to clean a CSV and export to Snowflake
/ffis What are the 4 DataFrames?
```

The agent responds with:
- Step-by-step guidance
- Code examples
- Configuration recommendations
- Troubleshooting help

**Files involved:**
- `.github/agents/ffis-agent.agent.md` – Agent definition
- `.github/skills/ffis/SKILL.md` – Reusable workflows

### 2️⃣ Python API (Programmatic)

Import and use the agent directly in your Python code:

```python
from ffis_agent import FFISAgent

agent = FFISAgent()

# Validate a CSV
result = agent.validate_csv("data.csv")
print(f"Valid: {result['valid']}, Rows: {result['rows']}")

# Clean with operations
result = agent.clean_csv(
    "data.csv",
    operations={
        "drop_duplicates": True,
        "strip_whitespace": True,
        "uppercase_columns": ["email", "phone"]
    }
)
print(f"Cleaned {result['output_rows']} rows")

# Export to Snowflake
result = agent.export_to_snowflake("cleaned.csv", "target_table")
print("✓ Exported to Snowflake" if result['success'] else "✗ Failed")

# Export to API
result = agent.export_to_api(
    "cleaned.csv",
    endpoint="https://api.salesforce.com/...",
    batch_size=200
)
print(f"Sent {result['rows_sent']} rows"
```

**File:** `ffis_agent.py`

### 3️⃣ REST API (HTTP Endpoints)

Expose the agent as REST endpoints for integration into other applications:

```bash
# Install FastAPI
pip install fastapi uvicorn

# Start the API server
python ffis_api.py
# or: uvicorn ffis_api:app --reload
```

Then call endpoints:

```bash
# Get configuration
curl http://localhost:8000/config

# Validate a CSV (upload file)
curl -X POST -F "file=@data.csv" http://localhost:8000/validate

# Clean a CSV
curl -X POST -F "file=@data.csv" \
  -F 'operations={"drop_duplicates": true}' \
  http://localhost:8000/clean

# Export to Snowflake
curl -X POST http://localhost:8000/export/snowflake \
  -H "Content-Type: application/json" \
  -d '{
    "filepath": "cleaned.csv",
    "table_name": "target_table"
  }'

# Interactive docs at:
# http://localhost:8000/docs
# http://localhost:8000/redoc
```

**File:** `ffis_api.py`

---

## Agent Capabilities

### Help & Information
- `agent.get_config()` – Get current configuration
- `agent.get_help(topic)` – Get help (topics: setup, cleaning, export, snowflake, troubleshooting)
- `agent.check_dependencies()` – Verify installed packages
- `agent.launch_ui()` – Get Streamlit UI info

### Data Validation
- `agent.validate_csv(filepath)` – Check CSV quality, find issues
- Returns: rows, columns, data types, missing values, duplicates

### Data Cleaning
- `agent.clean_csv(filepath, operations)` – Apply transformations
- Operations: drop_duplicates, drop_nulls_in, uppercase, lowercase, strip_whitespace, etc.

### Data Export
- `agent.export_to_snowflake(filepath, table_name)` – Load to Snowflake
- `agent.export_to_api(filepath, endpoint, batch_size)` – POST to REST API
- Handles batching, error handling, retries

---

## Example Workflows

### Workflow 1: Automated Daily Batch Processing

```python
from pathlib import Path
from ffis_agent import FFISAgent
import schedule
import time

agent = FFISAgent()

def daily_cleanup():
    """Run every morning at 6 AM."""
    data_dir = Path("raw_data")
    output_dir = Path("cleaned_data")
    
    for csv_file in data_dir.glob("*.csv"):
        print(f"Processing {csv_file.name}...")
        
        # Validate
        validation = agent.validate_csv(str(csv_file))
        if not validation['valid']:
            print(f"  ✗ Invalid: {validation['issues']}")
            continue
        
        # Clean
        clean_result = agent.clean_csv(
            str(csv_file),
            output_path=str(output_dir / csv_file.name),
            operations={
                "drop_duplicates": True,
                "drop_nulls_in": ["email"],
                "strip_whitespace": True
            }
        )
        
        # Export to Snowflake
        export_result = agent.export_to_snowflake(
            str(output_dir / csv_file.name),
            table_name=csv_file.stem
        )
        
        status = "✓" if export_result['success'] else "✗"
        print(f"  {status} {clean_result['rows_removed']} rows removed")
        print(f"  {status} Exported {export_result['rows']} rows to Snowflake")

# Schedule
schedule.every().day.at("06:00").do(daily_cleanup)

while True:
    schedule.run_pending()
    time.sleep(60)
```

### Workflow 2: RESTful Microservice

```python
# Start API: python ffis_api.py

# Call from another service:
import requests

data = {"filepath": "data.csv", "table_name": "contacts"}
response = requests.post("http://localhost:8000/export/snowflake", json=data)

if response.json()["success"]:
    print("✓ Loaded to Snowflake")
else:
    print(f"✗ Error: {response.json()['error']}")
```

### Workflow 3: Interactive Agent Help in VS Code

```
User: /ffis
Agent: I'm the FFIS Agent. How can I help you with data cleaning?

User: I have a CSV with 100k rows and need to:
1. Remove duplicates by email
2. Convert all names to uppercase
3. Load to Snowflake
4. Send cleaned data count via email

Agent: I'll help! Here's the recommended approach:

# 1. Validate data quality
from ffis_agent import FFISAgent
agent = FFISAgent()
result = agent.validate_csv("contacts.csv")
print(f"Found {result['duplicates']} duplicates")

# 2. Clean and prepare
result = agent.clean_csv(
    "contacts.csv",
    operations={
        "drop_duplicates": True,
        "uppercase_columns": ["firstName", "lastName"]
    }
)

# 3. Export to Snowflake
result = agent.export_to_snowflake(result['output_path'], "contacts")

# 4. Send email notification
# (Use your preferred email library)
print(f"✓ Loaded {result['rows']} rows to Snowflake")
```

---

## Integration Examples

### Integration 1: Streamlit UI + Agent Backend

Your existing Streamlit app now has an **agent brain** behind it:

```python
# In flat_file_scrubber.py
from ffis_agent import FFISAgent

agent = FFISAgent()

# In Export tab
if st.button("Export to Snowflake"):
    result = agent.export_to_snowflake(clean_df_path, table_name)
    if result['success']:
        st.success(f"✓ Loaded {result['rows']} rows")
    else:
        st.error(f"✗ {result['error']}")
```

### Integration 2: GitHub Actions Automation

```yaml
# .github/workflows/daily-cleanup.yml
name: Daily Data Cleanup

on:
  schedule:
    - cron: '0 6 * * *'  # Every day at 6 AM

jobs:
  cleanup:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: '3.9'
      
      - name: Install dependencies
        run: pip install -r requirements.txt
      
      - name: Run cleanup
        run: python cleanup_script.py  # Uses FFISAgent
```

### Integration 3: Kubernetes CronJob

```yaml
# k8s/cronjob.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ffis-daily-cleanup
spec:
  schedule: "0 6 * * *"  # Every day at 6 AM
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: ffis
            image: ffis:latest
            command: ["python", "cleanup_script.py"]
          restartPolicy: OnFailure
```

---

## Configuration

### Via Chat
```
/ffis How do I configure Snowflake credentials?

Agent: Use the interactive setup:
python setup_secrets.py

Or manually:
cp .env.example .env
nano .env
```

### Via API
```bash
curl http://localhost:8000/config
```

### Via Python
```python
from ffis_agent import FFISAgent
from config import get_streamlit_config

agent = FFISAgent()
config = agent.get_config()
```

---

## Testing the Agent

### 1. Test Chat Interface
In VS Code, type `/ffis` and experiment with prompts:
```
/ffis help
/ffis setup instructions
/ffis clean a CSV and export to Snowflake
/ffis troubleshoot configuration errors
```

### 2. Test Python API
```bash
python
>>> from ffis_agent import FFISAgent
>>> agent = FFISAgent()
>>> agent.get_config()
>>> agent.check_dependencies()
>>> agent.get_help("setup")
```

### 3. Test REST API
```bash
pip install fastapi uvicorn
python ffis_api.py

# In another terminal:
curl http://localhost:8000/health
curl http://localhost:8000/docs
```

---

## Files Created for Agentification

| File | Purpose |
|------|---------|
| `.github/agents/ffis-agent.agent.md` | VS Code agent definition (chat interface) |
| `.github/skills/ffis/SKILL.md` | Reusable workflows and operations |
| `.github/instructions/ffis-development.instructions.md` | Dev guidelines for the agent |
| `copilot-instructions.md` | Workspace-level instructions |
| `ffis_agent.py` | Python API for programmatic access |
| `ffis_api.py` | FastAPI REST endpoints (optional) |

---

## Next Steps

1. **Try the chat interface**: Type `/ffis` in VS Code chat
2. **Explore the Python API**: `from ffis_agent import FFISAgent; agent = FFISAgent()`
3. **Optional REST API**: `pip install fastapi uvicorn && python ffis_api.py`
4. **Integrate into your workflow**: Use in scripts, schedulers, pipelines
5. **Deploy**: Docker, K8s, Cloud Run with agent backend

---

## Support

- **Chat help**: `/ffis` in VS Code
- **Python API docs**: Read docstrings in `ffis_agent.py`
- **REST API docs**: Visit `http://localhost:8000/docs` when running `ffis_api.py`
- **Troubleshooting**: `/ffis help troubleshooting`

---

## You Now Have:

✅ **Chat Agent** – Intelligent help via `/ffis` commands  
✅ **Python API** – Direct code integration with `FFISAgent`  
✅ **REST API** – HTTP endpoints for microservices  
✅ **Workflows** – Pre-built skills for common tasks  
✅ **Documentation** – Complete guides for all three modes  

Your FFIS project is now **fully agentified** and ready for intelligent automation! 🚀
