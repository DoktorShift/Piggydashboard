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

# --------------------- Konfiguration und Setup ---------------------

# Laden von Umgebungsvariablen aus der .env-Datei
load_dotenv()

# Telegram-Konfiguration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# Konvertieren von CHAT_ID zu einem Integer, falls vorhanden
try:
    CHAT_ID = int(CHAT_ID)
except (TypeError, ValueError):
    raise EnvironmentError("CHAT_ID muss eine Ganzzahl sein.")

# LNbits-Konfiguration
LNBITS_READONLY_API_KEY = os.getenv("LNBITS_READONLY_API_KEY")
LNBITS_URL = os.getenv("LNBITS_URL")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "LNbits Instance")

# Extrahieren der Domain aus LNBITS_URL
parsed_lnbits_url = urlparse(LNBITS_URL)
LNBITS_DOMAIN = parsed_lnbits_url.netloc

# Spendenparameter
LNURLP_ID = os.getenv("LNURLP_ID")

# Benachrichtigungseinstellungen
BALANCE_CHANGE_THRESHOLD = int(os.getenv("BALANCE_CHANGE_THRESHOLD", "10"))  # Standard: 10 sats
HIGHLIGHT_THRESHOLD = int(os.getenv("HIGHLIGHT_THRESHOLD", "2100"))  # Standard: 2100 sats
LATEST_TRANSACTIONS_COUNT = int(os.getenv("LATEST_TRANSACTIONS_COUNT", "21"))  # Standard: 21 Transaktionen

# Scheduler-Intervalle (in Sekunden)
WALLET_INFO_UPDATE_INTERVAL = int(os.getenv("WALLET_INFO_UPDATE_INTERVAL", "86400"))  # Standard: 86400 Sekunden (24 Stunden)
WALLET_BALANCE_NOTIFICATION_INTERVAL = int(os.getenv("WALLET_BALANCE_NOTIFICATION_INTERVAL", "86400"))  # Standard: 86400 Sekunden (24 Stunden)
PAYMENTS_FETCH_INTERVAL = int(os.getenv("PAYMENTS_FETCH_INTERVAL", "60"))  # Standard: 60 Sekunden (1 Minute)

# Flask-Server-Konfiguration
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")  # Standard: localhost
APP_PORT = int(os.getenv("APP_PORT", "5009"))  # Standard: Port 5009

# Dateipfade
PROCESSED_PAYMENTS_FILE = os.getenv("PROCESSED_PAYMENTS_FILE", "processed_payments.txt")
CURRENT_BALANCE_FILE = os.getenv("CURRENT_BALANCE_FILE", "current-balance.txt")
DONATIONS_FILE = os.getenv("DONATIONS_FILE", "donations.json")

# Spenden-Konfiguration
DONATIONS_URL = os.getenv("DONATIONS_URL")  # Optional; kein Standardwert

# Informations-URL-Konfiguration
INFORMATION_URL = os.getenv("INFORMATION_URL")  # Neue Umgebungsvariable

# Schimpfwortfilter-Konfiguration
FORBIDDEN_WORDS_FILE = os.getenv("FORBIDDEN_WORDS_FILE", "forbidden_words.txt")

# Validierung wesentlicher Umgebungsvariablen (ausschließlich Overwatch und DONATIONS_URL ausschließend)
required_vars = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "CHAT_ID": CHAT_ID,
    "LNBITS_READONLY_API_KEY": LNBITS_READONLY_API_KEY,
    "LNBITS_URL": LNBITS_URL
}

missing_vars = [var for var, value in required_vars.items() if not value]
if missing_vars:
    raise EnvironmentError(f"Erforderliche Umgebungsvariablen fehlen: {', '.join(missing_vars)}")

# Initialisieren des Telegram-Bots
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# --------------------- Logging-Konfiguration ---------------------
logger = logging.getLogger("lnbits_logger")
logger.setLevel(logging.DEBUG)

# Dateihandler für detaillierte Logs
file_handler = RotatingFileHandler("app.log", maxBytes=5 * 1024 * 1024, backupCount=3)
file_handler.setLevel(logging.DEBUG)

# Console-Handler für allgemeine Informationen
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)

# Log-Format
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Hinzufügen der Handler zum Logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# --------------------- Hilfsfunktionen ---------------------

def load_forbidden_words(file_path):
    """
    Laden von Schimpfworten aus einer angegebenen Datei in eine Menge.
    
    Args:
        file_path (str): Pfad zur Schimpfwort-Datei.
        
    Returns:
        set: Eine Menge, die alle Schimpfwörter enthält.
    """
    forbidden = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                word = line.strip()
                if word:  # Leere Zeilen vermeiden
                    forbidden.add(word.lower())
        logger.debug(f"Geladene Schimpfwörter aus {file_path}: {forbidden}")
    except FileNotFoundError:
        logger.error(f"Schimpfwort-Datei nicht gefunden unter {file_path}.")
    except Exception as e:
        logger.error(f"Fehler beim Laden der Schimpfwörter aus {file_path}: {e}")
        logger.debug(traceback.format_exc())
    return forbidden

