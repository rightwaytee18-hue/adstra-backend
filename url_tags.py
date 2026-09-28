"""
The URL parameters every Meta ad carries (the ad creative's `url_tags`).

Meta appends this string to the destination on every click and substitutes its
dynamic macros at click time: {{campaign.id}}, {{adset.id}}, {{ad.id}}. Those
ids are what let a lead resolve back to the ad set the media buyer moves money
between. Reveal's capture (public/reveal.js) keeps fb_campaign, fb_adset and
fb_ad by name, so these names are the contract.

Mirror of reveal lib/ads/meta/urlTags.ts. KEEP IN SYNC.

⚠️ THE BRACES MUST REACH META UNESCAPED. urlencode turns `{{ad.id}}` into
`%7B%7Bad.id%7D%7D`, which Meta does not recognise, so the string is built by
hand and never passed through an encoder.
"""
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

META_URL_PARAMS = (
    ("utm_source", "facebook"),
    ("utm_medium", "paid"),
    ("utm_id", "{{campaign.id}}"),
    ("fb_campaign", "{{campaign.id}}"),
    ("fb_adset", "{{adset.id}}"),
    ("fb_ad", "{{ad.id}}"),
)


def meta_url_tags(existing: Optional[str] = None) -> str:
    """The url_tags string, skipping any parameter the destination already sets."""
    have = set()
    if existing:
        try:
            have = {k for k, _ in parse_qsl(urlsplit(existing).query, keep_blank_values=True)}
        except ValueError:
            have = set()
    return "&".join(f"{k}={v}" for k, v in META_URL_PARAMS if k not in have)
