Configuration directory
=======================

This directory contains environment-scoped settings and resource files.

* ``settings.py`` and the ``environments/`` examples describe supported keys.
* ``user_agents.txt`` is a non-secret resource.
* ``password.txt`` and ``5sim_config.txt`` (when present for legacy imports)
  are sensitive credentials.  They must stay local, use restrictive file
  permissions, and must never be copied into ordinary configuration output,
  task logs, Telegram notifications, browser pages, or account exports.

The active environment stores its effective ``.env`` under
``runtime/<mode>/.env``.  Secret values are read only by supervised workers;
the Web API and all default exports are metadata-only.