def sanitize_memo(memo, forbidden_words):
    """
    Sanitieren des Memo-Feldes, indem Schimpfwörter durch Sternchen ersetzt werden.
    
    Args:
        memo (str): Der ursprüngliche Memo-Text.
        forbidden_words (set): Eine Menge von Schimpfwörtern.
        
    Returns:
        str: Der sanitierte Memo-Text.
    """
    if not memo:
        return "Kein Memo"
    
    # Funktion zum Ersetzen des gefundenen Wortes durch Sternchen
    def replace_match(match):
        word = match.group()
        return '*' * len(word)
    
    # Erstellen eines Regex-Musters, das alle Schimpfwörter erkennt
    if not forbidden_words:
        return memo  # Keine Schimpfwörter zum Sanitieren
    
    pattern = re.compile(r'\b(' + '|'.join(map(re.escape, forbidden_words)) + r')\b', re.IGNORECASE)
    sanitized_memo = pattern.sub(replace_match, memo)
    logger.debug(f"Sanitierter Memo: Original: '{memo}' -> Sanitisiert: '{sanitized_memo}'")
    return sanitized_memo

def load_processed_payments():
    """
    Laden der bereits verarbeiteten Zahlungs-Hashes aus der Tracking-Datei in eine Menge.
    """
    processed = set()
    if os.path.exists(PROCESSED_PAYMENTS_FILE):
        try:
            with open(PROCESSED_PAYMENTS_FILE, 'r') as f:
                for line in f:
                    processed.add(line.strip())
            logger.debug(f"{len(processed)} verarbeitete Zahlungs-Hashes geladen.")
        except Exception as e:
            logger.error(f"Fehler beim Laden der verarbeiteten Zahlungen: {e}")
            logger.debug(traceback.format_exc())
    return processed

def add_processed_payment(payment_hash):
    """
    Hinzufügen eines verarbeiteten Zahlungs-Hashes zur Tracking-Datei.
    """
    try:
        with open(PROCESSED_PAYMENTS_FILE, 'a') as f:
            f.write(f"{payment_hash}\n")
        logger.debug(f"Zahlungs-Hash {payment_hash} zu verarbeiteten Zahlungen hinzugefügt.")
    except Exception as e:
        logger.error(f"Fehler beim Hinzufügen der verarbeiteten Zahlung: {e}")
        logger.debug(traceback.format_exc())

