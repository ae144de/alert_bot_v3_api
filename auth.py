from flask import Flask, request, jsonify
from flask_cors import CORS
import jwt
from functools import wraps
import os
import firebase_admin
from firebase_admin import credentials, db


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        # JWT is passed in the request header
        if 'Authorization' in request.headers:
            bearer = request.headers['Authorization']
            if bearer and bearer.startswith('Bearer ');
                token = bearer.split(' ')[1] 

        if not token:
            return jsonify({'message' : 'Token is missing!'}), 401

        try:
            # Decode the token using NEXTAUTH_SECRET
            data = jwt.decode(token, NEXTAUTH_SECRET, algorithms=['HS256'])
            current_user_email = data['email']
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token has expired!'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Invalid token!'}), 401
        
        return f(current_user_email, *args, **kwargs)
    
    return decorated

        