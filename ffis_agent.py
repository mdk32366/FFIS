"""
FFIS Agent API Backend
Exposes FFIS operations as a Python API for programmatic use.
Can be wrapped in FastAPI for REST endpoints.
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
import pandas as pd
import os
from config import (
    get_streamlit_config,
    get_salesforce_objects,
    get_api_config,
    get_imap_config,
    get_smtp_config,
)


class FFISAgent:
    """
    Agent interface for FFIS operations.
    Provides programmatic access to data cleaning, validation, and export workflows.
    """

    def __init__(self):
        self.config = get_streamlit_config()
        self.salesforce_objects = get_salesforce_objects()

    # ────────────────────────────────────────────────────────────────────────────
    # INFORMATION & HELP
    # ────────────────────────────────────────────────────────────────────────────

    def get_config(self) -> Dict[str, Any]:
        """Get current FFIS configuration (non-sensitive)."""
        return {
            "page_title": self.config.get("page_title"),
            "page_icon": self.config.get("page_icon"),
            "layout": self.config.get("layout"),
            "salesforce_objects": self.salesforce_objects,
        }

    def get_help(self, topic: Optional[str] = None) -> str:
        """
        Get help on FFIS usage.
        
        Topics: setup, cleaning, export, snowflake, troubleshooting
        """
        help_texts = {
            "setup": """
            SETUP & CONFIGURATION:
            1. python setup_secrets.py  # Interactive credential setup
            2. run.bat or ./run.sh      # Launch the app
            3. Visit http://localhost:8501
            """,
            "cleaning": """
            DATA CLEANING WORKFLOW:
            Step 1: Ingest - Upload CSV or load from file/email
            Step 2: Inspect - Preview data and schema
            Step 3-8: Transform - Clean columns, nulls, special chars, duplicates
            Step 9: Validate - Check for incomplete records
            Step 10: Export - Download or send to API/email/Snowflake
            """,
            "export": """
            EXPORT OPTIONS:
            - CSV Download: Direct browser download
            - REST API: POST to Salesforce or custom endpoint
            - SMTP Email: Send cleaned data via email
            - Snowflake: Direct database load (requires snowflake-connector-python)
            """,
            "snowflake": """
            SNOWFLAKE INTEGRATION:
            pip install snowflake-connector-python
            
            from snowflake_connector import export_to_snowflake
            export_to_snowflake(df, 'table_name', if_exists='replace')
            """,
            "troubleshooting": """
            COMMON ISSUES:
            - ModuleNotFoundError: pip install -r requirements.txt
            - Config errors: python setup_secrets.py
            - Docker issues: docker-compose logs ffis-app
            - Secrets exposed: git filter-branch (and rotate credentials!)
            """,
        }

        if topic and topic in help_texts:
            return help_texts[topic]
        elif topic:
            return f"Unknown help topic: {topic}. Available: {', '.join(help_texts.keys())}"
        else:
            return "\n".join(help_texts.values())

    # ────────────────────────────────────────────────────────────────────────────
    # DATA OPERATIONS
    # ────────────────────────────────────────────────────────────────────────────

    def validate_csv(self, filepath: str) -> Dict[str, Any]:
        """
        Validate a CSV file before processing.
        
        Returns: {
            'valid': bool,
            'rows': int,
            'columns': list,
            'dtypes': dict,
            'missing': dict,
            'duplicates': int,
            'issues': list
        }
        """
        try:
            df = pd.read_csv(filepath)
            issues = []

            # Check for missing values
            missing = df.isnull().sum().to_dict()

            # Check for duplicates
            duplicates = df.duplicated().sum()

            # Data types
            dtypes = {col: str(dtype) for col, dtype in df.dtypes.items()}

            # Validate
            if df.shape[0] == 0:
                issues.append("CSV is empty (no rows)")
            if df.shape[1] == 0:
                issues.append("CSV has no columns")

            return {
                "valid": len(issues) == 0,
                "rows": df.shape[0],
                "columns": list(df.columns),
                "dtypes": dtypes,
                "missing": missing,
                "duplicates": duplicates,
                "issues": issues,
            }
        except Exception as e:
            return {
                "valid": False,
                "error": str(e),
                "issues": [f"Failed to read CSV: {e}"],
            }

    def clean_csv(
        self,
        filepath: str,
        output_path: Optional[str] = None,
        operations: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Apply cleaning operations to a CSV file.
        
        Operations:
        {
            'drop_columns': ['col1', 'col2'],
            'drop_duplicates': True,
            'drop_nulls_in': ['required_col1', 'required_col2'],
            'uppercase_columns': ['col1', 'col2'],
            'lowercase_columns': ['col1'],
            'strip_whitespace': True,
        }
        """
        try:
            df = pd.read_csv(filepath)
            original_rows = len(df)

            operations = operations or {}

            # Drop specified columns
            if "drop_columns" in operations:
                df = df.drop(columns=operations["drop_columns"], errors="ignore")

            # Drop duplicates
            if operations.get("drop_duplicates", False):
                df = df.drop_duplicates()

            # Drop rows with nulls in required columns
            if "drop_nulls_in" in operations:
                df = df.dropna(subset=operations["drop_nulls_in"])

            # Uppercase specified columns
            if "uppercase_columns" in operations:
                for col in operations["uppercase_columns"]:
                    if col in df.columns:
                        df[col] = df[col].str.upper()

            # Lowercase specified columns
            if "lowercase_columns" in operations:
                for col in operations["lowercase_columns"]:
                    if col in df.columns:
                        df[col] = df[col].str.lower()

            # Strip whitespace
            if operations.get("strip_whitespace", False):
                for col in df.select_dtypes(include=["object"]).columns:
                    df[col] = df[col].str.strip()

            # Save output
            output_path = output_path or filepath.replace(".csv", "_cleaned.csv")
            df.to_csv(output_path, index=False)

            return {
                "success": True,
                "input_rows": original_rows,
                "output_rows": len(df),
                "rows_removed": original_rows - len(df),
                "output_path": output_path,
                "columns": list(df.columns),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    # ────────────────────────────────────────────────────────────────────────────
    # EXPORT OPERATIONS
    # ────────────────────────────────────────────────────────────────────────────

    def export_to_snowflake(
        self, filepath: str, table_name: str, if_exists: str = "replace"
    ) -> Dict[str, Any]:
        """
        Export CSV to Snowflake (requires snowflake-connector-python).
        """
        try:
            from snowflake_connector import export_to_snowflake

            df = pd.read_csv(filepath)
            success = export_to_snowflake(df, table_name, if_exists=if_exists)

            return {
                "success": success,
                "table_name": table_name,
                "rows": len(df),
                "columns": list(df.columns),
            }
        except ImportError:
            return {
                "success": False,
                "error": "snowflake-connector-python not installed",
                "install": "pip install snowflake-connector-python",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    def export_to_api(
        self,
        filepath: str,
        endpoint: str,
        batch_size: int = 200,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Export CSV to REST API endpoint (e.g., Salesforce).
        Uses batching to handle large datasets.
        """
        try:
            import requests

            df = pd.read_csv(filepath)
            headers = headers or get_api_config().get("headers", {})

            total_rows = len(df)
            batches_sent = 0
            rows_sent = 0
            errors = []

            # Process in batches
            for i in range(0, total_rows, batch_size):
                batch = df.iloc[i : i + batch_size]
                payload = batch.to_dict(orient="records")

                try:
                    response = requests.post(
                        endpoint, json=payload, headers=headers, timeout=30
                    )
                    if response.status_code in [200, 201, 202]:
                        batches_sent += 1
                        rows_sent += len(batch)
                    else:
                        errors.append(
                            f"Batch {batches_sent + 1}: {response.status_code} {response.text[:100]}"
                        )
                except Exception as e:
                    errors.append(f"Batch {batches_sent + 1}: {str(e)}")

            return {
                "success": len(errors) == 0,
                "total_rows": total_rows,
                "rows_sent": rows_sent,
                "batches_sent": batches_sent,
                "errors": errors,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    # ────────────────────────────────────────────────────────────────────────────
    # UTILITY
    # ────────────────────────────────────────────────────────────────────────────

    def launch_ui(self) -> Dict[str, str]:
        """
        Launch the Streamlit UI.
        Returns connection info.
        """
        return {
            "url": "http://localhost:8501",
            "command": "python -m streamlit run flat_file_scrubber.py",
            "windows": "run.bat",
            "unix": "./run.sh",
        }

    def check_dependencies(self) -> Dict[str, bool]:
        """Check if all required dependencies are installed."""
        dependencies = {
            "streamlit": self._check_import("streamlit"),
            "pandas": self._check_import("pandas"),
            "numpy": self._check_import("numpy"),
            "requests": self._check_import("requests"),
            "snowflake-connector": self._check_import("snowflake.connector"),
        }
        return dependencies

    @staticmethod
    def _check_import(module: str) -> bool:
        """Test if a module can be imported."""
        try:
            __import__(module)
            return True
        except ImportError:
            return False


# ────────────────────────────────────────────────────────────────────────────
# EXAMPLE USAGE
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    agent = FFISAgent()

    # Print configuration
    print("📋 FFIS Configuration:")
    print(json.dumps(agent.get_config(), indent=2))

    # Check dependencies
    print("\n✓ Dependencies:")
    deps = agent.check_dependencies()
    for dep, installed in deps.items():
        status = "✓" if installed else "✗"
        print(f"  {status} {dep}")

    # Show help
    print("\n📖 Help:")
    print(agent.get_help("setup"))

    print("\n🚀 Launch the UI with: run.bat or ./run.sh")
