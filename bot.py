import asyncio
import sqlite3
from datetime import datetime, timedelta
import logging
from typing import Dict, List, Optional
import json
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ChatMember
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import BadRequest, Forbidden
import schedule
import time
import threading
from flask import Flask, jsonify
import uvicorn
from fastapi import FastAPI

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(id.strip()) for id in ADMIN_IDS_STR.split(",") if id.strip().isdigit()]

# Port for Render (required for web service)
PORT = int(os.getenv("PORT", 8080))

# Conversation states for adding channels (simplified)
CHANNEL_NAME, CHANNEL_PRICE, CHANNEL_DEMO, CHANNEL_FORWARD = range(4)

# Create FastAPI app for health checks
app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Premium Telegram Bot is running!", "status": "healthy"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "bot": "running", "timestamp": datetime.now().isoformat()}

class PremiumBot:
    def __init__(self):
        self.setup_database()
        
    def setup_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TEXT,
                is_active INTEGER DEFAULT 1
            )
        ''')
        
        # Channels table (simplified without duration)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_key TEXT UNIQUE,
                channel_name TEXT,
                channel_id TEXT,
                price REAL,
                demo_link TEXT,
                invite_link TEXT,
                is_active INTEGER DEFAULT 1,
                created_date TEXT,
                created_by INTEGER
            )
        ''')
        
        # Subscriptions table (30 days default)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_key TEXT,
                start_date TEXT,
                end_date TEXT,
                is_active INTEGER DEFAULT 1,
                payment_confirmed INTEGER DEFAULT 0,
                invoice_sent INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (channel_key) REFERENCES channels (channel_key)
            )
        ''')
        
        # Pending payments table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                channel_key TEXT,
                amount REAL,
                payment_proof TEXT,
                timestamp TEXT,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        # Server premium plans table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS server_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_name TEXT,
                price REAL,
                included_channels TEXT,
                is_active INTEGER DEFAULT 1,
                created_date TEXT
            )
        ''')
        
        # Add default server premium plan
        cursor.execute('''
            INSERT OR IGNORE INTO server_plans (plan_name, price, included_channels, created_date)
            VALUES ('Server Premium', 599, '[]', ?)
        ''', (datetime.now().isoformat(),))
        
        conn.commit()
        conn.close()

    def get_main_keyboard(self):
        """Create main menu keyboard"""
        keyboard = [
            [KeyboardButton("💎 Premium Plans"), KeyboardButton("📊 My Subscriptions")],
            [KeyboardButton("💰 Payment Status"), KeyboardButton("📞 Contact Support")],
            [KeyboardButton("🎯 Demo Links"), KeyboardButton("ℹ️ Help")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    def get_admin_keyboard(self):
        """Create admin menu keyboard"""
        keyboard = [
            [KeyboardButton("➕ Add Channel"), KeyboardButton("📋 Manage Channels")],
            [KeyboardButton("💰 Pending Payments"), KeyboardButton("👥 User Stats")],
            [KeyboardButton("🔧 Server Plans"), KeyboardButton("📊 Analytics")]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    async def get_channels_from_db(self):
        """Get all active channels from database"""
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM channels WHERE is_active = 1')
        channels = cursor.fetchall()
        conn.close()
        
        channel_dict = {}
        for channel in channels:
            channel_dict[channel[1]] = {  # channel_key
                'name': channel[2],
                'channel_id': channel[3],
                'price': channel[4],
                'demo_link': channel[5],
                'invite_link': channel[6]
            }
        return channel_dict

    def get_plans_keyboard(self):
        """Create plans selection keyboard"""
        keyboard = []
        
        # Get dynamic channels
        try:
            channels = asyncio.run(self.get_channels_from_db())
            
            for channel_key, channel_info in channels.items():
                keyboard.append([InlineKeyboardButton(
                    f"💎 {channel_info['name']} - ₹{channel_info['price']}", 
                    callback_data=f"plan_{channel_key}"
                )])
        except:
            pass  # Handle async issues in sync context
        
        # Add server premium option
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
        server_plan = cursor.fetchone()
        conn.close()
        
        if server_plan:
            keyboard.append([InlineKeyboardButton(
                f"🌟 {server_plan[1]} - ₹{server_plan[2]} (All Channels)", 
                callback_data="plan_server_premium"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")])
        return InlineKeyboardMarkup(keyboard)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
        user = update.effective_user
        
        # Save user to database
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, first_name, join_date)
            VALUES (?, ?, ?, ?)
        ''', (user.id, user.username, user.first_name, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        
        # Check if user is admin
        if user.id in ADMIN_IDS:
            welcome_text = f"""
🌟 **Welcome Admin {user.first_name}!** 🌟

**👑 ADMIN CONTROLS:**
➕ Add/Remove Channels
💰 Approve/Reject Payments  
📊 View Analytics & Stats
🔧 Manage Server Plans

**📋 QUICK COMMANDS:**
/addchannel - Add new premium channel
/managechannels - View all channels
/pending - View pending payments

Choose an option below:
            """
            keyboard = self.get_admin_keyboard()
        else:
            welcome_text = f"""
🌟 **Welcome to Premium Channels Bot** 🌟

Hello {user.first_name}! 👋

💎 **Manage Premium Subscriptions**
📊 **Track Your Active Plans**
💰 **Handle Payments & Renewals**
🚀 **Instant Channel Access**

Choose an option below:
            """
            keyboard = self.get_main_keyboard()
        
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    async def add_channel_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding a new channel"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ You're not authorized to use this command.")
            return ConversationHandler.END
        
        await update.message.reply_text(
            "➕ **ADD NEW PREMIUM CHANNEL** ➕\n\n"
            "Let's set up your new premium channel.\n\n"
            "📝 **Step 1:** Enter the channel name\n"
            "Example: 'VIP Content', 'Premium Movies', etc.",
            parse_mode=ParseMode.MARKDOWN
        )
        return CHANNEL_NAME

    async def add_channel_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle channel name input"""
        channel_name = update.message.text.strip()
        context.user_data['channel_name'] = channel_name
        
        await update.message.reply_text(
            f"✅ **Channel Name:** {channel_name}\n\n"
            "💰 **Step 2:** Enter the price for this channel\n"
            "Example: 200, 500, 1000 (in ₹)",
            parse_mode=ParseMode.MARKDOWN
        )
        return CHANNEL_PRICE

    async def add_channel_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle channel price input"""
        try:
            price = float(update.message.text.strip())
            context.user_data['channel_price'] = price
            
            await update.message.reply_text(
                f"✅ **Price:** ₹{price}\n\n"
                "🎯 **Step 3:** Send demo link (optional)\n"
                "Send a demo link for users to preview content, or send 'skip' to continue without demo",
                parse_mode=ParseMode.MARKDOWN
            )
            return CHANNEL_DEMO
        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid price number.\n"
                "Example: 200, 500, 1000"
            )
            return CHANNEL_PRICE

    async def add_channel_demo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle channel demo link input"""
        demo_text = update.message.text.strip()
        
        if demo_text.lower() == 'skip':
            demo_link = ""
        else:
            demo_link = demo_text
        
        context.user_data['channel_demo'] = demo_link
        
        await update.message.reply_text(
            "📢 **Step 4:** Forward a message from your channel\n\n"
            "⚠️ **IMPORTANT:** Make sure I'm added as an admin in your channel first!\n\n"
            "🔹 Go to your channel\n"
            "🔹 Add this bot as admin with 'Invite Users' permission\n"
            "🔹 Forward any message from that channel here\n\n"
            "This helps me get the channel ID and create invite links.",
            parse_mode=ParseMode.MARKDOWN
        )
        return CHANNEL_FORWARD

    async def add_channel_forward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle forwarded message from channel"""
        if not update.message.forward_from_chat:
            await update.message.reply_text(
                "❌ Please forward a message FROM your channel.\n"
                "Make sure the message is forwarded from the channel you want to add."
            )
            return CHANNEL_FORWARD
        
        channel = update.message.forward_from_chat
        channel_id = channel.id
        
        # Check if bot is admin in the channel
        try:
            bot_member = await context.bot.get_chat_member(channel_id, context.bot.id)
            if bot_member.status not in [ChatMemberStatus.ADMINISTRATOR]:
                await update.message.reply_text(
                    "❌ I need to be an admin in your channel!\n\n"
                    "Please:\n"
                    "1. Go to your channel settings\n"
                    "2. Add me as administrator\n"
                    "3. Give me 'Invite Users' permission\n"
                    "4. Forward a message again"
                )
                return CHANNEL_FORWARD
        except (BadRequest, Forbidden):
            await update.message.reply_text(
                "❌ I can't access this channel. Please make sure:\n"
                "1. I'm added as admin in the channel\n"
                "2. I have 'Invite Users' permission\n"
                "3. The channel exists and is accessible"
            )
            return CHANNEL_FORWARD
        
        # Generate invite link
        try:
            invite_link = await context.bot.create_chat_invite_link(channel_id)
            invite_url = invite_link.invite_link
        except Exception as e:
            await update.message.reply_text(
                f"❌ Failed to create invite link: {str(e)}\n"
                "Please check bot permissions in the channel."
            )
            return CHANNEL_FORWARD
        
        # Save channel to database
        channel_name = context.user_data['channel_name']
        channel_price = context.user_data['channel_price']
        channel_demo = context.user_data['channel_demo']
        
        # Generate channel key
        channel_key = channel_name.lower().replace(' ', '_').replace('-', '_')
        
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO channels (channel_key, channel_name, channel_id, price, demo_link, invite_link, created_date, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (channel_key, channel_name, str(channel_id), channel_price, channel_demo, invite_url, datetime.now().isoformat(), update.effective_user.id))
            
            conn.commit()
            
            success_text = f"""
✅ **CHANNEL ADDED SUCCESSFULLY!** ✅

📋 **Channel Details:**
🏷️ **Name:** {channel_name}
🆔 **ID:** {channel_id}
💰 **Price:** ₹{channel_price}
⏰ **Duration:** 30 days (default)
🎯 **Demo:** {channel_demo if channel_demo else 'Not provided'}
🔗 **Invite Link:** Generated ✅

🚀 **Your channel is now live and available for purchase!**
            """
            
            await update.message.reply_text(success_text, parse_mode=ParseMode.MARKDOWN)
            
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                f"❌ A channel with key '{channel_key}' already exists.\n"
                "Please use a different channel name."
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Error saving channel: {str(e)}")
        finally:
            conn.close()
        
        # Clear user data
        context.user_data.clear()
        return ConversationHandler.END

    async def show_plans(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show available plans"""
        channels = await self.get_channels_from_db()
        
        if not channels:
            await update.message.reply_text(
                "📋 **No Premium Plans Available**\n\n"
                "No premium channels are currently available.\n"
                "Please check back later!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        plans_text = "💎 **PREMIUM PLANS AVAILABLE** 💎\n\n"
        
        for channel_key, channel_info in channels.items():
            plans_text += f"🔹 **{channel_info['name']}**\n"
            plans_text += f"   💰 Price: ₹{channel_info['price']}\n"
            plans_text += f"   ⏰ Duration: 30 days\n"
            if channel_info['demo_link']:
                plans_text += f"   🎯 [Demo Link]({channel_info['demo_link']})\n"
            plans_text += "\n"
        
        # Add server premium
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
        server_plan = cursor.fetchone()
        conn.close()
        
        if server_plan:
            plans_text += f"✨ **{server_plan[1]}** - ₹{server_plan[2]}\n"
            plans_text += "   🌟 Access to ALL premium channels!\n\n"
        
        plans_text += "Click below to purchase a plan:"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                plans_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.get_plans_keyboard(),
                disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(
                plans_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=self.get_plans_keyboard(),
                disable_web_page_preview=True
            )

    async def plan_selected(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle plan selection"""
        query = update.callback_query
        plan_key = query.data.split('_')[1]
        
        if plan_key == 'server' and len(query.data.split('_')) > 2:  # server_premium
            # Handle server premium
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
            server_plan = cursor.fetchone()
            conn.close()
            
            if not server_plan:
                await query.edit_message_text("❌ Server premium plan not available.")
                return
            
            plan_details = f"""
🌟 **{server_plan[1]}** 🌟

💰 **Price:** ₹{server_plan[2]}
⏰ **Duration:** 30 days
🌟 **Features:** Access to ALL premium channels!

📱 **Payment Instructions:**
1️⃣ Send ₹{server_plan[2]} to our payment method
2️⃣ Take screenshot of payment
3️⃣ Send the screenshot here
4️⃣ Wait for admin confirmation
5️⃣ Get instant access to ALL channels!

💳 **Payment Methods:**
• UPI: your-upi@paytm
• PhonePe: +91-XXXXXXXXXX
• Google Pay: your-gpay@okaxis

Ready to purchase? Click below:
            """
            
            keyboard = [
                [InlineKeyboardButton(f"💳 Purchase {server_plan[1]}", callback_data="purchase_server_premium")],
                [InlineKeyboardButton("🔙 Back to Plans", callback_data="show_plans")]
            ]
            
        else:
            # Handle individual channel
            channels = await self.get_channels_from_db()
            
            if plan_key not in channels:
                await query.edit_message_text("❌ This plan is no longer available.")
                return
            
            plan_info = channels[plan_key]
            
            plan_details = f"""
🎯 **{plan_info['name']}** 🎯

💰 **Price:** ₹{plan_info['price']}
⏰ **Duration:** 30 days
🌟 **Features:** Premium content access

📱 **Payment Instructions:**
1️⃣ Send ₹{plan_info['price']} to our payment method
2️⃣ Take screenshot of payment
3️⃣ Send the screenshot here
4️⃣ Wait for admin confirmation
5️⃣ Get instant access!

💳 **Payment Methods:**
• UPI: your-upi@paytm
• PhonePe: +91-XXXXXXXXXX
• Google Pay: your-gpay@okaxis

Ready to purchase? Click below:
            """
            
            keyboard = [
                [InlineKeyboardButton(f"💳 Purchase {plan_info['name']}", callback_data=f"purchase_{plan_key}")],
                [InlineKeyboardButton("🎯 View Demo", url=plan_info['demo_link'])] if plan_info['demo_link'] else [],
                [InlineKeyboardButton("🔙 Back to Plans", callback_data="show_plans")]
            ]
            
            # Remove empty lists
            keyboard = [row for row in keyboard if row]
        
        await query.edit_message_text(
            plan_details,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def initiate_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Initiate purchase process"""
        query = update.callback_query
        plan_data = query.data.split('_', 1)[1]  # Remove 'purchase_' prefix
        user_id = query.from_user.id
        
        if plan_data == 'server_premium':
            # Handle server premium purchase
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
            server_plan = cursor.fetchone()
            
            if server_plan:
                cursor.execute('''
                    INSERT INTO pending_payments (user_id, channel_key, amount, timestamp)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, 'server_premium', server_plan[2], datetime.now().isoformat()))
                conn.commit()
                
                plan_name = server_plan[1]
                plan_price = server_plan[2]
            conn.close()
        else:
            # Handle individual channel purchase
            channels = await self.get_channels_from_db()
            
            if plan_data not in channels:
                await query.edit_message_text("❌ This plan is no longer available.")
                return
            
            plan_info = channels[plan_data]
            
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pending_payments (user_id, channel_key, amount, timestamp)
                VALUES (?, ?, ?, ?)
            ''', (user_id, plan_data, plan_info['price'], datetime.now().isoformat()))
            conn.commit()
            conn.close()
            
            plan_name = plan_info['name']
            plan_price = plan_info['price']
        
        purchase_text = f"""
💰 **PAYMENT FOR {plan_name.upper()}** 💰

🎯 **Amount:** ₹{plan_price}
⏰ **Validity:** 30 days

💳 **PAYMENT METHODS:**

**UPI:**
└ `your-upi@paytm`

**PhonePe:**
└ `+91-XXXXXXXXXX`

**Google Pay:**
└ `your-gpay@okaxis`

📸 **NEXT STEPS:**
1️⃣ Complete payment using any method above
2️⃣ Take screenshot of successful transaction
3️⃣ Send the screenshot as photo/document
4️⃣ Admin will verify and activate your plan

⚡ **Processing Time:** Usually within 1-2 hours
        """
        
        await query.edit_message_text(
            purchase_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Plans", callback_data="show_plans")
            ]])
        )
        
        # Set user state for payment proof
        context.user_data['awaiting_payment'] = plan_data

    async def handle_payment_proof(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle payment proof submission"""
        if 'awaiting_payment' not in context.user_data:
            return
        
        user_id = update.effective_user.id
        plan_key = context.user_data['awaiting_payment']
        
        # Get plan info
        if plan_key == 'server_premium':
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
            server_plan = cursor.fetchone()
            conn.close()
            plan_name = server_plan[1] if server_plan else "Server Premium"
            plan_price = server_plan[2] if server_plan else 599
        else:
            channels = await self.get_channels_from_db()
            if plan_key in channels:
                plan_name = channels[plan_key]['name']
                plan_price = channels[plan_key]['price']
            else:
                await update.message.reply_text("❌ Invalid plan. Please try again.")
                return
        
        # Get file ID from photo or document
        file_id = None
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            file_id = update.message.document.file_id
        
        if file_id:
            # Save payment proof to database
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE pending_payments 
                SET payment_proof = ?, status = 'submitted'
                WHERE user_id = ? AND channel_key = ? AND status = 'pending'
            ''', (file_id, user_id, plan_key))
            conn.commit()
            conn.close()
            
            # Notify user
            await update.message.reply_text(
                "✅ **Payment proof submitted successfully!**\n\n"
                "📋 **Status:** Under Review\n"
                "⏰ **Processing Time:** 1-2 hours\n"
                "🔔 **You'll be notified once verified**\n\n"
                "Thank you for your patience! 😊",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Notify admins
            admin_text = f"""
🔔 **NEW PAYMENT SUBMISSION** 🔔

👤 **User:** {update.effective_user.first_name}
🆔 **User ID:** `{user_id}`
📱 **Username:** @{update.effective_user.username or 'N/A'}
💎 **Plan:** {plan_name}
💰 **Amount:** ₹{plan_price}
⏰ **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Use /approve {user_id} {plan_key} to approve
Use /reject {user_id} {plan_key} to reject
            """
            
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_photo(
                        chat_id=admin_id,
                        photo=file_id,
                        caption=admin_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin_id}: {e}")
            
            # Clear user state
            del context.user_data['awaiting_payment']

    async def send_invoice_with_links(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, plan_key: str):
        """Send invoice with invite links to user"""
        if plan_key == 'server_premium':
            # Get all channels for server premium
            channels = await self.get_channels_from_db()
            
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
            server_plan = cursor.fetchone()
            conn.close()
            
            if not server_plan:
                return
            
            end_date = datetime.now() + timedelta(days=30)
            
            invoice_text = f"""
🧾 **INVOICE - SERVER PREMIUM** 🧾

✅ **Payment Status:** APPROVED
📋 **Plan:** {server_plan[1]}
💰 **Amount:** ₹{server_plan[2]}
📅 **Valid Until:** {end_date.strftime('%Y-%m-%d %H:%M:%S')}

🌟 **YOUR PREMIUM CHANNELS ACCESS:**

"""
            
            # Add all channel invite links
            for channel_key, channel_info in channels.items():
                if channel_info['invite_link']:
                    invoice_text += f"🔗 [{channel_info['name']}]({channel_info['invite_link']})\n"
            
            invoice_text += """

🎉 **Welcome to Premium Experience!**

💡 **Important Notes:**
• Click on channel links to join
• Access valid for 30 days
• You'll get renewal reminders before expiry
• Contact support if you face any issues

Thank you for choosing our premium service! 😊
            """
            
        else:
            # Individual channel purchase
            channels = await self.get_channels_from_db()
            
            if plan_key not in channels:
                return
            
            channel_info = channels[plan_key]
            end_date = datetime.now() + timedelta(days=30)
            
            invoice_text = f"""
🧾 **INVOICE - {channel_info['name'].upper()}** 🧾

✅ **Payment Status:** APPROVED
📋 **Channel:** {channel_info['name']}
💰 **Amount:** ₹{channel_info['price']}
📅 **Valid Until:** {end_date.strftime('%Y-%m-%d %H:%M:%S')}

🔗 **YOUR CHANNEL ACCESS:**
[🎯 Join {channel_info['name']}]({channel_info['invite_link']})

🎉 **Welcome to Premium Experience!**

💡 **Important Notes:**
• Click the link above to join your premium channel
• Access valid for 30 days
• You'll get renewal reminders before expiry
• Contact support if you face any issues

Thank you for choosing our premium service! 😊
            """
        
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=invoice_text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=False
            )
            
            # Mark invoice as sent
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE subscriptions
                SET invoice_sent = 1
                WHERE user_id = ? AND channel_key = ? AND is_active = 1
            ''', (user_id, plan_key))
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Failed to send invoice to user {user_id}: {e}")

    async def approve_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command to approve payment"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ You're not authorized to use this command.")
            return
        
        try:
            args = context.args
            if len(args) < 2:
                await update.message.reply_text("Usage: /approve <user_id> <plan_key>")
                return
            
            user_id = int(args[0])
            plan_key = args[1]
            
            if plan_key == 'server_premium':
                # Handle server premium
                conn = sqlite3.connect('premium_bot.db')
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM server_plans WHERE is_active = 1 LIMIT 1')
                server_plan = cursor.fetchone()
                
                if not server_plan:
                    await update.message.reply_text("❌ Server premium plan not found.")
                    return
                
                end_date = datetime.now() + timedelta(days=30)
                plan_name = server_plan[1]
                plan_price = server_plan[2]
                
                # Create subscription for server premium
                cursor.execute('''
                    INSERT INTO subscriptions (user_id, channel_key, start_date, end_date, is_active, payment_confirmed)
                    VALUES (?, ?, ?, ?, 1, 1)
                ''', (user_id, 'server_premium', datetime.now().isoformat(), end_date.isoformat()))
                
                conn.commit()
                conn.close()
                
                # Add user to all channels
                channels = await self.get_channels_from_db()
                for channel_key, channel_info in channels.items():
                    try:
                        await context.bot.unban_chat_member(int(channel_info['channel_id']), user_id)
                    except Exception as e:
                        logger.error(f"Failed to add user to {channel_info['name']}: {e}")
                
            else:
                # Handle individual channel
                channels = await self.get_channels_from_db()
                
                if plan_key not in channels:
                    await update.message.reply_text("❌ Channel not found.")
                    return
                
                channel_info = channels[plan_key]
                end_date = datetime.now() + timedelta(days=30)
                plan_name = channel_info['name']
                plan_price = channel_info['price']
                
                conn = sqlite3.connect('premium_bot.db')
                cursor = conn.cursor()
                
                # Add subscription
                cursor.execute('''
                    INSERT INTO subscriptions (user_id, channel_key, start_date, end_date, is_active, payment_confirmed)
                    VALUES (?, ?, ?, ?, 1, 1)
                ''', (user_id, plan_key, datetime.now().isoformat(), end_date.isoformat()))
                
                conn.commit()
                conn.close()
                
                # Add user to channel
                try:
                    await context.bot.unban_chat_member(int(channel_info['channel_id']), user_id)
                except Exception as e:
                    logger.error(f"Failed to add user to channel: {e}")
            
            # Update payment status
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE pending_payments 
                SET status = 'approved'
                WHERE user_id = ? AND channel_key = ? AND status = 'submitted'
            ''', (user_id, plan_key))
            conn.commit()
            conn.close()
            
            # Send invoice with invite links
            await self.send_invoice_with_links(context, user_id, plan_key)
            
            await update.message.reply_text(
                f"✅ Payment approved for user {user_id}\n"
                f"Plan: {plan_name}\n"
                f"Amount: ₹{plan_price}\n"
                f"Valid until: {end_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Invoice with invite links sent to user!"
            )
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error approving payment: {str(e)}")

    async def reject_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Admin command to reject payment"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ You're not authorized to use this command.")
            return
        
        try:
            args = context.args
            if len(args) < 2:
                await update.message.reply_text("Usage: /reject <user_id> <plan_key> [reason]")
                return
            
            user_id = int(args[0])
            plan_key = args[1]
            reason = " ".join(args[2:]) if len(args) > 2 else "Payment verification failed"
            
            # Update payment status
            conn = sqlite3.connect('premium_bot.db')
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE pending_payments 
                SET status = 'rejected'
                WHERE user_id = ? AND channel_key = ? AND status = 'submitted'
            ''', (user_id, plan_key))
            conn.commit()
            conn.close()
            
            # Notify user
            reject_text = f"""
❌ **PAYMENT REJECTED** ❌

**Reason:** {reason}

Please contact support if you believe this is an error.
You can resubmit your payment proof if needed.
            """
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=reject_text,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to notify user: {e}")
            
            await update.message.reply_text(f"✅ Payment rejected for user {user_id}")
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error rejecting payment: {str(e)}")

    # Additional methods (manage_channels, my_subscriptions, etc.) - abbreviated for space
    async def manage_channels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all channels for management"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ You're not authorized to use this command.")
            return
        
        channels = await self.get_channels_from_db()
        
        if not channels:
            await update.message.reply_text(
                "📋 **No Channels Found**\n\n"
                "No premium channels are currently configured.\n"
                "Use /addchannel to add your first channel!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        manage_text = "📋 **CHANNEL MANAGEMENT** 📋\n\n"
        
        for channel_key, channel_info in channels.items():
            manage_text += f"🔹 **{channel_info['name']}**\n"
            manage_text += f"   💰 Price: ₹{channel_info['price']}\n"
            manage_text += f"   🆔 Channel ID: `{channel_info['channel_id']}`\n"
            manage_text += f"   🔑 Key: `{channel_key}`\n"
            if channel_info['demo_link']:
                manage_text += f"   🎯 [Demo Link]({channel_info['demo_link']})\n"
            manage_text += f"   🔗 [Invite Link]({channel_info['invite_link']})\n"
            manage_text += "\n"
        
        await update.message.reply_text(
            manage_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

    async def my_subscriptions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's active subscriptions"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT channel_key, start_date, end_date, is_active
            FROM subscriptions
            WHERE user_id = ? AND is_active = 1
            ORDER BY end_date DESC
        ''', (user_id,))
        
        subscriptions = cursor.fetchall()
        conn.close()
        
        if not subscriptions:
            await update.message.reply_text(
                "📋 **No Active Subscriptions**\n\n"
                "You don't have any active premium plans.\n"
                "Use 💎 Premium Plans to browse available options!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        subs_text = "📊 **YOUR ACTIVE SUBSCRIPTIONS** 📊\n\n"
        channels = await self.get_channels_from_db()
        
        for sub in subscriptions:
            channel_key, start_date, end_date, is_active = sub
            
            if channel_key == 'server_premium':
                plan_name = "Server Premium"
            else:
                plan_name = channels.get(channel_key, {}).get('name', 'Unknown Channel')
            
            end_dt = datetime.fromisoformat(end_date)
            days_left = (end_dt - datetime.now()).days
            
            status_emoji = "🟢" if days_left > 3 else "🟡" if days_left > 0 else "🔴"
            
            subs_text += f"{status_emoji} **{plan_name}**\n"
            subs_text += f"   📅 Started: {datetime.fromisoformat(start_date).strftime('%Y-%m-%d')}\n"
            subs_text += f"   ⏰ Expires: {end_dt.strftime('%Y-%m-%d %H:%M')}\n"
            subs_text += f"   📊 Days Left: {max(0, days_left)}\n\n"
        
        await update.message.reply_text(subs_text, parse_mode=ParseMode.MARKDOWN)

    async def pending_payments(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show pending payments to admin"""
        if update.effective_user.id not in ADMIN_IDS:
            await update.message.reply_text("❌ You're not authorized to use this command.")
            return
        
        conn = sqlite3.connect('premium_bot.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT pp.user_id, pp.channel_key, pp.amount, pp.timestamp, u.first_name, u.username
            FROM pending_payments pp
            LEFT JOIN users u ON pp.user_id = u.user_id
            WHERE pp.status = 'submitted'
            ORDER BY pp.timestamp DESC
        ''')
        
        pending = cursor.fetchall()
        conn.close()
        
        if not pending:
            await update.message.reply_text(
                "✅ **No Pending Payments**\n\n"
                "All payments have been processed!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        pending_text = "💰 **PENDING PAYMENTS** 💰\n\n"
        channels = await self.get_channels_from_db()
        
        for payment in pending:
            user_id, channel_key, amount, timestamp, first_name, username = payment
            
            if channel_key == 'server_premium':
                plan_name = "Server Premium"
            else:
                plan_name = channels.get(channel_key, {}).get('name', 'Unknown Channel')
            
            pending_text += f"👤 **{first_name}** (@{username or 'N/A'})\n"
            pending_text += f"   🆔 ID: `{user_id}`\n"
            pending_text += f"   💎 Plan: {plan_name}\n"
            pending_text += f"   💰 Amount: ₹{amount}\n"
            pending_text += f"   ⚡ Actions: `/approve {user_id} {channel_key}` | `/reject {user_id} {channel_key}`\n\n"
        
        await update.message.reply_text(pending_text, parse_mode=ParseMode.MARKDOWN)

    async def demo_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show demo links for all channels"""
        channels = await self.get_channels_from_db()
        
        demo_text = "🎯 **DEMO LINKS** 🎯\n\n"
        demo_text += "Get a preview of our premium content:\n\n"
        
        has_demos = False
        for channel_key, channel_info in channels.items():
            if channel_info['demo_link']:
                demo_text += f"🔹 [{channel_info['name']} Demo]({channel_info['demo_link']})\n"
                has_demos = True
        
        if not has_demos:
            demo_text += "No demo links are currently available.\n"
        
        demo_text += "\n💎 Ready to unlock premium content? Use 💎 Premium Plans to subscribe!"
        
        await update.message.reply_text(
            demo_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "show_plans":
            await self.show_plans(update, context)
        elif query.data.startswith("plan_"):
            await self.plan_selected(update, context)
        elif query.data.startswith("purchase_"):
            await self.initiate_purchase(update, context)
        elif query.data == "main_menu":
            await query.edit_message_text(
                "🏠 **Main Menu**\n\nWhat would you like to do?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💎 View Plans", callback_data="show_plans")
                ]])
            )

    async def cancel_add_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel adding channel"""
        context.user_data.clear()
        await update.message.reply_text(
            "❌ Channel addition cancelled.",
            reply_markup=self.get_admin_keyboard() if update.effective_user.id in ADMIN_IDS else self.get_main_keyboard()
        )
        return ConversationHandler.END

    # Health check for bot
    async def health_check_bot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Health check endpoint"""
        await update.message.reply_text("✅ Bot is running healthy!")

