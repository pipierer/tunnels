"""Configuration pytest commune."""
import sys
from pathlib import Path

# Ajoute la racine du projet au sys.path pour pouvoir importer relay.*
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Variables d'env factices pour que Settings puisse se construire.
# On FORCE (pas setdefault) car l'env sandbox peut déjà avoir DATABASE_URL.
import os
os.environ["DATABASE_URL"] = "mysql+asyncmy://test:test@127.0.0.1:3306/test"
os.environ["BEN_TUNNEL_SECRET"] = "test-secret-key-for-pytest-only-not-production-64chars"
os.environ["ADMIN_EMAIL"] = "admin@example.com"
os.environ["ADMIN_BOOTSTRAP_PASSWORD"] = "TestAdminPass123!"
os.environ["ADMIN_BOOTSTRAP_FIRST_NAME"] = "Admin"
os.environ["PUBLIC_BASE_URL"] = "https://bentunnel.test"
