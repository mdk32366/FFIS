#!/usr/bin/env python3
"""
Setup helper for Flat File Scrubber - Securely configure secrets
This script guides you through creating .env and secrets.json files
"""

import os
import json
import sys
from pathlib import Path
from getpass import getpass


def setup_env_file():
    """Interactively create .env file from .env.example"""
    env_file = Path(".env")
    env_example = Path(".env.example")
    
    if env_file.exists():
        overwrite = input(f"\n⚠️  {env_file} already exists. Overwrite? (y/n): ").lower()
        if overwrite != "y":
            print("Skipping .env setup")
            return
    
    if not env_example.exists():
        print(f"❌ {env_example} not found!")
        return
    
    print("\n📝 Setting up .env file...")
    print("(Press Enter to skip optional fields)\n")
    
    config = {}
    
    # Streamlit settings
    print("=== STREAMLIT CONFIGURATION ===")
    config["STREAMLIT_PAGE_TITLE"] = input("Page title [Flat File Scrubber]: ") or "Flat File Scrubber"
    config["STREAMLIT_PAGE_ICON"] = input("Page icon emoji [🧹]: ") or "🧹"
    config["STREAMLIT_LAYOUT"] = input("Layout [wide/centered] [wide]: ") or "wide"
    
    # Salesforce objects
    print("\n=== SALESFORCE CONFIGURATION ===")
    config["SALESFORCE_OBJECTS"] = input(
        "Salesforce objects (comma-separated) [Account,Contact,Lead]: "
    ) or "Account,Contact,Lead"
    
    # IMAP configuration
    print("\n=== EMAIL INTAKE (IMAP) ===")
    imap_enabled = input("Configure email intake? (y/n) [n]: ").lower()
    if imap_enabled == "y":
        config["IMAP_HOST"] = input("IMAP host [imap.gmail.com]: ") or "imap.gmail.com"
        config["IMAP_PORT"] = input("IMAP port [993]: ") or "993"
        config["IMAP_USE_SSL"] = input("Use SSL? (true/false) [true]: ") or "true"
    
    # API configuration
    print("\n=== API EXPORT ===")
    api_enabled = input("Configure API export? (y/n) [n]: ").lower()
    if api_enabled == "y":
        config["API_ENDPOINT_URL"] = input("API endpoint URL: ").strip()
        config["API_BATCH_SIZE"] = input("Batch size [200]: ") or "200"
    
    # SMTP configuration
    print("\n=== EMAIL EXPORT (SMTP) ===")
    smtp_enabled = input("Configure email export? (y/n) [n]: ").lower()
    if smtp_enabled == "y":
        config["SMTP_HOST"] = input("SMTP host [smtp.gmail.com]: ") or "smtp.gmail.com"
        config["SMTP_PORT"] = input("SMTP port [587]: ") or "587"
    
    # Write .env file
    with open(env_file, "w") as f:
        f.write("# Flat File Scrubber Configuration\n")
        f.write("# ⚠️  DO NOT COMMIT THIS FILE TO GIT\n\n")
        for key, value in config.items():
            f.write(f"{key}={value}\n")
    
    print(f"\n✅ {env_file} created successfully!")
    print(f"⚠️  IMPORTANT: Never commit {env_file} to git!")


