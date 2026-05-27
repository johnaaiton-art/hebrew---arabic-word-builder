import os
import json
import time
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
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
MAX_CACHED_CHATS = 100  # Prevent memory leak

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --------------------
# Globals
# --------------------
sheet_hebrew = None
sheet_arabic = None
client = None

# cache last result per chat with size limit
LAST_RESULTS: Dict[int, Dict[str, Any]] = {}

# Hebrew regex
HEBREW_WORD_RE = re.compile(r"^[\u05D0-\u05EA]+$")

# Arabic regex (Basic Arabic + extended)
ARABIC_WORD_RE = re.compile(r"^[\u0600-\u06FF\u0750-\u077F]+$")

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
    global sheet_hebrew, sheet_arabic
    if not os.path.exists(GOOGLE_CREDS_FILE):
        raise ValueError(f"Google credentials file not found: {GOOGLE_CREDS_FILE}")
    
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    
    spreadsheet = gc.open_by_key(SHEET_ID)
    
    # Hebrew tab
    try:
        sheet_hebrew = spreadsheet.worksheet("Hebrew")
    except gspread.exceptions.WorksheetNotFound:
        sheet_hebrew = spreadsheet.add_worksheet(title="Hebrew", rows="1000", cols="6")
        sheet_hebrew.append_row(["Hebrew", "Transliteration", "English", "Hebrew Root", "Arabic Cognate", "Arabic Examples"])
    
    # Arabic tab
    try:
        sheet_arabic = spreadsheet.worksheet("Arabic")
    except gspread.exceptions.WorksheetNotFound:
        sheet_arabic = spreadsheet.add_worksheet(title="Arabic", rows="1000", cols="4")
        sheet_arabic.append_row(["English", "Arabic", "Transliteration", "Original Word"])
    
    logger.info("Google Sheets initialized (Hebrew + Arabic tabs)")

# --------------------
# Hebrew prompts
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
  {{
    "hebrew": "...",
    "translit": "...",
    "english": "..."
  }}
]
"""

# --------------------
# Hebrew phrase prompt
# --------------------
PHRASE_SYSTEM_PROMPT = """
You are a Hebrew language tutor helping a beginner learner understand modern Hebrew sentences.

CRITICAL RULES:
1. The input is a modern Hebrew phrase or sentence (multiple words).
2. Break it down word by word, where a "word" is defined by spaces — attached prefixes/articles/prepositions count as ONE word with what follows.
3. SKIP: subject pronouns (אני, אתה, את, הוא, היא, אנחנו, אתם, הן, הם), question particles (האם), and common function words like: איפה, איך, מתי, למה, מה, מי, כן, לא — UNLESS they are the main meaningful content.
4. DO NOT skip: object pronouns, possessive suffixes, definite articles attached to meaningful words, prepositions attached to meaningful words.
5. For each kept word: give Hebrew, transliteration in parentheses, then English meaning. Keep it concise.
6. Output ONLY the word list, one per line. No intro, no summary sentence, no headers.

Output format — each line exactly like this:
WORD (translit) meaning1 / meaning2

