import os
import base64
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

ALLOWED_BASE_URLS = {
    "https://sandbox.safaricom.co.ke",
    "https://api.safaricom.co.ke",
}
KENYA_TIMEZONE = timezone(timedelta(hours=3))


class MpesaOutcomeUncertainError(Exception):
    pass


def payment_configuration(require_order_callback=False):
    base_url = os.getenv("MPESA_BASE_URL", "https://sandbox.safaricom.co.ke").rstrip("/")
    consumer_key = os.getenv("MPESA_CONSUMER_KEY", "").strip()
    consumer_secret = os.getenv("MPESA_CONSUMER_SECRET", "").strip()
    passkey = os.getenv("MPESA_PASSKEY", "").strip()
    shortcode = os.getenv("MPESA_SHORTCODE", "").strip()
    callback_url = os.getenv("MPESA_CALLBACK_URL", "").strip()
    order_callback_url = os.getenv("ORDER_CALLBACK_URL", "").strip()
    callback = urlparse(callback_url)
    order_callback = urlparse(order_callback_url)

    if base_url not in ALLOWED_BASE_URLS:
        raise ValueError("M-Pesa environment must use the official Daraja sandbox or production URL.")
    if not all((consumer_key, consumer_secret, passkey, shortcode)):
        raise ValueError("M-Pesa credentials are not configured.")
    if (callback.scheme != "https" or not callback.hostname or callback.path.rstrip("/") != "/callback" or
            callback.username or callback.password or callback.query or callback.fragment):
        raise ValueError("MPESA_CALLBACK_URL must be a public HTTPS URL ending in /callback.")
    if require_order_callback or order_callback_url:
        valid_order_callback_path = any(
            order_callback.path.rstrip("/").endswith(path)
            for path in ("/api/mpesa/callback", "/api/payments/daraja/callback")
        )
        if (order_callback.scheme != "https" or not order_callback.hostname or
                not valid_order_callback_path or
                order_callback.username or order_callback.password or order_callback.query or order_callback.fragment):
            raise ValueError("ORDER_CALLBACK_URL must be a public HTTPS PHP M-Pesa callback URL.")

    return base_url, consumer_key, consumer_secret, passkey, shortcode, callback_url, order_callback_url


def valid_service_authorization():
    expected_key = os.getenv("MPESA_SERVICE_API_KEY", "").strip()
    authorization = request.headers.get("Authorization", "")
    supplied_key = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    return len(expected_key) >= 32 and hmac.compare_digest(supplied_key, expected_key)


# 1. Get M-Pesa access token
def get_access_token():
    base_url, consumer_key, consumer_secret, *_ = payment_configuration()
    url = base_url + "/oauth/v1/generate"

    response = requests.get(
        url,
        params={"grant_type": "client_credentials"},
        auth=HTTPBasicAuth(
            consumer_key,
            consumer_secret
        ),
        timeout=15
    )

    response.raise_for_status()
    token_response = response.json()
    access_token = token_response.get("access_token") if isinstance(token_response, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("Daraja did not return an access token.")
    return access_token


# 2. Convert phone number to 254 format
def format_phone(phone):
    phone = "".join(character for character in phone.strip() if character.isdigit() or character == "+")

    if phone.startswith("+254"):
        phone = phone[1:]

    elif phone.startswith("0"):
        phone = "254" + phone[1:]
    elif len(phone) == 9:
        phone = "254" + phone

    if not (
        len(phone) == 12
        and phone.startswith("254")
        and phone[3] in "17"
        and phone.isdigit()
    ):
        raise ValueError("Invalid Kenyan phone number")

    return phone


# 3. Initiate STK Push
def stk_push(phone, amount, order_id):
    base_url, _, _, passkey, shortcode, callback_url, _ = payment_configuration()
    token = get_access_token()
    timestamp = datetime.now(KENYA_TIMEZONE).strftime("%Y%m%d%H%M%S")
    password_string = shortcode + passkey + timestamp

    password = base64.b64encode(
        password_string.encode()
    ).decode()

    url = base_url + "/mpesa/stkpush/v1/processrequest"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": str(order_id)[:12],
        "TransactionDesc": "Carina Fashion Payment"
    }

    try:
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=25
        )
    except requests.RequestException as error:
        raise MpesaOutcomeUncertainError from error

    if response.status_code >= 500:
        raise MpesaOutcomeUncertainError
    response.raise_for_status()
    try:
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        raise MpesaOutcomeUncertainError from error
    if not isinstance(result, dict):
        raise MpesaOutcomeUncertainError
    return result


