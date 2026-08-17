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
# support@, never a personal address, and never the retired adstra.live domain.
FROM_EMAIL = "Reveal <support@revealai.live>"
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
  <title>Reveal</title>
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
                You are receiving this because you have a Reveal account.
                Manage notification preferences in <a href="https://revealai.live/app"
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
        h2(f"Welcome, {name}") +
        p("Your account is live. Here's what to do first:") +
        p("""
          <strong style="color:rgba(255,255,255,0.85);">1. Connect your Facebook account</strong> so we can build and run your ads for you.<br><br>
          <strong style="color:rgba(255,255,255,0.85);">2. Tell us your budget</strong> so we know how much a day you are comfortable spending.<br><br>
          <strong style="color:rgba(255,255,255,0.85);">3. Build your first campaign</strong> — the Campaign Builder takes under 5 minutes.
        """) +
        cta_button("Go to Dashboard →", "https://revealai.live/app")
    )
    return _send(to, "Welcome to Reveal", _html_wrapper(content, "Your ads are ready to set up."))


def send_autopilot_summary(to: str, project_name: str, actions_taken: int, actions_queued: int) -> dict:
    """Daily autopilot summary."""
    total = actions_taken + actions_queued
    subject = f"{total} thing{'s' if total != 1 else ''} to look at for {project_name}"

    if actions_taken > 0 and actions_queued == 0:
        headline = f"We made {actions_taken} change{'s' if actions_taken != 1 else ''} to your ads"
        desc = f"We made {actions_taken} change{'s' if actions_taken != 1 else ''} to your {project_name} ads."
    elif actions_queued > 0:
        headline = f"{actions_queued} action{'s' if actions_queued != 1 else ''} waiting for your approval"
        desc = f"We found {actions_queued} thing{'s' if actions_queued != 1 else ''} worth changing on your {project_name} ads. Have a look and approve what you are happy with."
    else:
        headline = "Autopilot ran — no actions needed today"
        desc = f"Your {project_name} ads are doing fine. Nothing needed changing today."

    content = (
        h2(headline) +
        p(desc) +
        cta_button("Review in Autopilot →", "https://revealai.live/app/ads")
    )
    return _send(to, subject, _html_wrapper(content, desc[:80]))


def send_weekly_briefing(to: str, project_name: str, period_label: str, briefing_preview: str) -> dict:
    """Weekly briefing notification email."""
    subject = f"Your weekly ad summary ({period_label})"
    preview = (briefing_preview[:200] + "…") if len(briefing_preview) > 200 else briefing_preview
    content = (
        h2(f"Weekly Performance Briefing — {period_label}") +
        p(f"We looked over your <strong>{project_name}</strong> ads this week. Here is what stood out.") +
        p(preview, muted=True) +
        cta_button("Read Full Briefing →", "https://revealai.live/app/ads")
    )
    return _send(to, subject, _html_wrapper(content, f"Your weekly ads briefing for {period_label} is ready."))


def send_campaign_published(to: str, campaign_name: str, campaign_id: str) -> dict:
    """Sent when a campaign is successfully published."""
    content = (
        h2("Your ads are live") +
        p(f"<strong style='color:rgba(255,255,255,0.85);'>{campaign_name}</strong> has been created in your Meta Ads Manager. It starts <strong>paused</strong> — nothing spends until you turn it on.") +
        p(f"Campaign ID: <code style='font-family:monospace;color:#00c2ff;'>{campaign_id}</code>", muted=True) +
        cta_button("Open in Meta Ads Manager →", f"https://www.facebook.com/adsmanager/manage/campaigns")
    )
    return _send(to, f"Campaign ready: {campaign_name}", _html_wrapper(content))
