"""Persist analysis results to SQLite and optionally send them via email.

Both features are opt-in and configured through the project's ``config``
dict (``DEFAULT_CONFIG`` in ``default_config.py``), with env-var overrides
via ``TRADINGAGENTS_*`` variables.
"""

from __future__ import annotations

import datetime
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


def _get_db_path(config: dict) -> str:
    """Return the DB path under the cache directory."""
    base = config.get("data_cache_dir") or os.path.join(
        os.path.expanduser("~"), ".tradingagents"
    )
    return os.path.join(base, "analysis.db")


def _get_conn(config: dict) -> sqlite3.Connection:
    path = _get_db_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_tables(conn: sqlite3.Connection):
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_emails (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT    NOT NULL,
            analysis_date   TEXT    NOT NULL,
            decision        TEXT,
            report_body     TEXT,
            report_path     TEXT,
            smtp_server     TEXT,
            smtp_port       INTEGER DEFAULT 587,
            smtp_user       TEXT,
            mail_to         TEXT,
            mail_cc         TEXT,
            retry_count     INTEGER DEFAULT 0,
            max_retries     INTEGER DEFAULT 3,
            last_error      TEXT,
            next_retry_at   TEXT,
            created_at      TEXT    DEFAULT (datetime('now','localtime')),
            updated_at      TEXT    DEFAULT (datetime('now','localtime'))
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
    cfg = config or {}
    try:
        conn = _get_conn(cfg)
        _ensure_tables(conn)
    except Exception:
        logger.exception("Failed to open DB for %s", ticker)
        return

    investment_debate = final_state.get("investment_debate_state") or {}
    risk_debate = final_state.get("risk_debate_state") or {}

    try:
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
                cfg.get("llm_provider"),
                cfg.get("output_language"),
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
        logger.info("Saved analysis for %s on %s to %s", ticker, analysis_date, _get_db_path(cfg))
    except Exception:
        logger.exception("Failed to save analysis for %s to DB", ticker)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Email sending ───────────────────────────────────────────────────────


def _extract_smtp_domain(server: str) -> str:
    """Extract the registrable domain from an SMTP server hostname."""
    parts = server.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else server


def _send_smtp(
    server: str, port: int, user: str, password: str,
    msg: EmailMessage, recipients: list[str],
    ticker: str, mail_to: str,
) -> tuple[bool, str]:
    """Send *msg* via SMTP, trying SSL first then STARTTLS.

    Returns ``(True, "")`` on success or ``(False, error_message)`` on failure.
    """
    last_error = ""

    # Strategy 1 — SMTP_SSL
    try:
        with smtplib.SMTP_SSL(server, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
        logger.info("Analysis email for %s sent to %s (SSL)", ticker, mail_to)
        return True, ""
    except Exception as exc:
        last_error = f"SSL({type(exc).__name__}: {exc})"

    # Strategy 2 — SMTP + STARTTLS
    try:
        with smtplib.SMTP(server, port, timeout=30) as s:
            s.ehlo()
            if s.has_extn("STARTTLS"):
                s.starttls()
                s.ehlo()
            s.login(user, password)
            s.send_message(msg)
        logger.info("Analysis email for %s sent to %s (STARTTLS)", ticker, mail_to)
        return True, ""
    except Exception as exc:
        last_error += f"; STARTTLS({type(exc).__name__}: {exc})"

    # Detect Clash TUN fake IP for a helpful hint
    try:
        resolved = socket.gethostbyname(server)
        if resolved.startswith("198.18."):
            return False, (
                f"{server} resolves to fake IP {resolved} (198.18.x.x) — "
                "Clash/V2Ray/TUN proxy is blocking SMTP.\n"
                f"  Fix: add to Clash rules: DOMAIN-SUFFIX,{_extract_smtp_domain(server)},DIRECT"
            )
    except Exception:
        pass

    return False, (
        f"Tried SSL and STARTTLS on {server}:{port}, both failed. "
        f"Errors: {last_error}"
    )


def _save_failed_email(
    conn: sqlite3.Connection,
    ticker: str, analysis_date: str, decision: str,
    report_body: str, report_path: Optional[str],
    smtp_server: str, smtp_port: int, smtp_user: str,
    mail_to: str, mail_cc: str, error: str,
):
    """Insert a failed email record for later retry."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # First retry in 5 minutes
    next_retry = (
        datetime.datetime.now() + datetime.timedelta(minutes=5)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        INSERT INTO failed_emails
            (ticker, analysis_date, decision, report_body, report_path,
             smtp_server, smtp_port, smtp_user, mail_to, mail_cc,
             last_error, next_retry_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker, analysis_date, decision, report_body, report_path,
            smtp_server, smtp_port, smtp_user, mail_to, mail_cc,
            error, next_retry, now, now,
        ),
    )
    conn.commit()


def send_analysis_email(
    ticker: str,
    analysis_date: str,
    decision: str,
    report_path: Optional[Path | str] = None,
    final_state: Optional[dict] = None,
    config: Optional[dict] = None,
) -> bool:
    """Send analysis results via SMTP.

    Returns ``True`` if sent successfully, ``False`` if skipped or failed.
    On failure, the failed email is queued into the local SQLite database
    for later retry via :func:`retry_failed_emails`.
    """
    cfg = config or {}

    # Build email body
    subject = f"[TradingAgents] Analysis Report: {ticker} — {decision}"
    body_parts = [
        f"Ticker: {ticker}",
        f"Analysis Date: {analysis_date}",
        f"Decision: {decision}",
        "",
    ]
    if final_state:
        for key, label in [
            ("market_report", "Market Report"),
            ("sentiment_report", "Sentiment Report"),
            ("news_report", "News Report"),
            ("fundamentals_report", "Fundamentals Report"),
        ]:
            text = final_state.get(key)
            if text:
                body_parts.append(f"{label}:\n{text[:2000]}\n")
    if report_path:
        body_parts.append(f"Full report: {report_path}")
    body = "\n".join(body_parts)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg.set_content(body)

    # Resolve SMTP settings
    smtp_server = cfg.get("smtp_server") or os.environ.get("SMTP_SERVER")
    smtp_port = int(cfg.get("smtp_port") or os.environ.get("SMTP_PORT", 587))
    smtp_user = cfg.get("smtp_username") or os.environ.get("SMTP_USERNAME")
    smtp_pass = cfg.get("smtp_password") or os.environ.get("SMTP_PASSWORD")
    mail_to = cfg.get("smtp_mail_to") or os.environ.get("MAIL_TO")
    mail_cc = cfg.get("smtp_mail_cc") or os.environ.get("MAIL_CC", "")

    if not (smtp_server and smtp_user and smtp_pass and mail_to):
        logger.debug("Email disabled — SMTP config incomplete")
        return False

    msg["From"] = smtp_user
    msg["To"] = mail_to
    if mail_cc:
        msg["Cc"] = mail_cc
    recipients = [mail_to] + ([c.strip() for c in mail_cc.split(",") if c.strip()] if mail_cc else [])

    # Try to send
    ok, error = _send_smtp(
        smtp_server, smtp_port, smtp_user, smtp_pass, msg, recipients, ticker, mail_to
    )
    if ok:
        return True

    # On failure, queue into DB for later retry
    logger.warning("Email failed for %s, queued for retry: %s", ticker, error)
    try:
        conn = _get_conn(cfg)
        _ensure_tables(conn)
        _save_failed_email(
            conn, ticker, analysis_date, decision, body,
            str(report_path) if report_path else None,
            smtp_server, smtp_port, smtp_user, mail_to, mail_cc, error,
        )
        conn.close()
    except Exception:
        logger.exception("Failed to queue failed email for %s", ticker)

    return False


# ── Retry failed emails ─────────────────────────────────────────────────


def retry_failed_emails(config: Optional[dict] = None) -> tuple[int, int]:
    """Retry all pending failed emails.

    Returns ``(succeeded, failed)`` counts.

    Each failed email is retried up to ``max_retries`` (default 3) with
    exponential backoff (5min, 15min, 45min). Successful sends are removed
    from the queue permanently.
    """
    cfg = config or {}
    try:
        conn = _get_conn(cfg)
        _ensure_tables(conn)
    except Exception:
        logger.exception("Cannot open DB for email retry")
        return 0, 0

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    succeeded = 0
    failed = 0

    rows = conn.execute(
        "SELECT * FROM failed_emails WHERE retry_count < max_retries "
        "AND (next_retry_at IS NULL OR next_retry_at <= ?)",
        (now,),
    ).fetchall()

    if not rows:
        conn.close()
        return 0, 0

    # Resolve current SMTP password from env (not stored in DB)
    smtp_pass = (
        (cfg.get("smtp_password") or os.environ.get("SMTP_PASSWORD"))
    )

    for row in rows:
        row = dict(row)
        eid = row["id"]
        ticker = row["ticker"]

        # Reconstruct email
        msg = EmailMessage()
        msg["Subject"] = f"[TradingAgents] Analysis Report: {ticker} — {row['decision']}"
        msg["From"] = row["smtp_user"]
        msg["To"] = row["mail_to"]
        if row.get("mail_cc"):
            msg["Cc"] = row["mail_cc"]
        msg.set_content(row["report_body"])

        recipients = [row["mail_to"]] + (
            [c.strip() for c in row["mail_cc"].split(",") if c.strip()]
            if row.get("mail_cc") else []
        )

        ok, error = _send_smtp(
            row["smtp_server"],
            row["smtp_port"] or 587,
            row["smtp_user"],
            smtp_pass or "",
            msg, recipients, ticker, row["mail_to"],
        )

        if ok:
            conn.execute("DELETE FROM failed_emails WHERE id = ?", (eid,))
            succeeded += 1
        else:
            new_count = (row["retry_count"] or 0) + 1
            # Exponential backoff: 5min, 15min, 45min
            backoff_minutes = 5 * (3 ** (new_count - 1))
            next_retry = (
                datetime.datetime.now() + datetime.timedelta(minutes=backoff_minutes)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE failed_emails SET retry_count=?, last_error=?, "
                "next_retry_at=?, updated_at=? WHERE id=?",
                (new_count, error, next_retry, now, eid),
            )
            failed += 1

    conn.commit()
    conn.close()
    return succeeded, failed


def count_failed_emails(config: Optional[dict] = None) -> int:
    """Return the number of emails awaiting retry."""
    try:
        conn = _get_conn(config or {})
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM failed_emails WHERE retry_count < max_retries"
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0
