"""Persist analysis results to SQLite and optionally send them via email.

Both features are opt-in and configured through the project's ``config``
dict (``DEFAULT_CONFIG`` in ``default_config.py``), with env-var overrides
via ``TRADINGAGENTS_*`` variables.
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


def _get_db_path(config: dict) -> Optional[str]:
    """Return the DB path, or None if DB persistence is disabled."""
    raw = config.get("data_cache_dir")
    if raw:
        base = raw
    else:
        base = os.path.join(os.path.expanduser("~"), ".tradingagents")
    return os.path.join(base, "analysis.db")


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
    db_path = _get_db_path(config or {})

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


def _email_enabled(config: dict) -> bool:
    """Check whether all required SMTP config keys are present."""
    return bool(config.get("smtp_server") and config.get("smtp_username")
                and config.get("smtp_password") and config.get("smtp_mail_to"))


def send_analysis_email(
    ticker: str,
    analysis_date: str,
    decision: str,
    report_path: Optional[Path | str] = None,
    final_state: Optional[dict] = None,
    config: Optional[dict] = None,
) -> None:
    """Send analysis results via SMTP.

    Reads SMTP settings from the ``config`` dict (keys ``smtp_server``,
    ``smtp_port``, ``smtp_username``, ``smtp_password``, ``smtp_mail_to``,
    ``smtp_mail_cc``), which are populated from ``DEFAULT_CONFIG`` and
    overridable via ``TRADINGAGENTS_SMTP_*`` env vars.

    This is best-effort — failures are logged but not propagated.
    """
    cfg = config or {}
    if not _email_enabled(cfg):
        logger.debug("Email sending disabled — set smtp_server, smtp_username, "
                      "smtp_password, and smtp_mail_to in config (or their "
                      "TRADINGAGENTS_SMTP_* env vars) to enable")
        return

    smtp_server = cfg["smtp_server"]
    smtp_port = int(cfg.get("smtp_port", 587))
    smtp_user = cfg["smtp_username"]
    smtp_pass = cfg["smtp_password"]
    mail_to = cfg["smtp_mail_to"]
    mail_cc = cfg.get("smtp_mail_cc") or ""

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
