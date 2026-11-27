"""Create the explicitly requested hackathon controller account, if absent.
Run on a trusted machine after configuring .env; do not ship the service key to browsers.
"""
import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
url=os.environ['SUPABASE_URL']; key=os.environ['SUPABASE_SERVICE_ROLE_KEY']
email=os.getenv('CONTROLLER_EMAIL','controller@syncria.ai')
client=create_client(url,key)
page=1
while True:
    users=client.auth.admin.list_users(page=page,per_page=100)
    if any(u.email and u.email.lower()==email.lower() for u in users):
        print('Controller already exists; password was not changed.')
        break
    if len(users)<100:
        client.auth.admin.create_user({'email':email,'password':'syncria123','email_confirm':True})
        print('Demo controller created with confirmed email.')
        break
    page+=1
