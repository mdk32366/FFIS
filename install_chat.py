#!/usr/bin/env python3
"""
install_chat.py — FFIS AI Assistant One-Shot Installer
=======================================================
Run this from the root of your FFIS repo:

    python install_chat.py

What it does:
  1. Writes ffis_chat.py (the full chat module) to the repo root
  2. Patches flat_file_scrubber.py  (import + tab label + tab body)
  3. Appends anthropic>=0.40.0 to requirements.txt
  4. Appends ANTHROPIC_API_KEY placeholder to .env.example
  5. Prints a summary and next-step instructions

Safe to re-run — it checks before writing and will not double-patch.

Author: Matthew Kelly / FFIS v2.0
"""

import os
import sys
from pathlib import Path

# ── Colour helpers (degrade gracefully on Windows without colorama) ───────────
def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

OK   = lambda t: print(_c("92", f"  ✅  {t}"))
SKIP = lambda t: print(_c("93", f"  ⏭   {t}"))
ERR  = lambda t: print(_c("91", f"  ❌  {t}"))
HDR  = lambda t: print(_c("96;1", f"\n{'─'*60}\n  {t}\n{'─'*60}"))
INFO = lambda t: print(f"       {t}")


# ════════════════════════════════════════════════════════════════════════════
#  STEP 1 — Write ffis_chat.py
# ════════════════════════════════════════════════════════════════════════════

