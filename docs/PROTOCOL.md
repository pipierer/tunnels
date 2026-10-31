# BenTunnel — Spécification du protocole & de l'API

Ce document est la **référence unique** partagée entre le serveur Render (FastAPI) et le client Python. Tout changement ici doit être répercuté des deux côtés.

## 1. URLs

| URL | Méthode | Usage |
|---|---|---|
| `POST /api/auth/register` | POST | Inscription {first_name, email, password} |
| `POST /api/auth/login` | POST | Connexion {email, password} → set-cookie `bentunnel_session` |
| `POST /api/auth/logout` | POST | Déconnexion |
| `GET  /api/me` | GET | Infos utilisateur courant |
| `GET  /api/tunnels/me` | GET | Tunnel de l'utilisateur courant (status + URL) |
| `POST /api/tunnels/token` | POST | Génère un JWT court (5 min) pour la connexion WS |
| `POST /api/tunnels/status` | POST | Met à jour le port local signalé par le client |
| `GET  /api/admin/users` | GET | (admin) Liste users |
| `POST /api/admin/users/{id}/suspend` | POST | (admin) Suspendre |
| `POST /api/admin/users/{id}/reset-password` | POST | (admin) Reset MDP |
| `POST /api/admin/tunnels/{id}/revoke-token` | POST | (admin) Révoque token actif |
| `DELETE /api/admin/tunnels/{id}` | DELETE | (admin) Supprime tunnel |
| `GET  /api/admin/tunnels` | GET | (admin) Tunnels actifs (connectés) |
| `GET  /api/admin/audit-logs` | GET | (admin) Logs audit |
| `GET  /health` | GET | Healthcheck (utilisé par cron keep-alive) |
| `WS   /ws?tunnel_id=...&token=...` | WS | Connexion client Python (token JWT court) |
| `GET  /tunnels/{username}` | ANY | Reverse proxy vers le client |

**Note sur l'auth WS** : Le token JWT n'est **jamais** dans une URL publique. Il n'apparaît que dans la query string de la WS `/ws?...`, qui est une connexion privée client→serveur. Les URL publiques `/tunnels/{username}` ne contiennent jamais de token.

## 2. Format des messages WebSocket

Tous les messages sont en **JSON text frames** pour le contrôle, et en **binary frames** pour les corps HTTP binaires. Le champ `type` indique le type de message.

### 2.1 Messages de contrôle (JSON text frames)

```json
{
  "type": "CLIENT_CONNECT",
  "request_id": null,
  "tunnel_id": "alice",
  "payload": { "client_version": "1.0.0", "local_port": 3000 }
}
```

Champs obligatoires : `type` (string), `request_id` (string|null), `tunnel_id` (string), `payload` (object).

### 2.2 Types de messages

| Type | Sens | Description | Payload |
|---|---|---|---|
| `CLIENT_CONNECT` | client→serveur | Premier message après ouverture WS | `{client_version, local_port}` |
| `CLIENT_CONNECTED` | serveur→client | Ack de connexion | `{tunnel_id, server_time, max_request_bytes, max_response_bytes}` |
| `HTTP_REQUEST` | serveur→client | Requête HTTP à forwarder | `{method, path, query_string, headers, body_b64, body_size}` |
| `HTTP_RESPONSE` | client→serveur | Réponse HTTP | `{status_code, headers, body_size, has_body}` |
| `HTTP_BODY_CHUNK` | bidirectionnel | Chunk de corps binaire | `{seq, total, is_last}` (frame binaire suit) |
| `PING` | serveur→client | Heartbeat serveur | `{ts}` |
| `PONG` | client→serveur | Réponse heartbeat | `{ts}` |
| `TUNNEL_STATUS` | serveur→client | Changement de statut | `{status: "online"\|"offline"\|"suspended"}` |
| `CLIENT_DISCONNECT` | bidirectionnel | Annonce déconnexion propre | `{reason}` |
| `ERROR` | bidirectionnel | Erreur | `{code, message}` |

### 2.3 Transport des corps binaires

Pour préserver les corps binaires (images, fichiers uploadés, etc.) :

1. Le `HTTP_REQUEST` indique `body_size` (int) et `body_b64` (string, **null si > 4 KB**).
2. Si `body_size > 4096` : `body_b64` est `null` et le corps est envoyé en **une seule binary frame** juste après le JSON text frame.
3. Pour les réponses : `HTTP_RESPONSE` indique `body_size` et `has_body` (bool). Si `has_body=true` et `body_size > 4096`, une binary frame suit.
4. Pour les très gros corps (> `MAX_REQUEST_BYTES` ou `MAX_RESPONSE_BYTES`), une erreur `ERROR` avec `code="PAYLOAD_TOO_LARGE"` est renvoyée et la requête est abandonnée.

### 2.4 Headers HTTP

Les headers sont transportés comme un objet JSON `{header_name: [value1, value2]}` (valeurs en listes pour gérer les headers multiples). Les noms d'headers sont **en minuscules**. Les headers `host`, `x-forwarded-for`, `x-forwarded-proto`, `x-real-ip` sont **strippés** par le serveur avant l'envoi au client (anti-SSRF) ; le client fixe lui-même `Host: 127.0.0.1:PORT`.

## 3. Authentification

### 3.1 Login web (navigateur)

