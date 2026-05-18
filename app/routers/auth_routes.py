from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from app.models import User, Site, db
from app.auth import verify_password, token_required, log_audit, hash_password
import sendgrid
from sendgrid.helpers.mail import Mail as SGMail, Email
import secrets
import os

bp = Blueprint('auth', __name__, url_prefix='/api/auth')

ADMIN_EMAIL = 'inventoryadmin284@gmail.com'


def sg_send(to_email, subject, html_content):
    sg = sendgrid.SendGridAPIClient(api_key=os.getenv('SENDGRID_API_KEY'))
    msg = SGMail(
        from_email=Email(ADMIN_EMAIL, 'IFMS PRO'),
        to_emails=to_email,
        subject=subject,
        html_content=html_content
    )
    sg.send(msg)


def get_manager_site_ids(user):
    if not user.state_id or user.state_id == 'ALL':
        return None
    try:
        sites = Site.query.filter_by(state_id=int(user.state_id)).all()
        return [s.site_id for s in sites]
    except (ValueError, TypeError):
        return []


@bp.route('/login', methods=['POST'])
def login():
    data = request.json
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'success': False, 'message': 'Missing credentials'}), 400

    user = User.query.filter_by(username=data['username']).first()
    if not user:
        return jsonify({'success': False, 'message': 'Invalid username'}), 401
    if not verify_password(data['password'], user.password_hash):
        return jsonify({'success': False, 'message': 'Invalid password'}), 401
    if not user.is_active:
        return jsonify({'success': False, 'message': 'Account is deactivated'}), 403

    access_token = create_access_token(identity=str(user.id))
    log_audit(user, 'LOGIN', 'System', user.id)

    role = user.role.role_name
    redirect = '/admin-dashboard' if role == 'Admin' else '/manager-dashboard' if role == 'Manager' else '/analyst-dashboard'

    return jsonify({'success': True, 'data': {
        'access_token': access_token,
        'user': {'username': user.username, 'role': role, 'redirect': redirect}
    }})


@bp.route('/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    email = data.get('email', '').strip()
    if not email:
        return jsonify({'success': False, 'message': 'Email is required'}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({'success': True, 'message': 'If that email exists, a password has been sent.'})

    new_password = secrets.token_urlsafe(10)
    user.password_hash = hash_password(new_password)
    db.session.commit()

    try:
        sg_send(
            to_email=email,
            subject='IFMS PRO - Your Temporary Password',
            html_content=f"""
            <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:30px;border:1px solid #e5e7eb;border-radius:12px;">
                <h2 style="color:#3b82f6;">IFMS PRO</h2>
                <p>Hello <strong>{user.username}</strong>,</p>
                <p>A password reset was requested for your account. Your temporary password is:</p>
                <div style="background:#f1f5f9;border-radius:8px;padding:16px;text-align:center;font-size:22px;letter-spacing:2px;font-weight:bold;color:#1e293b;">
                    {new_password}
                </div>
                <p style="margin-top:20px;color:#64748b;font-size:13px;">Please log in and change your password immediately.</p>
            </div>
            """
        )
    except Exception as e:
        import traceback
        print("FORGOT PASSWORD EMAIL ERROR:", traceback.format_exc())
        return jsonify({'success': False, 'message': f'Email send failed: {str(e)}'}), 500

    return jsonify({'success': True, 'message': 'Temporary password sent to your email.'})


@bp.route('/change-password', methods=['POST'])
@token_required
def change_password(current_user):
    data = request.json
    old_pw = data.get('old_password', '')
    new_pw = data.get('new_password', '')
    if not old_pw or not new_pw:
        return jsonify({'success': False, 'message': 'Both old and new passwords are required'}), 400
    if not verify_password(old_pw, current_user.password_hash):
        return jsonify({'success': False, 'message': 'Current password is incorrect'}), 400
    if len(new_pw) < 6:
        return jsonify({'success': False, 'message': 'New password must be at least 6 characters'}), 400

    current_user.password_hash = hash_password(new_pw)
    db.session.commit()
    log_audit(current_user, 'CHANGE_PASSWORD', 'User', current_user.id)
    return jsonify({'success': True, 'message': 'Password updated successfully'})


@bp.route('/me', methods=['GET'])
@token_required
def get_me(current_user):
    role = current_user.role.role_name
    site_ids = get_manager_site_ids(current_user) if role in ('Manager', 'Analyst') else None
    return jsonify({'success': True, 'data': {
        'id':       current_user.id,
        'username': current_user.username,
        'role':     role,
        'state_id': current_user.state_id,
        'site_ids': site_ids
    }})
