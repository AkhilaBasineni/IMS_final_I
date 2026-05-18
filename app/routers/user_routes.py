from flask import Blueprint, request, jsonify,current_app
from app.models import User, Role, Site, State, Manager, db
from app.auth import token_required, role_required, hash_password, log_audit
from app.dependencies import handle_exceptions
import sendgrid
from sendgrid.helpers.mail import Mail as SGMail, Email
from sqlalchemy import or_
import secrets

bp = Blueprint('users', __name__, url_prefix='/api/users')


def resolve_warehouses(user):
    """
    Admin     → all sites across all states
    Manager/Analyst → only sites in their state_id
    """
    role_name = user.role.role_name if user.role else ''

    if role_name == 'Admin' or user.state_id == 'ALL':
        sites = Site.query.all()
        return [{'site_id': s.site_id, 'site_name': s.site_name, 'state_id': s.state_id} for s in sites]

    elif role_name in ('Manager', 'Analyst') and user.state_id:
        try:
            sites = Site.query.filter_by(state_id=int(user.state_id)).all()
            return [{'site_id': s.site_id, 'site_name': s.site_name, 'state_id': s.state_id} for s in sites]
        except (ValueError, TypeError):
            return []

    return []


def _resolve_photo(u):
    """Resolve user photo — DB field first, then filesystem scan using absolute path."""
    import os, hashlib
    from flask import current_app

    # 1. Use photo_url stored in DB (most reliable — set on new uploads)
    if u.photo_url and u.photo_url.strip():
        return u.photo_url

    # 2. Scan avatars directory with absolute path (avoids CWD ambiguity)
    avatar_dir = os.path.join(current_app.static_folder, 'uploads', 'avatars')
    if not os.path.isdir(avatar_dir):
        return None

    # New naming: user_<id>.<ext>
    for ext in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
        if os.path.exists(os.path.join(avatar_dir, f'user_{u.id}.{ext}')):
            return f'/static/uploads/avatars/user_{u.id}.{ext}'

    # Legacy naming: user_<md5_of_id>.<ext>
    h_id = hashlib.md5(str(u.id).encode()).hexdigest()
    for ext in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
        if os.path.exists(os.path.join(avatar_dir, f'user_{h_id}.{ext}')):
            return f'/static/uploads/avatars/user_{h_id}.{ext}'

    # Wildcard scan: any file starting with user_<id>_ or user_<id>.
    try:
        for fname in os.listdir(avatar_dir):
            if fname.startswith(f'user_{u.id}.') or fname.startswith(f'user_{u.id}_'):
                return f'/static/uploads/avatars/{fname}'
    except Exception:
        pass

    return None


def format_user(u):
    """Reusable user dict with role, manager, state, and warehouse info."""
    # Get linked manager record if exists
    manager = Manager.query.filter_by(user_id=u.id).first()

    # Get state name if state_id is not 'ALL'
    state_name = None
    if u.state_id and u.state_id != 'ALL':
        try:
            state = State.query.get(int(u.state_id))
            state_name = state.state_name if state else None
        except (ValueError, TypeError):
            state_name = None

    return {
        'id': u.id,
        'username': u.username,
        'email': u.email,
        'role': u.role.role_name if u.role else 'N/A',
        'role_id': u.role_id,
        'state_id': u.state_id,
        'state_name': 'ALL States' if u.state_id == 'ALL' else state_name,
        'manager_id': manager.manager_id if manager else None,
        'manager_name': manager.manager_name if manager else None,
        'warehouses': resolve_warehouses(u),
        'is_active': u.is_active,
        'photo_url': _resolve_photo(u),
        'created_at': u.created_at.isoformat() if u.created_at else None
    }


@bp.route('', methods=['GET'])
@token_required
@role_required('Admin')
@handle_exceptions
def get_users(current_user):
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    search = request.args.get('search', '')
    role_filter = request.args.get('role', '')
    status_filter = request.args.get('status', '')

    query = User.query
    if search:
        query = query.filter(or_(
            User.username.ilike(f'%{search}%'),
            User.email.ilike(f'%{search}%')
        ))
    if role_filter:
        query = query.join(Role).filter(Role.role_name == role_filter)
    if status_filter:
        is_active = status_filter.lower() == 'active'
        query = query.filter(User.is_active == is_active)

    pagination = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({'success': True, 'data': {
        'items': [format_user(u) for u in pagination.items],
        'total': pagination.total,
        'page': page,
        'pages': pagination.pages
    }})


