import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request
from urllib.request import urlopen

import django
from django.core.files.base import ContentFile


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from catalog.models import Product  # noqa: E402


USER_AGENT = 'NatureNestImageFix/2.0'
OPENVERSE_API = 'https://api.openverse.org/v1/images/'
MIN_IMAGE_BYTES = 12_000
VALID_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}

STOP_WORDS = {
    'eco',
    'friendly',
    'set',
    'kit',
    'pack',
    'organic',
    'recycled',
    'reusable',
    'refillable',
    'natural',
    'plant',
    'based',
    'urban',
    'classic',
    'fresh',
    'care',
    'herbal',
    'free',
    'plastic',
    'pet',
}

BANNED_TOKENS = {
    'bear',
    'cat',
    'dog',
    'statue',
    'monument',
    'selfie',
    'portrait',
}

CATEGORY_HINTS = {
    'reusable kitchen essentials': ['reusable kitchen tools', 'sustainable kitchen utensils'],
    'plastic-free personal care': ['plastic free personal care products', 'sustainable bathroom products'],
    'sustainable home cleaning': ['eco home cleaning products', 'biodegradable cleaning supplies'],
    'organic wellness pantry': ['organic food pantry products', 'healthy natural groceries'],
    'eco fashion & accessories': ['sustainable fashion accessories', 'eco friendly bag and wallet'],
}

QUERY_OVERRIDES = {
    'stainless steel lunch box': ['stainless steel lunchbox', 'steel tiffin box'],
    'compostable scrub sponge set': ['compostable kitchen sponge'],
    'recycled pet cap': ['recycled baseball cap', 'recycled fabric cap'],
    'recycled fabric backpack': ['recycled material backpack'],
    'upcycled denim pouch': ['upcycled denim bag', 'denim pouch'],
    'jute yoga mat bag': ['jute yoga bag', 'yoga mat bag'],
    'hemp laptop sleeve': ['laptop sleeve', 'hemp bag'],
}


