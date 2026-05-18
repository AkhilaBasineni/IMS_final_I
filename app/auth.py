import bcrypt
from functools import wraps
from flask import request, jsonify
from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from app.models import User, AuditLog, db

def hash_password(password):
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password, password_hash):
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))

def log_audit(user, action, entity_type, entity_id, details=None):
    try:
        log = AuditLog(
            user_id=user.id if hasattr(user, 'id') else None,
            username=user.username if hasattr(user, 'username') else 'SYSTEM',
            role=user.role.role_name if hasattr(user, 'role') else 'SYSTEM',
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=details or {},
            ip_address=request.remote_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        print(f"Audit Log Error: {e}")

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            verify_jwt_in_request()
            user_id = get_jwt_identity()
            current_user = User.query.get(user_id)
            if not current_user or not current_user.is_active:
                return jsonify({'success': False, 'message': 'Invalid user'}), 401
            return f(current_user, *args, **kwargs)
        except Exception:
            return jsonify({'success': False, 'message': 'Auth Error'}), 401
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(current_user, *args, **kwargs):
            if current_user.role.role_name not in roles:
                return jsonify({'success': False, 'message': 'Forbidden'}), 403
            return f(current_user, *args, **kwargs)
        return wrapper
    return decorator