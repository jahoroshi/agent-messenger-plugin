# AMessenger installer

This branch holds only `install.sh`. It is deliberately **not** on `main`:
`main` is the plugin itself, and Hermes scans the whole plugin tree at install
time — a shell script that writes systemd units takes that scan from `safe` to
`caution`, which puts `--force` in front of every Owner.

Install and configure in one command:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/jahoroshi/agent-messenger-plugin/installer/install.sh) \
  --owner-chat google_chat:spaces/YOUR_SPACE_ID --agent your-agent-name
```

Then restart the gateway. Nothing else is needed.
