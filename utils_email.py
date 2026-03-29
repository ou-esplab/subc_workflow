#!/usr/bin/env python3
"""Minimal email helper used by workflow failure notifications."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def _expand(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


def send_email(config: dict | None, subject: str, body: str) -> None:
    cfg = config or {}
    if not cfg.get("enabled", False):
        return

    host = _expand(cfg.get("smtp_host"))
    port = int(_expand(cfg.get("smtp_port", 25)))
    username = _expand(cfg.get("username", ""))
    password = _expand(cfg.get("password", ""))
    sender = _expand(cfg.get("from"))
    recipients = _expand(cfg.get("to") or [])

    if not host or not sender or not recipients:
        raise ValueError("email configuration requires smtp_host, from, and to when enabled=true")

    if isinstance(recipients, str):
        recipients = [recipients]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        if cfg.get("use_tls", True):
            smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message)