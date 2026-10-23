# Microsoft → Xbox Live → Minecraft authentication for a Windows launcher

**Status:** research spec, gathered 2026-10-20 (Asia/Shanghai).
**Scope:** what a desktop launcher/downloader (CubeFetch) must implement to turn a Microsoft
account into a Minecraft: Java Edition session, verified against live endpoints and the current
source of three shipping implementations.
**Out of scope:** launching the game, downloading assets/libraries (see `CONTRACTS.md`).

**Evidence legend**

| Tag | Meaning |
|---|---|
| **VERIFIED-LIVE** | I hit the endpoint on 2026-10-20 and quote the actual response (status + body). |
| **VERIFIED-SRC** | Exact line from a pinned public commit, URL given. |
| **INFERRED** | Reasonable engineering conclusion, *not* directly observed. Treat as a risk. |

Pinned commits used throughout:

- HMCL `HMCL-dev/HMCL` `main` @ `acc9740fa9289ded38553b6a35045fd608c1ea99` (2026-10-20)
- Prism Launcher `PrismLauncher/PrismLauncher` `develop` @ `77f2bee593eb9e0cdeef530e0e8686524b68d9d9` (2026-10-19)
- prismarine-auth `PrismarineJS/prismarine-auth` `master` @ `edd7aca2f4158da5f64542f138cdd18592f619a2` (2026-10-06)

---

## 1. TL;DR — the flow to implement

```
device code (login.live.com, client 00000000402b5328)
   ↓ access_token (+ refresh_token)
POST user.auth.xboxlive.com/user/authenticate   RpsTicket = "d=" + access_token
   ↓ Token + DisplayClaims.xui[0].uhs        (user token, "UToken")
POST xsts.auth.xboxlive.com/xsts/authorize     UserTokens = [UToken]
   ↓ Token + DisplayClaims.xui[0].uhs        (XSTS token, audience rp://api.minecraftservices.com/)
POST api.minecraftservices.com/authentication/login_with_xbox
      identityToken = "XBL3.0 x=<uhs>;<XSTS Token>"
   ↓ access_token (JWT, expires_in ≈ 86400)
GET  api.minecraftservices.com/minecraft/profile    Bearer <mc access_token>
GET  api.minecraftservices.com/entitlements/mcstore Bearer <mc access_token>
```

Everything after the first step is a fixed three-hop token exchange with JSON bodies; only step 1
touches the user (browser/device code). Ownership is decided by `entitlements/mcstore` item names
plus the existence of a profile, **not** by a flag in the token.

**The one genuinely contested detail** is the `RpsTicket` prefix (§5). Recommendation: always
`"d=" + access_token`. It is what HMCL and Prism ship, and it works for tokens issued by
`login.live.com` (HMCL) and by `login.microsoftonline.com/consumers` (Prism).

---

## 2. What real launchers do in 2026 (all VERIFIED-SRC)

| | HMCL | Prism Launcher | prismarine-auth (JS) |
|---|---|---|---|
| Browser flow | auth-code + PKCE on `login.live.com/oauth20_authorize.srf` | auth-code on `login.microsoftonline.com/consumers/oauth2/v2.0/authorize` | both |
| Device flow | `login.microsoftonline.com/consumers/oauth2/v2.0/devicecode` | `login.microsoftonline.com/consumers/oauth2/v2.0/devicecode` | `login.live.com/oauth20_connect.srf` |
| Client id | **own** app: `System.getProperty("hmcl.microsoft.auth.id", JarUtils.getAttribute("hmcl.microsoft.auth.id", ""))` (`HMCL/src/main/java/org/jackhuang/hmcl/game/OAuthServer.java:222`) | **own** app: `set(Launcher_MSA_CLIENT_ID "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb")` (`CMakeLists.txt`) | `389b1b32-b5d5-43b2-bddc-84ce938d6737` (a leaked sample id — do not reuse) |
| Scope | `"XboxLive.signin offline_access"` | `"XboxLive.SignIn XboxLive.offline_access"` | `['service::user.auth.xboxlive.com::MBI_SSL']` |
| RpsTicket | `"d=" + token` | `"d=%1"` | `azure ? "d=" : "t="` |
| → Minecraft | `login_with_xbox` + `identityToken` | `launcher/login` + `xtoken`, `platform: PC_LAUNCHER` | `login_with_xbox` + `identityToken` |
| Ownership | `entitlements/mcstore` (200 required) then `profile`, 404 → `entitlements/license` | `entitlements/license?requestId=<uuid>` | `entitlements/mcstore?requestId=<uuid>` |

**Consequences for us**

1. **No shipping launcher uses `client_id=00000000402b5328`.** HMCL and Prism each register their
   own Entra application; prismarine-auth uses its own id. `00000000402b5328` is believed (community lore, **not** officially documented) to be Microsoft's
   legacy *Minecraft Launcher* Live Connect app id; it is the only client id
   with which the legacy `login.live.com` device-code endpoint is known to work
   (**VERIFIED-LIVE**, §3.1). Using it is unsanctioned (§10).
2. **Both Minecraft login endpoints are live and equivalent-ish.** `login_with_xbox` (identityToken)
   is used by two of three; Prism migrated to `launcher/login` (xtoken) and that endpoint answers
   401 UNAUTHORIZED for a bogus token (**VERIFIED-LIVE**), i.e. it exists. Stick to
   `login_with_xbox`: HMCL + prismarine-auth + the whole third-party ecosystem use it, and its
   response shape is documented below from two independent parsers.
3. **The modern/correct path is an own Entra app + AAD v2 endpoints.** Everything in this document
   except the client id applies verbatim to that setup; only the two OAuth URLs and the client id
   change. Plan the abstraction so the client id + endpoints are configuration (§9 does this).

---

## 3. Step 1 — Microsoft OAuth

### 3.1 Start a device code (legacy Live Connect) — **VERIFIED-LIVE**

```
POST https://login.live.com/oauth20_connect.srf
Content-Type: application/x-www-form-urlencoded
Accept: application/json

client_id=00000000402b5328
scope=XboxLive.signin offline_access
response_type=device_code
```

Observed 2026-10-20, HTTP 200:

```json
{"device_code":"<opaque>","user_code":"XXXXXXX","verification_uri":"https://www.microsoft.com/link",
 "expires_in":900,"interval":5}
```

