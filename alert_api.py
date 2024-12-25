from flask import Flask, request, jsonify
import threading
import asyncio
import json
import firebase_admin
from firebase_admin import credentials, db
import websockets
from datetime import datetime
import uuid
from flask_cors import CORS
# from auth import token_required
from dotenv import load_dotenv
import os
from functools import wraps
import jwt
from flask_jwt_extended import JWTManager, verify_jwt_in_request


load_dotenv()

app = Flask(__name__)
# CORS(app, resources={r"/*":{"origins": "https://alert-bot-v3.vercel.app"}})
CORS(app, resources={
    r"/api/*": {
        "origins": "https://alert-bot-v3.vercel.app",
        "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "supports_credentials": True,
    }
})
FIREBASE_CREDENTIAL = "/opt/secrets/alert_bot_v3_api_firebase.json"
# Initialize the firebase admin.
cred = credentials.Certificate(FIREBASE_CREDENTIAL)
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://lambdacryptobotproject.firebaseio.com/'
})

NEXTAUTH_SECRET = os.getenv('NEXTAUTH_SECRET')
print(f'NEXTAUTH_SECRET: {NEXTAUTH_SECRET}')

if not NEXTAUTH_SECRET:
    raise ValueError("Missing NEXTAUTH_SECRET environment variable.")



# JWT Verification Decorator
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        # JWT is passed in the request header
        print(f"[**REQUEST]: {request.headers}")
        print(f"REQUEST_TYPE: {str(type(request.headers))}")
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            print(f"Bearer: {dir(bearer)} -- {repr(bearer)}")
            if bearer and bearer.startswith('Bearer '):
                print("There is a Bearer and starts with Bearer !!!")
                token = bearer.split(' ')[1]
                print(f"**TOKEN: {token}")

        if not token:
            return jsonify({'message': 'Token is missing!'}), 401

        try:
            # Decode the token using NEXTAUTH_SECRET
            print(f"**TOKEN: {token}")
            data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=["HS256"])
            # current_user_email = data['email']
            print(f"**DATA: {data}")
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token has expired!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Invalid token!'}), 401

        return f(*args, **kwargs)

    return decorated

alerts_ref = db.reference('alerts');
# Load NEXTAUTH_SECRET from environment variables
# NEXTAUTH_SECRET = os.getenv('NEXTAUTH_SECRET')

async_loop = None
WS_URL = "wss://fstream.binance.com/ws"
subscriptions = set()
ws_connection = None

async def websocket_handler():
    global subscriptions, ws_connection
    #Establish single base connection.
    async with websockets.connect(WS_URL) as ws:
        ws_connection = ws
        print("Websocket base connection established.")
        # After connection, subscribe to all symbols from existing alerts.
        await subscribe_existing_symbols()
        #Keep listening for incoming messages.
        async for message in ws:
            data = json.loads(message)
            # print(f"[**Message]: {data["s"]}")
            # print(f"[##SUBSCRIPTIONS]: {subscriptions}")
            event_type = data.get("e")
            if event_type == "kline":
                symbol = data["s"]
                close_price = float(data["k"]["c"])
                print(f"{symbol} ---- {close_price}")
                #print(f"{symbol} -- {close_price}")
                # Update all alerts for this symbol with close_price
                await update_and_check_alerts(symbol, close_price)


# async def websocket_handler():
    
#     global subscriptions, ws_connection
    
#     #Establish single base connection.
#     async with websockets.connect(WS_URL) as ws:
#         ws_connection = ws
#         print("Websocket base connection established.")
        
#         # After connection, subscribe to all symbols from existing alerts.
#         await subscribe_existing_symbols()
        
#         #Keep listening for incoming messages.
#         async for message in ws:
#             data = json.loads(message)
#             print(f"[**]- Message: {data}")
#             print(f"[##SUBSCRIPTIONS]: {subscriptions}")
#             event_type = data.get("e")
#             if event_type == "kline":
#                 symbol = data["s"]
#                 close_price = float(data["k"]["c"])
                
