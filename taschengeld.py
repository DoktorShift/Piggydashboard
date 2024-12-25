# taschengeld.py

import os
import logging
from logging.handlers import RotatingFileHandler
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from dotenv import load_dotenv
import requests
import traceback
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, render_template
from datetime import datetime, timedelta
import threading
import qrcode
import io
import base64
import json
from urllib.parse import urlparse
import re

# --------------------- Configuration and Setup ---------------------

# Load environment variables from the .env file
load_dotenv()

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Convert CHAT_ID to an integer, if present
try:
    CHAT_ID = int(CHAT_ID)
except (TypeError, ValueError):
    raise EnvironmentError("CHAT_ID must be an integer.")

# LNbits Configuration
LNBITS_READONLY_API_KEY = os.getenv("LNBITS_READONLY_API_KEY")
LNBITS_URL = os.getenv("LNBITS_URL")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "LNbits Instance")

# Extract domain from LNBITS_URL
parsed_lnbits_url = urlparse(LNBITS_URL)
LNBITS_DOMAIN = parsed_lnbits_url.netloc

# Donation Parameters
LNURLP_ID = os.getenv("LNURLP_ID")

# Notification Settings
BALANCE_CHANGE_THRESHOLD = int(os.getenv("BALANCE_CHANGE_THRESHOLD", "10"))  # Default: 10 sats
HIGHLIGHT_THRESHOLD = int(os.getenv("HIGHLIGHT_THRESHOLD", "2100"))  # Default: 2100 sats
LATEST_TRANSACTIONS_COUNT = int(os.getenv("LATEST_TRANSACTIONS_COUNT", "21"))  # Default: 21 transactions

# Scheduler Intervals (in seconds)
WALLET_INFO_UPDATE_INTERVAL = int(os.getenv("WALLET_INFO_UPDATE_INTERVAL", "86400"))  # Default: 86400 seconds (24 hours)
WALLET_BALANCE_NOTIFICATION_INTERVAL = int(os.getenv("WALLET_BALANCE_NOTIFICATION_INTERVAL", "86400"))  # Default: 86400 seconds (24 hours)
PAYMENTS_FETCH_INTERVAL = int(os.getenv("PAYMENTS_FETCH_INTERVAL", "60"))  # Default: 60 seconds (1 minute)

# Flask Server Configuration
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")  # Default: localhost
APP_PORT = int(os.getenv("APP_PORT", "5009"))  # Default: port 5009

# File Paths
PROCESSED_PAYMENTS_FILE = os.getenv("PROCESSED_PAYMENTS_FILE", "processed_payments.txt")
CURRENT_BALANCE_FILE = os.getenv("CURRENT_BALANCE_FILE", "current-balance.txt")
DONATIONS_FILE = os.getenv("DONATIONS_FILE", "donations.json")

# Donation Configuration
DONATIONS_URL = os.getenv("DONATIONS_URL")  # Optional; no default value

# Information URL Configuration
INFORMATION_URL = os.getenv("INFORMATION_URL")  # New environment variable

# Profanity Filter Configuration
FORBIDDEN_WORDS_FILE = os.getenv("FORBIDDEN_WORDS_FILE", "forbidden_words.txt")

# Validate essential environment variables (excluding Overwatch and DONATIONS_URL)
required_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "CHAT_ID": CHAT_ID,
    "LNBITS_READONLY_API_KEY": LNBITS_READONLY_API_KEY,
    "LNBITS_URL": LNBITS_URL
}

missing_vars = [var for var, value in required_vars.items() if not value]
if missing_vars:
    raise EnvironmentError(f"Required environment variables missing: {', '.join(missing_vars)}")

# Initialize the Telegram Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# --------------------- Logging Configuration ---------------------
logger = logging.getLogger("lnbits_logger")
logger.setLevel(logging.DEBUG)

# File handler for detailed logs
file_handler = RotatingFileHandler("app.log", maxBytes=5 * 1024 * 1024, backupCount=3)
file_handler.setLevel(logging.DEBUG)

# Console handler for general information
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Log format
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add handlers to the logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# --------------------- Helper Functions ---------------------

