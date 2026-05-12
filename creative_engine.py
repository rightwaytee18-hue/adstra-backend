import io
import os
import base64
import logging
import time

import requests

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

MODEL_FAST = 'gemini-3.1-flash-image-preview'
MODEL_PRO = 'gemini-3-pro-image-preview'
STORAGE_BUCKET = 'creatives'


def _build_brand_context(project: dict) -> str:
    name = project.get('business_name') or 'Our Brand'
    colors = project.get('brand_colors') or []
    primary = colors[0] if colors else '#000000'
    secondary = colors[1] if len(colors) > 1 else primary
    niche = project.get('niche') or 'brand'

    products = project.get('products') or []
    product_lines = []
    for p in (products or [])[:3]:
        if isinstance(p, dict) and p.get('name'):
            desc = (p.get('description') or '')[:80]
            product_lines.append(f"- {p['name']}" + (f": {desc}" if desc else ''))
    products_text = '\n'.join(product_lines)

    ctx = f"""You are generating premium Facebook/Instagram ad creatives for {name}, a {niche} brand.

VISUAL IDENTITY:
- Primary brand color: {primary}
- Secondary brand color: {secondary}
- Style: clean, modern, professional — premium brand aesthetic
- Output: 1:1 square format optimized for Facebook/Instagram feed

BRAND: {name}"""

    if products_text:
        ctx += f"\n\nPRODUCTS:\n{products_text}"

    compliance = project.get('compliance_notes') or ''
    if compliance:
        ctx += f"\n\nCOMPLIANCE (must appear in every creative): {compliance}"

    return ctx


def _fetch_pil_image(url: str):
    resp = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content))


class NanaBanana:
    def __init__(self, supabase_client):
        if not GEMINI_AVAILABLE:
            raise RuntimeError('google-genai not installed. Run: pip install google-genai')
        if not PIL_AVAILABLE:
            raise RuntimeError('Pillow not installed. Run: pip install Pillow')
        self.client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY', ''))
        self.db = supabase_client

    def _call(self, contents, use_pro=False):
        model = MODEL_PRO if use_pro else MODEL_FAST
        try:
            return self.client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(response_modalities=['TEXT', 'IMAGE']),
            )
        except Exception:
            if not use_pro:
                return self.client.models.generate_content(
                    model=MODEL_PRO,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(response_modalities=['TEXT', 'IMAGE']),
                )
            raise

    def _extract_and_upload(self, response, project_id: str) -> tuple:
        text_parts = []
        image_url = None

        try:
            parts = response.candidates[0].content.parts
        except (AttributeError, IndexError):
            parts = getattr(response, 'parts', [])

        for part in parts:
            if hasattr(part, 'text') and part.text:
                text_parts.append(part.text)
            elif hasattr(part, 'inline_data') and part.inline_data:
                try:
                    raw = part.inline_data.data
                    img_bytes = bytes(raw) if isinstance(raw, (bytes, bytearray)) else base64.b64decode(raw)
                    mime = getattr(part.inline_data, 'mime_type', 'image/png')
                    ext = 'jpg' if ('jpeg' in mime or 'jpg' in mime) else 'png'
                    path = f'{project_id}/{int(time.time())}.{ext}'
                    self.db.storage.from_(STORAGE_BUCKET).upload(
                        path, img_bytes,
                        {'content-type': mime, 'cache-control': '31536000'},
                    )
                    image_url = self.db.storage.from_(STORAGE_BUCKET).get_public_url(path)
                except Exception as e:
                    logger.warning(f'Creative upload failed: {e}')

        return image_url, '\n'.join(text_parts)

    def generate_fresh(self, project: dict, prompt: str, product_name: str = '') -> tuple:
        brand_ctx = _build_brand_context(project)
        full_prompt = f"""{brand_ctx}

Create a professional Facebook/Instagram ad creative.
Product/focus: {product_name or 'main product'}
Creative direction: {prompt}

Deliver a polished, high-end square ad that reflects the brand identity above."""
        return self._extract_and_upload(self._call([full_prompt]), project['id'])

    def iterate_competitor(self, project: dict, competitor_url: str, prompt: str = '') -> tuple:
        brand_ctx = _build_brand_context(project)
        comp_img = _fetch_pil_image(competitor_url)
        full_prompt = f"""{brand_ctx}

I'm showing you a competitor ad. Analyze what makes it effective — layout, visual hierarchy, composition, hook.
Recreate it as a version for our brand:
- Keep the winning structural elements (layout, hierarchy, composition)
- Replace all competitor branding with our brand identity and colors
- Make it cleaner and more premium
- Output in 1:1 square format

Direction: {prompt or 'Elevate quality — match our brand aesthetic'}"""
        return self._extract_and_upload(self._call([full_prompt, comp_img]), project['id'])

    def edit_creative(self, project: dict, image_url: str, edit_prompt: str) -> tuple:
        brand_ctx = _build_brand_context(project)
        existing_img = _fetch_pil_image(image_url)
        full_prompt = f"""{brand_ctx}

Edit this existing ad creative based on the instructions below.
Instructions: {edit_prompt}
Maintain brand consistency and output in the same square format."""
        return self._extract_and_upload(self._call([full_prompt, existing_img]), project['id'])


def generate_for_project(project_id: str, mode: str, payload: dict) -> dict:
    from db import get_db
    db = get_db()

    proj = db.table('projects').select('*').eq('id', project_id).single().execute()
    if not proj.data:
        return {'error': 'Project not found'}

    project = proj.data
    banana = NanaBanana(db)

    try:
        if mode == 'fresh':
            url, text = banana.generate_fresh(
                project,
                payload.get('prompt', ''),
                payload.get('product_name', ''),
            )
        elif mode == 'iterate':
            if not payload.get('competitor_url'):
                return {'error': 'competitor_url required for iterate mode'}
            url, text = banana.iterate_competitor(
                project,
                payload['competitor_url'],
                payload.get('prompt', ''),
            )
        elif mode == 'edit':
            if not payload.get('image_url') or not payload.get('edit_prompt'):
                return {'error': 'image_url and edit_prompt required for edit mode'}
            url, text = banana.edit_creative(
                project,
                payload['image_url'],
                payload['edit_prompt'],
            )
        else:
            return {'error': f'Unknown mode: {mode}'}
    except Exception as e:
        logger.error(f'Creative generation error for {project_id}: {e}')
        return {'error': str(e)}

    if not url:
        return {'error': 'Generation produced no image'}

    rec = db.table('creative_generations').insert({
        'project_id': project_id,
        'user_id': project['user_id'],
        'mode': mode,
        'prompt': payload.get('prompt') or payload.get('edit_prompt') or '',
        'image_url': url,
        'is_saved': False,
        'metadata': {'text': text} if text else None,
    }).execute()

    creative_id = rec.data[0]['id'] if rec.data else None
    return {'id': creative_id, 'image_url': url, 'text': text}
