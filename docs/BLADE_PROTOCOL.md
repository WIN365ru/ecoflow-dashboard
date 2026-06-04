# EcoFlow Blade — MQTT protocol notes

Reverse-engineered by capturing commands the iOS app publishes on the shared
MQTT account (the broker echoes them to all subscribers on the same account).
Capture them live with `/blade_obs` after pressing a button in the app.

All commands are published to:

```
/app/{user_id}/{sn}/thing/property/set
```

with the envelope:

```json
{
  "from": "Android",
  "lang": "en-us",
  "id": "<random>",
  "moduleType": <int>,
  "operateType": "<string>",
  "version": "1.0",
  "params": { ... , "sessionID": <random 1-1000> }
}
```

`sessionID` appears to be a per-request nonce; a fresh random value works.

## Command families (by moduleType / operateType)

| moduleType | operateType    | params                                                        | Meaning |
|-----------:|----------------|---------------------------------------------------------------|---------|
| 1          | `controlCmd`   | `{"cmd": <int>, "x": <int>, "y": <int>}`                      | Movement / action. `x`/`y` look like drive vectors (0,0 = neutral). |
| 20         | `startTask`    | `{"objID": [<int>...]}`                                       | Start mowing. `objID` = zones/maps; `[0]` = default/all. |
| 29         | `setWorkMode`  | `{"mode", "leafHeight", "movHeight", "speed", "cmdType", "reserve":[0,0,0,0]}` | Work settings. `cmdType` 0=set, 1=query. `mode` 1=Normal. `leafHeight`=cut mm, `movHeight`=transport mm, `speed`. |
| 34         | `setSwitch`    | `{"switchList": <int64 bitmask>, "type": 0/1}`               | Toggle switches (LEDs/indicators). `type` 0=query, 1=set. One bit per toggle. |
| 37         | `setGetParam`  | `{"cmdID": <int>, "data", "data1", "data2", "type": 0/1}`   | Device params. `type` 0=read, 1=set. `cmdID 1` = speech volume (`data` 0-100). |

### controlCmd `cmd` values (moduleType 1)

| cmd | Action | Status |
|----:|--------|--------|
| 2   | Continue / Resume | ✅ confirmed |
| ?   | Pause | ❓ capture pending |
| ?   | Return to dock | ❓ capture pending (note: rejected with FF13 while out of bounds, but the command is still emitted) |

### setGetParam `cmdID` values (moduleType 37)

| cmdID | Param | Notes |
|------:|-------|-------|
| 1     | Speech volume | `data` = 0-100 |

## Robot state codes (`normalBleHeartBeat.robotState`)

Separate namespace from error codes.

| Hex   | Label | Notes |
|-------|-------|-------|
| 0x402 | Paused | observed live (iOS "Pause") |
| 0x500 | Idle | |
| 0x501 | Charging | low-battery charging on dock |
| 0x502 | Mowing | |
| 0x503 | Returning | |
| 0x504 | Charging | mid-cycle |
| 0x505 | Mapping | |
| 0x506 | Paused | (earlier guess) |
| 0x507 | Error | |
| 0x801 | Charging | docked top-up at full |

NOTE: `normalBleHeartBeat.robotLowerr` MIRRORS `robotState` — it is NOT an error code.

## Error codes (`normalBleHeartBeat.errorCode.0..N`, array)

App displays each as 4-digit hex. Recently-cleared codes move to
`errorCodeDelete.N`.

| Hex    | App  | Meaning |
|--------|------|---------|
| 0x300  | 0300 | Failed to exit charging station |
| 0x500  | 0500 | Emergency stop — red button pressed |
| 0x502  | 0502 | Lifted from ground — safety system activated |
| 0x503  | 0503 | Out of bounds |
| 0x600  | 0600 | Stuck — move from obstacle, then Resume |
| 0x700  | 0700 | Low battery — charge to 90% before working |
| 0x701  | 0701 | Work suspended — rain detected |
| 0xFF0E | FF0E | Unable to begin work — move to charging station and retry |
| 0xFF13 | FF13 | Unable to return to base station |
| 0xFF28 | FF28 | Weak satellite signal — wait for blue tail light or rescan |

## Notes / open questions

- **No remote power-off** appears to exist (checked Work settings + Mover
  settings menus). Mowers generally can't power off remotely since nothing
  could wake them. BMS low-voltage cutoff protects the cells if left to drain.
- **Manual joystick drive is Bluetooth-only** in the app — its command never
  hits the cloud bus, so it can't be captured via `/blade_obs`. Whether
  `controlCmd` drive vectors (`x`/`y`) are accepted over MQTT/4G is untested.
- `signalInfo.baseLat/baseLng` ship a placeholder (~3.04, 3.05) until the RTK
  base publishes a real fix — sanity-check against robot position before use.