FFIS_CHAT_SOURCE = r'''"""
ffis_chat.py — FFIS Conversational Agent Panel
=================================================
Drop-in Streamlit chat module for the Flat File Scrubber.

Embeds a Claude-powered assistant directly in the app that:
  • Reads live DataFrame state (row counts, columns, stage) on every turn
  • Exposes 14 callable tools mapped to the 11-step pipeline
  • Requires confirmation before any destructive operation
  • Keeps full conversation history for the session
  • Supports named rollback to any point in the undo stack

Author: Matthew Kelly — FFIS v2.0
"""

from __future__ import annotations

import json
import os
from typing import Any

import pandas as pd
import streamlit as st

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

MODEL            = "claude-haiku-4-5-20251001"
MAX_TOKENS       = 2048
CHAT_HISTORY_KEY = "ffis_chat_history"


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    state = _get_state_snapshot()
    return f"""You are FFIS Assistant — an expert data-steward AI embedded inside the
Flat File Scrubber (FFIS), a Salesforce / Snowflake CSV cleaning tool.

Your job is to guide users through the 11-step cleaning pipeline, answer questions
about data quality, and execute operations against the live DataFrames using tools.

══════════════════════════════════════════
CURRENT APPLICATION STATE
══════════════════════════════════════════
{json.dumps(state, indent=2, default=str)}

══════════════════════════════════════════
TOOL USAGE RULES
══════════════════════════════════════════
1. ALWAYS describe what you are about to do and ask for confirmation before calling
   any destructive tool (drop_columns, fill_nulls, drop_null_rows, drop_column,
   move_duplicates, move_incomplete, scrub_special_chars, cast_types, split_column,
   rollback).
2. Read-only tools (get_summary, get_column_info, get_sample_rows, get_undo_history)
   may be called freely without confirmation.
3. Before executing rollback, ALWAYS call get_undo_history first so the user
   can see exactly what state they are returning to.
4. After every tool call, report the result clearly and concisely.
5. If data has not been loaded yet, tell the user to go to the Ingest tab first.
6. Keep responses concise and actionable.

══════════════════════════════════════════
PIPELINE OVERVIEW
══════════════════════════════════════════
Step 1  Ingest        — Load CSV via upload, file path, or email
Step 2  Inspect       — Schema review, required-field check
Step 3  Drop/Rename   — Remove or rename columns
Step 4  Null Handling — Fill, signal-value, or drop nulls
Step 5  Types/Splits  — Cast dtypes, split columns by delimiter
Step 6  Special Chars — Scrub regex-matched characters
Step 7  Duplicates    — Detect & quarantine row duplicates
Step 8  Incomplete    — Quarantine records missing required fields
Step 9  SF Dupe Check — Compare against Salesforce/Snowflake reference
Step 10 Export        — CSV download, REST API POST, or SMTP email
Step 11 Data Frames   — Browse all 4 DataFrames, restore rows, undo
"""


# ── State snapshot ────────────────────────────────────────────────────────────

def _get_state_snapshot() -> dict[str, Any]:
    def _shape(df):
        if df is None or (isinstance(df, pd.DataFrame) and df.empty):
            return {"rows": 0, "cols": 0, "columns": []}
        return {"rows": len(df), "cols": len(df.columns), "columns": list(df.columns)}

    def _null_summary(df):
        if df is None or df.empty:
            return {}
        pct = (df.isnull().sum() * 100 / max(len(df), 1)).round(1)
        return {c: float(v) for c, v in pct.items() if v > 0}

    clean = st.session_state.get("clean_df")
    history = st.session_state.get("history", [])
    return {
        "data_loaded":     clean is not None,
        "job_name":        st.session_state.get("job_name", ""),
        "target_object":   st.session_state.get("object_type", ""),
        "workflow_step":   st.session_state.get("step", 0),
        "clean_df":        _shape(clean),
        "dupes_df":        _shape(st.session_state.get("dupes_df", pd.DataFrame())),
        "bad_df":          _shape(st.session_state.get("bad_df", pd.DataFrame())),
        "sf_dupes_df":     _shape(st.session_state.get("sf_dupes_df", pd.DataFrame())),
        "required_columns": st.session_state.get("required_cols", []),
        "null_pct":        _null_summary(clean),
        "undo_stack_depth": len(history),
        "undo_labels":     [label for label, _ in history],
    }


# ── Tool definitions ──────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_summary",
        "description": "Return a summary of the current DataFrame state.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_column_info",
        "description": "Return dtype, null count, unique count, and sample value for every column in the Clean DataFrame.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_sample_rows",
        "description": "Return the first N rows of the Clean DataFrame as a markdown table.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of rows (default 5, max 20).", "default": 5}
            },
            "required": [],
        },
    },
    {
        "name": "get_undo_history",
        "description": "List all operations in the undo stack (newest first) with row/col counts at each checkpoint. Always call this before rollback.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "rollback",
        "description": (
            "Roll back the Clean DataFrame to a previous checkpoint. "
            "Use 'steps' to undo the last N operations, or 'to_index' to jump "
            "to a specific stack position (0 = oldest). Always call get_undo_history first. DESTRUCTIVE."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "steps":    {"type": "integer", "description": "Number of steps to undo (default 1).", "default": 1},
                "to_index": {"type": "integer", "description": "Roll back to this stack index (overrides steps)."},
            },
            "required": [],
        },
    },
    {
        "name": "drop_columns",
        "description": "Drop one or more columns from the Clean DataFrame. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "columns": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["columns"],
        },
    },
    {
        "name": "rename_column",
        "description": "Rename a single column in the Clean DataFrame.",
        "input_schema": {
            "type": "object",
            "properties": {
                "old_name": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["old_name", "new_name"],
        },
    },
    {
        "name": "fill_nulls",
        "description": "Fill null values in a column with a signal value or statistic. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "column": {"type": "string"},
                "method": {"type": "string", "enum": ["value", "mean", "median", "mode"]},
                "value":  {"type": "string", "description": "Required when method='value'."},
            },
            "required": ["column", "method"],
        },
    },
    {
        "name": "drop_null_rows",
        "description": "Drop all rows where the specified column is null. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {"column": {"type": "string"}},
            "required": ["column"],
        },
    },
    {
        "name": "drop_column",
        "description": "Drop a single column entirely. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {"column": {"type": "string"}},
            "required": ["column"],
        },
    },
    {
        "name": "scrub_special_chars",
        "description": "Remove or replace special characters from text columns using a regex pattern. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "columns":     {"type": "array", "items": {"type": "string"}},
                "pattern":     {"type": "string", "description": "Regex pattern. Defaults to app COMMON_SPECIAL_CHARS if omitted."},
                "replacement": {"type": "string", "description": "Replacement string (empty = remove).", "default": ""},
            },
            "required": ["columns"],
        },
    },
    {
        "name": "move_duplicates",
        "description": "Move duplicate rows to the Dupes DataFrame. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subset": {"type": "array", "items": {"type": "string"}, "description": "Columns to check (empty = all)."},
                "keep":   {"type": "string", "enum": ["first", "last", "none"], "default": "first"},
            },
            "required": [],
        },
    },
    {
        "name": "move_incomplete",
        "description": "Move records missing required fields to the Bad DataFrame. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "required_columns": {"type": "array", "items": {"type": "string"}, "description": "Defaults to session required_cols."},
            },
            "required": [],
        },
    },
    {
        "name": "cast_types",
        "description": "Cast columns to new data types. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "casts": {
                    "type": "object",
                    "description": "Mapping column_name → target_type. Types: object, int64, float64, bool, datetime64[ns], category, string.",
                    "additionalProperties": {"type": "string"},
                }
            },
            "required": ["casts"],
        },
    },
    {
        "name": "split_column",
        "description": "Split a column into two new columns by a delimiter. DESTRUCTIVE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "column":        {"type": "string"},
                "delimiter":     {"type": "string"},
                "new_col_a":     {"type": "string"},
                "new_col_b":     {"type": "string"},
                "keep_original": {"type": "boolean", "default": False},
            },
            "required": ["column", "delimiter", "new_col_a", "new_col_b"],
        },
    },
]


# ── Undo helper ───────────────────────────────────────────────────────────────

def _push_history(label: str):
    cdf = st.session_state.get("clean_df")
    if cdf is not None:
        history = st.session_state.get("history", [])
        history.append((label, cdf.copy()))
        try:
            from config import get_undo_history_limit
            limit = get_undo_history_limit()
        except Exception:
            limit = 20
        if len(history) > limit:
            history.pop(0)
        st.session_state.history = history


# ── Tool executor ─────────────────────────────────────────────────────────────

def _execute_tool(name: str, inputs: dict) -> str:
    cdf: pd.DataFrame | None = st.session_state.get("clean_df")

    # ── Read-only ────────────────────────────────────────────────────────────
    if name == "get_summary":
        return json.dumps(_get_state_snapshot(), indent=2, default=str)

    if name == "get_column_info":
        if cdf is None:
            return "No data loaded."
        rows = []
        for col in cdf.columns:
            null_n   = int(cdf[col].isna().sum())
            null_pct = round(null_n / max(len(cdf), 1) * 100, 1)
            sample   = str(cdf[col].dropna().iloc[0]) if cdf[col].notna().any() else "—"
            rows.append(f"| {col} | {cdf[col].dtype} | {null_n} ({null_pct}%) | {cdf[col].nunique()} | {sample[:60]} |")
        return "| Column | Dtype | Nulls | Unique | Sample |\n|---|---|---|---|---|\n" + "\n".join(rows)

    if name == "get_sample_rows":
        if cdf is None:
            return "No data loaded."
        n = min(int(inputs.get("n", 5)), 20)
        return cdf.head(n).to_markdown(index=False)

    if name == "get_undo_history":
        history = st.session_state.get("history", [])
        if not history:
            return "The undo stack is empty — no operations to roll back."
        lines = []
        for i, (label, df) in enumerate(reversed(history)):
            pos = len(history) - 1 - i
            lines.append(f"[{pos}] {label}  ({len(df):,} rows × {len(df.columns)} cols)")
        return (
            f"Undo stack ({len(history)} operation(s), newest first):\n"
            + "\n".join(lines)
            + "\n\nTo roll back, tell me how many steps or give me an index number."
        )

    # ── Rollback ─────────────────────────────────────────────────────────────
    if name == "rollback":
        history = st.session_state.get("history", [])
        if not history:
            return "The undo stack is empty — nothing to roll back."

        to_index = inputs.get("to_index")
        steps    = int(inputs.get("steps", 1))

        if to_index is not None:
            to_index = int(to_index)
            if to_index < 0 or to_index >= len(history):
                return f"Error: index {to_index} out of range (stack has {len(history)} entries, 0–{len(history)-1})."
            label, target_df = history[to_index]
            st.session_state.clean_df = target_df.copy()
            removed = len(history) - to_index
            st.session_state.history = history[:to_index]
            return (
                f"✅ Rolled back to before '{label}'. "
                f"Clean DataFrame restored to {len(target_df):,} rows × {len(target_df.columns)} cols. "
                f"Removed {removed} operation(s) from the undo stack."
            )
        else:
            steps = min(steps, len(history))
            label, target_df = history[-steps]
            st.session_state.clean_df = target_df.copy()
            st.session_state.history  = history[:-steps]
            return (
                f"✅ Rolled back {steps} step(s) to before '{label}'. "
                f"Clean DataFrame restored to {len(target_df):,} rows × {len(target_df.columns)} cols."
            )

    # ── Guard: data must be loaded for all remaining tools ───────────────────
    if cdf is None:
        return "Error: No data loaded. Ask the user to ingest a file first."

    # ── Destructive tools ────────────────────────────────────────────────────
    if name == "drop_columns":
        cols    = inputs.get("columns", [])
        missing = [c for c in cols if c not in cdf.columns]
        if missing:
            return f"Error: Columns not found: {missing}"
        _push_history(f"Chat: drop columns {cols}")
        st.session_state.clean_df = cdf.drop(columns=cols)
        return f"✅ Dropped {len(cols)} column(s): {cols}. Clean now has {len(st.session_state.clean_df.columns)} columns."

    if name == "rename_column":
        old, new = inputs["old_name"], inputs["new_name"]
        if old not in cdf.columns:
            return f"Error: Column '{old}' not found."
        _push_history(f"Chat: rename {old} → {new}")
        st.session_state.clean_df = cdf.rename(columns={old: new})
        return f"✅ Renamed '{old}' → '{new}'."

    if name == "fill_nulls":
        col, method = inputs["column"], inputs["method"]
        if col not in cdf.columns:
            return f"Error: Column '{col}' not found."
        _push_history(f"Chat: fill nulls {col} ({method})")
        df2 = cdf.copy()
        if method == "value":
            raw = inputs.get("value", "")
            try:
                val: Any = float(raw) if "." in str(raw) else int(raw)
            except (ValueError, TypeError):
                val = raw
            df2[col] = df2[col].fillna(val)
            msg = f"filled with {val!r}"
        elif method == "mean":
            df2[col] = df2[col].fillna(df2[col].mean());   msg = "filled with mean"
        elif method == "median":
            df2[col] = df2[col].fillna(df2[col].median()); msg = "filled with median"
        elif method == "mode":
            df2[col] = df2[col].fillna(df2[col].mode()[0]); msg = "filled with mode"
        else:
            return f"Error: Unknown method '{method}'."
        st.session_state.clean_df = df2
        return f"✅ Nulls in '{col}' {msg}. Remaining nulls: {int(df2[col].isna().sum())}."

    if name == "drop_null_rows":
        col = inputs["column"]
        if col not in cdf.columns:
            return f"Error: Column '{col}' not found."
        _push_history(f"Chat: drop null rows {col}")
        before = len(cdf)
        df2 = cdf.dropna(subset=[col]).reset_index(drop=True)
        st.session_state.clean_df = df2
        return f"✅ Dropped {before - len(df2):,} rows where '{col}' was null. Clean: {len(df2):,} rows."

    if name == "drop_column":
        col = inputs["column"]
        if col not in cdf.columns:
            return f"Error: Column '{col}' not found."
        _push_history(f"Chat: drop column {col}")
        st.session_state.clean_df = cdf.drop(columns=[col])
        return f"✅ Dropped column '{col}'."

    if name == "scrub_special_chars":
        try:
            from config import get_special_chars_pattern
            default_pattern = get_special_chars_pattern()
        except Exception:
            default_pattern = r"[\*\n\^\$\#\@\!\%\&\(\)\[\]\{\}\<\>\?\/\\|`~\"\';\:]"
        cols        = inputs.get("columns", [])
        pattern     = inputs.get("pattern") or default_pattern
        replacement = inputs.get("replacement", "")
        missing = [c for c in cols if c not in cdf.columns]
        if missing:
            return f"Error: Columns not found: {missing}"
        _push_history(f"Chat: scrub special chars {cols}")
        df2 = cdf.copy()
        total = 0
        for col in cols:
            if df2[col].dtype == object:
                hits = int(df2[col].astype(str).str.contains(pattern, regex=True).sum())
                df2[col] = df2[col].astype(str).str.replace(pattern, replacement, regex=True)
                total += hits
        st.session_state.clean_df = df2
        return f"✅ Scrubbed special characters from {total:,} cell(s) across {len(cols)} column(s)."

    if name == "move_duplicates":
        subset   = inputs.get("subset") or None
        keep_opt = inputs.get("keep", "first")
        keep_val: str | bool = keep_opt if keep_opt in ("first", "last") else False
        _push_history("Chat: move duplicates")
        dupe_mask = cdf.duplicated(subset=subset, keep=keep_val)
        dupe_rows = cdf[dupe_mask].copy()
        st.session_state.dupes_df = pd.concat(
            [st.session_state.get("dupes_df", pd.DataFrame()), dupe_rows], ignore_index=True
        )
        st.session_state.clean_df = cdf[~dupe_mask].reset_index(drop=True)
        return (
            f"✅ Moved {len(dupe_rows):,} duplicate rows to Dupes DataFrame. "
            f"Clean: {len(st.session_state.clean_df):,} rows."
        )

    if name == "move_incomplete":
        req_cols = inputs.get("required_columns") or st.session_state.get("required_cols", [])
        if not req_cols:
            return "Error: No required columns defined. Set them in the Inspect tab or pass them explicitly."
        present      = [c for c in req_cols if c in cdf.columns]
        missing_cols = [c for c in req_cols if c not in cdf.columns]
        if missing_cols:
            return f"Error: Required columns not in DataFrame: {missing_cols}"
        _push_history("Chat: move incomplete")
        mask     = cdf[present].isnull().any(axis=1)
        bad_rows = cdf[mask].copy()
        st.session_state.bad_df = pd.concat(
            [st.session_state.get("bad_df", pd.DataFrame()), bad_rows], ignore_index=True
        )
        st.session_state.clean_df = cdf[~mask].reset_index(drop=True)
        return (
            f"✅ Moved {len(bad_rows):,} incomplete rows to Bad DataFrame. "
            f"Clean: {len(st.session_state.clean_df):,} rows."
        )

    if name == "cast_types":
        casts = inputs.get("casts", {})
        _push_history(f"Chat: cast types {list(casts.keys())}")
        df2    = cdf.copy()
        errors = []
        success = []
        for col, new_type in casts.items():
            if col not in df2.columns:
                errors.append(f"'{col}' not found"); continue
            try:
                if new_type == "datetime64[ns]":
                    df2[col] = pd.to_datetime(df2[col])
                else:
                    df2[col] = df2[col].astype(new_type)
                success.append(col)
            except Exception as e:
                errors.append(f"'{col}': {e}")
        st.session_state.clean_df = df2
        msg = f"✅ Cast {len(success)} column(s): {success}."
        if errors:
            msg += f" Errors: {errors}"
        return msg

    if name == "split_column":
        col      = inputs["column"]
        delim    = inputs["delimiter"]
        col_a    = inputs["new_col_a"]
        col_b    = inputs["new_col_b"]
        keep_orig = inputs.get("keep_original", False)
        if col not in cdf.columns:
            return f"Error: Column '{col}' not found."
        _push_history(f"Chat: split {col}")
        df2 = cdf.copy()
        try:
            split_result = df2[col].str.split(delim, expand=True)
            df2[col_a]   = split_result[0]
            df2[col_b]   = split_result[1] if split_result.shape[1] > 1 else ""
            if not keep_orig:
                df2 = df2.drop(columns=[col])
            st.session_state.clean_df = df2
            return f"✅ Split '{col}' → '{col_a}' + '{col_b}'."
        except Exception as e:
            return f"Error splitting column: {e}"

    return f"Error: Unknown tool '{name}'."


# ── Anthropic agentic loop ────────────────────────────────────────────────────

def _run_agent(user_message: str) -> str:
    client   = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    history  = st.session_state.get(CHAT_HISTORY_KEY, [])
    messages = history + [{"role": "user", "content": user_message}]

    for _ in range(10):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_build_system_prompt(),
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return "\n\n".join(b.text for b in response.content if hasattr(b, "text"))

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result_text = _execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue
        break

    return "⚠️ Agent loop ended without a text response. Please try again."


# ── Streamlit UI ──────────────────────────────────────────────────────────────

def render_chat_tab():
    """Render the AI Assistant tab. Call inside `with tabs[N]:` in flat_file_scrubber.py."""

    if not _ANTHROPIC_AVAILABLE:
        st.error("The `anthropic` package is not installed. Run `pip install anthropic` and restart.")
        return

    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        st.warning("**ANTHROPIC_API_KEY is not set.** Add it to your `.env` file, then restart.")
        st.code("ANTHROPIC_API_KEY=sk-ant-...", language="bash")
        return

    st.markdown('<div class="section-header">🤖 FFIS AI Assistant</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="info-box">'
        "Chat with your data. The assistant reads your live DataFrames, answers data quality "
        "questions, executes cleaning operations on request, and can roll back any step — "
        "always asking for confirmation before anything destructive."
        "</div>",
        unsafe_allow_html=True,
    )

    snap = _get_state_snapshot()
    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("✅ Clean",    snap["clean_df"]["rows"])
    sc2.metric("🔁 Dupes",   snap["dupes_df"]["rows"])
    sc3.metric("❌ Bad",     snap["bad_df"]["rows"])
    sc4.metric("☁️ SF Dupes", snap["sf_dupes_df"]["rows"])

    st.divider()

    if CHAT_HISTORY_KEY not in st.session_state:
        st.session_state[CHAT_HISTORY_KEY] = []

    # Render existing messages
    for msg in st.session_state[CHAT_HISTORY_KEY]:
        role = msg["role"]
        if isinstance(msg["content"], str):
            text = msg["content"]
        else:
            text = "\n\n".join(
                b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
                for b in msg["content"]
            )
        if text.strip():
            with st.chat_message(role):
                st.markdown(text)

    # Quick-action buttons on first load
    if not st.session_state[CHAT_HISTORY_KEY]:
        st.markdown("**Quick actions — click to start:**")
        q1, q2, q3 = st.columns(3)
        quick_prompts = {
            "📊 Summarise my data": (
                "Give me a summary of the current data: row counts, column overview, "
                "and any data quality issues I should address before loading to Salesforce."
            ),
            "🔍 Find data issues": (
                "Review my dataset and identify the top data quality issues I should "
                "fix before loading to Salesforce."
            ),
            "🔄 Show undo history": (
                "Show me my undo stack so I can see what operations I can roll back."
            ),
        }
        for col, (label, prompt) in zip([q1, q2, q3], quick_prompts.items()):
            if col.button(label, use_container_width=True):
                st.session_state["_quick_prompt"] = prompt
                st.rerun()

    if "_quick_prompt" in st.session_state:
        _submit_message(st.session_state.pop("_quick_prompt"))
        st.rerun()

    user_input = st.chat_input("Ask about your data, request an operation, or say 'show undo history'…")
    if user_input:
        _submit_message(user_input)
        st.rerun()

    with st.sidebar:
        st.divider()
        st.markdown("**🤖 AI Assistant**")
        if st.button("🗑 Clear chat history", key="clear_chat_btn"):
            st.session_state[CHAT_HISTORY_KEY] = []
            st.rerun()
        depth = len(st.session_state.get(CHAT_HISTORY_KEY, []))
        st.caption(f"Model: `{MODEL}`")
        st.caption(f"Messages in context: {depth}")
        st.caption(f"Undo stack depth: {len(st.session_state.get('history', []))}")


def _submit_message(user_text: str):
    with st.chat_message("user"):
        st.markdown(user_text)
    history = st.session_state.get(CHAT_HISTORY_KEY, [])
    history.append({"role": "user", "content": user_text})
    with st.chat_message("assistant"):
        with st.spinner("Thinking…"):
            reply = _run_agent(user_text)
        st.markdown(reply)
    history.append({"role": "assistant", "content": reply})
    st.session_state[CHAT_HISTORY_KEY] = history
'''


