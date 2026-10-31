# Déploiement sur Render Free

Ce document décrit le déploiement du serveur BenTunnel sur Render Free.

## Pré-requis

- La base MySQL sur Alwaysdata est créée (voir [`DEPLOYMENT_ALWAYSDATA.md`](DEPLOYMENT_ALWAYSDATA.md)).
- Vous avez l'URL `DATABASE_URL` au format `mysql+asyncmy://...`.
- Vous avez un compte Render (https://render.com, inscription gratuite).
- Le code BenTunnel est sur GitHub (un dépôt public ou privé).

## 1. Pousser le code sur GitHub

```bash
cd bentunnel
git init
git add .
git commit -m "BenTunnel v1.0"
git branch -M main
git remote add origin https://github.com/VOTRE_USER/bentunnel.git
git push -u origin main
```

## 2. Créer le Web Service sur Render

1. Aller sur https://dashboard.render.com → **New +** → **Web Service**.
2. Connecter votre repo GitHub `bentunnel`.
3. Remplir :
   - **Name** : `bentunnel` (le sous-domaine sera `bentunnel.onrender.com`)
   - **Runtime** : Python 3
   - **Build Command** : `pip install -r relay/requirements.txt`
   - **Start Command** : `uvicorn relay.main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type** : Free
4. Ouvrir **Advanced** :
   - **Auto-Deploy** : Yes
   - **Health Check Path** : `/health` (Render va ping cette URL pour vérifier que le service démarre)
5. Ne pas créer encore — d'abord configurer les variables d'environnement (étape suivante).

### ⚠️ IMPORTANT : Pinner Python à 3.12

Render utilise **Python 3.14** par défaut, mais `pydantic-core==2.27.2` (dépendance de `pydantic==2.10.4`) n'a pas de wheel précompilée pour Python 3.14. Le build échouera avec une erreur du type :

```
error: failed to create directory `/usr/local/cargo/registry/cache/...`
Caused by: Read-only file system (os error 30)
💥 maturin failed
```

Pour éviter ça, **deux mécanismes redondants** sont en place dans le projet :

1. **Fichier `runtime.txt` à la racine du repo** contenant `3.12.7` — Render le lit automatiquement au build.
2. **Variable d'environnement `PYTHON_VERSION=3.12.7`** dans `render.yaml` (section `envVars`).

Si vous créez le service manuellement (sans `render.yaml`), ajoutez vous-même la variable `PYTHON_VERSION=3.12.7` dans l'onglet **Environment** avant le premier build.

## 3. Configurer les variables d'environnement

Dans la section **Environment** du service Render, ajouter les variables suivantes (voir `.env.example` pour le détail) :

### Obligatoires

| Variable | Valeur |
|---|---|
| `DATABASE_URL` | `mysql+asyncmy://USER:PASS@mysql-COMPTE.alwaysdata.net:3306/COMPTE_bentunnel?charset=utf8mb4` (de Alwaysdata) |
| `BEN_TUNNEL_SECRET` | 64 chars hex aléatoires. **Générer avec** : `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_EMAIL` | `admin@example.com` (votre email admin) |
| `ADMIN_BOOTSTRAP_PASSWORD` | Un mot de passe fort temporaire (à changer après le 1er login) |
| `ADMIN_BOOTSTRAP_FIRST_NAME` | `Admin` |
| `PUBLIC_BASE_URL` | `https://bentunnel.onrender.com` (ou votre sous-domaine Render) |

### Optionnelles (valeurs par défaut indiquées)

| Variable | Défaut |
|---|---|
| `MAX_REQUEST_BYTES` | `5242880` (5 Mo) |
| `MAX_RESPONSE_BYTES` | `5242880` (5 Mo) |
| `HTTP_TIMEOUT_SECONDS` | `30` |
| `MAX_WS_CONNECTIONS` | `200` |
| `SESSION_TTL_DAYS` | `7` |
| `TUNNEL_TOKEN_TTL_SECONDS` | `300` (5 min) |
| `SESSION_COOKIE_NAME` | `bentunnel_session` |
| `CSRF_COOKIE_NAME` | `bentunnel_csrf` |
| `KEEPALIVE_URL` | `https://bentunnel.onrender.com/health` |

> **Tip Render** : cochez **Secret** pour `DATABASE_URL`, `BEN_TUNNEL_SECRET`, `ADMIN_BOOTSTRAP_PASSWORD` afin qu'elles soient masquées dans l'interface.

## 4. Déclencher le premier déploiement

1. Cliquer **Create Web Service**.
2. Render lance le build (installe les dépendances via `pip install -r relay/requirements.txt`).
3. Render lance la start command (`uvicorn relay.main:app --host 0.0.0.0 --port $PORT`).
4. Au premier démarrage, `init_db()` crée les tables MySQL chez Alwaysdata.
5. `bootstrap_admin()` crée le compte admin avec `ADMIN_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD`.
6. Render ping `/health` → si 200, le service est **Live**.

Vérifier dans les **Logs** Render :
```
INFO bentunnel.main Starting BenTunnel server…
INFO bentunnel.db Database initialized (5 tables created)
INFO bentunnel.db Admin user bootstrap: admin@example.com
INFO uvicorn.error Application startup complete.
```

## 5. Tester

1. Ouvrir `https://bentunnel.onrender.com` → la page d'accueil BenTunnel doit s'afficher.
2. Cliquer **Connexion** → se loguer avec `ADMIN_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD`.
3. Changer le mot de passe admin (via le panneau admin → reset-password sur votre propre compte).
4. Créer un compte utilisateur de test via **Inscription**.
5. Noter le `tunnel_id` affiché sur le dashboard.

## 6. Configurer le cron keep-alive (TRÈS IMPORTANT)

Render Free met le service en veille après 15 min sans trafic. Pour éviter le cold start (~1 min) lors de la première visite, configurez un cron externe qui hit `/health` toutes les 10 min.

### Option A : cron-job.org (gratuit, recommandé)

1. Aller sur https://cron-job.org/en/ → créer un compte gratuit.
2. **Create Cronjob** :
   - **Title** : `BenTunnel keepalive`
   - **URL** : `https://bentunnel.onrender.com/health`
   - **Execution Schedule** : Every 10 minutes
   - **Request Method** : GET
3. Sauvegarder. Le cron va ping votre service toutes les 10 min → le service ne dort jamais.

### Option B : UptimeRobot (gratuit)

1. Aller sur https://uptimerobot.com → créer un compte gratuit (50 monitors gratuits).
2. **Add New Monitor** :
   - **Monitor Type** : HTTP(s)
   - **Friendly Name** : `BenTunnel`
   - **URL** : `https://bentunnel.onrender.com/health`
   - **Monitoring Interval** : 10 minutes
4. Sauvegarder.

> **Note coût Render** : garder le service éveillé 24/7 consomme ~744 h/750 h par mois. C'est juste sous la limite. Si vous dépassez (mois de 31 jours), le service sera suspendu temporairement. Pour ne pas risquer ça, vous pouvez laisser le service dormir la nuit (cron 06:00-22:00 uniquement) — au prix d'un cold start le matin.

## 7. (Optionnel) Configurer un domaine personnalisé

Si vous voulez `https://tunnel.mondomaine.fr` au lieu de `https://bentunnel.onrender.com` :

1. Render → votre service → **Settings** → **Custom Domains** → **Add Custom Domain**.
2. Render vous donne un CNAME à créer (ex. `bentunnel.onrender.com`).
3. Chez votre registrar DNS, ajouter : `tunnel CNAME bentunnel.onrender.com.`
4. Render génère automatiquement un certificat Let's Encrypt.
5. Une fois validé, mettre à jour `PUBLIC_BASE_URL=https://tunnel.mondomaine.fr` dans les variables d'environnement.
6. Redéployer.

## 8. Surveillance

- **Logs** : Render → votre service → **Logs**. Tous les logs FastAPI y vont.
- **Métriques** : Render Free ne donne pas de métriques détaillées, mais les logs suffisent.
- **Pings keep-alive** : vérifier que le cron-job.org ou UptimeRobot renvoie bien 200 toutes les 10 min.
- **DB Alwaysdata** : via phpMyAdmin, vérifier le nombre de users/tunnels/sessions.

## 9. Mise à jour

Pour mettre à jour le serveur :

```bash
# Local
git pull  # ou git push après modifications
# Render redéploie automatiquement si Auto-Deploy est ON
```

Pour forcer un redéploiement sans changer le code : Render → **Manual Deploy** → **Deploy latest commit**.

## 10. Que faire en cas de problème

### Le service ne démarre pas

- Vérifier les logs Render. Erreurs courantes :
  - `init_db()` échoue → `DATABASE_URL` incorrecte ou Alwaysdata injoignable. Tester la connexion MySQL depuis votre PC.
  - `bootstrap_admin()` échoue → `ADMIN_EMAIL` déjà pris par un user non-admin. Supprimer ce user en DB ou changer `ADMIN_EMAIL`.
  - `ModuleNotFoundError` → build command n'a pas installé un paquet. Vérifier `relay/requirements.txt`.

### Le service démarre mais `/health` renvoie 503

- Vérifier que la DB est accessible depuis Render.
- Le lifespan FastAPI catche les erreurs d'init et continue — donc le service peut démarrer même si la DB est down, mais les routes renverront 500.

### Les WebSockets ne se connectent pas

- Vérifier que `PUBLIC_BASE_URL` est bien en `https://` (le client en déduit `wss://`).
- Vérifier que le token JWT est valide (logs côté client : `Auth error: ...`).
- Vérifier les logs serveur pour `verify_tunnel_token` errors.

### Le service se met en veille malgré le cron

- Vérifier que le cron-job.org ou UptimeRobot hit bien `https://bentunnel.onrender.com/health` (pas juste `/`).
- Vérifier les logs Render : on doit voir un `GET /health 200` toutes les 10 min.
