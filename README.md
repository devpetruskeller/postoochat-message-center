# PosTooChat Message Center v2

This is the TypeScript replacement for the legacy Python Message Center.

## Blueprint first

[`message-center.blueprint.json`](./message-center.blueprint.json) is the
single design contract for this project. It defines the runtime, storage,
canonical message shape, validation rules, API surface and migration order.

Before changing the Worker, Composer, export format or database schema:

1. Update the blueprint when the product rule changes.
2. Implement the corresponding TypeScript change.
3. Add or update a regression test.
4. Confirm the behavior locally before deployment.

The legacy repository's `messages.json` is a one-time import source. It is not
a second source of truth for this application.

## WhatsApp template policy

Every WhatsApp override separates the message delivery method from the user
interaction. `delivery` describes what Twilio sends; `interaction` describes
how the recipient responds. `interactive` is not a delivery value.

No approved Twilio template:

```json
{
  "delivery": "plain_text",
  "template": {
    "enabled": false,
    "content_sid_env": ""
  },
  "interaction": {
    "type": "numbered_menu"
  }
}
```

Approved Twilio Content Template:

```json
{
  "delivery": "template",
  "template": {
    "enabled": true,
    "content_sid_env": "PT_ONBOARDING_CONTENT_SID"
  },
  "interaction": {
    "type": "quick_reply"
  }
}
```

Use `plain_text` for a free-form WhatsApp message, including a generated
numbered menu. Use `template` only when the Worker sends an approved Twilio
Content Template by Content SID. Legacy fields (`template_enabled` and
`binding.content_sid_env`) are import compatibility fields only. The v2
catalog and Composer use `template` and `interaction` instead.

## Blueprint examples

The following examples show the intended model using the two critical Suite
messages. `ONBOARDING` is the message referred to as “ONBOARD” in conversation.
They are deliberately compact examples: the imported catalog retains the full
published copy and full block list.

### `START_HERE` — Suite app router

`START_HERE` is shared Suite copy with channel-specific delivery settings. Its
actions are the single source for Telegram buttons and the generated WhatsApp
numbered menu.

```json
{
  "id": 202605281231,
  "name": "START_HERE",
  "suite_key": "SUITE_APP_ROUTER",
  "group": "postoochat_suite",
  "category": "General",
  "default": {
    "title": "🚀 PosToo Suite",
    "body": {
      "id": "body",
      "type": "text",
      "parts": [
        { "text": "Try any ", "format": [] },
        { "text": "Mini-App ", "format": ["bold", "italic"] },
        { "text": "right within your favorite Chat.", "format": [] }
      ],
      "required": true
    },
    "blocks": [
      { "id": "postoochat", "type": "text", "text": "💬 PosTooChat is a managed Chat Center.", "required": true },
      { "id": "postoogo", "type": "text", "text": "🚗 PosTooGo lets users book rides or register as drivers.", "required": true }
    ],
    "actions": [
      { "key": "chatcenter", "label": "ChatCenter", "triggers": ["1", "chatcenter", "postoochat"], "nextMessageId": 202605281246, "live": true },
      { "key": "faceluv", "label": "FaceLuv", "triggers": ["2", "faceluv"], "nextMessageId": 202605281246, "live": true },
      { "key": "market", "label": "Market", "triggers": ["3", "market"], "nextMessageId": 202605281246, "live": true },
      { "key": "ride", "label": "Ride", "triggers": ["4", "ride"], "nextMessageId": 202605281246, "live": true }
    ]
  },
  "overrides": {
    "telegram": {
      "delivery": "inline_buttons",
      "parse_mode": "HTML",
      "inline_buttons_enabled": true
    },
    "whatsapp": {
      "title": "PosToo Suite",
      "delivery": "plain_text",
      "template": {
        "enabled": false,
        "content_sid_env": ""
      },
      "interaction": {
        "type": "numbered_menu"
      },
      "actions_menu": {
        "enabled": true,
        "instruction": "To go to any of the Chat Apps, reply ONLY with the number in front of it.",
        "item_format": "{index}. {label}"
      }
    }
  },
  "assets": [],
  "links": []
}
```

### `ONBOARDING` — profile capture and video examples

`ONBOARDING` uses explicit channel overrides for its different instructions.
The Telegram buttons and WhatsApp numbered replies reference the video assets
already attached to this same message. No separate video message is needed.

