"""Telegram Stars receipts and resumable redemption of limited-use vouchers."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery


async def extend_once(db, uid: int, receipt: str, days: int):
    """One document update atomically grants days and records the event identity."""
    await db.users.update_one(
        {"_id": uid}, {"$setOnInsert": {"created_at": datetime.now(UTC)}}, upsert=True
    )
    await db.users.update_one(
        {"_id": uid, "credits": {"$ne": receipt}},
        [
            {
                "$set": {
                    "premium_until": {
                        "$add": [
                            {
                                "$max": [
                                    "$$NOW",
                                    {"$ifNull": ["$premium_until", "$$NOW"]},
                                ]
                            },
                            days * 86400000,
                        ]
                    },
                    "credits": {
                        "$concatArrays": [{"$ifNull": ["$credits", []]}, [receipt]]
                    },
                }
            }
        ],
    )


def build_router(settings, store):
    router = Router(name="billing")
    db = store.db

    @router.message(Command("terms", "paysupport"))
    async def support(m: Message):
        contact = (
            escape(settings.support_url)
            if settings.support_url
            else "the bot administrator"
        )
        await m.answer(
            f"Purchases grant the premium period shown on the invoice. "
            f"Contact {contact} with your Telegram Stars receipt for payment help."
        )

    @router.message(Command("premium", "buy"))
    async def buy(m: Message):
        if m.chat.type != "private" or not m.from_user:
            return await m.answer("Open this command in a private chat with me.")
        await store.touch_user(m.from_user)
        user = await db.users.find_one({"_id": m.from_user.id})
        if user and user.get("banned"):
            return await m.answer("Access is disabled.")
        if (
            user
            and user.get("premium_until")
            and user["premium_until"] > datetime.now(UTC)
        ):
            await m.answer(
                "✦ Premium is active until "
                + user["premium_until"].date().isoformat()
                + " UTC."
            )
        if not settings.premium_stars:
            return await m.answer(
                "Use /redeem CODE to activate a voucher. Paid plans are currently closed."
            )
        token = secrets.token_urlsafe(18)
        await db.invoices.insert_one(
            {
                "_id": token,
                "uid": m.from_user.id,
                "price": settings.premium_stars,
                "days": settings.premium_days,
                "valid_until": datetime.now(UTC) + timedelta(hours=1),
            }
        )
        await m.answer_invoice(
            title=f"{settings.bot_name[:18]} Premium",
            description=f"{settings.premium_days} days of premium access. See /terms.",
            payload=token,
            currency="XTR",
            provider_token="",
            prices=[LabeledPrice(label="Premium", amount=settings.premium_stars)],
        )

    @router.pre_checkout_query()
    async def checkout(q: PreCheckoutQuery):
        invoice = await db.invoices.find_one(
            {
                "_id": q.invoice_payload,
                "uid": q.from_user.id,
                "price": q.total_amount,
                "valid_until": {"$gt": datetime.now(UTC)},
                "charge": {"$exists": False},
            }
        )
        banned = await db.users.find_one(
            {"_id": q.from_user.id, "banned": True}, {"_id": 1}
        )
        ok = bool(invoice and q.currency == "XTR" and not banned)
        await q.answer(
            ok=ok,
            error_message=None
            if ok
            else "This invoice is unavailable. Request another with /buy.",
        )

    @router.message(F.successful_payment)
    async def paid(m: Message):
        payment = m.successful_payment
        if not m.from_user or payment.currency != "XTR":
            return
        charge = payment.telegram_payment_charge_id
        invoice = await db.invoices.find_one(
            {
                "_id": payment.invoice_payload,
                "uid": m.from_user.id,
                "price": payment.total_amount,
            }
        )
        if not invoice:
            await db.payment_review.update_one(
                {"_id": charge},
                {
                    "$setOnInsert": {
                        "uid": m.from_user.id,
                        "amount": payment.total_amount,
                        "payload": payment.invoice_payload,
                        "received_at": datetime.now(UTC),
                    }
                },
                upsert=True,
            )
            return await m.answer(
                "Your payment requires review. Send your receipt to /paysupport."
            )
        await extend_once(db, m.from_user.id, "stars:" + charge, invoice["days"])
        await db.invoices.update_one(
            {"_id": invoice["_id"]},
            {"$set": {"charge": charge, "paid_at": datetime.now(UTC)}},
        )
        await m.answer(
            f"✦ Payment received. Your account was credited with {invoice['days']} premium days."
        )

    @router.message(Command("redeem"))
    async def redeem(m: Message):
        if not m.from_user or m.chat.type != "private":
            return await m.answer("Open a private chat with me to redeem a code.")
        parts = (m.text or "").split()
        if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[1]):
            return await m.answer("Usage: /redeem CODE")
        uid = m.from_user.id
        if await db.users.find_one({"_id": uid, "banned": True}, {"_id": 1}):
            return await m.answer("Access is disabled.")
        key = hashlib.sha256(parts[1].upper().encode()).hexdigest()
        # Keep the claim and remaining capacity on one document, also on standalone MongoDB.
        await db.redeem_codes.update_one(
            {
                "_id": key,
                "claimants": {"$ne": uid},
                "$or": [
                    {"expires_at": None},
                    {"expires_at": {"$gt": datetime.now(UTC)}},
                ],
                "$expr": {"$lt": [{"$ifNull": ["$uses", 0]}, "$max_uses"]},
            },
            {"$addToSet": {"claimants": uid}, "$inc": {"uses": 1}},
        )
        coupon = await db.redeem_codes.find_one(
            {"_id": key, "claimants": uid}, {"days": 1}
        )
        if not coupon:
            return await m.answer("Code unavailable or expired.")
        await extend_once(db, uid, "code:" + key, coupon["days"])
        await m.answer(
            "✦ Your voucher has been credited. See /premium for the expiry date."
        )

    return router