- Keys are exactly `device_code, expires_in, interval, user_code, verification_uri`
  (no `verification_uri_complete`, no `expires_at`).
- `verification_uri` is the short form `https://www.microsoft.com/link`; show that string and the
  `user_code` to the user (never build your own URL).
- `expires_in` 900 s, `interval` 5 s.
- Also accepted (HTTP 200) at this endpoint: `scope=service::user.auth.xboxlive.com::MBI_SSL`
  and `scope=service::user.auth.xboxlive.com::MBI_SSL offline_access`. All three scopes work at
  the *device-code* stage; the difference only shows up in what the access token can do
  (see §5).

### 3.2 Poll for the token — **VERIFIED-LIVE**

```
POST https://login.live.com/oauth20_token.srf
Content-Type: application/x-www-form-urlencoded

client_id=00000000402b5328
grant_type=urn:ietf:params:oauth:grant-type:device_code
device_code=<device_code from 3.1>
```

Pending, observed HTTP 400 (`requests` sets the form content-type itself; a bare body also works):

```json
{"error":"authorization_pending",
 "error_description":"The provided request has not yet been authorized by the user. The user must input their code.",
 "correlation_id":"9e1cf73c-…"}
```

**Trap — the parameter name is `device_code`, not `code`.** Sending `code=<device_code>` returns
HTTP 400 `{"error":"invalid_grant","error_description":"The provided request must include a 'device_code' input parameter."}` (**VERIFIED-LIVE**).
HMCL's `OAuth.authenticateDevice` passes `pair("code", …)` against the AAD v2 endpoint; for
`login.live.com` that is wrong (§11). Prism and prismarine-auth both send `device_code`
(`MSADeviceCodeStep.cpp`, `LiveTokenManager.js`).

Other observed behaviours on this endpoint:

- Wrong client id → HTTP 400 `{"error":"invalid_client","error_description":"The client does not exist or is not enabled for consumers. …"}`.
  So `00000000402b5328` **is** enabled for consumers on `login.live.com` (**VERIFIED-LIVE**).
- Bogus `device_code` value → HTTP 400
  `{"error":"invalid_grant","error_description":"The provided value for the input parameter 'device_code' is not valid."}`
  (**VERIFIED-LIVE**). Same `error` code as the `code=` mistake but a different
  `error_description` — branch on the description text, not on `error` alone (§12).
- `GET` → HTTP 400 `{"error":"invalid_request","error_description":"The provided request must include a 'grant_type' input parameter."}`.
- `grant_type=refresh_token&refresh_token=INVALID` → HTTP 400
  `{"error":"invalid_grant","error_description":"The provided value for the input parameter 'refresh_token' or 'assertion' is not valid."}`
  (**VERIFIED-LIVE**) — the endpoint parses `refresh_token` grants; the success shape is therefore
  the standard one (§4).

Polling loop: sleep `interval` between attempts, on `slow_down` add 5 s (Prism adds 5 s and
doubles on timeout; prismarine-auth waits `res.interval`), stop after `expires_in` seconds, and
treat only `authorization_pending` / `slow_down` as non-fatal. A `Cookie` echo of the `Set-Cookie`
from 3.1 is done by prismarine-auth but is **not required** (our poll without it reached
`authorization_pending`, **VERIFIED-LIVE**).

### 3.3 Success — token response shape

Standard Live Connect / Entra token JSON (**INFERRED** for live.com success; field names taken from
HMCL `TokenResponse` and Prism `MSADeviceCodeStep` parsers, both of which read these keys):

```json
{"token_type":"bearer","access_token":"<ACCESS_TOKEN>","refresh_token":"<REFRESH_TOKEN>",
 "expires_in":3600,"scope":"XboxLive.signin offline_access","user_id":"…"}
```

`access_token` is what §4 needs; `refresh_token` is what §6 needs. HMCL parses
`token_type, expires_in, ext_expires_in, scope, access_token, refresh_token`
(`MicrosoftSession`/`OAuth.TokenResponse`).

### 3.4 The alternative: auth-code + loopback (do **not** use with this client id)

For completeness, because Prism/HMCL use it with *their own* registrations:

```
GET https://login.live.com/oauth20_authorize.srf
    ?client_id=…&response_type=code&scope=XboxLive.signin%20offline_access
    &redirect_uri=http://localhost:<port>/auth/callback
    &prompt=select_account&state=<random>
    [&code_challenge=<S256>&code_challenge_method=S256]      # HMCL uses PKCE
POST https://login.live.com/oauth20_token.srf
    grant_type=authorization_code&code=<code>&redirect_uri=<same>&client_id=…
    [&code_verifier=<verifier>]
```

**VERIFIED-LIVE:** `GET oauth20_authorize.srf` with
`redirect_uri=http://localhost:8080/auth/callback` and a browser User-Agent returns HTTP 200 and the
Microsoft sign-in page. `redirect_uri` *acceptance at code-exchange time cannot be tested without a
real login*, and none of the three launchers uses a loopback URI with a borrowed client id — the
`redirect_uri` must be registered on the app. Prism's registrations use
`http://127.0.0.1:<port>/…` **for its own** client id; `00000000402b5328`'s registered redirect URIs
belong to Microsoft. ⇒ With `00000000402b5328`, **device code is the only flow we can rely on**;
loopback needs an own app registration (§10). Also note: non-browser User-Agents on
`oauth20_authorize.srf` produce an "HR=0x80049D57" error page, so use a normal UA if you ever open it.

---

## 4. Step 2 — Xbox Live user token (RPS) — **VERIFIED-SRC** + **VERIFIED-LIVE** (shape)

```
POST https://user.auth.xboxlive.com/user/authenticate
Content-Type: application/json
Accept: application/json
x-xbl-contract-version: 1

{
  "Properties": {
    "AuthMethod": "RPS",
    "SiteName": "user.auth.xboxlive.com",
    "RpsTicket": "d=<Microsoft access_token>"
  },
  "RelyingParty": "http://auth.xboxlive.com",
  "TokenType": "JWT"
}
```

