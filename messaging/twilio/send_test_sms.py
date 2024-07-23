from twilio.rest import Client
from dotenv import load_dotenv
import os

load_dotenv()

account_sid = os.getenv('TWILIO_ACCOUNT_SID')
auth_token = os.getenv('TWILIO_AUTH_TOKEN')
client = Client(account_sid, auth_token)

message = client.messages.create(
    from_=os.getenv('TWILIO_PHONE_NUMBER'),
    to=os.getenv('SPAIN_PHONE_NUMBER'),
    body='Test from simple-cloud!'
)

print(message.sid)