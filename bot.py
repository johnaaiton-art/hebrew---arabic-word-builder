import os
import json
import time
import logging
import re
from typing import Dict, Any, List
import requests
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials

# --------------------
# Config
# --------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_CREDS_FILE = 'service_account.json'

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
TEMPERATURE = 0.2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------
# Globals (initialized in main)
# --------------------
sheet = None
client = None

# cache last result per chat to avoid big callback payloads
LAST_RESULTS: Dict[int, Dict[str, Any]] = {}

# Accept only Hebrew letters (no nikkud/vowel points)
HEBREW_WORD_RE = re.compile(r"^[\u05D0-\u05EA]+$")

# --------------------
# Telegram helpers
# --------------------
def telegram(method: str, payload: Dict[str, Any]):
    url = f"{TELEGRAM_API}/{method}"
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"Telegram API error, retrying: {e}")
            time.sleep(2)
    raise RuntimeError("Telegram API failed after retries")

def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram("sendMessage", payload)

def answer_callback(callback_id: str, text: str = "✔️"):
    telegram("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
        "show_alert": False
    })

# --------------------
# Init functions
# --------------------
def init_deepseek():
    global client
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com"
    )
    logger.info("DeepSeek initialized")

def init_google_sheets():
    global sheet
    if not os.path.exists(GOOGLE_CREDS_FILE):
        raise ValueError(f"Google credentials file not found: {GOOGLE_CREDS_FILE}")
    
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SHEET_ID).sheet1
    logger.info("Google Sheets initialized")

# --------------------
# DeepSeek prompt
# --------------------
SYSTEM_PROMPT = """
You are a Semitic linguistics assistant for a beginner Hebrew learner.

CRITICAL RULES:
1. The input is ONE modern Hebrew word.
2. Identify its root and core meaning.
3. Provide a short classical Hebrew example with reference.
4. Provide Arabic cognate if it exists, otherwise write exactly: Arabic cognate root = none
5. If Arabic cognate exists:
   - Give EXACTLY 3 Arabic examples
   - Each with precise English gloss
6. Use simple Latin transliteration (no diacritics).
7. Do NOT guess roots. If unsure, say root unknown.
8. Do NOT list derived words in the main text.

Derived words must:
- Be ONLY modern, common Hebrew
- Exactly 2 to 4 items
- Each has: Hebrew - transliteration - one or two English meanings
- No archaic or biblical forms
"""

USER_INSTRUCTIONS = """
Word: {word}

Return:

MAIN TEXT in this format:

[Hebrew word] root = [Hebrew root (K-W-N)] core meaning = "..."
classical Hebrew text example: ... (as in ...)

Arabic cognate root [Arabic root (K-W-N)] = "..."
Arabic examples:
* gloss: Arabic (translit)
* gloss: Arabic (translit)
* gloss: Arabic (translit)

OR if none:
Arabic cognate root = none

Then output ONLY this JSON block:

DERIVED_JSON:
[
  {{
    "hebrew": "...",
    "translit": "...",
    "english": "..."
  }}
]
"""

# --------------------
# DeepSeek call
# --------------------
def call_deepseek(word: str) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_INSTRUCTIONS.format(word=word)}
    ]
    
    for _ in range(3):
        try:
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=TEMPERATURE
            )
            text = resp.choices[0].message.content.strip()
            break
        except Exception as e:
            logger.warning(f"DeepSeek error, retrying: {e}")
            time.sleep(2)
    else:
        raise RuntimeError("DeepSeek failed after retries")
    
    # Robust JSON extraction
    main_text = text
    derived = []
    
    if "DERIVED_JSON:" in text:
        main_text, json_part = text.split("DERIVED_JSON:", 1)
    else:
        m = re.search(r"\[\s*{.*}\s*\]", text, re.DOTALL)
        if m:
            json_part = m.group(0)
            main_text = text.replace(json_part, "").strip()
        else:
            return {"main_text": text.strip(), "derived": []}
    
    json_part = re.sub(r"^```json|```$", "", json_part).strip()
    
    try:
        derived = json.loads(json_part)
    except json.JSONDecodeError:
        logger.warning("Failed to parse DERIVED_JSON")
        derived = []
    
    return {
        "main_text": main_text.strip(),
        "derived": derived
    }

