"""
Quick script to verify credentials are set correctly
Run: python test_credentials.py
"""
import os
import json

print("=" * 50)
print("CREDENTIAL CHECK")
print("=" * 50)

# Check environment variables
token = os.environ.get("TELEGRAM_BOT_TOKEN")
deepseek = os.environ.get("DEEPSEEK_API_KEY")
sheet_id = os.environ.get("GOOGLE_SHEET_ID")

print("\n📋 Environment Variables:")
print(f"TELEGRAM_BOT_TOKEN: {'✅ Set' if token else '❌ Missing'}")
if token and not token.startswith("REPLACE"):
    print(f"  Preview: {token[:20]}...")
    
print(f"DEEPSEEK_API_KEY: {'✅ Set' if deepseek else '❌ Missing'}")
if deepseek and not deepseek.startswith("REPLACE"):
    print(f"  Preview: {deepseek[:15]}...")
    
print(f"GOOGLE_SHEET_ID: {'✅ Set' if sheet_id else '❌ Missing'}")
if sheet_id and not sheet_id.startswith("REPLACE"):
    print(f"  Value: {sheet_id}")

# Check service account file
print("\n📄 Service Account File:")
if os.path.exists("service_account.json"):
    print("✅ service_account.json exists")
    try:
        with open("service_account.json") as f:
            data = json.load(f)
            if "YOUR_PROJECT_ID" in str(data):
                print("⚠️  WARNING: Still contains template values")
            else:
                print(f"✅ Project ID: {data.get('project_id', 'N/A')}")
                print(f"✅ Client Email: {data.get('client_email', 'N/A')}")
    except Exception as e:
        print(f"❌ Error reading file: {e}")
else:
    print("❌ service_account.json NOT FOUND")

print("\n" + "=" * 50)
print("Next steps:")
if not token or token.startswith("REPLACE"):
    print("1. Edit .env file with your actual credentials")
if not os.path.exists("service_account.json") or "YOUR_PROJECT_ID" in open("service_account.json").read():
    print("2. Replace service_account.json with real Google credentials")
print("3. Run: source .env (Git Bash) or set env vars (CMD)")
print("4. Run: python bot.py")
print("=" * 50)
