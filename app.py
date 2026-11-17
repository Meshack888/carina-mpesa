import os
import base64
from datetime import datetime

import requests
from flask import Flask, request, jsonify
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# Check server status
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "message": "Carina Fashions M-Pesa server is running"
    })
# Safaricom sandbox
BASE_URL = "https://sandbox.safaricom.co.ke"

CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
PASSKEY = os.getenv("MPESA_PASSKEY")
SHORTCODE = os.getenv("MPESA_SHORTCODE")
CALLBACK_URL = os.getenv("MPESA_CALLBACK_URL")


# 1. Get M-Pesa access token
def get_access_token():

    url = BASE_URL + "/oauth/v1/generate"

    response = requests.get(
        url,
        params={"grant_type": "client_credentials"},
        auth=HTTPBasicAuth(
            CONSUMER_KEY,
            CONSUMER_SECRET
        ),
        timeout=30
    )

    response.raise_for_status()

    return response.json()["access_token"]


# 2. Convert phone number to 254 format
def format_phone(phone):

    phone = phone.strip().replace(" ", "")

    if phone.startswith("+254"):
        phone = phone[1:]

    elif phone.startswith("0"):
        phone = "254" + phone[1:]

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

    token = get_access_token()

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    password_string = SHORTCODE + PASSKEY + timestamp

    password = base64.b64encode(
        password_string.encode()
    ).decode()

    url = BASE_URL + "/mpesa/stkpush/v1/processrequest"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": CALLBACK_URL,
        "AccountReference": str(order_id)[:12],
        "TransactionDesc": "Carina Fashion Payment"
    }

    response = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    return response.json()


# 4. Customer payment endpoint
@app.route("/pay", methods=["POST"])
def pay():

    data = request.get_json(silent=True) or {}
    print("RECEIVED DATA:", data)
    try:
        phone = format_phone(str(data.get("phone", "")))

        amount = int(data.get("amount", 0))

        order_id = str(data.get("order_id", "")).strip()

        if amount < 1 or not order_id:
            return jsonify({
                "success": False,
                "message": "Enter a valid amount and order ID"
            }), 400

        result = stk_push(phone, amount, order_id)

        if result.get("ResponseCode") != "0":
            return jsonify({
                "success": False,
                "message": result.get("ResponseDescription")
            }), 400

        return jsonify({
            "success": True,
            "message": "Payment prompt sent. Check your phone and enter your M-Pesa PIN.",
            "checkout_id": result.get("CheckoutRequestID")
        })

    except (ValueError, requests.RequestException) as error:

        return jsonify({
            "success": False,
            "message": str(error)
        }), 400


# 5. Receive Safaricom payment callback
@app.route("/callback", methods=["POST"])
def mpesa_callback():

    callback_data = request.get_json(silent=True) or {}

    print("M-PESA CALLBACK:")
    print(callback_data)

    stk_callback = (
        callback_data
        .get("Body", {})
        .get("stkCallback", {})
    )

    result_code = stk_callback.get("ResultCode")

    if result_code == 0:

        metadata = stk_callback.get(
            "CallbackMetadata", {}
        ).get("Item", [])

        payment_details = {
            item["Name"]: item.get("Value")
            for item in metadata
        }

        receipt = payment_details.get(
            "MpesaReceiptNumber"
        )

        print("PAYMENT SUCCESSFUL")
        print("Receipt:", receipt)

        # TODO:
        # Verify and update the matching order
        # in your database as PAID.

    else:

        print("PAYMENT FAILED OR CANCELLED")
        print(stk_callback.get("ResultDesc"))

    return jsonify({
        "ResultCode": 0,
        "ResultDesc": "Accepted"
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
