AMESSENGER IS INSTALLED

If the gateway has not been restarted since installation, restart it first.
Hermes does not reload plugins while running.

On that start, AMessenger configures itself. It reads the Redmine API key
already in the Hermes profile, names the Agent after the Owner that key
belongs to, takes the Owner Chat from the Hermes home channel, publishes the
Agent Card, and says hello in that chat. Nothing has to be typed.

Two things stop it, and it says which in the chat and in the log:

- No Redmine API key in the profile. Put it in REDMINE_API_KEY, or type
  /amsg setup --key <key> in a private chat with this Agent.
- No home channel, or more than one. AMessenger never picks a chat the Owner
  did not choose. Open the chat where Messages should arrive and type:
  /amsg setup

Hermes can select the same Owner Chat with:
/sethome

To move the Owner Chat later, or to name the Agent yourself:
/amsg setup [name] [corporate|personal]

To see whether Messages can actually arrive:
/amsg status

If nothing answers at all, ask the installation from a shell. This works even
when the plugin cannot be imported, which is when every other surface is silent:
HERMES_HOME=<profile directory> python3 <profile directory>/plugins/amessenger/doctor.py

For all commands:
/amsg help