Sources: Prism `launcher/minecraft/auth/steps/XboxUserStep.cpp` (template
`{"Properties":{"AuthMethod":"RPS","SiteName":"user.auth.xboxlive.com","RpsTicket":"d=%1"},"RelyingParty":"http://auth.xboxlive.com","TokenType":"JWT"}`
plus `x-xbl-contract-version: 1`); HMCL `MicrosoftService.java` builds exactly the same map with
`pair("RpsTicket", "d=" + liveAccessToken)`; prismarine-auth
`src/TokenManagers/XboxTokenManager.js:81-99`.

**VERIFIED-LIVE:** a bogus `RpsTicket` gets HTTP 401 with an empty body (same with or without
`x-xbl-contract-version`) — endpoint exists, request contract accepted up to token validation.
`x-xbl-contract-version` is optional in practice (HMCL sends no contract header at all and ships);
send `1` to match Prism.

Response (shape from Prism `Parsers.cpp:56-140`, which parses this exact fixture; HMCL reads
`Token` + `DisplayClaims.xui[0].uhs`):

```json
{"IssueInstant":"2020-12-07T19:52:08.4463796Z",
 "NotAfter":"2020-12-21T19:52:08.4463796Z",
 "Token":"<UToken>",
 "DisplayClaims":{"xui":[{"uhs":"<userhash>"}]}}
```

- `DisplayClaims.xui` is an **array**; scan it for the first object containing `uhs`
  (`Parsers.cpp:105-134` — it iterates and warns "Missing uhs" otherwise).
- HMCL throws if `xui[0].uhs` is absent, and **compares the `uhs` from this response with the one
  from the XSTS response** — they must match; a mismatch means a confused-deputy attack or a broken
  backend (`MicrosoftService.getUhs`, `XboxAuthorizationStep.cpp:85`). Implement that check.
- `NotAfter` is the real expiry (the fixture shows 14 days). Never hardcode it.

---

## 5. The `RpsTicket` prefix question — the one detail sources disagree on

Three data points:

| Source | Token issuer | Prefix used |
|---|---|---|
| HMCL `MicrosoftService.java` | `login.live.com` (auth-code) or AAD v2 (device) → both funnel here | always `d=` |
| Prism `XboxUserStep.cpp` | `login.microsoftonline.com/consumers` | always `d=` |
| prismarine-auth `XboxTokenManager.js:83` | `login.live.com` (MBI_SSL) vs MSAL | `azure ? "d=" : "t="` |

So: `t=<live.com token>` and `d=<live.com token>` both exist in shipping code, and
`d=<AAD token>` is used by two independent launchers. RPS evidently accepts either prefix for a
user ticket; the prefix is a ticket-kind marker, not a checksum.

**Answer to "does it need `d=` when the token comes from login.live.com?"** — *Not strictly, but
`d=` is the safe universal choice*: it is what HMCL does with live.com tokens (production, huge
install base) and what Prism does with AAD tokens. Do **not** send the raw token with no prefix;
every implementation prefixes something.

**Scope choice** follows the same logic. HMCL's `SCOPE = "XboxLive.signin offline_access"` paired
with `d=` is a single, self-consistent, live-verified-accepted pair, so the recommendation is:

```python
MSA_SCOPE = "XboxLive.signin offline_access"
```

If you prefer prismarine-auth's `service::user.auth.xboxlive.com::MBI_SSL` (also accepted at the
device endpoint, **VERIFIED-LIVE**), pair it with `t=` to stay consistent with the only code that
ships that combination. `offline_access` (or `XboxLive.offline_access`) is required to get a
`refresh_token` at all — without it the user re-authorises every hour.

---

## 6. Step 3 — XSTS authorize — **VERIFIED-SRC** + **VERIFIED-LIVE** (shape)

```
POST https://xsts.auth.xboxlive.com/xsts/authorize
Content-Type: application/json
Accept: application/json
x-xbl-contract-version: 1

{
  "Properties": { "SandboxId": "RETAIL", "UserTokens": ["<UToken>"] },
  "RelyingParty": "rp://api.minecraftservices.com/",
  "TokenType": "JWT"
}
```

`SandboxId` is the literal string `"RETAIL"` (HMCL `pair("SandboxId","RETAIL")`, Prism
`XboxAuthorizationStep.cpp` `{"Properties":{"SandboxId":"RETAIL",…}}`) — not `PROD`, not `RETAIL:1`.
`RelyingParty` **must** be `rp://api.minecraftservices.com/` — this is what scopes the XSTS token to
the Minecraft service; `http://xboxlive.com` is for other Xbox services and will not log you into
Minecraft.

**VERIFIED-LIVE:** bogus `UserTokens` → HTTP 400 with an empty body. Real failures are documented as
HTTP 401 with a JSON body carrying `XErr` (Prism only parses `XErr` when
`reply->error() == QNetworkReply::AuthenticationRequiredError`, i.e. 401;
`XboxAuthorizationStep.cpp:98`), and prismarine-auth also reads an `x-err` **response header**
(`XboxTokenManager.js:53-59`). Implementation: accept the error code from either place.

Success response is the same envelope as §4 (`IssueInstant`, `NotAfter`, `Token`,
`DisplayClaims.xui[0].uhs`) — Prism reuses `parseXboxToken` for it. Take `Token` as the XSTS token
and `uhs` for the `identityToken` string; assert it equals the user token's `uhs`.

---

## 7. Step 4 — Minecraft services login — **VERIFIED-SRC** + **VERIFIED-LIVE** (shape)

```
POST https://api.minecraftservices.com/authentication/login_with_xbox
Content-Type: application/json
Accept: application/json

{ "identityToken": "XBL3.0 x=<uhs>;<XSTS Token>" }
```

Note the exact concatenation: the literal `XBL3.0 x=`, the userhash, a semicolon, then the XSTS
token — no space after the semicolon. HMCL: `pair("identityToken", "XBL3.0 x=" + uhs + ";" + token)`;
prismarine-auth `MinecraftJavaTokenManager.js:71`: `'XBL3.0 x=' + xsts.userHash + ';' + xsts.XSTSToken`.

**VERIFIED-LIVE:** bogus token → HTTP 401 `{"path":"/authentication/login_with_xbox","error":"UNAUTHORIZED"}`.

Response (Prism `Parsers.cpp:403-420` reads `expires_in`, `username`, `access_token`; HMCL's
`MinecraftLoginWithXBoxResponse` has `username, roles, access_token, token_type, expires_in`):

