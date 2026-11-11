# USSD (M23)

Buyers and sellers on feature phones act on their orders by dialling a USSD code. The menu
(`app/services/ussd.py`) calls the same service functions as the web app, so USSD follows the same
state machine, timeouts and anchoring. What Africa's Talking actually sends is recorded in
`docs/africastalking-ussd-observed.md`.

## Setup

1. In the Africa's Talking dashboard (the **Sandbox** app for the demo): USSD → Create Channel →
   a shared code such as `*384*83579#`.
2. Callback URL: `https://<host>/ussd/africastalking/<USSD_CALLBACK_TOKEN>`. Locally `<host>` is the
   ngrok static domain (`make tunnel`); on the server it is the `api.` hostname.
3. `.env`: `USSD_CALLBACK_TOKEN` (a long random value; blank turns USSD off). Optionally
   `USSD_ALLOWED_IPS`, enforced only behind the Cloudflare tunnel with
   `USSD_CLIENT_IP_HEADER=cf-connecting-ip` (#289). Elsewhere (ngrok, Caddy) it stays off.
4. Users set a 4-digit USSD PIN with `POST /account/ussd-pin` (it needs their password). For the
   demo the seed sets it (below).

## Testing in the simulator: type +254

Open the simulator from the dashboard ("Launch Simulator"). **Type the phone number with `+254`**
(for example `+254711000123`). The country selector defaults to Afghanistan (+93). A number typed
without `+254` becomes a +93 number, and the dial silently fails with "You have reached Africa's
Talking USSD Services". Nothing reaches Vekro, and AT's session log stays empty. With `+254` the
flag switches to Kenya. Then open the phone app and dial the channel's code.

## Demo data

```bash
.venv/bin/python -m app.cli seed-ussd-demo --buyer-phone +2547XXXXXXXX --seller-phone +2547YYYYYYYY --pin 1234
```

It creates (or reuses) one buyer and one seller on those numbers, sets their USSD PIN, and moves
five new orders through the real services:

| Order state | Who acts | Over USSD |
|---|---|---|
| waiting for payment | buyer | 2 Pay for an order → reference → PIN → M-Pesa prompt |
| paid | seller | 1 My orders → order → 1 Mark dispatched → PIN |
| on the way | seller | 1 My orders → order → 1 Mark arrived → PIN |
| at the door (delivery code printed) | buyer | 1 My orders → order → 1 Accept → code, or 2 Reject → reason → PIN |
| serialized, 24-hour hold | buyer | 1 My orders → order → 1 Report a problem → category → PIN |

It prints the references, the at-the-door delivery code and, for accounts it created, a web
password. It refuses to run with `ENVIRONMENT=production`.

## Menu tree

```
Buyer                                         Seller
Vekro                                         Vekro seller
1 My orders  -> up to 3 orders, the ones      1 My orders -> up to 3 orders, the ones
                that need you first                          that need you first
   At the door: 1 Accept (code)                  Paid:          1 Mark dispatched (PIN)
                2 Reject -> reason (PIN)         On the way:    1 Mark arrived (PIN)
   24h check:   1 Report a problem               Return on the way:
                  -> category (PIN)                             1 I received the return (PIN)
   Problem reported: 1 I've sent it back (PIN)
   Is it fixed?: 1 Yes / 2 No (PIN)
2 Pay for an order -> reference -> PIN
0 Exit                                        0 Exit
```

Every screen is plain English, numbered, at most 160 characters (tested): handsets cut longer
screens, although the web simulator does not. A refusal from the services becomes a short reason,
for example "VK-7K3P9Q can't do that now. It is: On the way."

## Security and its limits

- **Unsigned requests.** Africa's Talking does not sign USSD callbacks. The secret token in the
  URL is the gate (compared in constant time), and anything else gets 404. `USSD_ALLOWED_IPS` is
  an optional extra; AT does not publish its addresses (discovery saw `35.177.236.113`), so it is
  off by default. When on, the caller is read **only** from `CF-Connecting-IP` (Cloudflare's edge
  overwrites any value a caller sends), and only for requests that reached the API from a private
  or loopback hop (cloudflared on the VM, or the web proxy). `X-Forwarded-For` is never trusted:
  a caller can put any address first (#289).
- **Who is calling.** The caller is identified by the `phoneNumber` AT sends. On a live network
  the operator asserts it. **In the sandbox simulator anyone can type any number**, so a demo
  caller is only as real as the number typed. That is why sensitive actions also need a USSD PIN,
  and accepting at the door needs the order's delivery code (sent by SMS to the buyer only).
- **PIN.** 4 digits, stored hashed like passwords. Three wrong PINs lock USSD actions for 15
  minutes, with a lockout separate from the web login so it cannot be used to lock someone out of
  the app.
- **Stale menus.** Each order's menu depends on its status. If the order changed after its menu
  was shown (on the web or in another session), the action is refused ("just changed, dial
  again") rather than letting the same digit do something else.
- **The 10-second limit.** Only database work happens while AT waits. The M-Pesa prompt for "Pay
  for an order" is sent after the reply.
- Swahili menus are future work.
