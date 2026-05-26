"""
Adstra Email Engine — Phase 9

Sends transactional emails via Resend.
Requires RESEND_API_KEY in environment.
"""

import os
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = "Adstra <hello@adstra.live>"
BASE_URL = "https://api.resend.com"


def _send(to: str, subject: str, html: str) -> dict:
    """POST to Resend API. Returns { id } or raises."""
    if not RESEND_API_KEY:
        logger.warning("[email] RESEND_API_KEY not set — skipping email send")
        return {"skipped": True}

    resp = requests.post(
        f"{BASE_URL}/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )
    data = resp.json()
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend error {resp.status_code}: {data}")
    return data


def _html_wrapper(content: str, preheader: str = "") -> str:
    """Wrap content in a minimal dark HTML email."""
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Adstra</title>
</head>
<body style="margin:0;padding:0;background:#04040a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  {"<span style='display:none;max-height:0;overflow:hidden;'>" + preheader + "</span>" if preheader else ""}
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#04040a;min-height:100vh;">
    <tr>
      <td align="center" style="padding:40px 20px;">
        <table width="560" cellpadding="0" cellspacing="0"
          style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.08);border-radius:16px;overflow:hidden;">
          <!-- Header -->
          <tr>
            <td style="padding:28px 32px 20px;border-bottom:1px solid rgba(255,255,255,0.05);">
              <span style="font-size:15px;font-weight:700;letter-spacing:0.12em;color:#00c2ff;text-transform:uppercase;">ADSTRA</span>
            </td>
          </tr>
          <!-- Content -->
          <tr>
            <td style="padding:28px 32px;">
              {content}
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding:20px 32px;border-top:1px solid rgba(255,255,255,0.05);">
              <p style="margin:0;font-size:11px;color:rgba(255,255,255,0.2);line-height:1.6;">
                You're receiving this because you have an Adstra account.
                Manage notification preferences in <a href="https://app.adstra.live/settings"
                  style="color:#00c2ff;text-decoration:none;">Settings</a>.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def p(text: str, muted: bool = False) -> str:
    color = "rgba(255,255,255,0.5)" if muted else "rgba(255,255,255,0.75)"
    return f'<p style="margin:0 0 16px;font-size:13px;line-height:1.7;color:{color};">{text}</p>'


def h2(text: str) -> str:
    return f'<h2 style="margin:0 0 12px;font-size:16px;font-weight:600;color:rgba(255,255,255,0.85);">{text}</h2>'


def cta_button(text: str, url: str) -> str:
    return f"""
<table cellpadding="0" cellspacing="0" style="margin:20px 0;">
  <tr>
    <td style="background:linear-gradient(135deg,rgba(0,194,255,0.15),rgba(123,47,255,0.15));
      border:1px solid rgba(0,194,255,0.3);border-radius:10px;">
      <a href="{url}"
        style="display:inline-block;padding:12px 24px;font-size:12px;font-weight:600;
        letter-spacing:0.08em;color:#fff;text-decoration:none;">{text}</a>
    </td>
  </tr>
</table>"""


# ─────────────────────────────────────────────────────────────
# Email templates
# ─────────────────────────────────────────────────────────────

def send_welcome(to: str, first_name: str) -> dict:
    """Welcome email sent after first successful onboarding."""
    name = first_name or "there"
    content = (
        h2(f"Welcome to Adstra, {name} 👋") +
        p("Your account is live. Here's what to do first:") +
        p("""
          <strong style="color:rgba(255,255,255,0.85);">1. Connect your Meta account</strong> — link your Ad Account and Facebook Page so Adstra can read your data and publish campaigns.<br><br>
          <strong style="color:rgba(255,255,255,0.85);">2. Set your goals</strong> — tell us your target ROAS or CPA so the rules engine knows what "winning" looks like.<br><br>
          <strong style="color:rgba(255,255,255,0.85);">3. Build your first campaign</strong> — the Campaign Builder takes under 5 minutes.
        """) +
        cta_button("Go to Dashboard →", "https://app.adstra.live/dashboard")
    )
    return _send(to, "Welcome to Adstra — let's get your ads optimized", _html_wrapper(content, "Your AI-powered Meta ads platform is ready."))


def send_autopilot_summary(to: str, project_name: str, actions_taken: int, actions_queued: int) -> dict:
    """Daily autopilot summary."""
    total = actions_taken + actions_queued
    subject = f"Adstra autopilot: {total} action{'s' if total != 1 else ''} for {project_name}"

    if actions_taken > 0 and actions_queued == 0:
        headline = f"Autopilot optimized {actions_taken} campaign element{'s' if actions_taken != 1 else ''} ✅"
        desc = f"Adstra automatically applied {actions_taken} optimization{'s' if actions_taken != 1 else ''} to your {project_name} account."
    elif actions_queued > 0:
        headline = f"{actions_queued} action{'s' if actions_queued != 1 else ''} waiting for your approval"
        desc = f"Adstra identified {actions_queued} optimization opportunit{'ies' if actions_queued != 1 else 'y'} for {project_name}. Review and approve below."
    else:
        headline = "Autopilot ran — no actions needed today"
        desc = f"Your {project_name} campaigns look healthy. Adstra found no scaling or budget changes to make today."

    content = (
        h2(headline) +
        p(desc) +
        cta_button("Review in Autopilot →", "https://app.adstra.live/autopilot")
    )
    return _send(to, subject, _html_wrapper(content, desc[:80]))


def send_weekly_briefing(to: str, project_name: str, period_label: str, briefing_preview: str) -> dict:
    """Weekly briefing notification email."""
    subject = f"Your Adstra weekly briefing ({period_label})"
    preview = (briefing_preview[:200] + "…") if len(briefing_preview) > 200 else briefing_preview
    content = (
        h2(f"Weekly Performance Briefing — {period_label}") +
        p(f"Your Adstra AI has reviewed your <strong>{project_name}</strong> account and has strategic recommendations ready.") +
        p(preview, muted=True) +
        cta_button("Read Full Briefing →", "https://app.adstra.live/autopilot")
    )
    return _send(to, subject, _html_wrapper(content, f"Your weekly ads briefing for {period_label} is ready."))


def send_campaign_published(to: str, campaign_name: str, campaign_id: str) -> dict:
    """Sent when a campaign is successfully published."""
    content = (
        h2("Campaign published ✅") +
        p(f"<strong style='color:rgba(255,255,255,0.85);'>{campaign_name}</strong> has been created in your Meta Ads Manager. It starts <strong>paused</strong> — nothing spends until you turn it on.") +
        p(f"Campaign ID: <code style='font-family:monospace;color:#00c2ff;'>{campaign_id}</code>", muted=True) +
        cta_button("Open in Meta Ads Manager →", f"https://www.facebook.com/adsmanager/manage/campaigns")
    )
    return _send(to, f"Campaign ready: {campaign_name}", _html_wrapper(content))