def load_last_balance():
    """
    Laden des letzten bekannten Guthabens aus der Guthaben-Datei.
    """
    if not os.path.exists(CURRENT_BALANCE_FILE):
        logger.info("Guthaben-Datei existiert nicht. Initialisierung mit aktuellem Guthaben.")
        return None
    try:
        with open(CURRENT_BALANCE_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                logger.warning("Guthaben-Datei ist leer. Setze letztes Guthaben auf 0.")
                return 0.0
            try:
                balance = float(content)
                logger.debug(f"Letztes Guthaben geladen: {balance} sats.")
                return balance
            except ValueError:
                logger.error(f"Ungültiger Guthabenswert in Datei: {content}. Setze letztes Guthaben auf 0.")
                return 0.0
    except Exception as e:
        logger.error(f"Fehler beim Laden des letzten Guthabens: {e}")
        logger.debug(traceback.format_exc())
        return 0.0

def save_current_balance(balance):
    """
    Speichern des aktuellen Guthabens in der Guthaben-Datei.
    """
    try:
        with open(CURRENT_BALANCE_FILE, 'w') as f:
            f.write(f"{balance}\n")
        logger.debug(f"Aktuelles Guthaben {balance} sats erfolgreich gespeichert.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern des aktuellen Guthabens: {e}")
        logger.debug(traceback.format_exc())

def load_donations():
    """
    Laden der Spenden aus der Spenden-Datei in die Spendenliste und Setzen der Gesamtspenden.
    """
    global donations, total_donations
    if os.path.exists(DONATIONS_FILE):
        try:
            with open(DONATIONS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                donations = data.get("donations", [])
                total_donations = data.get("total_donations", 0)
            logger.debug(f"{len(donations)} Spenden aus der Datei geladen.")
        except Exception as e:
            logger.error(f"Fehler beim Laden der Spenden: {e}")
            logger.debug(traceback.format_exc())

def save_donations():
    """
    Speichern der Spenden in der Spenden-Datei.
    """
    try:
        with open(DONATIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "total_donations": total_donations,
                "donations": donations
            }, f, ensure_ascii=False, indent=4)
        logger.debug("Spenden-Daten erfolgreich gespeichert.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der Spenden: {e}")
        logger.debug(traceback.format_exc())

# Initialisieren der Menge der verarbeiteten Zahlungen
processed_payments = load_processed_payments()

# Initialisieren der Flask-App
app = Flask(__name__)

# Globale Variablen zur Speicherung der neuesten Daten
latest_balance = {
    "balance_sats": None,
    "last_change": None,
    "memo": None
}

latest_payments = []

# Datenstrukturen für Spenden
donations = []
total_donations = 0

# Globale Variable zur Verfolgung der letzten Aktualisierungszeit
last_update = datetime.utcnow()

# Laden bestehender Spenden beim Start
load_donations()

# Laden der verbotenen Wörter beim Start
FORBIDDEN_WORDS = load_forbidden_words(FORBIDDEN_WORDS_FILE)

# --------------------- Funktionen ---------------------

def fetch_api(endpoint):
    """
    Abrufen von Daten von der LNbits-API.
    """
    url = f"{LNBITS_URL}/api/v1/{endpoint}"
    headers = {"X-Api-Key": LNBITS_READONLY_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            logger.debug(f"Daten von {endpoint} abgerufen: {data}")
            return data
        else:
            logger.error(f"Fehler beim Abrufen von {endpoint}. Status Code: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Fehler beim Abrufen von {endpoint}: {e}")
        logger.debug(traceback.format_exc())
        return None

def fetch_pay_links():
    """
    Abrufen von Pay-Links von der LNbits LNURLp Erweiterungs-API.
    """
    url = f"{LNBITS_URL}/lnurlp/api/v1/links"
    headers = {"X-Api-Key": LNBITS_READONLY_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            logger.debug(f"Pay-Links abgerufen: {data}")
            return data
        else:
            logger.error(f"Fehler beim Abrufen der Pay-Links. Status Code: {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Pay-Links: {e}")
        logger.debug(traceback.format_exc())
        return None

def get_lnurlp_info(lnurlp_id):
    """
    Abrufen von LNURLp-Informationen für eine gegebene lnurlp_id.
    """
    pay_links = fetch_pay_links()
    if pay_links is None:
        logger.error("Kann die Pay-Links nicht abrufen.")
        return None

    for pay_link in pay_links:
        if pay_link.get("id") == lnurlp_id:
            logger.debug(f"Passender Pay-Link gefunden: {pay_link}")
            return pay_link

    logger.error(f"Kein Pay-Link mit der ID {lnurlp_id} gefunden.")
    return None

def fetch_donation_details():
    """
    Abrufen von LNURLp-Informationen und Integration der Lightning Address und LNURL in die Spenden-Details.
    
    Returns:
        dict: Ein Wörterbuch mit Gesamtspenden, Spendenliste, Lightning Address und LNURL.
    """
    lnurlp_info = get_lnurlp_info(LNURLP_ID)
    if lnurlp_info is None:
        logger.error("Kann die LNURLp-Informationen für die Spenden-Details nicht abrufen.")
        return {
            "total_donations": total_donations,
            "donations": donations,
            "lightning_address": "Nicht verfügbar",
            "lnurl": "Nicht verfügbar",
            "highlight_threshold": HIGHLIGHT_THRESHOLD  # Schwellenwert einbeziehen
        }

    # Extrahieren des Benutzernamens und Konstruktion der Lightning Address
    username = lnurlp_info.get('username')  # Anpassen des Schlüssels basierend auf Ihrer LNURLp-Antwort
    if not username:
        username = "Unbekannt"
        logger.warning("Benutzername in den LNURLp-Informationen nicht gefunden.")

    # Konstruktion der vollständigen Lightning Address
    lightning_address = f"{username}@{LNBITS_DOMAIN}"

    # Extrahieren der LNURL
    lnurl = lnurlp_info.get('lnurl', 'Nicht verfügbar')  # Anpassen des Schlüssels basierend auf Ihrer Datenstruktur

    logger.debug(f"Konstruktion der Lightning Address: {lightning_address}")
    logger.debug(f"Abgerufene LNURL: {lnurl}")

    return {
        "total_donations": total_donations,
        "donations": donations,
        "lightning_address": lightning_address,
        "lnurl": lnurl,
        "highlight_threshold": HIGHLIGHT_THRESHOLD  # Schwellenwert einbeziehen
    }

def update_donations_with_details(data):
    """
    Aktualisiert die Spenden-Daten mit zusätzlichen Details wie Lightning Address und LNURL.
    
    Parameters:
        data (dict): Die ursprünglichen Spenden-Daten.
    
    Returns:
        dict: Aktualisierte Spenden-Daten mit zusätzlichen Details.
    """
    donation_details = fetch_donation_details()
    data.update({
        "lightning_address": donation_details.get("lightning_address"),
        "lnurl": donation_details.get("lnurl"),
        "highlight_threshold": donation_details.get("highlight_threshold")  # Schwellenwert hinzufügen
    })
    return data

def updateDonations(data):
    """
    Aktualisieren der Spenden und verwandter UI-Elemente mit neuen Daten.
    
    Diese Funktion wurde erweitert, um Lightning Address und LNURL in die an das Frontend gesendeten Daten einzubeziehen.
    
    Parameters:
        data (dict): Die Daten, die Gesamtspenden und die Spendenliste enthalten.
    """
    # Integration zusätzlicher Spenden-Details
    updated_data = update_donations_with_details(data)
    
    totalDonations = updated_data["total_donations"]
    # Aktualisierung der Gesamtspenden im Frontend
    # Da dies eine Backend-Funktion ist, wird das Frontend aktualisierte Daten über die API abrufen
    # Daher keine direkte DOM-Manipulation hier
    
    # Aktualisierung der neuesten Spende
    if updated_data["donations"]:
        latestDonation = updated_data["donations"][-1]
        # Frontend übernimmt die Aktualisierung der DOM
        sanitized_memo = sanitize_memo(latestDonation["memo"], FORBIDDEN_WORDS)
        logger.info(f'Letzte Spende: {latestDonation["amount"]} sats - "{sanitized_memo}"')
    else:
        logger.info('Letzte Spende: Noch keine Spenden vorhanden.')
    
    # Aktualisierung der Transaktionsdaten
    # Frontend ruft diese über die API ab
    
    # Aktualisierung der Lightning Address und LNURL
    logger.debug(f"Lightning Address: {updated_data.get('lightning_address')}")
    logger.debug(f"LNURL: {updated_data.get('lnurl')}")
    
    # Speichern der aktualisierten Spenden-Daten
    save_donations()

def send_latest_payments():
    """
    Abrufen der neuesten Zahlungen und Senden einer Benachrichtigung via Telegram.
    Zusätzlich werden Zahlungen überprüft, ob sie als Spenden qualifizieren.
    """
    global total_donations, donations, last_update  # Deklarieren globaler Variablen
    logger.info("Abrufen der neuesten Zahlungen...")
    payments = fetch_api("payments")
    if payments is None:
        return

    if not isinstance(payments, list):
        logger.error("Unerwartetes Datenformat für Zahlungen.")
        return

    # Sortieren der Zahlungen nach Erstellungszeit absteigend
    sorted_payments = sorted(payments, key=lambda x: x.get("created_at", ""), reverse=True)
    latest = sorted_payments[:LATEST_TRANSACTIONS_COUNT]  # Die neuesten n Zahlungen abrufen

    if not latest:
        logger.info("Keine Zahlungen gefunden.")
        return

    # Initialisieren von Listen für verschiedene Zahlungstypen
    incoming_payments = []
    outgoing_payments = []
    pending_payments = []
    new_processed_hashes = []

    for payment in latest:
        payment_hash = payment.get("payment_hash")
        if payment_hash in processed_payments:
            continue  # Bereits verarbeitete Zahlungen überspringen

        amount_msat = payment.get("amount", 0)
        memo = payment.get("memo", "Kein Memo")
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

        # Überprüfen auf Spenden via LNURLp ID
        extra_data = payment.get("extra", {})
        lnurlp_id_payment = extra_data.get("link")
        if lnurlp_id_payment == LNURLP_ID:
            # Es ist eine Spende
            donation_memo = extra_data.get("comment", "Kein Memo")
            # Sicherstellen, dass 'extra' ein numerischer Wert in msats ist
            try:
                donation_amount_msat = int(extra_data.get("extra", 0))
                donation_amount_sats = donation_amount_msat / 1000  # Umrechnung msats in sats
            except (ValueError, TypeError):
                donation_amount_sats = amount_sats  # Fallback, falls 'extra' nicht numerisch ist
            donation = {
                "date": datetime.utcnow().isoformat(),
                "memo": donation_memo,
                "amount": donation_amount_sats
            }
            donations.append(donation)
            total_donations += donation_amount_sats
            last_update = datetime.utcnow()
            sanitized_memo = sanitize_memo(donation_amount_sats, FORBIDDEN_WORDS)
            logger.info(f"Neue Spende erkannt: {donation_amount_sats} sats - {donation_memo}")
            updateDonations({
                "total_donations": total_donations,
                "donations": donations
            })  # Aktualisieren der Spenden mit Details

        # Markieren der Zahlung als verarbeitet
        processed_payments.add(payment_hash)
        new_processed_hashes.append(payment_hash)
        add_processed_payment(payment_hash)

    if not incoming_payments and not outgoing_payments and not pending_payments:
        logger.info("Keine neuen Zahlungen zum Benachrichtigen.")
        return

    message_lines = [
        f"⚡ *{INSTANCE_NAME}* - *Letzte Transaktionen* ⚡\n"
    ]

    if incoming_payments:
        message_lines.append("🟢 *Eingehende Zahlungen:*")
        for idx, payment in enumerate(incoming_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Betrag:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if outgoing_payments:
        message_lines.append("🔴 *Ausgehende Zahlungen:*")
        for idx, payment in enumerate(outgoing_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Betrag:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if pending_payments:
        message_lines.append("⏳ *Zahlungen in Bearbeitung:*")
        for payment in pending_payments:
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"   {payment['amount']} sats\n"
                f"   📝 *Memo:* {sanitized_memo}\n"
                f"   📅 *Status:* In Bearbeitung\n"
            )
        message_lines.append("")

    # Hinzufügen des Zeitstempels
    timestamp_text = f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    message_lines.append(timestamp_text)

    full_message = "\n".join(message_lines)

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Senden der Nachricht an Telegram mit der Inline-Tastatur
    try:
        bot.send_message(chat_id=CHAT_ID, text=full_message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        logger.info("Neueste Zahlungen Benachrichtigung erfolgreich an Telegram gesendet.")
        latest_payments.extend(new_processed_hashes)
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der Zahlungen-Nachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def check_balance_change():
    """
    Periodisches Überprüfen des Wallet-Guthabens und Benachrichtigung, wenn es sich über dem Schwellenwert ändert.
    """
    global last_update
    logger.info("Überprüfen der Guthabenänderungen...")
    wallet_info = fetch_api("wallet")
    if wallet_info is None:
        return

    current_balance_msat = wallet_info.get("balance", 0)
    current_balance_sats = current_balance_msat / 1000  # Umrechnung msats in sats

    last_balance = load_last_balance()

    if last_balance is None:
        # Erster Lauf, Initialisieren der Guthaben-Datei
        save_current_balance(current_balance_sats)
        latest_balance["balance_sats"] = current_balance_sats
        latest_balance["last_change"] = "Initiales Guthaben gesetzt."
        latest_balance["memo"] = "N/A"
        logger.info(f"Initiales Guthaben auf {current_balance_sats:.0f} sats gesetzt.")
        return

    change_amount = current_balance_sats - last_balance
    if abs(change_amount) < BALANCE_CHANGE_THRESHOLD:
        logger.info(f"Guthabenänderung ({abs(change_amount):.0f} sats) unter dem Schwellenwert ({BALANCE_CHANGE_THRESHOLD} sats). Keine Benachrichtigung gesendet.")
        return

    direction = "erhöht" if change_amount > 0 else "verringert"
    abs_change = abs(change_amount)

    # Vorbereitung der Telegram-Nachricht mit Markdown-Formatierung
    message = (
        f"⚡ *{INSTANCE_NAME}* - *Guthabenaktualisierung* ⚡\n\n"
        f"🔹 *Vorheriges Guthaben:* `{int(last_balance):,} sats`\n"
        f"🔹 *Änderung:* `{'+' if change_amount > 0 else '-'}{int(abs_change):,} sats`\n"
        f"🔹 *Neues Guthaben:* `{int(current_balance_sats):,} sats`\n\n"
        f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Senden der Nachricht an Telegram mit der Inline-Tastatur
    try:
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        logger.info(f"Guthaben von {last_balance:.0f} auf {current_balance_sats:.0f} sats geändert. Benachrichtigung gesendet.")
        # Aktualisieren der Guthaben-Datei und der neuesten Guthabensdaten
        save_current_balance(current_balance_sats)
        latest_balance["balance_sats"] = current_balance_sats
        latest_balance["last_change"] = f"Guthaben {direction} um {int(abs_change):,} sats."
        latest_balance["memo"] = "N/A"
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der Guthabenänderungsnachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def send_wallet_balance():
    """
    Senden der aktuellen Wallet-Bilanz via Telegram in einem professionellen und klaren Format.
    """
    logger.info("Senden der täglichen Wallet-Bilanzbenachrichtigung...")
    wallet_info = fetch_api("wallet")
    if wallet_info is None:
        return

    current_balance_msat = wallet_info.get("balance", 0)
    current_balance_sats = current_balance_msat / 1000  # Umrechnung msats in sats

    # Abrufen der Zahlungen zur Berechnung von Anzahl und Gesamtsummen
    payments = fetch_api("payments")
    incoming_count = outgoing_count = 0
    incoming_total = outgoing_total = 0
    if payments and isinstance(payments, list):
        for payment in payments:
            amount_msat = payment.get("amount", 0)
            status = payment.get("status", "completed")
            if status.lower() == "pending":
                continue  # Ausschließen von ausstehenden Zahlungen für die tägliche Bilanz
            if amount_msat > 0:
                incoming_count += 1
                incoming_total += amount_msat / 1000
            elif amount_msat < 0:
                outgoing_count += 1
                outgoing_total += abs(amount_msat) / 1000

    # Vorbereitung der Telegram-Nachricht mit Markdown-Formatierung
    message = (
        f"📊 *{INSTANCE_NAME}* - *Tägliche Wallet-Bilanz* 📊\n\n"
        f"🔹 *Aktuelles Guthaben:* `{int(current_balance_sats)} sats`\n"
        f"🔹 *Gesamteinnahmen:* `{int(incoming_total)} sats` über `{incoming_count}` Transaktionen\n"
        f"🔹 *Gesamtausgaben:* `{int(outgoing_total)} sats` über `{outgoing_count}` Transaktionen\n\n"
        f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Senden der Nachricht an Telegram mit der Inline-Tastatur
    try:
        bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
        logger.info("Tägliche Wallet-Bilanzbenachrichtigung mit Inline-Tastatur erfolgreich gesendet.")
        # Aktualisieren der neuesten Guthabensdaten
        latest_balance["balance_sats"] = current_balance_sats
        latest_balance["last_change"] = "Täglicher Bilanzbericht."
        latest_balance["memo"] = "N/A"
        # Speichern des aktuellen Guthabens
        save_current_balance(current_balance_sats)
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der täglichen Wallet-Bilanznachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_transactions_command(chat_id):
    """
    Behandeln des /transactions Befehls, der vom Benutzer gesendet wurde.
    """
    logger.info(f"Behandle /transactions Befehl für chat_id: {chat_id}")
    payments = fetch_api("payments")
    if payments is None:
        bot.send_message(chat_id=chat_id, text="Fehler beim Abrufen der Transaktionen.")
        return

    if not isinstance(payments, list):
        bot.send_message(chat_id=chat_id, text="Unerwartetes Datenformat für Transaktionen.")
        return

    # Sortieren der Transaktionen nach Erstellungszeit absteigend
    sorted_payments = sorted(payments, key=lambda x: x.get("created_at", ""), reverse=True)
    latest = sorted_payments[:LATEST_TRANSACTIONS_COUNT]  # Die neuesten n Transaktionen abrufen

    if not latest:
        bot.send_message(chat_id=chat_id, text="Keine Transaktionen gefunden.")
        return

    # Initialisieren von Listen für verschiedene Transaktionstypen
    incoming_payments = []
    outgoing_payments = []
    pending_payments = []

    for payment in latest:
        amount_msat = payment.get("amount", 0)
        memo = payment.get("memo", "Kein Memo")
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
        f"⚡ *{INSTANCE_NAME}* - *Letzte Transaktionen* ⚡\n"
    ]

    if incoming_payments:
        message_lines.append("🟢 *Eingehende Zahlungen:*")
        for idx, payment in enumerate(incoming_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Betrag:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if outgoing_payments:
        message_lines.append("🔴 *Ausgehende Zahlungen:*")
        for idx, payment in enumerate(outgoing_payments, 1):
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"{idx}. *Betrag:* `{payment['amount']} sats`\n   *Memo:* {sanitized_memo}"
            )
        message_lines.append("")

    if pending_payments:
        message_lines.append("⏳ *Zahlungen in Bearbeitung:*")
        for payment in pending_payments:
            sanitized_memo = sanitize_memo(payment["memo"], FORBIDDEN_WORDS)
            message_lines.append(
                f"   {payment['amount']} sats\n"
                f"   📝 *Memo:* {sanitized_memo}\n"
                f"   📅 *Status:* In Bearbeitung\n"
            )
        message_lines.append("")

    # Hinzufügen des Zeitstempels
    timestamp_text = f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    message_lines.append(timestamp_text)

    full_message = "\n".join(message_lines)

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=full_message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der /transactions Nachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_info_command(chat_id):
    """
    Behandeln des /info Befehls, der vom Benutzer gesendet wurde.
    """
    logger.info(f"Behandle /info Befehl für chat_id: {chat_id}")
    # Vorbereitung der Intervall-Informationen
    interval_info = (
        f"🔔 *Guthabenänderungsschwellenwert:* `{BALANCE_CHANGE_THRESHOLD} sats`\n"
        f"🔔 *Hervorhebungsschwellenwert:* `{HIGHLIGHT_THRESHOLD} sats`\n"
        f"⏲️ *Intervall zur Überwachung der Guthabenänderung:* Alle `{WALLET_INFO_UPDATE_INTERVAL} Sekunden`\n"
        f"📊 *Intervall für tägliche Wallet-Bilanzbenachrichtigungen:* Alle `{WALLET_BALANCE_NOTIFICATION_INTERVAL} Sekunden`\n"
        f"🔄 *Intervall zum Abrufen der neuesten Zahlungen:* Alle `{PAYMENTS_FETCH_INTERVAL} Sekunden`"
    )

    info_message = (
        f"ℹ️ *{INSTANCE_NAME}* - *Information*\n\n"
        f"{interval_info}\n\n"
        f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=info_message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der /info Nachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_balance_command(chat_id):
    """
    Behandeln des /balance Befehls, der vom Benutzer gesendet wurde.
    """
    logger.info(f"Behandle /balance Befehl für chat_id: {chat_id}")
    wallet_info = fetch_api("wallet")
    if wallet_info is None:
        bot.send_message(chat_id=chat_id, text="Fehler beim Abrufen des Wallet-Guthabens.")
        return

    current_balance_msat = wallet_info.get("balance", 0)
    current_balance_sats = current_balance_msat / 1000  # Umrechnung msats in sats

    message = (
        f"📊 *{INSTANCE_NAME}* - *Wallet-Guthaben*\n\n"
        f"🔹 *Aktuelles Guthaben:* `{int(current_balance_sats)} sats`\n\n"
        f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der /balance Nachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def handle_help_command(chat_id):
    """
    Behandeln des /help Befehls, der vom Benutzer gesendet wurde.
    """
    logger.info(f"Behandle /help Befehl für chat_id: {chat_id}")
    help_message = (
        f"ℹ️ *{INSTANCE_NAME}* - *Hilfe*\n\n"
        f"Verfügbare Befehle:\n"
        f"• `/balance` – Zeigt das aktuelle Wallet-Guthaben an.\n"
        f"• `/transactions` – Zeigt die neuesten Transaktionen.\n"
        f"• `/info` – Bietet Informationen über den Monitor und aktuelle Einstellungen.\n"
        f"• `/help` – Zeigt diese Hilfenachricht an.\n\n"
        f"🕒 *Zeitstempel:* {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    keyboard = []
    if DONATIONS_URL:
        keyboard.append([InlineKeyboardButton("🐽 Sparschwein Anzeigen", url=DONATIONS_URL)])
    keyboard.append([InlineKeyboardButton("🧮 Transaktionen Anzeigen", callback_data='view_transactions')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        bot.send_message(chat_id=chat_id, text=help_message, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
    except Exception as telegram_error:
        logger.error(f"Fehler beim Senden der /help Nachricht an Telegram: {telegram_error}")
        logger.debug(traceback.format_exc())

def process_update(update):
    """
    Verarbeiten von eingehenden Updates vom Telegram-Webhook.
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
                    text="Unbekannter Befehl. Verfügbare Befehle: /balance, /transactions, /info, /help"
                )
        elif 'callback_query' in update:
            process_callback_query(update['callback_query'])
        else:
            logger.info("Update enthält keine Nachricht oder callback_query. Ignoriere.")
    except Exception as e:
        logger.error(f"Fehler beim Verarbeiten des Updates: {e}")
        logger.debug(traceback.format_exc())

def process_callback_query(callback_query):
    """
    Verarbeiten von Callback-Abfragen von Inline-Tastaturen in Telegram-Nachrichten.
    """
    try:
        query_id = callback_query['id']
        data = callback_query.get('data', '')
        chat_id = callback_query['from']['id']

        if data == 'view_transactions':
            handle_transactions_command(chat_id)
            bot.answer_callback_query(callback_query_id=query_id, text="Transaktionen werden abgerufen...")
        else:
            bot.answer_callback_query(callback_query_id=query_id, text="Unbekannte Aktion.")
    except Exception as e:
        logger.error(f"Fehler beim Verarbeiten der Callback-Abfrage: {e}")
        logger.debug(traceback.format_exc())

def start_scheduler():
    """
    Starten des Schedulers für periodische Aufgaben mit BackgroundScheduler.
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
        logger.info(f"Guthabenänderungsüberwachung alle {WALLET_INFO_UPDATE_INTERVAL} Sekunden geplant.")
    else:
        logger.info("Guthabenänderungsüberwachung ist deaktiviert (WALLET_INFO_UPDATE_INTERVAL auf 0 gesetzt).")

    if WALLET_BALANCE_NOTIFICATION_INTERVAL > 0:
        scheduler.add_job(
            send_wallet_balance,
            'interval',
            seconds=WALLET_BALANCE_NOTIFICATION_INTERVAL,
            id='wallet_balance_notification',
            next_run_time=datetime.utcnow() + timedelta(seconds=1)
        )
        logger.info(f"Tägliche Wallet-Bilanzbenachrichtigung alle {WALLET_BALANCE_NOTIFICATION_INTERVAL} Sekunden geplant.")
    else:
        logger.info("Tägliche Wallet-Bilanzbenachrichtigung ist deaktiviert (WALLET_BALANCE_NOTIFICATION_INTERVAL auf 0 gesetzt).")

    if PAYMENTS_FETCH_INTERVAL > 0:
        scheduler.add_job(
            send_latest_payments,
            'interval',
            seconds=PAYMENTS_FETCH_INTERVAL,
            id='latest_payments_fetch',
            next_run_time=datetime.utcnow() + timedelta(seconds=1)
        )
        logger.info(f"Neueste Zahlungen Abruf alle {PAYMENTS_FETCH_INTERVAL} Sekunden geplant.")
    else:
        logger.info("Abrufen der neuesten Zahlungen ist deaktiviert (PAYMENTS_FETCH_INTERVAL auf 0 gesetzt).")

    scheduler.start()
    logger.info("Scheduler erfolgreich gestartet.")

# --------------------- Flask-Routen ---------------------

@app.route('/')
def home():
    return "🔍 LNbits Monitor läuft."

@app.route('/status', methods=['GET'])
def status():
    """
    Gibt den Status der Anwendung zurück, einschließlich des neuesten Guthabens, Zahlungen, Gesamtspenden, Spenden, Lightning Address und LNURL.
    """
    donation_details = fetch_donation_details()
    return jsonify({
        "latest_balance": latest_balance,
        "latest_payments": latest_payments,
        "total_donations": donation_details["total_donations"],
        "donations": donation_details["donations"],
        "lightning_address": donation_details["lightning_address"],
        "lnurl": donation_details["lnurl"],
        "highlight_threshold": donation_details["highlight_threshold"]  # Schwellenwert einbeziehen
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.get_json()
    if not update:
        logger.warning("Leeres Update erhalten.")
        return "No update found", 400

    logger.debug(f"Update empfangen: {update}")

    # Verarbeiten der Nachricht in einem separaten Thread, um Blockierungen zu vermeiden
    threading.Thread(target=process_update, args=(update,)).start()

    return "OK", 200

@app.route('/donations')
def donations_page():
    # Abrufen der LNURLp-Informationen
    lnurlp_id = LNURLP_ID
    lnurlp_info = get_lnurlp_info(lnurlp_id)
    if lnurlp_info is None:
        return "Fehler beim Abrufen der LNURLp-Informationen", 500

    # Extrahieren der notwendigen Informationen
    wallet_name = lnurlp_info.get('description', 'Unbekanntes Wallet')
    lightning_address = lnurlp_info.get('lightning_address', 'Unbekannte Lightning-Adresse')  # Anpassen des Schlüssels basierend auf Ihrer Datenstruktur
    lnurl = lnurlp_info.get('lnurl', 'Nicht verfügbar')  # Anpassen des Schlüssels basierend auf Ihrer Datenstruktur

    # Generieren eines QR-Codes aus der LNURL
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(lnurl)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # Konvertieren des PIL-Bildes in einen Base64-String, um es in HTML einzubetten
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    img_base64 = base64.b64encode(img_io.getvalue()).decode()

    # Berechnen der Gesamtspenden für diese LNURLp
    total_donations_current = sum(donation['amount'] for donation in donations)

    # Übergeben der Spendenliste und zusätzlicher Details an die Vorlage zur Anzeige einzelner Transaktionen
    return render_template(
        'donations.html',
        wallet_name=wallet_name,
        lightning_address=lightning_address,
        lnurl=lnurl,
        qr_code_data=img_base64,
        donations_url=DONATIONS_URL,  # Übergeben der Spenden-URL an die Vorlage
        information_url=INFORMATION_URL,  # Übergeben der Informations-URL an die Vorlage
        total_donations=total_donations_current,  # Übergeben der Gesamtspenden
        donations=donations,  # Übergeben der Spendenliste
        highlight_threshold=HIGHLIGHT_THRESHOLD  # Übergeben des Hervorhebungsschwellenwerts
    )

# API-Endpunkt zur Bereitstellung der Spenden-Daten
@app.route('/api/donations', methods=['GET'])
def get_donations_data():
    """
    Stellt die Spenden-Daten als JSON für das Frontend bereit, einschließlich Lightning Address, LNURL und Hervorhebungsschwellenwert.
    """
    try:
        donation_details = fetch_donation_details()
        data = {
            "total_donations": donation_details["total_donations"],
            "donations": donation_details["donations"],
            "lightning_address": donation_details["lightning_address"],
            "lnurl": donation_details["lnurl"],
            "highlight_threshold": donation_details["highlight_threshold"]  # Schwellenwert einbeziehen
        }
        logger.debug(f"Spenden-Daten mit Details serviert: {data}")
        return jsonify(data), 200
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Spenden-Daten: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({"error": "Fehler beim Abrufen der Spenden-Daten"}), 500

# Endpunkt für Long-Polling-Updates
@app.route('/donations_updates', methods=['GET'])
def donations_updates():
    """
    Endpunkt für Clients, um den Zeitstempel des letzten Spenden-Updates zu überprüfen.
    """
    global last_update
    try:
        return jsonify({"last_update": last_update.isoformat()}), 200
    except Exception as e:
        logger.error(f"Fehler beim Abrufen des letzten Updates: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({"error": "Fehler beim Abrufen des letzten Updates"}), 500

# --------------------- Anwendungseinstiegspunkt ---------------------

if __name__ == "__main__":
    logger.info("🚀 Starte Taschengeld Balance Monitor.")

    # Protokollieren der aktuellen Konfiguration
    logger.info(f"🔔 Benachrichtigungsschwellenwert: {BALANCE_CHANGE_THRESHOLD} sats")
    logger.info(f"🔔 Hervorhebungsschwellenwert: {HIGHLIGHT_THRESHOLD} sats")
    logger.info(f"📊 Abrufen der neuesten {LATEST_TRANSACTIONS_COUNT} Transaktionen für Benachrichtigungen")
    logger.info(f"⏲️ Scheduler Intervalle - Guthabenänderungsüberwachung: {WALLET_INFO_UPDATE_INTERVAL} Sekunden, Tägliche Wallet-Bilanzbenachrichtigung: {WALLET_BALANCE_NOTIFICATION_INTERVAL} Sekunden, Abrufen der neuesten Zahlungen: {PAYMENTS_FETCH_INTERVAL} Sekunden")

    # Starten des Schedulers in einem separaten Thread
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()

    # Starten der Flask-App
    logger.info(f"Flask-Server läuft auf {APP_HOST}:{APP_PORT}")
    app.run(host=APP_HOST, port=APP_PORT)