# --------------------
# Parsing roots
# --------------------
def extract_roots_and_arabic(main_text: str):
    heb_root = "unknown"
    ar_root = "none"
    ar_examples = "none"
    
    lines = [l.strip() for l in main_text.splitlines() if l.strip()]
    
    for line in lines:
        if " root =" in line:
            m = re.search(r"root\s*=\s*(.+?)\s*core meaning", line)
            if m:
                heb_root = m.group(1).strip()
        
        if line.startswith("Arabic cognate root"):
            if "= none" in line:
                ar_root = "none"
                ar_examples = "none"
            else:
                m = re.search(r"Arabic cognate root\s+(.+?)\s*=\s*(.+)$", line)
                if m:
                    ar_root = f"{m.group(1).strip()} {m.group(2).strip()}"
    
    if ar_root != "none":
        examples = []
        capture = False
        for line in lines:
            if line.startswith("Arabic examples"):
                capture = True
                continue
            if capture and line.startswith("*"):
                examples.append(line.strip("* ").strip())
        if examples:
            ar_examples = examples[0] + ("\n* " + "\n* ".join(examples[1:]) if len(examples) > 1 else "")
        else:
            ar_examples = "none"
    
    return heb_root, ar_root, ar_examples

# --------------------
# Sheet append with duplicate check
# --------------------
def append_to_sheet(hebrew, translit, english, heb_root, ar_root, ar_examples):
    # FIXED: Safe header check
    all_vals = sheet.col_values(1)
    existing = all_vals[1:] if len(all_vals) > 1 else []
    
    if hebrew in existing:
        logger.info(f"Duplicate skipped: {hebrew}")
        return False
    
    row = [hebrew, translit, english, heb_root, ar_root, ar_examples]
    sheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended: {hebrew}")
    return True

# --------------------
# Telegram handlers
# --------------------
def handle_message(msg: Dict[str, Any]):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    
    # Handle /start command
    if text == "/start":
        send_message(chat_id, 
            "👋 <b>Welcome to Hebrew Etymology Bot!</b>\n\n"
            "📝 Send me a single Hebrew word (letters only)\n"
            "🔍 I'll analyze its root, etymology, and Arabic cognates\n"
            "💾 Tap buttons to save derived words to your Google Sheet\n\n"
            "Example: send <b>מכין</b>"
        )
        return
    
    if not HEBREW_WORD_RE.match(text):
        send_message(chat_id, "❗ Please send a single Hebrew word (letters only, no vowel points).")
        return
    
    send_message(chat_id, "🤖 Analyzing...")
    
    try:
        result = call_deepseek(text)
        main_text = result["main_text"]
        derived = result["derived"]
        
        heb_root, ar_root, ar_examples = extract_roots_and_arabic(main_text)
        
        LAST_RESULTS[chat_id] = {
            "heb_root": heb_root,
            "ar_root": ar_root,
            "ar_examples": ar_examples,
            "derived": derived
        }
        
        buttons = []
        for i, d in enumerate(derived):
            # FIXED: Use hyphen not en-dash
            label = f"{d['hebrew']} - {d['translit']} - {d['english']}"
            if len(label) > 60:
                label = label[:57] + "..."
            buttons.append([{
                "text": label,
                "callback_data": f"save:{i}"
            }])
        
        reply_markup = {"inline_keyboard": buttons} if buttons else None
        send_message(chat_id, main_text, reply_markup=reply_markup)
    
    except Exception as e:
        logger.exception("Error processing word")
        send_message(chat_id, f"❌ Error: {e}")

def handle_callback(cb: Dict[str, Any]):
    cb_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]
    data = cb["data"]
    
    if not data.startswith("save:"):
        answer_callback(cb_id, "❌")
        return
    
    idx = int(data.split(":")[1])
    cached = LAST_RESULTS.get(chat_id)
    
    if not cached or idx >= len(cached["derived"]):
        answer_callback(cb_id, "❌ Expired")
        return
    
    d = cached["derived"][idx]
    
    try:
        saved = append_to_sheet(
            d["hebrew"],
            d["translit"],
            d["english"],
            cached["heb_root"],
            cached["ar_root"],
            cached["ar_examples"]
        )
        answer_callback(cb_id, "✔️" if saved else "⚠️ Duplicate")
    except Exception as e:
        logger.exception("Sheet append failed")
        answer_callback(cb_id, "❌ Error")

# --------------------
# Long polling loop
# --------------------
def get_updates(offset=None):
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json()["result"]

def main():
    logger.info("Starting bot...")
    init_deepseek()
    init_google_sheets()
    
    offset = None
    
    while True:
        try:
            updates = get_updates(offset)
            for u in updates:
                offset = u["update_id"] + 1
                
                if "message" in u:
                    handle_message(u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()