#                 # Update all alerts for this symbol with close_price
#                 await update_and_check_alerts(symbol, close_price)

def verify_jwt_token(token):
    try:
        # Decode with the same algo NextAuth uses (HS256 by default).
        payload = jwt.decode(token, NEXTAUTH_SECRET, algorithms=["HS256"])
        print(f"TOKEN: {token} --- JWT_SECRET: {NEXTAUTH_SECRET}")
        return payload
    except jwt.ExpiredSignatureError:
        return None  # Token has expired
    except jwt.InvalidTokenError:
        return None  # Invalid token

def requires_auth(f):
    """Decorator to protect Flask routes with JWT verification."""
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", None)
        if not auth_header or not auth_header.startswith("Bearer "):
            return jsonify({"error": "Authorization header missing"}), 401

        token = auth_header.split("Bearer ")[1]
        payload = verify_jwt_token(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401
        print(f"AUTH HEADER: {auth_header}")
        # If we need user info from the token:
        request.user = payload
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

async def update_and_check_alerts(symbol, close_price):
    
    current_alerts = alerts_ref.get() or {}
    to_delete = []
        
    for key, alert in current_alerts.items():
        print(f"[*ALERT*]: {alert} --- [*KEY*]: {key}")
        if alert.get("symbol").upper() == symbol.upper():
            
            #Check condition
            operator = alert["operator"]
            alert_value = float(alert["value"])

            print(f" ==> Close Price: {close_price} --- Operator: {operator} --- Alert Value: {alert_value}")
            
            if evaluate_condition(close_price, operator, alert_value):
                # to_delete.append(key)
                print(f"Alert {key} for {symbol} triggerend and deleted !!!")
                alerts_ref.child(key).delete()
                await unsubscribe_symbol(symbol, key)
                
    # # Delete satisfied alerts
    # for key in to_delete:
    #     # alerts_ref.child(key).delete()
    #     # Notify or log
    #     print(f"Alert {key} for {symbol} triggerend and deleted !!!")
    #     await unsubscribe_symbol(symbol, key)
        
def evaluate_condition(price, operator, value):
    if operator == '>':
        return price > value
    elif operator == '<':
        return price < value
    elif operator == '>=':
        return price >= value
    elif operator == '<=':
        return price <= value
    return False

async def subscribe_symbol(symbol, alert_id):
    
    global subscriptions, ws_connection
    symbol = symbol.lower()
    
    if alert_id in subscriptions:
        return # If already subscribed.
    
    # Wait until ws connection established.
    while ws_connection is None:
        await asyncio.sleep(1)
        
    #Send subscription message to the base connection.
    msg = {
        "method": "SUBSCRIBE",
        "params": [f"{symbol}@kline_1m"],
        "id": int(uuid.uuid4().int & (1<<31)-1 ) 
    }
    
    await ws_connection.send(json.dumps(msg))
    subscriptions.add((alert_id, symbol))
    print(f"Subscribed to {symbol} kline(1m) stream.")

async def unsubscribe_symbol(symbol, alert_id):
    global subscriptions, ws_connection
    symbol = symbol.lower()
    
    # Get subs that sub.symbol == symbol. And also get the number 
    # of how many of them.
    matching_alerts = [sub for sub in subscriptions if sub[1] == symbol]
    number_of_matching_subs = len(matching_alerts)
    print(f"[++MATCHING_ALERTS]: {matching_alerts} --- [++NUMOFMA]: {number_of_matching_subs}")
    if matching_alerts and number_of_matching_subs > 1:
        for sub in subscriptions:
            if sub[0] == alert_id:
                subscriptions.remove(sub)
                break
    else:
        for sub in subscriptions:
            print(f"CORRESPONDING ALERT_ID: {alert_id}")
            if sub[0] == alert_id:
                subscriptions.remove(sub)
                msg = {
                    "method": "UNSUBSCRIBE",
                    "params": [f"{sub[1]}@kline_1m"],
                    "id": int(uuid.uuid4().int & (1<<31)-1)
                }
                await ws_connection.send(json.dumps(msg))
                print(f"Unsubscribed from {symbol}.")
                break
        
async def subscribe_existing_symbols():
    # When the server starts or websocket restarts, fetch alerts and resubscribe to symbol.
    current_alerts = alerts_ref.get() or {}
    symbols_to_subscribe = set()
    
    for key, alert in current_alerts.items():
        symbol = alert.get('symbol')
        alert_id = key
        if symbol:
            symbols_to_subscribe.add((symbol.upper(), alert_id))
    
    # Subscribe to each symbol.
    for sym in symbols_to_subscribe:
        await subscribe_symbol(sym[0], sym[1])
# --------------------
# REST API Endpoints
# --------------------

@app.route('/api/users/updatePhoneNumber', methods=['POST'])
# @token_required
@requires_auth
def update_phone_number():
    data = request.get_json()
    phone_number = data.get("phoneNumber", "")
    
    if not phone_number:
        return jsonify({'message': 'Phone number is required.'}), 400
    try:
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            if bearer and bearer.startswith('Bearer '):
                token = bearer.split('Bearer ')[1]
        user_data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])

        userId = user_data['email'].split("@")[0].replace('.', '_')
        ref = db.reference("users")
        user_ref = ref.child(userId).get()

        if user_ref:
            
            #Update the phoneNumber
            ref.child(userId).update({"phoneNumber": phone_number})
            return jsonify({"message": "Phone number updated successfully !"}), 200

        user_id = user_email.replace(".", "_")
        ref = db.reference(f"{user_id}")
        ref.update({"phoneNumber": phone_number})

        return jsonify({'message': "Phone number updated successfully."}), 200
    except Exception as e:
        return jsonify({"message": f"Error updating phone number: {str(e)}"}), 500

    # try:
    #     user_key = current_user_email.replace('.', '%2E')  # Encode email for Firebase key
    #     user_ref = db.reference(f'users/{user_key}')
    #     user_ref.update({'phoneNumber': phone_number})
    #     return jsonify({'message': 'Phone number updated successfully.'}), 200
    # except Exception as e:
    #     return jsonify({'message': f'An error occurred: {str(e)}'}), 500