Example output:
הציפורן (ha-tsipóren) the nail / nail
הפתוחה (ha-ptukhá) the open one / open
שלך (shelkhá) yours / your
פועלת (po'élet) works / operates
"""

PHRASE_USER_INSTRUCTIONS = """
Phrase: {phrase}
"""

# --------------------
# Arabic prompt
# --------------------
ARABIC_SYSTEM_PROMPT = """
You are an Arabic linguistics assistant for a beginner learner.

CRITICAL RULES:
1. The input is ONE Arabic word in Arabic script.
2. Identify its 3-consonant root (جذر) and core meaning.
3. If there are prefixes or suffixes attached to the root, identify them (e.g., ي for present tense, ت for you, ـت for past tense I, etc.)
4. Identify if there is a plausible Hebrew cognate (same root or very similar meaning). If yes, provide the Hebrew word with transliteration and meaning. If not, say "no known Hebrew cognate".
5. Provide 4 common conjugations from the same root, but ONLY if they are commonly used in everyday Modern Standard Arabic (not rare/formal/academic):
   - First person present (I do): أنا + present tense
   - Second person masculine present (you do): أنتَ + present tense
   - Third person masculine present (he does): هو + present tense
   - First person past (I did): أنا + past tense
6. Also provide 1-3 other common derived words from the same root (nouns, adjectives, etc.) that are commonly used.
7. Use simple Latin transliteration (no diacritics) for both Arabic and Hebrew.
8. Do NOT guess roots. If unsure about the root, say: root unknown.

Output format:

MAIN TEXT:
Word: {word} (transliteration)
Root: (consonants with hyphens) - meaning: "core meaning"
Prefixes/Suffixes: (if any, otherwise say "none")
Hebrew cognate: Hebrew word (translit) - meaning (OR "no known Hebrew cognate")

Common conjugations (from this root):
• I do: أنا + verb (translit) - meaning
• You (m) do: أنتَ + verb (translit) - meaning
• He does: هو + verb (translit) - meaning
• I did: أنا + verb (past) (translit) - meaning

Other common words from this root:
• word (translit) - meaning
• word (translit) - meaning (if applicable)

Then output this JSON block for the buttons:

BUTTONS_JSON:
[
  {{
    "arabic": "كَتَبْتُ",
    "translit": "katabtu",
    "english": "I wrote",
    "category": "I_past"
  }},
  {{
    "arabic": "أَكْتُبُ",
    "translit": "aktubu",
    "english": "I write",
    "category": "I_present"
  }}
]

Only include the 4 conjugations plus any other common derived words (max 3) in BUTTONS_JSON.
"""

ARABIC_USER_INSTRUCTIONS = """
Arabic word: {word}
"""

# --------------------
# DeepSeek calls with safe JSON extraction
# --------------------
def extract_json_array(text: str, marker: str) -> Tuple[str, List[Dict]]:
    """Safely extract JSON array from DeepSeek response.
    Returns (main_text, json_array)"""
    cleaned = text.replace("```json", "").replace("```", "").strip()
    main_text = cleaned
    json_array = []
    
    # Look for marker
    marker_pos = cleaned.find(marker) if marker else -1
    
    if marker_pos != -1:
        # Find opening bracket after marker
        bracket_start = cleaned.find("[", marker_pos)
        if bracket_start == -1:
            logger.warning(f"Marker '{marker}' found but no '[' after it")
            return main_text, json_array
        
        # Find matching closing bracket
        bracket_count = 0
        bracket_end = -1
        for i in range(bracket_start, len(cleaned)):
            if cleaned[i] == '[':
                bracket_count += 1
            elif cleaned[i] == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    bracket_end = i + 1
                    break
        
        if bracket_end == -1:
            logger.warning(f"Unmatched brackets after marker '{marker}'")
            return main_text, json_array
        
        json_block = cleaned[bracket_start:bracket_end].strip()
        main_text = cleaned[:marker_pos].strip()
        
        try:
            parsed = json.loads(json_block)
            if isinstance(parsed, list):
                json_array = parsed
            elif isinstance(parsed, dict):
                json_array = [parsed]
            else:
                logger.warning(f"Parsed JSON is not list/dict: {type(parsed)}")
        except json.JSONDecodeError as e:
            logger.warning(f"JSON decode error: {e}")
            logger.warning(f"Problematic JSON (first 300 chars): {json_block[:300]}")
    else:
        # No marker - try to find any JSON array
        bracket_start = cleaned.find("[")
        if bracket_start != -1:
            bracket_count = 0
            bracket_end = -1
            for i in range(bracket_start, len(cleaned)):
                if cleaned[i] == '[':
                    bracket_count += 1
                elif cleaned[i] == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        bracket_end = i + 1
                        break
            
            if bracket_end != -1:
                json_block = cleaned[bracket_start:bracket_end].strip()
                main_text = (cleaned[:bracket_start] + cleaned[bracket_end:]).strip()
                try:
                    parsed = json.loads(json_block)
                    if isinstance(parsed, list):
                        json_array = parsed
                    elif isinstance(parsed, dict):
                        json_array = [parsed]
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON decode error (no marker): {e}")
    
    return main_text, json_array

def call_deepseek_hebrew(word: str) -> Dict[str, Any]:
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
    
    main_text, raw_derived = extract_json_array(text, "DERIVED_JSON:")
    
    # Validate derived items
    derived = []
    for item in raw_derived:
        if isinstance(item, dict) and all(k in item for k in ["hebrew", "translit", "english"]):
            derived.append(item)
        else:
            logger.warning(f"Skipping invalid derived item: {item}")
    
    return {
        "main_text": main_text.strip(),
        "derived": derived
    }

def call_deepseek_arabic(word: str) -> Dict[str, Any]:
    messages = [
        {"role": "system", "content": ARABIC_SYSTEM_PROMPT},
        {"role": "user", "content": ARABIC_USER_INSTRUCTIONS.format(word=word)}
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
    
    main_text, raw_buttons = extract_json_array(text, "BUTTONS_JSON:")
    
    # Validate button items
    buttons_data = []
    for item in raw_buttons:
        if isinstance(item, dict) and all(k in item for k in ["arabic", "translit", "english"]):
            buttons_data.append(item)
        else:
            logger.warning(f"Skipping invalid button item: {item}")
    
    return {
        "main_text": main_text.strip(),
        "buttons_data": buttons_data
    }

def call_deepseek_phrase(phrase: str) -> str:
    messages = [
        {"role": "system", "content": PHRASE_SYSTEM_PROMPT},
        {"role": "user", "content": PHRASE_USER_INSTRUCTIONS.format(phrase=phrase)}
    ]
    
    for _ in range(3):
        try:
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=TEMPERATURE
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"DeepSeek error, retrying: {e}")
            time.sleep(2)
    raise RuntimeError("DeepSeek failed after retries")

# --------------------
# Sheet operations
# --------------------
def append_hebrew_to_sheet(hebrew, translit, english, heb_root, ar_root, ar_examples):
    all_vals = sheet_hebrew.col_values(1)
    existing = all_vals[1:] if len(all_vals) > 1 else []
    
    if hebrew in existing:
        logger.info(f"Duplicate Hebrew skipped: {hebrew}")
        return False
    
    row = [hebrew, translit, english, heb_root, ar_root, ar_examples]
    sheet_hebrew.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended Hebrew: {hebrew}")
    return True

def append_arabic_to_sheet(english: str, arabic: str, translit: str, original_word: str):
    try:
        all_arabic = sheet_arabic.col_values(2)
        existing = all_arabic[1:] if len(all_arabic) > 1 else []
        
        if arabic in existing:
            logger.info(f"Duplicate Arabic skipped: {arabic}")
            return False
    except Exception as e:
        logger.warning(f"Error checking duplicates: {e}")
    
    row = [english, arabic, translit, original_word]
    sheet_arabic.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Appended Arabic: {arabic}")
    return True

# --------------------
# Helper functions
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
                # Remove leading "* " and any extra whitespace
                example = line.lstrip("* ").strip()
                if example:
                    examples.append(example)
        
        # FIXED BUG 2: Proper joining of examples
        if examples:
            ar_examples = "\n* ".join(examples)
    
    return heb_root, ar_root, ar_examples

def detect_language(text: str) -> str:
    """Detect if text is Hebrew, Arabic, or mixed with priority."""
    has_hebrew = bool(re.search(r'[\u05D0-\u05EA]', text))
    has_arabic = bool(re.search(r'[\u0600-\u06FF]', text))
    
    if has_hebrew and has_arabic:
        # Mixed - count which one dominates
        hebrew_chars = len(re.findall(r'[\u05D0-\u05EA]', text))
        arabic_chars = len(re.findall(r'[\u0600-\u06FF]', text))
        if hebrew_chars >= arabic_chars:
            return "hebrew"
        else:
            return "arabic"
    elif has_hebrew:
        return "hebrew"
    elif has_arabic:
        return "arabic"
    else:
        return "unknown"

def count_hebrew_words(text: str) -> int:
    tokens = text.strip().split()
    return sum(1 for t in tokens if re.search(r'[\u05D0-\u05EA]', t))

def is_single_hebrew_word(text: str) -> bool:
    cleaned = re.sub(r'[^\u05D0-\u05EA]', '', text.strip())
    return bool(HEBREW_WORD_RE.match(cleaned)) and len(text.strip().split()) == 1

def is_single_arabic_word(text: str) -> bool:
    cleaned = re.sub(r'[^\u0600-\u06FF\u0750-\u077F]', '', text.strip())
    return bool(ARABIC_WORD_RE.match(cleaned)) and len(text.strip().split()) == 1

def cleanup_last_results():
    """Prevent memory leak by limiting cache size"""
    if len(LAST_RESULTS) > MAX_CACHED_CHATS:
        # Remove oldest 20% of entries
        keys_to_remove = list(LAST_RESULTS.keys())[:MAX_CACHED_CHATS // 5]
        for key in keys_to_remove:
            del LAST_RESULTS[key]
        logger.info(f"Cleaned up {len(keys_to_remove)} cached entries")

# --------------------
# Telegram handlers
# --------------------
def handle_message(msg: Dict[str, Any]):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    
    # Handle /start command
    if text == "/start":
        send_message(chat_id, 
            "👋 <b>Welcome to Hebrew & Arabic Etymology Bot!</b>\n\n"
            "📝 <b>Single Hebrew word</b> → root, etymology & Arabic cognates\n"
            "   Example: send <b>מכין</b>\n\n"
            "🇸🇦 <b>Single Arabic word</b> → root, Hebrew cognate & common conjugations\n"
            "   Example: send <b>كتب</b>\n\n"
            "📖 <b>Hebrew phrase/sentence</b> → word-by-word breakdown\n"
            "   Example: send <b>האם הציפורן הפתוחה שלך פועלת</b>\n\n"
            "💾 Tap buttons to save derived words to your Google Sheet\n"
            "   - Hebrew words → 'Hebrew' tab\n"
            "   - Arabic words → 'Arabic' tab"
        )
        return
    
    # Detect language with priority
    lang = detect_language(text)
    
    if lang == "unknown":
        send_message(chat_id, "❗ Please send Hebrew or Arabic text.")
        return
    
    # ----- HEBREW MODE -----
    if lang == "hebrew":
        word_count = count_hebrew_words(text)
        
        # Phrase/sentence mode (2+ words)
        if word_count >= 2:
            send_message(chat_id, "🔍 Breaking down Hebrew phrase...")
            try:
                breakdown = call_deepseek_phrase(text)
                send_message(chat_id, breakdown)
            except Exception as e:
                logger.exception("Error processing Hebrew phrase")
                send_message(chat_id, f"❌ Error: {e}")
            return
        
        # Single Hebrew word mode
        if not is_single_hebrew_word(text):
            send_message(chat_id, "❗ Please send a single Hebrew word (letters only, no vowel points).")
            return
        
        send_message(chat_id, "🤖 Analyzing Hebrew word...")
        
        try:
            result = call_deepseek_hebrew(text)
            main_text = result["main_text"]
            derived = result["derived"]
            
            heb_root, ar_root, ar_examples = extract_roots_and_arabic(main_text)
            
            LAST_RESULTS[chat_id] = {
                "type": "hebrew",
                "heb_root": heb_root,
                "ar_root": ar_root,
                "ar_examples": ar_examples,
                "derived": derived
            }
            cleanup_last_results()
            
            buttons = []
            for i, d in enumerate(derived):
                hebrew = d.get("hebrew", "")
                translit = d.get("translit", "")
                english = d.get("english", "")
                
                if not hebrew or not translit or not english:
                    continue
                
                label = f"💾 {hebrew} - {translit} - {english[:40]}"
                if len(label) > 60:
                    label = label[:57] + "..."
                buttons.append([{
                    "text": label,
                    "callback_data": f"save_hebrew:{i}"
                }])
            
            reply_markup = {"inline_keyboard": buttons} if buttons else None
            send_message(chat_id, main_text, reply_markup=reply_markup)
        
        except Exception as e:
            logger.exception("Error processing Hebrew word")
            send_message(chat_id, f"❌ Error: {e}")
    
    # ----- ARABIC MODE -----
    elif lang == "arabic":
        if not is_single_arabic_word(text):
            send_message(chat_id, "❗ Please send a single Arabic word (letters only, no spaces).")
            return
        
        send_message(chat_id, "🤖 Analyzing Arabic word...")
        
        try:
            result = call_deepseek_arabic(text)
            main_text = result["main_text"]
            buttons_data = result["buttons_data"]
            
            LAST_RESULTS[chat_id] = {
                "type": "arabic",
                "original_word": text,
                "buttons_data": buttons_data
            }
            cleanup_last_results()
            
            buttons = []
            for i, item in enumerate(buttons_data):
                arabic = item.get("arabic", "")
                translit = item.get("translit", "")
                english = item.get("english", "")
                
                if not arabic or not translit or not english:
                    continue
                
                label = f"📖 {arabic} - {translit} - {english[:35]}"
                if len(label) > 60:
                    label = label[:57] + "..."
                buttons.append([{
                    "text": label,
                    "callback_data": f"save_arabic:{i}"
                }])
            
            if buttons:
                main_text += "\n\n<code>👇 Tap any word below to save it to your Arabic vocabulary sheet</code>"
            
            reply_markup = {"inline_keyboard": buttons} if buttons else None
            send_message(chat_id, main_text, reply_markup=reply_markup)
        
        except Exception as e:
            logger.exception("Error processing Arabic word")
            send_message(chat_id, f"❌ Error: {e}")

def handle_callback(cb: Dict[str, Any]):
    cb_id = cb["id"]
    chat_id = cb["message"]["chat"]["id"]
    data = cb["data"]
    
    # Hebrew save
    if data.startswith("save_hebrew:"):
        idx = int(data.split(":")[1])
        cached = LAST_RESULTS.get(chat_id)
        
        if not cached or cached.get("type") != "hebrew" or idx >= len(cached.get("derived", [])):
            answer_callback(cb_id, "❌ Expired")
            return
        
        d = cached["derived"][idx]
        hebrew = d.get("hebrew")
        translit = d.get("translit")
        english = d.get("english")
        
        if not hebrew or not translit or not english:
            answer_callback(cb_id, "❌ Invalid data")
            return
        
        try:
            saved = append_hebrew_to_sheet(
                hebrew,
                translit,
                english,
                cached.get("heb_root", "unknown"),
                cached.get("ar_root", "none"),
                cached.get("ar_examples", "none")
            )
            answer_callback(cb_id, "✔️ Saved to Hebrew tab" if saved else "⚠️ Duplicate")
        except Exception as e:
            logger.exception("Sheet append failed for Hebrew")
            answer_callback(cb_id, "❌ Error")
    
    # Arabic save
    elif data.startswith("save_arabic:"):
        idx = int(data.split(":")[1])
        cached = LAST_RESULTS.get(chat_id)
        
        if not cached or cached.get("type") != "arabic" or idx >= len(cached.get("buttons_data", [])):
            answer_callback(cb_id, "❌ Expired")
            return
        
        item = cached["buttons_data"][idx]
        arabic = item.get("arabic")
        translit = item.get("translit")
        english = item.get("english")
        original_word = cached.get("original_word", "")
        
        if not arabic or not translit or not english:
            answer_callback(cb_id, "❌ Invalid data")
            return
        
        try:
            saved = append_arabic_to_sheet(english, arabic, translit, original_word)
            answer_callback(cb_id, "✔️ Saved to Arabic tab" if saved else "⚠️ Duplicate")
        except Exception as e:
            logger.exception("Sheet append failed for Arabic")
            answer_callback(cb_id, "❌ Error")
    
    else:
        answer_callback(cb_id, "❌")

# --------------------
# Long polling loop with FIXED allowed_updates
# --------------------
def get_updates(offset=None):
    params = {
        "timeout": 30,
        "allowed_updates": ["message", "callback_query"]  # FIXED BUG 4: Filter update types
    }
    if offset:
        params["offset"] = offset
    r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    r.raise_for_status()
    return r.json()["result"]

def main():
    logger.info("Starting Hebrew+Arabic bot...")
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
                else:
                    # Log but skip other update types (like edited_message, channel_post)
                    logger.debug(f"Skipping non-handled update type: {u.keys()}")
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()