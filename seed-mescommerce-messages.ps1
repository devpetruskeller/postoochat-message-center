[CmdletBinding()]
param([string]$BaseUrl = "http://127.0.0.1:8765")

$ErrorActionPreference = "Stop"

function New-Message {
    param(
        [int64]$Id,
        [string]$Name,
        [string]$Text,
        [string[]]$Variables = @(),
        [string]$Description = "MesCommerce seller onboarding communication."
    )
    $variableMap = [ordered]@{}
    foreach ($variable in $Variables) {
        $variableMap[$variable] = [ordered]@{ type = "text"; required = $true; default = "" }
    }
    $message = [ordered]@{
        name = $Name
        id = $Id
        group = "postoo_market"
        category = ""
        description = $Description
        variables = $variableMap
        links = @()
        default = [ordered]@{
            title = ""
            body = [ordered]@{
                id = "body"
                type = "text"
                parts = @([ordered]@{ text = $Text })
                required = $true
            }
            blocks = @()
            actions = @()
        }
        overrides = [ordered]@{
            telegram = [ordered]@{ delivery = "plain_text"; parse_mode = "Markdown"; inline_buttons_enabled = $false }
            whatsapp = [ordered]@{ delivery = "plain_text"; template_enabled = $false; binding = [ordered]@{ content_sid_env = "" } }
        }
        assets = @()
    }
    return [pscustomobject]$message
}

