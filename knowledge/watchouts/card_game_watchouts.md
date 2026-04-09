# Card Game — Watchouts

## Card Must Show Card Number
- **Missed:** Cards rendered without any unique identifier — can't tell cards apart
- **Rule:** Every card must display its card number/ID visibly on the card face
- **Fix:** Add card_no label to CardVisual, display CardData.id or sequential number

## Right-Click Shows Card Info
- **Missed:** No way to inspect card details — player can't read abilities or full text
- **Rule:** Right-clicking any card must show a detail panel on the right side of screen with full card info (name, type, stats, description, cost, abilities)
- **Fix:** Add info panel to board UI, handle right-click on CardVisual, populate panel with card_data fields

## Card Hand Must Be Readable
- **Missed:** Too many cards overlap, text too small to read
- **Rule:** Cards in hand must be spaced enough to see at least the card name and cost
- **Fix:** Scroll or fan cards if hand exceeds container width, minimum card width enforced

## Graveyard Must Be Viewable
- **Missed:** Cards sent to graveyard disappear — player can't review what's been destroyed
- **Rule:** Graveyard zone must be clickable to show list of destroyed cards
- **Fix:** Add graveyard button per player, opens scrollable card list overlay
