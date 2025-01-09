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
from telethon_message_sender import send_alert_notification, send_telegram_message
from colorama import init, Fore, Style, Back

load_dotenv()

# Initialize colorama
init(autoreset=True)
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
subscribed_symbols = set()
ws_connection = None
subscriptions_lock = asyncio.Lock()
previous_close_prices = {}

async def websocket_handler():
    global subscriptions, ws_connection, subscribed_symbols
    #Establish single base connection.
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None, ping_timeout=40, close_timeout=5) as ws:
                ws_connection = ws
                print("Websocket base connection established.")
                # After connection, subscribe to all symbols from existing alerts.
                await subscribe_existing_symbols()
                #Keep listening for incoming messages.
                async for message in ws:
                    print(f"Message received: {message}")
                    if message == 'ping':
                        await ws.send('pong')
                        print(Fore.YELLOW + "Pong sent.")
                    else:
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
        except websockets.ConnectionClosedError:
            print("Connection closed. Reconnecting...")
            ws_connection = None
            subscribed_symbols = set()
            subscriptions = set()
            await asyncio.sleep(5)
            
        
        except Exception as e:
            print(f"Websocket error: {str(e)}")
            ws_connection = None
            subscribed_symbols = set()
            subscriptions = set()
            await asyncio.sleep(5)
            


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
    
    # current_alerts = alerts_ref.get() or {}
    related_alerts = alerts_ref.order_by_child('symbol').equal_to(symbol).get()

    to_delete = []
        
    for key, alert in related_alerts.items():
        print(Fore.CYAN + f"[*ALERT*]: {alert} --- [*KEY*]: {key}")
        if alert.get("symbol").upper() == symbol.upper() and alert.get("status") == "Active":
            
            #Check condition
            operator = alert["operator"]
            alert_value = float(alert["value"])
            lower_bound = alert['lowerBound']
            upper_bound = alert['upperBound']
            # lower_bound = alert.get('lowerBound', None)
            # upper_bound = alert.get('upperBound', None)

            if lower_bound != '-' and upper_bound != '-':
                lower_bound = float(lower_bound)
                upper_bound = float(upper_bound)
            elif lower_bound != '-':
                lower_bound = float(lower_bound)
                upper_bound = None
            elif upper_bound != '-':
                upper_bound = float(upper_bound)
                lower_bound = None
            else:
                lower_bound = None
                upper_bound = None

            print(Back.GREEN + f" ==> Close Price: {close_price} --- Operator: {operator} --- Alert Value: {alert_value}")
            
            if evaluate_condition(close_price, alert_value, operator, symbol, lower_bound, upper_bound):
                # to_delete.append(key)
                print(f"Alert {key} for {symbol} triggerend and deleted !!!")
                # alerts_ref.child(key).delete()
                message = f"{symbol} alert done! Close: {close_price} -- Value: {alert_value} -- Operator: {operator}"
                # test_message = 'This is the test message sent from the telethon!!!'
                # alert_phone_number = "+905367906728"+alert.get('userPhoneNumber')
                # print(f"ALERT_PHONE_NUMBER: {alert_phone_number}")
                
                # await send_alert_notification('+905367906728', test_message)
                send_telegram_message(alert.get('botToken'), alert.get('chatId'), message)
                await unsubscribe_symbol(symbol, key)
                alerts_ref.child(key).update({"status": "Done"})
                

    # Fetch alert that 

    # # Delete satisfied alerts
    # for key in to_delete:
    #     # alerts_ref.child(key).delete()
    #     # Notify or log
    #     print(f"Alert {key} for {symbol} triggerend and deleted !!!")
    #     await unsubscribe_symbol(symbol, key)
        
# def evaluate_condition(price, operator, value):
#     global previous_close_prices

#     if operator == '>':
#         return price > value
#     elif operator == '<':
#         return price < value
#     elif operator == '>=':
#         return price >= value
#     elif operator == '<=':
#         return price <= value
#     return False

