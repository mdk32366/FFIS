#!/bin/bash
# Start the Flat File Scrubber Streamlit app
# Usage: ./run.sh

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate virtual environment if not already active
if [ -z "$VIRTUAL_ENV" ]; then
    source .venv/bin/activate
fi

# Start the app
python -m streamlit run flat_file_scrubber.py