@app.route('/api')
def hello_server():
    return "Hello_World!!!"

@app.route('/api/users/getUserData', methods=['GET'])
# @token_required
@requires_auth
def get_user_data():
    # Retrieves the user's data (including the phone number) based on their email.
    token = None
    try:
        # user_key = current_user_email.replace('.', '%2E')
        # user_key = db.reference(f'users/{user_key}')
        # user_data = user_ref.get()

        # if not user_data:
        #     return jsonify({'message': 'No data found for this user.'}), 404

        # return jsonify({
        #     'email': current_user_email,
        #     'phoneNumber': user_data.get('phoneNumber', ''),
        #     'name': user_data.get('name', '')
        # })
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            print(f"Bearer: {bearer}")
            if bearer and bearer.startswith('Bearer '):
                token = bearer.split('Bearer ')[1]
                print(f"Token--: {token}")
        data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])
        print(f"User Token Data: {data}")

        userId = data['email'].split("@")[0].replace('.','_')
        ref = db.reference("users")
        user_ref = ref.child(userId).get()

        if user_ref:
            return jsonify({"phoneNumber": user_ref.get("phoneNumber", "")}), 200
        else:
            #Create new user
            new_user = {
                "phoneNumber": "-",
                "alerts": []
            }
            ref.child(userId).set(new_user)
            print("User created successfully !!!")
            return jsonify({"message": "User created successfully !", "phoneNumber": "-"}), 200

        print(f"CURRENT USER:")
        print("GET USER DATA WORKED !!!")
        return jsonify({'message': '***GET_USER_DATA***'})

    except Exception as e:
        return jsonify({'message': f'Error retrieving user data: {str(e)}'}), 500

