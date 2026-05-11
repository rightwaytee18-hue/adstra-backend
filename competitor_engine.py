import json
import logging
import os
from datetime import datetime

import anthropic

from db import get_db
from scraper import AdScraper

logger = logging.getLogger(__name__)


def analyze_ad(client: anthropic.Anthropic, ad: dict) -> dict:
    parts = []
    if ad.get('page_name'):
        parts.append(f"Advertiser: {ad['page_name']}")
    if ad.get('body'):
        parts.append(f"Ad copy: {ad['body']}")
    context = '\n'.join(parts) or 'No text available'

    prompt = (
        'Analyze this competitor ad briefly. Return ONLY valid JSON with these 4 fields:\n'
        '{"hook": "...", "angle": "...", "why_it_works": "...", "steal_this": "..."}\n\n'
        f'Ad details:\n{context}'
    )

    content: list = []
    url = ad.get('snapshot_url', '')
    if url.startswith('https://') and 'supabase' in url:
        content.append({'type': 'image', 'source': {'type': 'url', 'url': url}})
    content.append({'type': 'text', 'text': prompt})

    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': content}],
        )
        text = resp.content[0].text.strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception as e:
        logger.warning(f'Atlas analysis failed: {e}')
    return {}


def run_for_project(project_id: str) -> dict:
    db = get_db()

    proj_resp = db.table('projects').select('id, user_id, competitors').eq('id', project_id).single().execute()
    if not proj_resp.data:
        return {'scraped': 0, 'error': 'Project not found'}

    project = proj_resp.data
    competitors = project.get('competitors') or []
    user_id = project['user_id']

    if not competitors:
        return {'scraped': 0, 'message': 'No competitors configured'}

    scraper = AdScraper(db)
    ai = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''))
    total_saved = 0

    for comp in competitors:
        queries: list[tuple[str, str]] = []
        if comp.get('url'):
            domain = comp['url'].replace('https://', '').replace('http://', '').split('/')[0]
            if domain:
                queries.append((domain, 'domain'))
        if comp.get('page_name'):
            queries.append((comp['page_name'], 'page'))
        if comp.get('name') and not queries:
            queries.append((comp['name'], 'keyword'))

        for query, source_type in queries:
            try:
                ads = scraper.scrape(query, source_type, project_id, user_id)
                for ad in ads:
                    existing = db.table('competitor_ads')\
                        .select('id')\
                        .eq('project_id', project_id)\
                        .eq('ad_id', ad['ad_id'])\
                        .execute()

                    if existing.data:
                        db.table('competitor_ads')\
                            .update({'last_seen': datetime.now().isoformat(), 'is_active': True})\
                            .eq('ad_id', ad['ad_id'])\
                            .eq('project_id', project_id)\
                            .execute()
                        continue

                    analysis = analyze_ad(ai, ad)

                    db.table('competitor_ads').insert({
                        **ad,
                        'is_active': True,
                        'is_saved': False,
                        'atlas_analysis': analysis or None,
                        'first_seen': datetime.now().isoformat(),
                        'last_seen': datetime.now().isoformat(),
                    }).execute()
                    total_saved += 1

            except Exception as e:
                logger.error(f'Scrape failed for "{query}": {e}')

    return {'scraped': total_saved}


def run_all_projects() -> dict:
    import time
    db = get_db()
    resp = db.table('projects').select('id').execute()
    results = {}
    for p in (resp.data or []):
        try:
            results[p['id']] = run_for_project(p['id'])
        except Exception as e:
            results[p['id']] = {'error': str(e)}
        time.sleep(5)
    return results
