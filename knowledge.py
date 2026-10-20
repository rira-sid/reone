"""Shop knowledge: answers the seller gives once, which the bot then uses for every customer.

Ready-made common questions (picked for the kind of shop), the seller's own questions, and answers
saved from the seller's chat replies ("Save this answer for next time"). A question with no answer
here goes to the owner - the bot never guesses."""
import models

# Ready-made questions. "kinds" = the shop types they suit ("all" = every shop).
READY = [
    {"key": "returns", "kinds": ["all"], "question": "Do you accept returns or exchanges?"},
    {"key": "refund", "kinds": ["all"], "question": "How do refunds work, and how long do they take?"},
    {"key": "cancel", "kinds": ["all"], "question": "Can I cancel or change my order after placing it?"},
    {"key": "payments", "kinds": ["all"], "question": "Which payment methods do you accept?"},
    {"key": "pickup", "kinds": ["all"], "question": "Where is your shop? Can I pick up the order myself?"},
    {"key": "bulk", "kinds": ["all"], "question": "Do you take bulk or wholesale orders? Any discount?"},
    {"key": "gift", "kinds": ["all"], "question": "Do you do gift packing or gift messages?"},
    {"key": "custom", "kinds": ["all"], "question": "Do you take custom or personalised orders?"},
    {"key": "gst", "kinds": ["all"], "question": "Do you give a GST bill?"},
    {"key": "veg", "kinds": ["food"], "question": "Is it vegetarian / vegan?"},
    {"key": "preservatives", "kinds": ["food", "beauty"], "question": "Any preservatives, added colour or chemicals?"},
    {"key": "spice", "kinds": ["food"], "question": "How spicy is it?"},
    {"key": "shelf_life", "kinds": ["food", "beauty"], "question": "How long does it last (shelf life / expiry)?"},
    {"key": "storage", "kinds": ["food"], "question": "How should I store it?"},
    {"key": "allergens", "kinds": ["food", "beauty"], "question": "Any nuts, gluten or other allergens?"},
    {"key": "made_how", "kinds": ["food"], "question": "Is it homemade? How is it made?"},
    {"key": "size_fit", "kinds": ["clothing"], "question": "How do your sizes fit? Do you have a size chart?"},
    {"key": "fabric", "kinds": ["clothing"], "question": "What fabric is it, and how should I wash it?"},
    {"key": "colour", "kinds": ["clothing", "accessories"], "question": "Will the colour look the same as in the photo?"},
    {"key": "alteration", "kinds": ["clothing"], "question": "Do you do alterations or stitching?"},
    {"key": "material", "kinds": ["accessories"], "question": "What material is it? Will it tarnish or fade?"},
    {"key": "care", "kinds": ["accessories", "home"], "question": "How do I take care of it?"},
    {"key": "warranty", "kinds": ["electronics", "home"], "question": "Is there a warranty? How do I claim it?"},
    {"key": "compatibility", "kinds": ["electronics"], "question": "Will it fit / work with my model?"},
    {"key": "original", "kinds": ["electronics", "beauty"], "question": "Is it original / genuine?"},
    {"key": "install", "kinds": ["electronics", "home"], "question": "Do you help with installation or fitting?"},
    {"key": "skin", "kinds": ["beauty"], "question": "Is it suitable for sensitive skin?"},
]
READY_BY_KEY = {r["key"]: r for r in READY}
BUSINESS_TYPES = ["food", "clothing", "accessories", "electronics", "beauty", "home"]


def ready_for(business: models.Business) -> list[dict]:
    """Ready-made questions for this shop's type (all of them until the seller picks a type)."""
    kind = business.business_type
    if kind not in BUSINESS_TYPES:
        return READY
    return [r for r in READY if "all" in r["kinds"] or kind in r["kinds"]]


def answers(db, business: models.Business) -> list[models.ShopAnswer]:
    return db.query(models.ShopAnswer).filter(models.ShopAnswer.business_id == business.id).order_by(
        models.ShopAnswer.id).all()


def knowledge_text(db, business: models.Business) -> str:
    """Every answered question, for the bot."""
    lines = [f"Q: {a.question}\nA: {a.answer}" for a in answers(db, business) if (a.answer or "").strip()]
    return "\n".join(lines)


def answer_dict(a: models.ShopAnswer) -> dict:
    return {"id": a.id, "key": a.key, "question": a.question, "answer": a.answer, "source": a.source,
            "product_id": a.product_id, "updated_at": a.updated_at}