```json
{
  "id": 202605281246,
  "name": "ONBOARDING",
  "suite_key": "SUITE_ONBOARDING_PROFILE",
  "group": "postoochat_suite",
  "category": "General",
  "variables": {
    "admin_link": {
      "type": "text",
      "required": false,
      "default": "https://t.me/postoo_bot?text=Hello%20Admin"
    }
  },
  "default": {
    "title": "🚀 Onboarding",
    "body": { "id": "body", "type": "text", "text": "Let's get your account set up.", "required": true },
    "actions": [
      {
        "key": "watch_whatsapp_onboarding_example",
        "label": "Watch WhatsApp example",
        "triggers": ["1", "watch whatsapp example", "whatsapp example"],
        "assetId": "whatsapp_onboarding_example",
        "live": true
      },
      {
        "key": "watch_telegram_onboarding_example",
        "label": "Watch Telegram example",
        "triggers": ["2", "watch telegram example", "telegram example"],
        "assetId": "telegram_onboarding_example",
        "live": true
      }
    ]
  },
  "overrides": {
    "telegram": {
      "delivery": "inline_buttons",
      "parse_mode": "Markdown",
      "inline_buttons_enabled": true,
      "blocks": [
        { "id": "profile_request", "type": "text", "text": "Reply with your mobile number and birth date.", "required": true },
        { "id": "example", "type": "text", "text": "+27821234567 1990-04-25", "format": ["bold"], "required": true }
      ]
    },
    "whatsapp": {
      "delivery": "plain_text",
      "template": {
        "enabled": false,
        "content_sid_env": ""
      },
      "interaction": {
        "type": "numbered_menu"
      },
      "body": { "id": "body", "type": "text", "text": "Let's get you up and running.", "format": ["bold", "italic"], "required": true },
      "blocks": [
        { "id": "profile_request", "type": "text", "text": "Reply only with your birth date in YYYY-MM-DD format.", "required": true }
      ],
      "actions_menu": {
        "enabled": true,
        "instruction": "Would you like to watch an onboarding example? Reply with a number.",
        "item_format": "{index}. {label}"
      }
    }
  },
  "assets": [
    {
      "assetId": "whatsapp_onboarding_example",
      "type": "video",
      "url": "https://api.postoochat.com/wp-content/uploads/2026/09/whatsapp_onboarding.mp4",
      "live": true,
      "required": true,
      "alt": "WhatsApp onboarding example video"
    },
    {
      "assetId": "telegram_onboarding_example",
      "type": "video",
      "url": "https://api.postoochat.com/wp-content/uploads/2026/09/compressed_telegram_onboarding.mp4",
      "live": true,
      "required": true,
      "alt": "Telegram onboarding example video"
    }
  ],
  "links": [
    {
      "variable": "admin_link",
      "live": true,
      "required": true,
      "mode": "follow_up",
      "key": "admin_link_follow_up"
    }
  ]
}
```

`assets` are media references sent with the message. `links` are different:
they resolve their named variable and send the URL as a follow-up message, so a
long or raw URL does not interfere with the channel's formatted message body.

## Local development

```powershell
npm run dev
```

Open the local URL printed by Wrangler, normally `http://127.0.0.1:8787`.
The app uses a local D1 database while developing. Apply migrations after a
fresh local database or a new migration:

```powershell
npx wrangler d1 migrations apply postoochat-message-center --local
```

Then import a copy of the legacy `messages.json` through the v2 interface. Do
not deploy or import directly into the production database during this stage.

## Current state

- TypeScript Worker with one catalog API path
- Local D1 catalog migration
- Explicit channel-override persistence endpoint
- Local JSON import, catalog inspection and export check
- Regression test for channel override persistence

## Production access and Supabase collection

Protect the production Message Center hostname with a Cloudflare Access self-hosted application. Allow only the named administrator identities and require independent MFA. Do not rely on a browser-held API token for the Studio UI.

Supabase collects a deliberately narrow export from:

```text
GET /api/export/collect/postoochat
Authorization: Bearer <PT_MESSAGE_COLLECTOR_TOKEN>
```

This endpoint exports only `postoochat_suite / General` records for Telegram and WhatsApp. It does not expose catalog editing routes. In production, create a Cloudflare Access **Service Auth** policy for this exact path and store the generated service-token client ID and secret in Supabase Edge Function secrets. The Supabase request should include both the Cloudflare Access service-token headers and the collector bearer token.

Set these Worker secrets before deployment:

```text
MESSAGE_CENTER_ADMIN_TOKEN=...            # temporary API fallback only
PT_MESSAGE_COLLECTOR_TOKEN=...            # shared only with Supabase
PT_MESSAGE_COLLECTOR_WEBHOOK=https://...  # Supabase export-ready webhook
```

The **Notify Supabase** button calls `POST /api/export-webhook`. It sends `message_export.ready`, the collector name, record count and protected collection URL to `PT_MESSAGE_COLLECTOR_WEBHOOK` with the collector bearer token. Notify never sends the message bodies themselves; Supabase collects them afterwards from the protected collection endpoint.

The Composer, catalog validation, media library, and production cutover remain
to be implemented according to the blueprint.
