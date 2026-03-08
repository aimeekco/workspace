# Workspace TUI Notes

## Overview
- App entrypoint: `gws_tui`
- UI framework: Textual
- Transport boundary: `gws` CLI
- Architecture: one shared app shell with module-specific views and module-specific `gws` adapters

## Module Order
- `1` Gmail
- `2` Calendar
- `3` Tasks
- `4` Drive
- `5` Sheets
- `6` Docs

Keep this order aligned with:
- `gws_tui/modules/__init__.py`
- number-key switching in `gws_tui/app.py`
- any user-facing docs or screenshots

## Profiles
- In-app profile switching is supported with `p`
- Local profile mapping is stored in `.gws_tui_profiles.json`
- Profile switching updates the `gws` config dir and resets module/view state
- Do not commit machine-specific profile mappings

## Contextual Keys
- `a`
  - Calendar: create event
  - Tasks: create task
- `x`
  - Tasks: toggle complete / incomplete
- `c`
  - Gmail: compose
- `d`
  - Gmail: trash
  - Calendar: delete event from selected day
- `e`
  - Gmail: reply
- `Shift+E`
  - Gmail: reply all
- `f`
  - Gmail: forward
- `l`
  - Gmail: edit custom labels
- `n`
  - Docs: create doc
- `w`
  - Docs / Sheets: edit
- `p`
  - switch profiles
- `/`
  - Gmail search
- `u`
  - Gmail unread filter
- `[` and `]`
  - Calendar month navigation

## Module Notes

### Gmail
- Uses a dedicated three-pane view
- Supports mailbox filtering, search, drafts, reply, reply-all, forward, labels, attachments
- Opening an unread message marks it read

### Calendar
- Uses a dedicated month grid view
- Supports event creation and deletion
- Create-event defaults should respect the selected day and writable calendars

### Tasks
- Uses a dedicated three-pane view
- Supports task-list filtering, task creation, and complete/uncomplete
- Due date input is `YYYY-MM-DD`

### Drive
- Defaults to `My Drive`
- Folder navigation is handled in-module and in the Drive-specific view

### Sheets
- Editing is currently a large text-based aligned grid, not a native cell grid

### Docs
- Editing currently replaces plain-text body content, not rich formatting

## Testing
- Primary verification command:
  - `python3 -m unittest discover -s tests -v`

When changing module order, profile behavior, or shared view code, run the full suite.
