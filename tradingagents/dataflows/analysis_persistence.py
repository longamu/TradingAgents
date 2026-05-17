"""Persist analysis results to SQLite and optionally send them via email.

Both features are opt-in and configured through the project's ``config``
dict (``DEFAULT_CONFIG`` in ``default_config.py``), with env-var overrides
via ``TRADINGAGENTS_*`` variables.
"""

from __future__ import annotations

import logging
import os
import smtplib
import socket
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


def _extract_smtp_domain(server: str) -> str:
    """Extract the registrable domain from an SMTP server hostname.

    ``smtp.qq.com`` → ``qq.com``, ``smtp.gmail.com`` → ``gmail.com``
    """
    parts = server.split(".")
    # Take at most the last two components for the Clash rule
    return ".".join(parts[-2:]) if len(parts) >= 2 else server


def _send_smtp(
    server: str, port: int, user: str, password: str,
    msg: EmailMessage, recipients: list[str],
    ticker: str, mail_to: str,
) -> None:
    """Send *msg* via SMTP, trying SSL first then STARTTLS.

    Port 465 is strongly associated with SMTP-over-SSL; the other common
    ports (587, 25) expect plain SMTP upgraded via STARTTLS.  Rather than
    rely on the user picking the *right* port, we try both strategies:
      1. SMTP_SSL (port 465 or explicit)
      2. SMTP + STARTTLS (ports 587/25)
    """
    last_exc: Exception | None = None

    # Detect Clash TUN fake IP (198.18.0.0/15 range) which means a local
    # proxy is intercepting all traffic and likely blocking SMTP.
    try:
        resolved = socket.gethostbyname(server)
        is_clash_fake = resolved.startswith("198.18.")
    except Exception:
        resolved = server
        is_clash_fake = False

    # Strategy 1 — SMTP_SSL (required for port 465)
    try:
        with smtplib.SMTP_SSL(server, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
        logger.info("Analysis email for %s sent to %s (SSL)", ticker, mail_to)
        return
    except Exception as exc:
        last_exc = exc

    # Strategy 2 — SMTP + STARTTLS (standard for ports 587, 25)
    try:
        with smtplib.SMTP(server, port, timeout=30) as s:
            s.ehlo()
            if s.has_extn("STARTTLS"):
                s.starttls()
                s.ehlo()
            s.login(user, password)
            s.send_message(msg)
        logger.info("Analysis email for %s sent to %s (STARTTLS)", ticker, mail_to)
        return
    except Exception as exc:
        last_exc = exc

    if is_clash_fake:
        msg_text = (
            f"{server} resolves to fake IP {resolved} (198.18.x.x) — "
            "Clash/V2Ray/TUN proxy is blocking the SMTP connection.\n"
            f"  Fix: add this rule to your Clash config:\n"
            f"    - DOMAIN-SUFFIX,{_extract_smtp_domain(server)},DIRECT\n"
            f"  Or: temporarily disable the proxy / TUN mode."
        )
        logger.error("%s\nUnderlying error: %s", msg_text, last_exc)
        raise RuntimeError(msg_text) from last_exc
    else:
        msg_text = (
            f"Failed to send email via {server}:{port} — "
            "tried SSL and STARTTLS. Error: %s" % last_exc
        )
        logger.error(msg_text)
        raise last_exc from last_exc  # type: ignore[misc]


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

    # Build email body (independent of config source)
    subject = f"[TradingAgents] Analysis Report: {ticker} — {decision}"
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
    msg.set_content(body)

    # Resolve SMTP settings: config dict (TRADINGAGENTS_SMTP_*) first,
    # then fall back to legacy SMTP_SERVER / SMTP_USERNAME / MAIL_TO env vars.
    smtp_server = cfg.get("smtp_server") or os.environ.get("SMTP_SERVER")
    smtp_port = int(cfg.get("smtp_port") or os.environ.get("SMTP_PORT", 587))
    smtp_user = cfg.get("smtp_username") or os.environ.get("SMTP_USERNAME")
    smtp_pass = cfg.get("smtp_password") or os.environ.get("SMTP_PASSWORD")
    mail_to = cfg.get("smtp_mail_to") or os.environ.get("MAIL_TO")
    mail_cc = cfg.get("smtp_mail_cc") or os.environ.get("MAIL_CC", "")

    if not (smtp_server and smtp_user and smtp_pass and mail_to):
        logger.debug("Email sending disabled — set SMTP_SERVER, SMTP_USERNAME, "
                      "SMTP_PASSWORD, and MAIL_TO env vars (or their "
                      "TRADINGAGENTS_SMTP_* equivalents)")
        return

    msg["From"] = smtp_user
    msg["To"] = mail_to
    if mail_cc:
        msg["Cc"] = mail_cc

    recipients = [mail_to] + ([c.strip() for c in mail_cc.split(",") if c.strip()] if mail_cc else [])

    _send_smtp(smtp_server, smtp_port, smtp_user, smtp_pass, msg, recipients, ticker, mail_to)
