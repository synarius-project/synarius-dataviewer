"""Windows-only identifiers without Qt imports (must load before PySide6 for taskbar grouping)."""

# SetCurrentProcessExplicitAppUserModelID: Microsoft empfiehlt Company.Product.SubProduct.Version.
# Zu kurze IDs (z. B. nur zwei Segmente) werden von der Shell teils ignoriert — dann bleibt das python.exe-Icon.
PARAWIZ_APP_USER_MODEL_ID = "Synarius.SynariusApps.ParaWiz.1.0"
