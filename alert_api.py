from flask import Flask, request, jsonify
import threading
import asyncio
import json
import firebase_admin
from firebase_admin import credentials, db
import websockets
from datetime import datetime
import uuid


app = Flask(__name__)

# Initialize the firebase admin.
cred = credentials.Certificate("lambdacryptobotproject-firebase-adminsdk.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://lambdacryptobotproject.firebaseio.com/'
})

alerts_ref = db.reference('alerts');

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
            print(f"[**]- Message: {data}")
            print(f"[##SUBSCRIPTIONS]: {subscriptions}")
            event_type = data.get("e")
            if event_type == "kline":
                symbol = data["s"]
                close_price = float(data["k"]["c"])
                
                # Update all alerts for this symbol with close_price
                await update_and_check_alerts(symbol, close_price)

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
        await subscribe_symbol(sym[0], sym[1]);
# --------------------
# REST API Endpoints
# --------------------

@app.route('/api/alerts', methods=['POST'])
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
    
    current_date = datetime.now()
    formatted_current_date = current_date.strftime("%d%m%Y%H%M%S")
    alert_id = symbol+"_tickerAlert"+"_"+formatted_current_date
    
    # Now we use child(<id>) + set() to specify the id. Because push() method generates the id by itself.
    new_alert_ref = alerts_ref.child(alert_id).set({
        'symbol': symbol.upper(),
        'operator': operator,
        'value': float(value),
        'type': type,
        'created_at': created_at,
        'status': status,
    })
    
    #Schedule a subscription task.
    asyncio.run_coroutine_threadsafe(subscribe_symbol(symbol, alert_id), async_loop)
    
    return jsonify({'status': 'created', 'id': alert_id}), 201

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
def delete_alert(id):
    
    ref = alerts_ref.child(id)
    if ref.get() is not None:
        ref.delete()
        return jsonify({'status': 'deleted'}), 200
    else:
        return jsonify({'error': 'Alert not found'}), 404   

def start_async_loop():
    global async_loop
    async_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(async_loop)
    async_loop.run_until_complete(websocket_handler())
    
threading.Thread(target=start_async_loop, daemon=True).start()


if __name__ == '__main__':
    app.run(debug=True)