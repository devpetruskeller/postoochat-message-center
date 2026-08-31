# Message Studio

Local JSON-backed editor for Postoo message templates.

It is designed to stay simple:
- `messages.json` is the editable source file in this repo
- `messages.export.json` is regenerated whenever the catalog is saved
- Notify tells configured products that a new export is ready for collection

## Run

From this folder:

```powershell
python app.py
```

Then open:

```txt
http://127.0.0.1:8765
```

## Source Of Truth

The practical source of truth for authoring is:

```txt
messages.json
```

When you save in the studio, it updates:
- `messages.json`
- `messages.export.json`

## What The Studio Supports

- Browse messages from `messages.json`
- Edit the selected message as raw JSON
- Compose message title/body/blocks in the UI
- Preview Telegram and WhatsApp rendering
- Channel-specific overrides
- Variable defaults
- Numbered-menu and inline-button actions
- Media Studio URL references and media previews
- Follow-up links via `links`
- Duplicate, delete, reset, save
- Notify configured product collection webhooks
- Authenticated export collection

## Buttons

### `Save`

Writes the current message catalog back to local files.

### `Notify`

Posts a small `message_export.ready` notification to every collector with a
configured webhook in:

- `collectors.json`

The notification includes the export generation time, rendered message count, and:

- `collection_url`

Collectors fetch the current export from that URL using:

```http
Authorization: Bearer <collector-token>
```

The notification does not contain the message catalog. Every normalized group
has its own collector entry, token, category list, and filtered collection URL.
Collector entries are synchronized automatically when groups or categories are
created, renamed, or removed.

## Follow-Up Links

The studio now supports raw follow-up links as a first-class message field:

```json
"links": [
  {
    "key": "admin_link_follow_up",
    "variable": "admin_link",
    "live": true,
    "required": true,
    "mode": "follow_up"
  }
]
```

Purpose:
- keep human-readable message copy in the main body
- send raw deep links as a separate follow-up message
- avoid Telegram Markdown breaking deep links
- make links easier to tap and copy

Current invite and QR messages use this pattern.

## Message Shape

Each message is a complete delivery unit:

```json
{
  "message": "text and blocks",
  "assets": ["items sent immediately after this message"],
  "actions": ["trigger-to-next-message routes"]
}
```

Assets belong to the message, not to an action. An action selects the next
message; SB then sends that message's text, follow-up links, and assets in order.

Typical message fields:

```json
{
  "name": "TWO_FACTOR_AUTHENTICATION_MENU",
  "id": 202606041266,
  "description": "Offers two-factor authentication options.",
  "variables": {},
  "links": [],
  "default": {
    "title": "Two-Factor Authentication",
    "body": {
      "id": "body",
      "type": "text",
      "parts": [
        {
          "text": "Reply with a number from the menu."
        }
      ]
    },
    "blocks": [
      {
        "id": "numbered_menu",
        "type": "text",
        "text": "1. Show Example",
        "required": true
      }
    ],
    "actions": [
      {
        "key": "show_example",
        "label": "Show Example",
        "triggers": [
          "1",
          "show_example",
          "show example"
        ],
        "nextMessageId": 202606041268,
        "live": true
      }
    ]
  },
  "overrides": {
    "telegram": {
      "delivery": "inline_buttons",
      "parse_mode": "Markdown",
      "inline_buttons_enabled": true
    },
    "whatsapp": {
      "delivery": "interactive",
      "template_enabled": false,
      "binding": {
        "content_sid_env": "TWILIO_CONTENT_SID_2FA_MENU"
      }
    }
  },
  "assets": [
    {
      "assetId": "setup_guide",
      "type": "document",
      "url": "https://media.example.com/message-files/2fa-setup-guide.pdf",
      "live": true,
      "required": false,
      "alt": "2FA setup guide"
    }
  ]
}
```

## Actions And SB Routing

Triggers represent every accepted form of a choice. A typed menu number and a
Twilio or Telegram button payload can therefore select the same action:

```json
{
  "key": "show_example",
  "label": "Show Example",
  "triggers": [
    "1",
    "show_example",
    "show example"
  ],
  "nextMessageId": 202606041268,
  "live": true
}
```