def evaluate_condition(price, threshold, operator, symbol, lower_bound, upper_bound):
    # price: Current price
    # threshold: The value to compare with
    # operator: The comparison operator
    # symbol: The symbol
    # previous_price: The previous price
    # percentage: The percentage change
    # channel: The channel range

    global previous_close_prices
    previous_price = previous_close_prices.get(symbol)

    if operator == 'Crossing':
        result = previous_price is not None and ((previous_price < threshold and price >= threshold) or (previous_price > threshold and price <= threshold))
    elif operator == 'Crossing Up':
        result = previous_price is not None and previous_price < threshold and price >= threshold
    elif operator == 'Crossing Down':
        result = previous_price is not None and previous_price > threshold and price <= threshold
    elif operator == 'Entering Channel':
        result = previous_price is not None and lower_bound is not None and upper_bound is not None and (lower_bound <= price <= upper_bound) and not (lower_bound <= previous_price <= upper_bound)
    elif operator == 'Exiting Channel':
        result = previous_price is not None and lower_bound is not None and upper_bound is not None and not (lower_bound <= price <= upper_bound) and (lower_bound <= previous_price <= upper_bound)
    elif operator == 'Moving Up %':
        result = previous_price is not None and ((price - previous_price) / previous_price) * 100 >= threshold
    elif operator == 'Moving Down %':
        result = previous_price is not None and ((previous_price - price) / previous_price) * 100 >= threshold
    elif operator == 'Greater than':
        result = price > threshold
    elif operator == 'Less than':
        result = price < threshold
    else:
        raise ValueError(f"Unknown operator: {operator}")

    # Update the previous value
    previous_price[symbol] = price

    return result

async def subscribe_symbol(symbol, alert_id):
    
    global subscriptions, ws_connection, subscribed_symbols
    symbol = symbol.lower()
    
    async with subscriptions_lock:
        # Check if the alert_id is already in the subscriptions
        if any(sub[0] == alert_id for sub in subscriptions):
            print(f"Already subscribed to {symbol} for alert {alert_id}.")
            return  # If already subscribed.

        # Check if the symbol is already in the subscribed_symbols set
        if symbol in subscribed_symbols:
            print(f"Symbol {symbol} is already subscribed.")
            subscriptions.add((alert_id, symbol))
            return

    # Wait until ws connection is established.
    while ws_connection is None:
        await asyncio.sleep(1)

    # Send subscription message to the base connection.
    msg = {
        "method": "SUBSCRIBE",
        "params": [f"{symbol}@kline_1m"],
        "id": int(uuid.uuid4().int & (1 << 31) - 1)
    }

    await ws_connection.send(json.dumps(msg))
    print(f"Subscribed to {symbol} kline(1m) stream.")

    async with subscriptions_lock:
        # Add the subscription to the sets after sending the message
        subscriptions.add((alert_id, symbol))
        subscribed_symbols.add(symbol)

    await asyncio.sleep(2)
    print(f'SUBSCRIPTIONS: {subscriptions}')
    print(f'SUBSCRIBED_SYMBOLS: {subscribed_symbols}')
 

async def unsubscribe_symbol(symbol, alert_id):
    global subscriptions, ws_connection, subscribed_symbols
    symbol = symbol.lower()
    
    async with subscriptions_lock:
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
                    subscribed_symbols.remove(symbol)
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