@bp.route('/<int:user_id>', methods=['GET'])
@token_required
@role_required('Admin')
@handle_exceptions
def get_user(current_user, user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'message': 'Not found'}), 404
    return jsonify({'success': True, 'data': format_user(user)})


@bp.route('', methods=['POST'])
@token_required
@role_required('Admin')
@handle_exceptions
def create_user(current_user):
    import os, base64
    from werkzeug.utils import secure_filename

    # Support both JSON and multipart/form-data
    if request.content_type and 'multipart' in request.content_type:
        data = request.form
    else:
        data = request.json or {}

    if User.query.filter_by(username=data['username']).first():
        return jsonify({'success': False, 'message': 'Username exists'}), 400
    if User.query.filter_by(email=data['email']).first():
        return jsonify({'success': False, 'message': 'Email exists'}), 400

    role = Role.query.filter_by(role_name=data['role']).first()
    if not role:
        return jsonify({'success': False, 'message': 'Invalid role'}), 400

    role_name = role.role_name

    # Set state_id based on role
    if role_name == 'Admin':
        state_id = 'ALL'
    elif role_name == 'Analyst':
        # Analyst can be assigned to a specific state OR ALL states
        provided = data.get('state_id')
        state_id = str(provided) if provided and str(provided) != 'ALL' else 'ALL'
    else:
        state_id = str(data.get('state_id')) if data.get('state_id') else None
        if not state_id:
            return jsonify({'success': False, 'message': 'state_id is required for Manager'}), 400

    # Auto-generate a secure temporary password
    generated_password = secrets.token_urlsafe(6)

    # If Manager role → enforce one manager per state BEFORE creating user
    if role_name == 'Manager':
        conflict = (User.query
                    .join(Role)
                    .filter(
                        Role.role_name == 'Manager',
                        User.state_id == state_id,
                        User.is_active == True
                    ).first())
        if conflict:
            state_obj = State.query.get(int(state_id)) if state_id.isdigit() else None
            sname = state_obj.state_name if state_obj else state_id
            return jsonify({
                'success': False,
                'message': f'Manager {conflict.username} is already assigned to {sname}. Only one Manager per state is allowed.'
            }), 400

    new_user = User(
        username=data['username'],
        email=data['email'],
        password_hash=hash_password(generated_password),
        role_id=role.id,
        state_id=state_id,
        is_active=True
    )
    db.session.add(new_user)
    db.session.flush()  # get new_user.id before commit

    # Handle photo upload (multipart form)
    photo_file = request.files.get('photo')
    if photo_file and photo_file.filename:
        try:
            
            upload_dir = os.path.join(current_app.static_folder, 'uploads', 'avatars')
            os.makedirs(upload_dir, exist_ok=True)
            ext = secure_filename(photo_file.filename).rsplit('.', 1)[-1].lower()
            fname = f'user_{new_user.id}.{ext}'
            fpath = os.path.join(upload_dir, fname)
            photo_file.save(fpath)
            new_user.photo_url = f'/static/uploads/avatars/{fname}'
        except Exception as pe:
            pass  # photo save failure doesn't block user creation

    # Create Manager record (conflict already checked above)
    if role_name == 'Manager':

        new_manager = Manager(
            manager_name=data['username'],
            user_id=new_user.id
        )
        db.session.add(new_manager)
        db.session.flush()

        # Assign manager_id to all sites in their state
        try:
            sites = Site.query.filter_by(state_id=int(state_id)).all()
            for site in sites:
                site.manager_id = new_manager.manager_id
        except (ValueError, TypeError):
            pass  

    db.session.commit()

    email_status = "Email not attempted."
    try:
        sg = sendgrid.SendGridAPIClient(api_key=os.getenv('SENDGRID_API_KEY'))
        app_url = current_app.config.get('APP_URL', 'https://ims-final-i.onrender.com')
        email_msg = SGMail(
            from_email=Email('inventoryadmin284@gmail.com', 'IFMS PRO'),
            to_emails=new_user.email,
            subject='IFMS - Account Created',
            html_content=f"""
            <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:30px;border:1px solid #e5e7eb;border-radius:12px;">
                <h2 style="color:#3b82f6;">IFMS PRO</h2>
                <p>Hello <strong>{new_user.username}</strong>,</p>
                <p>Your account has been created. Below is your temporary password:</p>
                <div style="background:#f1f5f9;border-radius:8px;padding:16px;text-align:center;font-size:22px;letter-spacing:2px;font-weight:bold;color:#1e293b;">
                    {generated_password}
                </div>
                <p style="margin-top:16px;">Login URL: <a href="{app_url}">{app_url}</a></p>
                <p>Username: <strong>{new_user.username}</strong></p>
                <p style="color:#64748b;font-size:13px;">Please log in and change your password immediately.</p>
            </div>
            """
        )
        sg.send(email_msg)
        email_status = "Email sent."
    except Exception as e:
        import traceback
        print("EMAIL ERROR:", traceback.format_exc())
        email_status = f"Email failed: {str(e)}"

    log_audit(current_user, 'CREATE', 'User', new_user.id)
    return jsonify({'success': True, 'message': f'User created! {email_status}'}), 201


