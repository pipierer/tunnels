# Client BenTunnel sous Windows (PowerShell)

Ce document explique comment installer et utiliser le client BenTunnel sous Windows. La procédure est similaire sous Linux et macOS (remplacer PowerShell par Terminal).

## 1. Pré-requis

### 1.1 Installer Python 3.10 ou supérieur

1. Télécharger Python sur https://www.python.org/downloads/windows/
2. **Important pendant l'install** : cocher **"Add Python to PATH"** en bas de l'installeur.
3. Installer.

Vérifier dans PowerShell :
```powershell
python --version
# Python 3.12.x (ou supérieur)
```

### 1.2 Télécharger le client BenTunnel

Télécharger le dossier `client/` du projet BenTunnel (depuis GitHub ou le .zip fourni).

```powershell
# Exemple : extraire le .zip puis
cd bentunnel\client
dir
# bentunnel.py  config.py  authentication.py  http_forwarder.py  ws_client.py  requirements.txt
```

### 1.3 Installer les dépendances

```powershell
cd bentunnel\client
pip install -r requirements.txt
```

Vérifier :
```powershell
python bentunnel.py --help
# BenTunnel v1.0.0
# usage: bentunnel.py [-h] [--server URL] [--version] {login,start,stop,status,logout} ...
```

## 2. Premier usage : connexion

### 2.1 Créer un compte

Vous devez d'abord créer un compte sur le dashboard BenTunnel :
1. Ouvrir https://bentunnel.onrender.com/register
2. Remplir : prénom, email, mot de passe (8 caractères min).
3. Se connecter au dashboard.
4. Noter votre **tunnel_id** (ex. `alice`) et votre **URL publique** (ex. `https://bentunnel.onrender.com/tunnels/alice`).

### 2.2 Se connecter depuis le client

```powershell
python bentunnel.py login
```

Le client demande :
```
Email: alice@example.com
Password: ********
```

Si OK :
```
[12:34:56] [INFO] Connecting to https://bentunnel.onrender.com...
[12:34:57] [INFO] Login successful.
[12:34:57] [INFO] Tunnel: alice
[12:34:57] [INFO] Public URL: https://bentunnel.onrender.com/tunnels/alice
[12:34:57] [INFO] Credentials saved to C:\Users\you\.bentunnel\config.json
```

Les credentials sont stockés dans `~/.bentunnel/config.json` (permissions 0600).

> **Serveur différent** : si le serveur n'est pas `https://bentunnel.onrender.com`, utiliser `--server` :
> ```powershell
> python bentunnel.py login --server https://mon-serveur.onrender.com
> ```

## 3. Démarrer un tunnel

### 3.1 Préparer votre application locale

Lancez votre application sur un port local, par exemple 3000 :
```powershell
# Exemple : un serveur HTTP Python de test
python -m http.server 3000
```

Ou votre app Node, PHP, Flask, etc. — du moment qu'elle écoute sur `127.0.0.1:3000`.

### 3.2 Démarrer le tunnel

Dans un **autre** terminal PowerShell :

```powershell
python bentunnel.py start 3000
```

Sortie attendue :
```
[12:35:00] [INFO] Starting BenTunnel client v1.0.0
[12:35:00] [INFO] Tunnel: alice
[12:35:00] [INFO] Public URL: https://bentunnel.onrender.com/tunnels/alice
[12:35:00] [INFO] Forwarding to http://127.0.0.1:3000
[12:35:00] [INFO] Press Ctrl+C to stop.
[12:35:00] [INFO] Fetching tunnel token from https://bentunnel.onrender.com...
[12:35:01] [INFO] Connecting to tunnel server...
[12:35:01] [INFO] Connected to server (max_request=5242880, max_response=5242880)
```

### 3.3 Tester

