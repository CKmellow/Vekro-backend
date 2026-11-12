# Continue with Google (#324)

One `/login` for every role. Google is an extra way in, never a replacement for the phone.

## Flow

OpenID Connect, authorization-code flow, with PKCE, state and nonce (`app/services/google_auth.py`).

1. `GET /auth/google/start` (the button on /login) stores a single-use **state** (as a hash, for 10
   minutes), a **nonce** and a **PKCE verifier**. It sets the state in an HttpOnly, SameSite=Lax
   cookie, then redirects to Google with the state, the nonce and the S256 **code challenge**.
2. Google redirects to **`{FRONTEND_URL}/api/auth/google/callback`**. That is
   `https://app.vekro.me/api/auth/google/callback` live and
   `http://localhost:5173/api/auth/google/callback` locally (both registered in the Google client).
3. The callback requires the state to equal the cookie and an unused, unexpired request; it is used
   up at once (replays and forgeries fail). It exchanges the code with the verifier, then verifies
   the ID token itself: RS256 against Google's JWKS, `iss`, `aud` (and `azp`), `exp`, `iat`,
   `nonce`, and `email_verified`.
4. Only `google_sub` and the verified e-mail (for display) are stored. Google's access and ID tokens
   are discarded.

## Account rules

| Case | What happens |
|---|---|
| `google_sub` linked to an account | Signed in (any role). An admin gets here only after linking it themselves. |
| Unknown Google account | A pending **member sign-up** (cookie, 30 minutes): name and phone, the phone verified by SMS code (`/register/google`); the account (a member with no store; selling starts later from the buyer area) is created only then, so nobody transacts unverified. It has no password (`has_password=false`) until "Forgot password" sets one. A phone that already has an account, a member's or an admin's, is refused with "This number already has a Vekro account. Sign in with password or SMS code, then link Google in Profile & security." Google never links or creates anything for it (M28, 2026-11-12). |
| Same e-mail as an existing account | **Never linked or signed in.** Matching is by `google_sub` only. |
| Buyer, seller or admin wants Google | Link from settings while signed in: `POST /account/google/link` (CSRF), then the same callback. A Google account linked elsewhere is refused (`taken`). |
| Unlink | `DELETE /account/google`; refused while Google is the only way in (`password_needed`). |
| Consent screen in Testing, account not a test user | Google returns `access_denied`; /login explains that Google sign-in is open to invited test accounts only. |

The callback redirects with `?google=<code>`: `signed_in`, `linked`, `denied`, `state`, `token`,
`id_token`, `taken`, `inactive`, `not_configured`.

## Settings

`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (both in `.env` / `deploy/.env`; off while either is
blank), optional `GOOGLE_REDIRECT_URI`. Tests: `tests/test_google_auth.py`.