@app.route('/api/users/connectUserBot', methods=['POST'])
@requires_auth
def connect_user_bot():
    data = request.get_json()
    print(f"DATA: {data}")
    phone_number = data.get('phoneNumber', "")
    bot_token = data.get('botToken', "")
    chat_id = data.get('chatId', "")
    
    if not phone_number or not bot_token or not chat_id:
        return jsonify({'error': 'Invalid payload'}), 400
    try:
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            if bearer and bearer.startswith('Bearer '):
                token = bearer.split('Bearer ')[1]
        user_data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])
        userId = user_data['email'].split("@")[0].replace('.','_')
        ref = db.reference("users")
        user_ref = ref.child(userId).get()
        if user_ref:
            ref.child(userId).update({"phoneNumber": phone_number, "botToken": bot_token, "chatId": chat_id})
            return jsonify({"message": "User bot connected successfully !"}), 200
        
        ref.child(userId).update({"phoneNumber": phone_number, "botToken": bot_token, "chatId": chat_id})
        return jsonify({"message": "User bot connected successfully !"}), 200
    except Exception as e:
        return jsonify({"message": f"Error connecting user bot: {str(e)}"}), 500

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

        # user_id = user_email.replace(".", "_")
        # ref = db.reference(f"{user_id}")
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
                "botToken": "-",
                "chatId": "-",
                
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
    # symbol = data.get('selectedSymbol')
    # operator = data.get('operator')
    # value = data.get('value')
    # type = data.get('type')
    # created_at = data.get('created_at')
    # status = data.get('status')

    symbol = data.get('selectedSymbol')
    operator = data.get('operator')
    value = data.get('value')
    type = data.get('type')
    created_at = data.get('created_at')
    status = data.get('status')
    lower_bound = data.get('lowerBound')
    upper_bound = data.get('upperBound')
    alert_title = data.get('alertTitle')
    expiration_date = data.get('expiration')
    trigger = data.get('trigger')
    message = data.get('message')

    if lower_bound is None:
        lower_bound = '-'
    if upper_bound is None:
        upper_bound = '-'
    if expiration_date is None:
        expiration_date = '-'

    valid_operators = ['Crossing', 'Crossing Up', 'Crossing Down', 'Entering Channel', 'Exiting Channel', 'Moving Up %', 'Moving Down %', 'Greater than', 'Less than']
    if not symbol or operator not in valid_operators or value is None:
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
        # user_phone_number = user_ref.child('phoneNumber').get()
        user_phone_number_ref = db.reference(f'users/{userId}/phoneNumber')
        user_phone_number = user_phone_number_ref.get()
        
        user_bot_token_ref = db.reference(f'users/{userId}/botToken')
        user_bot_token = user_bot_token_ref.get()
        user_chat_id_ref = db.reference(f'users/{userId}/chatId')
        user_chat_id = user_chat_id_ref.get()
        
        #Retrieve alerts.
        # current_alerts = user_ref.get("alerts", [])

        
        

        # user_key = current_user_email.replace('.', '%2E') # Encode email for Firebase key.
        # alerts_ref = db.reference(f'reference/{user_key}')
    
        current_date = datetime.now()
        formatted_current_date = current_date.strftime("%d%m%Y%H%M%S")
        alert_id = symbol+"_tickerAlert"+"_"+formatted_current_date
    
        
        new_alert_ref = {
            'user_id': userId,
            'alert_id': alert_id,
            'symbol': symbol.upper(),
            'operator': operator,
            'value': float(value),
            'type': type,
            'created_at': created_at,
            'status': status,
            'lowerBound': lower_bound,
            'upperBound': upper_bound,
            'alertTitle': alert_title,
            'expiration': expiration_date,
            'trigger': trigger,
            'message': message,
            'userPhoneNumber': user_phone_number,
            'botToken': user_bot_token,
            'chatId': user_chat_id,
            # 'userEmail': current_user_email,
        }
        # alerts_ref.push(new_alert_ref)¨
        
        alerts_ref.child(alert_id).set(new_alert_ref)
        #df

        # current_alerts.append(new_alert_ref)
        # ref.child(userId).update({"alerts":current_alerts})
        # ref.child(userId).child('alerts').child(alert_id).set(new_alert_ref)


        #Schedule a subscription task.
        asyncio.run_coroutine_threadsafe(subscribe_symbol(symbol, alert_id), async_loop)
        
        return jsonify({'status': 'created', 'id': alert_id}), 201
    except Exception as e:
        return jsonify({'message' : f'An error occured: {str(e)}'}), 500

@app.route('/api/alerts', methods=['GET'])
@requires_auth
def get_alerts():
    # snapshot = alerts_ref.get() or {}
    # # Convert to list
    # alerts_list = []
    # for key, val in snapshot.items():
    #     alerts_list.append({
    #         'id': key,
    #         'symbol': val.get('symbol'),
    #         'operator': val.get('operator'),
    #         'value': val.get('value'),
    #         'status': val.get('status'),
    #         'type': val.get('type'),
    #         'created_at': val.get('created_at'),
    #     })
    # return jsonify(alerts_list)
    print('++++++++++++++++++++++++++ GET ALERTS ENDPOINT ++++++++++++++++++++++++++')
    user_alerts_as_list = []
    try:
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization'].strip()
            if bearer and bearer.startswith('Bearer '):
                token = bearer.split('Bearer ')[1]
        user_data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])
        userId = user_data['email'].split("@")[0].replace('.','_')
        
        ref = db.reference('users')
        user_ref = ref.child(userId).get()

        print(f'User Ref: ')
        if not user_ref:
            return jsonify({"error": 'User not found !'}), 404

        

        #Convert alerts dict to a list
        # alerts_list = list(alerts.values()) if isinstance(alerts, dict) else []

        alerts_for_user = alerts_ref.order_by_child('user_id').equal_to(userId).get()
        for key, alert in alerts_for_user.items():
            user_alerts_as_list.append(alert)
        print(f'Alerts For the User(as List): {user_alerts_as_list}')
        print(f'Alerts For the User: {alerts_for_user}')

        return jsonify({'alerts':user_alerts_as_list}), 200
    except Exception as e:
        return jsonify({'error':f'Error retrieving alerts: {str(e)}'}), 500

@app.route('/api/alerts/<alert_id>', methods=['DELETE'])
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
        
        coin_symbol = alert_id.split("_")[0]
        
        alert_ref = db.reference('alerts').child(alert_id)

        existing_alert = alert_ref.get()
        if not existing_alert:
            return jsonify({"message": 'Alert not found !'}), 404

        #Delete the alert
        alert_ref.delete()
        asyncio.run(unsubscribe_symbol(coin_symbol, alert_id))
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