SB normalizes the incoming response, looks it up in the active message's live
action triggers, and uses `nextMessageId` to continue:

```txt
SB sends the current message and its assets
    -> user replies "2" or presses "Show Example"
    -> SB matches the action
    -> SB loads nextMessageId from its message cache
    -> SB sends the next message's text and blocks
    -> SB sends that next message's links and assets
    -> SB waits for a response to the new message
```

SB should build a normalized trigger index when it caches the catalog. The
Message Center must reject duplicate normalized triggers among live actions in
the same message.

`nextMessageId: null` means that no deterministic catalog destination has been
assigned yet. SB must report that selection to WP and wait; it must not guess a
destination. These null routes preserve older business commands that still need
to be connected to a catalog message.

WP starts the conversation by asking SB to broadcast a message ID. SB handles
deterministic transitions without waiting for another WP instruction and reports
the response and transition back to WP asynchronously. WP handles unmatched
responses, null destinations, timeouts, delivery faults, and non-deterministic
business decisions.

## WhatsApp Numbered Menus And Twilio Templates

WhatsApp actions remain active even when Twilio templates are disabled:

```json
"whatsapp": {
  "delivery": "interactive",
  "template_enabled": false,
  "binding": {
    "content_sid_env": "TWILIO_CONTENT_SID_2FA_MENU"
  }
}
```

- `delivery: "interactive"` records that the message is template-ready.
- `template_enabled: false` makes WhatsApp use ordinary text and the required
  `numbered_menu` block.
- `content_sid_env` names the environment variable that will hold the approved
  Twilio Content SID.
- Setting `template_enabled` to `true` switches presentation to the approved
  Twilio template without changing the actions or conversation routes.

Disabling template buttons must suppress only the button presentation. It must
not remove `actions`, because SB still needs their numeric and text triggers.

## Media Storage And Routing

Message-owned assets are uploaded and administered by the Media Studio plugin.
The Message Center does not upload or store media files. Media Studio returns an
`assetId`, media `type`, and stable published HTTPS `url`; the Message Center
stores that reference in `messages.json`.

```txt
WP shared media
  message-images/
  message-audio/
  message-video/
  message-files/
```

Media delivery uses those published references rather than placing file bytes in
the WP-to-SB instruction:

```txt
WP -> SB: message ID
SB -> cache: load the message and its already-published asset URLs
SB -> channel API: message content plus HTTPS media URLs
WhatsApp/Telegram -> WP media endpoint: fetch the media bytes
WhatsApp/Telegram -> recipient: display the native media
```

The same URL-based record is retained in the authoring catalog and published
export:

```json
{
  "assetId": "example_screenshot",
  "type": "image",
  "url": "https://media.example.com/message-images/1004_mes_onboard_info.png",
  "live": true,
  "required": false
}
```

Media Studio participates in authoring only. SB does not call Media Studio to
resolve an asset while sending a message. It reads the stable URL directly from
its cached export. This avoids an additional network request, reduces delivery
latency, and prevents Media Studio availability from becoming a live
message-delivery dependency.

The file is not embedded in the WP instruction or SB message cache. The bytes
still travel from the WP media endpoint to WhatsApp or Telegram. The endpoint
must be HTTPS-accessible to the channel provider, return the correct MIME type,
and prevent path traversal. Stable media URLs should use unguessable asset
filenames or path tokens when the media must not be easily discoverable.

Where a channel returns a reusable media identifier, SB may cache it as a local
optimization and prefer it on later sends. That optimization does not require a
Media Studio lookup. The stable URL remains the portable fallback.

In the Message Center's right-hand preview, image, audio, and video elements load
directly from `assets[].url`. Failed loads show an unavailable-media warning
instead of a broken visual. Documents render as external links. Only HTTPS media
references are accepted and saved.

The Media Studio endpoint should use an allowlisted hostname in production.
Media Studio remains responsible for upload validation, MIME types, file sizes,
safe filenames, and storage. The Message Center is responsible only for message
association and preview.

## Environment

The studio reads config from its own `./.env`.

