# Hebrew Etymology Telegram Bot

## Setup Instructions

### 1. Install Python Dependencies
```bash
pip install requests openai gspread google-auth
```

### 2. Configure Credentials

**Edit `.env` file:**
- Replace `TELEGRAM_BOT_TOKEN` with token from @BotFather
- Replace `DEEPSEEK_API_KEY` with your DeepSeek API key
- Replace `GOOGLE_SHEET_ID` with your Google Sheet ID (from URL)

**Replace `service_account.json`:**
- Download your actual service account JSON from Google Cloud Console
- Replace the template file entirely

### 3. Share Google Sheet
Share your Google Sheet with the service account email found in `service_account.json`:
```
"client_email": "xxx@xxx.iam.gserviceaccount.com"
```
Give it **Editor** permissions.

### 4. Run the Bot

**Option A: Windows CMD**
```cmd
set TELEGRAM_BOT_TOKEN=your_token
set DEEPSEEK_API_KEY=your_key
set GOOGLE_SHEET_ID=your_sheet_id
python bot.py
```

**Option B: Git Bash**
```bash
source .env
python bot.py
```

**Option C: PowerShell**
```powershell
$env:TELEGRAM_BOT_TOKEN="your_token"
$env:DEEPSEEK_API_KEY="your_key"
$env:GOOGLE_SHEET_ID="your_sheet_id"
python bot.py
```

### 5. Test in Telegram
1. Send `/start` to your bot
2. Send a Hebrew word like `מכין`
3. Click buttons to save words
4. Check your Google Sheet

## Files
- `bot.py` - Main bot code
- `.env` - Environment variables (EDIT THIS)
- `service_account.json` - Google credentials (REPLACE THIS)
- `README.md` - This file

## Troubleshooting
See the main setup guide for detailed troubleshooting steps.
