from __future__ import annotations


PHONE = "+254700000111"
SESSION = "session-001"


def ussd(client, text: str, *, phone: str = PHONE, session: str = SESSION):
    return client.post(
        "/webhooks/ussd",
        json={"sessionId": session, "serviceCode": "*456#", "phoneNumber": phone, "text": text},
    )


def signup(client, *, phone: str = PHONE, consent: str = "1", name: str = "Samira Njeri", email: str = "samira@example.com"):
    texts = ["", "1", f"1*{name}", f"1*{name}*{email}", f"1*{name}*{email}*{consent}"]
    return [ussd(client, text, phone=phone) for text in texts]