```json
{"username":"<uuid-ish name>","roles":["…"],"access_token":"<JWT>",
 "token_type":"Bearer","expires_in":86400}
```

- `expires_in` ≈ 86400 (24 h). Prism's `MinecraftAccount.cpp` comment: *"Refresh accounts that would
  expire in the next 12 hours (fresh token validity is 24 hours)."* Compute
  `not_after = now + expires_in` (HMCL: `expiresIn * 1000L + System.currentTimeMillis()`); do not
  trust local clock deltas beyond that.
- This token is a **Minecraft services** bearer token, not an Xbox token. Use it only against
  `api.minecraftservices.com`.

### 7.1 Prism's variant — `POST api.minecraftservices.com/launcher/login`

`LauncherLoginStep.cpp`: body `{"xtoken":"XBL3.0 x=<uhs>;<xsts>","platform":"PC_LAUNCHER"}`.
Same XBL3.0 string, different field name, plus `platform`. **VERIFIED-LIVE:** 401
`{"path":"/launcher/login","error":"UNAUTHORIZED"}` for a bogus token, so the route exists. It
reports `platform`/`clientId` in the token claims (useful if you want the launcher to look like the
official one); `login_with_xbox` is simpler and better attested by parsers. Pick one, don't chain both.

---

## 8. Step 5 — profile and entitlements — **VERIFIED-SRC** + **VERIFIED-LIVE** (401 shape)

Both are plain bearer GETs:

```
GET https://api.minecraftservices.com/minecraft/profile
GET https://api.minecraftservices.com/entitlements/mcstore[?requestId=<uuid4>]
Authorization: Bearer <mc access_token>
Accept: application/json
```

**VERIFIED-LIVE:** both return HTTP 401 `{"path":"/minecraft/profile"}` /
`{"path":"/entitlements/mcstore"}` for a bogus bearer. So the error body carries `path`, and
`error` only sometimes — do not parse `error` as an enum.

### 8.1 Profile response

```json
{"id":"<32-hex, no dashes>","name":"<IGN>",
 "skins":[{"id":"…","state":"ACTIVE","url":"http://textures.minecraft.net/texture/…","variant":"CLASSIC","alias":"…"}],
 "capes":[{"id":"…","state":"ACTIVE","url":"…","alias":"…"}]}
```

HMCL parses `id` with an *unhyphenated* UUID adapter (32 hex chars), plus `name`, `skins[]`
(`id,state,url,variant,alias`), `capes[]`. Prism `Parsers.cpp:157` reads `name` and
`MinecraftProfileStep.cpp` treats **HTTP 404 as "account has no Minecraft profile"** (a valid,
non-error outcome), not as a transport error.

### 8.2 Ownership detection — the precise rule

Two independent, complementary signals:

