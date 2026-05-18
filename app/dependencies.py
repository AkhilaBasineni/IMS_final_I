from functools import wraps
from flask import jsonify,request

def handle_exceptions(f):
    """Global guard to prevent the app from crashing"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            print(f"LOG ERROR: {str(e)}") 
            return jsonify({
                'success': False,
                'message': 'An internal error occurred',
                'errors': [str(e)]
            }), 500
    return decorated_function

def validate_input(schema):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            data = request.get_json()

            errors = schema.validate(data)
            if errors:
                return jsonify(errors), 400

            return f(*args, **kwargs)
        return wrapper
    return decorator