# ════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════

def _already_contains(path: Path, marker: str) -> bool:
    """Return True if the file already contains the marker string."""
    try:
        return marker in path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False


def _patch_tabs_list(source: str) -> str:
    """
    Insert '"🤖 AI Assistant",' as the last entry in the st.tabs([...]) call.
    Handles any whitespace / trailing comma style.
    """
    # Find the closing bracket of the tabs list and insert before it.
    # We look for the last item before the closing ]) of the tabs call.
    marker = '"📋 Data Frames"'
    if '"🤖 AI Assistant"' in source:
        return source  # already patched

    # Strategy: find the last occurrence of the Data Frames tab entry and
    # insert our new tab after it (before the closing bracket).
    idx = source.rfind(marker)
    if idx == -1:
        return None  # couldn't find insertion point

    # Find the end of that line
    end_of_line = source.find("\n", idx)
    if end_of_line == -1:
        end_of_line = len(source)

    # Build the insertion — match the indentation of the line we found
    line_start = source.rfind("\n", 0, idx) + 1
    indent = ""
    for ch in source[line_start:]:
        if ch in (" ", "\t"):
            indent += ch
        else:
            break

    insert = f'\n{indent}"🤖 AI Assistant",'
    return source[:end_of_line] + insert + source[end_of_line:]


