# Trading Account Reminder Bot

Lightweight Telegram bot for tracking trading accounts with per-user privacy.

Each Telegram user sees only their own accounts. Friends can use the same bot without seeing your accounts.

## Run

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

Put your BotFather token in `.env` first.

## Latest Behavior

- View accounts shows buttons by account ID.
- Clicking an account ID opens only that account's details.
- Menu/list messages delete themselves after you choose an option.
- Reminder messages are not deleted.
- Reminders check every 30 seconds.
- Each account has its own reminder time, seller, country, and custom account ID.