def run_web_server():
    """Run FastAPI server in a separate thread"""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)

def main():
    """Main function to run both web server and bot"""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ BOT_TOKEN not set! Please set the BOT_TOKEN environment variable.")
        return
    
    if not ADMIN_IDS:
        logger.error("❌ No admin IDs found! Please set the ADMIN_IDS environment variable.")
        return
    
    # Start web server in a separate thread for Render
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    logger.info(f"🌐 Web server started on port {PORT}")
    
    # Create bot instance
    bot = PremiumBot()
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Conversation handler for adding channels
    add_channel_handler = ConversationHandler(
        entry_points=[
            CommandHandler("addchannel", bot.add_channel_start),
            MessageHandler(filters.TEXT & filters.Regex("➕ Add Channel"), bot.add_channel_start)
        ],
        states={
            CHANNEL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.add_channel_name)],
            CHANNEL_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.add_channel_price)],
            CHANNEL_DEMO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.add_channel_demo)],
            CHANNEL_FORWARD: [MessageHandler(filters.ALL & ~filters.COMMAND, bot.add_channel_forward)],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel_add_channel)]
    )
    
    # Add handlers
    application.add_handler(add_channel_handler)
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("health", bot.health_check_bot))
    application.add_handler(CommandHandler("plans", bot.show_plans))
    application.add_handler(CommandHandler("subscriptions", bot.my_subscriptions))
    application.add_handler(CommandHandler("demo", bot.demo_links))
    application.add_handler(CommandHandler("approve", bot.approve_payment))
    application.add_handler(CommandHandler("reject", bot.reject_payment))
    application.add_handler(CommandHandler("managechannels", bot.manage_channels))
    application.add_handler(CommandHandler("pending", bot.pending_payments))
    
    # Callback query handler
    application.add_handler(CallbackQueryHandler(bot.button_callback))
    
    # Message handlers
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("💎 Premium Plans"), bot.show_plans))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("📊 My Subscriptions"), bot.my_subscriptions))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("🎯 Demo Links"), bot.demo_links))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("📋 Manage Channels"), bot.manage_channels))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex("💰 Pending Payments"), bot.pending_payments))
    
    # Payment proof handler
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, bot.handle_payment_proof))
    
    # Log startup
    logger.info(f"🚀 Premium Bot starting...")
    logger.info(f"✅ Admin IDs: {ADMIN_IDS}")
    logger.info(f"✅ Port: {PORT}")
    
    # Start the bot
    print("🚀 Premium Bot is starting...")
    print(f"🌐 Web server running on port {PORT}")
    print("✅ Dynamic channel management enabled")
    print("✅ Auto invoice with invite links enabled")
    print("✅ Render deployment ready")
    
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()