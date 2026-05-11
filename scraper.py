import time
import random
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

AD_LIBRARY_URL = 'https://www.facebook.com/ads/library/'
STORAGE_BUCKET = 'competitor-ads'


class AdScraper:
    def __init__(self, supabase_client):
        self.db = supabase_client

    def scrape(self, query: str, source_type: str, project_id: str, user_id: str, country: str = 'US') -> list[dict]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise Exception('Playwright not installed. Run: playwright install chromium --with-deps')

        ads = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
            )
            ctx = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                viewport={'width': 1440, 'height': 900},
                locale='en-US',
                java_script_enabled=True,
            )
            page = ctx.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined })")

            try:
                url = (
                    f'{AD_LIBRARY_URL}?active_status=active&ad_type=all'
                    f'&country={country}&q={query}&search_type=keyword_unordered&media_type=all'
                )
                page.goto(url, wait_until='domcontentloaded', timeout=45000)
                time.sleep(random.uniform(3.0, 5.0))

                for btn_text in ['Allow all cookies', 'Accept All', 'Allow essential and optional cookies']:
                    try:
                        btn = page.get_by_role('button', name=btn_text)
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            time.sleep(1.5)
                            break
                    except Exception:
                        pass

                for i in range(6):
                    page.evaluate(f'window.scrollTo(0, {i * 600})')
                    time.sleep(random.uniform(0.8, 1.4))
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                time.sleep(2.0)
                page.evaluate('window.scrollTo(0, 0)')
                time.sleep(2.0)

                try:
                    page.wait_for_selector('img[src*="fbcdn"]', timeout=10000)
                except Exception:
                    pass

                raw = page.evaluate('''() => {
                    const results = [];
                    const seen = new Set();
                    const allImgs = Array.from(document.querySelectorAll('img'));
                    for (const img of allImgs) {
                        const src = img.src || img.getAttribute('src') || '';
                        if ((!src.includes('fbcdn') && !src.includes('scontent')) || seen.has(src)) continue;
                        const w = img.naturalWidth || img.width || img.offsetWidth || 0;
                        const h = img.naturalHeight || img.height || img.offsetHeight || 0;
                        if (w > 0 && w < 100) continue;
                        if (h > 0 && h < 100) continue;
                        seen.add(src);
                        let body = '', pageName = '';
                        try {
                            const container = img.closest('[role="article"]') ||
                                img.parentElement?.parentElement?.parentElement;
                            if (container) {
                                const strong = container.querySelector('strong, b, h2, h3');
                                if (strong) pageName = strong.innerText.trim();
                                const bodyEl = container.querySelector('p, [class*="body"], [class*="text"]');
                                if (bodyEl && bodyEl.innerText.length > 20)
                                    body = bodyEl.innerText.trim().slice(0, 300);
                            }
                            if (!body) {
                                let node = img.parentElement;
                                for (let d = 0; d < 12 && node; d++) {
                                    const txt = (node.innerText || '').trim();
                                    if (txt.length > 40) {
                                        const lines = txt.split('\\n').map(l => l.trim()).filter(l => l.length > 5);
                                        if (lines.length) body = lines.slice(0, 3).join(' | ');
                                        break;
                                    }
                                    node = node.parentElement;
                                }
                            }
                        } catch(e) {}
                        results.push({ src, body, pageName, width: w, height: h });
                        if (results.length >= 20) break;
                    }
                    return results;
                }''')

                ts = int(time.time())
                for idx, item in enumerate(raw):
                    src = item['src']
                    storage_url = src  # fallback to CDN URL

                    try:
                        resp = ctx.request.get(
                            src,
                            headers={'Referer': 'https://www.facebook.com/', 'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8'},
                            timeout=15000,
                        )
                        body_bytes = resp.body() if resp.ok else None
                        if body_bytes and len(body_bytes) > 3000:
                            ct = resp.headers.get('content-type', 'image/jpeg')
                            ext = 'png' if 'png' in ct else 'jpg'
                            path = f'{project_id}/{ts}_{idx}.{ext}'
                            self.db.storage.from_(STORAGE_BUCKET).upload(
                                path,
                                body_bytes,
                                {'content-type': ct, 'cache-control': '31536000'},
                            )
                            storage_url = self.db.storage.from_(STORAGE_BUCKET).get_public_url(path)
                    except Exception as e:
                        logger.warning(f'Image upload failed for ad {idx}: {e}')

                    ads.append({
                        'ad_id': f'{project_id}_{ts}_{idx}',
                        'project_id': project_id,
                        'user_id': user_id,
                        'page_name': item.get('pageName', '') or '',
                        'body': item.get('body', '') or '',
                        'snapshot_url': storage_url,
                        'source_type': source_type,
                        'source_value': query,
                    })

            except Exception as e:
                browser.close()
                raise Exception(f'Scrape error for "{query}": {e}')

            browser.close()

        return ads
