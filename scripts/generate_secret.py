#!/usr/bin/env python3
"""Génère un secret aléatoire pour BEN_TUNNEL_SECRET.

Usage :
    python scripts/generate_secret.py

Copier la sortie dans la variable d'environnement BEN_TUNNEL_SECRET
sur Render (ou dans votre .env local).
"""
import secrets


def main() -> None:
    secret = secrets.token_hex(32)  # 64 chars hex = 256 bits d'entropie
    print("# Ajoutez ceci à vos variables d'environnement Render :")
    print(f"BEN_TUNNEL_SECRET={secret}")
    print()
    print("# Ou dans un fichier .env local :")
    print(f"BEN_TUNNEL_SECRET={secret}")


if __name__ == "__main__":
    main()
