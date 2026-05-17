"""Persist analysis results to SQLite and optionally send them via email.

Both features are opt-in. Database storage uses a default path under
``~/.tradingagents/analysis.db`` and can be disabled by setting the
env var ``TRADINGAGENTS_DB_PATH`` to an empty string.

Email sending is activated only when the required SMTP environment
variables (``SMTP_SERVER``, ``SMTP_USERNAME``, ``SMTP_PASSWORD``,
``MAIL_TO``) are all set.
"""

from __future__ import annotations

import logging
import os
import smtplib
import sqlite3
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── SQLite persistence ──────────────────────────────────────────────────


def _get_db_path() -> Optional[str]:
    """Return the DB path, or None if DB persistence is disabled."""
    raw = os.environ.get("TRADINGAGENTS_DB_PATH")
    if raw == "":
        return None  # explicitly disabled
    if raw:
        return raw
    # Default: ~/.tradingagents/analysis.db
    default = os.path.join(os.path.expanduser("~"), ".tradingagents", "analysis.db")
    return default


def _ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_results (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT    NOT NULL,
            analysis_date   TEXT    NOT NULL,
            decision        TEXT,
            llm_provider    TEXT,
            output_language TEXT,
            market_report   TEXT,
            sentiment_report TEXT,
            news_report     TEXT,
            fundamentals_report TEXT,
            research_manager_decision TEXT,
            trader_plan     TEXT,
            portfolio_decision TEXT,
            report_path     TEXT,
            full_report_json TEXT,
            created_at      TEXT    DEFAULT (datetime('now','localtime'))
        )
    """)
    conn.commit()


def save_analysis_to_db(
    ticker: str,
    analysis_date: str,
    final_state: dict,
    decision: str,
    report_path: Optional[str] = None,
    config: Optional[dict] = None,
) -> None:
    """Save analysis results into the local SQLite database.

    This is a best-effort helper — failures are logged but not propagated
    so they never interrupt the CLI flow.
    """
    db_path = _get_db_path()
    if db_path is None:
        logger.debug("DB persistence disabled (TRADINGAGENTS_DB_PATH is empty)")
        return

    # Extract nested state
    investment_debate = final_state.get("investment_debate_state") or {}
    risk_debate = final_state.get("risk_debate_state") or {}

    try:
        conn = sqlite3.connect(db_path)
        _ensure_table(conn)

        import json

        conn.execute(
            """
            INSERT INTO analysis_results
                (ticker, analysis_date, decision, llm_provider, output_language,
                 market_report, sentiment_report, news_report, fundamentals_report,
                 research_manager_decision, trader_plan, portfolio_decision,
                 report_path, full_report_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                analysis_date,
                decision,
                (config or {}).get("llm_provider"),
                (config or {}).get("output_language"),
                final_state.get("market_report"),
                final_state.get("sentiment_report"),
                final_state.get("news_report"),
                final_state.get("fundamentals_report"),
                investment_debate.get("judge_decision"),
                final_state.get("trader_investment_plan"),
                risk_debate.get("judge_decision"),
                report_path,
                json.dumps(final_state, default=str, ensure_ascii=False),
            ),
        )
        conn.commit()
        logger.info("Saved analysis for %s on %s to %s", ticker, analysis_date, db_path)
    except Exception:
        logger.exception("Failed to save analysis for %s to DB", ticker)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Email sending ───────────────────────────────────────────────────────


def _email_enabled() -> bool:
    """Check whether all required SMTP env vars are present."""
    required = ["SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO"]
    return all(os.environ.get(v) for v in required)


def send_analysis_email(
    ticker: str,
    analysis_date: str,
    decision: str,
    report_path: Optional[Path | str] = None,
    final_state: Optional[dict] = None,
) -> None:
    """Send analysis results via SMTP.

    Requires env vars: ``SMTP_SERVER``, ``SMTP_PORT`` (default 587),
    ``SMTP_USERNAME``, ``SMTP_PASSWORD``, ``MAIL_TO`` (recipient address).
    ``MAIL_CC`` (comma-separated) is optional.

    This is best-effort — failures are logged but not propagated.
    """
    if not _email_enabled():
        logger.debug("Email sending disabled — set SMTP_SERVER, SMTP_USERNAME, "
                      "SMTP_PASSWORD, and MAIL_TO to enable")
        return

    smtp_server = os.environ["SMTP_SERVER"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USERNAME"]
    smtp_pass = os.environ["SMTP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]
    mail_cc = os.environ.get("MAIL_CC", "")

    subject = f"[TradingAgents] Analysis Report: {ticker} — {decision}"

    # Build email body
    body_parts = [
        f"Ticker: {ticker}",
        f"Analysis Date: {analysis_date}",
        f"Decision: {decision}",
        "",
    ]
    if final_state:
        if final_state.get("market_report"):
            body_parts.append(f"Market Report:\n{final_state['market_report'][:2000]}\n")
        if final_state.get("sentiment_report"):
            body_parts.append(f"Sentiment Report:\n{final_state['sentiment_report'][:2000]}\n")
        if final_state.get("news_report"):
            body_parts.append(f"News Report:\n{final_state['news_report'][:2000]}\n")
        if final_state.get("fundamentals_report"):
            body_parts.append(f"Fundamentals Report:\n{final_state['fundamentals_report'][:2000]}\n")
    if report_path:
        body_parts.append(f"Full report: {report_path}")

    body = "\n".join(body_parts)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    if mail_cc:
        msg["Cc"] = mail_cc
    msg.set_content(body)

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            recipients = [mail_to] + ([c.strip() for c in mail_cc.split(",") if c.strip()] if mail_cc else [])
            server.send_message(msg)
        logger.info("Analysis email for %s sent to %s", ticker, mail_to)
    except Exception:
        logger.exception("Failed to send analysis email for %s", ticker)