def slugify_ascii(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    return value.strip('-') or 'product'


def tokenize(value):
    return [token for token in re.split(r'[^a-z0-9]+', (value or '').lower()) if len(token) > 2]


def parse_extension(url, content_type):
    ext = Path((url or '').split('?', 1)[0].lower()).suffix.replace('.', '')
    if not ext and content_type:
        if 'png' in content_type:
            ext = 'png'
        elif 'webp' in content_type:
            ext = 'webp'
        else:
            ext = 'jpg'
    if ext == 'jpeg':
        ext = 'jpg'
    if ext not in VALID_EXTENSIONS:
        ext = 'jpg'
    return ext


def build_query_variants(product):
    key = (product.name or '').strip().lower()
    category_key = ((product.category.name if product.category_id and product.category else '') or '').lower()
    product_tokens = [token for token in tokenize(product.name) if token not in STOP_WORDS]

    variants = []

    def add(candidate):
        candidate = ' '.join((candidate or '').split())
        if candidate and candidate not in variants:
            variants.append(candidate)

    for override in QUERY_OVERRIDES.get(key, []):
        add(override)

    add(product.name)
    if product_tokens:
        add(' '.join(product_tokens))
    if len(product_tokens) >= 2:
        add(' '.join(product_tokens[:2]))

    for hint in CATEGORY_HINTS.get(category_key, []):
        add(hint)
        if product_tokens:
            add(f'{hint} {product_tokens[0]}')

    add('sustainable product')
    return variants


def openverse_search(query):
    params = {'q': query, 'page_size': 20}
    api_url = f'{OPENVERSE_API}?{urlencode(params)}'
    request = Request(api_url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=35) as response:
        payload = json.load(response)
    return payload.get('results') or []


def candidate_tokens(item):
    title = item.get('title') or ''
    tags = ' '.join(
        str(tag.get('name') or '')
        for tag in (item.get('tags') or [])
    )
    return set(tokenize(f'{title} {tags}'))


def score_candidate(item, query_tokens, product_tokens, category_tokens):
    tokens = candidate_tokens(item)
    product_overlap = len(tokens & product_tokens)
    query_overlap = len(tokens & query_tokens)
    category_overlap = len(tokens & category_tokens)

    if product_overlap == 0 and query_overlap == 0 and category_overlap == 0:
        return None

    score = (product_overlap * 6) + (query_overlap * 3) + category_overlap

    width = item.get('width') or 0
    height = item.get('height') or 0
    if width and height:
        ratio = width / height
        if 0.65 <= ratio <= 1.9:
            score += 1

    license_name = (item.get('license') or '').lower()
    if license_name in {'by', 'by-sa', 'cc0'}:
        score += 1

    if tokens & BANNED_TOKENS:
        score -= 4

    return score


def download_image(url):
    request = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(request, timeout=45) as response:
        data = response.read()
        final_url = response.geturl() or url
        content_type = response.headers.get('Content-Type') or ''
    if len(data) < MIN_IMAGE_BYTES:
        raise ValueError('image too small')
    ext = parse_extension(final_url, content_type.lower())
    return data, ext


def fetch_from_openverse(product, used_hashes, used_urls):
    product_tokens = set(tokenize(product.name)) - STOP_WORDS
    category_name = product.category.name if product.category_id and product.category else ''
    category_tokens = set(tokenize(category_name)) - STOP_WORDS

    for query in build_query_variants(product):
        query_tokens = set(tokenize(query)) - STOP_WORDS
        try:
            results = openverse_search(query)
        except Exception:
            continue

        scored_items = []
        for item in results:
            score = score_candidate(item, query_tokens, product_tokens, category_tokens)
            if score is None:
                continue
            scored_items.append((score, item))

        scored_items.sort(key=lambda entry: entry[0], reverse=True)

        for _, item in scored_items[:12]:
            source_url = item.get('url') or ''
            if not source_url or source_url in used_urls:
                continue
            try:
                image_data, ext = download_image(source_url)
            except Exception:
                continue
            image_hash = hashlib.sha256(image_data).hexdigest()
            if image_hash in used_hashes:
                continue
            used_urls.add(source_url)
            used_hashes.add(image_hash)
            return image_data, ext, query, source_url
    return None, None, None, None


def fetch_fallback(product, used_hashes):
    fallback_url = (
        f'https://picsum.photos/seed/naturenest-{product.id}-{slugify_ascii(product.name)}/1200/800'
    )
    data, ext = download_image(fallback_url)
    image_hash = hashlib.sha256(data).hexdigest()
    if image_hash in used_hashes:
        raise ValueError('fallback image duplicated')
    used_hashes.add(image_hash)
    return data, ext


def main():
    products = list(Product.objects.select_related('category').order_by('id'))
    updated = 0
    failed = []
    fallback_used = 0
    used_hashes = set()
    used_urls = set()

    for product in products:
        try:
            image_data, ext, matched_query, source_url = fetch_from_openverse(
                product,
                used_hashes,
                used_urls,
            )
            if image_data is None:
                image_data, ext = fetch_fallback(product, used_hashes)
                fallback_used += 1
                matched_query = 'picsum-fallback'
                source_url = 'https://picsum.photos'

            filename = f'{product.id:03d}-{slugify_ascii(product.name)}.{ext}'
            product.photo.save(filename, ContentFile(image_data), save=True)
            updated += 1
            print(f'updated {product.id:03d} | {product.name} | query={matched_query} | source={source_url}')
        except Exception as exc:
            failed.append((product.id, product.name, str(exc)))
            print(f'failed  {product.id:03d} | {product.name} | {exc}')

    print(f'updated={updated}')
    print(f'failed={len(failed)}')
    print(f'fallback_used={fallback_used}')
    print(f'unique_hashes={len(used_hashes)}')
    if failed:
        for item in failed[:20]:
            print(f' - {item[0]} | {item[1]} | {item[2]}')


if __name__ == '__main__':
    main()
