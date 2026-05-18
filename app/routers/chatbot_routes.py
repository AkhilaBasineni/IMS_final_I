import os
import re
import json
import httpx
from flask import Blueprint, request, jsonify, session
from app.models import Product, Site, Category, SubCategory, State, Manager, db
from sqlalchemy import distinct
from groq import Groq

bp = Blueprint('chatbot', __name__, url_prefix='/api/chatbot')

try:
    custom_http_client = httpx.Client()
    groq_client = Groq(
        api_key=os.environ.get("GROQ_API_KEY"),
        http_client=custom_http_client
    )
except Exception as e:
    groq_client = None
    print(f"Warning: Groq client failed to initialize. {e}")

# ═══════════════════════════════════════════════════════════════════════════
# INTENT DETECTION — pure Python, zero tokens
# ═══════════════════════════════════════════════════════════════════════════

INTENT_PATTERNS = {
    "buy":         r"\bbuy\b|purchase|how.*buy|want.*buy|place.*order|how can i (buy|get|order)",
    "sell":        r"\bsell\b|supply|supplier|vendor|provide.*product|become.*partner|how.*sell",
    "products":    r"\bproducts\b|show.*product|list.*product|available.*product|what.*product|price|show.*item|available.*item",
    "warehouses":  r"\bwarehouse|location|where.*store|which.*state|storage",
    "managers":    r"\bmanager|who.*manage|state.*head|contact.*state|regional",
    "categories":  r"\bcategor|subcategor|product type|classification|kind.*product",
    "transport":   r"\btransport|shipping|delivery|logistics|freight|how.*ship",
    "contact":     r"\bcontact|admin|support|help|reach|email|phone|address",
    "show_more":   r"\bmore|next|show more|see more|continue|yes\b|yeah|yep",
}

def detect_intent(message):
    msg = message.lower().strip()
    for intent, pattern in INTENT_PATTERNS.items():
        if re.search(pattern, msg):
            return intent
    return "unknown"

# ═══════════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

PER_CAT    = 5   # products per category per page
WH_PER_STATE = 5  # warehouses shown per state per page

def fetch_products(offset=0):
    cats_q = db.session.query(distinct(Product.category))\
        .filter(Product.status == 'Active', Product.category.isnot(None))\
        .order_by(Product.category).all()
    categories = [c[0] for c in cats_q if c[0]]

    result = []
    has_more = False
    for cat in categories:
        rows = Product.query.filter_by(status='Active')\
            .filter(Product.category == cat)\
            .order_by(Product.product_name)\
            .offset(offset).limit(PER_CAT + 1).all()
        if len(rows) > PER_CAT:
            has_more = True
            rows = rows[:PER_CAT]
        if rows:
            result.append((cat, rows))
    return result, has_more

def fetch_warehouses(wh_offset=0):
    """
    Group all real sites by state. Show WH_PER_STATE warehouses per state,
    using wh_offset to paginate within each state simultaneously.
    Returns list of (state_name, page_sites, state_total) and global has_more.
    """
    from collections import OrderedDict

    # state_id -> state_name lookup
    state_lookup = {s.state_id: s.state_name for s in State.query.all()}

    # All real sites — no status filter, skip auto-created placeholders
    all_sites = Site.query.order_by(Site.state_id, Site.city, Site.site_name).all()

    # Group into { state_name: [site, ...] }
    grouped = OrderedDict()
    for site in all_sites:
        if site.site_name and not site.site_name.startswith("Auto-created"):
            state_name = state_lookup.get(site.state_id, "Other")
            grouped.setdefault(state_name, []).append(site)

    result = []
    has_more = False
    for state_name in sorted(grouped.keys()):
        all_in_state = grouped[state_name]
        page = all_in_state[wh_offset: wh_offset + WH_PER_STATE]
        if len(all_in_state) > wh_offset + WH_PER_STATE:
            has_more = True
        if page:
            result.append((state_name, page, len(all_in_state)))

    return result, has_more

def fetch_managers():
    all_states = State.query.order_by(State.state_name).all()
    managed = db.session.query(State.state_id, State.state_name, Manager.manager_name)\
        .join(Site, Site.state_id == State.state_id)\
        .join(Manager, Site.manager_id == Manager.manager_id)\
        .filter(Site.status == 'Active').distinct(State.state_id).all()
    managed_ids = {r[0]: r[2] for r in managed}
    return [(s.state_name, managed_ids.get(s.state_id)) for s in all_states]

def fetch_categories():
    cats = Category.query.filter_by(status='Active').order_by(Category.category_name).all()
    result = []
    for c in cats:
        subs = SubCategory.query.filter_by(category_id=c.id, status='Active')\
                   .order_by(SubCategory.subcategory_name).all()
        result.append((c.category_name, [s.subcategory_name for s in subs]))
    return result

# ═══════════════════════════════════════════════════════════════════════════
# RESPONSE BUILDERS — pure Python Markdown, zero tokens
# ═══════════════════════════════════════════════════════════════════════════

