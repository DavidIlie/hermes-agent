# Friend code approval relay

The Fleet Discord overlay can render durable owner approval cards without
giving the model an approval tool. The Discord adapter polls a narrow broker
API, authenticates an exact button click, and reports a fixed status in the
friend's original thread.

## Agent configuration

Configure this only on the `default` Discord profile:

```yaml
extra:
  friend_code_approval:
    enabled: true
    broker_url: http://hermes-friends.hermes.svc.cluster.local:8080
    owner_guild_id: "853884362436116491"
    owner_channel_id: "1537861134076350465"
    owner_user_id: "243009043260637184"
    friend_guild_id: "1540757211146493993"
    friend_channel_id: "1540773031436353708"
    poll_interval_seconds: 5
    request_timeout_seconds: 10
```

The profile's secret scope must provide two non-empty, distinct credentials:

- `HERMES_FRIEND_CODE_DELIVERY_TOKEN`
- `HERMES_FRIEND_CODE_OWNER_TOKEN`

The broker should store only hashes of these tokens. The service must be
cluster-local; the agent rejects redirects and responses larger than 128 KiB.

## Broker HTTP contract

All bodies and successful responses use `application/json`. `event` is the
broker notification's unsigned 64-bit integer. `request` is a
`friend_<24-lowercase-hex>` identifier, `version` is a positive integer, and
`hash` is the request's 64-character lowercase hexadecimal digest.

### Poll notifications and terminal receipts

`GET /v1/discord/friend-code/notifications` uses the delivery token and
returns at most 25 entries in each list:

```json
{
  "guild_id": "853884362436116491",
  "channel_id": "1537861134076350465",
  "notifications": [
    {
      "id": 41,
      "kind": "approval_requested",
      "created_at": "2026-08-22T10:00:00Z",
      "request": {
        "id": "friend_0123456789abcdef01234567",
        "version": 3,
        "request_hash": "<64 lowercase hex>",
        "kind": "existing_public",
        "request": "Fix the public README and open a pull request.",
        "repository": {
          "id": 123,
          "full_name": "owner/public-repository",
          "url": "https://github.com/owner/public-repository",
          "default_branch": "main",
          "archived": false,
          "disabled": false,
          "fork": false
        },
        "new_repository_name": "",
        "origin": {
          "platform": "discord",
          "profile": "friends",
          "guild_id": "1540757211146493993",
          "channel_id": "1540773031436353708",
          "thread_id": "<friend thread snowflake>",
          "user_id": "<friend user snowflake>",
          "session_hash": "<64 lowercase hex>"
        },
        "state": "awaiting_owner_approval",
        "execution_ready": false,
        "expires_at": "2026-08-23T10:00:00Z"
      }
    }
  ],
  "receipts": [
    {
      "event": 41,
      "request": "friend_0123456789abcdef01234567",
      "version": 3,
      "hash": "<64 lowercase hex>",
      "state": "expired",
      "execution_ready": false,
      "friend_origin": {
        "guild_id": "1540757211146493993",
        "channel_id": "1540773031436353708",
        "thread_id": "<friend thread snowflake>",
        "user_id": "<friend user snowflake>"
      }
    }
  ]
}
```

For `new_public`, `repository` is `null` and `new_repository_name` is a valid
GitHub repository basename. Only `approved_blocked`, `denied`, `cancelled`,
`expired`, and `superseded` are terminal states. Receipts contain no owner
identity or free-form status message.

### Bind a notification to its Discord message

`POST /v1/discord/friend-code/notifications/{event}/ack` uses the delivery
token:

```json
{
  "request": "friend_0123456789abcdef01234567",
  "version": 3,
  "hash": "<64 lowercase hex>",
  "message_id": "<owner Discord message snowflake>"
}
```

The broker persists this exact binding before accepting a decision. Repeating
the same acknowledgement is a successful no-op; a conflicting binding is
`409 Conflict`.

### Record an owner decision

`POST /v1/discord/friend-code/decisions` uses the owner token and requires an
`Idempotency-Key` header:

```json
{
  "event": 41,
  "request": "friend_0123456789abcdef01234567",
  "version": 3,
  "hash": "<64 lowercase hex>",
  "decision": "approve",
  "message_id": "<owner Discord message snowflake>",
  "clicker": {
    "platform": "discord",
    "profile": "default",
    "guild_id": "853884362436116491",
    "channel_id": "1537861134076350465",
    "user_id": "243009043260637184"
  }
}
```

The broker verifies every identity field, the current request version and
hash, the unexpired pending state, and the previously acknowledged message.
It returns the committed receipt:

```json
{
  "request": "friend_0123456789abcdef01234567",
  "version": 3,
  "hash": "<64 lowercase hex>",
  "decision": "approve",
  "state": "approved_blocked",
  "execution_ready": false
}
```

`deny` returns state `denied`. Repeating the same key and body returns the same
receipt. Reusing a key with different input is `409 Conflict`. Approval never
sets `execution_ready` while the public runner remains unavailable.

`GET /v1/discord/friend-code/decisions/{event}` uses the owner token and the
same `Idempotency-Key`. It returns the same receipt after a lost POST response,
`404 Not Found` when no decision committed, and `409 Conflict` for a mismatched
key.

### Acknowledge a friend status receipt

`POST /v1/discord/friend-code/receipts/{event}/ack` uses the delivery token:

```json
{
  "request": "friend_0123456789abcdef01234567",
  "version": 3,
  "hash": "<64 lowercase hex>",
  "state": "expired"
}
```

Repeating an exact acknowledgement is a successful no-op. The agent creates
friend-facing text from fixed local templates, so broker content cannot leak
owner metadata into the friend guild.

## Recovery properties

Pending bindings live in `~/.hermes/gateway/friend_code_approvals.json` with
mode `0600`. The owner card includes an event marker, allowing a restart to
recover a sent message before its ID was persisted. Button custom IDs are an
HMAC over the decision, event, request, version, hash, Discord message, owner
guild, owner channel, and owner user. A lost broker response is recovered only
through the exact idempotency-key receipt endpoint. Fixed friend status text
is deduplicated against recent thread history before delivery.