# 4. Customer payment endpoint
@app.route("/pay", methods=["POST"])
def pay():
    if not os.getenv("MPESA_SERVICE_API_KEY", "").strip():
        return jsonify({"success": False, "message": "Payment service is not configured.", "uncertain": False}), 503
    if not valid_service_authorization():
        return jsonify({"success": False, "message": "Unauthorized payment request."}), 401

    data = request.get_json(silent=True) or {}

    try:
        if not isinstance(data, dict):
            raise ValueError("Payment request must be a JSON object.")
        phone = format_phone(str(data.get("phone", "")))
        raw_amount = data.get("amount")
        if isinstance(raw_amount, bool) or not isinstance(raw_amount, int):
            raise ValueError("Amount must be a whole number of Kenyan shillings.")
        amount = raw_amount
        order_id = str(data.get("order_id", "")).strip()
        if amount < 1 or not order_id.isdigit() or len(order_id) > 20:
            raise ValueError("Enter a valid amount and order ID.")

        result = stk_push(phone, amount, order_id)

        if result.get("ResponseCode") != "0":
            return jsonify({
                "success": False,
                "message": "Safaricom rejected the payment request."
            }), 400
        checkout_id = result.get("CheckoutRequestID")
        merchant_id = result.get("MerchantRequestID")
        if not isinstance(checkout_id, str) or not checkout_id or not isinstance(merchant_id, str) or not merchant_id:
            return jsonify({
                "success": False,
                "message": "Safaricom accepted the request but payment references were missing.",
                "uncertain": True
            }), 502

        return jsonify({
            "success": True,
            "message": "Payment prompt sent. Check your phone and enter your M-Pesa PIN.",
            "checkout_id": checkout_id,
            "merchant_request_id": merchant_id
        })

    except MpesaOutcomeUncertainError:
        app.logger.warning("Daraja STK request outcome is uncertain.")
        return jsonify({"success": False, "message": "Safaricom payment status could not be confirmed.", "uncertain": True}), 502
    except ValueError as error:
        return jsonify({
            "success": False,
            "message": str(error)
        }), 400
    except requests.RequestException as error:
        response = getattr(error, "response", None)
        if response is not None and 400 <= response.status_code < 500:
            app.logger.warning("Daraja rejected an STK request (HTTP %s).", response.status_code)
            return jsonify({"success": False, "message": "Safaricom rejected the payment request.", "uncertain": False}), 400
        app.logger.warning("Daraja authentication request failed: %s", type(error).__name__)
        return jsonify({"success": False, "message": "M-Pesa authentication could not be completed.", "uncertain": False}), 502


# 5. Receive Safaricom payment callback
@app.route("/callback", methods=["POST"])
def mpesa_callback():
    callback_data = request.get_json(silent=True) or {}
    if not isinstance(callback_data, dict):
        return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback payload."}), 400
    callback_body = callback_data.get("Body")
    if not isinstance(callback_body, dict):
        return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback payload."}), 400
    stk_callback = callback_body.get("stkCallback")
    if not isinstance(stk_callback, dict) or not stk_callback.get("CheckoutRequestID"):
        return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback payload."}), 400

    try:
        _, _, _, _, _, _, order_callback_url = payment_configuration(require_order_callback=True)
        response = requests.post(order_callback_url, json=callback_data, timeout=20)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ResultCode") != 0:
            app.logger.error("PHP payment callback did not accept the M-Pesa result (HTTP %s).", response.status_code)
            return jsonify({"ResultCode": 1, "ResultDesc": "Order system did not accept the callback."}), 503
    except (requests.RequestException, ValueError) as error:
        app.logger.error("Could not forward the M-Pesa callback: %s", type(error).__name__)
        return jsonify({"ResultCode": 1, "ResultDesc": "Callback forwarding is temporarily unavailable."}), 503

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "carina-mpesa"})


if __name__ == "__main__":
    app.run(debug=False, port=int(os.getenv("PORT", "5000")))