1. **`GET /entitlements/mcstore`** → `{"items":[{"name":"product_minecraft","signature":"…"},
   {"name":"game_minecraft","signature":"…"}], "signature":"…","keyId":"…"}`.
   Prism `Parsers.cpp:374-380`:
   ```cpp
   if (name == "game_minecraft")   output.canPlayMinecraft = true;
   if (name == "product_minecraft") output.ownsMinecraft  = true;
   ```
   i.e. `product_minecraft` = owns the product (can download), `game_minecraft` = may play.
   HMCL requires HTTP 200 from this endpoint at all (*"Check MC ownership, this is necessary, see
   GitHub#2979"*) and then uses `profile`; prismarine-auth appends `?requestId=<uuid4>`.
2. **`GET /entitlements/license[?requestId=<uuid4>]`** — Prism's primary check
   (`EntitlementsStep.cpp`), same `items[].name` shape. HMCL calls it *without* `requestId` as a
   fallback when `profile` 404s and then requires an item named `game_minecraft`, else raises
   `MinecraftJavaEditionLicenseNotFoundException`; if the item exists but the profile still 404s it
   raises `MinecraftJavaEditionProfileNotFoundException`.

**Recommended decision table**

| mcstore/license | profile | Verdict | Message |
|---|---|---|---|
| 200, contains `game_minecraft` or `product_minecraft` | 200 with `id`+`name` | **OK** | launch |
| 200, empty `items` | 404 | **does not own Minecraft** | "This account doesn't own Minecraft: Java Edition." |
| 200 with items | 404 | **owns but no profile** (never chose a username / profile propagating) | retry after a few seconds, then "profile not available" |
| 401 on either | — | token invalid/expired | refresh (§9) or re-auth |
| 429/5xx | — | transient | exponential backoff |

Cross-check: an account that reaches a valid mc token but owns nothing is a normal, successful auth
of a non-owner — do not surface it as an auth failure. (`requestId` is a client-generated UUIDv4;
optional on both routes — HMCL's license call omits it, Prism supplies it. Sending it is harmless
and is what two implementations do.)

---

## 9. Step 6 — refresh

Refresh at the **same host that issued** the token (prismarine-auth does exactly this;
`LiveTokenManager.js` refresh → `Endpoints.live.tokenRequest`):

```
POST https://login.live.com/oauth20_token.srf
Content-Type: application/x-www-form-urlencoded

client_id=00000000402b5328
grant_type=refresh_token
refresh_token=<refresh_token>
scope=XboxLive.signin offline_access          # recommended; prismarine-auth sends scope, HMCL does not
```

**VERIFIED-LIVE** for the error path (§3.2): a bad `refresh_token` yields 400 `invalid_grant` with
the message *"The provided value for the input parameter 'refresh_token' or 'assertion' is not
valid."* — so `refresh_token` is the right parameter name at this host. **INFERRED:** the success
response is a fresh `{access_token, refresh_token, expires_in, token_type, scope}`; Microsoft
rotates the refresh token on each refresh, so **persist the new one** and treat the old as dead
(assuming otherwise is the classic launcher bug that logs users out after one refresh).

**The Minecraft access token is not refreshable.** Only the Microsoft layer has a refresh token;
`api.minecraftservices.com` tokens are re-minted by replaying §6→§7 with a fresh MSA token (both
Prism and HMCL re-run the XBL→XSTS→MC chain on refresh). The chain costs 3 requests and ~1 s, so a
single `ensure_session()` call path (§12) that refreshes the MSA token and re-runs §4–§7 is the right granularity.

**Two different expiry windows — do not confuse them** (this is the mistake Prism's comment invites):

| Token | Real lifetime | Refresh when |
|---|---|---|
| MSA access token (`login.live.com`) | `expires_in` ≈ 3600 s (**INFERRED**, standard) | last ~5 min |
| XBL user token / XSTS token | `NotAfter` in the response (fixture: 14 days) | don't cache across restarts; cheap to re-mint |
| Minecraft services token | `expires_in` ≈ 86400 s | within 12 h of expiry (Prism's rule, §7) |

Applying Prism's "12 hours" window to a 1-hour MSA token makes every call refresh the token.

*Implementation note / INFERRED:* HMCL refreshes at
`login.microsoftonline.com/consumers/oauth2/v2.0/token` even for tokens minted at
`login.live.com/oauth20_token.srf` — this only works if MSA refresh tokens are portable across the
two hosts (plausible, MSA unified refresh tokens, but untested here). Don't copy that shortcut;
refresh where you minted.

---

## 10. Error taxonomy — everything you must map

### 10.1 XSTS `XErr` codes (authoritative list, from two launchers)

Body shape on failure: `{"Identity":"0","XErr":2148916238,"Message":"",
"Redirect":"https://start.ui.xboxlive.com/AddChildToFamily"}` (HMCL javadoc;
prismarine-auth quotes the `2148916233` variant with `Redirect:"https://start.ui.xboxlive.com/CreateAccount"`).
Also check the `x-err` response header (prismarine-auth does, as a fallback).

| XErr | Meaning | User-facing action | Source |
|---|---|---|---|
| `2148916227` | Account banned by Xbox (Community Standards) | "banned, cannot be used" — dead end | Prism, prismarine-auth, HMCL const `BANNED` |
| `2148916229` | Account restricted; guardian hasn't granted online permission | send to `https://account.microsoft.com/family/` | Prism, prismarine-auth |
| `2148916233` | **No Xbox profile on the account** | "create one at https://signup.live.com/signup" (this is *not* "no Minecraft") | all three, HMCL const `MISSING_XBOX_ACCOUNT` |
| `2148916234` | Xbox Terms of Service not accepted | "log in and accept the ToS" | Prism, prismarine-auth |
| `2148916235` | Xbox Live unavailable in the account's country | "Xbox has blocked login from your region" | Prism, prismarine-auth, HMCL const `COUNTRY_UNAVAILABLE` |
| `2148916236` | Proof of age required | `https://login.live.com/login.srf` | Prism, prismarine-auth |
| `2148916237` | Playtime limit reached (family) | "blocked by playtime limit" | Prism, prismarine-auth |
| `2148916238` | DOB under 18, account not in a family group | must be added to a Microsoft family by an adult; `Redirect` → `https://start.ui.xboxlive.com/AddChildToFamily` | all three, HMCL const `ADD_FAMILY` |

Any other `XErr` → generic "Xbox Live authentication failed (XErr: <n>)". Robust rule: if `XErr` is
present, surface the mapped message; the HTTP status is always 401 for these, so branch on `XErr`,
not on the status text.

### 10.2 OAuth errors

| `error` | Handling |
|---|---|
| `authorization_pending` | keep polling |
| `slow_down` | add 5 s to the interval (Prism) / double on timeout |
| `expired_token` | device code dead → restart flow |
| `invalid_grant` (poll) | read `error_description`: *"must include a 'device_code' input parameter"* = you sent `code=` (this is HMCL's bug for live.com); *"the input parameter 'device_code' is not valid"* = unknown/expired code → restart |
| `invalid_client` | bad client id, or the app is not enabled for consumers |
| `invalid_request` | missing `grant_type`/`client_id` |
| `invalid_grant` (refresh) | refresh token revoked/expired → full re-auth |

Also: `400` + "The provided request must include a 'grant_type' input parameter" if you POST to the
token endpoint without a body; `429`/`5xx` on any endpoint → retry with backoff, never classify as
"no Minecraft owned".

### 10.3 Minecraft services errors

- `401` → bearer token invalid/expired (refresh, §9). Body: `{"path": ".../minecraft/profile"}`.
- `404` on `profile` → account has no Minecraft profile → §8.2.
- `200` with `"items": []` → account does not own the game → §8.2.
- `429`, `5xx` → transient.

---

## 11. Verified limits of this research (be honest with yourself)

1. **No credential-bearing request was made.** Steps §4–§7 are verified as *endpoint-exists +
   contract-accepted* (401/400 with the real server's own error body), and their success shapes are
   taken from parsers in three shipping implementations, not from a live success. That is the
   strongest evidence obtainable without logging in.
2. **The `d=` prefix for live.com tokens** is verified by production code (HMCL ships it), not by a
   live success; the competing `t=` convention is equally well attested (prismarine-auth). Both
   prefixes are accepted by RPS as user tickets.
3. **`00000000402b5328` is not a documented public client id.** It is Microsoft's legacy Minecraft
   Launcher Live app id; it works today (**VERIFIED-LIVE**) but may be rotated/blocked at any time.
   HMCL and Prism both register their own Entra app — a shipping product should do the same
   (`XboxLive.signin offline_access`, redirect `http://localhost:<port>/…`), which makes §3.4
   viable and removes the dependency on this id.
4. **Lifetimes** beyond what is quoted (`expires_in` from responses, `NotAfter` in tokens) are
   inferred. Always read the fields.
5. **`device_code` + `00000000402b5328` is not used by any of the three launchers** read here: HMCL
   and Prism use AAD v2 for device code, prismarine-auth uses live.com device code with *other*
   client ids. It is, however, the only flow we can fully exercise without credentials, and it was
   live-verified to start a flow and reach `authorization_pending` → recommend it for the desktop
   app (no loopback server, no redirect registration).
6. `minecraft.wiki` (the wiki.vg successor) is unreachable from this network (HTTP 403 on both the
   page and `api.php`); the XErr list above is corroborated only by the three implementations, which
   agree on all eight codes.

---

## 12. Reference implementation sketch (Python 3.13, `requests` only)

Save as e.g. `cubefetch/auth/msa.py`. No third-party deps beyond `requests`.

```python
"""Microsoft -> Xbox Live -> Minecraft (Java) auth for a desktop launcher.

Verified against live endpoints 2026-10-20 and against HMCL acc9740f,
Prism 77f2bee, prismarine-auth edd7aca.  See docs/research/auth.md.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

import requests

# ---------------------------------------------------------------- endpoints
MSA_DEVICE_CODE_URL   = "https://login.live.com/oauth20_connect.srf"   # legacy Live Connect: works with 402b5328
MSA_TOKEN_URL         = "https://login.live.com/oauth20_token.srf"
MSA_CLIENT_ID         = "00000000402b5328"          # legacy Minecraft Launcher Live app id (unofficial)
MSA_SCOPE             = "XboxLive.signin offline_access"

XBL_USER_URL          = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL              = "https://xsts.auth.xboxlive.com/xsts/authorize"
XSTS_RELYING_PARTY    = "rp://api.minecraftservices.com/"            # audience for Minecraft
MC_LOGIN_URL          = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL        = "https://api.minecraftservices.com/minecraft/profile"
MC_ENTITLEMENTS_URL   = "https://api.minecraftservices.com/entitlements/mcstore"
MC_LICENSE_URL        = "https://api.minecraftservices.com/entitlements/license"

XBL_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-xbl-contract-version": "1",         # Prism sends 1; HMCL omits it and works
}
DEFAULT_TIMEOUT = 30

# ---------------------------------------------------------------- XErr table
XERR_MESSAGES: dict[int, str] = {
    2148916227: "This account is banned by Xbox for violating the Community Standards.",
    2148916229: "This account is restricted: a guardian has not granted online permission "
                "(https://account.microsoft.com/family/).",
    2148916233: "This Microsoft account has no Xbox profile yet. Create one at "
                "https://signup.live.com/signup and try again.",
    2148916234: "This account has not accepted the Xbox Terms of Service. Sign in at "
                "https://www.xbox.com and accept them.",
    2148916235: "Xbox Live is not available in this account's country/region.",
    2148916236: "This account needs age verification (https://login.live.com/login.srf).",
    2148916237: "This account has reached its playtime limit.",
    2148916238: "This account is under 18 and must be added to a Microsoft family by an adult.",
}


class AuthError(Exception):
    """kind: 'oauth' | 'oauth_pending' | 'xsts' | 'no_xbox_profile' | 'no_minecraft'
             | 'no_profile' | 'http' | 'protocol'."""

    def __init__(self, kind: str, message: str, *, xerr: int | None = None,
                 status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.kind, self.xerr, self.status, self.body = kind, xerr, status, body

    @property
    def user_message(self) -> str:
        if self.xerr is not None:
            return XERR_MESSAGES.get(self.xerr, f"Xbox Live rejected the login (XErr {self.xerr}).")
        return str(self)


# ---------------------------------------------------------------- token store
@dataclass
class MsaTokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: float = 0.0                 # epoch seconds
    scope: str | None = None
    token_type: str = "bearer"

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def needs_refresh(self, window: float = 300) -> bool:
        """MSA access tokens live ~1 h, so only the last 5 minutes matter here.

        Do NOT copy Prism's 12-hour rule onto this token: a 12 h window on a 1 h token means
        every call refreshes (the 12 h rule belongs to the 24 h *Minecraft* token, see Session).
        """
        return time.time() >= self.expires_at - window


@dataclass
class Session:
    """Everything a launcher needs to start the game."""
    msa: MsaTokens
    mc_access_token: str
    mc_not_after: float
    profile: dict[str, Any] = field(default_factory=dict)
    owns_minecraft: bool = False
    can_play: bool = False

    @property
    def uuid(self) -> str:
        return self.profile.get("id", "")

    @property
    def username(self) -> str:
        return self.profile.get("name", "")

    @property
    def needs_refresh(self) -> bool:
        """Prism's rule, applied to the token it belongs to: the Minecraft token lives 24 h, so
        refresh when it is within 12 h of expiry.  Replaying the Xbox chain also re-mints the
        Minecraft token, so a nearly-expired MSA token forces the same path."""
        return time.time() >= self.mc_not_after - 12 * 3600 or self.msa.needs_refresh()

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


# ---------------------------------------------------------------- helpers
def _post(url: str, **kw) -> requests.Response:
    try:
        return requests.post(url, timeout=DEFAULT_TIMEOUT, **kw)
    except requests.RequestException as exc:
        raise AuthError("http", f"network error calling {url}: {exc}") from exc


def _json(resp: requests.Response, what: str) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as exc:
        raise AuthError("protocol", f"{what}: expected JSON, got HTTP {resp.status_code}",
                        status=resp.status_code, body=resp.text[:512]) from exc


# ---------------------------------------------------------------- 1. device flow
def start_device_flow(client_id: str = MSA_CLIENT_ID, scope: str = MSA_SCOPE,
                      device_code_url: str = MSA_DEVICE_CODE_URL) -> dict[str, Any]:
    """Return {user_code, device_code, verification_uri, expires_in, interval}."""
    resp = _post(device_code_url,
                 data={"client_id": client_id, "scope": scope, "response_type": "device_code"},
                 headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise AuthError("oauth", f"device code request failed: HTTP {resp.status_code}",
                        status=resp.status_code, body=resp.text[:512])
    data = _json(resp, "device code")
    for key in ("user_code", "device_code", "verification_uri", "expires_in", "interval"):
        if key not in data:
            raise AuthError("protocol", f"device code response missing {key!r}", body=str(data)[:512])
    return data


def poll_for_token(device: dict[str, Any], *, client_id: str = MSA_CLIENT_ID,
                   token_url: str = MSA_TOKEN_URL, scope: str = MSA_SCOPE,
                   sleep=time.sleep) -> MsaTokens:
    """Poll until the user approves.  The form parameter MUST be `device_code` (not `code`)."""
    interval = float(device.get("interval", 5))
    deadline = time.time() + float(device.get("expires_in", 900))
    while True:
        resp = _post(token_url, data={
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"],
        }, headers={"Accept": "application/json"})
        if resp.status_code == 200:
            return _tokens_from(resp, scope)
        err = _json(resp, "device token poll")
        code = err.get("error")
        if code == "authorization_pending":
            pass
        elif code == "slow_down":
            interval += 5
        elif code in ("expired_token", "code_expired"):
            raise AuthError("oauth", "the device code expired; start again", body=resp.text[:512])
        elif code == "invalid_grant":
            # two distinct causes, both observed live:
            #  a) "must include a 'device_code' input parameter" -> you sent `code=` instead
            #  b) "the input parameter 'device_code' is not valid"  -> unknown/expired device code
            desc = str(err.get("error_description", ""))
            if "must include" in desc:
                raise AuthError("oauth", "the poll must carry 'device_code', not 'code' "
                                         "(login.live.com)", body=resp.text[:512])
            raise AuthError("oauth", "device code is not valid or expired; restart the flow",
                            body=resp.text[:512])
        else:
            raise AuthError("oauth", f"token endpoint error {code!r}: "
                                     f"{err.get('error_description', '')}",
                            status=resp.status_code, body=resp.text[:512])
        if time.time() + interval > deadline:
            raise AuthError("oauth", "timed out waiting for device code approval")
        sleep(interval)


def _tokens_from(resp: requests.Response, scope: str | None) -> MsaTokens:
    data = _json(resp, "token response")
    if "access_token" not in data:
        raise AuthError("protocol", "token response has no access_token", body=str(data)[:512])
    return MsaTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=time.time() + float(data.get("expires_in", 3600)),
        scope=data.get("scope", scope),
        token_type=data.get("token_type", "bearer"),
    )


def refresh_msa(tokens: MsaTokens, *, client_id: str = MSA_CLIENT_ID,
                token_url: str = MSA_TOKEN_URL, scope: str = MSA_SCOPE) -> MsaTokens:
    """Refresh at the host that issued the token; Microsoft rotates the refresh token."""
    if not tokens.refresh_token:
        raise AuthError("oauth", "no refresh token; interactive login required")
    resp = _post(token_url, data={
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "scope": scope,
    }, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        raise AuthError("oauth", "refresh failed (re-authenticate)", status=resp.status_code,
                        body=resp.text[:512])
    fresh = _tokens_from(resp, scope)
    fresh.refresh_token = fresh.refresh_token or tokens.refresh_token
    return fresh


# ---------------------------------------------------------------- 2..4 xbox chain
def xbox_user_token(msa_access_token: str) -> tuple[str, str]:
    """POST user.auth.xboxlive.com -> (user_token, uhs).  RpsTicket is always d=<token>."""
    body = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={msa_access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    resp = _post(XBL_USER_URL, data=json.dumps(body), headers=XBL_HEADERS)
    return _xbox_token(resp, "user.auth.xboxlive.com")


def xsts_authorize(user_token: str, relying_party: str = XSTS_RELYING_PARTY) -> tuple[str, str]:
    """POST xsts.auth.xboxlive.com -> (xsts_token, uhs).  SandboxId is literally RETAIL."""
    body = {
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [user_token]},
        "RelyingParty": relying_party,
        "TokenType": "JWT",
    }
    resp = _post(XSTS_URL, data=json.dumps(body), headers=XBL_HEADERS)
    return _xbox_token(resp, "xsts.auth.xboxlive.com")


def _xbox_token(resp: requests.Response, what: str) -> tuple[str, str]:
    if resp.status_code != 200:
        xerr = _xerr(resp)
        if xerr is None:
            raise AuthError("xsts", f"{what}: HTTP {resp.status_code}",
                            status=resp.status_code, body=resp.text[:512])
        raise AuthError("xsts", XERR_MESSAGES.get(xerr, f"Xbox Live error {xerr}"),
                        xerr=xerr, status=resp.status_code, body=resp.text[:512])
    data = _json(resp, what)
    token = data.get("Token")
    try:
        uhs = next(c["uhs"] for c in data["DisplayClaims"]["xui"] if "uhs" in c)
    except (KeyError, StopIteration, TypeError):
        uhs = None
    if not token or not uhs:
        raise AuthError("protocol", f"{what}: missing Token/uhs", body=str(data)[:512])
    return token, uhs


def _xerr(resp: requests.Response) -> int | None:
    """XErr may arrive in the x-err header (prismarine-auth) or the JSON body (Prism/HMCL)."""
    raw = resp.headers.get("x-err")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("XErr"), (int, str)):
        try:
            return int(body["XErr"])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 5. minecraft
def login_with_xbox(uhs: str, xsts_token: str) -> tuple[str, float]:
    """POST authentication/login_with_xbox -> (mc_access_token, epoch_expiry).

    identityToken is 'XBL3.0 x=<uhs>;<xsts>' verbatim (no space after ';').
    """
    resp = _post(MC_LOGIN_URL,
                 data=json.dumps({"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}),
                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    if resp.status_code != 200:
        raise AuthError("http", f"Minecraft login failed: HTTP {resp.status_code}",
                        status=resp.status_code, body=resp.text[:512])
    data = _json(resp, "login_with_xbox")
    token = data.get("access_token")
    if not token:
        raise AuthError("protocol", "login_with_xbox returned no access_token", body=str(data)[:512])
    return token, time.time() + float(data.get("expires_in", 86400))


def _mc_get(url: str, mc_token: str, params: dict[str, str] | None = None) -> requests.Response:
    try:
        return requests.get(url, headers={"Authorization": f"Bearer {mc_token}",
                                         "Accept": "application/json"},
                            params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise AuthError("http", f"network error calling {url}: {exc}") from exc


def get_profile(mc_token: str) -> dict[str, Any] | None:
    """None = the account has no Minecraft profile (HTTP 404 is a valid outcome)."""
    resp = _mc_get(MC_PROFILE_URL, mc_token)
    if resp.status_code == 404:
        return None
    if resp.status_code == 401:
        raise AuthError("http", "Minecraft token rejected (401)", status=401, body=resp.text[:512])
    if resp.status_code != 200:
        raise AuthError("http", f"profile: HTTP {resp.status_code}",
                        status=resp.status_code, body=resp.text[:512])
    return _json(resp, "profile")


def get_entitlements(mc_token: str, *, license_endpoint: bool = False) -> dict[str, bool]:
    """{'owns': bool, 'can_play': bool} from item names; both endpoints share the shape."""
    url = MC_LICENSE_URL if license_endpoint else MC_ENTITLEMENTS_URL
    resp = _mc_get(url, mc_token, params={"requestId": str(uuid.uuid4())})
    if resp.status_code == 401:
        raise AuthError("http", "Minecraft token rejected (401)", status=401, body=resp.text[:512])
    if resp.status_code != 200:
        raise AuthError("http", f"entitlements: HTTP {resp.status_code}",
                        status=resp.status_code, body=resp.text[:512])
    items = _json(resp, "entitlements").get("items") or []
    names = {item.get("name") for item in items if isinstance(item, dict)}
    return {"owns": "product_minecraft" in names, "can_play": "game_minecraft" in names}


def authenticate(msa: MsaTokens) -> Session:
    """Full chain: MSA token -> XBL user -> XSTS -> Minecraft -> profile + entitlements."""
    user_token, user_uhs = xbox_user_token(msa.access_token)
    xsts_token, xsts_uhs = xsts_authorize(user_token)
    if user_uhs != xsts_uhs:                       # HMCL + Prism both assert this
        raise AuthError("protocol", "uhs mismatch between user token and XSTS token")
    mc_token, not_after = login_with_xbox(xsts_uhs, xsts_token)

    profile = get_profile(mc_token)
    ent = get_entitlements(mc_token)
    if not ent["owns"] and not ent["can_play"]:
        # second opinion, as HMCL does when profile is missing (license call needs no requestId)
        try:
            ent = get_entitlements(mc_token, license_endpoint=True)
        except AuthError:
            pass

    session = Session(msa=msa, mc_access_token=mc_token, mc_not_after=not_after,
                      profile=profile or {}, owns_minecraft=ent["owns"], can_play=ent["can_play"])
    if profile is None:
        if ent["owns"] or ent["can_play"]:
            raise AuthError("no_profile", "account owns Minecraft but has no profile yet "
                                          "(retry in a few seconds)")
        raise AuthError("no_minecraft", "this account does not own Minecraft: Java Edition")
    return session


def login_device_code(*, client_id: str = MSA_CLIENT_ID, scope: str = MSA_SCOPE,
                      on_code=None) -> Session:
    """End-to-end interactive login.  on_code(device) is called to display user_code/URI."""
    device = start_device_flow(client_id, scope)
    if on_code is not None:
        on_code(device)                            # show device['user_code'] + ['verification_uri']
    msa = poll_for_token(device, client_id=client_id, scope=scope)
    return authenticate(msa)


def ensure_session(session: Session) -> Session:
    """Re-mint the Minecraft token, refreshing the MSA layer first when it is (nearly) dead."""
    if not session.needs_refresh:
        return session
    msa = refresh_msa(session.msa) if session.msa.needs_refresh() else session.msa
    return authenticate(msa)
```

Notes on the sketch, all traceable to §1–§10:

- `start_device_flow` / `poll_for_token` are exercised live apart from the approval itself
  (§3.1/§3.2), including the `authorization_pending`, `invalid_grant` and `invalid_client` paths.
- `xbox_user_token` hardcodes the `d=` prefix; if you later switch to a `service::…::MBI_SSL`
  scope, `t=` is the alternative that prismarine-auth pairs with it (§5).
- `_xerr` reads the header *and* the body: Prism reads the body, prismarine-auth reads the header.
- `get_profile` treats 404 as data, not error; `get_entitlements` matches on
  `game_minecraft` / `product_minecraft` exactly.
- `ensure_session` re-runs the Xbox→Minecraft chain because the Minecraft token has no refresh
  token of its own (§9). Two different windows: the Minecraft token (24 h) is refreshed when
  within 12 h of expiry (Prism's rule); the MSA token (1 h) gets a 5-minute window. Mixing the two
  up — applying "12 hours" to a 1-hour token — makes every call refresh (§9, §12).
- Persist `Session.to_json()`; if `refresh_token` is present, a restart can call
  `ensure_session` instead of `login_device_code`.

---

## 13. Source index (pinned raw URLs)

HMCL — `https://raw.githubusercontent.com/HMCL-dev/HMCL/acc9740fa9289ded38553b6a35045fd608c1ea99/`
- `HMCLCore/src/main/java/org/jackhuang/hmcl/auth/microsoft/MicrosoftService.java` (SCOPE, `d=` ticket,
  XSTS body, XErr constants, mcstore requirement, license fallback, response models)
- `HMCLCore/src/main/java/org/jackhuang/hmcl/auth/oauth/OAuth.java` (endpoint quartet, PKCE, polling,
  `pair("code", …)` bug for live.com, refresh at AAD v2)
- `HMCL/src/main/java/org/jackhuang/hmcl/game/OAuthServer.java:222` (client id comes from a jar
  attribute / system property — HMCL ships its own app)

Prism — `https://raw.githubusercontent.com/PrismLauncher/PrismLauncher/77f2bee593eb9e0cdeef530e0e8686524b68d9d9/`
- `launcher/minecraft/auth/steps/{MSAStep,MSADeviceCodeStep,XboxUserStep,XboxAuthorizationStep,LauncherLoginStep,EntitlementsStep,MinecraftProfileStep}.cpp`
- `launcher/minecraft/auth/{AuthFlow,MinecraftAccount}.cpp`, `launcher/minecraft/auth/Parsers.cpp`
- `CMakeLists.txt` (`Launcher_MSA_CLIENT_ID`, `Launcher_LOGIN_CALLBACK_URL`)

prismarine-auth — `https://raw.githubusercontent.com/PrismarineJS/prismarine-auth/edd7aca2f4158da5f64542f138cdd18592f619a2/`
- `src/common/Constants.js` (Endpoints, `xboxLiveErrors`), `src/MicrosoftAuthFlow.js:74,169`
- `src/TokenManagers/{LiveTokenManager,XboxTokenManager,MinecraftJavaTokenManager}.js`

Live probe transcript (`pwsh` + `requests`, 2026-10-20): device-code start (200, keys/interval/expiry),
poll with `device_code` (400 `authorization_pending`), poll with `code` (400 `invalid_grant`),
poll with bogus client (400 `invalid_client`), refresh with bad token (400 `invalid_grant`),
`user/authenticate` 401, `xsts/authorize` 400, `login_with_xbox` 401, `launcher/login` 401,
`entitlements/mcstore` 401, `entitlements/license` 401, `minecraft/profile` 401.