@bp.route('/<int:user_id>', methods=['PUT'])
@token_required
@role_required('Admin')
@handle_exceptions
def update_user(current_user, user_id):
    import os
    from werkzeug.utils import secure_filename
    from flask import current_app

    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    # Support both multipart/form-data (with photo) and JSON
    if request.content_type and 'multipart' in request.content_type:
        data = request.form
    else:
        data = request.json or {}

    # Handle photo upload if present
    photo_file = request.files.get('photo') if request.files else None
    if photo_file and photo_file.filename:
        try:
            upload_dir = os.path.join(current_app.static_folder, 'uploads', 'avatars')
            os.makedirs(upload_dir, exist_ok=True)
            ext = secure_filename(photo_file.filename).rsplit('.', 1)[-1].lower()
            fname = f'user_{user_id}.{ext}'
            photo_file.save(os.path.join(upload_dir, fname))
            user.photo_url = f'/static/uploads/avatars/{fname}'
        except Exception:
            pass

    # Update username if provided and not taken
    if 'username' in data and data['username']:
        new_uname = data['username'].strip()
        if new_uname != user.username:
            if User.query.filter(User.username == new_uname, User.id != user_id).first():
                return jsonify({'success': False, 'message': 'Username already taken'}), 400
            user.username = new_uname
            # Keep manager name in sync
            mgr = Manager.query.filter_by(user_id=user.id).first()
            if mgr:
                mgr.manager_name = new_uname

    # Update email if provided and not taken
    if 'email' in data and data['email']:
        new_email = data['email'].strip()
        if new_email != user.email:
            if User.query.filter(User.email == new_email, User.id != user_id).first():
                return jsonify({'success': False, 'message': 'Email already in use'}), 400
            user.email = new_email

    # Update role
    new_role_name = None
    if 'role' in data and data['role']:
        role = Role.query.filter_by(role_name=data['role']).first()
        if not role:
            return jsonify({'success': False, 'message': 'Invalid role'}), 400
        user.role_id = role.id
        new_role_name = role.role_name
        if new_role_name == 'Admin':
            user.state_id = 'ALL'
        elif new_role_name == 'Analyst':
            # Analyst keeps their current state_id or gets updated below
            pass

    # Determine effective role name after possible role change
    effective_role = new_role_name or (user.role.role_name if user.role else '')

    # Update state_id for Manager and Analyst roles
    new_state_id = data.get('state_id')
    if effective_role == 'Analyst' and new_state_id is not None:
        user.state_id = str(new_state_id) if str(new_state_id) != 'ALL' else 'ALL'
    if effective_role == 'Manager' and new_state_id:
        new_state_str = str(new_state_id)

        # ── Enforce: no two Managers for the same state ──────────────────────
        if effective_role == 'Manager':
            conflict = (User.query
                        .join(Role)
                        .filter(
                            Role.role_name == 'Manager',
                            User.state_id == new_state_str,
                            User.is_active == True,
                            User.id != user_id
                        ).first())
            if conflict:
                state = State.query.get(int(new_state_id))
                sname = state.state_name if state else new_state_str
                return jsonify({
                    'success': False,
                    'message': f'Manager {conflict.username} is already assigned to {sname}. Only one Manager per state is allowed.'
                }), 400

        user.state_id = new_state_str

        # Reassign sites in new state to this manager
        if effective_role == 'Manager':
            manager = Manager.query.filter_by(user_id=user.id).first()
            if manager:
                try:
                    sites = Site.query.filter_by(state_id=int(new_state_id)).all()
                    for site in sites:
                        site.manager_id = manager.manager_id
                except (ValueError, TypeError):
                    pass

    # Handle active/disabled status toggle (FormData sends strings, JSON sends bools)
    if 'is_active' in data:
        val = data['is_active']
        if isinstance(val, str):
            user.is_active = val.lower() in ('true', '1', 'yes', 'active')
        else:
            user.is_active = bool(val)

    # Clear stale photo_url if the file no longer exists on disk
    if user.photo_url:
        from flask import current_app
        disk_path = os.path.join(current_app.static_folder, user.photo_url.lstrip('/static/'))
        if not os.path.exists(disk_path):
            user.photo_url = None

    db.session.commit()
    log_audit(current_user, 'UPDATE', 'User', user_id)
    # Return updated user data so frontend can refresh without extra GET
    return jsonify({'success': True, 'message': 'User updated', 'data': format_user(user)})