def setup_secrets_file():
    """Interactively create secrets.json file"""
    secrets_file = Path("secrets.json")
    secrets_example = Path("secrets.json.example")
    
    if secrets_file.exists():
        overwrite = input(f"\n⚠️  {secrets_file} already exists. Overwrite? (y/n): ").lower()
        if overwrite != "y":
            print("Skipping secrets.json setup")
            return
    
    if not secrets_example.exists():
        print(f"❌ {secrets_example} not found!")
        return
    
    print("\n🔐 Setting up secrets.json...")
    print("(Press Enter to skip optional fields)\n")
    
    secrets = {
        "imap": {},
        "api": {"headers": {}},
        "smtp": {},
        "snowflake": {}
    }
    
    # IMAP secrets
    print("=== EMAIL INTAKE CREDENTIALS (IMAP) ===")
    imap_email = input("Email address: ").strip()
    if imap_email:
        imap_password = getpass("App password (will not be echoed): ")
        secrets["imap"] = {
            "host": "imap.gmail.com",
            "port": 993,
            "use_ssl": True,
            "folder": "INBOX",
            "email": imap_email,
            "password": imap_password
        }
    
    # API secrets
    print("\n=== API CREDENTIALS ===")
    api_endpoint = input("API endpoint URL: ").strip()
    if api_endpoint:
        api_token = getpass("API token/key (will not be echoed): ")
        secrets["api"] = {
            "endpoint_url": api_endpoint,
            "method": "POST",
            "headers": {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json"
            },
            "batch_size": 200
        }
    
    # SMTP secrets
    print("\n=== EMAIL EXPORT CREDENTIALS (SMTP) ===")
    smtp_email = input("Email address: ").strip()
    if smtp_email:
        smtp_password = getpass("App password (will not be echoed): ")
        secrets["smtp"] = {
            "host": "smtp.gmail.com",
            "port": 587,
            "from_email": smtp_email,
            "app_password": smtp_password
        }
    
    # Snowflake secrets
    print("\n=== SNOWFLAKE CREDENTIALS ===")
    sf_account = input("Snowflake account ID: ").strip()
    if sf_account:
        sf_user = input("Snowflake user: ").strip()
        sf_password = getpass("Snowflake password (will not be echoed): ")
        sf_db = input("Database name: ").strip()
        sf_schema = input("Schema name: ").strip()
        sf_warehouse = input("Warehouse name: ").strip()
        
        secrets["snowflake"] = {
            "account": sf_account,
            "user": sf_user,
            "password": sf_password,
            "database": sf_db,
            "schema": sf_schema,
            "warehouse": sf_warehouse
        }
    
    # Write secrets.json with restricted permissions
    with open(secrets_file, "w") as f:
        json.dump(secrets, f, indent=2)
    
    # Restrict file permissions (Unix-like systems)
    try:
        os.chmod(secrets_file, 0o600)  # rw------- (user only)
    except:
        print("⚠️  Could not restrict file permissions. Set manually if on Windows.")
    
    print(f"\n✅ {secrets_file} created successfully!")
    print(f"📋 File permissions restricted to user-only (0600)")
    print(f"⚠️  IMPORTANT: Never commit {secrets_file} to git!")


def verify_setup():
    """Verify that sensitive files are properly gitignored"""
    print("\n🔍 Verifying security setup...\n")
    
    gitignore_file = Path(".gitignore")
    if not gitignore_file.exists():
        print("❌ .gitignore not found!")
        return False
    
    with open(gitignore_file, "r") as f:
        gitignore_content = f.read()
    
    checks = {
        "secrets.json": "secrets.json" in gitignore_content,
        ".env": ".env" in gitignore_content,
        "credentials/": "credentials/" in gitignore_content,
    }
    
    all_good = True
    for item, is_ignored in checks.items():
        status = "✅" if is_ignored else "❌"
        print(f"{status} {item} is {'gitignored' if is_ignored else 'NOT gitignored'}")
        if not is_ignored:
            all_good = False
    
    # Check for accidentally committed secrets
    env_file = Path(".env")
    secrets_file = Path("secrets.json")
    
    if env_file.exists() and not env_file.name.endswith(".example"):
        print(f"\n⚠️  WARNING: {env_file} exists in working directory")
        print("   This file should not be committed to git!")
    
    if secrets_file.exists():
        print(f"\n⚠️  WARNING: {secrets_file} exists in working directory")
        print("   This file should not be committed to git!")
    
    return all_good


def main():
    """Main setup wizard"""
    print("\n" + "="*60)
    print("🔐 Flat File Scrubber - Secrets Configuration Setup")
    print("="*60)
    print("\nThis script will help you safely configure your secrets.")
    print("All sensitive values will be stored in .gitignored files.\n")
    
    steps = [
        ("1", "Setup .env file", setup_env_file),
        ("2", "Setup secrets.json file", setup_secrets_file),
        ("3", "Verify security configuration", verify_setup),
    ]
    
    print("What would you like to do?\n")
    for key, desc, _ in steps:
        print(f"{key}) {desc}")
    print("4) Run all steps")
    print("q) Quit\n")
    
    choice = input("Enter your choice: ").lower().strip()
    
    if choice == "q":
        print("Exiting...")
        sys.exit(0)
    elif choice == "4":
        setup_env_file()
        setup_secrets_file()
        verify_setup()
    elif choice in ["1", "2", "3"]:
        for key, _, func in steps:
            if key == choice:
                func()
                break
    else:
        print("Invalid choice!")
        sys.exit(1)
    
    print("\n" + "="*60)
    print("✅ Setup complete!")
    print("="*60)
    print("\n📖 Next steps:")
    print("  1. Review your .env and secrets.json files")
    print("  2. Run: streamlit run flat_file_scrubber.py")
    print("  3. Or with Docker: docker-compose up -d\n")


if __name__ == "__main__":
    main()