def load_forbidden_words(file_path):
    """
    Load forbidden words from a specified file into a set.
    
    Args:
        file_path (str): Path to the forbidden words file.
        
    Returns:
        set: A set containing all forbidden words.
    """
    forbidden = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                word = line.strip()
                if word:  # Avoid empty lines
                    forbidden.add(word.lower())
        logger.debug(f"Loaded forbidden words from {file_path}: {forbidden}")
    except FileNotFoundError:
        logger.error(f"Forbidden words file not found at {file_path}.")
    except Exception as e:
        logger.error(f"Error loading forbidden words from {file_path}: {e}")
        logger.debug(traceback.format_exc())
    return forbidden

def sanitize_memo(memo, forbidden_words):
    """
    Sanitize the memo field by replacing forbidden words with asterisks.
    
    Args:
        memo (str): The original memo text.
        forbidden_words (set): A set of forbidden words.
        
    Returns:
        str: The sanitized memo text.
    """
    if not memo:
        return "No Memo"
    
    # Function to replace the matched word with asterisks
    def replace_match(match):
        word = match.group()
        return '*' * len(word)
    
    # Create a regex pattern that matches any forbidden words
    if not forbidden_words:
        return memo  # No forbidden words to sanitize
    
    pattern = re.compile(r'\b(' + '|'.join(map(re.escape, forbidden_words)) + r')\b', re.IGNORECASE)
    sanitized_memo = pattern.sub(replace_match, memo)
    logger.debug(f"Sanitized Memo: Original: '{memo}' -> Sanitized: '{sanitized_memo}'")
    return sanitized_memo

def load_processed_payments():
    """
    Load already processed payment hashes from the tracking file into a set.
    """
    processed = set()
    if os.path.exists(PROCESSED_PAYMENTS_FILE):
        try:
            with open(PROCESSED_PAYMENTS_FILE, 'r') as f:
                for line in f:
                    processed.add(line.strip())
            logger.debug(f"Loaded {len(processed)} processed payment hashes.")
        except Exception as e:
            logger.error(f"Error loading processed payments: {e}")
            logger.debug(traceback.format_exc())
    return processed

def add_processed_payment(payment_hash):
    """
    Add a processed payment hash to the tracking file.
    """
    try:
        with open(PROCESSED_PAYMENTS_FILE, 'a') as f:
            f.write(f"{payment_hash}\n")
        logger.debug(f"Added payment hash {payment_hash} to processed payments.")
    except Exception as e:
        logger.error(f"Error adding processed payment: {e}")
        logger.debug(traceback.format_exc())

