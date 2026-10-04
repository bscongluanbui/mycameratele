# Telegram menu emphasis

Parent menu buttons and camera choices use the native Bot API field
`style: "success"`, which gives them a green background. Camera choices retain
their existing full-width rows, names, recording counts and stable callbacks.

The styling applies to `/start` and Home, camera archive pages, preset time
windows, custom date ranges and the legacy date camera selector. Back, paging,
sort, individual video actions and bulk-download controls keep their default
style, so they remain visually separate from menu and camera choices.

No image buttons, Mini App, external player or custom emoji subscription is
needed. The pinned local Bot API server already supports this field. Telegram
clients render the colors; the command-menu button beside the message input is
client-owned and is not an inline parent-menu button.

Reference: [InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton).
