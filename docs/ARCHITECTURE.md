# Architecture BenTunnel

## Vue d'ensemble

BenTunnel est composé de **3 briques** :

1. **Le serveur Render** (`relay/`) — application FastAPI unique qui héberge le site web, l'API REST, le serveur WebSocket, et le reverse proxy HTTP.
2. **Le client Python** (`client/`) — CLI installé sur le PC de l'utilisateur, qui ouvre une WebSocket vers Render et forward les requêtes vers `localhost:PORT`.
3. **La base MySQL** — hébergée sur Alwaysdata Free, accédée à distance par le serveur Render. C'est le **seul** composant sur Alwaysdata.

```
┌────────────────────────────────────────────────────────────────────┐
│                       VISITEUR (navigateur)                         │
│   https://bentunnel.onrender.com/tunnels/alice                     │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ HTTPS
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│  RENDER (Free Web Service, 1 instance, async)                      │
│                                                                     │
│  FastAPI app (relay/main.py) qui gère :                             │
│   ├─ Pages web (Jinja2) — /, /login, /register, /dashboard, /admin  │
│   ├─ API REST JSON — /api/auth/*, /api/tunnels/*, /api/admin/*      │
│   ├─ WebSocket — /ws (auth JWT, heartbeat, binaire)                 │
│   ├─ Reverse proxy — /tunnels/{username} (GET/POST/PUT/DELETE/…)    │
│   └─ Healthcheck — /health (pour cron keep-alive)                   │
│                                                                     │
│  Pool de connexions MySQL → Alwaysdata (SQLAlchemy async + asyncmy) │
│  pool_size=5, max_overflow=10, pool_pre_ping=True                   │
│                                                                     │
│  État en RAM (volatile, perdu au spin-down) :                       │
│   - Connexions WS actives (ws_manager.tunnels)                      │
│   - Futures des requêtes HTTP en cours (ws_manager.pending_requests)│
│   - Rate limiter login (in-process)                                 │
│                                                                     │
│  État persistant (DB Alwaysdata) :                                  │
│   - users, tunnels, sessions, revoked_tokens, audit_logs            │
└──────────────┬──────────────────────────────────┬──────────────────┘
               │                                  │
               │ WSS (JWT, 5 min)                 │ MySQL distant (TLS)
               ▼                                  ▼
┌──────────────────────────────┐   ┌─────────────────────────────────┐
│  CLIENT PYTHON (PC user)     │   │  ALWAYSDATA Free — MySQL         │
│  bentunnel.py                │   │  mysql-moncompte.alwaysdata.net  │
│   ├─ login (HTTP API)        │   │  Port 3306                       │
│   ├─ start PORT              │   │  40 connexions max par user      │
│   │   ├─ fetch_tunnel_token  │   │  Tables :                        │
│   │   ├─ connect WSS         │   │   - users (id, email, pass_hash, │
│   │   ├─ PING 60s (anti-sleep)│  │     is_admin, is_suspended,      │
│   │   ├─ HTTP_REQUEST →      │   │     tokens_invalidated_at)       │
│   │   │   forward_to_local   │   │   - tunnels (tunnel_id, user_id, │
│   │   │   → 127.0.0.1:PORT   │   │     local_port, is_active,       │
│   │   └─ HTTP_RESPONSE ←     │   │     last_seen_at)                │
│   ├─ stop (PID file)         │   │   - sessions (session_token,     │
│   └─ status                  │   │     csrf_token, expires_at)      │
│                              │   │   - revoked_tokens (jti, exp)    │
│  Config : ~/.bentunnel/      │   │   - audit_logs (action, ip, ts)  │
│   ├─ config.json (cookies,   │   └─────────────────────────────────┘
│   │  tunnel_id, public_url)  │
│   └─ tunnel.pid              │
└──────────────────────────────┘
```

## Flux : un visiteur ouvre /tunnels/alice

```
1. Visiteur → GET https://bentunnel.onrender.com/tunnels/alice
2. Render FastAPI route /tunnels/{username} → http_proxy.forward_request()
3. http_proxy :
   a. SELECT tunnel FROM tunnels WHERE tunnel_id = 'alice'
      → 404 si introuvable
   b. ws_manager.is_connected('alice') ?
      → Non  → renvoie page HTML "Tunnel hors ligne" (503)
      → Oui → continue
4. http_proxy.genère request_id = uuid4()
5. ws_manager.send_request('alice', method, path, qs, headers, body)
   a. Construit le message HTTP_REQUEST {method, path, query_string, headers, body_b64, body_size}
   b. Si body > 4 KB : body_b64=null, le body est envoyé comme binary frame WS juste après le JSON
   c. Crée un asyncio.Future, l'enregistre dans pending_requests[request_id]
   d. Envoie le message au client via sa WS
   e. Attend le Future avec timeout=30 s
6. Côté client (bentunnel.py) :
   a. Reçoit HTTP_REQUEST
   b. Spawn une task qui appelle forward_to_local(3000, method, path, qs, headers, body)
      → POST/GET/etc. vers http://127.0.0.1:3000/path?qs
   c. Récupère (status_code, headers, body)
   d. Envoie HTTP_RESPONSE {status_code, headers, body_size, has_body, body_b64?}
      Si body > 4 KB : body_b64=null, le body est envoyé comme binary frame juste après
7. Côté serveur :
   a. ws_manager.handle_text_message reçoit HTTP_RESPONSE
   b. Si body_b64 présent → résout le Future immédiatement
   c. Sinon → attend la binary frame suivante, puis résout
8. http_proxy récupère le résultat, construit une Response Starlette avec status, headers, body
9. Renvoie au visiteur
```

## Flux : authentification WebSocket

