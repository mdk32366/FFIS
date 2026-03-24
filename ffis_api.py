"""
Optional FastAPI wrapper for FFIS Agent
Exposes the agent as REST API endpoints
Install: pip install fastapi uvicorn
Run: uvicorn ffis_api:app --reload
"""

from fastapi import FastAPI, HTTPException, File, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import os
import tempfile
import json

# Only load if FastAPI is available
try:
    from ffis_agent import FFISAgent
    AGENT_AVAILABLE = True
except ImportError:
    AGENT_AVAILABLE = False

app = FastAPI(
    title="FFIS Agent API",
    description="REST API for Flat File Scrubber operations",
    version="1.0.0",
)

# Initialize agent
if AGENT_AVAILABLE:
    agent = FFISAgent()


# ────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS
# ────────────────────────────────────────────────────────────────────────────


class CleaningOperation(BaseModel):
    drop_columns: Optional[List[str]] = None
    drop_duplicates: Optional[bool] = False
    drop_nulls_in: Optional[List[str]] = None
    uppercase_columns: Optional[List[str]] = None
    lowercase_columns: Optional[List[str]] = None
    strip_whitespace: Optional[bool] = False


class ExportRequest(BaseModel):
    filepath: str
    table_name: Optional[str] = None
    if_exists: Optional[str] = "replace"


class APIExportRequest(BaseModel):
    filepath: str
    endpoint: str
    batch_size: Optional[int] = 200
    headers: Optional[Dict[str, str]] = None


# ────────────────────────────────────────────────────────────────────────────
# HEALTH & INFO
# ────────────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "agent": "ready" if AGENT_AVAILABLE else "not_loaded"}


@app.get("/config")
async def get_config():
    """Get FFIS configuration."""
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")
    return agent.get_config()


@app.get("/help")
async def get_help(topic: Optional[str] = None):
    """Get help on FFIS usage."""
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")
    return {"help": agent.get_help(topic)}


@app.get("/dependencies")
async def check_dependencies():
    """Check if all dependencies are installed."""
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")
    return agent.check_dependencies()


# ────────────────────────────────────────────────────────────────────────────
# DATA VALIDATION
# ────────────────────────────────────────────────────────────────────────────


@app.post("/validate")
async def validate_csv(file: UploadFile = File(...)):
    """
    Validate a CSV file.
    
    Returns: rows, columns, data types, missing values, duplicates, issues
    """
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")

    try:
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # Validate
        result = agent.validate_csv(tmp_path)

        # Clean up
        os.unlink(tmp_path)

        return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


# ────────────────────────────────────────────────────────────────────────────
# DATA CLEANING
# ────────────────────────────────────────────────────────────────────────────


@app.post("/clean")
async def clean_csv(
    file: UploadFile = File(...),
    operations: Optional[str] = None
):
    """
    Clean a CSV file with specified operations.
    
    operations: JSON string with cleaning operations
    Example: {"drop_duplicates": true, "strip_whitespace": true}
    """
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")

    try:
        # Save uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # Parse operations if provided
        ops = {}
        if operations:
            ops = json.loads(operations)

        # Clean
        result = agent.clean_csv(
            tmp_path,
            output_path=tmp_path.replace(".csv", "_cleaned.csv"),
            operations=ops,
        )

        # Return cleaned file path
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ────────────────────────────────────────────────────────────────────────────
# DATA EXPORT
# ────────────────────────────────────────────────────────────────────────────


@app.post("/export/snowflake")
async def export_to_snowflake(request: ExportRequest):
    """Export CSV to Snowflake."""
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")

    if not os.path.exists(request.filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return agent.export_to_snowflake(
        request.filepath,
        request.table_name or "imported_data",
        if_exists=request.if_exists,
    )


@app.post("/export/api")
async def export_to_api(request: APIExportRequest):
    """Export CSV to REST API endpoint."""
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")

    if not os.path.exists(request.filepath):
        raise HTTPException(status_code=404, detail="File not found")

    return agent.export_to_api(
        request.filepath,
        request.endpoint,
        batch_size=request.batch_size,
        headers=request.headers,
    )


# ────────────────────────────────────────────────────────────────────────────
# UI
# ────────────────────────────────────────────────────────────────────────────


@app.get("/ui")
async def get_ui_info():
    """Get information on launching the Streamlit UI."""
    if not AGENT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Agent not available")
    return agent.launch_ui()


# ────────────────────────────────────────────────────────────────────────────
# ROOT
# ────────────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    """API root with documentation."""
    return {
        "name": "FFIS Agent API",
        "version": "1.0.0",
        "docs": "/docs",
        "endpoints": {
            "health": "GET /health",
            "config": "GET /config",
            "help": "GET /help?topic=setup",
            "dependencies": "GET /dependencies",
            "validate": "POST /validate",
            "clean": "POST /clean",
            "export_snowflake": "POST /export/snowflake",
            "export_api": "POST /export/api",
            "ui": "GET /ui",
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# STARTUP/SHUTDOWN
# ────────────────────────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    """Initialize on startup."""
    if not AGENT_AVAILABLE:
        print("⚠️  FFISAgent not available")
    else:
        print("✓ FFIS Agent API ready")
        print("📖 Visit http://localhost:8000/docs for interactive documentation")


# ────────────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    print("🚀 Starting FFIS Agent API...")
    print("📖 Visit http://localhost:8000/docs for documentation")
    print("   or http://localhost:8000/redoc for alternative docs")

    uvicorn.run(app, host="0.0.0.0", port=8000)
