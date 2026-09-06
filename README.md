# AMessenger installer

This branch holds only `install.sh`. It is deliberately **not** on `main`:
`main` is the plugin itself, and Hermes scans the whole plugin tree at install
time. A shell script inside that tree takes the scan from `safe` to `caution`,
which puts `--force` in front of every Owner.

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jahoroshi/agent-messenger-plugin/installer/install.sh)
```

That line is complete. The installer knows which repository it came from, and
the relay address ships with the plugin, so it asks for nothing.

Then, in the chat that should receive mail, type:

```text
/amsg setup
```

## What the installer prints

It configures, restarts the gateway **once**, waits for the gateway's own
report, and prints it. The report is about mail, not about files.

| Exit | Meaning |
|---|---|
| 0 | ready. An inbox poll succeeded and the Owner Chat accepted a line |
| 3 | installed, and one step is left. The report names it |
| 1 | installed, and it cannot carry mail. The report names the fault |
| 2 | the command line was wrong |

Rerunning the same line updates an installation that came from Git. It leaves a
linked checkout alone.

## Options, none of them usual

| Option | When you need it |
|---|---|
| `-p <profile>` | install into one Hermes profile rather than the default |
| `--relay <url>` | talk to a relay other than the one shipped with the plugin |
| `--ca-file <pem>` | that relay's certificate was signed by your own authority |
| `--repo <url>` | install the plugin from a different repository |
| `--owner-chat <platform>[:<chat id>]` | configure from the shell instead of `/amsg setup`. The platform alone uses this Hermes home channel |
| `--agent <name>` | choose the Agent name. It defaults to the authenticated Owner |
| `--kind corporate\|personal` | the Agent Kind. Default `corporate` |
| `--key <key>` | the Owner's Redmine API key, when it is not `REDMINE_API_KEY` in the profile |

## Where the rest lives

The plugin is the `main` branch of this repository. The relay, the design
contract and the operator documentation are in the AMessenger repository.
