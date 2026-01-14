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
2. Identify its Hebrew root and core meaning.
3. Provide a short classical/biblical Hebrew example with reference, and include ALL of:
   - Hebrew text
   - Simple Latin transliteration
   - Concise English gloss
4. Try to identify an Arabic cognate whenever one is linguistically plausible.
   - If a well-attested or widely accepted Arabic cognate exists, include it.
   - If you are reasonably confident (not just guessing) that a cognate exists, include it.
   - If no reasonable cognate is known, write exactly: Arabic cognate root = none
5. If an Arabic cognate exists:
   - Give EXACTLY 3 Arabic examples
   - Each example has a precise English gloss
   - Use simple Latin transliteration (no diacritics)
6. Use simple Latin transliteration (no diacritics) for BOTH Hebrew and Arabic.
7. Do NOT guess Hebrew roots. If unsure about the Hebrew root, say: root unknown.
8. Do NOT invent purely speculative Arabic cognates. Only include them when there is a plausible historical / lexical connection.
9. Do NOT list derived words in the main text.

Derived words must:
- Be ONLY modern, common Hebrew
- Exactly 2 to 4 items
- Each item has: Hebrew - transliteration - one or two English meanings
- No archaic or biblical forms
"""

USER_INSTRUCTIONS = """
Word: {word}

Return:

MAIN TEXT in this format:

[Hebrew word] root = [Hebrew root (K-W-N)] core meaning = "..."
classical Hebrew example:
Hebrew: [short biblical/classical Hebrew phrase]
Translit: [simple Latin transliteration]
English: [short natural English translation]
Reference: [book chapter:verse or other source]

Arabic cognate root [Arabic root (K-W-N)] = "..."
Arabic examples:
* [English gloss]: [Arabic word/phrase] (translit)
* [English gloss]: [Arabic word/phrase] (translit)
* [English gloss]: [Arabic word/phrase] (translit)

OR if no Arabic cognate is known:
Arabic cognate root = none

Then output ONLY this JSON block:

DERIVED_JSON:
[
  {
    "hebrew": "...",
    "translit": "...",
    "english": "..."
  }
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
    
    # -------- Robust JSON extraction --------
    # Remove markdown fences if present
    cleaned = text.replace("```json", "").replace("```", "").strip()
    main_text = cleaned
    derived: List[Dict[str, Any]] = []

    # Prefer an explicit DERIVED_JSON: [...] block
    m = re.search(r"DERIVED_JSON:\s*(\[[\s\S]*\])", cleaned)
    if m:
        json_block = m.group(1).strip()
        # everything before DERIVED_JSON is the explanatory text
        main_text = cleaned[:m.start()].strip()
    else:
        # Fallback: first JSON array anywhere in the text
        m = re.search(r"(\[[\s\S]*\])", cleaned)
        if m:
            json_block = m.group(1).strip()
            # remove the array from the main text
            main_text = (cleaned[:m.start()] + cleaned[m.end():]).strip()
        else:
            logger.warning("No JSON found in DeepSeek response")
            return {"main_text": text.strip(), "derived": []}

    try:
        derived = json.loads(json_block)
        if not isinstance(derived, list):
            logger.warning("DERIVED_JSON is not a list, wrapping in list")
            derived = [derived]
    except Exception as e:
        logger.warning(f"Failed to parse DERIVED_JSON: {e}")
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