$messages = @(
    (New-Message 202608150001 "MESCOMMERCE_VERIFICATION_LOCAL" "For this local test, reply with verification code 123456."),
    (New-Message 202608150002 "MESCOMMERCE_VERIFICATION_SENT" "I sent a 6-digit verification code to this WhatsApp number. Reply with that code to continue."),
    (New-Message 202608150003 "MESCOMMERCE_VERIFICATION_FAILED" "I could not send the verification code. Please try LIST again shortly."),
    (New-Message 202608150004 "MESCOMMERCE_WELCOME_LOCAL" "Welcome to MesCommerce Marketplace.`n`nFor this local test, reply with verification code 123456."),
    (New-Message 202608150005 "MESCOMMERCE_WELCOME_VERIFICATION_SENT" "Welcome to MesCommerce Marketplace.`n`nI sent a 6-digit verification code to this WhatsApp number. Reply with that code to continue."),
    (New-Message 202608150006 "MESCOMMERCE_VERIFICATION_INVALID" "That verification code is not valid. Please try again, or reply RESTART for a new code."),
    (New-Message 202608150007 "MESCOMMERCE_CHOOSE_CATEGORY" "Choose a category:`n1. Houses for sale`n2. Cars for sale`n3. Water suppliers"),
    (New-Message 202608150008 "MESCOMMERCE_VERIFIED_CHOOSE_CATEGORY" "Number verified.`n`nChoose a category:`n1. Houses for sale`n2. Cars for sale`n3. Water suppliers"),
    (New-Message 202608150009 "MESCOMMERCE_CATEGORY_INVALID" "Please reply with 1, 2 or 3.`n1. Houses for sale`n2. Cars for sale`n3. Water suppliers"),
    (New-Message 202608150010 "MESCOMMERCE_CHECKOUT_READY" "Great—{{category}}.`n`nYour listing fee is R {{listing_fee}}. Pay securely with Yoco:`n{{checkout_url}}`n`nAfter payment, return here and reply PAID." @("category", "listing_fee", "checkout_url")),
    (New-Message 202608150011 "MESCOMMERCE_CHECKOUT_FAILED" "I could not create the Yoco checkout just now. Please try again in a moment."),
    (New-Message 202608150012 "MESCOMMERCE_PAYMENT_PENDING" "Your payment is not confirmed yet. Complete the Yoco checkout, then reply PAID. Reply RESTART to begin again."),
    (New-Message 202608150013 "MESCOMMERCE_PAYMENT_CONFIRMED" "Payment confirmed. What is your selling price in rand?`nExample: 12500"),
    (New-Message 202608150014 "MESCOMMERCE_PRICE_INVALID" "Please send only the selling price in rand. Example: 12500"),
    (New-Message 202608150015 "MESCOMMERCE_LOCATION_REQUEST" "Now send the product or service location using WhatsApp's attachment button → Location → Send your current location."),
    (New-Message 202608150016 "MESCOMMERCE_LOCATION_INVALID" "Please send a WhatsApp location pin—not a typed address."),
    (New-Message 202608150017 "MESCOMMERCE_RESTART_REQUIRED" "Something went wrong. Reply RESTART to try again."),
    (New-Message 202608150018 "MESCOMMERCE_LISTING_LIVE" "Your {{category}} listing is live at R {{price}}. Buyers who activate marketplace access can contact you directly using the channel you approved.`n`nReply LIST whenever you want to add another price." @("category", "price")),
    (New-Message 202608150019 "MESCOMMERCE_ALREADY_LIVE" "Your last listing is already live. Reply LIST to add another one."),
    (New-Message 202608150020 "MESCOMMERCE_CONDITION_PROMPT" "Payment confirmed.`n`nIs this product {{new_label}} or {{used_label}}?`n`n1. {{new_label}}`n2. {{used_label}}" @("new_label", "used_label")),
    (New-Message 202608150021 "MESCOMMERCE_CONDITION_INVALID" "Please reply with 1 or 2.`n`n1. {{new_label}}`n2. {{used_label}}" @("new_label", "used_label")),
    (New-Message 202608150022 "MESCOMMERCE_PRICE_REQUEST" "What is your selling price in rand?`nExample: 12500"),
    (New-Message 202608150023 "MESCOMMERCE_ADMIN_REPLY" "{{message}}" @("message")),
    (New-Message 202608150024 "MESCOMMERCE_MARKETPLACE_ACCESS_CONSENT" "Welcome to MesCommerce. Before you contact marketplace sellers directly, please confirm that MesCommerce and PosTooChat may process your channel identity and remember that you accepted the marketplace access terms.`n`nThis consent enables direct contact with any listing in any category. It does not subscribe you to marketing.`n`nTo continue, type AGREE in the message box and tap Send. To cancel, type DECLINE and tap Send.`nConsent version: {{consent_version}}`nTerms: {{terms_url}}" @("consent_version", "terms_url") "MesCommerce first-time marketplace access consent."),
    (New-Message 202608150025 "MESCOMMERCE_MARKETPLACE_ACCESS_READY" "You're all set. We hope you find what you're looking for.`n`nReturn to MesCommerce using the secure link below. You can now contact any listing in any category directly using its available WhatsApp or Telegram button:`n{{marketplace_url}}" @("marketplace_url") "MesCommerce marketplace access confirmation and return link."),
    (New-Message 202608150026 "MESCOMMERCE_MARKETPLACE_ACCESS_DECLINED" "Marketplace contact access was not activated. No seller contact details were unlocked.`n`nYou can browse MesCommerce and activate access later by tapping a WhatsApp or Telegram contact button again." @() "MesCommerce marketplace access consent outcome."),
    (New-Message 202608150032 "MESCOMMERCE_SELLER_CONTACT_CONSENT_REQUIRED" "To publish this listing, choose whether buyers who complete the MesCommerce inquiry introduction may contact you directly using this WhatsApp number.`n`nReply AGREE to publish with direct contact or DECLINE to stop without publishing.`nConsent version: {{consent_version}}" @("consent_version") "MesCommerce seller direct-contact consent communication."),
    (New-Message 202608150033 "MESCOMMERCE_SELLER_CONTACT_CONSENT_DECLINED" "Your listing was not published because direct buyer contact was not approved. Reply LIST whenever you want to begin again." @() "MesCommerce seller direct-contact consent outcome communication."),
    (New-Message 202608150034 "MESCOMMERCE_MARKETING_CONSENT_REQUIRED" "Would you like to receive optional MesCommerce news, offers and competition updates on this channel? This is not required to list or inquire.`n`nReply MARKETING YES to opt in or MARKETING NO to decline.`nMarketing consent version: {{consent_version}}" @("consent_version") "MesCommerce optional marketing consent communication."),
    (New-Message 202608150035 "MESCOMMERCE_MARKETING_CONSENT_RECORDED" "Your marketing preference is now {{preference}}. You can change it at any time by replying MARKETING YES or MARKETING NO." @("preference") "MesCommerce optional marketing consent outcome communication."),
    (New-Message 202608150036 "MESCOMMERCE_SHARE_DISCLOSURE" "MesCommerce can create a unique tracked link for this {{category}} listing. Competition credit is awarded when a new visitor opens your link—not merely when the share sheet opens.`n`nReply SHARE to create the link or CANCEL to stop.`nCompetition terms: {{terms_url}}" @("category", "terms_url") "MesCommerce tracked-share and competition disclosure."),
    (New-Message 202608150037 "MESCOMMERCE_SHARE_READY" "Your tracked MesCommerce share link is ready:`n{{share_url}}`n`nVerified competition entries: {{entry_count}}" @("share_url", "entry_count") "MesCommerce tracked-share outcome communication."),
    (New-Message 202608150038 "MESCOMMERCE_REFERRAL_INVALID" "This referral link is no longer available or cannot be used from this number. Ask the person who invited you to share a new link." @() "MesCommerce invalid, expired or self-referral outcome communication."),
    (New-Message 202608150039 "MESCOMMERCE_TAGS_REQUEST" "Add searchable tags for this listing, separated by commas.`nExample: Bellville, Brackenfell`n`nUse up to 8 tags. Reply SKIP if you do not want to add tags." @() "MesCommerce listing tag request communication."),
    (New-Message 202608150040 "MESCOMMERCE_TAGS_INVALID" "Please send up to 8 tags separated by commas. Each tag may contain up to 30 characters.`nExample: Bellville, Brackenfell`n`nReply SKIP if you do not want to add tags." @() "MesCommerce invalid listing tags communication.")
)

