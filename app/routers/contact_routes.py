from flask import Blueprint, request, jsonify
from app.models import ContactMessage, db
from app.auth import token_required, log_audit
from datetime import datetime, timezone
import sendgrid
from sendgrid.helpers.mail import Mail as SGMail, Email
import os

bp = Blueprint('contact', __name__, url_prefix='/api/contact')

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


@bp.route('/submit', methods=['POST'])
def submit_contact():
    data = request.get_json(silent=True) or {}
    first_name = data.get('first_name', '').strip()
    last_name  = data.get('last_name', '').strip()
    email      = data.get('email', '').strip()
    message    = data.get('message', '').strip()

    if not all([first_name, last_name, email, message]):
        return jsonify({'success': False, 'message': 'All fields are required.'}), 400

    contact = ContactMessage(
        first_name=first_name, last_name=last_name,
        email=email, message=message, status='unread'
    )
    db.session.add(contact)
    db.session.commit()

    try:
        sg_send(
            to_email=ADMIN_EMAIL,
            subject=f'[InventoHub] New Contact Message from {first_name} {last_name}',
            html_content=f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #0f172a, #4f46e5); padding: 30px; border-radius: 12px 12px 0 0; text-align: center;">
                    <h2 style="color: white; margin: 0;">New Contact Message</h2>
                </div>
                <div style="background: #f8fafc; padding: 30px; border: 1px solid #e2e8f0;">
                    <p><strong>Name:</strong> {first_name} {last_name}</p>
                    <p><strong>Email:</strong> {email}</p>
                    <p><strong>Message:</strong></p>
                    <blockquote style="border-left: 4px solid #4f46e5; padding-left: 15px; color: #475569;">{message}</blockquote>
                    <p style="font-size: 12px; color: #94a3b8;">Message ID: #{contact.id} | Reply via Admin panel at /contact-messages</p>
                </div>
            </div>
            """
        )
    except Exception as e:
        print(f"[WARN] Admin notification email failed: {e}")

    return jsonify({'success': True, 'message': 'Message sent! We will get back to you within 24 hours.'})


@bp.route('/messages', methods=['GET'])
@token_required
def list_messages(current_user):
    if current_user.role.role_name not in ('Admin', 'SuperAdmin'):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    status_filter = request.args.get('status', '').strip()
    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 15, type=int)

    query = ContactMessage.query.order_by(ContactMessage.created_at.desc())
    if status_filter:
        query = query.filter(ContactMessage.status == status_filter)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({'success': True, 'data': {
        'items': [{
            'id':          m.id,
            'first_name':  m.first_name,
            'last_name':   m.last_name,
            'email':       m.email,
            'message':     m.message,
            'status':      m.status,
            'admin_reply': m.admin_reply,
            'replied_at':  m.replied_at.isoformat() if m.replied_at else None,
            'created_at':  m.created_at.isoformat()
        } for m in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page
    }})


@bp.route('/messages/<int:msg_id>/mark-read', methods=['PUT'])
@token_required
def mark_read(current_user, msg_id):
    if current_user.role.role_name not in ('Admin', 'SuperAdmin'):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    contact = ContactMessage.query.get_or_404(msg_id)
    if contact.status == 'unread':
        contact.status = 'read'
        db.session.commit()
        log_audit(current_user, 'MARK_READ', 'ContactMessage', msg_id)
    return jsonify({'success': True})


@bp.route('/messages/<int:msg_id>/reply', methods=['POST'])
@token_required
def reply_message(current_user, msg_id):
    if current_user.role.role_name not in ('Admin', 'SuperAdmin'):
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data  = request.get_json(silent=True) or {}
    reply = data.get('reply', '').strip()
    if not reply:
        return jsonify({'success': False, 'message': 'Reply text is required.'}), 400

    contact = ContactMessage.query.get_or_404(msg_id)

    try:
        sg_send(
            to_email=contact.email,
            subject='Re: Your message to InventoHub',
            html_content=f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <div style="background: linear-gradient(135deg, #0f172a, #4f46e5); padding: 30px; border-radius: 12px 12px 0 0; text-align: center;">
                    <h2 style="color: white; margin: 0;">InventoHub Support</h2>
                </div>
                <div style="background: #f8fafc; padding: 30px; border: 1px solid #e2e8f0;">
                    <p>Hi <strong>{contact.first_name}</strong>,</p>
                    <p>Thank you for reaching out. Here is our response:</p>
                    <div style="background: white; border-left: 4px solid #4f46e5; padding: 15px 20px; border-radius: 0 8px 8px 0; margin: 20px 0;">
                        {reply}
                    </div>
                    <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                    <p style="color: #64748b;">Your original message:</p>
                    <blockquote style="color: #94a3b8; font-style: italic;">{contact.message}</blockquote>
                    <p style="margin-top: 30px;">Best regards,<br><strong>InventoHub Support Team</strong></p>
                </div>
            </div>
            """
        )
        contact.status      = 'replied'
        contact.admin_reply = reply
        contact.replied_at  = datetime.now(timezone.utc)
        db.session.commit()
        log_audit(current_user, 'REPLY', 'ContactMessage', msg_id, {'recipient': contact.email})
        return jsonify({'success': True, 'message': f'Reply sent to {contact.email}'})

    except Exception as e:
        import traceback
        print("REPLY EMAIL ERROR:", traceback.format_exc())
        return jsonify({'success': False, 'message': f'Email failed: {str(e)}'}), 500
