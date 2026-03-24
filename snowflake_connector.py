"""
Optional Snowflake integration helper for Flat File Scrubber
Install: pip install snowflake-connector-python
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any

try:
    import snowflake.connector
    SNOWFLAKE_AVAILABLE = True
except ImportError:
    SNOWFLAKE_AVAILABLE = False


def get_snowflake_config() -> Optional[Dict[str, Any]]:
    """
    Load Snowflake configuration from environment variables or secrets.json
    """
    if not SNOWFLAKE_AVAILABLE:
        return None
    
    # Try to load from secrets.json first
    secrets_file = Path(__file__).parent / "secrets.json"
    if secrets_file.exists():
        try:
            with open(secrets_file, "r") as f:
                secrets = json.load(f)
                if "snowflake" in secrets:
                    return secrets["snowflake"]
        except (json.JSONDecodeError, IOError):
            pass
    
    # Fall back to environment variables
    config = {
        "account": os.getenv("SNOWFLAKE_ACCOUNT"),
        "user": os.getenv("SNOWFLAKE_USER"),
        "password": os.getenv("SNOWFLAKE_PASSWORD"),
        "database": os.getenv("SNOWFLAKE_DATABASE"),
        "schema": os.getenv("SNOWFLAKE_SCHEMA"),
        "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE"),
    }
    
    # Only return if all required fields are present
    if all(config.values()):
        return config
    
    return None


def connect_snowflake() -> Optional[snowflake.connector.SnowflakeConnection]:
    """
    Create a Snowflake connection using configured credentials.
    
    Returns:
        SnowflakeConnection or None if configuration is missing
    """
    if not SNOWFLAKE_AVAILABLE:
        raise ImportError("snowflake-connector-python not installed. Install with: pip install snowflake-connector-python")
    
    config = get_snowflake_config()
    if not config:
        return None
    
    try:
        conn = snowflake.connector.connect(
            account=config["account"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            schema=config["schema"],
            warehouse=config["warehouse"],
        )
        return conn
    except Exception as e:
        print(f"Error connecting to Snowflake: {e}")
        return None


def export_to_snowflake(df, table_name: str, if_exists: str = "replace") -> bool:
    """
    Export a pandas DataFrame to Snowflake.
    
    Args:
        df: pandas DataFrame to export
        table_name: Target table name in Snowflake
        if_exists: 'replace', 'append', or 'fail'
    
    Returns:
        True if successful, False otherwise
    """
    try:
        import pandas as pd
        from sqlalchemy import create_engine
    except ImportError:
        print("pandas and/or sqlalchemy not installed")
        return False
    
    if not SNOWFLAKE_AVAILABLE:
        print("snowflake-connector-python not installed")
        return False
    
    config = get_snowflake_config()
    if not config:
        print("Snowflake configuration not found")
        return False
    
    try:
        # Create SQLAlchemy engine for Snowflake
        engine = create_engine(
            f"snowflake://{config['user']}:{config['password']}@{config['account']}/{config['database']}/{config['schema']}?warehouse={config['warehouse']}"
        )
        
        # Write DataFrame to Snowflake
        df.to_sql(
            table_name.lower(),
            con=engine,
            if_exists=if_exists,
            index=False,
            method="multi",
            chunksize=10000
        )
        
        engine.dispose()
        return True
    except Exception as e:
        print(f"Error exporting to Snowflake: {e}")
        return False


def query_snowflake(sql: str) -> Optional[list]:
    """
    Execute a SQL query against Snowflake and return results.
    
    Args:
        sql: SQL query to execute
    
    Returns:
        List of tuples (rows) or None if failed
    """
    conn = connect_snowflake()
    if not conn:
        return None
    
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        return results
    except Exception as e:
        print(f"Error querying Snowflake: {e}")
        return None


if __name__ == "__main__":
    # Test connection
    print("Testing Snowflake connector...")
    config = get_snowflake_config()
    if config:
        print("✓ Configuration found:")
        print(f"  Account: {config.get('account')}")
        print(f"  User: {config.get('user')}")
        print(f"  Database: {config.get('database')}")
        print(f"  Schema: {config.get('schema')}")
        print(f"  Warehouse: {config.get('warehouse')}")
        
        # Try to connect
        conn = connect_snowflake()
        if conn:
            print("✓ Connection successful!")
            conn.close()
        else:
            print("✗ Connection failed")
    else:
        print("✗ Snowflake configuration not found in secrets.json or environment variables")