$obsoleteNames = @(
    "MESCOMMERCE_INQUIRY_BUYER_OPENED",
    "MESCOMMERCE_INQUIRY_SELLER_ALERT",
    "MESCOMMERCE_INQUIRY_BUYER_TO_SELLER",
    "MESCOMMERCE_INQUIRY_SELLER_TO_BUYER",
    "MESCOMMERCE_INQUIRY_CLOSED",
    "MESCOMMERCE_INQUIRY_CONSENT_REQUIRED",
    "MESCOMMERCE_INQUIRY_CONSENT_ACCEPTED",
    "MESCOMMERCE_INQUIRY_CONSENT_DECLINED",
    "MESCOMMERCE_INQUIRY_MESSAGE_REQUEST",
    "MESCOMMERCE_INQUIRY_SELLER_NOTICE",
    "MESCOMMERCE_INQUIRY_HANDOFF_READY",
    "MESCOMMERCE_INQUIRY_UNAVAILABLE",
    "MESCOMMERCE_INQUIRY_INVALID"
)

foreach ($obsoleteName in $obsoleteNames) {
    try {
        $deleteJson = @{ selected_name = $obsoleteName } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/messages/delete" -ContentType "application/json" -Body $deleteJson | Out-Null
    }
    catch {
        if ([int]$_.Exception.Response.StatusCode -ne 404) { throw }
    }
}

foreach ($message in $messages) {
    if (-not $message.name) { throw "Invalid message object: $($message | ConvertTo-Json -Depth 3 -Compress)" }
    $json = $message | ConvertTo-Json -Depth 20
    $createJson = @{ message = $message } | ConvertTo-Json -Depth 20
    try {
        Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/messages/create" -ContentType "application/json" -Body $createJson | Out-Null
    }
    catch {
        if ([int]$_.Exception.Response.StatusCode -ne 409) { throw }
        $encodedName = [Uri]::EscapeDataString([string]$message.name)
        Invoke-RestMethod -Method Put -Uri "$BaseUrl/api/messages/$encodedName" -ContentType "application/json" -Body $json | Out-Null
    }
}

Write-Host "MesCommerce message definitions saved: $($messages.Count)"
