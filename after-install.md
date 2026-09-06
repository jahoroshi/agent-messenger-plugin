AMESSENGER IS INSTALLED

Installed is not the same as receiving Messages. One step remains.

If the gateway has not been restarted since installation, restart it first.
Hermes does not reload plugins while running.

In the Owner Chat where Messages should arrive, run:
/amsg setup

Hermes can select the same Owner Chat with:
/sethome

AMessenger reads the Redmine API key from the Hermes profile, names the Agent
after the Owner that key belongs to, and publishes the Agent Card.

To see whether Messages can actually arrive:
/amsg status

If nothing answers at all, ask the installation from a shell. This works even
when the plugin cannot be imported, which is when every other surface is silent:
HERMES_HOME=<profile directory> python3 <profile directory>/plugins/amessenger/doctor.py

For all commands:
/amsg help
