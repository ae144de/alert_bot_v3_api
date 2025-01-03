from telethon import TelegramClient
import requests

async def send_alert_notification(targetPhoneNumber, message):
    # 1. Replace these with your own values from https://my.telegram.org/
    api_id = 21893471  # <-- your API ID
    api_hash = '040a4c29637f6dce451130b400489720'
    phone_number = '+9054429229007'  # <-- your phone number (including country code)

    # 2. Create and start the TelegramClient
    client = TelegramClient('session_name', api_id, api_hash)

    await client.start(phone=phone_number)
    await client.send_message(targetPhoneNumber, message)
    print(f'Message sent to {targetPhoneNumber}')
    
    
def send_telegram_message(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message
    }
    response = requests.post(url, data=data)
    return response.json()