def load_last_balance():
    """
    Load the last known balance from the balance file.
    """
    if not os.path.exists(CURRENT_BALANCE_FILE):
        logger.info("Balance file does not exist. Initializing with current balance.")
        return None
    try:
        with open(CURRENT_BALANCE_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                logger.warning("Balance file is empty. Setting last balance to 0.")
                return 0.0
            try:
                balance = float(content)
                logger.debug(f"Loaded last balance: {balance} sats.")
                return balance
            except ValueError:
                logger.error(f"Invalid balance value in file: {content}. Setting last balance to 0.")
                return 0.0
    except Exception as e:
        logger.error(f"Error loading last balance: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def save_current_balance(balance):
    """
    Save the current balance to the balance file.
    """
    try:
        with open(CURRENT_BALANCE_FILE, 'w') as f:
            f.write(f"{balance}\n")
        logger.debug(f"Successfully saved current balance {balance} sats.")
    except Exception as e:
        logger.error(f"Error saving current balance: {e}")
        logger.debug(traceback.format_exc())

def load_donations():
    """
    Load donations from the donations file into the donations list and set total donations.
    """
    global donations, total_donations
    if os.path.exists(DONATIONS_FILE):
        try:
            with open(DONATIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                donations = data.get("donations", [])
                total_donations = data.get("total_donations", 0)
            logger.debug(f"Loaded {len(donations)} donations from the file.")
        except Exception as e:
            logger.error(f"Error loading donations: {e}")
            logger.debug(traceback.format_exc())

def save_donations():
    """
    Save donations to the donations file.
    """
    try:
        with open(DONATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "total_donations": total_donations,
                "donations": donations
            }, f, ensure_ascii=False, indent=4)
        logger.debug("Successfully saved donations data.")
    except Exception as e:
        logger.error(f"Error saving donations: {e}")
        logger.debug(traceback.format_exc())

# Initialize the set of processed payments
processed_payments = load_processed_payments()

# Initialize the Flask app
app = Flask(__name__)

# Global variables to store the latest data
latest_balance = {
    "balance_sats": None,
    "last_change": None,
    "memo": None
}

latest_payments = []

# Data structures for donations
donations = []
total_donations = 0

# Global variable to track the last update time
last_update = datetime.utcnow()

# Load existing donations at startup
load_donations()

# Load forbidden words at startup
FORBIDDEN_WORDS = load_forbidden_words(FORBIDDEN_WORDS_FILE)

# --------------------- Functions ---------------------

def fetch_api(endpoint):
    """
    Fetch data from the LNbits API.
    """
    url = f"{LNBITS_URL}/api/v1/{endpoint}"
    headers = {"X-Api-Key": LNBITS_READONLY_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            logger.debug(f"Fetched data from {endpoint}: {data}")
            return data
        else:
            logger.error(f"Error fetching {endpoint}. Status Code: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error fetching {endpoint}: {e}")
        logger.debug(traceback.format_exc())
        return None

def fetch_pay_links():
    """
    Fetch Pay-Links from the LNbits LNURLp Extension API.
    """
    url = f"{LNBITS_URL}/lnurlp/api/v1/links"
    headers = {"X-Api-Key": LNBITS_READONLY_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            logger.debug(f"Fetched Pay-Links: {data}")
            return data
        else:
            logger.error(f"Error fetching Pay-Links. Status Code: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Error fetching Pay-Links: {e}")
        logger.debug(traceback.format_exc())
        return None

def get_lnurlp_info(lnurlp_id):
    """
    Fetch LNURLp information for a given lnurlp_id.
    """
    pay_links = fetch_pay_links()
    if pay_links is None:
        logger.error("Cannot fetch Pay-Links.")
        return None

    for pay_link in pay_links:
        if pay_link.get("id") == lnurlp_id:
            logger.debug(f"Matching Pay-Link found: {pay_link}")
            return pay_link

    logger.error(f"No Pay-Link found with ID {lnurlp_id}.")
    return None

def fetch_donation_details():
    """
    Fetch LNURLp information and integrate the Lightning Address and LNURL into the donation details.
    
    Returns:
        dict: A dictionary containing total donations, donations list, Lightning Address, and LNURL.
    """
    lnurlp_info = get_lnurlp_info(LNURLP_ID)
    if lnurlp_info is None:
        logger.error("Cannot fetch LNURLp information for donation details.")
        return {
            "total_donations": total_donations,
            "donations": donations,
            "lightning_address": "Not Available",
            "lnurl": "Not Available",
            "highlight_threshold": HIGHLIGHT_THRESHOLD  # Include threshold
        }

    # Extract the username and construct the Lightning Address
    username = lnurlp_info.get('username')  # Adjust key based on your LNURLp response
    if not username:
        username = "Unknown"
        logger.warning("Username not found in LNURLp information.")

    # Construct the full Lightning Address
    lightning_address = f"{username}@{LNBITS_DOMAIN}"

    # Extract the LNURL
    lnurl = lnurlp_info.get('lnurl', 'Not Available')  # Adjust key based on your data structure

    logger.debug(f"Constructed Lightning Address: {lightning_address}")
    logger.debug(f"Fetched LNURL: {lnurl}")

    return {
        "total_donations": total_donations,
        "donations": donations,
        "lightning_address": lightning_address,
        "lnurl": lnurl,
        "highlight_threshold": HIGHLIGHT_THRESHOLD  # Include threshold
    }

def update_donations_with_details(data):
    """
    Update the donations data with additional details like Lightning Address and LNURL.
    
    Parameters:
        data (dict): The original donations data.
    
    Returns:
        dict: Updated donations data with additional details.
    """
    donation_details = fetch_donation_details()
    data.update({
        "lightning_address": donation_details.get("lightning_address"),
        "lnurl": donation_details.get("lnurl"),
        "highlight_threshold": donation_details.get("highlight_threshold")  # Include threshold
    })
    return data

def updateDonations(data):
    """
    Update donations and related UI elements with new data.
    
    This function has been extended to include Lightning Address and LNURL in the data sent to the frontend.
    
    Parameters:
        data (dict): The data containing total donations and the donations list.
    """
    # Integrate additional donation details
    updated_data = update_donations_with_details(data)
    
    totalDonations = updated_data["total_donations"]
    # Update total donations in the frontend
    # Since this is a backend function, the frontend will fetch updated data via the API
    # Therefore, no direct DOM manipulation here
    
    # Update the latest donation
    if updated_data["donations"]:
        latestDonation = updated_data["donations"][-1]
        # Frontend handles DOM updates
        sanitized_memo = sanitize_memo(latestDonation["memo"], FORBIDDEN_WORDS)
        logger.info(f'Latest donation: {latestDonation["amount"]} sats - "{sanitized_memo}"')
    else:
        logger.info('Latest donation: No donations yet.')
    
    # Update transaction data
    # Frontend retrieves this via the API
    
    # Update Lightning Address and LNURL
    logger.debug(f"Lightning Address: {updated_data.get('lightning_address')}")
    logger.debug(f"LNURL: {updated_data.get('lnurl')}")
    
    # Save the updated donations data
    save_donations()

def send_latest_payments():
    """
    Fetch the latest payments and send a notification via Telegram.
    Additionally, check if payments qualify as donations.
    """
    global total_donations, donations, last_update  # Declare global variables
    logger.info("Fetching the latest payments...")
    payments = fetch_api("payments")
    if payments is None:
        return

    if not isinstance(payments, list):
        logger.error("Unexpected data format for payments.")
        return

    # Sort payments by creation time descending
    sorted_payments = sorted(payments, key=lambda x: x.get("created_at", ""), reverse=True)
    latest = sorted_payments[:LATEST_TRANSACTIONS_COUNT]  # Fetch the latest n payments

    if not latest:
        logger.info("No payments found.")
        return

    # Initialize lists for different payment types
    incoming_payments = []
    outgoing_payments = []
    pending_payments = []
    new_processed_hashes = []

    for payment in latest:
        payment_hash = payment.get("payment_hash")
        if payment_hash in processed_payments:
            continue  # Skip already processed payments

        amount_msat = payment.get("amount", 0)
        memo = payment.get("memo", "No Memo")
        status = payment.get("status", "completed")

        try:
            amount_sats = int(abs(amount_msat) / 1000)
        except ValueError:
            amount_sats = 0

        if status.lower() == "pending":
            if amount_msat > 0:
                pending_payments.append({
                    "amount": amount_sats,
                    "memo": memo
                })
        else:
            if amount_msat > 0:
                incoming_payments.append({
                    "amount": amount_sats,
                    "memo": memo
                })
            elif amount_msat < 0:
                outgoing_payments.append({
                    "amount": amount_sats,
                    "memo": memo
                })

        # Check for donations via LNURLp ID
        extra_data = payment.get("extra", {})
        lnurlp_id_payment = extra_data.get("link")
        if lnurlp_id_payment == LNURLP_ID:
            # It's a donation
            donation_memo = extra_data.get("comment", "No Memo")
            # Ensure 'extra' is a numeric value in msats
            try:
                donation_amount_msat = int(extra_data.get("extra", 0))
                donation_amount_sats = donation_amount_msat / 1000  # Convert msats to sats
            except (ValueError, TypeError):
                donation_amount_sats = amount_sats  # Fallback if 'extra' is not numeric
            donation = {
                "date": datetime.utcnow().isoformat(),
                "memo": donation_memo,
                "amount": donation_amount_sats
            }
            donations.append(donation)
            total_donations += donation_amount_sats
            last_update = datetime.utcnow()
            # **Fixed Line:** Pass donation_memo instead of donation_amount_sats
            sanitized_memo = sanitize_memo(donation_memo, FORBIDDEN_WORDS)
            logger.info(f"New donation detected: {donation_amount_sats} sats - {donation_memo}")
            updateDonations({
                "total_donations": total_donations,
                "donations": donations
            })  # Update donations with details

        # Mark the payment as processed
        processed_payments.add(payment_hash)
        new_processed_hashes.append(payment_hash)
        add_processed_payment(payment_hash)

    if not incoming_payments and not outgoing_payments and not pending_payments:
        logger.info("No new payments to notify.")
        return

    message_lines = [
        f"⚡ *{INSTANCE_NAME}* - *Latest Transactions* ⚡\n"
    ]

    if incoming_payments:
        message_lines.append("🟢 *Incoming Payments:*")
        for idx, payment in enumerate(incoming_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Amount:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if outgoing_payments:
        message_lines.append("🔴 *Outgoing Payments:*")
        for idx, payment in enumerate(outgoing_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Amount:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if pending_payments:
        message_lines.append("⏳ *Pending Payments:*")
        for payment in pending_payments:
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"   {payment['amount']} sats\n"
                f"   📝 *Memo:* {sanitized_memo}\n"
                f"   📅 *Status:* Pending\n"
            )
        message_lines.append("")

    # Add timestamp
    timestamp_text = f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    message_lines.append(timestamp_text)

    full_message = "\n".join(message_lines)

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send the message to Telegram with the inline keyboard
    try:
        bot.send_message(chat_id=CHAT_ID, text=full_message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        logger.info("Latest payments notification successfully sent to Telegram.")
        latest_payments.extend(new_processed_hashes)
    except Exception as telegram_error:
        logger.error(f"Error sending payments message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def check_balance_change():
    """
    Periodically check the wallet balance and notify if it changes beyond the threshold.
    """
    global last_update
    logger.info("Checking balance changes...")
    wallet_info = fetch_api("wallet")
    if wallet_info is None:
        return

    current_balance_msat = wallet_info.get("balance", 0)
    current_balance_sats = current_balance_msat / 1000  # Convert msats to sats

    last_balance = load_last_balance()

    if last_balance is None:
        # First run, initialize the balance file
        save_current_balance(current_balance_sats)
        latest_balance["balance_sats"] = current_balance_sats
        latest_balance["last_change"] = "Initial balance set."
        latest_balance["memo"] = "N/A"
        logger.info(f"Initial balance set to {current_balance_sats:.0f} sats.")
        return

    change_amount = current_balance_sats - last_balance
    if abs(change_amount) < BALANCE_CHANGE_THRESHOLD:
        logger.info(f"Balance change ({abs(change_amount):.0f} sats) below threshold ({BALANCE_CHANGE_THRESHOLD} sats). No notification sent.")
        return

    direction = "increased" if change_amount > 0 else "decreased"
    abs_change = abs(change_amount)

    # Prepare the Telegram message with Markdown formatting
    message = (
        f"⚡ *{INSTANCE_NAME}* - *Balance Update* ⚡\n\n"
        f"🔹 *Previous Balance:* `{int(last_balance):,} sats`\n"
        f"🔹 *Change:* `{'+' if change_amount > 0 else '-'}{int(abs_change):,} sats`\n"
        f"🔹 *New Balance:* `{int(current_balance_sats):,} sats`\n\n"
        f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send the message to Telegram with the inline keyboard
    try:
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        logger.info(f"Balance changed from {last_balance:.0f} to {current_balance_sats:.0f} sats. Notification sent.")
        # Update the balance file and latest balance data
        save_current_balance(current_balance_sats)
        latest_balance["balance_sats"] = current_balance_sats
        latest_balance["last_change"] = f"Balance {direction} by {int(abs_change):,} sats."
        latest_balance["memo"] = "N/A"
    except Exception as telegram_error:
        logger.error(f"Error sending balance change message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def send_wallet_balance():
    """
    Send the current wallet balance via Telegram in a professional and clear format.
    """
    logger.info("Sending daily wallet balance notification...")
    wallet_info = fetch_api("wallet")
    if wallet_info is None:
        return

    current_balance_msat = wallet_info.get("balance", 0)
    current_balance_sats = current_balance_msat / 1000  # Convert msats to sats

    # Fetch payments to calculate counts and totals
    payments = fetch_api("payments")
    incoming_count = outgoing_count = 0
    incoming_total = outgoing_total = 0
    if payments and isinstance(payments, list):
        for payment in payments:
            amount_msat = payment.get("amount", 0)
            status = payment.get("status", "completed")
            if status.lower() == "pending":
                continue  # Exclude pending payments for daily balance
            if amount_msat > 0:
                incoming_count += 1
                incoming_total += amount_msat / 1000
            elif amount_msat < 0:
                outgoing_count += 1
                outgoing_total += abs(amount_msat) / 1000

    # Prepare the Telegram message with Markdown formatting
    message = (
        f"📊 *{INSTANCE_NAME}* - *Daily Wallet Balance* 📊\n\n"
        f"🔹 *Current Balance:* `{int(current_balance_sats)} sats`\n"
        f"🔹 *Total Incoming:* `{int(incoming_total)} sats` over `{incoming_count}` transactions\n"
        f"🔹 *Total Outgoing:* `{int(outgoing_total)} sats` over `{outgoing_count}` transactions\n\n"
        f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send the message to Telegram with the inline keyboard
    try:
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        logger.info("Daily wallet balance notification with inline keyboard successfully sent.")
        # Update the latest balance data
        latest_balance["balance_sats"] = current_balance_sats
        latest_balance["last_change"] = "Daily balance report."
        latest_balance["memo"] = "N/A"
        # Save the current balance
        save_current_balance(current_balance_sats)
    except Exception as telegram_error:
        logger.error(f"Error sending daily wallet balance message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_transactions_command(chat_id):
    """
    Handle the /transactions command sent by the user.
    """
    logger.info(f"Handling /transactions command for chat_id: {chat_id}")
    payments = fetch_api("payments")
    if payments is None:
        bot.send_message(chat_id=chat_id, text="Error fetching transactions.")
        return

    if not isinstance(payments, list):
        bot.send_message(chat_id=chat_id, text="Unexpected data format for transactions.")
        return

    # Sort transactions by creation time descending
    sorted_payments = sorted(payments, key=lambda x: x.get("created_at", ""), reverse=True)
    latest = sorted_payments[:LATEST_TRANSACTIONS_COUNT]  # Fetch the latest n transactions

    if not latest:
        bot.send_message(chat_id=chat_id, text="No transactions found.")
        return

    # Initialize lists for different transaction types
    incoming_payments = []
    outgoing_payments = []
    pending_payments = []

    for payment in latest:
        amount_msat = payment.get("amount", 0)
        memo = payment.get("memo", "No Memo")
        status = payment.get("status", "completed")

        try:
            amount_sats = int(abs(amount_msat) / 1000)
        except ValueError:
            amount_sats = 0

        if status.lower() == "pending":
            if amount_msat > 0:
                pending_payments.append({
                    "amount": amount_sats,
                    "memo": memo
                })
        else:
            if amount_msat > 0:
                incoming_payments.append({
                    "amount": amount_sats,
                    "memo": memo
                })
            elif amount_msat < 0:
                outgoing_payments.append({
                    "amount": amount_sats,
                    "memo": memo
                })

    message_lines = [
        f"⚡ *{INSTANCE_NAME}* - *Latest Transactions* ⚡\n"
    ]

    if incoming_payments:
        message_lines.append("🟢 *Incoming Payments:*")
        for idx, payment in enumerate(incoming_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Amount:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if outgoing_payments:
        message_lines.append("🔴 *Outgoing Payments:*")
        for idx, payment in enumerate(outgoing_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Amount:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if pending_payments:
        message_lines.append("⏳ *Pending Payments:*")
        for payment in pending_payments:
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"   {payment['amount']} sats\n"
                f"   📝 *Memo:* {sanitized_memo}\n"
                f"   📅 *Status:* Pending\n"
            )
        message_lines.append("")

    # Add timestamp
    timestamp_text = f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    message_lines.append(timestamp_text)

    full_message = "\n".join(message_lines)

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=full_message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Error sending /transactions message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_info_command(chat_id):
    """
    Handle the /info command sent by the user.
    """
    logger.info(f"Handling /info command for chat_id: {chat_id}")
    # Prepare interval information
    interval_info = (
        f"🔔 *Balance Change Threshold:* `{BALANCE_CHANGE_THRESHOLD} sats`\n"
        f"🔔 *Highlight Threshold:* `{HIGHLIGHT_THRESHOLD} sats`\n"
        f"⏲️ *Balance Change Monitoring Interval:* Every `{WALLET_INFO_UPDATE_INTERVAL} seconds`\n"
        f"📊 *Daily Wallet Balance Notification Interval:* Every `{WALLET_BALANCE_NOTIFICATION_INTERVAL} seconds`\n"
        f"🔄 *Latest Payments Fetch Interval:* Every `{PAYMENTS_FETCH_INTERVAL} seconds`"
    )

    info_message = (
        f"ℹ️ *{INSTANCE_NAME}* - *Information*\n\n"
        f"{interval_info}\n\n"
        f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=info_message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Error sending /info message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_balance_command(chat_id):
    """
    Handle the /balance command sent by the user.
    """
    logger.info(f"Handling /balance command for chat_id: {chat_id}")
    wallet_info = fetch_api("wallet")
    if wallet_info is None:
        bot.send_message(chat_id=chat_id, text="Error fetching wallet balance.")
        return

    current_balance_msat = wallet_info.get("balance", 0)
    current_balance_sats = current_balance_msat / 1000  # Convert msats to sats

    message = (
        f"📊 *{INSTANCE_NAME}* - *Wallet Balance*\n\n"
        f"🔹 *Current Balance:* `{int(current_balance_sats)} sats`\n\n"
        f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Error sending /balance message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_help_command(chat_id):
    """
    Handle the /help command sent by the user.
    """
    logger.info(f"Handling /help command for chat_id: {chat_id}")
    help_message = (
        f"ℹ️ *{INSTANCE_NAME}* - *Help*\n\n"
        f"Available Commands:\n"
        f"• `/balance` – Shows the current wallet balance.\n"
        f"• `/transactions` – Shows the latest transactions.\n"
        f"• `/info` – Provides information about the monitor and current settings.\n"
        f"• `/help` – Shows this help message.\n\n"
        f"🕒 *Timestamp:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Show Piggy Bank", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Show Transactions", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=help_message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Error sending /help message to Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def process_update(update):
    """
    Process incoming updates from the Telegram webhook.
    """
    try:
        if 'message' in update:
            message = update['message']
            chat_id = message['chat']['id']
            text = message.get('text', '').strip()

            if text.startswith('/balance'):
                handle_balance_command(chat_id)
            elif text.startswith('/transactions'):
                handle_transactions_command(chat_id)
            elif text.startswith('/info'):
                handle_info_command(chat_id)
            elif text.startswith('/help'):
                handle_help_command(chat_id)
            else:
                bot.send_message(
                    chat_id=chat_id,
                    text="Unknown command. Available commands: /balance, /transactions, /info, /help"
                )
        elif 'callback_query' in update:
            process_callback_query(update['callback_query'])
        else:
            logger.info("Update contains no message or callback_query. Ignoring.")
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        logger.debug(traceback.format_exc())

def process_callback_query(callback_query):
    """
    Process callback queries from inline keyboards in Telegram messages.
    """
    try:
        query_id = callback_query['id']
        data = callback_query.get('data', '')
        chat_id = callback_query['from']['id']

        if data == 'view_transactions':
            handle_transactions_command(chat_id)
            bot.answer_callback_query(callback_query_id=query_id, text="Fetching transactions...")
        else:
            bot.answer_callback_query(callback_query_id=query_id, text="Unknown action.")
    except Exception as e:
        logger.error(f"Error processing callback query: {e}")
        logger.debug(traceback.format_exc())

def start_scheduler():
    """
    Start the scheduler for periodic tasks using BackgroundScheduler.
    """
    scheduler = BackgroundScheduler(timezone='UTC')

    if WALLET_INFO_UPDATE_INTERVAL > 0:
        scheduler.add_job(
            check_balance_change,
            'interval',
            seconds=WALLET_INFO_UPDATE_INTERVAL,
            id='balance_check',
            next_run_time=datetime.utcnow() + timedelta(seconds=1)
        )
        logger.info(f"Balance change monitoring scheduled every {WALLET_INFO_UPDATE_INTERVAL} seconds.")
    else:
        logger.info("Balance change monitoring is disabled (WALLET_INFO_UPDATE_INTERVAL set to 0).")

    if WALLET_BALANCE_NOTIFICATION_INTERVAL > 0:
        scheduler.add_job(
            send_wallet_balance,
            'interval',
            seconds=WALLET_BALANCE_NOTIFICATION_INTERVAL,
            id='wallet_balance_notification',
            next_run_time=datetime.utcnow() + timedelta(seconds=1)
        )
        logger.info(f"Daily wallet balance notification scheduled every {WALLET_BALANCE_NOTIFICATION_INTERVAL} seconds.")
    else:
        logger.info("Daily wallet balance notification is disabled (WALLET_BALANCE_NOTIFICATION_INTERVAL set to 0).")

    if PAYMENTS_FETCH_INTERVAL > 0:
        scheduler.add_job(
            send_latest_payments,
            'interval',
            seconds=PAYMENTS_FETCH_INTERVAL,
            id='latest_payments_fetch',
            next_run_time=datetime.utcnow() + timedelta(seconds=1)
        )
        logger.info(f"Latest payments fetch scheduled every {PAYMENTS_FETCH_INTERVAL} seconds.")
    else:
        logger.info("Fetching latest payments is disabled (PAYMENTS_FETCH_INTERVAL set to 0).")

    scheduler.start()
    logger.info("Scheduler successfully started.")

# --------------------- Flask Routes ---------------------

@app.route('/')
def home():
    return "🔍 LNbits Monitor is running."

@app.route('/status', methods=['GET'])
def status():
    """
    Returns the status of the application, including the latest balance, payments, total donations, donations, Lightning Address, and LNURL.
    """
    donation_details = fetch_donation_details()
    return jsonify({
        "latest_balance": latest_balance,
        "latest_payments": latest_payments,
        "total_donations": donation_details["total_donations"],
        "donations": donation_details["donations"],
        "lightning_address": donation_details["lightning_address"],
        "lnurl": donation_details["lnurl"],
        "highlight_threshold": donation_details["highlight_threshold"]  # Include threshold
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        logger.warning("Received empty update.")
        return "No update found", 400

    logger.debug(f"Received update: {update}")

    # Process the message in a separate thread to avoid blocking
    threading.Thread(target=process_update, args=(update,)).start()

    return "OK", 200

@app.route('/donations')
def donations_page():
    # Fetch LNURLp information
    lnurlp_id = LNURLP_ID
    lnurlp_info = get_lnurlp_info(lnurlp_id)
    if lnurlp_info is None:
        return "Error fetching LNURLp information", 500

    # Extract necessary information
    wallet_name = lnurlp_info.get('description', 'Unknown Wallet')
    lightning_address = lnurlp_info.get('lightning_address', 'Unknown Lightning Address')  # Adjust key based on your data structure
    lnurl = lnurlp_info.get('lnurl', 'Not Available')  # Adjust key based on your data structure

    # Generate a QR code from the LNURL
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(lnurl)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # Convert the PIL image to a Base64 string to embed it in HTML
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    img_base64 = base64.b64encode(img_io.getvalue()).decode()

    # Calculate the total donations for this LNURLp
    total_donations_current = sum(donation['amount'] for donation in donations)

    # Pass the donations list and additional details to the template to display individual transactions
    return render_template(
        'donations.html',
        wallet_name=wallet_name,
        lightning_address=lightning_address,
        lnurl=lnurl,
        qr_code_data=img_base64,
        donations_url=DONATIONS_URL,  # Pass the donations URL to the template
        information_url=INFORMATION_URL,  # Pass the information URL to the template
        total_donations=total_donations_current,  # Pass the total donations
        donations=donations,  # Pass the donations list
        highlight_threshold=HIGHLIGHT_THRESHOLD  # Pass the highlight threshold
    )

# API endpoint to provide donation data
@app.route('/api/donations', methods=['GET'])
def get_donations_data():
    """
    Provides the donations data as JSON for the frontend, including Lightning Address, LNURL, and highlight threshold.
    """
    try:
        donation_details = fetch_donation_details()
        data = {
            "total_donations": donation_details["total_donations"],
            "donations": donation_details["donations"],
            "lightning_address": donation_details["lightning_address"],
            "lnurl": donation_details["lnurl"],
            "highlight_threshold": donation_details["highlight_threshold"]  # Include threshold
        }
        logger.debug(f"Served donations data with details: {data}")
        return jsonify(data), 200
    except Exception as e:
        logger.error(f"Error fetching donations data: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({"error": "Error fetching donations data"}), 500

# Endpoint for long-polling updates
@app.route('/donations_updates', methods=['GET'])
def donations_updates():
    """
    Endpoint for clients to check the timestamp of the last donations update.
    """
    global last_update
    try:
        return jsonify({"last_update": last_update.isoformat()}), 200
    except Exception as e:
        logger.error(f"Error fetching last update: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({"error": "Error fetching last update"}), 500

# --------------------- Application Entry Point ---------------------

if __name__ == "__main__":
    logger.info("🚀 Starting Pocket Money Balance Monitor.")

    # Log the current configuration
    logger.info(f"🔔 Notification Threshold: {BALANCE_CHANGE_THRESHOLD} sats")
    logger.info(f"🔔 Highlight Threshold: {HIGHLIGHT_THRESHOLD} sats")
    logger.info(f"📊 Fetching the latest {LATEST_TRANSACTIONS_COUNT} transactions for notifications")
    logger.info(f"⏲️ Scheduler Intervals - Balance Change Monitoring: {WALLET_INFO_UPDATE_INTERVAL} seconds, Daily Wallet Balance Notification: {WALLET_BALANCE_NOTIFICATION_INTERVAL} seconds, Latest Payments Fetch: {PAYMENTS_FETCH_INTERVAL} seconds")

    # Start the scheduler in a separate thread
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()

    # Start the Flask app
    logger.info(f"Flask server running on {APP_HOST}:{APP_PORT}")
    app.run(host=APP_HOST, port=APP_PORT)
