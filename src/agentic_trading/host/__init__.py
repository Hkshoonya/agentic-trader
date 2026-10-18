"""The Windows host: what systemd ``--user`` does on Linux, done by one supervisor process.

``units`` names the same services and timers as ``deploy/*.service`` and ``deploy/*.timer``;
``supervisor`` runs them; ``client`` is how the upgrader and the operator talk to it.
"""