# ════════════════════════════════════════════════════════════════════════════
#  Main installer
# ════════════════════════════════════════════════════════════════════════════

def main():
    repo_root = Path(__file__).parent
    scrubber  = repo_root / "flat_file_scrubber.py"
    chat_file = repo_root / "ffis_chat.py"
    req_file  = repo_root / "requirements.txt"
    env_ex    = repo_root / ".env.example"

    print(_c("96;1", "\n🤖  FFIS AI Assistant Installer"))
    print(_c("90", "    Flat File Scrubber — v2.0\n"))

    # ── Pre-flight checks ────────────────────────────────────────────────────
    HDR("Pre-flight checks")

    if not scrubber.exists():
        ERR(f"flat_file_scrubber.py not found at {repo_root}")
        ERR("Run this script from the root of your FFIS repository.")
        sys.exit(1)
    OK(f"Found flat_file_scrubber.py  ({scrubber.stat().st_size // 1024} KB)")

    if not req_file.exists():
        ERR("requirements.txt not found.")
        sys.exit(1)
    OK("Found requirements.txt")

    # ── Step 1: Write ffis_chat.py ───────────────────────────────────────────
    HDR("Step 1 — Write ffis_chat.py")

    if chat_file.exists():
        SKIP("ffis_chat.py already exists — overwriting with latest version")
    chat_file.write_text(FFIS_CHAT_SOURCE, encoding="utf-8")
    OK(f"Written ffis_chat.py  ({chat_file.stat().st_size // 1024} KB)")

    # ── Step 2: Patch flat_file_scrubber.py ──────────────────────────────────
    HDR("Step 2 — Patch flat_file_scrubber.py")

    source = scrubber.read_text(encoding="utf-8")
    changed = False

    # 2a — import
    import_line = "from ffis_chat import render_chat_tab"
    if import_line in source:
        SKIP("Import already present")
    else:
        # Insert after the last 'from config import' block
        anchor = "get_smtp_config,\n)"
        if anchor in source:
            source = source.replace(anchor, anchor + f"\n{import_line}\n", 1)
            OK("Added import: from ffis_chat import render_chat_tab")
            changed = True
        else:
            # Fallback: insert after the last existing import block
            lines = source.splitlines(keepends=True)
            last_import_idx = 0
            for i, line in enumerate(lines):
                if line.startswith(("import ", "from ")):
                    last_import_idx = i
            lines.insert(last_import_idx + 1, import_line + "\n")
            source = "".join(lines)
            OK("Added import (fallback position)")
            changed = True

    # 2b — tab label
    if '"🤖 AI Assistant"' in source:
        SKIP("Tab label already present")
    else:
        patched = _patch_tabs_list(source)
        if patched is None:
            ERR('Could not find "📋 Data Frames" in tabs list — add tab label manually.')
            INFO('  Add   "🤖 AI Assistant",')
            INFO('  after "📋 Data Frames",  in the st.tabs([...]) call.')
        else:
            source = patched
            OK('Added "🤖 AI Assistant" tab label to st.tabs([...])')
            changed = True

    # 2c — tab body
    tab_body = "\n# ══════════════════════════════════════════════\n# TAB 11 — AI ASSISTANT (Chat)\n# ══════════════════════════════════════════════\n\nwith tabs[11]:\n    render_chat_tab()\n"
    if "render_chat_tab()" in source:
        SKIP("Tab body (render_chat_tab) already present")
    else:
        source += tab_body
        OK("Appended tab body: with tabs[11]: render_chat_tab()")
        changed = True

    if changed:
        # Back up original before writing
        backup = scrubber.with_suffix(".py.bak")
        backup.write_text(scrubber.read_text(encoding="utf-8"), encoding="utf-8")
        INFO(f"Backup saved → {backup.name}")
        scrubber.write_text(source, encoding="utf-8")
        OK("flat_file_scrubber.py updated")
    else:
        SKIP("flat_file_scrubber.py — no changes needed")

    # ── Step 3: requirements.txt ─────────────────────────────────────────────
    HDR("Step 3 — Update requirements.txt")

    req_text   = req_file.read_text(encoding="utf-8")
    req_marker = "anthropic"
    if req_marker in req_text:
        SKIP("anthropic already in requirements.txt")
    else:
        req_file.write_text(req_text.rstrip() + "\nanthropic>=0.40.0\n", encoding="utf-8")
        OK("Added anthropic>=0.40.0 to requirements.txt")

    # ── Step 4: .env.example ─────────────────────────────────────────────────
    HDR("Step 4 — Update .env.example")

    env_block = (
        "\n# ──────────────────────────────────────────────\n"
        "# AI ASSISTANT (Chat tab)\n"
        "# ──────────────────────────────────────────────\n"
        "ANTHROPIC_API_KEY=sk-ant-your-key-here\n"
    )
    if env_ex.exists():
        env_text = env_ex.read_text(encoding="utf-8")
        if "ANTHROPIC_API_KEY" in env_text:
            SKIP("ANTHROPIC_API_KEY already in .env.example")
        else:
            env_ex.write_text(env_text.rstrip() + env_block, encoding="utf-8")
            OK("Added ANTHROPIC_API_KEY to .env.example")
    else:
        SKIP(".env.example not found — skipping")

    # ── Done ─────────────────────────────────────────────────────────────────
    HDR("✅  Installation complete")

    print(_c("92", "  Next steps:\n"))
    print(_c("97",  "  1. Add your key to .env:"))
    print(_c("90",  "       ANTHROPIC_API_KEY=sk-ant-...\n"))
    print(_c("97",  "  2. Install the new dependency:"))
    print(_c("90",  "       pip install anthropic>=0.40.0\n"))
    print(_c("97",  "  3. Restart the app:"))
    print(_c("90",  "       streamlit run flat_file_scrubber.py\n"))
    print(_c("97",  "  4. Commit and push:"))
    print(_c("90",  '       git add . && git commit -m "feat: add AI chat assistant tab" && git push\n'))
    print(_c("93",  "  The 🤖 AI Assistant tab will appear as the 12th tab in the app.\n"))


if __name__ == "__main__":
    main()