@bp.route('/<int:user_id>', methods=['DELETE'])
@token_required
@role_required('Admin')
@handle_exceptions
def delete_user(current_user, user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False}), 404
    if user.id == current_user.id:
        return jsonify({'success': False, 'message': 'Cannot delete yourself'}), 400

    # Log before deleting (while user still exists)
    log_audit(current_user, 'DELETE', 'User', user_id)

    # Null-out ALL FK references using correct table names from models
    null_updates = [
        'UPDATE audit_logs                SET user_id    = NULL WHERE user_id    = :uid',
        'UPDATE purchase_orders            SET created_by = NULL WHERE created_by = :uid',
        'UPDATE purchase_orders            SET approved_by = NULL WHERE approved_by = :uid',
        'UPDATE sales_orders               SET created_by = NULL WHERE created_by = :uid',
        'UPDATE stock_transfers            SET requested_by = NULL WHERE requested_by = :uid',
        'UPDATE stock_transfers            SET approved_by  = NULL WHERE approved_by  = :uid',
        'UPDATE stock_adjustment_requests  SET requested_by = NULL WHERE requested_by = :uid',
        'UPDATE stock_adjustment_requests  SET approved_by  = NULL WHERE approved_by  = :uid',
        'UPDATE sites                       SET manager_id = NULL WHERE manager_id IN (SELECT manager_id FROM managers WHERE user_id = :uid)',
        'UPDATE managers                   SET user_id    = NULL WHERE user_id    = :uid',
    ]
    for sql in null_updates:
        db.session.execute(db.text(sql), {'uid': user_id})
    db.session.flush()

    db.session.delete(user)
    db.session.commit()
    db.session.expire_all()  # Clear session cache so next GET reflects deletion

    # Verify deletion
    still_exists = db.session.get(User, user_id)
    if still_exists:
        return jsonify({'success': False, 'message': 'Delete failed — user still exists'}), 500

    return jsonify({'success': True, 'message': 'User deleted'})


@bp.route('/<int:user_id>/reset-password', methods=['POST'])
@token_required
@role_required('Admin')
@handle_exceptions
def reset_password(current_user, user_id):
    user = User.query.get(user_id)
    if not user:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    data = request.json
    user.password_hash = hash_password(data['new_password'])
    db.session.commit()
    log_audit(current_user, 'RESET_PASSWORD', 'User', user_id)
    return jsonify({'success': True, 'message': 'Password reset successful'})

@bp.route('/public/team', methods=['GET'])
def get_public_team():
    """Public endpoint — returns Admin and Manager users for the About page team section."""
    try:
        from app.models import State
        admin_role   = Role.query.filter_by(role_name='Admin').first()
        manager_role = Role.query.filter_by(role_name='Manager').first()

        admins   = User.query.filter_by(role_id=admin_role.id,   is_active=True).all() if admin_role   else []
        managers = User.query.filter_by(role_id=manager_role.id, is_active=True).all() if manager_role else []

        def resolve_state(u):
            if not u.state_id or u.state_id == 'ALL':
                return 'All States'
            try:
                state = State.query.get(int(u.state_id))
                return state.state_name if state else 'N/A'
            except Exception:
                return u.state_id

        def get_photo(u):
            return _resolve_photo(u)

        def fmt(u):
            return {
                'id':         u.id,
                'username':   u.username,
                'email':      u.email,
                'role':       u.role.role_name if u.role else '',
                'state_id':   u.state_id or '',
                'state_name': resolve_state(u),
                'photo_url':  get_photo(u),
            }

        return jsonify({
            'success': True,
            'data': {
                'admins':   [fmt(u) for u in admins],
                'managers': [fmt(u) for u in managers],
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