def build_products_reply(offset=0):
    rows, has_more = fetch_products(offset)
    if not rows:
        return "✅ **That's all our products!** No more items to show.", False, offset

    lines = ["Here are our available products:\n"]
    for cat, products in rows:
        lines.append(f"\n**{cat}**")
        for p in products:
            lines.append(f"• {p.product_name} — ₹{p.unit_price:,.2f}")

    if has_more:
        lines.append("\n---\n*Would you like to see more products?* Just say **'show more'**.")
    else:
        lines.append("\n---\n✅ *That's all our products!*")

    return "\n".join(lines), has_more, offset + PER_CAT

def build_warehouses_reply(wh_offset=0):
    rows, has_more = fetch_warehouses(wh_offset)
    if not rows:
        return "✅ **Those are all our warehouse locations!**", False, wh_offset

    total_all = sum(total for _, _, total in rows)
    batch_end = wh_offset + WH_PER_STATE
    lines = [f"**Our Warehouses** — showing {wh_offset + 1}–{min(batch_end, total_all)} of {total_all} locations:\n"]

    for state_name, sites, state_total in rows:
        lines.append(f"\n**{state_name}** ({state_total} total)")
        for s in sites:
            fmt  = f" · {s.site_format}" if s.site_format else ""
            city = f", {s.city}" if s.city else ""
            lines.append(f"• {s.site_name}{city}{fmt}")

    if has_more:
        lines.append("\n---\nWould you like to see more warehouses? Just say **\'show more\'**.")
    else:
        lines.append(f"\n---\n✅ All {total_all} warehouse locations shown.")

    return "\n".join(lines), has_more, wh_offset + WH_PER_STATE

def build_managers_reply():
    rows = fetch_managers()
    if not rows:
        return "No manager data available. Please [📋 contact admin](/contact)."
    lines = ["**State Managers:**\n"]
    for state_name, mgr in rows:
        mgr_str = mgr if mgr else "[Contact Admin](/contact)"
        lines.append(f"• **{state_name}** — {mgr_str}")
    return "\n".join(lines)

def build_categories_reply():
    rows = fetch_categories()
    if not rows:
        return "No categories found. Please [📋 contact admin](/contact)."
    lines = ["**Our Product Categories:**\n"]
    for cat_name, subs in rows:
        if subs:
            lines.append(f"• **{cat_name}** → {', '.join(subs)}")
        else:
            lines.append(f"• **{cat_name}**")
    return "\n".join(lines)

BUY_REPLY = (
    "To **buy products** from InventoHub, please contact our admin via the Contact Form.\n\n"
    "- 🛒 Our team will guide you through the available products and pricing.\n"
    "- 📦 We offer bulk ordering with flexible delivery options across all states.\n"
    "- ⚡ Once registered, orders are processed and dispatched promptly.\n\n"
    "[📋 Open Contact Form](/contact)"
)

SELL_REPLY = (
    "To **sell products** to InventoHub, please contact our admin via the Contact Form.\n\n"
    "- 🤝 Our procurement team will review your product catalogue and pricing.\n"
    "- 🏭 We source from suppliers across multiple categories and regions.\n"
    "- 📋 Once approved, you'll be onboarded as an official InventoHub supplier.\n\n"
    "[📋 Open Contact Form](/contact)"
)

TRANSPORT_REPLY = (
    "We support **4 transport types**:\n\n"
    "- 🚢 **Ship** — Sea freight\n"
    "- 🚂 **Rail** — Rail freight\n"
    "- 🚛 **Truck** — Road freight\n"
    "- ✈️ **Air** — Air freight\n\n"
    "Contact us to arrange the best option for your order."
)

CONTACT_REPLY = (
    "**InventoHub Support**\n\n"
    "- 📧 support@inventohub.com\n"
    "- 📞 +91 (800) 123-4567\n"
    "- 📍 Prasanthi Nilayam, Puttaparthi, Andhra Pradesh - 515134\n\n"
    "[📋 Open Contact Form](/contact)"
)

# ═══════════════════════════════════════════════════════════════════════════
# MAIN HANDLER — routes by intent, calls Groq only for truly unknown queries
# ═══════════════════════════════════════════════════════════════════════════