@app.route('/api/alerts', methods=['POST'])
@requires_auth
def create_alert():
    data = request.get_json()
    print(data)
    symbol = data.get('selectedSymbol')
    operator = data.get('operator')
    value = data.get('value')
    type = data.get('type')
    created_at = data.get('created_at')
    status = data.get('status')

    if not symbol or operator not in ['>', '<', '>=', '<=', '=='] or value is None:
        return jsonify({'error': 'Invalid payload'}), 400

    # # Push alert to Firebase
    # new_alert_ref = alerts_ref.push({
    #     'symbol': symbol.upper(),
    #     'operator': operator,
    #     'value': float(value)
    # })

    try:
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            if bearer and bearer.startswith('Bearer '):
                token = bearer.split('Bearer ')[1]
        user_data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])
        userId = user_data['email'].split("@")[0].replace('.','_')
        ref = db.reference("users")
        user_ref = ref.child(userId).get()
        
        #Retrieve alerts.
        current_alerts = user_ref.get("alerts", [])

        
        

        # user_key = current_user_email.replace('.', '%2E') # Encode email for Firebase key.
        # alerts_ref = db.reference(f'reference/{user_key}')
    
        current_date = datetime.now()
        formatted_current_date = current_date.strftime("%d%m%Y%H%M%S")
        alert_id = symbol+"_tickerAlert"+"_"+formatted_current_date
    
        
        new_alert_ref = {
            'symbol': symbol.upper(),
            'operator': operator,
            'value': float(value),
            'type': type,
            'created_at': created_at,
            'status': status,
            # 'userEmail': current_user_email,
        }
        # alerts_ref.push(new_alert_ref)¨

        # current_alerts.append(new_alert_ref)
        # ref.child(userId).update({"alerts":current_alerts})
        ref.child(userId).child('alerts').child(alert_id).set(new_alert_ref)


        #Schedule a subscription task.
        asyncio.run_coroutine_threadsafe(subscribe_symbol(symbol, alert_id), async_loop)
        
        return jsonify({'status': 'created', 'id': alert_id}), 201
    except Exception as e:
        return jsonify({'message' : f'An error occured: {str(e)}'}), 500

@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    snapshot = alerts_ref.get() or {}
    # Convert to list
    alerts_list = []
    for key, val in snapshot.items():
        alerts_list.append({
            'id': key,
            'symbol': val.get('symbol'),
            'operator': val.get('operator'),
            'value': val.get('value'),
            'status': val.get('status'),
            'type': val.get('type'),
            'created_at': val.get('created_at'),
        })
    return jsonify(alerts_list)

@app.route('/api/alerts/<id>', methods=['DELETE'])
@requires_auth
def delete_alert(alert_id):
    
    # ref = alerts_ref.child(id)
    # if ref.get() is not None:
    #     ref.delete()
    #     return jsonify({'status': 'deleted'}), 200
    # else:
    #     return jsonify({'error': 'Alert not found'}), 404   

    try:
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            if bearer and bearer.startswith('Bearer '):
                token = bearer.split('Bearer ')[1]
        user_data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])
        userId = user_data['email'].split("@")[0].replace('.','_')

        alert_ref = db.reference('users').child(userId).child('alerts').child(alert_id)

        existing_alert = alert_ref.get()
        if not existing_alert:
            return jsonify({"message": 'Alert not found !'}), 404

        #Delete the alert
        alert_ref.delete()
        return jsonify({"message": 'Alert deleted successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def start_async_loop():
    global async_loop
    async_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(async_loop)
    async_loop.run_until_complete(websocket_handler())
    
threading.Thread(target=start_async_loop, daemon=True).start()


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000 ,debug=False)