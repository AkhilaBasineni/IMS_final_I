import os
from dotenv import load_dotenv
from datetime import timedelta

load_dotenv()

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'a-very-long-and-secure-session-key-123')
    SQLALCHEMY_DATABASE_URI = os.getenv('DATABASE_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', 'another-long-secure-jwt-key-456')
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=8)

    # Email Settings (GMAIL)
    MAIL_SERVER = os.getenv('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.getenv('MAIL_PORT', 587))
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.getenv('MAIL_USERNAME', 'inventoryadmin284@gmail.com')
    MAIL_PASSWORD = os.getenv('MAIL_PASSWORD', 'bkroecidedhouwqc')
    MAIL_DEFAULT_SENDER = os.getenv('MAIL_USERNAME', 'inventoryadmin284@gmail.com')
    APP_URL = os.getenv('APP_URL', 'http://localhost:5000')