def handle_intent(intent, message, chat_state):
    """
    Returns (reply_text, updated_chat_state).
    chat_state holds pagination offsets between turns.
    Groq is ONLY called for intent == 'unknown'.
    """
    if intent == "products":
        reply, has_more, next_off = build_products_reply(0)
        chat_state["product_offset"] = next_off if has_more else 0
        chat_state["last_intent"] = "products"
        return reply, chat_state

    if intent == "warehouses":
        reply, has_more, next_off = build_warehouses_reply(0)
        chat_state["wh_offset"] = next_off if has_more else 0
        chat_state["last_intent"] = "warehouses"
        return reply, chat_state

    if intent == "show_more":
        last = chat_state.get("last_intent")
        if last == "products":
            off = chat_state.get("product_offset", 0)
            reply, has_more, next_off = build_products_reply(off)
            chat_state["product_offset"] = next_off if has_more else 0
            return reply, chat_state
        if last == "warehouses":
            off = chat_state.get("wh_offset", 0)
            reply, has_more, next_off = build_warehouses_reply(off)
            chat_state["wh_offset"] = next_off if has_more else 0
            return reply, chat_state
        # Generic show-more fallback
        return "What would you like to see more of? You can ask about **products**, **warehouses**, or anything else.", chat_state

    if intent == "buy":
        chat_state["last_intent"] = "buy"
        return BUY_REPLY, chat_state

    if intent == "sell":
        chat_state["last_intent"] = "sell"
        return SELL_REPLY, chat_state

    if intent == "managers":
        chat_state["last_intent"] = "managers"
        return build_managers_reply(), chat_state

    if intent == "categories":
        chat_state["last_intent"] = "categories"
        return build_categories_reply(), chat_state

    if intent == "transport":
        chat_state["last_intent"] = "transport"
        return TRANSPORT_REPLY, chat_state

    if intent == "contact":
        chat_state["last_intent"] = "contact"
        return CONTACT_REPLY, chat_state

    # ── Unknown intent: use Groq as a last resort ──
    return None, chat_state   # signals caller to use Groq


# Tiny system prompt used only for truly unknown questions
FALLBACK_SYSTEM = (
    "You are InventoBot, an assistant for InventoHub (a B2B Inventory Management System).\n\n"
    "You are ONLY allowed to answer questions related to:\n"
    "- Products, pricing, and categories\n"
    "- Warehouses and storage locations\n"
    "- Buying and selling process\n"
    "- Transport and logistics\n"
    "- Managers and contact information\n"
    "- Inventory management concepts\n\n"
    "STRICT RULES:\n"
    "1. If the user asks anything outside these topics, DO NOT answer it.\n"
    "2. Instead reply exactly with:\n"
    "   'I can only help with inventory management and InventoHub-related queries.'\n"
    "3. Do NOT provide explanations, suggestions, or extra information for unrelated questions.\n"
    "4. Keep answers short (maximum 2–3 sentences).\n"
    "5. Do NOT generate code, general knowledge, or personal advice.\n"
    "6. Only allowed link: [Contact Form](/contact).\n\n"
    "Always stay within the allowed domain."
)

QUICK_REPLIES = [
    {"label": "📦 Products & Prices",   "message": "What are all the available products with their prices?"},
    {"label": "🏭 Warehouses by State", "message": "Show all available warehouses in each state"},
    {"label": "🛒 How to Buy",          "message": "How can I buy a product from you?"},
    {"label": "💼 How to Sell",         "message": "How can I sell products to you?"},
    {"label": "👤 State Managers",      "message": "What is the manager name for each state?"},
    {"label": "🗂️ Categories",          "message": "Show all categories and subcategories"},
    {"label": "🚚 Transport Types",     "message": "What transport types do you offer?"},
    {"label": "📞 Contact Admin",       "message": "I need more details, how do I contact admin?"},
]

# ═══════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@bp.route('/ask', methods=['POST'])
def ask_chatbot():
    data = request.json
    user_message = data.get('message', '').strip()
    if not user_message:
        return jsonify({'success': False, 'reply': "Please ask a question!"})

    # Per-session pagination state (not in chat history — keeps history small)
    if 'chat_state' not in session:
        session['chat_state'] = {}
    chat_state = session['chat_state']

    intent = detect_intent(user_message)
    reply, chat_state = handle_intent(intent, user_message, chat_state)

    # Save updated pagination state
    session['chat_state'] = chat_state
    session.modified = True

    # If intent was handled natively — return immediately, zero Groq tokens used
    if reply is not None:
        return jsonify({'success': True, 'reply': reply})

    # ── Fallback to Groq only for unknown queries ──
    if not groq_client:
        return jsonify({'success': True,
                        'reply': "I can help with products, warehouses, buying, selling, managers, categories, transport types, or contact info. What would you like to know?"})

    if 'chat_history' not in session:
        session['chat_history'] = []

    # Only last 2 messages for context — absolute minimum
    history = session['chat_history'][-2:]
    messages = [{"role": "system", "content": FALLBACK_SYSTEM}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=200,   # short answers only
            temperature=0.3
        )
        final_reply = response.choices[0].message.content

        session['chat_history'].append({"role": "user", "content": user_message})
        session['chat_history'].append({"role": "assistant", "content": final_reply})
        if len(session['chat_history']) > 2:
            session['chat_history'] = session['chat_history'][-2:]
        session.modified = True

        return jsonify({'success': True, 'reply': final_reply})

    except Exception as e:
        err_str = str(e)
        print(f"Groq API Error: {err_str}")
        retry = re.search(r'try again in (\d+m\d+\.?\d*s|\d+\.?\d*s)', err_str)
        wait = retry.group(1) if retry else "a few minutes"
        return jsonify({'success': False,
                        'reply': f"⚠️ I'm temporarily overloaded. Please try again in **{wait}**.\n\nIn the meantime you can [📋 contact us directly](/contact)."})


@bp.route('/quick-replies', methods=['GET'])
def get_quick_replies():
    return jsonify({'success': True, 'quick_replies': QUICK_REPLIES})
