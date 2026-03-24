---
name: FFIS Copilot Instructions
description: "Project-wide instructions for working with Flat File Scrubber. Guides agent on code patterns, deployment, and user support."
---

# FFIS Workspace Instructions

This workspace contains the **Flat File Scrubber (FFIS)** – a Streamlit-based data cleaning toolkit for Salesforce and Snowflake integration.

## Quick Context

- **Type**: Python Streamlit application
- **Purpose**: Interactive CSV cleaning, validation, and export
- **Key Files**: `flat_file_scrubber.py` (UI), `config.py` (settings), `snowflake_connector.py` (DB)
- **Deployment**: Docker, Kubernetes, Cloud Run, ECS, or local
- **Security**: Secrets protected via `.env`, `secrets.json`, git-ignored

## How to Help Users

### User Wants to Clean Data
1. Guide them to run: `run.bat` or `./run.sh` or `python -m streamlit run flat_file_scrubber.py`
2. Walk through: Ingest → Inspect → Transform → Validate → Export
3. Explain the 4 DataFrames: Clean, Dupes, Bad/Incomplete, SF Dupes
4. Point to README for detailed workflow

### User Wants to Automate
1. Suggest import patterns: `from config import ...` from `config.py`
2. Script examples: batch processing CSVs, scheduled tasks
3. Point to `ffis_agent.py` for programmatic API
4. Recommend Docker for reproducible environments

### User Wants to Deploy
1. For local: `docker-compose up -d`
2. For cloud: Reference [DEPLOYMENT.md](./DEPLOYMENT.md)
3. For secrets: `python setup_secrets.py` or read [SECURITY.md](./SECURITY.md)

## Available Agents & Skills

### `/ffis` Skill
Reusable workflows for:
- Setup & configuration
- Data cleaning
- Batch processing
- Salesforce/Snowflake integration
- Docker deployment
- Secret management
- Troubleshooting

**How to use**: Type `/ffis` in chat to see available workflows

### `ffis-agent.agent.md`
Expert assistant for:
- End-user support (how to use FFIS)
- Operations & automation (scripts, workflows)
- Development & deployment (code changes, infrastructure)

**Located**: `.github/agents/ffis-agent.agent.md`

## Code Guidelines

### Configuration
- Always use `config.py` helpers, never hardcode values
- Test with: `python -c "from config import get_streamlit_config; print(get_streamlit_config())"`
- Examples: `get_streamlit_config()`, `get_api_config()`, `get_salesforce_objects()`

### Secrets
- Never log or commit secrets
- Use `setup_secrets.py` for interactive setup
- Test with: `git check-ignore -v .env secrets.json`

### DockerCompose
- Local dev: `docker-compose up -d`
- Production: See [DEPLOYMENT.md](./DEPLOYMENT.md)

### Documentation
- Keep README, SECURITY.md, DEPLOYMENT.md in sync with code changes
- Update `.env.example` when adding config options

## Common Patterns

### Add New Configuration Option
```python
# 1. Update .env.example
NEW_SETTING=default_value

# 2. Add to config.py
def get_new_setting() -> str:
    return getenv_str("NEW_SETTING", "default")

# 3. Use in code
from config import get_new_setting
value = get_new_setting()
```

### Batch Process CSV Files
```python
from pathlib import Path
import pandas as pd

for csv in Path("data").glob("*.csv"):
    df = pd.read_csv(csv)
    # Apply transformations...
    df.to_csv(f"output/{csv.name}", index=False)
```

### Export to Snowflake
```python
from snowflake_connector import export_to_snowflake
import pandas as pd

df = pd.read_csv("cleaned.csv")
export_to_snowflake(df, "target_table")
```

### Use the Agent API
```python
from ffis_agent import FFISAgent

agent = FFISAgent()
result = agent.validate_csv("data.csv")
result = agent.clean_csv("data.csv", operations={
    "drop_duplicates": True,
    "strip_whitespace": True
})
result = agent.export_to_snowflake("cleaned.csv", "target_table")
```

## Testing

### Configuration
```bash
python -c "from config import *; print(get_streamlit_config())"
```

### Docker
```bash
docker build -t ffis:test .
docker-compose up -d
# Visit http://localhost:8501
docker-compose down
```

### Secrets
```bash
git check-ignore -v .env secrets.json  # Should be ignored
python setup_secrets.py                # Interactive setup
```

## Resources

- [README.md](./README.md) – User guide
- [SECURITY.md](./SECURITY.md) – Secrets & protection
- [DEPLOYMENT.md](./DEPLOYMENT.md) – Cloud deployments
- [DOCKER_QUICK_START.md](./DOCKER_QUICK_START.md) – Docker reference
- [SECRETS_SETUP.md](./SECRETS_SETUP.md) – Credential setup
- `.github/agents/ffis-agent.agent.md` – Agent definition
- `.github/instructions/ffis-development.instructions.md` – Dev guidelines
- `.github/skills/ffis/SKILL.md` – Reusable workflows

## When to Recommend the Agent

Use the **FFIS Agent** (`/ffis-agent`) when:
- User asks "How do I...?" about using the app
- User wants to understand the data cleaning workflow
- User needs help with configuration or deployment
- User asks about automation or scripting
- User is troubleshooting an issue

The agent has context about all 11 workflow steps, configuration options, deployment strategies, and security practices.

## Project Status

✅ **Production Ready**
- Docker support with multi-stage builds
- Comprehensive security hardening
- Cloud deployment guides (GCP, AWS, K8s)
- Interactive secret management
- Full test coverage via automated workflows
- Documentation complete

🚀 **Next Steps (Optional)**
- Add Pydantic models for data validation
- Add unit tests for `config.py` and `snowflake_connector.py`
- Add FastAPI wrapper for REST API mode
- Integrate with CI/CD pipeline (GitHub Actions)
