"""Languages a customer can pick in the WhatsApp welcome menu, and the fixed texts the bot and the
shop page show in each (locales/<code>.json). Free-form replies (order status answers) are written
by the AI in the customer's language; these are the messages that must never depend on the AI."""
import json
import os

# code: (name shown in the picker, English name given to the AI). Picker order: WhatsApp lists hold
# at most 10 rows, so the first page shows the first 9 plus a "More languages" row.
LANGUAGES = {
    "en": ("English", "English"),
    "hi": ("हिन्दी", "Hindi"),
    "ta": ("தமிழ்", "Tamil"),
    "te": ("తెలుగు", "Telugu"),
    "ml": ("മലയാളം", "Malayalam"),
    "kn": ("ಕನ್ನಡ", "Kannada"),
    "bn": ("বাংলা", "Bengali"),
    "mr": ("मराठी", "Marathi"),
    "gu": ("ગુજરાતી", "Gujarati"),
    "or": ("ଓଡ଼ିଆ", "Odia"),
    "pa": ("ਪੰਜਾਬੀ", "Punjabi"),
    "ur": ("اردو", "Urdu"),
    "ar": ("العربية", "Arabic"),
    "ms": ("Bahasa Melayu", "Malay"),
    "id": ("Bahasa Indonesia", "Indonesian"),
    "es": ("Español", "Spanish"),
    "fr": ("Français", "French"),
    "pt": ("Português", "Portuguese"),
    "zh": ("中文", "Chinese (Simplified)"),
}
RTL = {"ar", "ur"}
FIRST_PAGE = 9

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
TEXTS: dict[str, dict[str, str]] = {}
for _code in LANGUAGES:
    _path = os.path.join(_DIR, f"{_code}.json")
    if os.path.exists(_path):
        with open(_path, encoding="utf-8") as f:
            TEXTS[_code] = json.load(f)


def lang_code(code: str | None) -> str:
    return code if code in TEXTS else "en"


def t(code: str | None, key: str, **values) -> str:
    text = TEXTS[lang_code(code)].get(key) or TEXTS["en"][key]
    return text.format(**values) if values else text


def language_name(code: str | None) -> str:
    return LANGUAGES[lang_code(code)][1]


def available_languages() -> list[str]:
    """Codes with translations, in picker order."""
    return [c for c in LANGUAGES if c in TEXTS]
