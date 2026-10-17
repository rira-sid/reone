"""Product names, descriptions, categories, nutrition facts and sizes in every customer language.

Each product is translated once (one Gemini call covers all languages) and again whenever its
English text changes. A seller's own correction for a language is kept (edited=True) until they
press "Translate again". The background loop (reminders.py) catches up products that are missing or
out of date, a couple per minute, so free-tier rate limits are respected."""
import hashlib
import json

from pydantic import BaseModel

import models
from languages import LANGUAGES

TARGETS = [code for code in LANGUAGES if code != "en"]
PER_TICK = 2


class _LangText(BaseModel):
    lang: str
    name: str
    description: str = ""
    category: str = ""
    nutrition: str = ""
    sizes: list[str] = []


class _AllLangs(BaseModel):
    items: list[_LangText]


def english(product: models.Product) -> dict:
    return {
        "name": product.name,
        "description": product.description or "",
        "category": product.category or "",
        "nutrition": product.nutrition or "",
        "sizes": [v.label for v in product.active_variants],
    }


def source_hash(product: models.Product) -> str:
    return hashlib.sha1(json.dumps(english(product), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def is_current(db, product: models.Product) -> bool:
    h = source_hash(product)
    rows = db.query(models.ProductTranslation).filter(models.ProductTranslation.product_id == product.id).all()
    done = {r.lang for r in rows if r.source_hash == h or r.edited}
    return all(code in done for code in TARGETS)


async def _ask_gemini(source: dict, shop_name: str) -> dict[str, _LangText]:
    from google.genai import types

    from ai import _gemini_client, GEMINI_MODEL

    names = ", ".join(f"{code} = {LANGUAGES[code][1]}" for code in TARGETS)
    prompt = f"""Translate this product from a small shop ("{shop_name}") for customers who read other languages.
Languages (code = language): {names}.
Return one item per language code with: name, description, category, nutrition, sizes.
Rules:
- Natural, simple words a shopper uses. Food names: use the common local name if one exists
  (e.g. "Pepper Powder" -> Tamil "மிளகுத் தூள்"), otherwise transliterate.
- Keep brand names, numbers, units (g, kg, ml, pcs) and ₹ prices exactly.
- nutrition: keep one fact per line as "Label: value", same number of lines, values unchanged.
- sizes: same count and order as the English list; translate words (Small, Large) but keep "100g" as is.
- Empty English field -> empty string.

English product (JSON):
{json.dumps(source, ensure_ascii=False)}"""
    config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=_AllLangs,
                                         temperature=0.2)
    response = await _gemini_client().aio.models.generate_content(model=GEMINI_MODEL, contents=[prompt], config=config)
    parsed = response.parsed if isinstance(response.parsed, _AllLangs) else _AllLangs.model_validate_json(response.text)
    return {item.lang: item for item in parsed.items if item.lang in TARGETS}


async def translate_product(db, product: models.Product, force: bool = False) -> int:
    """Translate into every language that is missing or outdated. Returns languages written."""
    h = source_hash(product)
    rows = {r.lang: r for r in db.query(models.ProductTranslation).filter(
        models.ProductTranslation.product_id == product.id).all()}
    todo = [code for code in TARGETS if force or code not in rows or (rows[code].source_hash != h and not rows[code].edited)]
    if not todo:
        return 0
    source = english(product)
    got = await _ask_gemini(source, product.business.name if product.business else "")
    written = 0
    for code in todo:
        item = got.get(code)
        if not item or not item.name.strip():
            continue
        sizes = item.sizes if len(item.sizes) == len(source["sizes"]) else source["sizes"]
        row = rows.get(code) or models.ProductTranslation(product_id=product.id, lang=code)
        row.name, row.description = item.name.strip()[:200], item.description.strip()
        row.category, row.nutrition = item.category.strip()[:80], item.nutrition.strip()
        row.sizes_json = json.dumps(sizes, ensure_ascii=False)
        row.source_hash, row.edited = h, False
        db.add(row)
        written += 1
    db.commit()
    return written


async def catch_up(db) -> int:
    """Background: translate a few products that need it. Never raises."""
    from tracking import log_event

    done = 0
    products = db.query(models.Product).filter(models.Product.is_active.is_(True)).order_by(models.Product.id).all()
    for product in products:
        if done >= PER_TICK:
            break
        if is_current(db, product):
            continue
        try:
            await translate_product(db, product)
        except Exception as e:  # noqa: BLE001 - Gemini busy / quota: try again next minute
            print(f"TRANSLATION FAILED (product {product.id}):", repr(e))
            if "429" not in repr(e) and "503" not in repr(e):
                log_event("ai", f"Product translation failed for '{product.name}': {e!r}"[:500], product.business_id)
            break
        done += 1
    return done


def translations_for(db, products: list[models.Product], lang: str) -> dict[int, models.ProductTranslation]:
    if lang == "en" or not products:
        return {}
    rows = db.query(models.ProductTranslation).filter(
        models.ProductTranslation.product_id.in_([p.id for p in products]),
        models.ProductTranslation.lang == lang).all()
    return {r.product_id: r for r in rows}


def row_out(row: models.ProductTranslation | None, code: str) -> dict:
    return {
        "lang": code, "language": LANGUAGES[code][1], "native": LANGUAGES[code][0],
        "name": row.name if row else "", "description": row.description if row else "",
        "category": row.category if row else "", "nutrition": row.nutrition if row else "",
        "sizes": json.loads(row.sizes_json or "[]") if row else [],
        "edited": bool(row and row.edited), "ready": row is not None,
    }