`POST /api/auth/login` avec `{email, password}`. Le serveur :
1. Vérifie l'email et le hash Argon2id.
2. Crée une `session` en DB avec `session_token` (32 octets aléatoires, hex).
3. Set cookie `bentunnel_session` HttpOnly, Secure, SameSite=Lax, durée 7 jours.

### 3.2 CSRF

Toutes les requêtes `POST/PUT/DELETE` vers `/api/*` doivent inclure :
- Header `X-CSRF-Token` dont la valeur doit être égale au cookie `bentunnel_csrf` (double-submit cookie).
Le cookie CSRF est set au login (non-HttpOnly pour que JS puisse le lire).

### 3.3 Token tunnel (JWT court)

`POST /api/tunnels/token` (avec session web valide) renvoie :
```json
{ "token": "<jwt>", "expires_in": 300, "ws_url": "wss://bentunnel.onrender.com/ws" }
```

JWT (HS256, signé avec `BEN_TUNNEL_SECRET`) payload :
```json
{
  "sub": "<user_id>",
  "tunnel_id": "<tunnel_id>",
  "iat": 1730000000,
  "exp": 1730000300,
  "jti": "<uuid4>"
}
```

Le serveur vérifie au moment de la connexion WS :
1. JWT valide (signature + non expiré).
2. `jti` pas dans la table `revoked_tokens`.
3. User non suspendu.
4. Tunnel `tunnel_id` appartient bien à `user_id` et est actif.

## 4. Reverse proxy HTTP

Quand un visiteur ouvre `GET /tunnels/alice/...` :

1. Le serveur identifie `tunnel_id="alice"` (ou 404 si tunnel inexistant).
2. Vérifie que le client est connecté (WS active). Si non → page HTML "Tunnel hors ligne" (status 503).
3. Génère `request_id = uuid4().hex`.
4. Envoie `HTTP_REQUEST` au client via sa WS.
5. Attend la réponse (timeout 30 s par défaut, configurable).
6. Renvoie la réponse au visiteur (status, headers, body).
7. En cas de timeout → 504 Gateway Timeout.

**Gestion des requêtes simultanées** : chaque requête a un `request_id` unique ; le serveur maintient un dict `request_id → asyncio.Future` pour router les réponses.

## 5. Heartbeat & reconnexion

- Le serveur envoie `PING` toutes les 30 s.
- Le client doit répondre `PONG` sous 10 s, sinon le serveur ferme la WS (code 4001).
- Le client envoie `PING` au serveur toutes les 60 s (anti-spin-down Render) ; le serveur répond `PONG`.
- Reconnexion client : backoff exponentiel 1s→2s→4s→8s→16s→30s→60s (max).
- À chaque reconnexion, le client doit redemander un token frais (`POST /api/tunnels/token`) car le JWT expire en 5 min.

## 6. Limites

- `MAX_REQUEST_BYTES` : 5 Mo par défaut (configurable via env).
- `MAX_RESPONSE_BYTES` : 5 Mo par défaut.
- `HTTP_TIMEOUT_SECONDS` : 30 s par défaut.
- `MAX_TUNNELS_PER_USER` : 1 (un tunnel par utilisateur dans la v1).
- `MAX_WS_CONNECTIONS` : 200 (limite globale serveur).

## 7. Schéma DB (MySQL)

```sql
CREATE TABLE users (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  email        VARCHAR(255) NOT NULL UNIQUE,
  first_name   VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  is_admin     TINYINT(1) NOT NULL DEFAULT 0,
  is_suspended TINYINT(1) NOT NULL DEFAULT 0,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE tunnels (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id     BIGINT NOT NULL,
  tunnel_id   VARCHAR(32) NOT NULL UNIQUE,
  local_port  INT NULL,
  is_active   TINYINT(1) NOT NULL DEFAULT 0,
  last_seen_at DATETIME NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_tunnel_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE KEY uk_tunnel_id (tunnel_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE sessions (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id       BIGINT NOT NULL,
  session_token VARCHAR(64) NOT NULL UNIQUE,
  csrf_token    VARCHAR(64) NOT NULL,
  expires_at    DATETIME NOT NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ip_address    VARCHAR(45) NULL,
  user_agent    VARCHAR(255) NULL,
  CONSTRAINT fk_session_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE KEY uk_session_token (session_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE revoked_tokens (
  jti        VARCHAR(64) PRIMARY KEY,
  user_id    BIGINT NOT NULL,
  revoked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NOT NULL,
  CONSTRAINT fk_revoked_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE audit_logs (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id    BIGINT NULL,
  action     VARCHAR(64) NOT NULL,
  ip_address VARCHAR(45) NULL,
  details    TEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_audit_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_audit_created ON audit_logs(created_at DESC);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);
```

## 8. Variables d'environnement (Render)

```
DATABASE_URL=mysql+asyncmy://USER:PASS@mysql-COMPTE.alwaysdata.net:3306/COMPTE_db?charset=utf8mb4
BEN_TUNNEL_SECRET=<64 chars hex aléatoire>
ADMIN_EMAIL=admin@example.com
ADMIN_BOOTSTRAP_PASSWORD=<mot de passe initial admin>
ADMIN_BOOTSTRAP_FIRST_NAME=Admin
MAX_REQUEST_BYTES=5242880
MAX_RESPONSE_BYTES=5242880
HTTP_TIMEOUT_SECONDS=30
MAX_WS_CONNECTIONS=200
PUBLIC_BASE_URL=https://bentunnel.onrender.com
KEEPALIVE_URL=https://bentunnel.onrender.com/health
```
