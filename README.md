# BenTunnel

> Exposez votre `localhost` au monde via une URL publique permanente.
> Alternative gratuite à [localhost.run](https://localhost.run) et [ngrok](https://ngrok.com).

![architecture](docs/architecture-overview.png)

## Comment ça marche ?

```
Visiteur → https://bentunnel.onrender.com/tunnels/alice
                          │
                          ▼
        ┌──────────────────────────────────┐
        │   Serveur Render (FastAPI)        │
        │   - Reverse proxy HTTP            │
        │   - WebSocket relay               │
        │   - Dashboard web + admin         │
        └────────┬─────────────┬────────────┘
                 │             │
                 │ WSS         │ MySQL distant (TLS)
                 ▼             ▼
   ┌──────────────────────┐   ┌─────────────────────────┐
   │  Votre PC (Python)   │   │  Alwaysdata (MySQL)      │
   │  bentunnel.py        │   │  - users                 │
   │  → localhost:3000    │   │  - tunnels               │
   └──────────────────────┘   │  - sessions, audit_logs  │
                              └─────────────────────────┘
```

1. Vous créez un compte sur BenTunnel (dashboard web).
2. Vous installez le client Python sur votre PC.
3. Vous lancez `python bentunnel.py login` puis `python bentunnel.py start 3000`.
4. Votre application `localhost:3000` est maintenant exposée à `https://bentunnel.onrender.com/tunnels/<votre-id>` — une URL **permanente**.
5. Vous partagez cette URL. Tant que le client tourne, n'importe qui peut y accéder.

## Composants

| Dossier | Rôle | Hébergement |
|---|---|---|
| `relay/` | Serveur FastAPI (HTTP + WS + UI + admin) | **Render Free** |
| `client/` | CLI Python (login, start, stop, status) | **Votre PC** (Windows/Linux/macOS) |
| `tests/` | Tests pytest | Local |
| `docs/` | Documentation | — |

La **base de données MySQL** est hébergée sur **Alwaysdata Free** — c'est la seule chose sur Alwaysdata. Aucune page PHP/HTML n'est servie par Alwaysdata.

## Démarrage rapide (utilisateur final)

> Pré-requis : un compte BenTunnel a déjà été créé sur le dashboard, et le serveur est déployé.

```powershell
# 1. Installer Python 3.10+ (https://python.org)
python --version

# 2. Installer les dépendances du client
cd client
pip install -r requirements.txt

# 3. Se connecter
python bentunnel.py login
# > Email: alice@example.com
# > Password: ********
# ✓ Connecté. Tunnel: alice
# ✓ URL publique: https://bentunnel.onrender.com/tunnels/alice

# 4. Démarrer le tunnel sur le port 3000
python bentunnel.py start 3000
# ✓ Tunnel démarré
# URL publique: https://bentunnel.onrender.com/tunnels/alice
# Forward vers http://127.0.0.1:3000
# Ctrl+C pour arrêter

# 5. (autre terminal) Voir le statut
python bentunnel.py status

# 6. Arrêter
# Dans le terminal où tourne `start` : Ctrl+C
# Ou depuis un autre terminal :
python bentunnel.py stop
```

## Déploiement (admin)

Voir la documentation détaillée :

- [`docs/DEPLOYMENT_ALWAYSDATA.md`](docs/DEPLOYMENT_ALWAYSDATA.md) — Créer la base MySQL sur Alwaysdata Free.
- [`docs/DEPLOYMENT_RENDER.md`](docs/DEPLOYMENT_RENDER.md) — Déployer le serveur sur Render Free.
- [`docs/CLIENT_WINDOWS.md`](docs/CLIENT_WINDOWS.md) — Installer et utiliser le client sous Windows.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — Architecture détaillée et contraintes techniques.
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — Spécification du protocole WebSocket et de l'API.

## Sécurité

- **Mots de passe** hashés avec Argon2id (time_cost=3, memory_cost=64 MB, parallelism=4).
- **Sessions** stockées en DB (révocables), cookies `HttpOnly` + `Secure` + `SameSite=Lax`.
- **CSRF** : double-submit cookie sur tous les POST/PUT/DELETE authentifiés.
- **Tokens tunnel** : JWT HS256, expiration 5 min, `jti` révocable individuellement + invalidation globale par user via `tokens_invalidated_at`.
- **Anti-SSRF** : le client Python ne forward **que** vers `127.0.0.1:PORT`. Les headers `Host`, `X-Forwarded-*`, `X-Real-IP` sont strippés avant forwarding.
- **Isolation** : un tunnel appartient à un user ; le JWT est vérifié (signature + ownership + suspension + révocation) à chaque connexion WS. Le `tunnel_id` de la query string est comparé au `tunnel_id` du JWT — mismatch = rejet.
- **HTTPS/WSS obligatoires** en production (Render fournit le TLS automatiquement).
- **Rate limit** : 5 tentatives de login par email par 5 minutes.

## Limites connues (Render Free)

| Limite | Impact | Mitigation |
|---|---|---|
| Mise en veille après 15 min d'inactivité | Premier visiteur après inactivité attend ~1 min | Cron externe toutes les 10 min hit `/health` (voir doc Render) |
| Filesystem éphémère | Pas de fichiers locaux | Tout est en DB Alwaysdata |
| 750 h/instance/mois | Si succès, suspension possible | Surveiller l'usage |
| Bande passante mensuelle | Suspension si dépassé sans moyen de paiement | Limites `MAX_REQUEST_BYTES` / `MAX_RESPONSE_BYTES` (5 Mo) |

## Tests

```bash
# Installer les dépendances de test
pip install -r tests/requirements.txt
pip install -r relay/requirements.txt

# Lancer les tests
pytest -v
```

## Licence

MIT — voir `LICENSE`.

## Roadmap

- [x] v1.0 — Tunnels HTTP, dashboard, admin, isolation, sécurité complète
- [ ] v1.1 — Stats bande passante par tunnel
- [ ] v1.2 — Multi-tunnels par utilisateur
- [ ] v1.3 — Webhooks (notification de connexion/déconnexion)
