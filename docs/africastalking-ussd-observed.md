# Africa's Talking USSD — Observed Behaviour (M23 U1 discovery)

Recorded on 2026-11-08 against the **sandbox** with a shared-code channel (`*384*83579#`) and
AT's web simulator. A scratch responder logged every request. Sanitised captures are in
`tests/fixtures/africastalking/ussd/`; phone numbers are replaced with `+254700000000`.

Official pages read: USSD overview, Handle Sessions, Notifications
(developers.africastalking.com/docs/ussd/…; plain HTTP clients get 403 there, so use a browser).

## 1. The request AT sends

Every step of a session is an HTTP **POST** to the channel's callback URL:

- `Content-Type: application/x-www-form-urlencoded`
- `User-Agent: at-ussd-api/1.0`
- Form fields:

| Field | Observed |
|---|---|
| `sessionId` | `ATUid_` + 32 hex characters, the same for every step of a session |
| `serviceCode` | `*384*83579#` |
| `phoneNumber` | `+254…` (E.164) |
| `networkCode` | `99999` in the sandbox |
| `text` | `""` on the dial, then every answer joined with `*`: `1`, `1*hello` |

- The `at-ussd-hop-metadata` header we send back is returned on the next request (`step-0`,
  `step-1`). The docs show it in AT's session details; it must not carry anything sensitive.
- The requests came from `35.177.236.113`; through ngrok that address is in `X-Forwarded-For`. AT
  does not sign USSD requests and does not publish its addresses (not on the pages read). So the
  callback is protected by a secret path token, and an IP allowlist can only be an extra, optional
  check.
- FastAPI needs `python-multipart` to read this form; without it, `request.form()` fails.

## 2. The reply

`text/plain`, starting `CON ` (the session continues) or `END ` (the session ends). The docs add:

- a 4xx or a reply without `CON`/`END` ends the session;
- the reply must come within **10 seconds**;
- avoid special characters in menus, which telcos may not render.

**Length.** A 323-character `CON` reply was shown in full by the web simulator, so the simulator
does not apply a handset limit. Real networks commonly cut a USSD screen at around 160 to 182
characters. Vekro keeps every screen at **160 characters or fewer** (tested), whatever the
simulator shows.

## 3. End of session

With an events callback set, AT posts an end-of-interaction notification (`sessionId`,
`serviceCode`, `networkCode`, `phoneNumber`, `status` = Incomplete / Success / Failed,
`durationInMillis`, …; from the docs, not observed: the events callback was not set).

## 4. The simulator's country selector

The web simulator's phone field defaults to **Afghanistan (+93)**. A Kenyan number typed without
`+254` becomes a +93 number, the dial never reaches a Kenyan `*384#` channel, and the simulator
shows only "You have reached Africa's Talking USSD Services". AT's session log stays empty. Type
the number with `+254` (the flag switches to Kenya). The sandbox simulator lets anyone type any
number, so the caller's number is only as trustworthy as the simulator; on a live network it is
asserted by the operator.

## 5. SMS in the sandbox (observed 2026-11-09)

- The sandbox (`AFRICASTALKING_USERNAME=sandbox`) never delivers to a real phone. Messages appear
  only in the web simulator: connect a phone number (typed with `+254`), then open the speech-bubble
  icon on the simulator's home screen. Only messages sent while that number is connected show up.
- A sender ID that is not registered for the username is refused for the whole request: HTTP 201
  with `{"SMSMessageData": {"Message": "InvalidSenderId", "Recipients": []}}`
  (`tests/fixtures/africastalking/send_invalid_sender_id.json`). Since then the sender treats this
  as a definite failure and logs `africastalking_request_rejected reason=InvalidSenderId`. For the
  sandbox, leave `AFRICASTALKING_SENDER_ID` blank.
- Without a sender ID the same request succeeds: `statusCode: 101`, `status: "Success"`, sender
  shown in the simulator as `AFRICASTKNG`.

