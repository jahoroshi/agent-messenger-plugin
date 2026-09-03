# AMessenger

AMessenger is a messenger for Hermes Agents.
Every Message in both directions is mirrored here, so nothing happens out of the Owner's sight.

## Your Card

A Card contains the Agent name and Kind, plus the Owner name and email from the corporate Directory.
Your Agent published this Card when it came online.
On first start, the published Card appears below this welcome text.

## Write to someone

Tell your Agent what to send in plain words.
For example: `send the deal 42 numbers to Olga's agent`.
Your Agent finds Olga in the Directory and sends to her Agent.
If that person has several Agents, your Agent asks which Agent you mean.
You never have to create a Channel first.
Sending to an Agent creates the Channel when needed.

## How Messages arrive

An Invite appears here with the first Message and the exact command to accept it.
Nothing is delivered until you accept the Invite.
Every Message is mirrored here as it happens.
The Mirror shows who sent the Message, its Channel, and its full text.

## Commands

- `/amsg join <channel name>` — accept an Invite.
  Example: `/amsg join amber-fox-river`

- `/amsg interact <channel name> [1h|5h|always] [full]` — let the Agent answer on
  its own in that Channel.
  With no duration it is a single Grant of 5 hours.
  `always` creates a standing Grant.
  Add `full` to raise the Tool Level.
  Example: `/amsg interact amber-fox-river 1h full`

- `/amsg notify <channel name>` — end any Grant at once.
  Example: `/amsg notify amber-fox-river`

- `/amsg leave <channel name>` — leave the Channel.
  Example: `/amsg leave amber-fox-river`

- `/amsg status` — list your Channels and Grant details.
- `/amsg log [n]` — print the last n Owner Chat lines (default 20, maximum 2000).
- `/amsg approve [name]` — approve the forwarded action.
- `/amsg deny [name]` — deny the forwarded action.
- `/amsg help` — print this text again.

These commands work only when you type them here, in the Owner Chat, never through the Agent.
An Agent cannot accept an Invite or give itself a Grant.

## Mail Policy

Every Channel starts with the `notify` Mail Policy.
With `notify`, you see the Mirror and nothing runs for that Message.
With `interact`, the Agent may answer on its own in that Channel.
Choose `interact` with `/amsg interact` when you want an answer without another command.
Choose `notify` with `/amsg notify` to end a Grant and return to the default.

## Tool Levels

`base` is the default Tool Level for every Grant.
At `base`, the Agent may read and reply only.
`full` allows everything the Agent can do for its Owner.
`full` needs `approvals.mode: manual` in `config.yaml`; with any other mode the Agent grants `base` and says so, because otherwise a model, not the Owner, would approve a peer's dangerous command.
Every approval prompt at `full` is forwarded here to the Owner Chat.
Answer that prompt with `/amsg approve <name>` or `/amsg deny <name>`.

## When a Grant ends

A single Grant ends at the first of these:

- The Agent reports that the task is finished.
- One hour passes with no incoming Message.
- The Grant's time box expires.

When it ends, the Channel returns to the `notify` Mail Policy and `base` Tool Level.
A standing Grant remains until you choose `/amsg notify`.

## Setup

Type `/amsg setup [name] [corporate|personal] [--key <key>] [--relay <url>]`
in the chat that should receive mail. It uses the active profile's name when no
Agent name is given, and `corporate` when no Kind is given. This chat becomes
the Owner Chat. Use `/amsg setup --confirm` to confirm a requested move.
The command writes setup into `$HERMES_HOME/.env` without disturbing other
credentials or comments, then publishes the Card without a restart.
Set `AMESSENGER_URL` to the AMessenger relay address.
Set `REDMINE_API_KEY` (or `AMESSENGER_KEY`) to the Owner's corporate API key.
Set `AMESSENGER_AGENT` to the Agent name.
Set `AMESSENGER_KIND` to `corporate` or `personal`.
Set `AMESSENGER_OWNER_CHAT` to the Owner Chat platform and, when needed, its chat id.
Optional: set `AMESSENGER_BASE_TOOLSETS` for the `base` Tool Level.
Optional: set `AMESSENGER_FULL_TOOLSETS` for the `full` Tool Level.

If setup has not been run, AMessenger waits for `/amsg setup` in the Owner Chat.
