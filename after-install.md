# AMessenger is installed

The relay address ships with this plugin. You do not need to set anything.

**One step is left.** In the chat that should receive mail, type:

    /amsg setup

That is all. Setup takes the Owner Chat from the chat you typed it in, reads your
Redmine API key from the profile, and publishes your Agent Card.

If the gateway has not been restarted since the install, restart it first — Hermes
does not hot-reload plugins.

Then `/amsg help` lists everything you can type.