```
1. bentunnel.py start 3000
2. Charge ~/.bentunnel/config.json (session_cookie + csrf_token + tunnel_id)
3. POST /api/tunnels/token (avec cookies + X-CSRF-Token)
   → serveur renvoie {token: <JWT 5min>, expires_in, ws_url}
4. websockets.connect("wss://bentunnel.onrender.com/ws?tunnel_id=alice&token=<JWT>")
5. Serveur /ws endpoint :
   a. verify_tunnel_token(token, db)
      - Décode le JWT (signature HMAC, exp)
      - Vérifie jti pas dans revoked_tokens
      - Vérifie user existe + pas suspendu
      - Vérifie iat >= user.tokens_invalidated_at
      - Vérifie tunnel appartient à user (tunnel_id + user_id match)
      → Si KO : close WS code 1008
   b. Vérifie tunnel.tunnel_id == query.tunnel_id (anti-SSRF)
      → Si mismatch : close WS code 1008
   c. ws_manager.connect(ws, tunnel_id, user_id)
      - Accepte la WS
      - Enregistre dans tunnels[tunnel_id]
      - UPDATE tunnels SET is_active=1, last_seen_at=NOW()
      - Envoie CLIENT_CONNECTED {tunnel_id, server_time, max_request_bytes, max_response_bytes}
      - Démarre la boucle PING (30 s)
6. Client envoie CLIENT_CONNECT {client_version, local_port}
7. Boucle :
   - Client PING serveur toutes les 60 s → serveur répond PONG (compte comme trafic entrant → empêche spin-down Render)
   - Serveur PING client toutes les 30 s → client doit PONG sous 10 s sinon déconnexion (code 4001)
8. Sur déconnexion (Ctrl+C, crash, ping timeout, admin suspend) :
   - ws_manager.disconnect(tunnel_id, reason)
   - UPDATE tunnels SET is_active=0
   - Annule les Futures des requêtes en cours avec 502
```

## Schéma de données

Voir [`PROTOCOL.md`](PROTOCOL.md) §7 pour le schéma SQL complet.

Tables :
- **users** : id, email, password_hash (Argon2id), first_name, is_admin, is_suspended, tokens_invalidated_at, created_at
- **tunnels** : id, user_id (FK), tunnel_id (unique, slug), local_port, is_active, last_seen_at, created_at
- **sessions** : id, user_id (FK), session_token (hex 64), csrf_token (hex 64), expires_at, ip_address, user_agent
- **revoked_tokens** : jti (PK), user_id (FK), revoked_at, expires_at
- **audit_logs** : id, user_id (FK nullable), action, ip_address, details, created_at

## Contraintes Render Free prises en compte

| Contrainte | Mitigation |
|---|---|
| Spin-down après 15 min d'inactivité | Le client envoie PING toutes les 60 s → message WS entrant → pas de sleep |
| Cold start ~1 min | Cron externe (cron-job.org) hit `/health` toutes les 10 min en plus |
| Filesystem éphémère | Aucun fichier écrit sur Render. Sessions/tokens/audit en DB. |
| 1 seule instance | Tout l'état WS en RAM (single-instance → pas de partage nécessaire) |
| 750 h/instance/mois | Le cron keep-alive consomme ces heures ; le PING client aussi quand tunnel actif |
| Pas de SSH | Debug via logs Render uniquement |
| SMTP bloqué (ports 25/465/587) | Pas d'envoi d'email prévu ; reset MDP par admin uniquement |

## Contraintes Alwaysdata Free prises en compte

| Contrainte | Mitigation |
|---|---|
| 40 connexions MySQL max par user | Pool SQLAlchemy `pool_size=5, max_overflow=10` = 15 max |
| Accès distant MySQL | Configuré nativement, IP whitelist via user MySQL |
| Pas de processus persistants | On n'y met QUE la DB, rien d'autre |
| Stockage limité (~100 Mo) | Schema BenTunnel très léger (~1 Ko par user) |

## Décisions techniques

| Choix | Alternative envisagée | Raison du choix |
|---|---|---|
| FastAPI | Flask, Django | Async natif pour WS, léger, moderne |
| SQLAlchemy 2.x async + asyncmy | aiomysql, SQLAlchemy sync | Pool natif, asyncmy plus maintenu |
| Argon2id | bcrypt, scrypt | Plus moderne, recommandé OWASP |
| JWT court (5 min) pour WS | Session longue pour WS | Révocable rapidement, jti individuel |
| Sessions en DB | Sessions JWT stateless | Révocables individuellement |
| Binaire WS pour corps > 4 KB | Base64 systématique | Moins de CPU, moins de bande passante |
| Cron externe (cron-job.org) | Self-ping interne | Render ne peut pas scheduler de tâches |
| Jinja2 | HTML statique + fetch | SSR plus simple pour dashboard |

## Sécurité

Voir `README.md` section Sécurité. Points clés :

- **Anti-SSRF client** : `http_forwarder.forward_to_local` ne construit l'URL qu'avec `127.0.0.1:PORT`. Le header `Host` envoyé par le serveur est ignoré (le client le fixe lui-même à `127.0.0.1:PORT`).
- **Anti-SSRF serveur** : `http_proxy.forward_request` strip les headers `Host`, `X-Forwarded-*`, `X-Real-IP`, `Connection`, `Content-Length`, `Transfer-Encoding` avant de les envoyer au client.
- **Isolation** : `verify_tunnel_token` fait `SELECT tunnel FROM tunnels WHERE tunnel_id = ? AND user_id = ?` — un user ne peut pas accéder au tunnel d'un autre.
- **Pas de token dans URL publique** : les URL `/tunnels/{username}` ne contiennent jamais de token. Le token JWT n'apparaît que dans la query string de la WS `/ws?...`, qui est une connexion privée client→serveur.
