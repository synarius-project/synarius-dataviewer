Diagnostics and crash logs
==========================

Synarius GUI applications share a small library, ``synarius_apps_diagnostics``, for file logging,
uncaught-exception handling, Python ``faulthandler`` output (enabled by default; see below), and
routing Qt messages into Python's ``logging`` system.

Log file location
-----------------

Log directories are chosen with `platformdirs <https://pypi.org/project/platformdirs/>`_ (user log
dir per application name). Typical layout:

* **Windows:** under ``%LOCALAPPDATA%``, e.g. ``Synarius\<AppName>\Logs\``.
* **macOS:** ``~/Library/Logs/<AppName>/``.
* **Linux:** ``~/.local/share/<AppName>/logs/`` (fallbacks apply if ``platformdirs`` is missing).

Each app writes a dedicated rotating log file (5 MiB × 10 files, UTF-8):

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Application
     - Log file name
     - Uncaught-exception logger
   * - Synarius Studio
     - ``synarius-studio.log``
     - ``synarius_studio.uncaught``
   * - ParaWiz
     - ``synarius-parawiz.log``
     - ``synarius_parawiz.uncaught``
   * - DataViewer
     - ``synarius-dataviewer.log``
     - ``synarius_dataviewer.uncaught``

On startup, the process prints the resolved log path to **stderr** (after ``--version`` handling).

Session marker
--------------

After file logging is configured, apps log one **INFO** line with logger names such as
``synarius_parawiz.bootstrap`` or ``synarius_dataviewer.bootstrap`` containing:

* application name and version
* process id, Python version, platform
* path to the main log file

Search the log for ``session_start`` to find process boundaries.

Environment variables
---------------------

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Variable
     - Effect
   * - ``SYNARIUS_LOG_DEBUG``
     - If set to ``1``, ``true``, ``yes``, or ``on``: root/file log level **DEBUG** (all apps that use the shared helper).
   * - ``SYNARIUS_STUDIO_LOG_DEBUG``
     - Studio-specific override (same truthy values).
   * - ``SYNARIUS_PARAWIZ_LOG_DEBUG``
     - ParaWiz-specific override.
   * - ``SYNARIUS_DATAVIEWER_LOG_DEBUG``
     - DataViewer-specific override.
   * - ``SYNARIUS_FAULT_HANDLER``
     - **Default:** ``faulthandler`` is **on** and writes to the main log file (helps with hangs / native crashes). Set to ``0``, ``false``, ``no``, or ``off`` to **disable** it explicitly.

Qt messages
-----------

After ``QApplication`` is constructed, apps call ``install_qt_message_handler()`` so Qt
``qDebug`` / ``qWarning`` / etc. are recorded under the Python logger ``qt``.

Developer setup (monorepo)
--------------------------

Studio depends on ``synarius-apps`` for ``synarius_apps_diagnostics``. In a checkout, install the
apps package editable into the same virtual environment as Studio, for example:

.. code-block:: bash

   pip install -e path/to/synarius-apps

API reference (import)
----------------------

.. code-block:: python

   from synarius_apps_diagnostics import (
       configure_file_logging,
       install_qt_message_handler,
       log_directory_for_app,
       log_session_start,
       main_log_path,
   )

Call ``configure_file_logging`` **before** creating ``QApplication``; call
``install_qt_message_handler`` **after** ``QApplication`` exists.