Current useful variables:

```env
PT_MESSAGE_STUDIO_IMAGES=public/message-images
PT_MESSAGE_STUDIO_PUBLIC_URL=https://message-center.example.com/public
PT_MESSAGE_COLLECTOR_TOKEN=<shared-secret>
PT_MESSAGE_COLLECTOR_WEBHOOK=https://communications.example.com/functions/v1/message-export-ready
```

The collector token is shared only with the Supabase communications router.
The public Message Center origin is derived from `PT_MESSAGE_STUDIO_PUBLIC_URL`.

## Collector Registry

`collectors.json` defines the single communications router's explicit scope.
Credentials and deployment URLs remain in `.env`:

```json
{
  "postoochat": {
    "token_env": "PT_MESSAGE_COLLECTOR_TOKEN",
    "webhook_env": "PT_MESSAGE_COLLECTOR_WEBHOOK",
    "groups": ["postoochat", "postoo_chat", "postoo_ride"],
    "categories": ["", "archive", "templates"],
    "channels": ["telegram", "whatsapp"]
  }
}
```

Scopes are deny-by-default and are not expanded automatically when taxonomy
changes. A blank category value represents General.

## Test Collection Client

Run the included notification receiver in a second terminal:

```powershell
python client.py
```

It listens on:

```txt
http://127.0.0.1:8876/message-export-ready
```

To use the test client, temporarily set `PT_MESSAGE_COLLECTOR_WEBHOOK` to the
URL above. Save a message and click **Notify**. The client receives the
notification, uses the collector token supplied in the notification request to
fetch the filtered export, and saves it as:

```txt
store/messages.import-<UTC timestamp>.json
```

Optional client environment settings:

```env
MS_CLIENT_HOST=127.0.0.1
MS_CLIENT_PORT=8876
MS_CLIENT_ALLOWED_COLLECTION_HOSTS=127.0.0.1,localhost
```

Leave `MS_CLIENT_ALLOWED_COLLECTION_HOSTS` empty when the Message Center uses
an ngrok or other external collection hostname during testing.

### Git Bash curl test

Start both servers in separate terminals:

```bash
python app.py
python client.py
```

Copy `PT_MESSAGE_COLLECTOR_TOKEN` from `.env`, then set these Git Bash
variables:

```bash
COLLECTOR="postoochat"
TOKEN="<PT_MESSAGE_COLLECTOR_TOKEN from .env>"
MESSAGE_CENTER_URL="http://127.0.0.1:8765"
CLIENT_URL="http://127.0.0.1:8876"
```

Post a simulated ready notification to the test client:

```bash
curl --fail-with-body \
  --request POST \
  "${CLIENT_URL}/message-export-ready" \
  --header "Authorization: Bearer ${TOKEN}" \
  --header "Content-Type: application/json" \
  --data "{
    \"event\": \"message_export.ready\",
    \"source\": \"message_center\",
    \"collector\": \"${COLLECTOR}\",
    \"generated_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
    \"collection_url\": \"${MESSAGE_CENTER_URL}/api/export/collect/${COLLECTOR}\"
  }"
```

The client uses the bearer token to collect the filtered export and writes:

```txt
store/messages.import-<UTC timestamp>.json
```

To test the Message Center collection endpoint directly:

```bash
curl --fail-with-body \
  --request GET \
  "${MESSAGE_CENTER_URL}/api/export/collect/${COLLECTOR}" \
  --header "Authorization: Bearer ${TOKEN}" \
  --header "Accept: application/json"
```

To save that direct response from Git Bash:

```bash
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

curl --fail-with-body \
  --request GET \
  "${MESSAGE_CENTER_URL}/api/export/collect/${COLLECTOR}" \
  --header "Authorization: Bearer ${TOKEN}" \
  --header "Accept: application/json" \
  --output "store/messages.import-${TIMESTAMP}.json"
```

## Notes

- The app is intentionally local-first and file-backed.
- Notify does not push message data or overwrite collector data. Each product decides when and how to import the collected export.
- Invite and QR cards should keep raw links out of the main message body and use `links` follow-up entries instead.