Dans un navigateur (sur n'importe quel appareil), ouvrir :
```
https://bentunnel.onrender.com/tunnels/alice
```

Vous devez voir le contenu de votre `localhost:3000`.

### 3.4 Pendant que le tunnel tourne

- Le client affiche des logs en temps réel : `GET / 200`, `POST /api 500`, etc.
- Le client envoie un PING au serveur toutes les 60 s pour empêcher Render de se mettre en veille.
- Le serveur envoie un PING au client toutes les 30 s pour vérifier qu'il est toujours là.
- Si la connexion se coupe (réseau, Render restart), le client se reconnecte automatiquement avec un backoff exponentiel (1s → 2s → 4s → … → 60s).

### 3.5 Arrêter le tunnel

Dans le terminal où tourne `start` : **Ctrl+C**.

Ou depuis un autre terminal :
```powershell
python bentunnel.py stop
```

## 4. Commandes disponibles

### `login`
```powershell
python bentunnel.py login [--server URL]
```
Demande email + mot de passe, stocke les credentials.

### `start PORT`
```powershell
python bentunnel.py start 3000 [--server URL]
```
Démarre le tunnel vers `localhost:PORT`. Tourne jusqu'à Ctrl+C.

### `stop`
```powershell
python bentunnel.py stop
```
Arrête le tunnel en cours (envoie SIGTERM au processus `start` via le PID file).

### `status`
```powershell
python bentunnel.py status
```
Affiche : utilisateur connecté, tunnel_id, URL publique, statut live (online/offline), port local.

### `logout`
```powershell
python bentunnel.py logout
```
Efface les credentials stockés localement.

### `--help`
```powershell
python bentunnel.py --help
python bentunnel.py start --help
```

## 5. Fichier de configuration

Le client stocke ses infos dans `~/.bentunnel/config.json` :

```json
{
  "server_url": "https://bentunnel.onrender.com",
  "session_cookie": "abc123...",
  "csrf_token": "def456...",
  "user_email": "alice@example.com",
  "tunnel_id": "alice",
  "public_url": "https://bentunnel.onrender.com/tunnels/alice"
}
```

Le fichier a les permissions `0600` (lecture/écriture propriétaire uniquement). Le dossier `~/.bentunnel/` a les permissions `0700`.

Pour réinitialiser complètement :
```powershell
python bentunnel.py logout
# ou supprimer le dossier
Remove-Item -Recurse -Force $HOME\.bentunnel
```

## 6. Diagnostic des erreurs

### `Cannot reach server`
- Vérifier la connexion internet.
- Vérifier que le serveur Render est up : `https://bentunnel.onrender.com/health` dans un navigateur.
- Si le serveur est en cold start, patienter 1 min.

### `Email ou mot de passe invalide`
- Vérifier les credentials.
- Après 5 tentatives, le compte est verrouillé 5 min (rate limit).

### `Compte suspendu`
- L'admin a suspendu le compte. Contacter l'admin.

### `Token CSRF invalide`
- Le fichier `config.json` est corrompu. Faire `logout` puis `login`.

### `Local server not reachable` (visible dans les logs serveur)
- Le port indiqué n'a pas d'application en écoute.
- Lancer d'abord votre app sur `localhost:PORT`, puis `start PORT`.

### `Connection failed: WebSocket ...`
- Render est en redémarrage. Le client se reconnecte automatiquement.
- Si ça persiste, vérifier les logs serveur.

### Le tunnel est "offline" alors que le client tourne
- Le serveur Render est en spin-down ET le client n'a pas réussi à se reconnecter.
- Redémarrer le client (Ctrl+C puis `start PORT`).
- Vérifier que le cron keep-alive hit bien `/health`.

## 7. Sous Linux / macOS

Les commandes sont identiques. Remplacer :
- `python` par `python3` (sur la plupart des distros Linux).
- `Remove-Item` par `rm -rf`.
- Le fichier de config est dans `~/.bentunnel/` (même chemin).

```bash
sudo apt install python3 python3-pip  # Debian/Ubuntu
cd bentunnel/client
pip3 install -r requirements.txt --user
python3 bentunnel.py login
python3 bentunnel.py start 3000
```

## 8. Automatisation (optionnel)

### Démarrer le tunnel au boot (Windows)

Créer un script `start-tunnel.bat` :
```bat
@echo off
cd C:\chemin\vers\bentunnel\client
python bentunnel.py start 3000
```

Puis : Win+R → `shell:startup` → coller un raccourci vers `start-tunnel.bat`.

### Service systemd (Linux)

Créer `/etc/systemd/system/bentunnel.service` :
```ini
[Unit]
Description=BenTunnel client
After=network.target

[Service]
Type=simple
User=alice
WorkingDirectory=/home/alice/bentunnel/client
ExecStart=/usr/bin/python3 bentunnel.py start 3000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bentunnel
sudo systemctl status bentunnel
```
